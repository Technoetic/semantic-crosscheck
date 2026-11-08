"""A configuration without [data_rules] behaves exactly as in v0.21.1.

The goldens in tests/fixtures/compat_v0_21_1 were written by its capture.py on v0.21.1, before any data-rules code
existed: the SQL of every shape view, shapes.ttl, fk_review.sql, report.TRUSTED_NOTE, stage_build.BUILD_SQL_HEADER,
the inputs_fingerprint keys, and two offline `build` + `report` runs of demo/crosscheck.toml through sxc.main with a
recording stand-in for the database (every statement build sends, build.sql, result.json, the console summaries,
report.md and report.json, and the check lists). Every golden must stay byte-identical; capture.py normalises only
run times, the engine fingerprint (new modules change it by design) and output paths. The command-line stage list
may gain data-rules and nothing else. No database is needed.
"""

import importlib.util
import unittest

import pgfixture
import sxc

GOLDEN = pgfixture.ROOT / "tests" / "fixtures" / "compat_v0_21_1"
STAGES = "static/stages.txt"
NEW_STAGES = {"data-rules"}


def load_capture():
    spec = importlib.util.spec_from_file_location("sxc_compat_capture_v0_21_1", GOLDEN / "capture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompatibilityGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture = load_capture()
        cls.current = cls.capture.collect()

    def golden(self, rel: str) -> bytes:
        return (GOLDEN / rel).read_bytes()

    def test_every_golden_is_byte_identical(self):
        for rel, text in self.current.items():
            if rel == STAGES:
                continue
            with self.subTest(golden=rel):
                golden = self.golden(rel)
                self.assertEqual(text, golden.decode("utf-8"))
                self.assertEqual(text.encode("utf-8"), golden)

    def test_no_golden_is_missing_or_unexpected(self):
        found = {p.relative_to(GOLDEN).as_posix() for p in GOLDEN.rglob("*")
                 if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts}
        self.assertEqual(found, set(self.current))

    def test_the_stage_list_gains_only_data_rules(self):
        golden = self.golden(STAGES).decode("utf-8").splitlines()
        current = self.current[STAGES].splitlines()
        self.assertEqual(current, sorted(sxc.STAGES))
        self.assertLessEqual(set(golden), set(current))
        self.assertLessEqual(set(current) - set(golden), NEW_STAGES)
        self.assertEqual([s for s in current if s not in NEW_STAGES], golden)

    def test_the_capture_is_deterministic(self):
        self.assertEqual(self.capture.collect(), self.current)

    def test_build_and_report_scenarios_cover_pass_and_fail(self):
        checks = self.current["checks.txt"].splitlines()
        self.assertIn("pass build status pass", checks)
        self.assertIn("fail build status fail", checks)
        self.assertIn("fail build shape:stock_nonnegative pass [shapes[2] stock_nonnegative]", checks)
        self.assertTrue(self.current["build_pass/console.txt"].startswith("exit 0\n"))
        self.assertTrue(self.current["build_fail/console.txt"].startswith("exit 1\n"))


if __name__ == "__main__":
    unittest.main()
