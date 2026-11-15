"""Regression tests for stale PASS verdict cleanup on rc=2, grain EXIT_ENV, and non-spec_source expect config errors."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import grain
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, Result, StageError


def run_cli(argv):
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            code = sxc.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    return code, out_buf.getvalue(), err_buf.getvalue()


def write_own_pass(out_dir: Path, stage_name: str) -> Path:
    res = Result(stage_name)
    res.add(f"{stage_name}:check", "pass")
    res.provenance = {"schema_version": 1, "binding": "unbound"}
    res.runtime_evidence = {"schema_version": 1, "binding": "unbound"}
    return res.write(out_dir)


class StaleVerdictCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_stale_hygiene_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"

    def assertNoSurvivingPass(self, stage_name: str):
        path = self.out / stage_name / "result.json"
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotEqual(doc.get("status"), "pass", f"stale PASS survived for {stage_name}: {doc}")

    def test_rc2_clears_prior_own_pass_across_diagnostic_and_guarded_stages(self):
        cases = [
            ("shacl_repair", ["shacl-repair", "--spec", str(self.tmp / "missing-spec.json"), "--out", str(self.out)]),
            ("shacl_repair", ["shacl-repair", "--out", str(self.out)]),
            ("semantic_preflight", ["semantic-preflight", "--config", str(self.tmp / "missing.toml"),
                                    "--contract", str(self.tmp / "c.json"), "--plan", str(self.tmp / "p.json"),
                                    "--out", str(self.out)]),
            ("semantic_preflight", ["semantic-preflight", "--out", str(self.out)]),
            ("sql_structure", ["sql-structure", "--sql", str(self.tmp / "missing.sql"),
                               "--expect", str(self.tmp / "e.json"), "--out", str(self.out)]),
            ("sql_structure", ["sql-structure", "--out", str(self.out)]),
            ("grain", ["grain", "--config", str(pgfixture.DEMO_CONFIG),
                       "--spec", str(self.tmp / "missing-grain.json"), "--out", str(self.out)]),
            ("boundary_schema", ["boundary-schema", "--out", str(self.out)]),
            ("boundary_schema", ["boundary-schema", "--spec", str(self.tmp / "missing-bs.json"), "--out", str(self.out)]),
            ("okf", ["okf", "--config", str(self.tmp / "missing.toml"),
                     "--export", str(self.tmp / "bundle"), "--select", "q01", "--out", str(self.out)]),
            ("okf", ["okf", "--out", str(self.out)]),
            ("knowledge_evolution", ["knowledge-evolution", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(self.out)]),
            ("knowledge_evolution", ["knowledge-evolution", "--unknown-flag", "--out", str(self.out)]),
        ]
        for stage_name, argv in cases:
            with self.subTest(stage=stage_name, argv=argv):
                shutil.rmtree(self.out, ignore_errors=True)
                write_own_pass(self.out, stage_name)
                code, _o, _e = run_cli(argv)
                self.assertEqual(code, EXIT_CONFIG)
                self.assertNoSurvivingPass(stage_name)

    def test_expect_broken_spec_source_config_clears_prior_own_pass_without_leaking_path(self):
        write_own_pass(self.out, "expect")
        cfg = self.tmp / "crosscheck.toml"
        cfg.write_text(
            'version = 1\n[source]\nschemas = ["sxc_demo"]\n'
            '[[gold]]\nid = "q1"\nsql = "q1.sql"\nkey = "id"\n'
            '[gold.cube_query]\ndimensions = ["a.b"]\n[gold.map]\nid = "a.b"\n'
            '[[gold.expect]]\nsource = "authored"\nspec = "S1"\nrows = []\n'
            'spec_source = { file = "../secret-spec-name.md", sha256 = "' + ("a" * 64) + '" }\n',
            encoding="utf-8",
        )
        (self.tmp / "q1.sql").write_text("SELECT 1 AS id", encoding="utf-8")
        code, out_txt, err_txt = run_cli(["expect", "--config", str(cfg), "--out", str(self.out)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertNoSurvivingPass("expect")
        self.assertNotIn("secret-spec-name", out_txt + err_txt)

    def test_report_broken_semantic_preflight_toml_clears_prior_own_pass_summaries(self):
        cfg = self.tmp / "crosscheck.toml"
        cfg.write_text(
            'version = 1\n[source]\nschemas = ["src"]\n'
            '[entities.item]\ntable = "src.item"\nkey = ["id"]\n',
            encoding="utf-8",
        )
        loaded = sxc.config.load(cfg)
        fp = sxc.inputs_fingerprint(loaded)
        for stage_name in ("build", "cube", "verify"):
            res = Result(stage_name)
            res.add(f"{stage_name}:ok", "pass")
            res.inputs = fp
            res.write(self.out)
        code1, _o1, _e1 = run_cli(["report", "--config", str(cfg), "--out", str(self.out)])
        self.assertEqual(code1, 0)
        self.assertTrue((self.out / "report.json").is_file())
        self.assertTrue((self.out / "report.md").is_file())
        self.assertEqual(json.loads((self.out / "report" / "result.json").read_text(encoding="utf-8"))["status"], "pass")

        cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[semantic_preflight]\ncontract = [broken\n", encoding="utf-8")
        code2, _o2, _e2 = run_cli(["report", "--config", str(cfg), "--out", str(self.out)])
        self.assertEqual(code2, EXIT_CONFIG)
        self.assertFalse((self.out / "report.json").exists())
        self.assertFalse((self.out / "report.md").exists())
        self.assertNoSurvivingPass("report")

    def test_cleanup_never_creates_missing_out_or_deletes_user_bytes_or_hardlinks(self):
        fresh = self.tmp / "never-created"
        code, _o, _e = run_cli(["sql-structure", "--out", str(fresh)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertFalse(fresh.exists())

        stage_dir = self.out / "sql_structure"
        stage_dir.mkdir(parents=True)
        target = stage_dir / "result.json"
        target.write_bytes(b"user-non-result-bytes")
        run_cli(["sql-structure", "--out", str(self.out)])
        self.assertEqual(target.read_bytes(), b"user-non-result-bytes")

        # Hardlinked own-looking Result JSON must also survive untouched
        target.unlink()
        receipt = write_own_pass(self.out, "sql_structure")
        linked = self.tmp / "hardlink-copy.json"
        os.link(receipt, linked)
        before = receipt.read_bytes()
        run_cli(["sql-structure", "--out", str(self.out)])
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(linked.read_bytes(), before)


class GrainExitCodeAndExpectErrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_grain_expect_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_grain_missing_dsn_exits_3_with_environment_reason(self):
        example = pgfixture.ROOT / "examples" / "grain"
        out = self.tmp / "out"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SXC_GRAIN_DSN", None)
            os.environ.pop("SXC_PG_DSN", None)
            cfg = sxc.config.load(example / "crosscheck.toml")
            os.environ.pop(cfg.source.dsn_env, None)
            code, _o, err = run_cli([
                "grain", "--config", str(example / "crosscheck.toml"),
                "--spec", str(example / "contract.json"), "--out", str(out),
            ])
        self.assertEqual(code, EXIT_ENV)
        saved = json.loads((out / "grain" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "error")
        self.assertEqual(saved["checks"][0]["detail"]["reason"], "diagnostic_environment_missing")
        self.assertIn("diagnostic_environment_missing", err)

    def test_expect_without_spec_source_reports_actionable_config_error(self):
        out = self.tmp / "out"
        missing = self.tmp / "absent.toml"
        code, _o, err = run_cli(["expect", "--config", str(missing), "--out", str(out)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("no such file", err)

        bad_cfg = self.tmp / "crosscheck.toml"
        bad_cfg.write_text("version = 2\n", encoding="utf-8")
        code2, _o2, err2 = run_cli(["expect", "--config", str(bad_cfg), "--out", str(out)])
        self.assertEqual(code2, EXIT_CONFIG)
        self.assertIn("version", err2)
        self.assertNotIn("invalid_expect_inputs", err2)


if __name__ == "__main__":
    unittest.main()
