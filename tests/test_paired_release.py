"""Exercise the release comparison through its public offline CLI."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "paired_release.py"


class PairedReleaseTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        rows = [
            ("caught", "error", "heldout", "clear", "flag"),
            ("noise", "clean", "heldout", "clear", "flag"),
            ("missed", "error", "development", "flag", "clear"),
            ("fixed", "clean", "development", "flag", "clear"),
            ("both", "error", "heldout", "flag", "flag"),
            ("neither", "clean", "unknown", "clear", "clear"),
            ("partial", "error", "heldout", "not_run", "flag"),
            ("broken", "clean", "unknown", "clear", "error"),
        ]
        self.plan = dict(schema_version=1, experiment="compare", added_check="new",
                         baseline_checks=["old"], budget=8, cases=[])
        self.baseline = dict(schema_version=1, arm="baseline", checks=["old"],
                             budget=8, observations=[])
        self.candidate = dict(schema_version=1, arm="candidate", checks=["new", "old"],
                              budget=8, observations=[])
        for case_id, expected, exposure, baseline, candidate in rows:
            identity = dict(id=case_id, fixture_sha256=hashlib.sha256(case_id.encode()).hexdigest())
            self.plan["cases"].append(dict(identity, expected=expected, exposure=exposure))
            self.baseline["observations"].append(dict(identity, status=baseline))
            self.candidate["observations"].append(dict(identity, status=candidate))
        self.candidate["observations"].reverse()
        self.refresh_plan()

    def write(self, name, value):
        path = self.directory / (name + ".json")
        path.write_bytes(json.dumps(value, indent=1).encode("utf-8") + b"\n")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def refresh_plan(self):
        digest = self.write("plan", self.plan)
        self.baseline["plan_sha256"] = self.candidate["plan_sha256"] = digest

    def compare(self, claims=None, markdown=None, extra_args=()):
        self.write("baseline", self.baseline)
        self.write("candidate", self.candidate)
        args = [sys.executable, str(SCRIPT), "--plan", "plan.json",
                "--baseline", "baseline.json", "--candidate", "candidate.json"]
        if claims is not None:
            self.write("claims", claims)
            args += ["--claims", "claims.json"]
        if markdown is not None:
            (self.directory / "release.md").write_text(markdown, encoding="utf-8")
            args += ["--check-markdown", "release.md"]
        args += list(extra_args)
        result = subprocess.run(args, cwd=self.directory, capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_pairs_credit_only_observed_transitions_within_each_exposure(self):
        code, report = self.compare({"heldout.added_detections": 1, "development.new_false_positives": 0})
        self.assertEqual(code, 0, report)
        groups = report["groups"]
        self.assertEqual(set(groups), {"heldout", "development", "unknown"})
        self.assertEqual(groups["heldout"]["cases"], 4)
        for exposure, metric, ids in [
            ("heldout", "added_detections", ["caught"]),
            ("heldout", "new_false_positives", ["noise"]),
            ("heldout", "inconclusive", ["partial"]),
            ("development", "lost_detections", ["missed"]),
            ("development", "fixed_false_positives", ["fixed"]),
            ("unknown", "inconclusive", ["broken"]),
            ("unknown", "added_detections", []),
        ]:
            with self.subTest(exposure=exposure, metric=metric):
                self.assertEqual(groups[exposure][metric], len(ids))
                self.assertEqual(groups[exposure]["case_ids"][metric], ids)
        pairs = {row["id"]: row for row in report["pairs"]}
        self.assertEqual((pairs["partial"]["baseline"], pairs["broken"]["candidate"]), ("not_run", "error"))
        self.assertEqual((pairs["both"]["outcome"], pairs["neither"]["outcome"]), ("unchanged", "unchanged"))
        self.assertEqual(pairs["caught"]["fixture_sha256"], self.plan["cases"][0]["fixture_sha256"])
        for name in ("plan", "baseline", "candidate", "claims"):
            raw = (self.directory / (name + ".json")).read_bytes()
            self.assertEqual(report["input_sha256"][name], hashlib.sha256(raw).hexdigest())

    def test_false_claim_displays_observed_value_and_preserves_evidence(self):
        code, report = self.compare({"heldout.added_detections": 2})
        self.assertEqual(code, 1)
        self.assertEqual(report["claim_mismatches"], {"heldout.added_detections": {"claimed": 2, "observed": 1}})
        self.assertEqual(report["groups"]["heldout"]["case_ids"]["added_detections"], ["caught"])

    def test_release_table_matches_recomputed_claims_and_exact_rows(self):
        claims = {"heldout.added_detections": 1, "development.lost_detections": 1}
        document = ("Prose before\n<!-- paired-claims:begin -->\n| Claim | Count |\n"
                    "| --- | ---: |\n| `development.lost_detections` | 1 |\n"
                    "| `heldout.added_detections` | 1 |\n<!-- paired-claims:end -->\nProse after")
        self.assertEqual(self.compare(claims, document)[0], 0)
        for bad in [
            document.replace("detections` | 1 |", "detections` | 2 |"),
            document.replace("| `development.lost_detections` | 1 |\n", ""),
            document.replace("| Claim | Count |", "| Claim | Count |\n| extra | 3 |"),
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(self.compare(claims, bad)[0], 1)
        self.assertEqual(self.compare(dict(claims, **{"heldout.added_detections": 2}),
                                      document.replace("`heldout.added_detections` | 1",
                                                       "`heldout.added_detections` | 2"))[0], 1)
        for bad in [
            "no markers", document + "\n<!-- paired-claims:end -->",
            document.replace("begin", "temp").replace("end", "begin").replace("temp", "end"),
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(self.compare(claims, bad)[0], 2)
        self.assertEqual(self.compare(None, document)[0], 2)

    def test_rejects_unknown_noninteger_and_vacuous_claims(self):
        for claims in [
            {}, {"heldout.added_detections": True}, {"heldout.added_detections": 1.0},
            {"heldout.added_detections": -1}, {"heldout.new_detections": 0},
            {"heldout.case_ids.cases": 0}, {"pooled.cases": 8},
            {"unknown.added_detections": 0},  # no comparable error in this stratum
        ]:
            with self.subTest(claims=claims):
                self.assertEqual(self.compare(claims)[0], 2)

    def test_changing_only_raw_plan_bytes_invalidates_both_receipts(self):
        with (self.directory / "plan.json").open("ab") as stream:
            stream.write(b" ")
        code, report = self.compare()
        self.assertEqual(code, 2)
        self.assertIn("plan_sha256", report["error"])

    def test_no_baseline_checks_and_a_group_with_no_cases(self):
        self.plan["baseline_checks"] = []
        self.baseline["checks"] = []
        self.candidate["checks"] = ["new"]
        for item in self.plan["cases"]:
            item["exposure"] = "development"
        self.refresh_plan()
        code, report = self.compare()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["groups"]["heldout"]["cases"], 0)
        self.assertEqual(report["groups"]["development"]["cases"], 8)
        self.assertEqual(self.compare({"heldout.inconclusive": 0})[0], 2)

    def test_attempt_budget_counts_errors_and_excludes_not_run(self):
        for record in (self.plan, self.baseline, self.candidate):
            record["budget"] = 7
        self.refresh_plan()
        # Baseline has one not_run and seven attempts. Candidate has seven
        # conclusive observations plus one errored attempt.
        code, report = self.compare()
        self.assertEqual(code, 2)
        self.assertIn("candidate", report["error"])
        self.candidate["observations"][-1]["status"] = "not_run"
        self.assertEqual(self.compare()[0], 0)

    def test_registered_claim_paths_bind_coverage_before_observation(self):
        claims = {"heldout.added_detections": 1, "heldout.new_false_positives": 1}
        self.plan["registered_claims"] = list(reversed(claims))
        self.refresh_plan()
        self.assertEqual(self.compare()[0], 0)  # still usable as a raw comparison
        self.assertEqual(self.compare(claims)[0], 0)
        self.assertEqual(self.compare({"heldout.added_detections": 1})[0], 2)
        self.assertEqual(self.compare(dict(claims, **{"heldout.cases": 4}))[0], 2)
        for bad in [[], ["other.added_detections"], ["heldout.cases", "heldout.cases"], [True]]:
            with self.subTest(bad=bad):
                self.plan["registered_claims"] = bad
                self.refresh_plan()
                self.assertEqual(self.compare()[0], 2)

    def test_unpaired_duplicate_or_changed_observations_are_invalid(self):
        first, *others = self.baseline["observations"]
        for observations in [
            others, [first, first, *others], [dict(first, id="unplanned"), *others],
            [dict(first, fixture_sha256="f" * 64), *others],
            [dict(first, status="unknown"), *others], [dict(first, status=[]), *others],
            [{"id": first["id"], "status": "clear"}, *others],
        ]:
            with self.subTest(first=observations[0]):
                self.baseline["observations"] = observations
                self.assertEqual(self.compare()[0], 2)

    def test_rejects_wrong_budget_arm_schema_or_enabled_checks(self):
        for receipt, key, bad in [
            (self.baseline, "budget", 6), (self.baseline, "budget", True),
            (self.baseline, "checks", ["old", "old"]), (self.candidate, "checks", ["new"]),
            (self.candidate, "checks", ["old", "new", "extra"]), (self.baseline, "arm", "candidate"),
            (self.candidate, "schema_version", True), (self.baseline, "schema_version", 2),
        ]:
            with self.subTest(arm=receipt["arm"], key=key, bad=bad):
                original = receipt[key]
                receipt[key] = bad
                self.assertEqual(self.compare()[0], 2)
                receipt[key] = original

    def test_plan_requires_distinct_fixtures_ids_and_a_single_added_check(self):
        first, second, *others = self.plan["cases"]
        for key, bad in [
            ("cases", [first, dict(second, id=first["id"]), *others]),
            ("cases", [first, dict(second, fixture_sha256=first["fixture_sha256"]), *others]),
            ("cases", [dict(first, fixture_sha256="A" * 64), second, *others]),
            ("cases", [dict(first, expected="maybe"), second, *others]),
            ("cases", [dict(first, exposure="unseen"), second, *others]),
            ("cases", []), ("added_check", "old"), ("baseline_checks", ["old", "old"]),
            ("budget", 0), ("budget", 5.0),
        ]:
            with self.subTest(key=key):
                original = self.plan[key]
                self.plan[key] = bad
                self.refresh_plan()
                self.assertEqual(self.compare()[0], 2)
                self.plan[key] = original

    def test_duplicate_json_keys_and_nonstandard_constants_are_invalid(self):
        for prefix in (b'{"budget":5,', b'{"metadata":NaN,'):
            with self.subTest(prefix=prefix):
                self.refresh_plan()
                path = self.directory / "plan.json"
                raw = prefix + path.read_bytes()[1:]
                path.write_bytes(raw)
                self.baseline["plan_sha256"] = self.candidate["plan_sha256"] = hashlib.sha256(raw).hexdigest()
                self.assertEqual(self.compare()[0], 2)

    def _compare_table(self, expected, a, b, c, d):
        """Build a single-stratum heldout comparison from a 2x2 matched-pair table."""
        rows = (
            [("flag", "flag")] * a
            + [("clear", "flag")] * b
            + [("flag", "clear")] * c
            + [("clear", "clear")] * d
        )
        n = len(rows)
        self.plan = dict(schema_version=1, experiment="stats", added_check="new",
                         baseline_checks=["old"], budget=max(n, 1), cases=[])
        self.baseline = dict(schema_version=1, arm="baseline", checks=["old"],
                             budget=max(n, 1), observations=[])
        self.candidate = dict(schema_version=1, arm="candidate", checks=["new", "old"],
                              budget=max(n, 1), observations=[])
        for idx, (before, after) in enumerate(rows):
            case_id = f"c{idx}"
            identity = dict(id=case_id, fixture_sha256=hashlib.sha256(case_id.encode()).hexdigest())
            self.plan["cases"].append(dict(identity, expected=expected, exposure="heldout"))
            self.baseline["observations"].append(dict(identity, status=before))
            self.candidate["observations"].append(dict(identity, status=after))
        self.refresh_plan()
        code, report = self.compare()
        self.assertEqual(code, 0, report)
        bucket = "detections" if expected == "error" else "false_positives"
        return report["groups"]["heldout"]["statistics"][bucket]

    def test_small_sample_mcnemar_and_newcombe_wilson_statistics(self):
        # Heldout v0.30.0 error table: n=3, a=1, b=2, c=0, d=0
        stats = self._compare_table("error", a=1, b=2, c=0, d=0)
        self.assertEqual(stats["n"], 3)
        self.assertEqual((stats["both_flag"], stats["added"], stats["lost"], stats["both_clear"]), (1, 2, 0, 0))
        self.assertEqual(stats["discordant"], 2)
        self.assertAlmostEqual(stats["baseline_rate"], 0.333333, places=6)
        self.assertEqual(stats["candidate_rate"], 1.0)
        self.assertAlmostEqual(stats["rate_diff"], 0.666667, places=6)
        self.assertEqual(stats["exact_mcnemar_p"], 0.5)
        self.assertEqual(stats["midp_mcnemar_p"], 0.25)
        self.assertEqual(stats["safe_e_value"], 1.5)
        self.assertAlmostEqual(stats["safe_e_log"], 0.405465, places=6)
        self.assertAlmostEqual(stats["safe_anytime_p"], 0.666667, places=6)
        self.assertEqual(stats["min_discordant_for_midp_005"], 5)
        self.assertEqual(stats["min_discordant_for_exact_005"], 6)
        self.assertEqual(stats["min_discordant_for_e_005"], 7)
        self.assertTrue(stats["underpowered_for_midp_005"])
        self.assertTrue(stats["underpowered_for_exact_005"])
        self.assertTrue(stats["underpowered_for_e_005"])
        # Zero-denominator Newcombe Method 10 (A=0 -> phi=0) crosses zero on n=3.
        lo, hi = stats["diff_ci_95"]
        self.assertAlmostEqual(lo, -0.058569, places=6)
        self.assertAlmostEqual(hi, 0.938508, places=6)

    def test_mcnemar_thresholds_ties_and_symmetry(self):
        # b=5, c=0 crosses Mid-P 0.05 (0.03125) but not exact 0.05 (0.0625).
        s5 = self._compare_table("error", a=0, b=5, c=0, d=0)
        self.assertEqual(s5["exact_mcnemar_p"], 0.0625)
        self.assertEqual(s5["midp_mcnemar_p"], 0.03125)
        self.assertFalse(s5["underpowered_for_midp_005"])
        self.assertTrue(s5["underpowered_for_exact_005"])

        # b=6, c=0 crosses both Mid-P (0.015625) and exact (0.03125).
        s6 = self._compare_table("error", a=0, b=6, c=0, d=0)
        self.assertEqual(s6["exact_mcnemar_p"], 0.03125)
        self.assertEqual(s6["midp_mcnemar_p"], 0.015625)
        self.assertFalse(s6["underpowered_for_midp_005"])
        self.assertFalse(s6["underpowered_for_exact_005"])

        # Tie b=c=0 gives p=1.0 for both; tie b=c=1 gives exact=1.0, midp=1 - 0.5*C(2,1)/4 = 0.75.
        s_zero = self._compare_table("clean", a=0, b=0, c=0, d=4)
        self.assertEqual((s_zero["exact_mcnemar_p"], s_zero["midp_mcnemar_p"]), (1.0, 1.0))
        self.assertEqual(s_zero["fixed"], 0)
        s_tie = self._compare_table("error", a=2, b=1, c=1, d=2)
        self.assertEqual(s_tie["exact_mcnemar_p"], 1.0)
        self.assertEqual(s_tie["midp_mcnemar_p"], 0.75)

        # Positive correlation (ad > bc) and negative correlation (ad < bc) plus antisymmetry of CI.
        pos_fwd = self._compare_table("error", a=10, b=6, c=1, d=8)
        pos_rev = self._compare_table("error", a=10, b=1, c=6, d=8)
        self.assertAlmostEqual(pos_fwd["exact_mcnemar_p"], 0.125, places=12)
        self.assertAlmostEqual(pos_fwd["midp_mcnemar_p"], 0.0703125, places=12)
        self.assertEqual(pos_fwd["diff_ci_95"][0], -pos_rev["diff_ci_95"][1])
        self.assertEqual(pos_fwd["diff_ci_95"][1], -pos_rev["diff_ci_95"][0])
        self.assertGreaterEqual(pos_fwd["diff_ci_95"][0], -1.0)
        self.assertLessEqual(pos_fwd["diff_ci_95"][1], 1.0)
        self.assertLessEqual(pos_fwd["diff_ci_95"][0], pos_fwd["rate_diff"])
        self.assertLessEqual(pos_fwd["rate_diff"], pos_fwd["diff_ci_95"][1])

        neg = self._compare_table("error", a=1, b=5, c=4, d=1)
        self.assertLessEqual(neg["diff_ci_95"][0], neg["rate_diff"])
        self.assertLessEqual(neg["rate_diff"], neg["diff_ci_95"][1])

    def test_empty_stratum_and_statistics_claim_path_rejection(self):
        code, report = self.compare()
        self.assertEqual(code, 0, report)
        # unknown stratum in default setUp has no conclusive error cases
        unk_det = report["groups"]["unknown"]["statistics"]["detections"]
        self.assertEqual(unk_det["n"], 0)
        self.assertIsNone(unk_det["baseline_rate"])
        self.assertIsNone(unk_det["candidate_rate"])
        self.assertIsNone(unk_det["rate_diff"])
        self.assertIsNone(unk_det["diff_ci_95"])
        self.assertIsNone(unk_det["exact_mcnemar_p"])
        self.assertIsNone(unk_det["midp_mcnemar_p"])
        self.assertIsNone(unk_det["safe_e_value"])
        self.assertIsNone(unk_det["safe_e_log"])
        self.assertIsNone(unk_det["safe_anytime_p"])
        self.assertTrue(unk_det["underpowered_for_midp_005"])
        self.assertTrue(unk_det["underpowered_for_exact_005"])
        self.assertTrue(unk_det["underpowered_for_e_005"])
        # statistics is never accepted as an integer claim path
        self.assertEqual(self.compare({"heldout.statistics": 0})[0], 2)

    def test_safe_evalue_martingale_validity_thresholds_and_pooling(self):
        import math
        import sys as _sys
        tools_dir = str(SCRIPT.parent)
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        import paired_release

        # Exact martingale property: E_H0[E(B, m - B)] == 1 for every m.
        for m in (0, 1, 2, 5, 7, 10):
            expected = sum(
                math.comb(m, b) * (0.5 ** m) * math.exp(paired_release.safe_mcnemar_log_evalue(b, m - b))
                for b in range(m + 1)
            )
            with self.subTest(m=m):
                self.assertAlmostEqual(expected, 1.0, places=12)

        # Symmetry and zero-discordant identity.
        self.assertEqual(paired_release.safe_mcnemar_evalue(0, 0), (1.0, 0.0, 1.0))
        self.assertEqual(paired_release.safe_mcnemar_log_evalue(7, 1), paired_release.safe_mcnemar_log_evalue(1, 7))
        self.assertEqual(paired_release.safe_mcnemar_evalue(7, 1), paired_release.safe_mcnemar_evalue(1, 7))

        # Threshold crossing: b=6, c=0 stays below 20; b=7, c=0 crosses 20 (anytime p < 0.05).
        s6 = self._compare_table("error", a=0, b=6, c=0, d=0)
        self.assertAlmostEqual(s6["safe_e_value"], 14.4375, places=6)
        self.assertGreater(s6["safe_anytime_p"], 0.05)
        self.assertTrue(s6["underpowered_for_e_005"])

        s7 = self._compare_table("error", a=0, b=7, c=0, d=0)
        self.assertAlmostEqual(s7["safe_e_value"], 26.8125, places=6)
        self.assertLess(s7["safe_anytime_p"], 0.05)
        self.assertFalse(s7["underpowered_for_e_005"])

        # Independent cohorts pool multiplicatively (additively in log space).
        log1 = paired_release.safe_mcnemar_log_evalue(4, 0)
        log2 = paired_release.safe_mcnemar_log_evalue(4, 0)
        self.assertAlmostEqual(math.exp(log1) * math.exp(log2), math.exp(log1 + log2), places=10)

    def test_large_discordant_count_does_not_overflow_float(self):
        import math
        import sys as _sys
        tools_dir = str(SCRIPT.parent)
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        import paired_release
        exact_p, midp = paired_release.mcnemar_pvalues(1200, 300)
        self.assertGreaterEqual(exact_p, 0.0)
        self.assertLessEqual(exact_p, 1.0)
        self.assertGreaterEqual(midp, 0.0)
        self.assertLessEqual(midp, exact_p)
        e_val, log_e, anytime_p = paired_release.safe_mcnemar_evalue(1200, 300)
        self.assertTrue(math.isfinite(log_e))
        self.assertGreater(log_e, 0.0)
        self.assertGreaterEqual(anytime_p, 0.0)
        self.assertLessEqual(anytime_p, 1.0)
        if e_val is not None:
            self.assertTrue(math.isfinite(e_val))
        # Extreme discordant counts (> exp(700)) stay JSON-finite with e_val=None and finite log_e.
        huge_e, huge_log, huge_p = paired_release.safe_mcnemar_evalue(2000, 0)
        self.assertIsNone(huge_e)
        self.assertTrue(math.isfinite(huge_log))
        self.assertEqual(huge_p, 0.0)

    def test_seal_plan_amendment_dual_verdict_and_strict_original(self):
        self.plan["frozen_utc"] = "2026-11-23T00:00:00Z"
        self.plan["claim_limit"] = "Synthetic SQLite inventory fixtures only; not live PostgreSQL."
        self.refresh_plan()

        seal = subprocess.run(
            [sys.executable, str(SCRIPT), "--plan", "plan.json", "--seal-plan"],
            cwd=self.directory, capture_output=True, text=True,
        )
        self.assertEqual(seal.returncode, 0, seal.stdout + seal.stderr)
        sealed_out = json.loads(seal.stdout)
        core_sha = sealed_out["frozen_core_sha256"]
        self.assertRegex(core_sha, r"^[0-9a-f]{64}$")

        # Append a post-hoc amendment excluding "noise" (a heldout false positive); core digest stays valid.
        self.plan["frozen_core_sha256"] = core_sha
        self.plan["amendments"] = [
            {
                "id": "post-hoc-drop-noise",
                "timing": "post_hoc",
                "recorded_utc": "2026-11-23T02:00:00Z",
                "what_changed": "Exclude heldout clean case 'noise' from amended summary",
                "why": "Owner wanted to inspect impact without the single noisy fixture",
                "exclude_case_ids": ["noise"],
            }
        ]
        self.baseline["measured_utc"] = "2026-11-23T01:00:00Z"
        self.candidate["measured_utc"] = "2026-11-23T01:05:00Z"
        self.refresh_plan()

        code, report = self.compare()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["claim_limit"], self.plan["claim_limit"])
        self.assertEqual(report["frozen_core_sha256"], core_sha)
        # Original groups stay untouched and still show the heldout false positive.
        self.assertEqual(report["groups"]["heldout"]["new_false_positives"], 1)
        aa = report["amendment_audit"]
        self.assertTrue(aa["has_post_hoc"])
        self.assertTrue(aa["post_hoc_altered_summary"])
        self.assertEqual(aa["post_hoc_excluded_case_ids"], ["noise"])
        self.assertEqual(aa["original_groups"]["heldout"]["new_false_positives"], 1)
        self.assertEqual(aa["amended_groups"]["heldout"]["new_false_positives"], 0)

        # --strict-original fails closed when a post_hoc amendment alters the summary.
        strict_code, _ = self.compare(extra_args=("--strict-original",))
        self.assertEqual(strict_code, 1)

        # Tampering with a pre-registered core field invalidates frozen_core_sha256.
        self.plan["budget"] = 9
        self.baseline["budget"] = self.candidate["budget"] = 9
        self.refresh_plan()
        bad_code, bad_rep = self.compare()
        self.assertEqual(bad_code, 2)
        self.assertIn("frozen_core_sha256", bad_rep["error"])

    def test_amendment_validation_and_chronology_fail_closed(self):
        self.plan["frozen_utc"] = "2026-11-23T01:00:00Z"
        self.refresh_plan()
        seal = json.loads(subprocess.check_output(
            [sys.executable, str(SCRIPT), "--plan", "plan.json", "--seal-plan"],
            cwd=self.directory, text=True,
        ))
        core_sha = seal["frozen_core_sha256"]

        # Amendments without frozen_core_sha256 are rejected.
        self.plan["amendments"] = [{
            "id": "a1", "timing": "pre_measurement", "recorded_utc": "2026-11-23T01:10:00Z",
            "what_changed": "note", "why": "reason",
        }]
        self.refresh_plan()
        self.assertEqual(self.compare()[0], 2)

        # pre_measurement dated after measured_utc is rejected.
        self.plan["frozen_core_sha256"] = core_sha
        self.baseline["measured_utc"] = "2026-11-23T01:05:00Z"
        self.candidate["measured_utc"] = "2026-11-23T01:06:00Z"
        self.refresh_plan()
        code, rep = self.compare()
        self.assertEqual(code, 2)
        self.assertIn("dated after receipt measured_utc", rep["error"])

        # measured_utc before frozen_utc is rejected.
        self.plan["amendments"] = []
        self.baseline["measured_utc"] = "2026-11-23T00:30:00Z"
        self.refresh_plan()
        code2, rep2 = self.compare()
        self.assertEqual(code2, 2)
        self.assertIn("precedes plan.frozen_utc", rep2["error"])

        # Non-hashable timing or unknown exclude_case_ids rejected cleanly (exit 2, no TypeError).
        self.baseline["measured_utc"] = "2026-11-23T01:30:00Z"
        for bad_amend in (
            {"id": "bad-t", "timing": ["post_hoc"], "recorded_utc": "2026-11-23T01:10:00Z",
             "what_changed": "x", "why": "y"},
            {"id": "bad-id", "timing": "post_hoc", "recorded_utc": "2026-11-23T01:10:00Z",
             "what_changed": "x", "why": "y", "exclude_case_ids": ["nonexistent"]},
            {"id": "all-drop", "timing": "post_hoc", "recorded_utc": "2026-11-23T01:10:00Z",
             "what_changed": "x", "why": "y", "exclude_case_ids": [c["id"] for c in self.plan["cases"]]},
        ):
            with self.subTest(bad_amend=bad_amend["id"]):
                self.plan["amendments"] = [bad_amend]
                self.refresh_plan()
                self.assertEqual(self.compare()[0], 2)

    def test_activation_audit_detects_silent_noop_control_leakage_and_missing_telemetry(self):
        # Without telemetry, --check-activation fails closed with exit 70 (missing_telemetry).
        code, rep = self.compare(extra_args=("--check-activation",))
        self.assertEqual(code, 70)
        self.assertEqual(rep["activation_audit"]["status"], "missing_telemetry")

        # Add valid telemetry: baseline=0, candidate executed=1 (error/not_run=0).
        for row in self.baseline["observations"]:
            row["activation"] = {"added_check_calls": 0, "fallback_calls": 0}
        for row in self.candidate["observations"]:
            calls = 1 if row["status"] in ("clear", "flag") else 0
            row["activation"] = {"added_check_calls": calls, "fallback_calls": 0}
        code_ok, rep_ok = self.compare(extra_args=("--check-activation",))
        self.assertEqual(code_ok, 0, rep_ok)
        self.assertEqual(rep_ok["activation_audit"]["status"], "passed")
        self.assertEqual(rep_ok["activation_audit"]["baseline_added_check_calls"], 0)
        self.assertEqual(rep_ok["activation_audit"]["candidate_added_check_calls"], 7)

        # Silent no-op on one executed candidate case -> exit 70.
        executed_row = next(r for r in self.candidate["observations"] if r["status"] in ("clear", "flag"))
        executed_row["activation"]["added_check_calls"] = 0
        code_noop, rep_noop = self.compare(extra_args=("--check-activation",))
        self.assertEqual(code_noop, 70)
        self.assertEqual(rep_noop["activation_audit"]["status"], "silent_noop")
        self.assertEqual(rep_noop["activation_audit"]["candidate_inactive_executed_case_ids"], [executed_row["id"]])

        # Control leakage in baseline while candidate also inactive -> both_failed, exit 70.
        self.baseline["observations"][0]["activation"]["added_check_calls"] = 1
        code_both, rep_both = self.compare(extra_args=("--check-activation",))
        self.assertEqual(code_both, 70)
        self.assertEqual(rep_both["activation_audit"]["status"], "both_failed")

        # Restore candidate -> control_leakage alone, exit 70.
        executed_row["activation"]["added_check_calls"] = 1
        code_leak, rep_leak = self.compare(extra_args=("--check-activation",))
        self.assertEqual(code_leak, 70)
        self.assertEqual(rep_leak["activation_audit"]["status"], "control_leakage")

        # Invalid bool or negative activation counts are rejected as schema errors (exit 2).
        for bad_act in ({"added_check_calls": True}, {"added_check_calls": -1}, {"added_check_calls": 0, "fallback_calls": False}, "bad"):
            with self.subTest(bad_act=bad_act):
                self.baseline["observations"][0]["activation"] = bad_act
                self.assertEqual(self.compare(extra_args=("--check-activation",))[0], 2)


if __name__ == "__main__":
    unittest.main()
