"""Integration tests of the build stage in PostgreSQL 17 (skipped unless SXC_TEST_ADMIN_DSN is set).

Each class works in its own throw-away database (prefix sxc_t_build) created by tests/pgfixture.py. The demo's
independent Python numbers (fx.data["expected"]) are the reference for the shape counts and the closure; on the
dirty demo pySHACL, run on rows exported with the naming.rdf_* IRIs, must flag exactly the keys the views select.
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

import pgfixture
import sxc
from semantic_crosscheck import canon, config, db, ident, naming, shapes, stage_build
from semantic_crosscheck.config import Hierarchy, Shape
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, EXIT_FAIL, EXIT_PASS, StageError

from test_shapes_unit import export_graph, focus_keys, pyshacl, rdflib

PREFIX = "sxc_t_build"
ROW_VALUE_MARKERS = ("P-0", "SO-0", "P-ORPHAN")   # demo key prefixes: never allowed in results or console output


def args(hierarchy: str = ""):
    return types.SimpleNamespace(hierarchy=hierarchy)


def get_check(res, check_id: str):
    found = [c for c in res.checks if c.id == check_id]
    if len(found) != 1:
        raise AssertionError(f"{check_id}: {len(found)} checks")
    return found[0]


def cli(*argv) -> tuple[int, str]:
    """sxc.main with the console output captured."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = sxc.main(list(argv))
    return code, buf.getvalue()


@unittest.skipUnless(pgfixture.available(), "SXC_TEST_ADMIN_DSN is not set")
class _BuildCase(unittest.TestCase):
    """One fixture database per class and one build run shared by its tests (cls.res, files in cls.out)."""

    variant = "clean"
    database = ""

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.database, cls.variant)
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.out = cls.tmp / "run"
        cls.res = stage_build.run(cls.cfg, args(), cls.out)

    def setUp(self):
        # another test class may have pointed the check-role DSN at its own database
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)

    def admin(self, *statements: str):
        a = pgfixture._admin(self.fx.name)
        try:
            for s in statements:
                a.run(s)
        finally:
            a.close()

    def check_role(self, sql: str, write: bool = False) -> list:
        conn = self.fx.check_conn()
        try:
            with (db.write_tx(conn) if write else db.read_only(conn)):
                return conn.run(sql) or []
        finally:
            conn.close()

    def view_count(self, shape_id: str) -> int:
        return int(self.check_role(f"SELECT count(*) FROM {ident.qualified(naming.shape_view(self.cfg, shape_id))}")[0][0])

    def relation_exists(self, qualified: str) -> bool:
        return bool(self.check_role(f"SELECT to_regclass('{qualified}') IS NOT NULL")[0][0])

    def run_cli(self, *extra: str) -> tuple[int, str, Path]:
        out = Path(tempfile.mkdtemp(dir=self.tmp))
        code, console = cli("build", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(out), *extra)
        return code, console, out

    def assert_no_row_values(self, *texts: str):
        for text in texts:
            for marker in ROW_VALUE_MARKERS:
                self.assertNotIn(marker, text)


