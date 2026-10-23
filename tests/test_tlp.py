"""TLP contract validation, exact comparison and hash-bound synthetic replay."""
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

try:
    tlp = importlib.import_module('semantic_crosscheck.tlp')
except ModuleNotFoundError:
    tlp = None


def document(**changes):
    value = dict(schema_version=1, scenario='nulls', relation_sql='SELECT qty FROM stock',
                 predicate_sql='qty > 0', aggregate={'function': 'sum', 'column': 'qty'})
    value.update(changes)
    return value


class TlpTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(tlp, 'The bounded TLP engine must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_tlp_')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def spec(self, value=None, raw=None):
        path = self.directory / 'spec.json'
        path.write_text(raw if raw is not None else json.dumps(value or document()), encoding='utf-8')
        return path

    def args(self, **changes):
        value = dict(spec=str(self.spec()), seed=7, max_examples=2, statement_timeout_ms=1000,
                     dsn_env='SXC_TLP_DSN', replay=None)
        value.update(changes)
        return SimpleNamespace(**value)

    def test_strict_version_fields_duplicate_keys_and_identifier(self):
        invalid = [document(schema_version=True), document(schema_version=2), document(extra=1),
                   document(aggregate={'function': 'sum', 'column': '*'}),
                   document(aggregate={'function': 'count', 'column': 'qty); SELECT 1'}),
                   document(scenario='production'), document(predicate_sql=''),
                   document(relation_sql='WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x'),
                   document(relation_sql='SELECT 1; SELECT 2'), document(predicate_sql='1; DELETE FROM stock')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(tlp.StageError):
                tlp.load_spec(self.spec(value))
        with self.assertRaises(tlp.StageError):
            tlp.load_spec(self.spec(raw='{"schema_version":1,"schema_version":1}'))
        with self.assertRaises(tlp.StageError):
            tlp.load_spec(self.spec(raw=' ' * (65536 + 1)))

    def test_comparison_preserves_null_duplicates_exact_decimal_and_sum_null(self):
        good = {'baseline': [{'qty': Decimal('1.000000000000000000001'), '__sxc_tlp_p': True}] * 2,
                'partitioned': [{'qty': Decimal('1.000000000000000000001'), '__sxc_tlp_p': True}] * 2,
                'baseline_aggregate': Decimal('2.000000000000000000002'),
                'partition_aggregate': Decimal('2.000000000000000000002'), 'aggregate_type': 'numeric'}
        self.assertTrue(tlp.compare(good)['equal'])
        bad = copy.deepcopy(good); bad['partitioned'].pop()
        self.assertFalse(tlp.compare(bad)['equal'])
        bad = copy.deepcopy(good); bad['partition_aggregate'] = Decimal('2.000000000000000000003')
        self.assertFalse(tlp.compare(bad)['equal'])
        empty = dict(baseline=[], partitioned=[], baseline_aggregate=None,
                     partition_aggregate=None, aggregate_type='numeric')
        self.assertTrue(tlp.compare(empty)['equal'])
        empty['partition_aggregate'] = 0
        self.assertFalse(tlp.compare(empty)['equal'])

    def test_malformed_engine_payload_never_passes(self):
        for payload in ({}, {'baseline': [], 'partitioned': []},
                        dict(baseline=[], partitioned=[], baseline_aggregate=0,
                             partition_aggregate=0, aggregate_type='double precision')):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                tlp.compare(payload)

    def test_replay_binds_spec_bytes_seed_generator_and_generated_data(self):
        spec = tlp.load_spec(self.spec())
        artifact = tlp.make_replay(spec, 7, 0)
        self.assertEqual(tlp.decode_replay(json.dumps(artifact), spec, 7), artifact)
        self.assertTrue(artifact['synthetic'])
        for field, value in [('seed', 8), ('generator_version', 999), ('spec_sha256', '0' * 64)]:
            bad = copy.deepcopy(artifact); bad[field] = value
            with self.subTest(field=field), self.assertRaises(tlp.StageError):
                tlp.decode_replay(json.dumps(bad), spec, 7)
        bad = copy.deepcopy(artifact); bad['tables']['stock'][0][2] = '123456'
        with self.assertRaises(tlp.StageError): tlp.decode_replay(json.dumps(bad), spec, 7)
        # JSON booleans must not masquerade as generated integer cells.
        integers = tlp.make_replay(tlp.load_spec(self.spec(document(scenario='bom_diamond'))), 7, 0)
        integers['tables']['bom_line'][0][2] = True
        with self.assertRaises(tlp.StageError):
            tlp.decode_replay(json.dumps(integers), tlp.load_spec(self.spec(document(scenario='bom_diamond'))), 7)

    def test_bad_arguments_and_replay_fail_before_connection(self):
        for changes in ({'max_examples': 0}, {'seed': True}, {'dsn_env': 'INVALID-NAME'},
                        {'statement_timeout_ms': 0}, {'replay': str(self.directory / 'missing')}):
            with self.subTest(changes=changes), patch.object(tlp.db, 'connect') as connect:
                with self.assertRaises(tlp.StageError): tlp.run(None, self.args(**changes), self.directory)
                connect.assert_not_called()

    def test_missing_engine_never_passes_or_exposes_connection_text(self):
        error = tlp.StageError('postgresql://secret-host/private', 3)
        with patch.object(tlp.db, 'connect', side_effect=error):
            result = tlp.run(None, self.args(), self.directory)
        self.assertEqual(result.status, 'not_run')
        self.assertNotIn('secret-host', json.dumps(result.to_dict()))
        self.assertTrue(result.not_proven)


if __name__ == '__main__':
    unittest.main()
