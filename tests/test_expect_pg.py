"""Integration: the `expect` stage on a dedicated fixture database with PostgreSQL 17 and the pinned Cube.

The test plays the user: it creates a throw-away database, loads examples/expect/fixture.sql into it itself (sxc
never runs fixture SQL) and runs the example configuration, whose authored cases pass. A copy with one wrong expected
quantity, which the gold SQL and Cube agree against, fails that case as common_mode. A copy whose gold SQL and
hierarchy both read the wrong quantity column (line_no for qty_per), with the authored rows unchanged, is the shared
mistake V1 accepts: gold and Cube agree and both cases fail as common_mode. No run writes the operational build/ or
cube/ results, and no result holds a fixture value. Skipped unless SXC_TEST_ADMIN_DSN names a
superuser connection, node is on PATH and the Cube adapter is installed (as tests/test_cube_stage.py).
"""

import importlib
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
from semantic_crosscheck import config, cube_run, db

try:
    expect = importlib.import_module("semantic_crosscheck.expect")
except ModuleNotFoundError:
    expect = None

ENABLED = pgfixture.available() and shutil.which("node") is not None and cube_run.installed(cube_run.cube_home())
EXAMPLE = pgfixture.ROOT / "examples" / "expect"
DEMO_GOLD = pgfixture.ROOT / "demo" / "gold" / "q01_explosion.sql"
FIXTURE_ENV = "SXC_T_EXPECT_FIXTURE_DSN"
FIXTURE_VALUES = ("P-100", "A-200", "C-300", "R-400", "Product P")


def statements(path: Path) -> list[str]:
    """The statements of a plain SQL file (no semicolons inside literals), comment lines dropped."""
    text = "\n".join(line for line in path.read_text(encoding="utf-8").split("\n") if not line.lstrip().startswith("--"))
    return [s.strip() for s in text.split(";") if s.strip()]


@unittest.skipUnless(ENABLED, "needs SXC_TEST_ADMIN_DSN, node on PATH and an installed Cube adapter (SXC_CUBE_HOME)")
class ExpectPgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = f"sxc_t_expect_{os.getpid()}"
        admin = pgfixture._admin()
        try:
            admin.run(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
            admin.run(f'CREATE DATABASE "{cls.database}"')
        finally:
            admin.close()
        cls.addClassCleanup(cls.drop)
        parsed = urlparse(os.environ[pgfixture.ADMIN_ENV])
        cls.env = patch.dict(os.environ, {FIXTURE_ENV: urlunparse(parsed._replace(path="/" + cls.database))})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        for name in ("SXC_PG_DSN", "SXC_PG_OWNER_DSN"):   # the example's operational variables (restored after)
            os.environ.pop(name, None)
        conn = db.connect(FIXTURE_ENV)
        try:
            with db.write_tx(conn):
                for statement in statements(EXAMPLE / "fixture.sql"):
                    conn.run(statement)
        finally:
            conn.close()

    @classmethod
    def drop(cls):
        admin = pgfixture._admin()
        try:
            admin.run(f'DROP DATABASE IF EXISTS "{cls.database}" WITH (FORCE)')
        finally:
            admin.close()

    def setUp(self):
        self.assertIsNotNone(expect, "the expect stage (semantic_crosscheck.expect) is missing")

    def run_expect(self, cfg, out: Path):
        res = expect.run(cfg, SimpleNamespace(dsn_env=FIXTURE_ENV, allow_fixture_database=True), out)
        text = json.dumps(res.to_dict())
        for value in FIXTURE_VALUES + (self.database,):
            self.assertNotIn(value, text)
        self.assertFalse((out / "build").exists() or (out / "cube").exists(), "operational output written")
        self.assertTrue((out / "expect" / "cube" / "model.json").is_file())
        return res, {c.id: c for c in res.checks}

    def test_the_authored_example_passes_on_its_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, checks = self.run_expect(config.load(EXAMPLE / "crosscheck.toml"), Path(tmp))
        self.assertEqual(res.status, "pass", res.to_dict())
        self.assertLessEqual({"expect:fixture", "expect:build", "expect:cube", "expect:q01_explosion[0]",
                              "expect:q01_explosion[1]"}, set(checks))
        first, partial = checks["expect:q01_explosion[0]"].detail, checks["expect:q01_explosion[1]"].detail
        self.assertEqual((first["expected_rows"], first["gold_rows"], first["cube_rows"], first["independent"]), (3, 3, 3, True))
        self.assertEqual((partial["partial"], partial["columns"]), (True, 2))
        self.assertEqual(checks["expect:cube"].detail["cube_version"], config.CUBE_VERSION)

    def test_a_wrong_expected_row_shared_by_gold_and_cube_is_common_mode(self):
        text = (EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8")
        wrong = text.replace('sql = "../../demo/gold/q01_explosion.sql"', 'sql = "q01_explosion.sql"')
        wrong = wrong.replace("path_count = 2, qty = 28 }", "path_count = 2, qty = 27 }")
        self.assertEqual(wrong.count("q01_explosion.sql"), 1)
        self.assertNotIn("qty = 28 }", wrong)
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(DEMO_GOLD, Path(tmp) / "q01_explosion.sql")
            path = Path(tmp) / "crosscheck.toml"
            path.write_text(wrong, encoding="utf-8")
            res, checks = self.run_expect(config.load(path), Path(tmp) / "out")
        case = checks["expect:q01_explosion[0]"]
        self.assertEqual((res.status, case.status, case.detail["reason"]), ("fail", "fail", "common_mode"))
        self.assertTrue(case.detail["gold_equals_cube"])
        self.assertFalse(case.detail["gold_equals_expected"] or case.detail["cube_equals_expected"])
        self.assertEqual(case.detail["cube_vs_expected"]["mismatched_columns"], {"qty": 1})
        self.assertEqual(checks["expect:q01_explosion[1]"].status, "pass")
        self.assertTrue(checks["expect:fixture"].detail["locked"])

    def test_a_mistake_the_gold_sql_and_the_configuration_share_is_common_mode(self):
        text = (EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8")
        shared = text.replace('sql = "../../demo/gold/q01_explosion.sql"', 'sql = "q01_explosion.sql"')
        shared = shared.replace('quantity = "qty_per"', 'quantity = "line_no"')
        gold = DEMO_GOLD.read_text(encoding="utf-8")
        self.assertEqual(gold.count("sum(qty_per)"), 1)
        self.assertEqual(shared.count('quantity = "line_no"'), 1)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "q01_explosion.sql").write_text(gold.replace("sum(qty_per)", "sum(line_no)"), encoding="utf-8")
            path = Path(tmp) / "crosscheck.toml"
            path.write_text(shared, encoding="utf-8")
            res, checks = self.run_expect(config.load(path), Path(tmp) / "out")
        self.assertEqual(checks["expect:build"].status, "pass", res.to_dict())
        for cid in ("expect:q01_explosion[0]", "expect:q01_explosion[1]"):
            case = checks[cid]
            self.assertEqual((case.status, case.detail["reason"]), ("fail", "common_mode"), case.detail)
            self.assertTrue(case.detail["gold_equals_cube"])   # what V1 sees: the two sides agree
            self.assertFalse(case.detail["gold_equals_expected"] or case.detail["cube_equals_expected"])
        self.assertEqual(checks["expect:fixture"].status, "pass")


if __name__ == "__main__":
    unittest.main()
