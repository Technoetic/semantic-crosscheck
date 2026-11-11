"""verify.gold_observation = "require": what verify discloses about authored evidence becomes check statuses (opt-in).

Why: in the default mode ("disclose") a mutant that only a check between generated layers killed still passes, a
facet no authored gold query compares is only a not_proven line, and a gold query whose every comparison had no row
still passes v1. A user who wants those to stop a run sets verify.gold_observation = "require": a generated-only or
unattributed kill fails (or is not judged when gold items were left out of the suite), every measure, hierarchy and
ontology.class_column gets a gold_evidence:<element> check on full-scope runs, a passing v1 of a zero-row gold query is
not_run, and operators left out of verify.mutation_operators are named. No rewrite makes a status less severe: an
error stays an error, a fail stays a fail. Without the key, or with "disclose", every output stays as it was before
require mode existed; DiscloseIsUnchangedTest pins that byte for byte.
"""

import contextlib
import hashlib
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import config, evidence, mutate, stage_verify
from semantic_crosscheck.result import Result
from test_compare_unit import FakeConn
from test_verify_evidence import AGG_ON_HAND, N4_TAIL, N5A, ON_HAND, MutantRun, VerifyRun

ELEMENTS = ["measures.firm_open", "measures.firm_open_dated", "measures.on_hand", "measures.consigned",
            "hierarchies.has_component", "ontology.class_column"]
AGG_FIRM_OPEN = "v4:measure_agg_to_max:firm_open"
AGG_REMEDY = ("a gold query that returns one row per key of measures.{m} cannot see this change, because the measure "
              "cube holds one pre-aggregated row per key; add a gold query that aggregates measures.{m} over more "
              "than one key")
N5B = ("{cid}: killed only by {item}, a check between generated layers; gold items were left out ({excluded}), which "
       "might have detected it; not judged")
N5B_UNATTRIBUTED = ("{cid}: killed only by {item}, which no evidence rule classifies; gold items were left out "
                    "({excluded}), which might have detected it; not judged")
N7 = ("verify.mutation_operators leaves out {ops}; require mode judges only the selected operators, so changes of "
      "those kinds are not proven to be detected")
KILL_ACTION = ("{cid}: only {item}, a check between generated layers, detected the mutant; no replayed gold item did "
               "({readers}); {remedy}, or list {cid} in verify.accepted_gaps, then rerun verify (do not drop the "
               "operator)")
UNATTRIBUTED_ACTION = ("{cid}: only {item}, which no evidence rule classifies, detected the mutant; no replayed gold "
                       "item is shown to have detected it ({readers}); {remedy}, or list {cid} in "
                       "verify.accepted_gaps, then rerun verify (do not drop the operator)")
INCOMPLETE_KILL_ACTION = ("{cid}: killed only by {item}, a check between generated layers, while gold items were left "
                          "out ({excluded}): make every gold query and closure run (full verify, passing builds), then "
                          "rerun verify")
GAP_ACTION = ("{cid}: no authored evidence for {facets}; add a gold query that maps them and compares rows, or list "
              "them in verify.accepted_gaps, then rerun verify")
NOT_RUN_ACTION = ("{cid}: the gold queries that compare {facets} did not run, or the model lacks their cube; fix their "
                  "errors or gates (rerun build and cube for a missing closure cube), then rerun verify")
NO_VALUES_ACTION = ("{cid}: no authored evidence for {facets}: compared rows held no value of them (NULL, 0 or empty); "
                    "add a binding or parameter set whose rows hold values, then rerun verify")
ZERO_ROWS_ACTION = ("v1:{g}: every comparison of this gold query had zero rows on both sides; check its parameters, "
                    "strata and the data, then rerun verify")
SEVERITY = {"pass": 0, "not_run": 1, "fail": 2, "error": 3}


