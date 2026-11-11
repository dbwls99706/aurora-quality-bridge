"""PostgreSQL storage for isolated visitor workspaces.
Requires an explicit app, database and user binding. A shared database role is
not a hostile-SQL or RCE isolation boundary. Importing this module does not
connect to a database or create a provider adapter.
"""
import hashlib
import math
import os
import re
import secrets
import threading
import uuid
from contextlib import contextmanager
from collections import OrderedDict
from dataclasses import dataclass,field
from urllib.parse import urlsplit,unquote,parse_qs
from backend import APIError,AuroraService,EvidenceStore,DEFAULT_DATA
from persistence import PostgresConnection
from public_demo import OfflineAI

REG='aurora_public_demo_registry'
LOCK=718401229
PURPOSE='aurora-public-isolated-demo-v1'
APPROVED_TEST_ROUNDS=frozenset({('00000000-0000-4000-8000-000000000001','performance-v1'),('00000000-0000-4000-8000-000000000001','combined-v2')})
PRODUCTION_APP='00000000-0000-4000-8000-000000000003'
PRODUCTION_DATABASE='example_protected'
# Official new-app SQL verified DB name/user and denied CONNECT to production.
# Host/port are NOT independently attested: only the new app's runtime-injected
# URL may supply them. Live current_database/current_user is checked before writes.
# Tuple: (app UUID, database name, database user).
APPROVED_BINDINGS=frozenset({('00000000-0000-4000-8000-000000000001',
    'example_demo','example_demo')})

AXHUB_STANDARD_BINDINGS=frozenset({('00000000-0000-4000-8000-000000000001',
    'example_demo','example_demo')})
def transport_allowed(app_id,db_name,db_user,transport,mode):
    if transport not in ('tls_required','axhub_runtime_standard'):return False
    if mode in ('require','verify-ca','verify-full'):return True
    # Official AXHub docs/features/data says injected URLs explicitly use
    # sslmode=disable. Preserve that standard only for this verified NEW app.
    # No TLS claim for the internal DB hop; browser HTTPS is a different hop.
    return (mode=='disable' and transport=='axhub_runtime_standard'
        and (app_id,db_name,db_user) in AXHUB_STANDARD_BINDINGS)


@dataclass(frozen=True)
class DedicatedDatabase:
    app_id:str
    database_name:str
    database_user:str
    url:str=field(repr=False)
    transport_profile:str='tls_required'
    def __post_init__(self):
        if str(uuid.UUID(self.app_id))!=self.app_id or self.app_id==PRODUCTION_APP or self.database_name==PRODUCTION_DATABASE:
            raise ValueError('Explicit new app UUID required; production app prohibited')
        parsed=urlsplit(self.url)
        query=parse_qs(parsed.query,keep_blank_values=True)
        if (parsed.scheme not in ('postgres','postgresql') or not parsed.hostname
            or ',' in parsed.hostname or parsed.fragment or not parsed.username
            or unquote(parsed.path.lstrip('/'))!=self.database_name
            or unquote(parsed.username)!=self.database_user
            or not self.database_name or not self.database_user
            or set(query)-{'sslmode'} or any(len(v)!=1 for v in query.values())
            or not transport_allowed(self.app_id,self.database_name,self.database_user,self.transport_profile,query.get('sslmode',['require'])[0])):
            raise ValueError('Explicit dedicated DB identity/TLS configuration required')
        _=parsed.port
        binding=(self.app_id,self.database_name,self.database_user)
        if binding not in APPROVED_BINDINGS:
            raise ValueError('No verified newly approved app/database binding; connection disabled')
    def parameters(self):
        p=urlsplit(self.url);q=parse_qs(p.query)
        return dict(host=p.hostname,port=p.port or 5432,user=self.database_user,
                    password=unquote(p.password or ''),dbname=self.database_name,
                    sslmode=q.get('sslmode',['require'])[0],passfile='/dev/null',
                    connect_timeout=10,prepare_threshold=None)

def connect(config,*,row_factory=None,connector=None):
    # Refuse libpq environment overrides without reading their values.
    if any(key.startswith('PG') for key in os.environ):
        raise ValueError('Inherited libpq defaults forbidden')
    if connector is None:
        import psycopg
        connector=psycopg.connect
    params=config.parameters()
    if row_factory is not None:params['row_factory']=row_factory
    return connector(**params)

def timeouts(db):
    db.execute("SELECT set_config('lock_timeout','10000',true),set_config('statement_timeout','20000',true)")

