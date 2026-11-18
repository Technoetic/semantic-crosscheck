"""Exercise the shipped grouped calendar spec against PostgreSQL and the real Cube adapter."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import pgfixture
from semantic_crosscheck import cube_run, db


EXAMPLE = pgfixture.ROOT / 'examples' / 'measure-oracle'
ENABLED = pgfixture.available() and shutil.which('node') and cube_run.installed(cube_run.cube_home())


@unittest.skipUnless(ENABLED, 'needs dedicated PostgreSQL, node and installed Cube')
class MeasureOracleGroupPgTest(unittest.TestCase):
    def test_shipped_item_window_matches_then_detects_wrong_cube_end_day(self):
        fx = pgfixture.PgFixture('sxc_measure_group_window_' + str(os.getpid())).setup()
        self.addCleanup(fx.teardown)
        # Use PgFixture's existing, isolated source schema and default SELECT grants.
        ddl = (EXAMPLE / 'fixture.sql').read_text(encoding='utf-8')
        ddl = ddl[ddl.index('CREATE TABLE '):].replace('sxc_measure_oracle.', 'sxc_demo.')
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run(ddl)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config_path = directory / 'crosscheck.toml'
            cfg_text = (EXAMPLE / 'crosscheck.toml').read_text(encoding='utf-8')
            config_path.write_text(cfg_text.replace('sxc_measure_oracle', 'sxc_demo'), encoding='utf-8')
            command = [sys.executable, str(pgfixture.SCRIPTS / 'sxc.py')]
            common = ['--config', str(config_path), '--out', str(directory), '--quiet']
            built = subprocess.run([*command, 'cube', *common], capture_output=True, timeout=300)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            spec_path = EXAMPLE / 'item-window.json'
            wrong = json.loads(spec_path.read_text(encoding='utf-8'))
            wrong['cube_query']['timeDimensions'][0]['dateRange'][1] = '2026-02-01'
            changed = directory / 'wrong.json'
            changed.write_text(json.dumps(wrong), encoding='utf-8')
            for path, exit_code, count in [(spec_path, 0, 2), (changed, 1, 3)]:
                with self.subTest(exit_code=exit_code):
                    ran = subprocess.run([*command, 'measure-oracle', *common, '--spec', str(path)],
                                         capture_output=True, timeout=300)
                    self.assertEqual(ran.returncode, exit_code, ran.stdout + ran.stderr)
                    receipt = json.loads((directory / 'measure_oracle/result.json').read_text(encoding='utf-8'))
                    self.assertEqual(receipt['status'], 'pass' if exit_code == 0 else 'fail')
                    detail = receipt['checks'][0]['detail']
                    self.assertEqual((detail['source_rows'], detail['qualifying_rows'], detail['cube_rows']), (5, 2, count))
                    self.assertEqual((detail['source_groups'], detail['extra_groups']), (2, count - 2))
