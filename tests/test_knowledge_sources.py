"""Opaque authored source pins: synthetic filesystem and no database connections."""
import hashlib
import contextlib
import importlib
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import config
from semantic_crosscheck.result import Result, StageError
from test_expect import BASE, GOLD, CASE, args


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_source_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'crosscheck.toml'
        self.source = self.root / 'definition.md'
        self.raw = b'private synthetic definition\x00\xff'
        self.source.write_bytes(self.raw)
        (self.root / 'q01.sql').write_text('SELECT 1', encoding='utf-8')
        self.out = self.root / 'out'

    def load(self, binding=None, extra='', source='authored'):
        if binding is None:
            binding = {'file': 'definition.md', 'sha256': digest(self.raw),
                       'object_id': 'private-object', 'revision': 'private-revision'}
        line = 'spec_source = { ' + ', '.join(k + ' = ' + json.dumps(v) for k, v in binding.items()) + ' }\n'
        self.path.write_text(BASE + GOLD + CASE.replace('source = "authored"', 'source = ' + json.dumps(source)) + line + extra,
                             encoding='utf-8')
        return config.load(self.path)

    def module(self):
        return importlib.import_module('semantic_crosscheck.knowledge_sources')

    def cases(self, cfg):
        return [(gi, g, i, c) for gi, g in enumerate(cfg.gold) for i, c in enumerate(g.expect)]

    def test_bound_authored_case_loads_and_missing_or_changed_stops_before_database(self):
        # Baseline rejects this valid nested table; this is the behavioral RED.
        cfg = self.load()
        expect = importlib.import_module('semantic_crosscheck.expect')
        for value in (None, b'changed synthetic definition'):
            if value is None:
                self.source.unlink()
            else:
                self.source.write_bytes(value)
            with patch.object(expect.db, 'connect', side_effect=AssertionError('DB must not connect')):
                result = expect.run(cfg, args(), self.out)
            self.assertNotEqual(result.status, 'pass')
            self.assertEqual(next(c.status for c in result.checks if c.id == 'expect:q01_explosion[0]'), 'not_run')
            self.assertEqual(result.inputs['spec_sources']['items'][0]['state'], 'missing' if value is None else 'changed')

    def test_closed_schema_and_private_validation_messages(self):
        invalid = [None, [], 'private-value', {}, {'file': 'definition.md'},
                   {'file': 'definition.md', 'sha256': 'A' * 64},
                   {'file': 'definition.md', 'sha256': digest(self.raw), 'private-key': 'private-value'},
                   {'file': 'definition.md', 'sha256': digest(self.raw), 'revision': 'x' * 129},
                   {'file': 'definition.md', 'sha256': digest(self.raw), 'object_id': 'bad\nlabel'}]
        for binding in invalid:
            with self.subTest(kind=type(binding).__name__):
                with self.assertRaises(StageError) as raised:
                    self.module().validate_spec_source(binding, 'gold[0] expect[0]')
                self.assertNotIn('private', str(raised.exception))
        with self.assertRaises(StageError):
            self.load(source='captured')

    def test_unsafe_paths_are_rejected_without_echo(self):
        invalid = ['', '.', '../definition.md', './definition.md', 'a//b', 'a/./b', 'a/../b',
                   '/definition.md', 'C:definition.md', 'C:/definition.md', '//host/share',
                   'a\\..\\b', '.env', 'sub/.hidden', 'secrets.txt', 'credentials.json',
                   'NUL.txt', 'sub/COM1', 'sub/LPT9.md', 'sub/AUX', 'a.', 'a ',
                   'sub/CONIN$.txt', 'sub/a:b', 'a\x00b', 'a\nb', 'x' * 513]
        for file in invalid:
            with self.subTest(index=invalid.index(file)):
                with self.assertRaises(StageError) as raised:
                    self.module().validate_spec_source({'file': file, 'sha256': digest(self.raw)}, 'gold[0]')
                self.assertNotIn(file, str(raised.exception)) if len(file) > 4 else None

    def test_current_snapshot_closed_public_metadata_and_opaque_bytes(self):
        cfg = self.load()
        snap = self.module().capture(cfg, self.cases(cfg), self.out)
        self.assertTrue(snap.ok)
        public = snap.public()
        self.assertEqual(set(public), {'schema', 'count', 'current', 'digest', 'items'})
        self.assertEqual((public['schema'], public['count'], public['current']), ('sxc-spec-sources/v1', 1, 1))
        self.assertEqual(set(public['items'][0]), {'gold_index', 'case_index', 'state', 'expected_sha256',
                                                  'observed_sha256', 'binding_sha256'})
        self.assertEqual(public['items'][0]['observed_sha256'], digest(self.raw))
        encoded = json.dumps(public)
        for private in ('definition.md', 'private-object', 'private-revision', str(self.root), 'synthetic'):
            self.assertNotIn(private, encoded)
        changed = self.load({'file': 'definition.md', 'sha256': digest(self.raw), 'revision': 'other'})
        other = self.module().capture(changed, self.cases(changed))
        self.assertNotEqual(public['items'][0]['binding_sha256'], other.public()['items'][0]['binding_sha256'])

    def test_missing_blocked_and_unavailable_are_distinct(self):
        cfg = self.load()
        mod = self.module()
        self.source.unlink()
        self.assertEqual(mod.capture(cfg, self.cases(cfg)).public()['items'][0]['state'], 'missing')
        self.source.mkdir()
        self.assertEqual(mod.capture(cfg, self.cases(cfg)).public()['items'][0]['state'], 'blocked')
        self.source.rmdir()
        self.source.write_bytes(self.raw)
        original = os.open
        def denied(path, *a, **kw):
            if Path(path) == self.source:
                raise PermissionError('private-path')
            return original(path, *a, **kw)
        with patch.object(mod.os, 'open', side_effect=denied):
            snap = mod.capture(cfg, self.cases(cfg))
        self.assertEqual(snap.public()['items'][0]['state'], 'unavailable')
        self.assertNotIn('private-path', json.dumps(snap.public()))

    def test_output_ancestors_and_aliases_leave_all_inputs_untouched(self):
        cfg = self.load()
        mod = self.module()
        before = self.source.read_bytes()
        for out in (self.root, self.source, self.source / 'child'):
            with self.subTest(depth=len(out.parts)), self.assertRaises(StageError):
                mod.preflight(cfg, out)
            self.assertEqual(self.source.read_bytes(), before)
        stage = self.out / 'expect'
        stage.mkdir(parents=True)
        target = stage / 'result.json'
        for original in (self.source, self.path, self.root / 'q01.sql'):
            try:
                os.link(original, target)
            except OSError:
                self.skipTest('hardlink unavailable')
            try:
                with self.assertRaises(StageError):
                    mod.preflight(cfg, self.out)
                self.assertTrue(target.exists())
            finally:
                target.unlink()

    def test_symlink_source_and_output_parent_are_blocked_before_cleanup(self):
        cfg = self.load()
        original = self.root / 'original.md'
        self.source.rename(original)
        try:
            self.source.symlink_to(original)
        except OSError:
            self.skipTest('symlink creation unavailable')
        mod = self.module()
        self.assertEqual(mod.capture(cfg, self.cases(cfg)).public()['items'][0]['state'], 'blocked')
        with self.assertRaises(StageError):
            mod.preflight(cfg, self.out)
        self.source.unlink()
        original.rename(self.source)
        self.out.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(StageError):
            mod.preflight(cfg, self.out)

    def test_capture_rechecks_earlier_files_after_scanning_all_cases(self):
        cfg = self.load()
        second = self.root / 'second.md'
        second.write_bytes(self.raw)
        case = config.Expect('authored', [], spec='second', spec_source=self.module().SourceBinding('second.md', digest(self.raw)))
        cfg.gold[0].expect.append(case)
        mod = self.module()
        original = os.open
        mutated = False
        def mutate(path, *a, **kw):
            nonlocal mutated
            if Path(path) == second and not mutated:
                self.source.write_bytes(b'changed during scan')
                mutated = True
            return original(path, *a, **kw)
        with patch.object(mod.os, 'open', side_effect=mutate):
            snap = mod.capture(cfg, self.cases(cfg))
        self.assertFalse(snap.ok)
        self.assertEqual([i['state'] for i in snap.public()['items']], ['changed', 'current'])

    def test_finish_demotes_only_affected_passing_cases_and_never_promotes(self):
        cfg = self.load()
        mod = self.module()
        snap = mod.capture(cfg, self.cases(cfg))
        for prior in ('pass', 'fail', 'error', 'not_run'):
            self.source.write_bytes(b'changed')
            result = Result('expect')
            check = result.add('expect:q01_explosion[0]', prior)
            result.add('expect:unbound[0]', 'pass')
            latest = mod.finish(snap, result)
            self.assertFalse(latest.ok)
            self.assertEqual(check.status, 'not_run' if prior == 'pass' else prior)
            self.assertEqual(result.checks[1].status, 'pass')
            self.source.write_bytes(self.raw)
            restored = mod.finish(latest, result)
            self.assertFalse(restored.ok)
            self.assertNotEqual(check.status, 'pass')

    def test_legacy_unbound_has_no_reads_or_namespace_or_case_limit(self):
        self.path.write_text(BASE + GOLD + CASE * 70, encoding='utf-8')
        cfg = config.load(self.path)
        mod = self.module()
        with patch.object(mod.os, 'open', side_effect=AssertionError('no source reads')):
            snap = mod.capture(cfg, self.cases(cfg))
            result = Result('expect')
            mod.finish(snap, result)
            mod.preflight(cfg, self.root)
        self.assertTrue(snap.ok)
        self.assertEqual(result.inputs, {})
        self.assertEqual(result.checks, [])

    def test_limits_bound_cases_file_and_total_bytes(self):
        cfg = self.load()
        mod = self.module()
        self.raw = b'x' * (256 * 1024)
        self.source.write_bytes(self.raw)
        cfg = self.load()
        self.assertTrue(mod.capture(cfg, self.cases(cfg)).ok)
        self.source.write_bytes(self.raw + b'x')
        self.assertEqual(mod.capture(cfg, self.cases(cfg)).public()['items'][0]['state'], 'blocked')
        self.source.write_bytes(self.raw)
        cfg.gold[0].expect *= 16
        self.assertTrue(mod.capture(cfg, self.cases(cfg)).ok)
        cfg.gold[0].expect.append(cfg.gold[0].expect[0])
        self.assertFalse(mod.capture(cfg, self.cases(cfg)).ok)
        self.source.write_bytes(b'small')
        cfg = self.load({'file': 'definition.md', 'sha256': digest(b'small')})
        cfg.gold[0].expect *= 64
        self.assertTrue(mod.capture(cfg, self.cases(cfg)).ok)
        cfg.gold[0].expect.append(cfg.gold[0].expect[0])
        with self.assertRaises(StageError):
            mod.capture(cfg, self.cases(cfg))

    def test_real_cli_current_namespace_and_private_failure_output(self):
        self.load()
        self.source.unlink()
        run = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'expect', '--config', str(self.path),
                              '--out', str(self.out)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        public = json.loads((self.out / 'expect' / 'result.json').read_text(encoding='utf-8'))
        self.assertIn('spec_sources', public['inputs'])
        for private in ('definition.md', 'private-object', 'private-revision', str(self.root)):
            self.assertNotIn(private, json.dumps(public) + run.stdout + run.stderr)

    def test_real_cli_malformed_binding_protects_stale_result(self):
        self.load()
        self.out = self.root / 'output'
        stage = self.out / 'expect'
        stage.mkdir(parents=True)
        result = stage / 'result.json'
        result.write_bytes(b'previous result bytes')
        self.path.write_text(self.path.read_text(encoding='utf-8').replace('definition.md', '../private.md'), encoding='utf-8')
        run = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'expect', '--config', str(self.path),
                              '--out', str(self.out)], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(result.read_bytes(), b'previous result bytes')
        self.assertNotIn('private.md', run.stdout + run.stderr)

    def test_direct_run_repeats_protection_and_rechecks_after_execution(self):
        cfg = self.load()
        expect = importlib.import_module('semantic_crosscheck.expect')
        with patch.object(expect.db, 'connect', side_effect=AssertionError('must not connect')):
            with self.assertRaises(StageError):
                expect.run(cfg, args(), self.root)
        def stage(result, *unused):
            result.add('expect:q01_explosion[0]', 'pass')
            self.source.write_bytes(b'changed during execution')
        conn = SimpleNamespace(close=lambda: None)
        with patch.object(expect, 'fixture_env', return_value='synthetic'), \
                patch.object(expect, 'operational_variables', return_value=([], [])), \
                patch.object(expect.db, 'connect', return_value=conn), \
                patch.object(expect, '_stage', side_effect=stage):
            result = expect.run(cfg, args(), self.out)
        self.assertEqual(next(c.status for c in result.checks if c.id == 'expect:q01_explosion[0]'), 'not_run')
        self.assertEqual(result.inputs['spec_sources']['items'][0]['state'], 'changed')

    def test_irrelevant_change_and_exact_restoration_are_current_without_execution_claim(self):
        cfg = self.load()
        mod = self.module()
        before = mod.capture(cfg, self.cases(cfg)).public()
        (self.root / 'unrelated.txt').write_bytes(b'irrelevant')
        self.assertEqual(mod.capture(cfg, self.cases(cfg)).public(), before)
        self.source.write_bytes(b'changed')
        self.source.write_bytes(self.raw)
        self.assertTrue(mod.capture(cfg, self.cases(cfg)).ok)

    def test_descriptor_metadata_drift_is_changed_even_if_bytes_match(self):
        cfg = self.load()
        mod = self.module()
        real = os.fstat
        calls = 0
        def drift(fd):
            nonlocal calls
            calls += 1
            value = real(fd)
            if calls == 2:
                os.utime(self.source, ns=(value.st_atime_ns, value.st_mtime_ns + 1000000))
                value = real(fd)
            return value
        with patch.object(mod.os, 'fstat', side_effect=drift):
            snap = mod.capture(cfg, self.cases(cfg))
        self.assertEqual(snap.public()['items'][0]['state'], 'changed')

    def test_cli_preserves_only_explicit_source_namespace(self):
        import sxc
        cfg = self.load()
        expect = importlib.import_module('semantic_crosscheck.expect')
        public = self.module().capture(cfg, self.cases(cfg)).public()
        result = Result('expect')
        result.add('expect:q01_explosion[0]', 'pass')
        result.inputs = {'spec_sources': public, 'arbitrary_private_namespace': {'value': 'private-value'}}
        with patch.object(expect, 'run', return_value=result), contextlib.redirect_stdout(io.StringIO()):
            code = sxc.main(['expect', '--config', str(self.path), '--out', str(self.out)])
        self.assertEqual(code, 0)
        stored = json.loads((self.out / 'expect' / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['inputs']['spec_sources'], public)
        self.assertNotIn('arbitrary_private_namespace', stored['inputs'])

    def test_binding_label_and_path_limits_accept_exact_boundaries(self):
        mod = self.module()
        file = '/'.join(['a' * 127] * 3 + ['b' * 128])
        self.assertEqual(len(file), 512)
        value = {'file': file, 'sha256': 'a' * 64, 'object_id': 'x' * 128, 'revision': 'y' * 128}
        self.assertEqual(mod.validate_spec_source(value, 'gold[0]').file, file)
        for key in ('file', 'object_id', 'revision'):
            over = dict(value)
            over[key] += 'x'
            with self.assertRaises(StageError):
                mod.validate_spec_source(over, 'gold[0]')

    def test_public_metadata_limit_is_fail_closed(self):
        cfg = self.load()
        mod = self.module()
        with patch.object(mod, 'MAX_METADATA', 1), self.assertRaises(StageError):
            mod.capture(cfg, self.cases(cfg))

    def test_selected_configuration_root_is_canonicalized_once(self):
        self.load()
        alias = self.root / 'root-alias'
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        cfg = config.load(alias / self.path.name)
        self.assertTrue(self.module().capture(cfg, self.cases(cfg), self.out).ok)

    def test_real_cli_output_input_alias_never_deletes_source(self):
        self.out.mkdir()
        stage = self.out / 'expect'
        stage.mkdir()
        target = stage / 'result.json'
        target.write_bytes(self.raw)
        self.load({'file': 'out/expect/result.json', 'sha256': digest(self.raw)})
        run = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'expect', '--config', str(self.path),
                              '--out', str(self.out)], capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(target.read_bytes(), self.raw)

    def test_result_writer_never_truncates_a_preexisting_fixed_part_hardlink(self):
        stage = self.out / 'expect'
        stage.mkdir(parents=True)
        part = stage / 'result.json.part'
        try:
            os.link(self.source, part)
        except OSError:
            self.skipTest('hardlink unavailable')
        result = Result('expect')
        result.add('expect:synthetic[0]', 'pass')
        result.write(self.out)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(part.read_bytes(), self.raw)

    def test_configuration_mutation_during_capture_and_execution_never_returns_pass(self):
        cfg = self.load()
        mod = self.module()
        real = os.open
        mutated = False
        def change(path, *a, **kw):
            nonlocal mutated
            if Path(path) == self.source and not mutated:
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write('\n# changed during source capture\n')
                mutated = True
            return real(path, *a, **kw)
        with patch.object(mod.os, 'open', side_effect=change), self.assertRaises(StageError):
            mod.capture(cfg, self.cases(cfg))
        cfg = self.load()
        snap = mod.capture(cfg, self.cases(cfg))
        self.path.write_text(self.path.read_text(encoding='utf-8') + '\n# changed during execution\n', encoding='utf-8')
        result = Result('expect')
        result.add('expect:q01_explosion[0]', 'pass')
        with self.assertRaises(StageError):
            mod.finish(snap, result)

    def test_final_and_error_writes_repeat_alias_protection(self):
        import sxc
        self.load()
        expect = importlib.import_module('semantic_crosscheck.expect')
        stage = self.out / 'expect'
        stage.mkdir(parents=True)
        target = stage / 'result.json'
        for mode in ('pass', 'error'):
            def execute(*unused):
                os.link(self.source, target)
                if mode == 'error':
                    raise StageError('private exception text', 2)
                result = Result('expect')
                result.add('expect:q01_explosion[0]', 'pass')
                return result
            output, error = io.StringIO(), io.StringIO()
            with patch.object(expect, 'run', side_effect=execute), contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(error):
                code = sxc.main(['expect', '--config', str(self.path), '--out', str(self.out)])
            self.assertNotEqual(code, 0)
            self.assertEqual(self.source.read_bytes(), self.raw)
            self.assertEqual(target.read_bytes(), self.raw)
            self.assertNotIn('private exception text', output.getvalue() + error.getvalue())
            target.unlink()

    def test_windows_junction_parent_is_blocked_without_reading_target(self):
        if os.name != 'nt':
            self.skipTest('Windows junction fixture')
        actual = self.root / 'actual'
        actual.mkdir()
        (actual / 'definition.md').write_bytes(self.raw)
        junction = self.root / 'junction'
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(actual)], capture_output=True)
        if created.returncode:
            self.skipTest('junction creation unavailable')
        self.addCleanup(os.rmdir, junction)
        cfg = self.load({'file': 'junction/definition.md', 'sha256': digest(self.raw)})
        mod = self.module()
        with patch.object(mod.os, 'open', side_effect=AssertionError('must not read target')):
            snapshot = mod.capture(cfg, self.cases(cfg))
            self.assertEqual(snapshot.public()['items'][0]['state'], 'blocked')
            with self.assertRaises(StageError):
                mod.preflight(cfg, self.out)

    def test_metadata_observation_has_no_database_network_or_subprocess_effects(self):
        cfg = self.load()
        mod = self.module()
        expect = importlib.import_module('semantic_crosscheck.expect')
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('subprocess forbidden')), \
                patch.object(expect.db, 'connect', side_effect=AssertionError('database forbidden')):
            snap = mod.capture(cfg, self.cases(cfg), self.out)
            final = mod.finish(snap, Result('expect'))
        self.assertTrue(final.ok)

    def test_actual_expect_configuration_drift_preserves_newly_declared_source(self):
        import sxc
        self.load()
        text = self.path.read_text(encoding='utf-8')
        expect = importlib.import_module('semantic_crosscheck.expect')
        target = self.out / 'expect' / 'result.json'
        new = b'new synthetic opaque source must remain intact'
        def stage(result, *unused):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new)
            self.path.write_text(text.replace('definition.md', 'out/expect/result.json')
                                 .replace(digest(self.raw), digest(new)), encoding='utf-8')
            result.add('expect:q01_explosion[0]', 'pass')
        output = io.StringIO()
        with patch.object(expect, 'fixture_env', return_value='synthetic'), \
                patch.object(expect, 'operational_variables', return_value=([], [])), \
                patch.object(expect.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
                patch.object(expect, '_stage', side_effect=stage), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(output):
            code = sxc.main(['expect', '--config', str(self.path), '--out', str(self.out)])
        self.assertNotEqual(code, 0)
        self.assertTrue(target.read_bytes() == new, 'newly declared source bytes must survive error publication')
        self.assertNotIn(str(self.root), output.getvalue())

    def test_all_publication_paths_suppress_writes_on_changed_or_unreadable_configuration(self):
        import sxc
        expect = importlib.import_module('semantic_crosscheck.expect')
        target = self.out / 'expect' / 'result.json'
        new = b'preserved synthetic boundary bytes'
        for action in ('pass', 'stage_error', 'interrupt', 'exception'):
            for drift in ('changed', 'missing'):
                with self.subTest(action=action, drift=drift):
                    self.load()
                    def execute(*unused):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(new)
                        if drift == 'missing':
                            self.path.unlink()
                        else:
                            self.path.write_bytes(b'configuration changed at execution boundary')
                        if action == 'stage_error':
                            raise StageError('private error text', 2)
                        if action == 'interrupt':
                            raise KeyboardInterrupt()
                        if action == 'exception':
                            raise RuntimeError('private error text')
                        result = Result('expect')
                        result.add('expect:q01_explosion[0]', 'pass')
                        return result
                    output = io.StringIO()
                    with patch.object(expect, 'run', side_effect=execute), contextlib.redirect_stdout(output), \
                            contextlib.redirect_stderr(output):
                        code = sxc.main(['expect', '--config', str(self.path), '--out', str(self.out)])
                    self.assertNotEqual(code, 0)
                    self.assertTrue(target.read_bytes() == new, 'changed or unreadable configuration blocks publication')
                    self.assertNotIn('private error text', output.getvalue())

    def test_verified_unbound_consent_error_retains_legacy_actionable_message(self):
        import sxc
        self.path.write_text(BASE + GOLD + CASE, encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = sxc.main(['expect', '--config', str(self.path), '--out', str(self.out)])
        self.assertEqual(code, 2)
        stored = json.loads((self.out / 'expect' / 'result.json').read_text(encoding='utf-8'))
        self.assertIn('--allow-fixture-database', stored['checks'][0]['detail'].get('message', ''))
        self.assertIn('--allow-fixture-database', output.getvalue())

    def test_configuration_drift_before_cleanup_preserves_new_source(self):
        import sxc
        self.load()
        text = self.path.read_text(encoding='utf-8')
        target = self.out / 'expect' / 'result.json'
        new = b'new synthetic source before cleanup'
        ensure = sxc.ensure_out
        def change(out):
            ensure(out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new)
            self.path.write_text(text.replace('definition.md', 'out/expect/result.json')
                                 .replace(digest(self.raw), digest(new)), encoding='utf-8')
        with patch.object(sxc, 'ensure_out', side_effect=change), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(['expect', '--config', str(self.path), '--out', str(self.out)])
        self.assertNotEqual(code, 0)
        self.assertTrue(target.read_bytes() == new, 'configuration drift suppresses stale cleanup')


if __name__ == '__main__':
    unittest.main()
