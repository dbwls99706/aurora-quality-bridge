"""Dedicated public-demo AI budget. No inherited private ledger or environment keys.
Caller must provide the verified isolated DB, explicit server key and approved expiry.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import threading
import urllib.request
from backend import APIError, ModelAdapter, RejectModelRedirects

POLICY_ID='public-ai-20261111'
MODEL='claude-haiku-4-5-20251001'
ENDPOINT='https://api.anthropic.com/v1/messages'
BUDGET=4_000_000  # micro-USD; margin beneath separately approved USD5
RESERVATION=210_240  # entire200k context at1 + output2048 at5 microUSD/token
MAX_ATTEMPTS=20
MAX_VISITOR=2
MAX_ACTIVE=2

def fail(code='AI_PUBLIC_BUDGET_UNAVAILABLE',status=503):
    messages={'AI_USAGE_LIMIT':'공개 AI의 전체 예산 또는 요청 한도에 도달했습니다.',
              'AI_VISITOR_LIMIT':'이 연습 공간의 AI 요청2회를 모두 사용했습니다.',
              'AI_BUSY':'다른 AI 요청이 처리 중이거나 결과 확인을 기다리고 있습니다.',
              'AI_PUBLIC_EXPIRED':'승인된 AI 사용 기간이 끝났습니다.',
              'IDEMPOTENCY_CONFLICT':'같은 요청키를 다른 입력에 사용할 수 없습니다.',
              'AI_ALREADY_RESERVED':'이미 예약된 AI 요청입니다. 자동으로 다시 호출하지 않습니다.'}
    return APIError(code,messages.get(code,'AI 예산 상태를 안전하게 확인하지 못해 호출하지 않았습니다.'),status)

def expiry_value(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if not isinstance(d,datetime) or d.tzinfo is None:raise ValueError('Explicit timezone-aware expiry required')
    return d.astimezone(timezone.utc)

class PublicAILedger:
    def __init__(self,config,expiry=None,connector=None,*,expires_at=None,enabled=False,pricing_verified=False):
        self.config=config;self.expiry=expiry_value(expiry if expiry is not None else expires_at);self.connector=connector
        self.approved=enabled is True and pricing_verified is True
    @contextmanager
    def connection(self):
        # Lazy import prevents PG service -> adapter import cycles.
        from public_demo_postgres import registry_connection
        with registry_connection(self.config,self.connector) as db:yield db
    def initialize(self):
        if not self.approved:raise fail()
        from public_demo_postgres import REG
        try:
            with self.connection() as db:
                db.execute(f'CREATE TABLE IF NOT EXISTS {REG}.public_ai_policy(id TEXT PRIMARY KEY,app_id TEXT NOT NULL,model TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL,budget BIGINT NOT NULL,per_attempt BIGINT NOT NULL,max_attempts INTEGER NOT NULL,max_visitor INTEGER NOT NULL,max_active INTEGER NOT NULL)')
                values=(POLICY_ID,self.config.app_id,MODEL,self.expiry.timestamp(),BUDGET,RESERVATION,MAX_ATTEMPTS,MAX_VISITOR,MAX_ACTIVE)
                db.execute(f'INSERT INTO {REG}.public_ai_policy VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',values)
                if db.execute(f'SELECT id,app_id,model,expires,budget,per_attempt,max_attempts,max_visitor,max_active FROM {REG}.public_ai_policy WHERE id=%s',(POLICY_ID,)).fetchone()!=values:
                    raise fail()
                db.execute(f"CREATE TABLE IF NOT EXISTS {REG}.public_ai_reservations(policy_id TEXT NOT NULL REFERENCES {REG}.public_ai_policy(id),request_id TEXT NOT NULL,workspace TEXT NOT NULL,payload_hash TEXT NOT NULL,reserved BIGINT NOT NULL CHECK(reserved>0),state TEXT NOT NULL CHECK(state IN ('reserved','uncertain','succeeded','definite_failure')),created DOUBLE PRECISION NOT NULL,PRIMARY KEY(policy_id,request_id))")
        except APIError:raise
        except Exception:raise fail() from None
    def _policy(self,db,workspace):
        if not self.approved:raise fail()
        from public_demo_postgres import REG
        p=db.execute(f'SELECT app_id,model,expires,budget,per_attempt,max_attempts,max_visitor,max_active FROM {REG}.public_ai_policy WHERE id=%s FOR UPDATE',(POLICY_ID,)).fetchone()
        expected=(self.config.app_id,MODEL,self.expiry.timestamp(),BUDGET,RESERVATION,MAX_ATTEMPTS,MAX_VISITOR,MAX_ACTIVE)
        if p!=expected:raise fail()
        now=float(db.execute('SELECT extract(epoch FROM clock_timestamp())').fetchone()[0])
        limits=db.execute(f'SELECT m.app_id,m.expires,w.expires FROM {REG}.marker m JOIN {REG}.workspaces w ON w.id=%s WHERE m.id=1',(workspace,)).fetchone()
        if not limits or limits[0]!=self.config.app_id or now>=min(limits[1],limits[2]):raise fail('DEMO_EXPIRED',403)
        return now
    def reserve(self,workspace,key,payload_hash):
        from public_demo_postgres import REG
        if not re.fullmatch('[0-9a-f]{32}',workspace) or not isinstance(key,str) or not 1<=len(key)<=200 or not re.fullmatch('[0-9a-f]{64}',payload_hash):raise fail('IDEMPOTENCY_CONFLICT',409)
        request_id=hashlib.sha256((workspace+'\0'+key).encode()).hexdigest()
        try:
            with self.connection() as db:
                now=self._policy(db,workspace)
                if now>=self.expiry.timestamp()-60:raise fail('AI_PUBLIC_EXPIRED',403)
                old=db.execute(f'SELECT workspace,payload_hash FROM {REG}.public_ai_reservations WHERE policy_id=%s AND request_id=%s',(POLICY_ID,request_id)).fetchone()
                if old:
                    if old!=(workspace,payload_hash):raise fail('IDEMPOTENCY_CONFLICT',409)
                    raise fail('AI_ALREADY_RESERVED',409)
                count,used,visitor,active=db.execute(f"SELECT count(*),COALESCE(sum(reserved),0),count(*) FILTER(WHERE workspace=%s),count(*) FILTER(WHERE state IN ('reserved','uncertain')) FROM {REG}.public_ai_reservations WHERE policy_id=%s",(workspace,POLICY_ID)).fetchone()
                if count>=MAX_ATTEMPTS or used+RESERVATION>BUDGET:raise fail('AI_USAGE_LIMIT',429)
                if visitor>=MAX_VISITOR:raise fail('AI_VISITOR_LIMIT',429)
                if active>=MAX_ACTIVE:raise fail('AI_BUSY',429)
                db.execute(f"INSERT INTO {REG}.public_ai_reservations VALUES(%s,%s,%s,%s,%s,'reserved',%s)",(POLICY_ID,request_id,workspace,payload_hash,RESERVATION,now))
            return request_id  # only after context manager confirms COMMIT
        except APIError:raise
        except Exception:raise fail() from None
    def finish(self,request_id,state):
        from public_demo_postgres import REG
        if state not in ('succeeded','definite_failure','uncertain'):raise ValueError('Invalid outcome')
        try:
            with self.connection() as db:
                db.execute(f"UPDATE {REG}.public_ai_reservations SET state=%s WHERE policy_id=%s AND request_id=%s AND state='reserved'",(state,POLICY_ID,request_id))
        except Exception:raise fail() from None
    def status(self,workspace):
        from public_demo_postgres import REG
        try:
            with self.connection() as db:
                now=self._policy(db,workspace)
                count,used,visitor,active=db.execute(f"SELECT count(*),COALESCE(sum(reserved),0),count(*) FILTER(WHERE workspace=%s),count(*) FILTER(WHERE state IN ('reserved','uncertain')) FROM {REG}.public_ai_reservations WHERE policy_id=%s",(workspace,POLICY_ID)).fetchone()
                # PostgreSQL sum(bigint) is Decimal; normalize for JSON and exact
                # integer reservation arithmetic before exposing a float USD value.
                count,used,visitor,active=map(int,(count,used,visitor,active))
                return {'ledger_readable':True,'total_attempts_reserved':count,'total_cost_reserved_usd':used/1_000_000,
                        'remaining_global_attempts':max(0,min(MAX_ATTEMPTS-count,(BUDGET-used)//RESERVATION)),
                        'remaining_visitor_attempts':max(0,MAX_VISITOR-visitor),'active_or_uncertain':active,
                        'expired':now>=self.expiry.timestamp()-60}
        except Exception:return {'ledger_readable':False,'expired':True,'remaining_global_attempts':0,'remaining_visitor_attempts':0,'active_or_uncertain':MAX_ACTIVE}

class PublicAIAdapter(ModelAdapter):
    """Reuses fixed payload/response validators, never ModelAdapter's legacy init."""
    def __init__(self,ledger,workspace,key,*,enabled=False):
        self.ledger=ledger
        self.workspace=workspace;self.expiry=ledger.expiry
        if not re.fullmatch('[0-9a-f]{32}',workspace):raise ValueError('Invalid workspace')
        self.key=key if isinstance(key,str) else '';self.enabled=enabled is True
        self._configuration_error=not ledger.approved or any(ord(c)<=32 for c in self.key)
        self.provider='anthropic';self.model=MODEL;self.url=ENDPOINT;self.cloud_budget=None
        self.max_tokens=2048;self.max_input_tokens=8192;self.max_requests=MAX_ATTEMPTS;self.max_concurrent=MAX_ACTIVE
        self.budget_microusd=BUDGET;self.verified=False;self._usage_lock=threading.Lock();self._attempts=self._active=0
        self._reservation_local=threading.local();self.opener=urllib.request.build_opener(RejectModelRedirects())
        self.ANTHROPIC_EXPIRES_AT=self.expiry
    def _check_anthropic_window(self,seconds=60):
        if datetime.now(timezone.utc)>=self.expiry-timedelta(seconds=seconds):raise fail('AI_PUBLIC_EXPIRED',403)
    def _reserve_anthropic_budget(self):
        if not getattr(self._reservation_local,'request_id',None):raise fail()
        return RESERVATION
    def generate(self,case):raise fail('IDEMPOTENCY_CONFLICT',409)
    def generate_for_request(self,case,key):
        if not(self.enabled and self.key and not self._configuration_error):raise APIError('AI_NOT_CONFIGURED','승인된 공개 AI 설정을 확인해 주세요.',503)
        self._check_anthropic_window()
        payload_hash=hashlib.sha256(json.dumps(case,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        request_id=self.ledger.reserve(self.workspace,key,payload_hash)
        self._reservation_local.request_id=request_id
        try:
            result=self._generate(case)
        except APIError as error:
            # Conservative: any transport/HTTP/parsing failure retains the global
            # active slot because server-side processing completion may be unknown.
            definite=error.code in ('AI_INPUT_TOO_LARGE','AI_PUBLIC_EXPIRED','AI_AUTH_FAILED','AI_RATE_LIMITED','AI_QUOTA_EXCEEDED')
            self.ledger.finish(request_id,'definite_failure' if definite else 'uncertain')
            raise
        except Exception:
            self.ledger.finish(request_id,'uncertain');raise fail() from None
        else:
            self.ledger.finish(request_id,'succeeded');return result
        finally:self._reservation_local.request_id=None
    def status(self):
        usage=self.ledger.status(self.workspace)
        configured=bool(self.enabled and self.key and not self._configuration_error)
        allowed=bool(configured and usage.get('ledger_readable') and not usage.get('expired') and usage.get('remaining_global_attempts',0)>0 and usage.get('remaining_visitor_attempts',0)>0 and usage.get('active_or_uncertain',2)<2)
        usage.update(budget_usd=BUDGET/1_000_000,max_requests_total=MAX_ATTEMPTS,max_requests_per_visitor=MAX_VISITOR,max_concurrent_requests=MAX_ACTIVE,budget_scope='public_app_postgres_global_reservations',automatic_retries=0,expires_at=self.expiry.isoformat(),max_output_tokens=self.max_tokens)
        return {'connected':bool(configured and self.verified),'configured':configured,'request_allowed':allowed,
                'status':'public_ai_ready' if allowed else 'public_ai_blocked','provider':'Anthropic Messages API','model':MODEL if configured else None,
                'message':'공개 AI · 전체 예산/방문자별 횟수 제한 적용' if allowed else '공개 AI 설정·기간·남은 한도를 확인해 주세요.',
                'verification_scope':'transport_and_structured_fields_only','narrative_trust':'untrusted','can_approve':False,'usage_limits':usage}
