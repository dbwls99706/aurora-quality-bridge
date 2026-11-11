"""Fake runtime/header tests, NOT actual HTTPS/gateway/DB integration."""
import io
import os
import json
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock,patch
from backend import APIError,Handler
import public_demo_cloud as cloud
from public_demo_cloud import Runtime,CloudHandler,NeutralNavigation

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.profile=patch.multiple(cloud.profile,ORIGIN='https://new-demo.example',OWNER_UUID='00000000-0000-0000-0000-000000000001',TENANT='test',EXPIRES_AT='2099-01-01T00:00:00Z',PUBLIC_RELEASE=True,GATEWAY_VERIFIED=True,HTTPS_BROWSER_VERIFIED=True);self.profile.start()
        self.runtime=Runtime();self.runtime.registry=Mock();self.runtime.registry.reserve_response.return_value='allowed';self.runtime.result={'status':'passed'}
        self.runtime.registry.resolve.return_value=('service','a'*43)
    def tearDown(self):self.profile.stop()
    def handler(self,method='GET',path='/'):
        h=CloudHandler.__new__(CloudHandler);h.command=method;h.path=path;h.headers=Message();h.headers['Host']='new-demo.example'
        h.server=SimpleNamespace(runtime=self.runtime);h.new_cookie=None;h.demo_service=None
        return h
    def test_empty_profile_cannot_initialize(self):
        with patch.object(cloud.profile,'APP_ID',''),patch('public_demo_cloud.PostgresDemoRegistry') as registry:
            r=Runtime();r.initialize();self.assertIsNone(r.registry);registry.assert_not_called()
    def test_release_requires_explicit_flag_and_passed(self):
        h=self.handler()
        for status,flag in [('failed',True),('running',True),('passed',False)]:
            self.runtime.result={'status':status}
            with patch.object(cloud.profile,'PUBLIC_RELEASE',flag):
                with self.assertRaises(APIError):h.validate_authority()
        self.runtime.registry.resolve.assert_not_called()
    def test_owner_preview_before_public_release(self):
        h=self.handler()
        for key,val in [('X-AxHub-User-ID',cloud.profile.OWNER_UUID),('X-AxHub-Tenant-Slug','test'),('X-AxHub-App-Role','owner'),('X-AxHub-Surface','public')]:h.headers[key]=val
        with patch.object(cloud.profile,'PUBLIC_RELEASE',False),patch.object(cloud.profile,'HTTPS_BROWSER_VERIFIED',False):
            h.validate_authority();self.assertEqual(h.demo_service,'service')
    def test_unverified_browser_blocks_anonymous_even_with_release_flag(self):
        with patch.object(cloud.profile,'HTTPS_BROWSER_VERIFIED',False):
            with self.assertRaises(APIError):self.handler().validate_authority()
        self.runtime.registry.resolve.assert_not_called()
    def test_unverified_gateway_blocks_owner(self):
        with patch.object(cloud.profile,'GATEWAY_VERIFIED',False):
            with self.assertRaises(APIError):self.handler()._owner()
    def owner_headers(self,h):
        for key,val in [('X-AxHub-User-ID',cloud.profile.OWNER_UUID),('X-AxHub-Tenant-Slug','test'),('X-AxHub-App-Role','owner'),('X-AxHub-Surface','public')]:h.headers[key]=val
    def test_owner_native_qa_form(self):
        h=self.handler(path='/__qa');self.owner_headers(h);h._html=Mock()
        h.do_GET();text=h._html.call_args.args[0]
        self.assertIn('method="post" action="/__qa/run"',text)
        self.assertIn('격리 검증 실행',text);self.assertIn('/__qa/report',text)
        self.runtime.registry.resolve.assert_not_called()
    def test_anonymous_cannot_view_qa_form(self):
        h=self.handler(path='/__qa');h.json_response=Mock();h._html=Mock();h.do_GET()
        self.assertEqual(h.json_response.call_args.args[0],401);h._html.assert_not_called()
    def test_native_empty_form_post_requires_origin_and_runs_once(self):
        h=self.handler(method='POST',path='/__qa/run');self.owner_headers(h)
        h.headers['Origin']='https://new-demo.example';h.headers['Content-Type']='application/x-www-form-urlencoded';h.headers['Content-Length']='0';h.headers['Sec-Fetch-Site']='same-origin'
        h.json_response=Mock();self.runtime.start_qa=Mock(return_value={'status':'running'})
        h.do_POST();self.runtime.start_qa.assert_called_once_with();self.assertEqual(h.json_response.call_args.args[0],202)
        del h.headers['Origin'];h.do_POST();self.assertEqual(h.json_response.call_args.args[0],403);self.assertEqual(self.runtime.start_qa.call_count,1)
    def test_short_window_allows_only_while_persisted_open(self):
        self.runtime.test_deadline=200
        with patch.object(cloud.profile,'PUBLIC_RELEASE',False),patch.object(cloud.profile,'HTTPS_BROWSER_VERIFIED',False),patch.object(cloud.profile,'ANONYMOUS_TEST_ENABLED',True),patch.object(cloud.profile,'ANONYMOUS_TEST_ROUND','performance-v1'):
            self.runtime.registry.test_window_open.return_value=True
            h=self.handler();h.validate_authority();self.assertEqual(h.demo_service,'service')
            self.runtime.registry.resolve.side_effect=APIError('DEMO_TEST_WINDOW_CLOSED','closed',503)
            for path in ['/','/workspace','/api/state']:
                h=self.handler(path=path);h.headers['Cookie']='__Host-aurora_demo='+'a'*43
                with self.assertRaises(APIError):h.validate_authority()
    def test_diagnostics_are_phase_and_booleans_without_secrets(self):
        base={'AURORA_ISOLATED_APP_ID':cloud.profile.APP_ID,'AURORA_AI_ALLOW_NETWORK':'0'}
        with patch.dict(os.environ,{**base,'LLM_API_KEY':'NEVER_DISCLOSE'},clear=True),patch('public_demo_cloud.PostgresDemoRegistry') as db:
            r=Runtime();r.initialize();self.assertEqual(r.result['phase'],'provider_presence');db.assert_not_called()
            self.assertTrue(r.result['diagnostics']['provider_env_present']);self.assertNotIn('NEVER_DISCLOSE',json.dumps(r.result))
        url='postgresql://'+cloud.profile.DATABASE_USER+':NEVER_DISCLOSE@internal.invalid/'+cloud.profile.DATABASE_NAME+'?service=forbidden'
        with patch.dict(os.environ,{**base,'DATABASE_URL':url},clear=True),patch('public_demo_cloud.PostgresDemoRegistry') as db:
            r=Runtime();r.initialize();self.assertEqual(r.result['phase'],'db_config');db.assert_not_called()
            self.assertFalse(r.result['diagnostics']['db_query_keys_allowed'])
            self.assertTrue(all(type(v) is bool for v in r.result['diagnostics'].values()))
            self.assertNotIn('NEVER_DISCLOSE',json.dumps(r.result));self.assertNotIn('internal.invalid',json.dumps(r.result))
        url='postgresql://'+cloud.profile.DATABASE_USER+':NEVER_DISCLOSE@internal.invalid/'+cloud.profile.DATABASE_NAME+'?sslmode=disable'
        with patch.dict(os.environ,{**base,'DATABASE_URL':url,'DIRECT_DATABASE_URL':'NEVER_READ'},clear=True),patch('public_demo_cloud.PostgresDemoRegistry',side_effect=ValueError('NEVER_DISCLOSE')):
            r=Runtime();r.initialize();self.assertEqual(r.result['phase'],'db_init')
            self.assertTrue(r.result['diagnostics']['db_sslmode_allowed']);self.assertNotIn('NEVER_DISCLOSE',json.dumps(r.result))
    def test_initialize_restores_passed_without_executing_qa(self):
        env={'AURORA_ISOLATED_APP_ID':cloud.profile.APP_ID,'AURORA_AI_ALLOW_NETWORK':'0',
             'DATABASE_URL':'postgresql://'+cloud.profile.DATABASE_USER+':fake@internal.invalid/'+cloud.profile.DATABASE_NAME+'?sslmode=disable'}
        saved={'status':'passed','qa_version':'verified-by-loader','real_judge_login_tested':False}
        with patch.dict(os.environ,env,clear=True),patch('public_demo_cloud.PostgresDemoRegistry'),patch('public_demo_cloud_selftest.load_result',return_value=saved) as load,patch('public_demo_cloud_selftest.run_once') as run:
            r=Runtime();r.initialize();self.assertEqual(r.result,saved);self.assertTrue(r.ready());load.assert_called_once();run.assert_not_called()
    def test_disconnected_client_is_not_sent_second_error(self):
        h=self.handler(path='/app.js');h.validate_authority=Mock();h.send_response=Mock();h.send_header=Mock();h.end_headers=Mock();h.wfile=Mock();h.wfile.write.side_effect=BrokenPipeError();h.json_response=Mock()
        h.do_GET();self.assertTrue(h.close_connection);h.json_response.assert_not_called();self.assertEqual(h.wfile.write.call_count,1)
    def test_visitor_query_cannot_select_verification_round(self):
        self.runtime.test_deadline=200;self.runtime.registry.test_window_open.return_value=True
        with patch.object(cloud.profile,'PUBLIC_RELEASE',False),patch.object(cloud.profile,'ANONYMOUS_TEST_ENABLED',True),patch.object(cloud.profile,'ANONYMOUS_TEST_ROUND','performance-v1'):
            h=self.handler(path='/api/state?round=visitor-chosen');h.headers['Cookie']='__Host-aurora_demo='+'a'*43
            h.validate_authority();self.assertEqual(self.runtime.registry.resolve.call_args.kwargs['required_window'],(200,'performance-v1'));self.runtime.registry.test_window_open.assert_not_called()
    def test_secure_cookie_and_no_domain(self):
        h=self.handler();h.validate_authority();h.send_header=Mock()
        with patch.object(Handler,'end_headers'):h.end_headers()
        cookie=h.send_header.call_args.args[1]
        self.assertIn('__Host-aurora_demo=',cookie);self.assertIn('Secure; HttpOnly; SameSite=Strict; Path=/',cookie);self.assertNotIn('Domain=',cookie)
    def test_cross_site_neutral_does_not_allocate(self):
        h=self.handler();h.headers['Sec-Fetch-Site']='cross-site';h.headers['Sec-Fetch-Mode']='navigate';h.headers['Sec-Fetch-Dest']='document'
        with self.assertRaises(NeutralNavigation):h.validate_authority()
        self.runtime.registry.resolve.assert_not_called()
    def test_cross_site_api_and_missing_post_origin_denied(self):
        h=self.handler('GET','/api/state');h.headers['Sec-Fetch-Site']='cross-site'
        with self.assertRaises(APIError):h.validate_authority()
        h=self.handler('POST','/api/session')
        with self.assertRaises(APIError):h.validate_authority()
    def test_owner_route_not_anonymous(self):
        h=self.handler()
        with self.assertRaises(APIError):h._owner()
        for key,val in [('X-AxHub-User-ID',cloud.profile.OWNER_UUID),('X-AxHub-Tenant-Slug','test'),('X-AxHub-App-Role','owner'),('X-AxHub-Surface','public')]:h.headers[key]=val
        h._owner()
        h.headers['X-AxHub-User-ID']=cloud.profile.OWNER_UUID
        with self.assertRaises(APIError):h._owner()
    def test_duplicate_cookie_rejected(self):
        h=self.handler();h.headers['Cookie']='__Host-aurora_demo='+'a'*43+'; __Host-aurora_demo = '+'b'*43
        with self.assertRaises(APIError):h.validate_authority()
    def test_egress_reserved_before_headers_and_small_denial(self):
        h=self.handler();h.connection=Mock();h.request_version='HTTP/1.1';h.requestline='GET / HTTP/1.1';h.wfile=io.BytesIO()
        self.runtime.registry.reserve_response.return_value='small_denial'
        h.json_response(200,{'data':'test'})
        self.assertEqual(h.wfile.getvalue(),b'');self.assertLessEqual(len(h.connection.sendall.call_args.args[0]),256)
        self.assertTrue(h.close_connection)
    def test_exhausted_budget_emits_no_bytes(self):
        h=self.handler();h.connection=Mock();h.request_version='HTTP/1.1';h.requestline='GET / HTTP/1.1';h.wfile=io.BytesIO()
        self.runtime.registry.reserve_response.return_value='close';h.json_response(200,{'data':'test'})
        h.connection.sendall.assert_not_called();self.assertEqual(h.wfile.getvalue(),b'')
    def test_response_hard_limit(self):
        h=self.handler()
        with self.assertRaises(APIError):h.send_header('Content-Length','2097153')
        self.runtime.registry.reserve_response.assert_not_called()

    def test_combined_window_static_and_owner_fallback(self):
        self.runtime.test_deadline=200
        with patch.multiple(cloud.profile,PUBLIC_RELEASE=False,ANONYMOUS_TEST_ENABLED=True,ANONYMOUS_TEST_ROUND='performance-v1'):
            h=self.handler(path='/app.js');h.headers['Cookie']='__Host-aurora_demo='+'a'*43
            h.validate_authority()
            self.assertEqual(self.runtime.registry.resolve.call_args.kwargs,{'initialize_service':False,'required_window':(200,'performance-v1')})
            self.runtime.registry.resolve.side_effect=[APIError('DEMO_TEST_WINDOW_CLOSED','closed',503),('owner-service',None)]
            self.owner_headers(h);h.validate_authority();self.assertEqual(h.demo_service,'owner-service')
            self.assertNotIn('required_window',self.runtime.registry.resolve.call_args.kwargs)
    def test_combined_neutral_still_checks_window(self):
        self.runtime.test_deadline=200
        with patch.multiple(cloud.profile,PUBLIC_RELEASE=False,ANONYMOUS_TEST_ENABLED=True,ANONYMOUS_TEST_ROUND='performance-v1'):
            h=self.handler();h.headers['Sec-Fetch-Site']='cross-site';h.headers['Sec-Fetch-Mode']='navigate';h.headers['Sec-Fetch-Dest']='document'
            self.runtime.registry.test_window_open.return_value=False
            with self.assertRaises(APIError):h.validate_authority()
            self.runtime.registry.test_window_open.assert_called_once_with(200,'performance-v1');self.runtime.registry.resolve.assert_not_called()

    def test_expired_entry_shows_explicit_restart_without_cookie_change(self):
        h=self.handler(path='/workspace');h.headers['Cookie']='__Host-aurora_demo='+'a'*43
        self.runtime.registry.resolve.side_effect=APIError('DEMO_EXPIRED','expired',403)
        h._html=Mock();h.json_response=Mock();h.do_GET()
        self.assertIn('method="post" action="/__demo/restart"',h._html.call_args.args[0])
        self.assertIn('이전 연습 기록은 보존',h._html.call_args.args[0]);self.assertFalse(getattr(h,'clear_demo_cookie',False))
        self.assertIsNone(h.new_cookie);self.runtime.registry.resolve.assert_called_once()

    def restart_handler(self):
        h=self.handler('POST','/__demo/restart');h.headers['Origin']='https://new-demo.example'
        h.headers['Cookie']='__Host-aurora_demo='+'a'*43;h.headers['Content-Length']='0';h.headers['Sec-Fetch-Site']='same-origin'
        h.send_response=Mock();h.send_header=Mock();h.end_headers=Mock();h.json_response=Mock()
        return h

    def test_restart_only_clears_expired_cookie_and_redirects(self):
        h=self.restart_handler();self.runtime.registry.resolve.side_effect=APIError('DEMO_EXPIRED','expired',403)
        h.do_POST();h.send_response.assert_called_once_with(303)
        self.assertTrue(h.clear_demo_cookie);self.assertIsNone(h.new_cookie)
        self.assertIn(('Location','/workspace'),[c.args for c in h.send_header.call_args_list])
        self.runtime.registry.resolve.assert_called_once();h.json_response.assert_not_called()
        h.end_headers=CloudHandler.end_headers.__get__(h)
        with patch.object(Handler,'end_headers'):h.end_headers()
        cookies=[c.args[1] for c in h.send_header.call_args_list if c.args[0]=='Set-Cookie']
        self.assertEqual(cookies,['__Host-aurora_demo=; Max-Age=0; Secure; HttpOnly; SameSite=Strict; Path=/'])

    def test_restart_does_not_clear_active_cookie(self):
        h=self.restart_handler();h.do_POST();self.assertEqual(h.json_response.call_args.args[0],409)
        self.assertFalse(getattr(h,'clear_demo_cookie',False));h.send_response.assert_not_called()

    def test_restart_keeps_origin_cookie_and_app_expiry_guards(self):
        for failure in ['origin','cross-site','missing-cookie','forged-cookie','app-expired','quota','app-limit']:
            with self.subTest(failure=failure):
                h=self.restart_handler();self.runtime.registry.resolve.reset_mock();self.runtime.registry.resolve.side_effect=APIError('DEMO_EXPIRED','expired',403)
                if failure=='origin':del h.headers['Origin']
                if failure=='cross-site':h.headers.replace_header('Sec-Fetch-Site','cross-site')
                if failure=='missing-cookie':del h.headers['Cookie']
                if failure=='forged-cookie':self.runtime.registry.resolve.side_effect=APIError('DEMO_SESSION_INVALID','invalid',401)
                if failure=='quota':self.runtime.registry.resolve.side_effect=APIError('DEMO_REQUEST_LIMIT','limit',429)
                if failure=='app-limit':self.runtime.registry.resolve.side_effect=APIError('DEMO_APP_EXPIRED','expired',403)
                with patch.object(self.runtime,'expired',return_value=failure=='app-expired'):h.do_POST()
                h.send_response.assert_not_called();self.assertFalse(getattr(h,'clear_demo_cookie',False));self.assertTrue(h.json_response.called)

    def test_restart_rejects_body_and_transfer_encoding(self):
        for kind in ['body','transfer']:
            h=self.restart_handler()
            if kind=='body':h.headers.replace_header('Content-Length','1')
            else:h.headers['Transfer-Encoding']='chunked'
            h.do_POST();self.assertEqual(h.json_response.call_args.args[0],400);h.send_response.assert_not_called()
        self.runtime.registry.resolve.assert_not_called()

    def test_entry_does_not_offer_restart_for_app_expiry_or_unknown_cookie(self):
        for code in ['DEMO_APP_EXPIRED','DEMO_SESSION_INVALID','DEMO_REQUEST_LIMIT']:
            h=self.handler(path='/workspace');h.headers['Cookie']='__Host-aurora_demo='+'a'*43
            self.runtime.registry.resolve.side_effect=APIError(code,'blocked',403);h._html=Mock();h.json_response=Mock()
            h.do_GET();h._html.assert_not_called();self.assertEqual(h.json_response.call_args.args[0],403)
