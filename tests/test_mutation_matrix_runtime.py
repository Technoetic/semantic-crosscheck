"""Disposable PostgreSQL/Cube regression for optional all-mode mutation observation."""

import argparse
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture
from semantic_crosscheck import closure, config, cube_gen, cube_run, db, stage_verify
from semantic_crosscheck.result import Result

CUBE_READY = cube_run.installed(cube_run.cube_home()) and bool(shutil.which('node'))


@unittest.skipUnless(pgfixture.available() and CUBE_READY,
                     'needs SXC_TEST_ADMIN_DSN and the Cube adapter (SXC_CUBE_HOME) with node on PATH')
class MutationMatrixRuntimeTest(unittest.TestCase):
    def test_actual_mutant_records_two_detectors_and_next_default_run_removes_matrix(self):
        import json
        fx = pgfixture.PgFixture('sxc_t_matrix_' + str(os.getpid()))
        self.addCleanup(fx.teardown)
        fx.setup()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            out = Path(tmp)
            cfg = config.load(pgfixture.DEMO_CONFIG)
            cfg.gold = [cfg.gold[0]]
            cfg.gold[0].params['root'].strata = ['top']
            cfg.verify.sweep_k = 1
            cfg.verify.mutant_bindings = 1
            cfg.verify.mutant_max_items = 2
            cfg.verify.mutation_operators = ['closure_depth_shift']
            cfg.verify.mutant_observation = 'all'
            conn = fx.check_conn()
            try:
                build = Result('build')
                with db.write_tx(conn):
                    checks, _notes = closure.build(conn, cfg, cfg.hierarchy())
                for cid, status, ref, detail in checks:
                    build.add(cid, status, ref, **detail)
                self.assertEqual(build.status, 'pass')
                build.write(out)
                with db.read_only(conn):
                    model, _notes = cube_gen.generate(cfg, conn)
            finally:
                conn.close()
            cube_gen.write_model(model, out / 'cube/model')
            cube = Result('cube')
            cube.add('cube:model', 'pass')
            cube.write(out)
            result = stage_verify.run(cfg, argparse.Namespace(only=['v4'], gold=[]), out)
            check = next(c for c in result.checks if c.id == 'v4:closure_depth_shift:has_component')
            self.assertEqual((check.status, check.detail['killed_by']), ('pass', 'v1:q01_explosion'))
            artifact = out / 'verify/mutation-matrix.json'
            matrix = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(len(matrix['mutants']), 1)
            self.assertEqual(matrix['mutants'][0]['observed_detectors'], [
                {'id': 'v1:q01_explosion', 'kind': 'authored'},
                {'id': 'v2:q01_explosion:top[0]', 'kind': 'authored'}])
            self.assertEqual(matrix['counts']['executed_cells'], 2)
            self.assertFalse(matrix['complete'], 'budget left generated items unobserved')
            self.assertNotIn('P-000', json.dumps(matrix))
            cfg.verify.mutant_observation = 'first'
            default = stage_verify.run(cfg, argparse.Namespace(only=['v1'], gold=[]), out)
            self.assertEqual(next(c for c in default.checks if c.id == 'v1:q01_explosion').status, 'pass')
            self.assertFalse(artifact.exists())
