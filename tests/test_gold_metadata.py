import json
import unittest

import pgfixture  # adds scripts to sys.path
from semantic_crosscheck import compare
from test_compare_unit import FakeClient, FakeConn, FakePs, demo_cfg, gold_of

SECRET = 'SECRET-ROW-VALUE'


class MetadataPs(FakePs):
    def run(self, **params):
        self.conn.calls.append(('execute', params))
        self.columns = self.conn.metadata
        return self.conn.data


class MetadataConn(FakeConn):
    def __init__(self, metadata, data):
        super().__init__()
        self.metadata, self.data = metadata, data

    def prepare(self, sql):
        self.calls.append(('prepare', sql))
        return MetadataPs(self)


def metadata(*names):
    return [{'name': name} for name in names]


class GoldMetadataTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.gold = gold_of(self.cfg, 'q04_depth_histogram')

    def assert_rejected(self, cols, rows):
        conn = MetadataConn(cols, rows)
        client = FakeClient(data=[{'has_component_closure.max_depth': 1,
                                   'has_component_closure.pairs': 3}] if rows else [])
        result = compare.run_binding(conn, client, self.cfg, self.gold, {})
        self.assertFalse(result['pass'])
        self.assertEqual(compare.status(result), 'error')
        self.assertEqual(result['side'], 'gold')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(client.queries, [])
        self.assertIn(('close', None), conn.calls)
        self.assertEqual(conn.statements()[-1], 'ROLLBACK')

    def test_duplicate_labels_cannot_overwrite_a_value_and_pass(self):
        self.assert_rejected(metadata('max_depth', 'pairs', 'pairs'), [(1, SECRET, 3)])

    def test_missing_mapped_column_with_zero_rows_is_error(self):
        self.assert_rejected(metadata('max_depth'), [])

    def test_invalid_metadata_is_error(self):
        for cols in (None, [], 'bad', [None], [{}], metadata('max_depth', ''),
                     metadata('max_depth', '  '), metadata('max_depth', 42)):
            with self.subTest(cols=cols):
                self.assert_rejected(cols, [])

    def test_wrong_row_width_is_error_without_exposing_values(self):
        for rows in ([(SECRET,)], [(1, 3, SECRET)], [None], ['bad']):
            with self.subTest(rows=rows):
                self.assert_rejected(metadata('max_depth', 'pairs'), rows)

    def test_valid_rows_allow_distinct_extra_column(self):
        conn = MetadataConn(metadata('max_depth', 'pairs', 'extra'), [(1, 3, 'x')])
        self.assertEqual(compare.gold_rows(conn, self.cfg, self.gold, {}),
                         [{'max_depth': 1, 'pairs': 3, 'extra': 'x'}])
        self.assertIn(('close', None), conn.calls)
        self.assertEqual(conn.statements()[-1], 'ROLLBACK')

    def test_valid_empty_result_passes(self):
        conn = MetadataConn(metadata('max_depth', 'pairs'), [])
        result = compare.run_binding(conn, FakeClient(), self.cfg, self.gold, {})
        self.assertTrue(result['pass'])
        self.assertTrue(result['empty'])
        self.assertIn(('close', None), conn.calls)
        self.assertEqual(conn.statements()[-1], 'ROLLBACK')


if __name__ == '__main__':
    unittest.main()