@contextmanager
def registry_connection(config,connector=None):
    with connect(config,connector=connector) as db:
        timeouts(db)
        db.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK,))
        # Validate the live database identity before ANY mutation on every connection.
        if db.execute('SELECT current_database(),current_user').fetchone()!=(config.database_name,config.database_user):
            raise ValueError('Connected database identity mismatch')
        yield db

class ScopedConnection(PostgresConnection):
    def __init__(self,config,workspace,connector=None):
        if not re.fullmatch(r'[0-9a-f]{32}',workspace):raise ValueError('Invalid workspace')
        self.config=config;self.workspace=workspace;self.schema='apd_'+workspace
        key=int.from_bytes(hashlib.sha256(self.schema.encode()).digest()[:8],'big',signed=True)
        super().__init__('',connector=lambda _url,**kw:connect(config,row_factory=kw['row_factory'],connector=connector),lock_key=key)
    def execute(self,sql,parameters=()):
        if not self.started:
            timeouts(self.raw)
            self.raw.execute('SELECT pg_advisory_xact_lock(%s)',(self.lock_key,))
            identity=self.raw.execute('SELECT current_database(),current_user').fetchone()
            if tuple(identity.values())!=(self.config.database_name,self.config.database_user):
                raise ValueError('Connected database identity mismatch')
            row=self.raw.execute(f'SELECT m.app_id,m.expires AS app_expires,w.expires AS workspace_expires,to_regnamespace(%s) IS NOT NULL AS present,extract(epoch FROM clock_timestamp()) AS clock FROM {REG}.marker m JOIN {REG}.workspaces w ON w.id=%s WHERE m.id=1',(self.schema,self.workspace)).fetchone()
            if not row or row['app_id']!=self.config.app_id or not row['present'] or float(row['clock'])>=min(row['app_expires'],row['workspace_expires']):
                raise APIError('DEMO_EXPIRED','연습 공간 또는 앱 운영 시간이 끝났습니다.',403)
            self.raw.execute("SELECT set_config('search_path',%s,true)",(self.schema+',pg_catalog,pg_temp',))
            self.started=True
        return super().execute(sql,parameters)

class PGDemoService(AuroraService):
    def __init__(self,config,workspace,connector=None):
        self.config=config;self.workspace=workspace;self.connector=connector
        self.evidence=EvidenceStore(DEFAULT_DATA)
        self.database_url=self.migration_url='';self.db_path=None
        self.lock=threading.RLock();self._ai_inflight={};self._ai_condition=threading.Condition(self.lock)
        self.ai=OfflineAI();self._initialize()
    def connect(self,migration=False):return ScopedConnection(self.config,self.workspace,self.connector)

