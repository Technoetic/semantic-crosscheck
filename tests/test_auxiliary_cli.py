"""Standalone synthetic checks must never inherit operational configuration or stale success."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck.result import EXIT_ENV, Result, StageError


class AuxiliaryCLI(unittest.TestCase):
    STAGES = ('cube-replay', 'tlp', 'shacl-corpus')

    def arguments(self, stage):
        self.assertIn(stage, sxc.STAGES, 'the standalone synthetic stage must be registered')
        return {'cube-replay': ['--mutation-operator', 'measure_filter_drop'],
                'tlp': ['--spec', 'spec.json'], 'shacl-corpus': []}[stage]

    def test_cube_replay_requires_explicit_write_consent_and_a_dedicated_dsn(self):
        args = sxc.parser().parse_args(['cube-replay', *self.arguments('cube-replay')])
        self.assertFalse(args.allow_scratch_schema)
        self.assertEqual(args.dsn_env, 'SXC_CUBE_REPLAY_DSN')
        self.assertEqual((args.seed, args.statement_timeout_ms), (7, 10000))

    def test_tlp_defaults_are_bounded_and_dedicated(self):
        args = sxc.parser().parse_args(['tlp', *self.arguments('tlp')])
        self.assertEqual((args.dsn_env, args.seed, args.max_examples), ('SXC_TLP_DSN', 7, 64))
        self.assertEqual(args.statement_timeout_ms, 10000)
        self.assertIsNone(args.replay)

    def test_corpus_exposes_optional_external_engine_and_timeout(self):
        args = sxc.parser().parse_args(['shacl-corpus', *self.arguments('shacl-corpus'),
                                     '--jena-home', 'jena', '--timeout-ms', '3000'])
        self.assertEqual((args.jena_home, args.timeout_ms), ('jena', 3000))

    def test_standalone_stages_never_pick_up_default_configuration(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                self.arguments(stage)
                with patch.object(Path, 'is_file', return_value=True):
                    self.assertIsNone(sxc.config_path(stage, None))

    def test_explicit_operational_configuration_is_rejected(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                self.arguments(stage)
                with self.assertRaises(StageError) as caught:
                    sxc.config_path(stage, 'crosscheck.toml')
                self.assertEqual(caught.exception.exit_code, 2)

    def test_dispatch_and_result_directory_use_standalone_contract(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                options = self.arguments(stage)
                seen = []
                def run(cfg, args, out):
                    seen.append((cfg, args.stage))
                    result = Result(stage.replace('-', '_'))
                    result.add('synthetic_scope', 'not_run', reason='bounded_only')
                    return result
                with patch.object(sxc.config, 'load', side_effect=AssertionError('operational config read')), \
                     patch.object(sxc.importlib, 'import_module', return_value=SimpleNamespace(run=run)), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = sxc.main([stage, *options, '--out', tmp])
                self.assertEqual(code, 1)
                self.assertEqual(seen, [(None, stage)])
                actual = json.loads((Path(tmp) / stage.replace('-', '_') / 'result.json').read_text())
                self.assertEqual(actual['status'], 'not_run')
                self.assertEqual(actual['inputs'], {})
                self.assertEqual(actual['provenance']['binding'], 'not_applicable')

    def test_missing_engine_replaces_previous_pass(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                options = self.arguments(stage)
                result_file = Path(tmp) / stage.replace('-', '_') / 'result.json'
                result_file.parent.mkdir()
                result_file.write_text('{"status":"pass"}')
                def run(cfg, args, out):
                    self.assertFalse(result_file.exists(), 'stale success must be removed before invocation')
                    raise StageError('synthetic engine is absent', EXIT_ENV)
                with patch.object(sxc.importlib, 'import_module', return_value=SimpleNamespace(run=run)), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = sxc.main([stage, *options, '--out', tmp])
                self.assertEqual(code, EXIT_ENV)
                self.assertEqual(json.loads(result_file.read_text())['status'], 'error')

    def test_cli_preserves_standalone_input_bindings(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                bindings = {'fixture_sha256': 'a' * 64, 'generator_sha256': 'b' * 64}
                def run(cfg, args, out):
                    result = Result(stage.replace('-', '_'))
                    result.inputs = dict(bindings)
                    result.add('synthetic_scope', 'pass')
                    return result
                with patch.object(sxc.importlib, 'import_module', return_value=SimpleNamespace(run=run)), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = sxc.main([stage, *self.arguments(stage), '--out', tmp])
                self.assertEqual(code, 0)
                actual = json.loads((Path(tmp) / stage.replace('-', '_') / 'result.json').read_text())
                self.assertEqual(actual['inputs'], bindings)

    def test_standalone_console_does_not_disclose_output_directory(self):
        for stage in self.STAGES:
            for error in (False, True):
                with self.subTest(stage=stage, error=error), tempfile.TemporaryDirectory(prefix='private-output-') as tmp:
                    def run(cfg, args, out):
                        if error:
                            raise StageError('synthetic engine is absent', EXIT_ENV)
                        result = Result(stage.replace('-', '_'))
                        result.add('synthetic_scope', 'pass')
                        return result
                    console = io.StringIO()
                    with patch.object(sxc.importlib, 'import_module', return_value=SimpleNamespace(run=run)), \
                         contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                        sxc.main([stage, *self.arguments(stage), '--out', tmp])
                    self.assertNotIn('private-output-', console.getvalue())
                    self.assertNotIn(tmp, console.getvalue())

    def test_forbidden_config_replaces_default_output_stale_pass(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                result_file = Path(tmp) / '.sxc' / stage.replace('-', '_') / 'result.json'
                result_file.parent.mkdir(parents=True)
                result_file.write_text('{"status":"pass"}')
                previous = Path.cwd()
                try:
                    os.chdir(tmp)
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        code = sxc.main([stage, *self.arguments(stage), '--config', 'operational.toml'])
                finally:
                    os.chdir(previous)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(result_file.read_text())['status'], 'error')


if __name__ == '__main__':
    unittest.main()
