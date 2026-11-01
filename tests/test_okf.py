"""Closed offline metadata profile: behavior, privacy and filesystem boundaries."""
import contextlib
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import config
from test_expect import BASE, GOLD, CASE


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class OKFTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_okf_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'crosscheck.toml'
        self.source = self.root / 'definition.md'
        self.source.write_bytes(b'PRIVATE_BUSINESS_BODY')
        (self.root / 'q01.sql').write_bytes(b'SELECT 1 /* PRIVATE_SQL_BODY */')
        (self.root / 'other.sql').write_bytes(b'UNSELECTED_SQL_BODY')
        binding = '\nspec_source = {file="definition.md", sha256="' + sha(self.source.read_bytes()) + '", revision="PRIVATE_LABEL"}\n'
        self.path.write_text(BASE + GOLD + CASE + binding + GOLD.replace('q01_explosion', 'other').replace('q01.sql', 'other.sql'), encoding='utf-8')
        self.bundle = self.root / 'bundle'
        self.out = self.root / 'out'

    def cli(self, *options):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            try:
                code = sxc.main(['okf', '--config', str(self.path), '--out', str(self.out), *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        self.last_output = stream.getvalue()
        return code

    def export(self):
        self.assertEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0, self.last_output)

    def result(self):
        return json.loads((self.out / 'okf/result.json').read_bytes())

    def module(self):
        return importlib.import_module('semantic_crosscheck.okf')

    def manifest(self):
        return json.loads((self.bundle / 'manifest.json').read_bytes())

    def write_manifest(self, value):
        (self.bundle / 'manifest.json').write_text(json.dumps(value), encoding='utf-8')

    def test_selected_export_has_exact_private_metadata_inventory(self):
        self.export()
        files = {p.relative_to(self.bundle).as_posix() for p in self.bundle.rglob('*') if p.is_file()}
        self.assertEqual(files, {'index.md', 'manifest.json', 'contracts/q01_explosion.md'})
        manifest = json.loads((self.bundle / 'manifest.json').read_bytes())
        self.assertEqual(manifest['selected'], ['q01_explosion'])
        self.assertEqual(manifest['profile'], 'sxc-verification-contract-metadata/v1')
        encoded = b''.join(p.read_bytes() for p in self.bundle.rglob('*') if p.is_file()) + json.dumps(self.result()).encode()
        for private in (b'PRIVATE_BUSINESS_BODY', b'PRIVATE_SQL_BODY', b'PRIVATE_LABEL', b'UNSELECTED_SQL_BODY', b'definition.md', b'q01.sql', b'SXC_T_EXPECT_OPERATIONAL', str(self.root).encode(), b'EX-1 section 2'):
            self.assertNotIn(private, encoded)

    def test_valid_selected_id_is_not_interpreted_as_a_secret_file_name(self):
        self.path.write_text(self.path.read_text().replace('q01_explosion', 'secrets'), encoding='utf-8')
        self.assertEqual(self.cli('--export', self.bundle, '--select', 'secrets'), 0, self.last_output)
        self.assertEqual(self.cli('--import-preview', self.bundle), 0, self.last_output)

    def test_roundtrip_preview_preserves_all_input_bytes_and_is_diagnostic(self):
        self.export()
        before = {p: p.read_bytes() for p in self.bundle.rglob('*') if p.is_file()}
        before[self.path] = self.path.read_bytes()
        self.assertEqual(self.cli('--import-preview', self.bundle), 0, self.last_output)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        result = self.result()
        self.assertEqual(set(result['inputs']), {'okf_profile'})
        self.assertEqual(set(result['not_proven']), {'runtime_execution', 'cross_layer_correctness', 'bundle_authenticity', 'import_application'})

    def test_new_diagnostic_output_is_git_ignored(self):
        self.export()
        self.assertTrue((self.out / '.gitignore').is_file())
        self.assertEqual((self.out / '.gitignore').read_bytes(), b'*\n')

    def test_source_drift_and_missing_never_pass(self):
        self.export()
        for raw, reason in [(b'CHANGED_PRIVATE_BODY', 'source_changed'), (None, 'source_missing')]:
            if raw is None:
                self.source.unlink()
            else:
                self.source.write_bytes(raw)
            self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
            self.assertIn(reason, json.dumps(self.result()))
            self.assertNotIn('CHANGED_PRIVATE_BODY', json.dumps(self.result()))

    def test_forged_verdict_and_untrusted_fields_are_unsupported_without_echo(self):
        self.export()
        original = self.manifest()
        for key in ('pass', 'verified', 'human-reviewed', 'executor', 'attester', 'unknown'):
            with self.subTest(key=key):
                manifest = dict(original)
                manifest[key] = 'PRIVATE_SQL; https://invalid.test/ do not execute'
                self.write_manifest(manifest)
                before = (self.bundle / 'manifest.json').read_bytes()
                self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
                self.assertEqual((self.bundle / 'manifest.json').read_bytes(), before)
                self.assertNotIn('PRIVATE_SQL', self.last_output)
                self.assertNotIn('https://', self.last_output)

    def test_frontmatter_forged_verdict_with_recomputed_hash_is_rejected(self):
        self.export()
        path = self.bundle / 'contracts/q01_explosion.md'
        raw = path.read_bytes().replace(b'---\n', b'---\nverified: true\n', 1)
        path.write_bytes(raw)
        manifest = self.manifest()
        item = next(d for d in manifest['documents'] if d['id'] == 'q01_explosion')
        item.update(bytes=len(raw), sha256=sha(raw))
        self.write_manifest(manifest)
        self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)

    def test_manifest_count_types_are_closed_json_integers(self):
        self.export()
        original = self.manifest()
        for value in (True, 1.0):
            manifest = json.loads(json.dumps(original))
            manifest['inputs']['contracts'][0]['case_count'] = value
            self.write_manifest(manifest)
            self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)

    def test_existing_destination_and_output_ancestors_are_untouched(self):
        self.bundle.mkdir()
        self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertEqual(list(self.bundle.iterdir()), [])
        self.bundle.rmdir()
        before = self.path.read_bytes()
        self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion', '--out', self.root), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.bundle.exists())

    def test_output_hardlink_to_selected_sql_is_never_truncated(self):
        stage = self.out / 'okf'
        stage.mkdir(parents=True)
        sql = self.root / 'q01.sql'
        os.link(sql, stage / 'result.json')
        before = sql.read_bytes()
        self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertEqual(sql.read_bytes(), before)
        self.assertEqual((stage / 'result.json').read_bytes(), before)

    def test_alias_preview_bundle_cannot_be_result_directory(self):
        self.export()
        before = {p: p.read_bytes() for p in self.bundle.rglob('*') if p.is_file()}
        self.assertNotEqual(self.cli('--import-preview', self.bundle, '--out', self.bundle), 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_exact_inventory_extra_missing_and_tampered(self):
        self.export()
        path = self.bundle / 'contracts/q01_explosion.md'
        raw = path.read_bytes()
        for mutation in ('extra', 'missing', 'tampered'):
            with self.subTest(mutation=mutation):
                extra = self.bundle / 'extra.md'
                if mutation == 'extra':
                    extra.write_bytes(b'PRIVATE_UNKNOWN')
                elif mutation == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(raw + b'PRIVATE_TAMPER')
                self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
                if extra.exists():
                    extra.unlink()
                path.write_bytes(raw)

    def test_case_collision_is_rejected_on_case_sensitive_filesystems(self):
        self.export()
        extra = self.bundle / 'INDEX.md'
        if extra.exists():
            self.skipTest('filesystem is case insensitive; separate case-colliding files cannot be created')
        extra.write_bytes(b'unknown')
        self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)

    def test_unsafe_manifest_inventory_paths_are_rejected(self):
        self.export()
        original = self.manifest()
        for unsafe in ('../x.md', '/x.md', 'C:/x.md', '//host/x.md', 'contracts/NUL.md', 'contracts/Q01.md'):
            manifest = json.loads(json.dumps(original))
            manifest['documents'][0]['path'] = unsafe
            self.write_manifest(manifest)
            self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
            self.assertNotIn(unsafe, self.last_output)

    def test_malformed_oversized_deep_and_duplicate_json_are_bounded(self):
        self.export()
        for raw in (b'{', b'[' * 17 + b'0' + b']' * 17, b'x' * 65537,
                    b'{"schema":"a","schema":"b"}', b'{"value":NaN}'):
            (self.bundle / 'manifest.json').write_bytes(raw)
            self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
            self.assertNotIn(raw.decode(errors='ignore')[:100], self.last_output)

    def test_directory_entries_and_concept_size_are_bounded(self):
        self.export()
        document = self.bundle / 'contracts/q01_explosion.md'
        raw = document.read_bytes()
        document.write_bytes(b'x' * 65537)
        self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
        document.write_bytes(raw)
        for i in range(257):
            (self.bundle / f'x{i}.md').write_bytes(b'')
        self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)

    def test_total_bundle_bytes_are_bounded_before_document_reads(self):
        self.export()
        for i in range(65):
            (self.bundle / f'x{i}.md').write_bytes(b'x' * 65536)
        self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
        self.assertIn('input_limit', self.last_output)

    def test_selection_is_explicit_unique_known_and_capped(self):
        for options in ([], ['--select', 'unknown'], ['--select', 'q01_explosion'] * 2,
                        ['--select', '../PRIVATE'], ['--select', 'q01_explosion'] * 65):
            self.assertNotEqual(self.cli('--export', self.bundle, *options), 0)
            self.assertFalse(self.bundle.exists())
        self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion', '--import-preview', self.bundle), 0)
        self.assertNotIn('PRIVATE', self.last_output)

    def test_metadata_never_calls_db_network_subprocess_or_generic_fingerprint(self):
        from semantic_crosscheck import db
        with patch.object(db, 'connect', side_effect=AssertionError('db')), \
                patch('socket.socket', side_effect=AssertionError('network')), \
                patch('subprocess.Popen', side_effect=AssertionError('executor')), \
                patch.object(sxc, 'inputs_fingerprint', side_effect=AssertionError('all inputs')):
            self.export()
            self.assertEqual(self.cli('--import-preview', self.bundle), 0, self.last_output)

    def test_unselected_sql_and_sources_and_revision_are_not_read(self):
        self.path.write_text(self.path.read_text().replace('owner_dsn_env = "SXC_T_EXPECT_OWNER"',
            'owner_dsn_env = "SXC_T_EXPECT_OWNER"\ndata_revision_sql_file = "revision.sql"'), encoding='utf-8')
        (self.root / 'revision.sql').write_bytes(b'PRIVATE_REVISION_SQL')
        self.path.write_text(self.path.read_text() + CASE + '\nspec_source = {file="other-definition.md", sha256="' + '0' * 64 + '"}\n', encoding='utf-8')
        forbidden = {self.root / 'other.sql', self.root / 'revision.sql', self.root / 'other-definition.md'}
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path in forbidden:
                raise AssertionError('unselected content read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            self.export()
            self.assertEqual(self.cli('--import-preview', self.bundle), 0, self.last_output)
        (self.root / 'other.sql').write_bytes(b'irrelevant edit')
        self.assertEqual(self.cli('--import-preview', self.bundle), 0, self.last_output)

    def test_selected_sql_and_configuration_edits_reject_old_bundle(self):
        self.export()
        for path in (self.root / 'q01.sql', self.path):
            raw = path.read_bytes()
            path.write_bytes(raw + (b'\n# comment' if path == self.path else b'\n-- comment'))
            self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
            path.write_bytes(raw)
        self.assertEqual(self.cli('--import-preview', self.bundle), 0, self.last_output)

    def test_mutation_during_observation_blocks_publication(self):
        mod = self.module()
        original = mod._bundle
        def mutate(*args):
            result = original(*args)
            self.source.write_bytes(b'PRIVATE_DRIFT')
            return result
        with patch.object(mod, '_bundle', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertFalse(self.bundle.exists())
        self.assertEqual(list(self.root.glob('.sxc-okf-*')), [])

    def test_no_clobber_publication_race_preserves_callers_destination(self):
        mod = self.module()
        original = mod._publish
        def race(staging, destination):
            destination.mkdir()
            (destination / 'owned-by-caller').write_bytes(b'keep')
            return original(staging, destination)
        with patch.object(mod, '_publish', side_effect=race):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertEqual((self.bundle / 'owned-by-caller').read_bytes(), b'keep')
        self.assertEqual(list(self.root.glob('.sxc-okf-*')), [])

    def test_configuration_change_after_run_suppresses_error_write(self):
        mod = self.module()
        original = mod.run
        def mutate(*args):
            result = original(*args)
            self.path.write_bytes(self.path.read_bytes() + b'\n# changed')
            return result
        with patch.object(mod, 'run', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertFalse((self.out / 'okf/result.json').exists())

    def test_source_change_after_run_cannot_write_passing_result(self):
        mod = self.module()
        original = mod.run
        def mutate(*args):
            result = original(*args)
            self.source.write_bytes(b'PRIVATE_AFTER_RUN')
            return result
        with patch.object(mod, 'run', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        if (self.out / 'okf/result.json').exists():
            self.assertNotEqual(self.result()['status'], 'pass')

    def test_review_preview_retains_validated_inventory_after_observation(self):
        self.export()
        mod = self.module()
        original = mod._observe
        calls = 0
        document = self.bundle / 'contracts/q01_explosion.md'
        def mutate(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                document.write_bytes(b'PRIVATE_PERSISTENT_PREVIEW_MUTATION')
            return result
        with patch.object(mod, '_observe', side_effect=mutate):
            self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
        if (self.out / 'okf/result.json').exists():
            self.assertNotEqual(self.result()['status'], 'pass')
        self.assertEqual(document.read_bytes(), b'PRIVATE_PERSISTENT_PREVIEW_MUTATION')

    def test_review_export_retains_inventory_from_generated_byte_verification(self):
        mod = self.module()
        original = mod._verify_generated
        def mutate(bundle, files):
            result = original(bundle, files)
            if bundle == self.bundle:
                (bundle / 'contracts/q01_explosion.md').write_bytes(b'PRIVATE_POST_VERIFICATION_MUTATION')
            return result
        with patch.object(mod, '_verify_generated', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        if (self.out / 'okf/result.json').exists():
            self.assertNotEqual(self.result()['status'], 'pass')

    def test_review_cleanup_preserves_staging_newly_declared_by_configuration(self):
        mod = self.module()
        original = mod._observe
        calls = 0
        target = None
        raw = None
        def mutate(*args, **kwargs):
            nonlocal calls, target, raw
            calls += 1
            if calls == 2:
                staging = next(self.root.glob('.sxc-okf-*'))
                target = staging / 'contracts/q01_explosion.md'
                raw = target.read_bytes()
                self.path.write_text(self.path.read_text().replace('other.sql', target.relative_to(self.root).as_posix()), encoding='utf-8')
                self.assertEqual(config.load(self.path).gold[1].sql_path(config.load(self.path)), target)
            return original(*args, **kwargs)
        with patch.object(mod, '_observe', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertIsNotNone(target)
        self.assertTrue(target.exists(), 'observed configuration drift must suppress cleanup')
        self.assertEqual(target.read_bytes(), raw)

    def test_review_cleanup_leaves_staging_when_configuration_is_unreadable(self):
        mod = self.module()
        original = mod._observe
        calls = 0
        target = None
        def mutate(*args, **kwargs):
            nonlocal calls, target
            calls += 1
            if calls == 2:
                target = next(self.root.glob('.sxc-okf-*')) / 'contracts/q01_explosion.md'
                self.path.unlink()
            return original(*args, **kwargs)
        with patch.object(mod, '_observe', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertTrue(target.exists(), 'unreadable configuration cannot authorize cleanup')

    def test_review_valid_profile_distinguishes_trusted_input_drift(self):
        self.export()
        before = {p: p.read_bytes() for p in self.bundle.rglob('*') if p.is_file()}
        for path in (self.root / 'q01.sql', self.path):
            with self.subTest(kind=path.suffix):
                raw = path.read_bytes()
                path.write_bytes(raw + (b'\n# comment' if path == self.path else b'\n-- comment'))
                self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
                self.assertEqual(self.result()['checks'][0]['detail']['reason'], 'trusted_inputs_changed')
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                path.write_bytes(raw)

    def test_review_unknown_nested_metadata_remains_unsupported(self):
        self.export()
        original = self.manifest()
        for section in ('inputs', 'contract', 'source_bindings', 'binding'):
            with self.subTest(section=section):
                manifest = json.loads(json.dumps(original))
                target = (manifest['inputs']['contracts'][0] if section == 'contract' else
                          manifest['source_bindings']['items'][0] if section == 'binding' else manifest[section])
                target['verified'] = 'PRIVATE_UNTRUSTED_CLAIM'
                self.write_manifest(manifest)
                raw = (self.bundle / 'manifest.json').read_bytes()
                self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
                self.assertIn('unsupported_profile', self.last_output)
                self.assertNotIn('PRIVATE_UNTRUSTED_CLAIM', self.last_output)
                self.assertEqual((self.bundle / 'manifest.json').read_bytes(), raw)

    def test_review_configuration_selection_change_is_changed_evidence(self):
        self.export()
        self.path.write_text(self.path.read_text().replace('q01_explosion', 'renamed'), encoding='utf-8')
        self.assertNotEqual(self.cli('--import-preview', self.bundle), 0)
        self.assertIn('trusted_inputs_changed', self.last_output)

    def test_direct_run_protects_input_alias(self):
        cfg = config.load(self.path)
        result = self.module().run(cfg, SimpleNamespace(export=str(self.bundle), import_preview=None, select=['q01_explosion']), self.root)
        self.assertNotEqual(result.status, 'pass')
        self.assertFalse(self.bundle.exists())

    def test_direct_run_cannot_bind_stale_parsed_configuration(self):
        cfg = config.load(self.path)
        self.path.write_text(self.path.read_text().replace('PRIVATE_LABEL', 'CHANGED_LABEL'), encoding='utf-8')
        result = self.module().run(cfg, SimpleNamespace(export=str(self.bundle), import_preview=None, select=['q01_explosion']), self.out)
        self.assertNotEqual(result.status, 'pass')
        self.assertFalse(self.bundle.exists())

    def test_symlink_bundle_and_source_and_destination_parent_are_blocked(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.root / 'link'
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable for this Windows token')
        self.assertNotEqual(self.cli('--export', link / 'bundle', '--select', 'q01_explosion'), 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotEqual(self.cli('--import-preview', link), 0)
        self.source.unlink()
        target = outside / 'definition.md'
        target.write_bytes(b'PRIVATE_OUTSIDE')
        os.symlink(target, self.source)
        self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
        self.assertEqual(target.read_bytes(), b'PRIVATE_OUTSIDE')

    if os.name == 'nt':  # Platform-specific coverage is registered only where executable.
        def test_windows_junction_source_and_destination_parent_are_blocked(self):
            outside = self.root / 'outside'
            outside.mkdir()
            target = outside / 'definition.md'
            target.write_bytes(self.source.read_bytes())
            junction = self.root / 'junction'
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True)
            if created.returncode:
                self.skipTest('junction creation unavailable')
            self.addCleanup(os.rmdir, junction)
            self.assertNotEqual(self.cli('--export', junction / 'bundle', '--select', 'q01_explosion'), 0)
            self.assertFalse((outside / 'bundle').exists())
            self.path.write_text(self.path.read_text().replace('file="definition.md"', 'file="junction/definition.md"'), encoding='utf-8')
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)
            self.assertEqual(target.read_bytes(), b'PRIVATE_BUSINESS_BODY')

    def test_unsupported_os_publication_fails_closed(self):
        mod = self.module()
        source = self.root / 'staging'
        source.mkdir()
        with patch.object(mod.sys, 'platform', 'unsupported-os'):
            with self.assertRaises(mod._Issue):
                mod._publish(source, self.bundle)
        self.assertTrue(source.is_dir())
        self.assertFalse(self.bundle.exists())

    def test_real_cli_subprocess_roundtrip_and_required_config(self):
        script = pgfixture.SCRIPTS / 'sxc.py'
        common = [sys.executable, str(script), 'okf', '--config', str(self.path), '--out', str(self.out), '--quiet']
        exported = subprocess.run([*common, '--export', str(self.bundle), '--select', 'q01_explosion'], capture_output=True, text=True)
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        preview = subprocess.run([*common, '--import-preview', str(self.bundle)], capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        missing = subprocess.run([sys.executable, str(script), 'okf', '--import-preview', str(self.bundle)], capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn(str(self.root), exported.stdout + preview.stdout + missing.stdout + missing.stderr)

    def test_publication_mutation_cannot_produce_current_bundle_verdict(self):
        mod = self.module()
        original = mod._publish
        def mutate(staging, destination):
            original(staging, destination)
            (destination / 'contracts/q01_explosion.md').write_bytes(b'PRIVATE_POST_PUBLICATION_TAMPER')
        with patch.object(mod, '_publish', side_effect=mutate):
            self.assertNotEqual(self.cli('--export', self.bundle, '--select', 'q01_explosion'), 0)

    def test_diagnostic_never_enters_operational_required_pipeline(self):
        from semantic_crosscheck import report
        self.assertIn('okf', sxc.DIAGNOSTIC_STAGES)
        self.assertNotIn('okf', report.PIPELINE)
        self.assertNotIn('okf', report.ALWAYS_REQUIRED)


if __name__ == '__main__':
    unittest.main()
