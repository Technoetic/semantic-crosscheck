"""What report says about the kinds of evidence behind the checks (the evidence key, section and console lines).

Why: a report that counts every passing check alike lets a reader take agreement between layers generated from one
crosscheck.toml, or a mutant killed only by such a check, for evidence that a person's gold SQL saw the data. report
now sorts every check of an included stage under its evidence kind (authored, recomputed, generated, structural,
mutation, and unclassified only when some check has no rule), and repeats what verify recorded: the mutant split,
the facets without authored evidence, the checks and gold queries that compared no row, and gold SQL with generated
text. It writes report.json `evidence`, a `## Evidence` section before `## Not proven` and `  EVIDENCE  ` console
lines, and only when a verify result exists, so a run without verify (the compatibility goldens) is unchanged. A
narrowed, stale or unrecorded verify run is said to be one, never shown as "0 of 0". Nothing of the gold text, the
rows or the parameters reaches these outputs. No database is needed.
"""

import contextlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture
import sxc
from semantic_crosscheck import config, evidence, report
from semantic_crosscheck.result import Result, inputs_fingerprint
from test_verify_evidence import SECRET_PARAM, SECRET_ROW, SECRET_RULE_VIEW, SECRET_TEXT, MutantRun, VerifyRun

DEMO_CONFIG = pgfixture.DEMO_CONFIG
DEMO_INPUTS = inputs_fingerprint(config.load(DEMO_CONFIG))
GOLDEN = pgfixture.ROOT / "tests" / "fixtures" / "compat_v0_21_1"
ORDER = ("doctor", "demo", "build", "reason", "cube", "verify")
ALL_OPERATORS = ", ".join(evidence.MUTATION_OPERATORS)
NO_SECTION = "not judged (the verify result has no evidence section)"
INTRO = [
    "Every check of an included stage counts once, under the kind of evidence it is (docs/design.md, section 8,",
    "\"Evidence kinds\"). Only authored and recomputed checks compare generated layers with something the generators "
    "did",
    "not produce. \"Authored\" says who wrote one side, not that it is right. Compared with rows is not detection: "
    "only an",
    "authored mutant kill shows that a change of a facet is seen. Paraphrased copies of generated SQL are not "
    "detected.",
    "Authored expected rows (`expect`) are not read by report.",
]
HEADER = ["| Kind | What the two sides are | Pass | Fail | Error | Not run |", "|---|---|---:|---:|---:|---:|"]
SIDES = {
    "authored": "gold SQL a person wrote (no verbatim generated text found) against the served model; {z} of them "
                "compared no row; v1 repeats the first binding of v2",
    "recomputed": "a recomputation from source rows by code that imports no generator",
    "generated": "two layers generated from crosscheck.toml (also by a second engine), or one generated layer with "
                 "itself",
    "structural": "one side only: a rule on the data, the configuration, the environment, the served model",
    "mutation": "broken copies of the model; pass = killed: {a} with an authored detection, {g} by generated checks "
                "only",
}

AGG = "v4:measure_agg_to_max:on_hand"
QTY = "v4:closure_qty_single_path:has_component"
VERIFY_CHECKS = [
    ("v3:closure_oracle:has_component", "pass", "hierarchies.has_component", {}),
    ("v1:q01_explosion", "pass", "gold.q01_explosion", {"empty": False}),
    ("v1:q02_where_used", "pass", "gold.q02_where_used", {"empty": False}),
    ("v2:q01_explosion:top", "pass", "gold.q01_explosion", {}),
    ("v2:q01_explosion:empty", "pass", "gold.q01_explosion", {"empty_stratum": True}),
    ("v2:q02_where_used:top", "pass", "gold.q02_where_used", {}),
    ("v3:total:m_on_hand.value", "pass", "measures.on_hand", {}),
    ("v4:suite", "pass", "", {}),
    (QTY, "pass", "hierarchies.has_component", {"killed_by": "v1:q01_explosion", "detection": "authored"}),
    (AGG, "pass", "measures.on_hand", {"killed_by": "v3:total:m_on_hand.value", "detection": "generated_only"}),
]
STAGE_CHECKS = {
    "doctor": [("python", "pass", "", {})],
    "demo": [("demo:load", "pass", "", {})],
    "build": [("shape:qty_positive", "pass", "shapes[1] qty_positive", {}),
              ("closure:has_component", "pass", "hierarchies.has_component", {}),
              ("closure_apply:has_component", "pass", "hierarchies.has_component", {})],
    "reason": [("shacl:meta:generated", "pass", "", {}), ("v5:closure:has_component", "pass", "", {})],
    "cube": [("cube:model", "pass", "", {})],
    "verify": VERIFY_CHECKS,
}


def split(**counts) -> dict:
    out = {"operators_selected": 9, "operators_not_selected": []}
    out.update({b: counts.get(b, 0) for b in evidence.MUTANT_BUCKETS})
    out["generated_only"] = counts.get("generated_only", [])
    return out


def facet_row(element: str, facet: str, status: str, reason: str = "") -> dict:
    return {"element": element, "facet": facet, "status": status, "reason": reason, "compared_by": [],
            "uses_only_by": [], "detected_by": []}


def gold_entry(gid: str, kind: str = "authored", text=()) -> dict:
    return {"id": gid, "kind": kind, "generated_text": list(text), "compares": [], "uses_only": [], "comparisons": 1,
            "nonempty_comparisons": 1, "value_rows": {}}


