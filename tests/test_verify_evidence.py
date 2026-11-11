"""What verify records about its gold queries and the facets they compare (the evidence object of verify results).

Why: a passing verify said nothing about which parts of the model a person's gold SQL actually compared with rows
that hold values. verify now records, per gold query, its kind (a gold whose text contains generated text is
generated evidence), the members it compares and the ones it only uses in filters, and how many of its comparisons
had rows; per facet of a measure, hierarchy or class dimension, whether authored evidence covers it or why not. These
tests run verify without a database or Cube (the planning, comparison and Cube functions are patched) and pin the
facet reasons, the zero-row lists, the new not_proven lines, the full-scope gate and that nothing of the gold text,
the rows or the parameters reaches the result.

A passing V4 check used to read as "a gold query detects this change", although a V3 check between generated layers
could have killed the mutant. The V4 suite now runs authored gold items first, and every V4 check with a killer
records what kind of item detected the mutant (detection), the facet its operator changes and the authored gold
queries that compare that facet; a kill by a generated or unclassified item gets a not_proven line with a remedy. The
attribution tests pin the suite order, the F1 guard (a V4 pass is never read as a gold detection unless an authored
gold item killed the mutant), the F1 trace with the real total test, the mutant split and the coverage annotations.
"""

import argparse
import contextlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import compare, config, cube_gen, evidence, mutate, shape_q, stage_verify, sweep
from semantic_crosscheck.result import Result
from test_compare_unit import FakeConn, demo_model

REAL_BINDINGS = sweep.bindings
DEMO_FACETS = [
    ("measures.firm_open", "m_firm_open.value"), ("measures.firm_open", "has_component_closure+m_firm_open"),
    ("measures.firm_open", "part+m_firm_open"), ("measures.firm_open_dated", "m_firm_open_dated.value"),
    ("measures.firm_open_dated", "m_firm_open_dated.ts"), ("measures.on_hand", "m_on_hand.value"),
    ("measures.on_hand", "part+m_on_hand"), ("measures.consigned", "m_consigned.value"),
    ("measures.consigned", "part+m_consigned"), ("hierarchies.has_component", "has_component_closure.qty"),
    ("hierarchies.has_component", "has_component_closure.path_count"),
    ("hierarchies.has_component", "has_component_closure.min_depth"),
    ("hierarchies.has_component", "has_component_closure.max_depth"),
    ("hierarchies.has_component", "has_component_closure.pairs"),
    ("hierarchies.has_component", "has_component_ancestor.*"),
    ("hierarchies.has_component", "has_component_descendant.*"), ("ontology.class_column", "part.part_class")]
ON_HAND = "upper(btrim(t.\"location_id\")) IN ('MAIN', 'SUB')"
N4_TAIL = ("; only checks between generated layers cover these facets; add a gold query, stratum or parameter set "
           "that compares them, or list them in verify.accepted_gaps")
GENERATED_TAIL = "count as generated evidence, not authored"
SECRET_ROW, SECRET_PARAM, SECRET_TEXT = "SECRET-ROW-VALUE", "SECRET-PARAM-VALUE", "SECRET-GOLD-TEXT"


