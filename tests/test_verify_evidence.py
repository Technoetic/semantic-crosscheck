"""What verify records about its gold queries and the facets they compare (the evidence object of verify results).

Why: a passing verify said nothing about which parts of the model a person's gold SQL actually compared with rows
that hold values. verify now records, per gold query, its kind (a gold whose text contains generated text is
generated evidence), the members it compares and the ones it only uses in filters, and how many of its comparisons
had rows; per facet of a measure, hierarchy or class dimension, whether authored evidence covers it or why not. These
tests run verify without a database or Cube (the planning, comparison and Cube functions are patched) and pin the
facet reasons, the zero-row lists, the new not_proven lines, the full-scope gate and that nothing of the gold text,
the rows or the parameters reaches the result.
"""

import argparse
import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import compare, config, cube_gen, evidence, mutate, stage_verify, sweep
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


if __name__ == "__main__":
    unittest.main()
