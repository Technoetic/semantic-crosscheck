"""Regression tests for v0.26.0 runtime & evidence hardening (Group B)."""

import argparse
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import closure_oracle, config, cube_run, db, expect, ident, naming, owl, report, shape_q, sweep
from semantic_crosscheck.result import EXIT_CONFIG, Result, StageError, inputs_fingerprint


class CubeAllowlistTest(unittest.TestCase):
    def test_unrelated_tokens_and_proxies_do_not_reach_cube_child(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        secret_env = {
            cfg.cube_dsn_env(): "postgresql://xc_check@127.0.0.1:5432/sxc_demo",
            "GITHUB_TOKEN": "ghp_synthetic_token_value",
            "OPENAI_API_KEY": "sk-synthetic-openai",
            "AWS_SECRET_ACCESS_KEY": "aws-synthetic-secret",
            "HTTP_PROXY": "http://127.0.0.1:8080",
            "UNLISTED_CUSTOM_VAR": "should-not-inherit",
            "PATH": os.environ.get("PATH", "/usr/bin"),
        }
        with patch.dict(os.environ, secret_env, clear=True):
            env = cube_run.child_env(cfg, 4000, "s" * 64)
        for blocked in ("GITHUB_TOKEN", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "HTTP_PROXY", "UNLISTED_CUSTOM_VAR"):
            self.assertNotIn(blocked, env)
        self.assertIn("PATH", env)


class TimeoutBoundsTest(unittest.TestCase):
    def test_db_connect_passes_bounded_timeout(self):
        captured = {}

        class FakeConn:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self, sql, **_params):
                return [["170004"]] if sql == "SHOW server_version_num" else []

            def close(self):
                pass

        with patch.dict(os.environ, {"SXC_T_TIMEOUT_DSN": "postgresql://u@127.0.0.1:5432/db"}),                 patch("pg8000.native.Connection", FakeConn):
            db.connect("SXC_T_TIMEOUT_DSN")
        self.assertEqual(captured.get("timeout"), getattr(db, "CONNECT_TIMEOUT_S", None))
        self.assertIsInstance(captured.get("timeout"), int)
        self.assertGreaterEqual(captured["timeout"], 5)

    @unittest.skipUnless(importlib.util.find_spec("owlready2") is not None or os.environ.get("SXC_TEST_FULLSTACK") == "1", "owlready2 not installed")
    def test_owl_hermit_sets_subprocess_timeout_and_maps_timeout_expired(self):
        import owlready2
        import owlready2.reasoning as reasoning

        seen_kargs = {}
        before_kargs = dict(getattr(reasoning, "_subprocess_kargs", {}))

        def fake_sync(_world, **_kw):
            seen_kargs.update(getattr(reasoning, "_subprocess_kargs", {}))
            raise subprocess.TimeoutExpired(cmd=["java"], timeout=60)

        w = owlready2.World()
        try:
            with patch.object(owlready2, "sync_reasoner_hermit", side_effect=fake_sync):
                with self.assertRaises(owl.ReasonerError) as raised:
                    owl.hermit(w, "java")
            self.assertIn("TimeoutExpired", str(raised.exception))
            self.assertEqual(seen_kargs.get("timeout"), getattr(owl, "HERMIT_TIMEOUT_S", None))
            self.assertEqual(getattr(reasoning, "_subprocess_kargs", {}), before_kargs)
        finally:
            with contextlib.suppress(Exception):
                w.close()