class VerifyRun(unittest.TestCase):
    """verify over a copy of the demo configuration and its gold files, without PostgreSQL or Cube.

    rows[gold id] gives the gold rows of each binding (a list, or a function of the parameters returning a list or
    "error"); the Cube side returns the same rows, so a comparison passes and counts its rows. Without an entry every
    binding has one row with the value 1 in every column."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_evidence_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copy(pgfixture.DEMO_CONFIG, self.tmp / "crosscheck.toml")
        shutil.copytree(pgfixture.DEMO_CONFIG.parent / "gold", self.tmp / "gold")
        self.cfg = config.load(self.tmp / "crosscheck.toml")
        self.out = self.tmp / "out"
        self.rows: dict = {}
        self.bindings: dict = {}
        self.mutants: tuple = ([], [])

    def gold_file(self, gid: str, text: str):
        (self.tmp / "gold" / f"{gid}.sql").write_text(text, encoding="utf-8")

    def write_stages(self, closure_status: str | None = "pass", closure_cubes: bool = True) -> dict:
        model = demo_model(self.cfg, closure=closure_cubes)
        cube_gen.write_model(model, self.out / "cube" / "model")
        cube = Result("cube")
        cube.add("cube:model", "pass", "")
        cube.write(self.out)
        if closure_status:
            build = Result("build")
            build.add("closure:has_component", closure_status, "hierarchies.has_component")
            build.write(self.out)
        return model

    def _bindings(self, conn, cfg, g):
        if g.id in self.bindings:
            return self.bindings[g.id]
        if g.param_sets or not g.params:
            return REAL_BINDINGS(conn, cfg, g)
        p = next(iter(g.params.values()))
        if p.strata:
            return [(s, {p.name: f"{s}-1"}) for s in p.strata]
        return [("values", {p.name: "v-1"})]

    def _run_binding(self, conn, client, cfg, g, params, cache=None, types=None):
        spec = self.rows.get(g.id)
        rows = spec(params) if callable(spec) else spec
        if rows is None:
            rows = [{col: 1 for col in g.map}]
        if rows == "error":
            return {"pass": False, "side": "gold", "error": "DatabaseError", "sqlstate": "57014"}
        return compare.compare_rows(g, rows, [dict(r) for r in rows], types)

    def _run_mutants(self, cfg, out, conn, model, suite, closure=None, sampled_items=(), coverage=None):
        return self.mutants

    def verify(self, only=None, gold=None, closure_status: str | None = "pass", closure_cubes: bool = True) -> Result:
        if not (self.out / "cube" / "model.json").is_file():
            self.write_stages(closure_status, closure_cubes)
        with mock.patch.object(stage_verify.db, "connect", return_value=FakeConn()), \
                mock.patch.object(stage_verify.sweep, "bindings", self._bindings), \
                mock.patch.object(stage_verify.cube_run, "serve", lambda *a, **k: contextlib.nullcontext("client")), \
                mock.patch.object(stage_verify.compare, "run_binding", self._run_binding), \
                mock.patch.object(stage_verify.shape_q, "run_test", lambda *a, **k: ("pass", {})), \
                mock.patch.object(stage_verify.shape_q, "replay_test", lambda *a, **k: ("pass", {})), \
                mock.patch.object(stage_verify.closure_oracle, "run", lambda *a, **k: ("pass", {})), \
                mock.patch.object(stage_verify.mutate, "run_mutants", self._run_mutants):
            return stage_verify.run(self.cfg, argparse.Namespace(only=only, gold=gold or []), self.out)

    @staticmethod
    def facet(res: Result, fid: str) -> dict:
        return next(f for f in res.evidence["facets"] if f["facet"] == fid)

    def status(self, res: Result, fid: str) -> tuple[str, str]:
        f = self.facet(res, fid)
        return f["status"], f["reason"]

    @staticmethod
    def gold(res: Result, gid: str) -> dict:
        return next(g for g in res.evidence["gold"] if g["id"] == gid)

    @staticmethod
    def lines(res: Result, *prefixes: str) -> list[str]:
        return [n for n in res.not_proven if n.startswith(prefixes)]


class GoldAccountingTest(VerifyRun):
    def test_compares_vs_uses(self):
        res = self.verify()
        q05 = self.gold(res, "q05_firm_window")
        self.assertEqual(q05, {"id": "q05_firm_window", "kind": "authored", "generated_text": [],
                               "compares": ["m_firm_open_dated.key", "m_firm_open_dated.value"],
                               "uses_only": ["m_firm_open_dated.ts"], "comparisons": 7, "nonempty_comparisons": 7,
                               "value_rows": {"m_firm_open_dated.key": 7, "m_firm_open_dated.value": 7}})
        self.assertEqual(self.gold(res, "q01_explosion")["uses_only"], ["has_component_closure.ancestor"])
        self.assertEqual(self.gold(res, "q04_depth_histogram")["comparisons"], 2, "v1 repeats v2's only binding")
        self.assertEqual([g["id"] for g in res.evidence["gold"]], [g.id for g in self.cfg.gold])

    def test_a_member_used_only_in_a_filter_is_not_compared(self):
        q03 = next(g for g in self.cfg.gold if g.id == "q03_stock")
        del q03.map["on_hand"]
        q03.zero_if_null.remove("on_hand")
        q03.cube_query["measures"] = ["m_consigned.value"]
        q03.cube_query["filters"].append({"member": "m_on_hand.value", "operator": "gt", "values": ["0"]})
        self.assertIn("m_on_hand.value", stage_verify._members(q03), "_members keeps it for gating")
        res = self.verify()
        g = self.gold(res, "q03_stock")
        self.assertNotIn("m_on_hand.value", g["compares"])
        self.assertIn("m_on_hand.value", g["uses_only"])
        self.assertEqual(self.facet(res, "m_on_hand.value"),
                         {"element": "measures.on_hand", "facet": "m_on_hand.value", "status": "gap",
                          "reason": "filter_only", "compared_by": [], "uses_only_by": ["q03_stock"], "detected_by": []})
        self.assertEqual(self.status(res, "part+m_on_hand"), ("gap", "filter_only"))
        self.assertEqual(self.status(res, "m_consigned.value"), ("compared", ""))
        self.assertIn("measures.on_hand: no authored evidence for m_on_hand.value (filter_only: q03_stock), "
                      "part+m_on_hand (filter_only: q03_stock)" + N4_TAIL, res.not_proven)

    def test_gold_kinds_are_stored_before_planning_returns(self):
        self.gold_file("q01_explosion", "SELECT * FROM sxc_out.has_component_closure")
        model = self.write_stages("fail")
        v = stage_verify._Verify(self.cfg, self.out, model, ["v1"])
        with mock.patch.object(stage_verify.sweep, "bindings", self._bindings):
            plans = [v.plan_gold(FakeConn(), i, g) for i, g in enumerate(self.cfg.gold)]
        self.assertEqual([bool(p["gate"]) for p in plans], [True, True, False, True, False])
        self.assertEqual(v.gold_kinds, {"q01_explosion": "generated", "q02_where_used": "authored",
                                        "q03_stock": "authored", "q04_depth_histogram": "authored",
                                        "q05_firm_window": "authored"}, "gated golds have a kind too")
        bare = self.write_stages("pass", closure_cubes=False)
        v = stage_verify._Verify(self.cfg, self.out, bare, ["v1"])
        plans = [v.plan_gold(FakeConn(), i, g) for i, g in enumerate(self.cfg.gold[:2])]
        self.assertEqual([p["problem"]["reason"] for p in plans], ["members_not_in_model"] * 2)
        self.assertEqual(v.gold_kinds, {"q01_explosion": "generated", "q02_where_used": "authored"},
                         "unplanned golds have a kind too")

    def test_generated_text_lines_and_kinds(self):
        self.gold_file("q03_stock", "-- copied\nSELECT p.part_id FROM sxc_demo.part p JOIN sxc_demo.stock t "
                                    "ON t.part_id = p.part_id WHERE " + ON_HAND)
        self.gold_file("q04_depth_histogram", 'SELECT max_depth, count(*) AS pairs FROM "SXC_OUT".'
                                              "has_component_closure "
                                              "UNION ALL SELECT 1, 1 FROM has_component_cycles GROUP BY 1")
        self.mutants = ([], ["v4:closure_depth_shift:has_component equivalent on this data"])
        res = self.verify()
        self.assertEqual(self.gold(res, "q03_stock")["generated_text"], ["filter:measures.on_hand"])
        self.assertEqual(self.gold(res, "q03_stock")["kind"], "generated")
        self.assertEqual(self.gold(res, "q04_depth_histogram")["generated_text"],
                         ["output_schema", "relation:has_component_cycles"])
        self.assertEqual(self.lines(res, "gold:"), [
            "gold:q03_stock: the gold SQL contains the generated filter of measures.on_hand verbatim; v1:q03_stock "
            "and v2:q03_stock:* " + GENERATED_TAIL,
            "gold:q04_depth_histogram: the gold SQL names the output schema sxc_out; v1:q04_depth_histogram and "
            "v2:q04_depth_histogram:* " + GENERATED_TAIL,
            "gold:q04_depth_histogram: the gold SQL names the generated relation has_component_cycles; "
            "v1:q04_depth_histogram and v2:q04_depth_histogram:* " + GENERATED_TAIL])
        for fid in ("m_on_hand.value", "part+m_on_hand", "m_consigned.value", "part+m_consigned",
                    "has_component_closure.pairs"):
            self.assertEqual(self.status(res, fid), ("gap", "generated_text"), fid)
        self.assertEqual(self.status(res, "part.part_class"), ("gap", "generated_text"))
        self.assertEqual(self.status(res, "has_component_closure.max_depth"), ("compared", ""),
                         "q01 and q02 compare it")
        self.assertIn("measures.on_hand: no authored evidence for m_on_hand.value (generated_text: q03_stock), "
                      "part+m_on_hand (generated_text: q03_stock)" + N4_TAIL, res.not_proven)
        lines = res.not_proven
        first_gold = lines.index(self.lines(res, "gold:")[0])
        self.assertLess(first_gold, lines.index("v4:closure_depth_shift:has_component equivalent on this data"),
                        "gold lines come before the V4 lines")
        self.assertTrue(all(n.startswith(("measures.", "hierarchies.", "ontology.")) for n in lines[-4:]),
                        "facet lines come last")


class FacetTest(VerifyRun):
    def test_facet_set(self):
        model = demo_model(self.cfg)
        self.assertEqual([(f.element, f.id) for f in evidence.facets(self.cfg, model)], DEMO_FACETS)
        bare = demo_model(self.cfg, closure=False)
        self.assertEqual([(f.element, f.id) for f in evidence.facets(self.cfg, bare)], DEMO_FACETS,
                         "facets of cubes missing from the model stay")
        self.assertEqual([f.id for f in evidence.facets(self.cfg)], [f for _e, f in DEMO_FACETS] + [
            "bom_line.part_class", "sales_order.part_class", "stock.part_class"],
                         "without a model every entity may hold the class column")
        self.assertNotIn("part+m_firm_open_dated", [f.id for f in evidence.facets(self.cfg, model)],
                         "a dated measure's cube is not joined to its node")
        self.assertEqual(evidence.MUTATION_OPERATORS, mutate.OPERATORS)
        plan = [(t.operator, t.target) for t in mutate._plan(self.cfg)]
        self.assertEqual(evidence.operator_targets(self.cfg), plan)
        facet_ids = {f for _e, f in DEMO_FACETS}
        self.assertEqual({evidence.operator_facet(self.cfg, op, target) for op, target in plan} - facet_ids, set())
        self.assertEqual(evidence.operator_facet(self.cfg, "closure_join_swap", "firm_open"),
                         "has_component_closure+m_firm_open")
        self.assertEqual(evidence.operator_facet(self.cfg, "closure_end_swap", "has_component"),
                         "has_component_descendant.*")
        res = self.verify(closure_cubes=False)
        for fid in ("has_component_closure.qty", "has_component_ancestor.*", "has_component_closure+m_firm_open"):
            self.assertEqual(self.status(res, fid), ("gap", "not_in_model"), fid)
        self.assertEqual(self.status(res, "part+m_firm_open"), ("gap", "no_gold_reads"))
        self.assertEqual(self.status(res, "part.part_class"), ("compared", ""))

    def test_clean_run_names_the_two_gaps(self):
        res = self.verify()
        self.assertEqual(res.evidence["scope"], "full")
        self.assertEqual(res.evidence["facets_reason"], "")
        self.assertEqual([(f["element"], f["facet"]) for f in res.evidence["facets"]], DEMO_FACETS)
        gaps = [(f["facet"], f["reason"]) for f in res.evidence["facets"] if f["status"] != "compared"]
        self.assertEqual(gaps, [("part+m_firm_open", "no_gold_reads"), ("m_firm_open_dated.ts", "filter_only")])
        self.assertEqual(self.lines(res, "measures.", "hierarchies.", "ontology."), [
            "measures.firm_open: no authored evidence for part+m_firm_open (no_gold_reads)" + N4_TAIL,
            "measures.firm_open_dated: no authored evidence for m_firm_open_dated.ts (filter_only: q05_firm_window)"
            + N4_TAIL])
        self.assertEqual(self.facet(res, "has_component_ancestor.*")["compared_by"], ["q02_where_used"])
        self.assertEqual(self.facet(res, "has_component_closure.max_depth")["compared_by"],
                         ["q01_explosion", "q02_where_used", "q04_depth_histogram"])
        self.assertEqual((res.evidence["zero_row_checks"], res.evidence["empty_stratum_zero_row_checks"],
                          res.evidence["gold_no_rows"]), ([], [], []))

    def test_facet_reasons(self):
        # gated reader: every reader of the closure did not run
        res = self.verify(closure_status="fail")
        self.assertEqual(self.status(res, "has_component_closure.qty"), ("gap", "gold_not_run"))
        self.assertEqual(self.status(res, "m_firm_open.value"), ("gap", "gold_not_run"))
        self.assertIn("hierarchies.has_component: no authored evidence for has_component_closure.qty (gold_not_run: "
                      "q01_explosion, q02_where_used), ", self.lines(res, "hierarchies.")[0])

    def test_reader_with_zero_rows_and_reader_without_values(self):
        self.rows["q04_depth_histogram"] = []
        res = self.verify()
        self.assertEqual(self.status(res, "has_component_closure.pairs"), ("gap", "no_rows_compared"))
        self.assertEqual(self.status(res, "has_component_closure.max_depth"), ("compared", ""))
        self.assertIn("hierarchies.has_component: no authored evidence for has_component_closure.pairs "
                      "(no_rows_compared: q04_depth_histogram)" + N4_TAIL, res.not_proven)
        shutil.rmtree(self.out)
        self.rows["q04_depth_histogram"] = [{"max_depth": 1, "pairs": 0}, {"max_depth": 2, "pairs": None}]
        res = self.verify()
        self.assertEqual(self.status(res, "has_component_closure.pairs"), ("gap", "no_values"))
        self.assertEqual(self.gold(res, "q04_depth_histogram")["value_rows"],
                         {"has_component_closure.max_depth": 4, "has_component_closure.pairs": 0})

    def test_detected_and_accepted(self):
        kill = ("v4:dated_shift_one_day:firm_open_dated", "pass", "measures.firm_open_dated",
                {"witness": 3, "killed_by": "v1:q05_firm_window", "detection": "authored"})
        self.mutants = ([kill], [])
        self.cfg.verify.accepted_gaps = ["m_firm_open_dated.ts", "measures.on_hand"]
        res = self.verify()
        self.assertEqual(self.facet(res, "m_firm_open_dated.ts"),
                         {"element": "measures.firm_open_dated", "facet": "m_firm_open_dated.ts", "status": "detected",
                          "reason": "", "compared_by": [], "uses_only_by": ["q05_firm_window"],
                          "detected_by": ["v4:dated_shift_one_day:firm_open_dated"]})
        self.assertEqual(self.status(res, "m_on_hand.value"), ("compared", ""), "accepted only replaces a gap")
        self.assertEqual(self.lines(res, "measures.firm_open_dated", "measures.on_hand"), [])
        for accepted in (["measures.firm_open"], ["part+m_firm_open"]):
            with self.subTest(accepted=accepted):
                shutil.rmtree(self.out)
                self.mutants = ([], [])
                self.cfg.verify.accepted_gaps = accepted
                res = self.verify()
                self.assertEqual(self.status(res, "part+m_firm_open"), ("accepted", ""))
                self.assertEqual(self.lines(res, "measures.firm_open:"), [
                    "measures.firm_open: no authored evidence for part+m_firm_open; accepted by "
                    "verify.accepted_gaps, so this is not proven"])

    def test_a_generated_kill_does_not_detect(self):
        kill = ("v4:dated_shift_one_day:firm_open_dated", "pass", "measures.firm_open_dated",
                {"witness": 3, "killed_by": "v3:total:m_firm_open_dated.value", "detection": "generated_only"})
        self.mutants = ([kill], [])
        res = self.verify()
        self.assertEqual(self.status(res, "m_firm_open_dated.ts"), ("gap", "filter_only"))

    def test_facets_full_scope_only(self):
        for only, gold in ((["v1"], None), (["v1", "v2", "v3", "v4"], ["q01_explosion"]), (["v1", "v2", "v3"], None)):
            with self.subTest(only=only, gold=gold):
                shutil.rmtree(self.out, ignore_errors=True)
                res = self.verify(only=only, gold=gold)
                self.assertEqual(res.evidence["scope"], "narrowed")
                self.assertIsNone(res.evidence["facets"])
                self.assertEqual(res.evidence["facets_reason"], "narrowed")
                self.assertEqual(self.lines(res, "measures.", "hierarchies.", "ontology."), [])

    def test_accepted_facet_lines_are_cut_at_ten(self):
        self.assertEqual(stage_verify._cut([f"g{i}" for i in range(3)]), "g0, g1, g2")
        self.assertEqual(stage_verify._cut([f"g{i}" for i in range(12)]),
                         "g0, g1, g2, g3, g4, g5, g6, g7, g8, g9, and 2 more")


class ZeroRowTest(VerifyRun):
    N3 = ("gold:q05_firm_window: every comparison that ran ({n}) had zero rows on both sides; {ids} pass without "
          "comparing any row; add a binding, stratum or parameter set with rows")

    def test_gold_zero_rows(self):
        self.rows["q05_firm_window"] = []
        res = self.verify()
        self.assertEqual(self.lines(res, "gold:"), [
            self.N3.format(n=7, ids="v1:q05_firm_window and v2:q05_firm_window:*")])
        self.assertEqual(res.evidence["gold_no_rows"], ["q05_firm_window"])
        self.assertEqual(self.status(res, "m_firm_open_dated.value"), ("gap", "no_rows_compared"))

    def test_one_binding_with_rows_clears_the_gold(self):
        self.rows["q05_firm_window"] = lambda p: None if p["date_from"] == "2030-01-01" else []
        res = self.verify()
        self.assertEqual(self.lines(res, "gold:"), [])
        self.assertEqual(res.evidence["gold_no_rows"], [])
        self.assertEqual(self.gold(res, "q05_firm_window")["nonempty_comparisons"], 1)

    def test_an_errored_binding_is_not_a_comparison(self):
        self.rows["q05_firm_window"] = lambda p: "error" if p["date_from"] == "2030-01-01" else []
        res = self.verify()
        self.assertEqual(self.lines(res, "gold:"), [
            self.N3.format(n=6, ids="v1:q05_firm_window and v2:q05_firm_window:*")])
        shutil.rmtree(self.out)
        self.rows["q05_firm_window"] = lambda p: "error" if p["date_to"] == "2026-03-31" else []
        res = self.verify()
        self.assertEqual(self.lines(res, "gold:"), [self.N3.format(n=5, ids="v2:q05_firm_window:*")],
                         "only the parts that ran are named")
        self.assertEqual(self.gold(res, "q05_firm_window")["comparisons"], 5)

    def test_gold_without_bindings_keeps_only_its_no_binding_line(self):
        self.bindings["q03_stock"] = [("values", None)]
        res = self.verify()
        self.assertEqual(self.lines(res, "gold:", "v1:q03_stock", "v2:q03_stock"), [
            "v1:q03_stock: no binding (every stratum is empty)",
            "v2:q03_stock:values: empty stratum (no node id qualifies)"])
        self.assertEqual(self.gold(res, "q03_stock")["comparisons"], 0)
        self.assertEqual(self.status(res, "m_on_hand.value"), ("gap", "gold_not_run"))
        self.assertEqual(res.evidence["zero_row_checks"], ["v2:q03_stock:values"])

    def test_zero_row_checks(self):
        self.bindings["q01_explosion"] = [("top", None), ("mid", {"root": "m1"}), ("deep", {"root": "d1"}),
                                          ("leaf_parent", {"root": "l1"}), ("empty", {"root": "e1"}),
                                          ("empty", {"root": "e2"})]
        self.rows["q01_explosion"] = lambda p: [] if p["root"] in ("d1", "e1", "e2") else None
        self.rows["q04_depth_histogram"] = []
        self.rows["q05_firm_window"] = lambda p: [] if p["date_to"] == "2026-03-31" else None
        res = self.verify()
        self.assertEqual(res.evidence["zero_row_checks"],
                         ["v1:q04_depth_histogram", "v1:q05_firm_window", "v2:q01_explosion:top",
                          "v2:q01_explosion:deep", "v2:q04_depth_histogram:all"])
        self.assertEqual(res.evidence["empty_stratum_zero_row_checks"], ["v2:q01_explosion:empty"])
        self.assertEqual(res.evidence["gold_no_rows"], ["q04_depth_histogram"])
        self.assertNotIn("v2:q05_firm_window:param_sets", res.evidence["zero_row_checks"], "a mixed stratum")


class EvidenceObjectTest(VerifyRun):
    def test_evidence_only_in_verify_results(self):
        build = Result("build")
        build.add("closure:has_component", "pass", "hierarchies.has_component")
        self.assertNotIn("evidence", build.to_dict())
        res = self.verify()
        d = res.to_dict()
        self.assertEqual({k: d["evidence"][k] for k in ("schema_version", "rule_version", "mode", "scope")},
                         {"schema_version": 1, "rule_version": evidence.RULE_VERSION, "mode": "disclose",
                          "scope": "full"})
        self.assertLessEqual({"schema_version", "rule_version", "mode", "scope", "gold", "facets", "facets_reason",
                              "zero_row_checks", "empty_stratum_zero_row_checks", "gold_no_rows"}, set(d["evidence"]))
        written = json.loads(res.write(self.out).read_text(encoding="utf-8"))
        self.assertEqual(written["evidence"], json.loads(json.dumps(d["evidence"])))
        self.assertNotIn("value_rows", json.dumps([c.detail for c in res.checks]), "check details stay as they were")

    def test_privacy(self):
        self.gold_file("q03_stock", f"-- {SECRET_TEXT}\nSELECT '{SECRET_TEXT}' FROM sxc_demo.stock t WHERE " + ON_HAND)
        self.bindings["q01_explosion"] = [(s, {"root": f"{SECRET_PARAM}-{s}"}) for s in
                                          ("top", "mid", "deep", "leaf_parent", "empty")]
        self.rows["q01_explosion"] = lambda p: [{"part_id": SECRET_ROW, "part_class": SECRET_ROW, "min_depth": 1,
                                                 "max_depth": 1, "path_count": 1, "qty": 2}]
        self.rows["q02_where_used"] = []
        res = self.verify()
        text = json.dumps(res.to_dict(), ensure_ascii=False)
        for secret in (SECRET_ROW, SECRET_PARAM, SECRET_TEXT, "location_id", "sxc_demo", "MAIN"):
            self.assertNotIn(secret, text, secret)
        self.assertIn("filter:measures.on_hand", text)
        self.assertIn('"gold_no_rows": ["q02_where_used"]', text)


# ---- V4 attribution ----------------------------------------------------------------------------------
AGG_ON_HAND = "v4:measure_agg_to_max:on_hand"
AGG_REMEDY = ("a gold query that returns one row per key of measures.on_hand cannot see this change, because the "
              "measure cube holds one pre-aggregated row per key; add a gold query that aggregates measures.on_hand "
              "over more than one key")
FILTER_REMEDY = ("add a gold query, stratum or parameter set whose keys have source rows that the filter of {m} "
                 "excludes")
GENERIC_REMEDY = "add a gold query, stratum or parameter set that would detect it"
N5 = ("{cid}: killed by {item}, a check between generated layers; no replayed gold item detected this change to {ref} "
      "({readers}); {remedy}")
N5A = ("{cid}: killed only by {item}, a check between generated layers or of no known kind; accepted by "
       "verify.accepted_gaps, so this is not proven")
N6 = ("{cid}: killed by {item}, which no evidence rule classifies; no replayed gold item is shown to have detected this "
      "change to {ref}")


class SuiteOrderTest(VerifyRun):
    """The V4 suite runs authored gold items (and gold items of unknown kind) first, then gold items whose SQL holds
    generated text, then every other item; without a generated gold the order is the planned one."""

    def _run_mutants(self, cfg, out, conn, model, suite, closure=None, sampled_items=(), coverage=None):
        self.suite_ids = [iid for iid, _fn in suite.items]
        return self.mutants

    @staticmethod
    def ranks(ids: list, generated=()) -> list:
        return [2 if not iid.startswith(("v1:", "v2:")) else 1 if iid.split(":")[1] in generated else 0 for iid in ids]

    def test_suite_order_contract(self):
        self.verify()
        sorted_ids = self.suite_ids
        shutil.rmtree(self.out)
        with mock.patch.object(stage_verify.evidence, "order_suite", lambda items, golds, key=None: list(items)):
            self.verify()
        planned = self.suite_ids
        self.assertEqual(sorted_ids, planned, "without a generated gold the suite runs in the planned order")
        self.assertEqual(self.ranks(planned), sorted(self.ranks(planned)))
        self.assertIn("v1:q03_stock", planned)
        self.assertTrue(any(iid.startswith("v3:") for iid in planned))
        shutil.rmtree(self.out)
        self.gold_file("q03_stock", "SELECT t.part_id FROM sxc_demo.stock t WHERE " + ON_HAND)
        res = self.verify()
        self.assertEqual(self.gold(res, "q03_stock")["kind"], "generated")
        ordered = self.suite_ids
        self.assertEqual(sorted(ordered), sorted(planned), "the same items")
        ranks = self.ranks(ordered, generated=("q03_stock",))
        self.assertEqual(ranks, sorted(ranks), "authored gold items, then generated gold items, then v3 items")
        self.assertEqual(ranks.count(1), len([i for i in planned if i.startswith(("v1:q03_stock", "v2:q03_stock:"))]))
        self.assertLess(planned.index("v1:q03_stock"), planned.index("v1:q05_firm_window"))
        self.assertLess(ordered.index("v1:q05_firm_window"), ordered.index("v1:q03_stock"))

    def test_gated_golds_have_a_kind_and_no_suite_item(self):
        res = self.verify(closure_status="fail")
        self.assertEqual({g["id"]: g["kind"] for g in res.evidence["gold"]}, {g.id: "authored" for g in self.cfg.gold})
        self.assertEqual({iid.split(":")[1] for iid in self.suite_ids if iid.startswith(("v1:", "v2:"))},
                         {"q03_stock", "q05_firm_window"})

    def test_items_of_golds_unknown_to_the_plan_keep_rank_zero(self):
        model = self.write_stages()
        v = stage_verify._Verify(self.cfg, self.out, model, ["v4"])
        v.gold_kinds = {"g": "generated"}
        v.items = [(iid, iid.split("[")[0], lambda client: "pass")
                   for iid in ("v1:g", "v1:a", "v2:b:top[0]", "v3:total:x", "v2:g:all[0]")]
        v.baseline = {iid: "pass" for iid, _cid, _fn in v.items}
        with mock.patch.object(stage_verify.mutate, "run_mutants", self._run_mutants):
            v.v4(FakeConn())
        self.assertEqual(self.suite_ids, ["v1:a", "v2:b:top[0]", "v1:g", "v2:g:all[0]", "v3:total:x"])
        self.assertEqual([evidence.item_kind(iid, v.gold_kinds) for iid in self.suite_ids],
                         ["unclassified", "unclassified", "generated", "generated", "generated"])
        self.assertEqual(evidence.detection("v1:a", v.gold_kinds), "unattributed")


class MutantRun(VerifyRun):
    """_Verify.v4 with the real mutate.run_mutants and mutation_coverage.Suite: witnesses (0 for
    measure_normalization_drop, else 4) and Cube are stubbed; Cube's client is the name of the served mutant directory,
    so a suite item can answer per mutant."""

    def attribute(self, answers: dict, operators=("measure_agg_to_max",), closure_cubes: bool = True):
        self.cfg.verify.mutation_operators = list(operators)
        model = self.write_stages(closure_cubes=closure_cubes)
        v = stage_verify._Verify(self.cfg, self.out, model, ["v4"])
        for g in self.cfg.gold:
            v.note_gold(g)

        def item(iid):
            def fn(client):
                answer = answers[iid]
                return answer(client) if callable(answer) else answer
            return iid, iid, fn

        v.items = [item(iid) for iid in answers]
        v.baseline = dict.fromkeys(answers, "pass")

        @contextlib.contextmanager
        def serve(cfg, run_dir, log=None):
            yield Path(run_dir).name

        def witness(conn, cfg, mu):
            return 0 if mu.operator == "measure_normalization_drop" else 4

        with mock.patch.object(mutate, "witness", witness), mock.patch.object(mutate.cube_run, "serve", serve):
            v.v4(FakeConn())
        v.res.evidence = v.evidence_object()
        return v

    def ledger(self) -> list:
        return json.loads((self.out / "verify" / "mutation-coverage.json").read_text(encoding="utf-8"))["mutants"]

    @staticmethod
    def check(v, cid: str):
        return next(c for c in v.res.checks if c.id == cid)


class AttributionTest(MutantRun):
    def test_v4_pass_is_not_gold_detection(self):
        """F1 guard: a mutant killed by a v3 item passes, but its detection says no gold item detected it."""
        v = self.attribute({"v1:q03_stock": "pass", "v3:total:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["killed_by"], c.detail["detection"], c.detail["facet"],
                          c.detail["gold_comparing"]), ("pass", "v3:total:x", "generated_only", "m_on_hand.value",
                                                        ["q03_stock"]))
        self.assertEqual(list(c.detail)[:5], ["witness", "killed_by", "detection", "facet", "gold_comparing"])
        self.assertIn(N5.format(cid=AGG_ON_HAND, item="v3:total:x", ref="measures.on_hand",
                                readers="gold queries that compare m_on_hand.value: q03_stock", remedy=AGG_REMEDY),
                      v.res.not_proven)
        self.assertEqual(len(self.lines(v.res, "v4:measure_agg_to_max:")), 4, "one line per generated-only kill")
        self.assertEqual(self.check(v, "v4:measure_agg_to_max:firm_open").detail["gold_comparing"], ["q02_where_used"])
        self.assertEqual(evidence.v4_violations(v.res), [])
        self.assertEqual((v.res.evidence["mutation"]["killed_generated_only"], c.detail["summary"]["killed"]), (4, 4))
        for row in self.ledger():
            self.assertEqual(row["detection"], self.check(v, row["id"]).detail["detection"], row["id"])
            self.assertEqual([(i["id"], i["kind"]) for i in row["coverage"]["items"]],
                             [("v1:q03_stock", "authored"), ("v3:total:x", "generated")])
        self.assertEqual([(i["id"], i["kind"]) for i in c.detail["coverage"]["items"]],
                         [("v1:q03_stock", "authored"), ("v3:total:x", "generated")])

        v = self.attribute({"v1:q03_stock": "fail", "v3:total:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["killed_by"], c.detail["detection"]), ("pass", "v1:q03_stock", "authored"))
        self.assertEqual(self.lines(v.res, "v4:"), [], "an authored kill gets no line")
        self.assertEqual(v.res.evidence["mutation"]["killed_authored"], 4)
        self.assertEqual(evidence.v4_violations(v.res), [])

        v = self.attribute({"v1:q03_stock": "pass", "v3:total:x": "pass", "w9:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.detail["killed_by"], c.detail["detection"]), ("w9:x", "unattributed"))
        self.assertIn(N6.format(cid=AGG_ON_HAND, item="w9:x", ref="measures.on_hand"), v.res.not_proven)
        self.assertEqual([i["kind"] for i in c.detail["coverage"]["items"]], ["authored", "generated", "unclassified"])

        v = self.attribute({"v1:q03_stock": "pass", "v3:total:x": "pass"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["killed_by"], c.detail["detection"]), ("fail", "", "none"))
        self.assertEqual(self.lines(v.res, "v4:"), [])
        self.assertEqual(v.res.evidence["mutation"]["survived"], 4)

    def test_v4_violations(self):
        golds = [{"id": "g", "kind": "authored", "generated_text": []},
                 {"id": "h", "kind": "generated", "generated_text": ["output_schema"]}]

        def result(*details):
            return {"checks": [{"id": f"v4:measure_agg_to_max:m{i}", "status": "pass", "detail": d}
                               for i, d in enumerate(details)], "evidence": {"gold": golds}}

        ok = result({"killed_by": "v1:g", "detection": "authored"},
                    {"killed_by": "v2:h:all[0]", "detection": "generated_only"},
                    {"killed_by": "v3:total:x:chunk[1]", "detection": "generated_only"},
                    {"killed_by": "w9:x", "detection": "unattributed"},
                    {"killed_by": "", "detection": "none"}, {"witness": 1}, {"reason": "suite_error", "item": "v1:g"})
        self.assertEqual(evidence.v4_violations(ok), [])
        bad = result({"killed_by": "v3:total:x", "detection": "authored"},
                     {"killed_by": "v1:h", "detection": "authored"},
                     {"killed_by": "v1:g"},
                     {"killed_by": "v1:unknown", "detection": "authored"},
                     {"killed_by": "v1:g", "detection": "authored"})
        self.assertEqual(evidence.v4_violations(bad), [f"v4:measure_agg_to_max:m{i}" for i in range(4)])
        golds[0]["generated_text"] = ["output_schema"]
        self.assertEqual(evidence.v4_violations(result({"killed_by": "v1:g", "detection": "authored"})),
                         ["v4:measure_agg_to_max:m0"], "a recorded kind cannot raise a detection")
        self.assertEqual(evidence.v4_violations({"checks": [{"id": "v4:a:b", "detail": {"killed_by": "v1:g",
                                                                                         "detection": "authored"}}]}),
                         ["v4:a:b"], "without recorded gold kinds no gold item is authored")


class TotalsClient:
    """A Cube stand-in that answers {"measures": ["m_<m>.value"]} with the total of the served cube's own SQL."""

    def __init__(self, lite, model: dict):
        self.lite, self.cubes = lite, {c["name"]: c for c in model["cubes"]}

    def load(self, query):
        (member,) = query["measures"]
        v = self.lite.execute(f"SELECT sum(value) FROM ({self.cubes[member.split('.')[0]]['sql']})").fetchone()[0]
        return [{member: None if v is None else str(Decimal(str(v)))}]


