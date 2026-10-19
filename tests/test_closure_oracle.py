"""Hand-derived raw-edge oracle cases; no expected result comes from closure SQL."""

import dataclasses
import importlib
import json
import random
import tempfile
import unittest
from collections import deque
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from unittest import mock

import pgfixture  # puts the plugin scripts on sys.path
from semantic_crosscheck import stage_verify
from semantic_crosscheck.result import Result
from test_compare_unit import demo_cfg, demo_model


class ClosureOracleTest(unittest.TestCase):
    def setUp(self):
        try:
            self.oracle = importlib.import_module("semantic_crosscheck.closure_oracle")
        except ModuleNotFoundError:
            self.fail("the independent closure oracle has not been implemented")

    def compare(self, edges, rows, **kwargs):
        return self.oracle.compare_rows(edges, rows, **kwargs)

    def test_diamond_counts_logical_paths_and_sums_path_products(self):
        # Break caught: counting distinct reachable nodes instead of paths, or multiplying summed branches.
        edges = [("a", "b", 2), ("a", "c", 3), ("b", "d", 5), ("c", "d", 7), ("a", "d", 11)]
        rows = [("a", "b", 1, 1, 1, 2), ("a", "c", 1, 1, 1, 3), ("b", "d", 1, 1, 1, 5),
                ("c", "d", 1, 1, 1, 7), ("a", "d", 1, 2, 3, 42)]
        status, detail = self.compare(edges, rows)
        self.assertEqual((status, detail["expected_pairs"], detail["matched_pairs"]), ("pass", 5, 5))

    def test_duplicate_lines_are_one_summed_edge(self):
        # Break caught: treating repeated source lines as separate paths.
        edges = [("a", "b", 2), ("a", "b", 3), ("a", "b", None), ("b", "c", 7)]
        rows = [("a", "b", 1, 1, 1, 5), ("b", "c", 1, 1, 1, 7), ("a", "c", 2, 2, 1, 35)]
        status, detail = self.compare(edges, rows)
        self.assertEqual((status, detail["logical_edges"]), ("pass", 2))

    def test_null_paths_count_but_only_nonnull_products_contribute_to_quantity(self):
        # Break caught: NULL as zero, NULL poisoning another path, or dropping NULL-quantity paths.
        edges = [("a", "b", None), ("a", "b", None), ("b", "d", 5), ("a", "c", 3),
                 ("c", "d", 7), (None, "x", 17), ("x", None, 19)]
        rows = [("a", "b", 1, 1, 1, None), ("b", "d", 1, 1, 1, 5), ("a", "c", 1, 1, 1, 3),
                ("c", "d", 1, 1, 1, 7), ("a", "d", 2, 2, 2, 21)]
        status, detail = self.compare(edges, rows)
        self.assertEqual((status, detail["null_endpoint_lines"], detail["null_quantity_edges"]), ("pass", 2, 1))
        self.assertEqual(self.compare([("a", "b", None), ("b", "c", 0)],
                                     [("a", "b", 1, 1, 1, None), ("b", "c", 1, 1, 1, 0),
                                      ("a", "c", 2, 2, 1, None)])[0], "pass")

    def test_decimal_arithmetic_stays_exact_beyond_context_precision(self):
        # Break caught: ordinary Decimal multiplication/sum silently rounding at precision 28.
        edges = [("a", "b", Decimal("10000000000000000000000000000.1")), ("a", "b", Decimal("0.2")),
                 ("b", "c", Decimal("3"))]
        rows = [("a", "b", 1, 1, 1, Decimal("10000000000000000000000000000.3")),
                ("b", "c", 1, 1, 1, 3), ("a", "c", 2, 2, 1, Decimal("30000000000000000000000000000.9"))]
        with localcontext() as ctx:
            ctx.prec = 6
            self.assertEqual(self.compare(edges, rows)[0], "pass")

    def test_each_wrong_generated_metric_fails_without_exposing_keys_or_values(self):
        # Break caught: checking reachability alone, or omitting any of the four generated metrics.
        for column, position in [("min_depth", 2), ("max_depth", 3), ("path_count", 4), ("qty", 5)]:
            with self.subTest(column=column):
                row = ["SECRET_PARENT", "SECRET_CHILD", 1, 1, 1, 947231]
                row[position] += 1
                status, detail = self.compare([("SECRET_PARENT", "SECRET_CHILD", 947231)], [row])
                self.assertEqual((status, detail["mismatched_columns"]), ("fail", {column: 1}))
                self.assertNotIn("SECRET", json.dumps(detail))
                self.assertNotIn("947231", json.dumps(detail))

    def test_missing_extra_and_duplicate_pairs_fail(self):
        # Break caught: overwriting duplicate rows or comparing only the intersection.
        edges = [("a", "b", 2), ("b", "c", 3)]
        rows = [("a", "b", 1, 1, 1, 2), ("a", "b", 1, 1, 1, 2), ("x", "y", 1, 1, 1, 7)]
        status, detail = self.compare(edges, rows)
        self.assertEqual((status, detail["missing_pairs"], detail["extra_pairs"], detail["duplicate_pairs"]),
                         ("fail", 2, 1, 1))

    def test_nonfinite_and_inexact_quantities_are_not_proven(self):
        # Break caught: coercing NaN/Infinity or binary floats to a supposedly exact finite result.
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), 0.1):
            with self.subTest(value=type(value).__name__):
                status, detail = self.compare([("a", "b", value)], [("a", "b", 1, 1, 1, value)])
                self.assertEqual((status, detail["reason"]), ("not_run", "unsupported_quantity"))
                status, detail = self.compare([("a", "b", 1)], [("a", "b", 1, 1, 1, value)])
                self.assertEqual((status, detail["reason"]), ("not_run", "unsupported_quantity"))

    def test_cycle_error_mode_still_fails(self):
        # Break caught: applying report mode's SCC-edge removal to error mode, which must reject every cycle.
        for edges in ([("a", "b", 2), ("b", "a", 3)], [("a", "a", 2)], [("a", "b", 2), ("b", "b", 3), ("b", "c", 5)]):
            with self.subTest(edges=len(edges)):
                status, detail = self.compare(edges, [], on_cycle="error")
                self.assertEqual((status, detail["reason"]), ("fail", "cycle"))
                self.assertNotIn("cycle_handling", detail)

    def test_unknown_cycle_policy_is_not_proven(self):
        # Break caught: treating an unrecognised policy as report or error instead of refusing to judge it, or
        # refusing it only when the graph happens to have a cycle.
        for edges in ([("a", "b", 2), ("b", "a", 3)], [("a", "b", 2)]):
            with self.subTest(edges=edges):
                status, detail = self.compare(edges, [("a", "b", 1, 1, 1, 2)], on_cycle="ignore")
                self.assertEqual((status, detail.get("reason")), ("not_run", "unsupported_on_cycle"))

    def test_depth_limit_is_not_accepted_as_a_truncated_closure(self):
        # Break caught: stopping the oracle at max_depth and calling the resulting subset complete.
        status, detail = self.compare([("a", "b", 2), ("b", "c", 3)],
                                      [("a", "b", 1, 1, 1, 2), ("b", "c", 1, 1, 1, 3)], max_depth=1)
        self.assertEqual((status, detail["reason"]), ("fail", "depth_limit"))

    def test_empty_actual_graph_discloses_vacuous_coverage(self):
        status, detail = self.compare([], [])
        self.assertEqual((status, detail["empty_graph"], detail["expected_pairs"]), ("pass", True, 0))
        self.assertEqual(self.compare([], [("a", "b", 1, 1, 1, 0)])[0], "fail")

    def test_budget_exhaustion_is_not_proven_instead_of_partial_pass(self):
        # Break caught: truncating row reads, reachable pairs, operations, or arithmetic without disclosing it.
        edges = [("a", "b", 2), ("b", "c", 3)]
        rows = [("a", "b", 1, 1, 1, 2), ("b", "c", 1, 1, 1, 3), ("a", "c", 2, 2, 1, 6)]
        for field, limit in [("max_raw_rows", 1), ("max_closure_rows", 2), ("max_pairs", 2),
                             ("max_operations", 1), ("max_nodes", 2), ("max_numeric_bits", 2)]:
            with self.subTest(field=field):
                limits = dataclasses.replace(self.oracle.Limits(), **{field: limit})
                status, detail = self.compare(edges, rows, limits=limits)
                self.assertEqual((status, detail["reason"], detail["limit"]), ("not_run", "budget_exceeded", field))

    def test_extreme_decimal_exponent_is_bounded_before_materializing_a_fraction(self):
        status, detail = self.compare([("a", "b", Decimal("1E+999999999"))], [])
        self.assertEqual((status, detail["reason"], detail["limit"]),
                         ("not_run", "budget_exceeded", "max_decimal_digits"))


