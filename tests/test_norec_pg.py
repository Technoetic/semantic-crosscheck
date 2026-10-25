"""Actual PostgreSQL bounded NoREC, including deliberately wrong CASE counts."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import db, tlp


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class NoRecPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_norec_pg').setup()
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run('CREATE FUNCTION sxc_demo.norec_volatile_value() RETURNS numeric LANGUAGE sql VOLATILE AS $$ SELECT EXTRACT(epoch FROM clock_timestamp())::numeric $$')
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.path = self.out / 'spec.json'

    def spec(self, **changes):
        value = dict(schema_version=1, scenario='nulls',
                     relation_sql='SELECT * FROM (VALUES (1::numeric), (0), (-1), (NULL), (1)) v(qty)',
                     predicate_sql='qty > 0', aggregate={'function': 'count', 'column': '*'})
        value.update(changes)
        self.path.write_text(json.dumps(value), encoding='utf-8')
        return tlp.load_spec(self.path)

    def args(self, **changes):
        value = dict(spec=str(self.path), oracle='norec', seed=7, max_examples=2,
                     statement_timeout_ms=1000, dsn_env='SXC_PG_DSN', replay=None)
        return SimpleNamespace(**dict(value, **changes))

    def evaluate(self, spec):
        conn = self.fx.check_conn()
        try:
            return tlp.evaluate(conn, tlp.ce.generate('nulls', 7, 0), spec, 1000, oracle='norec')
        finally:
            conn.close()

    def test_true_false_null_and_duplicate_rows(self):
        answer = self.evaluate(self.spec())
        self.assertTrue(answer['equal'])
        self.assertEqual((answer['baseline_rows'], answer['optimized_count'], answer['reference_count']), (5, 2, 2))

    def test_empty_all_null_and_all_false_relations_count_zero(self):
        for relation in ('SELECT 1 AS qty WHERE FALSE', 'SELECT NULL::numeric AS qty', 'SELECT 0 AS qty'):
            with self.subTest(relation=relation):
                answer = self.evaluate(self.spec(relation_sql=relation))
                self.assertTrue(answer['equal'])
                self.assertEqual((answer['optimized_count'], answer['reference_count']), (0, 0))

    def test_broken_case_transform_is_detected_and_failure_replays(self):
        spec = self.spec()
        with patch.object(tlp, '_norec_reference_sql', return_value='COUNT(*)'):
            self.assertFalse(self.evaluate(spec)['equal'])
            result = tlp.run(None, self.args(max_examples=1), self.out)
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.checks[0].id, 'norec:consistency')
        fixture = self.out / 'tlp' / 'fixture.json'
        self.assertEqual(json.loads(fixture.read_text())['oracle'], 'norec')
        with patch.object(tlp, '_norec_reference_sql', return_value='COUNT(*)'):
            replayed = tlp.run(None, self.args(replay=str(fixture)), self.out / 'replay')
        self.assertEqual(replayed.status, 'fail')
        encoded = json.dumps(result.to_dict())
        for private in ('SELECT ', 'qty >', str(self.out), 'postgresql://', '"tables":'):
            self.assertNotIn(private, encoded)

    def test_read_only_repeatable_read_and_normal_in_place_replay(self):
        self.spec(relation_sql="SELECT 1 AS qty WHERE current_setting('transaction_read_only') = 'on' AND current_setting('transaction_isolation') = 'repeatable read'")
        result = tlp.run(None, self.args(), self.out)
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.checks[0].detail['optimized_count'], 1)
        fixture = self.out / 'tlp' / 'fixture.json'
        self.assertEqual(tlp.run(None, self.args(replay=str(fixture)), self.out).status, 'pass')
        with self.assertRaises(tlp.StageError):
            tlp.run(None, self.args(replay=str(fixture), oracle='tlp'), self.out / 'wrong-oracle')

    def test_timeout_error_reserved_alias_and_bounds_never_pass(self):
        for relation, code in (('SELECT 1 / 0 AS qty', '22012'),
                               ('SELECT 1 AS qty FROM pg_sleep(2)', '57014'),
                               ('SELECT i AS qty FROM generate_series(1,10001) AS i', ''),
                               ('SELECT 1 AS qty, TRUE AS __sxc_tlp_p', ''),
                               ('SELECT 1 AS qty, 2 AS qty', '')):
            with self.subTest(sqlstate=code):
                self.spec(relation_sql=relation)
                result = tlp.run(None, self.args(max_examples=1), self.out)
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.checks[0].detail['sqlstate'], code)
                self.assertFalse((self.out / 'tlp' / 'fixture.json').exists())

    def test_known_and_custom_volatile_inputs_are_errors(self):
        for relation in ('SELECT random()::numeric AS qty', 'SELECT sxc_demo.norec_volatile_value() AS qty'):
            self.spec(relation_sql=relation)
            result = tlp.run(None, self.args(max_examples=1), self.out)
            self.assertEqual(result.status, 'error')

    def test_changed_spec_during_execution_removes_replay(self):
        self.spec()
        original = tlp.evaluate
        def changed(*args, **kwargs):
            answer = original(*args, **kwargs)
            self.path.write_text(self.path.read_text() + '\n', encoding='utf-8')
            return answer
        with patch.object(tlp, 'evaluate', side_effect=changed):
            result = tlp.run(None, self.args(max_examples=1), self.out)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.out / 'tlp' / 'fixture.json').exists())


if __name__ == '__main__':
    unittest.main()