class SqlConn:
    """A pg8000-like run() over SQLite that ignores the transaction statements of db.read_only."""

    def __init__(self, lite):
        self.lite = lite

    def run(self, sql, **params):
        if sql.startswith(("BEGIN", "SET LOCAL", "ROLLBACK")):
            return []
        return [list(r) for r in self.lite.execute(sql).fetchall()]


class F1TraceTest(VerifyRun):
    def test_f1_trace(self):
        """The evaluation's trace: the demo without q03_stock (the only gold that reads m_on_hand), dirty stock data
        and measure_normalization_drop. The real total test kills the mutant; no gold item reads m_on_hand."""
        self.cfg.gold = [g for g in self.cfg.gold if g.id != "q03_stock"]
        self.cfg.verify.mutation_operators = ["measure_normalization_drop"]
        model = self.write_stages()
        lite = sqlite3.connect(":memory:")
        self.addCleanup(lite.close)
        lite.execute("ATTACH DATABASE ':memory:' AS sxc_demo")
        lite.create_function("btrim", 1, lambda s: None if s is None else s.strip())
        lite.execute("CREATE TABLE sxc_demo.stock (part_id text, location_id text, qty numeric)")
        lite.executemany("INSERT INTO sxc_demo.stock VALUES (?, ?, ?)",
                         [("P1", "MAIN", 10), ("P1", " main", 5), ("P2", "SUB", 3), ("P2", "CONSIGN", 7)])
        conn = SqlConn(lite)
        v = stage_verify._Verify(self.cfg, self.out, model, list(stage_verify.PARTS))
        for g in self.cfg.gold:
            v.note_gold(g)
        tests = shape_q.tests(self.cfg, model)
        total = next(t for t in tests if t["id"] == "total:m_on_hand.value")
        self.assertEqual(v._test_fn(conn, total)(TotalsClient(lite, model)), "pass", "the model agrees with the SQL")
        v.items = [(f"v1:{g.id}", f"v1:{g.id}", lambda client: "pass") for g in self.cfg.gold]
        v.items += [(f"v3:{t['id']}", f"v3:{t['id']}", v._test_fn(conn, t) if t is total else (lambda client: "pass"))
                    for t in tests]
        v.baseline = {iid: "pass" for iid, _cid, _fn in v.items}

        @contextlib.contextmanager
        def serve(cfg, run_dir, log=None):
            yield TotalsClient(lite, json.loads((Path(run_dir) / "model.json").read_text(encoding="utf-8")))

        with mock.patch.object(mutate, "witness", return_value=2), mock.patch.object(mutate.cube_run, "serve", serve):
            v.v4(conn)
        v.facets()
        v.res.evidence = v.evidence_object()
        c = next(c for c in v.res.checks if c.id == "v4:measure_normalization_drop:on_hand")
        self.assertEqual((c.status, c.config_ref, c.detail["killed_by"], c.detail["detection"], c.detail["facet"],
                          c.detail["gold_comparing"]),
                         ("pass", "measures.on_hand", "v3:total:m_on_hand.value", "generated_only", "m_on_hand.value", []))
        executed = [(i["id"], i["status"], i["kind"]) for i in c.detail["coverage"]["items"] if i["status"] != "not_run"]
        self.assertEqual(executed[-1], ("v3:total:m_on_hand.value", "fail", "generated"))
        self.assertEqual({st for _iid, st, _kind in executed[:-1]}, {"pass"})
        self.assertEqual([iid for iid, _st, kind in executed if kind == "authored"],
                         [f"v1:{g.id}" for g in self.cfg.gold], "every replayed gold item ran and passed first")
        self.assertIn(N5.format(cid="v4:measure_normalization_drop:on_hand", item="v3:total:m_on_hand.value",
                                ref="measures.on_hand", readers="no authored gold query compares m_on_hand.value",
                                remedy=FILTER_REMEDY.format(m="measures.on_hand")), v.res.not_proven)
        self.assertIn("measures.on_hand: no authored evidence for m_on_hand.value (no_gold_reads), part+m_on_hand "
                      "(no_gold_reads)" + N4_TAIL, v.res.not_proven)
        self.assertEqual(evidence.v4_violations(v.res), [])


