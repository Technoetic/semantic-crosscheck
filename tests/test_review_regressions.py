"""Regression checks for skipped reasoning, exact numbers and silently dropped configuration."""

import copy
import json
import tempfile
import tomllib
import unittest
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import canon, compare, config, owl, report, shacl_check, shapes, stage_reason
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_FAIL, Result, StageError, inputs_fingerprint


class ReasonResourceLimits(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(pgfixture.DEMO_CONFIG)

    def reason_and_report(self, limited):
        """Exercise real stage/status code with synthetic engine boundaries and current prerequisite files."""
        with tempfile.TemporaryDirectory(prefix="sxc_review_limits_") as tmp:
            out = Path(tmp)
            fingerprint = inputs_fingerprint(self.cfg)
            for stage, minute in (("build", 0), ("cube", 2), ("verify", 3)):
                result = Result(stage, inputs=fingerprint)
                result.add(stage, "pass")
                if stage == "build":
                    for shape in self.cfg.shapes:
                        result.add(f"shape:{shape.id}", "pass")
                    result.add(f"closure:{self.cfg.ontology.hierarchy}", "pass")
                result.write(out)
                self.at(out, stage, minute)
            (out / "build/shapes.ttl").write_text(shapes.shapes_ttl(self.cfg)[0], encoding="utf-8")
            planned, _ = shacl_check.plan(self.cfg)
            compared = {s.id: {"sql_keys": set(), "shacl_keys": set(), "rows": 1, "group": shacl_check.value_group(s),
                               "group_rows": 1} for s in planned}
            shacl_boundary = ({"side_effect": shacl_check.TooManyRows(self.cfg.verify.shacl_max_rows + 1,
                                                                      self.cfg.verify.shacl_max_rows)}
                              if "shacl" in limited else {"return_value": compared})
            members = {d.name: set() for d in self.cfg.ontology.defined}
            owl_boundary = ({"side_effect": owl.TooManyIndividuals(self.cfg.verify.owl_max_individuals + 1,
                                                                    self.cfg.verify.owl_max_individuals)}
                            if "owl" in limited else {"return_value": (owl.Graph({}, []), set(), members)})
            answer = owl.Reasoned(True, 0, 0, 0, 0, 0, members=members,
                                  outside={name: 0 for name in members})
            with patch.object(stage_reason, "components", return_value="synthetic-java"), \
                 patch.object(stage_reason, "check_owl_sources"), \
                 patch.object(stage_reason.db, "connect", return_value=SimpleNamespace(close=lambda: None)), \
                 patch.object(shacl_check, "run", **shacl_boundary), \
                 patch.object(owl, "closure_table_exists", return_value=True), \
                 patch.object(owl, "read", **owl_boundary), \
                 patch.object(owl, "reason", return_value=answer):
                result = stage_reason.run(self.cfg, SimpleNamespace(only=["shacl", "owl"]), out)
            result.inputs = fingerprint
            result.write(out)
            self.at(out, "reason", 1)
            return result, report.run(self.cfg, SimpleNamespace(), out), planned

    @staticmethod
    def at(out, stage, minute):
        path = out / stage / "result.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["started_at"] = data["finished_at"] = f"2026-10-14T00:0{minute}:00+0000"
        path.write_text(json.dumps(data), encoding="utf-8")

    def assert_blocked(self, limited):
        result, full, planned = self.reason_and_report(limited)
        self.assertEqual((result.status, full.status), ("not_run", "not_run"))
        self.assertEqual((result.exit_code(), full.exit_code()), (EXIT_FAIL, EXIT_FAIL))
        checks = {c.id: c for c in result.checks}
        expected = ({f"shacl:{s.id}" for s in planned} if "shacl" in limited else set())
        if "owl" in limited:
            expected |= {"owl:consistency", f"v5:closure:{self.cfg.ontology.hierarchy}"}
            expected |= {f"v5:defined:{d.name}" for d in self.cfg.ontology.defined}
        self.assertTrue(expected)
        for check_id in expected:
            self.assertIn(check_id, checks)
            self.assertEqual(checks[check_id].status, "not_run", check_id)
            self.assertEqual(checks[check_id].detail.get("reason"), "resource_limit", check_id)
            self.assertTrue(checks[check_id].config_ref)
        self.assertIn("limit", result.next_safe_action)
        self.assertIn("rerun reason", result.next_safe_action)
        self.assertNotIn("run `sxc.py cube`", result.next_safe_action)
        return result

    def test_only_shacl_over_limit_blocks_reason_and_report(self):
        result = self.assert_blocked({"shacl"})
        self.assertEqual(next(c.status for c in result.checks if c.id == "owl:consistency"), "pass")
        self.assertTrue(any("verify.shacl_max_rows" in text for text in result.not_proven))

    def test_only_owl_over_limit_blocks_reason_and_report(self):
        result = self.assert_blocked({"owl"})
        self.assertTrue(any(c.id.startswith("shacl:") and c.status == "pass" for c in result.checks))
        self.assertTrue(any("verify.owl_max_individuals" in text for text in result.not_proven))

    def test_both_limits_leave_explicit_checks(self):
        self.assert_blocked({"shacl", "owl"})

    def test_completed_checks_can_pass_with_coverage_notes(self):
        result, full, _ = self.reason_and_report(set())
        self.assertTrue(result.not_proven)  # SQL-only shapes and vacuous classes are intentionally disclosed.
        self.assertEqual((result.status, full.status), ("pass", "pass"))


class ExactNumericValues(unittest.TestCase):
    def test_distinct_large_numbers_and_keys_stay_distinct(self):
        for a, b in (("10000000000000000000000000001", "10000000000000000000000000002"),
                     ("1.12345678901234567890123456781", "1.12345678901234567890123456782")):
            with self.subTest(a=a):
                self.assertNotEqual(canon.canon(Decimal(a), "number"), canon.canon(b, "number"))
                self.assertNotEqual(canon.key(Decimal(a)), canon.key(Decimal(b)))

    def test_high_precision_mismatch_fails_row_comparison(self):
        gold = config.Gold("precision", "unused.sql", ["id"], map={"id": "items.id", "amount": "items.amount"})
        result = compare.compare_rows(gold,
                                      [{"id": "a", "amount": Decimal("10000000000000000000000000001")}],
                                      [{"id": "a", "amount": "10000000000000000000000000002"}],
                                      {"id": "string", "amount": "number"})
        self.assertFalse(result["pass"])
        self.assertEqual(result["mismatched_columns"], {"amount": 1})

    def test_numeric_formatting_is_independent_of_decimal_context(self):
        with localcontext() as context:
            context.prec, context.Emax, context.Emin = 3, 2, -2
            context.traps[Inexact] = context.traps[Rounded] = True
            try:
                self.assertEqual(canon.canon("123456789.00123000", "number"), "123456789.00123")
                self.assertEqual(canon.canon("1e10", "number"), "10000000000")
                self.assertEqual(canon.canon("1e-10", "number"), "0.0000000001")
            except DecimalException as error:
                self.fail(f"canonicalization used rounding Decimal arithmetic: {type(error).__name__}")

    def test_equal_representations_and_special_values_are_preserved(self):
        cases = {"+1.230000": "1.23", "123e-2": "1.23", "1000": "1000", "-0.0000": "0",
                 "0e1000000": "0", "NaN": "NaN", "Infinity": "Infinity", "-Infinity": "-Infinity"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(canon.canon(value, "number"), expected)


class UnknownConfigurationKeys(unittest.TestCase):
    def setUp(self):
        self.raw = tomllib.loads(pgfixture.DEMO_CONFIG.read_text(encoding="utf-8"))

    def load(self, raw):
        with patch.object(config.tomllib, "loads", return_value=raw):
            return config.load(pgfixture.DEMO_CONFIG)

    def reject(self, raw, ref):
        with self.assertRaises(StageError) as caught:
            self.load(raw)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertIn(ref, str(caught.exception))
        self.assertIn("unknown", str(caught.exception))
        return str(caught.exception)

    def test_misspelled_no_children_is_not_silently_dropped(self):
        raw = copy.deepcopy(self.raw)
        raw["ontology"]["no_chidlren"] = raw["ontology"].pop("no_children")
        self.reject(raw, "ontology")

    def test_unknown_fields_are_rejected_in_each_previously_permissive_table(self):
        for part in ("entity", "ontology", "defined", "param"):
            with self.subTest(part=part):
                raw = copy.deepcopy(self.raw)
                if part == "entity":
                    name = next(iter(raw["entities"]))
                    table, ref = raw["entities"][name], f"entities.{name}"
                elif part == "ontology":
                    table, ref = raw["ontology"], "ontology"
                elif part == "defined":
                    table, ref = raw["ontology"]["defined"][0], "ontology.defined[0]"
                else:
                    name = next(iter(raw["gold"][0]["params"]))
                    table, ref = raw["gold"][0]["params"][name], f"params.{name}"
                table["unexpected_option"] = "SecretValueThatMustNotBeEchoed"
                message = self.reject(raw, ref)
                self.assertNotIn("SecretValueThatMustNotBeEchoed", message)

    def test_unknown_quoted_key_does_not_echo_arbitrary_key_text(self):
        raw = copy.deepcopy(self.raw)
        marker = "private-key-text://DoNotEcho"
        raw["ontology"][marker] = "unused"
        self.assertNotIn(marker, self.reject(raw, "ontology"))

    def test_user_defined_dictionary_keys_remain_supported(self):
        raw = copy.deepcopy(self.raw)
        raw["ontology"]["classes"]["additional_kind"] = "AdditionalKind"
        loaded = self.load(raw)
        self.assertEqual(loaded.ontology.classes["additional_kind"], "AdditionalKind")
        self.assertEqual(loaded.ontology.no_children, ["raw"])
        self.assertEqual(set(loaded.entities), set(raw["entities"]))
        self.assertEqual(loaded.gold[0].map, raw["gold"][0]["map"])


if __name__ == "__main__":
    unittest.main()
