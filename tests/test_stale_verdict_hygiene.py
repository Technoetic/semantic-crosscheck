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
        self.out = self.tmp / "out"

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


    def _write_own_report_pass(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(
            "# semantic-crosscheck report\n\nOverall: **PASS**\n\n| Stage | Status |\n|---|---|\n",
            encoding="utf-8",
            newline="\n",
        )
        (out_dir / "report.json").write_text(
            json.dumps({
                "status": "pass",
                "stages": {},
                "required": ["build", "cube", "verify"],
                "trusted_sql": {},
                "provenance": {"schema_version": 1, "binding": "unbound"},
                "runtime_evidence": {"schema_version": 1, "binding": "unbound"},
            }),
            encoding="utf-8",
            newline="\n",
        )
        write_own_pass(out_dir, "report")

    def test_missing_config_without_out_clears_prior_own_pass_and_never_creates_sxc(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        missing_cfg = proj / "missing.toml"
        for stage in ("build", "reason", "cube", "demo", "data-rules", "doctor", "cube-install", "verify", "expect"):
            stage_name = stage.replace("-", "_")
            with self.subTest(mode="explicit_missing_cfg", stage=stage):
                sxc_dir = proj / ".sxc"
                shutil.rmtree(sxc_dir, ignore_errors=True)
                write_own_pass(sxc_dir, stage_name)
                code, _o, _e = run_cli([stage, "--config", str(missing_cfg)])
                self.assertEqual(code, EXIT_CONFIG)
                self.assertFalse((sxc_dir / stage_name / "result.json").exists(), stage)

        sxc_dir = proj / ".sxc"
        shutil.rmtree(sxc_dir, ignore_errors=True)
        write_own_pass(sxc_dir, "grain")
        spec_file = self.tmp / "g.json"
        spec_file.write_text('{"schema_version": 1, "contracts": []}', encoding="utf-8")
        code, _o, _e = run_cli(["grain", "--config", str(missing_cfg), "--spec", str(spec_file)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertFalse((sxc_dir / "grain" / "result.json").exists())

        shutil.rmtree(sxc_dir, ignore_errors=True)
        self._write_own_report_pass(sxc_dir)
        code, _o, _e = run_cli(["report", "--config", str(missing_cfg)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertFalse((sxc_dir / "report.md").exists())
        self.assertFalse((sxc_dir / "report.json").exists())
        self.assertFalse((sxc_dir / "report" / "result.json").exists())

        cwd_dir = self.tmp / "cwd_empty"
        cwd_dir.mkdir()
        for stage in ("build", "verify", "expect", "report"):
            with self.subTest(mode="absent_sxc_never_created", stage=stage), contextlib.chdir(cwd_dir):
                code, _o, _e = run_cli([stage])
                self.assertEqual(code, EXIT_CONFIG)
                self.assertFalse((cwd_dir / ".sxc").exists(), f"{stage} created .sxc")

        with contextlib.chdir(cwd_dir):
            self._write_own_report_pass(cwd_dir / ".sxc")
            write_own_pass(cwd_dir / ".sxc", "expect")
            write_own_pass(cwd_dir / ".sxc", "build")
            self.assertEqual(run_cli(["report"])[0], EXIT_CONFIG)
            self.assertFalse((cwd_dir / ".sxc" / "report.md").exists())
            self.assertFalse((cwd_dir / ".sxc" / "report.json").exists())
            self.assertFalse((cwd_dir / ".sxc" / "report" / "result.json").exists())
            self.assertEqual(run_cli(["expect"])[0], EXIT_CONFIG)
            self.assertFalse((cwd_dir / ".sxc" / "expect" / "result.json").exists())
            self.assertEqual(run_cli(["build"])[0], EXIT_CONFIG)
            self.assertFalse((cwd_dir / ".sxc" / "build" / "result.json").exists())

    def test_report_missing_config_with_out_guards_hardlinks_symlinks_and_cli_aliases(self):
        missing_cfg = self.tmp / "absent.toml"
        hl_out = self.tmp / "hl_out"
        hl_out.mkdir()
        src = self.tmp / "precious.md"
        src.write_bytes(b"precious-hardlinked-data")
        os.link(src, hl_out / "report.md")
        code, _o, _e = run_cli(["report", "--config", str(missing_cfg), "--out", str(hl_out)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual((hl_out / "report.md").read_bytes(), b"precious-hardlinked-data")
        self.assertEqual(src.read_bytes(), b"precious-hardlinked-data")
        self.assertFalse((hl_out / "report" / "result.json").exists())

        real_dir = self.tmp / "real_victim"
        real_dir.mkdir()
        (real_dir / "report.md").write_text("keep-me", encoding="utf-8")
        (real_dir / "report.json").write_text('{"keep": true}', encoding="utf-8")
        link_out = self.tmp / "link_out"
        symlink_ok = True
        try:
            link_out.symlink_to(real_dir, target_is_directory=True)
        except OSError:
            symlink_ok = False
        if symlink_ok:
            code2, _o2, _e2 = run_cli(["report", "--config", str(missing_cfg), "--out", str(link_out)])
            self.assertEqual(code2, EXIT_CONFIG)
            self.assertEqual((real_dir / "report.md").read_text(encoding="utf-8"), "keep-me")
            self.assertEqual((real_dir / "report.json").read_text(encoding="utf-8"), '{"keep": true}')
            self.assertFalse((real_dir / "report" / "result.json").exists())

        dir_alias_out = self.tmp / "dir_alias_out"
        dir_alias_out.mkdir()
        (dir_alias_out / "report.md").write_text("keep-dir-alias", encoding="utf-8")
        code3, _o3, _e3 = run_cli([f"--config={dir_alias_out}", "report", f"--out={dir_alias_out}"])
        self.assertEqual(code3, EXIT_CONFIG)
        self.assertEqual((dir_alias_out / "report.md").read_text(encoding="utf-8"), "keep-dir-alias")

    def test_abbreviated_cli_options_and_symlink_parent_config_on_rc2(self):
        cwd_dir = self.tmp / "cwd_abbrev"
        cwd_dir.mkdir()
        custom_out = self.tmp / "custom_out"
        cfg_dir = self.tmp / "cfg_dir"
        cfg_dir.mkdir()
        cfg_file = cfg_dir / "crosscheck.toml"
        cfg_file.write_text('version = 1\n[source]\nschemas = ["s"]\n', encoding="utf-8")

        with contextlib.chdir(cwd_dir):
            write_own_pass(cwd_dir / ".sxc", "build")
            write_own_pass(custom_out, "build")
            code, _o, _e = run_cli(["build", "--ou", str(custom_out), "--unknown-flag"])
            self.assertEqual(code, EXIT_CONFIG)
            self.assertFalse((custom_out / "build" / "result.json").exists())
            self.assertTrue((cwd_dir / ".sxc" / "build" / "result.json").is_file(), "CWD .sxc must stay untouched")

            self._write_own_report_pass(cwd_dir / ".sxc")
            self._write_own_report_pass(cfg_dir / ".sxc")
            code2, _o2, _e2 = run_cli(["report", "--con", str(cfg_file), "--unknown-flag"])
            self.assertEqual(code2, EXIT_CONFIG)
            self.assertFalse((cfg_dir / ".sxc" / "report" / "result.json").exists())
            self.assertFalse((cfg_dir / ".sxc" / "report.json").exists())
            self.assertTrue((cwd_dir / ".sxc" / "report" / "result.json").is_file(), "CWD .sxc must stay untouched")

            write_own_pass(cwd_dir / ".sxc", "verify")
            self.assertEqual(run_cli(["verify", "--o", str(custom_out)])[0], EXIT_CONFIG)
            self.assertTrue((cwd_dir / ".sxc" / "verify" / "result.json").is_file())
            self.assertEqual(run_cli(["verify", "--out"])[0], EXIT_CONFIG)
            self.assertTrue((cwd_dir / ".sxc" / "verify" / "result.json").is_file())

        real_proj = self.tmp / "real_proj"
        real_proj.mkdir()
        (real_proj / "crosscheck.toml").write_text("version = 2\n", encoding="utf-8")
        write_own_pass(real_proj / ".sxc", "expect")
        link_proj = self.tmp / "link_proj"
        try:
            link_proj.symlink_to(real_proj, target_is_directory=True)
        except OSError:
            return
        code_sym, _o, _e = run_cli(["expect", "--config", str(link_proj / "crosscheck.toml")])
        self.assertEqual(code_sym, EXIT_CONFIG)
        self.assertTrue((real_proj / ".sxc" / "expect" / "result.json").is_file(), "symlink parent must block unlink")

    def test_equals_joined_cli_flags_protect_aliased_inputs_and_stage_key_and_comments(self):
        for stage_cli, stage_name, flag_builder in [
            ("semantic-preflight", "semantic_preflight",
             lambda target: ["semantic-preflight", f"--config={self.tmp / 'c.toml'}",
                             f"--contract={self.tmp / 'k.json'}", f"--plan={target}",
                             f"--out={self.out}", "--bad-flag"]),
            ("sql-structure", "sql_structure",
             lambda target: ["sql-structure", f"--sql={self.tmp / 'q.sql'}",
                             f"--expect={target}", f"--out={self.out}", "--bad-flag"]),
            ("shacl-repair", "shacl_repair",
             lambda target: ["shacl-repair", f"--spec={target}", f"--out={self.out}", "--bad-flag"]),
        ]:
            with self.subTest(stage=stage_cli):
                shutil.rmtree(self.out, ignore_errors=True)
                target = write_own_pass(self.out, stage_name)
                before = target.read_bytes()
                code, _o, _e = run_cli(flag_builder(target))
                self.assertEqual(code, EXIT_CONFIG)
                self.assertTrue(target.is_file(), f"--flag=PATH input was deleted for {stage_cli}")
                self.assertEqual(target.read_bytes(), before)

        shutil.rmtree(self.out, ignore_errors=True)
        (self.out / "build").mkdir(parents=True)
        no_stage = self.out / "build" / "result.json"
        raw_no_stage = json.dumps({
            "schema_version": 1,
            "checks": [],
            "provenance": {"schema_version": 1, "binding": "unbound"},
            "runtime_evidence": {"schema_version": 1, "binding": "unbound"},
        }).encode("utf-8")
        no_stage.write_bytes(raw_no_stage)
        run_cli(["build", f"--out={self.out}", "--bad-flag"])
        self.assertEqual(no_stage.read_bytes(), raw_no_stage)

        shutil.rmtree(self.out, ignore_errors=True)
        write_own_pass(self.out, "build")
        commented_cfg = self.tmp / "commented.toml"
        commented_cfg.write_text('# note: writes build/result.json\nversion = 1\n[source]\nschemas = ["s"]\n', encoding="utf-8")
        self.assertEqual(run_cli(["build", f"--config={commented_cfg}", f"--out={self.out}", "--bad-flag"])[0], EXIT_CONFIG)
        self.assertFalse((self.out / "build" / "result.json").exists())

if __name__ == "__main__":
    unittest.main()
