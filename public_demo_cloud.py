"""Separate anonymous HTTPS entrypoint, DISABLED until verified profile+QA.
Only /healthz is available with placeholders. Requires an exclusive new AXHub
app and header-overwriting gateway. No production config, key or DB fallback.
"""
import json
import os
import re
import secrets
import threading
from datetime import datetime,timezone
from http.cookies import SimpleCookie,CookieError
from urllib.parse import urlsplit,unquote,parse_qs
from uuid import UUID
from backend import Handler,APIError,BASE
from public_demo import BoundedDemoServer
from public_demo_postgres import DedicatedDatabase,PostgresDemoRegistry,APPROVED_BINDINGS,transport_allowed
import public_demo_cloud_profile as profile

class Runtime:
    def __init__(self):
        self.lock=threading.Lock();self.registry=None;self.test_deadline=None;self.ai_ledger=None;self.ai_key=None
        self.result={'status':'not_configured','real_judge_login_tested':False}
    def initialize(self):
        # Owner-only diagnostics contain fixed phase labels and booleans, never
        # URL, host, user, key names/values, exception text or DB driver details.
        phase='profile'
        diagnostics={
            'profile_id_match':os.environ.get('AURORA_ISOLATED_APP_ID')==profile.APP_ID,
            'ai_off':os.environ.get('AURORA_AI_ALLOW_NETWORK')=='0',
            'db_present':'DATABASE_URL' in os.environ,
            'provider_env_present':any(k in os.environ for k in ('ANTHROPIC_API_KEY','OPENAI_API_KEY','AURORA_AI_API_KEY','LLM_API_KEY')),
        }
        # No secret value read before ALL nonsecret profile guards pass.
        try:
            parsed=urlsplit(profile.ORIGIN)
            expiry=datetime.fromisoformat(profile.EXPIRES_AT.replace('Z','+00:00'))
            if (str(UUID(profile.APP_ID))!=profile.APP_ID or str(UUID(profile.OWNER_UUID))!=profile.OWNER_UUID
                or parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password or not profile.TENANT
                or not profile.DATABASE_NAME or not profile.DATABASE_USER
                or expiry.tzinfo is None or expiry<=datetime.now(timezone.utc)
                or os.environ.get('AURORA_ISOLATED_APP_ID')!=profile.APP_ID
                or os.environ.get('AURORA_AI_ALLOW_NETWORK')!=('1' if getattr(profile,'PUBLIC_AI_ENABLED',False) else '0')):
                raise ValueError('Invalid new app profile')
            phase='provider_presence'
            if diagnostics['provider_env_present'] or (not getattr(profile,'PUBLIC_AI_ENABLED',False) and 'AURORA_PUBLIC_AI_KEY' in os.environ):
                raise ValueError('Unexpected secret or alternate DB present')
            # DIRECT_DATABASE_URL may be platform-injected: never read or use it.
            phase='db_config'
            url=os.environ.get('DATABASE_URL','')
            try:
                db_url=urlsplit(url);query=parse_qs(db_url.query,keep_blank_values=True)
                diagnostics.update({
                    'db_url_parseable':True,
                    'db_scheme_allowed':db_url.scheme in ('postgres','postgresql'),
                    'db_host_present':bool(db_url.hostname),
                    'db_single_host':bool(db_url.hostname) and ',' not in db_url.hostname,
                    'db_fragment_absent':not bool(db_url.fragment),
                    'db_name_matches':unquote(db_url.path.lstrip('/'))==profile.DATABASE_NAME,
                    'db_user_matches':unquote(db_url.username or '')==profile.DATABASE_USER,
                    'db_query_keys_allowed':not bool(set(query)-{'sslmode'}),
                    'db_query_unique':all(len(v)==1 for v in query.values()),
                    'db_sslmode_present':'sslmode' in query,
                    'db_sslmode_allowed':transport_allowed(profile.APP_ID,profile.DATABASE_NAME,profile.DATABASE_USER,profile.DATABASE_TRANSPORT,query.get('sslmode',['require'])[0]),
                    'db_transport_profile_axhub':profile.DATABASE_TRANSPORT=='axhub_runtime_standard',
                    'db_port_valid':db_url.port is None or 1<=db_url.port<=65535,
                    'db_binding_matches':(profile.APP_ID,profile.DATABASE_NAME,profile.DATABASE_USER) in APPROVED_BINDINGS,
                })
            except (ValueError,TypeError):diagnostics['db_url_parseable']=False
            config=DedicatedDatabase(profile.APP_ID,profile.DATABASE_NAME,profile.DATABASE_USER,url,transport_profile=profile.DATABASE_TRANSPORT)
            seconds=max(1,int((expiry-datetime.now(timezone.utc)).total_seconds()))
            phase='db_init'
            self.registry=PostgresDemoRegistry(config,max_workspaces=200,app_ttl=seconds)
            if getattr(profile,'PUBLIC_AI_ENABLED',False):
                phase='public_ai_config'
                from public_ai import PublicAILedger
                self.ai_key=os.environ.get('AURORA_PUBLIC_AI_KEY','')
                if not self.ai_key or not getattr(profile,'PUBLIC_AI_PRICING_VERIFIED',False):
                    raise ValueError('Dedicated public AI configuration required')
                self.ai_ledger=PublicAILedger(config,expires_at=profile.EXPIRES_AT,enabled=True,pricing_verified=True)
                self.ai_ledger.initialize()
            if profile.ANONYMOUS_TEST_ENABLED is True:
                deadline=datetime.fromisoformat(profile.ANONYMOUS_TEST_UNTIL.replace('Z','+00:00'))
                if deadline.tzinfo is None:raise ValueError('Timezone-aware test deadline required')
                self.registry.configure_test_window(deadline.timestamp(),profile.ANONYMOUS_TEST_ROUND)
                self.test_deadline=deadline.timestamp()
            from public_demo_cloud_selftest import load_result
            self.result=load_result(self.registry)
        except Exception as error:
            self.registry=None;self.test_deadline=None
            self.result={'status':'configuration_blocked','phase':phase,'diagnostics':diagnostics,'error_type':type(error).__name__,'real_judge_login_tested':False}
    def expired(self):
        try:return datetime.now(timezone.utc)>=datetime.fromisoformat(profile.EXPIRES_AT.replace('Z','+00:00'))
        except (ValueError,TypeError):return True
    def qa_ready(self):return self.registry is not None and not self.expired() and self.result.get('status')=='passed'
    def ready(self):return self.qa_ready() and profile.PUBLIC_RELEASE is True and profile.GATEWAY_VERIFIED is True and profile.HTTPS_BROWSER_VERIFIED is True
    def test_candidate(self):
        # Not authorization: the live DB deadline is mandatory in resolve.
        return (self.qa_ready() and profile.GATEWAY_VERIFIED is True
            and profile.ANONYMOUS_TEST_ENABLED is True and self.test_deadline is not None)
    def test_ready(self):
        return (self.test_candidate()
            and self.registry.test_window_open(self.test_deadline,profile.ANONYMOUS_TEST_ROUND))
    def admit_connection(self):
        if self.registry is not None:self.registry.admit_connection()
    def start_qa(self):
        with self.lock:
            if self.registry is None or self.expired():raise APIError('QA_UNAVAILABLE','독립 DB 설정을 먼저 확인해야 합니다.',503)
            if self.result.get('status') in ('running','passed','failed'):
                return self.result
            self.result={'status':'running','real_judge_login_tested':False}
            threading.Thread(target=self._run_qa,daemon=True).start()
            return self.result
    def _run_qa(self):
        # Fixed internal harness only; never use request input as test code or SQL.
        from public_demo_cloud_selftest import run_once
        try:result=run_once(self.registry)
        except Exception as error:result={'status':'failed','error_type':type(error).__name__,'real_judge_login_tested':False}
        with self.lock:self.result=result

