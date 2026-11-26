"""Unit tests for opt-in report --permitted-assumptions closed allowlist checking."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pgfixture
import sxc
from semantic_crosscheck import config, cube_run, report
from semantic_crosscheck.result import EXIT_CONFIG, StageError


def _write_stage(out: Path, stage: str, started: str, finished: str, checks: list[dict], **extra) -> None:
    folder = out / stage
    folder.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": 1,
        "stage": stage,
        "status": "pass",
        "started_at": started,
        "finished_at": finished,
        "checks": checks,
        "not_proven": [],
        "next_safe_action": "",
        "inputs": {},
        "provenance": {"schema_version": 1, "binding": "unbound"},
        "runtime_evidence": {"schema_version": 1, "binding": "unbound"},
        **extra,
    }
    (folder / "result.json").write_text(json.dumps(doc), encoding="utf-8")


def _clean_pipeline(out: Path, *, with_doctor: bool = False, doctor_checks: list[dict] | None = None,
                    verify_extra: dict | None = None) -> None:
    facts = cube_run.runtime_facts(config.CUBE_VERSION)
    if with_doctor:
        checks = doctor_checks if doctor_checks is not None else [
            {"id": "python", "status": "pass", "detail": {}},
            {"id": "pg_version", "status": "pass", "detail": {"db_collation_version_mismatch": False}},
            {"id": "privileges:select:part", "status": "pass", "detail": {
                "relkind": "r", "relpersistence": "p", "view_security_invoker": False,
                "row_security": False, "child_partition_row_security": False,
                "child_foreign_partitions": False, "child_unlogged_partitions": False,
                "unvalidated_constraints": 0, "child_unvalidated_constraints": 0,
                "mismatched_column_collations": 0, "has_rules": False,
            }},
        ]
        _write_stage(out, "doctor", "2026-11-27T01:00:00+0000", "2026-11-27T01:00:01+0000", checks)
    _write_stage(out, "build", "2026-11-27T01:00:01+0000", "2026-11-27T01:00:02+0000",
                 [{"id": "closure:bom", "status": "pass", "detail": {}}])
    _write_stage(out, "reason", "2026-11-27T01:00:03+0000", "2026-11-27T01:00:04+0000",
                 [{"id": "owl:consistency", "status": "pass", "detail": {}}])
    _write_stage(out, "cube", "2026-11-27T01:00:03+0000", "2026-11-27T01:00:04+0000",
                 [{"id": "cube:meta", "status": "pass", "detail": dict(facts)}])
    v_extra = verify_extra if verify_extra is not None else {
        "evidence": {
            "schema_version": 1, "rule_version": 1, "mode": "disclose", "scope": "full",
            "gold": [{"id": "q01", "kind": "authored", "generated_text": []}],
            "facets": [{"element": "measures.m", "facet": "m_m.value", "status": "compared", "reason": ""}],
            "facets_reason": "",
            "zero_row_checks": [],
            "empty_stratum_zero_row_checks": [],
            "gold_no_rows": [],
            "mutation": {
                "operators_selected": 9, "operators_not_selected": [],
                "killed_authored": 1, "killed_generated_only": 0, "unattributed": 0,
                "accepted": 0, "survived": 0, "not_judged": 0, "equivalent": 0, "not_applicable": 0,
                "generated_only": [],
            },
        }
    }
    v_checks = [
        {"id": "v1:q01", "status": "pass", "detail": {}},
        {"id": "v4:measure_agg_to_max:m", "status": "pass",
         "detail": {"killed_by": "v1:q01", "detection": "authored"}},
    ]
    _write_stage(out, "verify", "2026-11-27T01:00:05+0000", "2026-11-27T01:00:06+0000", v_checks, **v_extra)


class ReportAssumptionsTest(unittest.TestCase):
    def test_parse_permitted_assumptions_valid_and_fail_closed(self):
        self.assertIsNone(report.parse_permitted_assumptions(None))
        self.assertEqual(report.parse_permitted_assumptions("none"), frozenset())
        self.assertEqual(
            report.parse_permitted_assumptions("doctor_not_run, unbound_data_revision"),
            frozenset({"doctor_not_run", "unbound_data_revision"}),
        )
        for bad in ("", "   ", "none,doctor_not_run", "not_a_real_assumption", "doctor_not_run,", 123):
            with self.subTest(bad=bad):
                with self.assertRaises(StageError) as ctx:
                    report.parse_permitted_assumptions(bad)
                self.assertEqual(ctx.exception.exit_code, EXIT_CONFIG)

    def test_default_report_omits_assumption_policy_and_check(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _clean_pipeline(out)
            res = report.run(None, SimpleNamespace(require_doctor=False, permitted_assumptions=None), out)
            self.assertEqual(res.status, "pass")
            self.assertFalse(any(c.id == "assumptions:permitted" for c in res.checks))
            summary = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertNotIn("assumption_policy", summary)
            self.assertNotIn("## Assumption policy", (out / "report.md").read_text(encoding="utf-8"))

    def test_opt_in_allowlist_fails_on_disallowed_and_passes_when_all_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _clean_pipeline(out)
            # Empty allowlist ('none') rejects unbound provenance, unbound runtime, and missing doctor.
            res_fail = report.run(None, SimpleNamespace(require_doctor=False, permitted_assumptions="none"), out)
            self.assertEqual(res_fail.status, "fail")
            self.assertEqual(res_fail.exit_code(), 1)
            chk = next(c for c in res_fail.checks if c.id == "assumptions:permitted")
            self.assertEqual(chk.status, "fail")
            self.assertEqual(
                chk.detail["disallowed"],
                ["doctor_not_run", "unbound_data_revision", "unbound_runtime_evidence"],
            )
            summary = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["assumption_policy"]["passed"])
            self.assertEqual(summary["status"], "fail")
            md = (out / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Assumption policy", md)
            self.assertIn("Policy verdict: **FAIL**", md)
            self.assertIn("resolve disallowed assumptions", res_fail.next_safe_action)

            # Explicitly permitting those three codes passes cleanly.
            allow = "doctor_not_run,unbound_data_revision,unbound_runtime_evidence"
            res_ok = report.run(None, SimpleNamespace(require_doctor=False, permitted_assumptions=allow), out)
            self.assertEqual(res_ok.status, "pass")
            self.assertEqual(res_ok.exit_code(), 0)
            summary_ok = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(summary_ok["assumption_policy"]["passed"])
            self.assertEqual(summary_ok["assumption_policy"]["disallowed"], [])

    def test_detects_doctor_evidence_and_runtime_assumption_codes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            doctor_checks = [
                {"id": "pg_version", "status": "pass", "detail": {"db_collation_version_mismatch": True}},
                {"id": "privileges:select:v_orders", "status": "pass", "detail": {
                    "relkind": "v", "relpersistence": "p", "view_security_invoker": False,
                    "row_security": True, "mismatched_column_collations": 1,
                }},
            ]
            verify_extra = {
                "evidence": {
                    "schema_version": 1, "rule_version": 1, "mode": "disclose", "scope": "full",
                    "gold": [{"id": "q01", "kind": "generated", "generated_text": ["output_schema"]}],
                    "facets": [
                        {"element": "measures.m", "facet": "m_m.value", "status": "gap", "reason": "generated_text"},
                        {"element": "measures.m", "facet": "part+m_m", "status": "accepted", "reason": ""},
                    ],
                    "facets_reason": "",
                    "zero_row_checks": ["v1:q01"],
                    "empty_stratum_zero_row_checks": [],
                    "gold_no_rows": ["q01"],
                    "mutation": {
                        "operators_selected": 7,
                        "operators_not_selected": ["closure_end_swap", "dated_shift_one_day"],
                        "killed_authored": 0, "killed_generated_only": 1, "unattributed": 0,
                        "accepted": 0, "survived": 0, "not_judged": 0, "equivalent": 0, "not_applicable": 0,
                        "generated_only": ["v4:measure_agg_to_max:m"],
                    },
                }
            }
            _clean_pipeline(out, with_doctor=True, doctor_checks=doctor_checks, verify_extra=verify_extra)
            # Update verify check to match generated_only mutant split
            v_path = out / "verify" / "result.json"
            v_doc = json.loads(v_path.read_text(encoding="utf-8"))
            v_doc["checks"][1]["detail"] = {"killed_by": "v3:total:m_m.value", "detection": "generated_only"}
            v_path.write_text(json.dumps(v_doc), encoding="utf-8")

            res = report.run(None, SimpleNamespace(require_doctor=False, permitted_assumptions="none"), out)
            self.assertEqual(res.status, "fail")
            summary = json.loads((out / "report.json").read_text(encoding="utf-8"))
            active = set(summary["assumption_policy"]["active"])
            expected = {
                "accepted_gaps_present",
                "catalog_hygiene_warnings",
                "collation_version_mismatch",
                "facet_gaps_present",
                "generated_only_mutant_kills",
                "gold_generated_text",
                "gold_no_rows",
                "narrowed_mutation_operators",
                "unbound_data_revision",
                "unbound_runtime_evidence",
                "view_security_definer",
                "zero_row_authored_checks",
            }
            self.assertEqual(active, expected)
            self.assertNotIn("doctor_not_run", active)

    def test_detects_missing_evidence_trusted_sql_and_data_rules(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _clean_pipeline(out, with_doctor=True, verify_extra={})
            demo_cfg = config.load(pgfixture.DEMO_CONFIG)
            # Simulate configured data_rules + trusted gold/shapes from demo_cfg
            fake_cfg = SimpleNamespace(
                path=demo_cfg.path,
                base_dir=demo_cfg.base_dir,
                source=demo_cfg.source,
                cube=demo_cfg.cube,
                ontology=demo_cfg.ontology,
                shapes=demo_cfg.shapes,
                gold=demo_cfg.gold,
                measures={},
                verify=demo_cfg.verify,
                semantic_preflight=None,
                data_rules=object(),
            )
            # Align inputs fingerprints so stages are not stale
            res = report.run(fake_cfg, SimpleNamespace(require_doctor=False, permitted_assumptions="none"), out)
            self.assertEqual(res.status, "fail")
            summary = json.loads((out / "report.json").read_text(encoding="utf-8"))
            active = set(summary["assumption_policy"]["active"])
            self.assertIn("verification_evidence_missing", active)
            self.assertIn("trusted_sql_present", active)
            self.assertIn("data_rules_not_counted", active)

    def test_detects_unattributed_mutant_kills_and_handles_malformed_inner_types(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            _clean_pipeline(out, with_doctor=True, verify_extra={
                "evidence": {
                    "schema_version": 1, "rule_version": 1, "mode": "disclose", "scope": "full",
                    "gold": [{"id": "q01", "kind": "authored", "generated_text": []}],
                    "facets": [{"element": "measures.m", "facet": "m_m.value", "status": "compared", "reason": ""}],
                    "facets_reason": "",
                    "zero_row_checks": [],
                    "empty_stratum_zero_row_checks": [],
                    "gold_no_rows": [],
                    "mutation": {
                        "operators_selected": 9,
                        "operators_not_selected": [],
                        "killed_authored": 0, "killed_generated_only": 0, "unattributed": 1,
                        "accepted": 0, "survived": 0, "not_judged": 0, "equivalent": 0, "not_applicable": 0,
                        "generated_only": [],
                    },
                }
            })
            # Mark existing mutant pass without killed_by/detection -> unattributed_mutant_kills
            v_path = out / "verify" / "result.json"
            v_doc = json.loads(v_path.read_text(encoding="utf-8"))
            v_doc["checks"][1]["detail"] = {}
            v_path.write_text(json.dumps(v_doc), encoding="utf-8")

            # Inject malformed non-dict runtime_evidence.components and non-int/bool doctor counts
            b_path = out / "build" / "result.json"
            b_doc = json.loads(b_path.read_text(encoding="utf-8"))
            b_doc["runtime_evidence"] = {"schema_version": 1, "binding": "bound", "components": ["bad"]}
            b_path.write_text(json.dumps(b_doc), encoding="utf-8")

            d_path = out / "doctor" / "result.json"
            d_doc = json.loads(d_path.read_text(encoding="utf-8"))
            d_doc["checks"].append({
                "id": "privileges:select:malformed",
                "status": "pass",
                "detail": {
                    "relkind": "r",
                    "mismatched_column_collations": "not-an-int",
                    "unvalidated_constraints": True,
                    "child_unvalidated_constraints": ["bad"],
                },
            })
            d_path.write_text(json.dumps(d_doc), encoding="utf-8")

            res = report.run(None, SimpleNamespace(require_doctor=False, permitted_assumptions="none"), out)
            self.assertEqual(res.status, "fail")
            summary = json.loads((out / "report.json").read_text(encoding="utf-8"))
            active = set(summary["assumption_policy"]["active"])
            self.assertIn("unattributed_mutant_kills", active)
            # Bool True / invalid string/list must NOT trigger numeric count assumptions
            self.assertNotIn("collation_version_mismatch", active)
            self.assertNotIn("catalog_hygiene_warnings", active)
            # Direct call with malformed summary sub-fields must also never crash
            self.assertEqual(
                report.active_assumptions({"stages": None, "provenance": ["x"], "runtime_evidence": ["y"]}, {}, None),
                ["doctor_not_run"],
            )

    def test_cli_parser_accepts_permitted_assumptions_flag(self):
        parsed = sxc.parser().parse_args(["report", "--permitted-assumptions", "none"])
        self.assertEqual(parsed.stage, "report")
        self.assertEqual(parsed.permitted_assumptions, "none")


if __name__ == "__main__":
    unittest.main()
