"""Integration: the `cube` stage on the clean demo, and proof that every Cube session is read-only.

Skipped unless SXC_TEST_ADMIN_DSN names a superuser connection, node is on PATH and the Cube adapter is
installed (cube_run.installed(cube_home()); SXC_CUBE_HOME may point at an existing install).
"""

import argparse
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture
from semantic_crosscheck import closure, config, cube_gen, cube_run, db, ident, stage_cube
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import EXIT_CONFIG, StageError

ENABLED = pgfixture.available() and shutil.which("node") is not None and cube_run.installed(cube_run.cube_home())
DATABASE = "sxc_t_cube_stage"
SEQUENCE = "sxc_out.sxc_t_cube_ro_seq"


@unittest.skipUnless(ENABLED, "needs SXC_TEST_ADMIN_DSN, node on PATH and an installed Cube adapter (SXC_CUBE_HOME)")
class CubeStageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_cube_"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.fx = pgfixture.PgFixture(DATABASE, "clean").setup()
        cls.addClassCleanup(cls.fx.teardown)
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        conn = cls.fx.check_conn()
        try:
            with db.write_tx(conn):
                cls.closure_checks, _sql = closure.build(conn, cls.cfg, cls.cfg.hierarchy())
        finally:
            conn.close()

    def test_stage_passes_on_the_clean_demo(self):
        self.assertEqual([c[1] for c in self.closure_checks], ["pass", "pass"])
        out = self.tmp / "out"
        res = stage_cube.run(self.cfg, argparse.Namespace(), out)
        statuses = {c.id: c.status for c in res.checks}
        expected = {"cube:model", "cube:meta"} | {f"cube:gold_members:{g.id}" for g in self.cfg.gold}
        self.assertEqual(set(statuses), expected)
        self.assertEqual(res.status, "pass", statuses)
        self.assertEqual(res.not_proven, [])

        model = cube_gen.load_model(out)
        public, everything = stage_cube.public_members(model), cube_gen.members(model)
        meta = next(c for c in res.checks if c.id == "cube:meta").detail
        self.assertEqual((meta["missing"], meta["extra"], meta["meta_members"]), (0, 0, len(public)))
        self.assertLess(len(public), len(everything), "hidden primary keys exist and Cube does not list them")
        self.assertEqual({p.stem for p in (out / "cube" / "model").glob("*.yml")}, {c["name"] for c in model["cubes"]})
        self.assertTrue((out / "cube" / "server.log").is_file())
        self.assertNotRegex(json.dumps(res.to_dict()), r"P-\d{6}|SO-\d{6}", "results must not carry row values")

    def test_generation_error_names_the_entity(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.entities["part"].key = ["no_such_column"]
        with self.assertRaises(StageError) as caught:
            stage_cube.run(cfg, argparse.Namespace(), self.tmp / "out_bad")
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertIn("[entities.part]", str(caught.exception))

    def test_cube_sessions_are_read_only(self):
        seq = ident.qualified(SEQUENCE)
        conn = self.fx.check_conn()
        try:
            with db.write_tx(conn):
                conn.run(f"CREATE SEQUENCE {seq}")
            first = db.scalar(conn, "SELECT nextval(CAST(:s AS regclass))", s=SEQUENCE)   # allowed outside Cube
            # the server's own wording of SQLSTATE 25006 (messages follow the server's lc_messages)
            with self.assertRaises(Exception) as direct:
                with db.read_only(conn):
                    conn.run("SELECT nextval(CAST(:s AS regclass))", s=SEQUENCE)
            fields = direct.exception.args[0]
            self.assertEqual(fields.get("C"), "25006")
        finally:
            conn.close()

        run_dir = self.tmp / "read_only"
        probes = [
            {"name": "ro_setting", "sql": "SELECT current_setting('transaction_read_only') AS ro",
             "dimensions": [{"name": "ro", "sql": "{CUBE}.ro", "type": "string", "primary_key": True, "public": True}]},
            {"name": "ro_probe", "sql": f"SELECT nextval({cube_gen._lit(SEQUENCE)}::regclass) AS v",
             "dimensions": [{"name": "v", "sql": "{CUBE}.v", "type": "number", "primary_key": True, "public": True}]},
        ]
        cube_gen.write_model({"cubes": probes}, run_dir / "model")
        with cube_run.serve(self.cfg, run_dir, run_dir / "server.log") as client:
            self.assertEqual(client.load({"dimensions": ["ro_setting.ro"]}), [{"ro_setting.ro": "on"}])
            with self.assertRaises(CubeError) as caught:
                client.load({"dimensions": ["ro_probe.v"]})
        text = str(caught.exception)
        self.assertTrue(fields["M"] in text or "25006" in text, f"not a read-only violation: {text[:300]}")

        conn = self.fx.check_conn()
        try:
            self.assertEqual(db.scalar(conn, f"SELECT last_value FROM {seq}"), first, "nextval ran inside a Cube session")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
