"""demo: rerunning it at any time, and what demo/expected.json promises.

Unit tests (no database) cover the expected.json document (which numbers apply to which variant, which check shows
each planted defect), the comparison of existing tables with the demo definition and the hint for a rolled-back
load. Integration tests (skipped unless SXC_TEST_ADMIN_DSN names a superuser connection) run in a throw-away
database named sxc_f_first_demo: the README walkthrough of a clean run followed by the dirty variant in the same
database, and demo tables that exist with other columns.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import pgfixture
import sxc
from semantic_crosscheck import config, demo, stage_build, stage_demo
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_PASS

DB_NAME = "sxc_f_first_demo"
PLANTED = ("cycle", "orphan_children", "zero_qty_lines", "negative_stock_rows", "open_gt_total", "raw_with_child",
           "location_variants", "out_of_range_due", "malformed_csv_rows")


class ExpectedDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clean = demo.build(False)["expected"]
        cls.dirty = demo.build(True)["expected"]

    def test_closure_numbers_are_a_reference_for_the_clean_variant_only(self):
        self.assertFalse(self.clean["has_cycle"])
        self.assertTrue(all(isinstance(self.clean[k], (int, str, dict)) for k in demo.CLOSURE_KEYS))
        self.assertTrue(self.dirty["has_cycle"])
        self.assertTrue(all(self.dirty[k] is None for k in demo.CLOSURE_KEYS))
        clean = demo.reference(self.clean, "clean", 1, 1)["closure"]
        self.assertEqual((clean["applies_to"], clean["compared"]), (["clean"], True))
        self.assertEqual({k: clean[k] for k in demo.CLOSURE_KEYS}, {k: self.clean[k] for k in demo.CLOSURE_KEYS})
        self.assertIn("build closure:has_component", clean["compare_with"])
        dirty = demo.reference(self.dirty, "dirty", 1, 1)["closure"]
        self.assertEqual((dirty["applies_to"], dirty["compared"]), (["clean"], False))
        self.assertIn("cycle", dirty["not_compared"])
        self.assertFalse(set(demo.CLOSURE_KEYS) & set(dirty))

    def test_every_planted_defect_names_the_check_that_shows_it(self):
        doc = demo.reference(self.dirty, "dirty", 20261013, 1)
        planted = doc["planted_defects"]
        self.assertEqual(planted["applies_to"], ["dirty"])
        self.assertEqual(set(planted) - {"applies_to", "note"}, set(PLANTED))
        for key in PLANTED:
            entry = planted[key]
            self.assertEqual(entry["count"], self.dirty[key], key)
            self.assertGreater(entry["count"], 0, key)
            if entry["reported"]:
                self.assertRegex(entry["check"], r"^(demo|closure|shape|owl):", key)
            else:
                self.assertTrue(entry["dirty_outcome"].startswith("not reported:"), key)
        self.assertEqual((planted["cycle"]["stage"], planted["cycle"]["check"]), ("build", "closure:has_component"))
        self.assertEqual(planted["raw_with_child"]["check"], "owl:consistency")
        self.assertEqual(planted["malformed_csv_rows"]["check"], "demo:copy_on_error")
        self.assertEqual(planted["location_variants"]["exercised_by"],
                         ["v1:q03_stock", "v2:q03_stock:values", "v4:measure_normalization_drop:on_hand"])
        self.assertEqual(planted["out_of_range_due"]["exercised_by"], ["v1:q05_firm_window", "v2:q05_firm_window:param_sets"])
        self.assertEqual((doc["variant"], doc["seed"], doc["scale"]), ("dirty", 20261013, 1))

    def test_clean_counts_are_zero_and_rows_apply_to_both_variants(self):
        doc = demo.reference(self.clean, "clean", 7, 2)
        self.assertTrue(all(doc["planted_defects"][k]["count"] == 0 for k in PLANTED))
        self.assertEqual(doc["rows"]["applies_to"], ["clean", "dirty"])
        self.assertEqual(doc["rows"]["parts"], self.clean["parts"])
        self.assertEqual((doc["graph"]["edges"], doc["graph"]["has_cycle"]), (self.clean["edges"], False))
        json.dumps(doc)   # serialisable as written by the stage


class TableDefinitionAndHints(unittest.TestCase):
    def test_matching_tables_have_no_problems(self):
        found = {name: ("r", dict(t.columns)) for name, t in demo.TABLES.items()}
        self.assertEqual(demo.table_problems(found), [])
        self.assertEqual(demo.table_problems({}), [])

    def test_differences_are_named(self):
        found = {"stock": ("r", {"part_id": "text", "location_id": "text", "quantity": "numeric(18,3)"}),
                 "bom_line": ("r", {**dict(demo.TABLES["bom_line"].columns), "line_no": "bigint"}),
                 "part": ("v", {})}
        self.assertEqual(demo.table_problems(found), [
            "sxc_demo.part is not a table",
            "sxc_demo.bom_line: line_no is bigint instead of integer",
            "sxc_demo.stock: missing column qty; unexpected column quantity",
        ])

    def test_create_statement_declares_every_column(self):
        sql = demo.create_sql("bom_line")
        self.assertEqual(sql, "CREATE TABLE sxc_demo.bom_line (parent_id text NOT NULL, child_id text NOT NULL, line_no "
                              "integer NOT NULL, qty_per numeric(18,6) NOT NULL, PRIMARY KEY (parent_id, child_id, line_no))")

    def test_load_hints_name_the_cause(self):
        self.assertIn("lacks a privilege: it must own the demo tables", stage_demo.load_hint("42501", "SXC_PG_OWNER_DSN"))
        self.assertIn("foreign key", stage_demo.load_hint("0A000", "SXC_PG_OWNER_DSN"))
        self.assertIn("constraint added to the demo tables", stage_demo.load_hint("23503", "SXC_PG_OWNER_DSN"))
        other = stage_demo.load_hint("XX000", "SXC_PG_OWNER_DSN")
        self.assertTrue(other.startswith("the load was rolled back (SQLSTATE XX000): check the server log"))
        for code in ("42501", "2BP01", "XX000"):
            self.assertNotIn("owns schema", stage_demo.load_hint(code, "SXC_PG_OWNER_DSN"))


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class DemoRerunIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(DB_NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)

    def setUp(self):
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)
        os.environ["SXC_PG_OWNER_DSN"] = pgfixture.dsn_for(self.fx.owner, self.fx.name)
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_firstrun_demo_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def admin(self, *statements: str):
        a = pgfixture._admin(self.fx.name)
        try:
            for s in statements:
                a.run(s)
        finally:
            a.close()

    def demo_cli(self, variant: str) -> tuple[int, str, dict]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = sxc.main(["demo", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(self.tmp), "--variant", variant])
        return code, buf.getvalue(), json.loads((self.tmp / "demo" / "result.json").read_text(encoding="utf-8"))

    def build(self):
        return stage_build.run(self.cfg, types.SimpleNamespace(hierarchy=""), self.tmp / "out")

    def count(self, table: str) -> int:
        conn = self.fx.check_conn()
        try:
            return int(conn.run(f"SELECT count(*) FROM {table}")[0][0])
        finally:
            conn.close()

    def test_the_dirty_variant_reloads_after_a_clean_build(self):
        self.assertEqual(self.build().status, "pass")
        code, console, saved = self.demo_cli("dirty")
        self.assertEqual(code, EXIT_PASS, console)
        dirty = demo.build(True)
        self.assertEqual(self.count("sxc_demo.stock"), len(dirty["stock"]))
        res = self.build()                                # the views build created on the tables still work
        exp = dirty["expected"]
        violations = {c.id: c.detail.get("violations") for c in res.checks if c.id.startswith("shape:")}
        self.assertEqual(violations, {"shape:child_exists": exp["orphan_child_refs"], "shape:qty_positive": exp["zero_qty_lines"],
                                      "shape:stock_nonnegative": exp["negative_stock_rows"],
                                      "shape:open_le_total": exp["open_gt_total"]})
        closure = next(c for c in res.checks if c.id == "closure:has_component")
        self.assertEqual((closure.status, closure.detail["reason"], closure.detail["edges"]), ("fail", "cycle", exp["edges"]))
        expected = json.loads((self.tmp / "demo" / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["closure"]["compared"], False)
        code, console, _saved = self.demo_cli("clean")
        self.assertEqual(code, EXIT_PASS, console)
        self.assertEqual(self.build().status, "pass")
        self.assertEqual(self.count("sxc_out.v_shape_qty_positive"), 0)

    def test_tables_with_other_columns_stop_the_stage(self):
        before = self.count("sxc_demo.stock")
        self.admin("ALTER TABLE sxc_demo.stock RENAME COLUMN qty TO quantity")
        self.addCleanup(self.admin, "ALTER TABLE sxc_demo.stock RENAME COLUMN quantity TO qty")
        code, console, saved = self.demo_cli("dirty")
        self.assertEqual(code, EXIT_CONFIG, console)
        message = saved["checks"][0]["detail"]["message"]
        self.assertIn("sxc_demo.stock: missing column qty; unexpected column quantity", message)
        self.assertIn("never drops a table", message)
        self.assertEqual(self.count("sxc_demo.stock"), before)        # nothing was written
        self.assertFalse((self.tmp / "demo" / "expected.json").exists())

    def test_missing_tables_are_created_with_the_demo_definition(self):
        self.admin("DROP TABLE sxc_demo.stock CASCADE")
        code, console, _saved = self.demo_cli("clean")
        self.assertEqual(code, EXIT_PASS, console)
        conn = pgfixture._admin(self.fx.name)
        try:
            found = demo.existing_tables(conn)
        finally:
            conn.close()
        self.assertEqual(found["stock"], ("r", dict(demo.TABLES["stock"].columns)))
        self.assertEqual(self.count("sxc_demo.stock"), len(demo.build(False)["stock"]))   # the check role can read it


if __name__ == "__main__":
    unittest.main()
