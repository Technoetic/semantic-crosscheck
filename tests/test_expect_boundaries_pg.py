"""Run the authored boundary corpus and injected mistakes on PostgreSQL/Cube.

Only a process-specific database is created/dropped; shared test roles are untouched.
The test loads the fixture itself because expect never executes fixture SQL.
"""

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse

import pgfixture
from semantic_crosscheck import config, cube_run, db, expect
from test_expect_boundaries import EXAMPLE, EXPECTED


ENABLED = pgfixture.available() and shutil.which("node") is not None and cube_run.installed(cube_run.cube_home())
FIXTURE_ENV = "SXC_T_EXPECT_BOUNDARIES_FIXTURE_DSN"


@unittest.skipUnless(ENABLED, "needs SXC_TEST_ADMIN_DSN, node and the installed pinned Cube adapter")
class ExpectBoundariesPgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = f"sxc_t_expect_boundaries_{os.getpid()}"
        admin = pgfixture._admin()
        try:
            admin.run(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.addClassCleanup(cls.drop_database)
        endpoint = urlparse(os.environ[pgfixture.ADMIN_ENV])
        changes = {FIXTURE_ENV: urlunparse(endpoint._replace(path="/" + cls.database)),
                   "SXC_PG_DSN": "", "SXC_PG_OWNER_DSN": ""}
        env = patch.dict(os.environ, changes)
        env.start()
        cls.addClassCleanup(env.stop)
        conn = db.connect(FIXTURE_ENV)
        try:
            version = int(conn.run("SHOW server_version_num")[0][0])
            if version < 170000:
                raise AssertionError("boundary fixture verification requires PostgreSQL 17+")
            # This authored SQL contains no semicolons in literals or comments.
            text = "\n".join(line for line in (EXAMPLE / "fixture.sql").read_text(encoding="utf-8").splitlines()
                             if not line.lstrip().startswith("--"))
            with db.write_tx(conn):
                for statement in text.split(";"):
                    if statement.strip():
                        conn.run(statement)
        finally:
            conn.close()

    @classmethod
    def drop_database(cls):
        admin = pgfixture._admin()
        try:
            admin.run(f'DROP DATABASE "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def run_expect(self, cfg, out):
        result = expect.run(cfg, SimpleNamespace(dsn_env=FIXTURE_ENV, allow_fixture_database=True), out)
        summary = json.dumps(result.to_dict())
        for value in ("P-EMPTY", "P-FILTER", "Relay", "NORTH", "SOUTH", self.database):
            self.assertNotIn(value, summary)
        self.assertFalse((out / "build").exists() or (out / "cube").exists())
        return result, {check.id: check for check in result.checks}

    def test_authored_boundaries_pass_with_the_pinned_fixture(self):
        cfg = config.load(EXAMPLE / "crosscheck.toml")
        with tempfile.TemporaryDirectory() as tmp:
            result, checks = self.run_expect(cfg, Path(tmp))
        self.assertEqual(result.status, "pass", result.to_dict())
        self.assertEqual(checks["expect:cube"].detail["cube_version"], config.CUBE_VERSION)
        self.assertTrue(checks["expect:fixture"].detail["locked"])
        pins = {gold.expect[0].fixture_sha256 for gold in cfg.gold}
        self.assertEqual(pins, {checks["expect:fixture"].detail["fixture_sha256"]})
        for name, rows in EXPECTED.items():
            detail = checks[f"expect:{name}[0]"].detail
            self.assertTrue(detail["independent"])
            self.assertEqual((detail["expected_rows"], detail["gold_rows"], detail["cube_rows"]),
                             (len(rows), len(rows), len(rows)))

    def test_wrong_authored_rows_fail_when_gold_and_cube_agree(self):
        cfg = config.load(EXAMPLE / "crosscheck.toml")
        for gold in cfg.gold:
            rows = [dict(row) for row in gold.expect[0].rows]
            column = "usable_qty" if gold.id == "facts_filtered_out" else "qty"
            rows[0][column] = 1 if rows[0][column] is None else int(rows[0][column]) + 1
            gold.expect = [dataclasses.replace(gold.expect[0], rows=rows)]
        with tempfile.TemporaryDirectory() as tmp:
            result, checks = self.run_expect(cfg, Path(tmp))
        self.assertEqual(result.status, "fail", result.to_dict())
        for name in EXPECTED:
            case = checks[f"expect:{name}[0]"]
            self.assertEqual((case.status, case.detail["reason"]), ("fail", "common_mode"))
            self.assertTrue(case.detail["gold_equals_cube"])
            self.assertFalse(case.detail["gold_equals_expected"] or case.detail["cube_equals_expected"])

    def test_shared_wrong_filter_and_collapsed_gold_group_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "example"
            shutil.copytree(EXAMPLE, copied)
            path = copied / "crosscheck.toml"
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count('filter = { status = ["USABLE"] }'), 1)
            path.write_text(text.replace('filter = { status = ["USABLE"] }',
                                         'filter = { status = ["QUARANTINE"] }'), encoding="utf-8")
            filtered = copied / "gold" / "facts_filtered_out.sql"
            text = filtered.read_text(encoding="utf-8")
            self.assertEqual(text.count("s.status = 'USABLE'"), 1)
            filtered.write_text(text.replace("s.status = 'USABLE'", "s.status = 'QUARANTINE'"), encoding="utf-8")
            grouped = copied / "gold" / "same_label_different_region.sql"
            grouped.write_text("SELECT min(p.region) AS region, p.label, sum(s.qty) AS qty\n"
                               "FROM sxc_boundary.part p LEFT JOIN sxc_boundary.stock s ON s.part_id = p.part_id\n"
                               "WHERE p.label = :label GROUP BY p.label\n", encoding="utf-8")
            result, checks = self.run_expect(config.load(path), Path(tmp) / "out")
        self.assertEqual(result.status, "fail", result.to_dict())
        self.assertEqual(checks["expect:build"].status, "pass", result.to_dict())
        self.assertEqual(checks["expect:cube"].status, "pass", result.to_dict())
        self.assertEqual(checks["expect:parent_without_facts[0]"].status, "pass")
        filtered = checks["expect:facts_filtered_out[0]"]
        self.assertEqual((filtered.status, filtered.detail["reason"]), ("fail", "common_mode"))
        self.assertTrue(filtered.detail["gold_equals_cube"])
        grouped = checks["expect:same_label_different_region[0]"]
        self.assertEqual((grouped.status, grouped.detail["reason"]), ("fail", "gold_mismatch"))
        self.assertEqual((grouped.detail["gold_rows"], grouped.detail["cube_rows"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
