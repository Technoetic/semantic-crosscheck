"""Optional mutation observation keeps verdicts while exposing safe, bounded diagnostics."""

import argparse
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck import config, mutate, mutation_coverage, stage_verify
from semantic_crosscheck.result import StageError
from test_mutate import demo_model


class ObservationSuiteTest(unittest.TestCase):
    def test_all_observes_later_failures_and_timeout_but_keeps_first_verdict(self):
        calls = []
        def item(iid, answer):
            def run(_client):
                calls.append(iid)
                return answer
            return iid, run
        items = [item('v1:a', 'fail'), item('v2:a:top[0]', 'fail'),
                 item('v3:total:m.value', ('error', {'cube': 'timeout', 'message': 'private'}))]
        suite = mutation_coverage.Suite(items, observation='all')
        self.assertEqual(suite(None), ('fail', 'v1:a'))
        self.assertEqual(calls, ['v1:a', 'v2:a:top[0]', 'v3:total:m.value', 'v3:total:m.value'])
        receipt = suite.receipt()
        self.assertEqual([row['status'] for row in receipt['items']], ['fail', 'fail', 'error'])
        self.assertEqual(receipt['executed_items'], 3)
        self.assertTrue(receipt['complete'])
        self.assertNotIn('private', json.dumps(receipt))

    def test_all_keeps_error_before_later_fail_and_retry_stays_local(self):
        recovered = iter([('error', {'cube': 'timeout'}), 'pass'])
        suite = mutation_coverage.Suite([
            ('v1:a', lambda _: next(recovered)), ('v1:b', lambda _: 'error'),
            ('v1:c', lambda _: 'fail')], observation='all')
        self.assertEqual(suite(None), ('error', 'v1:b'))
        rows = suite.receipt()['items']
        self.assertEqual([row['attempts'] for row in rows], [2, 2, 1])
        self.assertNotIn('cube', rows[0], 'a recovered timeout must not annotate the final pass')
        self.assertEqual(rows[2]['status'], 'fail')

    def test_budget_after_first_failure_retains_failure_and_marks_partial(self):
        suite = mutation_coverage.Suite([
            ('v1:a', lambda _: 'fail'), ('v1:b', lambda _: 'pass'),
            ('v1:c', lambda _: self.fail('over budget'))], max_items=2, observation='all')
        for _ in range(2):
            self.assertEqual(suite(None), ('fail', 'v1:a'))
            receipt = suite.receipt()
            self.assertEqual(receipt['executed_items'], 2)
            self.assertFalse(receipt['complete'])
            self.assertEqual(receipt['items'][2]['reason'], 'item_budget')

    def test_first_mode_legacy_receipt_is_unchanged(self):
        suite = mutation_coverage.Suite([('v1:a', lambda _: 'fail'), ('v1:b', lambda _: 'pass')])
        self.assertEqual(suite(None), ('fail', 'v1:a'))
        self.assertEqual(suite.receipt(), {
            'complete': False, 'max_items': 10000, 'planned_items': 2, 'executed_items': 1,
            'items': [{'id': 'v1:a', 'status': 'fail', 'attempts': 1},
                      {'id': 'v1:b', 'status': 'not_run', 'attempts': 0, 'reason': 'early_stop'}]})

    def test_not_run_and_unknown_status_are_never_mismatches(self):
        suite = mutation_coverage.Suite([('v1:a', lambda _: 'not_run'), ('v1:b', lambda _: 'invalid'),
                                         ('v1:c', lambda _: 'fail')], observation='all')
        self.assertEqual(suite(None), ('not_run', 'v1:a'))
        self.assertEqual([r['status'] for r in suite.receipt()['items']], ['not_run', 'error', 'fail'])


class ObservationConfigTest(unittest.TestCase):
    def test_config_accepts_only_first_and_all(self):
        self.assertEqual(getattr(config.Verify(), 'mutant_observation', None), 'first')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'crosscheck.toml'
            for value in ('"first"', '"all"', '"later"', 'true', '1', '[]'):
                path.write_text('version = 1\n[source]\nschemas = ["s"]\n[verify]\nmutant_observation = ' + value,
                                encoding='utf-8')
                if value in ('"first"', '"all"'):
                    self.assertEqual(config.load(path).verify.mutant_observation, value.strip('"'))
                else:
                    with self.assertRaises(StageError):
                        config.load(path)


