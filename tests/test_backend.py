import csv
import io
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import APIError, AuroraService, DEFAULT_DATA, ModelAdapter, add_business_days, make_server


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEFAULT_DATA.rglob('*') if p.is_file()}

    @classmethod
    def tearDownClass(cls):
        for path, sha in cls.original_hashes.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, 'Original changed: ' + str(path)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = self.tmp.name + '/state.sqlite3'
        self.s = AuroraService(db_path=self.db)
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, action, actor='quality', case='CASE-001', **payload):
        self.counter += 1
        payload.setdefault('expected_version', self.s.detail(case)['version'])
        return self.s.mutate(f'/api/cases/{case}/{action}', payload, actor, f'key-{self.counter}')

    def approved(self, case='CASE-001', quantity=None):
        if quantity is not None:
            self.op('conditions', case=case, changes={'replacement_qty': quantity}, reason='명시적 모의 고객 재공급 수량')
        self.op('submit', case=case)
        return self.op('decision', 'committee', case=case, decision='approve', reason='원본 배치 회수 요건 확인 · 모의 위원회 의결')

    def task(self, kind, case='CASE-001'):
        return next(t for t in self.s.detail(case)['tasks'] if t['type'] == kind)

    def reply(self, kind, actor, case='CASE-001', outcome='confirmed'):
        detail = self.s.detail(case)
        commitments = [{'customer_id':c['customer_id'], 'status':'confirmed' if i==0 else 'not_required',
            'quantity':detail['conditions']['replacement_qty'] if i==0 else 0,
            'due_date':detail['conditions']['customer_due_date'] if i==0 else None,
            'reason':'시험 전용 모의 고객별 확인'} for i,c in enumerate(detail['customers'])]
        return self.op('tasks/' + self.task(kind, case)['id'] + '/reply', actor, case, outcome=outcome, note='모의 회신 · 원본 증거로 위장하지 않음', customer_commitments=commitments)

    def assert_error(self, code, fn):
        with self.assertRaises(APIError) as ctx:
            fn()
        self.assertEqual(code, ctx.exception.code)

    def test_replacement_quantity_bounds_have_actionable_message(self):
        before = self.s.detail('CASE-001')
        for quantity in (0, -1, True, 1.5, before['analysis']['produced_qty'] + 1):
            with self.assertRaises(APIError) as error:
                self.op('conditions', changes={'replacement_qty': quantity}, reason='수량 범위 안내 검증')
            self.assertEqual(error.exception.code, 'INVALID_CONDITIONS')
            self.assertIn('미확인이면 비워 두세요', error.exception.message)
            self.assertIn('정수', error.exception.message)
        after = self.s.detail('CASE-001')
        for key in ('conditions', 'version', 'status', 'recall_authorized'):
            self.assertEqual(before[key], after[key])
        self.op('conditions', changes={'replacement_qty': 1}, reason='최소 허용 수량 입력 검증')
        detail = self.op('conditions', changes={'replacement_qty': None}, reason='수량 미확인으로 복원 검증')
        self.assertIsNone(detail['conditions']['replacement_qty'])

    def test_supply_preview_is_read_only_and_uses_existing_planner(self):
        before = self.s.detail('CASE-001')
        preview = self.s.supply_preview('CASE-001', 1000, '2026-07-20')
        self.assertEqual(preview['kind'], 'read_only_simulation')
        self.assertIsNone(preview['baseline']['quantity'])
        self.assertEqual(preview['candidate']['production_days'], 5)
        self.assertEqual(preview['candidate']['earliest_ship_date'], '2026-07-15')
        self.assertEqual(preview['candidate']['delay_days'], 0)
        shorter = self.s.supply_preview('CASE-001', 500, '2026-07-10')['candidate']
        self.assertEqual(shorter['earliest_ship_date'], '2026-07-13')
        self.assertEqual(shorter['delay_days'], 3)
        self.assertEqual(before, self.s.detail('CASE-001'))
        for quantity in (None, 0, -1, 1.5, True):
            self.assert_error('INVALID_CONDITIONS', lambda q=quantity: self.s.supply_preview('CASE-001', q, '2026-07-20'))
        self.assert_error('INVALID_CONDITIONS', lambda: self.s.supply_preview('CASE-001', 1000, '2026-06-01'))

    def test_supply_preview_and_save_share_effective_date_constraint(self):
        self.op('conditions', changes={'effective_date': '2026-08-01', 'customer_due_date': '2026-08-20'}, reason='후속 적용일의 비교 검증')
        before = self.s.detail('CASE-001')
        with self.assertRaises(APIError) as preview_error:
            self.s.supply_preview('CASE-001', 1000, '2026-07-20')
        self.assertEqual(preview_error.exception.code, 'INVALID_CONDITIONS')
        self.assertIn('2026-08-01', preview_error.exception.message)
        self.assertEqual(before, self.s.detail('CASE-001'), 'rejected preview is read-only, including audit')
        with self.assertRaises(APIError) as save_error:
            self.op('conditions', changes={'replacement_qty': 1000, 'customer_due_date': '2026-07-20'}, reason='같은 후보 저장 검증')
        self.assertEqual(preview_error.exception.code, save_error.exception.code)
        self.assertEqual(preview_error.exception.message, save_error.exception.message)
        after = self.s.detail('CASE-001')
        for key in ('conditions', 'version', 'status', 'recall_authorized'):
            self.assertEqual(before[key], after[key])
        valid = self.s.supply_preview('CASE-001', 1000, '2026-08-01')
        self.assertEqual(valid['candidate']['due_date'], '2026-08-01')
        saved = self.op('conditions', changes={'replacement_qty': 1000, 'customer_due_date': '2026-08-01'}, reason='적용일과 동일한 날짜 후보 저장')
        self.assertEqual(saved['conditions']['customer_due_date'], valid['candidate']['due_date'])
        self.assertEqual(saved['analysis']['recovery_plan']['earliest_ship_date'], valid['candidate']['earliest_ship_date'])

    def test_supply_preview_never_reuses_confirmations(self):
        self.approved(quantity=1000)
        for kind in ('customer_scope_check', 'recall_scope', 'inventory_check'):
            self.reply(kind, 'sales')
        self.reply('production_schedule', 'production')
        self.reply('quality_release', 'quality')
        self.reply('customer_promise', 'sales')
        before = self.s.detail('CASE-001')
        self.assertEqual(before['status'], 'recovery_ready')
        preview = self.s.supply_preview('CASE-001', 500, '2026-07-20')
        candidate = preview['candidate']
        self.assertFalse(candidate['feasible'])
        self.assertFalse(candidate['physical_supply_ready'])
        self.assertEqual(candidate['customer_agreement'], 'pending')
        for department in ('영업팀', '생산팀', '품질팀', '고객별'):
            self.assertTrue(any(department in text for text in candidate['blockers']))
        self.assertEqual(before, self.s.detail('CASE-001'))

    def test_supply_preview_checks_shared_production_reservations(self):
        self.approved(case='CASE-002', quantity=1000)
        self.reply('inventory_check', 'sales', case='CASE-002')
        self.reply('production_schedule', 'production', case='CASE-002')
        before = self.s.detail('CASE-001')
        preview = self.s.supply_preview('CASE-001', 1000, '2026-07-20')
        self.assertTrue(any('생산 예약의 합계' in text for text in preview['candidate']['blockers']))
        self.assertFalse(preview['candidate']['feasible'])
        self.assertEqual(before, self.s.detail('CASE-001'))

    def test_state_open_departments_match_actual_unfinished_tasks(self):
        for stage in range(3):
            for case in self.s.state()['cases']:
                tasks = self.s.detail(case['id'])['tasks']
                pending = [t for t in tasks if t['status'] != 'done']
                self.assertEqual(case['open_departments'], sorted({t['department'] for t in pending}))
                self.assertEqual(case['open_task_count'], len(pending))
            if stage == 0:
                self.approved(quantity=1000)
            elif stage == 1:
                self.reply('inventory_check', 'sales')

    def test_investigation_promoted_to_recall_can_complete_recovery(self):
        case = 'CASE-003'
        detail = self.op('conditions', case=case,
                         changes={'confirmed_qty_override': 20000, 'replacement_qty': 1000},
                         reason='모의 검사 확정 및 고객 요청 수량 확인')
        self.assertTrue(detail['analysis']['recall_eligible'])
        detail = self.approved(case=case)
        task_types = [t['type'] for t in detail['tasks']]
        self.assertEqual(task_types.count('inventory_check'), 1)
        self.assertEqual(task_types.count('mapping_check'), 1)
        for scope in ('customer_scope_check', 'recall_scope'):
            if any(t['type'] == scope for t in self.s.detail(case)['tasks']):
                self.reply(scope, 'sales', case)
        self.reply('inventory_check', 'sales', case)
        self.reply('production_schedule', 'production', case)
        detail = self.reply('quality_release', 'quality', case)
        self.assertTrue(detail['analysis']['recovery_plan']['feasible'])
        self.assertEqual(detail['status'], 'approved')
        detail = self.reply('customer_promise', 'sales', case)
        self.assertEqual(detail['status'], 'recovery_ready')

    def test_supply_change_during_pending_vote_does_not_cancel_vote(self):
        before = self.op('submit')
        detail = self.op('conditions', 'sales', changes={'customer_due_date': '2026-07-21'},
                         reason='고객 모의 희망일 조정')
        self.assertEqual(detail['quality_revision'], before['quality_revision'])
        self.assertEqual(detail['status'], 'awaiting_approval')
        self.assertFalse(any(t['type'] == 'committee_recheck' for t in detail['tasks']))
        self.assert_error('STALE_VERSION', lambda: self.op('decision', 'committee',
                          expected_version=before['version'], decision='approve', reason='이전 화면 승인 방어'))
        detail = self.op('decision', 'committee', decision='approve', reason='최신 상태와 품질 근거 검토')
        self.assertTrue(detail['recall_authorized'])

    def test_exact_original_calculation_and_population(self):
        state = self.s.state()
        self.assertEqual((480, 165, 2, 105), tuple(state['summary'][k] for k in ['total_batches', 'total_claims', 'recall_candidates', 'investigation_batches']))
        a = self.s.detail('CASE-001')['analysis']
        self.assertEqual((17900, 150745, 145860), (a['confirmed_qty'], a['produced_qty'], a['shipped_qty']))
        self.assertAlmostEqual(11.874357, a['defect_rate_pct'])
        b = self.s.detail('CASE-002')['analysis']
        self.assertEqual((18622, 160733, 153793), (b['confirmed_qty'], b['produced_qty'], b['shipped_qty']))
        self.assertAlmostEqual(11.585673, b['defect_rate_pct'])
        self.assertFalse(self.s.detail('CASE-003')['analysis']['recall_eligible'])

    def test_closed_claims_included_and_unconfirmed_excluded(self):
        raw = self.s.evidence.tables['claims']
        closed = [c for c in raw if c['status'] == '종결' and c['defect_confirmed']]
        self.assertEqual((19, 192), (len(closed), sum(c['qty_affected'] for c in closed)))
        all_confirmed = sum(c['qty_affected'] for c in raw if c['defect_confirmed'])
        derived = sum(self.s.evidence.batch_analysis(b)['confirmed_qty'] for b in self.s.evidence.batches)
        self.assertEqual(all_confirmed, derived)
        self.assertEqual(0, self.s.detail('CASE-003')['analysis']['confirmed_qty'])
        self.assertEqual(4, self.s.detail('CASE-003')['analysis']['unconfirmed_qty'])

    def test_threshold_boundary_uses_unrounded_rate(self):
        a = self.s.evidence.batch_analysis('B-02-2602-01', {'confirmed_qty_override': 15074})
        b = self.s.evidence.batch_analysis('B-02-2602-01', {'confirmed_qty_override': 15075})
        self.assertFalse(a['recall_eligible'])
        self.assertTrue(b['recall_eligible'])

    def test_claim_and_shipment_customer_union(self):
        case = self.s.detail('CASE-001')
        customer = next(c for c in case['customers'] if c['customer_id'] == 'C-105')
        self.assertGreater(customer['confirmed_claim_qty'], 0)
        self.assertEqual(0, customer['shipment_count'])
        self.assertEqual('claims_without_sbl', customer['link_status'])
        self.assertIn('customer_scope_check', {t['type'] for t in case['tasks']})

    def test_provenance_hash_and_physical_row(self):
        claim = self.s.detail('CASE-001')['claims'][0]
        source = claim['source']
        path = DEFAULT_DATA / source['path']
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source['sha256'])
        self.assertEqual('QC-00001', path.read_text().splitlines()[source['row_number'] - 1].split('|')[0])
        self.assertEqual(2, source['row_number'])

    def test_only_committee_approves_and_no_automatic_work(self):
        c = self.s.detail('CASE-001')
        self.assertNotIn('production_schedule', {t['type'] for t in c['tasks']})
        self.op('submit')
        self.assert_error('FORBIDDEN', lambda: self.op('decision', 'sales', decision='approve', reason='forged role', role='committee'))
        self.assertFalse(self.s.detail('CASE-001')['recall_authorized'])
        c = self.op('decision', 'committee', decision='approve', reason='정식 모의 의결')
        self.assertTrue(c['recall_authorized'])
        self.assertIn('production_schedule', {t['type'] for t in c['tasks']})

    def test_investigation_cannot_be_recalled(self):
        self.assert_error('RECALL_CRITERIA_NOT_MET', lambda: self.op('submit', case='CASE-003'))
        self.assertEqual('investigating', self.s.detail('CASE-003')['status'])

    def test_idempotency_replay_no_duplicate(self):
        payload = {'expected_version': 1}
        first = self.s.mutate('/api/cases/CASE-001/submit', payload, 'quality', 'same-key')
        second = self.s.mutate('/api/cases/CASE-001/submit', payload, 'quality', 'same-key')
        self.assertEqual(first, second)
        self.assertEqual(2, self.s.detail('CASE-001')['version'])
        self.assertEqual(1, sum(a['action'] == 'submitted_for_committee' for a in self.s.detail('CASE-001')['audit']))
        self.assert_error('IDEMPOTENCY_CONFLICT', lambda: self.s.mutate('/api/cases/CASE-001/submit', {'expected_version': 2}, 'quality', 'same-key'))

    def test_idempotency_concurrent_requests(self):
        def submit(_):
            return self.s.mutate('/api/cases/CASE-001/submit', {'expected_version': 1}, 'quality', 'concurrent')['version']
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual([2] * 5, list(pool.map(submit, range(5))))

    def test_stale_version_and_duplicate_decision(self):
        self.op('submit')
        self.assert_error('STALE_VERSION', lambda: self.op('decision', 'committee', expected_version=1, decision='approve', reason='stale'))
        self.op('decision', 'committee', decision='approve', reason='current')
        tasks = len(self.s.detail('CASE-001')['tasks'])
        self.assert_error('INVALID_TRANSITION', lambda: self.op('decision', 'committee', decision='approve', reason='duplicate'))
        self.assertEqual(tasks, len(self.s.detail('CASE-001')['tasks']))

    def test_rejected_case_creates_followup_and_resubmits(self):
        self.op('submit')
        c = self.op('decision', 'committee', decision='reject', reason='출처 범위를 다시 검토')
        self.assertFalse(c['recall_authorized'])
        self.assertEqual('rejected', c['status'])
        self.assertEqual('open', self.task('committee_recheck')['status'])
        self.assertIn('root_cause', {t['type'] for t in c['tasks']})
        self.op('submit')
        c = self.op('decision', 'committee', decision='approve', reason='근거 재검토 완료')
        self.assertTrue(c['recall_authorized'])

    def test_supply_change_preserves_recall_no_committee_reroute(self):
        self.approved(quantity=1000)
        c = self.op('conditions', 'production', changes={'production_delay_days': 7}, reason='모의 생산 지연')
        self.assertTrue(c['recall_authorized'])
        self.assertEqual('review_required', c['status'])
        self.assertEqual(1, c['quality_revision'])
        self.assertGreater(c['supply_revision'], 1)
        self.assertNotIn('committee_recheck', {t['type'] for t in c['tasks']})
        self.assertIn('customer_renegotiate', {t['type'] for t in c['tasks']})
        self.assert_error('RECALL_ALREADY_AUTHORIZED', lambda: self.op('submit'))
        c = self.reply('production_schedule', 'production')
        self.assertTrue(c['conditions']['production_confirmed'])

    def test_quality_correction_revokes_only_in_scenario(self):
        self.approved()
        c = self.op('conditions', changes={'confirmed_qty_override': 1000}, reason='모의 재검사 정정')
        self.assertFalse(c['recall_authorized'])
        self.assertFalse(c['analysis']['recall_eligible'])
        self.assertEqual(17900, c['analysis']['original_confirmed_qty'])
        self.assertEqual(2, c['quality_revision'])
        self.assertEqual('simulation', c['analysis']['calculation_kind'])
        self.op('submit')
        self.assert_error('RECALL_CRITERIA_NOT_MET', lambda: self.op('decision', 'committee', decision='approve', reason='회수 재승인 금지'))

    def test_confirmation_flags_require_real_task_replies(self):
        for flag in ('production_confirmed', 'inventory_verified', 'qa_release'):
            self.assert_error('REPLY_REQUIRED', lambda flag=flag: self.op('conditions', changes={flag: True}, reason='직접 우회 시도'))
        self.approved(quantity=1000)
        self.assert_error('FORBIDDEN', lambda: self.reply('production_schedule', 'quality'))

    def test_quantity_required_and_mapping_cannot_be_invented(self):
        self.approved()
        self.assert_error('QUANTITY_REQUIRED', lambda: self.reply('inventory_check', 'sales'))
        self.assert_error('MAPPING_EVIDENCE_REQUIRED', lambda: self.reply('mapping_check', 'sales'))
        c = self.reply('mapping_check', 'sales', outcome='blocked')
        self.assertTrue(any('SBL' in w for w in c['analysis']['warnings']))

    def test_partial_customer_agreement_does_not_confirm_every_customer(self):
        self.approved(quantity=1000)
        for task, actor in [('customer_scope_check','sales'),('recall_scope','sales'),
                            ('inventory_check','sales'),('production_schedule','production'),('quality_release','quality')]:
            self.reply(task,actor)
        d = self.s.detail('CASE-001')
        self.assertGreater(len(d['customers']), 1)
        self.assertNotEqual(d['status'],'recovery_ready')
        self.assert_error('CUSTOMER_COMMITMENTS_REQUIRED',lambda:self.op('tasks/'+self.task('customer_promise')['id']+'/reply','sales',
            outcome='confirmed',note='C-101 고객만 1000개 동의, 나머지 미확인',
            customer_commitments=[{'customer_id':'C-101','quantity':1000,'status':'confirmed','due_date':'2026-07-20','reason':'C-101만 확인된 모의 회신'}]))
        self.assert_error('CUSTOMER_COMMITMENTS_REQUIRED',lambda:self.op('tasks/'+self.task('customer_promise')['id']+'/reply','sales',
            outcome='confirmed',note='전체 동의라는 자유문구만 존재'))
        d=self.reply('customer_promise','sales')
        self.assertEqual(d['status'],'recovery_ready')
        self.assertEqual(len(d['analysis']['recovery_plan']['customer_commitments']),len(d['customers']))
        self.op('conditions',changes={'customer_due_date':'2026-07-21'},reason='모의 고객 일정 재검토')
        self.assertEqual(self.s.detail('CASE-001')['analysis']['recovery_plan']['customer_agreement'],'pending')

    def test_quality_gate_requires_production_and_is_invalidated_by_withdrawal(self):
        self.approved(quantity=1000)
        self.assert_error('PRODUCTION_NOT_CONFIRMED',lambda:self.reply('quality_release','quality'))
        self.reply('inventory_check','sales')
        self.reply('production_schedule','production')
        self.reply('quality_release','quality')
        d=self.reply('production_schedule','production',outcome='blocked')
        self.assertFalse(d['conditions']['qa_release'])
        self.assertEqual(self.task('quality_release')['status'],'needs_revalidation')
        self.assert_error('PRODUCTION_NOT_CONFIRMED',lambda:self.reply('quality_release','quality'))
        self.reply('production_schedule','production')
        self.reply('quality_release','quality')
        d=self.op('conditions',changes={'production_confirmed':False},reason='생산 확인 철회 모의 정정')
        self.assertFalse(d['conditions']['qa_release'])

    def test_recall_threshold_uses_unrounded_ratio(self):
        d=self.op('conditions',changes={'confirmed_qty_override':15074},reason='판정 경계 수량 모의 정정')
        self.assertEqual(d['analysis']['defect_rate_pct'],9.999668)
        self.assertFalse(d['analysis']['recall_eligible'])
        self.assert_error('RECALL_CRITERIA_NOT_MET',lambda:self.op('submit'))

    def test_zero_quantity_rejected_and_legacy_zero_plan_blocked(self):
        self.assert_error('INVALID_CONDITIONS',lambda:self.op('conditions',changes={'replacement_qty':0},reason='재공급 불필요 여부 미확인'))
        conditions=dict(self.s.detail('CASE-001')['conditions'],replacement_qty=0,inventory_verified=True,production_confirmed=True,qa_release=True)
        plan=self.s._plan(self.s.evidence.batches['B-02-2602-01'],conditions)
        self.assertFalse(plan['feasible'])
        self.assertIsNone(plan['earliest_ship_date'])

    def test_predecision_below_threshold_correction_can_be_reviewed(self):
        self.op('submit')
        d=self.op('conditions',changes={'confirmed_qty_override':1000},reason='의결 전 검사 정정 모의 입력')
        self.assertTrue(d['correction_review_required'])
        self.op('submit')
        d=self.op('decision','committee',decision='acknowledge_correction',reason='기준 미달 정정 확인 및 조사 계속')
        self.assertEqual(d['status'],'investigating')
        self.assertFalse(d['correction_review_required'])
        self.assertFalse(d['recall_authorized'])

    def test_normal_recovery_and_customer_promise(self):
        self.approved(quantity=1000)
        self.reply('customer_scope_check', 'sales')
        self.reply('recall_scope', 'sales')
        self.assert_error('SUPPLY_NOT_READY', lambda: self.reply('customer_promise', 'sales'))
        self.reply('inventory_check', 'sales')
        self.reply('production_schedule', 'production')
        c = self.reply('quality_release', 'quality')
        self.assertEqual('2026-07-15', c['analysis']['recovery_plan']['earliest_ship_date'])
        self.assertEqual('approved', c['status'])
        c = self.reply('customer_promise', 'sales')
        self.assertEqual('recovery_ready', c['status'])
        self.assertEqual('done', self.task('customer_promise')['status'])
        self.assertEqual('simulation', c['analysis']['recovery_plan']['kind'])

    def test_changed_quantity_invalidates_qa_and_production(self):
        self.approved(quantity=1000)
        self.reply('inventory_check', 'sales')
        self.reply('production_schedule', 'production')
        self.reply('quality_release', 'quality')
        c = self.op('conditions', 'sales', changes={'replacement_qty': 1200}, reason='모의 고객 수량 추가')
        self.assertFalse(c['conditions']['inventory_verified'])
        self.assertFalse(c['conditions']['production_confirmed'])
        self.assertFalse(c['conditions']['qa_release'])
        self.assertEqual('needs_revalidation', self.task('quality_release')['status'])
        self.assertTrue(c['recall_authorized'])

    def test_capacity_constraint_cannot_be_overridden_by_reply(self):
        self.approved(quantity=1000)
        c = self.op('conditions', 'production', changes={'production_daily_capacity': 3000}, reason='과도한 모의 슬롯')
        self.assertEqual(224, c['analysis']['recovery_plan']['daily_capacity_limit'])
        self.assertFalse(c['analysis']['recovery_plan']['feasible'])
        self.assert_error('CAPACITY_EXCEEDED', lambda: self.reply('production_schedule', 'production'))

    def test_parallel_cases_cannot_double_book_line(self):
        self.approved(quantity=1000)
        self.reply('production_schedule', 'production')
        self.approved(case='CASE-002', quantity=1000)
        self.assert_error('CAPACITY_ALREADY_RESERVED', lambda: self.reply('production_schedule', 'production', case='CASE-002'))
        c = self.s.detail('CASE-002')
        self.assertTrue(any('생산 예약' in b for b in c['analysis']['recovery_plan']['blockers']))

    def test_invalid_inputs_and_none_distinction(self):
        for changes in ({'replacement_qty': -1}, {'replacement_qty': True}, {'production_daily_capacity': 0}, {'customer_due_date': 'x'}, {'customer_due_date': '2026-06-01'}, {'production_delay_days': 1000000}):
            self.assert_error('INVALID_CONDITIONS', lambda changes=changes: self.op('conditions', changes=changes, reason='invalid'))
        self.assertIsNone(self.s.detail('CASE-001')['conditions']['replacement_qty'])
        self.assert_error('UNKNOWN_CONDITION', lambda: self.op('conditions', changes={'evil': 1}, reason='unknown'))

    def test_persistence_and_hash_chain(self):
        self.approved(quantity=1000)
        reopened = AuroraService(db_path=self.db)
        self.assertTrue(reopened.detail('CASE-001')['recall_authorized'])
        self.assertEqual(1000, reopened.detail('CASE-001')['conditions']['replacement_qty'])
        self.assertTrue(reopened.verify_audit()['valid'])
        with reopened.connect() as db:
            db.execute("UPDATE audit SET payload='{}' WHERE action='committee_approve'")
        self.assertFalse(reopened.verify_audit()['valid'])

    def test_source_change_blocks_mutation_without_editing_original(self):
        with patch.object(self.s.evidence, 'verify_unchanged', side_effect=APIError('SOURCE_CHANGED', 'test', 409)):
            self.assert_error('SOURCE_CHANGED', lambda: self.op('submit'))
        self.assertEqual(1, self.s.detail('CASE-001')['version'])

    def test_ai_unconfigured_is_honest_and_no_state_change(self):
        with patch.dict(os.environ, {}, clear=True):
            self.s.ai = ModelAdapter()
        self.assertFalse(self.s.state()['ai']['connected'])
        with patch('urllib.request.urlopen') as network:
            self.assert_error('AI_NOT_CONFIGURED', lambda: self.op('ai-brief'))
            network.assert_not_called()
        self.assertEqual(1, self.s.detail('CASE-001')['version'])

    def test_ai_advice_stales_after_reply_without_revision_change(self):
        self.approved(quantity=1000)
        # Fixture only; never calls or claims to verify a real model.
        def fake_generate(case):
            return {'kind': 'test_fixture', 'model': 'TEST ONLY', 'prompt_hash': 'fixture',
                    'review_revision': case['review_revision'],
                    'structured': {'summary': '보유량 미확인', 'risks': [], 'alternatives': [],
                                   'evidence_ids': ['fixture']}}
        with patch.object(self.s.ai, 'generate', side_effect=fake_generate):
            before = self.op('ai-brief')
            self.assertFalse(before['ai']['brief']['stale'])
            after = self.reply('inventory_check', 'sales')
            self.assertEqual(before['review_revision'], after['review_revision'])
            self.assertTrue(after['ai']['brief']['stale'])
            self.assertTrue(after['conditions']['inventory_verified'])
            regenerated = self.op('ai-brief')
            self.assertFalse(regenerated['ai']['brief']['stale'])
        self.assertTrue(regenerated['recall_authorized'])

    def test_explicit_scope_failure_blocks_supply_promise_not_recall(self):
        self.approved(quantity=1000)
        for kind in ('customer_scope_check', 'recall_scope'):
            self.reply(kind, 'sales', outcome='blocked')
        self.reply('inventory_check', 'sales')
        self.reply('production_schedule', 'production')
        detail = self.reply('quality_release', 'quality')
        self.assertTrue(detail['recall_authorized'])
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
        self.assertNotEqual(detail['status'], 'recovery_ready')
        self.assert_error('SUPPLY_NOT_READY', lambda: self.reply('customer_promise', 'sales'))
        detail = self.reply('customer_scope_check', 'sales')
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
        detail = self.reply('recall_scope', 'sales')
        self.assertTrue(detail['analysis']['recovery_plan']['feasible'])
        detail = self.reply('customer_promise', 'sales')
        self.assertEqual(self.task('customer_promise')['status'], 'done')

    def test_open_scope_blocks_global_supply_promise(self):
        self.approved(quantity=1000)
        self.reply('inventory_check', 'sales')
        self.reply('production_schedule', 'production')
        detail = self.reply('quality_release', 'quality')
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
        self.assertNotEqual(detail['status'], 'recovery_ready')
        self.assert_error('SUPPLY_NOT_READY', lambda: self.reply('customer_promise', 'sales'))
        self.reply('customer_scope_check', 'sales')
        detail = self.reply('recall_scope', 'sales')
        self.assertTrue(detail['analysis']['recovery_plan']['feasible'])
        self.reply('customer_promise', 'sales')

    def test_below_threshold_correction_has_committee_review_path(self):
        self.approved(quantity=1000)
        detail = self.op('conditions', changes={'confirmed_qty_override': 14000},
                         reason='모의 재검사 정정으로 기준 미달')
        self.assertFalse(detail['recall_authorized'])
        self.assertTrue(detail['correction_review_required'])
        self.op('submit')
        self.assert_error('RECALL_CRITERIA_NOT_MET', lambda: self.op('decision', 'committee', decision='approve', reason='회수로 재승인 불가'))
        self.assert_error('FORBIDDEN', lambda: self.op('decision', 'sales', decision='acknowledge_correction', reason='위원회 권한 필요'))
        detail = self.op('decision', 'committee', decision='acknowledge_correction', reason='정정 검토 및 조사 계속, 기존 실행 중지 확인')
        self.assertEqual(detail['status'], 'investigating')
        self.assertFalse(detail['recall_authorized'])
        self.assertFalse(detail['correction_review_required'])
        self.assertEqual(self.task('committee_recheck')['status'], 'done')
        self.assertEqual(detail['decisions'][0]['scope'], 'quality_correction')
        self.assert_error('APPROVAL_REQUIRED', lambda: self.reply('customer_promise', 'sales'))
        detail = self.op('conditions', changes={'confirmed_qty_override': 20000}, reason='새 모의 검사로 다시 기준 충족')
        detail = self.approved()
        self.assertTrue(detail['recall_authorized'])

    def test_quality_reply_cannot_close_committee_correction_review(self):
        self.approved()
        self.op('conditions', changes={'confirmed_qty_override': 14000}, reason='모의 검사 정정')
        self.assert_error('COMMITTEE_REVIEW_REQUIRED', lambda: self.reply('committee_recheck', 'quality'))
        self.assertTrue(self.s.detail('CASE-001')['correction_review_required'])
        self.op('submit')
        detail = self.op('decision', 'committee', decision='acknowledge_correction', reason='위원회 정정 확인')
        self.assertFalse(detail['correction_review_required'])
        self.op('conditions', changes={'confirmed_qty_override': 13000}, reason='추가 검사 정정')
        self.op('conditions', changes={'confirmed_qty_override': 14000}, reason='재검사 정정 복원')
        self.assertTrue(self.s.detail('CASE-001')['correction_review_required'])

    def test_completed_replies_can_be_corrected_to_unavailable(self):
        self.approved(quantity=1000)
        for kind in ('customer_scope_check', 'recall_scope', 'inventory_check'):
            self.reply(kind, 'sales')
        self.reply('production_schedule', 'production')
        self.reply('quality_release', 'quality')
        self.reply('customer_promise', 'sales')
        detail = self.reply('quality_release', 'quality', outcome='blocked')
        self.assertFalse(detail['conditions']['qa_release'])
        self.assertTrue(detail['recall_authorized'])
        self.assertNotEqual(self.task('customer_promise')['status'], 'done')
        self.reply('quality_release', 'quality')
        self.reply('customer_promise', 'sales')
        detail = self.reply('production_schedule', 'production', outcome='blocked')
        self.assertFalse(detail['conditions']['production_confirmed'])
        self.assertFalse(detail['analysis']['recovery_plan']['feasible'])
        self.assertNotEqual(self.task('customer_promise')['status'], 'done')

    def test_customer_negative_reply_creates_renegotiation(self):
        self.approved(quantity=1000)
        for kind in ('customer_scope_check', 'recall_scope', 'inventory_check'):
            self.reply(kind, 'sales')
        self.reply('production_schedule', 'production')
        self.reply('quality_release', 'quality')
        self.reply('customer_promise', 'sales')
        detail = self.reply('customer_promise', 'sales', outcome='blocked')
        plan = detail['analysis']['recovery_plan']
        self.assertTrue(plan['physical_supply_ready'])
        self.assertEqual(plan['customer_agreement'], 'blocked')
        self.assertFalse(plan['feasible'])
        self.assertEqual(detail['status'], 'review_required')
        self.assertEqual(self.task('customer_renegotiate')['status'], 'open')
        detail = self.reply('customer_promise', 'sales')
        self.assertEqual(detail['analysis']['recovery_plan']['customer_agreement'], 'confirmed')
        self.assertEqual(self.task('customer_renegotiate')['status'], 'done')
        self.assertTrue(detail['analysis']['recovery_plan']['feasible'])

    def test_session_identity_cannot_be_forged(self):
        response = self.s.session('sales')
        self.assertEqual('sales', self.s.authenticate(response['token']))
        self.assert_error('AUTH_REQUIRED', lambda: self.s.authenticate('committee'))
        self.assert_error('UNKNOWN_ACTOR', lambda: self.s.session('administrator'))

    def test_model_adapter_configuration_is_not_verified_connection(self):
        with patch.dict(os.environ, {'LLM_API_KEY': 'test-only', 'LLM_MODEL': 'test-model', 'LLM_BASE_URL': 'https://example.test/v1', 'AURORA_AI_ALLOW_NETWORK': '1'}, clear=True):
            adapter = ModelAdapter()
        self.assertTrue(adapter.status()['configured'])
        self.assertFalse(adapter.status()['connected'])
        self.assertEqual('configured_not_verified', adapter.status()['status'])
        grounded = {'facts': {**{k:self.s.detail('CASE-001')['analysis'][k] for k in ('confirmed_qty','produced_qty','defect_rate_pct','recall_eligible')}, 'recall_threshold_pct':10}, 'summary': '근거가 있는 모의 모델 답변', 'risks': [], 'alternatives': [], 'evidence_ids': ['QC-00001']}
        body = json.dumps({'choices': [{'message': {'content': json.dumps(grounded)}}]}).encode()
        with patch.object(adapter.opener, 'open', return_value=io.BytesIO(body)) as network:
            result = adapter.generate(self.s.detail('CASE-001'))
        self.assertTrue(result['evidence_ids_validated'])
        self.assertTrue(adapter.status()['connected'])
        self.assertEqual('none', result['approval_effect'])
        self.assertEqual('https://example.test/v1/chat/completions', network.call_args.args[0].full_url)

    def test_model_redirect_never_forwards_credentials_or_case_data(self):
        # Synthetic HTTPS transport: no network, no actual credentials.
        for code in (301, 302, 303, 307, 308):
            for target in ('http://other.test/steal', 'https://other.test/steal', 'https://provider.test/moved'):
                with self.subTest(code=code, target=target):
                    with patch.dict(os.environ, {'AURORA_AI_API_KEY':'TEST-ONLY-NOT-A-KEY',
                        'AURORA_AI_MODEL':'fixture', 'AURORA_AI_ENDPOINT':'https://provider.test/model',
                        'AURORA_AI_ALLOW_NETWORK':'1'}, clear=True):
                        adapter = ModelAdapter()
                    headers = Message(); headers['Location'] = target
                    response = urllib.response.addinfourl(io.BytesIO(b''), headers, adapter.url, code)
                    response.msg = 'Redirect fixture'
                    with patch.object(urllib.request.HTTPSHandler, 'https_open', return_value=response) as first, \
                         patch.object(urllib.request.HTTPHandler, 'http_open') as downgraded:
                        self.assert_error('AI_CALL_FAILED', lambda: adapter.generate(self.s.detail('CASE-001')))
                    self.assertEqual(first.call_count, 1)
                    downgraded.assert_not_called()
                    self.assertFalse(adapter.verified)

    def test_model_rejects_wrong_structured_facts_and_schema(self):
        with patch.dict(os.environ, {'AURORA_AI_API_KEY':'TEST-ONLY', 'AURORA_AI_MODEL':'fixture',
             'AURORA_AI_ALLOW_NETWORK':'1'}, clear=True):
            adapter = ModelAdapter()
        case = self.s.detail('CASE-001')
        valid = {'summary':'확정 불량률 11.87%, 회수 기준 10%', 'risks':[], 'alternatives':[],
                 'evidence_ids':['QC-00001'], 'facts':{**{k:case['analysis'][k] for k in
                  ('confirmed_qty','produced_qty','defect_rate_pct','recall_eligible')}, 'recall_threshold_pct':10}}
        mutations = [{'risks':'문자열은 배열이 아님'},
                     {'facts':{**valid['facts'],'recall_eligible':False}},
                     {'facts':{**valid['facts'],'defect_rate_pct':1}}, {'alternatives':[123]},
                     {'facts':{**valid['facts'],'recall_threshold_pct':20}}]
        for mutation in mutations:
            payload = {**valid, **mutation}
            body = json.dumps({'choices':[{'message':{'content':json.dumps(payload)}}]}).encode()
            with self.subTest(mutation=mutation), patch.object(adapter.opener,'open',return_value=io.BytesIO(body)):
                self.assert_error('AI_CALL_FAILED',lambda:adapter.generate(case))
        body = json.dumps({'choices':[{'message':{'content':json.dumps(valid)}}]}).encode()
        with patch.object(adapter.opener,'open',return_value=io.BytesIO(body)):
            result=adapter.generate(case)
        self.assertTrue(result['canonical_facts_validated'])
        self.assertIn('검증되지 않았습니다',result['verification_warning'])

    def test_model_adapter_rejects_fabricated_evidence(self):
        with patch.dict(os.environ, {'LLM_API_KEY': 'test-only', 'LLM_MODEL': 'test-model', 'AURORA_AI_ALLOW_NETWORK': '1'}, clear=True):
            adapter = ModelAdapter()
        answer = {'summary': 'unsupported', 'evidence_ids': ['SH-FAKE-001']}
        body = json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode()
        with patch.object(adapter.opener, 'open', return_value=io.BytesIO(body)):
            self.assert_error('AI_CALL_FAILED', lambda: adapter.generate(self.s.detail('CASE-001')))
        self.assertFalse(adapter.status()['connected'])

    def test_business_calendar_weekends(self):
        self.assertEqual('2026-07-06', add_business_days('2026-07-03', 1))
        self.assertEqual('2026-07-15', add_business_days('2026-07-01', 10))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = AuroraService(db_path=cls.tmp.name + '/http.sqlite3')
        cls.server = make_server(cls.service, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        h = {'Content-Type': 'application/json', **(headers or {})}
        request = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=h)
        try:
            with urllib.request.urlopen(request) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_static_ui_and_no_private_files(self):
        self.assertEqual(200, self.request('/')[0])
        self.assertEqual(200, self.request('/static/style.css')[0])
        for path in ('/backend.py', '/runtime/aurora.sqlite3', '/static/../backend.py', '/%2e%2e/backend.py'):
            self.assertEqual(404, self.request(path)[0])

    def test_landing_workspace_and_explicit_asset_routes(self):
        before = self.service.state()['cases']
        landing = self.request('/')
        self.assertEqual(200, landing[0])
        self.assertIn('품질 판단에서,'.encode(), landing[1])
        for path in ('/workspace', '/workspace/', '/workspace?case=CASE-002', '/static/index.html'):
            status, body, headers = self.request(path)
            self.assertEqual(200, status)
            self.assertIn(b'id="role-select"', body)
            self.assertIn('text/html', headers['Content-Type'])
        for path in ('/static/landing.css', '/static/landing.js', '/static/workspace.css', '/static/favicon.svg', '/static/common-nav.css', '/static/common-nav.js'):
            self.assertEqual(200, self.request(path)[0])
        for path in ('/static/../../backend.py', '/static/../local_secret.py', '/data/original/README.md', '/package.json', '/docs/LOCAL_SECRET_ENTRY.md'):
            self.assertEqual(404, self.request(path)[0])
        self.assertEqual(before, self.service.state()['cases'], 'Opening the landing and UI assets never changes a case')

    def test_http_auth_origin_and_complete_action(self):
        status, data, _ = self.request('/api/session', {'actor_id': 'quality'})
        token = json.loads(data)['token']
        self.assertEqual(200, status)
        self.assertEqual(401, self.request('/api/cases/CASE-001/submit', {'expected_version': 1})[0])
        self.assertEqual(403, self.request('/api/cases/CASE-001/submit', {'expected_version': 1}, {'Origin': 'https://malicious.test'})[0])
        h = {'Authorization': 'Bearer ' + token, 'Idempotency-Key': 'http-submit'}
        status, data, _ = self.request('/api/cases/CASE-001/submit', {'expected_version': 1}, h)
        self.assertEqual(200, status)
        self.assertEqual('awaiting_approval', json.loads(data)['status'])

    def test_http_supply_preview_validation_and_read_only_state(self):
        before = self.service.detail('CASE-001')
        endpoint = '/api/cases/CASE-001/supply-preview'
        status, body, headers = self.request(endpoint + '?quantity=1000&due_date=2026-07-20')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['candidate']['earliest_ship_date'], '2026-07-15')
        for query in ('quantity=0&due_date=2026-07-20', 'quantity=-1&due_date=2026-07-20',
                      'quantity=1.5&due_date=2026-07-20', 'quantity=1000',
                      'quantity=1&quantity=2&due_date=2026-07-20',
                      'quantity=1000&due_date=bad', 'quantity=1000&due_date=2026-07-20&unexpected=1'):
            self.assertEqual(self.request(endpoint + '?' + query)[0], 400)
        self.assertEqual(self.request(endpoint + '?quantity=1000&due_date=2026-07-20', headers={'Origin': 'https://untrusted.test'})[0], 403)
        self.assertEqual(before, self.service.detail('CASE-001'))

    def test_http_supply_preview_rejects_date_before_saved_effective_date(self):
        self.service.mutate('/api/cases/CASE-003/conditions', {'expected_version': self.service.detail('CASE-003')['version'],
            'changes': {'effective_date': '2026-08-01', 'customer_due_date': '2026-08-20'},
            'reason': '비교 HTTP 날짜 경계 검증'}, 'quality', 'effective-date-http')
        before = self.service.detail('CASE-003')
        endpoint = '/api/cases/CASE-003/supply-preview?quantity=1000&due_date='
        status, body, _ = self.request(endpoint + '2026-07-20')
        self.assertEqual(status, 400)
        error = json.loads(body)['error']
        self.assertEqual(error['code'], 'INVALID_CONDITIONS')
        self.assertIn('2026-08-01', error['message'])
        self.assertEqual(before, self.service.detail('CASE-003'))
        self.assertEqual(self.request(endpoint + '2026-08-01')[0], 200)
        self.assertEqual(before, self.service.detail('CASE-003'))

    def test_state_and_audit_get(self):
        self.assertEqual(480, json.loads(self.request('/api/state')[1])['summary']['total_batches'])
        self.assertTrue(json.loads(self.request('/api/audit/verify')[1])['valid'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
