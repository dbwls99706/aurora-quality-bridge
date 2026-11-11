"""Offline mocked-DB tests only. No provider or actual PostgreSQL calls."""
import copy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
import threading
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace
from backend import APIError
from public_ai import PublicAILedger,PublicAIAdapter,BUDGET,RESERVATION,MAX_ATTEMPTS

class Result:
    def __init__(self,row=None):self.row=row
    def fetchone(self):return self.row
class DB:
    def __init__(self,expiry):
        self.policy=None;self.rows={};self.now=datetime.now(timezone.utc).timestamp();self.expiry=expiry
        self.lock=threading.Lock();self.commit_unknown=False;self.fail_execute=False;self.expired_workspace=False
    def execute(self,sql,p=()):
        if self.fail_execute:raise RuntimeError('fixture unavailable')
        if sql.startswith('CREATE TABLE'):return Result()
        if sql.startswith('INSERT INTO') and 'public_ai_policy' in sql:
            if self.policy is None:self.policy=p
            return Result()
        if sql.startswith('SELECT id,'):return Result(self.policy)
        if sql.startswith('SELECT app_id,'):return Result(self.policy[1:] if self.policy else None)
        if sql.startswith('SELECT extract'):return Result((self.now,))
        if sql.startswith('SELECT m.app_id'):return Result(('fixture-app',self.expiry,0 if self.expired_workspace else self.expiry))
        if sql.startswith('SELECT workspace,payload_hash'):
            r=self.rows.get(p[1]);return Result((r['workspace'],r['payload_hash']) if r else None)
        if sql.startswith('SELECT count(*)'):
            rs=list(self.rows.values());return Result((len(rs),sum(r['reserved'] for r in rs),sum(r['workspace']==p[0] for r in rs),sum(r['state'] in ('reserved','uncertain') for r in rs)))
        if sql.startswith('INSERT INTO') and 'public_ai_reservations' in sql:
            self.rows[p[1]]={'workspace':p[2],'payload_hash':p[3],'reserved':p[4],'state':'reserved'};return Result()
        if sql.startswith('UPDATE'):
            r=self.rows.get(p[2])
            if r and r['state']=='reserved':r['state']=p[0]
            return Result()
        raise AssertionError(sql)
    @contextmanager
    def transaction(self):
        with self.lock:
            before=copy.deepcopy((self.policy,self.rows))
            try:yield self
            except Exception:
                self.policy,self.rows=before;raise
            if self.commit_unknown:raise RuntimeError('fixture commit outcome unknown')

