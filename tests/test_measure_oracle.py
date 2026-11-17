"""Independent raw-row measure expectations; Cube is the external boundary."""
import importlib
import json
import os
from contextlib import contextmanager
from decimal import Decimal, localcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401
from semantic_crosscheck import config
from semantic_crosscheck.result import Result, inputs_fingerprint

try:
    oracle = importlib.import_module('semantic_crosscheck.measure_oracle')
except ModuleNotFoundError:
    oracle = None


def document(**changes):
    value = dict(schema_version=1, entity='orders', column='qty', aggregation='sum',
                 where=[{'column': 'status', 'operator': 'eq', 'type': 'text', 'value': 'FIRM'}],
                 cube_query={'measures': ['m_orders.value']})
    value.update(changes)
    return value


CONFIG = '''version = 1
[source]
dsn_env = "SXC_PG_DSN"
owner_dsn_env = "SXC_PG_OWNER_DSN"
schemas = ["sxc_demo"]
output_schema = "sxc_out"
[entities.orders]
table = "sxc_demo.oracle_orders"
key = ["id"]
[measures.orders]
entity = "orders"
column = "qty"
agg = "sum"
by = "id"
filter = { status = ["DRAFT"] }
'''


def install_cube(cfg, directory):
    """Authored synthetic model, deliberately wrong shared configured filter."""
    stage = directory / 'cube'
    (stage / 'model').mkdir(parents=True, exist_ok=True)
    model = {'cubes': [{'name': 'm_orders', 'measures': [{'name': 'value', 'type': 'sum'}],
                         'dimensions': [{'name': 'status', 'type': 'string'}]}]}
    (stage / 'model.json').write_text(json.dumps(model), encoding='utf-8')
    (stage / 'model' / 'm_orders.yml').write_text('cubes: []\n', encoding='utf-8')
    receipt = Result('cube', inputs=inputs_fingerprint(cfg))
    receipt.add('cube:model', 'pass', cubes=1, members=2, public_members=2)
    receipt.add('cube:meta', 'pass', model_public=2, meta_members=2, missing=0, extra=0,
                cube_version=cfg.cube.version, sql_planner='tesseract')
    receipt.write(directory)


class MeasureOracleTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(oracle, 'The independent measure oracle must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_measure_oracle_')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.config_path = self.directory / 'crosscheck.toml'
        self.config_path.write_text(CONFIG, encoding='utf-8')
        self.cfg = config.load(self.config_path)

    def spec(self, value=None, raw=None):
        path = self.directory / 'spec.json'
        path.write_text(raw if raw is not None else json.dumps(value or document()), encoding='utf-8')
        return path

    def loaded(self, **changes):
        return oracle.load_spec(self.spec(document(**changes)))

    def test_authored_predicate_detects_shared_wrong_config_filter(self):
        answer = oracle.evaluate(self.loaded(), {'qty': 'numeric', 'status': 'text'},
                                 [{'qty': Decimal('13'), 'status': 'FIRM'},
                                  {'qty': Decimal('71'), 'status': 'DRAFT'}])
        self.assertFalse(oracle.compare_value(answer['value'], [{'m_orders.value': '71'}], 'm_orders.value'))
        self.assertEqual(answer['value'], Decimal('13'))
        self.assertEqual((answer['source_rows'], answer['qualifying_rows'], answer['non_null_values']), (2, 1, 1))

    def test_null_empty_count_and_exact_large_fractional_sum(self):
        cases = [('sum', [], None), ('count', [], 0),
                 ('sum', [{'qty': None}], None), ('count', [{'qty': None}, {'qty': 0}], 1)]
        for aggregation, rows, expected in cases:
            with self.subTest(aggregation=aggregation, rows=rows):
                answer = oracle.evaluate(self.loaded(aggregation=aggregation, where=[]), {'qty': 'numeric'}, rows)
                self.assertEqual(answer['value'], expected)
        with localcontext() as ctx:
            ctx.prec = 4
            answer = oracle.evaluate(self.loaded(where=[]), {'qty': 'numeric'},
                                     [{'qty': Decimal('123456789012345678901234567890.1')},
                                      {'qty': Decimal('0.000000000000000000000000000009')}])
        self.assertEqual(answer['value'], Decimal('123456789012345678901234567890.100000000000000000000000000009'))

    def test_predicates_keep_types_null_semantics_and_unmodified_text(self):
        rows = [{'qty': 2, 'flag': True, 'label': '01', 'n': 3},
                {'qty': 7, 'flag': False, 'label': '01', 'n': None},
                {'qty': 11, 'flag': True, 'label': '1', 'n': 3},
                {'qty': 19, 'flag': True, 'label': '01', 'n': 4}]
        where = [{'column': 'flag', 'operator': 'eq', 'type': 'boolean', 'value': True},
                 {'column': 'label', 'operator': 'in', 'type': 'text', 'values': ['01']},
                 {'column': 'n', 'operator': 'ne', 'type': 'integer', 'value': 4},
                 {'column': 'n', 'operator': 'is_not_null'}]
        answer = oracle.evaluate(self.loaded(where=where),
                                 {'qty': 'integer', 'flag': 'boolean', 'label': 'text', 'n': 'integer'}, rows)
        self.assertEqual(answer['value'], 2)
        self.assertEqual(oracle.evaluate(self.loaded(where=[{'column': 'n', 'operator': 'is_null'}]),
                                        {'qty': 'integer', 'n': 'integer'}, rows)['value'], 7)

    def test_budget_and_unsupported_values_never_return_partial_answer(self):
        cases = [({'qty': 'numeric'}, [{'qty': Decimal('NaN')}], 'unsupported_value'),
                 ({'qty': 'numeric'}, [{'qty': 1.5}], 'unsupported_value'),
                 ({'qty': 'double precision'}, [{'qty': None}], 'unsupported_type'),
                 ({'qty': 'text'}, [{'qty': '1'}], 'unsupported_aggregation_type'),
                 ({'qty': 'numeric'}, [{'qty': Decimal('1e4097')}], 'numeric_limit')]
        for types, rows, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(oracle.NotRun) as raised:
                oracle.evaluate(self.loaded(where=[]), types, rows)
            self.assertEqual(raised.exception.reason, reason)
        with self.assertRaises(oracle.NotRun) as raised:
            oracle.evaluate(self.loaded(where=[], max_rows=1), {'qty': 'numeric'}, [{'qty': 1}, {'qty': 2}])
        self.assertEqual(raised.exception.reason, 'row_limit')

    def test_catalog_text_requires_proven_deterministic_collation_before_python_equality(self):
        class Connection:
            def run(self, sql, **params):
                return None
            def close(self):
                pass
        for metadata in (False, None, 1, 'true', 'missing', True):
            with self.subTest(metadata=metadata):
                text = {'name': 'status', 'type': 'text', 'type_schema': 'pg_catalog'}
                if metadata != 'missing':
                    text['collation_deterministic'] = metadata
                catalog = [{'name': 'qty', 'type': 'numeric', 'type_schema': 'pg_catalog'}, text]
                rows = [{'qty': Decimal(13), 'status': 'FIRM'}]
                with patch.object(oracle.db, 'connect', return_value=Connection()), \
                        patch.object(oracle.db, 'rows', side_effect=[catalog, rows]):
                    if metadata is True:
                        self.assertEqual(oracle._source(self.cfg, self.loaded())['value'], Decimal(13))
                    else:
                        with self.assertRaises(oracle.NotRun) as raised:
                            oracle._source(self.cfg, self.loaded())
                        self.assertEqual(raised.exception.reason, 'unsafe_collation')
        catalog = [{'name': 'status', 'type': 'varchar', 'type_schema': 'pg_catalog',
                    'collation_deterministic': False}]
        with patch.object(oracle.db, 'connect', return_value=Connection()), \
                patch.object(oracle.db, 'rows', side_effect=[catalog, [{'status': 'FIRM'}]]):
            with self.assertRaises(oracle.NotRun) as raised:
                oracle._source(self.cfg, self.loaded(column='status', aggregation='count', where=[]))
        self.assertEqual(raised.exception.reason, 'unsafe_collation')

    def test_cube_numeric_scalar_is_exact_and_requires_one_complete_global_row(self):
        for value in ('100000000000000000000.0001', Decimal('100000000000000000000.0001')):
            self.assertTrue(oracle.compare_value(Decimal('100000000000000000000.0001'),
                                                 [{'m_orders.value': value}], 'm_orders.value'))
        self.assertTrue(oracle.compare_value(None, [{'m_orders.value': None}], 'm_orders.value'))
        self.assertFalse(oracle.compare_value(None, [{'m_orders.value': 0}], 'm_orders.value'))
        for rows in (None, {}, [], [{'other': '1'}], [{'m_orders.value': '1', 'extra': 2}],
                     [{'m_orders.value': '1'}, {'m_orders.value': '1'}],
                     [{'m_orders.value': True}], [{'m_orders.value': 0.1}],
                     [{'m_orders.value': 'NaN'}], [{'m_orders.value': '1e999999'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                oracle.compare_value(Decimal(1), rows, 'm_orders.value')

    def test_strict_spec_rejects_unknown_keys_coercions_duplicates_and_query_expansion(self):
        invalid = [document(schema_version=True), document(schema_version=2), document(extra=1),
                   document(entity='orders; DROP TABLE x'), document(column='*'),
                   document(aggregation='avg'), document(max_rows=50001), document(max_rows=True),
                   document(where=[{'column': 'status', 'operator': 'eq', 'type': 'text', 'value': None}]),
                   document(where=[{'column': 'qty', 'operator': 'eq', 'type': 'numeric', 'value': 0.1}]),
                   document(where=[{'column': 'qty', 'operator': 'eq', 'type': 'integer', 'value': True}]),
                   document(where=[{'column': 'qty', 'operator': 'is_null', 'value': None}]),
                   document(where=[{'column': 'qty', 'operator': 'gt', 'type': 'numeric', 'value': '1'}]),
                   document(cube_query={'measures': ['m_orders.value'], 'dimensions': []}),
                   document(cube_query={'measures': ['m_orders.value', 'm_orders.other']}),
                   document(cube_query={'measures': ['m_orders.value'], 'filters': [
                       {'member': 'other.status', 'operator': 'equals', 'values': ['FIRM']}]}),
                   document(cube_query={'measures': ['m_orders.value'], 'filters': [{'or': []}]}),
                   document(cube_query={'measures': ['m_orders.value'], 'limit': 1})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(oracle.StageError):
                oracle.load_spec(self.spec(value))
        for raw in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}', '{"value":1.1}'):
            with self.subTest(raw=raw), self.assertRaises(oracle.StageError):
                oracle.load_spec(self.spec(raw=raw))

    def test_missing_or_stale_prerequisite_and_malformed_spec_clear_old_pass_safely(self):
        path = self.spec()
        args = SimpleNamespace(spec=str(path))
        old = Result('measure_oracle')
        old.add('measure_oracle', 'pass')
        old.write(self.directory)
        result = oracle.run(self.cfg, args, self.directory)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.directory / 'measure_oracle' / 'result.json').exists())
        install_cube(self.cfg, self.directory)
        self.config_path.write_text(CONFIG + '\n', encoding='utf-8')
        result = oracle.run(config.load(self.config_path), args, self.directory)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['reason'], 'stale_cube')
        path.write_text('{"bad":"DO-NOT-LEAK"}', encoding='utf-8')
        result = oracle.run(self.cfg, args, self.directory)
        self.assertEqual(result.status, 'error')
        self.assertNotIn('DO-NOT-LEAK', json.dumps(result.to_dict()))

    def test_config_object_changes_cannot_authorize_different_raw_relation(self):
        args = SimpleNamespace(spec=str(self.spec()))
        install_cube(self.cfg, self.directory)
        self.cfg.entities['orders'].table = 'sxc_demo.other_orders'
        result = oracle.run(self.cfg, args, self.directory)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['reason'], 'configuration_changed')

    def test_prepare_rejects_every_output_alias_before_any_mutation(self):
        self.assertTrue(callable(getattr(oracle, 'prepare_run', None)), 'A read-only output guard must exist')
        stage = self.directory / 'measure_oracle'
        stage.mkdir()
        for target in (stage / 'result.json', stage / 'result.json.part', self.directory / '.gitignore'):
            with self.subTest(target=target.name):
                target.write_text('INPUT-MUST-SURVIVE', encoding='utf-8')
                args = SimpleNamespace(spec=str(target), config=str(self.config_path))
                with self.assertRaises(oracle.StageError):
                    oracle.prepare_run(args, self.directory)
                self.assertEqual(target.read_text(), 'INPUT-MUST-SURVIVE')
        args = SimpleNamespace(spec=str(self.spec()), config=str(stage / 'result.json'))
        with self.assertRaises(oracle.StageError):
            oracle.prepare_run(args, self.directory)

    def test_prepare_rejects_hardlinks(self):
        self.assertTrue(callable(getattr(oracle, 'prepare_run', None)), 'A read-only output guard must exist')
        spec = self.spec()
        linked = self.directory / 'hardlink.json'
        try:
            os.link(spec, linked)
        except OSError:
            self.skipTest('hard links are unavailable')
        with self.assertRaises(oracle.StageError):
            oracle.prepare_run(SimpleNamespace(spec=str(linked)), self.directory / 'new-output')

    def test_prepare_rejects_linked_containers(self):
        spec = self.spec()
        linked = self.directory / 'symlink'
        try:
            linked.symlink_to(self.directory, target_is_directory=True)
        except OSError:
            self.skipTest('symbolic links are unavailable')
        with self.assertRaises(oracle.StageError):
            oracle.prepare_run(SimpleNamespace(spec=str(spec)), linked / 'new-output')

    def test_new_attempt_clears_its_receipt_even_when_a_filter_contains_result_filename(self):
        self.config_path.write_text(CONFIG.replace('DRAFT', 'result.json'), encoding='utf-8')
        cfg = config.load(self.config_path)
        old = Result('measure_oracle')
        old.add('measure_oracle', 'pass')
        old.write(self.directory)
        args = SimpleNamespace(spec=str(self.spec(raw='{"bad":true}')))
        self.assertEqual(oracle.run(cfg, args, self.directory).status, 'error')
        self.assertFalse((self.directory / 'measure_oracle' / 'result.json').exists())

    def test_direct_failed_run_preserves_unrecognized_output_file(self):
        path = self.directory / 'measure_oracle' / 'result.json'
        path.parent.mkdir()
        path.write_bytes(b'UNRECOGNIZED-SYNTHETIC-OUTPUT')
        args = SimpleNamespace(spec=str(self.spec(raw='{"bad":true}')))
        self.assertEqual(oracle.run(self.cfg, args, self.directory).status, 'error')
        self.assertTrue(path.is_file(), 'A direct run may clear only a verified own receipt')
        self.assertEqual(path.read_bytes(), b'UNRECOGNIZED-SYNTHETIC-OUTPUT')

    def test_direct_run_protects_declared_gold_inputs_before_receipt_cleanup(self):
        old = Result('measure_oracle')
        old.add('measure_oracle', 'pass')
        path = old.write(self.directory)
        initial = path.read_bytes()
        self.cfg.gold = [SimpleNamespace(sql_path=lambda cfg: path)]
        with self.assertRaises(oracle.StageError):
            oracle.run(self.cfg, SimpleNamespace(spec=str(self.spec())), self.directory)
        self.assertEqual(path.read_bytes(), initial)

    def test_each_awaited_input_change_prevents_pass_without_requiring_database_service(self):
        # Source arithmetic is covered with explicit raw rows above and actual
        # PostgreSQL below; this test isolates the file/Cube await boundary.
        cfg_text = CONFIG.replace('[source]', '[source]\ndata_revision_sql_file = "revision.sql"') + '''
[[gold]]
id = "g"
sql = "gold.sql"
key = ["v"]
cube_query = { measures = ["m_orders.value"] }
map = { v = "m_orders.value" }
'''
        targets = [self.directory / 'spec.json', self.config_path,
                   self.directory / 'cube' / 'model' / 'm_orders.yml',
                   self.directory / 'cube' / 'model.json', self.directory / 'cube' / 'result.json',
                   self.directory / 'revision.sql', self.directory / 'gold.sql']
        for target in targets:
            with self.subTest(target=target.name):
                self.config_path.write_text(cfg_text, encoding='utf-8')
                (self.directory / 'revision.sql').write_text("SELECT 'synthetic-revision'", encoding='utf-8')
                (self.directory / 'gold.sql').write_text('SELECT 13 AS v', encoding='utf-8')
                cfg = config.load(self.config_path)
                install_cube(cfg, self.directory)
                args = SimpleNamespace(spec=str(self.spec()))
                @contextmanager
                def serve(cfg, run_dir):
                    class Client:
                        def load(inner, query):
                            target.write_bytes(target.read_bytes() + b'\n')
                            return [{'m_orders.value': '13'}]
                    yield Client()
                answer = {'value': Decimal(13), 'source_rows': 1, 'qualifying_rows': 1, 'non_null_values': 1}
                with patch.object(oracle, '_source', return_value=answer), \
                        patch.object(oracle.cube_run, 'serve', side_effect=serve):
                    result = oracle.run(cfg, args, self.directory)
                self.assertEqual(result.status, 'error')
                self.assertEqual(result.checks[0].detail['reason'], 'inputs_changed')

    def test_runtime_validation_covers_captured_inputs_after_return_for_match_and_mismatch(self):
        cfg_text = CONFIG.replace('[source]', '[source]\ndata_revision_sql_file = "revision.sql"') + '''
[[gold]]
id = "g"
sql = "gold.sql"
key = ["v"]
cube_query = { measures = ["m_orders.value"] }
map = { v = "m_orders.value" }
'''
        targets = [self.directory / 'spec.json', self.config_path,
                   self.directory / 'cube' / 'model' / 'm_orders.yml',
                   self.directory / 'cube' / 'model.json', self.directory / 'cube' / 'result.json',
                   self.directory / 'revision.sql', self.directory / 'gold.sql']
        for cube_value, expected_status in [('13', 'pass'), ('999', 'fail')]:
            for target in targets:
                with self.subTest(status=expected_status, target=target.name):
                    self.config_path.write_text(cfg_text, encoding='utf-8')
                    (self.directory / 'revision.sql').write_text("SELECT 'synthetic-revision'", encoding='utf-8')
                    (self.directory / 'gold.sql').write_text('SELECT 13 AS v', encoding='utf-8')
                    cfg = config.load(self.config_path)
                    install_cube(cfg, self.directory)
                    args = SimpleNamespace(spec=str(self.spec()))
                    @contextmanager
                    def serve(cfg, run_dir):
                        yield SimpleNamespace(load=lambda query: [{'m_orders.value': cube_value}])
                    answer = {'value': Decimal(13), 'source_rows': 1, 'qualifying_rows': 1, 'non_null_values': 1}
                    with patch.object(oracle, '_source', return_value=answer), \
                            patch.object(oracle.cube_run, 'serve', side_effect=serve):
                        result = oracle.run(cfg, args, self.directory)
                    self.assertEqual(result.status, expected_status)
                    callback = getattr(result, '_diagnostic_validate', None)
                    self.assertTrue(callable(callback), 'Result must retain a runtime publication freshness check')
                    self.assertIs(callback(), True)
                    self.assertNotIn('_diagnostic_validate', json.dumps(result.to_dict()))
                    target.write_bytes(target.read_bytes() + b'\n')
                    self.assertIs(callback(), False)


if __name__ == '__main__':
    unittest.main()
