"""Opt-in search integration: typed CTEs, provenance-bound replay and fail closed."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401
import sxc
from semantic_crosscheck import counterexample as ce


class SearchExtensionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def args(self, **changes):
        for name, sql in [('reference', 'SELECT count(*) AS n FROM items'),
                          ('candidate', 'SELECT count(qty) AS n FROM items')]:
            (self.root / (name + '.sql')).write_text(sql, encoding='utf-8')
        values = dict(reference_sql=str(self.root / 'reference.sql'), candidate_sql=str(self.root / 'candidate.sql'),
                      scenario=None, seed=7, max_examples=8, max_shrink_steps=100, dsn_env='SXC_TEST_UNUSED',
                      replay=None, statement_timeout_ms=1000, constraints=None, witness_schema=None,
                      shrink_strategy='rows', solver_timeout_ms=3000)
        values.update(changes)
        return SimpleNamespace(**values)

    def spec(self):
        path = self.root / 'schema.json'
        path.write_text(json.dumps({'version': 1, 'tables': {'items': {'rows': [1, 1], 'columns': [
            {'name': 'id', 'type': 'integer', 'values': [1]},
            {'name': 'qty', 'type': 'numeric(8,2)', 'values': [None, '0', '1.25']}]}}}), encoding='utf-8')
        return path

    def test_cli_exposes_explicit_opt_in_flags(self):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = sxc.parser().parse_args(['counterexample', '--witness-schema', 'schema.json',
                                              '--shrink-strategy', 'structured', '--solver-timeout-ms', '3000'])
            except SystemExit:
                self.fail('Typed witness and structured shrink options must be accepted')
        self.assertEqual(args.witness_schema, 'schema.json')
        self.assertEqual(args.shrink_strategy, 'structured')

    def test_generic_cte_quotes_identifiers_and_binds_every_value(self):
        schema = {'order': (('select', 'text'), ('n', 'numeric(8,2)'))}
        sql, params = ce.wrap_sql('SELECT * FROM "order"', {'order': [["VALUE-'quote", '1.23']]}, schema=schema)
        self.assertIn('"order"("select", "n")', sql)
        self.assertNotIn("VALUE-'quote", sql)
        self.assertIn("VALUE-'quote", params.values())
        self.assertIn('numeric(8,2)', sql)

    def test_schema_search_writes_bound_replay_and_no_values_in_result(self):
        args = self.args(witness_schema=str(self.spec()))
        def answer(_conn, fixture, *_a, **_kw):
            return {'equal': not any(row[1] is None for row in fixture['items']), 'reference_rows': 1,
                    'candidate_rows': 1, 'column_names_match': True}
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', side_effect=answer):
            result = ce.run(None, args, self.root)
            self.assertEqual(result.status, 'fail')
            path = self.root / 'counterexample/fixture.json'
            payload = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(payload['schema_version'], 2)
            self.assertEqual(payload['generator']['mode'], 'constraint_witness')
            self.assertNotIn('tables', json.dumps(result.to_dict()))
            args.replay = str(path)
            self.assertEqual(ce.run(None, args, self.root).status, 'fail')

    def test_replay_rejects_new_original_domain_value_before_connect(self):
        args = self.args(witness_schema=str(self.spec()))
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', return_value={'equal': False}):
            self.assertEqual(ce.run(None, args, self.root).status, 'fail')
        path = self.root / 'counterexample/fixture.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['original_tables']['items'][0][0] = 9999
        path.write_text(json.dumps(payload), encoding='utf-8')
        args.replay = str(path)
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, args, self.root)
        connect.assert_not_called()

    def test_schema_edits_invalidate_replay_before_connect(self):
        schema_path = self.spec()
        args = self.args(witness_schema=str(schema_path))
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', return_value={'equal': False}):
            ce.run(None, args, self.root)
        args.replay = str(self.root / 'counterexample/fixture.json')
        schema_path.write_text(schema_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, args, self.root)
        connect.assert_not_called()

    def test_new_options_cannot_be_silently_ignored_by_other_modes(self):
        for field in ('metamorphic_sql', 'mutation_operator'):
            args = self.args(witness_schema=str(self.spec()), **{field: 'unused'})
            with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
                ce.run(None, args, self.root)
            connect.assert_not_called()

    def test_equal_sql_stays_not_run(self):
        args = self.args(witness_schema=str(self.spec()))
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', return_value={'equal': True}):
            result = ce.run(None, args, self.root)
        self.assertEqual(result.status, 'not_run')
        self.assertTrue(result.not_proven)
        self.assertFalse((self.root / 'counterexample/fixture.json').exists())

    def test_reduction_keeps_declared_row_and_value_bounds(self):
        from semantic_crosscheck import witness
        for strategy in ('rows', 'structured'):
            args = self.args(witness_schema=str(self.spec()), shrink_strategy=strategy)
            with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), patch.object(ce, 'evaluate', return_value={'equal': False}):
                self.assertEqual(ce.run(None, args, self.root).status, 'fail')
            payload = json.loads((self.root / 'counterexample/fixture.json').read_text(encoding='utf-8'))
            witness.validate_fixture(witness.load(args.witness_schema), payload['tables'])
            self.assertEqual(len(payload['tables']['items']), 1)


if __name__ == '__main__':
    unittest.main()
