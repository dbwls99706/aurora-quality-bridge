"""Bounded worker/cache proofs with fake DB latency. Not real PG timing."""
import threading
from collections import OrderedDict
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock,patch
from backend import APIError
from public_demo import BoundedDemoServer
from public_demo_cloud import CloudServer,is_static_asset
from public_demo_postgres import PostgresDemoRegistry

class PerformanceTests(unittest.TestCase):
    def registry(self):
        r=PostgresDemoRegistry.__new__(PostgresDemoRegistry);r.config='fake';r.connector=None
        r._cache_lock=threading.Lock();r._service_locks={};r._services=OrderedDict()
        return r
    def test_same_workspace_concurrent_constructor_once(self):
        r=self.registry();created=[]
        def construct(*a):time.sleep(.03);obj=object();created.append(obj);return obj
        with patch('public_demo_postgres.PGDemoService',side_effect=construct):
            with ThreadPoolExecutor(4) as pool:results=list(pool.map(lambda _:r._get_service('a'*32),range(4)))
        self.assertEqual(len(created),1);self.assertTrue(all(x is created[0] for x in results))
    def test_distinct_workspace_services_not_shared(self):
        r=self.registry()
        with patch('public_demo_postgres.PGDemoService',side_effect=lambda *a:object()) as init:
            a=r._get_service('a'*32);b=r._get_service('b'*32)
            self.assertIsNot(a,b);self.assertIs(a,r._get_service('a'*32));self.assertEqual(init.call_count,2)
    def test_failed_initialization_not_cached(self):
        r=self.registry();service=object()
        with patch('public_demo_postgres.PGDemoService',side_effect=[ValueError('failed'),service]) as init:
            with self.assertRaises(ValueError):r._get_service('a'*32)
            self.assertNotIn('a'*32,r._services);self.assertIs(r._get_service('a'*32),service);self.assertEqual(init.call_count,2)
    def test_lru_keeps_only_eight_and_does_not_close_active_references(self):
        r=self.registry();made=[]
        def create(config,workspace,connector):
            obj=Mock();obj.workspace=workspace;made.append(obj);return obj
        with patch('public_demo_postgres.PGDemoService',side_effect=create):
            refs=[r._get_service(str(i)) for i in range(9)]
            self.assertEqual(len(r._services),8);self.assertNotIn('0',r._services)
            self.assertEqual(refs[0].workspace,'0');refs[0].close.assert_not_called()
            r._get_service('1');r._get_service('9')
            self.assertIn('1',r._services);self.assertNotIn('2',r._services)
            again=r._get_service('0');self.assertIsNot(again,refs[0]);self.assertEqual(again.workspace,'0')
            self.assertEqual(len(r._services),8)
            for obj in made:obj.close.assert_not_called()
    def test_cache_metadata_bounded_200(self):
        r=self.registry();r._service_locks={str(i):threading.Lock() for i in range(200)}
        with self.assertRaises(APIError):r._get_service('new')
        self.assertEqual(len(r._service_locks),200)
    def test_two_hundred_lock_slots_keep_only_eight_services(self):
        r=self.registry()
        with patch('public_demo_postgres.PGDemoService',side_effect=lambda *a:object()):
            for i in range(200):r._get_service(str(i))
            self.assertEqual(len(r._service_locks),200);self.assertEqual(len(r._services),8)
            with self.assertRaises(APIError):r._get_service('overflow')
            self.assertIsNotNone(r._get_service('0'))
            self.assertEqual(len(r._services),8);self.assertEqual(len(r._service_locks),200)

    def test_static_allowlist_excludes_all_api_and_html(self):
        for path in ['/app.js','/static/workspace.css','/app.js?v=1']:self.assertTrue(is_static_asset(path))
        for path in ['/api/state','/api/sources/app.js','/workspace','/index.html','/app%2ejs','/static/../api/state','/unknown.js']:self.assertFalse(is_static_asset(path))
    def test_cached_resolve_and_static_still_validate_limits(self):
        r=self.registry();r._get_service=Mock(return_value='cached')
        db=Mock();ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
        with patch('public_demo_postgres.registry_connection',return_value=ctx),patch.object(r,'_limits',return_value=([1000,40,2000,7200,120,4],200)) as limits:
            db.execute.return_value.fetchone.return_value=('a'*32,100,300,1,2000)
            self.assertEqual(r.resolve('b'*43),('cached',None))
            self.assertEqual(r.resolve('b'*43,initialize_service=False),(None,None))
            self.assertEqual(limits.call_count,2);r._get_service.assert_called_once()
            db.execute.return_value.fetchone.return_value=('a'*32,100,0,1,2000)
            with self.assertRaises(APIError):r.resolve('b'*43)
            db.execute.return_value.fetchone.return_value=('a'*32,100,300,2000,2000)
            with self.assertRaises(APIError):r.resolve('b'*43)
            self.assertEqual(r._get_service.call_count,1)
    def test_admission_runs_in_worker_before_handler(self):
        s=BoundedDemoServer.__new__(BoundedDemoServer);s.slots=threading.BoundedSemaphore(1)
        entered=threading.Event();release=threading.Event();finished=threading.Event();caller=threading.get_ident();ids=[]
        def admit():ids.append(threading.get_ident());entered.set();release.wait(2)
        s.registry=Mock();s.registry.admit_connection.side_effect=admit
        s.finish_request=Mock(side_effect=lambda *a:finished.set());s.shutdown_request=Mock();s.handle_error=Mock();s.daemon_threads=True
        s.process_request(object(),('127.0.0.1',1));self.assertTrue(entered.wait(1))
        self.assertNotEqual(ids[0],caller);s.finish_request.assert_not_called()
        release.set();self.assertTrue(finished.wait(1))
    def test_cloud_overflow_never_reads_db_or_transmits(self):
        s=CloudServer.__new__(CloudServer);s.slots=threading.BoundedSemaphore(1);s.slots.acquire()
        s.registry=Mock();s.shutdown_request=Mock();request=Mock()
        s.process_request(request,('127.0.0.1',1))
        s.registry.assert_not_called();s.registry.admit_connection.assert_not_called();s.shutdown_request.assert_called_once_with(request);request.sendall.assert_not_called()
