"""Anthropic transport/budget fixtures. No real key or external call is used.

python -m unittest discover -s tests -p 'test_anthropic_adapter.py' -v
"""
import copy
from datetime import datetime, timezone
from email.message import Message
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import urllib.response
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import APIError, AuroraService, ModelAdapter

MODEL = 'claude-haiku-4-5-20251001'
FAKE_KEY = 'TEST-ONLY-NOT-A-REAL-KEY'


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 9, 12, tzinfo=timezone.utc)
        return value if tz else value.replace(tzinfo=None)


class AnthropicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='anthropic-fixtures-')
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            'AURORA_AI_PROVIDER': 'anthropic', 'AURORA_AI_API_KEY': FAKE_KEY,
            'AURORA_AI_MODEL': MODEL, 'AURORA_AI_ALLOW_NETWORK': '1',
        }, clear=True)
        env.start(); self.addCleanup(env.stop)
        clock = patch('backend.datetime', FrozenDateTime)
        clock.start(); self.addCleanup(clock.stop)
        for handler, method in ((urllib.request.HTTPSHandler, 'https_open'), (urllib.request.HTTPHandler, 'http_open')):
            network = patch.object(handler, method, side_effect=AssertionError('External network is forbidden'))
            network.start(); self.addCleanup(network.stop)
        self.service = AuroraService(db_path=Path(self.tmp.name) / 'state.sqlite3')
        self.case = self.service.detail('CASE-001')
        self.sequence = 0
        self.adapter = self.new_adapter()
        self.service.ai = self.adapter

    def new_adapter(self, shared=None, env=None):
        self.sequence += 1
        with patch.dict(os.environ, env or {}):
            return ModelAdapter(budget_db_path=shared or Path(self.tmp.name) / f'budget-{self.sequence}.sqlite3')

    def brief(self):
        return {'summary': '확정 불량률 11.87%, 회수 기준 10%, 생산 가동률 97%입니다.',
                'risks': ['원본과 대조하고 담당자 검토가 필요합니다.'], 'alternatives': [],
                'evidence_ids': [self.case['analysis']['evidence'][0]['record_id']],
                'facts': {**{k: self.case['analysis'][k] for k in
                            ('confirmed_qty', 'produced_qty', 'defect_rate_pct', 'recall_eligible')},
                          'recall_threshold_pct': 10}}

    def envelope(self, brief=None):
        return {'id': 'msg_TEST_ONLY', 'type': 'message', 'role': 'assistant', 'model': MODEL,
                'stop_reason': 'tool_use', 'usage': {'input_tokens': 1200, 'output_tokens': 400},
                'content': [{'type': 'tool_use', 'id': 'toolu_TEST_ONLY',
                             'name': ModelAdapter.BRIEF_TOOL, 'input': brief if brief is not None else self.brief()}]}

    def transport(self, result=None, count=1200, raw=None):
        def respond(request, timeout):
            self.assertEqual(timeout, 30)
            if request.full_url.endswith('/count_tokens'):
                return io.BytesIO(json.dumps({'input_tokens': count}).encode())
            return io.BytesIO(raw if raw is not None else json.dumps(result or self.envelope()).encode())
        return respond

    def error(self, code, callback):
        with self.assertRaises(APIError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def reservations(self, adapter=None):
        adapter = adapter or self.adapter
        with sqlite3.connect(adapter.budget_db_path) as db:
            return db.execute('SELECT COUNT(*), SUM(reserved_microusd) FROM ai_reservations').fetchone()

    def test_network_errors_are_classified_without_secret_details(self):
        import errno
        import socket
        import ssl
        cases = [
            (OSError(errno.ENETUNREACH, FAKE_KEY), 'AI_NETWORK_UNREACHABLE'),
            (socket.gaierror(-2, FAKE_KEY), 'AI_DNS_FAILED'),
            (ssl.SSLError(FAKE_KEY), 'AI_TLS_FAILED'),
            (TimeoutError(FAKE_KEY), 'AI_NETWORK_TIMEOUT'),
            (OSError(errno.ECONNREFUSED, FAKE_KEY), 'AI_NETWORK_FAILED'),
        ]
        for reason, expected in cases:
            with self.subTest(code=expected):
                adapter = self.new_adapter()
                with patch.object(adapter.opener, 'open', side_effect=urllib.error.URLError(reason)) as transport:
                    self.error(expected, lambda: adapter.generate(self.case))
                self.assertEqual(transport.call_count, 1)
                self.assertFalse(adapter.status()['connected'])
                self.assertEqual(self.reservations(adapter), (1, 210240))

    def test_native_payload_headers_and_validated_result(self):
        self.assertFalse(self.adapter.status()['connected'])
        with patch.object(self.adapter.opener, 'open', side_effect=self.transport()) as network:
            result = self.adapter.generate(self.case)
        self.assertEqual(network.call_count, 2)
        count_request, request = (call.args[0] for call in network.call_args_list)
        self.assertEqual(count_request.full_url, ModelAdapter.ANTHROPIC_URL + '/count_tokens')
        self.assertEqual(request.full_url, ModelAdapter.ANTHROPIC_URL)
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers['x-api-key'], FAKE_KEY)
        self.assertEqual(headers['anthropic-version'], '2023-06-01')
        self.assertNotIn('authorization', headers)
        payload = json.loads(request.data)
        self.assertEqual(payload['max_tokens'], 2048)
        self.assertEqual(payload['service_tier'], 'standard_only')
        self.assertEqual([m['role'] for m in payload['messages']], ['user'])
        self.assertEqual(payload['tool_choice'], {'type': 'tool', 'name': ModelAdapter.BRIEF_TOOL, 'disable_parallel_tool_use': True})
        self.assertEqual(payload['tools'][0]['input_schema'], ModelAdapter.provider_brief_schema())
        self.assertNotIn(FAKE_KEY, request.data.decode())
        self.assertNotIn('cache_control', payload)
        self.assertNotIn('thinking', payload)
        self.assertEqual(json.loads(payload['messages'][0]['content']), ModelAdapter.input_snapshot(self.case))
        self.assertEqual(set(json.loads(count_request.data)), {'model', 'system', 'messages', 'tools', 'tool_choice'})
        self.assertEqual(result['provider'], 'Anthropic')
        self.assertEqual(result['reserved_cost_usd'], 0.21024)
        self.assertEqual(result['usage'], {'input_tokens': 1200, 'output_tokens': 400})
        self.assertTrue(result['canonical_facts_validated'])
        self.assertTrue(result['human_review_required'])
        self.assertEqual(result['approval_effect'], 'none')
        self.assertEqual(json.loads(result['content']), result['structured'])
        self.assertTrue(self.adapter.status()['connected'])
        self.assertNotIn(FAKE_KEY, json.dumps(self.adapter.status()))
        self.assertEqual(self.reservations(), (1, 210240))

    def test_opt_in_key_and_model_are_all_required(self):
        for name in ('AURORA_AI_ALLOW_NETWORK', 'AURORA_AI_API_KEY', 'AURORA_AI_MODEL'):
            with self.subTest(name=name):
                adapter = self.new_adapter(env={name: ''})
                with patch.object(adapter.opener, 'open') as network:
                    self.error('AI_NOT_CONFIGURED', lambda: adapter.generate(self.case))
                    network.assert_not_called()

    def test_provider_specific_fallback_never_reuses_openai_key_or_url(self):
        with patch.dict(os.environ, {'AURORA_AI_PROVIDER': 'anthropic', 'AURORA_AI_ALLOW_NETWORK': '1',
                                   'LLM_API_KEY': 'TEST-OPENAI', 'LLM_MODEL': 'TEST-OPENAI-MODEL',
                                   'LLM_BASE_URL': 'https://other.test/v1'}, clear=True):
            adapter = self.new_adapter()
            self.assertFalse(adapter.status()['configured'])
            self.assertEqual(adapter.url, ModelAdapter.ANTHROPIC_URL)
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': FAKE_KEY, 'ANTHROPIC_MODEL': MODEL}):
                adapter = self.new_adapter()
                self.assertTrue(adapter.status()['configured'])
                self.assertEqual(adapter.key, FAKE_KEY)

    def test_invalid_configuration_and_unknown_price_fail_before_network(self):
        invalid = [{'AURORA_AI_PROVIDER': 'unknown'}, {'AURORA_AI_ENDPOINT': 'https://other.test/messages'},
                   {'AURORA_AI_ENDPOINT': 'http://api.anthropic.com/v1/messages'},
                   {'AURORA_AI_ENDPOINT': ModelAdapter.ANTHROPIC_URL + '?key=bad'},
                   {'AURORA_AI_ENDPOINT': 'https://user:pass@api.anthropic.com/v1/messages'},
                   {'AURORA_AI_ENDPOINT': '\n' + ModelAdapter.ANTHROPIC_URL},
                   {'AURORA_AI_MAX_OUTPUT_TOKENS': '2049'}, {'AURORA_AI_MAX_REQUESTS': '4'},
                   {'AURORA_AI_MAX_INPUT_TOKENS': '8193'}, {'AURORA_AI_MAX_CONCURRENT': '0'},
                   {'AURORA_AI_BUDGET_USD': '1.01'}, {'AURORA_AI_BUDGET_USD': 'NaN'},
                   {'AURORA_AI_BUDGET_USD': '-1'}, {'AURORA_AI_API_KEY': 'fake\nheader'}]
        for env in invalid:
            with self.subTest(env=env):
                adapter = self.new_adapter(env=env)
                with patch.object(adapter.opener, 'open') as network:
                    self.error('AI_CONFIGURATION_INVALID', lambda: adapter.generate(self.case))
                    network.assert_not_called()
        adapter = self.new_adapter(env={'AURORA_AI_MODEL': 'claude-unknown-or-alias'})
        with patch.object(adapter.opener, 'open') as network:
            self.error('AI_PRICING_UNKNOWN', lambda: adapter.generate(self.case))
            network.assert_not_called()

    def test_expiry_and_safety_margin_fail_closed(self):
        for instant in ('2026-11-11T23:59:00+00:00', '2026-11-12T00:00:00+00:00', '2026-12-01T00:00:00+00:00'):
            adapter = self.new_adapter()
            with patch('backend.datetime') as clock, patch.object(adapter.opener, 'open') as network:
                clock.now.return_value = datetime.fromisoformat(instant)
                self.error('AI_CREDIT_EXPIRED', lambda: adapter.generate(self.case))
                network.assert_not_called()

    def test_expiry_rechecked_after_token_count(self):
        def respond(request, timeout):
            clock.now.return_value = datetime(2026, 11, 11, 23, 59, 31, tzinfo=timezone.utc)
            return io.BytesIO(b'{"input_tokens":1200}')
        with patch('backend.datetime') as clock, patch.object(self.adapter.opener, 'open', side_effect=respond) as network:
            clock.now.return_value = datetime(2026, 11, 9, tzinfo=timezone.utc)
            self.error('AI_CREDIT_EXPIRED', lambda: self.adapter.generate(self.case))
            self.assertEqual(network.call_count, 1)
        self.assertEqual(self.reservations(), (1, 210240))

    def test_input_token_estimate_margin_and_request_bytes_are_bounded(self):
        for count in (7169, 8192, True, -1, 2.5):
            adapter = self.new_adapter()
            code = 'AI_INPUT_TOO_LARGE' if type(count) is int and count >= 7169 else 'AI_CALL_FAILED'
            with self.subTest(count=count), patch.object(adapter.opener, 'open', side_effect=self.transport(count=count)) as network:
                self.error(code, lambda: adapter.generate(self.case))
                self.assertEqual(network.call_count, 1)
        case = copy.deepcopy(self.case)
        case['conditions']['large_fixture'] = 'X' * 32768
        with patch.object(self.adapter.opener, 'open') as network:
            self.error('AI_INPUT_TOO_LARGE', lambda: self.adapter.generate(case))
            network.assert_not_called()

    def test_schema_and_structured_facts_checks_apply_to_anthropic(self):
        mutations = [{'facts': {**self.brief()['facts'], 'confirmed_qty': True}},
                     {'risks': 'not an array'}, {'evidence_ids': ['NOT-REAL']},
                     {'alternatives': ['a'] * 4}, {'extra': 'not allowed'},
                     {'facts': {**self.brief()['facts'], 'defect_rate_pct': float('nan')}}]
        for mutation in mutations:
            adapter = self.new_adapter()
            with self.subTest(mutation=mutation), patch.object(adapter.opener, 'open', side_effect=self.transport(self.envelope({**self.brief(), **mutation}))):
                self.error('AI_CALL_FAILED', lambda: adapter.generate(self.case))
                self.assertFalse(adapter.verified)

    def test_refusal_truncation_wrong_tool_multiple_blocks_and_usage_fail(self):
        valid = self.envelope()
        mutations = [{'stop_reason': 'max_tokens'}, {'stop_reason': 'refusal'}, {'type': 'error'},
                     {'model': 'different-model'}, {'content': []}, {'content': valid['content'] * 2},
                     {'content': [{'type': 'text', 'text': json.dumps(self.brief())}]},
                     {'content': [{**valid['content'][0], 'name': 'execute_action'}]},
                     {'usage': {'input_tokens': 8193, 'output_tokens': 10}},
                     {'usage': {'input_tokens': 100, 'output_tokens': 2049}},
                     {'usage': {'input_tokens': True, 'output_tokens': 10}}]
        for mutation in mutations:
            adapter = self.new_adapter()
            with self.subTest(mutation=mutation), patch.object(adapter.opener, 'open', side_effect=self.transport({**valid, **mutation})):
                self.error('AI_CALL_FAILED', lambda: adapter.generate(self.case))
                self.assertFalse(adapter.verified)

    def test_duplicate_json_oversized_and_malformed_responses_fail(self):
        for raw in (b'{"type":"message","type":"error"}', b'{}' + b' ' * ModelAdapter.MAX_RESPONSE_BYTES,
                    b'not-json', b'[]', b'null', b'{"type":NaN}', b'\xff'):
            adapter = self.new_adapter()
            with self.subTest(raw=raw[:80]), patch.object(adapter.opener, 'open', side_effect=self.transport(raw=raw)):
                self.error('AI_CALL_FAILED', lambda: adapter.generate(self.case))

    def test_provider_failures_are_sanitized_not_retried_and_keep_reservation(self):
        cases = [(401, 'AI_AUTH_FAILED'), (403, 'AI_AUTH_FAILED'), (429, 'AI_RATE_LIMITED'),
                 (402, 'AI_QUOTA_EXCEEDED'), (529, 'AI_PROVIDER_UNAVAILABLE'), (500, 'AI_PROVIDER_UNAVAILABLE'),
                 (400, 'AI_CALL_FAILED')]
        for status, code in cases:
            adapter = self.new_adapter()
            failure = urllib.error.HTTPError(adapter.url, status, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
            with self.subTest(status=status), patch.object(adapter.opener, 'open', side_effect=failure) as network:
                self.error(code, lambda: adapter.generate(self.case))
                self.assertEqual(network.call_count, 1)
                self.assertEqual(self.reservations(adapter), (1, 210240))
                self.assertEqual(adapter.status()['usage_limits']['active_requests'], 0)

    def test_all_redirects_block_credentials_and_data_on_both_endpoints(self):
        for stage in ('count', 'generate'):
            for code in (301, 302, 303, 307, 308):
                adapter = self.new_adapter()
                headers = Message(); headers['Location'] = 'https://other.test/steal'
                redirect = urllib.response.addinfourl(io.BytesIO(b''), headers, adapter.url, code)
                redirect.msg = 'Fixture redirect'
                responses = [redirect] if stage == 'count' else [
                    urllib.response.addinfourl(io.BytesIO(b'{"input_tokens":1200}'), Message(), adapter.url + '/count_tokens', 200), redirect]
                responses[0].msg = 'Fixture'
                with self.subTest(stage=stage, code=code), patch.object(urllib.request.HTTPSHandler, 'https_open', side_effect=responses) as network:
                    self.error('AI_CALL_FAILED', lambda: adapter.generate(self.case))
                    self.assertEqual(network.call_count, len(responses))
                    self.assertFalse(adapter.verified)

    def test_budget_survives_new_instances_and_reserves_maximum_not_observed_tokens(self):
        ledger = Path(self.tmp.name) / 'shared-budget.sqlite3'
        for _ in range(3):
            adapter = self.new_adapter(shared=ledger)
            with patch.object(adapter.opener, 'open', side_effect=self.transport()):
                adapter.generate(self.case)
        self.assertEqual(self.reservations(adapter), (3, 630720))
        adapter = self.new_adapter(shared=ledger)
        usage = adapter.status()['usage_limits']
        self.assertEqual(usage['requests_attempted'], 0)
        self.assertEqual(usage['total_attempts_reserved'], 3)
        self.assertEqual(usage['total_cost_reserved_usd'], 0.63072)
        with patch.object(adapter.opener, 'open') as network:
            self.error('AI_USAGE_LIMIT', lambda: adapter.generate(self.case))
            network.assert_not_called()

    def test_low_budget_and_unwritable_ledger_fail_before_network(self):
        adapter = self.new_adapter(env={'AURORA_AI_BUDGET_USD': '0.21'})
        with patch.object(adapter.opener, 'open') as network:
            self.error('AI_USAGE_LIMIT', lambda: adapter.generate(self.case))
            network.assert_not_called()
        adapter = self.new_adapter(shared=Path(self.tmp.name))  # A directory is not a database.
        with patch.object(adapter.opener, 'open') as network:
            self.error('AI_BUDGET_UNAVAILABLE', lambda: adapter.generate(self.case))
            network.assert_not_called()
        self.assertFalse(adapter.status()['usage_limits']['ledger_readable'])

    def test_concurrent_instances_cannot_overspend_single_remaining_reservation(self):
        ledger = Path(self.tmp.name) / 'race-budget.sqlite3'
        adapters = [self.new_adapter(shared=ledger, env={'AURORA_AI_BUDGET_USD': '0.22'}) for _ in range(2)]
        start = threading.Barrier(2)
        def call(adapter):
            with patch.object(adapter.opener, 'open', side_effect=self.transport()):
                start.wait(5)
                try:
                    return adapter.generate(self.case)['provider']
                except APIError as error:
                    return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(call, adapters))
        self.assertCountEqual(outcomes, ['Anthropic', 'AI_USAGE_LIMIT'])
        self.assertEqual(self.reservations(adapters[0]), (1, 210240))

    def test_concurrency_limit_releases_after_failure(self):
        adapter = self.new_adapter(env={'AURORA_AI_MAX_CONCURRENT': '1'})
        entered, release = threading.Event(), threading.Event()
        def blocked(request, timeout):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Fixture did not release')
            raise TimeoutError('fixture')
        with patch.object(adapter.opener, 'open', side_effect=blocked) as network:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(adapter.generate, self.case)
                try:
                    self.assertTrue(entered.wait(5))
                    self.error('AI_BUSY', lambda: adapter.generate(self.case))
                finally:
                    release.set()
                self.error('AI_NETWORK_TIMEOUT', lambda: first.result(timeout=5))
            self.assertEqual(network.call_count, 1)
        self.assertEqual(adapter.status()['usage_limits']['active_requests'], 0)
        with patch.object(adapter.opener, 'open', side_effect=self.transport()):
            self.assertEqual(adapter.generate(self.case)['provider'], 'Anthropic')

    def test_failure_preserves_workflow_and_duplicate_success_calls_once(self):
        before = self.service.detail('CASE-001')
        with patch.object(self.adapter.opener, 'open', side_effect=self.transport(self.envelope({**self.brief(), 'facts':{**self.brief()['facts'],'recall_threshold_pct':97}}))):
            self.error('AI_CALL_FAILED', lambda: self.service.mutate('/api/cases/CASE-001/ai-brief', {'expected_version': 1}, 'quality', 'bad'))
        after = self.service.detail('CASE-001')
        for field in ('version', 'status', 'recall_authorized', 'tasks', 'ai_brief'):
            self.assertEqual(before[field], after[field])
        with patch.object(self.adapter.opener, 'open', side_effect=self.transport()) as network:
            first = self.service.mutate('/api/cases/CASE-001/ai-brief', {'expected_version': 1}, 'quality', 'same')
            replay = self.service.mutate('/api/cases/CASE-001/ai-brief', {'expected_version': 1}, 'quality', 'same')
            self.assertEqual(first, replay)
            self.assertEqual(network.call_count, 2)  # Count + generation, no replay charge.
        self.assertEqual(self.reservations(), (2, 420480))
        self.assertTrue(self.service.verify_audit()['valid'])
        self.assertEqual(self.service._ai_inflight, {})


if __name__ == '__main__':
    unittest.main()
