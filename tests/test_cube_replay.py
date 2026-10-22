"""Synthetic Cube replay contracts; failing guards must never contact a database."""
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
from semantic_crosscheck import mutate
from semantic_crosscheck.result import StageError

try:
    replay = importlib.import_module('semantic_crosscheck.cube_replay')
except ModuleNotFoundError:
    replay = None


def arguments(**changes):
    values = dict(mutation_operator='measure_agg_to_max', seed=7,
                  dsn_env='SXC_CUBE_REPLAY_DSN', allow_scratch_schema=True,
                  statement_timeout_ms=10000, replay=None)
    values.update(changes)
    return SimpleNamespace(**values)


class CubeReplayTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(replay, 'Actual model-bound synthetic Cube replay is missing')

    def test_write_opt_in_and_operational_config_are_rejected_before_connection(self):
        for cfg, args in [(None, arguments(allow_scratch_schema=False)), (object(), arguments())]:
            with self.subTest(cfg=cfg is None), tempfile.TemporaryDirectory() as tmp:
                with patch.object(replay.db, 'connect', side_effect=AssertionError('forbidden connection')):
                    with self.assertRaises(StageError) as raised:
                        replay.run(cfg, args, tmp)
                self.assertEqual(raised.exception.exit_code, 2)

    def test_unsupported_operator_is_explicitly_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(replay.db, 'connect', side_effect=AssertionError('forbidden connection')):
                result = replay.run(None, arguments(mutation_operator='unknown_operator'), tmp)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'unsupported_operator')

    def test_missing_dedicated_dsn_cannot_fall_back_and_removes_stale_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'cube_replay' / 'fixture.json'
            artifact.parent.mkdir(); artifact.write_text('stale', encoding='utf-8')
            with patch.dict(os.environ, {'SXC_PG_DSN': 'postgresql://secret@private.invalid/private_database_sentinel'}):
                os.environ.pop('SXC_CUBE_REPLAY_DSN', None)
                result = replay.run(None, arguments(), tmp)
            self.assertFalse(artifact.exists())
        self.assertEqual(result.status, 'not_run')
        self.assertNotIn('secret', json.dumps(result.to_dict()))
        self.assertNotIn('private_database_sentinel', json.dumps(result.to_dict()))

    def test_every_operator_has_a_bound_fixture_and_replay_rejects_tampering(self):
        for operator in mutate.OPERATORS:
            with self.subTest(operator=operator):
                payload = replay.fixture_payload(operator, 7)
                self.assertEqual(replay.load_replay(json.dumps(payload), operator, 7), payload)
                self.assertLess(sum(map(len, payload['tables'].values())), 32)
                for field, value in [('seed', True), ('seed', 8), ('synthetic', 1), ('contract_sha256', 'changed'),
                                     ('schema_version', True), ('engine_sha256', 'changed')]:
                    changed = copy.deepcopy(payload); changed[field] = value
                    with self.assertRaises(StageError):
                        replay.load_replay(json.dumps(changed), operator, 7)
                changed = copy.deepcopy(payload); changed['tables']['part'][0][0] = 'foreign'
                with self.assertRaises(StageError):
                    replay.load_replay(json.dumps(changed), operator, 7)
                with self.assertRaises(StageError):
                    replay.load_replay('{"seed":7,"seed":8}', operator, 7)

    def test_comparison_preserves_bags_nulls_and_exact_decimal_values(self):
        projection = [('key', 'c.key', 'string'), ('value', 'c.value', 'number')]
        expected = [['A', '9007199254740993.001'], ['A', None]]
        actual = [{'c.key': 'A', 'c.value': None}, {'c.key': 'A', 'c.value': '9007199254740993.0010'}]
        self.assertTrue(replay.compare(projection, expected, actual)['equal'])
        self.assertFalse(replay.compare(projection, expected, actual + [actual[0]])['equal'])
        actual[1]['c.value'] = '9007199254740993.002'
        self.assertFalse(replay.compare(projection, expected, actual)['equal'])
        with self.assertRaises(ValueError):
            replay.compare(projection, expected, [{'c.key': 'A'}])

    def test_invalid_projected_values_cannot_be_counted_as_mutant_disagreement(self):
        projection = [('key', 'c.key', 'string'), ('value', 'c.value', 'number')]
        for value in ['not-a-number', 'NaN', 'Infinity', True, {'row': 1}, '1_000', ' 12 ', 12.0, '1e10001']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                replay.compare(projection, [['A', '12']], [{'c.key': 'A', 'c.value': value}])
        for key in [True, 1, ['A']]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                replay.compare(projection, [['A', '12']], [{'c.key': key, 'c.value': '12'}])

    def test_replay_requires_json_object_and_sanitizes_invalid_unicode(self):
        for raw in ['null', '[]', '[1]', 'true', '"text"', '{"tables":"\\ud800"}']:
            with self.subTest(raw=raw), self.assertRaises(StageError):
                replay.load_replay(raw, 'measure_agg_to_max', 7)

    def test_verdict_requires_original_gold_and_mutant_disagreement(self):
        for original, mutant, expected in [(True, False, 'pass'), (False, False, 'fail'),
                                           (True, True, 'fail'), (False, True, 'fail')]:
            self.assertEqual(replay.comparison_status(original, mutant), expected)

    def test_invalid_replay_removes_stale_fixture_before_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'cube_replay/fixture.json'
            artifact.parent.mkdir(); artifact.write_text('malformed', encoding='utf-8')
            with self.assertRaises(StageError):
                replay.run(None, arguments(replay=str(artifact)), tmp)
            self.assertFalse(artifact.exists())

    def test_invalid_numeric_args_remove_stale_fixture_before_error(self):
        for args in [arguments(seed=True), arguments(statement_timeout_ms=0)]:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                artifact = Path(tmp) / 'cube_replay/fixture.json'
                artifact.parent.mkdir(); artifact.write_text('stale', encoding='utf-8')
                with self.assertRaises(StageError):
                    replay.run(None, args, tmp)
                self.assertFalse(artifact.exists())

    def test_malformed_cube_response_cannot_kill_a_mutant(self):
        class Client:
            timeout_s = 0
            def __init__(self, response):
                self.response = response
            def _req(self, path, payload):
                return self.response
        for response in [{}, {'data': None}, {'data': 'rows'}, {'error': 'Continue wait'}, []]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                replay._load(Client(response), {'measures': ['m.value']})
        self.assertEqual(replay._load(Client({'data': []}), {'measures': ['m.value']}), [])


if __name__ == '__main__':
    unittest.main()
