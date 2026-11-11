"""LOCAL LOOPBACK PROOF ONLY. Not a public/cloud deployment entrypoint.
Anonymous visitors receive isolated synthetic SQLite workspaces. No real login,
external execution, provider calls or production DB/key access. Original files
remain unchanged. This is NOT the earlier PostgreSQL QA result.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit,unquote
from http.cookies import SimpleCookie, CookieError
from http.server import ThreadingHTTPServer
from backend import APIError,AuroraService,EvidenceStore,DEFAULT_DATA,Handler,BASE

MARKER={'purpose':'aurora-anonymous-loopback-proof','version':1}

class OfflineAI:
    cloud_budget=None
    key=''
    def status(self):
        return {'connected':False,'configured':False,'request_allowed':False,
                'status':'anonymous_offline','provider':None,'model':None,
                'message':'방문자별 가상 업무 연습 공간입니다. 외부 실행과 실시간 AI 호출은 꺼져 있습니다.',
                'verification_scope':'offline_rules_only','narrative_trust':'untrusted',
                'usage':{'requests_attempted':0,'max_requests_total':0}}
    def generate(self,*args,**kwargs):
        raise APIError('DEMO_AI_DISABLED','이 연습 공간에서는 실시간 AI를 호출하지 않습니다.',403)

class DemoService(AuroraService):
    def __init__(self, db_path, expires_at):
        # Do not call base __init__: it would instantiate ModelAdapter/read DB env.
        self.evidence=EvidenceStore(DEFAULT_DATA)
        self.db_path=str(db_path)
        self.database_url=self.migration_url=''
        self.expires_at=expires_at
        self.lock=threading.RLock()
        self._ai_inflight={}
        self._ai_condition=threading.Condition(self.lock)
        self.ai=OfflineAI()
        self._initialize()
    def connect(self,migration=False):
        if time.time()>=self.expires_at:
            raise APIError('DEMO_EXPIRED','연습 시간이 끝났습니다. 기록은 보존됩니다.',403)
        if Path(self.db_path).is_symlink():
            raise ValueError('Refusing symlink workspace DB')
        return super().connect(migration)

class DemoRegistry:
    def __init__(self, root, ttl=7200, max_workspaces=12, max_requests=2000,max_global_per_min=120,max_new_per_min=4,app_ttl=86400):
        if os.environ.get('DATABASE_URL') or os.environ.get('DIRECT_DATABASE_URL') or os.environ.get('AURORA_DEPLOYMENT','local')!='local':
            raise ValueError('Local demo refuses inherited deployment/database configuration')
        if any(type(v) is not int or v<=0 for v in (ttl,max_workspaces,max_requests,max_global_per_min,max_new_per_min,app_ttl)):
            raise ValueError('Positive integer limits required')
        raw=Path(root)
        if raw.is_symlink():raise ValueError('Refusing symlink root')
        self.root=raw.resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        marker=self.root/'public-demo.json'
        if marker.is_symlink():raise ValueError('Refusing symlink marker')
        if not marker.exists():
            if any(self.root.iterdir()):raise ValueError('Refusing nonempty unmarked directory')
            with marker.open('x') as f:json.dump(MARKER,f)
        if json.loads(marker.read_text())!=MARKER:raise ValueError('Invalid demo marker')
        self.db=self.root/'visitors.sqlite3'
        if self.db.is_symlink():raise ValueError('Refusing symlink registry')
        self.ttl,self.max_workspaces,self.max_requests=ttl,max_workspaces,max_requests
        self.lock=threading.RLock();self.services={}
        with sqlite3.connect(self.db,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY CHECK(id=1),spaces INTEGER NOT NULL,requests INTEGER NOT NULL,ttl INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO limits VALUES(1,?,?,?)',(max_workspaces,max_requests,ttl))
            db.execute('UPDATE limits SET spaces=min(spaces,?),requests=min(requests,?),ttl=min(ttl,?) WHERE id=1',(max_workspaces,max_requests,ttl))
            db.execute('CREATE TABLE IF NOT EXISTS app_lifetime(id INTEGER PRIMARY KEY CHECK(id=1),expires REAL NOT NULL)')
            db.execute('INSERT OR IGNORE INTO app_lifetime VALUES(1,?)',(time.time()+app_ttl,))
            db.execute('UPDATE app_lifetime SET expires=min(expires,?) WHERE id=1',(time.time()+app_ttl,))
            db.execute('CREATE TABLE IF NOT EXISTS rate_limits(id INTEGER PRIMARY KEY CHECK(id=1),global_cap INTEGER,new_cap INTEGER)')
            db.execute('INSERT OR IGNORE INTO rate_limits VALUES(1,?,?)',(max_global_per_min,max_new_per_min))
            db.execute('UPDATE rate_limits SET global_cap=min(global_cap,?),new_cap=min(new_cap,?) WHERE id=1',(max_global_per_min,max_new_per_min))
            db.execute('CREATE TABLE IF NOT EXISTS rate_buckets(minute INTEGER PRIMARY KEY,requests INTEGER NOT NULL,new_spaces INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS visitors(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,requests INTEGER NOT NULL,request_cap INTEGER NOT NULL)')
    def admit_connection(self):
        # Every accepted HTTP connection is charged before headers/body are read.
        with sqlite3.connect(self.db,timeout=2) as db:
            db.execute('BEGIN IMMEDIATE')
            if time.time()>=db.execute('SELECT expires FROM app_lifetime WHERE id=1').fetchone()[0]:
                raise APIError('DEMO_APP_EXPIRED','로컬 연습 서버 운영 시간이 끝났습니다.',403)
            minute=int(time.time()//60)
            cap=db.execute('SELECT global_cap FROM rate_limits WHERE id=1').fetchone()[0]
            db.execute('INSERT OR IGNORE INTO rate_buckets VALUES(?,0,0)',(minute,))
            used=db.execute('SELECT requests FROM rate_buckets WHERE minute=?',(minute,)).fetchone()[0]
            if used>=cap:raise APIError('DEMO_GLOBAL_RATE','전체 요청 속도 한도입니다.',429)
            db.execute('UPDATE rate_buckets SET requests=requests+1 WHERE minute=?',(minute,))

    def resolve(self, cookie):
        if self.db.is_symlink():raise ValueError('Refusing symlink registry')
        with self.lock:
            with sqlite3.connect(self.db,timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                app_expires=db.execute('SELECT expires FROM app_lifetime WHERE id=1').fetchone()[0]
                if time.time()>=app_expires:raise APIError('DEMO_APP_EXPIRED','로컬 연습 서버 운영 시간이 끝났습니다.',403)
                spaces,req_cap,ttl=db.execute('SELECT spaces,requests,ttl FROM limits WHERE id=1').fetchone()
                now=time.time();fresh=cookie is None
                if fresh:
                    minute=int(now//60)
                    new_cap=db.execute('SELECT new_cap FROM rate_limits WHERE id=1').fetchone()[0]
                    db.execute('INSERT OR IGNORE INTO rate_buckets VALUES(?,0,0)',(minute,))
                    used=db.execute('SELECT new_spaces FROM rate_buckets WHERE minute=?',(minute,)).fetchone()[0]
                    if used>=new_cap:raise APIError('DEMO_NEW_SPACE_RATE','새 연습 공간 생성 속도 한도입니다.',429)
                    if db.execute('SELECT count(*) FROM visitors').fetchone()[0]>=min(spaces,self.max_workspaces):
                        raise APIError('DEMO_CAPACITY','준비된 연습 공간이 모두 사용되었습니다.',429)
                    db.execute('UPDATE rate_buckets SET new_spaces=new_spaces+1 WHERE minute=?',(minute,))
                    cookie=secrets.token_urlsafe(32);wid=secrets.token_hex(16)
                    row=(wid,now,now+min(ttl,self.ttl),0,min(req_cap,self.max_requests))
                    db.execute('INSERT INTO visitors VALUES(?,?,?,?,0,?)',(wid,hashlib.sha256(cookie.encode()).hexdigest(),row[1],row[2],row[4]))
                else:
                    if not isinstance(cookie,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',cookie):
                        raise APIError('DEMO_SESSION_INVALID','잘못된 연습 공간 정보입니다.',401)
                    row=db.execute('SELECT id,created,expires,requests,request_cap FROM visitors WHERE token_hash=?',(hashlib.sha256(cookie.encode()).hexdigest(),)).fetchone()
                    if not row:raise APIError('DEMO_SESSION_INVALID','알 수 없는 연습 공간입니다.',401)
                wid,created,expires,requests,cap=row
                expires=min(expires,created+ttl,created+self.ttl,app_expires)
                if now>=expires:raise APIError('DEMO_EXPIRED','연습 시간이 끝났습니다.',403)
                if requests>=min(cap,req_cap,self.max_requests):raise APIError('DEMO_REQUEST_LIMIT','연습 요청 한도에 도달했습니다.',429)
                db.execute('UPDATE visitors SET requests=requests+1,expires=? WHERE id=?',(expires,wid))
            path=self.root/(wid+'.sqlite3')
            if wid not in self.services:self.services[wid]=DemoService(path,expires)
            self.services[wid].expires_at=expires
            return self.services[wid],cookie if fresh else None

class PublicDemoHandler(Handler):
    @property
    def service(self):return self.demo_service
    def handle_one_request(self):
        self.new_demo_cookie=None;self.demo_service=None
        super().handle_one_request()
    def end_headers(self):
        if getattr(self,'new_demo_cookie',None):
            # HTTP only because this proof binds exclusively to loopback, never public.
            self.send_header('Set-Cookie','aurora_demo='+self.new_demo_cookie+'; HttpOnly; SameSite=Strict; Path=/')
        super().end_headers()
    def do_GET(self):
        entry=unquote(urlsplit(self.path).path).removeprefix('/static')
        if entry in ('/','/workspace','/workspace/','/index.html','/landing.html'):
            try:
                self.validate_authority()
                filename='landing.html' if entry in ('/','/landing.html') else 'index.html'
                root=BASE/'static' if (BASE/'static'/'index.html').is_file() else BASE
                body=(root/filename).read_text()
                banner='<aside role="note" style="padding:12px;background:#fff2ce;color:#302414;text-align:center;position:relative;z-index:10000">방문자별 가상업무 연습 · 실제 업무처리/새 AI 호출 없음 · 로컬 검증용</aside>'
                body=re.sub(r'(<body\b[^>]*>)',lambda m:m.group(0)+banner,body,count=1,flags=re.I)
                data=body.encode()
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('X-Frame-Options','DENY')
                self.send_header('Content-Security-Policy',"frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
                self.end_headers();self.wfile.write(data)
            except APIError as error:self.json_response(error.status,{'error':error.as_dict()})
            return
        return super().do_GET()

    def validate_authority(self):
        self.new_demo_cookie=None;self.demo_service=None
        super().validate_authority()
        if self.command=='POST' and len(self.headers.get_all('Origin',[]))!=1:
            raise APIError('ORIGIN_REQUIRED','변경 요청에는 동일 출처 Origin이 필요합니다.',403)
        fetch=self.headers.get_all('Sec-Fetch-Site',[])
        if len(fetch)>1 or (fetch and fetch[0] not in ('none','same-origin')):
            raise APIError('ORIGIN_REJECTED','동일 출처에서 연습 화면을 열어 주세요.',403)
        # Reject sensitive/paid paths before touching or allocating visitor state.
        if self.path.startswith(('/api/local-secret','/local-secret')):
            raise APIError('NOT_FOUND','이 연습 화면에서는 키를 입력하지 않습니다.',404)
        if self.command!='GET' and ('ai-' in self.path or self.path.endswith('/ai')):
            raise APIError('DEMO_AI_DISABLED','실시간 AI 호출은 꺼져 있습니다.',403)
        raws=self.headers.get_all('Cookie',[])
        if len(raws)>1:raise APIError('DEMO_SESSION_INVALID','중복 쿠키입니다.',401)
        raw=raws[0] if raws else ''
        if len(raw)>4096 or len(re.findall(r'(?:^|;)\s*aurora_demo\s*=',raw))>1:
            raise APIError('DEMO_SESSION_INVALID','잘못된 쿠키입니다.',401)
        cookies=SimpleCookie()
        try:cookies.load(raw)
        except CookieError:raise APIError('DEMO_SESSION_INVALID','잘못된 쿠키입니다.',401) from None
        cookie=cookies['aurora_demo'].value if 'aurora_demo' in cookies else None
        if 'aurora_demo' in raw and cookie is None:raise APIError('DEMO_SESSION_INVALID','잘못된 쿠키입니다.',401)
        if cookie is None and (self.command!='GET' or self.path not in ('/','/workspace','/workspace/')):
            raise APIError('DEMO_SESSION_REQUIRED','먼저 연습 화면을 열어 주세요.',401)
        self.demo_service,self.new_demo_cookie=self.server.registry.resolve(cookie)

class BoundedDemoServer(ThreadingHTTPServer):
    def __init__(self,address,handler,registry,max_workers=8,read_timeout=5):
        self.registry=registry
        self.slots=threading.BoundedSemaphore(max_workers)
        self.read_timeout=read_timeout
        super().__init__(address,handler)
    def get_request(self):
        request,address=super().get_request()
        request.settimeout(self.read_timeout)
        return request,address
    def reject(self,request,status):
        try:request.sendall(('HTTP/1.0 '+str(status)+' Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n').encode())
        except OSError:pass
        self.shutdown_request(request)
    def reject_over_capacity(self,request):
        # Loopback proof has no billed egress; cloud overrides this to close only.
        self.reject(request,503)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):return self.reject_over_capacity(request)
        try:
            # Accept loop never waits for database admission.
            super().process_request(request,address)
        except Exception:
            self.slots.release();self.reject_over_capacity(request)
    def process_request_thread(self,request,address):
        try:
            try:self.registry.admit_connection()
            except APIError as error:return self.reject(request,error.status)
            except Exception:return self.reject(request,503)
            # No handler/body processing before successful admission.
            super().process_request_thread(request,address)
        finally:self.slots.release()

def make_demo_server(root,port=0,max_workers=8,read_timeout=5,**limits):
    registry=DemoRegistry(root,**limits)
    server=BoundedDemoServer(('127.0.0.1',port),PublicDemoHandler,registry,max_workers,read_timeout)
    server.registry=registry
    server.cloud_policy=type('LocalPolicy',(),{'enabled':False})()
    server.allowed_authorities={f'127.0.0.1:{server.server_port}',f'localhost:{server.server_port}'}
    server.daemon_threads=True
    return server

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--port',type=int,default=8011)
    args=parser.parse_args()
    server=make_demo_server(args.state_dir,args.port)
    print(f'LOCAL OFFLINE PROOF ONLY http://127.0.0.1:{server.server_port}/workspace',flush=True)
    server.serve_forever()
