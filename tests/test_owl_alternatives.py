"""Bounded enumeration over synthetic monotone conflicts; no source values leave the oracle."""

import json
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # sets the local scripts import path
import sxc
from semantic_crosscheck import owl, owl_explain, stage_reason
from semantic_crosscheck.result import EXIT_CONFIG, Result, StageError
from test_owl_explain import HAVE_OWL, JAVA, SECRET, cfg_graph, options


class AlternativeExplainTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.graph = cfg_graph()
        self.ids = [group.id for group in owl.axiom_groups(self.cfg, self.graph)]

    def explain(self, conflicts, **budgets):
        calls = []
        def check(_cfg, _graph, _java, active, _deadline):
            calls.append(frozenset(active))
            return not any(core <= active for core in conflicts)
        with mock.patch.object(owl_explain, "_check", side_effect=check):
            got = owl_explain.explain(self.cfg, self.graph, "unused", **budgets)
        return got, calls

    def cores(self, got):
        return [frozenset(group["id"] for group in core) for core in got["explanations"]]

    def assert_verified(self, got, conflicts, calls):
        cores = self.cores(got)
        self.assertEqual(len(cores), len(set(cores)), "each core is emitted once")
        for core in cores:
            self.assertTrue(any(conflict <= core for conflict in conflicts))
            self.assertIn(core, calls, "sufficiency requires an observed inconsistent set")
            for group in core:
                self.assertFalse(any(conflict <= core - {group} for conflict in conflicts))
                self.assertIn(core - {group}, calls, "every final retained deletion is checked")
        self.assertNotIn(SECRET, json.dumps(got))
        for core in got["explanations"]:
            self.assertTrue(all(set(group) == {"id", "kind", "config_ref"} for group in core))

    def test_default_preserves_legacy_calls_and_reports_only_the_verified_first_core(self):
        required = frozenset(self.ids[-2:])
        got, calls = self.explain([required])
        self.assertEqual(len(calls), 1 + len(self.ids) + 2 * len(required))
        self.assertEqual(got["explanations"], [got["core"]])
        self.assertEqual((got["explanations_found"], got["max_explanations"], got["max_search_nodes"]), (1, 1, 256))
        self.assertEqual((got["enumeration_status"], got["enumeration_reason"], got["enumeration_complete"]),
                         ("not_run", "not_requested", False))
        self.assertEqual((got["enumeration_checks_completed"], got["search_nodes_visited"]), (0, 0))

    def test_two_independent_cores_are_verified_and_search_exhausts(self):
        conflicts = [frozenset(self.ids[-4:-2]), frozenset(self.ids[-2:])]
        got, calls = self.explain(conflicts, max_explanations=3, max_checks=512)
        self.assertEqual(set(self.cores(got)), set(conflicts))
        self.assertEqual((got["enumeration_status"], got["enumeration_reason"], got["enumeration_complete"]),
                         ("complete", "completed", True))
        self.assertEqual(got["uniqueness"], "not_unique")
        self.assert_verified(got, conflicts, calls)

    def test_overlapping_cores_deduplicate_and_are_stable_under_source_order(self):
        a, b, c = self.ids[-3:]
        conflicts = [frozenset((a, b)), frozenset((a, c)), frozenset((b, c))]
        got, calls = self.explain(conflicts, max_explanations=4, max_checks=512)
        self.assertEqual(set(self.cores(got)), set(conflicts))
        self.assertTrue(got["enumeration_complete"])
        self.assert_verified(got, conflicts, calls)
        self.graph = owl.Graph(dict(reversed(list(self.graph.nodes.items()))), list(reversed(self.graph.edges)))
        reordered, again = self.explain(conflicts, max_explanations=4, max_checks=512)
        self.assertEqual(got, reordered)
        self.assertEqual(calls, again)

    def test_every_antichain_on_three_groups_is_enumerated_completely(self):
        # Exhaust every nonempty monotone conflict family on three groups, not only disjoint examples.
        subsets = [frozenset(chosen) for size in range(1, 4)
                   for chosen in itertools.combinations(self.ids[-3:], size)]
        families = 0
        for mask in range(1, 1 << len(subsets)):
            conflicts = [core for index, core in enumerate(subsets) if mask & (1 << index)]
            if any(left < right for left in conflicts for right in conflicts):
                continue
            families += 1
            with self.subTest(conflicts=conflicts):
                got, calls = self.explain(conflicts, max_explanations=8, max_checks=512)
                self.assertEqual(set(self.cores(got)), set(conflicts))
                self.assertTrue(got["enumeration_complete"])
                self.assert_verified(got, conflicts, calls)
        self.assertEqual(families, 18)

    def test_result_cap_retains_verified_cores_without_claiming_exhaustion(self):
        conflicts = [frozenset((group,)) for group in self.ids[-3:]]
        got, calls = self.explain(conflicts, max_explanations=2, max_checks=512)
        self.assertEqual(len(self.cores(got)), 2)
        self.assertEqual((got["enumeration_status"], got["enumeration_reason"], got["enumeration_complete"]),
                         ("partial", "explanation_limit", False))
        self.assertEqual((got["status"], got["minimality"], got["uniqueness"]),
                         ("pass", "subset_minimal", "not_unique"))
        self.assert_verified(got, conflicts, calls)

    def test_node_cap_bounds_admitted_frontier_and_preserves_first_core(self):
        conflicts = [frozenset(self.ids[-4:-2]), frozenset(self.ids[-2:])]
        got, calls = self.explain(conflicts, max_explanations=3, max_search_nodes=1, max_checks=512)
        self.assertEqual((got["enumeration_status"], got["enumeration_reason"], got["enumeration_complete"]),
                         ("partial", "search_node_limit", False))
        self.assertLessEqual(got["search_nodes_visited"], 1)
        self.assertEqual(got["explanations"], [got["core"]])
        self.assertEqual(got["status"], "pass")
        self.assert_verified(got, conflicts, calls)

    def test_check_limit_is_shared_and_retains_only_fully_verified_cores(self):
        conflicts = [frozenset(self.ids[-4:-2]), frozenset(self.ids[-2:])]
        baseline, _calls = self.explain(conflicts)
        first_cost = baseline["checks_completed"]
        for extra in (0, 1, 3):
            with self.subTest(extra=extra):
                got, calls = self.explain(conflicts, max_explanations=3, max_checks=first_cost + extra)
                self.assertEqual(got["explanations"], [got["core"]])
                self.assertEqual((got["enumeration_status"], got["enumeration_reason"]), ("partial", "check_limit"))
                self.assertEqual(got["enumeration_checks_completed"], extra)
                self.assertEqual(got["checks_completed"], first_cost + extra)
                self.assertEqual((got["status"], got["minimality"]), ("pass", "subset_minimal"))
                self.assert_verified(got, conflicts, calls)

    def test_interruption_after_an_additional_verified_core_retains_both(self):
        conflicts = [frozenset((group,)) for group in self.ids[-3:]]
        full, _calls = self.explain(conflicts, max_explanations=4, max_checks=512)
        for cap in range(full["checks_completed"]):
            got, calls = self.explain(conflicts, max_explanations=4, max_checks=max(1, cap))
            if got["explanations_found"] == 2:
                break
        else:
            self.fail("a check cap after the second verified core must be observable")
        self.assertEqual((got["enumeration_reason"], got["enumeration_status"]), ("check_limit", "partial"))
        self.assertEqual(got["status"], "pass")
        self.assert_verified(got, conflicts, calls)

    def test_additional_core_final_deletion_disagreement_is_not_emitted(self):
        conflicts = [frozenset((group,)) for group in self.ids[-2:]]
        baseline, _calls = self.explain(conflicts)
        first_cost = baseline["checks_completed"]
        calls = []
        verified_deletions = set()
        def check(_cfg, _graph, _java, active, _deadline):
            active = frozenset(active)
            calls.append(active)
            if len(calls) > first_cost:
                if active in verified_deletions:
                    return False  # the repeated final deletion contradicts the shrink observation
                verified_deletions.add(active)
            return not any(core <= active for core in conflicts)
        with mock.patch.object(owl_explain, "_check", side_effect=check):
            got = owl_explain.explain(self.cfg, self.graph, "unused", max_explanations=3)
        self.assertEqual((got["enumeration_reason"], got["enumeration_status"]),
                         ("verification_disagreement", "partial"))
        self.assertEqual(got["explanations"], [got["core"]])
        self.assertEqual((got["status"], got["minimality"]), ("pass", "subset_minimal"))

    def test_invalid_additional_oracle_answer_is_an_incomplete_diagnostic(self):
        conflicts = [frozenset((group,)) for group in self.ids[-2:]]
        baseline, baseline_calls = self.explain(conflicts)
        answers = iter([not any(core <= active for core in conflicts) for active in baseline_calls] + ["private"])
        with mock.patch.object(owl_explain, "_check", side_effect=lambda *_a: next(answers)):
            got = owl_explain.explain(self.cfg, self.graph, "unused", max_explanations=3)
        self.assertEqual((got["enumeration_reason"], got["enumeration_status"]), ("reasoner_error", "partial"))
        self.assertEqual(got["checks_completed"], baseline["checks_completed"])
        self.assertEqual(got["explanations"], [got["core"]])

    def test_enumeration_failure_and_deadline_keep_the_first_core_and_redact_errors(self):
        conflicts = [frozenset(self.ids[-4:-2]), frozenset(self.ids[-2:])]
        baseline, _calls = self.explain(conflicts)
        first_cost = baseline["checks_completed"]
        for failure, reason in ((RuntimeError(SECRET + " C:/private/path"), "reasoner_error"),
                                (TimeoutError(SECRET), "timeout")):
            calls = []
            def check(_cfg, _graph, _java, active, _deadline):
                calls.append(frozenset(active))
                if len(calls) > first_cost:
                    raise failure
                return not any(core <= active for core in conflicts)
            with self.subTest(reason=reason), mock.patch.object(owl_explain, "_check", side_effect=check):
                got = owl_explain.explain(self.cfg, self.graph, "unused", max_explanations=3)
            self.assertEqual((got["enumeration_status"], got["enumeration_reason"]), ("partial", reason))
            self.assertEqual(got["explanations"], [got["core"]])
            self.assertEqual(got["status"], "pass")
            self.assertNotIn(SECRET, json.dumps(got))
            self.assertNotIn("C:/private", json.dumps(got))

    def test_wall_deadline_covers_additional_checks(self):
        conflicts = [frozenset(self.ids[-4:-2]), frozenset(self.ids[-2:])]
        baseline, _calls = self.explain(conflicts)
        first_cost, clock = baseline["checks_completed"], [0.0]
        calls = []
        def check(_cfg, _graph, _java, active, _deadline):
            calls.append(frozenset(active))
            if len(calls) > first_cost:
                clock[0] = 2.0
            return not any(core <= active for core in conflicts)
        with mock.patch.object(owl_explain, "_check", side_effect=check), \
             mock.patch.object(owl_explain.time, "monotonic", side_effect=lambda: clock[0]):
            got = owl_explain.explain(self.cfg, self.graph, "unused", max_explanations=3, timeout_ms=1000)
        self.assertEqual((got["status"], got["enumeration_reason"]), ("pass", "timeout"))
        self.assertEqual(got["checks_completed"], first_cost)
        self.assertEqual(got["explanations"], [got["core"]])

    def test_unique_first_core_completes_opt_in_enumeration_without_more_calls(self):
        required = frozenset(self.ids[-2:])
        baseline, baseline_calls = self.explain([required])
        got, calls = self.explain([required], max_explanations=3)
        self.assertEqual(calls, baseline_calls)
        self.assertEqual(got["core"], baseline["core"])
        self.assertEqual((got["enumeration_status"], got["enumeration_reason"], got["enumeration_complete"]),
                         ("complete", "unique", True))

    def test_unverified_first_core_does_not_enumerate_or_emit_explanations(self):
        got, calls = self.explain([frozenset(self.ids[-2:])], max_explanations=3, max_checks=1)
        self.assertEqual((got["enumeration_status"], got["enumeration_reason"]),
                         ("not_run", "minimality_not_proven"))
        self.assertEqual((got["explanations"], got["explanations_found"]), ([], 0))
        self.assertEqual(len(calls), 1)

    def test_duplicate_axiom_guard_prevents_enumeration(self):
        self.graph.edges *= 2
        self.ids = [group.id for group in owl.axiom_groups(self.cfg, self.graph)]
        got, _calls = self.explain([frozenset(self.ids[-2:])], max_explanations=3)
        self.assertEqual((got["uniqueness_reason"], got["enumeration_reason"]),
                         ("duplicate_axiom_groups", "duplicate_axiom_groups"))
        self.assertEqual(got["enumeration_status"], "not_run")
        self.assertFalse(got["enumeration_complete"])
        self.assertEqual(got["enumeration_checks_completed"], 0)

    def test_new_limits_are_positive_bounded_integers(self):
        for name, maximum in (("max_explanations", 32), ("max_search_nodes", 4096)):
            for bad in (0, -1, True, 1.5, "1", maximum + 1):
                with self.subTest(name=name, bad=bad), self.assertRaises(StageError) as caught:
                    owl_explain.explain(self.cfg, self.graph, "unused", **{name: bad})
                self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)

    def test_cli_flags_and_run_wiring(self):
        args = sxc.parser().parse_args(["reason", "--explain-inconsistency", "--explain-max-explanations", "3",
                                       "--explain-max-search-nodes", "9"])
        self.assertEqual((args.explain_max_explanations, args.explain_max_search_nodes), (3, 9))
        result = Result("reason")
        result.add("owl:consistency", "fail", "ontology")
        answer = dict(status="pass", uniqueness="not_unique", enumeration_status="partial",
                      enumeration_reason="explanation_limit", explanations_found=2)
        with mock.patch.object(owl_explain, "explain", return_value=answer) as explain:
            owl_explain.run(self.cfg, self.graph, "unused", args, result)
        self.assertEqual(explain.call_args.kwargs["max_explanations"], 3)
        self.assertEqual(explain.call_args.kwargs["max_search_nodes"], 9)
        self.assertEqual(result.status, "fail")
        self.assertTrue(any("enumeration" in text and "explanation_limit" in text for text in result.not_proven))
        self.assertFalse(any("only one is reported" in text for text in result.not_proven))

    def test_stage_rejects_new_invalid_limits_before_database_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            Result("build").write(out)
            for bad in (options(explain_max_explanations=33), options(explain_max_search_nodes=0)):
                with self.subTest(args=vars(bad)), mock.patch.object(stage_reason.db, "connect") as connect:
                    with self.assertRaises(StageError):
                        stage_reason.run(self.cfg, bad, out)
                    connect.assert_not_called()


@unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java")
class RealHermitAlternativeTest(unittest.TestCase):
    def test_two_real_cores_and_each_final_deletion_on_the_original_graph(self):
        cfg, _graph = cfg_graph()
        graph = owl.Graph({f"{SECRET}-1": {"raw"}, f"{SECRET}-2": {"raw"}, "leaf": set()},
                          [(f"{SECRET}-1", "leaf"), (f"{SECRET}-2", "leaf")])
        self.assertFalse(owl.reason(cfg, graph, JAVA).consistent)
        got = owl_explain.explain(cfg, graph, JAVA, max_explanations=3, max_checks=512, timeout_ms=120000)
        self.assertEqual((got["status"], got["uniqueness"], got["enumeration_status"]),
                         ("pass", "not_unique", "complete"), got)
        self.assertEqual(got["explanations_found"], 2)
        self.assertNotIn(SECRET, json.dumps(got))
        for descriptors in got["explanations"]:
            core = {group["id"] for group in descriptors}
            for active, expected in [(core, False)] + [(core - {group}, True) for group in core]:
                with self.subTest(active_count=len(active)):
                    model = owl.build(cfg, graph, active_axioms=active)
                    try:
                        self.assertEqual(owl.hermit(model.world, JAVA)[0], expected)
                    finally:
                        model.world.close()


if __name__ == "__main__":
    unittest.main()
