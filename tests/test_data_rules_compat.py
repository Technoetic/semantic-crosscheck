"""A configuration without [data_rules] behaves exactly as in v0.21.1.

The goldens in tests/fixtures/compat_v0_21_1 were written by its capture.py on v0.21.1, before any data-rules code
existed: the SQL of every shape view, shapes.ttl, fk_review.sql, report.TRUSTED_NOTE, stage_build.BUILD_SQL_HEADER,
the inputs_fingerprint keys, and two offline `build` + `report` runs of demo/crosscheck.toml through sxc.main with a
recording stand-in for the database (every statement build sends, build.sql, result.json, the console summaries,
report.md and report.json, and the check lists). Every golden must stay byte-identical; capture.py normalises only
run times, the engine fingerprint (new modules change it by design) and output paths. The command-line stage list
may gain data-rules and nothing else. With [data_rules] and views = false, build still sends exactly the v0.21.1
statements and writes the same build.sql; report adds its one data-rules line only when [data_rules] is configured;
expect's fixture configuration drops [data_rules]. No database is needed.
"""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import config, db, expect, report

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
        self.assertNotIn(report.DATA_RULES_NOTE, self.current["report_pass/report.md"])


class DataRulesConfiguredTest(unittest.TestCase):
    """The demo configuration with a [data_rules] table: views = false must leave build as it was in v0.21.1."""

    @classmethod
    def setUpClass(cls):
        cls.capture = load_capture()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sxc_compat_rules_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copytree(pgfixture.ROOT / "demo" / "gold", self.dir / "gold")
        text = (pgfixture.ROOT / "demo" / "crosscheck.toml").read_text(encoding="utf-8")
        self.config = self.dir / "crosscheck.toml"
        self.config.write_text(text + '\n[data_rules]\nfile = "data-rules.toml"\nviews = false\n', encoding="utf-8",
                               newline="\n")
        self.out = self.dir / "out"

    def test_build_with_views_false_sends_the_v0_21_1_statements(self):
        conn = self.capture.RecordingConnection(self.capture.SCENARIOS["pass"])
        with mock.patch.object(db, "connect", return_value=conn):
            code, console = self.capture._cli(["build", "--config", str(self.config), "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual(self.capture._statements(conn), (GOLDEN / "build_pass" / "statements.sql").read_text(
            encoding="utf-8"))
        self.assertEqual((self.out / "build" / "build.sql").read_bytes(),
                         (GOLDEN / "build_pass" / "build.sql").read_bytes())
        self.assertEqual(self.capture._console(code, console, self.out),
                         (GOLDEN / "build_pass" / "console.txt").read_text(encoding="utf-8"))

    def test_report_adds_one_line_only_with_data_rules(self):
        with mock.patch.object(db, "connect", side_effect=AssertionError("report connected")):
            self.capture._cli(["report", "--config", str(self.config), "--out", str(self.out)])
        self.assertEqual((self.out / "report.md").read_text(encoding="utf-8").count(report.DATA_RULES_NOTE), 1)

    def test_the_expect_fixture_configuration_has_no_data_rules(self):
        cfg = config.load(self.config)
        self.assertIsNotNone(cfg.data_rules)
        self.assertIsNone(expect.fixture_config(cfg, "SXC_EXPECT_DSN").data_rules)


if __name__ == "__main__":
    unittest.main()
