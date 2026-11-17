"""Actual PostgreSQL plans, DST controls, transaction scope and temporary isolation."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import db
from test_plan_compare import document, pc


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class PlanComparePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_plan_compare_pg').setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_plan_compare_pg_')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def spec(self, value=None, example=None):
        path = self.directory / 'input.json'
        if example:
            path.write_bytes((pgfixture.ROOT / 'examples' / 'plan-compare' / example).read_bytes())
        else:
            path.write_text(json.dumps(value or document()), encoding='utf-8')
        return path

    def run_spec(self, value=None, example=None, **extra):
        source = self.spec(value, example)
        original = source.read_bytes()
        args = SimpleNamespace(spec=str(source), dsn_env='SXC_PG_DSN', statement_timeout_ms=1000, quiet=True)
        for name, value in extra.items():
            setattr(args, name, value)
        result = pc.run(None, args, self.directory)
        self.assertEqual(source.read_bytes(), original)
        return result

    def assert_diverse(self, result):
        detail = result.checks[0].detail
        self.assertNotEqual(detail['sequential_plan_sha256'], detail['index_plan_sha256'])
        return detail

    def test_positive_integer_example_uses_real_sequential_and_index_plans(self):
        result = self.run_spec(example='integer-index.json')
        self.assertEqual(result.status, 'pass')
        detail = self.assert_diverse(result)
        self.assertEqual((detail['sequential_rows'], detail['index_rows']), (5, 5))

    def test_actual_explain_has_scan_diversity_and_comparison_is_readonly_repeatable_read(self):
        spec = pc.load_spec(self.spec())
        conn = self.fx.check_conn()
        baseline = conn.run("SELECT current_setting('TimeZone'), current_setting('enable_seqscan'), current_setting('enable_indexscan')")
        observed = []
        original = pc._explain
        def explain(connection, query, bindings):
            settings = connection.run("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")
            self.assertEqual(settings, [['on', 'repeatable read']])
            plan = original(connection, query, bindings)
            observed.append(set(pc._node_types(plan)))
            return plan
        try:
            with patch.object(pc, '_explain', side_effect=explain):
                status, detail = pc.evaluate(conn, spec, 1000)
            self.assertEqual(status, 'pass')
            self.assertIn('Seq Scan', observed[0])
            self.assertTrue({'Index Scan', 'Index Only Scan', 'Bitmap Index Scan'} & observed[1])
            self.assertEqual(conn.run("SELECT current_setting('TimeZone'), current_setting('enable_seqscan'), current_setting('enable_indexscan')"), baseline)
            self.assertEqual(conn.run("SELECT current_setting('transaction_read_only')"), [['off']])
            self.assertEqual(conn.run('SELECT count(*) FROM pg_temp.sxc_plan_compare_data'), [[20]])
        finally:
            conn.close()
        conn = self.fx.check_conn()
        try:
            self.assertEqual(conn.run("SELECT to_regclass('pg_temp.sxc_plan_compare_data')"), [[None]])
        finally:
            conn.close()

    def test_mixed_dst_reproduces_historic_36_vs_30_only_when_server_remains_affected(self):
        result = self.run_spec(example='dst-mixed.json')
        detail = self.assert_diverse(result)
        counts = (detail['sequential_rows'], detail['index_rows'])
        if result.status == 'fail':
            self.assertEqual(counts, (36, 30))
            self.assertFalse(detail['multiset_equal'])
        else:
            self.assertEqual(result.status, 'pass', 'Errors or unobserved plans are not a fixed-server result')
            self.assertEqual(counts[0], counts[1])
            self.assertTrue(detail['multiset_equal'])

    def test_utc_mixed_and_same_type_timestamp_controls_are_stable(self):
        for example, expected in [('dst-utc.json', 73), ('dst-same-type.json', 30)]:
            with self.subTest(example=example):
                result = self.run_spec(example=example)
                self.assertEqual(result.status, 'pass')
                detail = self.assert_diverse(result)
                self.assertEqual((detail['sequential_rows'], detail['index_rows']), (expected, expected))

    def test_integer64_exact_numeric_null_duplicate_text_and_boolean_bags(self):
        value = document(columns={'x': 'integer', 'n': 'numeric', 't': 'text', 'b': 'boolean'},
                         rows=[[2**62, '1.000000000000000000001', 'PRIVATE_VALUE "); DROP TABLE nope; --', True]] * 2 +
                              [[None, None, None, None]],
                         projection=['x', 'n', 't', 'b'],
                         predicate=[{'column': 'x', 'op': 'ge', 'value': {'type': 'integer', 'value': 2**62}}])
        result = self.run_spec(value)
        self.assertEqual(result.status, 'pass')
        detail = self.assert_diverse(result)
        self.assertEqual((detail['sequential_rows'], detail['index_rows']), (2, 2))
        for forbidden in ('PRIVATE_VALUE', 'DROP TABLE', 'SELECT ', 'postgresql://', str(self.directory)):
            self.assertNotIn(forbidden, json.dumps(result.to_dict()))

    def test_cap_plus_one_empty_result_and_invalid_server_timezone_do_not_pass(self):
        cases = [(document(max_rows=2), 'not_run', 'row_cap_exceeded'),
                 (document(predicate=[{'column': 'x', 'op': 'lt', 'value': {'type': 'integer', 'value': -1}}]), 'not_run', 'empty_answer'),
                 (document(timezone='SXC_Invalid_Timezone'), 'error', 'execution_error')]
        for value, status, reason in cases:
            with self.subTest(reason=reason):
                result = self.run_spec(value)
                self.assertEqual(result.status, status)
                self.assertEqual(result.checks[0].detail['reason'], reason)

    def test_setup_leaves_all_existing_user_relations_unchanged(self):
        conn = self.fx.check_conn()
        try:
            before = conn.run('SELECT count(*), sum(qty) FROM sxc_demo.stock')
            objects = conn.run("SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' ORDER BY 1,2")
        finally:
            conn.close()
        self.assertEqual(self.run_spec().status, 'pass')
        conn = self.fx.check_conn()
        try:
            self.assertEqual(conn.run('SELECT count(*), sum(qty) FROM sxc_demo.stock'), before)
            self.assertEqual(conn.run("SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' ORDER BY 1,2"), objects)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
