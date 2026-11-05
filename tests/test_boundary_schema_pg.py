"""Generated offline boundaries must distinguish real PostgreSQL query results."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import pgfixture
from semantic_crosscheck import boundary_schema as boundary, counterexample as ce, witness


@unittest.skipUnless(pgfixture.available(), 'requires throw-away PostgreSQL')
class BoundarySchemaPgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = pgfixture.PgFixture('sxc_test_boundary_schema').setup()

    @classmethod
    def tearDownClass(cls):
        cls.pg.teardown()

    def test_scalar_boundary_artifacts_find_real_inclusive_exclusive_mismatches(self):
        cases = [
            ('integer', 'qty', 'qty >= 10', 'qty > 10', 10),
            ('numeric(6,2)', 'qty', 'qty >= 1.20', 'qty > 1.20', '1.20'),
            ('date', 'day', "day >= DATE '2024-02-29'",
             "day > DATE '2024-02-29'", '2024-02-29'),
        ]
        with tempfile.TemporaryDirectory(prefix='sxc_boundary_pg_') as temporary:
            root = Path(temporary)
            conn = self.pg.check_conn()
            try:
                for index, (kind, column, reference_predicate, candidate_predicate, center) in enumerate(cases):
                    with self.subTest(kind=kind):
                        source = root / f'input-{index}.json'
                        source.write_text(json.dumps({'version': 1, 'table': 'items', 'column': column,
                            'type': kind, 'rows': [0, 1], 'predicate_sql': reference_predicate}), encoding='utf-8')
                        result = boundary.run(None, SimpleNamespace(spec=str(source)), root / str(index))
                        self.assertEqual(result.status, 'pass')
                        spec = witness.load(root / str(index) / 'boundary_schema' / 'witness-schema.json')
                        generation = witness.generate(spec, max_examples=5, timeout_ms=10000)
                        self.assertEqual(len(generation.fixtures), 5)
                        reference = 'SELECT count(*) AS n FROM items WHERE ' + reference_predicate
                        candidate = 'SELECT count(*) AS n FROM items WHERE ' + candidate_predicate
                        mismatches = []
                        for fixture in generation.fixtures:
                            answer = ce.evaluate(conn, fixture, reference, candidate, 1000, schema=spec.schema)
                            if not answer['equal']:
                                mismatches.append(fixture)
                                # Independently checked: exactly the boundary row contributes
                                # to >= and contributes zero to >, in every supported type.
                                self.assertEqual(fixture, {'items': [[center]]})
                                for sql, expected in ((reference, [[1]]), (candidate, [[0]])):
                                    wrapped, params = ce.wrap_sql(sql, fixture, schema=spec.schema)
                                    self.assertEqual(conn.run(wrapped, **params), expected)
                        self.assertEqual(mismatches, [{'items': [[center]]}])
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
