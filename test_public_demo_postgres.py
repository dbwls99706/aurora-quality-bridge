"""Fake-driver boundary tests ONLY. No real PostgreSQL connection or execution."""
import os
import unittest
from unittest.mock import patch,Mock
from public_demo_postgres import DedicatedDatabase,PostgresDemoRegistry,ScopedConnection,connect,registry_connection,PGDemoService,REG,PRODUCTION_APP,PRODUCTION_DATABASE
from backend import APIError
from persistence import Record

APP='00000000-0000-0000-0000-000000000123'
def config():return DedicatedDatabase(APP,'isolated_qa','qa_user','postgresql://qa_user:fake_password@db.invalid/isolated_qa?sslmode=require')

class Fake:
    def __init__(self,identity=('isolated_qa','qa_user'),workspace_live=True):
        self.calls=[];self.identity=identity;self.workspace_live=workspace_live;self.last=''
    def execute(self,sql,params=()):self.calls.append((sql,params));self.last=sql;return self
    def fetchone(self):
        if self.last=='SELECT current_database(),current_user':return self.identity
        if 'to_regnamespace' in self.last:return Record(app_id=APP,app_expires=200,workspace_expires=200 if self.workspace_live else 0,present=True,clock=100)
        return None
    def commit(self):pass
    def rollback(self):pass
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{},clear=True);self.env.start()
        self.binding=patch('public_demo_postgres.APPROVED_BINDINGS',frozenset({(APP,'isolated_qa','qa_user')}));self.binding.start()
    def tearDown(self):self.binding.stop();self.env.stop()
    def test_production_id_and_mismatched_identity_rejected(self):
        for app,db,user,url in [(PRODUCTION_APP,'isolated_qa','qa_user',config().url),(APP,'other','qa_user',config().url),(APP,'isolated_qa','other',config().url)]:
            with self.assertRaises(ValueError):DedicatedDatabase(app,db,user,url)
    def test_empty_approval_binding_prevents_any_connection(self):
        with patch('public_demo_postgres.APPROVED_BINDINGS',frozenset()):
            with self.assertRaises(ValueError):config()
    def test_original_production_database_rejected_even_if_binding_mistaken(self):
        binding=(APP,PRODUCTION_DATABASE,'qa_user')
        with patch('public_demo_postgres.APPROVED_BINDINGS',frozenset({binding})):
            with self.assertRaises(ValueError):DedicatedDatabase(APP,PRODUCTION_DATABASE,'qa_user','postgresql://qa_user:fake@db.invalid/'+PRODUCTION_DATABASE)
    def test_credentials_not_in_repr(self):self.assertNotIn('fake_password',repr(config()))
    def test_override_and_weak_tls_rejected(self):
        for suffix in ['?sslmode=disable','?hostaddr=127.0.0.1','?service=prod','?sslmode=require&sslmode=disable']:
            with self.assertRaises(ValueError):DedicatedDatabase(APP,'isolated_qa','qa_user','postgresql://qa_user:fake@db.invalid/isolated_qa'+suffix)
    def test_never_reads_database_environment(self):
        with patch.dict(os.environ,{'DATABASE_URL':'postgresql://production/never','DIRECT_DATABASE_URL':'postgresql://production/never'}):
            seen=[]
            connect(config(),connector=lambda **kw:seen.append(kw))
            self.assertEqual(seen[0]['host'],'db.invalid');self.assertEqual(seen[0]['passfile'],'/dev/null')
    def test_libpq_override_denied(self):
        with patch.dict(os.environ,{'PGHOSTADDR':'127.0.0.1'}):
            with self.assertRaises(ValueError):connect(config(),connector=lambda **kw:self.fail('connected'))
    def test_connected_identity_before_writes(self):
        fake=Fake(identity=('production','owner'))
        with self.assertRaises(ValueError):
            with registry_connection(config(),lambda **kw:fake):self.fail('entered')
        self.assertFalse(any(x[0].startswith(('CREATE','INSERT','UPDATE')) for x in fake.calls))
    def test_transfer_reservation_outcomes(self):
        reg=PostgresDemoRegistry.__new__(PostgresDemoRegistry);reg.config=config();reg.connector=None
        for used,cap,amount,outcome in [(0,1000,500,'allowed'),(500,1000,700,'small_denial'),(900,1000,700,'close')]:
            db=Mock();db.execute.return_value.fetchone.return_value=(used,cap)
            ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
            with patch('public_demo_postgres.registry_connection',return_value=ctx),patch.object(reg,'_limits'):
                self.assertEqual(reg.reserve_response(amount),outcome)
            updates=[call for call in db.execute.call_args_list if call.args[0].startswith('UPDATE')]
            self.assertEqual(len(updates),0 if outcome=='close' else 1)
    def test_test_window_cap_and_minimum_persistence_sql(self):
        reg=PostgresDemoRegistry.__new__(PostgresDemoRegistry);reg.config=config();reg.connector=None
        db=Mock();ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
        with patch('public_demo_postgres.registry_connection',return_value=ctx),patch.object(reg,'_limits',return_value=([10000],1000)),patch('public_demo_postgres.APPROVED_TEST_ROUNDS',frozenset({(APP,'performance-v1')})):
            for deadline in [999,1901,float('inf')]:
                with self.assertRaises(ValueError):reg.configure_test_window(deadline,'performance-v1')
            db.execute.assert_not_called()
            reg.configure_test_window(1800,'performance-v1')
            self.assertTrue(any('least(existing.expires,EXCLUDED.expires)' in c.args[0] for c in db.execute.call_args_list))
    def test_only_verified_axhub_transport_accepts_documented_disable(self):
        app='00000000-0000-4000-8000-000000000001';db='example_demo'
        url='postgresql://'+db+':fake@db.invalid/'+db+'?sslmode=disable'
        with patch('public_demo_postgres.APPROVED_BINDINGS',frozenset({(app,db,db)})):
            with self.assertRaises(ValueError):DedicatedDatabase(app,db,db,url)
            c=DedicatedDatabase(app,db,db,url,transport_profile='axhub_runtime_standard')
            self.assertEqual(c.parameters()['sslmode'],'disable')
        with self.assertRaises(ValueError):DedicatedDatabase(APP,'isolated_qa','qa_user','postgresql://qa_user:fake@db.invalid/isolated_qa?sslmode=disable',transport_profile='axhub_runtime_standard')
    def test_unknown_round_refused_before_database(self):
        reg=PostgresDemoRegistry.__new__(PostgresDemoRegistry);reg.config=config();reg.connector=None
        with patch('public_demo_postgres.registry_connection') as db:
            with self.assertRaises(ValueError):reg.configure_test_window(1800,'visitor-chosen')
            self.assertFalse(reg.test_window_open(1800,'visitor-chosen'));db.assert_not_called()
    def test_new_round_does_not_modify_legacy_or_other_round(self):
        reg=PostgresDemoRegistry.__new__(PostgresDemoRegistry);reg.config=config();reg.connector=None
        db=Mock();ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
        with patch('public_demo_postgres.registry_connection',return_value=ctx),patch.object(reg,'_limits',return_value=([10000],1000)),patch('public_demo_postgres.APPROVED_TEST_ROUNDS',frozenset({(APP,'performance-v1')})):
            reg.configure_test_window(1800,'performance-v1')
        sql=' '.join(c.args[0] for c in db.execute.call_args_list)
        self.assertNotIn('.test_window',sql);self.assertNotIn('DELETE',sql);self.assertNotIn('TRUNCATE',sql)
        self.assertIn('ON CONFLICT(round_id)',sql);self.assertEqual(db.execute.call_args_list[-1].args[1],('performance-v1',1000,1800))
    def test_explicit_configuration_required(self):
        with self.assertRaises(ValueError):PostgresDemoRegistry(None)

