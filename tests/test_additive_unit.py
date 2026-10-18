"""V3 additivity over many_to_one / one_to_one join paths (shape_q, design section 7): no database, no Cube.

  JoinPathTest           the path Cube uses between a measure's cube and a dimension's cube, seen from the measure:
                         declared joins from the measure side keep their relationship, joins declared from the
                         dimension side are inverted, cubes Cube cannot join on their own have no path, and only
                         paths made of many_to_one / one_to_one steps are additive.
  AdditiveCatalogueTest  which additive tests the model yields: the node cube for sum/count measures and the closure
                         ends for qty/pairs; paths with a one_to_many (or unknown) step and non-additive aggregations
                         get no test, and the paths left out for their relationship are listed (not applicable).
  NullGroupTest          the NULL group Cube returns is compared with an independent anti-join over the measure
                         source (rows without a dimension row with a non-null value); a mismatch fails even when the
                         groups add up, the SQL runs once per test, and the detail holds flags and counts only.
  StageNotesTest         verify lists the not applicable paths in not_proven when V3 runs.
"""

import argparse
import contextlib
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import cube_gen, shape_q, stage_verify
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import Result
from test_compare_unit import FakeClient, FakeConn, demo_cfg, demo_model

CLOSURE_ADDITIVE = [
    "additive:has_component_closure.qty:has_component_ancestor.part_class",
    "additive:has_component_closure.qty:has_component_descendant.part_class",
    "additive:has_component_closure.pairs:has_component_ancestor.part_class",
    "additive:has_component_closure.pairs:has_component_descendant.part_class",
]
NODE_ADDITIVE = ["additive:m_firm_open.value:part.part_class", "additive:m_on_hand.value:part.part_class",
                 "additive:m_consigned.value:part.part_class"]
SECRET_VALUE = "987654.321"


def cubes_of(model: dict) -> dict:
    return {c["name"]: c for c in model["cubes"]}


def set_relationship(model: dict, cube: str, target: str, relationship: str):
    join = next(j for j in cubes_of(model)[cube]["joins"] if j["name"] == target)
    join["relationship"] = relationship


def additive_ids(cfg, model) -> list[str]:
    return [t["id"] for t in shape_q.tests(cfg, model) if t["kind"] == "additive"]


class JoinPathTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.cubes = cubes_of(demo_model(self.cfg))

    def test_many_to_one_declared_from_the_measure_side(self):
        steps = shape_q.join_path(self.cubes, "has_component_closure", "has_component_ancestor")
        self.assertEqual(steps, [("has_component_closure", "has_component_ancestor", "many_to_one")])
        self.assertTrue(shape_q.additive_path(steps))

    def test_join_declared_from_the_dimension_side_is_inverted(self):
        steps = shape_q.join_path(self.cubes, "m_on_hand", "part")
        self.assertEqual(steps, [("m_on_hand", "part", "one_to_one")])
        self.assertTrue(shape_q.additive_path(steps))
        steps = shape_q.join_path(self.cubes, "m_firm_open", "has_component_closure")
        self.assertEqual(steps, [("m_firm_open", "has_component_closure", "one_to_many")],
                         "the closure declares many_to_one to the measure cube: one measure row, many closure rows")
        self.assertFalse(shape_q.additive_path(steps))

    def test_cubes_cube_cannot_join_on_their_own_have_no_path(self):
        self.assertIsNone(shape_q.join_path(self.cubes, "m_on_hand", "has_component_ancestor"))
        self.assertIsNone(shape_q.join_path(self.cubes, "has_component_closure", "part"))
        self.assertIsNone(shape_q.join_path(self.cubes, "m_firm_open_dated", "part"))

    def test_relationship_aliases_and_unknown_relationships(self):
        self.assertEqual(shape_q.relationship({"relationship": "belongsTo"}), "many_to_one")
        self.assertEqual(shape_q.relationship({"relationship": "belongs_to"}), "many_to_one")
        self.assertEqual(shape_q.relationship({"relationship": "hasMany"}), "one_to_many")
        self.assertEqual(shape_q.relationship({"relationship": "has_one"}), "one_to_one")
        self.assertEqual(shape_q.relationship({}), "")
        self.assertFalse(shape_q.additive_path([("a", "b", "many_to_many")]))
        self.assertFalse(shape_q.additive_path([("a", "b", "")]))
        self.assertFalse(shape_q.additive_path([("a", "b", "many_to_one"), ("b", "c", "one_to_many")]))
        self.assertTrue(shape_q.additive_path([("a", "b", "many_to_one"), ("b", "c", "one_to_one")]))
        self.assertFalse(shape_q.additive_path([]), "no join is not a join path")

    def test_multi_step_paths_follow_one_direction(self):
        cubes = {"x": {"name": "x", "joins": [{"name": "y", "relationship": "many_to_one"}]},
                 "y": {"name": "y", "joins": [{"name": "z", "relationship": "belongsTo"}]},
                 "z": {"name": "z"}}
        self.assertEqual(shape_q.join_path(cubes, "x", "z"), [("x", "y", "many_to_one"), ("y", "z", "many_to_one")])
        self.assertEqual(shape_q.join_path(cubes, "z", "x"), [("z", "y", "one_to_many"), ("y", "x", "one_to_many")])
        cubes["y"]["joins"] = []
        cubes["z"]["joins"] = [{"name": "y", "relationship": "one_to_many"}]
        self.assertIsNone(shape_q.join_path(cubes, "x", "z"), "x -> y <- z: neither cube reaches the other")


class AdditiveCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.model = demo_model(self.cfg)

    def test_demo_model_adds_the_closure_ends(self):
        self.assertEqual(additive_ids(self.cfg, self.model), NODE_ADDITIVE + CLOSURE_ADDITIVE)
        self.assertEqual(shape_q.not_applicable(self.cfg, self.model), [])
        tests = {t["id"]: t for t in shape_q.tests(self.cfg, self.model)}
        t = tests[CLOSURE_ADDITIVE[0]]
        self.assertEqual((t["members"], t["config_ref"], t["agg"]),
                         (["has_component_closure.qty", "has_component_ancestor.part_class"], "hierarchies.has_component",
                          "sum"))
        self.assertEqual(t["path"], [["has_component_closure", "has_component_ancestor", "many_to_one"]])
        self.assertEqual(tests[CLOSURE_ADDITIVE[2]]["agg"], "count")
        node = tests[NODE_ADDITIVE[1]]
        self.assertEqual((node["config_ref"], node["agg"], node["path"]),
                         ("measures.on_hand", "sum", [["m_on_hand", "part", "one_to_one"]]))

    def test_one_to_many_paths_get_no_test_and_are_listed(self):
        set_relationship(self.model, "part", "m_on_hand", "many_to_one")
        set_relationship(self.model, "has_component_closure", "has_component_ancestor", "one_to_many")
        ids = additive_ids(self.cfg, self.model)
        self.assertNotIn("additive:m_on_hand.value:part.part_class", ids)
        self.assertNotIn(CLOSURE_ADDITIVE[0], ids)
        self.assertNotIn(CLOSURE_ADDITIVE[2], ids)
        self.assertIn(CLOSURE_ADDITIVE[1], ids)
        lines = shape_q.not_applicable(self.cfg, self.model)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("v3:additive:m_on_hand.value:part.part_class: not applicable"), lines[0])
        self.assertIn("m_on_hand -> part (one_to_many)", lines[0])
        self.assertTrue(lines[1].startswith(f"v3:{CLOSURE_ADDITIVE[0]}: not applicable"), lines[1])
        self.assertTrue(lines[2].startswith(f"v3:{CLOSURE_ADDITIVE[2]}: not applicable"), lines[2])

    def test_many_to_many_and_unknown_relationships_are_not_additive(self):
        set_relationship(self.model, "has_component_closure", "has_component_ancestor", "many_to_many")
        set_relationship(self.model, "has_component_closure", "has_component_descendant", "sideways")
        ids = additive_ids(self.cfg, self.model)
        self.assertEqual(ids, NODE_ADDITIVE)
        self.assertEqual(len(shape_q.not_applicable(self.cfg, self.model)), 4)

    def test_join_declared_one_to_many_from_the_dimension_side_is_many_to_one_from_the_measure(self):
        set_relationship(self.model, "part", "m_on_hand", "one_to_many")
        t = next(t for t in shape_q.tests(self.cfg, self.model) if t["id"] == "additive:m_on_hand.value:part.part_class")
        self.assertEqual(t["path"], [["m_on_hand", "part", "many_to_one"]])

    def test_non_additive_aggregations_are_excluded(self):
        self.cfg.measures["on_hand"].agg = "max"
        self.cfg.measures["consigned"].agg = "min"
        model = demo_model(self.cfg)
        cubes_of(model)["has_component_closure"]["measures"][0]["type"] = "count_distinct"
        ids = additive_ids(self.cfg, model)
        self.assertEqual(ids, ["additive:m_firm_open.value:part.part_class"] + CLOSURE_ADDITIVE[2:])
        self.assertEqual(shape_q.not_applicable(self.cfg, model), [], "an excluded aggregation is not a join path")

    def test_count_measures_keep_their_aggregation(self):
        self.cfg.measures["on_hand"].agg = "count"
        t = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"] == NODE_ADDITIVE[1])
        self.assertEqual(t["agg"], "count")

    def test_without_class_column_there_is_nothing(self):
        self.cfg.ontology.class_column = ""
        self.assertEqual(additive_ids(self.cfg, self.model), [])
        self.assertEqual(shape_q.not_applicable(self.cfg, self.model), [])

    def test_without_closure_table_only_the_node_tests_remain(self):
        model = demo_model(self.cfg, closure=False)
        self.assertEqual(additive_ids(self.cfg, model), NODE_ADDITIVE)

    def test_an_additive_path_without_configuration_sql_gets_no_test(self):
        cubes = cubes_of(self.model)
        cubes["has_component_ancestor"]["joins"] = [{"name": "part", "relationship": "many_to_one", "sql": "x"}]
        cubes["has_component_closure"]["joins"] = [j for j in cubes["has_component_closure"]["joins"]
                                                   if j["name"] != "has_component_ancestor"]
        cubes["has_component_closure"]["joins"].append({"name": "has_component_ancestor", "relationship": "many_to_one",
                                                        "sql": "y"})
        ids = additive_ids(self.cfg, self.model)
        self.assertNotIn("additive:has_component_closure.qty:part.part_class", ids)
        lines = shape_q.not_applicable(self.cfg, self.model)
        self.assertTrue(any(line.startswith("v3:additive:has_component_closure.qty:part.part_class: not compared")
                            for line in lines), lines)


class NullGroupTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.model = demo_model(self.cfg)
        self.tests = {t["id"]: t for t in shape_q.tests(self.cfg, self.model)}

    def fresh(self, tid: str) -> dict:
        """A newly derived test (a test reads its SQL side once and keeps it)."""
        return next(t for t in shape_q.tests(self.cfg, self.model) if t["id"] == tid)

    def client(self, t: dict, total, groups: list) -> FakeClient:
        m, d = t["measure"], t["dimension"]
        return FakeClient(answers={((), (m,)): [{m: total}], ((d,), (m,)): [{d: k, m: v} for k, v in groups]})

    def test_closure_null_group_sql_is_an_anti_join_on_the_configured_key(self):
        sql = shape_q.null_group_sql(self.cfg, self.tests[CLOSURE_ADDITIVE[0]])
        self.assertIn('FROM sxc_out.has_component_closure t', sql)
        self.assertIn('NOT EXISTS (SELECT 1 FROM "sxc_demo"."part" d WHERE d."part_id" = t."ancestor" '
                      'AND d."part_class" IS NOT NULL)', sql)
        self.assertIn('NOT EXISTS (SELECT 1 FROM "sxc_demo"."part" d WHERE d."part_id" = t."ancestor")', sql)
        self.assertTrue(sql.startswith('SELECT sum(t."qty") FILTER'), sql)
        self.assertTrue(shape_q.null_group_sql(self.cfg, self.tests[CLOSURE_ADDITIVE[3]]).startswith(
            "SELECT count(*) FILTER"))
        self.assertIn('d."part_id" = t."descendant"', shape_q.null_group_sql(self.cfg, self.tests[CLOSURE_ADDITIVE[3]]))

    def test_measure_null_group_sql_keeps_the_measure_filters(self):
        sql = shape_q.null_group_sql(self.cfg, self.tests[NODE_ADDITIVE[1]])
        self.assertTrue(sql.startswith('SELECT sum(t."qty") FILTER'), sql)
        self.assertIn('FROM "sxc_demo"."stock" t WHERE t."part_id" IS NOT NULL', sql)
        self.assertIn("upper(btrim(t.\"location_id\")) IN ('MAIN', 'SUB')", sql)
        self.assertIn('d."part_id" = t."part_id"', sql)

    def test_groups_and_null_group_agree(self):
        t = self.tests[CLOSURE_ADDITIVE[0]]
        conn = FakeConn(rows=[[Decimal("2.000"), 3, 1]])
        st, detail = shape_q.run_test(conn, self.client(t, "7.5", [("raw", "5.5"), (None, "2")]), self.cfg, {}, t)
        self.assertEqual((st, detail), ("pass", {"equal": True, "groups": 2, "null_class_groups": 1,
                                                 "null_group_equal": True, "null_group_rows": 3, "orphan_rows": 1}))

    def test_null_group_mismatch_fails_even_when_the_groups_add_up(self):
        t = self.tests[CLOSURE_ADDITIVE[0]]
        conn = FakeConn(rows=[[Decimal("3"), 2, 2]])
        st, detail = shape_q.run_test(conn, self.client(t, "7.5", [("raw", "5.5"), (None, "2")]), self.cfg, {}, t)
        self.assertEqual(st, "fail")
        self.assertEqual((detail["equal"], detail["null_group_equal"]), (True, False))

    def test_rows_without_a_dimension_row_that_cube_drops_fail(self):
        t = self.tests[NODE_ADDITIVE[1]]
        conn = FakeConn(rows=[[Decimal(SECRET_VALUE), 4, 4]])
        st, detail = shape_q.run_test(conn, self.client(t, "10", [("raw", "10")]), self.cfg, {}, t)
        self.assertEqual(st, "fail")
        self.assertEqual(detail, {"equal": True, "groups": 1, "null_class_groups": 0, "null_group_equal": False,
                                  "null_group_rows": 4, "orphan_rows": 4})
        self.assertNotIn(SECRET_VALUE, json.dumps(detail))
        self.assertNotIn("987654", json.dumps(detail))

    def test_fan_out_fails_the_sum(self):
        t = self.tests[CLOSURE_ADDITIVE[1]]
        conn = FakeConn(rows=[[None, 0, 0]])
        st, detail = shape_q.run_test(conn, self.client(t, "7", [("raw", "5"), ("assembly", "4")]), self.cfg, {}, t)
        self.assertEqual((st, detail["equal"], detail["null_group_equal"]), ("fail", False, True))

    def test_empty_null_group_of_a_sum_is_null_on_both_sides(self):
        t = self.tests[CLOSURE_ADDITIVE[0]]
        st, detail = shape_q.run_test(FakeConn(rows=[[None, 0, 0]]), self.client(t, "4", [("raw", "4")]), self.cfg, {}, t)
        self.assertEqual((st, detail["null_group_equal"], detail["null_class_groups"]), ("pass", True, 0))
        t = self.fresh(CLOSURE_ADDITIVE[0])
        st, detail = shape_q.run_test(FakeConn(rows=[[Decimal(0), 1, 0]]), self.client(t, "4", [("raw", "4")]),
                                      self.cfg, {}, t)
        self.assertEqual((st, detail["null_group_equal"]), ("fail", False),
                         "a NULL group row SQL finds (sum 0) that Cube does not return is a difference")

    def test_count_null_group_of_nothing_is_zero(self):
        t = self.tests[CLOSURE_ADDITIVE[2]]
        st, detail = shape_q.run_test(FakeConn(rows=[[0, 0, 0]]), self.client(t, "4", [("raw", "4")]), self.cfg, {}, t)
        self.assertEqual((st, detail["null_group_equal"]), ("pass", True))
        t = self.fresh(CLOSURE_ADDITIVE[2])
        st, detail = shape_q.run_test(FakeConn(rows=[[0, 0, 0]]), self.client(t, "4", [("raw", "3"), (None, "1")]),
                                      self.cfg, {}, t)
        self.assertEqual((st, detail["null_group_equal"]), ("fail", False))

    def test_the_null_group_sql_runs_once_per_test(self):
        t = self.tests[CLOSURE_ADDITIVE[0]]
        conn = FakeConn(rows=[[Decimal("2"), 1, 0]])
        client = self.client(t, "7", [("raw", "5"), (None, "2")])
        for _ in range(3):
            self.assertEqual(shape_q.run_test(conn, client, self.cfg, {}, t)[0], "pass")
        self.assertEqual(sum(1 for s in conn.statements() if "FILTER" in s), 1)
        self.assertNotIn("sql_null_group", json.dumps(shape_q.run_test(conn, client, self.cfg, {}, t)[1]))

    def test_errors_are_reported_by_type(self):
        t = self.tests[CLOSURE_ADDITIVE[0]]
        st, detail = shape_q.run_test(FakeConn(), FakeClient(error=CubeError("HTTP 400: x")), self.cfg, {}, t)
        self.assertEqual((st, detail), ("error", {"error": "CubeError", "http_status": 400}))
        st, detail = shape_q.run_test(FakeConn(rows=[]), self.client(t, "7", [("raw", "7")]), self.cfg, {}, t)
        self.assertEqual((st, detail["error"]), ("error", "ValueError"))


class StageNotesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.out = Path(tempfile.mkdtemp(prefix="sxc_t_additive_unit_"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_verify_lists_not_applicable_paths_when_v3_runs(self):
        model = demo_model(self.cfg)
        set_relationship(model, "part", "m_on_hand", "many_to_one")
        cube_gen.write_model(model, self.out / "cube" / "model")
        for stage, cid in (("cube", "cube:model"), ("build", "closure:has_component")):
            r = Result(stage)
            r.add(cid, "pass", "")
            r.write(self.out)
        serve = mock.patch.object(stage_verify.cube_run, "serve",
                                  return_value=contextlib.nullcontext(FakeClient(error=CubeError("HTTP 400: x"))))
        with mock.patch.object(stage_verify.db, "connect", return_value=FakeConn()), serve:
            res = stage_verify.run(self.cfg, argparse.Namespace(only=["v3"], gold=[]), self.out)
            self.assertFalse([line for line in res.not_proven if "not applicable" in line] == [])
            ids = [c.id for c in res.checks]
            self.assertNotIn("v3:additive:m_on_hand.value:part.part_class", ids)
            self.assertIn(f"v3:{CLOSURE_ADDITIVE[0]}", ids)
            res = stage_verify.run(self.cfg, argparse.Namespace(only=["v1"], gold=[]), self.out)
            self.assertEqual([line for line in res.not_proven if "not applicable" in line], [],
                             "V3 did not run, so it lists nothing")


if __name__ == "__main__":
    unittest.main()