class MatrixStageTest(unittest.TestCase):
    def verify(self, out, observation='all', statuses=('fail', 'fail', 'error')):
        cfg, model = demo_model()
        cfg.verify.mutant_observation = observation
        cfg.verify.mutation_operators = ['measure_normalization_drop']
        v = stage_verify._Verify(cfg, out, model, ['v4'])
        v.gold_kinds = {'authored': 'authored', 'generated': 'generated'}
        ids = ['v2:authored:top[0]', 'v1:generated', 'v3:total:m.value']
        v.items = [(iid, iid, lambda _, st=st: st) for iid, st in zip(ids, statuses)]
        v.items.append(('v1:excluded', 'v1:excluded', lambda _: self.fail('failed baseline replayed')))
        v.baseline = dict.fromkeys(ids, 'pass') | {'v1:excluded': 'fail'}
        return v

    def run_mutants(self, v, *, witness=3, startup=False):
        serve = (mock.Mock(side_effect=StageError('private startup failure', 3)) if startup else
                 lambda *_a, **_kw: contextlib.nullcontext(None))
        with mock.patch.object(mutate, 'witness', return_value=witness), \
             mock.patch.object(mutate.cube_run, 'serve', side_effect=serve):
            v.v4(None)
        return json.loads((v.out / 'verify/mutation-matrix.json').read_text(encoding='utf-8'))

    def test_matrix_contains_all_detectors_bindings_and_excluded_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self.verify(Path(tmp))
            matrix = self.run_mutants(v)
            self.assertTrue(matrix['diagnostic_only'])
            self.assertEqual(matrix['counts'], {'selected_mutants': 2, 'selected_items': 3, 'planned_cells': 6,
                                               'executed_cells': 6, 'not_run_cells': 0, 'mismatch_cells': 4})
            self.assertEqual(matrix['excluded_items'], [{'id': 'v1:excluded', 'baseline_status': 'fail'}])
            self.assertEqual(matrix['mutants'][0]['observed_detectors'], [
                {'id': 'v2:authored:top[0]', 'kind': 'authored'}, {'id': 'v1:generated', 'kind': 'generated'}])
            self.assertEqual(matrix['detector_sets'][0]['mutants'], [
                'v4:measure_normalization_drop:on_hand', 'v4:measure_normalization_drop:consigned'])
            self.assertEqual(matrix['detector_sets'][0]['unique_mutants'], [])
            self.assertEqual(len(matrix['detector_sets'][0]['redundant_mutants']), 2)
            self.assertTrue(matrix['complete'])
            self.assertRegex(matrix['identity']['model_sha256'], r'^[a-f0-9]{64}$')
            self.assertRegex(matrix['identity']['config_sha256'], r'^[a-f0-9]{64}$')
            self.assertEqual(matrix['identity']['verify_started_at'], v.res.started_at)
            self.assertTrue(matrix['identity']['observation_id'])

    def test_matrix_does_not_upgrade_first_error_when_later_authored_item_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            all_v = self.verify(Path(tmp) / 'all', statuses=('error', 'fail', 'fail'))
            first_v = self.verify(Path(tmp) / 'first', 'first', statuses=('error', 'fail', 'fail'))
            matrix = self.run_mutants(all_v)
            with mock.patch.object(mutate, 'witness', return_value=3), \
                 mock.patch.object(mutate.cube_run, 'serve', side_effect=lambda *_a, **_kw: contextlib.nullcontext(None)):
                first_v.v4(None)
            for all_check, first_check in zip(all_v.res.checks, first_v.res.checks):
                self.assertEqual((all_check.status, all_check.detail['reason'], all_check.detail['item']),
                                 (first_check.status, first_check.detail['reason'], first_check.detail['item']))
                self.assertNotIn('killed_by', all_check.detail)
            self.assertEqual(all_v.mutation_split(), first_v.mutation_split())
            self.assertEqual(len(matrix['mutants'][0]['observed_detectors']), 2)
            self.assertFalse((first_v.out / 'verify/mutation-matrix.json').exists())

    def test_budget_equivalent_startup_and_no_baseline_never_imply_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            for reason in ('item_budget', 'data_equivalent', 'startup', 'no_passing_suite_item'):
                with self.subTest(reason=reason):
                    v = self.verify(Path(tmp) / reason, statuses=('pass', 'fail', 'fail'))
                    if reason == 'item_budget':
                        v.cfg.verify.mutant_max_items = 1
                    if reason == 'no_passing_suite_item':
                        v.baseline = dict.fromkeys(v.baseline, 'fail')
                    matrix = self.run_mutants(v, witness=0 if reason == 'data_equivalent' else 3,
                                              startup=reason == 'startup')
                    self.assertEqual(matrix['counts']['mismatch_cells'], 0)
                    self.assertEqual(matrix['detector_sets'], [])
                    self.assertTrue(all(not row['observed_detectors'] for row in matrix['mutants']))
                    self.assertFalse(matrix['complete'])
                    if reason != 'no_passing_suite_item':
                        self.assertEqual(matrix['mutants'][0]['coverage']['items'][-1]['reason'], reason)
                    else:
                        self.assertEqual(matrix['counts']['selected_items'], 0)

    def test_verify_attempt_removes_stale_matrix_before_validation_or_early_failure(self):
        cfg, _model = demo_model()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            artifact = out / 'verify/mutation-matrix.json'
            artifact.parent.mkdir()
            for args in (argparse.Namespace(only=['bad'], gold=[]), argparse.Namespace(only=['v1'], gold=[])):
                artifact.write_text('{"stale":true}', encoding='utf-8')
                with self.assertRaises(StageError):
                    stage_verify.run(cfg, args, out)
                self.assertFalse(artifact.exists())

