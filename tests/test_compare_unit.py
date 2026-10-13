"""Unit tests for the verify stage's comparison, sampling and query-shape logic: no database, no Cube.

They pin the strict comparison rules of V1/V2 (canonical keys, duplicate keys, NULL against '', the single
declared zero_if_null equivalence, decimals and dates), placeholder substitution, the binding rules of design
section 7 (through a fake connection that records SQL and parameters), the V3 test catalogue derived from a
model generated against a fake catalog, and the verify stage's gating and mutation-suite contract.
"""

import argparse
import datetime as dt
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import compare, config, cube_gen, shape_q, stage_verify, sweep
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import EXIT_CONFIG, Result, StageError

try:
    from pg8000.exceptions import DatabaseError
except ImportError:  # pragma: no cover - pg8000 is a core dependency
    DatabaseError = None

SECRET = "SECRET-ROW-VALUE"
DEMO_COLUMNS = {
    "sxc_demo.part": [("part_id", "text"), ("part_class", "text"), ("unit", "text"), ("procure", "text"), ("name", "text")],
    "sxc_demo.bom_line": [("parent_id", "text"), ("child_id", "text"), ("line_no", "integer"), ("qty_per", "numeric")],
    "sxc_demo.sales_order": [("order_id", "text"), ("part_id", "text"), ("status", "text"), ("qty_total", "numeric"),
                             ("qty_open", "numeric"), ("due_date", "date")],
    "sxc_demo.stock": [("part_id", "text"), ("location_id", "text"), ("qty", "numeric")],
}


def demo_cfg():
    return config.load(pgfixture.DEMO_CONFIG)


def gold_of(cfg, gid: str):
    return next(g for g in cfg.gold if g.id == gid)


def plain_gold(key=("k",), cols=("k", "v"), zero=()) -> config.Gold:
    return config.Gold(id="g", sql="unused.sql", key=list(key), map={c: f"c.{c}" for c in cols}, zero_if_null=list(zero))


class CatalogConn:
    """Answers the two catalog queries of cube_gen.generate for the demo tables."""

    def __init__(self, closure: bool = True):
        self.closure = closure

    def run(self, sql, **params):
        if "information_schema.columns" in sql:
            return [list(c) for c in DEMO_COLUMNS[f"{params['s']}.{params['t']}"]]
        if "to_regclass" in sql:
            return [[self.closure]]
        raise AssertionError("unexpected SQL")


def demo_model(cfg, closure: bool = True) -> dict:
    model, _notes = cube_gen.generate(cfg, CatalogConn(closure))
    return model


class FakePs:
    def __init__(self, conn):
        self.conn, self.columns = conn, None

    def run(self, **params):
        self.conn.calls.append(("execute", params))
        if self.conn.error is not None:
            raise self.conn.error
        self.columns = [{"name": c} for c in self.conn.cols]
        return [list(r) for r in self.conn.data]

    def close(self):
        self.conn.calls.append(("close", None))


class FakeConn:
    """Records statements; transaction control returns nothing, to_regclass answers `closure`, others `rows`."""

    def __init__(self, cols=(), data=(), error=None, rows=None, closure=True, scalar=None):
        self.cols, self.data, self.error = list(cols), list(data), error
        self.rows, self.closure, self.scalar = rows or [], closure, scalar
        self.calls: list = []

    def run(self, sql, **params):
        self.calls.append((sql, params))
        s = sql.strip()
        if s.startswith(("BEGIN", "SET LOCAL", "ROLLBACK")):
            return None
        if "to_regclass" in s:
            return [[self.closure]]
        if self.scalar is not None:
            return [[self.scalar]]
        return self.rows

    def prepare(self, sql):
        self.calls.append(("prepare", sql))
        return FakePs(self)

    def close(self):
        self.calls.append(("conn_close", None))

    def statements(self) -> list[str]:
        return [c[0] for c in self.calls if isinstance(c[0], str) and c[0] not in ("prepare", "execute", "close", "conn_close")]


