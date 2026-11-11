import sys, unittest, tempfile, threading, json, http.client, re, os
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import AuroraService, make_server, APIError
from local_secret import LocalSecret

class SecretTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  with patch.dict(os.environ,{'AURORA_AI_PROVIDER':'anthropic','AURORA_AI_API_KEY':'','ANTHROPIC_API_KEY':''}):
   self.s=AuroraService(db_path=Path(self.tmp.name)/'state.db')
  self.server=make_server(self.s,port=0);self.entry=LocalSecret(self.s.ai, APIError);self.server.local_secret=self.entry
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.host=f'127.0.0.1:{self.server.server_port}';self.key='sk-ant-test-dummy-only-123456789'
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.tmp.cleanup()
 def req(self,path='/api/local-secret',payload=None,headers=None,method='POST'):
  c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
  h={'Content-Type':'application/json','Origin':'http://'+self.host,'X-Local-Secret-CSRF':self.entry.token};h.update(headers or {})
  c.request(method,path,json.dumps(payload) if payload is not None else None,h);r=c.getresponse();b=r.read();out=r.status,dict(r.getheaders()),b;c.close();return out
 def test_one_shot_memory_clear(self):
  code,h,b=self.req(payload={'action':'set','key':self.key});self.assertEqual(code,200);self.assertNotIn(self.key.encode(),b)
  self.assertEqual(self.s.ai.key,self.key)
  self.assertEqual(self.req(payload={'action':'set','key':self.key})[0],409)
  self.assertEqual(self.req(payload={'action':'clear'})[0],200);self.assertEqual(self.s.ai.key,'')
  self.assertEqual(self.req(payload={'action':'set','key':self.key})[0],409)
  for f in Path(self.tmp.name).rglob('*'):
   if f.is_file():self.assertNotIn(self.key.encode(),f.read_bytes())
 def test_page_headers_no_external_storage(self):
  code,h,b=self.req('/local-secret',method='GET');self.assertEqual(code,200);self.assertEqual(h['Cache-Control'],'no-store');self.assertEqual(h['X-Frame-Options'],'DENY')
  self.assertIn('frame-ancestors',h['Content-Security-Policy']);self.assertIn(b'type="password"',b)
  for text in [b'localStorage',b'sessionStorage',b'https://',self.key.encode()]:self.assertNotIn(text,b)
 def test_csrf_origin_host(self):
  p={'action':'set','key':self.key}
  for h in [{'X-Local-Secret-CSRF':'bad'},{'Origin':'http://evil.test'},{'Host':'evil.test'},{'Origin':''},{'Sec-Fetch-Site':'cross-site'}]:
   self.assertEqual(self.req(payload=p,headers=h)[0],403)
  self.assertEqual(self.s.ai.key,'')
 def test_expiry_clear_and_busy(self):
  self.entry.deadline=0;self.assertEqual(self.req(payload={'action':'set','key':self.key})[0],409)
  self.s.ai._active=1;self.assertEqual(self.req(payload={'action':'clear'})[0],409)
  self.s.ai._active=0;self.assertEqual(self.req(payload={'action':'clear'})[0],200)
 def test_no_route_when_disabled(self):
  del self.server.local_secret
  self.assertEqual(self.req('/local-secret',method='GET')[0],404)
  self.assertEqual(self.req(payload={'action':'set','key':self.key})[0],404)
 def test_control_characters_rejected(self):
  for suffix in ['\x00','\x1b','\x7f',' ', '\n']:
   self.assertEqual(self.req(payload={'action':'set','key':self.key+suffix})[0],400)
  self.assertEqual(self.s.ai.key,'')
 def test_discard_before_admission_blocks_generation(self):
  self.s.ai.model='claude-haiku-4-5-20251001';self.s.ai.enabled=True
  self.entry.submit(self.entry.token,{'action':'set','key':self.key})
  self.entry.submit(self.entry.token,{'action':'clear'})
  with patch.object(self.s.ai,'_generate') as gen:
   with self.assertRaises(APIError) as e:self.s.ai.generate({})
   self.assertEqual(e.exception.code,'AI_NOT_CONFIGURED');gen.assert_not_called()
  self.assertEqual(self.s.ai._attempts,0)
 def test_invalid_key_not_echoed(self):
  code,h,b=self.req(payload={'action':'set','key':'SECRET-bad'});self.assertEqual(code,400);self.assertNotIn(b'SECRET',b)

if __name__=='__main__':unittest.main()
