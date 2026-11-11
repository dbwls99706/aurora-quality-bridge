"""Fixed successor allowance fixtures. Never uses a live DB or provider API."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_supplemental_ai_budget as original_tests
from ai_budget import (CloudBudgetLedger,BudgetError,MODEL,POLICY,SUPPLEMENTAL_ID,
    FOLLOWUP_ID,PREDECESSOR_CLAIM_ID,PREDECESSOR_CLAIM_TIME,PREDECESSOR_FINGERPRINT,
    supplemental_snapshot,followup_snapshot,validate_followup,prepare_followup,
    followup_import_sql,supplemental_import_sql,canonical)
from backend import APIError
from persistence import PostgresConnection

class Imported(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,10,23,54,35,994273,tzinfo=timezone.utc)

class FirstClaim(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,10,23,59,40,318916,tzinfo=timezone.utc)


class FollowupOfflineTests(unittest.TestCase):
    def test_exact_fixed_successor_not_general_rollover(self):
        snapshot=followup_snapshot()
        self.assertEqual(snapshot['predecessor']['claim_id'],PREDECESSOR_CLAIM_ID)
        self.assertEqual(snapshot['predecessor']['snapshot_sha256'],PREDECESSOR_FINGERPRINT)
        self.assertEqual(snapshot['predecessor']['created_at'],PREDECESSOR_CLAIM_TIME)
        self.assertEqual(snapshot['maximum_new_attempts'],1)
        self.assertEqual(snapshot['maximum_new_reserved_microusd'],210240)
        self.assertEqual(snapshot['maximum_output_tokens'],2048)
        self.assertEqual(snapshot['cumulative_supplemental_maximum_attempts'],2)
        self.assertEqual(snapshot['cumulative_supplemental_maximum_reserved_microusd'],420480)
        self.assertFalse(snapshot['history_reconciled'])
        self.assertEqual(snapshot['observed_history'],supplemental_snapshot()['observed_history'])
        self.assertEqual(snapshot['uncertainty_reserve'],supplemental_snapshot()['uncertainty_reserve'])

    def test_changed_id_model_expiry_budget_predecessor_rejected(self):
        for key,value in [('authorization_id',FOLLOWUP_ID+'-reset'),('model','new'),
            ('expires_at','2026-12-12T00:00:00+00:00'),('maximum_new_attempts',2),
            ('maximum_new_reserved_microusd',210241),('maximum_output_tokens',2049),
            ('cumulative_supplemental_maximum_attempts',3),('history_reconciled',True),
            ('predecessor',{})]:
            with self.subTest(key=key),self.assertRaises(BudgetError):
                validate_followup(dict(followup_snapshot(),**{key:value}))

    def test_prepare_requires_explicit_reviewed_exact_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'wrong.json';path.write_text('{}')
            with self.assertRaises(BudgetError):prepare_followup(path)
            with self.assertRaises(BudgetError):prepare_followup(path,True)
        sql=followup_import_sql(followup_snapshot())
        for word in ('UPDATE ','DELETE ','ON CONFLICT','CREATE ','API_KEY','DATABASE_URL'):
            self.assertNotIn(word,sql)
        self.assertIn('pg_advisory_xact_lock(718401217)',sql)


@unittest.skipUnless(os.environ.get('AURORA_TEST_POSTGRES_URL'),'Needs disposable loopback PostgreSQL')
class FollowupPostgresTests(unittest.TestCase):
    setUp=original_tests.SupplementalPostgresTests.setUp
    code=original_tests.SupplementalPostgresTests.code
    adapter_and_fixture=original_tests.SupplementalPostgresTests.adapter_and_fixture
    reserve=original_tests.SupplementalPostgresTests.reserve

    def seed_predecessor(self,claim_id=PREDECESSOR_CLAIM_ID):
        with patch('ai_budget.datetime',Imported):
            self.ledger.import_supplemental(supplemental_snapshot(),True)
        with patch('ai_budget.datetime',FirstClaim),patch('ai_budget.uuid.uuid4',return_value=uuid.UUID(claim_id)):
            self.ledger.reserve(210240,3,1000000)
        return self.predecessor_rows()

    def predecessor_rows(self):
        with self.ledger.connect() as db:
            return {table:[dict(row) for row in db.execute('SELECT * FROM '+table).fetchall()]
                for table in ('ai_cloud_supplemental_authorizations','ai_cloud_supplemental_receipts','ai_cloud_supplemental_claims')}

    def import_followup(self):
        return self.ledger.import_followup(followup_snapshot(),True)

    def sql_import(self,sql=None):
        sql=(sql or followup_import_sql(followup_snapshot())).replace('clock_timestamp()',"'2026-11-11T00:00:00+00:00'::timestamptz")
        with self.ledger.connect() as db:return db.raw.execute(sql).fetchone()['followup_import_status']

    def assert_spent(self):
        status=self.ledger.status()
        self.assertEqual(status['supplemental_sequence'],2)
        self.assertEqual(status['supplemental_attempts_reserved'],1)
        self.assertEqual(status['predecessor_attempts_reserved'],1)
        self.assertEqual(status['predecessor_cost_reserved_usd'],.21024)
        self.assertEqual(status['cumulative_new_attempts_reserved'],2)
        self.assertEqual(status['cumulative_new_cost_reserved_usd'],.42048)
        self.assertEqual(status['cumulative_new_max_reserved_usd'],.42048)
        self.assertEqual(status['supplemental_remaining_attempts'],0)
        self.assertEqual(status['observed_prior_reserved_usd'],.21024)
        self.assertEqual(status['uncertainty_reserve_usd'],.42048)
        self.assertFalse(status['history_reconciled'])
        self.assertIsNone(status['total_cost_reserved_usd'])

    def test_empty_or_unspent_predecessor_cannot_authorize_followup(self):
        self.code('AI_BUDGET_UNAVAILABLE',self.import_followup)
        self.assertEqual(self.sql_import(),'blocked')
        self.ledger.import_supplemental(supplemental_snapshot(),True)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_followup)
        self.assertEqual(self.sql_import(),'blocked')
        self.assertEqual(self.ledger.status()['supplemental_remaining_attempts'],1)

    def test_different_valid_predecessor_claim_is_rejected(self):
        self.seed_predecessor(str(uuid.uuid4()))
        self.code('AI_BUDGET_UNAVAILABLE',self.import_followup)
        self.assertEqual(self.sql_import(),'blocked')

    def test_explicit_review_required(self):
        self.seed_predecessor()
        self.code('AI_SUPPLEMENTAL_REVIEW_REQUIRED',lambda:self.ledger.import_followup(followup_snapshot()))

    def test_exact_predecessor_preserved_through_import_reserve_restart(self):
        before=self.seed_predecessor()
        self.assertEqual(self.import_followup(),'imported')
        self.assertEqual(self.import_followup(),'already_imported')
        self.assertEqual(self.ledger.status()['cumulative_new_attempts_reserved'],1)
        self.assertEqual(self.reserve(),210240)
        self.assert_spent()
        self.ledger=CloudBudgetLedger(self.url);self.ledger.initialize()
        self.assertEqual(self.import_followup(),'already_imported')
        self.code('AI_USAGE_LIMIT',self.reserve)
        self.assertEqual(self.predecessor_rows(),before)

    def test_eight_processes_exactly_one_followup(self):
        before=self.seed_predecessor();self.import_followup()
        with ProcessPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(original_tests.process_claim,[self.url]*8))
        self.assertEqual(results.count('ok'),1)
        self.assertEqual(results.count('AI_USAGE_LIMIT'),7)
        self.assert_spent();self.assertEqual(self.predecessor_rows(),before)

    def test_new_claim_unique_key_independent_of_lock(self):
        import psycopg
        self.seed_predecessor();self.import_followup()
        fingerprint=hashlib.sha256(canonical(followup_snapshot()).encode()).hexdigest()
        def claim(_):
            try:
                with psycopg.connect(self.url) as db:
                    db.execute('INSERT INTO ai_cloud_followup_claims VALUES (%s,%s,%s,%s,%s,%s)',
                        (FOLLOWUP_ID,str(uuid.uuid4()),fingerprint,'2026-11-11T12:00:00+00:00',MODEL,210240))
                return 'ok'
            except psycopg.errors.UniqueViolation:return 'blocked'
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(claim,range(6)))
        self.assertEqual(results.count('ok'),1);self.assertEqual(results.count('blocked'),5)
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)

    def test_committed_before_both_provider_requests_and_no_second(self):
        before=self.seed_predecessor();self.import_followup()
        adapter,fixture=self.adapter_and_fixture()
        def transport(request,timeout):
            self.assert_spent()
            return fixture.transport()(request,timeout)
        with patch.object(adapter.opener,'open',side_effect=transport) as network:
            result=adapter.generate(fixture.case)
        self.assertEqual(network.call_count,2)
        self.assertEqual(result['approval_effect'],'none')
        self.assertFalse(adapter.status()['request_allowed'])
        self.assertEqual(adapter.status()['status'],'supplemental_exhausted')
        with patch.object(adapter.opener,'open',side_effect=AssertionError('No extra call')) as network:
            self.code('AI_USAGE_LIMIT',lambda:adapter.generate(fixture.case))
        network.assert_not_called();self.assertEqual(self.predecessor_rows(),before)

    def test_count_only_failure_consumes_permanently(self):
        self.seed_predecessor();self.import_followup();adapter,fixture=self.adapter_and_fixture()
        with patch.object(adapter.opener,'open',side_effect=fixture.transport(count=99999)) as network:
            self.code('AI_INPUT_TOO_LARGE',lambda:adapter.generate(fixture.case))
        self.assertEqual(network.call_count,1);self.assert_spent()
        self.code('AI_USAGE_LIMIT',self.reserve)

    def test_validation_failure_consumes_followup_without_facts_relaxation(self):
        self.seed_predecessor();self.import_followup();adapter,fixture=self.adapter_and_fixture()
        brief=fixture.brief();brief['facts']['defect_rate_pct']=11.87
        with patch.object(adapter.opener,'open',side_effect=fixture.transport(fixture.envelope(brief))) as network:
            self.code('AI_CALL_FAILED',lambda:adapter.generate(fixture.case))
        self.assertEqual(network.call_count,2);self.assert_spent()
        self.code('AI_USAGE_LIMIT',self.reserve)

    def test_commit_acknowledgement_failure_never_transmits(self):
        self.seed_predecessor();self.import_followup();adapter,fixture=self.adapter_and_fixture()
        original=PostgresConnection.commit
        def uncertain(db):original(db);raise RuntimeError('fixture lost acknowledgement')
        with patch.object(PostgresConnection,'commit',uncertain),patch.object(adapter.opener,'open',side_effect=AssertionError('No transport')) as network:
            self.code('AI_BUDGET_UNAVAILABLE',lambda:adapter.generate(fixture.case))
        network.assert_not_called();self.assert_spent()

    def test_marker_write_failure_rolls_back_and_never_transmits(self):
        before=self.seed_predecessor();self.import_followup();adapter,fixture=self.adapter_and_fixture()
        original=PostgresConnection.execute
        def fail(db,sql,parameters=()):
            if sql.startswith('UPDATE ai_cloud_followup_authorizations'):raise RuntimeError('fixture')
            return original(db,sql,parameters)
        with patch.object(PostgresConnection,'execute',fail),patch.object(adapter.opener,'open',side_effect=AssertionError('No transport')) as network:
            self.code('AI_BUDGET_UNAVAILABLE',lambda:adapter.generate(fixture.case))
        network.assert_not_called()
        self.assertEqual(self.ledger.status()['supplemental_attempts_reserved'],0)
        self.assertEqual(self.predecessor_rows(),before)

    def test_cumulative_cap_and_expiry_never_expand(self):
        self.seed_predecessor();self.import_followup()
        self.code('AI_USAGE_LIMIT',lambda:self.ledger.reserve(210240,3,420479))
        self.code('AI_CONFIGURATION_INVALID',lambda:self.ledger.reserve(210241,3,1000000))
        with patch('ai_budget.datetime',original_tests.Expired):
            self.code('AI_CREDIT_EXPIRED',self.reserve)
            self.assertEqual(self.ledger.status()['supplemental_remaining_attempts'],0)
        self.assertEqual(self.ledger.status()['supplemental_attempts_reserved'],0)

    def test_missing_followup_receipt_blocks_all_reimports(self):
        self.seed_predecessor();self.import_followup()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_followup_receipts')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_followup)
        self.code('AI_BUDGET_UNAVAILABLE',lambda:self.ledger.import_supplemental(supplemental_snapshot(),True))
        self.assertEqual(self.sql_import(),'blocked')

    def test_missing_followup_claim_after_consumption_blocks(self):
        self.seed_predecessor();self.import_followup();self.reserve()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_followup_claims')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.assertEqual(self.sql_import(),'blocked')

    def test_cleared_followup_marker_blocks(self):
        self.seed_predecessor();self.import_followup();self.reserve()
        with self.ledger.connect() as db:db.execute('UPDATE ai_cloud_followup_authorizations SET consumed_claim_id=NULL')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.assertEqual(self.sql_import(),'blocked')

    def test_corrupt_followup_snapshot_blocks(self):
        self.seed_predecessor();self.import_followup()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_followup_authorizations SET snapshot_json='{}'")
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.assertEqual(self.sql_import(),'blocked')

    def test_missing_predecessor_receipt_blocks_existing_followup(self):
        self.seed_predecessor();self.import_followup()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_supplemental_receipts')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_followup)
        self.assertEqual(self.sql_import(),'blocked')

    def test_predecessor_timestamp_corruption_blocks_existing_followup(self):
        self.seed_predecessor();self.import_followup()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_supplemental_claims SET created_at='2026-11-10T23:59:41+00:00'")
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.assertEqual(self.sql_import(),'blocked')

    def test_predecessor_claim_deletion_rejected_by_foreign_key(self):
        import psycopg
        self.seed_predecessor();self.import_followup()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_supplemental_claims')
        self.assertEqual(self.ledger.status()['supplemental_remaining_attempts'],1)

    def test_mixed_complete_history_blocks_all(self):
        self.seed_predecessor();self.import_followup()
        with self.ledger.connect() as db:db.execute('INSERT INTO ai_cloud_budget_history VALUES (?,?,?,?,?)',(POLICY,'a'*64,1,210240,'fixture'))
        self.code('AI_BUDGET_MODE_CONFLICT',self.reserve)
        self.code('AI_BUDGET_MODE_CONFLICT',self.import_followup)
        self.assertEqual(self.sql_import(),'blocked')

    def test_sql_import_atomic_idempotent_no_predecessor_changes(self):
        before=self.seed_predecessor()
        self.assertEqual(self.sql_import(),'imported')
        self.assertEqual(self.sql_import(),'already_imported')
        self.reserve();self.assert_spent()
        self.assertEqual(self.sql_import(),'already_imported')
        self.assertEqual(self.import_followup(),'already_imported')
        self.assertEqual(self.predecessor_rows(),before)
        with self.ledger.connect() as db:
            self.assertEqual(db.raw.execute(supplemental_import_sql(supplemental_snapshot())).fetchone()['supplemental_import_status'],'blocked')

    def test_concurrent_sql_import_exactly_one(self):
        import psycopg
        self.seed_predecessor()
        def run(_):
            try:return self.sql_import()
            except psycopg.errors.UniqueViolation:return 'unique_blocked'
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(run,range(8)))
        self.assertEqual(results.count('imported'),1)
        self.assertTrue(all(result in {'imported','already_imported','unique_blocked'} for result in results))
        self.reserve();self.code('AI_USAGE_LIMIT',self.reserve)

    def test_sql_failed_receipt_rolls_back_new_authorization(self):
        import psycopg
        before=self.seed_predecessor()
        with self.ledger.connect() as db:db.execute('ALTER TABLE ai_cloud_followup_receipts ADD CONSTRAINT test_reject CHECK (false)')
        with self.assertRaises(psycopg.errors.CheckViolation):self.sql_import()
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_cloud_followup_authorizations').fetchone()[0],0)
        self.assertEqual(self.predecessor_rows(),before)

    def test_sql_import_after_expiry_or_before_predecessor_blocked(self):
        self.seed_predecessor()
        for timestamp in ('2026-11-12T00:00:00+00:00','2026-11-10T23:59:39+00:00'):
            sql=followup_import_sql(followup_snapshot()).replace('clock_timestamp()',"'"+timestamp+"'::timestamptz")
            self.assertEqual(self.sql_import(sql),'blocked')

if __name__=='__main__':unittest.main()
