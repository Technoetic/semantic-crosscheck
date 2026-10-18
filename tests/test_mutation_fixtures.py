"""Operator fixture contracts; no database or source rows are used here."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - package import path
from semantic_crosscheck import constraints, counterexample as ce, mutate

try:
    mf = importlib.import_module('semantic_crosscheck.mutation_fixtures')
except ModuleNotFoundError:
    mf = None


class MutationFixtureTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(mf, 'Operator-directed fixture generation must exist')

    def test_each_operator_generates_bounded_admissible_deterministic_data(self):
        profile = constraints.load(pgfixture.ROOT / 'examples/counterexamples/constraints.json', ce.SCHEMA)
        for operator in mutate.OPERATORS:
            with self.subTest(operator=operator):
                fixture = mf.generate(operator, 7, 0)
                self.assertEqual(fixture, mf.generate(operator, 7, 0))
                self.assertNotEqual(fixture, mf.generate(operator, 8, 0))
                self.assertEqual(set(fixture), set(ce.SCHEMA))
                self.assertLessEqual(sum(map(len, fixture.values())), 32)
                constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_unsupported_operator_is_not_run_without_connecting(self):
        args = SimpleNamespace(mutation_operator='unknown_operator', metamorphic_sql=None)
        with tempfile.TemporaryDirectory() as tmp, patch.object(ce.db, 'connect', side_effect=AssertionError('must not connect')):
            result = ce.run(None, args, tmp)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'unsupported_operator')
        self.assertFalse(result.checks[0].detail['synthetic_distinguished'])

    def test_gold_without_operator_is_configuration_error_before_connection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ce.db, 'connect', side_effect=AssertionError('must not connect')):
            sql = Path(tmp) / 'query.sql'; sql.write_text('SELECT 1 AS value', encoding='utf-8')
            args = SimpleNamespace(gold_sql=str(sql), mutation_operator=None, metamorphic_sql=None,
                                   reference_sql=str(sql), candidate_sql=str(sql), seed=7, max_examples=1,
                                   max_shrink_steps=0, statement_timeout_ms=1000, scenario=None,
                                   dsn_env='SXC_PG_DSN', constraints=None, replay=None)
            with self.assertRaisesRegex(ce.StageError, '--gold-sql requires --mutation-operator') as raised:
                ce.run(None, args, tmp)
        self.assertEqual(raised.exception.exit_code, 2)

    def test_operator_rejects_legacy_scenario_or_metamorphic_combination(self):
        for scenario, metamorphic in [('nulls', None), (None, 'closure.sql')]:
            with self.subTest(scenario=scenario, metamorphic=metamorphic):
                args = SimpleNamespace(mutation_operator='measure_agg_to_max', scenario=scenario,
                                       metamorphic_sql=metamorphic)
                with tempfile.TemporaryDirectory() as tmp, patch.object(ce.db, 'connect', side_effect=AssertionError('must not connect')):
                    with self.assertRaises(ce.StageError) as raised:
                        ce.run(None, args, tmp)
                self.assertEqual(raised.exception.exit_code, 2)

    def payload(self):
        return {'format': mf.FORMAT, 'schema_version': 1, 'generator_version': mf.GENERATOR_VERSION,
                'synthetic': True, 'mutation_operator': 'measure_agg_to_max', 'seed': 7, 'case_index': 0,
                'sql_sha256': {'reference': 'a', 'candidate': 'b', 'gold': 'c'},
                'constraints': constraints.binding(None), 'tables': mf.generate('measure_agg_to_max', 7, 0)}

    def test_replay_rejects_changed_sql_operator_version_and_non_generated_rows(self):
        payload = self.payload()
        kwargs = dict(operator='measure_agg_to_max', seed=7,
                      digests={'reference': 'a', 'candidate': 'b', 'gold': 'c'}, profile=None)
        accepted = mf.replay(json.dumps(payload), **kwargs)
        self.assertEqual(accepted['tables'], payload['tables'])
        for key, value in [('mutation_operator', 'closure_end_swap'), ('generator_version', 2),
                           ('generator_version', True), ('seed', True),
                           ('sql_sha256', {'reference': 'a', 'candidate': 'b', 'gold': 'changed'}),
                           ('constraints', {'mode': 'unconstrained', 'version': 1, 'sha256': 'changed'})]:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(payload); changed[key] = value
                with self.assertRaises(ce.StageError):
                    mf.replay(json.dumps(changed), **kwargs)
        payload['tables']['stock'].append(['not-generated', 'L0', '1'])
        with self.assertRaises(ce.StageError):
            mf.replay(json.dumps(payload), **kwargs)

    def test_replay_accepts_only_valid_row_deletions_under_exact_constraints(self):
        payload = self.payload()
        payload['tables']['stock'].pop()
        self.assertEqual(len(mf.replay(json.dumps(payload), 'measure_agg_to_max', 7,
                                      payload['sql_sha256'])['tables']['stock']), 1)
        with self.assertRaises(ce.StageError):
            mf.replay(json.dumps(payload), 'measure_agg_to_max', 7,
                      {'reference': 'a', 'candidate': 'b'})


if __name__ == '__main__':
    unittest.main()
