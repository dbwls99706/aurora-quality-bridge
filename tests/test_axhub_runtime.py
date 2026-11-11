"""Offline cloud boundary tests; optional integration uses a disposable Postgres DB."""
import http.client
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import AuroraService, make_server
from cloud_runtime import CloudPolicy
from persistence import PostgresConnection, Record, postgres_sql, row_factory

CLOUD = {'AURORA_DEPLOYMENT': 'axhub', 'AURORA_PUBLIC_ORIGIN': 'aurora.lounge.axhub.ai',
         'AURORA_AXHUB_TENANT': 'lounge', 'DATABASE_URL': 'postgresql://unused',
         'AURORA_AI_ALLOW_NETWORK': '0'}
CLOUD['AURORA_PUBLIC_ORIGIN'] = 'https://' + CLOUD['AURORA_PUBLIC_ORIGIN']
IDENTITY = {'X-AxHub-User-ID': '8ad7c774-71fb-48c1-9dc0-7cd4047b702d',
            'X-AxHub-App-Role': 'owner', 'X-AxHub-Surface': 'tenant', 'X-AxHub-Tenant-Slug': 'lounge'}


class CloudHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {}, clear=True):
            self.service = AuroraService(db_path=self.tmp.name + '/test.sqlite3')
        with patch.dict(os.environ, CLOUD, clear=True):
            self.server = make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path='/', headers=None, method='GET', body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_health_has_no_identity_host_or_database_requirement(self):
        with patch.object(self.service, 'connect', side_effect=AssertionError('DB called')):
            status, body = self.request('/healthz', {'Host': '10.0.0.17:8000'})
            self.assertEqual((status, body), (200, b'{"ok":true}'))
        self.assertNotEqual(self.request('/healthz/')[0], 200)

    def test_private_routes_require_gateway_identity(self):
        for path in ('/', '/workspace', '/api/health', '/api/state', '/app.js', '/api/audit/verify'):
            self.assertEqual(self.request(path, {'Host': 'aurora.lounge.axhub.ai'})[0], 401)

    def test_authenticated_html_cannot_be_framed(self):
        for path in ('/', '/workspace', '/index.html', '/landing.html'):
            with self.subTest(path=path):
                conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
                try:
                    conn.request('GET', path, headers=dict(IDENTITY, Host='aurora.lounge.axhub.ai'))
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader('X-Frame-Options'), 'DENY')
                    self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
                    self.assertEqual(response.getheader('Referrer-Policy'), 'same-origin')
                    response.read()
                finally:
                    conn.close()

    def test_valid_identity_and_role_simulation(self):
        headers = dict(IDENTITY, Host='aurora.lounge.axhub.ai', Origin=CLOUD['AURORA_PUBLIC_ORIGIN'])
        self.assertEqual(self.request('/api/state', headers)[0], 200)
        headers['Content-Type'] = 'application/json'
        status, body = self.request('/api/session', headers, 'POST', '{"actor_id":"committee"}')
        self.assertEqual(status, 200)
        self.assertIn(b'local_demo_identity', body)

    def test_unknown_guest_wrong_tenant_public_rejected(self):
        for key, value in [('X-AxHub-App-Role', 'guest'), ('X-AxHub-App-Role', 'app_member'),
                           ('X-AxHub-App-Role', 'super_admin'), ('X-AxHub-Tenant-Slug', 'other'),
                           ('X-AxHub-Surface', 'public'), ('X-AxHub-User-ID', 'fake')]:
            headers = dict(IDENTITY, Host='aurora.lounge.axhub.ai')
            headers[key] = value
            self.assertEqual(self.request('/api/state', headers)[0], 401)

    def test_hosts_origins_and_forwarded_headers(self):
        headers = dict(IDENTITY, Host='evil.test', **{'X-Forwarded-Host': 'aurora.lounge.axhub.ai'})
        self.assertEqual(self.request('/api/state', headers)[0], 403)
        for origin in ('https://evil.test', 'http://aurora.lounge.axhub.ai', 'null'):
            headers = dict(IDENTITY, Host='aurora.lounge.axhub.ai', Origin=origin)
            self.assertEqual(self.request('/api/state', headers)[0], 403)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.putrequest('GET', '/api/state', skip_host=True)
        for k, v in dict(IDENTITY, Host='aurora.lounge.axhub.ai').items():
            conn.putheader(k, v)
        conn.putheader('X-AxHub-User-ID', IDENTITY['X-AxHub-User-ID'])
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()


class ConfigurationTests(unittest.TestCase):
    def test_fail_closed(self):
        for key, value in [('DATABASE_URL', ''), ('AURORA_PUBLIC_ORIGIN', ''),
                           ('AURORA_PUBLIC_ORIGIN', 'https://user:pass@site.test'),
                           ('AURORA_PUBLIC_ORIGIN', 'https://site.test/'),
                           ('AURORA_AXHUB_TENANT', ''), ('AURORA_AI_ALLOW_NETWORK', '1'),
                           ('AURORA_DEPLOYMENT', 'public')]:
            with patch.dict(os.environ, dict(CLOUD, **{key: value}), clear=True):
                with self.assertRaises(ValueError):
                    CloudPolicy()

    def test_local_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(CloudPolicy().enabled)


