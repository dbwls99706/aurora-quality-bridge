"""Offline authorization tests. PostgreSQL tests accept loopback disposable DB only.

All provider transports are mocked; no real credentials or external calls.
"""
import copy
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_budget import (CloudBudgetLedger, BudgetError, canonical, supplemental_snapshot,
    validate_supplemental, prepare_supplemental, supplemental_import_sql,
    POLICY, MODEL, SUPPLEMENTAL_ID, SUPPLEMENTAL_EXPIRES, SOURCE_FILE_SHA256)
from backend import APIError, ModelAdapter, AuroraService
from persistence import PostgresConnection
from test_cloud_ai_budget import ENABLED, SNAPSHOT
import test_anthropic_adapter

class BeforeExpiry(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 11, 12, tzinfo=timezone.utc)
        return value if tz else value.replace(tzinfo=None)

class Expired(BeforeExpiry):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 12, tzinfo=timezone.utc)
        return value if tz else value.replace(tzinfo=None)


def process_claim(url):
    with patch('ai_budget.datetime', BeforeExpiry):
        try:
            CloudBudgetLedger(url).reserve(210240, 3, 1000000)
            return 'ok'
        except BudgetError as error:
            return error.code


class SupplementalOfflineTests(unittest.TestCase):
    def test_snapshot_separates_fact_and_uncertainty(self):
        snapshot = supplemental_snapshot()
        self.assertFalse(snapshot['history_reconciled'])
        self.assertEqual(snapshot['observed_history']['rows'],
            [[1, '2026-11-09T14:23:32.049664Z', MODEL, 210240]])
        self.assertEqual(snapshot['observed_history']['source_file_sha256'], SOURCE_FILE_SHA256)
        self.assertEqual(snapshot['observed_history']['attempts'], 1)
        self.assertEqual(snapshot['uncertainty_reserve']['assumed_additional_attempts'], 2)
        self.assertFalse(snapshot['uncertainty_reserve']['is_observed_history'])
        self.assertFalse(snapshot['uncertainty_reserve']['bounds_account_usage'])
        self.assertEqual(snapshot['maximum_new_attempts'], 1)
        self.assertEqual(snapshot['maximum_new_reserved_microusd'], 210240)
        self.assertEqual(snapshot['expires_at'], SUPPLEMENTAL_EXPIRES)

    def test_changed_policy_model_expiry_scope_types_and_evidence_rejected(self):
        original = supplemental_snapshot()
        for key, value in [('authorization_id', SUPPLEMENTAL_ID+'-new'), ('model','new-model'),
            ('expires_at','2026-12-12T00:00:00+00:00'), ('maximum_new_attempts',2),
            ('maximum_new_attempts',True), ('maximum_new_reserved_microusd',210241),
            ('history_reconciled',True), ('policy','new-policy'), ('extra',None)]:
            with self.subTest(key=key):
                changed = dict(original, **{key:value})
                with self.assertRaises(BudgetError): validate_supplemental(changed)
        changed = copy.deepcopy(original)
        changed['observed_history']['rows'][0][1] = '2026-11-09T00:00:00Z'
        with self.assertRaises(BudgetError): validate_supplemental(changed)

    def test_prepare_does_not_read_env_connect_or_touch_network(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory)/'unverified.sqlite3'
            ledger.write_bytes(b'not the observed ledger')
            with patch.dict(os.environ, {}, clear=True), patch('sqlite3.connect',side_effect=AssertionError('unverified bytes must block first')), patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
                with self.assertRaises(BudgetError): prepare_supplemental(ledger)
                with self.assertRaises(BudgetError): prepare_supplemental(ledger,True)
                with self.assertRaises(BudgetError): prepare_supplemental(Path(directory)/'missing',True)

    def test_generated_sql_is_single_no_reset_statement(self):
        sql = supplemental_import_sql(supplemental_snapshot())
        body = '\n'.join(line for line in sql.splitlines() if not line.startswith('--'))
        self.assertEqual(body.rstrip()[-1],';')
        self.assertNotIn(';\n',body.rstrip()[:-1])
        for word in ['UPDATE ', 'DELETE ', 'CREATE ', 'ON CONFLICT', 'DATABASE_URL', 'API_KEY']:
            self.assertNotIn(word, body)
        self.assertIn('pg_advisory_xact_lock(718401217)', sql)
        self.assertIn('new_receipt',sql)
        self.assertIn('already_imported',sql)
        self.assertEqual(sql,supplemental_import_sql(supplemental_snapshot()))


