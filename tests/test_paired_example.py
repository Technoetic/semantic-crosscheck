"""Replay a frozen fixture pack and detect drift in the example producer."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import run_paired_example as runner


class PairedExampleTests(unittest.TestCase):
    def test_checked_in_receipts_replay(self):
        result = subprocess.run([sys.executable, str(Path(runner.__file__))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_producer_refuses_replaced_fixture_even_with_equivalent_json(self):
        plan = json.loads((runner.EXAMPLE / "plan.json").read_bytes())
        case = plan["cases"][0]
        filename = case["id"] + ".json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cases").mkdir()
            (root / "cases" / filename).write_bytes((runner.EXAMPLE / "cases" / filename).read_bytes() + b" ")
            (root / "plan.json").write_text(json.dumps(dict(plan, cases=[case], budget=1)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixture hash mismatch"):
                runner.generate(root / "plan.json")

    def test_malformed_fixture_cannot_fall_back_to_clean_query(self):
        good = json.loads((runner.EXAMPLE / "cases" / "dev-return.json").read_bytes())
        for change in ({"model_mode": "other"}, {"gold_mode": True}, {"extra": True},
                       {"facts": [{"sku": "A", "units": False}]}, {"expected_rows": [{"sku": "A"}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.load_fixture(json.dumps(dict(good, **change)).encode())

    def test_with_activation_replay_passes_paired_release_activation_audit(self):
        paired_script = Path(__file__).resolve().parents[1] / "tools" / "paired_release.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_out = root / "baseline.json"
            cand_out = root / "candidate.json"
            proc = subprocess.run(
                [sys.executable, str(Path(runner.__file__)),
                 "--plan", str(runner.EXAMPLE / "plan.json"),
                 "--baseline", str(base_out), "--candidate", str(cand_out),
                 "--write", "--with-activation"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            check = subprocess.run(
                [sys.executable, str(paired_script),
                 "--plan", str(runner.EXAMPLE / "plan.json"),
                 "--baseline", str(base_out), "--candidate", str(cand_out),
                 "--check-activation"],
                capture_output=True, text=True,
            )
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            report = json.loads(check.stdout)
            audit = report["activation_audit"]
            self.assertEqual(audit["status"], "passed")
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["baseline_added_check_calls"], 0)
            self.assertEqual(audit["candidate_added_check_calls"], 10)
            self.assertEqual(audit["candidate_executed_cases"], 10)

            # Mutating candidate to silent no-op must fail with exit 70.
            cand_data = json.loads(cand_out.read_text(encoding="utf-8"))
            cand_data["observations"][0]["activation"]["added_check_calls"] = 0
            cand_out.write_text(json.dumps(cand_data, indent=2) + "\n", encoding="utf-8")
            noop = subprocess.run(
                [sys.executable, str(paired_script),
                 "--plan", str(runner.EXAMPLE / "plan.json"),
                 "--baseline", str(base_out), "--candidate", str(cand_out),
                 "--check-activation"],
                capture_output=True, text=True,
            )
            self.assertEqual(noop.returncode, 70, noop.stdout + noop.stderr)
            self.assertEqual(json.loads(noop.stdout)["activation_audit"]["status"], "silent_noop")


if __name__ == "__main__":
    unittest.main()