class ClosureSqlQuotingTest(unittest.TestCase):
    def test_closure_table_is_quoted_in_sweep_shape_q_and_closure_oracle(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        h = cfg.hierarchy()
        quoted = ident.qualified(naming.closure_table(cfg, h.name))
        self.assertEqual(quoted, '"sxc_out"."has_component_closure"')
        self.assertIn(quoted, sweep.strata_sql(cfg, h, ["deep"], True))
        self.assertEqual(shape_q.closure_total_sql(cfg, h.name, "qty"), f'SELECT sum("qty") FROM {quoted}')
        test_spec = {
            "root": "measure",
            "null_source": {
                "dim_table": "sxc_demo.part",
                "dim_key": "part_id",
                "fk": "ancestor",
                "class_column": "part_class",
                "hierarchy": h.name,
                "closure_column": "qty",
            },
        }
        self.assertIn(f"FROM {quoted} t", shape_q.null_group_sql(cfg, test_spec))

        executed = []

        class RecordingConn:
            def run(self, sql, **_params):
                executed.append(sql)
                if "pg_attribute" in sql:
                    return [("part_id", "text", True), ("parent_id", "text", True), ("child_id", "text", True)]
                return []

        closure_oracle.run(RecordingConn(), cfg, h)
        self.assertTrue(any(f"FROM {quoted}" in s for s in executed), executed)


class DoctorGateReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_doc_gate_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write_stage(self, out: Path, stage: str, fp: dict, status: str = "pass", minute: int = 1):
        res = Result(stage)
        res.add(f"{stage}:ok", status)
        data = res.to_dict()
        data["started_at"] = f"2026-11-15T10:{minute:02d}:00+0000"
        data["finished_at"] = f"2026-11-15T10:{minute:02d}:30+0000"
        data["inputs"] = fp
        (out / stage).mkdir(parents=True, exist_ok=True)
        (out / stage / "result.json").write_text(json.dumps(data), encoding="utf-8")

    def test_require_doctor_Via_config_and_cli_gates_missing_stale_and_passing_doctor(self):
        cfg_path = self.tmp / "crosscheck.toml"
        base_text = (
            'version = 1\n[source]\nschemas = ["src"]\n'
            '[entities.item]\ntable = "src.item"\nkey = ["id"]\n'
        )
        cfg_path.write_text(base_text, encoding="utf-8")
        cfg = config.load(cfg_path)
        self.assertEqual(cfg.verify.doctor_observation, "disclose")
        fp = inputs_fingerprint(cfg)
        out = self.tmp / "out"
        for idx, stg in enumerate(("build", "cube", "verify"), 2):
            self._write_stage(out, stg, fp, minute=idx)

        # Default disclose mode still passes without doctor
        self.assertEqual(report.run(cfg, argparse.Namespace(), out).status, "pass")

        # Empty results with --require-doctor must not list "doctor" twice in next_safe_action
        empty_out = self.tmp / "empty_out"
        empty_res = report.run(cfg, argparse.Namespace(require_doctor=True), empty_out)
        self.assertEqual(empty_res.status, "not_run")
        self.assertNotIn("doctor`, then doctor", empty_res.next_safe_action)

        # CLI --require-doctor fails (not_run missing) when doctor is absent
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = sxc.main(["report", "--config", str(cfg_path), "--out", str(out), "--require-doctor"])
        self.assertEqual(rc, 1)
        rj = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertIn("doctor", rj["required"])
        self.assertIn("doctor", rj["missing_required"])

        # Doctor without config fingerprint (e.g. run without --config) is stale under --require-doctor
        self._write_stage(out, "doctor", {}, minute=0)
        res_stale = report.run(cfg, argparse.Namespace(require_doctor=True), out)
        self.assertEqual(res_stale.status, "not_run")

        # Current matching doctor passes both CLI --require-doctor and [verify] doctor_observation = "require"
        self._write_stage(out, "doctor", fp, minute=0)
        self.assertEqual(report.run(cfg, argparse.Namespace(require_doctor=True), out).status, "pass")

        cfg_path.write_text(base_text + '[verify]\ndoctor_observation = "require"\n', encoding="utf-8")
        cfg_req = config.load(cfg_path)
        fp_req = inputs_fingerprint(cfg_req)
        for idx, stg in enumerate(("doctor", "build", "cube", "verify"), 1):
            self._write_stage(out, stg, fp_req, minute=idx)
        self.assertEqual(report.run(cfg_req, argparse.Namespace(), out).status, "pass")

        cfg_path.write_text(base_text + '[verify]\ndoctor_observation = "invalid"\n', encoding="utf-8")
        with self.assertRaises(StageError) as bad:
            config.load(cfg_path)
        self.assertEqual(bad.exception.exit_code, EXIT_CONFIG)
        self.assertIn("verify.doctor_observation", str(bad.exception))


class ExpectZeroRowDisclosureTest(unittest.TestCase):
    def test_zero_row_expect_pass_marks_empty_and_discloses_not_proven(self):
        g = config.Gold(id="q", sql="q.sql", key=["part_id"], map={"part_id": "c.d"}, zero_if_null=[])
        c_empty = config.Expect(source="authored", rows=[], spec="EX-EMPTY")
        status, detail = expect.judge(g, c_empty, [], [], {"part_id": "string"})
        self.assertEqual(status, "pass")
        self.assertIs(detail.get("empty"), True)

        c_nonempty = config.Expect(source="authored", rows=[{"part_id": "A"}], spec="EX-ONE")
        status2, detail2 = expect.judge(g, c_nonempty, [{"part_id": "A"}], [{"part_id": "A"}], {"part_id": "string"})
        self.assertEqual(status2, "pass")
        self.assertNotIn("empty", detail2)

        with tempfile.TemporaryDirectory(prefix="sxc_exp_empty_") as td:
            work = Path(td)
            cfg = config.load(pgfixture.DEMO_CONFIG)
            res = Result("expect")
            digest = {"fixture_sha256": "a" * 64, "tables_sha256": "b" * 64, "rows": 0}
            planned = [
                ("expect:q[0]", "gold[0] q expect[0]", g, c_empty),
                ("expect:q[1]", "gold[0] q expect[1]", g, c_nonempty),
            ]
            with patch.object(expect, "share_locks", lambda *_a, **_k: contextlib.nullcontext(True)),                  patch.object(expect, "fixture_digest", return_value=digest),                  patch.object(cube_run, "serve", lambda *_a, **_k: contextlib.nullcontext(object())),                  patch.object(expect, "run_case", side_effect=[(status, detail), (status2, detail2)]):
                expect._run_cases(res, object(), cfg, "SXC_EXPECT_DSN", work, planned, {}, [], digest, {})
            self.assertEqual(res.status, "pass")
            self.assertTrue(
                any("expect:q[0] matched 0 expected and 0 returned rows (empty = true)" in line for line in res.not_proven),
                res.not_proven,
            )
            self.assertFalse(any("expect:q[1]" in line for line in res.not_proven), res.not_proven)

            # If expect:fixture detects mid-run tampering, demoted not_run checks must not retain empty=True
            res_tampered = Result("expect")
            tampered_end = {"fixture_sha256": "c" * 64, "tables_sha256": "b" * 64, "rows": 1}
            with patch.object(expect, "share_locks", lambda *_a, **_k: contextlib.nullcontext(True)),                  patch.object(expect, "fixture_digest", side_effect=[digest, tampered_end]),                  patch.object(cube_run, "serve", lambda *_a, **_k: contextlib.nullcontext(object())),                  patch.object(expect, "run_case", side_effect=[(status, dict(detail)), (status2, dict(detail2))]):
                expect._run_cases(res_tampered, object(), cfg, "SXC_EXPECT_DSN", work, planned, {}, [], digest, {})
            self.assertEqual(res_tampered.status, "error")
            c0 = next(c for c in res_tampered.checks if c.id == "expect:q[0]")
            self.assertEqual(c0.status, "not_run")
            self.assertNotIn("empty", c0.detail)


if __name__ == "__main__":
    unittest.main()
