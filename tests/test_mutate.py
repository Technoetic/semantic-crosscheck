"""V4 mutation tests (mutate.py).

Unit tests run cube_gen.generate against a catalog-only fake connection, so the model has exactly the shape
of the real one (the integration test proves that), and check that every operator changes one element of
a copy, that only a failing suite item kills a mutant and that the closure operators follow build's closure check.
Integration tests load the demo into PostgreSQL 17, compute witnesses against the independent demo reference
numbers and, with the Cube adapter installed, serve every observable mutant to a small gold suite.
"""

import contextlib
import copy
import dataclasses
import os
import re
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pgfixture

from semantic_crosscheck import canon, closure, config, cube_gen, cube_run, db, ident, mutate, naming
from semantic_crosscheck.config import Param
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import Result, StageError

# information_schema view of demo.DDL
DEMO_COLUMNS = {
    "sxc_demo.part": [("part_id", "text"), ("part_class", "text"), ("unit", "text"), ("procure", "text"), ("name", "text")],
    "sxc_demo.bom_line": [("parent_id", "text"), ("child_id", "text"), ("line_no", "integer"), ("qty_per", "numeric")],
    "sxc_demo.sales_order": [("order_id", "text"), ("part_id", "text"), ("status", "text"), ("qty_total", "numeric"),
                             ("qty_open", "numeric"), ("due_date", "date")],
    "sxc_demo.stock": [("part_id", "text"), ("location_id", "text"), ("qty", "numeric")],
}
CLOSURE, DESC = "has_component_closure", "has_component_descendant"
TS = "m_firm_open_dated.ts"

# (operator, target) in catalogue and configuration order -> (changed path, new value or None for SQL rewrites)
EXPECTED = {
    ("closure_depth_shift", "has_component"): (("cubes", CLOSURE, "dimensions", "min_depth", "sql"), "({CUBE}.min_depth) + 1"),
    ("closure_path_count_one", "has_component"): (("cubes", CLOSURE, "dimensions", "path_count", "sql"), "1"),
    ("closure_qty_single_path", "has_component"): (("cubes", CLOSURE, "measures", "qty", "sql"), "({CUBE}.qty) / ({CUBE}.path_count)"),
    ("measure_filter_drop", "firm_open"): (("cubes", "m_firm_open", "sql"), None),
    ("measure_filter_drop", "firm_open_dated"): (("cubes", "m_firm_open_dated", "sql"), None),
    ("measure_normalization_drop", "on_hand"): (("cubes", "m_on_hand", "sql"), None),
    ("measure_normalization_drop", "consigned"): (("cubes", "m_consigned", "sql"), None),
    ("measure_agg_to_max", "firm_open"): (("cubes", "m_firm_open", "measures", "value", "type"), "max"),
    ("measure_agg_to_max", "firm_open_dated"): (("cubes", "m_firm_open_dated", "measures", "value", "type"), "max"),
    ("measure_agg_to_max", "on_hand"): (("cubes", "m_on_hand", "measures", "value", "type"), "max"),
    ("measure_agg_to_max", "consigned"): (("cubes", "m_consigned", "measures", "value", "type"), "max"),
    ("dated_shift_one_day", "firm_open_dated"): (("cubes", "m_firm_open_dated", "dimensions", "ts", "sql"), "({CUBE}.ts) + interval '1 day'"),
    ("closure_join_swap", "firm_open"): (("cubes", CLOSURE, "joins", "m_firm_open", "sql"), "{CUBE}.descendant = {m_firm_open}.key"),
    ("closure_end_swap", "has_component"): (("cubes", CLOSURE, "joins", DESC, "sql"), '{CUBE}.ancestor = {has_component_descendant}."part_id"'),
}
# the filter mutants equal the cube cube_gen.measure_cube generates for these measure copies
REWRITES = {
    ("measure_filter_drop", "firm_open"): {"filter": {}},
    ("measure_filter_drop", "firm_open_dated"): {"filter": {}},
    ("measure_normalization_drop", "on_hand"): {"filter": {"location_id": ["MAIN", "SUB"]}, "filter_normalized": {}},
    ("measure_normalization_drop", "consigned"): {"filter": {"location_id": ["CONSIGN"]}, "filter_normalized": {}},
}
PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