class Tests(unittest.TestCase):
    def setUp(self):
        self.expiry=datetime.now(timezone.utc)+timedelta(days=7)
        self.db=DB(self.expiry.timestamp());self.config=SimpleNamespace(app_id='fixture-app')
        self.ledger=PublicAILedger(self.config,expires_at=self.expiry,enabled=True,pricing_verified=True)
        self.ledger.connection=self.db.transaction;self.ledger.initialize()
        self.w='a'*32;self.h='b'*64
    def reserve(self,key='k',workspace=None,hash=None):return self.ledger.reserve(workspace or self.w,key,hash or self.h)
    def adapter(self):return PublicAIAdapter(self.ledger,self.w,'fixture-not-a-real-key',enabled=True)
    def denied(self,code,fn):
        with self.assertRaises(APIError) as e:fn()
        self.assertEqual(e.exception.code,code)
    def test_unapproved_never_initializes(self):
        l=PublicAILedger(self.config,expires_at=self.expiry);l.connection=Mock()
        self.denied('AI_PUBLIC_BUDGET_UNAVAILABLE',l.initialize);l.connection.assert_not_called()
    def test_pricing_must_be_verified(self):
        l=PublicAILedger(self.config,expires_at=self.expiry,enabled=True)
        self.denied('AI_PUBLIC_BUDGET_UNAVAILABLE',l.initialize)
    def test_policy_mismatch_does_not_reset_budget(self):
        self.reserve();old=copy.deepcopy(self.db.rows)
        self.db.policy=(*self.db.policy[:4],5000000,*self.db.policy[5:])
        self.denied('AI_PUBLIC_BUDGET_UNAVAILABLE',self.ledger.initialize);self.assertEqual(old,self.db.rows)
    def test_reservation_and_duplicate(self):
        self.reserve();self.denied('AI_ALREADY_RESERVED',self.reserve);self.assertEqual(len(self.db.rows),1)
    def test_key_payload_conflict(self):
        self.reserve();self.denied('IDEMPOTENCY_CONFLICT',lambda:self.reserve(hash='c'*64))
    def test_keys_scoped_per_visitor(self):
        self.reserve();self.reserve(workspace='c'*32);self.assertEqual(len(self.db.rows),2)
    def test_visitor_cap_and_no_refund(self):
        for k in ('a','b'):r=self.reserve(k);self.ledger.finish(r,'definite_failure')
        self.denied('AI_VISITOR_LIMIT',lambda:self.reserve('c'))
        self.assertEqual(sum(r['reserved'] for r in self.db.rows.values()),2*RESERVATION)
    def test_global_budget_floor19(self):
        for i in range(19):r=self.reserve(str(i),f'{i:032x}');self.ledger.finish(r,'succeeded')
        self.denied('AI_USAGE_LIMIT',lambda:self.reserve('next','f'*32))
        self.assertLessEqual(sum(r['reserved'] for r in self.db.rows.values()),BUDGET)
    def test_uncertain_keeps_slot(self):
        r=self.reserve();self.ledger.finish(r,'uncertain');self.reserve('b','c'*32)
        self.denied('AI_BUSY',lambda:self.reserve('c','d'*32))
    def test_parallel_independent_ledgers_share_cap(self):
        def attempt(i):
            l=PublicAILedger(self.config,expires_at=self.expiry,enabled=True,pricing_verified=True);l.connection=self.db.transaction
            try:l.reserve(f'{i:032x}',f'{i}',self.h);return True
            except APIError:return False
        with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(attempt,range(12)))
        self.assertEqual(sum(results),2)
    def test_expiry_db_clock(self):
        self.db.now=self.expiry.timestamp()-59
        self.denied('AI_PUBLIC_EXPIRED',self.reserve)
    def test_workspace_expiry(self):
        self.db.expired_workspace=True;self.denied('DEMO_EXPIRED',self.reserve)
    def test_db_failure_no_provider(self):
        a=self.adapter();a._generate=Mock();self.db.fail_execute=True
        self.denied('AI_PUBLIC_BUDGET_UNAVAILABLE',lambda:a.generate_for_request({'id':'fixture'},'k'));a._generate.assert_not_called()
    def test_commit_unknown_no_provider(self):
        a=self.adapter();a._generate=Mock();self.db.commit_unknown=True
        self.denied('AI_PUBLIC_BUDGET_UNAVAILABLE',lambda:a.generate_for_request({'id':'fixture'},'k'));a._generate.assert_not_called()
        self.assertEqual(len(self.db.rows),1)
    def test_no_legacy_generate(self):
        self.denied('IDEMPOTENCY_CONFLICT',lambda:self.adapter().generate({}))
    def test_adapter_success_reserves_before_generate(self):
        a=self.adapter()
        def output(case):
            self.assertEqual(a._reserve_anthropic_budget(),RESERVATION);self.assertEqual(len(self.db.rows),1)
            return {'kind':'fixture'}
        a._generate=output
        self.assertEqual(a.generate_for_request({'id':'fixture'},'k'),{'kind':'fixture'})
        self.assertEqual(next(iter(self.db.rows.values()))['state'],'succeeded')
    def test_adapter_timeout_uncertain(self):
        a=self.adapter();a._generate=Mock(side_effect=APIError('AI_NETWORK_TIMEOUT','fixture',502))
        self.denied('AI_NETWORK_TIMEOUT',lambda:a.generate_for_request({'id':'fixture'},'k'))
        self.assertEqual(next(iter(self.db.rows.values()))['state'],'uncertain')
    def test_adapter_input_rejection_definite(self):
        a=self.adapter();a._generate=Mock(side_effect=APIError('AI_INPUT_TOO_LARGE','fixture',413))
        self.denied('AI_INPUT_TOO_LARGE',lambda:a.generate_for_request({'id':'fixture'},'k'))
        self.assertEqual(next(iter(self.db.rows.values()))['state'],'definite_failure')
    def test_empty_key_not_configured(self):
        a=PublicAIAdapter(self.ledger,self.w,'',enabled=True);self.assertFalse(a.status()['request_allowed'])
    def test_status_no_secret(self):
        a=self.adapter();s=a.status();self.assertTrue(s['request_allowed']);self.assertNotIn(a.key,str(s))
        self.assertEqual(s['usage_limits']['remaining_global_attempts'],19)
    def test_status_db_failure_failclosed(self):
        self.db.fail_execute=True;self.assertFalse(self.adapter().status()['request_allowed'])
    def test_gate_disabled_no_reserve(self):
        a=PublicAIAdapter(self.ledger,self.w,'fixture',enabled=False)
        self.denied('AI_NOT_CONFIGURED',lambda:a.generate_for_request({},'k'));self.assertFalse(self.db.rows)
    def test_reservation_override_without_context_denied(self):
        self.denied('AI_PUBLIC_BUDGET_UNAVAILABLE',self.adapter()._reserve_anthropic_budget)
    def test_inherited_transport_and_validation_with_mock_provider(self):
        import tempfile,io,json,time
        from pathlib import Path
        from public_demo import DemoService
        from public_ai import MODEL,ENDPOINT
        with tempfile.TemporaryDirectory() as directory:
            service=DemoService(Path(directory)/'fixture.sqlite3',time.time()+120)
            case=service.detail('CASE-001');a=self.adapter();urls=[]
            brief={'summary':'가상 데이터 검토','risks':[],'alternatives':[],
                   'evidence_ids':[case['analysis']['evidence'][0]['record_id']],
                   'facts':{**{k:case['analysis'][k] for k in ('confirmed_qty','produced_qty','defect_rate_pct','recall_eligible')},'recall_threshold_pct':10}}
            envelope={'type':'message','role':'assistant','model':MODEL,'stop_reason':'tool_use',
                      'usage':{'input_tokens':1200,'output_tokens':100},
                      'content':[{'type':'tool_use','name':a.BRIEF_TOOL,'input':brief}]}
            def provider(request,timeout):
                urls.append(request.full_url)
                self.assertEqual(len(self.db.rows),1)
                self.assertIn(request.full_url,(ENDPOINT,ENDPOINT+'/count_tokens'))
                return io.BytesIO(json.dumps({'input_tokens':1200} if request.full_url.endswith('/count_tokens') else envelope).encode())
            with patch.object(a.opener,'open',side_effect=provider):
                result=a.generate_for_request(case,'fixture-request')
            self.assertEqual(urls,[ENDPOINT+'/count_tokens',ENDPOINT])
            self.assertEqual(result['kind'],'model_generated_advice')
            self.assertEqual(result['approval_effect'],'none')
            self.assertTrue(result['human_review_required']);self.assertNotIn(a.key,str(result))
            self.assertEqual(next(iter(self.db.rows.values()))['state'],'succeeded')
    def test_pg_decimal_aggregates_are_json_safe(self):
        import json
        from decimal import Decimal
        original=self.db.execute
        def execute(sql,p=()):
            if sql.startswith('SELECT count(*)'):
                return Result((19,Decimal(19*RESERVATION),2,0))
            return original(sql,p)
        self.db.execute=execute
        status=self.ledger.status(self.w)
        self.assertTrue(status['ledger_readable'])
        self.assertEqual(status['total_cost_reserved_usd'],19*RESERVATION/1_000_000)
        self.assertEqual(status['remaining_global_attempts'],0)
        self.assertIs(type(status['total_cost_reserved_usd']),float)
        for field in ('total_attempts_reserved','remaining_global_attempts','remaining_visitor_attempts','active_or_uncertain'):
            self.assertIs(type(status[field]),int)
        json.dumps(self.adapter().status(),allow_nan=False)

    def test_init_no_environment_or_legacy(self):
        with patch('backend.ModelAdapter.__init__',side_effect=AssertionError('legacy init forbidden')):
            a=self.adapter();self.assertIsNone(a.cloud_budget)

if __name__=='__main__':unittest.main()