class ReportModeCycleTest(unittest.TestCase):
    """on_cycle = "report": the builder drops every logical edge whose child reaches its parent (the edges inside a
    strongly connected component, self-loops included) and closes the rest. Expected rows here are derived by hand."""

    def setUp(self):
        self.oracle = importlib.import_module("semantic_crosscheck.closure_oracle")

    def compare(self, edges, rows, **kwargs):
        return self.oracle.compare_rows(edges, rows, on_cycle="report", **kwargs)

    def assert_report_pass(self, edges, rows, dropped, cycle_nodes, **kwargs):
        status, detail = self.compare(edges, rows, **kwargs)
        self.assertEqual((status, detail.get("reason"), detail["expected_pairs"], detail["matched_pairs"]),
                         ("pass", None, len(rows), len(rows)))
        self.assertEqual((detail["cycle_handling"], detail["dropped_cycle_edges"], detail["cycle_nodes"]),
                         ("scc_edge_removal", dropped, cycle_nodes))
        return detail

    def test_self_loop_only_leaves_a_node_without_pairs(self):
        # Break caught: Kahn's in-degree treats a self-loop as a cycle; report mode must drop it, not refuse the graph.
        detail = self.assert_report_pass([("a", "a", 2)], [], dropped=1, cycle_nodes=1)
        self.assertEqual((detail["nodes"], detail["empty_graph"]), (1, True))
        self.assert_report_pass([("a", "a", 2), ("a", "b", 3)], [("a", "b", 1, 1, 1, 3)], dropped=1, cycle_nodes=1)

    def test_two_cycle_with_a_tail_below(self):
        # Break caught: keeping one direction of the cycle, or dropping the tail edges that leave the component.
        edges = [("a", "b", 2), ("b", "a", 3), ("b", "c", 5), ("c", "d", 7)]
        rows = [("b", "c", 1, 1, 1, 5), ("c", "d", 1, 1, 1, 7), ("b", "d", 2, 2, 1, 35)]
        detail = self.assert_report_pass(edges, rows, dropped=2, cycle_nodes=2)
        self.assertEqual(detail["nodes"], 4)

    def test_nested_cycles_sharing_a_node_with_a_dag_below_and_an_entry_above(self):
        # Break caught: removing only one elementary cycle per component, or following edges inside the component
        # after entering it (x reaches a, but not b or c, once every edge inside {a, b, c} is gone).
        edges = [("a", "b", 1), ("b", "a", 1), ("b", "c", 1), ("c", "b", 1),
                 ("c", "d", 2), ("a", "d", 3), ("d", "e", 5), ("b", "e", 7), ("x", "a", 11)]
        rows = [("c", "d", 1, 1, 1, 2), ("a", "d", 1, 1, 1, 3), ("d", "e", 1, 1, 1, 5), ("b", "e", 1, 1, 1, 7),
                ("c", "e", 2, 2, 1, 10), ("a", "e", 2, 2, 1, 15), ("x", "a", 1, 1, 1, 11),
                ("x", "d", 2, 2, 1, 33), ("x", "e", 3, 3, 1, 165)]
        self.assert_report_pass(edges, rows, dropped=4, cycle_nodes=3)

    def test_two_separate_components_one_above_the_other(self):
        edges = [("a", "b", 1), ("b", "a", 1), ("b", "c", 2), ("c", "d", 1), ("d", "c", 1), ("d", "e", 3)]
        rows = [("b", "c", 1, 1, 1, 2), ("d", "e", 1, 1, 1, 3)]
        self.assert_report_pass(edges, rows, dropped=4, cycle_nodes=4)

    def test_component_edge_with_duplicate_lines_and_null_quantity_counts_once(self):
        # Break caught: counting dropped source lines instead of logical edges, or letting a NULL inside the
        # component change the quantities of the remaining graph.
        edges = [("a", "b", 2), ("a", "b", 3), ("a", "b", None), ("b", "a", None), ("a", "c", 4), ("c", "d", None),
                 (None, "a", 9)]
        rows = [("a", "c", 1, 1, 1, 4), ("c", "d", 1, 1, 1, None), ("a", "d", 2, 2, 1, None)]
        detail = self.assert_report_pass(edges, rows, dropped=2, cycle_nodes=2)
        self.assertEqual((detail["logical_edges"], detail["null_quantity_edges"], detail["null_endpoint_lines"]),
                         (4, 2, 1))

    def test_wrong_closures_of_a_cyclic_graph_fail(self):
        # Break caught: a report-mode pass that ignores the stored metrics or pairs through dropped edges.
        edges = [("a", "b", 2), ("b", "a", 3), ("b", "c", 5), ("c", "d", 7)]
        good = [("b", "c", 1, 1, 1, 5), ("c", "d", 1, 1, 1, 7), ("b", "d", 2, 2, 1, 35)]
        for position, column in ((4, "path_count"), (5, "qty")):
            with self.subTest(column=column):
                rows = [list(r) for r in good]
                rows[2][position] += 1
                status, detail = self.compare(edges, rows)
                self.assertEqual((status, detail["mismatched_columns"]), ("fail", {column: 1}))
        # A closure that kept the edges inside the cycle has extra pairs such as (a, c).
        status, detail = self.compare(edges, good + [("a", "c", 2, 2, 1, 10)])
        self.assertEqual((status, detail["extra_pairs"]), ("fail", 1))
        status, detail = self.compare(edges, good[:2])
        self.assertEqual((status, detail["missing_pairs"]), ("fail", 1))

    def test_depth_limit_applies_after_cycle_removal(self):
        # Break caught: skipping the depth check on the graph that remains after removal.
        edges = [("a", "b", 1), ("b", "a", 1), ("b", "c", 2), ("c", "d", 3)]
        rows = [("b", "c", 1, 1, 1, 2), ("c", "d", 1, 1, 1, 3), ("b", "d", 2, 2, 1, 6)]
        status, detail = self.compare(edges, rows, max_depth=1)
        self.assertEqual((status, detail["reason"], detail["dropped_cycle_edges"]), ("fail", "depth_limit", 2))
        self.assertEqual(self.compare(edges, rows, max_depth=2)[0], "pass")

    def test_component_search_is_charged_to_the_operation_budget(self):
        # Break caught: an unbounded component search outside the operation limit.
        edges = [("a", "b", 1), ("b", "c", 1), ("c", "a", 1), ("c", "d", 2)]
        rows = [("c", "d", 1, 1, 1, 2)]
        search = self.oracle._strong_components
        seen = []

        def measured(nodes, children, budget):
            before = budget.operations
            result = search(nodes, children, budget)
            seen.append((before, budget.operations))
            return result

        with mock.patch.object(self.oracle, "_strong_components", measured):
            status, detail = self.compare(edges, rows)
        self.assertEqual(status, "pass")
        (before, after), = seen
        self.assertEqual(after - before, 2 * 4 + 4)   # one visit and one pop per node, one step per logical edge
        self.assertGreater(detail["operations"], after)
        limits = dataclasses.replace(self.oracle.Limits(), max_operations=before + 1)
        status, detail = self.compare(edges, rows, limits=limits)
        self.assertEqual((status, detail["reason"], detail["limit"]), ("not_run", "budget_exceeded", "max_operations"))

    def test_component_search_needs_no_recursion(self):
        # Break caught: a recursive Tarjan that overflows Python's recursion limit on a long cycle.
        n = 3000
        edges = [(i, i + 1, 1) for i in range(n)] + [(n, 0, 1)]
        status, detail = self.compare(edges, [], max_depth=n + 1)
        self.assertEqual((status, detail["dropped_cycle_edges"], detail["cycle_nodes"]), ("pass", n + 1, n + 1))

    def test_cycle_left_after_removal_is_an_error_not_a_pass(self):
        # Break caught: folding a graph that still has a cycle (a broken component search) and reporting pass.
        def singletons(nodes, children, budget):
            return {node: index for index, node in enumerate(nodes)}

        with mock.patch.object(self.oracle, "_strong_components", singletons):
            status, detail = self.compare([("a", "b", 2), ("b", "a", 3)], [])
        self.assertEqual((status, detail["reason"]), ("error", "scc_removal_left_cycle"))

    def test_detail_has_counts_only(self):
        edges = [("SECRET_A", "SECRET_B", 947231), ("SECRET_B", "SECRET_A", 947231), ("SECRET_B", "SECRET_C", 3)]
        status, detail = self.compare(edges, [("SECRET_B", "SECRET_C", 1, 1, 1, 3)])
        self.assertEqual(status, "pass")
        self.assertNotIn("SECRET", json.dumps(detail))
        self.assertNotIn("947231", json.dumps(detail))


