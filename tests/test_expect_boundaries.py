"""Authored boundary rows exercise the existing expect contract without engines.

The literals come from fixture-spec.md's arithmetic, rather than a SQL/Cube run.
The integration companion executes the corpus and deliberate mistakes on real engines.
"""

import dataclasses
import re
import unittest
from decimal import Decimal

import pgfixture
from semantic_crosscheck import config, expect


EXAMPLE = pgfixture.ROOT / "examples" / "expect-boundaries"
# Independently hand-calculated from the five-part/seven-stock specification.
EXPECTED = {
    "parent_without_facts": [{"part_id": "P-EMPTY", "qty": None}],
    "facts_filtered_out": [{"part_id": "P-FILTER", "all_qty": Decimal("9.00"), "usable_qty": 0}],
    "same_label_different_region": [
        {"region": "NORTH", "label": "Relay", "qty": Decimal("10.00")},
        {"region": "SOUTH", "label": "Relay", "qty": Decimal("20.00")},
    ],
}
TYPES = {
    "parent_without_facts": {"part_id": "string", "qty": "number"},
    "facts_filtered_out": {"part_id": "string", "all_qty": "number", "usable_qty": "number"},
    "same_label_different_region": {"region": "string", "label": "string", "qty": "number"},
}


class ExpectBoundariesTest(unittest.TestCase):
    def setUp(self):
        path = EXAMPLE / "crosscheck.toml"
        self.assertTrue(path.is_file(), "the independently authored boundary corpus is missing")
        self.cfg = config.load(path)
        self.gold = {gold.id: gold for gold in self.cfg.gold}

    def test_authored_cases_match_the_independent_specification_rows(self):
        self.assertEqual(set(self.gold), set(EXPECTED))
        pins = set()
        for name, rows in EXPECTED.items():
            with self.subTest(case=name):
                gold = self.gold[name]
                self.assertEqual(len(gold.expect), 1)
                case = gold.expect[0]
                self.assertEqual(case.source, "authored")
                self.assertTrue(case.spec.strip())
                self.assertFalse(case.partial)
                self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", case.fixture_sha256))
                pins.add(case.fixture_sha256)
                status, detail = expect.judge(gold, case, rows, rows, TYPES[name])
                self.assertEqual(status, "pass", detail)
        self.assertEqual(len(pins), 1)

    def test_wrong_authored_rows_are_common_mode_failures(self):
        wrong_rows = {
            "parent_without_facts": [{"part_id": "P-EMPTY", "qty": 0}],
            "facts_filtered_out": [{"part_id": "P-FILTER", "all_qty": 9, "usable_qty": 1}],
            "same_label_different_region": [
                {"region": "NORTH", "label": "Relay", "qty": 11},
                {"region": "SOUTH", "label": "Relay", "qty": 20},
            ],
        }
        for name, rows in wrong_rows.items():
            with self.subTest(case=name):
                gold = self.gold[name]
                wrong = dataclasses.replace(gold.expect[0], rows=rows)
                status, detail = expect.judge(gold, wrong, EXPECTED[name], EXPECTED[name], TYPES[name])
                self.assertEqual((status, detail["reason"]), ("fail", "common_mode"))
                self.assertTrue(detail["gold_equals_cube"])
                self.assertFalse(detail["gold_equals_expected"] or detail["cube_equals_expected"])

    def test_parent_without_facts_does_not_silently_equate_null_and_zero(self):
        name = "parent_without_facts"
        gold = self.gold[name]
        status, detail = expect.judge(gold, gold.expect[0], EXPECTED[name],
                                      [{"part_id": "P-EMPTY", "qty": 0}], TYPES[name])
        self.assertEqual((status, detail["reason"]), ("fail", "cube_mismatch"))

    def test_filtered_facts_zero_policy_only_normalizes_the_cube_side(self):
        name = "facts_filtered_out"
        gold = self.gold[name]
        null_rows = [{"part_id": "P-FILTER", "all_qty": 9, "usable_qty": None}]
        status, detail = expect.judge(gold, gold.expect[0], EXPECTED[name], null_rows, TYPES[name])
        self.assertEqual(status, "pass", detail)
        status, detail = expect.judge(gold, gold.expect[0], null_rows, EXPECTED[name], TYPES[name])
        self.assertEqual((status, detail["reason"]), ("fail", "gold_mismatch"))

    def test_label_only_grouping_cannot_pass_the_composite_key_case(self):
        name = "same_label_different_region"
        gold = self.gold[name]
        collapsed = [{"region": "NORTH", "label": "Relay", "qty": 30}]
        status, detail = expect.judge(gold, gold.expect[0], collapsed, collapsed, TYPES[name])
        self.assertEqual((status, detail["reason"]), ("fail", "common_mode"))
        self.assertEqual(detail["gold_vs_expected"]["only_in_expected"], 1)
        self.assertEqual(detail["gold_vs_expected"]["mismatched_columns"], {"qty": 1})


if __name__ == "__main__":
    unittest.main()
