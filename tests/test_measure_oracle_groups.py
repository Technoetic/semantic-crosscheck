"""Grouped and UTC-window oracle regressions using independent synthetic rows."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_measure_oracle import CONFIG, config, document, install_cube, oracle


class GroupedMeasureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        path = self.directory / 'crosscheck.toml'
        path.write_text(CONFIG, encoding='utf-8')
        self.cfg = config.load(path)
        install_cube(self.cfg, self.directory)

    def load(self, **options):
        path = self.directory / 'measure.json'
        path.write_text(json.dumps(document(**options)), encoding='utf-8')
        return oracle.load_spec(path)

    def grouped(self, **options):
        defaults = dict(where=[], group_by='item',
                        cube_query={'measures': ['m_orders.value'], 'dimensions': ['m_orders.status']})
        return self.load(**(defaults | options))

    def compare(self, spec, types, rows, observed):
        query = spec['cube_query']
        @contextmanager
        def serve(cfg, directory):
            def load(request):
                self.assertEqual({k: v for k, v in request.items() if k != 'limit'}, query)
                if 'group_by' in spec:
                    self.assertEqual(request['limit'], oracle.MAX_GROUPS + 1)
                if isinstance(observed, Exception):
                    raise observed
                return observed
            yield SimpleNamespace(load=load)
        args = SimpleNamespace(spec=str(self.directory / 'measure.json'))
        with patch.object(oracle, '_source', return_value=oracle.evaluate(spec, types, rows)), \
                patch.object(oracle.cube_run, 'serve', side_effect=serve):
            return oracle.run(self.cfg, args, self.directory)

    def test_group_comparison_preserves_key_sets_per_item_sums_and_nulls(self):
        spec = self.grouped()
        types = {'item': 'text', 'qty': 'numeric'}
        rows = [{'item': '001', 'qty': Decimal('1.123456789012345678901234567891')},
                {'item': '1', 'qty': 3}, {'item': None, 'qty': None}, {'item': '001', 'qty': 7}]
        def record(k, v):
            return {'m_orders.status': k, 'm_orders.value': v}
        observed = [record(None, None), record('1', '3'), record('001', '8.123456789012345678901234567891')]
        for answer, status in [(list(reversed(observed)), 'pass'), (observed[1:], 'fail'),
                               ([*observed, record('EXTRA-SECRET', '0')], 'fail'),
                               ([observed[0], record('1', observed[2]['m_orders.value']), record('001', '3')], 'fail'),
                               ([*observed, observed[0]], 'error'),
                               ([record(None, False)], 'error'), ([record(1, '3')], 'error'),
                               ([dict(observed[0], extra='EXTRA-SECRET')], 'error')]:
            with self.subTest(status=status, count=len(answer)):
                result = self.compare(spec, types, rows, answer)
                self.assertEqual(result.status, status, result.to_dict())
                self.assertNotIn('EXTRA-SECRET', json.dumps(result.to_dict()))
                if answer == [*observed, observed[0]]:
                    self.assertEqual(result.checks[0].detail['reason'], 'duplicate_cube_group')
                if status == 'pass':
                    self.assertEqual(result.checks[0].detail['source_groups'], 3)
                    self.assertEqual(result.checks[0].detail['cube_rows'], 3)

    def test_empty_group_result_requires_a_valid_list_and_discloses_zero_coverage(self):
        spec = self.grouped()
        types = {'item': 'text', 'qty': 'numeric'}
        result = self.compare(spec, types, [], [])
        self.assertEqual(result.status, 'pass')
        self.assertTrue(any('empty' in note for note in result.not_proven))
        self.assertEqual(self.compare(spec, types, [], None).status, 'error')
        self.assertEqual(self.compare(spec, types, [], [{'m_orders.status': 'x', 'm_orders.value': '0'}]).status, 'fail')

    def test_group_key_catalog_kind_must_match_cube_even_for_empty_inputs(self):
        spec = self.grouped()
        result = self.compare(spec, {'item': 'integer', 'qty': 'numeric'}, [], [])
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'group_type_mismatch')

    def test_numeric_boolean_groups_follow_source_types_and_reject_duplicate_encodings(self):
        for kind, model_type, keys, observed in [
                ('integer', 'number', [99, 100], ['99', '100']),
                ('numeric', 'number', [Decimal('2.0000000000001'), Decimal('2.0000000000002')],
                 ['2.0000000000001', '2.0000000000002']),
                ('boolean', 'boolean', [True, False], ['true', False])]:
            with self.subTest(kind=kind):
                self.change_dimension(model_type)
                spec = self.grouped(aggregation='count')
                types = {'item': kind, 'qty': 'integer'}
                rows = [{'item': keys[0], 'qty': None}, {'item': keys[1], 'qty': 0}]
                items = [{'m_orders.status': val, 'm_orders.value': i} for i, val in enumerate(observed)]
                self.assertEqual(self.compare(spec, types, rows, items).status, 'pass')
                duplicate = {'m_orders.status': keys[0], 'm_orders.value': 0}
                self.assertEqual(self.compare(spec, types, rows, [*items, duplicate]).status, 'error')

    def change_dimension(self, kind, **attributes):
        path = self.directory / 'cube' / 'model.json'
        model = json.loads(path.read_text(encoding='utf-8'))
        model['cubes'][0]['dimensions'][0].update(type=kind, **attributes)
        path.write_text(json.dumps(model), encoding='utf-8')

    def test_time_window_uses_utc_half_open_days_and_excludes_null(self):
        query = {'measures': ['m_orders.value'], 'timeDimensions': [
            {'dimension': 'm_orders.status', 'dateRange': ['2026-01-01', '2026-01-31']}]}
        self.change_dimension('time')
        spec = self.load(where=[], time_window={'column': 'stamp', 'start': '2026-01-01', 'end': '2026-02-01'},
                         cube_query=query)
        east = timezone(timedelta(hours=9))
        for kind, timestamps in [
                ('date', [date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)]),
                ('timestamp', [datetime(2025, 12, 31, 23, 59, 59), datetime(2026, 1, 1),
                               datetime(2026, 1, 31, 23, 59, 59, 999999), datetime(2026, 2, 1)]),
                ('timestamptz', [datetime(2026, 1, 1, 8, tzinfo=east), datetime(2026, 1, 1, 9, tzinfo=east),
                                 datetime(2026, 2, 1, 8, 59, 59, 999999, tzinfo=east),
                                 datetime(2026, 2, 1, 9, tzinfo=east)])]:
            with self.subTest(kind=kind):
                rows = [{'stamp': stamp, 'qty': qty} for stamp, qty in zip([*timestamps, None], [1, 2, 4, 8, 16])]
                result = self.compare(spec, {'stamp': kind, 'qty': 'integer'}, rows, [{'m_orders.value': '6'}])
                self.assertEqual(result.status, 'pass', result.to_dict())
                self.assertEqual(result.checks[0].detail['qualifying_rows'], 2)
        for kind, bad in [('text', None), ('date', '2026-01-01'), ('timestamp', datetime.now(timezone.utc)),
                          ('timestamptz', datetime(2026, 1, 1))]:
            with self.subTest(kind=kind), self.assertRaises(oracle.NotRun):
                oracle.evaluate(spec, {'stamp': kind, 'qty': 'integer'}, [{'stamp': bad, 'qty': 1}])

    def test_group_window_contract_validates_shape_role_and_bounds(self):
        query = {'measures': ['m_orders.value']}
        window = {'column': 'stamp', 'start': '2026-01-01', 'end': '2026-02-01'}
        td = {'dimension': 'm_orders.status', 'dateRange': ['2026-01-01', '2026-01-31']}
        time_query = dict(query, timeDimensions=[td])
        for opts in [
                {'group_by': 'item', 'cube_query': query},
                {'cube_query': dict(query, dimensions=['m_orders.status'])},
                {'group_by': 'bad;column', 'cube_query': dict(query, dimensions=['m_orders.status'])},
                {'group_by': 'item', 'cube_query': dict(query, dimensions=['other.key'])},
                {'group_by': 'item', 'cube_query': dict(query, dimensions=['m_orders.value'])},
                {'time_window': window, 'cube_query': query}, {'cube_query': time_query},
                *[{'time_window': dict(window, start=bad), 'cube_query': time_query}
                  for bad in ['2026-02-01', '2025-02-29', '20260101', None]],
                {'time_window': window, 'cube_query': dict(query, timeDimensions=[dict(td, granularity='day')])},
                {'time_window': window, 'cube_query': dict(query, timeDimensions=[dict(td, dateRange=['today'])])}]:
            with self.subTest(opts=opts), self.assertRaises(oracle.StageError):
                self.load(**opts)
        self.change_dimension('string', public=False)
        spec = self.grouped()
        result = self.compare(spec, {'item': 'text', 'qty': 'integer'}, [], [])
        self.assertEqual(result.checks[0].detail['reason'], 'cube_member_missing')

    def test_group_bound_counts_all_keys_and_never_returns_partial_matches(self):
        spec = self.grouped()
        with patch.object(oracle, 'MAX_GROUPS', 1), self.assertRaises(oracle.NotRun) as error:
            oracle.evaluate(spec, {'item': 'text', 'qty': 'integer'},
                            [{'item': 'a', 'qty': 1}, {'item': 'b', 'qty': 2}])
        self.assertEqual(error.exception.reason, 'group_limit')
        for observed in (oracle.CubeRowLimitError('truncated'),
                         [{'m_orders.status': k, 'm_orders.value': 1} for k in ('a', 'b')]):
            with self.subTest(real_client_limit=isinstance(observed, Exception)), patch.object(oracle, 'MAX_GROUPS', 1):
                result = self.compare(spec, {'item': 'text', 'qty': 'integer'}, [{'item': 'a', 'qty': 1}], observed)
                self.assertEqual(result.status, 'not_run')
                self.assertEqual(result.checks[0].detail['reason'], 'group_limit')

    def test_composed_group_window_checks_only_qualifying_values_per_key(self):
        model_path = self.directory / 'cube/model.json'
        model = json.loads(model_path.read_text(encoding='utf-8'))
        model['cubes'][0]['dimensions'].append({'name': 'ts', 'type': 'time'})
        model_path.write_text(json.dumps(model), encoding='utf-8')
        query = {'dimensions': ['m_orders.status'], 'measures': ['m_orders.value'],
                 'timeDimensions': [{'dimension': 'm_orders.ts', 'dateRange': ['2026-03-01', '2026-03-01']}]}
        spec = self.grouped(time_window={'column': 'stamp', 'start': '2026-03-01', 'end': '2026-03-02'},
                            cube_query=query, where=[{'column': 'qty', 'operator': 'is_not_null'}])
        types = {'stamp': 'date', 'qty': 'integer', 'item': 'text'}
        rows = [{'stamp': date(2026, 3, 1), 'item': 'A', 'qty': 2},
                {'stamp': date(2026, 3, 1), 'item': 'B', 'qty': 3},
                {'stamp': date(2026, 3, 1), 'item': 'A', 'qty': 5},
                {'stamp': date(2026, 3, 1), 'item': 'C', 'qty': None},
                {'stamp': date(2026, 3, 2), 'item': 'B', 'qty': 10}]
        observed = [{'m_orders.status': 'B', 'm_orders.value': '3'}, {'m_orders.status': 'A', 'm_orders.value': '7'}]
        self.assertEqual(self.compare(spec, types, rows, observed).status, 'pass')
        observed[0]['m_orders.value'] = '13'
        result = self.compare(spec, types, rows, observed)
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.checks[0].detail['mismatched_groups'], 1)
