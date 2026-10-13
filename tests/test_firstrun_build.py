"""build: concurrent runs, partial runs and database-error hints.

Unit tests (no database) check with a recording connection that every write transaction of build takes the output
schema's advisory lock first, that --hierarchy marks the other hierarchies not_run, and the next action for each
kind of database error. Integration tests (skipped unless SXC_TEST_ADMIN_DSN names a superuser connection) run in
throw-away databases named sxc_f_first_build*: two builds of one output schema at the same time, a --hierarchy run
read by the verify gate, and a check role without the TEMPORARY privilege.
"""

import copy
import os
import shutil
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, db, ident, naming, stage_build
from semantic_crosscheck.config import Hierarchy
from semantic_crosscheck.result import Result, check_status

DB_PREFIX = "sxc_f_first_build"


class RecordingConn:
    """pg8000 stand-in: records every statement; answers the few queries build asks with fixed rows."""

    def __init__(self, schema_exists: bool = True):
        self.statements: list[tuple[str, dict]] = []
        self.schema_exists = schema_exists

    def run(self, sql: str, **params):
        self.statements.append((sql, params))
        if sql.startswith("SELECT 1 FROM pg_namespace"):
            return [[1]] if self.schema_exists else []
        if sql.startswith("SELECT current_user"):
            return [["check_role"]]
        if sql.startswith("SELECT has_schema_privilege"):
            return [[True, True]]
        if sql.startswith("CREATE SCHEMA"):
            self.schema_exists = True
        return []

    def sql(self) -> list[str]:
        return [s for s, _p in self.statements]


def args(hierarchy: str = ""):
    return types.SimpleNamespace(hierarchy=hierarchy)


def two_hierarchies(cfg):
    c = copy.deepcopy(cfg)
    c.hierarchies["uses_part"] = Hierarchy(name="uses_part", edge="bom_line", node="part", parent="child_id",
                                           child="parent_id", quantity="qty_per")
    return c


