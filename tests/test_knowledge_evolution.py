"""Fixed offline replay: literal independent oracle, no operational execution."""
import contextlib
import copy
from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import config, db, knowledge_sources, provenance, report, runtime_provenance
from semantic_crosscheck.result import Result, inputs_fingerprint


class EvolutionTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('semantic_crosscheck.knowledge_evolution')

    def test_cli_runs_fixed_five_checkpoints_before_any_stage_exists(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = sxc.main(['knowledge-evolution', '--out', tmp, '--quiet'])
            except SystemExit as exc:
                code = exc.code
            self.assertEqual(code, 0, 'fixed offline replay must be a successful diagnostic')
            result = json.loads((Path(tmp) / 'knowledge_evolution/result.json').read_text())
            self.assertEqual(result['status'], 'pass')
            self.assertEqual(len(result['checks']), 26)

    def test_literal_checkpoint_oracle_and_retained_case(self):
        result = self.module().evaluate()
        checks = {c.id: c for c in result.checks}
        for tid, source, case, operational in [
            ('T0', 'current', 'pass', 'pass'), ('T1', 'changed', 'not_run', 'pass'),
            ('T2', 'missing', 'not_run', 'pass'), ('T3', 'current', 'not_run', 'pass'),
            ('T4', 'retired', 'not_run', 'not_run')]:
            self.assertEqual(checks[f'knowledge_evolution:{tid}:source'].detail['observed'], source)
            self.assertEqual(checks[f'knowledge_evolution:{tid}:case'].detail['observed'], case)
            for stage in ('build', 'cube', 'verify'):
                self.assertEqual(checks[f'knowledge_evolution:{tid}:{stage}'].detail['observed'], operational)
        self.assertEqual(result.status, 'pass')
        self.assertEqual(checks['knowledge_evolution:T4:empty_expect'].detail['observed'], 'not_run')
        self.assertEqual(result.inputs['knowledge_evolution']['checkpoints'], 5)
        self.assertEqual(result.inputs['knowledge_evolution']['contracts'], 26)
        self.assertFalse(result.inputs['knowledge_evolution']['semantic_coverage_measured'])

    def test_actual_apis_and_no_forbidden_side_effects(self):
        mod = self.module()
        with contextlib.ExitStack() as stack:
            for obj, name in [(db, 'connect'), (provenance, 'current_probe'),
                              (runtime_provenance, 'current_probe'), (subprocess, 'Popen'),
                              (socket, 'create_connection'), (socket.socket, 'connect'),
                              (report, 'run'), (Result, 'write')]:
                stack.enter_context(patch.object(obj, name, side_effect=AssertionError('forbidden')))
            capture = stack.enter_context(patch.object(knowledge_sources, 'capture', wraps=knowledge_sources.capture))
            finish = stack.enter_context(patch.object(knowledge_sources, 'finish', wraps=knowledge_sources.finish))
            load = stack.enter_context(patch.object(config, 'load', wraps=config.load))
            stale = stack.enter_context(patch.object(report, 'staleness', wraps=report.staleness))
            counted = stack.enter_context(patch.object(report, 'counted_status', wraps=report.counted_status))
            empty_expect = stack.enter_context(patch.object(mod.expect, 'run', wraps=mod.expect.run))
            res = mod.evaluate()
        self.assertEqual(res.status, 'pass')
        self.assertEqual((capture.call_count, finish.call_count, load.call_count, stale.call_count, counted.call_count), (4, 4, 2, 5, 15))
        self.assertEqual(empty_expect.call_count, 1)
        info = res.inputs['knowledge_evolution']
        self.assertEqual([info[k] for k in ('engines_executed', 'database_queries', 'model_calls')], [0, 0, 0])

    def test_always_current_mutation_and_missing_observations_keep_denominator(self):
        mod = self.module()
        original = knowledge_sources.SourceSnapshot.public
        def always_current(snap):
            public = original(snap)
            for item in public['items']:
                item['state'] = 'current'
            return public
        with patch.object(knowledge_sources.SourceSnapshot, 'public', always_current):
            result = mod.evaluate()
        self.assertEqual(result.status, 'fail')
        self.assertEqual(len(result.checks), 26)
        with patch.object(knowledge_sources, 'capture', side_effect=OSError('PRIVATE_SENTINEL')):
            result = mod.evaluate()
        self.assertNotEqual(result.status, 'pass')
        self.assertEqual(len(result.checks), 26)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result.to_dict()))

        original_observe = knowledge_sources._observe
        def classifier(root, entry, budget):
            observed, size = original_observe(root, entry, budget)
            return replace(observed, state='current'), size
        with patch.object(knowledge_sources, '_observe', classifier):
            result = mod.evaluate()
        self.assertEqual(result.status, 'fail')
        self.assertEqual(len(result.checks), 26)

    def test_missing_checkpoint_and_operational_receipt_remain_nonpassing(self):
        mod = self.module()
        original_observe = mod._observe
        def omitted(root):
            observed = original_observe(root)
            del observed['T2']
            del observed['T3']['cube']
            return observed
        with patch.object(mod, '_observe', omitted):
            result = mod.evaluate()
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(result.checks), 26)
        original_receipts = mod.synthetic_receipts
        def missing(inputs):
            receipts = original_receipts(inputs)
            del receipts['cube']
            return receipts
        with patch.object(mod, 'synthetic_receipts', missing):
            result = mod.evaluate()
        self.assertEqual(result.status, 'fail')
        self.assertEqual(len(result.checks), 26)
        self.assertEqual(next(c.detail['observed'] for c in result.checks
                              if c.id == 'knowledge_evolution:T0:cube'), 'not_run')

    def test_unreadable_engine_binding_retains_all_contracts(self):
        mod = self.module()
        with patch.object(provenance, 'engine_fingerprint', side_effect=OSError('PRIVATE_SENTINEL')):
            result = mod.evaluate()
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(result.checks), 26)
        self.assertEqual(result.inputs['knowledge_evolution']['passed'], 0)
        self.assertEqual(result.inputs['knowledge_evolution']['engine_state'], 'unavailable')
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result.to_dict()))

    def test_fixture_is_closed_bounded_and_fixed(self):
        mod = self.module()
        fixture = mod.fixture_bytes()
        original = json.loads(fixture)
        corruptions = [b'x' * 65537, b'{', b'[' * 1000]
        corruptions.append(fixture.replace(b'"schema":', b'"schema": "sxc/knowledge-evolution-fixture/v1", "schema":', 1))
        for change in ('schema', 'extra', 'missing', 'reorder', 'operation', 'id'):
            value = copy.deepcopy(original)
            if change == 'schema': value['schema'] = 'unknown'
            if change == 'extra': value['path'] = 'PRIVATE_SENTINEL'
            if change == 'missing': value['checkpoints'].pop()
            if change == 'reorder': value['checkpoints'].reverse()
            if change == 'operation': value['checkpoints'][1]['operation'] = 'exec'
            if change == 'id': value['checkpoints'][0]['id'] = 'other'
            corruptions.append(json.dumps(value).encode())
        for raw in corruptions:
            with self.subTest(size=len(raw)), patch.object(mod, 'fixture_bytes', return_value=raw):
                result = mod.evaluate()
                self.assertNotEqual(result.status, 'pass')
                self.assertEqual(len(result.checks), 26)
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result.to_dict()))

    def test_restore_retains_execution_identity_and_receipts(self):
        mod = self.module()
        seen, receipts = [], []
        original_finish, original_stale = knowledge_sources.finish, report.staleness
        def finish(snap, result):
            final = original_finish(snap, result)
            seen.append((id(result), result.started_at, result.finished_at,
                         [(c.id, c.status) for c in result.checks if c.id == 'expect:q01[0]']))
            return final
        def stale(original, *args, **kwargs):
            receipts.append(json.dumps(original, sort_keys=True))
            return original_stale(original, *args, **kwargs)
        with patch.object(knowledge_sources, 'finish', finish), patch.object(report, 'staleness', stale):
            self.assertEqual(mod.evaluate().status, 'pass')
        self.assertEqual(len({row[0] for row in seen}), 1)
        self.assertEqual(len({row[1:3] for row in seen}), 1)
        self.assertEqual(seen[-1][3], [('expect:q01[0]', 'not_run')])
        self.assertEqual(len(set(receipts)), 1)

    def test_whole_input_guards_noop_irrelevant_config_and_sql(self):
        mod = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = mod.synthetic_project(root)
            configuration_bytes = Path(cfg.path).read_bytes()
            before = inputs_fingerprint(cfg)
            receipts = mod.synthetic_receipts(before)
            def stale():
                return report.staleness(receipts, inputs_fingerprint(cfg),
                    {'schema_version': 1, 'binding': 'unbound'},
                    {'schema_version': 1, 'binding': 'unbound'}, {})
            self.assertEqual(stale(), {})
            cases = [(0, cfg.gold[0], 0, cfg.gold[0].expect[0])]
            self.assertEqual(knowledge_sources.capture(cfg, cases).public()['items'][0]['state'], 'current')
            (root / 'irrelevant.txt').write_bytes(b'irrelevant')
            self.assertEqual(inputs_fingerprint(cfg), before)
            self.assertEqual(stale(), {})
            self.assertEqual(knowledge_sources.capture(cfg, cases).public()['items'][0]['state'], 'current')
            with Path(cfg.path).open('a') as stream: stream.write('\n# edit\n')
            self.assertEqual(set(stale()), {'build', 'cube', 'verify'})
            Path(cfg.path).write_bytes(configuration_bytes)
            (root / 'query.sql').write_text('SELECT 2')
            self.assertEqual(set(stale()), {'verify'})

    def test_cli_rejects_external_inputs_and_ignores_cwd_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'crosscheck.toml').write_text('PRIVATE_SENTINEL invalid config')
            old = Path.cwd()
            try:
                os.chdir(root)
                for extra in ([], ['--config', 'PRIVATE_SENTINEL'], ['--fixture', 'PRIVATE_SENTINEL']):
                    stream = io.StringIO()
                    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                        code = sxc.main(['knowledge-evolution', '--out', str(root / 'out'), *extra])
                    self.assertEqual(code, 2 if extra else 0)
                    self.assertNotIn('PRIVATE_SENTINEL', stream.getvalue())
                self.assertEqual(sorted(p.name for p in (root / 'out').iterdir()), ['.gitignore', 'knowledge_evolution'])
            finally:
                os.chdir(old)

    def test_cli_stamps_only_the_explicit_namespace(self):
        mod = self.module()
        result = mod.evaluate()
        original = copy.deepcopy(result.inputs['knowledge_evolution'])
        result.inputs['PRIVATE_SENTINEL'] = {'arbitrary': 'data'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(mod, 'run', return_value=result), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sxc.main(['knowledge-evolution', '--out', tmp]), 0)
            public = json.loads((Path(tmp) / 'knowledge_evolution/result.json').read_text())
        self.assertEqual(public['inputs'], {'knowledge_evolution': original})


if __name__ == '__main__':
    unittest.main()
