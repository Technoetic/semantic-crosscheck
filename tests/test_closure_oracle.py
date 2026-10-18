"""Hand-derived raw-edge oracle cases; no expected result comes from closure SQL."""

import dataclasses
import importlib
import json
import unittest
from decimal import Decimal, localcontext

import pgfixture  # puts the plugin scripts on sys.path
from semantic_crosscheck import stage_verify
from semantic_crosscheck.result import Result


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

    def test_cycle_error_fails_and_cycle_report_is_explicitly_not_proven(self):
        # Break caught: ignoring error-mode's cycle rejection, or passing report mode without independent SCC removal.
        for mode, wanted_status, wanted_reason in (("error", "fail", "cycle"),
                                                    ("report", "not_run", "cycle_not_supported")):
            with self.subTest(mode=mode):
                status, detail = self.compare([("a", "b", 2), ("b", "a", 3)], [], on_cycle=mode)
                self.assertEqual((status, detail["reason"]), (wanted_status, wanted_reason))

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


class OracleNextActionTest(unittest.TestCase):
    def action(self, status, **detail):
        result = Result("verify")
        result.add("v3:closure_oracle:example", status, "hierarchies.example", **detail)
        return stage_verify._next_action(result)

    def test_budget_action_identifies_the_exhausted_oracle_limit(self):
        self.assertIn("max_raw_rows", self.action("not_run", reason="budget_exceeded", limit="max_raw_rows"))

    def test_cycle_action_explains_the_missing_independent_cycle_support(self):
        self.assertIn("unsupported", self.action("not_run", reason="cycle_not_supported"))

    def test_oracle_disagreement_action_identifies_raw_edges_and_generated_closure(self):
        action = self.action("fail", mismatched_columns={"qty": 1})
        self.assertIn("raw-edge", action)
        self.assertIn("generated closure", action)


if __name__ == "__main__":
    unittest.main()
