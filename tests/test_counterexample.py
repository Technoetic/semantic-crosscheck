"""Bounded, source-write-free SQL counterexample search and row-deletion replay."""
import copy
import datetime as dt
from decimal import Decimal
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - puts the package on sys.path

try:
    ce = importlib.import_module('semantic_crosscheck.counterexample')
except ModuleNotFoundError:
    ce = None


class CounterexampleTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ce, 'The real PostgreSQL counterexample engine must exist')

    def test_seed_determinism_and_all_four_bounded_scenarios(self):
        for scenario in ce.SCENARIOS:
            a = ce.generate(scenario, 7, 0)
            self.assertEqual(a, ce.generate(scenario, 7, 0))
            self.assertEqual(set(a), {'part', 'bom_line', 'sales_order', 'stock'})
            self.assertLessEqual(sum(map(len, a.values())), 64)
            self.assertNotEqual(a, ce.generate(scenario, 8, 0))

    def test_cte_values_are_bound_and_nested_with_is_preserved(self):
        fixture = ce.generate('nulls', 7, 0)
        fixture['part'][0][-1] = "ROW-VALUE-'quote"
        sql, params = ce.wrap_sql('WITH q AS (SELECT * FROM stock) SELECT * FROM q;', fixture)
        self.assertNotIn("ROW-VALUE", sql)
        self.assertIn("ROW-VALUE-'quote", params.values())
        self.assertIn('CAST(:', sql)
        self.assertIn('WITH q AS', sql)
        self.assertIn('sxc_candidate AS (', sql)
        self.assertNotIn('CREATE TABLE', sql)

    def test_empty_cte_is_typed_and_has_no_rows(self):
        fixture = {name: [] for name in ce.SCHEMA}
        sql, params = ce.wrap_sql('SELECT count(*) FROM stock', fixture)
        self.assertIn('WHERE FALSE', sql)
        self.assertEqual(params, {})

    def test_bag_comparison_preserves_multiplicity_and_column_names(self):
        self.assertFalse(ce.compare_answers(['x'], [[1], [1]], ['x'], [[1]])['equal'])
        self.assertFalse(ce.compare_answers(['x'], [], ['y'], [])['equal'])
        self.assertTrue(ce.compare_answers(['x'], [[1], [2]], ['x'], [[2], [1]])['equal'])

    def test_duplicate_projection_aliases_are_errors_not_witnesses(self):
        with self.assertRaises(ValueError):
            ce.compare_answers(['x', 'x'], [[1, 2]], ['x', 'y'], [[1, 2]])

    def test_canonical_comparison_keeps_text_exact_and_decimal_digits(self):
        self.assertTrue(ce.compare_answers(['x'], [[Decimal('1.000')]], ['x'], [[1]])['equal'])
        self.assertFalse(ce.compare_answers(['x'], [['1.10']], ['x'], [['1.1']])['equal'])
        self.assertFalse(ce.compare_answers(['x'], [[True]], ['x'], [[1]])['equal'])
        self.assertFalse(ce.compare_answers(['x'], [[None]], ['x'], [['']])['equal'])
        self.assertFalse(ce.compare_answers(['x'], [[Decimal('1.0000000000000000000001')]], ['x'], [[Decimal('1.0000000000000000000002')]])['equal'])
        a = dt.datetime(2026, 1, 1, microsecond=1)
        b = dt.datetime(2026, 1, 1, microsecond=2)
        self.assertFalse(ce.compare_answers(['x'], [[a]], ['x'], [[b]])['equal'])

    def test_shrink_preserves_witness_and_is_deletion_one_minimal(self):
        fixture = ce.generate('nulls', 7, 0)
        predicate = lambda f: any(row[2] is None for row in f['stock'])
        answer = ce.shrink(fixture, predicate, 300)
        self.assertTrue(predicate(answer['tables']))
        self.assertTrue(answer['deletion_1_minimal'])
        self.assertFalse(answer['budget_exhausted'])
        self.assertEqual(sum(map(len, answer['tables'].values())), 1)
        self.assertEqual(fixture, ce.generate('nulls', 7, 0))

    def test_budget_exhaustion_never_claims_minimal(self):
        answer = ce.shrink(ce.generate('nulls', 7, 0), lambda f: True, 0)
        self.assertTrue(answer['budget_exhausted'])
        self.assertFalse(answer['deletion_1_minimal'])

    def test_invalid_shrink_trials_preserve_valid_witness_and_disclose_limit(self):
        def broken(_fixture):
            raise RuntimeError('ROW-VALUE-MUST-NOT-LEAK')
        fixture = ce.generate('nulls', 7, 0)
        reduced = ce.shrink(fixture, broken, 30)
        self.assertEqual(reduced['tables'], fixture)
        self.assertGreater(reduced['shrink_errors'], 0)
        self.assertFalse(reduced['deletion_1_minimal'])

    def test_valid_initial_witness_survives_errors_during_shrinking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); args = self.args(path)
            original_rows = sum(map(len, ce.generate('nulls', 7, 0).values()))
            def answer(_connection, tables, *_args):
                if sum(map(len, tables.values())) != original_rows:
                    raise RuntimeError('ROW-VALUE-MUST-NOT-LEAK')
                return {'equal': False, 'reference_rows': 1, 'candidate_rows': 1, 'column_names_match': True}
            with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', side_effect=answer):
                result = ce.run(None, args, path)
            self.assertEqual(result.status, 'fail')
            self.assertTrue((path / 'counterexample/fixture.json').exists())
            self.assertGreater(result.checks[0].detail['shrink_errors'], 0)
            self.assertNotIn('ROW-VALUE', json.dumps(result.to_dict()))

    def test_both_queries_use_one_readonly_repeatable_transaction(self):
        class Prepared:
            columns = [{'name': 'n'}]
            def run(self, **params):
                conn.events.append(('execute', params))
                return [[1]]
            def close(self):
                conn.events.append(('close',))
        class Connection:
            def __init__(self): self.events = []
            def run(self, sql, **params): self.events.append((sql,))
            def prepare(self, sql):
                self.events.append(('prepare', sql))
                return Prepared()
        conn = Connection()
        result = ce.evaluate(conn, ce.generate('nulls', 7, 0), 'SELECT 1 AS n', 'SELECT 1 AS n', 1000)
        self.assertTrue(result['equal'])
        names = [event[0] for event in conn.events]
        self.assertEqual(names.count('BEGIN TRANSACTION READ ONLY'), 1)
        self.assertEqual(names.count('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'), 1)
        self.assertEqual(names.count('ROLLBACK'), 1)
        self.assertEqual(names.count('prepare'), 2)

    def args(self, directory, **changes):
        reference = directory / 'reference.sql'; reference.write_text('SELECT 1 AS n', encoding='utf-8')
        candidate = directory / 'candidate.sql'; candidate.write_text('SELECT 2 AS n', encoding='utf-8')
        values = dict(reference_sql=str(reference), candidate_sql=str(candidate), scenario='nulls', seed=7,
                      max_examples=2, max_shrink_steps=300, dsn_env='SXC_PG_DSN', replay=None,
                      statement_timeout_ms=1000)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_run_writes_only_separate_synthetic_fixture_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); args = self.args(path)
            fake_conn = SimpleNamespace(close=lambda: None)
            different = {'equal': False, 'reference_rows': 1, 'candidate_rows': 1, 'column_names_match': True}
            with patch.object(ce.db, 'connect', return_value=fake_conn), patch.object(ce, 'evaluate', return_value=different):
                result = ce.run(None, args, path)
                fixture_path = path / 'counterexample/fixture.json'
                artifact = json.loads(fixture_path.read_text())
                self.assertTrue(artifact['synthetic'])
                self.assertEqual(result.status, 'fail')
                self.assertNotIn('"tables":', json.dumps(result.to_dict()))
                args.replay = str(fixture_path)
                self.assertEqual(ce.run(None, args, path).status, 'fail')

    def test_no_mismatch_is_not_equivalence_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); args = self.args(path)
            with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', return_value={'equal': True, 'reference_rows': 0, 'candidate_rows': 0, 'column_names_match': True}):
                result = ce.run(None, args, path)
            self.assertEqual(result.status, 'not_run')
            self.assertTrue(result.not_proven)
            self.assertFalse((path / 'counterexample/fixture.json').exists())

    def test_query_errors_are_errors_and_never_counterexamples_or_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); args = self.args(path)
            with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', side_effect=RuntimeError('ROW-VALUE-MUST-NOT-LEAK')):
                result = ce.run(None, args, path)
            self.assertEqual(result.status, 'error')
            self.assertNotIn('ROW-VALUE', json.dumps(result.to_dict()))
            self.assertFalse((path / 'counterexample/fixture.json').exists())

    def test_invalid_arguments_refused_before_database_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for changes in ({'max_examples': 0}, {'max_shrink_steps': -1}, {'scenario': 'unknown'}, {'statement_timeout_ms': 0}):
                with self.subTest(changes=changes), patch.object(ce.db, 'connect') as connect:
                    with self.assertRaises(ce.StageError): ce.run(None, self.args(path, **changes), path)
                    connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