class MatrixBindingAndCleanupTest(unittest.TestCase):
    def test_first_and_sweep_bindings_identify_same_replay_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, model = demo_model()
            cfg.verify.mutant_observation = 'all'
            cfg.verify.mutation_operators = ['measure_normalization_drop']
            v = stage_verify._Verify(cfg, Path(tmp), model, ['v4'])
            v.closure = dict.fromkeys(v.closure, 'pass')
            g = cfg.gold[0]
            params = {'root': 'private_binding_value'}
            with mock.patch.object(stage_verify.sweep, 'bindings', return_value=[('top', params)]):
                v.plan_gold(None, 0, g)
            v.items = [(iid, cid, lambda _: 'fail') for iid, cid, _fn in v.items]
            v.baseline = {iid: 'pass' for iid, _cid, _fn in v.items}
            with mock.patch.object(mutate, 'witness', return_value=3), \
                 mock.patch.object(mutate.cube_run, 'serve', side_effect=lambda *_a, **_kw: contextlib.nullcontext(None)):
                v.v4(None)
            payload = (v.out / 'verify/mutation-matrix.json').read_text(encoding='utf-8')
            matrix = json.loads(payload)
            self.assertEqual([row['binding_id'] for row in matrix['selected_items']],
                             ['q01_explosion:top[0]', 'q01_explosion:top[0]'])
            self.assertEqual(matrix['mutants'][0]['coverage']['items'][0]['binding_id'], 'q01_explosion:top[0]')
            self.assertNotIn('private_binding_value', payload)

    def test_cli_invalid_or_missing_configuration_invalidates_old_matrix(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            artifact = folder / 'out/verify/mutation-matrix.json'
            artifact.parent.mkdir(parents=True)
            config_file = folder / 'crosscheck.toml'
            config_file.write_text('invalid TOML [', encoding='utf-8')
            for path in (config_file, folder / 'missing.toml'):
                artifact.write_text('{"stale":true}', encoding='utf-8')
                completed = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'verify',
                                            '--config', str(path), '--out', str(folder / 'out'), '--quiet'],
                                           capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(artifact.exists(), completed.stderr)

    def test_atomic_matrix_replacement_does_not_follow_artifact_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = MatrixStageTest().verify(Path(tmp))
            folder = v.out / 'verify'
            folder.mkdir()
            target = v.out / 'keep.json'
            target.write_text('private_data', encoding='utf-8')
            artifact = folder / 'mutation-matrix.json'
            try:
                artifact.symlink_to(target)
            except OSError:
                self.skipTest('symlinks unavailable')
            MatrixStageTest().run_mutants(v)
            self.assertEqual(target.read_text(encoding='utf-8'), 'private_data')
            self.assertFalse(artifact.is_symlink())