STATIC_ASSETS=frozenset({'app.js','landing.js','landing.css','workspace.css','styles.css',
    'style.css','favicon.svg','common-nav.css','common-nav.js','supply-alternatives.js','supply-alternatives.css'})
def is_static_asset(path):
    # Exact non-HTML assets only. No API/source/unknown route can skip service.
    route=urlsplit(path).path
    return route in {'/'+name for name in STATIC_ASSETS} or route in {'/static/'+name for name in STATIC_ASSETS}

class CloudHandler(Handler):
    @property
    def service(self):return self.demo_service
    def handle_one_request(self):
        self.demo_service=None;self.new_cookie=None;self.clear_demo_cookie=False
        try:super().handle_one_request()
        except EgressStop as error:self._stop_egress(error.outcome)
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
    def _stop_egress(self,outcome):
        self._headers_buffer=[];self.close_connection=True
        if outcome=='small_denial':
            # Fixed small response, prepaid by a 256-byte ledger reservation.
            try:self.connection.sendall(b'HTTP/1.0 429 Limit\r\nContent-Length: 0\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n')
            except OSError:pass
    def send_header(self,name,value):
        if name.lower()=='content-length':
            size=int(value)
            if size>2097152:raise APIError('DEMO_RESPONSE_TOO_LARGE','한 번에 보낼 수 있는 자료 크기를 초과했습니다.',413)
            registry=self.server.runtime.registry
            if registry is not None:
                outcome=registry.reserve_response(size+2048)
                if outcome!='allowed':raise EgressStop(outcome)
        return super().send_header(name,value)
    def json_response(self,status,value):
        self._headers_buffer=[]
        try:return super().json_response(status,value)
        except EgressStop as error:self._stop_egress(error.outcome)
        except APIError:self._stop_egress('close')
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
    def end_headers(self):
        if getattr(self,'clear_demo_cookie',False):self.send_header('Set-Cookie','__Host-aurora_demo=; Max-Age=0; Secure; HttpOnly; SameSite=Strict; Path=/')
        if getattr(self,'new_cookie',None):self.send_header('Set-Cookie','__Host-aurora_demo='+self.new_cookie+'; Secure; HttpOnly; SameSite=Strict; Path=/')
        super().end_headers()
    def _authority(self,write=False):
        authority=urlsplit(profile.ORIGIN).netloc
        if not authority or self.headers.get_all('Host',[])!=[authority]:raise APIError('HOST_REJECTED','허용된 앱 주소가 아닙니다.',403)
        origins=self.headers.get_all('Origin',[])
        if len(origins)>1 or (origins and origins!=[profile.ORIGIN]) or (write and not origins):raise APIError('ORIGIN_REJECTED','동일 출처 요청만 허용합니다.',403)
        if self.server.runtime.expired():raise APIError('DEMO_APP_EXPIRED','시연 운영 시간이 끝났습니다.',403)
    def _owner(self,write=False):
        self._authority(write)
        if profile.GATEWAY_VERIFIED is not True:raise APIError('GATEWAY_NOT_VERIFIED','배포 경계를 확인 중입니다.',503)
        def one(name):
            values=self.headers.get_all(name,[]);return values[0] if len(values)==1 else None
        if not (one('X-AxHub-User-ID')==profile.OWNER_UUID and one('X-AxHub-App-Role')=='owner'
                and one('X-AxHub-Tenant-Slug')==profile.TENANT and one('X-AxHub-Surface') in ('tenant','public','admin')):
            raise APIError('OWNER_QA_ONLY','소유자 검증 화면입니다.',401)
    def validate_authority(self):
        self.new_cookie=None;self.demo_service=None
        self._authority(self.command!='GET')
        runtime=self.server.runtime
        required_window=None
        if not runtime.ready() and runtime.test_candidate():
            required_window=(runtime.test_deadline,profile.ANONYMOUS_TEST_ROUND)
        if not (runtime.ready() or required_window is not None):
            if not self.server.runtime.qa_ready():raise APIError('DEMO_NOT_RELEASED','공개 전 검증 중입니다.',503)
            # Owner-only preview permits real HTTPS/browser verification before release.
            self._owner(self.command!='GET')
        fetch=self.headers.get_all('Sec-Fetch-Site',[])
        top_entry=(self.command=='GET' and self.path in ('/','/workspace','/workspace/')
                   and self.headers.get_all('Sec-Fetch-Mode',[])==['navigate']
                   and self.headers.get_all('Sec-Fetch-Dest',[])==['document'])
        if len(fetch)>1 or (fetch and fetch[0] not in ('none','same-origin') and not (fetch[0] in ('same-site','cross-site') and top_entry)):
            raise APIError('ORIGIN_REJECTED','동일 출처 요청만 허용합니다.',403)
        if top_entry and fetch and fetch[0] in ('same-site','cross-site'):
            if required_window is not None and not runtime.test_ready():self._owner(False)
            raise NeutralNavigation()
        path=urlsplit(self.path).path
        if path.startswith(('/local-secret','/api/local-secret','/__qa')):raise APIError('NOT_FOUND','사용하지 않는 경로입니다.',404)
        if self.command!='GET' and ('ai-' in path or path.endswith('/ai')) and not getattr(profile,'PUBLIC_AI_ENABLED',False):raise APIError('DEMO_AI_DISABLED','실시간 AI 호출은 꺼져 있습니다.',403)
        raws=self.headers.get_all('Cookie',[])
        if len(raws)>1:raise APIError('DEMO_SESSION_INVALID','중복 쿠키입니다.',401)
        raw=raws[0] if raws else ''
        if len(raw)>4096 or len(re.findall(r'(?:^|;)\s*__Host-aurora_demo\s*=',raw))>1:raise APIError('DEMO_SESSION_INVALID','잘못된 쿠키입니다.',401)
        cookies=SimpleCookie()
        try:cookies.load(raw)
        except CookieError:raise APIError('DEMO_SESSION_INVALID','잘못된 쿠키입니다.',401) from None
        cookie=cookies['__Host-aurora_demo'].value if '__Host-aurora_demo' in cookies else None
        if '__Host-aurora_demo' in raw and cookie is None:raise APIError('DEMO_SESSION_INVALID','잘못된 쿠키입니다.',401)
        if cookie is None and (self.command!='GET' or self.path not in ('/','/workspace','/workspace/')):raise APIError('DEMO_SESSION_REQUIRED','먼저 연습 화면을 열어 주세요.',401)
        kwargs={'initialize_service':not (self.command=='GET' and is_static_asset(self.path))}
        if required_window is not None:kwargs['required_window']=required_window
        try:self.demo_service,self.new_cookie=runtime.registry.resolve(cookie,**kwargs)
        except APIError as error:
            if error.code!='DEMO_TEST_WINDOW_CLOSED':raise
            # Preserve exact-owner preview after a test round ends. The failed
            # transaction mutated nothing; anonymous visitors cannot retry here.
            self._owner(self.command!='GET')
            self.demo_service,self.new_cookie=runtime.registry.resolve(cookie,initialize_service=kwargs['initialize_service'])
        if self.demo_service is not None and getattr(runtime,'ai_ledger',None) is not None:
            from public_ai import PublicAIAdapter
            # Registry's isolated owner QA services remain offline until an actual
            # visitor request explicitly binds this dedicated budget adapter.
            with self.demo_service.lock:
                if not isinstance(self.demo_service.ai,PublicAIAdapter):
                    self.demo_service.ai=PublicAIAdapter(runtime.ai_ledger,self.demo_service.workspace,runtime.ai_key,enabled=True)
    def _html(self,text,nonce=None):
        data=text.encode();self.send_response(200)
        for key,value in [('Content-Type','text/html; charset=utf-8'),('Content-Length',str(len(data))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),('Referrer-Policy','same-origin')]:self.send_header(key,value)
        self.send_header('Content-Security-Policy',("default-src 'none'; script-src 'nonce-"+nonce+"'; " if nonce else '')+"frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        try:
            path=urlsplit(self.path).path
            if path=='/healthz':return self.json_response(200,{'ok':True})
            if path=='/__qa':
                self._owner()
                return self._html('<!doctype html><html lang="ko"><meta charset="utf-8"><title>소유자 격리 검증</title><h1>소유자 격리 검증</h1><p>새 앱의 가상 데이터만 검증합니다. 실시간 AI 호출과 외부 업무 실행은 없습니다.</p><form method="post" action="/__qa/run"><button type="submit">격리 검증 실행</button></form><p><a href="/__qa/report">검증 결과 확인</a></p><p><a href="/workspace">소유자 미리보기</a></p></html>')
            if path=='/__qa/report':
                self._owner();return self.json_response(200,self.server.runtime.result)
            entry=unquote(path).removeprefix('/static')
            if entry in ('/','/workspace','/workspace/','/index.html','/landing.html'):
                self.validate_authority()
                name='landing.html' if entry in ('/','/landing.html') else 'index.html'
                root=BASE/'static' if (BASE/'static'/'index.html').is_file() else BASE
                text=(root/name).read_text()
                banner=('방문자별 가상업무 연습 · 실제 외부 업무처리 없음 · AI는 합성 사건 자료를 Anthropic에 전송하며 전체 예산 한도 적용' if getattr(profile,'PUBLIC_AI_ENABLED',False) else '방문자별 가상업무 연습 · 실제 외부 업무처리와 새 AI 호출 없음')
                text=re.sub(r'(<body\b[^>]*>)',lambda m:m.group(0)+'<aside role="note" style="padding:12px;background:#fff2ce;color:#302414;text-align:center">'+banner+'</aside>',text,count=1,flags=re.I)
                return self._html(text)
            return super().do_GET()
        except NeutralNavigation:
            nonce=secrets.token_urlsafe(16);target='/workspace' if self.path.startswith('/workspace') else '/'
            return self._html('<!doctype html><html lang="ko"><meta charset="utf-8"><title>연습 공간 열기</title><p>기존 연습 공간을 확인하고 있습니다.</p><a href="'+target+'">계속하기</a><script nonce="'+nonce+'">location.replace("'+target+'");</script></html>',nonce)
        except EgressStop as error:return self._stop_egress(error.outcome)
        except APIError as e:
            if e.code=='DEMO_EXPIRED' and urlsplit(self.path).path in ('/','/workspace','/workspace/','/index.html','/landing.html'):
                # Only a known expired visitor reaches this branch. No automatic
                # renewal, data deletion, quota reset or workspace allocation.
                return self._html('<!doctype html><html lang="ko"><meta charset="utf-8"><title>연습 시간 만료</title><h1>연습 시간이 끝났습니다</h1><p>이전 연습 기록은 보존됩니다. 새 연습 공간은 원본 가상 자료로 시작하며 전체 이용 한도가 적용됩니다.</p><form method="post" action="/__demo/restart"><button type="submit">새 연습 시작</button></form>')
            return self.json_response(e.status,{'error':e.as_dict()})
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True;return
        except Exception:return self.json_response(503,{'error':{'code':'DEMO_UNAVAILABLE','message':'시연 공간을 준비하지 못했습니다. 잠시 후 다시 확인해 주세요.'}})
    def do_POST(self):
        if self.path=='/__demo/restart':
            try:
                # An empty native form, same-origin POST and a valid expired
                # cookie are mandatory. Keep ordinary release/TTL/quota guards.
                if self.headers.get_all('Content-Length',[]) not in ([],['0']) or self.headers.get_all('Transfer-Encoding',[]):
                    raise APIError('INVALID_REQUEST','빈 재시작 요청만 허용합니다.',400)
                try:self.validate_authority()
                except APIError as error:
                    if error.code!='DEMO_EXPIRED':raise
                else:raise APIError('DEMO_NOT_EXPIRED','현재 연습 공간을 계속 이용해 주세요.',409)
                self.new_cookie=None;self.clear_demo_cookie=True;self.close_connection=True
                self.send_response(303);self.send_header('Location','/workspace')
                self.send_header('Cache-Control','no-store');self.send_header('Content-Length','0');self.end_headers()
                return
            except EgressStop as error:return self._stop_egress(error.outcome)
            except APIError as error:return self.json_response(error.status,{'error':error.as_dict()})
            except (BrokenPipeError,ConnectionResetError):self.close_connection=True;return
        if urlsplit(self.path).path=='/__qa/run':
            try:
                self._owner(write=True)
                if self.headers.get_all('Sec-Fetch-Site',[]) not in ([],['same-origin']):raise APIError('ORIGIN_REJECTED','동일 출처 요청만 허용합니다.',403)
                return self.json_response(202,self.server.runtime.start_qa())
            except APIError as e:return self.json_response(e.status,{'error':e.as_dict()})
        return super().do_POST()

class NeutralNavigation(Exception):pass

class EgressStop(Exception):
    def __init__(self,outcome):self.outcome=outcome

class CloudServer(BoundedDemoServer):
    def reject_over_capacity(self,request):
        # Accept overflow must neither query DB nor transmit unreserved bytes.
        self.shutdown_request(request)
    def reject(self,request,status):
        registry=self.registry.registry
        if registry is not None:
            try:outcome=registry.reserve_response(256)
            except Exception:outcome='close'
            if outcome=='close':return self.shutdown_request(request)
            if outcome=='small_denial':status=429
        return super().reject(request,status)

def make_cloud_server(port=8000):
    runtime=Runtime()
    server=CloudServer(('0.0.0.0',port),CloudHandler,runtime,max_workers=8,read_timeout=5)
    server.runtime=runtime;server.daemon_threads=True
    # Readiness starts blocked; failed configuration leaves only liveness usable.
    threading.Thread(target=runtime.initialize,daemon=True).start()
    return server

if __name__=='__main__':make_cloud_server().serve_forever()
