"""Check published numeric claims against the two raw observations in the source archive."""

import json
import subprocess
import sys
import unittest
from pathlib import Path


class ReleaseClaimTableTests(unittest.TestCase):
    def test_v0300_table_matches_paired_observations(self):
        root = Path(__file__).resolve().parents[1]
        example = root / "examples" / "paired-release"
        command = [sys.executable, str(root / "tools" / "paired_release.py")]
        for name in ("plan", "baseline", "candidate", "claims"):
            command.extend(["--" + name, str(example / (name + ".json"))])
        command.extend(["--check-markdown", str(root / "docs" / "releases" / "v0.30.0.md")])
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertIs(report["markdown_matches"], True)
        self.assertEqual(report["claim_mismatches"], {})
