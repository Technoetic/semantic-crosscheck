"""Additive judgements must preserve every digit, whatever the Decimal context."""

import json
import unittest
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_FLOOR, ROUND_UP, Rounded, localcontext
from itertools import permutations
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck import canon, shape_q


class AdditiveExactTest(unittest.TestCase):
    def judge(self, total, values, sql_null=None):
        measure, dimension = "m_value.value", "entity.class"
        test = {"kind": "additive", "measure": measure, "dimension": dimension, "agg": "sum",
                "sql_null_group": (sql_null, 0, 0)}
        responses = [[{measure: total}], [{measure: value, dimension: f"class-{i}"}
                                         for i, value in enumerate(values)]]
        # Only the external Cube read is replaced: run_test, parsing, summation and comparison stay real.
        with mock.patch.object(shape_q, "_load", side_effect=responses):
            return shape_q.run_test(None, None, None, {}, test)

    def test_correct_and_missing_unit_verdicts_are_context_independent(self):
        for precision in (2, 28, 60):
            for rounding in (ROUND_DOWN, ROUND_FLOOR, ROUND_UP):
                with self.subTest(precision=precision, rounding=rounding), localcontext() as ctx:
                    ctx.prec, ctx.rounding = precision, rounding
                    for total, expected in (("10000000000000000000000000001", ("pass", True)),
                                            ("1e28", ("fail", False))):
                        with self.subTest(total=total):
                            status, detail = self.judge(total, ["1e28", "1"])
                            self.assertEqual((status, detail["equal"]), expected)

    def test_cancellation_and_tiny_fraction_are_permutation_and_trap_independent(self):
        values = ["1e28", "-1e28", "0.00000000000000000000000000001", "1"]
        with localcontext() as ctx:
            ctx.prec, ctx.Emax, ctx.Emin = 2, 3, -3
            ctx.traps[Inexact] = ctx.traps[Rounded] = True
            flags = dict(ctx.flags)
            for ordering in permutations(values):
                status, detail = self.judge("1.00000000000000000000000000001", ordering)
                self.assertEqual((status, detail["equal"]), ("pass", True))
            self.assertEqual(dict(ctx.flags), flags)

    def test_resource_rejection_is_an_error_with_no_values_in_detail(self):
        status, detail = self.judge("1e4096", ["1e4096"])
        self.assertEqual((status, detail), ("error", {"error": "NumericLimit"}))
        self.assertNotIn("4096", json.dumps(detail))

    def test_every_numeric_comparison_input_is_bounded_before_fixed_point_formatting(self):
        def reject_large_format(value, spec):
            if not value.is_zero() and abs(value.adjusted()) >= 4096:
                self.fail("oversized numeric input reached fixed-point formatting")
            return format(value, spec)

        for value in (Decimal("1e1000000"), Decimal("1e-1000000"), "1e1000000", "1e-1000000"):
            cases = [("1", [value], None), (value, ["1"], None), ("1", ["1"], value)]
            for total, groups, sql_null in cases:
                with self.subTest(value=value, total=total, sql_null=sql_null), \
                        mock.patch.object(canon, "format", side_effect=reject_large_format, create=True):
                    self.assertEqual(self.judge(total, groups, sql_null),
                                     ("error", {"error": "NumericLimit"}))

    def test_bounded_comparison_preflight_preserves_nonnumeric_and_leading_zero_totals(self):
        for total in (True, "private-not-a-number", "01"):
            with self.subTest(total=total):
                self.assertEqual(self.judge(total, ["1"])[0], "fail")

    def test_all_null_and_existing_malformed_values_keep_their_behavior(self):
        self.assertEqual(self.judge(None, [None, None])[0], "pass")
        for value in (True, "private-not-a-number"):
            with self.subTest(value=value):
                self.assertEqual(self.judge(value, [value]), ("error", {"error": "ValueError"}))

    def test_nonfinite_values_cannot_make_an_additive_check_pass(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
                      float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(self.judge(value, [value]), ("error", {"error": "ValueError"}))

    def test_finite_float_inputs_keep_decimal_parsing_without_binary_float_summation(self):
        self.assertEqual(self.judge("0.3", [0.1, 0.2])[0], "pass")
        self.assertEqual(self.judge("0.30000000000000004", [0.1, 0.2])[0], "fail")


if __name__ == "__main__":
    unittest.main()