@unittest.skipUnless(os.environ.get('AURORA_TEST_POSTGRES_URL'), 'Needs explicitly disposable loopback PostgreSQL')
class SupplementalPostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.base = os.environ['AURORA_TEST_POSTGRES_URL']
        self.assertEqual(urllib.parse.urlsplit(self.base).hostname,'127.0.0.1')
        schema = 'supplemental_test_' + uuid.uuid4().hex
        with psycopg.connect(self.base,autocommit=True) as db:
            db.execute('CREATE SCHEMA '+schema)
        parts = urllib.parse.urlsplit(self.base)
        self.url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode({'options':'-c search_path='+schema},quote_via=urllib.parse.quote)))
        self.ledger = CloudBudgetLedger(self.url)
        self.ledger.initialize()
        self.snapshot = supplemental_snapshot()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        for module in ('ai_budget','backend'):
            clock = patch(module+'.datetime', BeforeExpiry)
            clock.start(); self.addCleanup(clock.stop)
        for handler, method in ((urllib.request.HTTPSHandler,'https_open'),(urllib.request.HTTPHandler,'http_open')):
            network = patch.object(handler,method,side_effect=AssertionError('External network forbidden'))
            network.start(); self.addCleanup(network.stop)

    def code(self, expected, callback):
        with self.assertRaises((BudgetError,APIError)) as caught: callback()
        self.assertEqual(caught.exception.code,expected)

    def import_authorization(self):
        return self.ledger.import_supplemental(self.snapshot,True)

    def reserve(self):
        return self.ledger.reserve(210240,3,1000000)

    def assert_consumed(self):
        status = self.ledger.status()
        self.assertFalse(status['history_reconciled'])
        self.assertEqual(status['supplemental_attempts_reserved'],1)
        self.assertEqual(status['supplemental_remaining_attempts'],0)
        self.assertEqual(status['supplemental_cost_reserved_usd'],.21024)
        self.assertIsNone(status['total_cost_reserved_usd'])
        self.assertIsNone(status['total_attempts_reserved'])

    def sql_import(self, sql=None):
        # Only tests substitute a deterministic DB clock. Production artifact
        # always uses PostgreSQL clock_timestamp(), with no override facility.
        sql = (sql or supplemental_import_sql(self.snapshot)).replace('clock_timestamp()', "'2026-11-11T00:00:00+00:00'::timestamptz")
        with self.ledger.connect() as db:
            return db.raw.execute(sql).fetchone()['supplemental_import_status']

    def adapter_and_fixture(self):
        with patch.dict(os.environ,{},clear=True):
            service = AuroraService(db_path=Path(self.temp.name)/'fixture.sqlite3')
        fixture = test_anthropic_adapter.AnthropicAdapterTests(); fixture.case=service.detail('CASE-001')
        with patch.dict(os.environ,dict(ENABLED,DATABASE_URL=self.url,DIRECT_DATABASE_URL=self.url),clear=True):
            adapter=ModelAdapter(budget_db_path=Path(self.temp.name)/'forbidden.sqlite3')
        return adapter,fixture

    def test_empty_startup_remains_blocked(self):
        self.code('AI_BUDGET_HISTORY_REQUIRED',self.reserve)
        self.ledger.initialize()
        self.code('AI_BUDGET_HISTORY_REQUIRED',self.reserve)
        adapter,_=self.adapter_and_fixture()
        self.assertFalse(adapter.status()['request_allowed'])
        self.assertFalse((Path(self.temp.name)/'forbidden.sqlite3').exists())

    def test_review_required_and_invalid_snapshot_cannot_import(self):
        self.code('AI_SUPPLEMENTAL_REVIEW_REQUIRED',lambda:self.ledger.import_supplemental(self.snapshot))
        self.code('AI_SUPPLEMENTAL_CONFLICT',lambda:self.ledger.import_supplemental(dict(self.snapshot,maximum_new_attempts=2),True))
        self.code('AI_BUDGET_HISTORY_REQUIRED',self.reserve)

    def test_new_import_idempotent_before_and_after_use(self):
        self.assertEqual(self.import_authorization(),'imported')
        self.assertEqual(self.import_authorization(),'already_imported')
        status=self.ledger.status()
        self.assertEqual(status['observed_prior_attempts'],1)
        self.assertEqual(status['observed_prior_reserved_usd'],.21024)
        self.assertEqual(status['uncertainty_reserve_usd'],.42048)
        self.assertEqual(status['supplemental_remaining_attempts'],1)
        self.assertEqual(self.reserve(),210240)
        self.assertEqual(self.import_authorization(),'already_imported')
        self.assert_consumed()
        self.code('AI_USAGE_LIMIT',self.reserve)

    def test_new_process_restart_does_not_reset(self):
        self.import_authorization(); self.reserve()
        restarted=CloudBudgetLedger(self.url); restarted.initialize()
        self.code('AI_USAGE_LIMIT',lambda:restarted.reserve(210240,3,1000000))
        self.assertEqual(restarted.import_supplemental(self.snapshot,True),'already_imported')
        self.assert_consumed()

    def test_eight_processes_claim_exactly_one(self):
        self.import_authorization()
        with ProcessPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(process_claim,[self.url]*8))
        self.assertEqual(results.count('ok'),1)
        self.assertEqual(results.count('AI_USAGE_LIMIT'),7)
        self.assert_consumed()

    def test_db_unique_key_protects_independently_without_advisory_lock(self):
        import psycopg
        self.import_authorization()
        with self.ledger.connect() as db:
            auth,_=self.ledger.supplemental_state(db)
        def raw_claim(_):
            try:
                with psycopg.connect(self.url) as db:
                    db.execute('INSERT INTO ai_cloud_supplemental_claims VALUES (%s,%s,%s,%s,%s,%s)',
                        (SUPPLEMENTAL_ID,str(uuid.uuid4()),auth['snapshot_sha256'],'2026-11-11T12:00:00+00:00',MODEL,210240))
                return 'ok'
            except psycopg.errors.UniqueViolation:return 'unique_blocked'
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(raw_claim,range(6)))
        self.assertEqual(results.count('ok'),1)
        self.assertEqual(results.count('unique_blocked'),5)
        # An isolated claim without its consumed marker is corruption, not ready.
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)

    def test_failure_at_count_endpoint_consumes_without_retry(self):
        self.import_authorization(); adapter,fixture=self.adapter_and_fixture()
        with patch.object(adapter.opener,'open',side_effect=urllib.error.URLError(TimeoutError('fixture'))) as transport:
            self.code('AI_NETWORK_TIMEOUT',lambda:adapter.generate(fixture.case))
        self.assertEqual(transport.call_count,1)
        self.assert_consumed()
        restarted,_=self.adapter_and_fixture()
        with patch.object(restarted.opener,'open',side_effect=AssertionError('No second attempt')) as transport:
            self.code('AI_USAGE_LIMIT',lambda:restarted.generate(fixture.case))
        transport.assert_not_called()
        self.assertFalse(restarted.status()['request_allowed'])
        self.assertEqual(restarted.status()['status'],'supplemental_exhausted')

    def test_success_claim_is_committed_before_both_provider_requests(self):
        self.import_authorization(); adapter,fixture=self.adapter_and_fixture()
        calls=[]
        def transport(request,timeout):
            # Independent connection can observe the committed claim before I/O.
            self.assert_consumed()
            calls.append(request.full_url)
            return fixture.transport()(request,timeout)
        with patch.object(adapter.opener,'open',side_effect=transport):
            result=adapter.generate(fixture.case)
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[0].endswith('/count_tokens'))
        self.assertEqual(calls[1],adapter.ANTHROPIC_URL)
        self.assertEqual(result['approval_effect'],'none')
        self.assertFalse(adapter.status()['request_allowed'])
        self.assertEqual(adapter.status()['status'],'supplemental_exhausted')

    def test_count_only_rejection_permanently_consumes(self):
        self.import_authorization(); adapter,fixture=self.adapter_and_fixture()
        with patch.object(adapter.opener,'open',side_effect=fixture.transport(count=20000)) as transport:
            self.code('AI_INPUT_TOO_LARGE',lambda:adapter.generate(fixture.case))
        self.assertEqual(transport.call_count,1)
        self.assert_consumed(); self.code('AI_USAGE_LIMIT',self.reserve)

    def test_uncertain_commit_never_calls_provider_and_remains_consumed(self):
        self.import_authorization(); adapter,fixture=self.adapter_and_fixture()
        original=PostgresConnection.commit
        def ambiguous_commit(db):
            original(db)
            raise RuntimeError('Simulated lost commit acknowledgement')
        with patch.object(PostgresConnection,'commit',ambiguous_commit),patch.object(adapter.opener,'open',side_effect=AssertionError('No network')) as transport:
            self.code('AI_BUDGET_UNAVAILABLE',lambda:adapter.generate(fixture.case))
        transport.assert_not_called(); self.assert_consumed()

    def test_write_failure_rolls_back_both_records_and_calls_no_provider(self):
        self.import_authorization(); adapter,fixture=self.adapter_and_fixture()
        original=PostgresConnection.execute
        def fail_marker(db,sql,parameters=()):
            if sql.startswith('UPDATE ai_cloud_supplemental_authorizations'):raise RuntimeError('fixture write failure')
            return original(db,sql,parameters)
        with patch.object(PostgresConnection,'execute',fail_marker),patch.object(adapter.opener,'open',side_effect=AssertionError('No network')) as transport:
            self.code('AI_BUDGET_UNAVAILABLE',lambda:adapter.generate(fixture.case))
        transport.assert_not_called()
        self.assertEqual(self.ledger.status()['supplemental_attempts_reserved'],0)
        # No provider request happened; atomic rollback did not consume a slot.

    def test_expiry_blocks_ledger_and_adapter_without_claim(self):
        self.import_authorization(); adapter,fixture=self.adapter_and_fixture()
        with patch('ai_budget.datetime',Expired),patch('backend.datetime',Expired):
            self.code('AI_CREDIT_EXPIRED',self.reserve)
            with patch.object(adapter.opener,'open',side_effect=AssertionError('No network')) as transport:
                self.code('AI_CREDIT_EXPIRED',lambda:adapter.generate(fixture.case))
            transport.assert_not_called()
            self.assertFalse(adapter.status()['request_allowed'])
            self.assertEqual(adapter.status()['status'],'credit_expired')
        self.assertEqual(self.ledger.status()['supplemental_attempts_reserved'],0)

    def test_full_cap_is_reserved_even_for_smaller_output(self):
        self.import_authorization()
        self.assertEqual(self.ledger.reserve(201280,3,1000000),210240)
        self.assert_consumed()

    def test_cap_and_small_config_budget_cannot_expand_or_spend(self):
        self.import_authorization()
        self.code('AI_CONFIGURATION_INVALID',lambda:self.ledger.reserve(210241,3,1000000))
        self.code('AI_USAGE_LIMIT',lambda:self.ledger.reserve(210240,3,200000))
        self.assertEqual(self.ledger.status()['supplemental_remaining_attempts'],1)

    def test_missing_receipt_never_repairs_or_reimports(self):
        self.import_authorization()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_supplemental_receipts')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_authorization)
        self.assertEqual(self.sql_import(),'blocked')
        self.ledger.initialize()
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)

    def test_missing_claim_after_use_fails_closed(self):
        self.import_authorization();self.reserve()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_supplemental_claims')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_authorization)
        self.assertEqual(self.sql_import(),'blocked')

    def test_missing_consumed_marker_after_use_fails_closed(self):
        self.import_authorization();self.reserve()
        with self.ledger.connect() as db:db.execute('UPDATE ai_cloud_supplemental_authorizations SET consumed_claim_id=NULL')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_authorization)
        self.assertEqual(self.sql_import(),'blocked')

    def test_corrupt_snapshot_fails_closed(self):
        self.import_authorization()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_supplemental_authorizations SET snapshot_json='{}'")
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.assertFalse(self.ledger.status()['ledger_readable'])
        self.assertEqual(self.sql_import(),'blocked')

    def test_corrupt_claim_fingerprint_fails_closed(self):
        self.import_authorization();self.reserve()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_supplemental_claims SET snapshot_sha256='corrupt'")
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.assertEqual(self.sql_import(),'blocked')

    def test_complete_history_then_supplemental_import_rejected(self):
        self.ledger.import_history(SNAPSHOT,True)
        self.code('AI_BUDGET_MODE_CONFLICT',self.import_authorization)
        self.assertEqual(self.sql_import(),'blocked')
        self.assertTrue(self.ledger.status()['history_reconciled'])

    def test_supplemental_then_complete_history_import_rejected(self):
        self.import_authorization()
        self.code('AI_BUDGET_MODE_CONFLICT',lambda:self.ledger.import_history(SNAPSHOT,True))
        self.assertFalse(self.ledger.status()['history_reconciled'])

    def test_mixed_raw_records_block_every_path(self):
        self.import_authorization()
        with self.ledger.connect() as db:
            db.execute('INSERT INTO ai_cloud_budget_history VALUES (?,?,?,?,?)',(POLICY,'a'*64,1,210240,'fixture'))
        self.code('AI_BUDGET_MODE_CONFLICT',self.reserve)
        self.code('AI_BUDGET_MODE_CONFLICT',self.import_authorization)
        self.code('AI_BUDGET_MODE_CONFLICT',lambda:self.ledger.import_history(SNAPSHOT,True))
        self.assertEqual(self.sql_import(),'blocked')

    def test_atomic_sql_import_is_idempotent_and_preserves_consumption(self):
        self.assertEqual(self.sql_import(),'imported')
        self.assertEqual(self.sql_import(),'already_imported')
        self.assertEqual(self.import_authorization(),'already_imported')
        self.reserve();self.assert_consumed()
        self.assertEqual(self.sql_import(),'already_imported')
        self.code('AI_USAGE_LIMIT',self.reserve)

    def test_sql_refuses_expired_pristine_authorization(self):
        sql=supplemental_import_sql(self.snapshot).replace('clock_timestamp()',"'2026-11-12T00:00:00+00:00'::timestamptz")
        self.assertEqual(self.sql_import(sql),'blocked')
        self.code('AI_BUDGET_HISTORY_REQUIRED',self.reserve)

    def test_sql_receipt_failure_rolls_back_authorization_atomically(self):
        import psycopg
        # Failure injection in a disposable schema only, never production DDL.
        with self.ledger.connect() as db:
            db.execute('ALTER TABLE ai_cloud_supplemental_receipts ADD CONSTRAINT test_reject CHECK (false)')
        with self.assertRaises(psycopg.errors.CheckViolation):self.sql_import()
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_cloud_supplemental_authorizations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_cloud_supplemental_receipts').fetchone()[0],0)

    def test_sql_conflicting_existing_authorization_does_not_overwrite(self):
        self.import_authorization()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_supplemental_receipts SET snapshot_sha256='conflict'")
        self.assertEqual(self.sql_import(),'blocked')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)

    def test_concurrent_sql_import_creates_exactly_one_allowance(self):
        import psycopg
        def import_once(_):
            try:return self.sql_import()
            except psycopg.errors.UniqueViolation:return 'concurrent_unique_blocked'
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(import_once,range(8)))
        self.assertEqual(results.count('imported'),1)
        self.assertTrue(all(result in {'imported','already_imported','concurrent_unique_blocked'} for result in results))
        self.assertEqual(self.ledger.status()['supplemental_remaining_attempts'],1)
        self.reserve();self.assert_consumed()
        self.code('AI_USAGE_LIMIT',self.reserve)

    def test_sql_and_runtime_reject_future_import_timestamps(self):
        self.import_authorization()
        with self.ledger.connect() as db:
            db.execute("UPDATE ai_cloud_supplemental_authorizations SET imported_at='2026-11-12T00:00:00+00:00'")
            db.execute("UPDATE ai_cloud_supplemental_receipts SET imported_at='2026-11-12T00:00:00+00:00'")
        self.assertEqual(self.sql_import(),'blocked')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_authorization)

    def test_authorization_single_record_deletion_is_rejected_by_foreign_key(self):
        import psycopg
        self.import_authorization()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.ledger.connect() as db:
                db.execute('DELETE FROM ai_cloud_supplemental_authorizations')
        self.assertEqual(self.ledger.status()['supplemental_remaining_attempts'],1)

    def test_status_outside_application_transaction_retains_supplemental(self):
        self.import_authorization()
        with patch.dict(os.environ,dict(ENABLED,DATABASE_URL=self.url,DIRECT_DATABASE_URL=self.url),clear=True):
            service=AuroraService(db_path=Path(self.temp.name)/'forbidden.sqlite3')
        detail=service.detail('CASE-001')
        self.assertEqual(detail['ai']['usage_limits']['supplemental_remaining_attempts'],1)
        self.assertTrue(detail['ai']['request_allowed'])
        with service.connect() as db:
            with patch.object(service.ai, 'status', side_effect=AssertionError('Nested live status forbidden')):
                snapshot=service._detail('CASE-001',db)
            self.assertNotIn('usage_limits',snapshot['ai'])
        self.assertFalse((Path(self.temp.name)/'forbidden.sqlite3').exists())

if __name__=='__main__':unittest.main()
