"""Exact bounded sums are independent of Decimal and integer-string contexts."""

import random
import unittest
from decimal import Decimal, Inexact, ROUND_DOWN, ROUND_FLOOR, ROUND_UP, Rounded, localcontext
from fractions import Fraction
from itertools import permutations

import pgfixture  # noqa: F401
from semantic_crosscheck.exact_numeric import NumericLimit, exact_sum


class ExactSumTest(unittest.TestCase):
    def test_null_empty_and_signed_zero(self):
        self.assertIsNone(exact_sum([]))
        self.assertIsNone(exact_sum(iter([None, None])))
        result = exact_sum([None, Decimal("-0e-999999999"), Decimal("0e999999999")], max_digits=1)
        self.assertEqual(result.as_tuple(), Decimal(0).as_tuple())

    def test_large_integer_tiny_fraction_and_cancellation_keep_every_digit(self):
        fixtures = [(["1e28", "1"], "10000000000000000000000000001"),
                    (["1e28", "-1e28", "1e-28"], "1e-28"),
                    (["1234.5678", "-1234.5677"], "0.0001"),
                    (["0.10", "0.20"], "0.3"),
                    (["1e1000", "1e-1000", "-1e1000"], "1e-1000")]
        for texts, expected in fixtures:
            for ordering in permutations(map(Decimal, texts)):
                with self.subTest(texts=texts, ordering=ordering):
                    self.assertEqual(exact_sum(ordering), Decimal(expected))

    def test_rounding_traps_exponent_limits_and_flags_are_untouched(self):
        values = list(map(Decimal, ["1e28", "-1e28", "1", "1e-28"]))
        for precision in (1, 28, 60):
            for rounding in (ROUND_DOWN, ROUND_FLOOR, ROUND_UP):
                with self.subTest(precision=precision, rounding=rounding), localcontext() as ctx:
                    ctx.prec, ctx.Emax, ctx.Emin, ctx.rounding = precision, 3, -3, rounding
                    ctx.traps[Inexact] = ctx.traps[Rounded] = True
                    ctx.flags[Inexact] = True  # Do not clear a caller's existing flags either.
                    before = (ctx.prec, ctx.Emax, ctx.Emin, ctx.rounding, dict(ctx.traps), dict(ctx.flags))
                    self.assertEqual(exact_sum(values), Decimal("1.0000000000000000000000000001"))
                    self.assertEqual((ctx.prec, ctx.Emax, ctx.Emin, ctx.rounding,
                                      dict(ctx.traps), dict(ctx.flags)), before)

    def test_seeded_finite_sums_match_an_independent_rational_oracle(self):
        rng = random.Random(28017)
        for _ in range(150):
            values = [Decimal((rng.randrange(2), tuple(map(int, str(rng.randrange(100000)))),
                               rng.randrange(-35, 36))) for _ in range(rng.randrange(1, 12))]
            expected = sum(map(Fraction, values), Fraction())
            rng.shuffle(values)
            with localcontext() as ctx:
                ctx.prec, ctx.rounding = rng.randrange(1, 5), ROUND_UP
                ctx.traps[Inexact] = ctx.traps[Rounded] = True
                self.assertEqual(Fraction(exact_sum(values)), expected)

    def test_coefficient_exponent_alignment_and_result_have_explicit_bounds(self):
        self.assertEqual(exact_sum([Decimal("999")], max_digits=3), Decimal("999"))
        self.assertEqual(exact_sum([Decimal("0.001")], max_digits=3), Decimal("0.001"))
        self.assertEqual(exact_sum([Decimal("9.99"), Decimal("0.01")], max_digits=3), Decimal("10"))
        rejected = [[Decimal("1000")], [Decimal("0.0001")], [Decimal("1.000")],
                    [Decimal("999"), Decimal("1")], [Decimal("1e2"), Decimal("0.01")],
                    [Decimal("9.99"), Decimal("0.02")],
                    [Decimal("1e999999999")], [Decimal("1e-999999999")]]
        for values in rejected:
            with self.subTest(values=values), self.assertRaises(NumericLimit):
                exact_sum(values, max_digits=3)

    def test_transient_carry_during_cancellation_does_not_depend_on_order(self):
        for values in permutations(map(Decimal, ["999", "1", "-999"])):
            self.assertEqual(exact_sum(values, max_digits=3), Decimal("1"))
        for values in permutations(map(Decimal, ["1e2", "0.01", "-1e2"])):
            with self.assertRaises(NumericLimit):
                exact_sum(values, max_digits=3)

    def test_entry_budget_counts_nulls_and_stops_a_generator(self):
        self.assertEqual(exact_sum(iter([None, Decimal(1), None]), max_terms=3), Decimal(1))
        consumed = []

        def nulls():
            for i in range(100):
                consumed.append(i)
                yield None

        with self.assertRaises(NumericLimit):
            exact_sum(nulls(), max_terms=3)
        self.assertEqual(len(consumed), 4)

    def test_nonfinite_and_nondecimal_values_are_rejected_without_echoing_values(self):
        for value in (True, False, 1, 1.5, "private-invalid-value", Decimal("NaN"),
                      Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                exact_sum([value])
            self.assertNotIn("private-invalid-value", str(caught.exception))

    def test_invalid_limits_are_rejected(self):
        for name in ("max_digits", "max_terms"):
            for limit in (True, False, 0, -1, 1.5, None):
                with self.subTest(name=name, limit=limit), self.assertRaises(ValueError):
                    exact_sum([], **{name: limit})

    def test_caller_digit_budget_does_not_depend_on_python_integer_string_limit(self):
        value = Decimal("1" + "2" * 4500)
        self.assertEqual(exact_sum([value], max_digits=5000), value)

    def test_oversized_coefficients_are_rejected_before_digit_tuple_allocation(self):
        class NoDigitTuple(Decimal):
            def as_tuple(self):
                raise AssertionError("oversized coefficient reached digit tuple allocation")

        value = NoDigitTuple("1." + "0" * 4096)
        with self.assertRaises(NumericLimit):
            exact_sum([value])

    def test_soft_function_boundary_numeric_seeds_preserve_exactness(self):
        # Exercises Soft (EuroSys 2025) numeric(18,3)/(18,6) boundary seeds: signed zero,
        # full 18-digit boundaries, tiny sub-unit scale, NULLs and exact cancellation.
        seeds = [
            Decimal("-0.000"),
            Decimal("0.000000"),
            Decimal("999999999999999.999"),
            Decimal("-999999999999999.999"),
            Decimal("-0.000001"),
            Decimal("0.000001"),
            None,
        ]
        self.assertEqual(exact_sum(seeds).as_tuple(), Decimal(0).as_tuple())
        self.assertEqual(
            exact_sum([Decimal("999999999999.000001"), Decimal("-0.000001")]),
            Decimal("999999999999"),
        )


if __name__ == "__main__":
    unittest.main()