def recorded(**changes) -> dict:
    """A verify evidence object as P2 and P3 write it, for the checks of VERIFY_CHECKS."""
    data = {
        "schema_version": 1, "rule_version": evidence.RULE_VERSION, "mode": "disclose", "scope": "full",
        "gold": [gold_entry("q01_explosion"), gold_entry("q02_where_used")],
        "facets": [facet_row("measures.on_hand", "m_on_hand.value", "compared"),
                   facet_row("measures.firm_open", "m_firm_open.value", "compared"),
                   facet_row("hierarchies.has_component", "has_component_closure.qty", "detected"),
                   facet_row("measures.firm_open", "part+m_firm_open", "gap", "no_gold_reads")],
        "facets_reason": "",
        "zero_row_checks": ["v2:q02_where_used:top"],
        "empty_stratum_zero_row_checks": ["v2:q01_explosion:empty"],
        "gold_no_rows": [],
        "mutation": split(killed_authored=1, killed_generated_only=1, generated_only=[AGG]),
    }
    data.update(changes)
    return data


def at(minute: int) -> str:
    return f"2026-11-11T10:{minute:02d}:00+0000"


def write_stage(out: Path, stage: str, checks, minute: int, evidence_object=None, inputs=None,
                not_proven=()) -> None:
    res = Result(stage)
    for cid, status, ref, detail in checks:
        res.add(cid, status, ref, **detail)
    res.not_proven = list(not_proven)
    if evidence_object is not None:
        res.evidence = evidence_object
    data = res.to_dict()
    data["started_at"] = data["finished_at"] = at(minute)
    data["inputs"] = DEMO_INPUTS if inputs is None else inputs
    (out / stage).mkdir(parents=True, exist_ok=True)
    (out / stage / "result.json").write_text(json.dumps(data), encoding="utf-8")


def write_pipeline(out: Path, verify_evidence=None, stages=ORDER, verify_checks=None) -> None:
    for i, stage in enumerate(stages):
        checks = STAGE_CHECKS[stage] if stage != "verify" or verify_checks is None else verify_checks
        write_stage(out, stage, checks, 2 * i, verify_evidence if stage == "verify" else None)


def run_report(out: Path, *extra, config_file=DEMO_CONFIG) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = sxc.main(["report", "--config", str(config_file), "--out", str(out), *extra])
    return code, buf.getvalue()


def section(md: str) -> list[str]:
    """The lines of the ## Evidence section, from its heading to the line before ## Not proven."""
    lines = md.split("\n")
    start, end = lines.index("## Evidence"), lines.index("## Not proven")
    return lines[start:end]


def evidence_lines(console: str) -> list[str]:
    return [line for line in console.split("\n") if line.startswith("  EVIDENCE  ")]


class ReportOutputs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_report_evidence_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def outputs(self, *extra) -> tuple[dict, str, list[str], list[str]]:
        """report.json, report.md, the ## Evidence lines and the EVIDENCE console lines of one report run."""
        _code, console = run_report(self.tmp, *extra)
        js = json.loads((self.tmp / "report.json").read_text(encoding="utf-8"))
        md = (self.tmp / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("evidence", json.loads((self.tmp / "report" / "result.json").read_text(encoding="utf-8")))
        return js, md, (section(md) if "## Evidence" in md else []), evidence_lines(console)


class GoldenAbsenceTest(ReportOutputs):
    def test_golden_absence(self):
        """The compatibility goldens (build and report without verify) gain no key, section or line."""
        spec = importlib.util.spec_from_file_location("sxc_compat_capture_report_evidence", GOLDEN / "capture.py")
        capture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capture)
        files = capture.collect()
        for name in ("report_pass", "report_fail"):
            with self.subTest(scenario=name):
                self.assertNotIn("evidence", json.loads(files[f"{name}/report.json"]))
                self.assertNotIn("evidence", json.loads(files[f"{name}/result.json"]))
                self.assertNotIn("## Evidence", files[f"{name}/report.md"])
                self.assertNotIn("EVIDENCE", files[f"{name}/console.txt"])
                self.assertEqual(files[f"{name}/report.md"], (GOLDEN / name / "report.md").read_text(encoding="utf-8"))

    def test_every_stage_but_verify_adds_nothing(self):
        write_pipeline(self.tmp, stages=("doctor", "demo", "build", "reason", "cube"))
        js, md, lines, console = self.outputs()
        self.assertNotIn("evidence", js)
        self.assertNotIn("## Evidence", md)
        self.assertEqual((lines, console), ([], []))


