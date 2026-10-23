"""Actual PostgreSQL TLP, including intentionally broken NULL/bag partitions."""
import json
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import db
from test_tlp import document, tlp


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class TlpPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_tlp_pg').setup()
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run("CREATE FUNCTION sxc_demo.tlp_volatile_value() RETURNS numeric LANGUAGE sql VOLATILE AS $$ SELECT EXTRACT(epoch FROM clock_timestamp())::numeric $$")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.assertIsNotNone(tlp, 'The bounded TLP engine must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_tlp_pg_')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def spec(self, **changes):
        path = self.directory / 'spec.json'
        path.write_text(json.dumps(document(**changes)), encoding='utf-8')
        return tlp.load_spec(path)

    def evaluate(self, spec, **changes):
        conn = self.fx.check_conn()
        try:
            fixture = tlp.ce.generate(spec['scenario'], 7, 0)
            return tlp.evaluate(conn, fixture, spec, 1000, **changes)
        finally:
            conn.close()

    def args(self, **changes):
        value = dict(spec=str(self.directory / 'spec.json'), seed=7, max_examples=3,
                     dsn_env='SXC_PG_DSN', statement_timeout_ms=1000, replay=None)
        value.update(changes)
        return SimpleNamespace(**value)

    def test_actual_three_valued_partition_count_star_and_nullable_count(self):
        for column, expected in [('*', 3), ('qty', 2)]:
            spec = self.spec(aggregate={'function': 'count', 'column': column})
            answer = self.evaluate(spec)
            self.assertTrue(answer['equal'])
            self.assertEqual(answer['baseline_aggregate'], expected)

    def test_actual_sum_empty_all_null_and_exact_decimal(self):
        cases = [('SELECT qty FROM stock WHERE FALSE', None),
                 ('SELECT qty FROM stock WHERE qty IS NULL', None),
                 ('SELECT 0.123456789012345678901::numeric AS qty UNION ALL SELECT 0.000000000000000000001::numeric',
                  Decimal('0.123456789012345678902'))]
        for relation, expected in cases:
            with self.subTest(relation=relation):
                answer = self.evaluate(self.spec(relation_sql=relation))
                self.assertTrue(answer['equal'])
                self.assertEqual(answer['baseline_aggregate'], expected)

    def test_wrong_null_omission_and_union_distinct_are_real_detected_disagreements(self):
        spec = self.spec()
        # Replace only the transform, then run actual SQL + real checker.
        with patch.object(tlp, '_partition_sql', return_value='SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p UNION ALL SELECT * FROM sxc_tlp_evaluated WHERE NOT __sxc_tlp_p'):
            answer = self.evaluate(spec)
            self.assertFalse(answer['equal'])
            self.assertFalse(answer['multiset_equal'])
            self.assertTrue(answer['aggregate_equal'], 'SUM alone cannot detect a lost NULL row')
        duplicate = self.spec(relation_sql='SELECT 0::numeric AS qty FROM stock UNION ALL SELECT 0::numeric FROM stock')
        with patch.object(tlp, '_partition_sql', return_value='SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p UNION SELECT * FROM sxc_tlp_evaluated WHERE NOT __sxc_tlp_p UNION SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p IS NULL'):
            answer = self.evaluate(duplicate)
            self.assertFalse(answer['equal'])
            self.assertFalse(answer['multiset_equal'])
            self.assertTrue(answer['aggregate_equal'], 'SUM zero cannot detect deduplicated zero rows')

    def test_unstable_relation_or_predicate_is_error(self):
        for changes in ({'relation_sql': 'SELECT random()::numeric AS qty'},
                        {'predicate_sql': 'random()::numeric > qty / 10'}):
            with self.subTest(changes=changes):
                self.spec(**changes)
                result = tlp.run(None, self.args(max_examples=1), self.directory)
                self.assertEqual(result.status, 'error')

    def test_valid_run_replays_and_spec_or_fixture_tampering_is_rejected(self):
        self.spec()
        result = tlp.run(None, self.args(), self.directory)
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.checks[0].detail['examples'], 3)
        replay = self.directory / 'tlp' / 'fixture.json'
        self.assertTrue(replay.is_file())
        self.assertEqual(tlp.run(None, self.args(replay=str(replay)), self.directory / 'replay').status, 'pass')
        self.assertEqual(tlp.run(None, self.args(replay=str(replay)), self.directory).status, 'pass')
        encoded = json.dumps(result.to_dict())
        for forbidden in ('SELECT ', 'qty >', 'postgresql://', str(self.directory), '"tables":'):
            self.assertNotIn(forbidden, encoded)
        source = Path(self.args().spec)
        source.write_text(source.read_text() + '\n', encoding='utf-8')
        with self.assertRaises(tlp.StageError):
            tlp.run(None, self.args(replay=str(replay)), self.directory / 'changed')

    def test_query_error_and_timeout_are_sanitized_and_never_pass(self):
        for relation, code in [('SELECT 1 / 0 AS qty', '22012'),
                               ('SELECT 1::numeric AS qty FROM pg_sleep(2)', '57014')]:
            with self.subTest(code=code):
                self.spec(relation_sql=relation)
                result = tlp.run(None, self.args(max_examples=1), self.directory)
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.checks[0].detail['sqlstate'], code)
                self.assertFalse((self.directory / 'tlp' / 'fixture.json').exists())

    def test_row_bound_float_sum_and_duplicate_projection_are_not_partial_passes(self):
        for relation in ('SELECT i::numeric AS qty FROM generate_series(1,10001) AS i',
                         'SELECT 1.25::double precision AS qty',
                         'SELECT 1 AS qty, 2 AS qty',
                         'SELECT 1 AS qty, TRUE AS __sxc_tlp_p'):
            with self.subTest(relation=relation):
                self.spec(relation_sql=relation)
                result = tlp.run(None, self.args(max_examples=1), self.directory)
                self.assertEqual(result.status, 'error')
                self.assertFalse((self.directory / 'tlp' / 'fixture.json').exists())

    def test_execution_is_readonly_and_repeatable_read(self):
        relation = ("SELECT 1::numeric AS qty WHERE current_setting('transaction_read_only') = 'on' "
                    "AND current_setting('transaction_isolation') = 'repeatable read'")
        answer = self.evaluate(self.spec(relation_sql=relation))
        self.assertTrue(answer['equal'])
        self.assertEqual(answer['baseline_rows'], 1)

    def test_materialized_comparison_freezes_custom_volatile_relation_but_recheck_rejects_changes(self):
        spec = self.spec(relation_sql='SELECT sxc_demo.tlp_volatile_value() AS qty')
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn, 1000, isolation='repeatable read'):
                frozen = tlp._fetch(conn, tlp.ce.generate('nulls', 7, 0), spec)
                self.assertTrue(tlp.compare(frozen)['equal'])
        finally:
            conn.close()
        result = tlp.run(None, self.args(max_examples=1), self.directory)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.directory / 'tlp' / 'fixture.json').exists())

    def test_spec_changed_during_actual_execution_cannot_retain_a_pass(self):
        self.spec()
        original = tlp.evaluate
        def execute_and_change(*args):
            answer = original(*args)
            path = self.directory / 'spec.json'
            path.write_text(path.read_text() + '\n', encoding='utf-8')
            return answer
        with patch.object(tlp, 'evaluate', side_effect=execute_and_change):
            result = tlp.run(None, self.args(max_examples=1), self.directory)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.directory / 'tlp' / 'fixture.json').exists())

    def test_replay_changed_during_actual_execution_cannot_retain_a_pass(self):
        self.spec()
        self.assertEqual(tlp.run(None, self.args(max_examples=1), self.directory).status, 'pass')
        replay = self.directory / 'tlp' / 'fixture.json'
        original = tlp.evaluate
        def execute_and_change(*args):
            answer = original(*args)
            replay.write_text(replay.read_text() + '\n', encoding='utf-8')
            return answer
        with patch.object(tlp, 'evaluate', side_effect=execute_and_change):
            result = tlp.run(None, self.args(replay=str(replay)), self.directory / 'replayed')
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.directory / 'replayed' / 'tlp' / 'fixture.json').exists())


if __name__ == '__main__':
    unittest.main()