class PostgresAdapterTests(unittest.TestCase):
    def test_dialect_migrations_and_upserts(self):
        self.assertIn('BIGSERIAL', postgres_sql('id INTEGER PRIMARY KEY AUTOINCREMENT'))
        self.assertIn('information_schema.columns', postgres_sql('PRAGMA table_info(cases)'))
        self.assertIn('ON CONFLICT (key) DO NOTHING', postgres_sql('INSERT OR IGNORE INTO metadata VALUES (?,?)'))
        self.assertIn('ON CONFLICT (case_id) DO UPDATE', postgres_sql('INSERT OR REPLACE INTO customer_commitments VALUES (?,?,?,?,?)'))
        self.assertEqual(postgres_sql("SELECT '?' AS literal, ? AS parameter, 'it''s?'"), "SELECT '?' AS literal, %s AS parameter, 'it''s?'")
        self.assertIsNone(postgres_sql('BEGIN IMMEDIATE'))
        with self.assertRaises(ValueError):
            postgres_sql('PRAGMA table_info(arbitrary)')

    def test_lock_reacquired_and_closed_after_rollback(self):
        connector = Mock()
        db = PostgresConnection('postgresql://unused', connector=connector)
        raw = connector.return_value
        db.execute('BEGIN IMMEDIATE')
        db.execute('SELECT ?', (3,))
        db.rollback()
        db.execute('INSERT INTO audit VALUES (?)', ('x',))
        db.commit()
        db.close()
        locks = [call for call in raw.execute.call_args_list if 'pg_advisory_xact_lock' in call.args[0]]
        self.assertEqual(len(locks), 2)
        raw.close.assert_called_once()
        self.assertIsNone(connector.call_args.kwargs['prepare_threshold'])

    def test_ddl_has_no_result_description(self):
        self.assertEqual(row_factory(Mock(description=None))(()), {})

    def test_row_compatible_with_sqlite(self):
        row = Record(a=3, b=5)
        self.assertEqual(row[0], 3)
        self.assertEqual(row['b'], 5)
        self.assertEqual(dict(row), {'a': 3, 'b': 5})


@unittest.skipUnless(os.environ.get('AURORA_TEST_POSTGRES_URL'), 'Needs explicitly disposable PostgreSQL test DB')
class RealPostgresTests(unittest.TestCase):
    def test_concurrent_instances_preserve_versions_and_audit(self):
        from concurrent.futures import ThreadPoolExecutor
        import uuid
        from backend import APIError
        with patch.dict(os.environ, {'DATABASE_URL': os.environ['AURORA_TEST_POSTGRES_URL'],
                                    'DIRECT_DATABASE_URL': os.environ['AURORA_TEST_POSTGRES_URL']}):
            first, second = AuroraService(), AuroraService()
            version = first.detail('CASE-001')['version']
            payload = {'expected_version': version, 'changes': {'production_delay_days': 3},
                       'reason': 'Concurrent PostgreSQL instances'}
            def mutate(service):
                try:
                    service.mutate('/api/cases/CASE-001/conditions', payload, 'quality', str(uuid.uuid4()))
                    return 'ok'
                except APIError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(mutate, (first, second)))
            self.assertCountEqual(results, ['ok', 'STALE_VERSION'])
            self.assertEqual(first.detail('CASE-001')['version'], version + 1)
            self.assertTrue(second.verify_audit()['valid'])

    def test_restart_migration_mutation_idempotency_audit(self):
        with patch.dict(os.environ, {'DATABASE_URL': os.environ['AURORA_TEST_POSTGRES_URL'],
                                    'DIRECT_DATABASE_URL': os.environ['AURORA_TEST_POSTGRES_URL']}):
            first = AuroraService()
            case = first.detail('CASE-001')
            payload = {'expected_version': case['version'], 'changes': {'production_delay_days': 2}, 'reason': 'Postgres persistence test'}
            import uuid
            key = 'pg-test-' + str(uuid.uuid4())
            result = first.mutate('/api/cases/CASE-001/conditions', payload, 'quality', key)
            self.assertEqual(first.mutate('/api/cases/CASE-001/conditions', payload, 'quality', key), result)
            # Sessions exercise writes independently of case workflow.
            token = first.session('quality')['token']
            second = AuroraService()
            self.assertEqual(second.authenticate(token), 'quality')
            self.assertEqual(second.detail('CASE-001')['version'], case['version'] + 1)
            self.assertEqual(second.detail('CASE-001')['conditions']['production_delay_days'], 2)
            self.assertEqual(second.mutate('/api/cases/CASE-001/conditions', payload, 'quality', key), result)
            self.assertTrue(second.verify_audit()['valid'])
            with second.connect() as db:
                columns = {r['name'] for r in db.execute('PRAGMA table_info(cases)')}
                self.assertTrue({'quality_revision', 'supply_revision'}.issubset(columns))

if __name__ == '__main__':
    unittest.main()