class SectionAndLinesTest(ReportOutputs):
    def test_section_and_lines(self):
        write_pipeline(self.tmp, recorded())
        js, md, lines, console = self.outputs()
        self.assertEqual(js["status"], "pass")
        self.assertEqual(lines, ["## Evidence", ""] + INTRO + [""] + HEADER + [
            f"| authored | {SIDES['authored'].format(z=2)} | 5 | 0 | 0 | 0 |",
            f"| recomputed | {SIDES['recomputed']} | 1 | 0 | 0 | 0 |",
            f"| generated | {SIDES['generated']} | 3 | 0 | 0 | 0 |",
            f"| structural | {SIDES['structural']} | 7 | 0 | 0 | 0 |",
            f"| mutation | {SIDES['mutation'].format(a=1, g=1)} | 2 | 0 | 0 | 0 |",
            "",
            f"- Mutants (9 of 9 operators selected): 1 killed with an authored detection; 1 killed by generated checks "
            f"only ({AGG}); 0 unattributed; 0 accepted; 0 survived; 0 not judged; 0 equivalent on this data; 0 not "
            "applicable.",
            "- Facets: 2 of 4 compared with rows holding values by authored gold SQL; 1 more detected by an authored "
            "mutant kill; no authored evidence: part+m_firm_open (no_gold_reads); accepted: none.",
            "- Authored checks that compared no row: 1 (v2:q02_where_used:top); in strata named `empty`: 1 "
            "(v2:q01_explosion:empty). Gold queries that compared no row: none.",
            "- Gold SQL with generated text: none.",
            "- Stages not included: none.",
            ""])
        self.assertLess(md.index("## Checks that did not pass"), md.index("## Evidence"))
        self.assertEqual(console, [
            "  EVIDENCE  checks by kind (pass of all): authored 5/5 (1+1 compared no row), recomputed 1/1, "
            "generated 3/3, structural 7/7, mutation 2/2",
            f"  EVIDENCE  mutants (9 of 9 operators selected): 1 killed with authored detection, 1 killed by "
            f"generated checks only ({AGG}), 0 unattributed, 0 accepted, 0 survived, 0 not judged, 0 equivalent, "
            "0 not applicable",
            "  EVIDENCE  facets: 2 of 4 compared with rows holding values by authored gold SQL, 1 more detected by an "
            "authored mutant kill; no authored evidence: part+m_firm_open (no_gold_reads)",
            "  EVIDENCE  zero rows: 1 authored check(s) compared no row (v2:q02_where_used:top), 1 more in strata "
            "named empty; gold queries that compared no row: none; gold SQL with generated text: none"])
        authored = [c[0] for c in VERIFY_CHECKS if c[0].startswith(("v1:", "v2:"))]
        self.assertEqual(js["evidence"], {
            "schema_version": 1, "rule_version": evidence.RULE_VERSION,
            "included_stages": list(ORDER), "excluded_stages": [], "observation_recorded": True, "mode": "disclose",
            "kinds": {
                "authored": {"pass": 5, "fail": 0, "error": 0, "not_run": 0, "zero_row": 2,
                             "ids": {"verify": authored}},
                "recomputed": {"pass": 1, "fail": 0, "error": 0, "not_run": 0,
                               "ids": {"verify": ["v3:closure_oracle:has_component"]}},
                "generated": {"pass": 3, "fail": 0, "error": 0, "not_run": 0,
                              "ids": {"build": ["closure_apply:has_component"], "reason": ["v5:closure:has_component"],
                                      "verify": ["v3:total:m_on_hand.value"]}},
                "structural": {"pass": 7, "fail": 0, "error": 0, "not_run": 0,
                               "ids": {"doctor": ["python"], "demo": ["demo:load"],
                                       "build": ["shape:qty_positive", "closure:has_component"],
                                       "reason": ["shacl:meta:generated"], "cube": ["cube:model"],
                                       "verify": ["v4:suite"]}},
                "mutation": {"pass": 2, "fail": 0, "error": 0, "not_run": 0, "ids": {"verify": [QTY, AGG]}}},
            "overridden": 0,
            "mutation": split(killed_authored=1, killed_generated_only=1, generated_only=[AGG]),
            "mutation_reason": "",
            "facets": {"total": 4, "compared": 2, "detected": 1, "accepted": [],
                       "gaps": [{"facet": "part+m_firm_open", "reason": "no_gold_reads"}]},
            "facets_reason": "",
            "zero_row_checks": ["v2:q02_where_used:top"],
            "empty_stratum_zero_row_checks": ["v2:q01_explosion:empty"],
            "gold_no_rows": [], "gold_generated_text": []})

    def test_quiet_prints_no_evidence_line(self):
        write_pipeline(self.tmp, recorded())
        _js, md, lines, console = self.outputs("--quiet")
        self.assertEqual(console, [])
        self.assertIn("## Evidence", md)

    def test_unclassified_row_only_above_zero(self):
        write_pipeline(self.tmp, recorded())
        js, _md, lines, console = self.outputs()
        self.assertNotIn("unclassified", js["evidence"]["kinds"])
        self.assertFalse(any(line.startswith("| unclassified") for line in lines))
        self.assertNotIn("unclassified", console[0])
        checks = VERIFY_CHECKS + [("w9:x", "fail", "", {})]
        write_pipeline(self.tmp, recorded(), verify_checks=checks)
        js, _md, lines, console = self.outputs()
        self.assertEqual(js["evidence"]["kinds"]["unclassified"],
                         {"pass": 0, "fail": 1, "error": 0, "not_run": 0, "ids": {"verify": ["w9:x"]}})
        self.assertIn("| unclassified | no evidence rule names these checks; never counted as authored or recomputed "
                      "| 0 | 1 | 0 | 0 |", lines)
        self.assertTrue(console[0].endswith(", mutation 2/2, unclassified 0/1"))

    def test_lists_and_details(self):
        """Gaps, accepted facets, operators not selected, zero-row golds and generated gold SQL; lists cut at 10."""
        gaps = [facet_row("measures.on_hand", f"f{i}", "gap", "filter_only") for i in range(12)]
        changes = {
            "gold": [gold_entry("q01_explosion"), gold_entry("q02_where_used", "generated", ["output_schema"])],
            "facets": gaps + [facet_row("measures.on_hand", "acc1", "accepted")],
            "zero_row_checks": ["v1:q01_explosion"], "empty_stratum_zero_row_checks": [],
            "gold_no_rows": ["q01_explosion"],
            "mutation": dict(split(survived=1, equivalent=1),
                             operators_selected=7, operators_not_selected=["closure_end_swap", "closure_join_swap"])}
        # the V4 checks the split above follows from: one survivor (an equivalent mutant has no check)
        checks = [c for c in VERIFY_CHECKS if not c[0].startswith("v4:")] + [
            ("v4:suite", "pass", "", {}),
            (QTY, "fail", "hierarchies.has_component", {"killed_by": "", "detection": "none"})]
        write_pipeline(self.tmp, recorded(**changes), verify_checks=checks)
        js, _md, lines, console = self.outputs()
        ev = js["evidence"]
        self.assertEqual(ev["kinds"]["authored"]["ids"], {"verify": ["v1:q01_explosion", "v2:q01_explosion:top",
                                                                     "v2:q01_explosion:empty"]})
        self.assertEqual(ev["kinds"]["authored"]["zero_row"], 1)
        self.assertEqual(ev["kinds"]["generated"]["ids"]["verify"],
                         ["v1:q02_where_used", "v2:q02_where_used:top", "v3:total:m_on_hand.value"])
        self.assertEqual((ev["overridden"], ev["gold_generated_text"], ev["gold_no_rows"]),
                         (0, ["q02_where_used"], ["q01_explosion"]))
        self.assertEqual(ev["facets"]["accepted"], ["acc1"])
        self.assertEqual(len(ev["facets"]["gaps"]), 12)
        cut = ", ".join(f"f{i} (filter_only)" for i in range(10)) + ", and 2 more"
        self.assertIn(f"- Facets: 0 of 13 compared with rows holding values by authored gold SQL; 0 more detected "
                      f"by an authored mutant kill; no authored evidence: {cut}; accepted: acc1.", lines)
        self.assertIn("- Mutants (7 of 9 operators selected; not selected: closure_end_swap, closure_join_swap): 0 "
                      "killed with an authored detection; 0 killed by generated checks only; 0 unattributed; 0 "
                      "accepted; 1 survived; 0 not judged; 1 equivalent on this data; 0 not applicable.", lines)
        self.assertIn("- Authored checks that compared no row: 1 (v1:q01_explosion); in strata named `empty`: 0. Gold "
                      "queries that compared no row: q01_explosion.", lines)
        self.assertIn("- Gold SQL with generated text: q02_where_used.", lines)
        self.assertEqual(console[1:], [
            "  EVIDENCE  mutants (7 of 9 operators selected; not selected: closure_end_swap, closure_join_swap): 0 "
            "killed with authored detection, 0 killed by generated checks only, 0 unattributed, 0 accepted, 1 "
            "survived, 0 not judged, 1 equivalent, 0 not applicable",
            f"  EVIDENCE  facets: 0 of 13 compared with rows holding values by authored gold SQL, 0 more detected by "
            f"an authored mutant kill; no authored evidence: {cut}; accepted: acc1",
            "  EVIDENCE  zero rows: 1 authored check(s) compared no row (v1:q01_explosion); gold queries that compared "
            "no row: q01_explosion; gold SQL with generated text: q02_where_used"])
        self.assertTrue(console[0].startswith("  EVIDENCE  checks by kind (pass of all): authored 3/3 (1+0 compared "
                                              "no row), recomputed 1/1, generated 5/5,"))

    def test_no_operator_selected(self):
        """verify.mutation_operators = []: the mutant line says 0 of 9 and names every operator."""
        mutation = dict(split(), operators_selected=0, operators_not_selected=list(evidence.MUTATION_OPERATORS))
        checks = [c for c in VERIFY_CHECKS if not c[0].startswith("v4:")]
        write_pipeline(self.tmp, recorded(mutation=mutation), verify_checks=checks)
        _js, _md, lines, console = self.outputs()
        self.assertIn(f"- Mutants (0 of 9 operators selected; not selected: {ALL_OPERATORS}): 0 killed with an "
                      "authored detection; 0 killed by generated checks only; 0 unattributed; 0 accepted; 0 survived; "
                      "0 not judged; 0 equivalent on this data; 0 not applicable.", lines)
        self.assertTrue(console[1].startswith(f"  EVIDENCE  mutants (0 of 9 operators selected; not selected: "
                                              f"{ALL_OPERATORS}): 0 killed"))

    def test_recorded_kinds_only_lower(self):
        """A gold recorded authored with generated text, or with another kind, counts as generated and as overridden."""
        gold = [gold_entry("q01_explosion", "authored", ["relation:has_component_closure"]),
                gold_entry("q02_where_used", "recomputed")]
        # q01 counts as generated, so the kill by v1:q01_explosion is generated_only (a check that still says
        # "authored" makes the whole observation unrecorded: SplitFollowsChecksTest)
        checks = [c if c[0] != QTY else (QTY, "pass", c[2], {"killed_by": "v1:q01_explosion",
                                                              "detection": "generated_only"}) for c in VERIFY_CHECKS]
        mutation = split(killed_generated_only=2, generated_only=[QTY, AGG])
        write_pipeline(self.tmp, recorded(gold=gold, mutation=mutation), verify_checks=checks)
        js, _md, lines, _console = self.outputs()
        kinds = js["evidence"]["kinds"]
        self.assertEqual((kinds["authored"]["pass"], kinds["authored"]["zero_row"], kinds["recomputed"]["pass"]),
                         (0, 0, 1))
        self.assertEqual(kinds["generated"]["pass"], 8)
        self.assertEqual(js["evidence"]["overridden"], 2)
        self.assertEqual(js["evidence"]["gold_generated_text"], ["q01_explosion", "q02_where_used"])
        self.assertIn(f"| authored | {SIDES['authored'].format(z=0)} | 0 | 0 | 0 | 0 |", lines)