class LockInEveryWriteTransaction(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(pgfixture.DEMO_CONFIG)

    def assert_locked_transactions(self, conn: RecordingConn, expected: int):
        sql = conn.sql()
        begins = [i for i, s in enumerate(sql) if s == "BEGIN"]
        self.assertEqual(len(begins), expected)
        for i in begins:
            self.assertTrue(sql[i + 1].startswith("SET LOCAL statement_timeout"), sql[i + 1])
            self.assertEqual(sql[i + 2], stage_build.LOCK_SQL)
            self.assertEqual(conn.statements[i + 2][1], {"key": "semantic-crosscheck build sxc_out"})

    def test_shapes_and_each_closure_take_the_lock(self):
        conn = RecordingConn()
        res, executed = Result("build"), []
        views = [{"shape": s.id, "violations": 0, "sql": f"-- view {s.id}"} for s in self.cfg.shapes]
        with mock.patch.object(stage_build.shapes, "create_views", return_value=views), \
                mock.patch.object(stage_build.closure, "build", return_value=([], ["-- closure"])):
            stage_build.build_shapes(conn, self.cfg, res, executed)
            stage_build.build_closure(conn, self.cfg, self.cfg.hierarchies["has_component"], res, executed)
        self.assert_locked_transactions(conn, 2)
        self.assertEqual(conn.sql().count("COMMIT"), 2)
        self.assertEqual(sum(1 for s in executed if s.startswith(stage_build.LOCK_SQL)), 2)

    def test_a_missing_output_schema_is_created_under_the_lock(self):
        conn = RecordingConn(schema_exists=False)
        executed = stage_build.ensure_output_schema(conn, self.cfg)
        self.assert_locked_transactions(conn, 1)
        sql = conn.sql()
        lock, create = sql.index(stage_build.LOCK_SQL), sql.index('CREATE SCHEMA "sxc_out"')
        self.assertTrue(sql[lock + 1].startswith("SELECT 1 FROM pg_namespace"))    # checked again under the lock
        self.assertLess(lock, create)
        self.assertEqual(executed[-1], 'CREATE SCHEMA "sxc_out";')

    def test_an_existing_output_schema_needs_no_transaction(self):
        conn = RecordingConn(schema_exists=True)
        self.assertEqual(stage_build.ensure_output_schema(conn, self.cfg), [])
        self.assertNotIn("BEGIN", conn.sql())

    def test_a_failed_shapes_transaction_is_an_error_for_every_shape(self):
        conn = RecordingConn()
        res = Result("build")
        timeout = Exception({"C": "57014", "M": "canceling statement due to statement timeout"})
        with mock.patch.object(stage_build.shapes, "create_views", side_effect=timeout):
            stage_build.build_shapes(conn, self.cfg, res, [])
        self.assertEqual({c.id: (c.status, c.detail["sqlstate"]) for c in res.checks},
                         {f"shape:{s.id}": ("error", "57014") for s in self.cfg.shapes})
        self.assertIn("ROLLBACK", conn.sql())
        self.assertIn("another build of the same output schema", stage_build.next_action(self.cfg, res))


class PartialBuildAndHints(unittest.TestCase):
    def setUp(self):
        self.cfg = two_hierarchies(config.load(pgfixture.DEMO_CONFIG))

    def test_excluded_hierarchies_are_not_run(self):
        res = Result("build")
        res.add("closure:has_component", "pass", "hierarchies.has_component")
        stage_build.exclude_hierarchies(self.cfg, [self.cfg.hierarchies["has_component"]], res)
        excluded = res.checks[-1]
        self.assertEqual((excluded.id, excluded.status, excluded.config_ref, excluded.detail["reason"]),
                         ("closure:uses_part", "not_run", "hierarchies.uses_part", "excluded_by_option"))
        self.assertEqual(res.status, "not_run")
        self.assertEqual(stage_build.next_action(self.cfg, res),
                         "this run left uses_part out (--hierarchy): rerun build without --hierarchy before reason, "
                         "cube and verify, so every closure is current")
        full = Result("build")
        stage_build.exclude_hierarchies(self.cfg, list(self.cfg.hierarchies.values()), full)
        self.assertEqual(full.checks, [])

    def test_database_error_hints(self):
        cases = {"42501": "TEMPORARY on the database", "23505": "another run was writing output schema sxc_out",
                 "40P01": "another run was writing output schema sxc_out", "57014": "source.statement_timeout_ms",
                 "42601": "fix [hierarchies.has_component] in crosscheck.toml"}
        for code, text in cases.items():
            res = Result("build")
            res.add("closure:has_component", "error", "hierarchies.has_component", sqlstate=code, closure_applied=False)
            action = stage_build.next_action(self.cfg, res)
            self.assertIn(f"SQLSTATE {code}", action)
            self.assertIn(text, action, code)
        shape = Result("build")
        shape.add("shape:qty_positive", "error", "shapes[1] qty_positive", sqlstate="42501", violations=None)
        action = stage_build.next_action(self.cfg, shape)
        self.assertIn("SELECT on the source tables or CREATE on the output schema", action)
        self.assertNotIn("TEMPORARY", action)                  # only the closure uses temporary tables


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class BuildIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(DB_PREFIX, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)

    def setUp(self):
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_firstrun_build_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def admin(self, *statements: str):
        a = pgfixture._admin(self.fx.name)
        try:
            for s in statements:
                a.run(s)
        finally:
            a.close()

    def drop_outputs(self):
        conn = self.fx.check_conn()
        try:
            with db.write_tx(conn):
                conn.run(f"DROP TABLE IF EXISTS {ident.qualified(naming.closure_table(self.cfg, 'has_component'))}")
                for s in self.cfg.shapes:
                    conn.run(f"DROP VIEW IF EXISTS {ident.qualified(naming.shape_view(self.cfg, s.id))}")
        finally:
            conn.close()

    def test_concurrent_builds_of_one_output_schema_both_pass(self):
        for attempt in range(3):
            self.drop_outputs()          # both runs find nothing and create the same objects
            barrier, results, errors = threading.Barrier(2), {}, []

            def build(i: int):
                try:
                    barrier.wait()
                    results[i] = stage_build.run(self.cfg, args(), self.tmp / f"run{attempt}_{i}")
                except Exception as e:  # noqa: BLE001 - reported below
                    errors.append(type(e).__name__)

            threads = [threading.Thread(target=build, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=300)
            self.assertEqual(errors, [])
            for res in results.values():
                self.assertEqual(res.status, "pass", [(c.id, c.status, c.detail.get("sqlstate")) for c in res.checks])
                apply = next(c for c in res.checks if c.id == "closure_apply:has_component")
                self.assertEqual(apply.detail["table_vs_recompute_diff"], 0)

    def test_a_partial_build_gates_the_left_out_hierarchy(self):
        cfg = two_hierarchies(self.cfg)
        out = self.tmp / "partial"
        res = stage_build.run(cfg, args("has_component"), out)
        res.write(out)
        self.assertEqual(res.status, "not_run")
        self.assertEqual(check_status(out, "build", "closure:has_component"), "pass")
        self.assertEqual(check_status(out, "build", "closure:uses_part"), "not_run")
        saved = (out / "build" / "result.json").read_text(encoding="utf-8")
        self.assertIn('"reason": "excluded_by_option"', saved)

    def test_without_temporary_privilege_the_hint_names_it(self):
        self.admin(f'REVOKE TEMPORARY ON DATABASE "{self.fx.name}" FROM PUBLIC')
        self.addCleanup(self.admin, f'GRANT TEMPORARY ON DATABASE "{self.fx.name}" TO PUBLIC')
        res = stage_build.run(self.cfg, args(), self.tmp / "notemp")
        closure = next(c for c in res.checks if c.id == "closure:has_component")
        self.assertEqual((closure.status, closure.detail["sqlstate"]), ("error", "42501"))
        self.assertIn("TEMPORARY on the database", res.next_safe_action)


if __name__ == "__main__":
    unittest.main()
