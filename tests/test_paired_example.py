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


if __name__ == "__main__":
    unittest.main()
