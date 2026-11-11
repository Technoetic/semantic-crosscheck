"""Generated text in gold SQL, the measure filter clauses it is matched against, and the value_rows count.

Why: a gold query that names the output schema, reads a generated relation by its bare name or copies a generated
filter clause verbatim compares the generated layers with themselves, so its checks are generated evidence, not
authored. verify reads every gold file once at plan time and records only what matched (never the text). Rows that
were compared prove little about a measure when every gold value was NULL, 0 or empty (gold SQL usually coalesces),
so compare_rows counts, per gold column, the matched rows whose gold value holds something; that count never reaches
a V1 or V2 check detail.
"""

import dataclasses
import unittest
from decimal import Decimal

import pgfixture
from semantic_crosscheck import compare, config, cube_gen, stage_verify
from test_compare_unit import plain_gold

ON_HAND = "upper(btrim(t.\"location_id\")) IN ('MAIN', 'SUB')"
FIRM = "t.\"status\" IN ('FIRM')"


def demo_cfg():
    return config.load(pgfixture.DEMO_CONFIG)


class GoldTextTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()

    def found(self, text: str) -> list:
        return stage_verify.generated_text(self.cfg, text)

    def test_matches(self):
        sql = "SELECT part_id, sum(qty) FROM sxc_demo.stock t WHERE " + ON_HAND + " GROUP BY part_id"
        self.assertEqual(self.found(sql), ["filter:measures.on_hand"])
        spaced = "SELECT 1 FROM sxc_demo.stock t\nWHERE upper(btrim(t.\"location_id\"))\n   IN  ('MAIN',\t'SUB')"
        self.assertEqual(self.found(spaced), ["filter:measures.on_hand"], "whitespace runs are collapsed")
        self.assertEqual(self.found("SELECT 1 FROM sxc_demo.sales_order t WHERE " + FIRM),
                         ["filter:measures.firm_open", "filter:measures.firm_open_dated"],
                         "one clause generated for two measures names both")
        self.assertEqual(self.found("-- " + ON_HAND + "\nSELECT 1"), [], "text only in a line comment")
        self.assertEqual(self.found("SELECT 1 /* " + ON_HAND + " */"), [], "text only in a block comment")
        self.assertEqual(self.found("SELECT '--' AS x WHERE " + ON_HAND), ["filter:measures.on_hand"],
                         "a comment marker inside a literal starts no comment")
        self.assertEqual(self.found('SELECT 1 FROM sxc_demo.stock t WHERE t."part_id" IS NOT NULL'), [],
                         "the non-null grain clause alone is too common to count")
        self.assertEqual(self.found("SELECT * FROM sxc_out.x"), ["output_schema"])
        self.assertEqual(self.found('SELECT * FROM "sxc_out"."x"'), ["output_schema"])
        self.assertEqual(self.found("SELECT * FROM SXC_OUT . x"), ["output_schema"], "case-insensitive")
        self.assertEqual(self.found("SELECT * FROM sxc_outer.x"), [])
        self.assertEqual(self.found("SELECT * FROM my_sxc_out.x"), [])
        self.assertEqual(self.found("SELECT * FROM has_component_closure c"), ["relation:has_component_closure"])
        self.assertEqual(self.found('SELECT * FROM "has_component_closure"'), ["relation:has_component_closure"])
        self.assertEqual(self.found("SELECT * FROM HAS_COMPONENT_CLOSURE"), ["relation:has_component_closure"])
        self.assertEqual(self.found("SELECT * FROM has_component_closure_old"), [])
        self.assertEqual(self.found("SELECT * FROM x.has_component_closure"), [])
        self.assertEqual(self.found("SELECT * FROM sxc_out.has_component_closure"), ["output_schema"],
                         "a schema-qualified name is the output-schema match only")
        self.assertEqual(self.found("SELECT * FROM has_component_cycles, v_shape_child_exists"),
                         ["relation:has_component_cycles", "relation:v_shape_child_exists"])
        self.assertEqual(self.found("SELECT * FROM v_rule_open_orders JOIN v_rules_subjects USING (k)"),
                         ["relation:v_rule_open_orders", "relation:v_rules_subjects"])
        self.assertEqual(self.found("SELECT * FROM v_shape_nope"), [], "only shapes of this configuration")
        both = "SELECT * FROM sxc_out.v_shape_qty_positive s, sxc_demo.stock t WHERE " + ON_HAND
        self.assertEqual(self.found(both), ["output_schema", "filter:measures.on_hand"])

    def test_five_demo_golds_are_not_flagged(self):
        self.assertEqual(len(self.cfg.gold), 5)
        for g in self.cfg.gold:
            with self.subTest(gold=g.id):
                self.assertEqual(self.found(g.sql_path(self.cfg).read_text(encoding="utf-8-sig")), [])

    def test_normalised_text(self):
        self.assertEqual(stage_verify.gold_text("SELECT  a -- x\n FROM\tb /* y\n z */ WHERE c = '--' "),
                         "SELECT a FROM b WHERE c = '--' ")
        self.assertEqual(stage_verify.gold_text("SELECT 'it''s -- kept' -- dropped"), "SELECT 'it''s -- kept' ")
        self.assertEqual(stage_verify.gold_text('SELECT "a--b" FROM t'), 'SELECT "a--b" FROM t',
                         "a comment marker inside a quoted identifier starts no comment")

    def test_where_clauses_join(self):
        for m in self.cfg.measures.values():
            with self.subTest(measure=m.name):
                self.assertEqual(" AND ".join(cube_gen.where_clauses(m)), cube_gen.where_sql(m))
                self.assertEqual(" AND ".join(cube_gen.where_clauses(m, "s")), cube_gen.where_sql(m, "s"))
                self.assertEqual(cube_gen.where_clauses(m)[0], f't."{m.by}" IS NOT NULL')
        m = dataclasses.replace(self.cfg.measures["on_hand"], filter={"status": ["A AND B", "C"]},
                                filter_normalized={"location_id": ["MAIN"]})
        clauses = cube_gen.where_clauses(m)
        self.assertEqual(clauses, ['t."part_id" IS NOT NULL', "t.\"status\" IN ('A AND B', 'C')",
                                   "upper(btrim(t.\"location_id\")) IN ('MAIN')"])
        self.assertEqual(" AND ".join(clauses), cube_gen.where_sql(m))