class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{},clear=True);self.env.start()
        self.binding=patch('public_demo_postgres.APPROVED_BINDINGS',frozenset({(APP,'isolated_qa','qa_user')}));self.binding.start()
    def tearDown(self):self.binding.stop();self.env.stop()
    def db(self,live=True):
        raw=Fake(identity=Record(db='isolated_qa',user='qa_user'),workspace_live=live)
        return ScopedConnection(config(),'a'*32,lambda **kw:raw),raw
    def test_reapply_scope_and_timeouts_after_commit_rollback(self):
        db,raw=self.db();db.execute('SELECT 1');db.execute('SELECT 2');db.commit();db.execute('SELECT 3');db.rollback();db.execute('SELECT 4')
        scopes=[x for x in raw.calls if 'search_path' in x[0]]
        self.assertEqual(len(scopes),3);self.assertTrue(all(x[1]==('apd_'+'a'*32+',pg_catalog,pg_temp',) for x in scopes))
        self.assertIn('lock_timeout',raw.calls[0][0]);self.assertIn('pg_advisory_xact_lock',raw.calls[1][0]);self.assertIn('current_database',raw.calls[2][0]);self.assertIn('clock_timestamp',raw.calls[3][0])
    def test_expiry_stops_sql(self):
        db,raw=self.db(False)
        with self.assertRaises(APIError):db.execute('UPDATE cases SET version=999')
        self.assertFalse(any('UPDATE cases' in x[0] for x in raw.calls))
    def test_schema_injection_before_connect(self):
        with self.assertRaises(ValueError):ScopedConnection(config(),'public;drop table cases',lambda **kw:self.fail('connected'))
    def test_offline_service_never_constructs_model(self):
        with patch('backend.ModelAdapter.__init__',side_effect=AssertionError('provider constructed')),patch.object(PGDemoService,'_initialize'):
            service=PGDemoService(config(),'a'*32)
            self.assertFalse(service.ai.status()['request_allowed'])
            with self.assertRaises(APIError):service.ai.generate({})