class BuildCleanTest(_BuildCase):
    database = f"{PREFIX}_clean"

    def test_every_shape_passes(self):
        for i, s in enumerate(self.cfg.shapes):
            c = get_check(self.res, f"shape:{s.id}")
            self.assertEqual((c.status, c.detail["violations"], c.config_ref), ("pass", 0, f"shapes[{i}] {s.id}"))
            self.assertEqual(c.detail["kind"], shapes.kind(s))
            self.assertEqual(self.view_count(s.id), 0)
        self.assertTrue(get_check(self.res, "shape:open_le_total").detail["trusted_sql"])

    def test_closure_equals_the_independent_reference(self):
        exp = self.fx.data["expected"]
        c = get_check(self.res, "closure:has_component")
        self.assertEqual(c.status, "pass")
        self.assertEqual(c.detail["pairs"], exp["closure_pairs"])
        for k in ("paths_total", "multi_path_pairs", "max_depth", "depth_histogram"):
            self.assertEqual(c.detail[k], exp[k], k)
        ap = get_check(self.res, "closure_apply:has_component")
        self.assertEqual((ap.status, ap.detail["table_vs_recompute_diff"]), ("pass", 0))
        qty = self.check_role(f"SELECT sum(qty) FROM {ident.qualified(naming.closure_table(self.cfg, 'has_component'))}")[0][0]
        self.assertEqual(canon.canon(qty), canon.canon(exp["qty_checksum"]))
        self.assertEqual(self.res.status, "pass")
        self.assertIn("sxc.py reason", self.res.next_safe_action)

    def test_files_record_the_build(self):
        d = self.out / "build"
        build_sql = (d / "build.sql").read_text(encoding="utf-8")
        self.assertTrue(build_sql.startswith(stage_build.BUILD_SQL_HEADER))
        for s in self.cfg.shapes:
            self.assertIn(f"CREATE OR REPLACE VIEW {ident.qualified(naming.shape_view(self.cfg, s.id))} AS", build_sql)
        self.assertIn("MERGE INTO", build_sql)
        self.assertEqual((d / "shapes.ttl").read_text(encoding="utf-8"), shapes.shapes_ttl(self.cfg)[0])
        self.assertEqual((d / "fk_review.sql").read_text(encoding="utf-8"), shapes.fk_review_sql(self.cfg))
        for f in d.iterdir():
            self.assertNotIn(b"\r\n", f.read_bytes(), f.name)
            self.assert_no_row_values(f.read_text(encoding="utf-8"))

    def test_rebuild_changes_nothing(self):
        res = stage_build.run(self.cfg, args(), self.tmp / "rebuild")
        self.assertEqual(res.status, "pass")
        merge = get_check(res, "closure_apply:has_component").detail["merge"]
        self.assertEqual(merge, {"insert": 0, "update": 0, "delete": 0})

    def test_an_acyclic_build_removes_old_cycle_examples(self):
        cycles = naming.cycles_table(self.cfg, "has_component")
        self.check_role(f"CREATE TABLE IF NOT EXISTS {ident.qualified(cycles)} (start text, length int, path text)", write=True)
        res = stage_build.run(self.cfg, args("has_component"), self.tmp / "acyclic")
        self.assertEqual(get_check(res, "closure:has_component").status, "pass")
        self.assertFalse(self.relation_exists(cycles))

    def test_a_view_with_other_columns_is_replaced(self):
        view = ident.qualified(naming.shape_view(self.cfg, "qty_positive"))
        self.check_role(f"DROP VIEW {view}", write=True)
        self.check_role(f"CREATE VIEW {view} AS SELECT 1 AS other_column", write=True)
        res = stage_build.run(self.cfg, args("has_component"), self.tmp / "replace")
        self.assertEqual(get_check(res, "shape:qty_positive").status, "pass")
        cols = self.check_role("SELECT column_name FROM information_schema.columns WHERE table_schema = 'sxc_out' "
                               "AND table_name = 'v_shape_qty_positive' ORDER BY ordinal_position")
        self.assertEqual([r[0] for r in cols], self.cfg.entities["bom_line"].key)
        self.assertIn(f"DROP VIEW {view};", (self.tmp / "replace" / "build" / "build.sql").read_text(encoding="utf-8"))

    def test_database_errors_stay_with_their_shape(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.shapes += [Shape(id="unknown_column", target="sales_order", sql_violation="t.no_such_column > 0"),
                       Shape(id="divide_by_zero", target="sales_order", sql_violation="t.qty_total / 0 > 1"),
                       Shape(id="commented", target="sales_order", sql_violation="t.qty_open > t.qty_total -- open above total")]
        self.addCleanup(self.check_role, f"DROP VIEW IF EXISTS {ident.qualified(naming.shape_view(cfg, 'commented'))}", True)
        res = stage_build.run(cfg, args("has_component"), self.tmp / "errors")
        unknown, divide = get_check(res, "shape:unknown_column"), get_check(res, "shape:divide_by_zero")
        self.assertEqual((unknown.status, unknown.detail["sqlstate"], unknown.detail["violations"]), ("error", "42703", None))
        self.assertEqual((divide.status, divide.detail["sqlstate"]), ("error", "22012"))
        self.assertFalse(self.relation_exists(naming.shape_view(cfg, "divide_by_zero")))   # no broken view left behind
        self.assertEqual(get_check(res, "shape:commented").status, "pass")
        for s in self.cfg.shapes:
            self.assertEqual(get_check(res, f"shape:{s.id}").status, "pass")
        self.assertEqual(get_check(res, "closure:has_component").status, "pass")
        self.assertEqual(res.status, "error")
        self.assertIn("[shapes[4] unknown_column]", res.next_safe_action)

    def test_unknown_tables_and_columns_are_configuration_errors(self):
        typo = copy.deepcopy(self.cfg)
        typo.shapes.append(Shape(id="typo", target="bom_line", path="qty_perr", min_exclusive=Decimal(0)))
        no_table = copy.deepcopy(self.cfg)
        no_table.entities["stock"].table = "sxc_demo.no_such_table"
        edge = copy.deepcopy(self.cfg)
        edge.hierarchies["has_component"].quantity = "qty"
        for cfg, ref, name in ((typo, "shapes[4] typo", "qty_perr"), (no_table, "shapes[2] stock_nonnegative", "no_such_table"),
                               (edge, "hierarchies.has_component", "column qty ")):
            with self.assertRaises(StageError) as e:
                stage_build.run(cfg, args(), self.tmp / "typo")
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
            self.assertIn(f"[{ref}]", str(e.exception))
            self.assertIn(name, str(e.exception))

    def test_unreadable_tables_are_environment_errors(self):
        self.admin("CREATE TABLE IF NOT EXISTS sxc_demo.unreadable (part_id text, location_id text, qty numeric)",
                   "CREATE SCHEMA IF NOT EXISTS sxc_hidden",
                   "CREATE TABLE IF NOT EXISTS sxc_hidden.stock (part_id text, location_id text, qty numeric)",
                   f'GRANT SELECT ON sxc_hidden.stock TO "{self.fx.check}"')   # but no USAGE on the schema
        for table in ("sxc_demo.unreadable", "sxc_hidden.stock"):
            cfg = copy.deepcopy(self.cfg)
            cfg.entities["stock"].table = table
            with self.assertRaises(StageError) as e:
                stage_build.run(cfg, args(), self.tmp / "unreadable")
            self.assertEqual(e.exception.exit_code, EXIT_ENV)
            self.assertIn(f"cannot read {table} (used by [shapes[2] stock_nonnegative])", str(e.exception))

    def test_only_the_named_hierarchy_is_built(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.hierarchies["uses_part"] = Hierarchy(name="uses_part", edge="bom_line", node="part", parent="child_id",
                                                 child="parent_id", quantity="qty_per")
        res = stage_build.run(cfg, args("has_component"), self.tmp / "one")
        self.assertEqual({c.id for c in res.checks if c.id.startswith("closure")},
                         {"closure:has_component", "closure_apply:has_component", "closure:uses_part"})
        self.assertEqual(get_check(res, "closure:has_component").status, "pass")
        excluded = get_check(res, "closure:uses_part")
        self.assertEqual((excluded.status, excluded.config_ref, excluded.detail["reason"]),
                         ("not_run", "hierarchies.uses_part", "excluded_by_option"))
        self.assertEqual(res.status, "not_run")                 # a partial build is not a pass
        self.assertIn("rerun build without --hierarchy", res.next_safe_action)
        self.assertFalse(self.relation_exists(naming.closure_table(cfg, "uses_part")))
        res = stage_build.run(cfg, args(), self.tmp / "both")
        self.assertEqual(get_check(res, "closure:uses_part").detail["pairs"], self.fx.data["expected"]["closure_pairs"])
        self.check_role(f"DROP TABLE IF EXISTS {ident.qualified(naming.closure_table(cfg, 'uses_part'))}", write=True)

    def test_cli_passes_without_row_values(self):
        code, console, out = self.run_cli()
        self.assertEqual(code, EXIT_PASS, console)
        result = (out / "build" / "result.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(result)["status"], "pass")
        self.assert_no_row_values(result, console)
        self.assertTrue((out / "build" / "build.sql").is_file() and (out / "build" / "shapes.ttl").is_file())

    def test_cli_rejects_an_unknown_hierarchy(self):
        code, console, _out = self.run_cli("--hierarchy", "no_such_hierarchy")
        self.assertEqual(code, EXIT_CONFIG, console)


class BuildDirtyTest(_BuildCase):
    variant = "dirty"
    database = f"{PREFIX}_dirty"

    def test_shape_counts_equal_the_reference(self):
        exp = self.fx.data["expected"]
        cases = {"child_exists": ("fail", exp["orphan_child_refs"]), "qty_positive": ("fail", exp["zero_qty_lines"]),
                 "stock_nonnegative": ("pass", exp["negative_stock_rows"]), "open_le_total": ("fail", exp["open_gt_total"])}
        for sid, (status, n) in cases.items():
            c = get_check(self.res, f"shape:{sid}")
            self.assertEqual((c.status, c.detail["violations"]), (status, n), sid)
            self.assertGreater(n, 0)
            self.assertEqual(self.view_count(sid), n)
        self.assertEqual(get_check(self.res, "shape:stock_nonnegative").detail["severity"], "warning")

    def test_closure_fails_on_the_cycle(self):
        c = get_check(self.res, "closure:has_component")
        self.assertEqual((c.status, c.detail["reason"], c.detail["closure_applied"]), ("fail", "cycle", False))
        self.assertGreater(c.detail["cycle_nodes"], 0)
        self.assertGreater(c.detail["cycle_examples"], 0)
        self.assertFalse([x for x in self.res.checks if x.id == "closure_apply:has_component"])
        cycles = self.check_role(f"SELECT count(*) FROM {ident.qualified(naming.cycles_table(self.cfg, 'has_component'))}")
        self.assertEqual(int(cycles[0][0]), c.detail["cycle_examples"])
        self.assertEqual(self.res.status, "fail")

    def test_next_safe_action_points_at_cycles_and_views(self):
        action = self.res.next_safe_action
        self.assertIn(naming.cycles_table(self.cfg, "has_component"), action)
        self.assertIn('hierarchies.has_component.on_cycle = "report"', action)
        for sid in ("child_exists", "qty_positive", "open_le_total"):
            self.assertIn(naming.shape_view(self.cfg, sid), action)
        self.assertNotIn(naming.shape_view(self.cfg, "stock_nonnegative"), action)

    def test_on_cycle_report_builds_the_rest(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.hierarchies["has_component"].on_cycle = "report"
        res = stage_build.run(cfg, args(), self.tmp / "report")
        c = get_check(res, "closure:has_component")
        self.assertEqual(c.status, "pass")
        self.assertGreater(c.detail["dropped_cycle_edges"], 0)
        self.assertEqual(get_check(res, "closure_apply:has_component").status, "pass")
        self.assertEqual(len([n for n in res.not_proven if n.startswith("closure:has_component")]), 1)

    def test_cli_fails_without_row_values(self):
        code, console, out = self.run_cli()
        self.assertEqual(code, EXIT_FAIL, console)
        result = (out / "build" / "result.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(result)["status"], "fail")
        self.assert_no_row_values(result, console)
        for name in ("build.sql", "shapes.ttl", "fk_review.sql"):
            self.assertTrue((out / "build" / name).is_file(), name)

    @unittest.skipUnless(rdflib and pyshacl, "rdflib and pyshacl are not installed")
    def test_shacl_flags_exactly_the_view_keys(self):
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn):
                rows = {e: db.rows(conn, f"SELECT * FROM {ident.qualified(self.cfg.entities[e].table)}")
                        for e in ("part", "bom_line", "stock")}
                views = {}
                for s in self.cfg.shapes:
                    if not shapes.no_shacl_reason(s):
                        found = conn.run(f"SELECT * FROM {ident.qualified(naming.shape_view(self.cfg, s.id))}")
                        views[s.id] = {tuple(canon.canon(v) for v in r) for r in found}
        finally:
            conn.close()
        ttl = (self.out / "build" / "shapes.ttl").read_text(encoding="utf-8")
        flagged, _severities = focus_keys(self.cfg, export_graph(self.cfg, rows), ttl)
        self.assertEqual(sorted(views), ["child_exists", "qty_positive", "stock_nonnegative"])
        for sid, keys in views.items():
            got = flagged.get(sid, set())
            self.assertTrue(got == keys, f"{sid}: {len(got)} SHACL focus nodes vs {len(keys)} view rows, {len(got ^ keys)} differ")


class BuildOutputSchemaTest(_BuildCase):
    """The output schema is dropped after the first build: build must recreate it only when the role may."""

    database = f"{PREFIX}_schema"

    def test_missing_schema_is_created_only_when_the_role_may(self):
        self.admin("DROP SCHEMA IF EXISTS sxc_out CASCADE", f'REVOKE CREATE ON DATABASE "{self.fx.name}" FROM "{self.fx.check}"')
        code, console, out = self.run_cli()
        self.assertEqual(code, EXIT_ENV, console)
        message = json.loads((out / "build" / "result.json").read_text(encoding="utf-8"))["checks"][0]["detail"]["message"]
        self.assertIn(f"CREATE SCHEMA sxc_out AUTHORIZATION {self.fx.check}", message)
        self.admin(f'GRANT CREATE ON DATABASE "{self.fx.name}" TO "{self.fx.check}"')
        code, console, out = self.run_cli()
        self.assertEqual(code, EXIT_PASS, console)
        owner = self.check_role("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'sxc_out'")
        self.assertEqual(owner[0][0], self.fx.check)
        self.assertIn('CREATE SCHEMA "sxc_out";', (out / "build" / "build.sql").read_text(encoding="utf-8"))

    def test_schema_without_create_privilege_is_an_environment_error(self):
        self.admin("DROP SCHEMA IF EXISTS sxc_ro CASCADE", "CREATE SCHEMA sxc_ro", f'GRANT USAGE ON SCHEMA sxc_ro TO "{self.fx.check}"')
        cfg = copy.deepcopy(self.cfg)
        cfg.source.output_schema = "sxc_ro"
        with self.assertRaises(StageError) as e:
            stage_build.run(cfg, args(), self.tmp / "ro")
        self.assertEqual(e.exception.exit_code, EXIT_ENV)
        self.assertIn("USAGE and CREATE on output schema sxc_ro", str(e.exception))


del _BuildCase   # the base class has no tests of its own

if __name__ == "__main__":
    unittest.main()
