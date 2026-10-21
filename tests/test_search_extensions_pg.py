"""Real PostgreSQL checks for typed solver witnesses and structured replay."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import pgfixture
from semantic_crosscheck import counterexample as ce


@unittest.skipUnless(pgfixture.available(), 'requires throw-away PostgreSQL')
class SearchExtensionsPgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = pgfixture.PgFixture('sxc_test_search_extensions').setup()

    @classmethod
    def tearDownClass(cls):
        cls.pg.teardown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def args(self, reference, candidate, **changes):
        for name, text in [('reference', reference), ('candidate', candidate)]:
            (self.root / (name + '.sql')).write_text(text, encoding='utf-8')
        values = dict(reference_sql=str(self.root / 'reference.sql'), candidate_sql=str(self.root / 'candidate.sql'),
                      scenario=None, seed=7, max_examples=16, max_shrink_steps=256, dsn_env='SXC_PG_DSN',
                      replay=None, statement_timeout_ms=1000, constraints=None, witness_schema=None,
                      shrink_strategy='structured', solver_timeout_ms=3000)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_solver_generates_typed_null_mismatch_and_replays_twice(self):
        path = self.root / 'schema.json'
        path.write_text(json.dumps({'version': 1, 'tables': {'items': {'rows': [1, 2], 'columns': [
            {'name': 'id', 'type': 'integer', 'values': [1, 2]},
            {'name': 'qty', 'type': 'numeric(8,3)', 'values': [None, '0.00049', '0.0005']}]}}}), encoding='utf-8')
        profile = self.root / 'constraints.json'
        profile.write_text(json.dumps({'version': 1, 'tables': {'items': {'primary_key': ['id']}},
                                      'checks': [{'table': 'items', 'column': 'qty', 'op': 'ge', 'value': '0'}]}), encoding='utf-8')
        args = self.args('SELECT count(*) AS n FROM items', 'SELECT count(qty) AS n FROM items',
                         witness_schema=str(path), constraints=str(profile))
        self.assertEqual(ce.run(None, args, self.root).status, 'fail')
        saved = self.root / 'counterexample/fixture.json'
        payload = json.loads(saved.read_text(encoding='utf-8'))
        self.assertTrue(any(row[1] is None for row in payload['tables']['items']))
        args.replay = str(saved)
        for _ in range(2):
            self.assertEqual(ce.run(None, args, self.root).status, 'fail')

    def test_builtin_structured_numeric_simplification_is_real_and_replayable(self):
        args = self.args('SELECT sum(qty) AS n FROM stock', 'SELECT sum(qty) + 1 AS n FROM stock', scenario='nulls')
        self.assertEqual(ce.run(None, args, self.root).status, 'fail')
        saved = self.root / 'counterexample/fixture.json'
        payload = json.loads(saved.read_text(encoding='utf-8'))
        self.assertEqual(len(payload['tables']['stock']), 1)
        self.assertEqual(payload['tables']['stock'][0][2], '0')
        self.assertTrue(any(op['operation'] == 'value_simplification' for op in payload['shrink']['trace']))
        args.replay = str(saved)
        self.assertEqual(ce.run(None, args, self.root).status, 'fail')

    def test_cast_rounding_uses_real_postgresql_numeric(self):
        schema = {'order': (('select', 'numeric(8,3)'),)}
        conn = pgfixture.db.connect('SXC_PG_DSN')
        try:
            answer = ce.evaluate(conn, {'order': [['0.0005']]}, 'SELECT "select" AS n FROM "order"',
                                 'SELECT 0.001::numeric AS n', 1000, schema=schema)
            self.assertTrue(answer['equal'])
        finally:
            conn.close()

    def test_high_precision_boundaries_match_real_postgresql(self):
        from semantic_crosscheck import constraints
        cases = [('numeric(29,0)', '9' * 29), ('numeric(38,0)', '9' * 38),
                 ('numeric(38,38)', '0.' + '9' * 38)]
        conn = pgfixture.db.connect('SXC_PG_DSN')
        try:
            for kind, value in cases:
                with self.subTest(kind=kind):
                    schema = {'items': (('qty', kind),)}
                    tables = {'items': [[value]]}
                    constraints.validate_fixture(tables, schema)
                    answer = ce.evaluate(conn, tables, 'SELECT qty AS n FROM items',
                                         "SELECT '" + value + "'::numeric AS n", 1000, schema=schema)
                    self.assertTrue(answer['equal'])
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
