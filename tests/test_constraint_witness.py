"""Finite-domain integrity witnesses do not prove SQL query equivalence."""
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - package path setup
from semantic_crosscheck import constraints
from semantic_crosscheck.result import StageError

try:
    witness = importlib.import_module('semantic_crosscheck.witness')
except ModuleNotFoundError:
    witness = None

HAS_Z3 = importlib.util.find_spec('z3') is not None


class _WitnessCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(witness, 'The bounded integrity witness generator must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def spec(self, tables=None):
        document = {'version': 1, 'tables': tables or {
            'part': {'columns': [{'name': 'part_id', 'type': 'text', 'values': [None, 'A', 'B']}],
                     'rows': [1, 1]}}}
        source = self.directory / 'spec.json'
        source.write_text(json.dumps(document), encoding='utf-8')
        return witness.load(source)

    def profile(self, spec, document):
        source = self.directory / 'profile.json'
        source.write_text(json.dumps(document), encoding='utf-8')
        return constraints.load(source, spec.schema)


class WitnessContractTest(_WitnessCase):
    def test_spec_hashes_bind_exact_declaration_and_typed_schema(self):
        spec = self.spec({'stock': {'columns': [
            {'name': 'qty', 'type': 'numeric(6,3)', 'values': ['0.0005', '0.00050', None]},
            {'name': 'day', 'type': 'date', 'values': ['2026-10-21']}], 'rows': [0, 2]}})
        self.assertEqual(spec.schema['stock'], (('qty', 'numeric(6,3)'), ('day', 'date')))
        witness.validate_fixture(spec, {'stock': [['.001', '2026-10-21']]})
        binding = witness.binding(spec, None)
        self.assertEqual(binding['mode'], 'constraint_witness')
        self.assertEqual(binding['spec_sha256'], spec.sha256)
        self.assertEqual(len(binding['schema_sha256']), 64)
        self.assertIsNone(binding['profile_sha256'])
        self.assertTrue(spec.unchanged())
        spec.source.write_bytes(spec.source.read_bytes() + b'\n')
        self.assertFalse(spec.unchanged())

    def test_replay_domain_validation_uses_stored_numeric_values_and_row_bounds(self):
        spec = self.spec({'stock': {'columns': [
            {'name': 'qty', 'type': 'numeric(6,3)', 'values': ['0.0005']}], 'rows': [1, 2]}})
        witness.validate_fixture(spec, {'stock': [['.00149']]})
        for fixture in ({'stock': []}, {'stock': [['.002']]}, {'stock': [['.001']] * 3},
                        {'stock': [[True]]}, {'other': [['.001']]}):
            with self.subTest(fixture=fixture), self.assertRaises(constraints.ConstraintViolation):
                witness.validate_fixture(spec, fixture)

    def test_profile_integrity_is_also_required_by_replay_validation(self):
        spec = self.spec()
        profile = self.profile(spec, {'version': 1, 'tables': {'part': {'primary_key': ['part_id']}}})
        with self.assertRaises(constraints.ConstraintViolation):
            witness.validate_fixture(spec, {'part': [[None]]}, profile)
        witness.validate_fixture(spec, {'part': [['A']]}, profile)
        self.assertEqual(witness.binding(spec, profile)['profile_sha256'], profile.sha256)

    def test_invalid_schema_domains_and_bounds_are_configuration_errors(self):
        base = {'version': 1, 'tables': {'part': {'columns': [
            {'name': 'part_id', 'type': 'text', 'values': ['A']}], 'rows': [1, 1]}}}
        invalid = [dict(base, version=True), dict(base, sql='SELECT 1'), dict(base, tables={})]
        mutations = [
            ('rows', [-1, 1]), ('rows', [2, 1]), ('rows', [0, 5]), ('rows', [False, 1]),
            ('rows', 1), ('columns', []),
            ('columns', [{'name': 'part_id', 'type': 'integer', 'values': [True]}]),
            ('columns', [{'name': 'part_id', 'type': 'text', 'values': []}]),
            ('columns', [{'name': 'part_id', 'type': 'text', 'values': list('123456789')}]),
            ('columns', [{'name': 'part_id', 'type': 'numeric(0,0)', 'values': ['0']}]),
            ('columns', [{'name': 'part_id', 'type': 'numeric(2,3)', 'values': ['0']}]),
            ('columns', [{'name': 'part_id', 'type': 'numeric(39,0)', 'values': ['0']}]),
            ('columns', [{'name': 'part_id', 'type': 'numeric(2,1)', 'values': ['9.99']}]),
            ('columns', [{'name': 'part_id', 'type': 'text; SELECT 1', 'values': ['A']}]),
            ('columns', [{'name': 'part_id;', 'type': 'text', 'values': ['A']}]),
            ('columns', [{'name': 'part_id', 'type': 'date', 'values': ['2026-02-30']}]),
            ('columns', [{'name': 'part_id', 'type': 'text', 'values': ['A'], 'sql': '1'}]),
            ('columns', [{'name': 'part_id', 'type': 'text', 'values': ['A']}] * 2),
        ]
        for field, value in mutations:
            document = copy.deepcopy(base)
            document['tables']['part'][field] = value
            invalid.append(document)
        invalid.append({'version': 1, 'tables': {'part;': base['tables']['part']}})
        invalid.append({'version': 1, 'tables': {'sxc_candidate': base['tables']['part']}})
        invalid.append({'version': 1, 'tables': {f'part_{n}': base['tables']['part'] for n in range(5)}})
        wide = {'columns': [{'name': f'c_{n}', 'type': 'text', 'values': ['A']} for n in range(8)],
                'rows': [4, 4]}
        invalid.append({'version': 1, 'tables': {f'part_{n}': wide for n in range(3)}})
        source = self.directory / 'invalid.json'
        for document in invalid:
            with self.subTest(document=document):
                source.write_text(json.dumps(document), encoding='utf-8')
                with self.assertRaises(StageError):
                    witness.load(source)

    def test_strict_json_duplicate_keys_nonfinite_and_file_limits(self):
        source = self.directory / 'invalid.json'
        for raw in ('{"version":1,"version":1,"tables":{}}',
                    '{"version":1,"tables":{"part":{"columns":NaN,"rows":[0,1]}}}',
                    ' ' * (256 * 1024 + 1)):
            with self.subTest(raw_length=len(raw)):
                source.write_text(raw, encoding='utf-8')
                with self.assertRaises(StageError):
                    witness.load(source)

    def test_missing_optional_solver_is_explicit_and_has_no_fixtures(self):
        spec = self.spec()
        with patch('semantic_crosscheck.witness.importlib.import_module', side_effect=ModuleNotFoundError):
            generated = witness.generate(spec, None, 2, 1000)
        self.assertEqual(generated.fixtures, ())
        self.assertEqual(generated.reason, 'solver_unavailable')
        self.assertEqual(generated.solver_checks, 0)

    def test_changed_declarations_never_emit_a_stale_fixture(self):
        spec = self.spec()
        spec.source.write_bytes(spec.source.read_bytes() + b'\n')
        self.assertEqual(witness.generate(spec, None, 2, 1000).reason, 'spec_changed')
        spec = self.spec()
        profile = self.profile(spec, {'version': 1, 'tables': {}})
        profile.source.write_bytes(profile.source.read_bytes() + b'\n')
        self.assertEqual(witness.generate(spec, profile, 2, 1000).reason, 'profile_changed')

    def test_generation_limits_are_checked_even_without_optional_solver(self):
        spec = self.spec()
        for maximum, timeout in ((0, 1000), (1001, 1000), (True, 1000), (2, 0), (2, True)):
            with self.subTest(maximum=maximum, timeout=timeout), self.assertRaises(StageError):
                witness.generate(spec, None, maximum, timeout)


@unittest.skipUnless(HAS_Z3, 'optional z3-solver is not installed')
class WitnessSolverTest(_WitnessCase):
    def test_models_are_distinct_deterministic_and_never_claim_query_proof(self):
        spec = self.spec()
        first = witness.generate(spec, None, 10, 5000)
        second = witness.generate(spec, None, 10, 5000)
        self.assertEqual(first.fixtures, ({'part': [[None]]}, {'part': [['A']]}, {'part': [['B']]}))
        self.assertEqual(first.fixtures, second.fixtures)
        self.assertEqual(first.reason, 'space_exhausted')
        limited = witness.generate(spec, None, 2, 5000)
        self.assertEqual(limited.fixtures, first.fixtures[:2])
        self.assertEqual(limited.reason, 'example_limit')

    def test_row_count_range_has_one_empty_fixture_and_no_inactive_cell_duplicates(self):
        spec = self.spec({'part': {'columns': [
            {'name': 'part_id', 'type': 'text', 'values': ['A', 'B']}], 'rows': [0, 1]}})
        generated = witness.generate(spec, None, 10, 5000)
        self.assertEqual(generated.fixtures, ({'part': []}, {'part': [['A']]}, {'part': [['B']]}))
        self.assertEqual(generated.reason, 'space_exhausted')

    def test_impossible_primary_key_is_bounded_unsat_and_null_unique_is_allowed(self):
        spec = self.spec({'part': {'columns': [
            {'name': 'part_id', 'type': 'text', 'values': [None, 'A']}], 'rows': [2, 2]}})
        profile = self.profile(spec, {'version': 1, 'tables': {'part': {'primary_key': ['part_id']}}})
        generated = witness.generate(spec, profile, 10, 5000)
        self.assertEqual((generated.fixtures, generated.reason), ((), 'unsatisfiable'))
        profile = self.profile(spec, {'version': 1, 'tables': {'part': {'unique': [['part_id']]}}})
        generated = witness.generate(spec, profile, 10, 5000)
        self.assertEqual(generated.fixtures, ({'part': [[None], [None]]}, {'part': [[None], ['A']]},
                                             {'part': [['A'], [None]]}))

    def test_numeric_cast_dedup_and_sql_check_unknown(self):
        spec = self.spec({'stock': {'columns': [
            {'name': 'qty', 'type': 'numeric(6,3)', 'values': ['0.00049', '0.0005', '0.00050', None]}],
            'rows': [1, 1]}})
        profile = self.profile(spec, {'version': 1, 'tables': {}, 'checks': [
            {'table': 'stock', 'column': 'qty', 'op': 'gt', 'value': '0.0001'}]})
        generated = witness.generate(spec, profile, 10, 5000)
        self.assertEqual(generated.fixtures, ({'stock': [['0.001']]}, {'stock': [[None]]}))
        profile = self.profile(spec, {'version': 1, 'tables': {'stock': {'not_null': ['qty']}}, 'checks': [
            {'table': 'stock', 'column': 'qty', 'op': 'gt', 'value': '0.0001'}]})
        self.assertEqual(witness.generate(spec, profile, 10, 5000).fixtures, ({'stock': [['0.001']]},))

    def test_foreign_keys_link_domain_values_instead_of_unrelated_domain_indices(self):
        spec = self.spec({
            'part': {'columns': [{'name': 'part_id', 'type': 'text', 'values': ['A', 'B']}], 'rows': [1, 1]},
            'stock': {'columns': [{'name': 'part_id', 'type': 'text', 'values': ['B']}], 'rows': [1, 1]}})
        profile = self.profile(spec, {'version': 1, 'tables': {'part': {'primary_key': ['part_id']}},
                                     'foreign_keys': [{'table': 'stock', 'columns': ['part_id'],
                                                       'references': {'table': 'part', 'columns': ['part_id']}}]})
        self.assertEqual(witness.generate(spec, profile, 10, 5000).fixtures,
                         ({'part': [['B']], 'stock': [['B']]},))

    def test_composite_foreign_key_match_simple_and_no_parent_orphans(self):
        tables = {
            'part': {'columns': [{'name': 'part_id', 'type': 'text', 'values': ['A']},
                                 {'name': 'unit', 'type': 'text', 'values': ['ea']}], 'rows': [0, 0]},
            'stock': {'columns': [{'name': 'part_id', 'type': 'text', 'values': ['A']},
                                  {'name': 'unit', 'type': 'text', 'values': [None, 'ea']}], 'rows': [1, 1]}}
        spec = self.spec(tables)
        profile = self.profile(spec, {'version': 1, 'tables': {'part': {'unique': [['part_id', 'unit']]}},
                                     'foreign_keys': [{'table': 'stock', 'columns': ['part_id', 'unit'],
                                                       'references': {'table': 'part', 'columns': ['part_id', 'unit']}}]})
        self.assertEqual(witness.generate(spec, profile, 10, 5000).fixtures,
                         ({'part': [], 'stock': [['A', None]]},))

    def test_all_check_operators_and_dates_share_existing_cast_rules(self):
        for operation, literal, wanted in [
            ('eq', 2, [2]), ('ne', 2, [1, 3]), ('lt', 2, [1]), ('le', 2, [1, 2]),
            ('gt', 2, [3]), ('ge', 2, [2, 3]), ('in', [1, 3], [1, 3])]:
            with self.subTest(operation=operation):
                spec = self.spec({'part': {'columns': [
                    {'name': 'part_id', 'type': 'integer', 'values': [1, 2, 3]}], 'rows': [1, 1]}})
                profile = self.profile(spec, {'version': 1, 'tables': {}, 'checks': [
                    {'table': 'part', 'column': 'part_id', 'op': operation, 'value': literal}]})
                self.assertEqual(witness.generate(spec, profile, 10, 5000).fixtures,
                                 tuple({'part': [[value]]} for value in wanted))
        spec = self.spec({'sales_order': {'columns': [
            {'name': 'day', 'type': 'date', 'values': ['2026-10-20', '2026-10-21']}], 'rows': [1, 1]}})
        profile = self.profile(spec, {'version': 1, 'tables': {}, 'checks': [
            {'table': 'sales_order', 'column': 'day', 'op': 'ge', 'value': '2026-10-21'}]})
        self.assertEqual(witness.generate(spec, profile, 10, 5000).fixtures,
                         ({'sales_order': [['2026-10-21']]},))

    def test_model_integrity_is_revalidated_before_emission(self):
        spec = self.spec()
        with patch.object(witness, 'validate_fixture', side_effect=constraints.ConstraintViolation('invalid')):
            generated = witness.generate(spec, None, 2, 5000)
        self.assertEqual((generated.fixtures, generated.reason), ((), 'model_validation_failed'))

    def test_elapsed_budget_is_explicit_and_emits_no_unchecked_model(self):
        spec = self.spec()
        with patch('semantic_crosscheck.witness.time.perf_counter', side_effect=[0, 100, 100, 100]):
            generated = witness.generate(spec, None, 2, 1)
        self.assertEqual((generated.fixtures, generated.reason), ((), 'solver_timeout'))

    def test_solver_unknown_never_leaks_an_unchecked_model_or_a_success_reason(self):
        spec = self.spec()
        z3 = importlib.import_module('z3')
        with patch.object(z3.Solver, 'check', return_value=z3.unknown), \
             patch.object(z3.Solver, 'reason_unknown', return_value='incomplete'):
            generated = witness.generate(spec, None, 2, 5000)
        self.assertEqual((generated.fixtures, generated.reason), ((), 'solver_unknown'))
        self.assertEqual(generated.solver_checks, 1)

    def test_profile_change_while_generating_discards_all_models(self):
        spec = self.spec()
        profile = self.profile(spec, {'version': 1, 'tables': {}})
        original_validate = witness.validate_fixture

        def validate_and_change(*args):
            original_validate(*args)
            profile.source.write_bytes(profile.source.read_bytes() + b'\n')

        with patch.object(witness, 'validate_fixture', side_effect=validate_and_change):
            generated = witness.generate(spec, profile, 2, 5000)
        self.assertEqual((generated.fixtures, generated.reason), ((), 'profile_changed'))

    def test_constraint_compilation_stops_at_the_same_wall_clock_budget(self):
        spec = self.spec({'part': {'columns': [
            {'name': 'part_id', 'type': 'integer', 'values': list(range(8))}], 'rows': [4, 4]}})
        profile = self.profile(spec, {'version': 1, 'tables': {}, 'checks': [
            {'table': 'part', 'column': 'part_id', 'op': 'ge', 'value': 0}] * 128})
        elapsed = [0]
        original_matches = witness._matches

        def timed_comparison(*args):
            elapsed[0] += .01
            return original_matches(*args)

        with patch('semantic_crosscheck.witness.time.perf_counter', side_effect=lambda: elapsed[0]), \
             patch.object(witness, '_matches', side_effect=timed_comparison):
            generated = witness.generate(spec, profile, 2, 50)
        self.assertEqual((generated.fixtures, generated.reason), ((), 'solver_timeout'))
        self.assertLessEqual(elapsed[0], .081, 'compilation continued after a completed rule exhausted its budget')


if __name__ == '__main__':
    unittest.main()
