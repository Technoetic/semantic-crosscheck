"""Tests for tools/check_bom.py UTF-8 BOM rejection guard."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_bom.py"


class CheckBomTests(unittest.TestCase):
    def run_guard(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def test_clean_files_pass_and_bom_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "clean.py"
            bom = Path(tmp) / "bom.py"
            clean.write_bytes(b"print('ok')\n")
            bom.write_bytes(b"\xef\xbb\xbfprint('bom')\n")

            ok = self.run_guard(str(clean))
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)

            bad = self.run_guard(str(clean), str(bom))
            self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
            self.assertIn("bom.py", bad.stdout + bad.stderr)

    def test_missing_explicit_file_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist.py"
            result = self.run_guard(str(missing))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_repository_has_no_tracked_utf8_bom_files(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