class MatrixDiagnosticBoundaryTest(unittest.TestCase):
    def test_all_mode_retains_categorical_binding_timeout(self):
        cfg, model = demo_model()
        cfg.verify.mutant_observation = 'all'
        v = stage_verify._Verify(cfg, Path('unused'), model, ['v4'])
        g = cfg.gold[0]
        with mock.patch.object(stage_verify.compare, 'run_binding', return_value={
                'pass': False, 'error': 'CubeError', 'cube': 'timeout', 'message': 'private'}):
            suite = mutation_coverage.Suite([('v1:a', v._binding_fn(None, g, {}, {}))], observation='all')
            self.assertEqual(suite(None), ('error', 'v1:a'))
        self.assertEqual(suite.receipt()['items'][0]['cube'], 'timeout')
        self.assertNotIn('private', json.dumps(suite.receipt()))

    def test_attempted_not_run_cell_counts_as_executed_and_keeps_safe_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = MatrixStageTest().verify(Path(tmp), statuses=('not_run', 'pass', 'pass'))
            matrix = MatrixStageTest().run_mutants(v)
            self.assertEqual(matrix['counts']['executed_cells'], 6)
            self.assertEqual(matrix['counts']['not_run_cells'], 0, 'not-run denominator counts missing execution')
            self.assertEqual(matrix['cell_status_counts'], {'pass': 4, 'fail': 0, 'error': 0, 'not_run': 2})
            self.assertEqual(matrix['mutants'][0]['coverage']['items'][0]['reason'], 'item_not_run')

    def test_verify_cleanup_cannot_delete_configuration_alias(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            artifact = folder / 'verify/mutation-matrix.json'
            artifact.parent.mkdir()
            raw = 'version = 1\n[source]\nschemas = ["s"]\n'
            artifact.write_text(raw, encoding='utf-8')
            completed = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'verify',
                                        '--config', str(artifact), '--out', str(folder), '--quiet'],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(artifact.is_file(), 'configuration is an input, even with a diagnostic filename')
            self.assertEqual(artifact.read_text(encoding='utf-8'), raw)
            self.assertFalse((folder / 'verify/result.json').exists(), 'unsafe diagnostic aliases suppress writes')

