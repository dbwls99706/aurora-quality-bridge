"""Strict-provider schema and privacy-preserving exact diagnostics, offline only."""
import copy
import json
import unittest
from unittest.mock import Mock, patch
import test_live_case_diagnostics as recorded
from backend import APIError, ModelAdapter, dumps, safe_ai_diagnostic


class StrictSchemaTests(unittest.TestCase):
    setUp = recorded.RecordedCaseDiagnosticTests.setUp
    new_adapter = recorded.RecordedCaseDiagnosticTests.new_adapter
    brief = recorded.RecordedCaseDiagnosticTests.brief
    envelope = recorded.RecordedCaseDiagnosticTests.envelope
    transport = recorded.RecordedCaseDiagnosticTests.transport
    snapshot = recorded.RecordedCaseDiagnosticTests.snapshot

    def diagnostic(self, structured):
        adapter = self.new_adapter()
        result = self.envelope()
        result['content'][0]['input'] = structured
        with patch.object(adapter.opener, 'open', side_effect=self.transport(result)), self.assertRaises(APIError) as caught:
            adapter.generate(self.case)
        self.assertFalse(adapter.verified)
        self.assertEqual(adapter._active, 0)
        return caught.exception.diagnostic

    def test_supported_subset_strict_count_parity_and_original_immutable(self):
        original = copy.deepcopy(ModelAdapter.BRIEF_SCHEMA)
        subset = ModelAdapter.provider_brief_schema()
        removed = []
        def compare(a, b):
            if isinstance(a, dict):
                for key, value in a.items():
                    if key in ('minLength', 'maxLength', 'maxItems'):
                        self.assertNotIn(key, b); removed.append(key)
                    else:
                        compare(value, b[key])
            elif isinstance(a, list):
                for x, y in zip(a, b): compare(x, y)
                self.assertEqual(len(a), len(b))
            else: self.assertEqual(a, b)
        compare(original, subset)
        self.assertEqual(len(removed), 9)
        self.assertEqual((len(dumps(original)), len(dumps(subset))), (846, 713))
        self.assertEqual(subset['properties']['evidence_ids']['minItems'], 1)
        self.assertEqual(subset['properties']['facts']['properties']['recall_threshold_pct']['enum'], [10])
        self.assertEqual(subset['properties']['facts']['properties']['defect_rate_pct']['type'], ['number', 'null'])
        subset['properties']['summary']['type'] = 'BAD'
        self.assertEqual(ModelAdapter.BRIEF_SCHEMA, original)
        before = self.snapshot()
        with patch.object(self.adapter.opener, 'open', side_effect=self.transport(count=7133)) as calls:
            result = self.adapter.generate(self.case)
        count, message = [json.loads(c.args[0].data) for c in calls.call_args_list]
        self.assertEqual(count['tools'], message['tools'])
        self.assertEqual(count['messages'], message['messages'])
        self.assertEqual(count['system'], message['system'])
        self.assertIs(message['tools'][0]['strict'], True)
        self.assertNotIn('strict', message['tools'][0]['input_schema'])
        self.assertEqual(message['tools'][0]['input_schema'], ModelAdapter.provider_brief_schema())
        self.assertEqual(ModelAdapter.BRIEF_SCHEMA, original)
        transmitted = json.loads(message['messages'][0]['content'])
        self.assertEqual(transmitted, ModelAdapter.input_snapshot(self.case))
        self.assertEqual(len(transmitted['analysis']['evidence']), len(self.case['analysis']['evidence']))
        self.assertEqual(transmitted['workflow_context']['status'], 'approved')
        self.assertEqual(result['structured']['facts']['defect_rate_pct'], 11.874357)
        self.assertEqual(before, self.snapshot())

    def test_top_level_types_and_private_unknown_keys(self):
        for value, kind in [(None, 'null'), ([], 'array'), ('SECRET', 'string'), (True, 'boolean'), (5, 'number')]:
            with self.subTest(kind=kind):
                d = self.diagnostic(value)
                self.assertEqual(d['validation_reason'], 'schema_top_level_type')
                self.assertEqual(d['schema_field'], 'top_level')
                self.assertEqual(d['observed_type'], kind)
                self.assertNotIn('SECRET', dumps(d))
        value = self.brief(); del value['summary']; value['SECRET-UNKNOWN-KEY'] = 'SECRET'
        d = self.diagnostic(value)
        self.assertEqual(d['validation_reason'], 'schema_top_level_keys')
        self.assertEqual(d['missing_keys'], ['summary'])
        self.assertEqual(d['unknown_key_count'], 1)
        self.assertNotIn('SECRET', dumps(d))

    def test_all_local_summary_and_advice_constraints_remain(self):
        for value, reason, extra in [(None, 'type', {'observed_type':'null'}),
                (' \n ', 'blank', {'observed_length':0}), ('x'*4001, 'length', {'observed_length':4001})]:
            d = self.diagnostic({**self.brief(), 'summary':value})
            self.assertEqual(d['validation_reason'], 'schema_summary_'+reason)
            self.assertEqual(d['schema_field'], 'summary')
            for k,v in extra.items(): self.assertEqual(d[k],v)
        for field in ('risks', 'alternatives'):
            for value, reason, extra in [(None, 'type', {'observed_type':'null'}),
                    (['a']*4, 'count', {'observed_count':4}),
                    ([False], 'item_type', {'item_index':0,'observed_type':'boolean'}),
                    (['ok', ' \n '], 'item_blank', {'item_index':1,'observed_length':0}),
                    (['x'*2001], 'item_length', {'item_index':0,'observed_length':2001})]:
                with self.subTest(field=field, reason=reason):
                    d = self.diagnostic({**self.brief(),field:value})
                    self.assertEqual(d['validation_reason'], 'schema_list_'+reason)
                    self.assertEqual(d['schema_field'], field)
                    for k,v in extra.items(): self.assertEqual(d[k],v)
        for value in ([], ['valid']*31, [True], ['UNKNOWN-ID']):
            self.assertEqual(self.diagnostic({**self.brief(),'evidence_ids':value})['validation_reason'], 'invalid_evidence')
        brief = self.brief(); brief['facts']['recall_threshold_pct'] = 9
        self.assertEqual(self.diagnostic(brief)['validation_reason'], 'canonical_fact_mismatch')

    def test_sanitizer_closed_metadata_and_bounded_numbers(self):
        for key in ('observed_length','observed_count','item_index','unknown_key_count'):
            for value in (-1, 1000001, True, 1.5, 'SECRET'):
                self.assertNotIn(key, safe_ai_diagnostic({key:value}))
            for value in (0, 1000000): self.assertEqual(safe_ai_diagnostic({key:value}), {key:value})
        d = safe_ai_diagnostic({'schema_field':'SECRET','observed_type':'SECRET',
            'validation_reason':'SECRET','missing_keys':['SECRET',{}, 'summary','facts','summary'],
            'unknown_keys':['SECRET'], 'body':'SECRET', 'headers':{'SECRET':'SECRET'}})
        self.assertEqual(d, {'schema_field':'unknown','observed_type':'unknown',
            'validation_reason':'unknown','missing_keys':['summary','facts']})
        self.assertNotIn('SECRET', dumps(d))

    def test_repair_pool_effective_limits_and_process_admission(self):
        pool = dict(mode='repair_pool', ledger_readable=True, history_reconciled=False,
            repair_max_attempts=5, repair_attempts_reserved=0, repair_remaining_attempts=5,
            repair_max_reserved_usd=1.0512, known_new_max_attempts=7,
            known_new_max_reserved_usd=1.47168, repair_expired=False)
        self.adapter.cloud_budget = Mock()
        self.adapter.cloud_budget.status.return_value = pool
        self.adapter.cloud_budget.reserve.return_value = 210240
        self.adapter._attempts = 3
        status = self.adapter.status()
        self.assertTrue(status['request_allowed'])
        self.assertEqual(status['status'], 'repair_ready')
        self.assertFalse(status['usage_limits']['legacy_limits_apply'])
        self.assertEqual(status['usage_limits']['budget_usd'], 1.47168)
        self.assertEqual(status['usage_limits']['max_requests_total'], 7)
        self.assertIsNone(status['usage_limits']['max_requests_per_process'])
        with patch.object(self.adapter.opener, 'open', side_effect=self.transport()):
            self.adapter.generate(self.case)
        self.adapter.cloud_budget.reserve.assert_called_once()
        self.assertEqual(self.adapter._attempts, 4)
        for change, expected in [({'repair_attempts_reserved':5,'repair_remaining_attempts':0}, 'repair_exhausted'),
                ({'repair_expired':True,'repair_remaining_attempts':0},'credit_expired'), ({'ledger_readable':False},'budget_blocked'),
                ({'repair_max_attempts':999},'budget_blocked')]:
            self.adapter.cloud_budget.status.return_value = {**pool, **change}
            status = self.adapter.status()
            self.assertFalse(status['request_allowed']); self.assertEqual(status['status'],expected)
            if expected == 'credit_expired':
                self.assertFalse(status['usage_limits']['legacy_limits_apply'])
                self.assertEqual(status['usage_limits']['max_requests_total'], 7)
                self.assertEqual(status['usage_limits']['budget_usd'], 1.47168)
                self.assertIsNone(status['usage_limits']['max_requests_per_process'])


if __name__ == '__main__': unittest.main()
