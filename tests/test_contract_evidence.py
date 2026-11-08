"""Configured preflight evidence must bind the declared files, not any saved pass."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import business_contracts, config, report
from semantic_crosscheck.result import Result, StageError, inputs_fingerprint
from test_firstrun_report import pipeline
from test_semantic_preflight_cli import contract, plan


CONFIG = '''version = 1
[source]
schemas = ["sxc_demo"]
[entities.stock]
table = "sxc_demo.stock"
key = ["part_id"]
[measures.stock]
entity = "stock"
column = "qty"
agg = "sum"
by = "part_id"
[[gold]]
id = "stock"
sql = "stock.sql"
key = ["part_id"]
map = {part_id="m_stock.key", qty="m_stock.value"}
cube_query = {dimensions=["m_stock.key"], measures=["m_stock.value"]}
'''
OPTION = '\n[semantic_preflight]\ncontract = "contract.json"\nplan = "plan.json"\n'


class ConfiguredPreflightConfig(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sxc-contract-config-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'crosscheck.toml'
        (self.root / 'stock.sql').write_text('select 1', encoding='utf-8')

    def parse(self, option):
        return config.loads((CONFIG + option).encode(), self.path)

    def test_optional_pair_is_resolved_relative_to_the_config(self):
        try:
            cfg = self.parse(OPTION)
        except StageError as exc:
            self.fail(f'configured preflight must be supported: {exc}')
        self.assertEqual(cfg.semantic_preflight.contract, 'contract.json')
        self.assertEqual(cfg.semantic_preflight.plan, 'plan.json')
        canonical_root = self.root.resolve()
        self.assertEqual(cfg.semantic_preflight.paths(cfg),
                         (canonical_root / 'contract.json', canonical_root / 'plan.json'))
        self.assertIsNone(getattr(self.parse(''), 'semantic_preflight', None))

    def test_relative_sidecars_use_the_canonical_configuration_directory(self):
        canonical_root = self.root.resolve() / 'canonical-base'
        canonical_root.mkdir()
        (canonical_root / 'stock.sql').write_text('select 1', encoding='utf-8')
        declared = self.root / 'simulated-short-parent' / 'crosscheck.toml'
        canonical_config = canonical_root / 'crosscheck.toml'
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == declared:
                return canonical_config
            return original_resolve(path, *args, **kwargs)
        # Simulate a temp-root spelling such as RUNNER~1 or /var without
        # creating a symlink or changing the guarded filesystem reader.
        with patch.object(Path, 'resolve', resolve):
            cfg = config.loads((CONFIG + OPTION).encode(), declared)
        self.assertEqual(cfg.path, canonical_config)
        self.assertEqual(cfg.semantic_preflight.paths(cfg),
                         (canonical_root / 'contract.json', canonical_root / 'plan.json'))

    def test_malformed_or_unpaired_options_are_configuration_errors(self):
        cases = [
            '\n[semantic_preflight]\n',
            '\n[semantic_preflight]\ncontract="contract.json"\n',
            '\n[semantic_preflight]\nplan="plan.json"\n',
            OPTION + 'unknown = true\n',
        ]
        for key in ('contract', 'plan'):
            for value in ('""', '" "', 'true', '[]', '"/tmp/input.json"', '"C:/input.json"',
                          '"../input.json"', '"folder/../input.json"', '"."', '"https://example.org/input.json"'):
                cases.append('\n[semantic_preflight]\n' +
                             f'{key} = {value}\n' +
                             f'{"plan" if key == "contract" else "contract"} = "sidecar.json"\n')
        for option in cases:
            with self.subTest(option=option):
                with self.assertRaises(StageError) as caught:
                    self.parse(option)
                self.assertEqual(caught.exception.exit_code, 2)
                self.assertIn('semantic_preflight', str(caught.exception))

    def test_preflight_setting_must_be_a_table(self):
        raw = CONFIG.replace('version = 1', 'version = 1\nsemantic_preflight = true')
        with self.assertRaises(StageError) as caught:
            config.loads(raw.encode(), self.path)
        self.assertIn('semantic_preflight', str(caught.exception))


class ConfiguredPreflightReport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sxc-contract-report-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg_path = self.root / 'crosscheck.toml'
        self.contract_path = self.root / 'contract.json'
        self.plan_path = self.root / 'plan.json'
        self.out = self.root / 'out'
        (self.root / 'stock.sql').write_text('select 1', encoding='utf-8')
        self.cfg_path.write_text(CONFIG + OPTION, encoding='utf-8')
        self.contract_path.write_text(json.dumps(contract()), encoding='utf-8')
        self.plan_path.write_text(json.dumps(plan()), encoding='utf-8')

    def cfg(self):
        return config.load(self.cfg_path)

    def diagnostic(self, contract_path=None, plan_path=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sxc.main(['semantic-preflight', '--config', str(self.cfg_path),
                             '--contract', str(contract_path or self.contract_path),
                             '--plan', str(plan_path or self.plan_path), '--out', str(self.out)])

    def operational(self):
        pipeline(self.out, ('build', 'cube', 'verify'), inputs=inputs_fingerprint(self.cfg()))

    def run_report(self):
        return report.run(self.cfg(), argparse.Namespace(), self.out)

    def check(self, result):
        return next((c for c in result.checks if c.id == 'stage:semantic_preflight'), None)

    def receipt(self):
        return self.out / 'semantic_preflight' / 'result.json'

    def rewrite(self, modify):
        document = json.loads(self.receipt().read_text(encoding='utf-8'))
        modify(document)
        self.receipt().write_text(json.dumps(document), encoding='utf-8')

    def capture_pass(self):
        self.assertEqual(self.diagnostic(), 0)
        self.operational()

    def test_missing_configured_preflight_prevents_an_operational_pass(self):
        self.operational()
        result = self.run_report()
        self.assertEqual(result.status, 'not_run')
        check = self.check(result)
        self.assertIsNotNone(check)
        self.assertEqual(check.detail['reason'], 'missing')
        self.assertIn('semantic-preflight', result.next_safe_action)

    def test_current_configured_cli_result_is_required_and_passes(self):
        self.capture_pass()
        result = self.run_report()
        self.assertEqual(result.status, 'pass')
        check = self.check(result)
        self.assertIsNotNone(check, 'the configured diagnostic must contribute a required check')
        self.assertTrue(check.detail['required'])
        summary = json.loads((self.out / 'report.json').read_text(encoding='utf-8'))
        self.assertIn('semantic_preflight', summary['required'])
        self.assertEqual(summary['stages']['semantic_preflight']['counted_as'], 'pass')
        self.assertIn('semantic_preflight', (self.out / 'report.md').read_text(encoding='utf-8'))

    def test_without_opt_in_standalone_diagnostic_is_ignored(self):
        self.cfg_path.write_text(CONFIG, encoding='utf-8')
        document = plan()
        document['selections'][0]['context']['population'] = 'different'
        self.plan_path.write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(self.diagnostic(), 1)
        self.operational()
        result = self.run_report()
        self.assertEqual(result.status, 'pass')
        self.assertIsNone(self.check(result))
        summary = json.loads((self.out / 'report.json').read_text(encoding='utf-8'))
        self.assertNotIn('semantic_preflight', summary['required'])
        self.assertNotIn('semantic_preflight', summary['stages'])

    def test_current_failure_and_incomplete_plan_are_not_passes(self):
        for incomplete, wanted in ((False, 'fail'), (True, 'not_run')):
            with self.subTest(incomplete=incomplete):
                document = plan()
                if incomplete:
                    del document['selections'][0]['context']['population']
                else:
                    document['selections'][0]['context']['population'] = 'different'
                self.plan_path.write_text(json.dumps(document), encoding='utf-8')
                self.assertEqual(self.diagnostic(), 1)
                self.operational()
                result = self.run_report()
                self.assertEqual(result.status, wanted)
                self.assertEqual(self.check(result).status, wanted)

    def test_any_changed_config_contract_or_plan_invalidates_the_saved_pass(self):
        for path in (self.cfg_path, self.contract_path, self.plan_path):
            with self.subTest(file=path.name):
                self.capture_pass()
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                result = self.run_report()
                self.assertNotEqual(result.status, 'pass')
                self.assertEqual(self.check(result).status, 'not_run')
                path.write_bytes(original)

    def test_different_explicit_cli_paths_cannot_satisfy_configured_sidecars(self):
        # Equal contents deliberately rule out content hashes as the selected-file binding.
        alternate_contract = self.root / 'alternate-contract.json'
        alternate_plan = self.root / 'alternate-plan.json'
        alternate_contract.write_bytes(self.contract_path.read_bytes())
        alternate_plan.write_bytes(self.plan_path.read_bytes())
        self.assertEqual(self.diagnostic(alternate_contract, alternate_plan), 0)
        self.operational()
        result = self.run_report()
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(self.check(result).detail['why'], 'contract_evidence_changed')

    def test_legacy_or_partial_fingerprints_are_not_authority(self):
        for key in ('config_sha256', 'contract_sha256', 'plan_sha256',
                    'generator_sha256', 'selected_binding_sha256'):
            with self.subTest(key=key):
                self.capture_pass()
                self.rewrite(lambda doc: doc['inputs'].pop(key, None))
                result = self.run_report()
                self.assertEqual(result.status, 'not_run')

    def test_recorded_status_cannot_hide_a_failing_check(self):
        document = plan()
        document['selections'][0]['context']['population'] = 'different'
        self.plan_path.write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(self.diagnostic(), 1)
        self.operational()
        self.rewrite(lambda doc: doc.update(status='pass'))
        self.assertEqual(self.run_report().status, 'fail')

    def test_forged_pass_or_missing_checks_cannot_replace_recomputed_declarations(self):
        for mutation in ('empty', 'subset', 'forged'):
            with self.subTest(mutation=mutation):
                if mutation == 'forged':
                    document = plan()
                    document['selections'][0]['context']['population'] = 'different'
                    self.plan_path.write_text(json.dumps(document), encoding='utf-8')
                    self.assertEqual(self.diagnostic(), 1)
                else:
                    self.assertEqual(self.diagnostic(), 0)
                self.operational()
                def change(doc):
                    if mutation == 'empty':
                        doc['checks'] = []
                    elif mutation == 'subset':
                        doc['checks'] = doc['checks'][:1]
                    else:
                        for check in doc['checks']:
                            check['status'] = 'pass'
                    doc['status'] = 'pass'
                self.rewrite(change)
                self.assertEqual(self.run_report().status, 'error')

    def test_missing_unreadable_or_malformed_current_inputs_never_reuse_a_saved_pass(self):
        original = self.plan_path.read_bytes()
        for mutation in ('missing', 'malformed', 'oversize', 'utf8'):
            with self.subTest(mutation=mutation):
                self.plan_path.write_bytes(original)
                self.capture_pass()
                if mutation == 'missing':
                    self.plan_path.unlink()
                else:
                    self.plan_path.write_bytes({'malformed': b'{', 'oversize': b'x' * (256 * 1024 + 1),
                                                'utf8': b'\xff'}[mutation])
                self.assertEqual(self.run_report().status, 'error')

    def test_malformed_or_truncated_saved_result_is_an_error(self):
        for raw in (b'{', b'\xff', b'null', b'{"checks":[],"checks":[]}',
                    b'x' * (256 * 1024 + 1)):
            with self.subTest(size=len(raw)):
                self.capture_pass()
                self.receipt().write_bytes(raw)
                self.assertEqual(self.run_report().status, 'error')

    def test_evidence_requires_readable_chronological_run_times(self):
        for change in ('missing', 'invalid', 'backwards'):
            with self.subTest(change=change):
                self.capture_pass()
                def update(doc):
                    if change == 'missing':
                        doc.pop('finished_at')
                    elif change == 'invalid':
                        doc['started_at'] = 'unknown'
                    else:
                        doc['started_at'] = '2026-02-01T00:00:00+0000'
                        doc['finished_at'] = '2026-01-01T00:00:00+0000'
                self.rewrite(update)
                self.assertNotEqual(self.run_report().status, 'pass')

    def test_change_during_recomputation_invalidates_the_captured_verdict(self):
        self.capture_pass()
        evaluate = business_contracts.evaluate
        def changing(*args):
            result = evaluate(*args)
            self.plan_path.write_bytes(self.plan_path.read_bytes() + b'\n')
            return result
        with patch.object(business_contracts, 'evaluate', side_effect=changing):
            self.assertEqual(self.run_report().status, 'error')

    def test_changed_generator_invalidates_existing_preflight(self):
        self.capture_pass()
        from semantic_crosscheck import contract_evidence
        with patch.object(contract_evidence, 'generator_fingerprint', return_value='0' * 64):
            self.assertEqual(self.run_report().status, 'not_run')

    def test_changed_result_during_report_cannot_leave_a_pass(self):
        self.capture_pass()
        render = report.render_md
        def changing(summary):
            markdown = render(summary)
            self.receipt().write_bytes(b'{')
            return markdown
        with patch.object(report, 'render_md', side_effect=changing):
            result = self.run_report()
        self.assertEqual(result.status, 'error')
        self.assertEqual(sum(note.startswith('stage doctor was not run') for note in result.not_proven), 1)
        summary = json.loads((self.out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['status'], 'error')
        self.assertEqual(summary['stages']['semantic_preflight']['counted_as'], 'error')
        self.assertIn('Overall: **ERROR**', (self.out / 'report.md').read_text(encoding='utf-8'))

    def test_changed_input_during_report_cannot_leave_a_pass(self):
        self.capture_pass()
        summarize = report._stage_summary
        def changing(*args):
            summary = summarize(*args)
            self.plan_path.write_bytes(self.plan_path.read_bytes() + b'\n')
            return summary
        with patch.object(report, '_stage_summary', side_effect=changing):
            self.assertEqual(self.run_report().status, 'error')

    def unreadable_report(self, selected):
        unavailable = Path(selected).resolve()
        denied = []
        old_open = os.open
        def blocked(path, *args, **kwargs):
            if Path(path).resolve() == unavailable:
                denied.append(path)
                raise PermissionError('private-canary')
            return old_open(path, *args, **kwargs)
        with patch('os.open', side_effect=blocked):
            result = self.run_report()
        self.assertTrue(denied, 'the permission fault must reach the actual selected-file read')
        return result

    def test_unreadable_or_linked_inputs_and_saved_receipts_fail_closed(self):
        self.capture_pass()
        result = self.unreadable_report(self.plan_path)
        self.assertEqual(result.status, 'error')
        self.assertNotIn('private-canary', (self.out / 'report.json').read_text(encoding='utf-8'))
        self.capture_pass()
        original = self.root / 'original-result.json'
        original.write_bytes(self.receipt().read_bytes())
        self.receipt().unlink()
        os.link(original, self.receipt())
        self.assertEqual(self.run_report().status, 'error')

    def test_unreadable_alias_spelling_blocks_the_canonical_selected_file(self):
        self.capture_pass()
        canonical_plan = self.plan_path.resolve()
        declared = self.root / 'simulated-short-parent' / 'plan.json'
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == declared:
                return canonical_plan
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve):
            result = self.unreadable_report(declared)
        self.assertEqual(result.status, 'error')

    def test_evidence_inspection_never_reads_sql_or_uses_database_or_network(self):
        self.capture_pass()
        from semantic_crosscheck import contract_evidence, db
        old_open, old_bytes = os.open, Path.read_bytes
        def guard_open(path, *args, **kwargs):
            self.assertFalse(str(path).endswith('.sql'))
            return old_open(path, *args, **kwargs)
        def guard_bytes(path):
            self.assertFalse(str(path).endswith('.sql'))
            return old_bytes(path)
        with patch('os.open', side_effect=guard_open), patch.object(Path, 'read_bytes', guard_bytes), \
                patch.object(db, 'connect', side_effect=AssertionError('no database')), \
                patch('socket.create_connection', side_effect=AssertionError('no network')):
            evidence = contract_evidence.inspect(self.cfg(), self.out).refresh()
        self.assertEqual(report.stage_status(evidence.result), 'pass')
        self.assertIsNone(evidence.stale)

    def test_saved_arbitrary_explanation_cannot_become_report_instructions(self):
        self.capture_pass()
        self.rewrite(lambda doc: doc.update(not_proven=['private-canary'], next_safe_action='private-canary'))
        result = self.run_report()
        self.assertEqual(result.status, 'pass')
        self.assertNotIn('private-canary', (self.out / 'report.json').read_text(encoding='utf-8'))

    def test_report_cli_observes_missing_and_current_configured_preflight(self):
        self.operational()
        def invoke():
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return sxc.main(['report', '--config', str(self.cfg_path), '--out', str(self.out)])
        self.assertEqual(invoke(), 1)
        self.assertEqual(self.diagnostic(), 0)
        self.assertEqual(invoke(), 0)

    def test_diagnostic_change_during_publication_removes_its_earlier_verdict(self):
        write = Result.write
        def changing(result, out):
            target = write(result, out)
            self.plan_path.write_bytes(self.plan_path.read_bytes() + b'\n')
            return target
        with patch.object(Result, 'write', changing):
            self.assertEqual(self.diagnostic(), 2)
        self.assertFalse(self.receipt().exists())

    def test_stale_receipt_keeps_its_worse_check_severity(self):
        for status in ('fail', 'error'):
            with self.subTest(status=status):
                self.capture_pass()
                def change(doc):
                    doc['checks'][0]['status'] = status
                    doc['status'] = 'pass'
                    doc['inputs']['plan_sha256'] = '0' * 64
                self.rewrite(change)
                result = self.run_report()
                check = self.check(result)
                self.assertEqual(result.status, status)
                self.assertEqual(check.detail['last_status'], status)
                self.assertGreater(check.detail[status], 0)

    def protected_alias_case(self, source_name, relative, cli, hardlink=False):
        with tempfile.TemporaryDirectory(prefix='sxc-report-protection-') as temporary:
            root = Path(temporary)
            out = root / 'out'
            target = out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            paths = {name: root / filename for name, filename in
                     (('config', 'crosscheck.toml'), ('contract', 'contract.json'), ('plan', 'plan.json'))}
            if not hardlink:
                paths[source_name] = target
            cfg_base = paths['config'].parent
            # Config paths are deliberately relative even when the config itself
            # occupies a reserved output. All data in this fixture is synthetic.
            for name in ('contract', 'plan'):
                if source_name == 'config' and not hardlink:
                    paths[name] = cfg_base / (name + '.json')
            option = '\n[semantic_preflight]\n' + ''.join(
                f'{name} = "{paths[name].relative_to(cfg_base).as_posix()}"\n' for name in ('contract', 'plan'))
            (cfg_base / 'stock.sql').write_text('select 1', encoding='utf-8')
            paths['config'].write_text(CONFIG + option, encoding='utf-8')
            paths['contract'].write_text(json.dumps(contract()), encoding='utf-8')
            paths['plan'].write_text(json.dumps(plan()), encoding='utf-8')
            if hardlink:
                os.link(paths[source_name], target)
            original = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            if cli:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = sxc.main(['report', '--config', str(paths['config']), '--out', str(out)])
            else:
                try:
                    result = report.run(config.load(paths['config']), argparse.Namespace(), out)
                    code = result.exit_code()
                except StageError as exc:
                    code = exc.exit_code
            observed = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(observed, original, 'protected inputs and existing outputs must remain byte-identical')
            self.assertEqual(code, 2)

    def test_selected_inputs_cannot_occupy_any_report_output_or_legacy_temp(self):
        for source in ('config', 'contract', 'plan'):
            for target in ('report.json', 'report.md', 'report/result.json', 'report/result.json.part', '.gitignore'):
                for cli in (False, True):
                    with self.subTest(source=source, target=target, cli=cli):
                        self.protected_alias_case(source, target, cli)

    def test_hardlink_aliases_of_all_report_outputs_are_preserved(self):
        for source in ('config', 'contract', 'plan'):
            for target in ('report.json', 'report.md', 'report/result.json', 'report/result.json.part', '.gitignore'):
                for cli in (False, True):
                    with self.subTest(source=source, target=target, cli=cli):
                        self.protected_alias_case(source, target, cli, hardlink=True)

    def test_malformed_legacy_config_still_removes_prior_passing_summaries(self):
        self.cfg_path.write_text(CONFIG, encoding='utf-8')
        self.operational()
        def invoke():
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return sxc.main(['report', '--config', str(self.cfg_path), '--out', str(self.out)])
        self.assertEqual(invoke(), 0)
        self.cfg_path.write_text('version = [broken', encoding='utf-8')
        self.assertEqual(invoke(), 2)
        self.assertFalse((self.out / 'report.json').exists())
        self.assertFalse((self.out / 'report.md').exists())

    def test_late_selection_drift_does_not_delete_newly_selected_input(self):
        self.capture_pass()
        render = report.render_md
        target = self.out / 'report.json'
        original = self.plan_path.read_bytes()
        def changing(summary):
            target.write_bytes(original)
            self.cfg_path.write_text((CONFIG + OPTION).replace('plan = "plan.json"',
                                                              'plan = "out/report.json"'), encoding='utf-8')
            return render(summary)
        with patch.object(report, 'render_md', changing), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(['report', '--config', str(self.cfg_path), '--out', str(self.out)])
        self.assertTrue(target.exists(), 'error cleanup must preserve newly selected input')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(code, 2)

    def test_stale_receipt_cannot_downgrade_current_recomputed_failure(self):
        self.capture_pass()
        document = plan()
        document['selections'][0]['context']['population'] = 'different'
        self.plan_path.write_text(json.dumps(document), encoding='utf-8')
        result = self.run_report()
        self.assertEqual(result.status, 'fail')
        self.assertGreater(self.check(result).detail['fail'], 0)

    def test_cli_final_result_publication_cannot_accept_changed_sidecars(self):
        self.capture_pass()
        write = Result.write
        def changing(result, out):
            target = write(result, out)
            if result.stage == 'report':
                self.plan_path.write_bytes(self.plan_path.read_bytes() + b'\n')
            return target
        with patch.object(Result, 'write', changing), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(['report', '--config', str(self.cfg_path), '--out', str(self.out)])
        self.assertNotEqual(code, 0)
        for target in (self.out / 'report.json', self.out / 'report.md', self.out / 'report' / 'result.json'):
            self.assertFalse(target.exists(), 'do not retain a pass after final publication drift')

    def test_diagnostic_drift_preserves_another_writers_result_replacement(self):
        write = Result.write
        replacement = b'another writer replacement\n'
        def changing(result, out):
            target = write(result, out)
            self.plan_path.write_bytes(self.plan_path.read_bytes() + b'\n')
            target.write_bytes(replacement)
            return target
        with patch.object(Result, 'write', changing):
            self.assertEqual(self.diagnostic(), 2)
        self.assertTrue(self.receipt().exists())
        self.assertEqual(self.receipt().read_bytes(), replacement)
