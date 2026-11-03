"""V3 rejects omitted requested members while accepting explicitly present NULLs."""
import unittest
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck import mutate, shape_q
from test_compare_unit import FakeClient, demo_cfg


class MemberContractTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.pair = {'kind': 'pair', 'measures': ['a.value', 'b.value'], 'group_by': 'part.id',
                     'alone': {'a.value': 'a.key', 'b.value': 'b.key'}}
        self.answers = {(('part.id',), ('a.value', 'b.value')):
                        [{'part.id': None, 'a.value': None, 'b.value': '2'}],
                        (('a.key',), ('a.value',)): [{'a.key': None, 'a.value': None}],
                        (('b.key',), ('b.value',)): [{'b.key': None, 'b.value': '2'}]}

    def assert_invalid(self, result):
        status, detail = result
        self.assertEqual((status, detail.get('error'), detail.get('cube')),
                         ('error', 'MissingMember', 'invalid_response'))
        self.assertNotIn('SECRET', repr(detail))
        verdict, evidence = mutate.verdict(status, 'v3:pair')
        self.assertEqual(verdict, 'error')
        self.assertNotIn('killed_by', evidence)

    def test_total_absent_measure_is_error_but_explicit_null_passes(self):
        test = {'kind': 'total', 'members': ['a.value'], 'measure': next(iter(self.cfg.measures))}
        with mock.patch.object(shape_q, '_sql_value', return_value=None):
            self.assert_invalid(shape_q.run_test(None, FakeClient(answers={((), ('a.value',)): [{}]}), self.cfg, {}, test))
            self.assertEqual(shape_q.run_test(None, FakeClient(answers={((), ('a.value',)): [{'a.value': None}]}),
                                              self.cfg, {}, test)[0], 'pass')

    def test_additive_requires_dimension_and_measure_in_every_group(self):
        test = {'kind': 'additive', 'measure': 'a.value', 'dimension': 'part.class', 'agg': 'sum',
                'sql_null_group': (None, 0, 0)}
        base = {((), ('a.value',)): [{'a.value': None}],
                (('part.class',), ('a.value',)): [{'part.class': None, 'a.value': None, 'extra': 'SECRET'}]}
        self.assertEqual(shape_q.run_test(None, FakeClient(answers=base), self.cfg, {}, test)[0], 'pass')
        for missing in ('part.class', 'a.value'):
            with self.subTest(missing=missing):
                rows = {k: [dict(r) for r in v] for k, v in base.items()}
                del rows[(('part.class',), ('a.value',))][0][missing]
                self.assert_invalid(shape_q.run_test(None, FakeClient(answers=rows), self.cfg, {}, test))

    def test_pair_rejects_each_missing_member_in_together_and_alone_null_group(self):
        self.assertEqual(shape_q.run_test(None, FakeClient(answers=self.answers), self.cfg, {}, self.pair)[0], 'pass')
        for query, member in [((('part.id',), ('a.value', 'b.value')), 'part.id'),
                              ((('part.id',), ('a.value', 'b.value')), 'a.value'),
                              ((('a.key',), ('a.value',)), 'a.key'),
                              ((('a.key',), ('a.value',)), 'a.value')]:
            with self.subTest(query=query, member=member):
                answers = {k: [dict(r) for r in v] for k, v in self.answers.items()}
                del answers[query][0][member]
                self.assert_invalid(shape_q.run_test(None, FakeClient(answers=answers), self.cfg, {}, self.pair))

    def test_chunked_null_group_missing_member_is_error_after_completed_chunk(self):
        class Client:
            def __init__(self, omit):
                self.omit = omit

            def load(self, query):
                dim, = query['dimensions']
                null_group = query['filters'][0]['operator'] == 'notSet'
                row = {dim: None if null_group else 'SECRET', **{m: None for m in query['measures']}}
                if null_group and self.omit and len(query['measures']) > 1:
                    del row[self.omit]
                return [row]

        self.assertEqual(shape_q._run_chunks(Client(None), self.pair, 1, ['SECRET'], 0)[0], 'pass')
        for missing in ('part.id', 'a.value'):
            with self.subTest(missing=missing):
                result = shape_q._run_chunks(Client(missing), self.pair, 1, ['SECRET'], 0)
                self.assert_invalid(result)
                self.assertEqual(result[1]['chunks_compared'], 1)

    def test_chunk_and_null_replay_return_error_instead_of_kill(self):
        test = {**self.pair, 'replay_keys': ['SECRET'], 'sample_keys': ['SECRET'], 'total_keys': 1}
        answers = {k: [dict(r) for r in v] for k, v in self.answers.items()}
        del answers[(('part.id',), ('a.value', 'b.value'))][0]['a.value']
        for suffix, run in shape_q.replay_parts(test):
            with self.subTest(suffix=suffix):
                self.assert_invalid(run(FakeClient(answers=answers)))
        self.assert_invalid(shape_q.replay_test(None, FakeClient(answers=answers), self.cfg, {}, test))


if __name__ == '__main__':
    unittest.main()