class MutantSplitTest(MutantRun):
    def test_mutant_split(self):
        def per_mutant(**statuses):
            return lambda client: statuses.get(client, "pass")

        self.cfg.verify.accepted_gaps = ["v4:measure_agg_to_max:firm_open_dated"]
        v = self.attribute({
            "v1:q03_stock": per_mutant(measure_filter_drop__firm_open="fail", measure_agg_to_max__firm_open="error"),
            "v3:total:x": per_mutant(measure_filter_drop__firm_open_dated="fail",
                                     measure_agg_to_max__firm_open_dated="fail"),
            "w9:x": per_mutant(measure_agg_to_max__on_hand="fail")},
            operators=("closure_depth_shift", "measure_filter_drop", "measure_normalization_drop",
                       "measure_agg_to_max"), closure_cubes=False)
        mutation = v.res.evidence["mutation"]
        self.assertEqual(mutation, {
            "operators_selected": 4,
            "operators_not_selected": ["closure_path_count_one", "closure_qty_single_path", "dated_shift_one_day",
                                       "closure_join_swap", "closure_end_swap"],
            "killed_authored": 1, "killed_generated_only": 1, "unattributed": 1, "accepted": 1, "survived": 1,
            "not_judged": 1, "equivalent": 2, "not_applicable": 1,
            "generated_only": ["v4:measure_filter_drop:firm_open_dated"]})
        self.assertEqual(list(mutation), ["operators_selected", "operators_not_selected", *evidence.MUTANT_BUCKETS,
                                          "generated_only"])
        ledger = self.ledger()
        self.assertEqual(sum(mutation[b] for b in evidence.MUTANT_BUCKETS), len(ledger), "every mutant in one bucket")
        self.assertEqual(sum(1 for c in v.res.checks if c.id.startswith("v4:")), 6, "no check for 3 of the 9")
        error = self.check(v, "v4:measure_agg_to_max:firm_open")
        self.assertEqual((error.status, error.detail["reason"]), ("error", "suite_error"))
        self.assertFalse({"detection", "facet", "gold_comparing"} & set(error.detail), "no killed_by key, no new key")
        self.assertNotIn("detection", next(row for row in ledger if row["id"] == error.id))
        self.assertEqual([i["kind"] for i in error.detail["coverage"]["items"]],
                         ["authored", "generated", "unclassified"])
        lines = self.lines(v.res, "v4:")
        self.assertEqual(lines[3:], [
            N5.format(cid="v4:measure_filter_drop:firm_open_dated", item="v3:total:x", ref="measures.firm_open_dated",
                      readers="gold queries that compare m_firm_open_dated.value: q05_firm_window",
                      remedy=FILTER_REMEDY.format(m="measures.firm_open_dated")),
            N5A.format(cid="v4:measure_agg_to_max:firm_open_dated", item="v3:total:x"),
            N6.format(cid=AGG_ON_HAND, item="w9:x", ref="measures.on_hand")])
        self.assertEqual(len(lines), 6, "the lines of run_mutants come first: one not applicable, two equivalent")
        self.assertEqual(evidence.v4_violations(v.res), [])

    def test_no_operator_selected(self):
        v = self.attribute({"v1:q03_stock": "pass"}, operators=())
        self.assertEqual(v.res.evidence["mutation"], {
            "operators_selected": 0, "operators_not_selected": list(mutate.OPERATORS),
            **dict.fromkeys(evidence.MUTANT_BUCKETS, 0), "generated_only": []})

    def test_unrun_ledger_items_have_kinds(self):
        model = self.write_stages()
        self.cfg.verify.mutation_operators = ["measure_normalization_drop"]
        v = stage_verify._Verify(self.cfg, self.out, model, ["v4"])
        v.note_gold(self.cfg.gold[2])
        v.items = [("v1:q03_stock", "v1:q03_stock", lambda client: "pass"),
                   ("v3:total:x", "v3:total:x", lambda client: "pass")]
        v.baseline = {"v1:q03_stock": "fail", "v3:total:x": "error"}
        v.v4(FakeConn())
        ledger = self.ledger()
        self.assertEqual(len(ledger), 2)
        for row in ledger:
            self.assertEqual((row["outcome"], [(i["id"], i["kind"]) for i in row["coverage"]["items"]]),
                             ("not_run", [("v1:q03_stock", "authored"), ("v3:total:x", "generated")]))
            self.assertNotIn("detection", row)
        mutation = v.evidence_object()["mutation"]
        self.assertEqual((mutation["not_judged"], mutation["operators_selected"]), (2, 1))