class CombinedWindowTests(unittest.TestCase):
    def test_expired_window_blocks_existing_new_and_static_before_mutations(self):
        reg=PostgresDemoRegistry.__new__(PostgresDemoRegistry);reg.config=Mock(app_id=APP);reg.connector=None;reg._get_service=Mock()
        db=Mock();db.execute.return_value.fetchone.return_value=(150,200)
        ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
        with patch('public_demo_postgres.registry_connection',return_value=ctx),patch.object(reg,'_limits',return_value=([1000,40,2000,7200,120,4],200)),patch('public_demo_postgres.APPROVED_TEST_ROUNDS',frozenset({(APP,'performance-v1')})):
            for cookie,initialize in [(None,True),('b'*43,True),('b'*43,False)]:
                with self.assertRaises(APIError) as error:reg.resolve(cookie,initialize_service=initialize,required_window=(300,'performance-v1'))
                self.assertEqual(error.exception.code,'DEMO_TEST_WINDOW_CLOSED')
            self.assertTrue(all(c.args[0].startswith('SELECT expires,') for c in db.execute.call_args_list));reg._get_service.assert_not_called()
    def test_open_window_uses_single_transaction_and_preserves_quota(self):
        reg=PostgresDemoRegistry.__new__(PostgresDemoRegistry);reg.config=Mock(app_id=APP);reg.connector=None;reg._get_service=Mock(return_value='cached')
        db=Mock();db.execute.return_value.fetchone.side_effect=[(300,200),('a'*32,100,300,1,2000)]
        ctx=Mock();ctx.__enter__=Mock(return_value=db);ctx.__exit__=Mock(return_value=False)
        with patch('public_demo_postgres.registry_connection',return_value=ctx) as connection,patch.object(reg,'_limits',return_value=([1000,40,2000,7200,120,4],200)),patch('public_demo_postgres.APPROVED_TEST_ROUNDS',frozenset({(APP,'performance-v1')})):
            self.assertEqual(reg.resolve('b'*43,required_window=(300,'performance-v1')),('cached',None))
            connection.assert_called_once();self.assertEqual(sum(c.args[0].startswith('UPDATE') for c in db.execute.call_args_list),1)
