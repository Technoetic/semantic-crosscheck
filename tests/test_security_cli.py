"""sxc.py behaviour that keeps a run honest: the configuration it was asked for, no stale verdicts, no leaky crashes.

- An explicit --config that does not exist is exit 2 for every stage. doctor used to ignore it, pass with exit 0
  and skip the check-role privilege checks, so a typo turned the security gate off.
- Without --config, only doctor and cube-install run when ./crosscheck.toml is absent.
- The stage's previous result.json is removed before it runs: an interrupted or killed run cannot leave an earlier
  PASS for report to trust. Ctrl-C records an error result.
- A new output directory gets a .gitignore of `*`; an existing directory is left alone.
- An unexpected exception prints its type and location only (pg8000 messages quote row values); SXC_DEBUG=1 prints
  the traceback.
- On Python older than 3.11 the CLI stops with exit 3 and a clear message before importing anything that needs 3.11.
No database is needed: stage functions are replaced where a stage would connect.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture

import sxc
from semantic_crosscheck import doctor, stage_build  # noqa: F401  (imported so their run() can be replaced)
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, Result

SXC = pgfixture.SCRIPTS / "sxc.py"


def run_main(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sxc.main(argv)
    return code, out.getvalue(), err.getvalue()


def stand_in(stage: str, seen: dict):
    """A stage run() that records what sxc.main passed and passes one check."""
    def run(cfg, args, out):
        seen.update(cfg=cfg, out=Path(out))
        res = Result(stage)
        res.add("x", "pass")
        return res
    return run


def write_pass(out: Path, stage: str) -> None:
    res = Result(stage)
    res.add("x", "pass")
    res.write(out)


def saved(out: Path, stage: str) -> dict:
    return json.loads((out / stage / "result.json").read_text(encoding="utf-8"))


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_sec_cli_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("SXC_PG_DSN", "SXC_PG_OWNER_DSN", "SXC_DEBUG"):
            os.environ.pop(name, None)


class ConfigOptionTest(CliCase):
    def test_explicit_missing_config_is_exit_2_for_every_stage(self):
        missing = self.tmp / "sxc-dmeo" / "crosscheck.toml"
        for stage in ("doctor", "cube-install", "build", "report"):
            with self.subTest(stage=stage):
                out = self.tmp / f"out-{stage}"
                code, _o, err = run_main([stage, "--config", str(missing), "--out", str(out)])
                self.assertEqual(code, EXIT_CONFIG)
                result = saved(out, stage.replace("-", "_"))
                self.assertEqual(result["status"], "error")
                message = result["checks"][0]["detail"]["message"]
                self.assertEqual(message, "--config crosscheck.toml: no such file (check the path; nothing was run)")
                self.assertNotIn(self.tmp.name, json.dumps(result))
                self.assertIn("no such file", err)

    def test_explicit_missing_config_without_out_writes_nothing(self):
        with contextlib.chdir(self.tmp):
            code, _o, _e = run_main(["doctor", "--config", "absent.toml"])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_without_config_only_doctor_runs_when_the_default_is_absent(self):
        seen = {}
        with contextlib.chdir(self.tmp), mock.patch("semantic_crosscheck.doctor.run", stand_in("doctor", seen)):
            code, _o, err = run_main(["build"])
            self.assertEqual(code, EXIT_CONFIG)
            self.assertIn("no crosscheck.toml in the current directory: pass --config PATH", err)
            self.assertFalse((self.tmp / ".sxc").exists())
            code, _o, _e = run_main(["doctor", "--quiet"])
        self.assertEqual(code, 0)
        self.assertIsNone(seen["cfg"])
        self.assertEqual(saved(self.tmp / ".sxc", "doctor")["status"], "pass")

    def test_default_config_is_used_when_present(self):
        project = self.tmp / "project"
        shutil.copytree(pgfixture.DEMO_CONFIG.parent, project)
        seen = {}
        with contextlib.chdir(project), mock.patch("semantic_crosscheck.stage_build.run", stand_in("build", seen)):
            code, _o, _e = run_main(["build", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["cfg"].path, (project / "crosscheck.toml").resolve())
        self.assertEqual(seen["out"].resolve(), (project / ".sxc").resolve())


class StaleResultTest(CliCase):
    def test_previous_result_is_gone_before_the_stage_runs(self):
        out = self.tmp / "out"
        write_pass(out, "build")

        def fake_run(cfg, args, out_dir):
            self.assertFalse((Path(out_dir) / "build" / "result.json").exists(), "the old verdict is still there")
            raise SystemExit(9)                          # a run that never gets to write (like a kill)

        with mock.patch("semantic_crosscheck.stage_build.run", fake_run):
            with self.assertRaises(SystemExit):
                run_main(["build", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(out)])
        self.assertFalse((out / "build" / "result.json").exists())

    def test_interrupt_records_an_error(self):
        out = self.tmp / "out"
        write_pass(out, "build")
        with mock.patch("semantic_crosscheck.stage_build.run", side_effect=KeyboardInterrupt):
            code, _o, err = run_main(["build", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(out)])
        self.assertEqual(code, 1)
        result = saved(out, "build")
        self.assertEqual((result["status"], result["checks"][0]["detail"]), ("error", {"reason": "interrupted"}))
        self.assertIn("interrupted", err)

    def test_configuration_error_replaces_an_earlier_pass(self):
        cfg = self.tmp / "crosscheck.toml"
        cfg.write_text("version = 2\n", encoding="utf-8")
        write_pass(self.tmp / ".sxc", "build")
        code, _o, _e = run_main(["build", "--config", str(cfg)])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(saved(self.tmp / ".sxc", "build")["status"], "error")


class OutputDirectoryTest(CliCase):
    def test_new_output_directory_is_git_ignored_and_existing_ones_are_left_alone(self):
        new = self.tmp / "fresh" / "results"
        existing = self.tmp / "existing"
        existing.mkdir()
        with mock.patch("semantic_crosscheck.doctor.run", stand_in("doctor", {})):
            run_main(["doctor", "--quiet", "--out", str(new)])
            run_main(["doctor", "--quiet", "--out", str(existing)])
        self.assertEqual((new / ".gitignore").read_bytes(), b"*\n")
        self.assertFalse((existing / ".gitignore").exists())
        self.assertTrue((existing / "doctor" / "result.json").is_file())


class CrashOutputTest(CliCase):
    def crash(self) -> tuple[int, str, dict]:
        out = self.tmp / "out"

        def fake_run(cfg, args, out_dir):
            raise ValueError("invalid input syntax for type integer: \"P-000123\"")

        with mock.patch("semantic_crosscheck.stage_build.run", fake_run):
            code, _o, err = run_main(["build", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(out)])
        return code, err, saved(out, "build")

    def test_unexpected_exception_prints_type_and_location_only(self):
        code, err, result = self.crash()
        self.assertEqual(code, 1)
        self.assertNotIn("P-000123", err)
        self.assertIn("unexpected ValueError in test_security_cli.py:", err)
        self.assertIn("SXC_DEBUG=1", err)
        self.assertEqual(result["checks"][0]["detail"], {"exception": "ValueError", "sqlstate": ""})

    def test_debug_prints_the_traceback(self):
        os.environ["SXC_DEBUG"] = "1"
        code, err, _result = self.crash()
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)
        self.assertIn("P-000123", err)


class PythonVersionTest(unittest.TestCase):
    def test_old_python_gets_exit_3_before_the_package_is_imported(self):
        probe = ("import runpy, sys\n"
                 "sys.version_info = (3, 10, 14, 'final', 0)\n"
                 "sys.argv = ['sxc.py', 'doctor']\n"
                 "try:\n"
                 "    runpy.run_path(sys.argv_path, run_name='__main__')\n"
                 "finally:\n"
                 "    sys.stdout.write('imported=' + str('semantic_crosscheck' in sys.modules))\n")
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", probe.replace("sys.argv_path", repr(str(SXC)))],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, EXIT_ENV, p.stderr)
        self.assertIn("semantic-crosscheck needs Python 3.11 or newer (this is 3.10)", p.stderr)
        self.assertEqual(p.stdout, "imported=False")

    def test_sxc_parses_with_an_older_grammar(self):
        import ast
        ast.parse(SXC.read_text(encoding="utf-8"), feature_version=(3, 7))


if __name__ == "__main__":
    unittest.main()