class ReportModePropertyTest(unittest.TestCase):
    """Seeded random small graphs against an emulation of the builder that uses neither a component search nor the
    oracle's fold: a logical edge (p, c) is dropped iff c reaches p by BFS over all logical edges, then every path
    of the remaining graph is enumerated by brute force."""

    SEEDS = range(240)

    @staticmethod
    def graph(rng):
        nodes = [f"n{i}" for i in range(rng.randint(1, 7))]
        lines = []
        for _ in range(rng.randint(0, 12)):
            quantity = rng.choice([None, 0, 1, 2, 3, -1, Decimal("0.5"), Decimal("1.25")])
            lines.append((rng.choice(nodes), rng.choice(nodes), quantity))
        if lines and rng.random() < 0.3:
            lines.append(rng.choice(lines))   # a duplicate source line
        if rng.random() < 0.15:
            lines.append((None, rng.choice(nodes), 4))
        return lines, rng.randint(1, 6)

    @staticmethod
    def emulate(lines, max_depth):
        """(closure or None when a path exceeds max_depth, number of dropped logical edges)."""
        logical = {}
        for parent, child, quantity in lines:
            if parent is None or child is None:
                continue
            value = None if quantity is None else Fraction(quantity)
            if (parent, child) not in logical or logical[(parent, child)] is None:
                logical[(parent, child)] = value
            elif value is not None:
                logical[(parent, child)] += value

        def reaches(start, goal):
            seen, queue = {start}, deque([start])
            while queue:
                node = queue.popleft()
                if node == goal:
                    return True
                for p, c in logical:
                    if p == node and c not in seen:
                        seen.add(c)
                        queue.append(c)
            return False

        dropped = {pair for pair in logical if reaches(pair[1], pair[0])}
        kept = {pair: q for pair, q in logical.items() if pair not in dropped}
        closure = {}

        def walk(origin, node, depth, product):
            for (p, c), q in kept.items():
                if p != node:
                    continue
                if depth + 1 > max_depth:
                    raise OverflowError
                value = None if product is None or q is None else product * q
                lo, hi, paths, total = closure.get((origin, c), (depth + 1, depth + 1, 0, None))
                total = value if total is None else (total if value is None else total + value)
                closure[(origin, c)] = (min(lo, depth + 1), max(hi, depth + 1), paths + 1, total)
                walk(origin, c, depth + 1, value)

        try:
            for origin in {n for pair in logical for n in pair}:
                walk(origin, origin, 0, Fraction(1))
        except OverflowError:
            return None, len(dropped)
        return closure, len(dropped)

    @staticmethod
    def decimal(value):
        """Stored closures hold numeric, never Fraction; these denominators are products of 2 and 5, so exact."""
        if value is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 60
            result = Decimal(value.numerator) / Decimal(value.denominator)
        assert Fraction(result) == value
        return result

    def test_report_mode_matches_brute_force_builder_semantics(self):
        oracle = importlib.import_module("semantic_crosscheck.closure_oracle")
        cyclic = depth_failures = 0
        for seed in self.SEEDS:
            rng = random.Random(seed)
            lines, max_depth = self.graph(rng)
            want, dropped = self.emulate(lines, max_depth)
            cyclic += bool(dropped)
            with self.subTest(seed=seed):
                rows = [] if want is None else [(a, d, lo, hi, paths, self.decimal(total))
                                                for (a, d), (lo, hi, paths, total) in want.items()]
                status, detail = oracle.compare_rows(lines, rows, max_depth=max_depth, on_cycle="report")
                if want is None:
                    depth_failures += 1
                    self.assertEqual((status, detail["reason"]), ("fail", "depth_limit"))
                    continue
                self.assertEqual((status, detail.get("dropped_cycle_edges", 0)), ("pass", dropped))
                expected = oracle._expected(lines, max_depth, "report", oracle._Budget(oracle.Limits()), {})
                self.assertEqual(expected, want)
        self.assertGreaterEqual(cyclic, 60, "the seeds must exercise cycles")
        self.assertGreaterEqual(depth_failures, 5, "the seeds must exercise the depth limit after removal")


