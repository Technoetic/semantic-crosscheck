"""Bounded NoREC counts and explicit oracle/replay identity."""
import copy
import json
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import pgfixture  # noqa: F401
import sxc
from semantic_crosscheck import tlp


class NoRecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.path = self.out / 'spec.json'
        self.path.write_text(json.dumps(dict(schema_version=1, scenario='nulls',
            relation_sql='SELECT qty FROM stock', predicate_sql='qty > 0',
            aggregate={'function': 'count', 'column': '*'})), encoding='utf-8')
        self.spec = tlp.load_spec(self.path)

    def args(self, **changes):
        value = dict(spec=str(self.path), oracle='norec', seed=7, max_examples=2,
                     statement_timeout_ms=1000, dsn_env='SXC_TLP_DSN', replay=None)
        return SimpleNamespace(**dict(value, **changes))

    def payload(self):
        return {'baseline': [{'qty': None, '__sxc_tlp_p': None}, {'qty': 1, '__sxc_tlp_p': True}],
                'optimized_count': 1, 'reference_count': 1}

    def test_cli_oracle_selection_preserves_default(self):
        parser = sxc.parser()
        self.assertEqual(parser.parse_args(['tlp', '--spec', 'spec.json']).oracle, 'tlp')
        self.assertEqual(parser.parse_args(['tlp', '--spec', 'spec.json', '--oracle', 'norec']).oracle, 'norec')

    def test_exact_counts_and_malformed_or_incomplete_payloads(self):
        self.assertTrue(tlp.compare_norec(self.payload())['equal'])
        wrong = dict(self.payload(), reference_count=0)
        self.assertFalse(tlp.compare_norec(wrong)['equal'])
        for key in ('optimized_count', 'reference_count'):
            for value in (None, True, -1, 3, 1.0, Decimal('1'), '1'):
                with self.subTest(key=key, type=type(value).__name__), self.assertRaises(ValueError):
                    tlp.compare_norec(dict(self.payload(), **{key: value}))
        for payload in ({}, dict(self.payload(), extra=1), {'baseline': [], 'optimized_count': 0},
                        dict(self.payload(), baseline=[{'qty': 1}]),
                        dict(self.payload(), baseline=self.payload()['baseline'] * 10001)):
            with self.subTest(keys=list(payload)), self.assertRaises(ValueError):
                tlp.compare_norec(payload)

    def test_empty_relation_counts_zero(self):
        self.assertTrue(tlp.compare_norec({'baseline': [], 'optimized_count': 0, 'reference_count': 0})['equal'])

    def test_count_only_and_unknown_oracle_are_rejected_before_connecting(self):
        for aggregate in ({'function': 'sum', 'column': 'qty'}, {'function': 'count', 'column': 'qty'}):
            spec = dict(self.spec, aggregate=aggregate)
            spec.pop('spec_sha256')
            self.path.write_text(json.dumps(spec), encoding='utf-8')
            with mock.patch.object(tlp.db, 'connect') as connect, self.assertRaises(tlp.StageError):
                tlp.run(None, self.args(), self.out)
            connect.assert_not_called()
        with mock.patch.object(tlp.db, 'connect') as connect, self.assertRaises(tlp.StageError):
            tlp.run(None, self.args(oracle='unknown'), self.out)
        connect.assert_not_called()

    def test_replay_cannot_switch_oracles_and_legacy_tlp_is_preserved(self):
        legacy = tlp.make_replay(self.spec, 7, 0)
        norec = tlp.make_replay(self.spec, 7, 0, oracle='norec')
        self.assertEqual(legacy['format'], 'sxc-synthetic-tlp')
        self.assertNotIn('oracle', legacy)
        self.assertEqual(norec['format'], 'sxc-synthetic-norec')
        self.assertEqual(norec['oracle'], 'norec')
        self.assertEqual(tlp.decode_replay(json.dumps(norec), self.spec, 7, oracle='norec'), norec)
        for payload, oracle in ((legacy, 'norec'), (norec, 'tlp')):
            with self.subTest(oracle=oracle), self.assertRaises(tlp.StageError):
                tlp.decode_replay(json.dumps(payload), self.spec, 7, oracle=oracle)

    def test_no_reused_materialized_predicate_in_oracle_arms(self):
        sql = tlp._norec_query(self.spec)
        self.assertIn('WHERE (qty > 0)', sql)
        self.assertIn('CASE WHEN (qty > 0)', sql)
        self.assertEqual(sql.count('(qty > 0)'), 3, 'independent diagnostic, WHERE and CASE evaluations')

    def test_stability_checks_rows_even_when_counts_agree(self):
        second = copy.deepcopy(self.payload())
        second['baseline'][1]['qty'] = 2
        with mock.patch.object(tlp, '_fetch', side_effect=[self.payload(), second]), \
                mock.patch.object(tlp.db, 'read_only') as tx, self.assertRaises(ValueError):
            tlp.evaluate(object(), {}, self.spec, 1000, oracle='norec')
        self.assertEqual(tx.call_args.kwargs['isolation'], 'repeatable read')

    def test_missing_engine_is_explicit_and_does_not_expose_dsn(self):
        with mock.patch.object(tlp.db, 'connect', side_effect=tlp.StageError('private-marker', 3)):
            result = tlp.run(None, self.args(), self.out)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.inputs['oracle'], 'norec')
        self.assertEqual(result.checks[0].detail['oracle'], 'norec')
        self.assertNotIn('private-marker', json.dumps(result.to_dict()))


if __name__ == '__main__':
    unittest.main()
