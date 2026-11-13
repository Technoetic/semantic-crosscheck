"""Literal specifications catch duplicate loss, NULL conflation and aggregate rewrites."""

from collections import Counter
from decimal import Decimal
import importlib
import json
import unittest

import pgfixture

try:
    jf = importlib.import_module('semantic_crosscheck.join_aggregate_fixtures')
except ModuleNotFoundError:
    jf = None


def cases():
    path = pgfixture.ROOT / 'examples/join-aggregate-fixtures/cases.json'
    return json.loads(path.read_text(encoding='utf-8'))['cases']


def expected_bag(case):
    return Counter({tuple(item['row']): item['count'] for item in case['expected']})


def bag(rows):
    return Counter(tuple(row) for row in rows)


def evaluate(case):
    operation = case['operation']
    if operation == 'inner_join':
        return jf.inner_join(case['left'], case['right'])
    if operation in ('grouped', 'global'):
        return jf.aggregate(case['rows'], grouped=operation == 'grouped')
    if operation in ('join_grouped', 'join_global'):
        rows = [(key, left) for key, left, _ in jf.inner_join(case['left'], case['right'])]
        return jf.aggregate(rows, grouped=operation == 'join_grouped')
    raise AssertionError('unknown authored operation')


def incorrect_operation(case, operation):
    """Deliberately wrong variants, confined to tests rather than production APIs."""
    if operation.startswith('dedup_'):
        left, right = case['left'], case['right']
        if operation in ('dedup_left', 'dedup_both'):
            left = list(dict.fromkeys(map(tuple, left)))
        if operation in ('dedup_right', 'dedup_both'):
            right = list(dict.fromkeys(map(tuple, right)))
        return jf.inner_join(left, right)
    groups = {}
    for group, value in case['rows']:
        groups.setdefault(group, []).append(value)
    result = []
    for group, values in groups.items():
        present = [value for value in values if value is not None]
        rows_count = len(present) if operation == 'count_rows_to_nonnull' else len(values)
        value_count = len(values) if operation == 'count_nonnull_to_rows' else len(present)
        total = (max(present) if operation == 'sum_to_max' else sum(present)) if present else None
        result.append((group, rows_count, value_count, total))
    return result


class JoinAggregateFixturesTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(jf, 'The bounded join/aggregate reference arithmetic must exist')

    def test_operations_each_match_authored_literal_bags(self):
        for case in cases():
            with self.subTest(case=case['id']):
                self.assertEqual(bag(evaluate(case)), expected_bag(case))

    def test_wrong_operations_match_literal_witnesses_and_differ_from_correct(self):
        for case in cases():
            for mutant in case.get('mutants', []):
                with self.subTest(case=case['id'], mutant=mutant['operation']):
                    wrong = bag(incorrect_operation(case, mutant['operation']))
                    self.assertEqual(wrong, expected_bag(mutant))
                    self.assertNotEqual(wrong, expected_bag(case))

    def test_join_rejects_non_pairs_and_values_outside_the_bounded_domain(self):
        for rows in (None, 'A', iter([['A', 1]]), [[1]], [['A', 1, 2]], ['AB'],
                     [['A', True]], [[True, 1]], [['A', 1.0]], [['A', Decimal('1')]],
                     [['A', {}]], [['A', 'x' * 257]], [['A', 1000001]], [['A', -1000001]],
                     [['A', 1]] * 65):
            with self.subTest(rows=repr(rows)):
                for left, right in ((rows, []), ([], rows)):
                    with self.assertRaises(ValueError):
                        jf.inner_join(left, right)

    def test_join_rejects_incompatible_key_and_column_types_even_without_matches(self):
        for left, right in (([['1', 1]], [[1, 1]]),
                            ([['A', 1], [1, 1]], []),
                            ([['A', 1], ['B', 'one']], [])):
            with self.subTest(left=left, right=right):
                with self.assertRaises(ValueError):
                    jf.inner_join(left, right)

    def test_aggregate_rejects_noninteger_values_and_nonboolean_mode(self):
        for rows in ([[['g'], 1]], [['g', True]], [['g', '1']], [['g', 1.0]],
                     [['g', Decimal('1')]], [['g', 1000001]], [['g', -1000001]],
                     [[True, 1]], [['g', 1], [0, 2]], [['g', 1, 2]],
                     [['g', 1]] * 4097, None, iter([['g', 1]])):
            with self.subTest(rows=repr(rows)):
                with self.assertRaises(ValueError):
                    jf.aggregate(rows)
        for grouped in (None, 0, 1, 'false'):
            with self.subTest(grouped=grouped):
                with self.assertRaises(ValueError):
                    jf.aggregate([], grouped=grouped)

    def test_bounded_join_product_can_be_aggregated_without_losing_multiplicity(self):
        left = [['A', 1000000]] * 64
        right = [['A', None]] * 64
        joined = jf.inner_join(left, right)
        self.assertEqual(bag(joined), {('A', 1000000, None): 4096})
        self.assertEqual(jf.aggregate([(key, value) for key, value, _ in joined]),
                         [('A', 4096, 4096, 4096000000)])
        self.assertEqual(left, [['A', 1000000]] * 64)
        self.assertEqual(right, [['A', None]] * 64)


if __name__ == '__main__':
    unittest.main()
