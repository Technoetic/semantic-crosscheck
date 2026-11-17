"""Real PostgreSQL bounded exports; only the external Cube runtime is doubled."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import config, db
from test_measure_oracle import CONFIG, document, install_cube, oracle


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class MeasureOraclePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_measure_oracle_pg').setup()
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run('''CREATE TABLE sxc_demo.oracle_orders (
                    id integer PRIMARY KEY, qty numeric, status text, flag boolean,
                    float_qty double precision, stamp date)''')
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.assertIsNotNone(oracle, 'The independent measure oracle must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_measure_oracle_pg_')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.config_path = self.directory / 'crosscheck.toml'
        self.config_path.write_text(CONFIG, encoding='utf-8')
        self.cfg = config.load(self.config_path)
        install_cube(self.cfg, self.directory)
        self.execute('''TRUNCATE sxc_demo.oracle_orders''')

    def execute(self, sql):
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run(sql)
        finally:
            conn.close()

    def args(self, **changes):
        path = self.directory / 'spec.json'
        path.write_text(json.dumps(document(**changes)), encoding='utf-8')
        return SimpleNamespace(spec=str(path), quiet=True)

    def run_cube(self, args, rows, during_load=None):
        @contextmanager
        def serve(cfg, run_dir):
            class Client:
                def load(inner, query):
                    if during_load:
                        during_load()
                    return rows
            yield Client()
        with patch.object(oracle.cube_run, 'serve', side_effect=serve):
            return oracle.run(self.cfg, args, self.directory)

    def test_actual_raw_predicate_detects_wrong_shared_generator_filter(self):
        self.execute("INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES (1,13,'FIRM'),(2,71,'DRAFT')")
        result = self.run_cube(self.args(), [{'m_orders.value': '71'}])
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.checks[0].detail['reason'], 'value_mismatch')
        self.assertEqual(result.checks[0].detail['source_rows'], 2)
        self.assertEqual(result.checks[0].detail['qualifying_rows'], 1)
        self.assertEqual(result.checks[0].detail['non_null_values'], 1)

    def test_actual_null_count_and_fractional_sum_stay_exact(self):
        self.execute('''INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES
            (1,123456789012345678901234567890.1,'FIRM'),
            (2,0.000000000000000000000000000009,'FIRM'),(3,NULL,'FIRM')''')
        result = self.run_cube(self.args(),
                               [{'m_orders.value': '123456789012345678901234567890.100000000000000000000000000009'}])
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.checks[0].detail['non_null_values'], 2)
        result = self.run_cube(self.args(aggregation='count'), [{'m_orders.value': '2'}])
        self.assertEqual(result.status, 'pass')

    def test_actual_empty_sources_and_all_null_are_disclosed(self):
        for aggregation, value in [('sum', None), ('count', '0')]:
            result = self.run_cube(self.args(aggregation=aggregation), [{'m_orders.value': value}])
            self.assertEqual(result.status, 'pass')
            self.assertEqual(result.checks[0].detail['source_rows'], 0)
            self.assertTrue(any('empty' in note for note in result.not_proven))
        self.execute("INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES (1,NULL,'FIRM')")
        result = self.run_cube(self.args(), [{'m_orders.value': None}])
        self.assertEqual(result.status, 'pass')
        self.assertTrue(any('NULL' in note for note in result.not_proven))
        result = self.run_cube(self.args(where=[{'column': 'status', 'operator': 'eq',
                                                'type': 'text', 'value': 'ABSENT'}]),
                               [{'m_orders.value': None}])
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.checks[0].detail['qualifying_rows'], 0)
        self.assertTrue(any('No raw rows qualified' in note for note in result.not_proven))

    def test_actual_row_limit_and_unsupported_catalog_are_not_run(self):
        self.execute("INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES (1,2,'FIRM'),(2,3,'FIRM')")
        result = self.run_cube(self.args(max_rows=1), [{'m_orders.value': '2'}])
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'row_limit')
        for column in ('float_qty', 'stamp'):
            result = self.run_cube(self.args(column=column), [{'m_orders.value': None}])
            self.assertEqual(result.status, 'not_run')
            self.assertEqual(result.checks[0].detail['reason'], 'unsupported_type')

    def test_actual_nondeterministic_icu_text_collation_is_not_run(self):
        try:
            self.execute("""CREATE COLLATION sxc_demo.oracle_ci
                (provider = icu, locale = 'und-u-ks-level2', deterministic = false)""")
        except Exception as exc:
            details = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
            if details.get('C') == '0A000':
                self.skipTest('PostgreSQL was built without ICU collation support')
            raise
        self.execute('''CREATE TABLE sxc_demo.oracle_collated_orders (
            id integer PRIMARY KEY, qty numeric,
            status text COLLATE sxc_demo.oracle_ci)''')
        self.execute("INSERT INTO sxc_demo.oracle_collated_orders VALUES (1,13,'firm')")
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn):
                self.assertIs(db.scalar(conn, "SELECT 'firm'::text COLLATE sxc_demo.oracle_ci = 'FIRM'::text"), True)
        finally:
            conn.close()
        self.config_path.write_text(CONFIG.replace('sxc_demo.oracle_orders', 'sxc_demo.oracle_collated_orders'),
                                    encoding='utf-8')
        self.cfg = config.load(self.config_path)
        install_cube(self.cfg, self.directory)
        result = self.run_cube(self.args(), [{'m_orders.value': '13'}])
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'unsafe_collation')

    def test_actual_source_is_readonly_repeatable_read_and_rolls_back_before_cube(self):
        self.execute('''CREATE OR REPLACE VIEW sxc_demo.oracle_snapshot AS
            SELECT 1 AS id, 1::numeric AS qty, 'FIRM'::text AS status
            WHERE current_setting('transaction_read_only')='on'
              AND current_setting('transaction_isolation')='repeatable read' ''')
        self.config_path.write_text(CONFIG.replace('sxc_demo.oracle_orders', 'sxc_demo.oracle_snapshot'), encoding='utf-8')
        self.cfg = config.load(self.config_path)
        install_cube(self.cfg, self.directory)
        result = self.run_cube(self.args(), [{'m_orders.value': '1'}])
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.checks[0].detail['source_rows'], 1)
        # An exclusive lock acquired during Cube proves the source transaction ended.
        self.config_path.write_text(CONFIG, encoding='utf-8')
        self.cfg = config.load(self.config_path)
        install_cube(self.cfg, self.directory)
        self.execute("INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES (1,2,'FIRM')")
        def exclusive_lock():
            conn = db.connect('SXC_PG_OWNER_DSN')
            try:
                with db.write_tx(conn, 1000):
                    conn.run('LOCK sxc_demo.oracle_orders IN ACCESS EXCLUSIVE MODE NOWAIT')
            finally:
                conn.close()
        self.assertEqual(self.run_cube(self.args(), [{'m_orders.value': '2'}], exclusive_lock).status, 'pass')

    def test_actual_late_spec_configuration_model_and_receipt_tampering_cannot_pass(self):
        self.execute("INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES (1,2,'FIRM')")
        targets = [self.directory / 'spec.json', self.config_path,
                   self.directory / 'cube' / 'model' / 'm_orders.yml',
                   self.directory / 'cube' / 'model.json', self.directory / 'cube' / 'result.json']
        for target in targets:
            with self.subTest(target=target.name):
                self.config_path.write_text(CONFIG, encoding='utf-8')
                self.cfg = config.load(self.config_path)
                install_cube(self.cfg, self.directory)
                args = self.args()
                def change():
                    target.write_bytes(target.read_bytes() + b'\n')
                result = self.run_cube(args, [{'m_orders.value': '2'}], change)
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.checks[0].detail['reason'], 'inputs_changed')

    def test_actual_source_and_cube_failures_and_malformed_responses_do_not_leak(self):
        self.execute("INSERT INTO sxc_demo.oracle_orders(id,qty,status) VALUES (1,2,'FIRM')")
        for rows in (None, [], [{'different': 'PRIVATE-ANSWER'}], [{'m_orders.value': True}],
                     [{'m_orders.value': '2'}, {'m_orders.value': '2'}]):
            with self.subTest(rows=rows):
                result = self.run_cube(self.args(), rows)
                self.assertEqual(result.status, 'error')
                self.assertNotIn('PRIVATE-ANSWER', json.dumps(result.to_dict()))
        def fail():
            raise RuntimeError('PRIVATE-FILTER postgresql://PRIVATE-CREDENTIALS SELECT PRIVATE-ROW')
        result = self.run_cube(self.args(), [{'m_orders.value': '2'}], fail)
        self.assertEqual(result.status, 'error')
        encoded = json.dumps(result.to_dict())
        for forbidden in ('PRIVATE', 'postgresql://', 'SELECT ', str(self.directory), 'DRAFT', 'FIRM'):
            self.assertNotIn(forbidden, encoded)
        self.execute('DROP TABLE sxc_demo.oracle_orders')
        try:
            result = self.run_cube(self.args(), [{'m_orders.value': '2'}])
            self.assertEqual(result.status, 'error')
        finally:
            self.execute('''CREATE TABLE sxc_demo.oracle_orders (
                id integer PRIMARY KEY, qty numeric, status text, flag boolean,
                float_qty double precision, stamp date)''')


if __name__ == '__main__':
    unittest.main()
