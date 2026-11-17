"""Optional diagnostics guard every input before cleanup and final publication."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401
import sxc
from semantic_crosscheck import config, db, measure_oracle, plan_compare
from semantic_crosscheck.result import Result, StageError
from test_measure_oracle import CONFIG, document as measure_document
from test_plan_compare import Engine, document as plan_document


class BoundedDiagnosticsCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='private-bounded-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = self.root / 'private-spec.json'
        self.cfg = self.root / 'private-config.toml'
        self.cfg.write_text(CONFIG, encoding='utf-8')
        self.out = self.root / 'out'
        self.set_spec('plan-compare')

    def set_spec(self, stage):
        document = measure_document() if stage == 'measure-oracle' else plan_document()
        self.spec.write_text(json.dumps(document), encoding='utf-8')

    def args(self, stage='plan-compare'):
        args = [stage, '--spec', str(self.spec), '--out', str(self.out)]
        if stage == 'measure-oracle':
            args += ['--config', str(self.cfg)]
        return args

    def invoke(self, args=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = sxc.main(self.args() if args is None else args)
            except SystemExit as error:
                code = error.code
        console = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn('private-', console)
        self.assertNotIn(str(self.root), console)
        self.assertNotIn('postgresql://', console)
        self.assertNotIn('Traceback', console)
        return code, console

    def saved(self, stage='plan_compare'):
        return json.loads((self.out / stage / 'result.json').read_text(encoding='utf-8'))

    def old_pass(self, stage='plan_compare'):
        result = Result(stage)
        result.add(stage, 'pass', rows=1)
        return result.write(self.out)

    @staticmethod
    def passing(stage, **inputs):
        result = Result(stage, inputs=inputs)
        result.add(stage, 'pass', rows=1)
        return result

    def test_parser_registers_independent_diagnostics_and_correct_config_contract(self):
        self.assertIn('measure-oracle', sxc.STAGES)
        self.assertIn('plan-compare', sxc.STAGES)
        args = sxc.parser().parse_args(['plan-compare', '--spec', 'spec.json'])
        self.assertEqual(args.dsn_env, 'SXC_TEST_ADMIN_DSN')
        self.assertEqual(args.statement_timeout_ms, 1000)
        self.assertIsNone(sxc.config_path('plan-compare', None))
        with self.assertRaises(StageError):
            sxc.config_path('plan-compare', 'private-config.toml')
        self.assertIn('measure-oracle', sxc.NEEDS_CONFIG)
        self.assertNotIn('plan-compare', sxc.NEEDS_CONFIG)

    def test_stage_help_requires_no_config_database_or_engine(self):
        with patch.object(config, 'load', side_effect=AssertionError('private-config-read')), \
                patch.object(db, 'connect', side_effect=AssertionError('private-db-read')):
            for stage in ('measure-oracle', 'plan-compare'):
                with self.subTest(stage=stage):
                    code, console = self.invoke([stage, '--help'])
                    self.assertEqual(code, 0)
                    self.assertIn('--spec', console)
        self.assertFalse(self.out.exists())

    def test_real_plan_command_is_standalone_and_preserves_spec_hash(self):
        with patch.object(config, 'load', side_effect=AssertionError('private-config-read')), \
                patch.object(db, 'connect', return_value=Engine()):
            self.assertEqual(self.invoke()[0], 0)
        result = self.saved()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['inputs']['spec_sha256'], hashlib.sha256(self.spec.read_bytes()).hexdigest())
        self.assertEqual(result['provenance']['binding'], 'not_applicable')
        self.assertNotIn('private-', json.dumps(result))

    def test_default_plan_output_does_not_discover_operational_configuration(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            (self.root / 'crosscheck.toml').write_text('private-invalid-config', encoding='utf-8')
            with patch.object(config, 'load', side_effect=AssertionError('private-config-read')), \
                    patch.object(db, 'connect', return_value=Engine()):
                self.assertEqual(self.invoke(['plan-compare', '--spec', str(self.spec)])[0], 0)
        finally:
            os.chdir(previous)
        self.assertTrue((self.root / '.sxc' / 'plan_compare' / 'result.json').is_file())

    def test_measure_dispatch_keeps_module_evidence_and_common_fingerprints(self):
        self.set_spec('measure-oracle')
        # Config canonicalizes its path. Exercise a noncanonical spelling on
        # every OS, including hosts whose temporary root already has aliases.
        (self.root / 'config-parent').mkdir()
        self.cfg = self.root / 'config-parent' / '..' / self.cfg.name
        def run(cfg, args, out):
            self.assertEqual(cfg.path, self.cfg.resolve())
            self.assertEqual(args.spec, str(self.spec))
            self.assertEqual(out, self.out)
            return self.passing('measure_oracle', measure_oracle={'spec_sha256': 'a' * 64})
        with patch.object(measure_oracle, 'run', side_effect=run):
            self.assertEqual(self.invoke(self.args('measure-oracle'))[0], 0)
        result = self.saved('measure_oracle')
        self.assertEqual(result['inputs']['measure_oracle']['spec_sha256'], 'a' * 64)
        self.assertEqual(result['inputs']['config_sha256'], hashlib.sha256(self.cfg.read_bytes()).hexdigest())

    def test_measure_uses_default_current_directory_config(self):
        self.set_spec('measure-oracle')
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            (self.root / 'crosscheck.toml').write_text(CONFIG, encoding='utf-8')
            with patch.object(measure_oracle, 'run', return_value=self.passing('measure_oracle')):
                self.assertEqual(self.invoke(['measure-oracle', '--spec', str(self.spec), '--out', str(self.out)])[0], 0)
        finally:
            os.chdir(previous)

    def test_usage_errors_clear_only_a_verified_old_receipt_and_do_not_echo(self):
        cases = [['--unknown', 'private-canary'], ['--sp', str(self.spec)],
                 ['--spec', str(self.spec)], ['--quiet', '--quiet'],
                 ['--statement-timeout-ms', 'private-timeout'], ['--config', str(self.cfg)]]
        for extra in cases:
            with self.subTest(option=extra[0]):
                target = self.old_pass()
                self.assertEqual(self.invoke(self.args() + extra)[0], 2)
                self.assertFalse(target.exists())
        target = self.old_pass()
        self.assertEqual(self.invoke(['--unknown', 'private-prefix', *self.args()])[0], 2)
        self.assertFalse(target.exists())

    def test_ambiguous_output_flags_do_not_choose_a_cleanup_destination(self):
        target = self.old_pass()
        other = self.root / 'other'
        self.assertEqual(self.invoke(self.args() + ['--out', str(other)])[0], 2)
        self.assertTrue(target.exists())
        self.assertFalse(other.exists())

    def test_rejected_filesystem_guards_also_block_failure_cleanup(self):
        target = self.old_pass()
        raw = target.read_bytes()
        with patch.object(plan_compare, '_inspect', side_effect=ValueError('private-reparse-container')):
            self.assertEqual(self.invoke()[0], 2)
        self.assertTrue(target.exists(), 'cleanup must respect the rejected filesystem guard')
        self.assertEqual(target.read_bytes(), raw)

    def test_missing_input_cannot_leave_a_stale_passing_receipt(self):
        target = self.old_pass()
        self.spec.unlink()
        self.assertEqual(self.invoke()[0], 2)
        self.assertTrue(not target.exists() or self.saved()['status'] != 'pass')

    def test_fixed_output_names_and_repeated_equals_inputs_are_never_overwritten(self):
        for stage, module in [('plan-compare', plan_compare), ('measure-oracle', measure_oracle)]:
            self.set_spec(stage)
            for kind in ('spec', 'config'):
                for name in ('result.json', 'result.json.part', '.gitignore'):
                    with self.subTest(stage=stage, input=kind, output=name):
                        target = self.out / name if name == '.gitignore' else self.out / stage.replace('-', '_') / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        raw = self.spec.read_bytes() if kind == 'spec' else self.cfg.read_bytes()
                        target.write_bytes(raw)
                        args = self.args(stage) + [f'--{kind}={target}']
                        with patch.object(module, 'run', side_effect=AssertionError('private-dispatched')):
                            self.assertEqual(self.invoke(args)[0], 2)
                        self.assertEqual(target.read_bytes(), raw)

    def test_hardlinked_result_is_rejected_before_cleanup(self):
        target = self.out / 'plan_compare' / 'result.json'
        target.parent.mkdir(parents=True)
        os.link(self.spec, target)
        raw = self.spec.read_bytes()
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(self.spec.read_bytes(), raw)

    def test_symlinked_ancestors_are_rejected_before_cleanup(self):
        linked = self.root / 'private-link'
        try:
            linked.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        self.out = linked / 'new-out'
        self.assertEqual(self.invoke()[0], 2)
        self.assertFalse((self.root / 'new-out').exists())

    def test_config_declared_gold_revision_and_sidecar_aliases_are_protected(self):
        self.set_spec('measure-oracle')
        target = self.out / 'measure_oracle' / 'result.json'
        target.parent.mkdir(parents=True)
        variants = [CONFIG + '\n[[gold]]\nid="g"\nsql="out/measure_oracle/result.json"\nkey=["id"]\nmap={id="m_orders.key"}\ncube_query={dimensions=["m_orders.key"]}\n',
                    CONFIG.replace('[source]', '[source]\ndata_revision_sql_file="out/measure_oracle/result.json"'),
                    CONFIG + '\n[semantic_preflight]\ncontract="out/measure_oracle/result.json"\nplan="plan.json"\n',
                    CONFIG + '\n[data_rules]\nfile="out/measure_oracle/result.json"\n']
        for value in variants:
            with self.subTest(selection=value[-80:]):
                self.cfg.write_text(value, encoding='utf-8')
                target.write_bytes(b'private-input-bytes')
                config.load(self.cfg)  # Confirm that this is a valid declared-input case.
                self.assertEqual(self.invoke(self.args('measure-oracle'))[0], 2)
                self.assertEqual(target.read_bytes(), b'private-input-bytes')

    def test_configuration_errors_and_unexpected_errors_are_sanitized(self):
        for error, expected in [(StageError('private-value postgresql://secret', 2), 2),
                                (RuntimeError('private-value postgresql://secret'), 1)]:
            with self.subTest(error=type(error).__name__):
                with patch.object(plan_compare, 'run', side_effect=error), patch.dict(os.environ, {'SXC_DEBUG': '1'}):
                    self.assertEqual(self.invoke()[0], expected)
                result = self.saved()
                self.assertEqual(result['status'], 'error')
                self.assertNotIn('private-', json.dumps(result))

    def test_repeated_configs_cannot_hide_a_declared_result_input_from_cleanup(self):
        target = self.old_pass('measure_oracle')
        raw = target.read_bytes()
        self.cfg.write_text(CONFIG + '\n[[gold]]\nid="g"\nsql="out/measure_oracle/result.json"\n'
                            'key=["id"]\nmap={id="m_orders.key"}\n'
                            'cube_query={dimensions=["m_orders.key"]}\n', encoding='utf-8')
        other = self.root / 'private-other-config.toml'
        other.write_text(CONFIG, encoding='utf-8')
        self.assertEqual(self.invoke(self.args('measure-oracle') + ['--config=' + str(other)])[0], 2)
        self.assertTrue(target.exists(), 'a receipt selected as a Gold input must not be deleted')
        self.assertEqual(target.read_bytes(), raw)

    def test_module_freshness_callback_invalidates_publication_and_is_never_serialized(self):
        original = Result.write
        for during_write in (False, True):
            result = self.passing('plan_compare')
            current = [True]
            result._diagnostic_validate = lambda: current[0]
            def run(cfg, args, out):
                if not during_write:
                    current[0] = False
                return result
            def write(result, out):
                target = original(result, out)
                current[0] = False
                return target
            with self.subTest(during_write=during_write), patch.object(plan_compare, 'run', side_effect=run), \
                    patch.object(Result, 'write', write):
                self.assertEqual(self.invoke()[0], 2)
            self.assertFalse((self.out / 'plan_compare' / 'result.json').exists())

    def test_post_run_input_changes_prevent_publishing_a_pass(self):
        def run(cfg, args, out):
            self.spec.write_text('private-changed-input', encoding='utf-8')
            return self.passing('plan_compare')
        with patch.object(plan_compare, 'run', side_effect=run):
            self.assertEqual(self.invoke()[0], 2)
        self.assertFalse((self.out / 'plan_compare' / 'result.json').exists())

    def test_output_creation_rechecks_aliases_before_any_receipt_cleanup(self):
        original = sxc.ensure_out
        raw = self.spec.read_bytes()
        def ensure(out):
            original(out)
            target = Path(out) / 'plan_compare' / 'result.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            self.spec.unlink()
            os.link(target, self.spec)
        with patch.object(sxc, 'ensure_out', side_effect=ensure):
            self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.spec.read_bytes(), raw)
        self.assertEqual((self.out / 'plan_compare' / 'result.json').read_bytes(), raw)

    def test_config_change_before_cleanup_cannot_select_a_receipt_for_deletion(self):
        self.set_spec('measure-oracle')
        target = self.old_pass('measure_oracle')
        raw = target.read_bytes()
        original = sxc.ensure_out
        def ensure(out):
            original(out)
            self.cfg.write_text(CONFIG + '\n[[gold]]\nid="g"\nsql="out/measure_oracle/result.json"\n'
                                'key=["id"]\nmap={id="m_orders.key"}\n'
                                'cube_query={dimensions=["m_orders.key"]}\n', encoding='utf-8')
        with patch.object(sxc, 'ensure_out', side_effect=ensure):
            self.assertEqual(self.invoke(self.args('measure-oracle'))[0], 2)
        self.assertTrue(target.exists(), 'a newly selected input must remain protected on failure')
        self.assertEqual(target.read_bytes(), raw)

    def test_config_sidecar_edits_after_run_cannot_publish_a_pass(self):
        self.set_spec('measure-oracle')
        self.cfg.write_text(CONFIG + '\n[semantic_preflight]\ncontract="contract.json"\nplan="plan.json"\n', encoding='utf-8')
        sidecar = self.root / 'contract.json'
        sidecar.write_text('{}', encoding='utf-8')
        (self.root / 'plan.json').write_text('{}', encoding='utf-8')
        def run(cfg, args, out):
            sidecar.write_text('private-edited-business-input', encoding='utf-8')
            return self.passing('measure_oracle')
        with patch.object(measure_oracle, 'run', side_effect=run):
            self.assertEqual(self.invoke(self.args('measure-oracle'))[0], 2)
        self.assertFalse((self.out / 'measure_oracle' / 'result.json').exists())

    def test_invalid_config_and_missing_spec_clear_old_pass_without_console_values(self):
        self.set_spec('measure-oracle')
        target = self.old_pass('measure_oracle')
        self.cfg.write_text('private-canary = [', encoding='utf-8')
        self.assertEqual(self.invoke(self.args('measure-oracle'))[0], 2)
        self.assertFalse(target.exists())
        target = self.old_pass('measure_oracle')
        args = ['measure-oracle', '--config', str(self.cfg), '--out', str(self.out)]
        self.assertEqual(self.invoke(args)[0], 2)
        self.assertFalse(target.exists())

    def test_input_changes_during_write_remove_only_the_just_published_receipt(self):
        original = Result.write
        for replace in (False, True):
            self.set_spec('plan-compare')
            def write(result, out):
                target = original(result, out)
                self.spec.write_text('private-changed-input', encoding='utf-8')
                if replace:
                    target.write_bytes(b'private-another-writer')
                return target
            with self.subTest(replaced=replace), patch.object(db, 'connect', return_value=Engine()), \
                    patch.object(Result, 'write', write):
                self.assertEqual(self.invoke()[0], 2)
            target = self.out / 'plan_compare' / 'result.json'
            if replace:
                self.assertEqual(target.read_bytes(), b'private-another-writer')
            else:
                self.assertFalse(target.exists())

    def test_post_write_cleanup_preserves_a_receipt_newly_selected_as_a_gold_input(self):
        self.set_spec('measure-oracle')
        original = Result.write
        published = []
        def write(result, out):
            target = original(result, out)
            published.append(target.read_bytes())
            self.cfg.write_text(CONFIG + '\n[[gold]]\nid="g"\nsql="out/measure_oracle/result.json"\n'
                                'key=["id"]\nmap={id="m_orders.key"}\n'
                                'cube_query={dimensions=["m_orders.key"]}\n', encoding='utf-8')
            return target
        with patch.object(measure_oracle, 'run', return_value=self.passing('measure_oracle')), \
                patch.object(Result, 'write', write):
            self.assertEqual(self.invoke(self.args('measure-oracle'))[0], 2)
        target = self.out / 'measure_oracle' / 'result.json'
        self.assertTrue(target.exists(), 'an input selected during publication must not be removed')
        self.assertEqual(target.read_bytes(), published[0])


if __name__ == '__main__':
    unittest.main()
