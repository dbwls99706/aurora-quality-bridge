"""No provider calls. Guard connection lifetimes and constrained-pool liveness."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import AuroraService
from persistence import PostgresConnection
from ai_budget import CloudBudgetLedger


class TrackedConnection:
    def __init__(self, raw, active):
        self.raw, self.active = raw, active
        self.started = False

    def execute(self, *args):
        if not self.started:
            self.started = True
            self.active.add(id(self))
        return self.raw.execute(*args)

    def executescript(self, *args):
        self.started = True
        self.active.add(id(self))
        return self.raw.executescript(*args)

    def clear(self):
        self.started = False
        self.active.discard(id(self))

    def commit(self):
        try: return self.raw.commit()
        finally: self.clear()

    def rollback(self):
        try: return self.raw.rollback()
        finally: self.clear()

    def close(self):
        try: return self.raw.close()
        finally: self.clear()

    def __enter__(self): return self

    def __exit__(self, kind, value, traceback):
        try: self.rollback() if kind else self.commit()
        finally: self.close()


class DetailTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            self.service = AuroraService(db_path=self.tmp.name + '/state.sqlite3')
        self.active = set()
        real = self.service.connect
        self.service.connect = lambda migration=False: TrackedConnection(real(migration), self.active)
        self.status_calls = 0
        self.generated = False
        status = self.service.ai.status
        def guarded_status():
            self.assertFalse(self.active, 'Live AI status must never open a budget connection while a business transaction is active')
            self.status_calls += 1
            return {**status(), 'test_generation_seen': self.generated}
        self.service.ai.status = guarded_status
        self.service.ai.opener.open = lambda *a, **k: self.fail('Real provider network forbidden')

    def test_detail_and_state_enrich_after_transaction(self):
        before = self.service.state()
        for case in before['cases']:
            d = self.service.detail(case['id'])
            self.assertIn('usage_limits', d['ai'])
            self.assertIn('brief', d['ai'])
            self.assertIn('reviews', d['ai'])
        after = self.service.state()
        self.assertEqual(before, after)
        self.assertEqual(self.status_calls, 5)

    def test_preview_and_transaction_assembly_never_query_status(self):
        with self.service.connect() as db:
            detail = self.service._detail('CASE-001', db)
            self.assertNotIn('usage_limits', detail['ai'])
            self.assertIn('reviews', detail['ai'])
        self.service.supply_preview('CASE-001', 100, '2026-08-01')
        self.assertEqual(self.status_calls, 0)

    def test_mutation_and_idempotency_keep_full_exact_response(self):
        path = '/api/cases/CASE-001/submit'
        payload = {'expected_version': 1}
        first = self.service.mutate(path, payload, 'quality', 'same-response')
        second = self.service.mutate(path, payload, 'quality', 'same-response')
        self.assertEqual(first, second)
        self.assertIn('usage_limits', first['ai'])
        self.assertEqual(first['version'], 2)
        self.assertTrue(self.service.verify_audit()['valid'])

    def test_ai_generation_and_review_do_not_reintroduce_nested_status(self):
        def fixture(case):
            self.assertFalse(self.active)
            self.generated = True
            return {'kind': 'test_fixture', 'model': 'TEST ONLY', 'prompt_hash': 'offline-fixture',
                    'review_revision': case['review_revision'],
                    'structured': {'summary': '격리 시험 의견', 'risks': [], 'alternatives': [], 'evidence_ids': []}}
        self.service.ai.generate = fixture
        submitted = self.service.mutate('/api/cases/CASE-001/submit', {'expected_version': 1}, 'quality', 'fixture-submit')
        result = self.service.mutate('/api/cases/CASE-001/ai-brief', {'expected_version': submitted['version']}, 'quality', 'fixture-ai')
        self.assertTrue(result['ai']['test_generation_seen'], 'Mutation must refresh informational status after generation')
        review = self.service.mutate('/api/cases/CASE-001/ai-review', {
            'expected_version': result['version'], 'brief_id': result['ai']['brief']['id'],
            'field': 'summary', 'index': 0, 'disposition': 'defer', 'note': '격리된 시험 의견 추가 확인'}, 'quality', 'fixture-review')
        review_id = review['ai']['reviews'][0]['id']
        latest = self.service.detail('CASE-001')
        decision = self.service.mutate('/api/cases/CASE-001/decision', {
            'expected_version': latest['version'], 'decision': 'approve', 'reason': '격리된 모의 의결 근거 확인',
            'ai_review_id': review_id}, 'committee', 'fixture-decision')
        self.assertEqual(decision['status'], 'approved')
        self.assertTrue(self.service.verify_audit()['valid'])


class TransactionPool:
    """Constrain actual local PG transactions, not a mock of SQL or row results."""
    def __init__(self, size):
        self.slots = threading.BoundedSemaphore(size)
        self.barrier = None
        self.starved = []

    def connect(self, *args, **kwargs):
        import psycopg
        return PooledConnection(psycopg.connect(*args, **kwargs), self)


class PooledConnection:
    def __init__(self, raw, pool): self.raw, self.pool, self.held = raw, pool, False

    def execute(self, sql, params=()):
        if not self.held:
            if not self.pool.slots.acquire(timeout=0.8):
                self.pool.starved.append(sql)
                raise TimeoutError('Disposable local pool slot exhausted')
            self.held = True
            if self.pool.barrier and sql.startswith('SELECT pg_advisory_xact_lock') and params == (718401216,):
                self.pool.barrier.wait(timeout=3)
        return self.raw.execute(sql, params)

    def release(self):
        if self.held:
            self.held = False
            self.pool.slots.release()

    def commit(self):
        try: self.raw.commit()
        finally: self.release()

    def rollback(self):
        try: self.raw.rollback()
        finally: self.release()

    def close(self):
        try: self.raw.close()
        finally: self.release()


@unittest.skipUnless(os.environ.get('AURORA_TEST_POSTGRES_URL'), 'Needs explicitly disposable local PostgreSQL test DB')
class ConstrainedPoolTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        url = os.environ['AURORA_TEST_POSTGRES_URL']
        parsed = urllib.parse.urlsplit(url)
        self.assertIn(parsed.hostname, ('127.0.0.1', 'localhost'), 'Never run pool fault tests against a remote DB')
        schema = 'read_liveness_' + uuid.uuid4().hex
        with psycopg.connect(url, autocommit=True) as admin: admin.execute('CREATE SCHEMA ' + schema)
        params = urllib.parse.parse_qsl(parsed.query)
        self.assertFalse(any(k == 'options' for k, _ in params))
        params.append(('options', '-c search_path=' + schema))
        self.url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(params, quote_via=urllib.parse.quote)))
        with patch.dict(os.environ, {'DATABASE_URL': self.url, 'DIRECT_DATABASE_URL': self.url}, clear=True):
            self.service = AuroraService()
        ledger = CloudBudgetLedger(self.url)
        ledger.initialize()
        self.service.ai.provider = 'anthropic'
        self.service.ai.cloud_budget = ledger
        self.service.ai.opener.open = lambda *a, **k: self.fail('Real provider network forbidden')

    def limited(self, size):
        pool = TransactionPool(size)
        self.service.connect = lambda migration=False: PostgresConnection(self.url, connector=pool.connect)
        self.service.ai.cloud_budget.connector = pool.connect
        return pool

    def test_one_slot_sequential_detail_and_mutation(self):
        pool = self.limited(1)
        for cid in ('CASE-001', 'CASE-002', 'CASE-003'):
            self.assertTrue(self.service.detail(cid)['ai']['usage_limits']['ledger_readable'])
        result = self.service.mutate('/api/cases/CASE-001/submit', {'expected_version': 1}, 'quality', 'single-slot')
        self.assertTrue(result['ai']['usage_limits']['ledger_readable'])
        self.assertEqual(result, self.service.mutate('/api/cases/CASE-001/submit', {'expected_version': 1}, 'quality', 'single-slot'))
        self.assertEqual(pool.starved, [])

    def test_one_slot_parallel_http_reads_and_mutation_complete(self):
        import http.client
        from backend import make_server
        pool = self.limited(1)
        with patch.dict(os.environ, {}, clear=True):
            server = make_server(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def get(path):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
            finally: connection.close()
        try:
            with ThreadPoolExecutor(max_workers=5) as executor:
                reads = [executor.submit(get, path) for path in (
                    '/api/cases/CASE-001', '/api/cases/CASE-002', '/api/cases/CASE-003', '/api/state')]
                mutation = executor.submit(self.service.mutate, '/api/cases/CASE-001/submit',
                                           {'expected_version': 1}, 'quality', 'parallel-single-slot')
                results = [future.result(timeout=7) for future in reads]
                results.append(mutation.result(timeout=7))
            self.assertTrue(all(result['ai']['usage_limits']['ledger_readable'] for result in results))
            self.assertEqual(pool.starved, [])
            self.assertEqual(self.service.detail('CASE-001')['version'], 2)
            self.assertTrue(self.service.verify_audit()['valid'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_two_slots_parallel_details_do_not_starve_budget(self):
        pool = self.limited(2)
        before = self.service.state()
        pool.barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.service.detail, cid) for cid in ('CASE-002', 'CASE-003')]
            results = [f.result(timeout=5) for f in futures]
        pool.barrier = None
        self.assertTrue(all(d['ai']['usage_limits']['ledger_readable'] for d in results))
        self.assertEqual(pool.starved, [])
        self.assertEqual(before, self.service.state())


if __name__ == '__main__': unittest.main()