class AttributionHelpersTest(unittest.TestCase):
    def test_n5_remedy(self):
        self.assertEqual(evidence.remedy("measure_agg_to_max", "on_hand"), AGG_REMEDY)
        for op in ("measure_filter_drop", "measure_normalization_drop"):
            self.assertEqual(evidence.remedy(op, "on_hand"), FILTER_REMEDY.format(m="measures.on_hand"), op)
        self.assertEqual(evidence.remedy("dated_shift_one_day", "firm_open_dated"),
                         "add a parameter set whose date range starts or ends on a date with rows of "
                         "measures.firm_open_dated")
        for op in ("closure_depth_shift", "closure_path_count_one", "closure_qty_single_path", "closure_join_swap",
                   "closure_end_swap", "not_an_operator"):
            self.assertEqual(evidence.remedy(op, "x"), GENERIC_REMEDY, op)

    def test_detection(self):
        golds = {"a": "authored", "g": "generated"}
        for killer, want in (("v1:a", "authored"), ("v2:a:top[0]", "authored"), ("v1:g", "generated_only"),
                             ("v3:total:m_x.value", "generated_only"), ("v3:pair:x:chunk[2]", "generated_only"),
                             ("v1:z", "unattributed"), ("w9:x", "unattributed"), ("v3w:pair_witness:x", "unattributed"),
                             ("", "none")):
            with self.subTest(killer=killer):
                self.assertEqual(evidence.detection(killer, golds), want)
        self.assertEqual(evidence.DETECTIONS, ("authored", "generated_only", "unattributed", "none"))


if __name__ == "__main__":
    unittest.main()
