"""Closed synthetic-plan experiment boundaries; SQL values never become evidence."""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401
from semantic_crosscheck.result import StageError

try:
    pc = importlib.import_module('semantic_crosscheck.plan_compare')
except ModuleNotFoundError:
    pc = None


def document(**changes):
    value = dict(schema_version=1, columns={'id': 'integer', 'x': 'integer'},
                 rows=[[i, i] for i in range(20)], projection=['id'],
                 predicate=[{'column': 'x', 'op': 'lt', 'value': {'type': 'integer', 'value': 5}}],
                 index='x', timezone='UTC')
    value.update(changes)
    return value


class Engine:
    """Small DB protocol double, only for cleanup/error paths unavailable in SQL."""
    def __init__(self, index_plan=True, answers=None, fail_at=None, callback=None):
        self.index_plan, self.answers = index_plan, answers or [[[1], [1]], [[1], [1]]]
        self.fail_at, self.callback = fail_at, callback
        self.calls, self.closed, self.profile, self.selected = [], False, 'sequential', 0

    def run(self, sql, **params):
        self.calls.append((sql, params))
        if self.callback:
            self.callback(sql)
        if self.fail_at and self.fail_at in sql:
            raise RuntimeError({'C': '57014', 'M': 'PRIVATE_VALUE SELECT password'})
        if sql == 'SET LOCAL enable_seqscan = off':
            self.profile = 'index'
        if sql.startswith('EXPLAIN'):
            node = 'Index Scan' if self.profile == 'index' and self.index_plan else 'Seq Scan'
            return [[[{'Plan': {'Node Type': node, 'Relation Name': 'sxc_plan_compare_data',
                                'Total Cost': self.selected, 'Filter': 'PRIVATE_VALUE'}}]]]
        if sql.startswith('SELECT ') and 'pg_temp.' in sql:
            answer = self.answers[self.selected]
            self.selected += 1
            return answer
        return []

    def close(self):
        self.closed = True


class PlanCompareTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(pc, 'The bounded plan comparison module must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_plan_compare_')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def spec(self, value=None, raw=None):
        path = self.directory / 'input.json'
        path.write_text(raw if raw is not None else json.dumps(value or document()), encoding='utf-8')
        return path

    def args(self, **changes):
        value = dict(spec=changes.get('spec') or str(self.spec()), dsn_env='SXC_TEST_ADMIN_DSN', statement_timeout_ms=1000, quiet=True)
        value.update(changes)
        return SimpleNamespace(**value)

    def test_closed_spec_rejects_sql_unknown_fields_duplicate_keys_and_invalid_types(self):
        bad = [document(schema_version=True), document(sql='SELECT 1'), document(columns={'x); DROP TABLE t': 'integer'}),
               document(columns={'id': 'jsonb'}), document(index='missing'), document(projection=['id', 'id']),
               document(rows=[[True, 1]]), document(rows=[{'id': 1}]), document(rows=[[1, 'private']]),
               document(predicate=[{'column': 'x', 'op': 'raw', 'value': {'type': 'integer', 'value': 1}}]),
               document(predicate=[{'column': 'x', 'op': 'is_null', 'value': {'type': 'integer', 'value': 1}}]),
               document(predicate=[{'column': 'x', 'op': 'lt', 'value': {'type': 'text', 'value': '1'}}]),
               document(predicate=[]), document(timezone="UTC'; SELECT 1"), document(max_rows=2001),
               document(rows=[[1, 1]] * 2001), document(operation_budget=True)]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(StageError):
                pc.load_spec(self.spec(item))
        for raw in ('{"schema_version":1,"schema_version":1}', ' ' * (256 * 1024 + 1),
                    '{"schema_version":NaN}', '{"schema_version":Infinity}'):
            with self.subTest(raw=raw[:50]), self.assertRaises(StageError):
                pc.load_spec(self.spec(raw=raw))

    def test_timestamp_cross_type_is_admitted_but_timezone_and_finite_values_are_checked(self):
        good = document(columns={'id': 'integer', 'x': 'timestamp'}, rows=[[1, '2026-03-08T03:05:00']],
                        predicate=[{'column': 'x', 'op': 'lt', 'value': {'type': 'timestamptz', 'value': '2026-03-08T07:30:00+00:00'}}])
        self.assertEqual(pc.load_spec(self.spec(good))['columns']['x'], 'timestamp')
        for kind, cell in [('timestamp', '2026-03-08T03:05:00+00:00'),
                           ('timestamptz', '2026-03-08T03:05:00'), ('numeric', 'NaN'),
                           ('numeric', '1e10000'), ('integer', 2**63), ('boolean', 1)]:
            value = document(columns={'id': 'integer', 'x': kind}, rows=[[1, cell]],
                             predicate=[{'column': 'x', 'op': 'is_not_null'}])
            with self.subTest(kind=kind, cell=cell), self.assertRaises(StageError):
                pc.load_spec(self.spec(value))

    def test_oversized_json_numeric_tokens_and_numeric_strings_are_sanitized_config_errors(self):
        value = document(columns={'id': 'integer', 'x': 'numeric'}, rows=[[1, 'NUMERIC_TOKEN']],
                         predicate=[{'column': 'x', 'op': 'is_not_null'}])
        raw = json.dumps(value)
        cases = ('1e9999999999999999999', '1e-9999999999999999999',
                 '9' * 129 + '.0', '0.' + '0' * 513 + '1', '9' * 129)
        for token in cases:
            for encoded in (token, json.dumps(token)):
                with self.subTest(token=token[:40], string=encoded.startswith('"')):
                    with self.assertRaises(StageError) as caught:
                        pc.load_spec(self.spec(raw=raw.replace('"NUMERIC_TOKEN"', encoded)))
                    self.assertEqual(caught.exception.exit_code, 2)
                    self.assertNotIn(token, str(caught.exception))
        for token, expected in [('1e512', '1E+512'), ('1e-512', '1E-512'),
                                ('0.001e+512', '1E+509'), ('9' * 128, '9' * 128)]:
            for encoded in (token, json.dumps(token)):
                with self.subTest(valid=token[:40], string=encoded.startswith('"')):
                    spec = pc.load_spec(self.spec(raw=raw.replace('"NUMERIC_TOKEN"', encoded)))
                    self.assertEqual(str(spec['rows'][0][1]), expected)

    def test_plan_fingerprint_ignores_estimates_filters_literals_but_preserves_physical_tree(self):
        base = {'Node Type': 'Seq Scan', 'Relation Name': 'sxc_plan_compare_data', 'Total Cost': 3,
                'Filter': "x = 'PRIVATE_VALUE'", 'Plan Rows': 2}
        changed = dict(base, **{'Total Cost': 300, 'Plan Rows': 400, 'Filter': "x = 'other'"})
        self.assertEqual(pc.plan_fingerprint(base), pc.plan_fingerprint(changed))
        self.assertNotEqual(pc.plan_fingerprint(base), pc.plan_fingerprint(dict(base, **{'Node Type': 'Index Scan'})))
        self.assertNotEqual(pc.plan_fingerprint(base), pc.plan_fingerprint({'Node Type': 'Aggregate', 'Plans': [base]}))

    def test_full_typed_bags_preserve_duplicates_null_and_exact_decimals(self):
        from decimal import Decimal
        self.assertTrue(pc.bags_equal([[None, Decimal('1.000')], [None, Decimal('1')]],
                                      [[None, Decimal('1')], [None, Decimal('1.0')]]))
        self.assertFalse(pc.bags_equal([[1], [1]], [[1]]))
        self.assertFalse(pc.bags_equal([[1]], [[True]]))
        self.assertFalse(pc.bags_equal([[Decimal('1.0000000000000000001')]], [[Decimal('1.0000000000000000002')]]))

    def test_real_select_and_binding_are_identical_under_observed_distinct_plans(self):
        engine = Engine()
        args = self.args()
        original = Path(args.spec).read_bytes()
        with patch.object(pc.db, 'connect', return_value=engine):
            result = pc.run(None, args, self.directory)
        self.assertEqual(result.status, 'pass')
        self.assertTrue(engine.closed)
        sql = [call for call in engine.calls if call[0].startswith('SELECT ') and 'pg_temp.' in call[0]]
        self.assertEqual(len(sql), 2)
        self.assertEqual(sql[0], sql[1])
        self.assertNotIn('WITH ', sql[0][0])
        self.assertIn('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY', [q for q, _ in engine.calls])
        self.assertEqual(Path(args.spec).read_bytes(), original)
        encoded = json.dumps(result.to_dict())
        for private in ('PRIVATE_VALUE', 'SELECT ', 'pg_temp', str(self.directory), 'postgresql://', 'Filter'):
            self.assertNotIn(private, encoded)
        self.assertNotEqual(result.checks[0].detail['sequential_plan_sha256'], result.checks[0].detail['index_plan_sha256'])

    def test_same_physical_plan_empty_answer_and_cap_exceed_never_pass(self):
        for engine, extra, reason in [(Engine(index_plan=False), {}, 'insufficient_plan_diversity'),
                                       (Engine(answers=[[], []]), {}, 'empty_answer'),
                                       (Engine(answers=[[[1], [2]], [[1]]]), {'max_rows': 1}, 'row_cap_exceeded')]:
            args = self.args(spec=str(self.spec(document(**extra))))
            with self.subTest(reason=reason), patch.object(pc.db, 'connect', return_value=engine):
                result = pc.run(None, args, self.directory)
                self.assertEqual(result.status, 'not_run')
                self.assertEqual(result.checks[0].detail['reason'], reason)
                self.assertTrue(engine.closed)

    def test_duplicate_difference_is_fail_without_any_truth_claim(self):
        engine = Engine(answers=[[[1], [1]], [[1]]])
        with patch.object(pc.db, 'connect', return_value=engine):
            result = pc.run(None, self.args(), self.directory)
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.checks[0].detail['sequential_rows'], 2)
        self.assertEqual(result.checks[0].detail['index_rows'], 1)

    def test_timeout_rolls_back_closes_and_sanitizes_values(self):
        engine = Engine(fail_at='EXPLAIN')
        with patch.object(pc.db, 'connect', return_value=engine):
            result = pc.run(None, self.args(), self.directory)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['sqlstate'], '57014')
        self.assertTrue(engine.closed)
        self.assertEqual(engine.calls[-1][0], 'ROLLBACK')
        self.assertNotIn('PRIVATE_VALUE', json.dumps(result.to_dict()))

    def test_budget_and_empty_fixture_never_connect_and_operational_config_is_rejected(self):
        for value, reason in [(document(operation_budget=1), 'operation_budget_exceeded'),
                              (document(rows=[]), 'empty_fixture')]:
            args = self.args(spec=str(self.spec(value)))
            with patch.object(pc.db, 'connect', side_effect=AssertionError('must not connect')):
                result = pc.run(None, args, self.directory)
            self.assertEqual(result.status, 'not_run')
            self.assertEqual(result.checks[0].detail['reason'], reason)
        with self.assertRaises(StageError):
            pc.run(object(), self.args(), self.directory)

    def test_changed_input_during_run_cannot_retain_verdict(self):
        args = self.args()
        def change(sql):
            if sql.startswith('EXPLAIN'):
                Path(args.spec).write_text(Path(args.spec).read_text() + ' ', encoding='utf-8')
        with patch.object(pc.db, 'connect', return_value=Engine(callback=change)):
            result = pc.run(None, args, self.directory)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['reason'], 'input_changed')

    def test_output_alias_and_hardlinks_are_rejected_without_mutation(self):
        directory = self.directory / 'output'
        stage = directory / 'plan_compare'
        stage.mkdir(parents=True)
        source = self.spec()
        original = source.read_bytes()
        for filename in ('result.json', 'result.json.part'):
            target = stage / filename
            target.write_bytes(original)
            with self.subTest(filename=filename), self.assertRaises(StageError):
                pc.prepare_run(self.args(spec=str(target)), directory)
            target.unlink()
        os.link(source, directory / '.gitignore')
        with self.assertRaises(StageError):
            pc.prepare_run(self.args(spec=str(source)), directory)
        self.assertEqual(source.read_bytes(), original)

    def test_explicit_unused_config_and_repeated_spec_paths_are_protected(self):
        directory = self.directory / 'output'
        stage = directory / 'plan_compare'
        stage.mkdir(parents=True)
        target = stage / 'result.json'
        target.write_text('private input', encoding='utf-8')
        for extra in ({'config': str(target)}, {'_plan_compare_inputs': (str(target),)}):
            with self.subTest(extra=extra), self.assertRaises(StageError):
                pc.prepare_run(self.args(**extra), directory)
        self.assertEqual(target.read_text(), 'private input')

    def test_connection_close_error_is_sanitized_and_cannot_keep_a_pass(self):
        engine = Engine()
        def close():
            raise RuntimeError('PRIVATE_VALUE postgresql://secret@private')
        engine.close = close
        with patch.object(pc.db, 'connect', return_value=engine):
            result = pc.run(None, self.args(), self.directory)
        self.assertEqual(result.status, 'error')
        self.assertNotIn('PRIVATE_VALUE', json.dumps(result.to_dict()))


if __name__ == '__main__':
    unittest.main()
