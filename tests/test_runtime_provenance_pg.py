"""Actual fixed read-only PostgreSQL runtime metadata, only on the explicit private test fixture."""

import json
import os
import unittest
from unittest import mock

import pgfixture
from semantic_crosscheck import config, runtime_provenance as runtime


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class RuntimePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_env = {key: os.environ.get(key) for key in ('SXC_PG_DSN', 'SXC_PG_OWNER_DSN')}
        cls.fx = pgfixture.PgFixture('sxc_t_runtime_' + str(os.getpid()), owner='sxc_runtime_owner',
                                     check='sxc_runtime_check').setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_actual_server_version_settings_hash_stable_and_no_dsn_or_host(self):
        first = runtime._postgres('SXC_PG_DSN', 1000)
        second = runtime._postgres('SXC_PG_DSN', 1000)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first['server_version_num'], 170000)
        self.assertRegex(first['settings_sha256'], r'^[0-9a-f]{64}$')
        encoded = json.dumps(first)
        for raw in (os.environ['SXC_PG_DSN'], self.fx.name, self.fx.check, '127.0.0.1'):
            self.assertNotIn(raw, encoded)

    def test_fixed_metadata_query_is_supported_by_actual_postgres(self):
        conn = self.fx.check_conn()
        try:
            rows = conn.run(runtime.PG_SQL)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]), 1 + len(runtime.PG_SETTINGS) + len(runtime.PG_NULLABLE_METADATA))
        finally:
            conn.close()

    def test_actual_nullable_locale_and_icu_rules_are_in_fixed_probe(self):
        conn = self.fx.check_conn()
        try:
            expected = conn.run('SELECT datlocale, daticurules FROM pg_catalog.pg_database '
                                'WHERE datname = current_database()')[0]
            actual = conn.run(runtime.PG_SQL)[0]
            self.assertEqual(actual[-4:-2], expected)
        finally:
            conn.close()

    def test_actual_role_semantic_setting_changes_fingerprint(self):
        admin = pgfixture._admin(self.fx.name)
        try:
            admin.run('ALTER ROLE "sxc_runtime_check" IN DATABASE "' + self.fx.name + '" SET timezone TO \'UTC\'')
            baseline = runtime._postgres('SXC_PG_DSN', 1000)
            admin.run('ALTER ROLE "sxc_runtime_check" IN DATABASE "' + self.fx.name + '" SET timezone TO \'Asia/Seoul\'')
            changed = runtime._postgres('SXC_PG_DSN', 1000)
            self.assertNotEqual(baseline['settings_sha256'], changed['settings_sha256'])
            self.assertEqual(baseline['server_version_num'], changed['server_version_num'])
        finally:
            admin.run('ALTER ROLE "sxc_runtime_check" IN DATABASE "' + self.fx.name + '" RESET timezone')
            admin.close()

    def test_actual_postgres_probe_transaction_is_readonly(self):
        conn = self.fx.check_conn()
        original = conn.run
        observations = []
        def run(sql, **params):
            if sql == runtime.PG_SQL:
                observations.append(original("SHOW transaction_read_only")[0][0])
            return original(sql, **params)
        with mock.patch.object(conn, 'run', side_effect=run), mock.patch.object(runtime, '_connect', return_value=conn):
            runtime._postgres('SXC_PG_DSN', 1000)
        self.assertEqual(observations, ['on'])

    def test_enabled_full_pipeline_missing_java_cannot_claim_bound(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        cfg.source.runtime_evidence = True
        with mock.patch.object(runtime.owl, 'java_executable', return_value=None):
            with self.assertRaises(runtime.RuntimeEvidenceError) as raised:
                runtime.current_probe(cfg)
        self.assertEqual(raised.exception.reason, 'java_missing')


if __name__ == '__main__':
    unittest.main()