class MatrixReplayAndClassificationTest(unittest.TestCase):
    def test_all_expands_pair_chunks_and_observes_null_after_first_mismatch(self):
        from test_pair_chunks import CAP, KeyedClient, KeysConn, baseline, keys
        every = keys(CAP + 1)
        conn = KeysConn(every)
        v, check, test = baseline(self, 5000, conn, KeyedClient(every, null='same'))
        v.cfg.verify.mutant_observation = 'all'
        v.items = [(check.id, check.id, v._test_fn(conn, test))]
        v.suite_tests = [test]
        seen = {}
        def run_mutants(cfg, out, conn, model, suite, **kwargs):
            seen['answer'] = suite(KeyedClient(every, bad=[every[0]], null='bad'))
            seen['receipt'] = suite.receipt()
            return [], []
        with mock.patch.object(stage_verify.mutate, 'run_mutants', side_effect=run_mutants):
            v.v4(conn)
        self.assertEqual(seen['answer'], ('fail', check.id + ':chunk[0]'))
        self.assertEqual([r['status'] for r in seen['receipt']['items']], ['fail', 'pass', 'fail'])
        self.assertTrue(seen['receipt']['complete'])
        self.assertNotIn(every[0], json.dumps(seen['receipt']))

    def test_not_applicable_and_gated_instances_have_only_unexecuted_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, model = demo_model(closure_exists=False)
            cfg.verify.mutant_observation = 'all'
            cfg.verify.mutation_operators = ['closure_depth_shift']
            for build_status, outcome in [('pass', 'not_applicable'), ('fail', 'not_run')]:
                v = stage_verify._Verify(cfg, Path(tmp) / build_status, model, ['v4'])
                v.closure = {'has_component': build_status}
                v.items = [('v3:total:m.value', 'v3:total:m.value', lambda _: self.fail('must not execute'))]
                v.baseline = {'v3:total:m.value': 'pass'}
                v.v4(None)
                matrix = json.loads((v.out / 'verify/mutation-matrix.json').read_text(encoding='utf-8'))
                self.assertEqual(matrix['counts'], {'selected_mutants': 1, 'selected_items': 1, 'planned_cells': 1,
                                                   'executed_cells': 0, 'not_run_cells': 1, 'mismatch_cells': 0})
                self.assertEqual(matrix['mutants'][0]['outcome'], outcome)
                self.assertEqual(matrix['mutants'][0]['coverage']['items'][0]['kind'], 'generated')
                self.assertEqual(matrix['mutants'][0]['observed_detectors'], [])
                self.assertFalse(matrix['complete'])

    def test_later_observation_never_enters_authored_facet_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = MatrixStageTest().verify(Path(tmp), statuses=('error', 'fail', 'pass'))
            v.gold_kinds['generated'] = 'authored'
            MatrixStageTest().run_mutants(v)
            self.assertEqual(v._detections(), {})
            self.assertEqual(v.mutation_split()['not_judged'], 2)

    def test_single_observed_detector_has_unique_set_even_when_later_cells_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = MatrixStageTest().verify(Path(tmp), statuses=('pass', 'fail', 'pass'))
            matrix = MatrixStageTest().run_mutants(v)
            self.assertEqual(matrix['counts']['mismatch_cells'], 2)
            self.assertEqual(matrix['detector_sets'][0]['id'], 'v1:generated')
            self.assertEqual(len(matrix['detector_sets'][0]['unique_mutants']), 2)
            self.assertEqual(matrix['detector_sets'][0]['redundant_mutants'], [])

class MatrixIdentityTest(unittest.TestCase):
    def test_config_identity_is_captured_before_queries_can_change_the_file(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            cfg, model = demo_model()
            cfg.path = Path(tmp) / 'crosscheck.toml'
            cfg.path.write_bytes(b'before')
            cfg.verify.mutant_observation = 'all'
            cfg.verify.mutation_operators = ['measure_normalization_drop']
            v = stage_verify._Verify(cfg, Path(tmp) / 'out', model, ['v4'])
            v.items = [('v1:a', 'v1:a', lambda _: 'fail')]
            v.baseline = {'v1:a': 'pass'}
            def serve(*_args, **_kwargs):
                cfg.path.write_bytes(b'after')
                return contextlib.nullcontext(None)
            with mock.patch.object(mutate, 'witness', return_value=3), \
                 mock.patch.object(mutate.cube_run, 'serve', side_effect=serve):
                v.v4(None)
            matrix = json.loads((v.out / 'verify/mutation-matrix.json').read_text(encoding='utf-8'))
            self.assertEqual(matrix['identity']['config_sha256'], hashlib.sha256(b'before').hexdigest())

class MatrixOutputBoundaryTest(unittest.TestCase):
    def test_cli_refuses_verify_directory_escape_without_writing_outside_output(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = folder / 'out'
            outside = folder / 'outside'
            out.mkdir()
            outside.mkdir()
            try:
                (out / 'verify').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('directory symlinks unavailable')
            artifact = outside / 'mutation-matrix.json'
            artifact.write_text('keep', encoding='utf-8')
            completed = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'verify',
                                        '--config', str(pgfixture.DEMO_CONFIG), '--out', str(out), '--quiet'],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(artifact.read_text(encoding='utf-8'), 'keep')
            self.assertFalse((outside / 'result.json').exists())

class MatrixDefaultOutputCleanupTest(unittest.TestCase):
    def test_missing_configuration_still_clears_known_default_output(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            artifact = folder / '.sxc/verify/mutation-matrix.json'
            artifact.parent.mkdir(parents=True)
            for extra in ([], ['--config', str(folder / 'missing.toml')]):
                artifact.write_text('{"stale":true}', encoding='utf-8')
                completed = subprocess.run([sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'verify', '--quiet', *extra],
                                           cwd=folder, capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(artifact.exists(), completed.stderr)


if __name__ == '__main__':
    unittest.main()