def set_mode(run: VerifyRun, value: str | None):
    """Write verify.gold_observation into the run's copy of the demo configuration (None: no key) and reload it."""
    raw = pgfixture.DEMO_CONFIG.read_bytes()
    if value is not None:
        line = f'mutant_bindings = 3\ngold_observation = "{value}"\n'.encode("utf-8")
        raw = raw.replace(b"mutant_bindings = 3\n", line, 1)
        assert line in raw
    (run.tmp / "crosscheck.toml").write_bytes(raw)
    run.cfg = config.load(run.tmp / "crosscheck.toml")


def kill(operator: str, target: str, killer: str) -> tuple:
    """A V4 check of a measure operator as mutate.run_mutants returns it for a killed mutant."""
    return f"v4:{operator}:{target}", "pass", f"measures.{target}", {"witness": 4, "killed_by": killer}


class Judge(MutantRun):
    """_Verify.v4 with the real mutate.run_mutants (as MutantRun.attribute), optionally with suite items left out, and
    the next action of the result."""

    def judge(self, answers: dict, operators=("measure_agg_to_max",), excluded=()):
        self.cfg.verify.mutation_operators = list(operators)
        model = self.write_stages()
        v = stage_verify._Verify(self.cfg, self.out, model, ["v4"])
        v.excluded |= set(excluded)
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
        v.res.next_safe_action = stage_verify._next_action(v.res)
        return v


def per_mutant(default: str = "pass", **statuses):
    return lambda client: statuses.get(client, default)


