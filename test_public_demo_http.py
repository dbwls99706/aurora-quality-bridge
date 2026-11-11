"""Actual loopback HTTP tests. These are not public gateway/PG tests."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from public_demo import make_demo_server

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{},clear=True);self.env.start()
        self.tmp=tempfile.TemporaryDirectory()
        self.server=make_demo_server(self.tmp.name+'/state')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup();self.env.stop()
    def request(self,path='/',method='GET',cookie=None,token=None,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        h={'Content-Type':'application/json'}
        if method=='POST':h.update({'Origin':self.origin,'Idempotency-Key':'http-proof-key'})
        if cookie:h['Cookie']=cookie
        if token:h['Authorization']='Bearer '+token
        h.update(headers or {})
        c.request(method,path,json.dumps(body) if body is not None else None,h)
        r=c.getresponse();raw=r.read();result=(r.status,dict(r.getheaders()),json.loads(raw) if 'application/json' in r.getheader('Content-Type','') else raw)
        c.close();return result
    def open(self):
        status,headers,_=self.request('/workspace');self.assertEqual(status,200)
        self.assertIn('HttpOnly; SameSite=Strict',headers['Set-Cookie']);self.assertNotIn('Max-Age=',headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';')[0]
    def test_full_http_session_and_business_isolation(self):
        a=self.open();b=self.open()
        status,_,session=self.request('/api/session','POST',a,body={'actor_id':'quality'});self.assertEqual(status,200)
        self.assertEqual(self.request('/api/cases/CASE-001/submit','POST',b,session['token'],{'expected_version':1})[0],401)
        self.assertEqual(self.request('/api/cases/CASE-001/submit','POST',a,session['token'],{'expected_version':1})[0],200)
        _,_,sa=self.request('/api/cases/CASE-001',cookie=a);_,_,sb=self.request('/api/cases/CASE-001',cookie=b)
        self.assertNotEqual(sa['version'],sb['version'])
    def test_html_aliases_keep_warning(self):
        a=self.open()
        for path in ['/workspace?x','/index.html','/landing.html','/static/index.html','/index%2ehtml']:
            status,_,body=self.request(path,cookie=a)
            self.assertEqual(status,200);self.assertIn('방문자별 가상업무 연습'.encode(),body)
    def test_secret_ai_and_files(self):
        a=self.open()
        for path in ['/backend.py','/public_demo.py','/.env','/../backend.py','/runtime/aurora.sqlite3','/local-secret']:
            self.assertEqual(self.request(path,cookie=a)[0],404)
        self.assertEqual(self.request('/api/cases/CASE-001/ai-brief','POST',a,body={})[0],403)
        _,_,state=self.request('/api/state',cookie=a);self.assertFalse(state['ai']['request_allowed'])
    def test_cross_site_and_body_limit(self):
        self.assertEqual(self.request('/workspace',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        a=self.open()
        self.assertEqual(self.request('/api/session','POST',a,body={},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/session','POST',a,body={},headers={'Content-Length':'70000'})[0],413)
    def test_unsupported_method_does_not_crash(self):
        self.assertEqual(self.request('/',method='HEAD')[0],501)
    def test_forged_cookie_and_post_origin_missing(self):
        self.assertEqual(self.request('/api/state',cookie='aurora_demo='+'x'*43)[0],401)
        a=self.open();c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        c.request('POST','/api/session','{}',{'Content-Type':'application/json','Cookie':a})
        r=c.getresponse();r.read();self.assertEqual(r.status,403);c.close()

class BoundTests(unittest.TestCase):
    def test_workers_and_read_timeout(self):
        import socket,time
        with patch.dict(os.environ,{},clear=True),tempfile.TemporaryDirectory() as t:
            s=make_demo_server(t+'/state',max_workers=1,read_timeout=.3)
            thread=threading.Thread(target=s.serve_forever,daemon=True);thread.start()
            first=socket.create_connection(s.server_address,timeout=2)
            try:
                end=time.monotonic()+2
                while s.slots._value and time.monotonic()<end:time.sleep(.01)
                second=socket.create_connection(s.server_address,timeout=2)
                self.assertIn(b'503',second.recv(1024));second.close()
                time.sleep(.4);self.assertEqual(first.recv(1024),b'')
            finally:first.close();s.shutdown();s.server_close();thread.join()
    def test_global_rate_and_banner_http(self):
        with patch.dict(os.environ,{},clear=True),tempfile.TemporaryDirectory() as t:
            s=make_demo_server(t+'/state',max_global_per_min=2)
            thread=threading.Thread(target=s.serve_forever,daemon=True);thread.start()
            try:
                for n,path in enumerate(['/workspace','/healthz','/healthz']):
                    c=http.client.HTTPConnection(*s.server_address,timeout=30);c.request('GET',path)
                    r=c.getresponse();body=r.read();self.assertEqual(r.status,429 if n==2 else 200)
                    if n==0:self.assertIn('방문자별 가상업무 연습'.encode(),body)
                    c.close()
            finally:s.shutdown();s.server_close();thread.join()
