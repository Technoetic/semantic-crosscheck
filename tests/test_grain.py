"""Independent grain claims must fail closed before touching source data."""
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
from semantic_crosscheck import config

try:
    grain = importlib.import_module('semantic_crosscheck.grain')
except ModuleNotFoundError:
    grain = None


def document():
    return {'schema_version': 1, 'contracts': [{
        'measure': 'total',
        'source': {'entity': 'fact', 'table': 'sxc_demo.grain_fact', 'key': ['fact_id']},
        'group_by': ['node_id'], 'aggregate': {'function': 'sum', 'column': 'qty'},
        'filters': {'exact': {}, 'normalized': {}},
        'output': {'cube': 'm_total', 'key': ['key'], 'value': 'value', 'reaggregate': 'sum'},
        'node': {'entity': 'node', 'table': 'sxc_demo.grain_node', 'key': ['node_id']},
        'join': {'relationship': 'one_to_one', 'node_key': ['node_id'], 'output_key': ['key']},
        'null_policy': {'operation': 'exclude', 'allow_excluded_rows': False}}]}


CONFIG = '''version = 1
[source]
schemas = ["sxc_demo"]
statement_timeout_ms = 1000
[entities.fact]
table = "sxc_demo.grain_fact"
key = ["fact_id"]
[entities.node]
table = "sxc_demo.grain_node"
key = ["node_id"]
[measures.total]
entity = "fact"
column = "qty"
agg = "sum"
by = "node_id"
node = "node"
'''


