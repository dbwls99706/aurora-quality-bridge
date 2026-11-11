"""Finite pool regressions. Disposable local PostgreSQL and fake transports only."""
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
import urllib.error
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_supplemental_ai_budget as original_tests
import test_followup_ai_budget as followup_tests
from ai_budget import (CloudBudgetLedger,BudgetError,POLICY,MODEL,REPAIR_ID,
    FOLLOWUP_CLAIM_ID,FOLLOWUP_CLAIM_TIME,repair_snapshot,validate_repair,prepare_repair,
    repair_import_sql,claim_digest,canonical,supplemental_snapshot,followup_snapshot,
    supplemental_import_sql,followup_import_sql)
from persistence import PostgresConnection

class SecondImported(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,11,0,33,23,1715,tzinfo=timezone.utc)
class SecondClaim(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,11,0,34,22,140277,tzinfo=timezone.utc)

class RepairOfflineTests(unittest.TestCase):
    def test_fixed_pool_caps_and_separate_observed_uncertainty(self):
        snap=repair_snapshot()
        self.assertEqual(snap['maximum_pool_attempts'],5)
        self.assertEqual(snap['maximum_pool_reserved_microusd'],1051200)
        self.assertEqual(snap['known_new_maximum_attempts'],7)
        self.assertEqual(snap['known_new_maximum_reserved_microusd'],1471680)
        self.assertEqual(snap['per_attempt_reserved_microusd'],210240)
        self.assertEqual(snap['maximum_output_tokens'],2048)
        self.assertEqual(snap['observed_history'],supplemental_snapshot()['observed_history'])
        self.assertEqual(snap['uncertainty_reserve'],supplemental_snapshot()['uncertainty_reserve'])
        self.assertFalse(snap['history_reconciled'])
        self.assertEqual(snap['automatic_retries'],0)
        self.assertEqual(snap['predecessors'][1]['claim_id'],FOLLOWUP_CLAIM_ID)

    def test_changed_pool_limits_model_expiry_or_predecessors_rejected(self):
        for key,val in [('pool_id',REPAIR_ID+'-new'),('maximum_pool_attempts',6),
          ('maximum_pool_reserved_microusd',1051201),('known_new_maximum_attempts',8),
          ('known_new_maximum_reserved_microusd',1471681),('automatic_retries',1),
          ('model','new'),('expires_at','2026-12-12T00:00:00+00:00'),('predecessors',[])]:
            with self.subTest(key=key),self.assertRaises(BudgetError):
                validate_repair(dict(repair_snapshot(),**{key:val}))

    def test_offline_prepare_no_unknown_readback_or_reset_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'incorrect.json';path.write_text('{}')
            with self.assertRaises(BudgetError):prepare_repair(path)
            with self.assertRaises(BudgetError):prepare_repair(path,True)
        sql=repair_import_sql(repair_snapshot())
        for text in ('UPDATE ','DELETE ','CREATE ','ON CONFLICT','API_KEY','DATABASE_URL','pgcrypto'):
            self.assertNotIn(text,sql)
        self.assertIn('sha256(',sql)
        self.assertIn('pg_advisory_xact_lock(718401217)',sql)