class FakeClient:
    def __init__(self, data=None, error=None, answers=None):
        self.data, self.error, self.answers = data or [], error, answers or {}
        self.queries: list = []

    def load(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if self.answers:
            return self.answers[(tuple(query.get("dimensions", [])), tuple(query.get("measures", [])))]
        return self.data


class SubstTest(unittest.TestCase):
    def test_replaces_exact_placeholders_recursively(self):
        q = {"filters": [{"member": "c.k", "operator": "equals", "values": [":root"]}],
             "timeDimensions": [{"dimension": "c.ts", "dateRange": [":date_from", ":date_to"]}], "limit": 5}
        out = compare.subst(q, {"root": 7, "date_from": "2026-01-01", "date_to": "2026-03-31"})
        self.assertEqual(out["filters"][0]["values"], [7])
        self.assertEqual(out["timeDimensions"][0]["dateRange"], ["2026-01-01", "2026-03-31"])
        self.assertEqual(out["limit"], 5)
        self.assertEqual(q["filters"][0]["values"], [":root"], "the configured query must not be modified")

    def test_other_strings_stay(self):
        q = ["x:root", ":root ", "::root", ":Root", "c.k", ":", ""]
        self.assertEqual(compare.subst(q, {"root": "v"}), q)

    def test_unbound_placeholder_is_an_error(self):
        with self.assertRaises(compare.UnboundParameter):
            compare.subst({"values": [":part"]}, {"root": "v"})

    def test_cube_query_turns_decimals_and_dates_into_text(self):
        g = config.Gold(id="g", sql="x.sql", key=["k"], map={"k": "c.k"},
                        cube_query={"filters": [{"member": "c.k", "operator": "equals", "values": [":a", ":b", ":c"]}]})
        q = compare.cube_query(g, {"a": Decimal("2.50"), "b": dt.date(2026, 1, 5), "c": True})
        self.assertEqual(q["filters"][0]["values"], ["2.5", "2026-01-05", True])
        json.dumps(q)


class CompareRowsTest(unittest.TestCase):
    def test_identical_rows_pass(self):
        r = compare.compare_rows(plain_gold(), [{"k": "a", "v": 1}, {"k": "b", "v": 2}], [{"k": "b", "v": "2"}, {"k": "a", "v": "1"}])
        self.assertTrue(r["pass"])
        self.assertEqual((r["gold_rows"], r["cube_rows"], r["matched_rows"]), (2, 2, 2))
        self.assertFalse(r["empty"])

    def test_duplicate_keys_fail_on_either_side(self):
        g = plain_gold()
        r = compare.compare_rows(g, [{"k": "a", "v": 1}, {"k": "a", "v": 1}], [{"k": "a", "v": 1}])
        self.assertFalse(r["pass"])
        self.assertEqual((r["duplicate_keys_gold"], r["duplicate_keys_cube"]), (1, 0))
        r = compare.compare_rows(g, [{"k": "a", "v": 1}], [{"k": "a", "v": 1}, {"k": "a", "v": "1.0"}])
        self.assertFalse(r["pass"])
        self.assertEqual(r["duplicate_keys_cube"], 1)

    def test_canonically_equal_keys_are_duplicates(self):
        r = compare.compare_rows(plain_gold(), [{"k": 1, "v": 1}], [{"k": "1", "v": 1}, {"k": "1.00", "v": 1}])
        self.assertEqual(r["duplicate_keys_cube"], 1)
        self.assertFalse(r["pass"])

    def test_null_differs_from_empty_text(self):
        r = compare.compare_rows(plain_gold(), [{"k": "a", "v": None}], [{"k": "a", "v": ""}])
        self.assertFalse(r["pass"])
        self.assertEqual(r["mismatched_columns"], {"v": 1})

    def test_zero_if_null_turns_only_a_cube_null_into_zero(self):
        g = plain_gold(cols=("k", "v", "w"), zero=("v",))
        r = compare.compare_rows(g, [{"k": "a", "v": 0, "w": None}], [{"k": "a", "v": None, "w": None}])
        self.assertTrue(r["pass"])
        self.assertEqual(r["declared_null_as_zero"], 1)
        r = compare.compare_rows(g, [{"k": "a", "v": 0, "w": 0}], [{"k": "a", "v": 0, "w": None}])
        self.assertEqual(r["mismatched_columns"], {"w": 1}, "w is not declared")
        r = compare.compare_rows(g, [{"k": "a", "v": None, "w": None}], [{"k": "a", "v": None, "w": None}])
        self.assertEqual(r["mismatched_columns"], {"v": 1}, "the gold SQL must coalesce a zero_if_null column itself")
        r = compare.compare_rows(g, [{"k": "a", "v": 0, "w": None}], [{"k": "a", "v": "", "w": None}])
        self.assertEqual(r["mismatched_columns"], {"v": 1}, "'' is not NULL")
        self.assertEqual(r["declared_null_as_zero"], 0)

    def test_decimals_compare_canonically_without_tolerance(self):
        g = plain_gold()
        for gv, cv in (("2.50", "2.5"), (Decimal("2.500000"), 2.5), (1, "1.000"), (Decimal("-0.0"), "0")):
            self.assertTrue(compare.compare_rows(g, [{"k": "a", "v": gv}], [{"k": "a", "v": cv}])["pass"], (gv, cv))
        for gv, cv in (("2.5", "2.51"), (Decimal("0.1"), "0.10000001")):
            self.assertFalse(compare.compare_rows(g, [{"k": "a", "v": gv}], [{"k": "a", "v": cv}])["pass"], (gv, cv))

    def test_leading_zero_text_is_not_a_number(self):
        self.assertFalse(compare.compare_rows(plain_gold(), [{"k": "a", "v": "01"}], [{"k": "a", "v": "1"}])["pass"])

    def test_dates_compare_as_iso_text(self):
        g = plain_gold()
        self.assertTrue(compare.compare_rows(g, [{"k": "a", "v": dt.date(2026, 1, 5)}],
                                             [{"k": "a", "v": "2026-01-05T00:00:00.000"}])["pass"])
        self.assertFalse(compare.compare_rows(g, [{"k": "a", "v": dt.date(2026, 1, 5)}],
                                              [{"k": "a", "v": "2026-01-05T00:00:01.000"}])["pass"])
        self.assertFalse(compare.compare_rows(g, [{"k": "a", "v": dt.date(2026, 1, 5)}], [{"k": "a", "v": "2026-01-06"}])["pass"])
        self.assertTrue(compare.compare_rows(g, [{"k": dt.date(2026, 1, 5), "v": 1}],
                                             [{"k": "2026-01-05T00:00:00.000", "v": 1}])["pass"])

    def test_both_sides_empty_is_a_flagged_pass(self):
        r = compare.compare_rows(plain_gold(), [], [])
        self.assertTrue(r["pass"])
        self.assertTrue(r["empty"])
        r = compare.compare_rows(plain_gold(), [], [{"k": "a", "v": 1}])
        self.assertFalse(r["pass"])
        self.assertFalse(r["empty"])
        self.assertEqual(r["extra_in_cube"], 1)

    def test_missing_and_extra_keys(self):
        r = compare.compare_rows(plain_gold(), [{"k": "a", "v": 1}, {"k": "b", "v": 1}], [{"k": "b", "v": 1}, {"k": "c", "v": 1}])
        self.assertEqual((r["missing_in_cube"], r["extra_in_cube"], r["matched_rows"]), (1, 1, 1))
        self.assertFalse(r["pass"])

    def test_composite_keys_match_canonically(self):
        g = plain_gold(key=("k", "n"), cols=("k", "n", "v"))
        r = compare.compare_rows(g, [{"k": "a", "n": 5, "v": Decimal("1.50")}], [{"k": "a", "n": "5", "v": "1.5"}])
        self.assertTrue(r["pass"])

    def test_gold_without_a_mapped_column_fails(self):
        r = compare.compare_rows(plain_gold(cols=("k", "v", "w")), [{"k": "a", "v": 1}], [{"k": "a", "v": 1, "w": None}])
        self.assertFalse(r["pass"])
        self.assertEqual(r["gold_columns_missing"], ["w"])

    def test_result_holds_no_row_values(self):
        r = compare.compare_rows(plain_gold(), [{"k": SECRET, "v": SECRET}, {"k": SECRET, "v": 1}], [{"k": SECRET, "v": SECRET + "x"}])
        self.assertNotIn(SECRET, json.dumps(r))


@unittest.skipIf(DatabaseError is None, "pg8000 is not installed")
class RunBindingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.gold = gold_of(self.cfg, "q04_depth_histogram")

    def test_gold_rows_use_one_prepared_statement_in_a_read_only_transaction(self):
        conn = FakeConn(cols=["max_depth", "pairs"], data=[(1, 3)])
        rows = compare.gold_rows(conn, self.cfg, self.gold, {})
        self.assertEqual(rows, [{"max_depth": 1, "pairs": 3}])
        stmts = conn.statements()
        self.assertEqual(stmts[0], "BEGIN TRANSACTION READ ONLY")
        self.assertIn(f"statement_timeout = {self.cfg.source.statement_timeout_ms}", stmts[1])
        self.assertEqual(stmts[-1], "ROLLBACK")
        kinds = [c[0] for c in conn.calls if c[0] in ("prepare", "execute", "close")]
        self.assertEqual(kinds, ["prepare", "execute", "close"])

    def test_database_error_keeps_type_and_sqlstate_only(self):
        err = DatabaseError({"S": "ERROR", "C": "42P01", "M": f"relation {SECRET} does not exist"})
        conn = FakeConn(error=err)
        r = compare.run_binding(conn, FakeClient(), self.cfg, self.gold, {})
        self.assertEqual((r["pass"], r["side"], r["error"], r["sqlstate"]), (False, "gold", "DatabaseError", "42P01"))
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertEqual(conn.statements()[-1], "ROLLBACK")
        self.assertIn(("close", None), conn.calls, "the prepared statement is closed after an error")
        self.assertEqual(compare.status(r), "error")

    def test_cube_error_keeps_http_status_only(self):
        conn = FakeConn(cols=["max_depth", "pairs"], data=[(1, 3)])
        r = compare.run_binding(conn, FakeClient(error=CubeError(f"HTTP 500: Error: bad value {SECRET}")), self.cfg, self.gold, {})
        self.assertEqual((r["side"], r["error"], r["http_status"], r["gold_rows"]), ("cube", "CubeError", 500, 1))
        self.assertNotIn(SECRET, json.dumps(r))

    def test_error_detail_classifies_client_errors(self):
        self.assertEqual(compare.error_detail(CubeError("timed out waiting for Cube"))["cube"], "timeout")
        self.assertEqual(compare.error_detail(CubeError("result reached the limit (50000); raise"))["cube"], "row_limit")
        self.assertEqual(compare.error_detail(CubeError(f"Error: {SECRET}"))["cube"], "query_error")
        self.assertEqual(compare.error_detail(TimeoutError("x")), {"error": "TimeoutError"})

    def test_cache_reuses_gold_rows(self):
        conn = FakeConn(cols=["max_depth", "pairs"], data=[(1, 3)])
        client = FakeClient(data=[{"has_component_closure.max_depth": 1, "has_component_closure.pairs": "3"}])
        cache: dict = {}
        for _ in range(3):
            self.assertTrue(compare.run_binding(conn, client, self.cfg, self.gold, {}, cache=cache)["pass"])
        self.assertEqual(sum(1 for c in conn.calls if c[0] == "execute"), 1)
        self.assertEqual(len(client.queries), 3)

    def test_reserved_parameter_names_are_refused(self):
        r = compare.run_binding(FakeConn(), FakeClient(), self.cfg, self.gold, {"stream": "x"})
        self.assertEqual((r["side"], r["error"]), ("gold", "ValueError"))

    def test_cube_rows_are_renamed_through_the_map(self):
        g = gold_of(self.cfg, "q01_explosion")
        client = FakeClient(data=[{m: None for m in g.map.values()} | {"has_component_closure.descendant": "x"}])
        rows = compare.cube_rows(client, g, {"root": "r"})
        self.assertEqual(set(rows[0]), set(g.map))
        self.assertEqual(rows[0]["part_id"], "x")
        self.assertEqual(client.queries[0]["filters"][0]["values"], ["r"])


class BindingsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()

    def test_no_parameters_is_one_binding_named_all(self):
        conn = FakeConn()
        self.assertEqual(sweep.bindings(conn, self.cfg, gold_of(self.cfg, "q04_depth_histogram")), [("all", {})])
        self.assertEqual(conn.calls, [])

    def test_param_sets_keep_the_configured_order(self):
        g = gold_of(self.cfg, "q05_firm_window")
        b = sweep.bindings(FakeConn(), self.cfg, g)
        self.assertEqual([s for s, _p in b], ["param_sets"] * len(g.param_sets))
        self.assertEqual([p for _s, p in b], g.param_sets)

    def test_explicit_values(self):
        g = config.Gold(id="gv", sql="x.sql", key=["k"], map={"k": "c.k"}, params={"p": config.Param(name="p", values=[1, "a"])})
        conn = FakeConn()
        self.assertEqual(sweep.bindings(conn, self.cfg, g), [("values", {"p": 1}), ("values", {"p": "a"})])
        self.assertEqual(conn.calls, [])

    def test_values_of_reads_every_distinct_value_read_only(self):
        conn = FakeConn(rows=[["assembly"], ["raw"]])
        b = sweep.bindings(conn, self.cfg, gold_of(self.cfg, "q03_stock"))
        self.assertEqual(b, [("values", {"part_class": "assembly"}), ("values", {"part_class": "raw"})])
        stmts = conn.statements()
        self.assertEqual((stmts[0], stmts[-1]), ("BEGIN TRANSACTION READ ONLY", "ROLLBACK"))
        query = stmts[2]
        for part in ('SELECT DISTINCT "part_class"', '"sxc_demo"."part"', 'IS NOT NULL', "ORDER BY 1"):
            self.assertIn(part, query)

    def test_values_of_without_values_is_an_empty_stratum(self):
        self.assertEqual(sweep.bindings(FakeConn(rows=[]), self.cfg, gold_of(self.cfg, "q03_stock")), [("values", None)])

    def test_strata_sample_k_ids_per_stratum_in_one_statement(self):
        g = gold_of(self.cfg, "q01_explosion")      # strata top, mid, deep, leaf_parent, empty
        conn = FakeConn(rows=[(0, "A"), (0, "B"), (2, "C")])
        b = sweep.bindings(conn, self.cfg, g)
        self.assertEqual(b, [("top", {"root": "A"}), ("top", {"root": "B"}), ("mid", None), ("deep", {"root": "C"}),
                             ("leaf_parent", None), ("empty", None)])
        sql, params = next(c for c in conn.calls if isinstance(c[0], str) and "row_number" in c[0])
        self.assertEqual((params["k"], params["seed"]), (8, "7"))      # ceil(40 / 5); the seed is bound as text
        self.assertIn('md5(f.id::text || CAST(:seed AS text)) COLLATE "C", f.id', sql)
        self.assertIn("rn <= :k", sql)

    def test_deep_uses_the_closure_table_when_present_and_a_bounded_recursion_otherwise(self):
        g = gold_of(self.cfg, "q01_explosion")
        h = self.cfg.hierarchy()
        with_closure = sweep.strata_sql(self.cfg, h, ["deep"], True)
        self.assertIn("sxc_out.has_component_closure", with_closure)
        self.assertTrue(with_closure.startswith("WITH "))
        self.assertIn("d.height DESC NULLS LAST", with_closure)
        without = sweep.strata_sql(self.cfg, h, ["deep"], False)
        self.assertTrue(without.startswith("WITH RECURSIVE "))
        self.assertIn("up.height < :max_depth", without)
        self.assertNotIn("has_component_closure", without)
        conn = FakeConn(rows=[], closure=False)
        sweep.bindings(conn, self.cfg, g)
        _sql, params = next(c for c in conn.calls if isinstance(c[0], str) and "row_number" in c[0])
        self.assertEqual(params["max_depth"], h.max_depth)

    def test_strata_without_deep_need_no_depth(self):
        sql = sweep.strata_sql(self.cfg, self.cfg.hierarchy(), ["top", "shared"], True)
        self.assertNotIn("height", sql)
        self.assertNotIn("RECURSIVE", sql)

    def test_k_is_rounded_up_and_duplicate_strata_are_sampled_once(self):
        g = gold_of(self.cfg, "q01_explosion")
        self.cfg.verify.sweep_k = 41
        self.assertEqual(sweep.per_stratum(self.cfg, g), 9)
        g.params["root"].strata = ["top", "top", "leaf"]
        self.assertEqual(sweep.per_stratum(self.cfg, g), 21)
        self.assertEqual(sweep.strata_names(g), ["top", "leaf"])

    def test_strata_names_without_database(self):
        self.assertEqual(sweep.strata_names(gold_of(self.cfg, "q01_explosion")), ["top", "mid", "deep", "leaf_parent", "empty"])
        self.assertEqual(sweep.strata_names(gold_of(self.cfg, "q03_stock")), ["values"])
        self.assertEqual(sweep.strata_names(gold_of(self.cfg, "q04_depth_histogram")), ["all"])
        self.assertEqual(sweep.strata_names(gold_of(self.cfg, "q05_firm_window")), ["param_sets"])

    def test_every_configured_stratum_has_a_predicate(self):
        self.assertEqual(set(sweep.PREDICATES), set(config.STRATA))


class ShapeCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()

    def test_demo_model_tests(self):
        ids = [t["id"] for t in shape_q.tests(self.cfg, demo_model(self.cfg))]
        self.assertEqual(ids, [
            "total:m_firm_open.value", "total:m_firm_open_dated.value", "total:m_on_hand.value", "total:m_consigned.value",
            "total:has_component_closure.qty", "total:has_component_closure.pairs",
            "additive:m_firm_open.value:part.part_class", "additive:m_on_hand.value:part.part_class",
            "additive:m_consigned.value:part.part_class",
            "pair:has_component_closure.qty+m_firm_open.value",
            "pair:m_firm_open.value+m_on_hand.value", "pair:m_firm_open.value+m_consigned.value",
            "pair:m_on_hand.value+m_consigned.value",
        ])

    def test_every_queried_member_is_listed_and_exists(self):
        model = demo_model(self.cfg)
        known = cube_gen.members(model)
        for t in shape_q.tests(self.cfg, model):
            queried = set(t["members"])
            if t["kind"] == "pair":
                self.assertTrue({t["group_by"], *t["alone"].values(), *t["measures"]} <= queried, t["id"])
            self.assertTrue(queried <= known, t["id"])

    def test_closure_pair_groups_by_the_joined_end_and_reads_the_measure_by_its_own_key(self):
        t = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"].startswith("pair:has_component_closure"))
        self.assertEqual(t["group_by"], "has_component_closure.ancestor")
        self.assertEqual(t["alone"], {"has_component_closure.qty": "has_component_closure.ancestor",
                                      "m_firm_open.value": "m_firm_open.key"})

    def test_without_closure_table_there_are_no_closure_tests(self):
        ids = [t["id"] for t in shape_q.tests(self.cfg, demo_model(self.cfg, closure=False))]
        self.assertFalse([i for i in ids if "closure" in i])
        self.assertIn("pair:m_firm_open.value+m_on_hand.value", ids)

    def test_without_class_column_there_are_no_additive_tests(self):
        self.cfg.ontology.class_column = ""
        ids = [t["id"] for t in shape_q.tests(self.cfg, demo_model(self.cfg))]
        self.assertFalse([i for i in ids if i.startswith("additive:")])

    def test_total_compares_cube_with_sql_from_the_configuration(self):
        t = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"] == "total:m_on_hand.value")
        conn = FakeConn(scalar=Decimal("10.500"))
        st, detail = shape_q.run_test(conn, FakeClient(data=[{"m_on_hand.value": "10.5"}]), self.cfg, {}, t)
        self.assertEqual((st, detail), ("pass", {"equal": True}))
        sql = next(s for s in conn.statements() if s.startswith("SELECT sum("))
        self.assertIn("upper(btrim(t.\"location_id\")) IN ('MAIN', 'SUB')", sql)
        st, _ = shape_q.run_test(FakeConn(scalar=Decimal("10.5")), FakeClient(data=[{"m_on_hand.value": "10.6"}]), self.cfg, {}, t)
        self.assertEqual(st, "fail")

    def test_count_total_of_nothing_is_zero(self):
        self.cfg.measures["on_hand"].agg = "count"
        t = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"] == "total:m_on_hand.value")
        st, detail = shape_q.run_test(FakeConn(scalar=0), FakeClient(data=[{"m_on_hand.value": None}]), self.cfg, {}, t)
        self.assertEqual((st, detail), ("pass", {"equal": True, "null_as_zero": True}))

    def test_additive_groups_must_add_up_to_the_total(self):
        t = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["kind"] == "additive")
        measure, dim = t["measure"], t["dimension"]
        groups = [{dim: "assembly", measure: "1.5"}, {dim: "raw", measure: None}, {dim: None, measure: "2"}]
        ok = FakeClient(answers={((), (measure,)): [{measure: "3.50"}], ((dim,), (measure,)): groups})
        self.assertEqual(shape_q.run_test(FakeConn(), ok, self.cfg, {}, t),
                         ("pass", {"equal": True, "groups": 3, "null_class_groups": 1}))
        lost = FakeClient(answers={((), (measure,)): [{measure: "4"}], ((dim,), (measure,)): groups})
        self.assertEqual(shape_q.run_test(FakeConn(), lost, self.cfg, {}, t)[0], "fail")

    def test_pair_exposes_fan_out_per_key(self):
        t = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"] == "pair:m_firm_open.value+m_on_hand.value")
        a, b = t["measures"]
        together = [{"part.part_id": "p1", a: "4", b: "1"}, {"part.part_id": "p2", a: None, b: "2"}]
        alone_a = [{"m_firm_open.key": "p1", a: "2"}, {"m_firm_open.key": "orphan", a: "9"}]
        alone_b = [{"m_on_hand.key": "p1", b: "1.0"}, {"m_on_hand.key": "p2", b: "2"}]
        client = FakeClient(answers={(("part.part_id",), (a, b)): together, (("m_firm_open.key",), (a,)): alone_a,
                                     (("m_on_hand.key",), (b,)): alone_b})
        st, detail = shape_q.run_test(FakeConn(), client, self.cfg, {}, t)
        self.assertEqual(st, "fail")
        self.assertEqual(detail["mismatched"], {a: 1})
        self.assertEqual(detail["alone_only_keys"], {a: 1, b: 0})
        together[0][a] = "2.00"
        self.assertEqual(shape_q.run_test(FakeConn(), client, self.cfg, {}, t)[0], "pass")

    def test_cube_errors_become_error_status_without_messages(self):
        t = shape_q.tests(self.cfg, demo_model(self.cfg))[0]
        st, detail = shape_q.run_test(FakeConn(), FakeClient(error=CubeError(f"HTTP 400: {SECRET}")), self.cfg, {}, t)
        self.assertEqual((st, detail), ("error", {"error": "CubeError", "http_status": 400}))


class StageVerifyUnitTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.out = Path(tempfile.mkdtemp(prefix="sxc_t_verify_unit_"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def write_stages(self, closure_status: str | None):
        model = demo_model(self.cfg)
        cube_gen.write_model(model, self.out / "cube" / "model")
        cube = Result("cube")
        cube.add("cube:model", "pass", "")
        cube.write(self.out)
        if closure_status:
            build = Result("build")
            build.add("closure:has_component", closure_status, "hierarchies.has_component")
            build.write(self.out)
        return model

    def test_usage_errors_are_configuration_errors(self):
        for args in (argparse.Namespace(only=["v9"], gold=[]), argparse.Namespace(only=["v1"], gold=["nope"])):
            with self.assertRaises(StageError) as e:
                stage_verify.run(self.cfg, args, self.out)
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)

    def test_missing_cube_stage_is_a_configuration_error(self):
        with self.assertRaises(StageError) as e:
            stage_verify.run(self.cfg, argparse.Namespace(only=["v1"], gold=[]), self.out)
        self.assertEqual(e.exception.exit_code, EXIT_CONFIG)

    def test_gold_members_include_filters_and_time_dimensions(self):
        self.assertIn("has_component_closure.ancestor", stage_verify._members(gold_of(self.cfg, "q01_explosion")))
        self.assertIn("m_firm_open_dated.ts", stage_verify._members(gold_of(self.cfg, "q05_firm_window")))

    def test_closure_gate_follows_the_build_result(self):
        model = self.write_stages(None)
        v = stage_verify._Verify(self.cfg, self.out, model, ["v1"])
        self.assertEqual(v.gate(stage_verify._members(gold_of(self.cfg, "q01_explosion"))), ("has_component", "missing"))
        self.assertIsNone(v.gate(stage_verify._members(gold_of(self.cfg, "q03_stock"))))
        self.write_stages("fail")
        v = stage_verify._Verify(self.cfg, self.out, model, ["v1"])
        self.assertEqual(v.gate(["has_component_descendant.part_class"]), ("has_component", "fail"))
        self.write_stages("pass")
        v = stage_verify._Verify(self.cfg, self.out, model, ["v1"])
        self.assertIsNone(v.gate(stage_verify._members(gold_of(self.cfg, "q02_where_used"))))

    def test_gated_gold_is_not_run_and_cube_is_not_started(self):
        self.write_stages("fail")
        conn = FakeConn()
        with mock.patch.object(stage_verify.db, "connect", return_value=conn), \
                mock.patch.object(stage_verify.cube_run, "serve", side_effect=AssertionError("Cube must not start")):
            res = stage_verify.run(self.cfg, argparse.Namespace(only=["v1", "v2"], gold=["q01_explosion"]), self.out)
        ids = [(c.id, c.status) for c in res.checks]
        gated = [("v1:q01_explosion", "not_run")] + [(f"v2:q01_explosion:{s}", "not_run")
                                                     for s in ("top", "mid", "deep", "leaf_parent", "empty")]
        scope = [("scope:v3", "not_run"), ("scope:v4", "not_run")] + [
            (f"scope:gold:{g}", "not_run") for g in ("q02_where_used", "q03_stock", "q04_depth_histogram", "q05_firm_window")]
        self.assertEqual(ids, gated + scope)
        self.assertTrue(all(c.detail["reason"] == "closure_not_passed" and c.config_ref == "hierarchies.has_component"
                            for c in res.checks[:len(gated)]))
        self.assertEqual(len(res.not_proven), 6)
        self.assertEqual(res.status, "not_run")
        self.assertIn("did not run", res.next_safe_action)
        self.assertIn(("conn_close", None), conn.calls)

    def test_sweep_aggregate(self):
        agg = stage_verify._Sweep()
        agg.add(0, {"pass": True, "gold_rows": 3, "empty": False, "declared_null_as_zero": 2, "mismatched_columns": {}})
        agg.add(1, {"pass": True, "gold_rows": 0, "empty": True, "mismatched_columns": {}})
        self.assertEqual(agg.status(), "pass")
        agg.add(2, {"pass": False, "gold_rows": 4, "missing_in_cube": 1, "mismatched_columns": {"qty": 2}})
        self.assertEqual(agg.status(), "fail")
        agg.add(3, {"pass": False, "side": "gold", "error": "DatabaseError", "sqlstate": "57014"})
        d = agg.detail()
        self.assertEqual(agg.status(), "error")
        self.assertEqual((d["bindings"], d["failed_bindings"], d["error_bindings"], d["empty_bindings"], d["rows_compared"]),
                         (4, 2, 1, 1, 7))
        self.assertEqual((d["failed_indexes"], d["mismatched_columns"], d["errors"], d["sqlstates"]),
                         ([2, 3], {"qty": 2}, {"DatabaseError": 1}, {"57014": 1}))
        self.assertEqual((d["declared_null_as_zero"], d["missing_in_cube"]), (2, 1))

    def test_mutation_suite_replays_only_items_that_pass_on_the_unmutated_model(self):
        model = self.write_stages("pass")
        v = stage_verify._Verify(self.cfg, self.out, model, ["v4"])
        v.items = [("v1:a", "v1:a", lambda client: "fail" if client == "mutant" else "pass"),
                   ("v2:b:top[0]", "v2:b:top", lambda client: "fail"),
                   ("v2:b:top[1]", "v2:b:top", lambda client: "error" if client == "broken" else "pass"),
                   ("v3:total:x", "v3:total:x", lambda client: "pass")]
        v.baseline = {"v1:a": "pass", "v2:b:top[0]": "fail", "v2:b:top[1]": "pass", "v3:total:x": "pass"}
        seen = {}

        def run_mutants(cfg, out, conn, model_arg, suite, closure=None):
            seen["model"], seen["closure"] = model_arg is model, closure
            seen["original"] = suite("original")
            seen["mutant"] = suite("mutant")
            seen["broken"] = suite("broken")
            return [("v4:op:t", "pass", "measures.t", {"witness": 1})], ["v4:op2:t equivalent on this data"]

        with mock.patch.object(stage_verify.mutate, "run_mutants", run_mutants):
            v.v4(FakeConn())
        self.assertTrue(seen["model"])
        self.assertEqual(seen["closure"], {"has_component": "pass"})
        self.assertEqual(seen["original"], ("pass", ""))
        self.assertEqual(seen["mutant"], ("fail", "v1:a"))
        self.assertEqual(seen["broken"], ("error", "v2:b:top[1]"), "an item that errors is not a kill")
        self.assertEqual([(c.id, c.status, c.config_ref, c.detail) for c in v.res.checks],
                         [("v4:op:t", "pass", "measures.t", {"witness": 1})])
        self.assertEqual(len(v.res.not_proven), 2)
        self.assertIn("1 suite item(s)", v.res.not_proven[0])

    def test_mutation_suite_without_passing_items_is_not_run(self):
        model = self.write_stages("pass")
        v = stage_verify._Verify(self.cfg, self.out, model, ["v4"])
        v.items = [("v1:a", "v1:a", lambda client: "fail")]
        v.baseline = {"v1:a": "fail"}
        v.v4(FakeConn())
        self.assertEqual([(c.id, c.status) for c in v.res.checks], [("v4:suite", "not_run")])


if __name__ == "__main__":
    unittest.main()