# ---- V4 ---------------------------------------------------------------------------------------------
class RequireV4Test(Judge):
    def setUp(self):
        super().setUp()
        set_mode(self, "require")

    def test_generated_only_kill_fails(self):
        v = self.judge({"v1:q03_stock": "pass", "v3:total:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["reason"], c.detail["killed_by"], c.detail["detection"]),
                         ("fail", "generated_only", "v3:total:x", "generated_only"), "killed_by keeps its meaning")
        self.assertEqual({k: c.detail["summary"][k] for k in ("killed", "survived")}, {"killed": 0, "survived": 4},
                         "a require-mode generated_only counts as survived")
        for row in self.ledger():
            self.assertEqual((row["outcome"], row["reason"], row["killed_by"], row["detection"]),
                             ("survived", "generated_only", "v3:total:x", "generated_only"), row["id"])
            self.assertEqual(self.check(v, row["id"]).status, "fail")
        self.assertEqual(self.lines(v.res, "v4:"), [], "the status says it; no N5 line")
        mutation = v.res.evidence["mutation"]
        self.assertEqual((mutation["killed_generated_only"], mutation["survived"]), (4, 0))
        self.assertEqual(v.res.evidence["mode"], "require")
        self.assertEqual(evidence.v4_violations(v.res), [])
        self.assertEqual(v.res.next_safe_action, KILL_ACTION.format(
            cid=AGG_FIRM_OPEN, item="v3:total:x", readers="gold queries that compare m_firm_open.value: q02_where_used",
            remedy=AGG_REMEDY.format(m="firm_open")))

    def test_unattributed_kill_fails(self):
        v = self.judge({"v1:q03_stock": "pass", "v3:total:x": "pass", "w9:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["reason"], c.detail["detection"]),
                         ("fail", "unattributed", "unattributed"))
        self.assertEqual(self.lines(v.res, "v4:"), [])
        self.assertEqual(v.res.evidence["mutation"]["unattributed"], 4)
        self.assertEqual(v.res.next_safe_action, UNATTRIBUTED_ACTION.format(
            cid=AGG_FIRM_OPEN, item="w9:x", readers="gold queries that compare m_firm_open.value: q02_where_used",
            remedy=AGG_REMEDY.format(m="firm_open")))

    def test_authored_kill_and_accepted_kill_pass(self):
        v = self.judge({"v1:q03_stock": "fail", "v3:total:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["detection"], c.detail["summary"]["killed"]), ("pass", "authored", 4))
        self.assertNotIn("reason", c.detail)
        self.assertEqual(v.res.status, "pass")
        self.cfg.verify.accepted_gaps = [AGG_ON_HAND]
        v = self.judge({"v1:q03_stock": "pass", "v3:total:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["detection"]), ("pass", "generated_only"))
        self.assertNotIn("reason", c.detail)
        self.assertEqual(self.lines(v.res, "v4:"), [N5A.format(cid=AGG_ON_HAND, item="v3:total:x")])
        self.assertEqual(self.check(v, AGG_FIRM_OPEN).status, "fail", "only the listed check is accepted")
        self.assertEqual(c.detail["summary"]["killed"], 1)
        self.assertEqual((v.res.evidence["mutation"]["accepted"], v.res.evidence["mutation"]["killed_generated_only"]),
                         (1, 3))

    def test_suite_incomplete_takes_precedence(self):
        answers = {"v1:q03_stock": "pass", "v3:total:x": per_mutant(measure_agg_to_max__on_hand="fail"),
                   "w9:x": per_mutant(measure_agg_to_max__consigned="fail")}
        v = self.judge(answers, excluded=("gold_not_planned",))
        on_hand, consigned = self.check(v, AGG_ON_HAND), self.check(v, "v4:measure_agg_to_max:consigned")
        for c in (on_hand, consigned):
            self.assertEqual((c.status, c.detail["reason"], c.detail["excluded"]),
                             ("not_run", "suite_incomplete", ["gold_not_planned"]), c.id)
        self.assertEqual(on_hand.detail["killed_by"], "v3:total:x")
        self.assertEqual(self.check(v, AGG_FIRM_OPEN).detail["killed_by"], "", "a survivor")
        self.assertEqual({k: on_hand.detail["summary"][k] for k in ("killed", "survived", "not_run")},
                         {"killed": 0, "survived": 0, "not_run": 4})
        rows = {row["id"]: row for row in self.ledger()}
        self.assertEqual((rows[AGG_ON_HAND]["outcome"], rows[AGG_ON_HAND]["reason"], rows[AGG_ON_HAND]["excluded"]),
                         ("not_run", "suite_incomplete", ["gold_not_planned"]))
        survivor = ("v4:measure_agg_to_max:{t}: survived a suite with items left out (gold_not_planned), which might "
                    "have killed it; not judged")
        self.assertEqual(self.lines(v.res, "v4:"), [
            survivor.format(t="firm_open"), survivor.format(t="firm_open_dated"),
            N5B.format(cid=AGG_ON_HAND, item="v3:total:x", excluded="gold_not_planned"),
            N5B_UNATTRIBUTED.format(cid="v4:measure_agg_to_max:consigned", item="w9:x", excluded="gold_not_planned")],
            "a killed mutant never gets the survivor text")
        self.assertEqual(v.res.evidence["mutation"]["not_judged"], 4)
        self.assertEqual(evidence.v4_violations(v.res), [])
        self.assertIn("survived a suite with items left out", v.res.next_safe_action, "the first is a survivor")
        v = self.judge({"v1:q03_stock": "pass", "v3:total:x": "fail"}, excluded=("closure_not_passed",))
        self.assertEqual(v.res.next_safe_action, INCOMPLETE_KILL_ACTION.format(
            cid=AGG_FIRM_OPEN, item="v3:total:x", excluded="closure_not_passed"))

    def test_an_error_is_never_rewritten(self):
        v = self.judge({"v1:q03_stock": "error", "v3:total:x": "fail"})
        c = self.check(v, AGG_ON_HAND)
        self.assertEqual((c.status, c.detail["reason"]), ("error", "suite_error"))
        self.assertNotIn("detection", c.detail)
        self.assertEqual(v.res.evidence["mutation"]["not_judged"], 4)

    def test_no_rewrite_lowers_a_status(self):
        answers = [{"v1:q03_stock": "pass", "v3:total:x": "fail"},
                   {"v1:q03_stock": "pass", "v3:total:x": "pass", "w9:x": "fail"},
                   {"v1:q03_stock": per_mutant(measure_agg_to_max__on_hand="fail"),
                    "v3:total:x": per_mutant("fail", measure_agg_to_max__consigned="pass"),
                    "w9:x": per_mutant(measure_agg_to_max__firm_open="error")}]
        for i, a in enumerate(answers):
            for excluded in ((), ("excluded_by_gold",)):
                with self.subTest(answers=i, excluded=excluded):
                    shutil.rmtree(self.out, ignore_errors=True)
                    set_mode(self, "disclose")
                    disclosed = {c.id: c.status for c in self.judge(a, excluded=excluded).res.checks}
                    shutil.rmtree(self.out, ignore_errors=True)
                    set_mode(self, "require")
                    required = {c.id: c.status for c in self.judge(a, excluded=excluded).res.checks}
                    self.assertEqual(set(required), set(disclosed))
                    for cid, st in required.items():
                        self.assertTrue(st == disclosed[cid] or disclosed[cid] == "pass", (cid, disclosed[cid], st))
                        self.assertGreaterEqual(SEVERITY[st], SEVERITY[disclosed[cid]])


# ---- gold_evidence:<element>, N7 and v1 ---------------------------------------------------------------------
class RequireRun(VerifyRun):
    def setUp(self):
        super().setUp()
        set_mode(self, "require")

    @staticmethod
    def evidence_checks(res: Result) -> list:
        return [c for c in res.checks if c.id.startswith("gold_evidence:")]


class GoldEvidenceTest(RequireRun):
    def test_clean_demo(self):
        self.mutants = ([kill("measure_agg_to_max", "on_hand", "v1:q03_stock")], [])
        res = self.verify()
        checks = self.evidence_checks(res)
        self.assertEqual([c.id for c in checks], [f"gold_evidence:{e}" for e in ELEMENTS])
        self.assertEqual([c.config_ref for c in checks], ELEMENTS)
        ids = [c.id for c in res.checks]
        self.assertEqual(ids[-len(ELEMENTS):], [c.id for c in checks], "after every other check, V4 included")
        self.assertLess(ids.index(AGG_ON_HAND), ids.index(checks[0].id))
        self.assertEqual([(c.status, c.detail.get("reason")) for c in checks],
                         [("fail", "no_gold_reads"), ("fail", "filter_only")] + [("pass", None)] * 4)
        self.assertEqual(checks[0].detail, {"reason": "no_gold_reads", "facets": 3, "with_evidence": 2, "accepted": 0,
                                            "gaps": 1, "operators_not_selected": []})
        self.assertEqual(checks[4].detail, {"facets": 7, "with_evidence": 7, "accepted": 0, "gaps": 0,
                                            "operators_not_selected": []})
        self.assertEqual(self.lines(res, "measures.", "hierarchies.", "ontology.", "verify.mutation_operators"), [],
                         "no N4 line in require mode; every operator is selected")
        self.assertEqual(res.evidence["mode"], "require")
        self.assertEqual(res.status, "fail")
        self.assertEqual(res.next_safe_action, GAP_ACTION.format(cid=checks[0].id, facets="part+m_firm_open"))
        self.assertEqual(evidence.check_kind("verify", checks[0].id, {}), "structural")

    def test_accepted_gaps_pass(self):
        self.cfg.verify.accepted_gaps = ["measures.firm_open", "m_firm_open_dated.ts"]
        res = self.verify()
        checks = self.evidence_checks(res)
        self.assertEqual({c.status for c in checks}, {"pass"})
        self.assertEqual((checks[0].detail["accepted"], checks[0].detail["gaps"], checks[1].detail["accepted"]),
                         (1, 0, 1))
        self.assertEqual(self.lines(res, "measures."), [
            "measures.firm_open: no authored evidence for part+m_firm_open; accepted by verify.accepted_gaps, so this "
            "is not proven",
            "measures.firm_open_dated: no authored evidence for m_firm_open_dated.ts; accepted by "
            "verify.accepted_gaps, so this is not proven"])
        self.assertEqual(res.status, "pass")

    def test_not_run_reasons(self):
        res = self.verify(closure_status="fail")
        checks = {c.config_ref: c for c in self.evidence_checks(res)}
        hierarchy = checks["hierarchies.has_component"]
        self.assertEqual({f["reason"] for f in res.evidence["facets"] if f["element"] == "hierarchies.has_component"},
                         {"gold_not_run"})
        self.assertEqual((hierarchy.status, hierarchy.detail["reason"]), ("not_run", "gold_not_run"))
        self.assertEqual([f["reason"] for f in res.evidence["facets"] if f["element"] == "measures.firm_open"],
                         ["gold_not_run", "gold_not_run", "no_gold_reads"])
        self.assertEqual((checks["measures.firm_open"].status, checks["measures.firm_open"].detail["reason"]),
                         ("fail", "no_gold_reads"), "the first gap that is not gold_not_run or not_in_model")
        shutil.rmtree(self.out)
        res = self.verify(closure_cubes=False)
        hierarchy = next(c for c in self.evidence_checks(res) if c.config_ref == "hierarchies.has_component")
        self.assertEqual((hierarchy.status, hierarchy.detail["reason"], hierarchy.detail["gaps"]),
                         ("not_run", "not_in_model", 7))

    def test_no_values(self):
        self.cfg.verify.accepted_gaps = ["measures.firm_open", "measures.firm_open_dated"]
        self.rows["q04_depth_histogram"] = [{"max_depth": 1, "pairs": 0}]
        res = self.verify()
        c = next(c for c in self.evidence_checks(res) if c.config_ref == "hierarchies.has_component")
        self.assertEqual((c.status, c.detail["reason"], c.detail["gaps"]), ("fail", "no_values", 1))
        self.assertEqual(res.next_safe_action, NO_VALUES_ACTION.format(cid=c.id, facets="has_component_closure.pairs"))

    def test_next_actions(self):
        res = Result("verify")
        res.add("gold_evidence:hierarchies.has_component", "not_run", "hierarchies.has_component",
                reason="gold_not_run", facets=7, with_evidence=5, accepted=0, gaps=2, operators_not_selected=[])
        res.evidence = {"facets": [
            {"element": "hierarchies.has_component", "facet": "has_component_closure.qty", "status": "gap",
             "reason": "gold_not_run"},
            {"element": "hierarchies.has_component", "facet": "has_component_closure.pairs", "status": "compared",
             "reason": ""},
            {"element": "hierarchies.has_component", "facet": "has_component_ancestor.*", "status": "gap",
             "reason": "not_in_model"},
            {"element": "measures.on_hand", "facet": "m_on_hand.value", "status": "gap", "reason": "gold_not_run"}]}
        self.assertEqual(stage_verify._next_action(res), NOT_RUN_ACTION.format(
            cid="gold_evidence:hierarchies.has_component",
            facets="has_component_closure.qty, has_component_ancestor.*"))
        res = Result("verify")
        res.add("gold_evidence:measures.on_hand", "fail", "measures.on_hand", reason="filter_only", facets=2,
                with_evidence=0, accepted=0, gaps=2, operators_not_selected=[])
        res.evidence = {"facets": [
            {"element": "measures.on_hand", "facet": "m_on_hand.value", "status": "gap", "reason": "gold_not_run"},
            {"element": "measures.on_hand", "facet": "part+m_on_hand", "status": "gap", "reason": "filter_only"}]}
        self.assertEqual(stage_verify._next_action(res), GAP_ACTION.format(cid="gold_evidence:measures.on_hand",
                                                                             facets="part+m_on_hand"))

    def test_full_scope_only(self):
        for only, gold in ((["v1"], None), (["v1", "v2", "v3", "v4"], ["q01_explosion"])):
            with self.subTest(only=only, gold=gold):
                shutil.rmtree(self.out, ignore_errors=True)
                res = self.verify(only=only, gold=gold)
                self.assertEqual(self.evidence_checks(res), [])
                self.assertEqual(res.evidence["scope"], "narrowed")

    def test_operators_not_selected(self):
        left_out = [op for op in mutate.OPERATORS if op not in ("measure_agg_to_max", "dated_shift_one_day")]
        self.cfg.verify.mutation_operators = ["measure_agg_to_max", "dated_shift_one_day"]
        self.cfg.verify.accepted_gaps = ["measures.firm_open"]
        self.mutants = ([kill("measure_agg_to_max", "on_hand", "v1:q03_stock")],
                        ["v4:dated_shift_one_day:firm_open_dated equivalent on this data"])
        res = self.verify()
        line = N7.format(ops=", ".join(left_out))
        self.assertIn(line, res.not_proven)
        lines = res.not_proven
        equivalent = lines.index("v4:dated_shift_one_day:firm_open_dated equivalent on this data")
        self.assertLess(equivalent, lines.index(line), "N7 after the V4 lines")
        self.assertEqual(lines[lines.index(line) + 1:], [
            "measures.firm_open: no authored evidence for part+m_firm_open; accepted by verify.accepted_gaps, so this "
            "is not proven"], "N7, then N4a last")
        for c in self.evidence_checks(res):
            self.assertEqual(c.detail["operators_not_selected"], left_out, c.id)
        self.assertEqual(res.evidence["mutation"]["operators_not_selected"], left_out)
        set_mode(self, "disclose")
        self.cfg.verify.mutation_operators = ["measure_agg_to_max", "dated_shift_one_day"]
        shutil.rmtree(self.out)
        res = self.verify()
        self.assertNotIn(line, res.not_proven, "no N7 in disclose mode")
        self.assertEqual(self.evidence_checks(res), [])

    def test_privacy(self):
        self.rows["q01_explosion"] = lambda p: [{"part_id": "SECRET-ROW-VALUE", "part_class": "SECRET-ROW-VALUE",
                                                 "min_depth": 1, "max_depth": 1, "path_count": 1, "qty": 2}]
        self.rows["q05_firm_window"] = []
        res = self.verify()
        text = json.dumps(res.to_dict(), ensure_ascii=False) + sxc._summary(res, None, False)
        for secret in ("SECRET-ROW-VALUE", "2030-01-01", "location_id", "sxc_demo"):
            self.assertNotIn(secret, text, secret)


class RequireV1Test(RequireRun):
    first_q05 = None

    def _run_binding(self, conn, client, cfg, g, params, cache=None, types=None):
        if g.id == "q05_firm_window" and self.first_q05 is not None:
            first, self.first_q05 = self.first_q05, None
            return first
        return super()._run_binding(conn, client, cfg, g, params, cache, types)

    def test_zero_row_gold_v1_is_not_run(self):
        self.cfg.verify.accepted_gaps = ["measures.firm_open", "measures.firm_open_dated"]
        self.rows["q05_firm_window"] = []
        res = self.verify()
        v1 = next(c for c in res.checks if c.id == "v1:q05_firm_window")
        self.assertEqual((v1.status, v1.detail["reason"], v1.detail["empty"]), ("not_run", "no_rows_compared", True))
        self.assertEqual({c.status for c in res.checks if c.id.startswith("v2:q05_firm_window:")}, {"pass"},
                         "only v1 is rewritten")
        self.assertIn("gold:q05_firm_window: every comparison that ran (7) had zero rows on both sides; "
                      "v1:q05_firm_window and v2:q05_firm_window:* pass without comparing any row; add a binding, "
                      "stratum or parameter set with rows", res.not_proven)
        self.assertIn("v1:q05_firm_window", res.evidence["zero_row_checks"])
        self.assertEqual(res.status, "not_run")
        self.assertEqual(res.next_safe_action, ZERO_ROWS_ACTION.format(g="q05_firm_window"))
        set_mode(self, "disclose")
        shutil.rmtree(self.out)
        res = self.verify()
        self.assertEqual(next(c for c in res.checks if c.id == "v1:q05_firm_window").status, "pass")

    def test_error_and_fail_stay(self):
        self.rows["q05_firm_window"] = []
        self.first_q05 = {"pass": False, "side": "gold", "error": "DatabaseError", "sqlstate": "57014"}
        res = self.verify()
        v1 = next(c for c in res.checks if c.id == "v1:q05_firm_window")
        self.assertEqual((v1.status, v1.detail.get("reason")), ("error", None), "an error is never rewritten")
        self.assertEqual(self.lines(res, "gold:q05_firm_window"), [
            "gold:q05_firm_window: every comparison that ran (6) had zero rows on both sides; "
            "v2:q05_firm_window:* pass without comparing any row; add a binding, stratum or parameter set with rows"])
        shutil.rmtree(self.out)
        self.first_q05 = {"pass": False, "empty": True, "gold_rows": 0, "cube_rows": 0,
                          "value_rows": {"key": 0, "value": 0}}
        res = self.verify()
        v1 = next(c for c in res.checks if c.id == "v1:q05_firm_window")
        self.assertEqual((v1.status, v1.detail.get("reason")), ("fail", None), "a fail stays a fail")
        self.assertEqual(len(self.lines(res, "gold:q05_firm_window: every comparison that ran (7)")), 1, "N3 written")


# ---- disclose mode: unchanged --------------------------------------------------------------------------
# SHA-256 of each scenario's outputs (result.json without its two timestamps, verify/mutation-coverage.json and the
# console summary), captured with the engine before require mode existed (commit aa9b286, no gold_observation key).
# They may change only when disclose-mode output changes on purpose.
UNCHANGED = {
    "full_kills": "89491142786a78ebb0758a03a5b1e7542617c5af733b03bdabcfe2bc16ff80a0",
    "full_zero_rows": "1d2b814b0dd4fcb68eb7b114299dc828565ecb914b771580889c8322ef23c307",
    "full_operators": "86c632ec7b4aed145dda4d713e2b1d8a37633a0251160c0173c70f21de069515",
    "full_narrowed_gold": "e405e4374eaebf3e12e6668cbe461abfb2f03182753b72e6185a9d0521b69038",
    "full_gated": "595e2104aeea718bbed5d35cb16a925e3f86a12795cf6a6c47c51907c8d1e1b8",
    "full_generated_text": "ff78575a08d6fc911de961a583adfbfbbb9a0df261a4860fd7b0a7707248ac0b",
    "mutants_real": "3c96beecfd437e2278963f0086665156091d71d3c72a501c65149c0c9ffc0824",
    "mutants_excluded": "52cf59fd2b396b3d9d90f5aa99599bd622028aac2291a045ab823656a15beade",
}


class Scenarios(Judge):
    """Runs in which every require-mode rewrite would apply, so disclose mode must leave each of them unchanged."""

    def outputs(self, res: Result) -> str:
        self.last = res
        d = res.to_dict()
        del d["started_at"], d["finished_at"]
        coverage = self.out / "verify" / "mutation-coverage.json"
        ledger = coverage.read_bytes().decode("utf-8") if coverage.is_file() else ""
        return "\n".join([json.dumps(d, ensure_ascii=False, indent=1), ledger, sxc._summary(res, None, False)])

    def scenario(self, name: str) -> str:
        """The outputs of one scenario, from a fresh output directory and the demo's gold files."""
        shutil.rmtree(self.out, ignore_errors=True)
        shutil.rmtree(self.tmp / "gold")
        shutil.copytree(pgfixture.DEMO_CONFIG.parent / "gold", self.tmp / "gold")
        self.rows, self.bindings, self.mutants = {}, {}, ([], [])
        verify = self.verify
        if name == "full_kills":
            self.cfg.verify.accepted_gaps = [AGG_FIRM_OPEN]
            self.mutants = ([kill("measure_agg_to_max", "firm_open", "v3:total:m_firm_open.value"),
                             kill("measure_agg_to_max", "on_hand", "v3:total:m_on_hand.value"),
                             kill("measure_agg_to_max", "consigned", "w9:x"),
                             kill("dated_shift_one_day", "firm_open_dated", "v1:q05_firm_window")],
                            ["v4:measure_normalization_drop:on_hand equivalent on this data"])
            return self.outputs(verify())
        if name == "full_zero_rows":
            self.rows["q05_firm_window"] = []
            self.rows["q04_depth_histogram"] = [{"max_depth": 1, "pairs": 0}]
            return self.outputs(verify())
        if name == "full_operators":
            self.cfg.verify.mutation_operators = ["measure_agg_to_max"]
            self.mutants = ([kill("measure_agg_to_max", "on_hand", "v3:total:m_on_hand.value")], [])
            return self.outputs(verify())
        if name == "full_narrowed_gold":
            self.mutants = ([kill("measure_agg_to_max", "on_hand", "v3:total:m_on_hand.value")], [])
            return self.outputs(verify(gold=["q03_stock", "q05_firm_window"]))
        if name == "full_gated":
            self.mutants = ([kill("measure_agg_to_max", "on_hand", "v3:total:m_on_hand.value")], [])
            return self.outputs(verify(closure_status="fail"))
        if name == "full_generated_text":
            self.gold_file("q03_stock", "SELECT t.part_id FROM sxc_demo.stock t WHERE " + ON_HAND)
            return self.outputs(verify())
        # authored, generated, accepted and unattributed kills; with items left out also a survivor (never first:
        # a survivor's fail next action is not part of what this test pins)
        survivor = "measure_agg_to_max__on_hand" if name == "mutants_excluded" else ""
        answers = {"v1:q03_stock": per_mutant(measure_filter_drop__firm_open="fail"),
                   "v3:total:x": per_mutant("fail", measure_agg_to_max__consigned="pass", **{survivor: "pass"}),
                   "w9:x": per_mutant(measure_agg_to_max__consigned="fail")}
        self.cfg.verify.accepted_gaps = ["v4:measure_agg_to_max:firm_open_dated"]
        operators = ("measure_filter_drop", "measure_normalization_drop", "measure_agg_to_max")
        if name == "mutants_real":
            return self.outputs(self.judge(answers, operators).res)
        if name == "mutants_excluded":
            return self.outputs(self.judge(answers, operators, excluded=("excluded_by_gold",)).res)
        raise KeyError(name)


class DiscloseIsUnchangedTest(Scenarios):
    def test_disclose_equals_no_key_and_the_engine_before_require_mode(self):
        for name, digest in UNCHANGED.items():
            with self.subTest(scenario=name):
                set_mode(self, None)
                absent = self.scenario(name)
                set_mode(self, "disclose")
                disclose = self.scenario(name)
                self.assertEqual(disclose, absent)
                self.assertEqual(hashlib.sha256(absent.encode("utf-8")).hexdigest(), digest)
                rewritten = [c.id for c in self.last.checks if c.id.startswith("gold_evidence:") or c.detail.get(
                    "reason") in ("generated_only", "unattributed", "no_rows_compared")]
                self.assertEqual(rewritten, [])
                set_mode(self, "require")
                self.assertNotEqual(self.scenario(name), absent, "the scenario exercises require mode")

    def test_the_key_is_part_of_the_configuration_hash(self):
        from semantic_crosscheck.result import inputs_fingerprint
        hashes = []
        for value in (None, "disclose", "require"):
            set_mode(self, value)
            hashes.append(inputs_fingerprint(self.cfg)["config_sha256"])
        self.assertEqual(len(set(hashes)), 3, "changing the key makes earlier results stale")


if __name__ == "__main__":
    unittest.main()
