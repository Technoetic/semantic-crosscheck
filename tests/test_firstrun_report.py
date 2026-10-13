"""report: the overall verdict passes only for a complete, current pipeline (no database needed).

Regression tests for reports that passed while a required stage had never run (doctor alone), while results were
older than the data or model they checked (a later demo or cube run), and while a stage's own result was not_run
(a narrowed verify). Stage results are written by hand with explicit start and finish times.
"""

import argparse
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture

import sxc
from semantic_crosscheck import config, report
from semantic_crosscheck.result import Result, inputs_fingerprint

DEMO_INPUTS = inputs_fingerprint(config.load(pgfixture.DEMO_CONFIG))

DEMO_CONFIG = pgfixture.DEMO_CONFIG
ORDER = ("doctor", "demo", "build", "reason", "cube", "verify")


def at(minute: int) -> str:
    return f"2026-10-13T10:{minute:02d}:00+0000"


def write_result(out: Path, stage: str, checks=(("x", "pass"),), start: int = 0, finish: int | None = None,
                 not_proven=(), action="", inputs=None) -> None:
    res = Result(stage)
    for cid, status in checks:
        res.add(cid, status, "")
    res.not_proven, res.next_safe_action = list(not_proven), action
    data = res.to_dict()
    data["started_at"], data["finished_at"] = at(start), at(start if finish is None else finish)
    data["inputs"] = DEMO_INPUTS if inputs is None else inputs
    (out / stage).mkdir(parents=True, exist_ok=True)
    (out / stage / "result.json").write_text(json.dumps(data), encoding="utf-8")


def pipeline(out: Path, stages=ORDER, inputs=None) -> None:
    """Every stage passing, each started after the previous one finished."""
    for i, stage in enumerate(stages):
        write_result(out, stage, start=2 * i, finish=2 * i + 1, inputs=inputs)


def statuses(res: Result) -> dict:
    return {c.id: c.status for c in res.checks}


class ReportVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_firstrun_report_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = config.load(DEMO_CONFIG)

    def run_report(self, cfg="demo") -> Result:
        return report.run(self.cfg if cfg == "demo" else cfg, argparse.Namespace(), self.tmp)

    def cli(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sxc.main(["report", "--config", str(DEMO_CONFIG), "--out", str(self.tmp)])

    def test_complete_current_pipeline_passes(self):
        pipeline(self.tmp)
        res = self.run_report()
        self.assertEqual((res.status, res.exit_code()), ("pass", 0))
        self.assertEqual(set(statuses(res)), {f"stage:{s}" for s in ORDER})
        self.assertTrue(res.next_safe_action.startswith("every required stage ran after the stages it depends on"))
        self.assertIn("Overall: **PASS**", (self.tmp / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(self.cli(), 0)

    def test_doctor_alone_is_not_a_pass(self):
        write_result(self.tmp, "doctor")
        res = self.run_report()
        self.assertEqual((res.status, res.exit_code()), ("not_run", 1))
        for stage in ("build", "reason", "cube", "verify"):
            check = next(c for c in res.checks if c.id == f"stage:{stage}")
            self.assertEqual((check.status, check.detail), ("not_run", {"reason": "missing", "required": True}))
        self.assertIn("Overall: **NOT_RUN**", (self.tmp / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(self.cli(), 1)

    def test_demo_and_doctor_are_optional(self):
        pipeline(self.tmp, ("build", "reason", "cube", "verify"))
        res = self.run_report()
        self.assertEqual((res.status, res.exit_code()), ("pass", 0))
        self.assertEqual(set(statuses(res)), {"stage:build", "stage:reason", "stage:cube", "stage:verify"})
        self.assertTrue(any(n.startswith("stage doctor was not run") for n in res.not_proven))

    def test_a_present_optional_stage_that_failed_still_fails(self):
        pipeline(self.tmp)
        write_result(self.tmp, "doctor", checks=(("python", "pass"), ("privileges:no_write:part", "fail")), start=0, finish=1)
        res = self.run_report()
        self.assertEqual((statuses(res)["stage:doctor"], res.status), ("fail", "fail"))
        self.assertTrue(res.next_safe_action.startswith("fix doctor first (fail)"))

    def test_a_new_demo_load_makes_every_later_result_stale(self):
        pipeline(self.tmp)
        write_result(self.tmp, "demo", start=20, finish=21)           # README step 6: the dirty variant loaded last
        res = self.run_report()
        st = statuses(res)
        self.assertEqual({s: st[f"stage:{s}"] for s in ("build", "reason", "cube", "verify")},
                         dict.fromkeys(("build", "reason", "cube", "verify"), "not_run"))
        self.assertEqual(st["stage:demo"], "pass")
        self.assertEqual((res.status, res.exit_code()), ("not_run", 1))
        self.assertEqual(res.next_safe_action,
                         "rerun the stale stages (build, reason, cube, verify), in this order, then rerun report")
        self.assertEqual(self.cli(), 1)

    def test_verify_older_than_the_latest_cube_run_is_stale(self):
        pipeline(self.tmp)
        write_result(self.tmp, "cube", start=20, finish=21)           # the model was regenerated after verify
        res = self.run_report()
        verify = next(c for c in res.checks if c.id == "stage:verify")
        self.assertEqual((verify.status, verify.detail["reason"], verify.detail["after"], verify.detail["why"]),
                         ("not_run", "stale", "cube", "newer"))
        self.assertEqual(statuses(res)["stage:cube"], "pass")
        self.assertEqual(statuses(res)["stage:reason"], "pass")    # reason depends on build only
        self.assertEqual(res.next_safe_action, "rerun the stale stages (verify), in this order, then rerun report")

    def test_a_stale_failure_keeps_its_own_worse_status(self):
        pipeline(self.tmp)
        write_result(self.tmp, "build", checks=(("closure:has_component", "fail"),), start=4, finish=5)
        write_result(self.tmp, "demo", start=20, finish=21)
        build = next(c for c in self.run_report().checks if c.id == "stage:build")
        self.assertEqual((build.status, build.detail["reason"], build.detail["last_status"]), ("fail", "stale", "fail"))

    def test_a_result_without_readable_times_cannot_be_current(self):
        pipeline(self.tmp)
        data = json.loads((self.tmp / "verify" / "result.json").read_text(encoding="utf-8"))
        del data["started_at"]
        (self.tmp / "verify" / "result.json").write_text(json.dumps(data), encoding="utf-8")
        verify = next(c for c in self.run_report().checks if c.id == "stage:verify")
        self.assertEqual((verify.status, verify.detail["why"]), ("not_run", "no_timestamp"))

    def test_a_narrowed_verify_is_not_a_pass(self):
        pipeline(self.tmp)
        write_result(self.tmp, "verify", checks=(("v1:q04_depth_histogram", "pass"), ("scope:gold", "not_run")),
                     start=10, finish=11, action="rerun verify without --gold to check every gold query")
        res = self.run_report()
        self.assertEqual((statuses(res)["stage:verify"], res.status, res.exit_code()), ("not_run", "not_run", 1))
        self.assertEqual(res.next_safe_action,
                         "fix verify first (not_run): rerun verify without --gold to check every gold query")

    def test_reason_is_required_only_with_shapes_or_an_ontology(self):
        plain = self.tmp / "plain.toml"
        plain.write_text('version = 1\n[source]\nschemas = ["src"]\n[entities.item]\ntable = "src.item"\nkey = ["item_id"]\n',
                         encoding="utf-8")
        cfg = config.load(plain)
        self.assertEqual(report.required_stages(cfg), ["build", "cube", "verify"])
        self.assertEqual(report.required_stages(self.cfg), ["build", "reason", "cube", "verify"])
        self.assertEqual(report.required_stages(None), ["build", "reason", "cube", "verify"])
        pipeline(self.tmp, ("build", "cube", "verify"), inputs=inputs_fingerprint(cfg))
        self.assertEqual(self.run_report(cfg).status, "pass")
        write_result(self.tmp, "reason", checks=(), start=3, finish=3, inputs=inputs_fingerprint(cfg))   # nothing to check
        res = self.run_report(cfg)
        self.assertEqual(res.status, "pass")
        self.assertNotIn("stage:reason", statuses(res))
        self.assertTrue(any(n.startswith("stage reason is optional for this configuration") for n in res.not_proven))
        js = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        self.assertEqual((js["stages"]["reason"]["required"], js["stages"]["reason"]["counted_as"]), (False, None))

    def test_results_recorded_with_other_inputs_are_stale(self):
        """Editing crosscheck.toml or a gold query after a passing run, without rerunning, must not pass."""
        pipeline(self.tmp)
        self.assertEqual(self.run_report().status, "pass")
        other = dict(DEMO_INPUTS, config_sha256="0" * 64)
        pipeline(self.tmp, inputs=other)
        res = self.run_report()
        self.assertNotEqual(res.status, "pass")
        build = next(c for c in res.checks if c.id == "stage:build")
        self.assertEqual((build.status, build.detail["reason"], build.detail["why"]), ("not_run", "stale", "inputs_changed"))
        gold_only = dict(DEMO_INPUTS, gold_sha256="0" * 64)
        pipeline(self.tmp, inputs=gold_only)
        st = statuses(self.run_report())
        self.assertEqual((st["stage:build"], st["stage:cube"], st["stage:verify"]), ("pass", "pass", "not_run"))
        pipeline(self.tmp, inputs={})
        self.assertNotEqual(self.run_report().status, "pass")

    def test_staleness_follows_the_dependencies(self):
        self.assertEqual(report.DEPENDS, {"build": ("demo",), "reason": ("build",), "cube": ("build",),
                                          "verify": ("build", "cube")})
        pipeline(self.tmp)
        write_result(self.tmp, "build", start=20, finish=21)
        stale = report.staleness(report.read_results(self.tmp))
        self.assertEqual(stale, {"reason": {"after": "build", "why": "newer"}, "cube": {"after": "build", "why": "newer"},
                                 "verify": {"after": "build", "why": "newer"}})


if __name__ == "__main__":
    unittest.main()