class PostgresDemoRegistry:
    def __init__(self,config,*,ttl=7200,max_workspaces=12,max_requests=2000,max_global_per_min=120,max_new_per_min=4,app_ttl=86400,connector=None):
        if not isinstance(config,DedicatedDatabase):raise ValueError('Explicit dedicated DB configuration required')
        if any(type(x) is not int or x<=0 for x in (ttl,max_workspaces,max_requests,max_global_per_min,max_new_per_min,app_ttl)):
            raise ValueError('Positive integer limits required')
        if max_workspaces>200:raise ValueError('Maximum 200 isolated workspaces per process')
        self.config=config;self.connector=connector
        self._services=OrderedDict();self._service_locks={};self._cache_lock=threading.Lock()
        with registry_connection(config,connector) as db:
            schemas={r[0] for r in db.execute("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'")}
            if any(s not in ('public',REG) and not re.fullmatch(r'apd_[0-9a-f]{32}',s) for s in schemas):raise ValueError('Unrelated database schemas')
            if db.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' LIMIT 1").fetchone():raise ValueError('Public schema is not empty')
            now=float(db.execute('SELECT extract(epoch FROM clock_timestamp())').fetchone()[0])
            if REG not in schemas:
                if schemas!={'public'}:raise ValueError('Unmarked nonempty database')
                db.execute(f'CREATE SCHEMA {REG}')
                db.execute(f'CREATE TABLE {REG}.marker(id INTEGER PRIMARY KEY CHECK(id=1),purpose TEXT NOT NULL,app_id TEXT NOT NULL,db_name TEXT NOT NULL,db_user TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL,spaces INTEGER NOT NULL,requests INTEGER NOT NULL,ttl INTEGER NOT NULL,global_cap INTEGER NOT NULL,new_cap INTEGER NOT NULL)')
                db.execute(f'INSERT INTO {REG}.marker VALUES(1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(PURPOSE,config.app_id,config.database_name,config.database_user,now+app_ttl,max_workspaces,max_requests,ttl,max_global_per_min,max_new_per_min))
                db.execute(f'CREATE TABLE {REG}.workspaces(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,created DOUBLE PRECISION NOT NULL,expires DOUBLE PRECISION NOT NULL,requests INTEGER NOT NULL,request_cap INTEGER NOT NULL)')
                db.execute(f'CREATE TABLE {REG}.rates(minute BIGINT PRIMARY KEY,requests INTEGER NOT NULL,new_spaces INTEGER NOT NULL)')
            else:
                row=db.execute(f'SELECT purpose,app_id,db_name,db_user FROM {REG}.marker WHERE id=1').fetchone()
                if row!=(PURPOSE,config.app_id,config.database_name,config.database_user):raise ValueError('Dedicated database marker mismatch')
                db.execute(f'UPDATE {REG}.marker SET expires=least(expires,%s),spaces=least(spaces,%s),requests=least(requests,%s),ttl=least(ttl,%s),global_cap=least(global_cap,%s),new_cap=least(new_cap,%s) WHERE id=1',(now+app_ttl,max_workspaces,max_requests,ttl,max_global_per_min,max_new_per_min))
            db.execute(f'CREATE TABLE IF NOT EXISTS {REG}.transfer(id INTEGER PRIMARY KEY CHECK(id=1),used BIGINT NOT NULL,cap BIGINT NOT NULL)')
            db.execute(f'INSERT INTO {REG}.transfer VALUES(1,0,524288000) ON CONFLICT DO NOTHING')

    def configure_test_window(self,deadline,round_id):
        # Only an explicit code-reviewed operator round, never an HTTP parameter.
        if (self.config.app_id,round_id) not in APPROVED_TEST_ROUNDS:
            raise ValueError('New verification round is not explicitly approved')
        if not isinstance(deadline,(int,float)) or not math.isfinite(deadline):raise ValueError('Finite absolute test deadline required')
        with registry_connection(self.config,self.connector) as db:
            limits,now=self._limits(db)
            if deadline<=now or deadline>now+900 or deadline>limits[0]:raise ValueError('Test window must end within 15 minutes and app lifetime')
            # Legacy test_window(id=1) and all earlier rounds stay untouched.
            db.execute(f'CREATE TABLE IF NOT EXISTS {REG}.test_rounds(round_id TEXT PRIMARY KEY,created DOUBLE PRECISION NOT NULL,expires DOUBLE PRECISION NOT NULL)')
            db.execute(f'INSERT INTO {REG}.test_rounds AS existing VALUES(%s,%s,%s) ON CONFLICT(round_id) DO UPDATE SET expires=least(existing.expires,EXCLUDED.expires)',(round_id,now,deadline))
    def test_window_open(self,configured_deadline,round_id):
        if (self.config.app_id,round_id) not in APPROVED_TEST_ROUNDS:return False
        with registry_connection(self.config,self.connector) as db:
            self._limits(db)
            row=db.execute(f'SELECT expires,extract(epoch FROM clock_timestamp()) FROM {REG}.test_rounds WHERE round_id=%s',(round_id,)).fetchone()
            return bool(row and float(row[1])<min(row[0],configured_deadline))

    def reserve_response(self,amount):
        if type(amount) is not int or amount<0:raise ValueError('Invalid response size')
        with registry_connection(self.config,self.connector) as db:
            self._limits(db)
            used,cap=db.execute(f'SELECT used,cap FROM {REG}.transfer WHERE id=1').fetchone()
            if used+amount<=cap:
                db.execute(f'UPDATE {REG}.transfer SET used=used+%s WHERE id=1',(amount,))
                return 'allowed'
            # Reserve a bounded final 429 if available; otherwise transmit nothing.
            if cap-used>=256:
                db.execute(f'UPDATE {REG}.transfer SET used=used+256 WHERE id=1')
                return 'small_denial'
            return 'close'

    def _limits(self,db):
        row=db.execute(f'SELECT expires,spaces,requests,ttl,global_cap,new_cap,app_id FROM {REG}.marker WHERE id=1').fetchone()
        now=float(db.execute('SELECT extract(epoch FROM clock_timestamp())').fetchone()[0])
        if not row or row[6]!=self.config.app_id:raise ValueError('Dedicated database marker mismatch')
        if now>=row[0]:raise APIError('DEMO_APP_EXPIRED','연습 서버 운영 시간이 끝났습니다.',403)
        return row,now
    def admit_connection(self):
        with registry_connection(self.config,self.connector) as db:
            limits,now=self._limits(db);minute=math.floor(now/60)
            db.execute(f'INSERT INTO {REG}.rates VALUES(%s,0,0) ON CONFLICT DO NOTHING',(minute,))
            if db.execute(f'SELECT requests FROM {REG}.rates WHERE minute=%s',(minute,)).fetchone()[0]>=limits[4]:raise APIError('DEMO_GLOBAL_RATE','전체 요청 속도 한도입니다.',429)
            db.execute(f'UPDATE {REG}.rates SET requests=requests+1 WHERE minute=%s',(minute,))
    def _get_service(self,workspace):
        # At most 200 admitted lock IDs, but only 8 resident service objects.
        # Eviction removes cache ownership only; in-flight references stay valid.
        with self._cache_lock:
            if workspace in self._services:
                self._services.move_to_end(workspace)
                return self._services[workspace]
            if workspace not in self._service_locks:
                if len(self._service_locks)>=200:raise APIError('DEMO_CAPACITY','연습 서비스 한도입니다.',429)
                self._service_locks[workspace]=threading.Lock()
            lock=self._service_locks[workspace]
        with lock:
            with self._cache_lock:
                existing=self._services.get(workspace)
                if existing is not None:self._services.move_to_end(workspace)
            if existing is not None:return existing
            service=PGDemoService(self.config,workspace,self.connector)
            # Failed initialization never enters cache; a later request may retry.
            with self._cache_lock:
                self._services[workspace]=service
                self._services.move_to_end(workspace)
                while len(self._services)>8:self._services.popitem(last=False)
            return service

    def resolve(self,cookie,*,initialize_service=True,required_window=None):
        with registry_connection(self.config,self.connector) as db:
            limits,now=self._limits(db);fresh=cookie is None
            # One registry transaction checks the live window BEFORE any space or
            # quota mutation. No deadline caching and no alternate client input.
            if required_window is not None:
                deadline,round_id=required_window
                if ((self.config.app_id,round_id) not in APPROVED_TEST_ROUNDS
                    or not isinstance(deadline,(int,float)) or not math.isfinite(deadline)):
                    raise APIError('DEMO_TEST_WINDOW_CLOSED','검증 시간이 끝났습니다.',503)
                window=db.execute(f'SELECT expires,extract(epoch FROM clock_timestamp()) FROM {REG}.test_rounds WHERE round_id=%s',(round_id,)).fetchone()
                if not window or float(window[1])>=min(window[0],deadline):
                    raise APIError('DEMO_TEST_WINDOW_CLOSED','검증 시간이 끝났습니다.',503)
            if fresh:
                minute=math.floor(now/60)
                db.execute(f'INSERT INTO {REG}.rates VALUES(%s,0,0) ON CONFLICT DO NOTHING',(minute,))
                if db.execute(f'SELECT new_spaces FROM {REG}.rates WHERE minute=%s',(minute,)).fetchone()[0]>=limits[5]:raise APIError('DEMO_NEW_SPACE_RATE','새 공간 생성 속도 한도입니다.',429)
                if db.execute(f'SELECT count(*) FROM {REG}.workspaces').fetchone()[0]>=limits[1]:raise APIError('DEMO_CAPACITY','연습 공간 한도입니다.',429)
                workspace=secrets.token_hex(16);cookie=secrets.token_urlsafe(32)
                row=(workspace,now,min(now+limits[3],limits[0]),0,limits[2])
                db.execute(f'INSERT INTO {REG}.workspaces VALUES(%s,%s,%s,%s,0,%s)',(workspace,hashlib.sha256(cookie.encode()).hexdigest(),now,row[2],row[4]))
                db.execute('CREATE SCHEMA apd_'+workspace)
                db.execute(f'UPDATE {REG}.rates SET new_spaces=new_spaces+1 WHERE minute=%s',(minute,))
            else:
                if not isinstance(cookie,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',cookie):raise APIError('DEMO_SESSION_INVALID','잘못된 연습 쿠키입니다.',401)
                row=db.execute(f'SELECT id,created,expires,requests,request_cap FROM {REG}.workspaces WHERE token_hash=%s',(hashlib.sha256(cookie.encode()).hexdigest(),)).fetchone()
                if not row:raise APIError('DEMO_SESSION_INVALID','알 수 없는 연습 쿠키입니다.',401)
            workspace,created,expires,requests,cap=row
            expires=min(expires,created+limits[3],limits[0])
            if now>=expires:raise APIError('DEMO_EXPIRED','연습 시간이 끝났습니다.',403)
            if requests>=min(cap,limits[2]):raise APIError('DEMO_REQUEST_LIMIT','연습 요청 한도입니다.',429)
            db.execute(f'UPDATE {REG}.workspaces SET expires=%s,requests=requests+1 WHERE id=%s',(expires,workspace))
        return self._get_service(workspace) if initialize_service else None,cookie if fresh else None
