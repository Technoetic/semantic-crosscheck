"""Run the shipped fan-out example through the actual PostgreSQL/Cube CLI."""
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
from test_measure_oracle_join import EXAMPLE


ENABLED = pgfixture.available() and shutil.which('node') and cube_run.installed(cube_run.cube_home())


@unittest.skipUnless(ENABLED, 'needs disposable PostgreSQL, Node and installed Cube')
class MeasureOracleJoinPgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_t_measure_join_' + str(os.getpid())).setup()
        cls.addClassCleanup(cls.fx.teardown)
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run((EXAMPLE / 'fixture.sql').read_text(encoding='utf-8'))
        finally:
            conn.close()

    def test_actual_cube_detects_join_multiplication_through_normal_cli(self):
        with tempfile.TemporaryDirectory() as output:
            args = ['--config', str(EXAMPLE / 'crosscheck.toml'), '--out', output, '--quiet']
            command = [sys.executable, str(pgfixture.SCRIPTS / 'sxc.py')]
            built = subprocess.run([*command, 'cube', *args], capture_output=True, timeout=300)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            for name, status, code in [('eligible', 'pass', 0), ('multiplied', 'fail', 1)]:
                with self.subTest(name=name):
                    path = EXAMPLE / (name + '.json')
                    run = subprocess.run([*command, 'measure-oracle', *args, '--spec', str(path)],
                                         capture_output=True, timeout=300)
                    self.assertEqual(run.returncode, code, run.stdout + run.stderr)
                    receipt = json.loads((Path(output) / 'measure_oracle/result.json').read_text(encoding='utf-8'))
                    self.assertEqual(receipt['status'], status)
                    detail = receipt['checks'][0]['detail']
                    self.assertEqual(detail['reason'], 'match' if code == 0 else 'value_mismatch')
                    self.assertEqual((detail['source_rows'], detail['related_rows'], detail['matched_facts'],
                                      detail['multi_match_facts'], detail['qualifying_rows'],
                                      detail['non_null_values'], detail['null_foreign_keys'],
                                      detail['orphan_related_rows']), (6, 9, 5, 2, 4, 3, 1, 1))
                    self.assertEqual(detail['qualifying_multi_match_facts'], 1)
                    self.assertNotIn('41.25', json.dumps(receipt))
                    self.assertNotIn('posted', json.dumps(receipt))

            document = json.loads((EXAMPLE / 'eligible.json').read_text(encoding='utf-8'))
            document['where'].append({'column': 'id', 'type': 'integer', 'operator': 'eq', 'value': 2})
            document['cube_query']['filters'] = [{'member': 'm_eligible.key', 'operator': 'equals', 'values': ['2']}]
            path = Path(output) / 'one-order.json'
            path.write_text(json.dumps(document), encoding='utf-8')
            run = subprocess.run([*command, 'measure-oracle', *args, '--spec', str(path)],
                                 capture_output=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            receipt = json.loads((Path(output) / 'measure_oracle/result.json').read_text(encoding='utf-8'))
            detail = receipt['checks'][0]['detail']
            self.assertEqual((detail['multi_match_facts'], detail['qualifying_multi_match_facts']), (2, 0))
            self.assertIn('No qualifying fact matched multiple related rows.', receipt['not_proven'])
