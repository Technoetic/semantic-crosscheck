"""Structured synthetic witness reduction and constrained replay, without a DB."""
import copy
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest

import pgfixture  # noqa: F401 - package path setup
from semantic_crosscheck import constraints, counterexample as ce, reduction


class StructuredReductionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def profile(self, document, schema=None):
        path = self.directory / 'profile.json'
        path.write_text(json.dumps(document), encoding='utf-8')
        return constraints.load(path, schema or ce.SCHEMA)

    def fixture(self):
        return {table: [] for table in ce.SCHEMA}

    def reduce(self, fixture, predicate, budget=1000, profile=None):
        return reduction.shrink(fixture, predicate, budget, profile=profile, schema=ce.SCHEMA)

    def cyclic_profile(self):
        return self.profile({'version': 1, 'tables': {'part': {'primary_key': ['part_id']}},
                             'foreign_keys': [{'table': 'part', 'columns': ['unit'],
                                               'references': {'table': 'part', 'columns': ['part_id']}}]})

    def test_cyclic_fk_group_removes_rows_that_single_deletions_cannot(self):
        profile = self.cyclic_profile()
        fixture = self.fixture()
        fixture['part'] = [['A', None, 'B', None, None], ['B', None, 'A', None, None],
                           ['C', None, 'C', None, None]]
        original = copy.deepcopy(fixture)
        predicate = lambda trial: any(row[0] == 'C' for row in trial['part'])
        legacy = ce.shrink(fixture, predicate, 1000, profile=profile)
        self.assertEqual(len(legacy['tables']['part']), 3)
        reduced = self.reduce(fixture, predicate, profile=profile)
        self.assertEqual(reduced['tables']['part'], [['C', None, 'C', None, None]])
        self.assertTrue(reduced['deletion_1_minimal'])
        self.assertEqual(reduced['operation_counts']['dependency_group_deletion']['accepted'], 1)
        self.assertEqual(fixture, original)

    def test_self_fk_reverse_dependency_closure_includes_transitive_dependants(self):
        profile = self.cyclic_profile()
        fixture = self.fixture()
        fixture['part'] = [['A', None, 'B', None, None], ['B', None, 'A', None, None],
                           ['D', None, 'A', None, None], ['C', None, 'C', None, None]]
        def predicate(trial):
            ids = {row[0] for row in trial['part']}
            return 'C' in ids and ('A' not in ids or 'D' in ids)
        reduced = self.reduce(fixture, predicate, profile=profile)
        self.assertEqual(reduced['tables']['part'], [['C', None, 'C', None, None]])
        groups = [op for op in reduced['trace'] if op['operation'] == 'dependency_group_deletion']
        # A and B form a cycle; D references A and must leave in the same operation.
        self.assertEqual(groups, [{'operation': 'dependency_group_deletion', 'indices': {'part': [0, 1, 2]}}])
        self.assertEqual(reduced['operation_counts']['row_deletion']['accepted'], 0)

    def test_normalized_numeric_fk_closure_and_key_values_are_preserved(self):
        schema = {'part': (('id', 'numeric(18,3)'), ('parent', 'numeric(18,3)'), ('qty', 'integer'))}
        profile = self.profile({'version': 1, 'tables': {'part': {'primary_key': ['id']}},
                                'foreign_keys': [{'table': 'part', 'columns': ['parent'],
                                                  'references': {'table': 'part', 'columns': ['id']}}]}, schema)
        fixture = {'part': [['1.000', '2', 9], [2, 1, 8], ['3', '3.000', 7]]}
        predicate = lambda trial: any(row[0] == '3' for row in trial['part'])
        result = reduction.shrink(fixture, predicate, 1000, profile=profile, schema=schema)
        self.assertEqual(result['tables'], {'part': [['3', '3.000', None]]})
        constraints.validate_fixture(result['tables'], schema, profile)

    def test_type_aware_values_simplify_then_unlock_further_row_deletion(self):
        fixture = self.fixture()
        fixture['stock'] = [['A', 'L', '9'], ['B', 'L', '8']]
        profile = self.profile({'version': 1, 'tables': {'stock': {
            'primary_key': ['part_id', 'location_id'], 'not_null': ['qty']}}})
        # Two positive rows initially witness the discrepancy. The canonical
        # positive value 1 also permits a one-row witness.
        def predicate(trial):
            rows = trial['stock']
            return bool(rows) and all(float(row[2]) > 0 for row in rows) and (len(rows) == 2 or rows[0][2] == '1')
        reduced = self.reduce(fixture, predicate, profile=profile)
        self.assertEqual(len(reduced['tables']['stock']), 1)
        self.assertEqual(reduced['tables']['stock'][0][2], '1')
        self.assertGreater(reduced['operation_counts']['value_simplification']['accepted'], 0)
        self.assertGreater(reduced['operation_counts']['row_deletion']['accepted'], 0)
        self.assertTrue(reduced['deletion_1_minimal'])

    def test_invalid_candidates_are_counted_but_never_reach_predicate(self):
        fixture = self.fixture()
        fixture['stock'] = [['A', 'L', '8']]
        profile = self.profile({'version': 1, 'tables': {'stock': {
            'primary_key': ['part_id', 'location_id'], 'not_null': ['qty']}},
            'checks': [{'table': 'stock', 'column': 'qty', 'op': 'ge', 'value': '7'}]})
        calls = []
        def predicate(trial):
            constraints.validate_fixture(trial, ce.SCHEMA, profile)
            calls.append(copy.deepcopy(trial))
            return bool(trial['stock'])
        reduced = self.reduce(fixture, predicate, profile=profile)
        self.assertEqual(reduced['tables'], fixture)
        self.assertGreater(reduced['invalid_domain_trials'], 0)
        self.assertEqual(reduced['shrink_errors'], 0)
        self.assertTrue(reduced['deletion_1_minimal'])
        self.assertEqual(calls, [self.fixture()])

    def test_query_errors_never_accept_trials_or_disclose_values(self):
        fixture = self.fixture()
        fixture['stock'] = [['ROW-VALUE-MUST-NOT-LEAK', 'L', '8']]
        def broken(_trial):
            raise RuntimeError('ROW-VALUE-MUST-NOT-LEAK')
        reduced = self.reduce(fixture, broken)
        self.assertEqual(reduced['tables'], fixture)
        self.assertGreater(reduced['shrink_errors'], 0)
        self.assertFalse(reduced['deletion_1_minimal'])
        self.assertNotIn('ROW-VALUE', json.dumps({k: v for k, v in reduced.items() if k != 'tables'}))

    def test_shared_budget_counts_invalid_and_query_trials_without_overrun(self):
        fixture = self.fixture()
        fixture['stock'] = [['A', 'L', '9']]
        profile = self.profile({'version': 1, 'tables': {'stock': {
            'primary_key': ['part_id', 'location_id'], 'not_null': ['qty']}}})
        for budget in (0, 1, 2, 3):
            calls = []
            with self.subTest(budget=budget):
                reduced = self.reduce(fixture, lambda trial: calls.append(trial) or bool(trial['stock']),
                                      budget, profile=profile)
                self.assertEqual(reduced['steps'], budget)
                self.assertTrue(reduced['budget_exhausted'])
                self.assertFalse(reduced['deletion_1_minimal'])
                self.assertLessEqual(len(calls), budget)
                self.assertIn('operation_counts', reduced)
                self.assertEqual(sum(counts['attempted'] for counts in reduced['operation_counts'].values()), budget)

    def test_value_reduction_is_monotone_deterministic_and_replays_exactly(self):
        schema = {'stock': (('qty', 'integer'),)}
        fixture = {'stock': [[42]]}
        calls = []
        def predicate(trial):
            calls.append(copy.deepcopy(trial))
            return bool(trial['stock']) and trial['stock'][0][0] in (42, 1, -1)
        first = reduction.shrink(fixture, predicate, 100, schema=schema)
        self.assertEqual(first['tables'], {'stock': [[1]]})
        self.assertEqual(first['accepted'], 1)
        self.assertLess(first['steps'], 20)
        second = reduction.shrink(fixture, predicate, 100, schema=schema)
        self.assertEqual(first, second)
        replayed = reduction.replay_trace(fixture, first['trace'], schema=schema)
        self.assertEqual(first['tables'], replayed)
        self.assertEqual(fixture, {'stock': [[42]]})
        # Once 1 is reached, -1 must never be accepted as a second simplification.
        self.assertEqual([op['value'] for op in first['trace']], [1])

    def test_replay_refuses_forged_values_key_changes_and_wrong_dependency_groups(self):
        profile = self.cyclic_profile()
        fixture = self.fixture()
        fixture['part'] = [['A', 'ordinary', 'B', None, None], ['B', None, 'A', None, None],
                           ['C', None, 'C', None, None]]
        forged = [
            [{'operation': 'value_simplification', 'table': 'part', 'row': 0, 'column': 1, 'value': 'arbitrary'}],
            [{'operation': 'value_simplification', 'table': 'part', 'row': 0, 'column': 0, 'value': None}],
            [{'operation': 'dependency_group_deletion', 'indices': {'part': [0, 1, 2]}}],
            [{'operation': 'row_deletion', 'table': 'part', 'index': True}],
            [{'operation': 'row_deletion', 'table': 'part', 'index': 999}],
            [{'operation': 'row_deletion', 'table': 'part', 'index': 2, 'extra': 'ROW-VALUE-MUST-NOT-LEAK'}],
        ]
        for trace in forged:
            with self.subTest(trace=trace):
                with self.assertRaises(ValueError) as failure:
                    reduction.replay_trace(fixture, trace, schema=ce.SCHEMA, profile=profile)
                self.assertNotIn('arbitrary', str(failure.exception))
                self.assertNotIn('ROW-VALUE', str(failure.exception))

    def test_replay_checks_each_intermediate_domain_not_just_final_fixture(self):
        profile = self.cyclic_profile()
        fixture = self.fixture()
        fixture['part'] = [['A', None, 'B', None, None], ['B', None, 'A', None, None]]
        trace = [{'operation': 'row_deletion', 'table': 'part', 'index': 0},
                 {'operation': 'row_deletion', 'table': 'part', 'index': 0}]
        with self.assertRaises(ValueError):
            reduction.replay_trace(fixture, trace, schema=ce.SCHEMA, profile=profile)
        valid_trace = [{'operation': 'dependency_group_deletion', 'indices': {'part': [0, 1]}}]
        self.assertEqual(reduction.replay_trace(fixture, valid_trace, schema=ce.SCHEMA, profile=profile), self.fixture())

    def test_empty_fixture_with_zero_budget_is_already_deletion_minimal(self):
        reduced = self.reduce(self.fixture(), lambda trial: True, 0)
        self.assertEqual(reduced['steps'], 0)
        self.assertFalse(reduced['budget_exhausted'])
        self.assertTrue(reduced['deletion_1_minimal'])

    def test_schema_budget_and_initial_domain_fail_before_predicate(self):
        calls = []
        predicate = lambda trial: calls.append(trial) or True
        for budget in (-1, True, 10001, '1'):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                reduction.shrink(self.fixture(), predicate, budget, schema=ce.SCHEMA)
        with self.assertRaises(ValueError):
            reduction.shrink(self.fixture(), predicate, 10)
        with self.assertRaises(constraints.ConstraintViolation):
            reduction.shrink({'wrong': []}, predicate, 10, schema=ce.SCHEMA)
        self.assertEqual(calls, [])

    def test_read_only_schema_mapping_is_accepted_without_reordering_tables(self):
        schema = MappingProxyType({'stock': (('qty', 'integer'),), 'part': (('name', 'text'),)})
        fixture = {'stock': [[42]], 'part': [['ordinary']]}
        result = reduction.shrink(fixture, lambda trial: bool(trial['stock']), 100, schema=schema)
        self.assertEqual(result['tables'], {'stock': [[None]], 'part': []})
        self.assertEqual(result['trace'][0], {'operation': 'row_deletion', 'table': 'part', 'index': 0})
        self.assertEqual(reduction.replay_trace(fixture, result['trace'], schema=schema), result['tables'])

    def test_composite_fk_match_simple_does_not_link_nullable_source(self):
        schema = {'part': (('id', 'numeric(18,3)'), ('unit', 'text')),
                  'stock': (('part_id', 'numeric(18,3)'), ('location_id', 'text'), ('qty', 'integer'))}
        profile = self.profile({'version': 1, 'tables': {'part': {'unique': [['id', 'unit']]}},
                                'foreign_keys': [{'table': 'stock', 'columns': ['part_id', 'location_id'],
                                                  'references': {'table': 'part', 'columns': ['id', 'unit']}}]}, schema)
        fixture = {'part': [[1, 'L'], [2, 'M']], 'stock': [['1.000', 'L', 9], [None, 'missing', 8]]}
        def predicate(trial):
            ids = {row[0] for row in trial['part']}
            return (2 in ids and (1 not in ids or any(row[0] == '1.000' for row in trial['stock']))
                    and any(row[0] is None for row in trial['stock']))
        result = reduction.shrink(fixture, predicate, 1000, profile=profile, schema=schema)
        self.assertEqual(result['tables'], {'part': [[2, 'M']], 'stock': [[None, 'missing', None]]})
        groups = [op for op in result['trace'] if op['operation'] == 'dependency_group_deletion']
        self.assertEqual(groups, [{'operation': 'dependency_group_deletion',
                                   'indices': {'part': [0], 'stock': [0]}}])
        self.assertEqual(reduction.replay_trace(fixture, result['trace'], schema=schema, profile=profile), result['tables'])

    def test_nonnull_type_specific_targets_and_json_trace_round_trip(self):
        schema = {'stock': (('id', 'text'), ('qty', 'integer'), ('due', 'date'),
                            ('measure', 'numeric(18,3)'), ('name', 'text'))}
        profile = self.profile({'version': 1, 'tables': {'stock': {
            'primary_key': ['id'], 'not_null': ['qty', 'due', 'measure', 'name']}}}, schema)
        fixture = {'stock': [['A', 9, '2026-01-10', '5.5', 'ordinary']]}
        result = reduction.shrink(fixture, lambda trial: bool(trial['stock']), 100, profile=profile, schema=schema)
        self.assertEqual(result['tables'], {'stock': [['A', 0, '2000-01-01', '0', '']]})
        self.assertEqual(result['accepted'], 4)
        self.assertEqual(reduction.replay_trace(fixture, json.loads(json.dumps(result['trace'])),
                                               schema=schema, profile=profile), result['tables'])

    def test_replay_bounds_trace_and_rejects_rank_increasing_cycles(self):
        schema = {'stock': (('qty', 'integer'),)}
        fixture = {'stock': [[42]]}
        cycle = [{'operation': 'value_simplification', 'table': 'stock', 'row': 0, 'column': 0, 'value': 1},
                 {'operation': 'value_simplification', 'table': 'stock', 'row': 0, 'column': 0, 'value': -1}]
        with self.assertRaises(ValueError):
            reduction.replay_trace(fixture, cycle, schema=schema)
        overlong = [{'operation': 'row_deletion', 'table': 'stock', 'index': 0}] * 10001
        with self.assertRaises(ValueError):
            reduction.replay_trace(fixture, overlong, schema=schema)

    def test_admissible_callback_keeps_finite_values_and_row_bound_during_group_trials(self):
        schema = {'part': (('id', 'integer'), ('parent', 'integer'), ('qty', 'integer'))}
        profile = self.profile({'version': 1, 'tables': {'part': {'primary_key': ['id']}},
                                'foreign_keys': [{'table': 'part', 'columns': ['parent'],
                                                  'references': {'table': 'part', 'columns': ['id']}}]}, schema)
        fixture = {'part': [[1, 2, 9], [2, 1, 8], [3, 3, 7]]}
        def admissible(trial):
            if not 2 <= len(trial['part']) <= 3 or any(row[2] not in (7, 8, 9) for row in trial['part']):
                raise constraints.ConstraintViolation('ROW-VALUE-MUST-NOT-LEAK')
        calls = []
        def predicate(trial):
            admissible(trial)
            calls.append(copy.deepcopy(trial))
            return any(row[0] == 3 for row in trial['part'])
        result = reduction.shrink(fixture, predicate, 100, profile=profile, schema=schema, admissible=admissible)
        self.assertEqual(result['tables'], fixture)
        self.assertEqual(result['accepted'], 0)
        self.assertGreater(result['invalid_domain_trials'], 2)
        self.assertEqual(result['shrink_errors'], 0)
        self.assertTrue(result['deletion_1_minimal'])
        self.assertEqual(calls, [{'part': [[1, 2, 9], [2, 1, 8]]}])
        forged = [
            [{'operation': 'dependency_group_deletion', 'indices': {'part': [0, 1]}}],
            [{'operation': 'value_simplification', 'table': 'part', 'row': 0, 'column': 2, 'value': 0}],
        ]
        for trace in forged:
            with self.subTest(trace=trace), self.assertRaises(ValueError) as failure:
                reduction.replay_trace(fixture, trace, schema=schema, profile=profile, admissible=admissible)
            self.assertNotIn('ROW-VALUE', str(failure.exception))
        calls.clear()
        with self.assertRaises(constraints.ConstraintViolation):
            reduction.shrink({'part': [[3, 3, 7]]}, predicate, 100,
                             profile=profile, schema=schema, admissible=admissible)
        self.assertEqual(calls, [])

    def test_schema_only_admissible_scope_and_replay_validate_each_accepted_step(self):
        schema = {'stock': (('qty', 'integer'),)}
        fixture = {'stock': [[7], [7]]}
        checks = []
        def admissible(trial):
            if not 1 <= len(trial['stock']) <= 2 or any(row[0] not in (0, 7) for row in trial['stock']):
                raise constraints.ConstraintViolation('Outside finite witness domain')
            checks.append(copy.deepcopy(trial))
        result = reduction.shrink(fixture, lambda trial: bool(trial['stock']), 100,
                                  schema=schema, admissible=admissible)
        self.assertEqual(result['tables'], {'stock': [[0]]})
        self.assertEqual(result['minimality_scope'], 'admissible_single_row_deletions_after_structured_reduction')
        checks.clear()
        replayed = reduction.replay_trace(fixture, result['trace'], schema=schema, admissible=admissible)
        self.assertEqual(replayed, result['tables'])
        self.assertEqual(checks, [fixture, {'stock': [[7]]}, {'stock': [[0]]}])

    def test_fractional_numeric_types_skip_out_of_range_canonical_integers(self):
        for kind in ('numeric(1,1)', 'numeric(38,38)'):
            with self.subTest(kind=kind):
                schema = {'items': (('qty', kind),)}
                fixture = {'items': [['0.5']]}
                predicate = lambda trial: bool(trial['items']) and trial['items'][0][0] is not None
                result = reduction.shrink(fixture, predicate, 100, schema=schema)
                self.assertEqual(result['tables'], {'items': [['0']]})
                self.assertEqual(result['shrink_errors'], 0)
                self.assertTrue(result['deletion_1_minimal'])
                self.assertEqual(reduction.replay_trace(fixture, result['trace'], schema=schema), result['tables'])


if __name__ == '__main__':
    unittest.main()
