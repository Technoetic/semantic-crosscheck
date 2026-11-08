"""Independent literal bags catch lost duplicates and confused difference semantics."""

from collections import Counter
import importlib
import json
import unittest

import pgfixture

try:
    pf = importlib.import_module('semantic_crosscheck.provenance_fixtures')
except ModuleNotFoundError:
    pf = None


EXAMPLE = pgfixture.ROOT / 'examples' / 'provenance-fixtures'


def cases():
    return json.loads((EXAMPLE / 'cases.json').read_text(encoding='utf-8'))['cases']


def expected_bag(case):
    # The counts are authored literals, never calculated by the reference operations.
    return Counter({tuple(item['row']): item['count'] for item in case['expected']})


def evaluate(case):
    operation = case['operation']
    left = case['left']
    if operation == 'bag':
        return pf.bag(left)
    if operation == 'distinct':
        return pf.distinct(left)
    if operation in ('except', 'except_all'):
        return pf.except_bag(left, case['right'], all_rows=operation == 'except_all')
    if operation == 'equal':
        return pf.select_equal(left, case['literal'])
    if operation == 'is_null':
        return pf.select_null(left)
    raise AssertionError('Unknown authored fixture operation')


class ProvenanceFixturesTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(pf, 'The bounded offline bag reference operations must exist')

    def test_reference_operations_match_independently_authored_bags(self):
        for case in cases():
            with self.subTest(case=case['id']):
                self.assertEqual(evaluate(case), expected_bag(case))

    def test_upstream_dedup_diff_separates_annotations_from_row_counts(self):
        left, right = [['P-A'], ['P-A']], [['P-A']]
        # Base annotations are 1: Dedup accumulates 2, then Nat monus gives 2 - 1.
        self.assertEqual(pf.dedup_diff_annotations(left, right), {('P-A',): 1})
        # SQL deduplication leaves one actual row; EXCEPT ALL removes that row.
        self.assertEqual(pf.except_bag([list(row) for row in pf.distinct(left)], right,
                                     all_rows=True), {})
        self.assertEqual(pf.except_bag(left, right), {})
        self.assertEqual(pf.except_bag(left, right, all_rows=True), {('P-A',): 1})

    def test_annotation_monus_saturates_and_only_tracks_left_dedup_slots(self):
        self.assertEqual(pf.dedup_diff_annotations([['P-A'], ['P-B']],
                                                 [['P-A'], ['P-A'], ['P-C']]),
                         {('P-A',): 0, ('P-B',): 1})
        self.assertEqual(pf.dedup_diff_annotations([], [['P-A']]), {})

    def test_one_column_text_scope_rejects_values_python_would_conflate(self):
        for rows in ([[1]], [[True]], [[]], [['P-A', 'P-B']], ['P-A']):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    pf.bag(rows)
        with self.assertRaises(ValueError):
            pf.select_equal([['P-A']], 1)
        with self.assertRaises(ValueError):
            pf.dedup_diff_annotations([[None]], [])

    def test_null_text_and_empty_text_are_distinct_bag_values(self):
        self.assertEqual(pf.bag([[None], ['NULL'], ['']]),
                         {(None,): 1, ('NULL',): 1, ('',): 1})


if __name__ == '__main__':
    unittest.main()