class ExcludedAndUnrecordedTest(ReportOutputs):
    def test_stale_verify_is_not_included(self):
        write_pipeline(self.tmp, recorded())
        write_stage(self.tmp, "verify", VERIFY_CHECKS, 7, recorded())     # started before cube finished
        js, _md, lines, console = self.outputs()
        ev = js["evidence"]
        self.assertEqual((ev["included_stages"], ev["excluded_stages"]),
                         (list(ORDER[:-1]), [{"stage": "verify", "why": "stale"}]))
        self.assertTrue(ev["observation_recorded"])
        self.assertEqual((ev["mutation"], ev["mutation_reason"]), ({"not_judged": 2}, "not_included"))
        self.assertEqual((ev["facets"], ev["facets_reason"]), (None, "not_included"))
        self.assertEqual(ev["kinds"]["authored"], {"pass": 0, "fail": 0, "error": 0, "not_run": 0, "zero_row": 0,
                                                   "ids": {}})
        self.assertIn("- Stages not included: verify (stale).", lines)
        self.assertIn("- Mutants: not judged (verify is not included: stale).", lines)
        self.assertIn("- Facets: not judged (verify is not included: stale).", lines)
        self.assertIn("  EVIDENCE  not included: verify (stale)", console)
        self.assertIn("  EVIDENCE  mutants: not judged (verify is not included: stale)", console)
        self.assertIn("  EVIDENCE  facets: not judged (verify is not included: stale)", console)
        self.assertNotIn("observation not recorded", "\n".join(console))

    def test_missing_and_optional_stages(self):
        write_pipeline(self.tmp, recorded(), stages=("build", "cube", "verify"))
        js, _md, lines, console = self.outputs()
        self.assertEqual(js["evidence"]["excluded_stages"], [{"stage": "reason", "why": "missing"}])
        self.assertEqual(js["evidence"]["included_stages"], ["build", "cube", "verify"])
        self.assertIn("- Stages not included: reason (missing).", lines)
        self.assertEqual(console[-1], "  EVIDENCE  not included: reason (missing)")
        write_stage(self.tmp, "doctor", [("python", "not_run", "", {})], 0)   # optional, no decisive result
        js, _md, lines, console = self.outputs()
        self.assertEqual(js["evidence"]["excluded_stages"], [{"stage": "doctor", "why": "not_counted"},
                                                             {"stage": "reason", "why": "missing"}])
        self.assertEqual(console[-1], "  EVIDENCE  not included: doctor (not_counted), reason (missing)")

    def test_verify_without_evidence(self):
        write_pipeline(self.tmp)
        js, _md, lines, console = self.outputs()
        ev = js["evidence"]
        self.assertEqual((ev["observation_recorded"], ev["mode"]), (False, None))
        self.assertEqual((ev["mutation"], ev["mutation_reason"]), ({"not_judged": 2}, "not_recorded"))
        self.assertEqual((ev["facets"], ev["facets_reason"]), (None, "not_recorded"))
        v12 = [c[0] for c in VERIFY_CHECKS if c[0].startswith(("v1:", "v2:"))]
        self.assertEqual(ev["kinds"]["unclassified"], {"pass": 5, "fail": 0, "error": 0, "not_run": 0,
                                                       "ids": {"verify": v12}})
        self.assertEqual((ev["kinds"]["authored"]["pass"], ev["kinds"]["authored"]["ids"]), (0, {}))
        self.assertIn("- Mutants: " + NO_SECTION + ".", lines)
        self.assertIn("- Facets: " + NO_SECTION + ".", lines)
        self.assertIn("  EVIDENCE  mutants: " + NO_SECTION, console)
        self.assertIn("  EVIDENCE  facets: " + NO_SECTION, console)
        self.assertEqual(console[-1], "  EVIDENCE  observation not recorded: the verify result has no readable "
                                      "evidence section")
        self.assertTrue(console[0].endswith(", unclassified 5/5"))

    def test_malformed_evidence_is_absent(self):
        bad = [{"schema_version": 1, "gold": "x"}, recorded(schema_version=2), recorded(rule_version=99),
               recorded(mode="other"), recorded(scope="full", facets=None),
               recorded(mutation={"operators_selected": 9}), recorded(zero_row_checks=[1]),
               recorded(gold_no_rows="q01_explosion"), "evidence", recorded(scope="narrowed")]
        for value in bad:
            with self.subTest(value=str(value)[:80]):
                write_pipeline(self.tmp, value)
                js, _md, _lines, console = self.outputs()
                self.assertFalse(js["evidence"]["observation_recorded"])
                self.assertEqual(js["evidence"]["facets_reason"], "not_recorded")
                self.assertIn("observation not recorded", console[-1])

    def test_narrowed_verify(self):
        checks = [c for c in VERIFY_CHECKS if not c[0].startswith("v4:")] + [("scope:v4", "not_run", "",
                                                                             {"reason": "excluded_by_only"})]
        write_pipeline(self.tmp, recorded(scope="narrowed", facets=None, facets_reason="narrowed",
                                          mutation=split()), verify_checks=checks)
        js, md, lines, console = self.outputs()
        ev = js["evidence"]
        self.assertEqual((ev["facets"], ev["facets_reason"]), (None, "narrowed"))
        self.assertEqual((ev["mutation"], ev["mutation_reason"]), ({"not_judged": 0}, "v4_not_run"))
        self.assertIn("- Facets: not judged (narrowed verify run).", lines)
        self.assertIn("- Mutants: not judged (V4 was left out of this narrowed verify run).", lines)
        self.assertIn("  EVIDENCE  facets: not judged (narrowed verify run)", console)
        self.assertIn("  EVIDENCE  mutants: not judged (V4 was left out of this narrowed verify run)", console)
        for text in (md, "\n".join(console)):
            self.assertNotIn("0 of 0", text)
            self.assertNotIn("0 of 9", text)
        # a run narrowed by --gold still ran V4: its split is shown, the facets are not judged
        write_pipeline(self.tmp, recorded(scope="narrowed", facets=None, facets_reason="narrowed"))
        js, _md, lines, _console = self.outputs()
        self.assertEqual(js["evidence"]["mutation_reason"], "")
        self.assertTrue(any(line.startswith("- Mutants (9 of 9 operators selected): 1 killed") for line in lines))
        self.assertIn("- Facets: not judged (narrowed verify run).", lines)


