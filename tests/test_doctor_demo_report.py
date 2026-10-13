"""doctor, demo and report.

Unit tests (no database): doctor's version parsing and component probes with fake runners, doctor's full report
when the DSN is missing or unusable, and report aggregation over hand-written stage results.
Integration tests (skipped unless SXC_TEST_ADMIN_DSN names a superuser connection) run the stages through
sxc.main on throw-away databases named sxc_t_misc_*.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture

import sxc
from semantic_crosscheck import config, demo, doctor, report
from semantic_crosscheck.result import EXIT_CONFIG, Result, StageError, inputs_fingerprint

DEMO_INPUTS = inputs_fingerprint(config.load(pgfixture.DEMO_CONFIG))

DB_PREFIX = "sxc_t_misc"
DEMO_CONFIG = pgfixture.DEMO_CONFIG


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def fake_runner(outcome):
    """subprocess.run stand-in returning (or raising) outcome and recording the argv of every call."""
    def run(argv, **_kw):
        run.calls.append(list(argv))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    run.calls = []
    return run


def quiet_main(argv) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return sxc.main(argv)


def statuses(res: Result) -> dict:
    return {c.id: c.status for c in res.checks}


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_misc_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ---- doctor: pure functions and probes -------------------------------------------------------------------
class DoctorVersionParsing(unittest.TestCase):
    def test_python_minimum(self):
        self.assertTrue(doctor.python_ok((3, 11, 0, "final", 0)))
        self.assertTrue(doctor.python_ok((3, 13, 1)))
        self.assertFalse(doctor.python_ok((3, 10, 14)))

    def test_postgresql_minimum(self):
        self.assertTrue(doctor.pg_version_ok(170000))
        self.assertTrue(doctor.pg_version_ok(180002))
        self.assertFalse(doctor.pg_version_ok(160004))

    def test_java_major(self):
        cases = {
            'openjdk version "21.0.2" 2024-01-16\nOpenJDK Runtime Environment (build 21.0.2+13)': 21,
            'java version "1.8.0_381"\nJava(TM) SE Runtime Environment': 8,
            'openjdk version "23-ea" 2024-09-17': 23,
            'Picked up JAVA_TOOL_OPTIONS: -Xmx1g\nopenjdk version "11.0.21" 2023-10-17': 11,
            "openjdk 17.0.9 2023-10-17": 17,
            "": None,
            "the system cannot find the file": None,
        }
        for text, major in cases.items():
            self.assertEqual(doctor.parse_java_major(text), major, text)

    def test_node_version(self):
        self.assertEqual(doctor.parse_node_version("v24.19.0\n"), (24, 19, 0))
        self.assertEqual(doctor.parse_node_version("20.0.1"), (20, 0, 1))
        self.assertIsNone(doctor.parse_node_version("node: command not found"))


class DoctorProbes(TempDirTest):
    def test_java_missing(self):
        runner = fake_runner(AssertionError("must not run"))
        ok, detail, hint = doctor.probe_java(env={}, which=lambda _n: None, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(detail, {"found": False, "source": "PATH"})
        self.assertIn("SXC_JAVA", hint)
        self.assertEqual(runner.calls, [])

    def test_java_from_env_variable_wins(self):
        runner = fake_runner(FakeProc(0, "", 'openjdk version "21.0.2" 2024-01-16\n'))
        found = {"configured-java": "resolved-configured-java", "java": "path-java"}
        ok, detail, _ = doctor.probe_java(env={"SXC_JAVA": "configured-java"}, which=found.get, runner=runner)
        self.assertTrue(ok)
        self.assertEqual((detail["source"], detail["major"]), ("SXC_JAVA", 21))
        self.assertEqual(runner.calls, [["resolved-configured-java", "-version"]])

    def test_unresolvable_sxc_java_does_not_fall_back_to_path(self):
        runner = fake_runner(AssertionError("must not run"))
        ok, detail, hint = doctor.probe_java(env={"SXC_JAVA": "relative/bin/java"}, which={"java": "path-java"}.get,
                                             runner=runner)
        self.assertEqual((ok, detail), (False, {"found": False, "source": "SXC_JAVA"}))
        self.assertIn("absolute path", hint)
        self.assertEqual(runner.calls, [])

    def test_java_from_path_failures(self):
        for outcome in (FakeProc(1, "", 'openjdk version "21.0.2"'), FakeProc(0, "", "no version here"), FileNotFoundError(2, "x"),
                        FakeProc(0, "", 'java version "1.8.0_381"')):
            ok, detail, _ = doctor.probe_java(env={}, which=lambda _n: "java", runner=fake_runner(outcome))
            self.assertFalse(ok, outcome)
            self.assertEqual(detail["source"], "PATH")

    def test_node_minimum_version(self):
        from semantic_crosscheck import cube_install
        minimum = cube_install.MIN_NODE
        too_old = f"v{minimum - 2}.20.4\n"
        ok, detail, hint = doctor.probe_node(which=lambda _n: "node", runner=fake_runner(FakeProc(0, too_old)))
        self.assertFalse(ok)
        self.assertEqual(detail["version"], too_old.strip()[1:])
        self.assertIn(str(minimum), hint)
        ok, detail, _ = doctor.probe_node(which=lambda _n: "node", runner=fake_runner(FakeProc(0, f"v{minimum}.0.0\n")))
        self.assertTrue(ok)
        ok, detail, _ = doctor.probe_node(which=lambda _n: None, runner=fake_runner(AssertionError("must not run")))
        self.assertEqual((ok, detail), (False, {"found": False}))

    def test_native_module_against_install_record(self):
        from semantic_crosscheck import cube_install
        try:
            key = cube_install.platform_key()
        except StageError:
            self.skipTest("no pinned native module for this platform")
        pinned = cube_install.pinned_asset(cube_install.load_pins(), key)["sha256"]
        home = self.tmp / "cube-home"
        index = home / cube_install.INDEX_NODE
        self.assertEqual(doctor.probe_native(home)[1]["reason"], "no_install_record")
        index.parent.mkdir(parents=True)
        index.write_bytes(b"synthetic native module")
        record = {"platform": key, "native": {"tarball_sha256": pinned,
                                              "index_node_sha256": hashlib.sha256(b"synthetic native module").hexdigest()}}
        (home / cube_install.RECORD).write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(doctor.probe_native(home)[:2], (True, {"reason": "verified", "platform": key}))
        index.write_bytes(b"swapped after install")
        self.assertEqual(doctor.probe_native(home)[1]["reason"], "hash_mismatch")
        index.unlink()
        self.assertEqual(doctor.probe_native(home)[1]["reason"], "missing")
        index.write_bytes(b"synthetic native module")
        record["native"]["tarball_sha256"] = "0" * 64
        (home / cube_install.RECORD).write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(doctor.probe_native(home)[1]["reason"], "pin_mismatch")


def _probe_stub(ok: bool):
    return lambda *_a, **_k: (ok, {"stub": True}, "stub hint")


class DoctorWithoutDatabase(TempDirTest):
    """No database needed: the DSN is missing or unusable, and the component probes are stubbed."""

    def setUp(self):
        super().setUp()
        for name in ("probe_module", "probe_java", "probe_node", "probe_adapter", "probe_native"):
            patcher = mock.patch.object(doctor, name, _probe_stub(name != "probe_java"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = config.load(DEMO_CONFIG)

    def run_doctor(self, env: dict, cfg="demo", require=("core",)) -> Result:
        with mock.patch.dict(os.environ, env):
            for k in ("SXC_PG_DSN",):
                if k not in env:
                    os.environ.pop(k, None)
            return doctor.run(self.cfg if cfg == "demo" else cfg, argparse.Namespace(require=list(require)), self.tmp)

    def test_missing_dsn_gives_a_full_failing_report(self):
        res = self.run_doctor({})
        st = statuses(res)
        self.assertEqual((st["python"], st["pg8000"], st["dsn"], st["connect"]), ("pass", "pass", "fail", "not_run"))
        for e in self.cfg.entities:
            self.assertEqual(st[f"privileges:select:{e}"], "not_run")
            self.assertEqual(st[f"privileges:no_write:{e}"], "not_run")
        self.assertEqual(st["output_schema"], "not_run")
        self.assertEqual((res.status, res.exit_code()), ("fail", 1))
        self.assertIn("SXC_PG_DSN", res.next_safe_action)
        dsn = next(c for c in res.checks if c.id == "dsn")
        self.assertEqual((dsn.config_ref, dsn.detail), ("source.dsn_env", {"env": "SXC_PG_DSN"}))

    def test_unusable_dsn_fails_connect_without_storing_it(self):
        for dsn, reason in (("postgresql://nobody@127.0.0.1:1/nowhere", "unreachable"), ("mysql://nobody@127.0.0.1/x", "bad_dsn")):
            res = self.run_doctor({"SXC_PG_DSN": dsn})
            st = statuses(res)
            self.assertEqual((st["dsn"], st["connect"], st["pg_version"]), ("pass", "fail", "not_run"))
            connect = next(c for c in res.checks if c.id == "connect")
            self.assertEqual(connect.detail["reason"], reason)
            self.assertNotIn("nobody", json.dumps(res.to_dict()))

    def test_without_config_only_basics(self):
        res = self.run_doctor({}, cfg=None)
        self.assertEqual(set(statuses(res)), {"python", "pg8000", "dsn", "connect", "pg_version", "privileges:temporary"})
        self.assertEqual(res.checks[2].config_ref, "")
        self.assertTrue(res.not_proven[0].startswith("the check role's privileges on the source tables and the output "
                                                     "schema were not checked"))

    def test_required_components_become_checks_others_not_proven(self):
        res = self.run_doctor({}, require=("core", "reason"))
        st = statuses(res)
        self.assertEqual((st["reason:owlready2"], st["reason:pyshacl"], st["reason:java"]), ("pass", "pass", "fail"))
        self.assertFalse(any(c.startswith("cube:") for c in st))
        self.assertEqual(res.not_proven, [])
        res = self.run_doctor({}, require=("core",))
        self.assertFalse(any(c.startswith(("reason:", "cube:")) for c in statuses(res)))
        self.assertEqual(len(res.not_proven), 1)
        self.assertTrue(res.not_proven[0].startswith("reason:java is not ready"))

    def test_unknown_component_is_a_usage_error(self):
        with self.assertRaises(StageError) as cm:
            self.run_doctor({}, require=("core", "gpu"))
        self.assertEqual(cm.exception.exit_code, EXIT_CONFIG)

    def test_sxc_main_writes_the_doctor_result(self):
        with mock.patch.dict(os.environ, {}), contextlib.chdir(self.tmp):     # no crosscheck.toml here: environment only
            os.environ.pop("SXC_PG_DSN", None)
            code = quiet_main(["doctor", "--out", str(self.tmp)])
        self.assertEqual(code, 1)
        saved = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "fail")
        self.assertIn("dsn", [c["id"] for c in saved["checks"]])


class DemoStageGuards(TempDirTest):
    """Refused before any connection: no database needed."""

    def test_config_without_the_demo_schema_is_refused(self):
        cfg_file = self.tmp / "other.toml"
        cfg_file.write_text('version = 1\n[source]\nschemas = ["warehouse"]\n'
                            '[entities.item]\ntable = "warehouse.item"\nkey = ["item_id"]\n', encoding="utf-8")
        self.assertEqual(quiet_main(["demo", "--config", str(cfg_file), "--out", str(self.tmp)]), EXIT_CONFIG)
        saved = json.loads((self.tmp / "demo" / "result.json").read_text(encoding="utf-8"))
        self.assertIn("sxc_demo", saved["checks"][0]["detail"]["message"])

    def test_scale_below_one_is_refused(self):
        self.assertEqual(quiet_main(["demo", "--config", str(DEMO_CONFIG), "--out", str(self.tmp), "--scale", "0"]), EXIT_CONFIG)


# ---- report aggregation ----------------------------------------------------------------------------------
def write_result(out: Path, stage: str, checks, not_proven=(), action="", started="", finished="", raw=None, inputs=None):
    d = out / stage
    d.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (d / "result.json").write_text(raw, encoding="utf-8")
        return
    res = Result(stage)
    for cid, status, ref, detail in checks:
        res.add(cid, status, ref, **detail)
    res.not_proven, res.next_safe_action = list(not_proven), action
    data = res.to_dict()
    data["started_at"], data["finished_at"] = started or data["started_at"], finished or data["started_at"]
    data["inputs"] = DEMO_INPUTS if inputs is None else inputs
    (d / "result.json").write_text(json.dumps(data), encoding="utf-8")


class ReportAggregation(TempDirTest):
    def setUp(self):
        super().setUp()
        self.cfg = config.load(DEMO_CONFIG)

    def run_report(self, cfg="demo") -> Result:
        return report.run(self.cfg if cfg == "demo" else cfg, argparse.Namespace(), self.tmp)

    def test_mixed_pipeline(self):
        write_result(self.tmp, "doctor", [("python", "pass", "", {})], not_proven=["reason:java is not ready: install Java"],
                     action="ready: run build")
        write_result(self.tmp, "build", [("shape:qty_positive", "pass", "shapes[1] qty_positive", {"violations": 0}),
                                         ("closure:has_component", "fail", "hierarchies.has_component", {"cycle_nodes": 2})],
                     action="fix the cycle in hierarchies.has_component")
        write_result(self.tmp, "cube", [("stage", "error", "", {"exception": "OSError"})])
        res = self.run_report()
        st = statuses(res)
        self.assertEqual(st, {"stage:doctor": "pass", "stage:build": "fail", "stage:reason": "not_run", "stage:cube": "error",
                              "stage:verify": "not_run"})
        build = next(c for c in res.checks if c.id == "stage:build")
        self.assertEqual(build.config_ref, "hierarchies.has_component")
        self.assertEqual((build.detail["pass"], build.detail["fail"], build.detail["required"]), (1, 1, True))
        self.assertEqual(next(c for c in res.checks if c.id == "stage:verify").detail, {"reason": "missing", "required": True})
        self.assertEqual((res.status, res.exit_code()), ("error", 1))
        self.assertEqual(res.not_proven, [])
        self.assertTrue(res.next_safe_action.startswith("fix build first (fail): fix the cycle"))

        md = (self.tmp / "report.md").read_text(encoding="utf-8")
        self.assertIn("`closure:has_component`, fix `hierarchies.has_component`: cycle_nodes=2", md)
        self.assertIn("doctor: reason:java is not ready: install Java", md)
        self.assertIn("- verify: required, but it has not run (no verify/result.json)", md)
        self.assertIn("| reason | not run (required) |", md)
        self.assertIn("| demo | not run (optional) |", md)
        self.assertIn("trusted code", md)
        self.assertIn("shape `open_le_total` (sql_violation)", md)
        self.assertIn("gold `q05_firm_window`: `gold/q05_firm_window.sql`", md)
        self.assertNotIn("qty_positive", md)

        js = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(js["status"], "error")
        self.assertEqual(js["not_run"], ["demo", "reason", "verify"])
        self.assertEqual(js["required"], ["build", "reason", "cube", "verify"])
        self.assertEqual(js["missing_required"], ["reason", "verify"])
        self.assertEqual(js["stages"]["build"]["failing"][0]["config_ref"], "hierarchies.has_component")
        self.assertEqual(js["stages"]["doctor"]["counts"], {"pass": 1, "fail": 0, "error": 0, "not_run": 0})
        self.assertEqual((js["stages"]["doctor"]["required"], js["stages"]["doctor"]["counted_as"]), (False, "pass"))
        self.assertEqual(js["trusted_sql"]["shapes"], ["open_le_total"])
        self.assertEqual(js["config"], "crosscheck.toml")

    def test_no_stage_results(self):
        res = self.run_report()
        self.assertEqual(statuses(res), {f"stage:{s}": "not_run" for s in ("build", "reason", "cube", "verify")})
        self.assertEqual((res.status, res.exit_code()), ("not_run", 1))
        self.assertEqual(len(res.not_proven), 1)
        self.assertTrue(res.not_proven[0].startswith("stage doctor was not run"))
        self.assertTrue(res.next_safe_action.startswith("run `sxc.py doctor`, then build, reason, cube, verify"))
        self.assertTrue((self.tmp / "report.md").is_file())
        self.assertEqual(json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))["stages"], {})

    def test_passing_present_stages_are_not_enough(self):
        for stage in ("doctor", "build"):
            write_result(self.tmp, stage, [("x", "pass", "", {})])
        res = self.run_report(cfg=None)
        self.assertEqual((res.status, res.exit_code()), ("not_run", 1))
        self.assertEqual(statuses(res), {"stage:doctor": "pass", "stage:build": "pass", "stage:reason": "not_run",
                                         "stage:cube": "not_run", "stage:verify": "not_run"})
        self.assertEqual(res.next_safe_action,
                         "run the stages that have not run yet (reason, cube, verify), in this order, then rerun report")
        md = (self.tmp / "report.md").read_text(encoding="utf-8")
        self.assertIn("Overall: **NOT_RUN**", md)
        self.assertIn("none in this configuration", md)

    def test_status_is_recomputed_fail_closed(self):
        ok_checks = [("a", "pass", "", {})]
        write_result(self.tmp, "build", [("a", "pass", "", {}), ("b", "fail", "shapes[0] child_exists", {})])
        data = json.loads((self.tmp / "build" / "result.json").read_text(encoding="utf-8"))
        data["status"] = "pass"                                   # hand-edited to look better
        (self.tmp / "build" / "result.json").write_text(json.dumps(data), encoding="utf-8")
        write_result(self.tmp, "reason", ok_checks)
        data = json.loads((self.tmp / "reason" / "result.json").read_text(encoding="utf-8"))
        data["status"] = "error"                                  # a worse recorded status is kept
        (self.tmp / "reason" / "result.json").write_text(json.dumps(data), encoding="utf-8")
        write_result(self.tmp, "cube", [], raw=json.dumps({"stage": "cube", "status": "pass",
                                                           "checks": [{"id": "a", "status": "weird"}]}))
        write_result(self.tmp, "verify", [], raw="{not json")
        st = statuses(self.run_report())
        self.assertEqual(st, {"stage:build": "fail", "stage:reason": "error", "stage:cube": "error", "stage:verify": "error"})

    def test_stale_results_are_not_run(self):
        write_result(self.tmp, "demo", [("demo:rows:part", "pass", "", {})],
                     started="2026-10-13T10:05:00+0000", finished="2026-10-13T10:06:00+0000")
        write_result(self.tmp, "build", [("x", "pass", "", {})],
                     started="2026-10-13T10:00:00+0000", finished="2026-10-13T10:04:00+0000")
        # 19:03+0900 is 10:03 UTC, before build finished (a text comparison of the two times would say after)
        write_result(self.tmp, "reason", [("x", "pass", "", {})],
                     started="2026-10-13T19:03:00+0900", finished="2026-10-13T19:04:00+0900")
        write_result(self.tmp, "cube", [("x", "pass", "", {})],
                     started="2026-10-13T10:05:00+0000", finished="2026-10-13T10:05:30+0000")
        res = self.run_report()
        checks = {c.id: c for c in res.checks}
        self.assertEqual({k: c.status for k, c in checks.items()},
                         {"stage:demo": "pass", "stage:build": "not_run", "stage:reason": "not_run", "stage:cube": "not_run",
                          "stage:verify": "not_run"})
        self.assertEqual({k: checks["stage:build"].detail[k] for k in ("reason", "after", "why", "last_status")},
                         {"reason": "stale", "after": "demo", "why": "newer", "last_status": "pass"})
        self.assertEqual((checks["stage:reason"].detail["after"], checks["stage:reason"].detail["why"]), ("build", "newer"))
        self.assertEqual((checks["stage:cube"].detail["after"], checks["stage:cube"].detail["why"]), ("build", "stale_dependency"))
        self.assertEqual(checks["stage:verify"].detail["reason"], "missing")
        self.assertEqual((res.status, res.exit_code()), ("not_run", 1))
        self.assertEqual(res.next_safe_action, "rerun the stale stages (build, reason, cube) and run the stages that have "
                                               "not run yet (verify), in the order build, reason, cube, verify, then rerun report")
        md = (self.tmp / "report.md").read_text(encoding="utf-8")
        self.assertIn("| build | pass, stale (after demo) |", md)
        self.assertIn("- build: stale (build ran before the latest demo run finished); rerun it", md)
        self.assertIn("- cube: stale (cube depends on build, which is stale); rerun it", md)

    def test_stale_next_action_when_every_stage_ran(self):
        for stage in report.PIPELINE:
            write_result(self.tmp, stage, [("x", "pass", "", {})],
                         started="2026-10-13T10:00:00+0000", finished="2026-10-13T10:00:00+0000")
        write_result(self.tmp, "demo", [("x", "pass", "", {})],
                     started="2026-10-13T10:00:00+0000", finished="2026-10-13T10:30:00+0000")
        res = self.run_report()
        self.assertEqual((res.status, res.exit_code()), ("not_run", 1))
        self.assertEqual(res.next_safe_action, "rerun the stale stages (build, reason, cube, verify), in this order, then rerun report")
        stale = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))["stale"]
        self.assertIn({"stage": "build", "after": "demo", "why": "newer"}, stale)
        self.assertIn({"stage": "verify", "after": "build", "why": "stale_dependency"}, stale)

    def test_through_sxc_main(self):
        write_result(self.tmp, "build", [("closure:has_component", "fail", "hierarchies.has_component", {})])
        self.assertEqual(quiet_main(["report", "--config", str(DEMO_CONFIG), "--out", str(self.tmp)]), 1)
        saved = json.loads((self.tmp / "report" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual([(c["id"], c["status"]) for c in saved["checks"]],
                         [("stage:build", "fail"), ("stage:reason", "not_run"), ("stage:cube", "not_run"),
                          ("stage:verify", "not_run")])


# ---- integration -----------------------------------------------------------------------------------------
@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class DemoStageIntegration(TempDirTest):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(f"{DB_PREFIX}_demo", "clean").setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def run_demo(self, variant: str, seed: int) -> dict:
        code = quiet_main(["demo", "--config", str(DEMO_CONFIG), "--out", str(self.tmp), "--variant", variant, "--seed", str(seed)])
        saved = json.loads((self.tmp / "demo" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, saved)
        return saved

    def check_loaded(self, saved: dict, data: dict, variant: str, seed: int):
        checks = {c["id"]: c for c in saved["checks"]}
        self.assertEqual(set(checks), {"demo:rows:part", "demo:rows:bom_line", "demo:rows:sales_order", "demo:rows:stock",
                                       "demo:copy_on_error"})
        self.assertTrue(all(c["status"] == "pass" for c in checks.values()))
        self.assertEqual(checks["demo:rows:sales_order"]["detail"]["loaded"], len(data["orders"]))
        self.assertEqual(checks["demo:copy_on_error"]["detail"], {"skipped": len(data["malformed"]), "malformed": len(data["malformed"])})
        expected = json.loads((self.tmp / "demo" / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, demo.reference(data["expected"], variant, seed, 1))
        self.assertEqual(expected["rows"]["parts"], checks["demo:rows:part"]["detail"]["loaded"])
        self.assertIn("build", saved["next_safe_action"])
        conn = self.fx.check_conn()
        try:
            self.assertEqual(conn.run("SELECT count(*) FROM sxc_demo.bom_line")[0][0], len(data["edges"]))
        finally:
            conn.close()

    def test_clean_variant(self):
        saved = self.run_demo("clean", 7)
        self.check_loaded(saved, demo.build(False, 7, 1), "clean", 7)

    def test_dirty_variant(self):
        saved = self.run_demo("dirty", 20261013)
        data = demo.build(True, 20261013, 1)
        self.check_loaded(saved, data, "dirty", 20261013)
        self.assertEqual(len(data["malformed"]), 2)
        self.assertIn("fails on purpose", saved["next_safe_action"])

    def test_load_error_keeps_only_the_sqlstate(self):
        (self.tmp / "demo").mkdir()
        (self.tmp / "demo" / "expected.json").write_text("{}", encoding="utf-8")      # stale file from an earlier run
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"SXC_PG_OWNER_DSN": os.environ["SXC_PG_DSN"]}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sxc.main(["demo", "--config", str(DEMO_CONFIG), "--out", str(self.tmp)])
        saved = json.loads((self.tmp / "demo" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual([(c["id"], c["status"]) for c in saved["checks"]], [("demo:load", "error")])
        self.assertEqual(saved["checks"][0]["detail"]["sqlstate"], "42501")          # insufficient privilege
        self.assertIn("SQLSTATE 42501", saved["next_safe_action"])
        self.assertIn("lacks a privilege: it must own the demo tables", saved["next_safe_action"])
        self.assertFalse((self.tmp / "demo" / "expected.json").exists())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        conn = self.fx.check_conn()
        try:
            self.assertGreater(conn.run("SELECT count(*) FROM sxc_demo.part")[0][0], 0)    # rolled back, data intact
        finally:
            conn.close()


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class DoctorIntegration(TempDirTest):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(f"{DB_PREFIX}_doctor", "clean").setup()
        cls.cfg = config.load(DEMO_CONFIG)

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def run_doctor(self, *extra) -> tuple[int, dict]:
        code = quiet_main(["doctor", "--config", str(DEMO_CONFIG), "--out", str(self.tmp), *extra])
        return code, json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))

    def test_core_passes_for_the_check_role(self):
        code, saved = self.run_doctor("--require", "core")
        checks = {c["id"]: c for c in saved["checks"]}
        self.assertEqual(code, 0, saved)
        self.assertTrue(all(c["status"] == "pass" for c in checks.values()))
        for e in self.cfg.entities:
            self.assertEqual(checks[f"privileges:no_write:{e}"]["detail"],
                             {"held": "", "member_of_owner": False, "schema_owner": False, "schema_create": False})
            self.assertTrue(checks[f"privileges:select:{e}"]["detail"]["select"])
        self.assertGreaterEqual(checks["pg_version"]["detail"]["server_version_num"], 170000)
        self.assertEqual(checks["privileges:temporary"]["detail"], {"temporary": True})
        self.assertEqual(checks["output_schema"]["detail"]["exists"], True)
        self.assertFalse(any(c.startswith(("reason:", "cube:")) for c in checks))
        self.assertEqual(saved["next_safe_action"], "ready: run `sxc.py build`")

    def test_owner_role_cannot_pass_as_check_role(self):
        with mock.patch.dict(os.environ, {"SXC_PG_DSN": os.environ["SXC_PG_OWNER_DSN"]}):
            code, saved = self.run_doctor()
        checks = {c["id"]: c for c in saved["checks"]}
        self.assertEqual(code, 1)
        for e in self.cfg.entities:
            detail = checks[f"privileges:no_write:{e}"]["detail"]
            self.assertEqual(checks[f"privileges:no_write:{e}"]["status"], "fail")
            self.assertEqual(detail["held"], "INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER")
            self.assertEqual((detail["member_of_owner"], detail["schema_owner"]), (True, True))
        self.assertIn("read-only role", saved["next_safe_action"])

    def test_missing_table_is_reported_not_raised(self):
        cfg_file = self.tmp / "minimal.toml"
        cfg_file.write_text('version = 1\n[source]\nschemas = ["sxc_demo"]\noutput_schema = "sxc_out"\n'
                            '[entities.part]\ntable = "sxc_demo.part"\nkey = ["part_id"]\n'
                            '[entities.ghost]\ntable = "sxc_demo.ghost"\nkey = ["ghost_id"]\n', encoding="utf-8")
        code = quiet_main(["doctor", "--config", str(cfg_file), "--out", str(self.tmp)])
        saved = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        checks = {c["id"]: c for c in saved["checks"]}
        self.assertEqual(code, 1)
        self.assertEqual(checks["privileges:select:part"]["status"], "pass")
        self.assertEqual(checks["privileges:select:ghost"]["status"], "fail")
        self.assertEqual(checks["privileges:select:ghost"]["detail"]["table_exists"], False)
        self.assertEqual(checks["privileges:no_write:ghost"]["status"], "not_run")
        self.assertIn("does not exist in the connected database: check entities.ghost.table", saved["next_safe_action"])
        self.assertNotIn("grant", saved["next_safe_action"])


if __name__ == "__main__":
    unittest.main()
