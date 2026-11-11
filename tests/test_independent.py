"""Independent W03 acceptance tests. Never mutates supplied original files.

Run: python -m unittest discover -s tests -p 'test_independent.py' -v
Every service uses a disposable SQLite database. Model networking is disabled.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backend


class IndependentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aurora-independent-')
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            'AURORA_AI_ALLOW_NETWORK': '0', 'AURORA_AI_API_KEY': '',
            'AURORA_AI_MODEL': '', 'LLM_API_KEY': '', 'LLM_MODEL': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db_path = Path(self.temp.name) / 'state.sqlite3'
        self.svc = backend.AuroraService(db_path=self.db_path)
        self.sequence = 0

    def change(self, operation, actor='quality', case_id='CASE-001', **payload):
        self.sequence += 1
        payload.setdefault('expected_version', self.svc.detail(case_id)['version'])
        return self.svc.mutate('/api/cases/' + case_id + '/' + operation,
                               payload, actor, f'independent-{self.sequence}')

    def conditions(self, changes, actor='quality', case_id='CASE-001'):
        return self.change('conditions', actor, case_id,
                           changes=changes, reason='독립 시험용 모의 조건')

    def approve(self, case_id='CASE-001'):
        self.change('submit', case_id=case_id)
        return self.change('decision', 'committee', case_id,
                           decision='approve', reason='독립 시험용 모의 의결')

    def task(self, task_type, outcome='confirmed', case_id='CASE-001'):
        task = next(t for t in self.svc.detail(case_id)['tasks'] if t['type'] == task_type)
        detail = self.svc.detail(case_id)
        commitments = [{'customer_id':c['customer_id'], 'status':'confirmed' if i==0 else 'not_required',
            'quantity':detail['conditions']['replacement_qty'] if i==0 else 0,
            'due_date':detail['conditions']['customer_due_date'] if i==0 else None,
            'reason':'독립 시험 전용 모의 고객별 확인'} for i,c in enumerate(detail['customers'])]
        return self.change('tasks/' + task['id'] + '/reply', task['department'],
                           case_id, outcome=outcome, note='독립 시험용 담당자 모의 회신', customer_commitments=commitments)

    def ready(self, quantity=1000, case_id='CASE-001'):
        self.conditions({'replacement_qty': quantity}, case_id=case_id)
        self.task('inventory_check', case_id=case_id)
        self.approve(case_id)
        for scope in ('customer_scope_check', 'recall_scope'):
            if any(t['type'] == scope for t in self.svc.detail(case_id)['tasks']):
                self.task(scope, case_id=case_id)
        self.task('production_schedule', case_id=case_id)
        return self.task('quality_release', case_id=case_id)

    def error(self, code, fn):
        with self.assertRaises(backend.APIError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_original_counts_and_candidates(self):
        state = self.svc.state()
        self.assertEqual(state['summary']['total_batches'], 480)
        self.assertEqual(state['summary']['total_claims'], 165)
        candidates = [b for b in state['batches'] if b['recall_eligible']]
        self.assertEqual({b['batch_id'] for b in candidates},
                         {'B-02-2602-01', 'B-02-2603-01'})
        first = self.svc.detail('CASE-001')['analysis']
        second = self.svc.detail('CASE-002')['analysis']
        self.assertEqual((first['confirmed_qty'], first['produced_qty'], first['shipped_qty']),
                         (17900, 150745, 145860))
        self.assertEqual((second['confirmed_qty'], second['produced_qty'], second['shipped_qty']),
                         (18622, 160733, 153793))
        total = sum(self.svc.evidence.batch_analysis(b)['confirmed_qty']
                    for b in self.svc.evidence.batches)
        self.assertEqual(total, 36714)  # Includes 192 EA in closed-but-confirmed claims.

    def test_claim_customer_not_lost_when_sbl_missing(self):
        for case_id, qty in [('CASE-001', 5320), ('CASE-002', 6445)]:
            detail = self.svc.detail(case_id)
            customer = next(c for c in detail['customers'] if c['customer_id'] == 'C-105')
            self.assertEqual(customer['confirmed_claim_qty'], qty)
            self.assertEqual(customer['link_status'], 'claims_without_sbl')
            task = next(t for t in detail['tasks'] if t['type'] == 'customer_scope_check')
            self.assertIn('C-105', task['note'])
            self.assertNotEqual(task['status'], 'done')

    def test_missing_recovery_information_does_not_block_recall(self):
        detail = self.approve()
        self.assertTrue(detail['recall_authorized'])
        self.assertIsNone(detail['conditions']['replacement_qty'])
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
        self.assertTrue(any(t['type'] == 'recall_scope' for t in detail['tasks']))

    def test_foreign_role_cannot_approve(self):
        detail = self.change('submit')
        self.error('FORBIDDEN', lambda: self.change('decision', 'sales',
                   decision='approve', reason='권한 우회 시험'))
        after = self.svc.detail('CASE-001')
        self.assertEqual(after['version'], detail['version'])
        self.assertFalse(after['recall_authorized'])
        self.assertEqual(after['decisions'], [])

    def test_rejection_preserves_risk_and_followup(self):
        self.change('submit')
        detail = self.change('decision', 'committee', decision='reject', reason='대상 범위 보완 필요')
        self.assertEqual(detail['status'], 'rejected')
        self.assertFalse(detail['recall_authorized'])
        self.assertTrue(detail['analysis']['recall_eligible'])
        task = next(t for t in detail['tasks'] if t['type'] == 'committee_recheck')
        self.assertEqual(task['status'], 'open')
        self.assertIn('대상 범위', task['note'])

    def test_stale_version_cannot_approve(self):
        old = self.change('submit')
        self.conditions({'confirmed_qty_override': 15000})
        self.error('STALE_VERSION', lambda: self.change('decision', 'committee',
                   expected_version=old['version'], decision='approve', reason='오래된 화면'))
        self.assertEqual(self.svc.detail('CASE-001')['decisions'], [])

    def test_identical_request_is_idempotent(self):
        payload = {'expected_version': 1}
        a = self.svc.mutate('/api/cases/CASE-001/submit', payload, 'quality', 'stable-key')
        b = self.svc.mutate('/api/cases/CASE-001/submit', payload, 'quality', 'stable-key')
        self.assertEqual(a, b)
        self.assertEqual(self.svc.detail('CASE-001')['version'], 2)
        self.assertTrue(self.svc.verify_audit()['valid'])

    def test_threshold_uses_unrounded_integer_rule(self):
        a = self.conditions({'confirmed_qty_override': 15074})
        self.assertFalse(a['analysis']['recall_eligible'])
        b = self.conditions({'confirmed_qty_override': 15075})
        self.assertTrue(b['analysis']['recall_eligible'])

    def test_free_text_cannot_validate_financial_mapping(self):
        self.error('MAPPING_EVIDENCE_REQUIRED', lambda: self.task('mapping_check'))

    def test_override_never_changes_original(self):
        original_hashes = {k: hashlib.sha256((self.svc.evidence.root / p).read_bytes()).hexdigest()
                           for k, p in backend.SOURCE_FILES.items()}
        self.conditions({'confirmed_qty_override': 15000})
        detail = self.svc.detail('CASE-001')
        self.assertEqual(detail['analysis']['original_confirmed_qty'], 17900)
        self.assertEqual(detail['analysis']['confirmed_qty'], 15000)
        self.assertEqual(detail['analysis']['calculation_kind'], 'simulation')
        for k, p in backend.SOURCE_FILES.items():
            self.assertEqual(hashlib.sha256((self.svc.evidence.root / p).read_bytes()).hexdigest(),
                             original_hashes[k])

    def test_unconfigured_model_does_not_fabricate_advice(self):
        before = self.svc.detail('CASE-001')
        self.assertFalse(before['ai']['connected'])
        with mock.patch('urllib.request.urlopen') as network:
            self.error('AI_NOT_CONFIGURED', lambda: self.change('ai-brief'))
            network.assert_not_called()
        after = self.svc.detail('CASE-001')
        self.assertEqual(after['version'], before['version'])
        self.assertEqual(after['tasks'], before['tasks'])
        self.assertIsNone(after['ai_brief'])

    def test_state_and_audit_survive_restart(self):
        before = self.approve()
        second = backend.AuroraService(db_path=self.db_path)
        after = second.detail('CASE-001')
        self.assertEqual(after['decisions'], before['decisions'])
        self.assertEqual(after['tasks'], before['tasks'])
        self.assertTrue(after['recall_authorized'])
        self.assertTrue(second.verify_audit()['valid'])

    def test_source_change_detected_only_on_disposable_copy(self):
        copy_root = Path(self.temp.name) / 'data-copy'
        for relative in backend.SOURCE_FILES.values():
            destination = copy_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.svc.evidence.root / relative, destination)
        service = backend.AuroraService(data_dir=copy_root,
                                        db_path=Path(self.temp.name) / 'copy.sqlite3')
        copied_policy = copy_root / backend.SOURCE_FILES['policy']
        copied_policy.write_text(copied_policy.read_text() + '\nDisposable-copy test marker.\n')
        self.error('SOURCE_CHANGED', lambda: service.mutate('/api/cases/CASE-001/submit',
                    {'expected_version': 1}, 'quality', 'tamper-check'))
        self.assertEqual(service.detail('CASE-001')['version'], 1)
        self.svc.evidence.verify_unchanged()

    def test_normal_recovery_requires_all_department_replies(self):
        detail = self.ready()
        self.assertTrue(detail['analysis']['recovery_plan']['feasible'])
        self.assertEqual(detail['analysis']['recovery_plan']['earliest_ship_date'], '2026-07-15')
        self.assertEqual(detail['status'], 'approved')
        detail = self.task('customer_promise')
        self.assertEqual(detail['status'], 'recovery_ready')
        self.assertTrue(any(t['type'] == 'customer_promise' and t['status'] == 'done'
                            for t in detail['tasks']))
        self.assertNotEqual(detail['status'], 'closed')

    def test_quantity_change_invalidates_previous_quality_release(self):
        self.ready()
        detail = self.conditions({'replacement_qty': 1200}, actor='sales')
        self.assertFalse(detail['conditions']['qa_release'],
                         'A new quantity must not reuse the prior QA release.')
        qa = next(t for t in detail['tasks'] if t['type'] == 'quality_release')
        self.assertNotEqual(qa['status'], 'done')

    def test_supply_only_change_does_not_require_new_recall_vote(self):
        self.ready()
        old_decisions = len(self.svc.detail('CASE-001')['decisions'])
        detail = self.conditions({'production_delay_days': 1}, actor='production')
        self.assertTrue(detail['recall_authorized'])
        # The existing quality basis is unchanged. Production must be able to
        # revalidate its own revised slot without a new recall-committee vote.
        detail = self.task('production_schedule')
        self.assertTrue(detail['conditions']['production_confirmed'])
        self.assertEqual(len(detail['decisions']), old_decisions)

    def test_condition_endpoint_cannot_bypass_shared_capacity(self):
        for case_id in ['CASE-001', 'CASE-002']:
            self.conditions({'replacement_qty': 30000, 'customer_due_date': '2027-07-01'},
                            case_id=case_id)
            self.approve(case_id)
        self.task('production_schedule', case_id='CASE-001')
        # 60,000 > the declared 58,484 EA residual scenario capacity.
        try:
            detail = self.conditions({'production_confirmed': True}, actor='production',
                                     case_id='CASE-002')
        except backend.APIError:
            return  # Rejecting this route is a valid control.
        self.assertFalse(detail['conditions']['production_confirmed'],
                         'Direct condition input bypassed shared reservation validation.')

    def test_withdrawn_confirmations_reopen_owner_task(self):
        for flag, owner, task_type in [
            ('production_confirmed', 'production', 'production_schedule'),
            ('inventory_verified', 'sales', 'inventory_check'),
            ('qa_release', 'quality', 'quality_release'),
        ]:
            with self.subTest(flag=flag):
                self.svc = backend.AuroraService(
                    db_path=Path(self.temp.name) / (flag + '.sqlite3'))
                self.ready()
                detail = self.conditions({flag: False}, actor=owner)
                task = next(t for t in detail['tasks'] if t['type'] == task_type)
                self.assertNotEqual(task['status'], 'done',
                                    'Withdrawn confirmation must create a new owner follow-up.')
                detail = self.task(task_type)
                self.assertTrue(detail['conditions'][flag])

    def test_withdrawn_confirmation_invalidates_customer_promise(self):
        self.ready()
        self.task('customer_promise')
        detail = self.conditions({'production_confirmed': False}, actor='production')
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
        promise = next(t for t in detail['tasks'] if t['type'] == 'customer_promise')
        self.assertNotEqual(promise['status'], 'done',
                            'A customer promise must be reviewed after its production basis is withdrawn.')

    def test_same_day_capacity_shared_between_two_cases(self):
        for case_id in ['CASE-001', 'CASE-002']:
            self.conditions({'replacement_qty': 1000}, case_id=case_id)
            self.approve(case_id)
        self.task('production_schedule', case_id='CASE-001')
        self.error('CAPACITY_ALREADY_RESERVED',
                   lambda: self.task('production_schedule', case_id='CASE-002'))
        self.assertFalse(self.svc.detail('CASE-002')['conditions']['production_confirmed'])

    def test_three_overlapping_reservations_use_aggregate_daily_capacity(self):
        for case_id in ['CASE-001', 'CASE-002', 'CASE-003']:
            changes = {'replacement_qty': 1000, 'production_daily_capacity': 100,
                       'customer_due_date': '2026-08-01'}
            if case_id == 'CASE-003':
                produced = self.svc.detail(case_id)['batch']['produced_qty']
                changes['confirmed_qty_override'] = produced // 5
            self.conditions(changes, case_id=case_id)
            self.approve(case_id)
        self.task('production_schedule', case_id='CASE-001')
        self.task('production_schedule', case_id='CASE-002')
        # Every pair is 200 <= 224, but three concurrent 100 EA/day slots are 300.
        self.error('CAPACITY_ALREADY_RESERVED',
                   lambda: self.task('production_schedule', case_id='CASE-003'))

    def test_negative_zero_fractional_inputs_do_not_mutate(self):
        for field, value in [('replacement_qty', -1), ('replacement_qty', 1.5),
                             ('production_daily_capacity', 0), ('qa_days', -1),
                             ('customer_due_date', '2026-13-40')]:
            before = self.svc.detail('CASE-001')
            self.error('INVALID_CONDITIONS', lambda f=field, v=value: self.conditions({f: v}))
            after = self.svc.detail('CASE-001')
            self.assertEqual(after['version'], before['version'])
            self.assertEqual(after['conditions'], before['conditions'])


class IndependentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aurora-independent-http-')
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(os.environ, {'AURORA_AI_ALLOW_NETWORK': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = backend.AuroraService(db_path=Path(self.temp.name) / 'state.sqlite3')
        self.server = backend.make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload=None, token=None, origin=None, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Content-Type': 'application/json', 'Idempotency-Key': path + '-test'}
        if host is not None:
            headers['Host'] = host
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = origin
        connection.request('POST' if payload is not None else 'GET', path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw)

    def test_dns_rebinding_host_rejected_on_read_and_session(self):
        for host in ('evil.test', f'evil.test:{self.server.server_port}',
                     '127.0.0.1.evil.test', f'localhost:{self.server.server_port+1}'):
            for path, payload in (('/api/state', None), ('/api/session', {'actor_id': 'committee'})):
                with self.subTest(host=host, path=path):
                    status, body = self.request(path, payload, origin='http://' + host, host=host)
                    self.assertEqual((status, body['error']['code']), (403, 'HOST_REJECTED'))
        host = f'localhost:{self.server.server_port}'
        status, body = self.request('/api/session', {'actor_id':'committee'}, origin='http://'+host, host=host)
        self.assertEqual(status, 200)
        self.assertIn('token', body)

    def test_origin_scheme_and_malformed_origin_rejected(self):
        host = f'127.0.0.1:{self.server.server_port}'
        for origin in ('https://'+host, 'http://'+host+'/', 'null', 'http://user@'+host):
            status, body = self.request('/api/session', {'actor_id':'committee'}, origin=origin)
            self.assertEqual((status, body['error']['code']), (403, 'ORIGIN_REJECTED'))

    def test_http_auth_required_and_payload_cannot_spoof_role(self):
        status, body = self.request('/api/cases/CASE-001/submit', {'expected_version': 1})
        self.assertEqual((status, body['error']['code']), (401, 'AUTH_REQUIRED'))
        _, session = self.request('/api/session', {'actor_id': 'sales'})
        status, body = self.request('/api/cases/CASE-001/decision',
            {'expected_version': 1, 'actor': 'committee', 'decision': 'approve', 'reason': 'spoof'},
            session['token'])
        self.assertEqual((status, body['error']['code']), (403, 'FORBIDDEN'))

    def test_http_cross_origin_write_rejected(self):
        status, body = self.request('/api/session', {'actor_id': 'committee'},
                                    origin='https://unrelated.invalid')
        self.assertEqual((status, body['error']['code']), (403, 'ORIGIN_REJECTED'))

    def test_http_health_and_backend_file_not_exposed(self):
        status, body = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(body['mode'], 'local_simulation')
        status, _ = self.request('/backend.py')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
