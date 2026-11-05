"""Boundary CLI guards every declared input before any cleanup or output write."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import boundary_schema as boundary, db
from semantic_crosscheck.result import StageError


def document():
    return {'version': 1, 'table': 'items', 'column': 'qty', 'type': 'integer',
            'rows': [0, 1], 'predicate_sql': 'qty >= 10'}


class BoundaryCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='private-boundary-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = self.root / 'private-spec.json'
        self.raw = json.dumps(document()).encode('utf-8')
        self.spec.write_bytes(self.raw)
        self.out = self.root / 'out'

    def invoke(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = sxc.main(['boundary-schema', '--spec', str(self.spec), '--out', str(self.out), *options])
            except SystemExit as error:
                code = error.code
        console = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn('private-', console)
        self.assertNotIn(str(self.root), console)
        return code, console

    def test_standalone_parser_and_configuration_contract(self):
        self.assertIn('boundary-schema', sxc.STAGES)
        self.assertNotIn('boundary-schema', sxc.NEEDS_CONFIG)
        args = sxc.parser().parse_args(['boundary-schema', '--spec', 'input.json'])
        self.assertEqual(args.spec, 'input.json')
        self.assertIsNone(sxc.config_path('boundary-schema', None))
        with self.assertRaises(StageError):
            sxc.config_path('boundary-schema', 'private-company-config.toml')

    def test_tlp_shrink_flags_are_opt_in_with_the_agreed_budget(self):
        args = sxc.parser().parse_args(['tlp', '--spec', 'input.json'])
        self.assertTrue(hasattr(args, 'shrink'), 'TLP must expose the explicit opt-in shrink flag')
        self.assertFalse(args.shrink)
        self.assertEqual(args.max_shrink_steps, 200)
        args = sxc.parser().parse_args(['tlp', '--spec', 'input.json', '--shrink', '--max-shrink-steps', '12'])
        self.assertTrue(args.shrink)
        self.assertEqual(args.max_shrink_steps, 12)

    def test_real_command_preserves_hash_bindings_and_never_loads_config_or_database(self):
        with patch.object(sxc.config, 'load', side_effect=AssertionError('private-config-read')), \
                patch.object(db, 'connect', side_effect=AssertionError('private-database-call')):
            self.assertEqual(self.invoke()[0], 0)
        saved = json.loads((self.out / 'boundary_schema' / 'result.json').read_text(encoding='utf-8'))
        artifact = self.out / 'boundary_schema' / 'witness-schema.json'
        self.assertEqual(saved['stage'], 'boundary_schema')
        self.assertEqual(saved['provenance']['binding'], 'not_applicable')
        self.assertEqual(saved['inputs']['source_sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(saved['inputs']['witness_schema_sha256'], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(saved['inputs']['generator_sha256'], saved['checks'][0]['detail']['generator_sha256'])
        self.assertNotIn(str(self.root), json.dumps(saved))

    def test_default_output_ignores_an_existing_operational_config(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            (self.root / 'crosscheck.toml').write_text('invalid private operational config', encoding='utf-8')
            with patch.object(sxc.config, 'load', side_effect=AssertionError('private-config-read')), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = sxc.main(['boundary-schema', '--spec', str(self.spec)])
                except SystemExit as error:
                    code = error.code
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / '.sxc' / 'boundary_schema' / 'result.json').is_file())

    def test_every_fixed_output_alias_preserves_the_input_before_cleanup(self):
        targets = [('boundary_schema', 'result.json'), ('boundary_schema', 'result.json.part'),
                   ('boundary_schema', 'witness-schema.json'), ('.gitignore',)]
        for parts in targets:
            with self.subTest(target=parts):
                target = self.out.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.raw)
                original = self.spec
                self.spec = target
                try:
                    self.assertEqual(self.invoke()[0], 2)
                    self.assertEqual(target.read_bytes(), self.raw)
                finally:
                    self.spec = original

    def test_output_file_or_file_ancestor_is_preserved(self):
        for output in (self.spec, self.spec / 'child'):
            with self.subTest(ancestor=output == self.spec):
                self.out = output
                self.assertEqual(self.invoke()[0], 2)
                self.assertEqual(self.spec.read_bytes(), self.raw)

    def test_hardlinked_result_alias_does_not_delete_either_input_name(self):
        target = self.out / 'boundary_schema' / 'result.json'
        target.parent.mkdir(parents=True)
        os.link(self.spec, target)
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.spec.read_bytes(), self.raw)
        self.assertEqual(target.read_bytes(), self.raw)

    def test_symlinked_source_and_output_ancestors_are_rejected_before_writes(self):
        linked = self.root / 'source-link.json'
        try:
            linked.symlink_to(self.spec)
        except OSError:
            self.skipTest('symbolic link creation requires a developer-enabled Windows environment')
        original = self.spec
        self.spec = linked
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(original.read_bytes(), self.raw)
        self.assertFalse(self.out.exists())
        self.spec = original
        parent = self.root / 'output-parent'
        parent.symlink_to(self.root, target_is_directory=True)
        self.out = parent / 'out'
        self.assertEqual(self.invoke()[0], 2)
        self.assertFalse((self.root / 'out').exists())

    def test_forbidden_configuration_alias_and_repeated_specs_remain_protected(self):
        target = self.out / 'boundary_schema' / 'result.json'
        target.parent.mkdir(parents=True)
        target.write_bytes(self.raw)
        self.assertEqual(self.invoke('--config', str(target))[0], 2)
        self.assertEqual(target.read_bytes(), self.raw)
        original = self.spec
        self.spec = target
        try:
            self.assertEqual(self.invoke('--spec', str(original))[0], 2)
            self.assertEqual(target.read_bytes(), self.raw)
        finally:
            self.spec = original

    def test_bad_arguments_make_no_output_mutation_and_never_echo_values(self):
        for options in (('--unknown', 'private-canary-value'), ('--sp', str(self.spec)),
                        ('--rows', 'private-row-value')):
            with self.subTest(option=options[0]):
                self.assertEqual(self.invoke(*options)[0], 2)
                self.assertFalse(self.out.exists())
                self.assertEqual(self.spec.read_bytes(), self.raw)

    def test_invalid_options_before_the_stage_are_also_sanitized_without_writes(self):
        console = io.StringIO()
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            try:
                code = sxc.main(['--unknown', 'private-prefix-canary', 'boundary-schema',
                                 '--spec', str(self.spec), '--out', str(self.out)])
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, 2)
        self.assertNotIn('private-', console.getvalue())
        self.assertFalse(self.out.exists())
        self.assertEqual(self.spec.read_bytes(), self.raw)

    def test_malformed_input_replaces_stale_result_with_sanitized_error(self):
        target = self.out / 'boundary_schema' / 'result.json'
        target.parent.mkdir(parents=True)
        target.write_text('{"status":"pass"}', encoding='utf-8')
        self.spec.write_text('{"private-canary":', encoding='utf-8')
        self.assertEqual(self.invoke()[0], 2)
        saved = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'error')
        self.assertNotIn('private-', json.dumps(saved))

    def test_unexpected_stage_errors_are_sanitized_without_a_traceback(self):
        with patch.object(boundary, 'run', side_effect=RuntimeError('private-canary-stage-error')):
            self.assertEqual(self.invoke()[0], 1)
        saved = json.loads((self.out / 'boundary_schema' / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'error')
        self.assertNotIn('private-', json.dumps(saved))

    def test_final_result_write_rechecks_new_input_output_aliases(self):
        real = boundary.run
        def run_and_alias(cfg, args, out):
            result = real(cfg, args, out)
            target = Path(out) / 'boundary_schema' / 'result.json'
            target.write_bytes(self.raw)
            self.spec.unlink()
            os.link(target, self.spec)
            return result
        with patch.object(boundary, 'run', side_effect=run_and_alias):
            self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.spec.read_bytes(), self.raw)
        self.assertEqual((self.out / 'boundary_schema' / 'result.json').read_bytes(), self.raw)


if __name__ == '__main__':
    unittest.main()