class CatalogConn:
    """Answers the two catalog queries cube_gen.generate runs: the column list and the closure table check."""

    def __init__(self, closure_exists: bool = True):
        self.closure_exists = closure_exists

    def run(self, sql, **params):
        if "information_schema.columns" in sql:
            return [list(c) for c in DEMO_COLUMNS.get(f"{params['s']}.{params['t']}", [])]
        if "to_regclass" in sql:
            return [[self.closure_exists]]
        raise AssertionError("unexpected SQL in a unit test")


class RecordingConn:
    """Records statements and answers every query with one count."""

    def __init__(self, count):
        self.count, self.statements = count, []

    def run(self, sql, **params):
        self.statements.append((sql, params))
        return [[self.count]]


def demo_model(closure_exists: bool = True) -> tuple:
    cfg = config.load(pgfixture.DEMO_CONFIG)
    model, _notes = cube_gen.generate(cfg, CatalogConn(closure_exists))
    return cfg, model


def element(obj, path):
    for p in path:
        obj = next(x for x in obj if x.get("name") == p) if isinstance(obj, list) else obj[p]
    return obj


def changes(a, b, path=()) -> list:
    """Paths where two model structures differ; lists of named dicts are compared by name (and name order)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in list(a) + [k for k in b if k not in a]:
            out.extend([path + (k,)] if k not in a or k not in b else changes(a[k], b[k], path + (k,)))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if a and b and all(isinstance(x, dict) and "name" in x for x in a + b):
            if [x["name"] for x in a] != [x["name"] for x in b]:
                return [path]
            return changes({x["name"]: x for x in a}, {x["name"]: x for x in b}, path)
        if len(a) != len(b):
            return [path]
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in changes(x, y, path + (i,))]
    return [] if a == b else [path]


def regenerated_sql(cfg, measure: str, cols: dict, **change) -> str:
    m2 = dataclasses.replace(cfg.measures[measure], **change)
    return cube_gen.measure_cube(cfg, m2, cols)["sql"]


class OperatorTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.model = demo_model()
        self.original = copy.deepcopy(self.model)

    def mutants(self) -> dict:
        return {(mu.operator, mu.target): mu for mu in mutate.operators(self.cfg, self.model)}

    def test_catalogue_matches_design(self):
        self.assertEqual(len(mutate.OPERATORS), 9)
        self.assertEqual(list(dict.fromkeys(op for op, _t in EXPECTED)), list(mutate.OPERATORS))

    def test_one_mutant_per_applicable_target_in_order(self):
        got = [(mu.operator, mu.target) for mu in mutate.operators(self.cfg, self.model)]
        self.assertEqual(got, list(EXPECTED))

    def test_each_mutant_changes_exactly_its_element(self):
        for key, mu in self.mutants().items():
            with self.subTest(mutant=mu.check_id):
                path, value = EXPECTED[key]
                self.assertEqual(changes(self.original, mu.model), [path])
                if value is None:
                    cols = dict(DEMO_COLUMNS[self.cfg.entities[self.cfg.measures[mu.target].entity].table])
                    value = regenerated_sql(self.cfg, mu.target, cols, **REWRITES[key])
                self.assertEqual(element(mu.model, path), value)
        self.assertEqual(self.model, self.original)

    def test_filter_rewrites_remove_only_the_filter(self):
        mu = self.mutants()
        firm = element(mu[("measure_filter_drop", "firm_open")].model, ("cubes", "m_firm_open", "sql"))
        self.assertNotIn('"status"', firm)
        self.assertIn('t."part_id" IS NOT NULL GROUP BY', firm)
        on_hand = element(mu[("measure_normalization_drop", "on_hand")].model, ("cubes", "m_on_hand", "sql"))
        self.assertNotIn("upper(btrim(", on_hand)
        self.assertIn("""t."location_id" IN ('MAIN', 'SUB')""", on_hand)

    def test_mutant_models_are_independent_copies(self):
        for mu in mutate.operators(self.cfg, self.model):
            for a, b in zip(mu.model["cubes"], self.model["cubes"]):
                self.assertIsNot(a, b)
            mu.model["cubes"].clear()
        self.assertEqual(self.model, self.original)

    def test_witness_sql_is_one_read_query_with_bound_parameters(self):
        for mu in mutate.operators(self.cfg, self.model):
            with self.subTest(mutant=mu.check_id):
                self.assertRegex(mu.witness_sql, r"^(SELECT|WITH) ")
                self.assertNotIn(";", mu.witness_sql)
                self.assertNotRegex(mu.witness_sql, r"(?i)\b(insert|update|delete|merge|create|drop|alter|truncate)\b")
                self.assertEqual(set(PARAM.findall(mu.witness_sql)), set(mu.witness_params))
                self.assertTrue(mu.config_ref.startswith(("hierarchies.", "measures.")))

    def test_witness_sql_of_closure_and_measure_operators(self):
        mu = self.mutants()
        closure_table = '"sxc_out"."has_component_closure"'
        self.assertEqual(mu[("closure_depth_shift", "has_component")].witness_sql, f"SELECT count(*) FROM {closure_table}")
        self.assertEqual(mu[("closure_path_count_one", "has_component")].witness_sql,
                         f"SELECT count(*) FROM {closure_table} WHERE path_count > 1")
        self.assertEqual(mu[("closure_qty_single_path", "has_component")].witness_sql,
                         f"SELECT count(*) FROM {closure_table} WHERE path_count > 1 AND qty <> 0")
        drop = mu[("measure_filter_drop", "firm_open")].witness_sql
        self.assertIn(cube_gen.where_sql(self.cfg.measures["firm_open"]), drop)
        self.assertIn("""AND t."qty_open" IS NOT NULL AND t."qty_open" <> 0)""", drop)
        self.assertEqual(drop.count(' EXCEPT SELECT t."part_id" FROM '), 2, "keys only one filtered row set has")
        self.assertEqual(mu[("closure_end_swap", "has_component")].witness_sql,
                         f'SELECT count(*) FROM {closure_table} p LEFT JOIN "sxc_demo"."part" a ON a."part_id" = p.ancestor '
                         'LEFT JOIN "sxc_demo"."part" d ON d."part_id" = p.descendant WHERE a."part_id" IS DISTINCT FROM d."part_id"')
        self.assertIn("coalesce(a.v, 0) <> coalesce(d.v, 0)", mu[("closure_join_swap", "firm_open")].witness_sql)

    def test_agg_to_max_witness_aggregates_the_cube_rows_per_key_overall_and_in_pairs(self):
        mu = self.mutants()
        on_hand = mu[("measure_agg_to_max", "on_hand")].witness_sql
        self.assertIn(f'FROM ({element(self.model, ("cubes", "m_on_hand", "sql"))}) c', on_hand)
        self.assertIn('SELECT c."key" AS k, c."value" AS v', on_hand)
        for part in ("sum(v) IS DISTINCT FROM max(v)", "GROUP BY k", "count(*) > 0 AND sum(v)",
                     "count(*) FILTER (WHERE w <> 0) >= 2", "count(*) FILTER (WHERE w < 0) >= 1 AND count(w) >= 2"):
            self.assertIn(part, on_hand)
        dated = mu[("measure_agg_to_max", "firm_open_dated")].witness_sql
        self.assertIn(f'FROM ({element(self.model, ("cubes", "m_firm_open_dated", "sql"))}) c', dated)
        self.assertIn('c."v" AS v', dated)
        self.cfg.measures["firm_open_dated"].agg = self.cfg.measures["on_hand"].agg = "count"
        model, _notes = cube_gen.generate(self.cfg, CatalogConn())
        counted = {(m.operator, m.target): m.witness_sql for m in mutate.operators(self.cfg, model)}
        self.assertIn("count(v) IS DISTINCT FROM max(v)", counted[("measure_agg_to_max", "firm_open_dated")])
        self.assertIn("sum(v) IS DISTINCT FROM max(v)", counted[("measure_agg_to_max", "on_hand")],
                      "a pre-aggregated count is summed over its per-key counts")

    def test_witnesses_depend_on_the_data_not_on_the_ontology_or_the_gold_windows(self):
        end = self.mutants()[("closure_end_swap", "has_component")].witness_sql
        dated = self.mutants()[("dated_shift_one_day", "firm_open_dated")]
        rows = element(self.model, ("cubes", "m_firm_open_dated", "sql"))
        self.assertEqual((dated.witness_sql, dated.witness_params), (f'SELECT count(*) FROM ({rows}) c WHERE c."ts" IS NOT NULL', {}))
        swept = {"dimensions": ["m_firm_open_dated.key"], "measures": ["m_firm_open_dated.value"],
                 "timeDimensions": [{"dimension": TS, "dateRange": [":d", "2026-12-31"]}]}
        variants = {"no class column": lambda: setattr(self.cfg, "ontology", dataclasses.replace(self.cfg.ontology, class_column="")),
                    "no gold window": lambda: setattr(self.cfg, "gold", [g for g in self.cfg.gold if g.id != "q05_firm_window"]),
                    "swept window": lambda: setattr(self.cfg, "gold", [
                        dataclasses.replace(g, cube_query=swept, param_sets=[], params={"d": Param(name="d", values=["2026-07-01"])})
                        if g.id == "q05_firm_window" else g for g in self.cfg.gold])}
        for name, change in variants.items():
            with self.subTest(variant=name):
                self.cfg = config.load(pgfixture.DEMO_CONFIG)
                change()
                mu = self.mutants()
                self.assertEqual(mu[("closure_end_swap", "has_component")].witness_sql, end)
                self.assertEqual((mu[("dated_shift_one_day", "firm_open_dated")].witness_sql,
                                  mu[("dated_shift_one_day", "firm_open_dated")].witness_params), (dated.witness_sql, {}))

    def test_selection_by_configuration(self):
        self.cfg.verify.mutation_operators = ["closure_end_swap", "closure_depth_shift"]
        self.assertEqual([mu.operator for mu in mutate.operators(self.cfg, self.model)],
                         ["closure_depth_shift", "closure_end_swap"])
        self.cfg.verify.mutation_operators = []
        self.assertEqual(mutate.operators(self.cfg, self.model), [])
        for bad in (["closure_depth_shift", "no_such_operator"], "all", [3]):
            self.cfg.verify.mutation_operators = bad
            with self.assertRaises(StageError) as e:
                mutate.operators(self.cfg, self.model)
            self.assertEqual(e.exception.exit_code, 2)

    def test_missing_closure_cube_is_not_applicable(self):
        cfg, model = demo_model(closure_exists=False)
        got = [(mu.operator, mu.target) for mu in mutate.operators(cfg, model)]
        self.assertEqual(got, [k for k in EXPECTED if not k[0].startswith("closure_")])

    def test_model_from_another_configuration_raises(self):
        stale = copy.deepcopy(self.model)
        cube = element(stale, ("cubes", "m_on_hand"))
        cube["sql"] = cube["sql"].replace("upper(btrim(", "lower(btrim(")
        self.cfg.verify.mutation_operators = ["measure_normalization_drop"]
        with self.assertRaises(mutate.MutationError):
            mutate.operators(self.cfg, stale)
        stale = copy.deepcopy(self.model)
        joins = element(stale, ("cubes", CLOSURE, "joins"))
        joins[:] = [j for j in joins if j["name"] != "m_firm_open"]
        self.cfg.verify.mutation_operators = ["closure_join_swap"]
        with self.assertRaises(mutate.MutationError):
            mutate.operators(self.cfg, stale)
        stale = copy.deepcopy(self.model)
        del element(stale, ("cubes", "m_firm_open_dated"))["sql"]
        self.cfg.verify.mutation_operators = ["dated_shift_one_day"]
        with self.assertRaises(mutate.MutationError):
            mutate.operators(self.cfg, stale)
        self.cfg.verify.mutation_operators = ["measure_agg_to_max"]
        with self.assertRaises(mutate.MutationError):
            mutate.operators(self.cfg, stale)
        for patch in ({"type": "max"}, {"sql": "sum({CUBE}.value)"}):
            stale = copy.deepcopy(self.model)
            element(stale, ("cubes", "m_on_hand", "measures", "value")).update(patch)
            with self.subTest(patch=patch), self.assertRaises(mutate.MutationError):
                mutate.operators(self.cfg, stale)

    def test_witness_runs_in_a_read_only_transaction(self):
        mu = self.mutants()[("dated_shift_one_day", "firm_open_dated")]
        conn = RecordingConn(4)
        self.assertEqual(mutate.witness(conn, self.cfg, mu), 4)
        sqls = [s for s, _p in conn.statements]
        self.assertEqual(sqls[0], "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(sqls[1], f"SET LOCAL statement_timeout = {self.cfg.source.statement_timeout_ms}")
        self.assertEqual(conn.statements[2], (mu.witness_sql, mu.witness_params))
        self.assertEqual(sqls[3], "ROLLBACK")


CLOSURE_CHECKS = ["v4:closure_depth_shift:has_component", "v4:closure_path_count_one:has_component",
                  "v4:closure_qty_single_path:has_component", "v4:closure_join_swap:firm_open",
                  "v4:closure_end_swap:has_component"]
BUILT = {"has_component": "pass"}


class RunMutantsTest(unittest.TestCase):
    """run_mutants control flow with a stubbed witness, Cube runner and suite."""

    def run_with(self, model, witnesses: dict, fail_startup: str = "", suite_error: str = "", item_error: str = "",
                 survivors: tuple = (), closure=BUILT, build_status: str = ""):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        self.served = []

        def fake_witness(conn, cfg_, mu):
            n = witnesses.get(mu.operator, 3)
            if isinstance(n, Exception):
                raise n
            return n

        @contextlib.contextmanager
        def fake_serve(cfg_, run_dir, log_path=None):
            run_dir = Path(run_dir)
            self.served.append(run_dir.name)
            self.assertTrue(list((run_dir / "model").glob("*.yml")))
            if run_dir.name == fail_startup:
                raise StageError("Cube adapter exited during startup (code 1)", 3)
            yield types.SimpleNamespace(run_dir=run_dir)

        def suite(client):
            name = client.run_dir.name
            if name == suite_error:
                raise CubeError("HTTP 500")
            if name == item_error:
                return "error", "v2:q01_explosion:top[1]"
            if name.startswith(survivors):
                return "pass", ""
            return "fail", "v1:q01_explosion"

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name)
        if build_status:
            build = Result("build")
            build.add("closure:has_component", build_status, "hierarchies.has_component")
            build.write(out)
        with mock.patch.object(mutate, "witness", fake_witness), mock.patch.object(cube_run, "serve", fake_serve):
            checks, not_proven = mutate.run_mutants(cfg, out, None, model, suite, closure=closure)
        return out, {cid: (st, ref, det) for cid, st, ref, det in checks}, checks, not_proven

    def test_outcomes_details_and_summary(self):
        _cfg, model = demo_model()
        out, by_id, checks, not_proven = self.run_with(
            model, {"measure_normalization_drop": 0, "closure_qty_single_path": Exception({"C": "57014", "M": "canceled"})},
            fail_startup="closure_end_swap__has_component", suite_error="closure_join_swap__firm_open",
            survivors=("measure_agg_to_max__",))
        self.assertEqual(by_id["v4:closure_depth_shift:has_component"],
                         ("pass", "hierarchies.has_component",
                          {"witness": 3, "killed_by": "v1:q01_explosion", "summary": checks[0][3]["summary"]}))
        for target in ("firm_open", "firm_open_dated", "on_hand", "consigned"):
            st, ref, det = by_id[f"v4:measure_agg_to_max:{target}"]
            self.assertEqual((st, ref, det["killed_by"]), ("fail", f"measures.{target}", ""))
        self.assertEqual(by_id["v4:closure_qty_single_path:has_component"][0], "error")
        self.assertEqual(by_id["v4:closure_qty_single_path:has_component"][2]["sqlstate"], "57014")
        self.assertNotIn("canceled", repr(checks))
        st, _ref, det = by_id["v4:closure_end_swap:has_component"]
        self.assertEqual((st, det["reason"], det["exit_code"], det["log"]),
                         ("error", "startup", 3, "verify/mutants/closure_end_swap__has_component/server.log"))
        st, _ref, det = by_id["v4:closure_join_swap:firm_open"]
        self.assertEqual((st, det["reason"], det["exception"]), ("error", "suite_error", "CubeError"))
        self.assertEqual(not_proven, ["v4:measure_normalization_drop:on_hand equivalent on this data",
                                      "v4:measure_normalization_drop:consigned equivalent on this data"])
        statuses = [st for st, _r, _d in by_id.values()]
        summary = {"killed": statuses.count("pass"), "survived": statuses.count("fail"), "equivalent": 2,
                   "error": statuses.count("error"), "not_applicable": 0, "not_run": 0}
        self.assertEqual(summary, {"killed": 5, "survived": 4, "equivalent": 2, "error": 3, "not_applicable": 0, "not_run": 0})
        self.assertTrue(all(det["summary"] == summary for _st, _r, det in by_id.values()))
        yml = out / "verify" / "mutants" / "closure_depth_shift__has_component" / "model" / f"{CLOSURE}.yml"
        self.assertIn('"({CUBE}.min_depth) + 1"', yml.read_text(encoding="utf-8"))
        self.assertTrue((yml.parents[1] / "model.json").is_file())

    def test_an_item_error_leaves_the_mutant_unjudged_never_killed(self):
        _cfg, model = demo_model()
        _out, by_id, _checks, _np = self.run_with(model, {}, item_error="closure_depth_shift__has_component")
        st, ref, det = by_id["v4:closure_depth_shift:has_component"]
        self.assertEqual((st, ref, det["reason"], det["item"], det["witness"]),
                         ("error", "hierarchies.has_component", "suite_error", "v2:q01_explosion:top[1]", 3))
        self.assertNotIn("killed_by", det)
        self.assertEqual(det["summary"]["error"], 1)
        self.assertEqual(mutate.verdict("fail", "v1:a"), ("pass", {"killed_by": "v1:a"}))
        self.assertEqual(mutate.verdict("pass", ""), ("fail", {"killed_by": ""}))
        for status in ("error", "not_run", None):
            self.assertEqual(mutate.verdict(status, "v3:x")[0], "error", status)

    def test_closure_operators_follow_the_build_closure_check(self):
        _cfg, model = demo_model()
        for closure, build_status, seen in (({"has_component": "fail"}, "", "fail"), (None, "fail", "fail"),
                                            (None, "", "missing"), ({}, "", "missing")):
            with self.subTest(closure=closure, build=build_status):
                _out, by_id, _checks, not_proven = self.run_with(model, {}, closure=closure, build_status=build_status)
                for cid in CLOSURE_CHECKS:
                    st, ref, det = by_id[cid]
                    self.assertEqual((st, ref, det["reason"], det["hierarchy"], det["build_check"]),
                                     ("not_run", "hierarchies.has_component", "closure_not_passed", "has_component", seen))
                    self.assertIn(f"{cid}: closure:has_component did not pass in build ({seen})", not_proven)
                self.assertFalse([name for name in self.served if name.startswith("closure_")], "no closure mutant served")
                self.assertEqual(by_id["v4:measure_filter_drop:firm_open"][0], "pass")
                self.assertEqual(by_id["v4:measure_filter_drop:firm_open"][2]["summary"]["not_run"], 5)
        _out, by_id, _checks, _np = self.run_with(model, {}, closure=None, build_status="pass")
        self.assertTrue(all(by_id[cid][0] == "pass" for cid in CLOSURE_CHECKS))

    def test_unbuilt_closure_and_stale_model(self):
        _cfg, model = demo_model(closure_exists=False)
        cube = element(model, ("cubes", "m_firm_open"))
        cube["sql"] = cube["sql"].replace("""IN ('FIRM')""", """IN ('FIRM', 'OPEN')""")
        _out, by_id, _checks, not_proven = self.run_with(model, {})
        self.assertEqual(by_id["v4:measure_filter_drop:firm_open"][0:2], ("error", "measures.firm_open"))
        self.assertEqual(by_id["v4:measure_filter_drop:firm_open"][2]["reason"], "model_mismatch")
        self.assertEqual(by_id["v4:measure_filter_drop:firm_open_dated"][0], "pass")
        skipped = [n for n in not_proven if " not run: " in n]
        self.assertEqual([n.split(" ")[0] for n in skipped], CLOSURE_CHECKS)
        self.assertTrue(all(det["summary"]["not_applicable"] == 5 for _st, _r, det in by_id.values()))


def cube_ready() -> bool:
    return cube_run.installed(cube_run.cube_home()) and shutil.which("node") is not None


def subst(obj, params: dict):
    if isinstance(obj, str) and obj.startswith(":") and obj[1:] in params:
        return params[obj[1:]]
    if isinstance(obj, list):
        return [subst(x, params) for x in obj]
    if isinstance(obj, dict):
        return {k: subst(v, params) for k, v in obj.items()}
    return obj


def keyed(rows: list, key: list) -> dict | None:
    out = {}
    for r in rows:
        k = tuple(canon.canon(r[c]) for c in key)
        if k in out:
            return None
        out[k] = r
    return out


def same_rows(g, gold_rows: list, cube_rows: list) -> bool:
    """Strict comparison of the design (canonical key match, every column identical, zero_if_null)."""
    mapped = [{col: r.get(member) for col, member in g.map.items()} for r in cube_rows]
    a, b = keyed(gold_rows, g.key), keyed(mapped, g.key)
    if a is None or b is None or a.keys() != b.keys():
        return False
    for k, ra in a.items():
        for col in g.map:
            vb = b[k][col]
            if vb is None and col in g.zero_if_null:
                vb = 0
            if not canon.same(ra[col], vb):
                return False
    return True


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class CleanDemoTest(unittest.TestCase):
    NAME = "sxc_t_mutate_clean"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.conn = cls.fx.check_conn()
        cls.addClassCleanup(cls.conn.close)
        with db.write_tx(cls.conn):
            checks, _log = closure.build(cls.conn, cls.cfg, cls.cfg.hierarchy())
        if [c[1] for c in checks] != ["pass", "pass"]:
            raise AssertionError(f"closure build failed: {[c[:2] for c in checks]}")
        with db.read_only(cls.conn):
            cls.model, _notes = cube_gen.generate(cls.cfg, cls.conn)

    def setUp(self):
        os.environ[self.cfg.source.dsn_env] = pgfixture.dsn_for(self.fx.check, self.fx.name)
        self.cfg.verify.mutation_operators = "default"

    def tmpdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def mutants(self) -> dict:
        return {mu.check_id: mu for mu in mutate.operators(self.cfg, self.model)}

    def test_unit_fixture_model_equals_the_catalog_model(self):
        self.assertEqual(demo_model()[1], self.model)

    def test_filter_rewrites_equal_regenerated_cubes_with_catalog_types(self):
        mu = self.mutants()
        for (op, target), change in REWRITES.items():
            m = self.cfg.measures[target]
            with db.read_only(self.conn):
                cols = dict(cube_gen.columns(self.conn, self.cfg.entities[m.entity].table))
            self.assertEqual(element(mu[f"v4:{op}:{target}"].model, ("cubes", naming.cube_measure(target), "sql")),
                             regenerated_sql(self.cfg, target, cols, **change))

    def test_witnesses_against_the_demo_reference(self):
        mu, data = self.mutants(), self.fx.data
        exp = data["expected"]
        dated = sum(1 for o in data["orders"] if o["status"] == "FIRM" and o["part_id"] and o["due_date"])
        self.assertEqual(mutate.witness(self.conn, self.cfg, mu["v4:closure_depth_shift:has_component"]), exp["closure_pairs"])
        self.assertEqual(mutate.witness(self.conn, self.cfg, mu["v4:closure_path_count_one:has_component"]), exp["multi_path_pairs"])
        self.assertEqual(mutate.witness(self.conn, self.cfg, mu["v4:closure_end_swap:has_component"]), exp["closure_pairs"],
                         "every clean pair joins two different part rows")
        self.assertEqual(mutate.witness(self.conn, self.cfg, mu["v4:dated_shift_one_day:firm_open_dated"]), dated)
        for cid in ("v4:closure_qty_single_path:has_component", "v4:measure_filter_drop:firm_open",
                    "v4:measure_agg_to_max:on_hand", "v4:measure_agg_to_max:firm_open_dated", "v4:closure_join_swap:firm_open"):
            self.assertGreater(mutate.witness(self.conn, self.cfg, mu[cid]), 0, cid)

    def test_normalization_drop_is_equivalent_on_clean_data(self):
        self.cfg.verify.mutation_operators = ["measure_normalization_drop"]
        out = self.tmpdir()
        checks, not_proven = mutate.run_mutants(self.cfg, out, self.conn, self.model, lambda client: ("pass", ""))
        self.assertEqual(checks, [])
        self.assertEqual(not_proven, ["v4:measure_normalization_drop:on_hand equivalent on this data",
                                      "v4:measure_normalization_drop:consigned equivalent on this data"])
        self.assertFalse((out / "verify").exists())

    def gold_cases(self) -> list:
        """Bindings that expose the mutations: q01 top root, q02 shared part, q03 component, q04, q05 one window."""
        cfg, h = self.cfg, self.cfg.hierarchy()
        closure_t = ident.qualified(naming.closure_table(cfg, h.name))
        edges = ident.qualified(cfg.entities[h.edge].table)
        parent, child = ident.quote(h.parent), ident.quote(h.child)
        orders = ident.qualified(cfg.entities["sales_order"].table)
        gold = {g.id: g for g in cfg.gold}
        with db.read_only(self.conn):
            root = db.scalar(self.conn, f"""SELECT c.ancestor FROM {closure_t} c WHERE c.path_count > 1
                AND NOT EXISTS (SELECT 1 FROM {edges} b WHERE b.{child} = c.ancestor) ORDER BY c.ancestor LIMIT 1""")
            part = db.scalar(self.conn, f"""SELECT c.descendant FROM {closure_t} c WHERE c.descendant IN
                (SELECT {child} FROM {edges} GROUP BY {child} HAVING count(DISTINCT {parent}) >= 2)
                GROUP BY c.descendant ORDER BY count(*) DESC, c.descendant LIMIT 1""")
            window = next(ps for ps in gold["q05_firm_window"].param_sets if db.scalar(
                self.conn, f"""SELECT count(*) FROM {orders} WHERE status = 'FIRM'
                AND (due_date = CAST(:e AS date) OR due_date + 1 = CAST(:s AS date))""", e=ps["date_to"], s=ps["date_from"]))
            cases = []
            for gid, params in (("q01_explosion", {"root": root}), ("q02_where_used", {"part": part}),
                                ("q03_stock", {"part_class": "component"}), ("q04_depth_histogram", {}),
                                ("q05_firm_window", window)):
                g = gold[gid]
                rows = db.rows(self.conn, g.sql_path(cfg).read_text(encoding="utf-8-sig"), **params)
                self.assertTrue(rows, gid)
                cases.append((f"v1:{gid}", g, params, rows))
        return cases

    @unittest.skipUnless(cube_ready(), "Cube adapter not installed (sxc.py cube-install or SXC_CUBE_HOME) or node missing")
    def test_gold_suite_kills_the_observable_mutants(self):
        cases = self.gold_cases()

        def suite(client):
            for case_id, g, params, rows in cases:
                try:
                    got = client.load(subst(g.cube_query, params))
                except CubeError:
                    return "error", case_id
                if not same_rows(g, rows, got):
                    return "fail", case_id
            return "pass", ""

        out = self.tmpdir()
        cube_gen.write_model(self.model, out / "original" / "model")
        with cube_run.serve(self.cfg, out / "original", out / "original" / "server.log") as client:
            self.assertEqual(suite(client), ("pass", ""), "the unmutated model must pass the suite")

        checks, not_proven = mutate.run_mutants(self.cfg, out, self.conn, self.model, suite, closure=BUILT)
        by_id = {cid: (st, ref, det) for cid, st, ref, det in checks}

        self.assertEqual({cid for cid, (st, _r, _d) in by_id.items() if st == "error"}, set())
        for cid in ("v4:closure_depth_shift:has_component", "v4:closure_path_count_one:has_component",
                    "v4:closure_qty_single_path:has_component", "v4:measure_filter_drop:firm_open",
                    "v4:dated_shift_one_day:firm_open_dated", "v4:closure_end_swap:has_component"):
            st, _ref, det = by_id[cid]
            self.assertEqual(st, "pass", cid)
            self.assertGreater(det["witness"], 0, cid)
            self.assertTrue(det["killed_by"].startswith("v1:"), cid)
        for target in ("on_hand", "consigned"):
            self.assertIn(f"v4:measure_normalization_drop:{target} equivalent on this data", not_proven)
            self.assertNotIn(f"v4:measure_normalization_drop:{target}", by_id)
        # per-key gold queries cannot see sum -> max on a cube pre-aggregated per key (V3 totals do)
        for target in ("firm_open", "on_hand", "consigned"):
            self.assertEqual(by_id[f"v4:measure_agg_to_max:{target}"][0], "fail")
        statuses = [st for st, _r, _d in by_id.values()]
        self.assertEqual(checks[0][3]["summary"], {"killed": statuses.count("pass"), "survived": statuses.count("fail"),
                                                   "equivalent": 2, "error": 0, "not_applicable": 0, "not_run": 0})
        for cid in by_id:
            _v4, op, target = cid.split(":")
            run_dir = out / "verify" / "mutants" / f"{op}__{target}"
            self.assertTrue(list((run_dir / "model").glob("*.yml")), cid)
            self.assertTrue((run_dir / "server.log").is_file(), cid)


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class DirtyDemoTest(unittest.TestCase):
    NAME = "sxc_t_mutate_dirty"

    def test_normalization_drop_is_observable_on_dirty_data(self):
        fx = pgfixture.PgFixture(self.NAME, "dirty")
        self.addCleanup(fx.teardown)
        fx.setup()
        cfg = config.load(pgfixture.DEMO_CONFIG)
        conn = fx.check_conn()
        self.addCleanup(conn.close)
        with db.read_only(conn):
            model, _notes = cube_gen.generate(cfg, conn)      # the dirty closure has a cycle: no closure built
        mutants = {mu.check_id: mu for mu in mutate.operators(cfg, model)}
        self.assertNotIn("v4:closure_depth_shift:has_component", mutants)
        n = mutate.witness(conn, cfg, mutants["v4:measure_normalization_drop:on_hand"])
        self.assertGreater(n, 0)
        self.assertEqual(n, fx.data["expected"]["location_variants"])
        self.assertEqual(mutate.witness(conn, cfg, mutants["v4:measure_normalization_drop:consigned"]), 0)


if __name__ == "__main__":
    unittest.main()