@unittest.skipUnless(os.environ.get('AURORA_TEST_POSTGRES_URL'),'Needs disposable loopback PostgreSQL')
class RepairPostgresTests(unittest.TestCase):
    setUp=original_tests.SupplementalPostgresTests.setUp
    code=original_tests.SupplementalPostgresTests.code
    reserve=original_tests.SupplementalPostgresTests.reserve
    adapter_and_fixture=original_tests.SupplementalPostgresTests.adapter_and_fixture
    predecessor_rows=followup_tests.FollowupPostgresTests.predecessor_rows

    def seed_both(self):
        followup_tests.FollowupPostgresTests.seed_predecessor(self)
        with patch('ai_budget.datetime',SecondImported):self.ledger.import_followup(followup_snapshot(),True)
        with patch('ai_budget.datetime',SecondClaim),patch('ai_budget.uuid.uuid4',return_value=uuid.UUID(FOLLOWUP_CLAIM_ID)):
            self.reserve()
        return self.all_predecessors()

    def all_predecessors(self):
        with self.ledger.connect() as db:
            return {table:[dict(row) for row in db.execute('SELECT * FROM '+table).fetchall()]
                for table in ('ai_cloud_supplemental_authorizations','ai_cloud_supplemental_receipts','ai_cloud_supplemental_claims',
                              'ai_cloud_followup_authorizations','ai_cloud_followup_receipts','ai_cloud_followup_claims')}

    def import_pool(self):return self.ledger.import_repair(repair_snapshot(),True)

    def sql_import(self,sql=None):
        sql=(sql or repair_import_sql(repair_snapshot())).replace('clock_timestamp()',"'2026-11-11T01:00:00+00:00'::timestamptz")
        with self.ledger.connect() as db:return db.raw.execute(sql).fetchone()['repair_import_status']

    def assert_usage(self,count):
        status=self.ledger.status()
        self.assertEqual(status['mode'],'repair_pool')
        self.assertEqual(status['repair_attempts_reserved'],count)
        self.assertEqual(status['repair_remaining_attempts'],5-count)
        self.assertEqual(status['repair_cost_reserved_usd'],count*210240/1000000)
        self.assertEqual(status['known_new_attempts_reserved'],2+count)
        self.assertEqual(status['known_new_reserved_usd'],(420480+count*210240)/1000000)
        self.assertEqual(status['known_new_max_reserved_usd'],1.47168)
        self.assertFalse(status['history_reconciled'])
        self.assertIsNone(status['total_cost_reserved_usd'])
        self.assertEqual(status['observed_prior_reserved_usd'],.21024)
        self.assertEqual(status['uncertainty_reserve_usd'],.42048)
        return status

    def test_empty_and_unspent_second_predecessor_block(self):
        self.code('AI_BUDGET_UNAVAILABLE',self.import_pool)
        self.assertEqual(self.sql_import(),'blocked')
        followup_tests.FollowupPostgresTests.seed_predecessor(self)
        self.ledger.import_followup(followup_snapshot(),True)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_pool)
        self.assertEqual(self.sql_import(),'blocked')

    def test_second_import_time_before_first_claim_blocks_sql_and_runtime(self):
        self.seed_both()
        with self.ledger.connect() as db:
            db.execute("UPDATE ai_cloud_followup_authorizations SET imported_at='2026-11-10T23:59:39+00:00'")
            db.execute("UPDATE ai_cloud_followup_receipts SET imported_at='2026-11-10T23:59:39+00:00'")
        self.code('AI_BUDGET_UNAVAILABLE',self.import_pool)
        self.assertEqual(self.sql_import(),'blocked')

    def test_exact_second_claim_required(self):
        self.seed_both()
        with self.ledger.connect() as db:
            db.execute("UPDATE ai_cloud_followup_claims SET created_at='2026-11-11T00:34:23+00:00'")
        self.code('AI_BUDGET_UNAVAILABLE',self.import_pool)
        self.assertEqual(self.sql_import(),'blocked')

    def test_five_claims_and_every_reimport_preserve_predecessors(self):
        before=self.seed_both()
        self.assertEqual(self.import_pool(),'imported')
        for count in range(6):
            self.assert_usage(count)
            self.assertEqual(self.import_pool(),'already_imported')
            self.assertEqual(self.sql_import(),'already_imported')
            self.assertEqual(self.ledger.import_supplemental(supplemental_snapshot(),True),'already_imported')
            self.assertEqual(self.ledger.import_followup(followup_snapshot(),True),'already_imported')
            if count<5:self.assertEqual(self.reserve(),210240)
        self.code('AI_USAGE_LIMIT',self.reserve)
        self.assertEqual(self.all_predecessors(),before)

    def test_twelve_processes_admit_exactly_five(self):
        before=self.seed_both();self.import_pool()
        with ProcessPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(original_tests.process_claim,[self.url]*12))
        self.assertEqual(results.count('ok'),5)
        self.assertEqual(results.count('AI_USAGE_LIMIT'),7)
        self.assert_usage(5);self.assertEqual(self.all_predecessors(),before)

    def test_one_adapter_can_make_five_manual_calls_despite_legacy_three(self):
        self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        self.assertEqual(adapter.max_requests,3)
        self.assertEqual(adapter.budget_microusd,1000000)
        for count in range(5):
            status=adapter.status();self.assertTrue(status['request_allowed'])
            self.assertEqual(status['usage_limits']['max_requests_per_process'],None)
            self.assertEqual(status['usage_limits']['budget_usd'],1.47168)
            with patch.object(adapter.opener,'open',side_effect=fixture.transport()) as calls:
                adapter.generate(fixture.case)
            self.assertEqual(calls.call_count,2)
            self.assert_usage(count+1)
        self.assertEqual(adapter.status()['status'],'repair_exhausted')
        self.assertFalse(adapter.status()['request_allowed'])
        with patch.object(adapter.opener,'open',side_effect=AssertionError('No sixth request')) as calls:
            self.code('AI_USAGE_LIMIT',lambda:adapter.generate(fixture.case))
        calls.assert_not_called()

    def test_committed_counter_and_chain_visible_before_transport(self):
        self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        def respond(request,timeout):
            self.assert_usage(1)
            with self.ledger.connect() as db:
                auth,claims=self.ledger.repair_state(db)
                self.assertEqual(auth['last_claim_id'],claims[0]['claim_id'])
                self.assertEqual(auth['last_claim_sha256'],claims[0]['claim_sha256'])
            return fixture.transport()(request,timeout)
        with patch.object(adapter.opener,'open',side_effect=respond) as calls:adapter.generate(fixture.case)
        self.assertEqual(calls.call_count,2)

    def test_count_failure_consumes_one_without_automatic_retry(self):
        self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        with patch.object(adapter.opener,'open',side_effect=urllib.error.URLError(TimeoutError('fixture'))) as calls:
            self.code('AI_NETWORK_TIMEOUT',lambda:adapter.generate(fixture.case))
        self.assertEqual(calls.call_count,1);self.assert_usage(1)
        self.assertTrue(adapter.status()['request_allowed'])
        self.assertEqual(adapter.status()['usage_limits']['automatic_retries'],0)

    def test_count_only_and_schema_failure_each_consumes_once(self):
        self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        with patch.object(adapter.opener,'open',side_effect=fixture.transport(count=99999)) as calls:
            self.code('AI_INPUT_TOO_LARGE',lambda:adapter.generate(fixture.case))
        self.assertEqual(calls.call_count,1);self.assert_usage(1)
        brief=fixture.brief();brief['risks']='not an array'
        with patch.object(adapter.opener,'open',side_effect=fixture.transport(fixture.envelope(brief))) as calls:
            self.code('AI_CALL_FAILED',lambda:adapter.generate(fixture.case))
        self.assertEqual(calls.call_count,2);self.assert_usage(2)

    def test_lost_commit_ack_consumes_no_provider_call(self):
        self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        original=PostgresConnection.commit
        def uncertain(db):original(db);raise RuntimeError('lost acknowledgement')
        # Only reservation connection is faulted; admission status remains readable.
        original_reserve=adapter.cloud_budget.reserve
        def reserve(*args):
            with patch.object(PostgresConnection,'commit',uncertain):return original_reserve(*args)
        with patch.object(adapter.cloud_budget,'reserve',side_effect=reserve),patch.object(adapter.opener,'open',side_effect=AssertionError('No provider')) as calls:
            self.code('AI_BUDGET_UNAVAILABLE',lambda:adapter.generate(fixture.case))
        calls.assert_not_called();self.assert_usage(1)

    def test_counter_write_failure_rolls_back_claim(self):
        before=self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        original=PostgresConnection.execute
        def fail(db,sql,parameters=()):
            if sql.startswith('UPDATE ai_cloud_repair_authorizations'):raise RuntimeError('fixture')
            return original(db,sql,parameters)
        with patch.object(PostgresConnection,'execute',fail),patch.object(adapter.opener,'open',side_effect=AssertionError('No provider')) as calls:
            self.code('AI_BUDGET_UNAVAILABLE',lambda:adapter.generate(fixture.case))
        calls.assert_not_called();self.assert_usage(0);self.assertEqual(self.all_predecessors(),before)

    def test_restart_and_initialize_never_refill(self):
        self.seed_both();self.import_pool();self.reserve();self.reserve()
        self.ledger=CloudBudgetLedger(self.url);self.ledger.initialize();self.assert_usage(2)
        self.assertEqual(self.import_pool(),'already_imported');self.assert_usage(2)
        for _ in range(3):self.reserve()
        self.code('AI_USAGE_LIMIT',self.reserve)

    def test_missing_receipt_blocks_original_and_new_imports(self):
        self.seed_both();self.import_pool()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_repair_receipts')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve)
        self.code('AI_BUDGET_UNAVAILABLE',self.import_pool)
        self.code('AI_BUDGET_UNAVAILABLE',lambda:self.ledger.import_followup(followup_snapshot(),True))
        self.assertEqual(self.sql_import(),'blocked')

    def test_middle_claim_deletion_blocks(self):
        self.seed_both();self.import_pool()
        for _ in range(3):self.reserve()
        with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_repair_claims WHERE ordinal=2')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_counter_change_blocks_without_refill(self):
        self.seed_both();self.import_pool();self.reserve();self.reserve()
        with self.ledger.connect() as db:
            db.execute('UPDATE ai_cloud_repair_authorizations SET consumed_count=1,consumed_microusd=210240')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_last_marker_change_blocks(self):
        self.seed_both();self.import_pool();self.reserve()
        with self.ledger.connect() as db:db.execute('UPDATE ai_cloud_repair_authorizations SET last_claim_id=NULL')
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_middle_claim_mutation_even_with_new_self_hash_blocks(self):
        self.seed_both();self.import_pool()
        for _ in range(3):self.reserve()
        with self.ledger.connect() as db:
            claim=dict(db.execute('SELECT * FROM ai_cloud_repair_claims WHERE ordinal=2').fetchone())
            claim['claim_id']=str(uuid.uuid4());sha=claim_digest(claim)
            db.execute('UPDATE ai_cloud_repair_claims SET claim_id=?,claim_sha256=? WHERE ordinal=2',(claim['claim_id'],sha))
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_last_claim_mutation_even_with_new_self_hash_blocks(self):
        self.seed_both();self.import_pool();self.reserve()
        with self.ledger.connect() as db:
            claim=dict(db.execute('SELECT * FROM ai_cloud_repair_claims').fetchone())
            claim['claim_id']=str(uuid.uuid4());sha=claim_digest(claim)
            db.execute('UPDATE ai_cloud_repair_claims SET claim_id=?,claim_sha256=?',(claim['claim_id'],sha))
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_snapshot_mutation_blocks(self):
        self.seed_both();self.import_pool()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_repair_authorizations SET snapshot_json='{}'")
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_old_receipt_corruption_blocks_entire_pool(self):
        self.seed_both();self.import_pool()
        with self.ledger.connect() as db:db.execute("UPDATE ai_cloud_supplemental_receipts SET snapshot_sha256='wrong'")
        self.code('AI_BUDGET_UNAVAILABLE',self.reserve);self.assertEqual(self.sql_import(),'blocked')

    def test_followup_claim_cannot_be_deleted_with_pool(self):
        import psycopg
        self.seed_both();self.import_pool()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.ledger.connect() as db:db.execute('DELETE FROM ai_cloud_followup_claims')
        self.assert_usage(0)

    def test_expiry_blocks_without_reserving(self):
        self.seed_both();self.import_pool();adapter,fixture=self.adapter_and_fixture()
        with patch('ai_budget.datetime',original_tests.Expired),patch('backend.datetime',original_tests.Expired):
            self.code('AI_CREDIT_EXPIRED',self.reserve)
            with patch.object(adapter.opener,'open',side_effect=AssertionError('No provider')) as calls:
                self.code('AI_CREDIT_EXPIRED',lambda:adapter.generate(fixture.case))
            calls.assert_not_called()
            self.assertFalse(adapter.status()['request_allowed'])
            self.assertEqual(adapter.status()['status'],'credit_expired')
            self.assertEqual(adapter.status()['usage_limits']['budget_usd'],1.47168)
        self.assert_usage(0)

    def test_mixed_complete_history_blocks(self):
        self.seed_both();self.import_pool()
        with self.ledger.connect() as db:db.execute('INSERT INTO ai_cloud_budget_history VALUES (?,?,?,?,?)',(POLICY,'a'*64,1,210240,'fixture'))
        self.code('AI_BUDGET_MODE_CONFLICT',self.reserve)
        self.code('AI_BUDGET_MODE_CONFLICT',self.import_pool)
        self.assertEqual(self.sql_import(),'blocked')

    def test_sql_import_atomic_idempotent_with_nonempty_hash_chain(self):
        before=self.seed_both();self.assertEqual(self.sql_import(),'imported')
        for count in range(6):
            self.assertEqual(self.sql_import(),'already_imported')
            self.assert_usage(count)
            if count<5:self.reserve()
        self.assertEqual(self.all_predecessors(),before)
        with self.ledger.connect() as db:
            for sql,key in ((supplemental_import_sql(supplemental_snapshot()),'supplemental_import_status'),
                            (followup_import_sql(followup_snapshot()),'followup_import_status')):
                self.assertEqual(db.raw.execute(sql).fetchone()[key],'blocked')

    def test_concurrent_sql_import_creates_only_one_five_slot_pool(self):
        import psycopg
        self.seed_both()
        def run(_):
            try:return self.sql_import()
            except psycopg.errors.UniqueViolation:return 'unique_blocked'
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(run,range(8)))
        self.assertEqual(results.count('imported'),1)
        self.assertTrue(all(r in {'imported','already_imported','unique_blocked'} for r in results))
        self.assert_usage(0)

    def test_sql_receipt_failure_rolls_back_authorization(self):
        import psycopg
        before=self.seed_both()
        with self.ledger.connect() as db:db.execute('ALTER TABLE ai_cloud_repair_receipts ADD CONSTRAINT test_reject CHECK (false)')
        with self.assertRaises(psycopg.errors.CheckViolation):self.sql_import()
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ai_cloud_repair_authorizations').fetchone()[0],0)
        self.assertEqual(self.all_predecessors(),before)

    def test_sql_expired_import_blocks(self):
        self.seed_both()
        sql=repair_import_sql(repair_snapshot()).replace('clock_timestamp()',"'2026-11-12T00:00:00+00:00'::timestamptz")
        self.assertEqual(self.sql_import(sql),'blocked')

    def test_db_ordinal_uniqueness_and_hard_maximum_independent_of_lock(self):
        import psycopg
        self.seed_both();self.import_pool();self.reserve()
        with self.ledger.connect() as db:claim=dict(db.execute('SELECT * FROM ai_cloud_repair_claims').fetchone())
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.ledger.connect() as db:
                db.execute('INSERT INTO ai_cloud_repair_claims SELECT pool_id,ordinal,?,snapshot_sha256,created_at,model,reserved_microusd,previous_claim_sha256,claim_sha256 FROM ai_cloud_repair_claims',(str(uuid.uuid4()),))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.ledger.connect() as db:
                db.execute('INSERT INTO ai_cloud_repair_claims SELECT pool_id,6,?,snapshot_sha256,created_at,model,reserved_microusd,previous_claim_sha256,claim_sha256 FROM ai_cloud_repair_claims',(str(uuid.uuid4()),))
        self.assert_usage(1)

if __name__=='__main__':unittest.main()