class SummariseTest(unittest.TestCase):
    def test_summarise_never_counts_an_unknown_id_as_authored_or_recomputed(self):
        """The summarise half of 8.1 no_default_row: no rule -> unclassified, whatever the gold kinds say."""
        checks = [{"id": "w9:x", "status": "pass"}, {"id": "v1:g", "status": "pass"}, {"id": "v1:q", "status": "pass"},
                  {"id": "v3w:x", "status": "pass"}, {"id": 7, "status": "pass"}, {"id": "v4:op", "status": "odd"}]
        obs = recorded(gold=[gold_entry("q")], zero_row_checks=["w9:x", "v1:g"], empty_stratum_zero_row_checks=[],
                       mutation=split())
        ev = evidence.summarise({"verify": checks, "counterexample": [{"id": "stage", "status": "pass"}]}, [], checks,
                                obs)
        kinds = ev["kinds"]
        self.assertEqual(kinds["unclassified"]["ids"], {"verify": ["w9:x", "v1:g", "7", "v4:op"],
                                                        "counterexample": ["stage"]})
        self.assertEqual((kinds["unclassified"]["pass"], kinds["unclassified"]["error"]), (4, 1))
        self.assertEqual(kinds["authored"]["ids"], {"verify": ["v1:q"]})
        self.assertEqual(kinds["authored"]["zero_row"], 0)
        self.assertEqual((kinds["recomputed"]["pass"], kinds["recomputed"]["ids"]), (0, {}))
        self.assertEqual(kinds["generated"]["ids"], {"verify": ["v3w:x"]})


