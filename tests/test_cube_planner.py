"""The Cube SQL planner is pinned, recorded and part of what decides whether a cube result is current.

- Cube 1.8.3 picks its SQL planner from CUBEJS_TESSERACT_SQL_PLANNER (default true, Tesseract; false selects the
  legacy planner). The runner sets it in the child's environment (tests/test_security_cube.py), and the cube stage
  records the planner the child was given next to the Cube version on `cube:meta`, error or not.
- report treats a cube result that recorded another Cube version or planner than this checkout pins as stale
  (why=cube_runtime_changed), and one whose `cube:meta` recorded neither as stale too (why=cube_runtime_missing):
  a result from a runner that did not pin the planner cannot be shown to have been served by the pinned one. verify,
  which depends on cube, is then stale as well.
- The engine fingerprint, which already makes every stage stale when the code changes, hashes cube_run.py, where
  the fixed planner value lives.
No database, Cube install or network is needed: the stage's catalog read and Cube are replaced by stand-ins.
"""

import argparse
import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import config, cube_run, provenance, report, stage_cube
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import Result, inputs_fingerprint

MODEL = {"cubes": [{"name": "part", "sql_table": "src.part",
                    "dimensions": [{"name": "part_id", "sql": "part_id", "type": "string", "primary_key": True,
                                    "public": True}],
                    "measures": [{"name": "count", "type": "count"}]}]}
META = {"cubes": [{"name": "part", "dimensions": [{"name": "part.part_id"}], "measures": [{"name": "part.count"}]}]}


class _Client:
    def __init__(self, meta, runtime):
        self._meta = meta
        if runtime is not None:
            self.runtime = runtime

    def meta(self):
        if isinstance(self._meta, Exception):
            raise self._meta
        return self._meta


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sxc_t_planner_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "crosscheck.toml"
        self.path.write_text('version=1\n[source]\nschemas=["src"]\n', encoding="utf-8")
        self.cfg = config.load(self.path)

    def stage(self, meta=META, runtime="pinned") -> Result:
        facts = cube_run.runtime_facts(self.cfg.cube.version) if runtime == "pinned" else runtime

        @contextlib.contextmanager
        def serve(_cfg, _run_dir, _log=None):
            yield _Client(meta, facts)

        with mock.patch.object(stage_cube, "generate", return_value=(MODEL, [])), \
             mock.patch.object(stage_cube.cube_run, "serve", serve):
            return stage_cube.run(self.cfg, argparse.Namespace(), self.root / "out")


class StageRecordTest(Case):
    def meta_check(self, res: Result):
        return next(c for c in res.checks if c.id == "cube:meta")

    def test_cube_meta_records_the_cube_version_and_the_planner(self):
        res = self.stage()
        check = self.meta_check(res)
        self.assertEqual(check.status, "pass")
        self.assertEqual((check.detail["cube_version"], check.detail["sql_planner"]), (config.CUBE_VERSION, "tesseract"))
        self.assertEqual(res.to_dict()["checks"][-1]["detail"]["sql_planner"], "tesseract")

    def test_errors_and_failures_record_the_served_planner_too(self):
        for meta in (CubeError("HTTP 500: boom"), ConnectionResetError(), [], None, {"cubes": []}):
            with self.subTest(meta=type(meta).__name__):
                check = self.meta_check(self.stage(meta=meta))
                self.assertNotEqual(check.status, "pass")
                self.assertEqual(check.detail["sql_planner"], "tesseract")
                self.assertEqual(check.detail["cube_version"], config.CUBE_VERSION)

    def test_a_json_null_meta_answer_is_an_unexpected_response(self):
        """HTTP 200 with the body `null` still leaves a cube:meta check, so report sees the served runtime (or its
        absence) instead of a stage that passes without one."""
        res = self.stage(meta=None)
        check = self.meta_check(res)
        self.assertEqual((check.status, check.detail["reason"]), ("error", "unexpected_response"))
        self.assertEqual(res.status, "error")
        self.assertEqual(self.meta_check(self.stage(meta=None, runtime=None)).detail, {"reason": "unexpected_response"})

    def test_a_client_without_runtime_facts_records_none_instead_of_guessing(self):
        check = self.meta_check(self.stage(runtime=None))
        self.assertNotIn("sql_planner", check.detail)
        self.assertNotIn("cube_version", check.detail)


