"""Real opt-in runtime guards on the disposable CI stack; no probe mocks.

SXC_TEST_FULLSTACK=1 registers these tests in the provisioned integration job.
Core-only unit jobs do not register them. Once requested, absent prerequisites
are failures, never skips. Each run owns its database and roles; no operational
database or existing Cube installation is modified.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import pgfixture
from semantic_crosscheck import config, cube_run, db, owl, runtime_provenance as runtime
from semantic_crosscheck.result import Result


@contextlib.contextmanager
def environment(**changes):
    previous = {key: os.environ.get(key) for key in changes}
    try:
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if os.environ.get('SXC_TEST_FULLSTACK') == '1':
    class RuntimeFullStackTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            if not pgfixture.available():
                raise AssertionError('SXC_TEST_FULLSTACK requires a disposable SXC_TEST_ADMIN_DSN')
            if not owl.java_executable():
                raise AssertionError('SXC_TEST_FULLSTACK requires Java')
            if not cube_run.installed(cube_run.cube_home()):
                raise AssertionError('SXC_TEST_FULLSTACK requires the installed pinned Cube adapter')

            # PgFixture changes these two process variables. Restore them after
            # dropping this database, so other integration classes keep theirs.
            cls.env = environment(SXC_PG_DSN=None, SXC_PG_OWNER_DSN=None)
            cls.env.__enter__()
            cls.addClassCleanup(cls.env.__exit__, None, None, None)
            cls.temporary = tempfile.TemporaryDirectory(prefix='sxc_runtime_fullstack_')
            cls.addClassCleanup(cls.temporary.cleanup)
            cls.root = Path(cls.temporary.name)
            cls.fx = pgfixture.PgFixture('sxc_runtime_fullstack',
                                        owner='xc_runtime_owner', check='xc_runtime_check')
            # Register teardown before setup so a partial setup is cleaned too.
            cls.addClassCleanup(cls.fx.teardown)
            cls.fx.setup()
            source = pgfixture.ROOT / 'demo'
            demo = cls.root / 'demo'
            shutil.copytree(source, demo)
            cls.path = demo / 'crosscheck.toml'
            text = cls.path.read_text(encoding='utf-8')
            if text.count('[source]\n') != 1:
                raise AssertionError('the demo must have exactly one source section')
            cls.path.write_text(text.replace('[source]\n', '[source]\nruntime_evidence = true\n', 1),
                                encoding='utf-8', newline='\n')
            cls.cfg = config.load(cls.path)
            cls.out = cls.root / 'out'
            cls.script = pgfixture.SCRIPTS / 'sxc.py'
            for stage in ('build', 'reason', 'cube', 'verify', 'report'):
                cls.run_cli(stage)

        @classmethod
        def run_cli(cls, stage, expected=0, out=None):
            target = cls.out if out is None else Path(out)
            command = [sys.executable, str(cls.script), stage, '--config', str(cls.path), '--out', str(target)]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
            if completed.returncode != expected:
                raise AssertionError(f'actual runtime {stage}: exit {completed.returncode}, expected {expected}\n'
                                     + (completed.stdout + completed.stderr)[-8000:])
            return json.loads((target / stage / 'result.json').read_text(encoding='utf-8'))

        def summary(self):
            return json.loads((self.out / 'report.json').read_text(encoding='utf-8'))

        def test_clean_pipeline_binds_actual_postgres_node_java_hermit_and_cube(self):
            import owlready2.reasoning as reasoning

            self.assertIs(self.cfg.source.runtime_evidence, True)
            summary = self.summary()
            self.assertEqual(summary['status'], 'pass')
            current = summary['runtime_evidence']
            self.assertEqual(current['binding'], 'bound')
            self.assertEqual(current['scope'], 'full_pipeline')
            components = current['components']
            for env_name, role in ((self.cfg.source.dsn_env, 'source'), (self.cfg.cube_dsn_env(), 'cube')):
                conn = db.connect(env_name)
                try:
                    actual_version = int(conn.run("SELECT current_setting('server_version_num')")[0][0])
                finally:
                    conn.close()
                self.assertGreaterEqual(actual_version, 170000)
                self.assertEqual(components['postgres'][role]['server_version_num'], actual_version)
                self.assertRegex(components['postgres'][role]['settings_sha256'], r'^[0-9a-f]{64}$')
            self.assertEqual(components['postgres']['source'], components['postgres']['cube'])
            node = subprocess.run(['node', '--version'], capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(components['node_version'], node.stdout.strip().removeprefix('v'))
            java = subprocess.run([owl.java_executable(), '-version'], capture_output=True,
                                  text=True, check=True, timeout=10)
            self.assertIn(components['java_version'], java.stdout + java.stderr)
            jars = [Path(entry) for entry in reasoning._HERMIT_CLASSPATH.split(os.pathsep)
                    if Path(entry).suffix.lower() == '.jar']
            self.assertEqual(len(jars), 1)
            self.assertEqual(components['hermit']['jar_sha256'], hashlib.sha256(jars[0].read_bytes()).hexdigest())
            self.assertRegex(components['hermit']['classpath_sha256'], r'^[0-9a-f]{64}$')
            home = cube_run.cube_home(self.cfg.cube.version)
            self.assertEqual(components['cube']['native_sha256'],
                             hashlib.sha256((home / cube_run.INDEX_NODE).read_bytes()).hexdigest())
            self.assertGreater(components['cube']['file_count'], 0)
            self.assertEqual((components['cube']['cube_version'], components['cube']['sql_planner']),
                             (self.cfg.cube.version, 'tesseract'))
            cube_meta = next(c for c in json.loads((self.out / 'cube' / 'result.json').read_text(encoding='utf-8'))
                             ['checks'] if c['id'] == 'cube:meta')
            self.assertEqual(cube_meta['detail']['sql_planner'], components['cube']['sql_planner'])
            for package, version in components['cube']['package_versions'].items():
                installed = json.loads((home / 'node_modules' / package / 'package.json').read_text(encoding='utf-8'))
                self.assertEqual(version, installed['version'])
            for stage in ('build', 'reason', 'cube', 'verify'):
                saved = json.loads((self.out / stage / 'result.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['status'], 'pass', stage)
                evidence = saved['runtime_evidence']
                self.assertEqual(evidence['binding'], 'bound', stage)
                self.assertIs(evidence['stable'], True, stage)
                self.assertEqual(evidence['start_sha256'], current['sha256'], stage)
                self.assertEqual(evidence['end_sha256'], current['sha256'], stage)
            encoded = json.dumps(current)
            private_values = (str(self.root), str(home), 'postgresql://', os.environ.get('SXC_TEST_ROLE_PASSWORD'))
            for private_value in private_values:
                if private_value:
                    self.assertTrue(private_value not in encoded, 'runtime evidence must not expose private values')

        def test_database_setting_change_blocks_stage_finish_and_cached_report(self):
            conn = self.fx.check_conn()
            try:
                old_zone = conn.run("SELECT current_setting('TimeZone')")[0][0]
            finally:
                conn.close()
            zone = 'Pacific/Honolulu' if old_zone == 'UTC' else 'UTC'
            start = runtime.begin(self.cfg, 'verify')
            admin = pgfixture._admin()
            try:
                admin.run('ALTER ROLE "xc_runtime_check" IN DATABASE "sxc_runtime_fullstack" '
                          f"SET TimeZone TO '{zone}'")
                finished = Result('verify')
                finished.add('previous_comparison', 'pass')
                runtime.finish(self.cfg, finished, start)
                self.assertEqual(finished.status, 'fail')
                self.assertEqual(finished.runtime_evidence['binding'], 'changed')
                self.assertIs(finished.runtime_evidence['stable'], False)
                self.run_cli('report', expected=1)
                summary = self.summary()
                self.assertEqual(summary['status'], 'not_run')
                self.assertTrue(any(item['why'] == 'runtime_evidence_changed' for item in summary['stale']))
            finally:
                try:
                    admin.run('ALTER ROLE "xc_runtime_check" IN DATABASE "sxc_runtime_fullstack" RESET TimeZone')
                finally:
                    admin.close()
            self.run_cli('report')

        def test_missing_selected_cube_blocks_report_and_replaces_cached_stage_pass(self):
            missing_home = self.root / 'not-installed-cube'
            missing_home.mkdir()
            isolated = self.root / 'missing-runtime-out'
            shutil.copytree(self.out, isolated)
            with environment(SXC_CUBE_HOME=str(missing_home)):
                self.run_cli('report', expected=1)
                summary = self.summary()
                self.assertEqual(summary['status'], 'error')
                self.assertEqual(summary['runtime_evidence']['reason'], 'cube_install_unavailable')
                self.assertTrue(any(item['why'] == 'runtime_evidence_unavailable' for item in summary['stale']))
                saved = self.run_cli('build', expected=3, out=isolated)
                self.assertEqual(saved['status'], 'error')
                self.assertIn('cube_install_unavailable', json.dumps(saved))
            self.run_cli('report')

        def test_missing_recorded_binding_prevents_reuse_of_previous_pass(self):
            target = self.out / 'build' / 'result.json'
            previous = target.read_bytes()
            saved = json.loads(previous)
            self.assertEqual(saved['status'], 'pass')
            del saved['runtime_evidence']
            try:
                target.write_text(json.dumps(saved), encoding='utf-8')
                self.run_cli('report', expected=1)
                summary = self.summary()
                self.assertEqual(summary['status'], 'not_run')
                self.assertTrue(any(item['stage'] == 'build' and item['why'] == 'runtime_evidence_missing'
                                    for item in summary['stale']))
            finally:
                target.write_bytes(previous)
            self.run_cli('report')


if __name__ == '__main__':
    unittest.main()