class ValueRowsTest(unittest.TestCase):
    def test_value_rows(self):
        g = plain_gold(cols=("k", "v", "w"))
        gold = [{"k": "a", "v": 1, "w": None}, {"k": "b", "v": 0, "w": ""}, {"k": "c", "v": Decimal("2.5"), "w": "x"},
                {"k": "d", "v": 5, "w": "y"}, {"k": "e", "v": Decimal("0.00"), "w": False}]
        cube = [{"k": "a", "v": 1, "w": None}, {"k": "b", "v": 0, "w": ""}, {"k": "c", "v": 2.5, "w": "x"},
                {"k": "e", "v": 0, "w": False}]
        r = compare.compare_rows(g, gold, cube)
        self.assertEqual(r["matched_rows"], 4)
        self.assertEqual(r["value_rows"], {"k": 4, "v": 2, "w": 2},
                         "matched rows only (d is missing in Cube); NULL, numeric 0 and '' hold no value")

    def test_cube_null_declared_as_zero_counts_no_value(self):
        g = plain_gold(zero=("v",))
        r = compare.compare_rows(g, [{"k": "a", "v": 0}], [{"k": "a", "v": None}])
        self.assertTrue(r["pass"])
        self.assertEqual(r["declared_null_as_zero"], 1)
        self.assertEqual(r["value_rows"], {"k": 1, "v": 0})

    def test_empty_comparison_counts_zero_per_column(self):
        self.assertEqual(compare.compare_rows(plain_gold(), [], [])["value_rows"], {"k": 0, "v": 0})

    def test_check_details_leave_value_rows_out(self):
        r = compare.compare_rows(plain_gold(), [{"k": "a", "v": 3}], [{"k": "a", "v": 3}])
        self.assertNotIn("value_rows", stage_verify._detail(r))
        agg = stage_verify._Sweep()
        agg.add(0, r)
        agg.add(1, compare.compare_rows(plain_gold(), [{"k": "b", "v": 0}], [{"k": "b", "v": 0}]))
        agg.add(2, {"pass": False, "side": "gold", "error": "DatabaseError"})
        self.assertNotIn("value_rows", agg.detail())
        self.assertEqual(agg.value_rows, {"k": 2, "v": 1}, "summed internally over the comparisons that ran")


if __name__ == "__main__":
    unittest.main()
