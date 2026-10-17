"""Integrity-domain boundaries for synthetic search, shrinking and replay."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - package path setup
import sxc
from semantic_crosscheck import counterexample as ce

try:
    constraints = importlib.import_module('semantic_crosscheck.constraints')
except ModuleNotFoundError:
    constraints = None


class ConstraintDomainTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(constraints, 'The strict synthetic constraint validator must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def profile(self, document):
        path = self.directory / 'constraints.json'
        path.write_text(json.dumps(document), encoding='utf-8')
        return constraints.load(path, ce.SCHEMA)

    def fixture(self):
        return {table: [] for table in ce.SCHEMA}

    def invalid(self, fixture, profile):
        with self.assertRaises(constraints.ConstraintViolation):
            constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_pk_implies_not_null_and_duplicate_keys_are_rejected(self):
        profile = self.profile({'version': 1, 'tables': {'part': {'primary_key': ['part_id']}}})
        fixture = self.fixture()
        fixture['part'] = [[None, None, None, None, None]]
        self.invalid(fixture, profile)
        fixture['part'] = [['A', None, None, None, None]] * 2
        self.invalid(fixture, profile)
        fixture['part'] = [['A', None, None, None, None], ['B', None, None, None, None]]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_unique_nulls_are_distinct_and_numbers_compare_by_sql_value(self):
        profile = self.profile({'version': 1, 'tables': {'stock': {'unique': [['part_id', 'qty']]}}})
        fixture = self.fixture()
        fixture['stock'] = [['A', 'L0', None], ['A', 'L1', None], [None, 'L2', '1.00'], [None, 'L3', '1']]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)
        fixture['stock'] += [['A', 'L4', '1.000'], ['A', 'L5', 1]]
        self.invalid(fixture, profile)

    def test_fk_match_simple_and_declared_referenced_key(self):
        profile = self.profile({'version': 1, 'tables': {'part': {'unique': [['part_id', 'unit']]}},
                                'foreign_keys': [{'table': 'stock', 'columns': ['part_id', 'location_id'],
                                                  'references': {'table': 'part', 'columns': ['part_id', 'unit']}}]})
        fixture = self.fixture()
        fixture['stock'] = [['orphan', None, '1']]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)
        fixture['stock'][0][1] = 'L0'
        self.invalid(fixture, profile)
        fixture['part'] = [['orphan', None, 'L0', None, None]]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_checks_are_typed_null_passes_and_not_null_overrides(self):
        document = {'version': 1, 'tables': {}, 'checks': [
            {'table': 'stock', 'column': 'qty', 'op': 'gt', 'value': '0.0001'},
            {'table': 'sales_order', 'column': 'due_date', 'op': 'ge', 'value': '2026-01-01'},
            {'table': 'part', 'column': 'unit', 'op': 'in', 'value': ['ea', 'kg']}]}
        profile = self.profile(document)
        fixture = self.fixture()
        fixture['stock'] = [['A', 'L0', None]]
        fixture['sales_order'] = [['o', 'A', None, None, None, '2026-01-01']]
        fixture['part'] = [['A', None, 'ea', None, None]]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)
        fixture['stock'][0][2] = '0.00049'  # numeric(18,3) stores zero
        self.invalid(fixture, profile)
        fixture['stock'][0][2] = '0.0005'  # numeric(18,3) stores .001
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)
        document['tables'] = {'stock': {'not_null': ['qty']}}
        fixture['stock'][0][2] = None
        self.invalid(fixture, self.profile(document))

    def test_all_check_operators_have_sql_value_semantics(self):
        for op, literal, valid, invalid in [
            ('eq', '1.00', '1', '2'), ('ne', '1', '2', '1.000'),
            ('lt', '2', '1', '2'), ('le', '2', '2', '3'),
            ('gt', '1', '2', '1'), ('ge', '1', '1', '0'),
            ('in', ['1.00', '2'], '1', '3'),
        ]:
            with self.subTest(op=op):
                profile = self.profile({'version': 1, 'tables': {}, 'checks': [
                    {'table': 'stock', 'column': 'qty', 'op': op, 'value': literal}]})
                fixture = self.fixture(); fixture['stock'] = [['A', 'L0', valid]]
                constraints.validate_fixture(fixture, ce.SCHEMA, profile)
                fixture['stock'][0][2] = invalid
                self.invalid(fixture, profile)

    def test_json_decimal_check_literal_keeps_precision_beyond_binary_float(self):
        path = self.directory / 'exact.json'
        path.write_text('{"version":1,"tables":{},"checks":[{"table":"stock","column":"qty",'
                        '"op":"gt","value":0.999999999999999999999999999999999999}]}', encoding='utf-8')
        profile = constraints.load(path, ce.SCHEMA)
        fixture = self.fixture(); fixture['stock'] = [['A', 'L0', '1.000']]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_numeric_rounding_overflow_dates_and_boolean_cells_fail_closed(self):
        profile = self.profile({'version': 1, 'tables': {}})
        for table, row in [
            ('stock', ['A', 'L', True]), ('stock', ['A', 'L', 'NaN']),
            ('stock', ['A', 'L', float('inf')]), ('stock', ['A', 'L', '999999999999999.9995']),
            ('bom_line', ['A', 'B', True, '1']), ('bom_line', ['A', 'B', '1.2', '1']),
            ('bom_line', ['A', 'B', 2147483648, '1']),
            ('sales_order', ['o', 'A', None, None, None, '2026-02-30']),
            ('part', [1, None, None, None, None]),
        ]:
            with self.subTest(table=table, row=row):
                fixture = self.fixture(); fixture[table] = [row]
                self.invalid(fixture, profile)
        fixture = self.fixture(); fixture['stock'] = [['A', 'L', '-0.0005']]
        constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_malformed_profiles_are_configuration_errors(self):
        documents = [
            {'version': True, 'tables': {}}, {'version': 2, 'tables': {}},
            {'version': 1, 'tables': {}, 'sql': 'SELECT 1'},
            {'version': 1, 'tables': {'other': {}}},
            {'version': 1, 'tables': {'stock': {'not_null': ['qty; DROP TABLE stock']}}},
            {'version': 1, 'tables': {'stock': {'primary_key': []}}},
            {'version': 1, 'tables': {'stock': {'unique': [['qty', 'qty']]}}},
            {'version': 1, 'tables': {}, 'foreign_keys': [
                {'table': 'stock', 'columns': ['part_id'], 'references': {'table': 'part', 'columns': ['part_id']}}]},
            {'version': 1, 'tables': {'part': {'primary_key': ['part_id']}}, 'foreign_keys': [
                {'table': 'stock', 'columns': ['qty'], 'references': {'table': 'part', 'columns': ['part_id']}}]},
        ]
        for value in [True, 'NaN', 'Infinity', None, {'sql': '1'}, ['1']]:
            documents.append({'version': 1, 'tables': {}, 'checks': [
                {'table': 'stock', 'column': 'qty', 'op': 'gt', 'value': value}]})
        documents += [{'version': 1, 'tables': {}, 'checks': [
            {'table': 'stock', 'column': 'qty', 'op': 'in', 'value': []}]}]
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(ce.StageError):
                    self.profile(document)

    def test_json_duplicate_keys_and_oversized_profiles_are_rejected(self):
        path = self.directory / 'bad.json'
        for data in ['{"version":1,"version":1,"tables":{}}',
                     '{"version":1,"tables":{},"checks":NaN}', ' ' * (256 * 1024 + 1)]:
            path.write_text(data, encoding='utf-8')
            with self.assertRaises(ce.StageError):
                constraints.load(path, ce.SCHEMA)

    def test_orphan_deletion_is_not_queried_or_cascaded_and_minimality_is_scoped(self):
        profile = self.profile({'version': 1, 'tables': {'part': {'primary_key': ['part_id']}},
                                'foreign_keys': [{'table': 'stock', 'columns': ['part_id'],
                                                  'references': {'table': 'part', 'columns': ['part_id']}}]})
        fixture = self.fixture()
        fixture['part'] = [['A', None, None, None, None]]
        fixture['stock'] = [['A', 'L0', '1']]
        seen = []
        def predicate(trial):
            seen.append(copy.deepcopy(trial))
            return bool(trial['stock'])
        reduced = ce.shrink(fixture, predicate, 30, profile=profile)
        self.assertEqual(reduced['tables'], fixture)
        self.assertEqual(len(seen), 1)  # only deletion of the stock row is admissible
        self.assertEqual(seen[0]['part'], fixture['part'])
        self.assertEqual(reduced['invalid_domain_trials'], 1)
        self.assertEqual(reduced['shrink_errors'], 0)
        self.assertTrue(reduced['deletion_1_minimal'])
        self.assertEqual(reduced['minimality_scope'], 'admissible_single_row_deletions')

    def test_invalid_initial_fixture_is_refused_without_a_predicate_call(self):
        profile = self.profile({'version': 1, 'tables': {'stock': {'not_null': ['qty']}}})
        fixture = self.fixture(); fixture['stock'] = [['A', 'L0', None]]
        with patch.object(ce, 'evaluate') as query:
            with self.assertRaises(constraints.ConstraintViolation):
                ce.shrink(fixture, query, 30, profile=profile)
            query.assert_not_called()

    def test_text_ordering_is_rejected_without_a_collation_contract(self):
        with self.assertRaises(ce.StageError):
            self.profile({'version': 1, 'tables': {}, 'checks': [
                {'table': 'part', 'column': 'unit', 'op': 'lt', 'value': 'z'}]})

    def test_only_constrained_text_ctes_have_explicit_c_collation(self):
        profile = self.profile({'version': 1, 'tables': {}})
        fixture = self.fixture(); fixture['part'] = [["A'quoted", None, None, None, None]]
        sql, params = ce.wrap_sql('SELECT part_id FROM part', fixture, profile=profile)
        self.assertIn('AS text) COLLATE "C"', sql)
        self.assertIn('CAST(NULL AS text) COLLATE "C"', sql)
        self.assertNotIn("A'quoted", sql)
        self.assertIn("A'quoted", params.values())
        legacy_sql, legacy_params = ce.wrap_sql('SELECT part_id FROM part', fixture)
        self.assertNotIn('COLLATE', legacy_sql)
        self.assertEqual(params, legacy_params)


class ConstraintSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        (self.path / 'reference.sql').write_text('SELECT 1 AS n', encoding='utf-8')
        (self.path / 'candidate.sql').write_text('SELECT 2 AS n', encoding='utf-8')

    def args(self, profile=None, **changes):
        constraint_path = None
        if profile is not None:
            constraint_path = str(self.path / 'profile.json')
            Path(constraint_path).write_text(json.dumps(profile), encoding='utf-8')
        values = dict(reference_sql=str(self.path / 'reference.sql'), candidate_sql=str(self.path / 'candidate.sql'),
                      scenario='nulls', seed=7, max_examples=2, max_shrink_steps=100, dsn_env='SXC_PG_DSN',
                      statement_timeout_ms=1000, replay=None, constraints=constraint_path)
        values.update(changes)
        return SimpleNamespace(**values)

    def execute(self, args, out=None, answer=None):
        different = {'equal': False, 'reference_rows': 1, 'candidate_rows': 1, 'column_names_match': True}
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(ce, 'evaluate', side_effect=answer, return_value=different):
            return ce.run(None, args, out or self.path)

    def test_cli_accepts_optional_profile(self):
        args = sxc.parser().parse_args(['counterexample', '--reference-sql', 'r.sql', '--candidate-sql', 'c.sql',
                                       '--constraints', 'domain.json'])
        self.assertEqual(args.constraints, 'domain.json')

    def test_invalid_profile_is_refused_before_any_connection(self):
        args = self.args({'version': 1, 'tables': {'arbitrary_table': {}}})
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError):
                ce.run(None, args, self.path)
            connect.assert_not_called()

    def test_missing_database_environment_preserves_configuration_error(self):
        args = self.args({'version': 1, 'tables': {}})
        with patch.object(ce.db, 'connect', side_effect=ce.StageError('test environment unavailable', 3)):
            with self.assertRaises(ce.StageError) as failure:
                ce.run(None, args, self.path)
        self.assertEqual(failure.exception.exit_code, 3)

    def test_invalid_generated_cases_consume_search_budget_without_queries_or_witness(self):
        args = self.args({'version': 1, 'tables': {'stock': {'not_null': ['qty']}}}, max_examples=3)
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(ce, 'evaluate') as evaluate:
            result = ce.run(None, args, self.path)
        self.assertEqual(result.status, 'not_run')
        detail = result.checks[0].detail
        self.assertEqual(detail['examples'], 0)
        self.assertEqual(detail['attempted_examples'], 3)
        self.assertEqual(detail['invalid_domain_examples'], 3)
        self.assertEqual(detail['reason'], 'no_admissible_generated_examples')
        evaluate.assert_not_called()
        self.assertFalse((self.path / 'counterexample/fixture.json').exists())

    def test_profile_replay_requires_exact_profile_and_rejects_each_mode_mismatch(self):
        profile = {'version': 1, 'tables': {'part': {'primary_key': ['part_id']}}}
        args = self.args(profile)
        self.assertEqual(self.execute(args).status, 'fail')
        artifact = self.path / 'counterexample/fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual(payload['constraints']['mode'], 'constrained')
        self.assertEqual(payload['constraints']['version'], 1)
        self.assertEqual(len(payload['constraints']['sha256']), 64)
        args.replay = str(artifact)
        self.assertEqual(self.execute(args, self.path / 'good-replay').status, 'fail')
        no_profile = copy.copy(args); no_profile.constraints = None
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError): ce.run(None, no_profile, self.path / 'bad-mode')
            connect.assert_not_called()
        Path(args.constraints).write_text(json.dumps(profile) + '\n', encoding='utf-8')
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError): ce.run(None, args, self.path / 'changed-profile')
            connect.assert_not_called()
        legacy = copy.deepcopy(payload); legacy.pop('constraints')
        legacy_path = self.path / 'legacy.json'; legacy_path.write_text(json.dumps(legacy), encoding='utf-8')
        args.replay = str(legacy_path)
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError): ce.run(None, args, self.path / 'legacy-with-profile')
            connect.assert_not_called()
        no_profile.replay = str(legacy_path)
        self.assertEqual(self.execute(no_profile, self.path / 'legacy-replay').status, 'fail')

    def test_profile_edit_during_sql_evaluation_never_saves_a_witness(self):
        args = self.args({'version': 1, 'tables': {}})
        def answer(*_args):
            Path(args.constraints).write_text('{"version":1,"tables":{}}\n', encoding='utf-8')
            return {'equal': False, 'reference_rows': 1, 'candidate_rows': 1, 'column_names_match': True}
        result = self.execute(args, answer=answer)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['reason'], 'constraint_profile_changed_during_run')
        self.assertFalse((self.path / 'counterexample/fixture.json').exists())

    def test_invalid_first_generated_case_is_skipped_before_valid_case_is_queried(self):
        args = self.args({'version': 1, 'tables': {'stock': {'not_null': ['qty']}}})
        invalid = ce.generate('nulls', 7, 0)
        valid = copy.deepcopy(invalid); valid['stock'][0][2] = '1'
        seen = []
        def answer(_connection, fixture, *_args):
            self.assertTrue(all(row[2] is not None for row in fixture['stock']))
            seen.append(copy.deepcopy(fixture))
            return {'equal': True, 'reference_rows': 0, 'candidate_rows': 0, 'column_names_match': True}
        with patch.object(ce, 'generate', side_effect=[invalid, valid]):
            result = self.execute(args, answer=answer)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['examples'], 1)
        self.assertEqual(result.checks[0].detail['invalid_domain_examples'], 1)
        self.assertEqual(result.checks[0].detail['attempted_examples'], 2)
        self.assertEqual(len(seen), 1)

    def test_new_unconstrained_binding_cannot_replay_with_a_profile(self):
        self.assertEqual(self.execute(self.args()).status, 'fail')
        args = self.args({'version': 1, 'tables': {}}, replay=str(self.path / 'counterexample/fixture.json'))
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError):
                ce.run(None, args, self.path / 'wrong-mode')
            connect.assert_not_called()

    def test_tampered_profile_binding_and_non_generated_fixture_fail_before_connection(self):
        args = self.args({'version': 1, 'tables': {}})
        self.assertEqual(self.execute(args).status, 'fail')
        payload = json.loads((self.path / 'counterexample/fixture.json').read_text(encoding='utf-8'))
        for change in ('hash', 'boolean_version', 'row'):
            tampered = copy.deepcopy(payload)
            if change == 'hash': tampered['constraints']['sha256'] = '0' * 64
            elif change == 'boolean_version': tampered['constraints']['version'] = True
            else: tampered['tables']['stock'].append(['not-generated', 'L0', '1'])
            replay = self.path / (change + '.json'); replay.write_text(json.dumps(tampered), encoding='utf-8')
            args.replay = str(replay)
            with self.subTest(change=change), patch.object(ce.db, 'connect') as connect:
                with self.assertRaises(ce.StageError): ce.run(None, args, self.path / ('bad-' + change))
                connect.assert_not_called()

    def test_legacy_search_records_unconstrained_scope(self):
        result = self.execute(self.args())
        payload = json.loads((self.path / 'counterexample/fixture.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['constraints']['mode'], 'unconstrained')
        self.assertTrue(any('unconstrained' in item.lower() for item in result.not_proven))


if __name__ == '__main__':
    unittest.main()