class GrainInputs(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(grain, 'The independent grain diagnostic must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_grain_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text(CONFIG, encoding='utf-8')
        self.cfg = config.load(self.config_path)
        self.spec_path = self.root / 'spec.json'
        self.spec()

    def spec(self, value=None, raw=None):
        self.spec_path.write_text(raw if raw is not None else json.dumps(value or document()), encoding='utf-8')
        return self.spec_path

    def args(self, **changes):
        values = dict(spec=str(self.spec_path), max_rows=100000)
        values.update(changes)
        return SimpleNamespace(**values)

    def check(self, result, suffix):
        return next(c for c in result.checks if c.id == 'grain:c0001:' + suffix)


class GrainContractTest(GrainInputs):
    def test_entity_table_nonstring_types_are_sanitized_configuration_errors(self):
        from semantic_crosscheck.result import StageError
        for entity in ('source', 'node'):
            for value in (None, True, 17, [], {}):
                with self.subTest(entity=entity, value_type=type(value).__name__):
                    spec=document();spec['contracts'][0][entity]['table']=value
                    with self.assertRaises(StageError):
                        grain.load_spec(self.spec(spec))

    def test_independent_source_group_output_and_node_claims_cannot_self_certify(self):
        cases = [('source', 'table', 'sxc_demo.other'), ('source', 'key', ['node_id']),
                 ('aggregate', 'function', 'count'), ('aggregate', 'column', 'other'),
                 ('output', 'cube', 'm_other'), ('output', 'key', ['other']),
                 ('output', 'reaggregate', 'max'), ('node', 'table', 'sxc_demo.other'),
                 ('node', 'key', ['other']), ('join', 'node_key', ['other']),
                 ('null_policy', 'operation', 'retain')]
        for section, field, value in cases:
            doc = document()
            doc['contracts'][0][section][field] = value
            self.spec(doc)
            with self.subTest(section=section, field=field):
                result = grain.run(self.cfg, self.args(), self.root)
                self.assertEqual(result.status, 'fail')
                self.assertFalse(self.check(result, 'contract').detail['matches'])
        doc = document()
        doc['contracts'][0]['group_by'] = ['fact_id']
        self.spec(doc)
        self.assertEqual(grain.run(self.cfg, self.args(), self.root).status, 'fail')

    def test_strict_schema_duplicate_keys_nonfinite_and_vacuous_scope_are_rejected(self):
        bad = [dict(document(), schema_version=True), dict(document(), extra='PRIVATE'),
               dict(document(), contracts=[]), dict(document(), contracts=document()['contracts'] * 2)]
        for field, value in [('measure', 'absent'), ('date_column', 'PRIVATE'),
                             ('group_by', []), ('group_by', ['node_id', 'node_id']),
                             ('null_policy', {'operation': 'exclude', 'allow_excluded_rows': 1}),
                             ('filters', {'exact': {'status': []}, 'normalized': {}})]:
            doc = document()
            doc['contracts'][0][field] = value
            bad.append(doc)
        doc = document()
        doc['contracts'][0]['node']['extra'] = 'PRIVATE'
        bad.append(doc)
        doc = document()
        doc['contracts'][0]['filters']['exact'] = {'status': [1.5]}
        bad.append(doc)
        doc = document()
        doc['contracts'][0]['filters']['normalized'] = {'status': [' lower ']}
        bad.append(doc)
        for doc in bad:
            self.spec(doc)
            with self.subTest(doc=doc), self.assertRaises(grain.StageError) as raised:
                grain.run(self.cfg, self.args(), self.root)
            self.assertNotIn('PRIVATE', str(raised.exception))
        for raw in ['{"schema_version":1,"schema_version":1,"contracts":[]}',
                    '{"schema_version":NaN,"contracts":[]}',
                    '{"schema_version":Infinity,"contracts":[]}', 'x' * 65537]:
            self.spec(raw=raw)
            with self.assertRaises(grain.StageError):
                grain.run(self.cfg, self.args(), self.root)

    def test_unsupported_date_closure_missing_node_and_composite_node_are_rejected(self):
        for field, value in [('date_column', 'due'), ('closure_join', 'ancestor'), ('node', '')]:
            cfg = copy.deepcopy(self.cfg)
            setattr(cfg.measures['total'], field, value)
            with self.subTest(field=field), self.assertRaises(grain.StageError):
                grain.run(cfg, self.args(), self.root)
        self.cfg.entities['node'].key = ['node_id', 'other']
        with self.assertRaises(grain.StageError):
            grain.run(self.cfg, self.args(), self.root)

    def test_invalid_budgets_and_private_identifier_errors_do_not_leak(self):
        for value in [True, 0, -1, 1.5, 1000001, 'PRIVATE']:
            with self.subTest(value=value), self.assertRaises(grain.StageError) as raised:
                grain.run(self.cfg, self.args(max_rows=value), self.root)
            self.assertNotIn('PRIVATE', str(raised.exception))
        doc = document()
        doc['contracts'][0]['source']['table'] = 'PRIVATE; SELECT 1'
        self.spec(doc)
        with self.assertRaises(grain.StageError) as raised:
            grain.run(self.cfg, self.args(), self.root)
        self.assertNotIn('PRIVATE', str(raised.exception))

    def test_alias_protection_preserves_inputs_including_hardlinks_and_cleans_stale_result(self):
        destination = self.root / 'grain'
        destination.mkdir()
        target = destination / 'result.json'
        target.write_text('stale pass', encoding='utf-8')
        self.spec(raw='{}')
        with self.assertRaises(grain.StageError):
            grain.run(self.cfg, self.args(), self.root)
        self.assertFalse(target.exists())
        target.write_bytes(self.spec().read_bytes())
        before = target.read_bytes()
        for alias in [target, destination / 'result.json.part']:
            if alias != target:
                os.link(target, alias)
            with self.subTest(alias=alias), self.assertRaises(grain.StageError):
                grain.run(self.cfg, self.args(spec=str(alias)), self.root)
            self.assertEqual(target.read_bytes(), before)

    def test_connection_errors_are_sanitized_without_names_paths_or_values(self):
        with patch.object(grain.db, 'connect', side_effect=RuntimeError('PRIVATE-cell-postgresql://secret-path')):
            result = grain.run(self.cfg, self.args(), self.root)
        self.assertEqual(result.status, 'error')
        payload = json.dumps(result.to_dict())
        for secret in ['PRIVATE', 'postgresql://', str(self.root), 'grain_fact', 'node_id', 'total']:
            self.assertNotIn(secret, payload)

    def test_cli_preflight_checks_manifest_alias_before_any_discard(self):
        destination = self.root / 'grain'
        destination.mkdir()
        target = destination / 'result.json'
        target.write_bytes(self.spec_path.read_bytes())
        before = target.read_bytes()
        with self.assertRaises(grain.StageError):
            grain.prepare_run(self.args(spec=str(target)), self.root)
        self.assertEqual(target.read_bytes(), before)
        grain.prepare_run(self.args(), self.root)
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
