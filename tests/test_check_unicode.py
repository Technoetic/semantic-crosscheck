"""Tests for tools/check_unicode.py invisible Unicode and Bidi smuggling guard."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_unicode.py"


class CheckUnicodeTests(unittest.TestCase):
    def run_guard(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_normal_ascii_and_korean_utf8_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / "clean.toml"
            clean.write_text('# 한글 주석과 정상 SQL 텍스트\nname = "부품-01"\n', encoding="utf-8")
            proc = self.run_guard(str(clean))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_bidi_and_default_ignorable_smuggling_fail_without_echoing_secret_text(self) -> None:
        cases = [
            ("bidi_rlo.sql", f"SELECT 1 -- SECRET_VALUE_{chr(0x202E)}_END\n", "U+202E"),
            ("bidi_lri.toml", f'note = "SAFE_{chr(0x2066)}_VAL"\n', "U+2066"),
            ("zwsp.toml", f'key = "A{chr(0x200B)}B"\n', "U+200B"),
            ("cgj.py", f'x = "TOK{chr(0x034F)}EN"\n', "U+034F"),
            ("soft_hyphen.md", f"soft{chr(0x00AD)}hyphen\n", "U+00AD"),
            ("hangul_filler.sql", f"SELECT '{chr(0x3164)}'\n", "U+3164"),
            ("tag_smuggling.md", f"tag{chr(0xE0041)}end\n", "U+E0041"),
            ("mid_bom.toml", f'a = "x{chr(0xFEFF)}y"\n', "U+FEFF"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for filename, content, codepoint in cases:
                with self.subTest(filename=filename, codepoint=codepoint):
                    target = Path(tmp) / filename
                    target.write_text(content, encoding="utf-8")
                    proc = self.run_guard(str(target))
                    combined = proc.stdout + proc.stderr
                    self.assertEqual(proc.returncode, 1, combined)
                    self.assertIn(codepoint, combined)
                    self.assertNotIn("SECRET_VALUE", combined)

    def test_missing_explicit_file_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.toml"
            proc = self.run_guard(str(missing))
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_repository_has_no_tracked_forbidden_unicode(self) -> None:
        proc = self.run_guard()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
