"""Actual-runtime evidence is optional, bounded and independent of data revision evidence."""

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import config, provenance, report
from semantic_crosscheck.result import Result, StageError, inputs_fingerprint

if importlib.util.find_spec('semantic_crosscheck.runtime_provenance'):
    from semantic_crosscheck import runtime_provenance as runtime
else:
    runtime = None


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sxc_runtime_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'crosscheck.toml'
        self.path.write_text('version=1\n[source]\nschemas=["src"]\n', encoding='utf-8')
        self.cfg = config.load(self.path)

    def enabled(self):
        self.cfg.source.runtime_evidence = True

    def need_runtime(self):
        self.assertIsNotNone(runtime, 'actual-runtime evidence has not been implemented')

    def evidence(self, token='one'):
        return {'schema_version': 1, 'binding': 'bound', 'scope': 'full_pipeline',
                'sha256': hashlib.sha256(token.encode()).hexdigest(),
                'components': {'postgres': {'source': {'server_version_num': 170006},
                                             'cube': {'server_version_num': 170006}}}}

    def passed(self, stage='verify'):
        result = Result(stage)
        result.add('fixture', 'pass')
        return result

    def test_strict_optional_bool_and_format_v1_compatibility(self):
        self.assertIs(self.cfg.source.runtime_evidence, False)
        for literal in ('true', 'false'):
            self.path.write_text('version=1\n[source]\nschemas=["src"]\nruntime_evidence=' + literal + '\n', encoding='utf-8')
            self.assertIs(config.load(self.path).source.runtime_evidence, literal == 'true')
        for literal in ('1', '0', '"true"', '[]', '{}'):
            with self.subTest(literal=literal):
                self.path.write_text('version=1\n[source]\nschemas=["src"]\nruntime_evidence=' + literal + '\n', encoding='utf-8')
                with self.assertRaises(StageError) as raised:
                    config.load(self.path)
                self.assertIn('source.runtime_evidence', str(raised.exception))

    def test_disabled_has_no_runtime_pg_subprocess_or_install_probes(self):
        self.need_runtime()
        with mock.patch.object(runtime, '_postgres', side_effect=AssertionError('PG probe')), \
             mock.patch.object(runtime, '_version', side_effect=AssertionError('child probe')), \
             mock.patch.object(runtime, '_cube', side_effect=AssertionError('install probe')):
            start = provenance.begin(self.cfg, 'verify')
            result = provenance.finish(self.cfg, self.passed(), start)
            self.assertEqual(runtime.current_probe(self.cfg)['binding'], 'unbound')
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.runtime_evidence['binding'], 'unbound')
        self.assertTrue(any('runtime' in line and 'unbound' in line for line in result.not_proven))

    def test_stage_begin_end_independent_revision_and_runtime_bindings(self):
        self.need_runtime()
        self.enabled()
        with mock.patch.object(runtime, 'current_probe', return_value=self.evidence()) as probe:
            start = provenance.begin(self.cfg, 'verify')
            result = provenance.finish(self.cfg, self.passed(), start)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.provenance['binding'], 'unbound')
        self.assertTrue(result.runtime_evidence['stable'])
        self.assertEqual(result.runtime_evidence['start_sha256'], self.evidence()['sha256'])

    def test_changed_or_missing_end_fails_even_with_passing_cached_comparisons(self):
        self.need_runtime()
        self.enabled()
        for end in (self.evidence('two'), runtime.RuntimeEvidenceError('node_missing')):
            with self.subTest(end=type(end).__name__):
                with mock.patch.object(runtime, 'current_probe', side_effect=[self.evidence(), end]):
                    result = provenance.finish(self.cfg, self.passed(), provenance.begin(self.cfg, 'verify'))
                self.assertNotEqual(result.status, 'pass')
                self.assertFalse(result.runtime_evidence['stable'])

    def test_start_failure_is_sanitized_even_in_debug_traceback(self):
        self.need_runtime()
        self.enabled()
        secret = 'postgres://secret-host /private/local/path SECRET_TOKEN'
        with mock.patch.object(runtime, '_postgres', side_effect=RuntimeError(secret)):
            with self.assertRaises(StageError) as raised:
                provenance.begin(self.cfg, 'build')
        import traceback
        self.assertNotIn(secret, ''.join(traceback.format_exception(raised.exception)))

    def test_cli_start_failure_replaces_an_old_pass_and_never_executes_stage(self):
        self.need_runtime()
        self.path.write_text('version=1\n[source]\nschemas=["src"]\nruntime_evidence=true\n', encoding='utf-8')
        out = self.root / 'out'
        self.passed('build').write(out)
        runner = mock.Mock()
        with mock.patch.object(runtime, 'current_probe', side_effect=runtime.RuntimeEvidenceError('java_missing')), \
             mock.patch.object(sxc.importlib, 'import_module', return_value=mock.Mock(run=runner)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(['build', '--config', str(self.path), '--out', str(out)])
        self.assertEqual(code, 3)
        runner.assert_not_called()
        saved = json.loads((out / 'build' / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'error')
        self.assertIn('java_missing', json.dumps(saved))

    def test_actual_available_node_and_used_hermit_expose_only_bounded_tokens(self):
        self.need_runtime()
        if runtime.procutil.which('node'):
            self.assertRegex(runtime._version('node'), r'^[0-9]+\.[0-9]+\.[0-9]+$')
        if importlib.util.find_spec('owlready2'):
            actual = runtime._hermit()
            self.assertRegex(actual['jar_sha256'], r'^[0-9a-f]{64}$')
            self.assertNotIn(str(Path.home()), json.dumps(actual))

    def test_report_rejects_old_missing_changed_and_unavailable_runtime(self):
        self.need_runtime()
        self.enabled()
        with mock.patch.object(runtime, 'current_probe', return_value=self.evidence()):
            recorded = provenance.finish(self.cfg, self.passed(), provenance.begin(self.cfg, 'verify')).runtime_evidence
        self.assertIsNone(runtime.evidence_staleness(recorded, self.evidence()))
        self.assertEqual(runtime.evidence_staleness({}, self.evidence()), 'runtime_evidence_missing')
        self.assertEqual(runtime.evidence_staleness(recorded, self.evidence('two')), 'runtime_evidence_changed')
        self.assertEqual(runtime.evidence_staleness(recorded, {'binding': 'error'}), 'runtime_evidence_unavailable')
        for i, stage in enumerate(('build', 'cube', 'verify')):
            saved = self.passed(stage).to_dict()
            saved.update(inputs=inputs_fingerprint(self.cfg), runtime_evidence=copy.deepcopy(recorded),
                         started_at=f'2026-10-17T10:{2*i:02d}:00+0000',
                         finished_at=f'2026-10-17T10:{2*i+1:02d}:00+0000')
            target = self.root / 'out' / stage
            target.mkdir(parents=True)
            (target / 'result.json').write_text(json.dumps(saved), encoding='utf-8')
        for value, status in ((self.evidence(), 'pass'), (self.evidence('two'), 'not_run'),
                              (runtime.RuntimeEvidenceError('java_missing'), 'error')):
            with mock.patch.object(runtime, 'current_probe', side_effect=[value]):
                res = report.run(self.cfg, argparse.Namespace(), self.root / 'out')
            self.assertEqual(res.status, status)
        saved = json.loads((self.root / 'out' / 'build' / 'result.json').read_text())
        del saved['runtime_evidence']
        (self.root / 'out' / 'build' / 'result.json').write_text(json.dumps(saved))
        with mock.patch.object(runtime, 'current_probe', return_value=self.evidence()):
            self.assertEqual(report.run(self.cfg, argparse.Namespace(), self.root / 'out').status, 'not_run')

    def test_postgres_fixed_readonly_probe_validates_hashes_and_closes(self):
        self.need_runtime()
        row = ['170006'] + ['private-setting-' + str(i) for i in range(len(runtime.PG_SETTINGS))] + [None] * len(runtime.PG_NULLABLE_METADATA)
        conn = mock.Mock()
        conn.run.return_value = [row]
        statements = []
        conn.run.side_effect = lambda sql: statements.append(sql) or ([row] if sql == runtime.PG_SQL else [])
        with mock.patch.object(runtime, '_connect', return_value=conn):
            result = runtime._postgres('SXC_TEST_DSN', 1000)
        self.assertEqual(result['server_version_num'], 170006)
        self.assertRegex(result['settings_sha256'], r'^[0-9a-f]{64}$')
        self.assertNotIn('private-setting', json.dumps(result))
        self.assertEqual(statements[0], 'BEGIN TRANSACTION READ ONLY')
        self.assertEqual(statements[-1], 'ROLLBACK')
        conn.close.assert_called_once()

    def test_probe_components_fingerprint_actual_values_and_endpoint_mismatch(self):
        self.need_runtime()
        self.enabled()
        pg = {'server_version_num': 170006, 'settings_sha256': 'a' * 64}
        with mock.patch.object(runtime, '_postgres', return_value=pg), \
             mock.patch.object(runtime, '_version', side_effect=lambda kind: '22.13.1' if kind == 'node' else '21.0.2'), \
             mock.patch.object(runtime, '_hermit', return_value={'jar_sha256': 'b' * 64}), \
             mock.patch.object(runtime, '_cube', return_value={'installation_sha256': 'c' * 64}):
            first = runtime.current_probe(self.cfg)
            self.assertEqual(first, runtime.current_probe(self.cfg))
            self.assertEqual(first['scope'], 'full_pipeline')
        with mock.patch.object(runtime, '_postgres', side_effect=[pg, {**pg, 'settings_sha256': 'd' * 64}]), \
             self.assertRaises(runtime.RuntimeEvidenceError):
            runtime.current_probe(self.cfg)

    def test_child_argv_timeout_output_bounds_normalization_and_no_secret_env(self):
        self.need_runtime()
        for kind, output, expected in (('node', b'v22.13.1\n', '22.13.1'),
                                       ('java', b'openjdk version "21.0.2" 2024-01-16\n', '21.0.2')):
            with mock.patch.object(runtime.procutil, 'which', return_value='C:/safe/node.exe'), \
                 mock.patch.object(runtime.owl, 'java_executable', return_value='C:/safe/java.exe'), \
                 mock.patch.object(runtime, '_capture_version', return_value=output) as child:
                self.assertEqual(runtime._version(kind), expected)
                self.assertEqual(child.call_args.args[0][1:], ['--version'] if kind == 'node' else ['-version'])
        with mock.patch.object(runtime.procutil, 'which', return_value='C:/safe/node.exe'), \
             mock.patch.object(runtime, '_capture_version', return_value=b'v22.13.1 SECRET_TOKEN'):
            with self.assertRaises(runtime.RuntimeEvidenceError):
                runtime._version('node')

    def test_used_hermit_classpath_jar_hash_and_missing_jar(self):
        self.need_runtime()
        import owlready2.reasoning as reasoning
        jar = self.root / 'used.jar'
        jar.write_bytes(b'actual-hermit-one')
        with mock.patch.object(reasoning, '_HERMIT_CLASSPATH', str(self.root) + os.pathsep + str(jar)):
            first = runtime._hermit()
            jar.write_bytes(b'actual-hermit-two')
            self.assertNotEqual(first, runtime._hermit())
            self.assertNotIn(str(jar), json.dumps(first))
            jar.unlink()
            with self.assertRaises(runtime.RuntimeEvidenceError):
                runtime._hermit()

    def test_hermit_classpath_walk_is_bounded_and_binds_actual_loose_classes(self):
        self.need_runtime()
        import owlready2.reasoning as reasoning
        jar = self.root / 'used.jar'
        jar.write_bytes(b'actual-jar')
        with mock.patch.object(reasoning, '_HERMIT_CLASSPATH', str(self.root) + os.pathsep + str(jar)):
            baseline = runtime._hermit()
            (self.root / 'unexpected.class').write_bytes(b'loose bytecode')
            self.assertNotEqual(baseline['classpath_sha256'], runtime._hermit()['classpath_sha256'])
            (self.root / 'unexpected.class').unlink()
            with mock.patch.object(runtime, 'MAX_CLASSPATH_FILES', 1), self.assertRaises(runtime.RuntimeEvidenceError):
                runtime._hermit()

    def cube_fixture(self):
        home = self.root / 'selected-cube'
        home.mkdir()
        for name in runtime.cube_run.ADAPTER_FILES:
            shutil.copyfile(runtime.cube_run.ADAPTER_DIR / name, home / name)
        lock = json.loads((home / 'package-lock.json').read_text(encoding='utf-8'))['packages']
        for name in runtime._PACKAGES:
            package = home / 'node_modules' / name
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({'name': name, 'version': lock['node_modules/' + name]['version']}))
            (package / 'index.js').write_text('module.exports = {};')
        native = home / runtime.cube_run.INDEX_NODE
        native.parent.mkdir()
        native.write_bytes(b'synthetic-native')
        (home / runtime.cube_run.RECORD).write_text(json.dumps({
            'files_sha256': runtime.cube_run.adapter_hashes(),
            'native': {'index_node_sha256': hashlib.sha256(native.read_bytes()).hexdigest()}}))
        return home

    def test_selected_cube_actual_bytes_bind_changes_and_reject_incomplete_install(self):
        self.need_runtime()
        home = self.cube_fixture()
        with mock.patch.object(runtime.cube_run, 'cube_home', return_value=home):
            first = runtime._cube(self.cfg)
            self.assertEqual(first, runtime._cube(self.cfg))
            self.assertNotIn(str(home), json.dumps(first))
            source = home / 'node_modules' / 'express' / 'index.js'
            source.write_text('module.exports = {changed:true};')
            self.assertNotEqual(first['installation_sha256'], runtime._cube(self.cfg)['installation_sha256'])
            (home / runtime.cube_run.INDEX_NODE).write_bytes(b'changed-native')
            with self.assertRaises(runtime.RuntimeEvidenceError) as raised:
                runtime._cube(self.cfg)
            self.assertEqual(raised.exception.reason, 'cube_native_changed')

    def test_cube_package_version_adapter_and_file_limits_fail_closed(self):
        self.need_runtime()
        home = self.cube_fixture()
        with mock.patch.object(runtime.cube_run, 'cube_home', return_value=home):
            package = home / 'node_modules' / 'express' / 'package.json'
            original = package.read_bytes()
            package.write_text('{"name":"express","version":"99.0.0"}')
            with self.assertRaises(runtime.RuntimeEvidenceError):
                runtime._cube(self.cfg)
            package.write_bytes(original)
            with mock.patch.object(runtime, 'MAX_TREE_FILES', 1), self.assertRaises(runtime.RuntimeEvidenceError):
                runtime._cube(self.cfg)
            (home / 'server.js').write_text('changed adapter')
            with self.assertRaises(runtime.RuntimeEvidenceError) as raised:
                runtime._cube(self.cfg)
            self.assertEqual(raised.exception.reason, 'cube_adapter_changed')

    def test_cube_unreadable_directory_cannot_silently_drop_runtime_files(self):
        self.need_runtime()
        home = self.cube_fixture()
        def unreadable(_path, followlinks=False, onerror=None):
            if onerror:
                onerror(PermissionError('/private/secret-runtime-directory'))
            return iter([])
        with mock.patch.object(runtime.cube_run, 'cube_home', return_value=home), \
             mock.patch.object(runtime.os, 'walk', side_effect=unreadable), \
             self.assertRaises(runtime.RuntimeEvidenceError) as raised:
            runtime._cube(self.cfg)
        self.assertEqual(raised.exception.reason, 'cube_installation_unreadable')

    def test_child_capture_literal_argv_environment_deadline_and_output_limit(self):
        self.need_runtime()
        import io
        process = mock.Mock()
        process.stdout = io.BytesIO(b'v22.13.1\n')
        process.wait.return_value = 0
        process.poll.return_value = 0
        with mock.patch.object(runtime.subprocess, 'Popen', return_value=process) as spawn, \
             mock.patch.object(runtime, '_WindowsProbeJob') as job, \
             mock.patch.object(runtime.os, 'killpg', create=True), \
             mock.patch.object(runtime, '_available_pipe_chunk', side_effect=[b'v22.13.1\n', b'']), \
             mock.patch.dict(os.environ, {'NODE_OPTIONS': '--require secret.js', 'JAVA_TOOL_OPTIONS': '-javaagent:secret',
                                          'SXC_PG_DSN': 'postgresql://secret', 'SECRET_TOKEN': 'private'}):
            self.assertEqual(runtime._capture_version(['safe-node', '--version']), b'v22.13.1\n')
        args, kwargs = spawn.call_args
        self.assertEqual(args, (['safe-node', '--version'],))
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['bufsize'], 0)
        for key in ('NODE_OPTIONS', 'JAVA_TOOL_OPTIONS', 'SXC_PG_DSN', 'SECRET_TOKEN'):
            self.assertNotIn(key, kwargs['env'])
        self.assertGreater(process.wait.call_args_list[0].kwargs['timeout'], 0)
        self.assertLessEqual(process.wait.call_args_list[0].kwargs['timeout'], runtime.PROBE_TIMEOUT_S)
        process = mock.Mock()
        process.stdout = io.BytesIO(b'X' * (runtime.MAX_OUTPUT_BYTES + 1))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with mock.patch.object(runtime.subprocess, 'Popen', return_value=process), \
             mock.patch.object(runtime, '_WindowsProbeJob') as job, \
             mock.patch.object(runtime.os, 'killpg', create=True), \
             mock.patch.object(runtime, '_available_pipe_chunk', return_value=b'X' * (runtime.MAX_OUTPUT_BYTES + 1)), \
             self.assertRaises(runtime.RuntimeEvidenceError):
            runtime._capture_version(['safe-node', '--version'])
        if os.name == 'nt':
            job.return_value.close.assert_called_once()

    def test_actual_inherited_pipe_grandchild_cannot_extend_probe_deadline(self):
        self.need_runtime()
        pid_file = self.root / 'grandchild.pid'
        script = self.root / 'version-wrapper.py'
        script.write_text('import subprocess,sys\nfrom pathlib import Path\n'
                          'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(4)"], '
                          'stdout=sys.stdout,stderr=sys.stderr)\n'
                          'Path(sys.argv[1]).write_text(str(child.pid))\n'
                          'print("v22.13.1",flush=True)\n', encoding='utf-8')
        readers_before = len(threading.enumerate())
        started = time.monotonic()
        with mock.patch.object(runtime, 'PROBE_TIMEOUT_S', .25), self.assertRaises(runtime.RuntimeEvidenceError):
            runtime._capture_version([sys.executable, str(script), str(pid_file)])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5, 'inherited pipe writers must not delay closing the probe')
        self.assertEqual(len(threading.enumerate()), readers_before, 'a probe cannot leave a blocked reader thread')

    if os.name == 'nt':  # Platform-specific coverage is registered only where executable.
        def test_repeated_actual_probes_terminate_only_their_grandchildren_without_handle_leaks(self):
            self.need_runtime()
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.GetCurrentProcess.restype = wintypes.HANDLE
            api.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            api.OpenProcess.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            api.CloseHandle.argtypes = (wintypes.HANDLE,)
            def handle_count():
                count = wintypes.DWORD()
                self.assertTrue(api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(count)))
                return count.value
            script = self.root / 'child-tree.py'
            script.write_text('import subprocess,sys\nfrom pathlib import Path\n'
                              'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(4)"], '
                              'stdout=sys.stdout,stderr=sys.stderr)\n'
                              'Path(sys.argv[1]).write_text(str(child.pid))\n'
                              'print("v22.13.1",flush=True)\n', encoding='utf-8')
            before = handle_count()
            for index in range(3):
                pid_file = self.root / ('descendant-' + str(index) + '.pid')
                with mock.patch.object(runtime, 'PROBE_TIMEOUT_S', .25), self.assertRaises(runtime.RuntimeEvidenceError):
                    runtime._capture_version([sys.executable, str(script), str(pid_file)])
                pid = int(pid_file.read_text())
                child = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only for our recorded child PID
                if child:
                    try:
                        self.assertEqual(api.WaitForSingleObject(child, 500), 0, 'dedicated probe descendant must terminate')
                    finally:
                        api.CloseHandle(child)
            self.assertLessEqual(handle_count(), before + 1)

    def test_java_agent_environment_is_rejected_only_in_opt_in_mode_without_values(self):
        self.need_runtime()
        self.enabled()
        for key in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: '-javaagent:PRIVATE_SECRET_TOKEN'}), \
                 mock.patch.object(runtime, '_postgres') as pg, \
                 mock.patch.object(runtime, '_version', return_value='21.0.2'), \
                 mock.patch.object(runtime, '_hermit', return_value={}), \
                 mock.patch.object(runtime, '_cube', return_value={}):
                with self.assertRaises(runtime.RuntimeEvidenceError) as raised:
                    runtime.current_probe(self.cfg)
                self.assertEqual(raised.exception.reason, 'java_environment_unsupported')
                self.assertNotIn('PRIVATE_SECRET_TOKEN', str(raised.exception))
                pg.assert_not_called()
        self.cfg.source.runtime_evidence = False
        with mock.patch.dict(os.environ, {'JAVA_TOOL_OPTIONS': '-javaagent:PRIVATE_SECRET_TOKEN'}):
            self.assertEqual(runtime.current_probe(self.cfg)['binding'], 'unbound')

    def test_postgres_failure_cleanup_and_semantic_settings_changes_are_sanitized(self):
        self.need_runtime()
        secret = 'postgresql://secret-host/private-database SECRET_TOKEN'
        conn = mock.Mock()
        conn.run.side_effect = RuntimeError(secret)
        with mock.patch.object(runtime, '_connect', return_value=conn), self.assertRaises(StageError) as raised:
            runtime._postgres('PRIVATE_DSN', 1000)
        self.assertNotIn(secret, str(raised.exception))
        conn.close.assert_called_once()
        row = ['170006'] + ['setting'] * len(runtime.PG_SETTINGS) + [None] * len(runtime.PG_NULLABLE_METADATA)
        conn = mock.Mock()
        conn.run.side_effect = lambda sql: [row] if sql == runtime.PG_SQL else []
        with mock.patch.object(runtime, '_connect', return_value=conn):
            first = runtime._postgres('PRIVATE_DSN', 1000)
            row[1] = 'different-time-zone'
            self.assertNotEqual(first, runtime._postgres('PRIVATE_DSN', 1000))

    def test_nullable_database_locale_and_icu_rules_are_exact_hash_inputs(self):
        self.need_runtime()
        self.assertIn('datlocale', runtime.PG_SQL)
        self.assertIn('daticurules', runtime.PG_SQL)
        row = ['170006'] + ['setting'] * len(runtime.PG_SETTINGS) + [None, None, None, None]
        conn = mock.Mock()
        conn.run.side_effect = lambda sql: [row] if sql == runtime.PG_SQL else []
        with mock.patch.object(runtime, '_connect', return_value=conn):
            baseline = runtime._postgres('PRIVATE_DSN', 1000)
            row[-4] = 'en-US-private-locale'
            locale = runtime._postgres('PRIVATE_DSN', 1000)
            self.assertNotEqual(baseline['settings_sha256'], locale['settings_sha256'])
            row[-3] = '&z<private-icu-rule'
            rules = runtime._postgres('PRIVATE_DSN', 1000)
            self.assertNotEqual(locale['settings_sha256'], rules['settings_sha256'])
            row[-4:] = ['', None, None, None]
            empty = runtime._postgres('PRIVATE_DSN', 1000)
            self.assertNotEqual(baseline['settings_sha256'], empty['settings_sha256'])
        self.assertNotIn('private-locale', json.dumps(locale))
        self.assertNotIn('private-icu-rule', json.dumps(rules))


if __name__ == '__main__':
    unittest.main()
