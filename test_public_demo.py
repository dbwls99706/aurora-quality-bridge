import io
import os
import sqlite3
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from backend import APIError
from public_demo import DemoRegistry,PublicDemoHandler,OfflineAI

class DemoTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{},clear=True);self.env.start()
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'state'
    def tearDown(self):self.tmp.cleanup();self.env.stop()
    def reg(self,**kw):return DemoRegistry(self.root,**kw)
    def denied(self,fn,code=None):
        with self.assertRaises(APIError) as got:fn()
        if code:self.assertEqual(got.exception.code,code)
    def test_no_model_adapter_constructed(self):
        with patch('backend.ModelAdapter.__init__',side_effect=AssertionError('provider constructed')):
            s,c=self.reg().resolve(None);self.assertFalse(s.state()['ai']['request_allowed'])
            self.denied(lambda:s.ai.generate({}),'DEMO_AI_DISABLED')
    def test_role_business_audit_and_idempotency_separation(self):
        r=self.reg();a,ca=r.resolve(None);b,cb=r.resolve(None)
        token=a.session('quality')['token'];self.denied(lambda:b.authenticate(token))
        a.mutate('/api/cases/CASE-001/submit',{'expected_version':1},a.authenticate(token),'same-test-key')
        self.assertEqual(b.detail('CASE-001')['version'],1)
        with a.connect() as db:ac=db.execute('SELECT count(*) FROM audit').fetchone()[0];ic=db.execute('SELECT count(*) FROM idempotency').fetchone()[0]
        with b.connect() as db:bc=db.execute('SELECT count(*) FROM audit').fetchone()[0];jc=db.execute('SELECT count(*) FROM idempotency').fetchone()[0]
        self.assertGreater(ac,bc);self.assertEqual((ic,jc),(1,0));self.assertTrue(a.verify_audit()['valid'])
    def test_cookie_hash_only_and_forgery(self):
        r=self.reg();_,c=r.resolve(None);self.assertEqual(len(c),43)
        with sqlite3.connect(r.db) as db:self.assertNotIn(c,str(db.execute('SELECT * FROM visitors').fetchall()))
        for bad in ['', 'a'*43,'../../owner']:self.denied(lambda:r.resolve(bad),'DEMO_SESSION_INVALID')
    def test_restart_preserves_limits_and_state(self):
        r=self.reg(max_workspaces=1,max_requests=2);s,c=r.resolve(None);s.session('quality');r.resolve(c)
        other=self.reg(max_workspaces=99,max_requests=99)
        self.denied(lambda:other.resolve(c),'DEMO_REQUEST_LIMIT');self.denied(lambda:other.resolve(None),'DEMO_CAPACITY')
    def test_expiry_and_no_automatic_deletion(self):
        r=self.reg();s,c=r.resolve(None)
        with sqlite3.connect(r.db) as db:db.execute('UPDATE visitors SET expires=0')
        self.denied(lambda:self.reg().resolve(c),'DEMO_EXPIRED')
        self.assertTrue(Path(s.db_path).exists())
    def test_cached_service_expiry(self):
        s,_=self.reg().resolve(None);s.expires_at=0;self.denied(s.state,'DEMO_EXPIRED')
    def test_concurrent_capacity_and_requests(self):
        a=self.reg(max_workspaces=1,max_requests=2);b=self.reg(max_workspaces=1,max_requests=2)
        def race(cookies):
            barrier=threading.Barrier(2);out=[]
            def work(reg,cookie):
                barrier.wait()
                try:_,c=reg.resolve(cookie);out.append(('ok',c))
                except APIError as e:out.append((e.code,None))
            threads=[threading.Thread(target=work,args=(r,c)) for r,c in zip((a,b),cookies)]
            for t in threads:t.start()
            for t in threads:t.join()
            return out
        first=race((None,None));self.assertEqual(sorted(x[0] for x in first),['DEMO_CAPACITY','ok'])
        cookie=next(x[1] for x in first if x[0]=='ok')
        second=race((cookie,cookie));self.assertEqual(sorted(x[0] for x in second),['DEMO_REQUEST_LIMIT','ok'])
    def test_older_registry_obeys_lowered_global_cap(self):
        old=self.reg();self.reg(max_workspaces=1);old.resolve(None)
        self.denied(lambda:old.resolve(None),'DEMO_CAPACITY')
    def test_cloud_and_database_config_fail_closed(self):
        for key,val in [('DATABASE_URL','postgresql://not-used/db'),('DIRECT_DATABASE_URL','postgresql://not-used/db'),('AURORA_DEPLOYMENT','axhub')]:
            with patch.dict(os.environ,{key:val}):
                with self.assertRaises(ValueError):self.reg()
    def test_unmarked_and_symlink_state_rejected(self):
        self.root.mkdir();(self.root/'existing').write_text('preserve')
        with self.assertRaises(ValueError):self.reg()
        link=Path(self.tmp.name)/'link';link.symlink_to(self.root)
        with self.assertRaises(ValueError):DemoRegistry(link)
    def handler(self,method='GET',path='/',cookie=None):
        h=PublicDemoHandler.__new__(PublicDemoHandler);h.command=method;h.path=path
        h.headers=Message();h.headers['Host']='127.0.0.1:8011'
        if cookie:h.headers['Cookie']=cookie
        h.server=SimpleNamespace(registry=self.reg(),allowed_authorities={'127.0.0.1:8011'},cloud_policy=SimpleNamespace(enabled=False))
        return h
    def test_headers_and_duplicate_cookie(self):
        for key,val in [('Host','evil.example'),('Origin','http://evil.example'),('Sec-Fetch-Site','cross-site')]:
            h=self.handler();h.headers[key]=val;self.denied(h.validate_authority)
        h=self.handler(cookie='aurora_demo='+'a'*43+'; aurora_demo = '+'b'*43);self.denied(h.validate_authority,'DEMO_SESSION_INVALID')
        h=self.handler();h.headers['Origin']='http://127.0.0.1:8011';h.headers['Origin']='http://127.0.0.1:8011';self.denied(h.validate_authority)
    def test_post_origin_and_bootstrap_required(self):
        h=self.handler('POST','/api/session');self.denied(h.validate_authority,'ORIGIN_REQUIRED')
        h.headers['Origin']='http://127.0.0.1:8011';self.denied(h.validate_authority,'DEMO_SESSION_REQUIRED')
    def test_nonentry_get_cannot_allocate(self):
        h=self.handler(path='/api/state');self.denied(h.validate_authority,'DEMO_SESSION_REQUIRED')
        with sqlite3.connect(h.server.registry.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM visitors').fetchone()[0],0)
    def test_secret_and_ai_routes_rejected_before_allocation(self):
        for path,method in [('/local-secret','GET'),('/api/local-secret','POST'),('/api/cases/CASE-001/ai-brief','POST')]:
            h=self.handler(method,path);h.headers['Origin']='http://127.0.0.1:8011';self.denied(h.validate_authority)
            with sqlite3.connect(h.server.registry.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM visitors').fetchone()[0],0)

class RateTests(unittest.TestCase):
    setUp=DemoTests.setUp
    tearDown=DemoTests.tearDown
    reg=DemoTests.reg
    denied=DemoTests.denied
    def test_global_rate_persistent_across_registries(self):
        a=self.reg(max_global_per_min=2);a.admit_connection()
        b=self.reg(max_global_per_min=99);b.admit_connection()
        self.denied(a.admit_connection,'DEMO_GLOBAL_RATE')
        self.denied(b.admit_connection,'DEMO_GLOBAL_RATE')
    def test_new_space_rate_persistent(self):
        a=self.reg(max_new_per_min=1);a.resolve(None)
        self.denied(lambda:self.reg(max_new_per_min=99).resolve(None),'DEMO_NEW_SPACE_RATE')

class AppExpiryTests(unittest.TestCase):
    setUp=DemoTests.setUp
    tearDown=DemoTests.tearDown
    reg=DemoTests.reg
    denied=DemoTests.denied
    def test_app_lifetime_cannot_extend_and_no_new_bucket(self):
        a=self.reg(app_ttl=10)
        with sqlite3.connect(a.db) as db:
            initial=db.execute('SELECT expires FROM app_lifetime').fetchone()[0]
        b=self.reg(app_ttl=999999)
        with sqlite3.connect(a.db) as db:
            self.assertEqual(db.execute('SELECT expires FROM app_lifetime').fetchone()[0],initial)
            db.execute('UPDATE app_lifetime SET expires=0')
        self.denied(a.admit_connection,'DEMO_APP_EXPIRED');self.denied(lambda:b.resolve(None),'DEMO_APP_EXPIRED')
        with sqlite3.connect(a.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM rate_buckets').fetchone()[0],0)