class OracleNotProvenTest(unittest.TestCase):
    """verify's not_proven lines for the oracle checks, without PostgreSQL (closure_oracle.run is replaced)."""

    def not_proven(self, status, **detail):
        cfg = demo_cfg()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        v = stage_verify._Verify(cfg, Path(tmp.name), demo_model(cfg), ["v3"])
        v.closure = dict.fromkeys(cfg.hierarchies, "pass")
        with mock.patch.object(stage_verify.closure_oracle, "run", return_value=(status, detail)):
            v.closure_oracles(None)
        self.assertTrue(cfg.hierarchies)
        return {name: [line for line in v.res.not_proven if line.startswith(f"v3:closure_oracle:{name}: ")]
                for name in cfg.hierarchies}, v.res.not_proven

    def test_lines_say_why_no_pair_was_exercised_and_which_edges_were_left_out(self):
        # Break caught: calling an all-cycle graph an empty raw-edge graph, or losing the removed-edge disclosure.
        removed = "on_cycle = report removed 2 logical edge(s) inside cycles"
        cases = [("pass", 2, False, {}, [removed]),
                 ("pass", 2, True, {}, ["every logical edge lay inside a cycle", removed]),
                 ("pass", 0, True, {}, ["empty raw-edge graph"]),
                 ("pass", 0, False, {}, []),
                 ("fail", 2, False, {}, []),
                 ("fail", 2, True, {}, ["every logical edge lay inside a cycle"]),
                 ("not_run", 2, False, {"reason": "budget_exceeded"}, ["not proven (budget_exceeded)"])]
        for status, dropped, empty, extra, wanted in cases:
            with self.subTest(status=status, dropped=dropped, empty=empty):
                per_name, every = self.not_proven(status, dropped_cycle_edges=dropped, empty_graph=empty, **extra)
                self.assertEqual(len(every), len(wanted) * len(per_name))
                for lines in per_name.values():
                    self.assertEqual(len(lines), len(wanted))
                    for line, fragment in zip(lines, wanted):
                        self.assertIn(fragment, line)
                if dropped:
                    self.assertFalse(any("empty raw-edge graph" in line for line in every))


class OracleNextActionTest(unittest.TestCase):
    def action(self, status, **detail):
        result = Result("verify")
        result.add("v3:closure_oracle:example", status, "hierarchies.example", **detail)
        return stage_verify._next_action(result)

    def test_budget_action_identifies_the_exhausted_oracle_limit(self):
        self.assertIn("max_raw_rows", self.action("not_run", reason="budget_exceeded", limit="max_raw_rows"))

    def test_internal_cycle_removal_error_is_reported_as_an_oracle_defect(self):
        action = self.action("error", reason="scc_removal_left_cycle")
        self.assertIn("independent oracle", action)
        self.assertNotIn("gold SQL", action)

    def test_oracle_disagreement_action_identifies_raw_edges_and_generated_closure(self):
        action = self.action("fail", mismatched_columns={"qty": 1})
        self.assertIn("raw-edge", action)
        self.assertIn("generated closure", action)


if __name__ == "__main__":
    unittest.main()
