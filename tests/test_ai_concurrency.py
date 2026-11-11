"""Offline AI locking/race regressions. Every model response is a test fixture."""
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import APIError, AuroraService


class AIConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'state.sqlite3')
        with patch.dict(os.environ, {}, clear=True):
            self.service = AuroraService(db_path=self.db)
        self.sequence = 0

    @staticmethod
    def fixture(case, label='fixture'):
        return {'kind': 'test_fixture', 'model': 'TEST ONLY', 'prompt_hash': label,
                'review_revision': case['review_revision'],
                'structured': {'summary': label, 'risks': [], 'alternatives': [],
                               'evidence_ids': ['fixture']}}

    def mutate(self, operation, case='CASE-001', actor='quality', service=None, **payload):
        service = service or self.service
        self.sequence += 1
        payload.setdefault('expected_version', service.detail(case)['version'])
        return service.mutate(f'/api/cases/{case}/{operation}', payload, actor, f'test-{self.sequence}')

    def ai(self, key='ai', version=None, service=None):
        service = service or self.service
        if version is None:
            version = service.detail('CASE-001')['version']
        return service.mutate('/api/cases/CASE-001/ai-brief', {'expected_version': version}, 'quality', key)

    def expect_error(self, code, callback):
        with self.assertRaises(APIError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def blocking_fixture(self, entered, release):
        def generate(case):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Test did not release offline model fixture')
            return self.fixture(case)
        return generate

    def assert_clean(self):
        self.assertEqual({}, self.service._ai_inflight)
        self.assertTrue(self.service.verify_audit()['valid'])

    def test_slow_ai_does_not_block_other_case_in_same_service(self):
        entered, release = threading.Event(), threading.Event()
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ai = pool.submit(self.ai)
                try:
                    self.assertTrue(entered.wait(5))
                    other = pool.submit(self.mutate, 'submit', case='CASE-002').result(timeout=3)
                    self.assertEqual('awaiting_approval', other['status'])
                    self.assertFalse(ai.done())
                finally:
                    release.set()
                result = ai.result(timeout=5)
        self.assertFalse(result['ai']['brief']['stale'])
        self.assertEqual(2, result['version'])
        self.assert_clean()

    def test_slow_ai_releases_sqlite_transaction_for_other_service(self):
        with patch.dict(os.environ, {}, clear=True):
            other_service = AuroraService(db_path=self.db)
        entered, release = threading.Event(), threading.Event()
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ai = pool.submit(self.ai)
                try:
                    self.assertTrue(entered.wait(5))
                    other = pool.submit(self.mutate, 'submit', case='CASE-002', service=other_service).result(timeout=3)
                    self.assertEqual(2, other['version'])
                    self.assertFalse(ai.done())
                finally:
                    release.set()
                self.assertFalse(ai.result(timeout=5)['ai']['brief']['stale'])
        self.assert_clean()

    def test_same_case_change_discards_late_result_and_preserves_old_brief(self):
        with patch.object(self.service.ai, 'generate', side_effect=lambda case: self.fixture(case, 'old')):
            old = self.ai('old')
        entered, release = threading.Event(), threading.Event()
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ai = pool.submit(self.ai, 'slow', old['version'])
                try:
                    self.assertTrue(entered.wait(5))
                    changed = pool.submit(self.mutate, 'conditions', changes={'replacement_qty': 1000},
                                          reason='Offline concurrent quantity correction').result(timeout=3)
                finally:
                    release.set()
                self.expect_error('STALE_VERSION', lambda: ai.result(timeout=5))
        after = self.service.detail('CASE-001')
        self.assertEqual(changed['version'], after['version'])
        self.assertEqual(changed['conditions'], after['conditions'])
        self.assertEqual(changed['tasks'], after['tasks'])
        self.assertEqual('old', after['ai']['brief']['prompt_hash'])
        self.assertTrue(after['ai']['brief']['stale'])
        self.assert_clean()

    def test_shared_capacity_change_invalidates_snapshot_without_case_version_change(self):
        for case in ('CASE-001', 'CASE-002'):
            self.mutate('conditions', case=case, changes={'replacement_qty': 1000},
                        reason='Offline shared capacity fixture')
        self.mutate('submit', case='CASE-002')
        approved = self.mutate('decision', case='CASE-002', actor='committee', decision='approve',
                               reason='Offline committee fixture')
        task = next(t for t in approved['tasks'] if t['type'] == 'production_schedule')
        before = self.service.detail('CASE-001')
        entered, release = threading.Event(), threading.Event()
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ai = pool.submit(self.ai)
                try:
                    self.assertTrue(entered.wait(5))
                    pool.submit(self.mutate, 'tasks/' + task['id'] + '/reply', case='CASE-002',
                                actor='production', outcome='confirmed', note='Offline slot confirmation').result(timeout=3)
                    current = self.service.detail('CASE-001')
                    self.assertEqual(before['version'], current['version'])
                    self.assertNotEqual(self.service._ai_input_fingerprint(before),
                                        self.service._ai_input_fingerprint(current))
                finally:
                    release.set()
                self.expect_error('STALE_VERSION', lambda: ai.result(timeout=5))
        self.assertIsNone(self.service.detail('CASE-001')['ai']['brief'])
        self.assert_clean()

    def test_duplicate_inflight_key_calls_model_once_and_replays(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        original_wait = self.service._ai_condition.wait
        def observe_wait(*args, **kwargs):
            waiting.set()
            return original_wait(*args, **kwargs)
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)) as model:
            with patch.object(self.service._ai_condition, 'wait', side_effect=observe_wait):
                with ThreadPoolExecutor(max_workers=3) as pool:
                    first = pool.submit(self.ai, 'same', 1)
                    try:
                        self.assertTrue(entered.wait(5))
                        second = pool.submit(self.ai, 'same', 1)
                        self.assertTrue(waiting.wait(5))
                        pool.submit(self.mutate, 'submit', case='CASE-002').result(timeout=3)
                        self.assertFalse(first.done())
                        self.assertFalse(second.done())
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=5), second.result(timeout=5))
            self.assertEqual(1, model.call_count)
        detail = self.service.detail('CASE-001')
        self.assertEqual(2, detail['version'])
        self.assertEqual(1, sum(a['action'] == 'ai_brief_created' for a in detail['audit']))
        self.assert_clean()

    def test_inflight_key_cannot_be_claimed_by_different_request(self):
        entered, release = threading.Event(), threading.Event()
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)) as model:
            with ThreadPoolExecutor(max_workers=2) as pool:
                ai = pool.submit(self.ai, 'same', 1)
                try:
                    self.assertTrue(entered.wait(5))
                    conflict = pool.submit(self.service.mutate, '/api/cases/CASE-002/submit',
                                           {'expected_version': 1}, 'quality', 'same')
                    self.expect_error('IDEMPOTENCY_CONFLICT', lambda: conflict.result(timeout=3))
                    self.assertEqual(1, self.service.detail('CASE-002')['version'])
                finally:
                    release.set()
                self.assertFalse(ai.result(timeout=5)['ai']['brief']['stale'])
            self.assertEqual(1, model.call_count)
        self.assert_clean()

    def test_newer_ai_result_cannot_be_overwritten_by_older_call(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def generate(case):
            calls.append(case)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Test did not release offline model fixture')
                return self.fixture(case, 'slow')
            return self.fixture(case, 'winner')
        with patch.object(self.service.ai, 'generate', side_effect=generate):
            with ThreadPoolExecutor(max_workers=2) as pool:
                old = pool.submit(self.ai, 'slow', 1)
                try:
                    self.assertTrue(entered.wait(5))
                    winner = pool.submit(self.ai, 'fast', 1).result(timeout=3)
                    self.assertEqual('winner', winner['ai']['brief']['prompt_hash'])
                finally:
                    release.set()
                self.expect_error('STALE_VERSION', lambda: old.result(timeout=5))
        self.assertEqual(winner['version'], self.service.detail('CASE-001')['version'])
        self.assertEqual('winner', self.service.detail('CASE-001')['ai']['brief']['prompt_hash'])
        self.assert_clean()

    def test_failure_wakes_same_key_retry_without_leaking_inflight_state(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def generate(case):
            calls.append(case)
            if len(calls) == 1:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Test did not release offline model fixture')
                raise APIError('AI_CALL_FAILED', 'Offline failure fixture', 502)
            return self.fixture(case, 'retried')
        original_wait = self.service._ai_condition.wait
        def observe_wait(*args, **kwargs):
            waiting.set()
            return original_wait(*args, **kwargs)
        with patch.object(self.service.ai, 'generate', side_effect=generate):
            with patch.object(self.service._ai_condition, 'wait', side_effect=observe_wait):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(self.ai, 'same', 1)
                    try:
                        self.assertTrue(entered.wait(5))
                        second = pool.submit(self.ai, 'same', 1)
                        self.assertTrue(waiting.wait(5))
                    finally:
                        release.set()
                    self.expect_error('AI_CALL_FAILED', lambda: first.result(timeout=5))
                    self.assertEqual('retried', second.result(timeout=5)['ai']['brief']['prompt_hash'])
        self.assertEqual(2, len(calls))
        self.assert_clean()

    def test_source_integrity_is_rechecked_after_model_call(self):
        entered, release = threading.Event(), threading.Event()
        changed = threading.Event()
        original_verify = self.service.evidence.verify_unchanged
        def verify():
            if changed.is_set():
                raise APIError('SOURCE_CHANGED', 'Offline source integrity fixture', 409)
            return original_verify()
        with patch.object(self.service.evidence, 'verify_unchanged', side_effect=verify):
            with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    ai = pool.submit(self.ai)
                    try:
                        self.assertTrue(entered.wait(5))
                        changed.set()
                    finally:
                        release.set()
                    self.expect_error('SOURCE_CHANGED', lambda: ai.result(timeout=5))
        detail = self.service.detail('CASE-001')
        self.assertEqual(1, detail['version'])
        self.assertIsNone(detail['ai']['brief'])
        self.assert_clean()

    def test_snapshot_is_detached_and_fingerprint_is_captured_before_model_call(self):
        before = self.service.detail('CASE-001')
        fingerprint = self.service._ai_input_fingerprint(before)
        def generate(case):
            case['analysis']['supply_constraints']['product']['product_name'] = 'Changed only in fixture'
            case['claims'][0]['qty_affected'] = -123
            case['conditions']['replacement_qty'] = 1
            return self.fixture(case)
        with patch.object(self.service.ai, 'generate', side_effect=generate):
            result = self.ai()
        self.assertEqual(fingerprint, result['ai']['brief']['input_fingerprint'])
        self.assertFalse(result['ai']['brief']['stale'])
        self.assertEqual(before['conditions'], result['conditions'])
        self.assertEqual(before['claims'], result['claims'])
        self.assertEqual(before['analysis'], result['analysis'])
        self.assert_clean()

    def test_other_instance_same_key_commit_is_replayed_not_overwritten(self):
        with patch.dict(os.environ, {}, clear=True):
            other_service = AuroraService(db_path=self.db)
        entered, release = threading.Event(), threading.Event()
        with patch.object(self.service.ai, 'generate', side_effect=self.blocking_fixture(entered, release)):
            with patch.object(other_service.ai, 'generate', side_effect=lambda case: self.fixture(case, 'other')):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    slow = pool.submit(self.ai, 'same', 1)
                    try:
                        self.assertTrue(entered.wait(5))
                        winner = pool.submit(self.ai, 'same', 1, other_service).result(timeout=3)
                    finally:
                        release.set()
                    self.assertEqual(winner, slow.result(timeout=5))
        detail = self.service.detail('CASE-001')
        self.assertEqual('other', detail['ai']['brief']['prompt_hash'])
        self.assertEqual(2, detail['version'])
        self.assertEqual(1, sum(a['action'] == 'ai_brief_created' for a in detail['audit']))
        self.assert_clean()

    def test_unexpected_model_exception_releases_lock_and_allows_retry(self):
        with patch.object(self.service.ai, 'generate', side_effect=RuntimeError('Offline unexpected fixture')):
            with self.assertRaisesRegex(RuntimeError, 'Offline unexpected fixture'):
                self.ai('retry', 1)
        with patch.object(self.service.ai, 'generate', side_effect=self.fixture):
            self.assertEqual(2, self.ai('retry', 1)['version'])
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
