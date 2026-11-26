"""Guard against raw assert statements in production/CI code and exercise tools/portability_check.py."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PortabilityAndAssertGuardTest(unittest.TestCase):
    def test_no_raw_assert_statements_in_production_scripts_or_tools(self):
        offenders: list[str] = []
        scan_roots = (ROOT / "skills" / "semantic-crosscheck" / "scripts", ROOT / "tools")
        for base in scan_roots:
            for path in sorted(base.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assert):
                        offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertEqual(offenders, [], f"production/tool code must not rely on assert (stripped by python -O): {offenders}")

    def test_no_raw_assert_statements_in_ci_workflow(self):
        workflow = ROOT / ".github" / "workflows" / "tests.yml"
        offenders = [
            f"{idx}: {line.strip()}"
            for idx, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1)
            if line.strip().startswith("assert ")
        ]
        self.assertEqual(offenders, [], f"CI workflow must not rely on assert: {offenders}")

    def test_portability_check_passes_under_python_O_B(self):
        proc = subprocess.run(
            [sys.executable, "-O", "-B", str(ROOT / "tools" / "portability_check.py"), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], "semantic-crosscheck/portability-check/v1")
        self.assertTrue(payload["all_passed"])
        self.assertTrue(payload["optimize_flag_verified"])
        self.assertGreaterEqual(payload["checks_passed"], 8)
        self.assertNotIn(str(ROOT), proc.stdout)


if __name__ == "__main__":
    unittest.main()
