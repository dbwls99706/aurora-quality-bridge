"""Offline cloud AI preparation tests. Only an explicit disposable local DB is used."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.parse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_budget import CloudBudgetLedger, BudgetError, export_history, POLICY, MODEL
from backend import ModelAdapter, APIError, AuroraService
from cloud_runtime import CloudPolicy
from test_axhub_runtime import CLOUD

SNAPSHOT = {'version': 1, 'policy': POLICY, 'model': MODEL,
            'prior_attempts': 1, 'prior_reserved_microusd': 210240, 'source_rows_sha256': 'a' * 64}
ENABLED = dict(CLOUD, AURORA_AI_ALLOW_NETWORK='1', AURORA_AI_POLICY=POLICY,
               AURORA_AI_PROVIDER='anthropic', AURORA_AI_MODEL=MODEL,
               AURORA_AI_API_KEY='TEST-ONLY-NOT-A-REAL-KEY')


class CloudProfileTests(unittest.TestCase):
    def test_only_explicit_same_provider_profile_allowed(self):
        with patch.dict(os.environ, ENABLED, clear=True):
            self.assertTrue(CloudPolicy().enabled)
        for key, value in [('AURORA_AI_POLICY', ''), ('AURORA_AI_PROVIDER', 'openai'),
                           ('AURORA_AI_MODEL', 'new-model'), ('AURORA_AI_ENDPOINT', 'https://other.test'),
                           ('AURORA_AI_ALLOW_NETWORK', 'true'), ('DATABASE_URL', '')]:
            with self.subTest(key=key), patch.dict(os.environ, dict(ENABLED, **{key:value}), clear=True):
                with self.assertRaises(ValueError): CloudPolicy()
                with self.assertRaises(ValueError): ModelAdapter()

    def test_unsafe_cloud_limits_fail_before_network(self):
        for key, value in [('AURORA_AI_MAX_REQUESTS','4'), ('AURORA_AI_BUDGET_USD','1.01'),
                           ('AURORA_AI_MAX_OUTPUT_TOKENS','2049')]:
            with patch.dict(os.environ, dict(ENABLED, **{key:value}), clear=True):
                adapter = ModelAdapter()
                with patch.object(adapter.opener, 'open', side_effect=AssertionError('Network forbidden')):
                    with self.assertRaises(APIError) as caught: adapter.generate({})
                self.assertEqual(caught.exception.code, 'AI_CONFIGURATION_INVALID')

    def test_approved_single_egress_but_docker_still_defaults_off(self):
        root = Path(__file__).resolve().parents[1]
        network = (root/'axhub.yaml').read_text().split('network:\n',1)[1].split('env:\n',1)[0]
        self.assertEqual(network, '  egress:\n    - host: api.anthropic.com\n      port: 443\n')
        self.assertIn('AURORA_AI_ALLOW_NETWORK=0', (root/'Dockerfile').read_text())

    def test_export_requires_existing_reconciled_reservations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'budget.sqlite3'
            with self.assertRaises(BudgetError): export_history(path)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE ai_reservations (id INTEGER PRIMARY KEY,created_at TEXT,model TEXT,reserved_microusd INTEGER)')
            with self.assertRaises(BudgetError): export_history(path)
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO ai_reservations VALUES (1,?,?,?)', ('2026-11-09',MODEL,210240))
            result = export_history(path)
            self.assertEqual((result['prior_attempts'], result['prior_reserved_microusd']), (1,210240))
            self.assertEqual(export_history(path), result)
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO ai_reservations VALUES (2,?,?,?)', ('2026-11-10',MODEL,210240))
            self.assertEqual(export_history(path)['prior_attempts'], 2)


@unittest.skipUnless(os.environ.get('AURORA_TEST_POSTGRES_URL'), 'Needs explicitly disposable PostgreSQL test DB')
class CloudBudgetPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        class BeforeExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026,11,10,12,tzinfo=timezone.utc)
                return value if tz else value.replace(tzinfo=None)
        clock = patch('backend.datetime',BeforeExpiry)
        clock.start(); self.addCleanup(clock.stop)
        base = os.environ['AURORA_TEST_POSTGRES_URL']
        # This test helper never accepts external or production DB hosts.
        self.assertEqual(urllib.parse.urlsplit(base).hostname, '127.0.0.1')
        schema = 'ai_test_' + uuid.uuid4().hex
        with psycopg.connect(base, autocommit=True) as db:
            db.execute('CREATE SCHEMA ' + schema)
        parts = urllib.parse.urlsplit(base)
        self.url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode({'options':'-c search_path='+schema}, quote_via=urllib.parse.quote)))
        self.ledger = CloudBudgetLedger(self.url)
        self.ledger.initialize()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def assert_code(self, expected, fn):
        with self.assertRaises((BudgetError, APIError)) as caught: fn()
        self.assertEqual(caught.exception.code, expected)

    def test_empty_history_blocks_and_never_autoseeds(self):
        self.assert_code('AI_BUDGET_HISTORY_REQUIRED', lambda: self.ledger.reserve(210240,3,1000000))
        self.ledger.initialize()
        self.assertFalse(self.ledger.status()['history_reconciled'])
        self.assert_code('AI_BUDGET_HISTORY_REQUIRED', lambda: self.ledger.import_history(SNAPSHOT))

    def test_import_idempotent_but_changed_history_refused(self):
        self.assertEqual(self.ledger.import_history(SNAPSHOT, True), 'imported')
        self.ledger.reserve(210240,3,1000000)
        self.assertEqual(self.ledger.import_history(SNAPSHOT, True), 'already_imported')
        self.assertEqual(self.ledger.status()['total_attempts_reserved'], 2)
        changed = dict(SNAPSHOT, prior_attempts=2, prior_reserved_microusd=420480)
        self.assert_code('AI_BUDGET_HISTORY_CONFLICT', lambda: self.ledger.import_history(changed,True))
        self.assertEqual(self.ledger.status()['total_attempts_reserved'], 2)

    def test_parallel_instances_share_exact_total_and_restart(self):
        self.ledger.import_history(SNAPSHOT,True)
        def reserve(_):
            try: CloudBudgetLedger(self.url).reserve(210240,3,1000000); return 'ok'
            except BudgetError as e: return e.code
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(reserve,range(8)))
        self.assertEqual(results.count('ok'),2)
        self.assertEqual(results.count('AI_USAGE_LIMIT'),6)
        restarted = CloudBudgetLedger(self.url); restarted.initialize()
        self.assertEqual(restarted.status()['total_attempts_reserved'],3)
        self.assertAlmostEqual(restarted.status()['total_cost_reserved_usd'],0.63072)
        self.assert_code('AI_USAGE_LIMIT',lambda: restarted.reserve(210240,3,1000000))

    def test_money_limit_and_hard_ceiling(self):
        self.ledger.import_history(SNAPSHOT,True)
        self.assert_code('AI_USAGE_LIMIT',lambda:self.ledger.reserve(210240,3,300000))
        self.assert_code('AI_CONFIGURATION_INVALID',lambda:self.ledger.reserve(210240,4,1000000))
        self.assert_code('AI_CONFIGURATION_INVALID',lambda:self.ledger.reserve(210240,3,1000001))

    def test_missing_imported_reservation_fails_closed(self):
        self.ledger.import_history(SNAPSHOT,True)
        # Only this freshly initialized disposable test schema is mutated.
        with self.ledger.connect() as db:
            db.execute('UPDATE ai_cloud_budget_reservations SET attempts=2')
        self.assert_code('AI_BUDGET_UNAVAILABLE', lambda:self.ledger.reserve(210240,3,1000000))
        self.assertFalse(self.ledger.status()['ledger_readable'])
        self.assert_code('AI_BUDGET_UNAVAILABLE',lambda:self.ledger.import_history(SNAPSHOT,True))

    def test_db_failure_is_sanitized_and_no_sqlite_fallback(self):
        with patch.object(self.ledger,'connect',side_effect=RuntimeError('sensitive DB detail')):
            self.assert_code('AI_BUDGET_UNAVAILABLE',lambda:self.ledger.reserve(210240,3,1000000))
            self.assertNotIn('sensitive',str(self.ledger.status()))
        self.assertFalse((Path(self.tmp.name)/'anthropic_budget.sqlite3').exists())

    def cloud_adapter(self):
        with patch.dict(os.environ,dict(ENABLED,DATABASE_URL=self.url,DIRECT_DATABASE_URL=self.url),clear=True):
            return ModelAdapter(budget_db_path=Path(self.tmp.name)/'should-not-exist.sqlite3')

    def test_adapter_uses_persistent_cloud_ledger(self):
        adapter = self.cloud_adapter()
        self.assert_code('AI_BUDGET_HISTORY_REQUIRED',adapter._reserve_anthropic_budget)
        self.ledger.import_history(SNAPSHOT,True)
        self.assertEqual(adapter._reserve_anthropic_budget(),210240)
        self.assertEqual(self.cloud_adapter().status()['usage_limits']['total_attempts_reserved'],2)
        self.assertEqual(adapter.status()['usage_limits']['budget_scope'],'persistent_app_postgres_reservations')
        self.assertFalse((Path(self.tmp.name)/'should-not-exist.sqlite3').exists())

    def test_expiry_still_blocks_before_reservation(self):
        class Expired(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,11,12,tzinfo=timezone.utc)
        self.ledger.import_history(SNAPSHOT,True)
        with patch('backend.datetime',Expired):
            self.assert_code('AI_CREDIT_EXPIRED',self.cloud_adapter()._reserve_anthropic_budget)
        self.assertEqual(self.ledger.status()['total_attempts_reserved'],1)

    def test_adapter_failure_retains_reservation_without_retry(self):
        from test_anthropic_adapter import AnthropicAdapterTests
        import urllib.error
        with patch.dict(os.environ,{},clear=True):
            service = AuroraService(db_path=Path(self.tmp.name)/'fixture.sqlite3')
        fixture = AnthropicAdapterTests()
        fixture.case = service.detail('CASE-001')
        adapter = self.cloud_adapter()
        with patch.object(adapter.opener,'open',side_effect=AssertionError('Network forbidden')) as transport:
            self.assert_code('AI_BUDGET_HISTORY_REQUIRED',lambda:adapter.generate(fixture.case))
        transport.assert_not_called()
        self.ledger.import_history(SNAPSHOT,True)
        with patch.object(adapter.opener,'open',side_effect=urllib.error.URLError(TimeoutError('hidden'))) as transport:
            self.assert_code('AI_NETWORK_TIMEOUT',lambda:adapter.generate(fixture.case))
        self.assertEqual(transport.call_count,1)
        self.assertEqual(self.ledger.status()['total_attempts_reserved'],2)
        # A new process retains the failed call reservation. Success is fixture-only.
        adapter = self.cloud_adapter()
        with patch.object(adapter.opener,'open',side_effect=fixture.transport()) as transport:
            result = adapter.generate(fixture.case)
        self.assertEqual(transport.call_count,2)
        self.assertEqual(result['approval_effect'],'none')
        self.assertEqual(self.ledger.status()['total_attempts_reserved'],3)
        with patch.object(adapter.opener,'open',side_effect=AssertionError('Network forbidden')) as transport:
            self.assert_code('AI_USAGE_LIMIT',lambda:adapter.generate(fixture.case))
        transport.assert_not_called()
        self.assertFalse((Path(self.tmp.name)/'should-not-exist.sqlite3').exists())

    def test_status_outside_application_transaction_retains_budget(self):
        self.ledger.import_history(SNAPSHOT,True)
        with patch.dict(os.environ,dict(ENABLED,DATABASE_URL=self.url,DIRECT_DATABASE_URL=self.url),clear=True):
            service = AuroraService(db_path=Path(self.tmp.name)/'unused.sqlite3')
        # Live budget status is now enriched only after the business transaction
        # exits; distinct lock keys alone did not prevent pool starvation.
        detail = service.detail('CASE-001')
        self.assertEqual(detail['ai']['usage_limits']['total_attempts_reserved'],1)
        with service.connect() as db:
            with patch.object(service.ai, 'status', side_effect=AssertionError('Nested live status forbidden')):
                snapshot = service._detail('CASE-001',db)
            self.assertNotIn('usage_limits', snapshot['ai'])
        self.assertFalse((Path(self.tmp.name)/'unused.sqlite3').exists())


if __name__ == '__main__':unittest.main()
