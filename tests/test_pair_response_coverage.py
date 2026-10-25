"""Requested pair-test keys must be answered, including NULL-valued measures."""
import unittest

import pgfixture  # noqa: F401
from semantic_crosscheck import shape_q
from test_compare_unit import FakeClient


class PairResponseCoverageTest(unittest.TestCase):
    def pair(self, keys=None):
        return {'measures': ['a.value', 'b.value'], 'group_by': 'part.id',
                'alone': {'a.value': 'a.key', 'b.value': 'b.key'},
                'keys_from': ['public.part', 'id'], 'total_keys': 10000,
                'sample_keys': ['K0', 'K1'] if keys is None else keys}

    def answers(self):
        return {(('part.id',), ('a.value', 'b.value')):
                [{'part.id': k, 'a.value': None, 'b.value': '2'} for k in ('K0', 'K1')],
                (('a.key',), ('a.value',)): [{'a.key': k, 'a.value': None} for k in ('K0', 'K1')],
                (('b.key',), ('b.value',)): [{'b.key': k, 'b.value': '2'} for k in ('K0', 'K1')]}

    def run_pair(self, answers, test=None):
        return shape_q._run_pair(None, FakeClient(answers=answers), None, test or self.pair())

    def test_empty_sample_response_cannot_pass_or_claim_probability(self):
        status, detail = self.run_pair({key: [] for key in self.answers()})
        self.assertEqual(status, 'not_run')
        self.assertEqual(detail['reason'], 'keys_not_answered')
        self.assertEqual(detail['answered_keys'], 0)
        self.assertFalse({'d95_keys', 'd95_rate'} & set(detail))
        note = shape_q.sample_note(detail)
        self.assertNotIn('compared 2 of', note)
        self.assertNotIn('95%', note)

    def test_partial_response_cannot_pass(self):
        answers = {key: rows[:1] for key, rows in self.answers().items()}
        status, detail = self.run_pair(answers)
        self.assertEqual(status, 'not_run')
        self.assertEqual(detail['answered_keys'], 1)
        self.assertNotIn('d95_keys', detail)

    def test_missing_alone_null_value_cannot_masquerade_as_answer(self):
        answers = self.answers()
        answers[(('a.key',), ('a.value',))].pop()
        status, detail = self.run_pair(answers)
        self.assertEqual(status, 'not_run')
        self.assertEqual(detail['answered_keys'], 1)
        self.assertNotIn('d95_keys', detail)

    def test_known_mismatch_still_fails_but_incomplete_sample_has_no_bound(self):
        answers = {key: rows[:1] for key, rows in self.answers().items()}
        answers[(('b.key',), ('b.value',))][0]['b.value'] = '3'
        status, detail = self.run_pair(answers)
        self.assertEqual(status, 'fail')
        self.assertNotIn('d95_keys', detail)

    def test_complete_sample_retains_bound_and_canonical_key_comparison(self):
        test = self.pair([1, 2])
        test['kinds'] = {'part.id': 'number', 'a.key': 'number', 'b.key': 'number'}
        answers = self.answers()
        for rows in answers.values():
            for i, row in enumerate(rows):
                row[next(iter(row))] = str(i + 1)
        status, detail = self.run_pair(answers, test)
        self.assertEqual(status, 'pass')
        self.assertEqual(detail['answered_keys'], 2)
        self.assertIn('d95_keys', detail)

    def test_disjoint_missing_keys_are_counted_as_not_fully_answered(self):
        answers = self.answers()
        answers[(('a.key',), ('a.value',))] = answers[(('a.key',), ('a.value',))][:1]
        answers[(('b.key',), ('b.value',))] = answers[(('b.key',), ('b.value',))][1:]
        status, detail = self.run_pair(answers)
        self.assertIn(status, ('fail', 'not_run'))
        self.assertEqual(detail['answered_keys'], 0)

    def test_chunked_path_also_rejects_missing_alone_null_values(self):
        answers = self.answers()
        answers[(('a.key',), ('a.value',))].pop()
        detail = shape_q._compare(FakeClient(answers=answers), self.pair(), shape_q._equals(['K0', 'K1']), ['K0', 'K1'])
        self.assertEqual(detail['answered_keys'], 1)


if __name__ == '__main__':
    unittest.main()