class ReportStalenessTest(Case):
    def write(self, stage: str, minute: int, **meta_detail) -> None:
        res = Result(stage)
        if stage == "cube":
            res.add("cube:meta", "pass", "", missing=0, extra=0, **meta_detail)
        else:
            res.add("fixture", "pass")
        saved = res.to_dict()
        saved.update(inputs=inputs_fingerprint(self.cfg), started_at=f"2026-10-18T10:{minute:02d}:00+0000",
                     finished_at=f"2026-10-18T10:{minute:02d}:30+0000")
        target = self.root / "out" / stage
        target.mkdir(parents=True, exist_ok=True)
        (target / "result.json").write_text(json.dumps(saved), encoding="utf-8")

    def pipeline(self, **meta_detail):
        self.write("build", 1)
        self.write("cube", 2, **meta_detail)
        self.write("verify", 3)
        results = report.read_results(self.root / "out")
        stale = report.staleness(results, inputs_fingerprint(self.cfg))
        res = report.run(self.cfg, argparse.Namespace(), self.root / "out")
        return stale, res

    def test_pinned_planner_is_current(self):
        stale, res = self.pipeline(**cube_run.runtime_facts(config.CUBE_VERSION))
        self.assertEqual(stale, {})
        self.assertEqual(res.status, "pass")

    def test_another_planner_or_cube_version_is_stale(self):
        for recorded in ({"cube_version": config.CUBE_VERSION, "sql_planner": "legacy"},
                         {"cube_version": config.CUBE_VERSION, "sql_planner": "unknown"},
                         {"cube_version": "1.8.4", "sql_planner": "tesseract"}):
            with self.subTest(recorded=recorded):
                stale, res = self.pipeline(**recorded)
                self.assertEqual(stale["cube"], {"after": "Cube runtime", "why": "cube_runtime_changed"})
                self.assertEqual(stale["verify"], {"after": "cube", "why": "stale_dependency"})
                self.assertEqual(res.status, "not_run", "a result served by another planner must not pass the report")
                summary = json.loads((self.root / "out" / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["stages"]["cube"]["stale"]["why"], "cube_runtime_changed")
                self.assertIn("another Cube version or SQL planner", (self.root / "out" / "report.md").read_text(
                    encoding="utf-8"))

    def test_a_cube_meta_without_recorded_planner_is_stale(self):
        for recorded in ({}, {"cube_version": config.CUBE_VERSION}, {"sql_planner": "tesseract"}):
            with self.subTest(recorded=recorded):
                stale, res = self.pipeline(**recorded)
                self.assertEqual(stale["cube"]["why"], "cube_runtime_missing")
                self.assertEqual(res.status, "not_run")

    def test_the_current_runtime_follows_the_fixed_child_value(self):
        """Changing the value the runner sets changes what report expects: results recorded before are stale."""
        recorded = cube_run.runtime_facts(config.CUBE_VERSION)
        with mock.patch.dict(cube_run.PLANNER_ENV, {cube_run.SQL_PLANNER_VAR: "false"}):
            stale, res = self.pipeline(**recorded)
        self.assertEqual(stale["cube"]["why"], "cube_runtime_changed")
        self.assertEqual(res.status, "not_run")

    def test_results_without_cube_meta_are_left_to_their_own_checks(self):
        """A cube result that stopped before Cube served anything (a `stage` error) has nothing to compare; its own
        error keeps it from passing."""
        res = Result("cube")
        res.add("stage", "error", "", message="node is not on PATH")
        stale = report.staleness({"cube": res.to_dict()})
        self.assertNotIn("cube", stale)


class EngineFingerprintTest(unittest.TestCase):
    def test_engine_fingerprint_hashes_the_runner_that_fixes_the_planner(self):
        labels = {label: path for label, path in provenance._engine_files()}
        self.assertIn("python/cube_run.py", labels)
        self.assertEqual(labels["python/cube_run.py"].resolve(), Path(cube_run.__file__).resolve())
        self.assertTrue(b"CUBEJS_TESSERACT_SQL_PLANNER" in labels["python/cube_run.py"].read_bytes(),
                        "the fixed planner value must live in a file the engine fingerprint hashes")


if __name__ == "__main__":
    unittest.main()
