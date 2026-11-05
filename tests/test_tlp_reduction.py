"""Opt-in reduction preserves an observed discrepancy and explicit replay evidence."""
import copy
import importlib
import json
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401
from semantic_crosscheck import tlp

try:
    tools = importlib.import_module('semantic_crosscheck.tlp_reduction')
except ModuleNotFoundError:
    tools = None


def payload(rows=2, partition=1, aggregate=0, other=0, value=0):
    row = {'qty': value, '__sxc_tlp_p': False}
    return {'baseline': [dict(row) for _ in range(rows)],
            'partitioned': [dict(row) for _ in range(partition)],
            'baseline_aggregate': aggregate, 'partition_aggregate': other, 'aggregate_type': 'numeric'}


class TlpReductionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(tools, 'The opt-in TLP reduction adapter must exist')
        temporary = tempfile.TemporaryDirectory(prefix='sxc_tlp_reduction_')
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.spec_path = self.out / 'spec.json'
        self.spec_path.write_text(json.dumps({'schema_version': 1, 'scenario': 'nulls',
            'relation_sql': 'SELECT qty FROM stock', 'predicate_sql': 'qty > 0',
            'aggregate': {'function': 'sum', 'column': 'qty'}}), encoding='utf-8')
        self.spec = tlp.load_spec(self.spec_path)
        self.original = tlp.make_replay(self.spec, 7, 0)

    def args(self, **changes):
        value = dict(spec=str(self.spec_path), seed=7, max_examples=1, statement_timeout_ms=1000,
                     dsn_env='SXC_TLP_DSN', replay=None, oracle='tlp', shrink=True, max_shrink_steps=200)
        return SimpleNamespace(**dict(value, **changes))

    def evidence(self, fixture):
        # Fault injection of a duplicate-dropping partition; real bag comparison/signature.
        count = len(fixture['stock']) * 2
        raw = payload(count, 1 if count else 0)
        return tlp.compare(raw), tools.signature(raw, 'tlp')

    def run_stage(self, directory=None, **changes):
        with patch.object(tlp.db, 'connect') as connect, \
                patch.object(tlp, 'evaluate_evidence', side_effect=lambda _c, f, *_a, **_k: self.evidence(f)):
            return tlp.run(None, self.args(**changes), directory or self.out)

    def test_signature_preserves_signed_support_but_permits_duplicate_count_reduction(self):
        self.assertEqual(tools.signature(payload(6, 1), 'tlp'), tools.signature(payload(2, 1), 'tlp'))
        self.assertNotEqual(tools.signature(payload(2, 1), 'tlp'), tools.signature(payload(1, 2), 'tlp'))
        self.assertNotEqual(tools.signature(payload(2, 1, value=Decimal('1.000000000000000000001')), 'tlp'),
                            tools.signature(payload(2, 1, value=Decimal('1.000000000000000000002')), 'tlp'))
        self.assertNotEqual(tools.signature(payload(2, 1, aggregate=None), 'tlp'),
                            tools.signature(payload(2, 1, other=None), 'tlp'))
        self.assertIsNone(tools.signature(payload(1, 1), 'tlp'))

    def test_norec_signature_preserves_only_count_gap_direction(self):
        def raw(a, b):
            return {'baseline': payload(4)['baseline'], 'optimized_count': a, 'reference_count': b}
        self.assertEqual(tools.signature(raw(1, 3), 'norec'), tools.signature(raw(2, 3), 'norec'))
        self.assertNotEqual(tools.signature(raw(1, 3), 'norec'), tools.signature(raw(3, 1), 'norec'))
        self.assertIsNone(tools.signature(raw(2, 2), 'norec'))

    def test_norec_shrink_keeps_reduction_and_sql_scope_notes_when_engine_missing(self):
        spec = dict(self.spec, aggregate={'function': 'count', 'column': '*'})
        spec.pop('spec_sha256')
        self.spec_path.write_text(json.dumps(spec), encoding='utf-8')
        with patch.object(tlp.db, 'connect', side_effect=tlp.StageError('private-marker', 3)):
            result = tlp.run(None, self.args(oracle='norec'), self.out)
        self.assertEqual(result.status, 'not_run')
        notes = ' '.join(result.not_proven)
        self.assertIn('Reduction preserves only signed row-discrepancy support', notes)
        self.assertIn('not the same optimizer bug', notes)
        self.assertIn('synthetic CTEs and a read-only transaction are not a SQL sandbox', notes)
        self.assertIn('A bounded NoREC pass proves only predicate-count consistency', notes)
        self.assertNotIn('A bounded TLP pass', notes)
        self.assertNotIn('Each comparison freezes the relation and predicate', notes)

    def test_evidence_path_rejects_changing_partition_even_when_baseline_stable(self):
        original = payload(3, 1)
        changed = payload(3, 2)
        with patch.object(tlp.db, 'read_only'), patch.object(tlp, '_fetch', side_effect=[original, changed]):
            with self.assertRaises(ValueError):
                tlp.evaluate_evidence(object(), self.original['tables'], self.spec, 1000)
        with patch.object(tlp.db, 'read_only'), patch.object(tlp, '_fetch', side_effect=[original, original]):
            answer, signature = tlp.evaluate_evidence(object(), self.original['tables'], self.spec, 1000)
        self.assertFalse(answer['equal'])
        self.assertEqual(signature, tools.signature(original, 'tlp'))

    def test_reducer_never_accepts_error_or_different_signature(self):
        signature = self.evidence(self.original['tables'])[1]
        def failing(_fixture):
            raise ValueError('private-marker')
        summary = tools.reduce(self.original['tables'], failing, signature, 2)
        self.assertEqual(summary['accepted'], 0)
        self.assertEqual(summary['shrink_errors'], 2)
        self.assertFalse(summary['deletion_1_minimal'])
        other = tools.signature(payload(1, 2), 'tlp')
        summary = tools.reduce(self.original['tables'], lambda _f: (tlp.compare(payload(1, 2)), other), signature, 2)
        self.assertEqual(summary['accepted'], 0)

    def test_opt_in_run_reduces_and_replays_every_accepted_operation(self):
        result = self.run_stage()
        self.assertEqual(result.status, 'fail')
        artifact = self.out / 'tlp' / 'fixture.json'
        saved = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual(saved['schema_version'], 2)
        self.assertGreater(saved['search']['accepted'], 0)
        self.assertEqual(len(saved['tables']['stock']), 1)
        self.assertEqual(len(saved['tables']['part']), 0)
        self.assertEqual(saved['original'], self.original)
        self.assertNotIn('"tables"', json.dumps(result.to_dict()))
        replayed = self.run_stage(self.out / 'replayed', replay=str(artifact))
        self.assertEqual(replayed.status, 'fail')
        self.assertFalse(replayed.checks[0].detail['reduction']['deletion_1_minimal'])
        self.assertEqual(replayed.checks[0].detail['reduction']['scope'], 'accepted_trace_replay_only')

    def test_v2_tampering_and_switching_modes_are_rejected_before_connection(self):
        self.run_stage()
        path = self.out / 'tlp' / 'fixture.json'
        saved = json.loads(path.read_text())
        invalid = []
        for key, value in [('schema_version', True), ('oracle', 'norec'), ('budget', 201), ('extra', 1),
                           ('bindings', {}), ('signature', {'kind': 'private-marker'})]:
            invalid.append(dict(saved, **{key: value}))
        wrong = copy.deepcopy(saved); wrong['trace'][0]['index'] = True; invalid.append(wrong)
        wrong = copy.deepcopy(saved); wrong['tables']['stock'] = []; invalid.append(wrong)
        wrong = copy.deepcopy(saved); wrong['search']['accepted'] = True; invalid.append(wrong)
        for value in invalid:
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(value=value), patch.object(tlp.db, 'connect') as connect:
                with self.assertRaises(tlp.StageError):
                    tlp.run(None, self.args(replay=str(path)), self.out / 'invalid')
                connect.assert_not_called()
        path.write_text(json.dumps(saved), encoding='utf-8')
        with patch.object(tlp.db, 'connect') as connect, self.assertRaises(tlp.StageError):
            tlp.run(None, self.args(replay=str(path), shrink=False), self.out / 'disabled')
        connect.assert_not_called()

    def test_replay_rechecks_each_trace_step_and_final_signature(self):
        self.run_stage()
        path = self.out / 'tlp' / 'fixture.json'
        calls = []
        def changed(_conn, fixture, *_a, **_k):
            calls.append(copy.deepcopy(fixture))
            if len(calls) == 2:
                return tlp.compare(payload(1, 2)), tools.signature(payload(1, 2), 'tlp')
            return self.evidence(fixture)
        with patch.object(tlp.db, 'connect'), patch.object(tlp, 'evaluate_evidence', side_effect=changed):
            result = tlp.run(None, self.args(replay=str(path)), self.out / 'changed')
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.out / 'changed' / 'tlp' / 'fixture.json').exists())
        self.assertNotIn('private-marker', json.dumps(result.to_dict()))

    def test_final_recheck_source_change_and_budget_bound_cannot_create_evidence(self):
        for change in ({'max_shrink_steps': True}, {'max_shrink_steps': -1}, {'max_shrink_steps': 10001}):
            with self.subTest(change=change), patch.object(tlp.db, 'connect') as connect:
                with self.assertRaises(tlp.StageError):
                    tlp.run(None, self.args(**change), self.out)
                connect.assert_not_called()
        result = self.run_stage(max_shrink_steps=0)
        self.assertEqual(result.status, 'fail')
        self.assertTrue(result.checks[0].detail['reduction']['budget_exhausted'])
        def changed(_c, fixture, *_a, **_k):
            answer = self.evidence(fixture)
            self.spec_path.write_text(self.spec_path.read_text() + '\n', encoding='utf-8')
            return answer
        with patch.object(tlp.db, 'connect'), patch.object(tlp, 'evaluate_evidence', side_effect=changed):
            result = tlp.run(None, self.args(max_shrink_steps=0), self.out)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.out / 'tlp' / 'fixture.json').exists())


if __name__ == '__main__':
    unittest.main()