FORGED = "x\n  EVIDENCE  facets: 17 of 17 compared"


def checks_of(rows) -> list:
    """Check dictionaries as a verify result.json holds them."""
    return [{"id": cid, "status": status, "config_ref": ref, "detail": detail} for cid, status, ref, detail in rows]


class ObservationValuesTest(unittest.TestCase):
    """report copies ids, reason codes and operator names from verify's evidence object into report.json, report.md and
    its console. A value outside its closed set, or an id with a character other than [A-Za-z0-9_.:*+[]-] (a space,
    a line break), makes the whole observation unrecorded, so nothing of it is printed."""
    CHECKS = checks_of(VERIFY_CHECKS)

    def ev(self, rec) -> dict:
        return evidence.summarise({"verify": self.CHECKS}, [], self.CHECKS, rec)

    def test_values_outside_the_closed_sets(self):
        self.assertTrue(self.ev(recorded())["observation_recorded"])
        on_hand = "measures.on_hand"
        bad = {
            "unknown gap reason": {"facets": [facet_row(on_hand, "m_on_hand.value", "gap", "rows: SECRET-ROW")]},
            "reason of a facet that is no gap": {"facets": [facet_row(on_hand, "m_on_hand.value", "compared",
                                                                      "no_values")]},
            "line break in a reason": {"facets": [facet_row(on_hand, "m_on_hand.value", "gap", FORGED)]},
            "line break in a facet id": {"facets": [facet_row(on_hand, FORGED, "gap", "no_gold_reads")]},
            "row value as a facet id": {"facets": [facet_row(on_hand, "P-123456 customer", "compared")]},
            "line break in a gold id": {"gold": [gold_entry("q01_explosion"), gold_entry("q\n  EVIDENCE  forged")]},
            "line break in an id list": {"gold_no_rows": ["q\n  NOT PROVEN  forged"]},
            "row value in an id list": {"zero_row_checks": ["P-123456 customer"]},
            "unknown operator": {"mutation": dict(recorded()["mutation"], operators_selected=8,
                                                  operators_not_selected=["SECRET-ROW"])},
            "operator named twice": {"mutation": dict(recorded()["mutation"], operators_selected=7,
                                                      operators_not_selected=["closure_end_swap"] * 2)},
            "operators that do not add up": {"mutation": dict(recorded()["mutation"],
                                                              operators_not_selected=["closure_end_swap"])},
            "line break in generated_only": {"mutation": split(killed_authored=1, killed_generated_only=1,
                                                               generated_only=[AGG + "\n  EVIDENCE  forged"])},
        }
        for name, change in bad.items():
            with self.subTest(name):
                ev = self.ev(recorded(**change))
                self.assertFalse(ev["observation_recorded"])
                self.assertEqual((ev["mutation_reason"], ev["facets_reason"]), ("not_recorded", "not_recorded"))
                text = evidence.console_lines(ev) + evidence.render_md(ev)
                self.assertFalse(any("\n" in line or "\r" in line for line in text), text)
                for secret in ("SECRET", "P-123456", "forged", "17 of 17"):
                    self.assertNotIn(secret, json.dumps(ev) + "\n".join(text), secret)

    def test_report_console_has_no_forged_line(self):
        """End to end: a verify result whose facet id carries a line break prints no extra console line."""
        tmp = Path(tempfile.mkdtemp(prefix="sxc_t_report_values_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        write_pipeline(tmp, recorded(facets=[facet_row("measures.on_hand", FORGED, "gap", "no_gold_reads")]))
        _code, console = run_report(tmp)
        self.assertNotIn("17 of 17", console)
        self.assertTrue(all(line.startswith("  EVIDENCE  ") for line in evidence_lines(console)))
        self.assertIn("observation not recorded", evidence_lines(console)[-1])


class SplitFollowsChecksTest(unittest.TestCase):
    """report shows verify's mutant split only when it follows from the V4 checks of the same result: an authored
    detection whose killer is no authored gold item (v4_violations), or buckets that do not add up to the V4 mutant
    checks, make the observation unrecorded, so report.md never prints an authored detection the checks do not show."""

    @staticmethod
    def ev(rows, mutation, **changes) -> dict:
        checks = checks_of(rows)
        return evidence.summarise({"verify": checks}, [], checks, recorded(mutation=mutation, **changes))

    def test_consistent_splits_are_recorded(self):
        base = [c for c in VERIFY_CHECKS if not c[0].startswith("v4:")] + [("v4:suite", "pass", "", {})]
        firm = "v4:measure_agg_to_max:firm_open"
        rows = base + [
            (QTY, "pass", "", {"killed_by": "v1:q01_explosion", "detection": "authored"}),
            (AGG, "fail", "", {"killed_by": "v3:total:m_on_hand.value", "detection": "generated_only",
                               "reason": "generated_only"}),
            (firm, "pass", "", {"killed_by": "v3:total:m_firm_open.value", "detection": "generated_only"}),
            ("v4:measure_agg_to_max:consigned", "pass", "", {"killed_by": "w9:x", "detection": "unattributed"}),
            ("v4:measure_filter_drop:on_hand", "fail", "", {"killed_by": "", "detection": "none"}),
            ("v4:measure_filter_drop:firm_open", "not_run", "", {"reason": "suite_incomplete"}),
            ("v4:dated_shift_one_day:firm_open_dated", "error", "", {"reason": "suite_error"})]
        mutation = split(killed_authored=1, killed_generated_only=1, accepted=1, unattributed=1, survived=1,
                         not_judged=3, equivalent=2, generated_only=[AGG])
        ev = self.ev(rows, mutation)
        self.assertTrue(ev["observation_recorded"])
        self.assertEqual(ev["mutation"], mutation)

    def test_splits_that_do_not_follow_are_not_recorded(self):
        rows = [c for c in VERIFY_CHECKS if not c[0].startswith("v4:")]
        v3_kill = (AGG, "pass", "", {"killed_by": "v3:total:m_on_hand.value", "detection": "authored"})
        authored = (QTY, "pass", "", {"killed_by": "v1:q01_explosion", "detection": "authored"})
        generated = (AGG, "pass", "", {"killed_by": "v3:total:m_on_hand.value", "detection": "generated_only"})
        survivor = (AGG, "fail", "", {"killed_by": "", "detection": "none"})
        cases = {
            "a v3 kill recorded as an authored detection": ([v3_kill], split(killed_authored=1)),
            "authored kills without a V4 check": ([], split(killed_authored=10 ** 12)),
            "more authored kills than checks": ([authored], split(killed_authored=2)),
            "a survivor counted as a kill": ([survivor], split(killed_authored=1)),
            "a kill counted as a survivor": ([authored], split(survived=1)),
            "generated_only naming an authored kill": ([authored, generated],
                                                       split(killed_authored=1, killed_generated_only=1,
                                                             generated_only=[QTY])),
            "generated_only naming one kill twice": ([authored, generated],
                                                     split(killed_authored=1, killed_generated_only=2,
                                                           generated_only=[AGG, AGG])),
            "fewer not judged than unjudged checks": ([(AGG, "error", "", {"reason": "suite_error"})], split()),
            "one V4 check twice": ([authored, authored], split(killed_authored=2)),
        }
        for name, (v4, mutation) in cases.items():
            with self.subTest(name):
                ev = self.ev(rows + v4, mutation)
                self.assertFalse(ev["observation_recorded"])
                self.assertEqual(ev["mutation_reason"], "not_recorded")
                md = "\n".join(evidence.render_md(ev))
                self.assertNotIn("with an authored detection", md)
                self.assertIn("- Mutants: not judged (the verify result has no evidence section).", md)


class RealSplitIsRecordedTest(MutantRun):
    """Every split verify writes follows from its own V4 checks: report records it, in both modes, with kills of every
    kind, an accepted kill and survivors."""

    def test_real_splits_are_recorded(self):
        answers = {"v1:q03_stock": lambda c: "fail" if c.endswith("firm_open") else "pass",
                   "v3:total:x": lambda c: "pass" if c.endswith(("consigned", "on_hand")) else "fail",
                   "w9:x": lambda c: "fail" if c.endswith("consigned") else "pass"}
        for mode in ("disclose", "require"):
            with self.subTest(mode=mode):
                shutil.rmtree(self.out, ignore_errors=True)
                self.cfg.verify.gold_observation = mode
                self.cfg.verify.accepted_gaps = ["v4:measure_agg_to_max:firm_open_dated"]
                v = self.attribute(answers)
                checks = json.loads(json.dumps(v.res.to_dict()))["checks"]
                ev = evidence.summarise({"verify": checks}, [], checks, v.res.to_dict()["evidence"])
                self.assertTrue(ev["observation_recorded"], v.res.to_dict()["evidence"]["mutation"])
                m = ev["mutation"]
                self.assertEqual([m[b] for b in ("killed_authored", "unattributed", "accepted", "survived")],
                                 [1, 1, 1, 1], m)


class ReportPrivacyTest(VerifyRun):
    def test_privacy(self):
        """Row values, parameter values and gold text never reach report.json, report.md or the console, not even
        when a hand-edited verify result carries them in keys report does not read."""
        self.gold_file("q03_stock", f"-- {SECRET_TEXT}\nSELECT '{SECRET_TEXT}', '{SECRET_RULE_VIEW}' FROM "
                                    "sxc_demo.stock t WHERE upper(btrim(t.\"location_id\")) IN ('MAIN', 'SUB')")
        self.bindings["q01_explosion"] = [(s, {"root": f"{SECRET_PARAM}-{s}"}) for s in
                                          ("top", "mid", "deep", "leaf_parent", "empty")]
        self.rows["q01_explosion"] = lambda p: [{"part_id": SECRET_ROW, "part_class": SECRET_ROW, "min_depth": 1,
                                                 "max_depth": 1, "path_count": 1, "qty": 2}]
        self.rows["q02_where_used"] = []
        res = self.verify()
        data = res.to_dict()
        data["evidence"]["rows"] = [SECRET_ROW, "P-000001"]
        data["evidence"]["gold"][0]["sql"] = SECRET_TEXT
        data["evidence"]["gold"][0]["params"] = {"root": SECRET_PARAM}
        if data["evidence"]["facets"]:
            data["evidence"]["facets"][0]["value"] = SECRET_ROW
        data["evidence"]["mutation"]["sample"] = SECRET_ROW
        out = self.tmp / "report-out"
        inputs = inputs_fingerprint(self.cfg)
        for i, stage in enumerate(ORDER[:-1]):
            write_stage(out, stage, STAGE_CHECKS[stage], 2 * i, inputs=inputs)
        data["started_at"] = data["finished_at"] = at(10)
        data["inputs"] = inputs
        (out / "verify").mkdir(parents=True)
        (out / "verify" / "result.json").write_text(json.dumps(data), encoding="utf-8")
        _code, console = run_report(out, config_file=self.tmp / "crosscheck.toml")
        js = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(js["evidence"]["observation_recorded"])
        self.assertEqual(js["evidence"]["gold_generated_text"], ["q03_stock"])
        texts = {"report.json evidence": json.dumps(js["evidence"], ensure_ascii=False),
                 "report.md": (out / "report.md").read_text(encoding="utf-8"),
                 "console": "\n".join(evidence_lines(console)),
                 "report result": (out / "report" / "result.json").read_text(encoding="utf-8")}
        for name, text in texts.items():
            for secret in (SECRET_ROW, SECRET_PARAM, SECRET_TEXT, "location_id", "MAIN"):
                self.assertNotIn(secret, text, f"{name}: {secret}")
            self.assertIsNone(re.search(r"P-\d{6}", text), name)
            self.assertNotIn("customer_acme", text.lower(), name)
        self.assertNotIn("sxc_demo", texts["report.json evidence"] + texts["console"])
        self.assertIn("relation:v_rule_*", json.dumps(data["evidence"]["gold"]))
        # the same secrets in keys report reads: the observation is not recorded and none of them is printed
        for key, edit in (("facet reason", lambda e: e["facets"][0].update(status="gap", reason=SECRET_ROW)),
                          ("facet id", lambda e: e["facets"][0].update(facet=f"x\n{SECRET_ROW}")),
                          ("gold id", lambda e: e["gold"][0].update(id=f"{SECRET_PARAM} P-000001")),
                          ("id list", lambda e: e["gold_no_rows"].append(SECRET_ROW + " P-000001")),
                          ("operators", lambda e: e["mutation"]["operators_not_selected"].append(SECRET_TEXT))):
            with self.subTest(key=key):
                edited = json.loads(json.dumps(data))
                edit(edited["evidence"])
                (out / "verify" / "result.json").write_text(json.dumps(edited), encoding="utf-8")
                _code, console = run_report(out, config_file=self.tmp / "crosscheck.toml")
                js = json.loads((out / "report.json").read_text(encoding="utf-8"))
                self.assertFalse(js["evidence"]["observation_recorded"])
                for text in (json.dumps(js["evidence"], ensure_ascii=False), console,
                             (out / "report.md").read_text(encoding="utf-8")):
                    for secret in (SECRET_ROW, SECRET_PARAM, SECRET_TEXT):
                        self.assertNotIn(secret, text, secret)
                    self.assertIsNone(re.search(r"P-\d{6}", text))


if __name__ == "__main__":
    unittest.main()
