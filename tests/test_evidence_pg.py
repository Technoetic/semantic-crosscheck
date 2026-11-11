"""Integration tests of what verify records about the evidence behind its checks (PostgreSQL 17 and the Cube adapter).

Why: the evidence kinds, the attribution of V4 kills, the facets without authored evidence and the zero-row
disclosures were built and unit-tested without a database. These tests run the integration scenarios of the design
against the demo data and a served model:

  ScenarioTest  (database + Cube)  clean demo: a gold query that reads the generated closure table by its bare name,
                                   through a search_path that names the output schema, is generated evidence
                                   (relation:<name>, its checks counted generated); q03_stock with a copied generated
                                   filter clause is generated evidence (N2) and leaves its facets without authored
                                   evidence (generated_text, F2); q05_firm_window with only a parameter set without rows
                                   gets N3 and N4 (no_rows_compared, filter_only) while no status changes (F3); the
                                   opt-in require mode fails the generated-only kills and adds one gold_evidence check
                                   per element, and only those checks and rewritten kills leave pass
  DirtyF1Test   (database + Cube)  dirty demo without q03_stock, measure_normalization_drop only: a check between
                                   generated layers kills the on_hand mutant, so it passes with N5, and measures.on_hand
                                   gets N4 (F1)

Every test that runs V4 calls evidence.v4_violations. Every test asserts what must hold whatever the numbers are: the
mutant buckets add up to the mutants, every generated-only kill has an N5 line (require mode: a fail) and no authored
detection, every facet without authored evidence is named, the zero-row lists follow from the checks, report's counts
equal verify's lists, lines hold ids only. The values of the clean demo measured in the first CI integration run
(design plan 8.2: kinds, mutant split, facets, zero-row lists, N4 and N5 lines, the require-mode result) are pinned
on top of that, in assert_clean_demo and test_require_mode_on_the_clean_demo. Each test prints
its measured values (ids and counts only) between "==== SXC MEASURED <test> ====" and "==== END SXC MEASURED <test>
====" lines, before its assertions, so that they can be read from the CI log and pinned. test_docs_quickstart applies
the same checks to the clean and dirty demo it runs through the command line.

Database names start with sxc_t_evidence. Skipped unless SXC_TEST_ADMIN_DSN names a superuser connection and the Cube
adapter is installed (SXC_CUBE_HOME or the per-user default) with node on PATH.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture
import test_docs_catalogue as catalogue
import test_verify as verify_tests
import test_verify_evidence as unit
from semantic_crosscheck import config, cube_gen, evidence

ROW_ID = verify_tests.ROW_ID
DEMO_GOLDS = list(verify_tests.V2_STRATA)
READY = pgfixture.available() and verify_tests.CUBE_READY
SKIP = "needs SXC_TEST_ADMIN_DSN and the Cube adapter (SXC_CUBE_HOME) with node on PATH"
OUTCOME = {"pass": "killed", "fail": "survived", "not_run": "not_run", "error": "error"}
N5_MIDDLE = ", a check between generated layers; no replayed gold item detected this change to "
NOT_RUN_GAPS = ("gold_not_run", "not_in_model")
AGG_TO_MAX = ["v4:measure_agg_to_max:firm_open", "v4:measure_agg_to_max:on_hand", "v4:measure_agg_to_max:consigned"]
# The clean demo as measured in the first CI integration run (the "==== SXC MEASURED" blocks of
# test_docs_quickstart.QuickStartRunTest.test_readme_quick_start and ScenarioTest.test_require_mode_on_the_clean_demo).
CLEAN_KINDS = {"authored": {"pass": 17, "fail": 0, "error": 0, "not_run": 0, "zero_row": 2},
               "recomputed": {"pass": 1, "fail": 0, "error": 0, "not_run": 0},
               "generated": {"pass": 26, "fail": 0, "error": 0, "not_run": 0},
               "structural": {"pass": 40, "fail": 0, "error": 0, "not_run": 0},
               "mutation": {"pass": 12, "fail": 0, "error": 0, "not_run": 0}}
CLEAN_MUTATION = {"operators_selected": 9, "operators_not_selected": [], "killed_authored": 9,
                  "killed_generated_only": 3, "unattributed": 0, "accepted": 0, "survived": 0, "not_judged": 0,
                  "equivalent": 2, "not_applicable": 0, "generated_only": AGG_TO_MAX}
CLEAN_EQUIVALENT = ["v4:measure_normalization_drop:on_hand", "v4:measure_normalization_drop:consigned"]
CLEAN_FACETS = [("m_firm_open.value", "compared", ""), ("has_component_closure+m_firm_open", "compared", ""),
                ("part+m_firm_open", "gap", "no_gold_reads"), ("m_firm_open_dated.value", "compared", ""),
                ("m_firm_open_dated.ts", "detected", ""), ("m_on_hand.value", "compared", ""),
                ("part+m_on_hand", "compared", ""), ("m_consigned.value", "compared", ""),
                ("part+m_consigned", "compared", ""), ("has_component_closure.qty", "compared", ""),
                ("has_component_closure.path_count", "compared", ""),
                ("has_component_closure.min_depth", "compared", ""),
                ("has_component_closure.max_depth", "compared", ""), ("has_component_closure.pairs", "compared", ""),
                ("has_component_ancestor.*", "compared", ""), ("has_component_descendant.*", "compared", ""),
                ("part.part_class", "compared", "")]
CLEAN_N4 = ["measures.firm_open: no authored evidence for part+m_firm_open (no_gold_reads)" + unit.N4_TAIL]
REQUIRE_GOLD_EVIDENCE = {"gold_evidence:measures.firm_open": ("fail", "no_gold_reads"),
                         "gold_evidence:measures.firm_open_dated": ("pass", None),
                         "gold_evidence:measures.on_hand": ("pass", None),
                         "gold_evidence:measures.consigned": ("pass", None),
                         "gold_evidence:hierarchies.has_component": ("pass", None),
                         "gold_evidence:ontology.class_column": ("pass", None)}
BARE = "q06_closure_bare_name"
BARE_GOLD = f"""
[[gold]]
id = "{BARE}"
sql = "gold/{BARE}.sql"
key = "max_depth"
[gold.cube_query]
dimensions = ["has_component_closure.max_depth"]
measures = ["has_component_closure.pairs"]
[gold.map]
max_depth = "has_component_closure.max_depth"
pairs = "has_component_closure.pairs"
"""
BARE_SQL = ("-- pairs by longest path, read from the generated closure table by its bare name (no schema)\n"
            "SELECT max_depth, count(*) AS pairs FROM has_component_closure GROUP BY max_depth\n")
# q03_stock as a person might write it after copying the generated WHERE clause of measures.on_hand verbatim
COPIED_Q03 = """-- q03 stock by part for one part class, with the on-hand filter copied from the generated measure SQL.
SELECT p.part_id, p.part_class,
       coalesce(sum(t.qty) FILTER (WHERE upper(btrim(t."location_id")) IN ('MAIN', 'SUB')), 0) AS on_hand,
       coalesce(sum(t.qty) FILTER (WHERE upper(btrim(t.location_id)) = 'CONSIGN'), 0) AS consigned
FROM sxc_demo.part p
LEFT JOIN sxc_demo.stock t ON t.part_id = p.part_id
WHERE p.part_class = :part_class
GROUP BY p.part_id, p.part_class
"""


# ---- measured values (printed for pinning, never asserted here) ----------------------------------------
def print_measured(title: str, values: dict) -> None:
    """One marked block of measured values. Results hold ids and counts only; anything shaped like a demo row id is
    masked all the same."""
    text = ROW_ID.sub("<row value>", json.dumps(values, indent=1, ensure_ascii=True, default=str))
    print(f"\n==== SXC MEASURED {title} (ids and counts only) ====\n{text}\n==== END SXC MEASURED {title} ====",
          flush=True)


def v4_mutants(result: dict) -> dict:
    """V4 mutant checks (v4:<operator>:<target>) of a result dictionary, by id."""
    return {c.get("id"): c for c in result.get("checks") or []
            if isinstance(c.get("id"), str) and c["id"].startswith("v4:") and len(c["id"].split(":")) == 3}


def verify_measures(result: dict) -> dict:
    """What a verify result says about evidence, for the CI log: statuses, gold accounting, facets, zero-row lists,
    the mutant split, every V4 attribution, the gold_evidence checks and the not_proven lines."""
    ev = result.get("evidence") or {}
    statuses: dict = {}
    for c in result.get("checks") or []:
        statuses[c.get("status")] = statuses.get(c.get("status"), 0) + 1
    facets = ev.get("facets")
    return {
        "status": result.get("status"), "check_statuses": statuses, "mode": ev.get("mode"), "scope": ev.get("scope"),
        "gold": {g.get("id"): {k: g.get(k) for k in ("kind", "generated_text", "comparisons", "nonempty_comparisons",
                                                     "value_rows")} for g in ev.get("gold") or []},
        "facets": None if facets is None else {r.get("facet"): [r.get("status"), r.get("reason")] for r in facets},
        "zero_row_checks": ev.get("zero_row_checks"),
        "empty_stratum_zero_row_checks": ev.get("empty_stratum_zero_row_checks"),
        "gold_no_rows": ev.get("gold_no_rows"), "mutation": ev.get("mutation"),
        "v4": {cid: {"status": c.get("status"), **{k: (c.get("detail") or {}).get(k) for k in
                                                     ("detection", "killed_by", "facet", "gold_comparing", "reason")}}
               for cid, c in v4_mutants(result).items()},
        "gold_evidence": {c.get("id"): [c.get("status"), (c.get("detail") or {}).get("reason", "")]
                          for c in result.get("checks") or [] if str(c.get("id")).startswith("gold_evidence:")},
        "not_proven": result.get("not_proven"),
    }


def report_measures(report_ev: dict, console: list) -> dict:
    """What report.json `evidence` and the EVIDENCE console lines say, for the CI log."""
    kinds = {k: {s: v for s, v in entry.items() if s != "ids"} for k, entry in (report_ev.get("kinds") or {}).items()}
    return {"kinds": kinds, **{k: report_ev.get(k) for k in (
        "included_stages", "excluded_stages", "observation_recorded", "mode", "overridden", "mutation",
        "mutation_reason", "facets", "facets_reason", "zero_row_checks", "empty_stratum_zero_row_checks",
        "gold_no_rows", "gold_generated_text")}, "console": console}


# ---- relationships that hold whatever the measured numbers are -------------------------------------------
def zero_row_lists(result: dict) -> tuple[list, list]:
    """The zero-row V1/V2 checks by the rule of the design (V1 empty; V2 with every binding empty, or an empty
    stratum), those of a stratum named `empty` apart."""
    zero, empty = [], []
    for c in result["checks"]:
        parts, d = c["id"].split(":"), c.get("detail") or {}
        if parts[0] == "v1" and len(parts) == 2 and d.get("empty") is True:
            stratum = d.get("stratum", "")
        elif parts[0] == "v2" and len(parts) == 3 and (
                d.get("empty_stratum") or (d.get("bindings", 0) > 0 and d.get("empty_bindings") == d["bindings"])):
            stratum = parts[2]
        else:
            continue
        (empty if stratum == "empty" else zero).append(c["id"])
    return zero, empty


def elements(cfg) -> list[str]:
    return ([f"measures.{m}" for m in cfg.measures] + [f"hierarchies.{h}" for h in cfg.hierarchies]
            + ([evidence.CLASS_ELEMENT] if cfg.ontology.class_column else []))


def coverage_rows(out: Path) -> list:
    f = Path(out) / "verify" / "mutation-coverage.json"
    return json.loads(f.read_text(encoding="utf-8"))["mutants"] if f.is_file() else []


def assert_verify_evidence(tc: unittest.TestCase, result: dict, out: Path, cfg) -> None:
    """Everything that must hold for a verify result with an evidence object (any data, either mode)."""
    ev = result["evidence"]
    lines = result["not_proven"]
    new_outputs = json.dumps(ev) + json.dumps(lines)
    tc.assertIsNone(ROW_ID.search(new_outputs), "the evidence object and the lines hold ids and counts only")
    tc.assertNotIn("btrim", new_outputs, "no gold or generated SQL text")
    tc.assertNotIn("sxc_demo.", json.dumps(ev), "no gold or generated SQL text")
    tc.assertIsNotNone(evidence.observation(ev), "report can read the evidence object")
    read = evidence.summarise({"verify": result["checks"]}, [], result["checks"], ev)
    tc.assertTrue(read["observation_recorded"], "report reads the evidence object: its mutant split follows from the "
                                                "V4 checks of the same result")
    for c in result["checks"]:
        tc.assertTrue(catalogue.matches(c["id"]), f"a result holds a check id design.md does not list: {c['id']}")
    golds, overridden = evidence.gold_kinds(ev["gold"])
    tc.assertEqual(overridden, 0, "verify records the kind its own rule allows")
    checks = {c["id"]: c for c in result["checks"]}
    mode = ev["mode"]

    # gold queries and zero rows
    for g in ev["gold"]:
        tc.assertEqual(g["kind"], "generated" if g["generated_text"] else "authored", g["id"])
        tc.assertLessEqual(g["nonempty_comparisons"], g["comparisons"], g["id"])
        tc.assertEqual(sorted(g["value_rows"]), g["compares"], g["id"])
    tc.assertEqual(ev["gold_no_rows"],
                   [g["id"] for g in ev["gold"] if g["comparisons"] and not g["nonempty_comparisons"]])
    for g in ev["gold"]:
        n3 = [line for line in lines if line.startswith(f"gold:{g['id']}: every comparison that ran (")]
        if g["id"] in ev["gold_no_rows"]:
            tc.assertEqual(len(n3), 1, g["id"])
            tc.assertTrue(n3[0].startswith(f"gold:{g['id']}: every comparison that ran ({g['comparisons']}) had zero "
                                           "rows on both sides; "), n3[0])
            v1 = checks.get(f"v1:{g['id']}")
            if mode == "require" and v1 is not None:
                tc.assertNotEqual(v1["status"], "pass", "require mode never passes a gold query that compared no row")
        else:
            tc.assertEqual(n3, [], g["id"])
    tc.assertEqual((ev["zero_row_checks"], ev["empty_stratum_zero_row_checks"]), zero_row_lists(result))

    # V4: attribution, the mutant split and the coverage ledger
    tc.assertEqual(evidence.v4_violations(result), [])
    mutants = v4_mutants(result)
    ledger = coverage_rows(out)
    split = ev["mutation"]
    accepted = set(cfg.verify.accepted_gaps)
    tc.assertEqual(sum(split[b] for b in evidence.MUTANT_BUCKETS),
                   len(dict.fromkeys([row["id"] for row in ledger] + list(mutants))), "every mutant in one bucket")
    tc.assertEqual(split["operators_selected"] + len(split["operators_not_selected"]), len(evidence.MUTATION_OPERATORS))
    judged = {cid: c for cid, c in mutants.items() if c["status"] not in ("not_run", "error")}
    generated_only = [cid for cid, c in judged.items() if c["detail"].get("killed_by")
                      and c["detail"].get("detection") == "generated_only" and cid not in accepted]
    tc.assertEqual(sorted(split["generated_only"]), sorted(generated_only))
    tc.assertEqual(split["killed_generated_only"], len(generated_only))
    tc.assertEqual(split["killed_authored"], sum(c["detail"].get("detection") == "authored" for c in judged.values()))
    tc.assertEqual(split["not_judged"] - sum(1 for row in ledger if row["id"] not in mutants
                                             and row.get("outcome") not in ("equivalent", "not_applicable")),
                   len(mutants) - len(judged))
    no_check = [row for row in ledger if row["id"] not in mutants]
    tc.assertEqual(split["equivalent"], sum(row.get("outcome") == "equivalent" for row in no_check))
    tc.assertEqual(split["not_applicable"], sum(row.get("outcome") == "not_applicable" for row in no_check))
    summary = next((c["detail"]["summary"] for c in mutants.values() if "summary" in c["detail"]), None)
    if summary is not None:
        tc.assertEqual((summary["killed"], summary["survived"]),
                       (sum(c["status"] == "pass" for c in mutants.values()),
                        sum(c["status"] == "fail" for c in mutants.values())), "summary tally")
    for row in ledger:
        c = mutants.get(row["id"])
        if c is not None:
            tc.assertEqual(row["outcome"], OUTCOME[c["status"]], row["id"])
            if "killed_by" in row:
                tc.assertEqual(row.get("detection"), c["detail"].get("detection"), row["id"])
        items = (row.get("coverage") or {}).get("items", [])
        ranks = [evidence.suite_rank(item["id"], golds) for item in items]
        tc.assertEqual(ranks, sorted(ranks), f"{row['id']}: authored gold items run first")
        for item in items:
            tc.assertEqual(item.get("kind"), evidence.item_kind(item["id"], golds), row["id"])
    n5 = [line for line in lines if N5_MIDDLE in line]
    for cid in generated_only:
        c, (_v4, op, target) = judged[cid], cid.split(":")
        head = f"{cid}: killed by {c['detail']['killed_by']}{N5_MIDDLE}{c['config_ref']} ("
        mine = [line for line in n5 if line.startswith(head)]
        if mode == "disclose":
            tc.assertEqual(c["status"], "pass", cid)
            tc.assertEqual(len(mine), 1, f"{cid}: one N5 line")
            tc.assertTrue(mine[0].endswith("); " + evidence.remedy(op, target)), mine[0])
        else:
            tc.assertEqual((c["status"], c["detail"].get("reason")), ("fail", "generated_only"), cid)
            tc.assertEqual(mine, [], cid)
        for row in ev["facets"] or []:
            tc.assertNotIn(cid, row["detected_by"], f"{cid} is no authored detection")
    tc.assertEqual(len(n5), len(generated_only) if mode == "disclose" else 0)
    if mode == "disclose":
        tc.assertEqual([c["id"] for c in result["checks"] if (c.get("detail") or {}).get("reason") in
                        ("generated_only", "unattributed", "no_rows_compared")], [], "disclose mode changes no status")

    # facets
    gold_evidence = [c for c in result["checks"] if c["id"].startswith("gold_evidence:")]
    if ev["scope"] != "full":
        tc.assertEqual((ev["facets"], ev["facets_reason"]), (None, "narrowed"))
        tc.assertEqual([line for line in lines if line.endswith(unit.N4_TAIL)], [])
        tc.assertEqual(gold_evidence, [])
        return
    model = cube_gen.load_model(out)
    facets = {f.id: f for f in evidence.facets(cfg, model)}
    rows = ev["facets"]
    tc.assertEqual([r["facet"] for r in rows], list(facets))
    entries = {g["id"]: g for g in ev["gold"]}
    gaps: dict[str, list] = {}
    for r in rows:
        f = facets[r["facet"]]
        tc.assertEqual(r["element"], f.element)
        tc.assertIn(r["status"], evidence.FACET_STATUSES, r["facet"])
        if r["status"] == "compared":
            tc.assertTrue(any(golds.get(gid) == "authored" and entries[gid]["nonempty_comparisons"] > 0
                              and any(entries[gid]["value_rows"].get(m, 0) > 0
                                      for m in f.value_members(set(entries[gid]["compares"])))
                              for gid in r["compared_by"]), f"{r['facet']}: compared needs rows holding values")
        elif r["status"] == "detected":
            tc.assertTrue(r["detected_by"], r["facet"])
            for cid in r["detected_by"]:
                _v4, op, target = cid.split(":")
                tc.assertEqual(mutants[cid]["detail"]["detection"], "authored", cid)
                tc.assertEqual(evidence.operator_facet(cfg, op, target), r["facet"], cid)
        elif r["status"] == "gap":
            tc.assertIn(r["reason"], evidence.FACET_REASONS, r["facet"])
            gaps.setdefault(r["element"], []).append(r)
    n4 = [line for line in lines if line.endswith(unit.N4_TAIL)]
    if mode == "disclose":
        tc.assertEqual(gold_evidence, [])
        tc.assertEqual(len(n4), len(gaps), "one N4 line per element with a gap")
        for element, items in gaps.items():
            mine = [line for line in n4 if line.startswith(f"{element}: no authored evidence for ")]
            tc.assertEqual(len(mine), 1, element)
            for r in items[:evidence.MAX_LISTED]:
                tc.assertIn(f"{r['facet']} ({r['reason']}", mine[0], "every facet without authored evidence is named")
        return
    tc.assertEqual(n4, [], "require mode turns the facet gaps into gold_evidence checks")
    want = [f"gold_evidence:{e}" for e in elements(cfg)]
    tc.assertEqual([c["id"] for c in gold_evidence], want)
    tc.assertEqual([c["id"] for c in result["checks"][-len(want):]], want, "after every other check")
    for c in gold_evidence:
        mine = [r for r in rows if r["element"] == c["config_ref"]]
        open_gaps = gaps.get(c["config_ref"], [])
        other = [r for r in open_gaps if r["reason"] not in NOT_RUN_GAPS]
        if not mine:
            expected = ("not_run", "not_in_model")
        elif not open_gaps:
            expected = ("pass", None)
        elif other:
            expected = ("fail", other[0]["reason"])
        else:
            expected = ("not_run", open_gaps[0]["reason"])
        d = c["detail"]
        tc.assertEqual((c["status"], d.get("reason")), expected, c["id"])
        tc.assertEqual((d["facets"], d["gaps"], d["accepted"], d["with_evidence"], d["operators_not_selected"]),
                       (len(mine), len(open_gaps), sum(r["status"] == "accepted" for r in mine),
                        sum(r["status"] in ("compared", "detected") for r in mine), split["operators_not_selected"]),
                       c["id"])


def assert_report_evidence(tc: unittest.TestCase, report_ev: dict, results: dict, console: list, verify: dict) -> None:
    """report.json `evidence` repeats verify's lists and counts every check of an included stage once; the EVIDENCE
    console lines are the ones its contents give."""
    verify_ev = verify["evidence"]
    tc.assertIsNone(ROW_ID.search(json.dumps(report_ev) + json.dumps(console)), "ids and counts only")
    tc.assertTrue(report_ev["observation_recorded"])
    tc.assertEqual(report_ev["mode"], verify_ev["mode"])
    tc.assertIn("verify", report_ev["included_stages"])
    by_stage: dict[str, list] = {}
    for kind, entry in report_ev["kinds"].items():
        counted = entry["pass"] + entry["fail"] + entry["error"] + entry["not_run"]
        tc.assertEqual(counted, sum(len(ids) for ids in entry["ids"].values()), kind)
        for stage, ids in entry["ids"].items():
            by_stage.setdefault(stage, []).extend(ids)
    for stage in report_ev["included_stages"]:
        tc.assertEqual(sorted(by_stage.pop(stage, [])), sorted(c["id"] for c in results[stage]["checks"]),
                       f"{stage}: every check counted once")
    tc.assertEqual(by_stage, {}, "only included stages are counted")
    mutation = report_ev["kinds"].get("mutation", {})
    tc.assertEqual(sorted(mutation.get("ids", {}).get("verify", [])), sorted(v4_mutants(verify)))
    if report_ev["mutation_reason"] == "":
        tc.assertEqual(report_ev["mutation"], verify_ev["mutation"])
    rows = verify_ev["facets"]
    if rows is not None:
        tc.assertEqual(report_ev["facets"], {
            "total": len(rows), "compared": sum(r["status"] == "compared" for r in rows),
            "detected": sum(r["status"] == "detected" for r in rows),
            "accepted": [r["facet"] for r in rows if r["status"] == "accepted"],
            "gaps": [{"facet": r["facet"], "reason": r["reason"]} for r in rows if r["status"] == "gap"]})
    for key in ("zero_row_checks", "empty_stratum_zero_row_checks", "gold_no_rows"):
        tc.assertEqual(report_ev[key], verify_ev[key], key)
    authored = set(report_ev["kinds"].get("authored", {}).get("ids", {}).get("verify", []))
    zero = verify_ev["zero_row_checks"] + verify_ev["empty_stratum_zero_row_checks"]
    tc.assertEqual(report_ev["kinds"].get("authored", {}).get("zero_row", 0), sum(i in authored for i in zero))
    golds, _overridden = evidence.gold_kinds(verify_ev["gold"])
    tc.assertEqual(report_ev["gold_generated_text"], [g for g, k in golds.items() if k == "generated"])
    tc.assertEqual(console, evidence.console_lines(report_ev), "the EVIDENCE lines say what report.json holds")


def assert_clean_demo(tc: unittest.TestCase, verify: dict, report_ev: dict | None = None) -> None:
    """What is known about the clean demo by reading the code and the demo (design 3.3, 4.2, 11): the five gold
    queries are authored, part+m_firm_open has no gold reader, the three per-key measure_agg_to_max mutants are killed
    by generated checks only, and the two zero-row strata; disclose mode changes no status. On top of that, the values
    measured in the first CI integration run (design plan 8.2): the mutant split, every facet's status, both zero-row
    lists, the N4 and N5 lines and, with report's evidence, the checks by kind."""
    ev = verify["evidence"]
    tc.assertEqual((ev["mode"], ev["scope"]), ("disclose", "full"))
    tc.assertEqual({g["id"]: (g["kind"], g["generated_text"]) for g in ev["gold"]},
                   {gid: ("authored", []) for gid in DEMO_GOLDS})
    gaps = {r["facet"]: r["reason"] for r in ev["facets"] if r["status"] == "gap"}
    tc.assertEqual(gaps.get("part+m_firm_open"), "no_gold_reads")
    tc.assertLessEqual(set(AGG_TO_MAX), set(ev["mutation"]["generated_only"]))
    tc.assertIn("v2:q01_explosion:empty", ev["empty_stratum_zero_row_checks"])
    tc.assertIn("v2:q02_where_used:top", ev["zero_row_checks"])
    tc.assertEqual(ev["gold_no_rows"], [], "every demo gold query compares rows in some binding")
    tc.assertEqual([c["id"] for c in verify["checks"] if c["status"] != "pass"], [])
    # measured
    tc.assertEqual(ev["mutation"], CLEAN_MUTATION)
    tc.assertEqual([(r["facet"], r["status"], r["reason"]) for r in ev["facets"]], CLEAN_FACETS)
    tc.assertEqual((ev["zero_row_checks"], ev["empty_stratum_zero_row_checks"]),
                   (["v2:q02_where_used:top"], ["v2:q01_explosion:empty"]))
    lines = verify["not_proven"]
    tc.assertEqual([line for line in lines if line.endswith(" equivalent on this data")],
                   [f"{cid} equivalent on this data" for cid in CLEAN_EQUIVALENT])
    tc.assertEqual([line.split(": killed by ", 1)[0] for line in lines if N5_MIDDLE in line], AGG_TO_MAX)
    tc.assertEqual([line for line in lines if line.endswith(unit.N4_TAIL)], CLEAN_N4)
    if report_ev is not None:
        tc.assertEqual((report_ev["mutation_reason"], report_ev["facets_reason"], report_ev["overridden"]), ("", "", 0))
        tc.assertNotIn("unclassified", report_ev["kinds"])
        tc.assertEqual(report_ev["gold_generated_text"], [])
        tc.assertEqual(report_ev["excluded_stages"], [], "the quick start runs every stage in order")
        # measured
        tc.assertEqual({k: {s: v for s, v in entry.items() if s != "ids"} for k, entry in report_ev["kinds"].items()},
                       CLEAN_KINDS)
        tc.assertEqual(report_ev["mutation"], CLEAN_MUTATION)
        tc.assertEqual(report_ev["facets"], {"total": 17, "compared": 15, "detected": 1, "accepted": [],
                                             "gaps": [{"facet": "part+m_firm_open", "reason": "no_gold_reads"}]})


# ---- scenarios -------------------------------------------------------------------------------------------
class DemoStages(unittest.TestCase):
    """A demo database with build's closure result and the generated model in `out`, as test_verify prepares them;
    each scenario copies them next to its own configuration."""

    NAME = VARIANT = ""

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, cls.VARIANT).setup()
        cls.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_evidence_"))
        cls.out = cls.tmp / "out"
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        try:
            cls.prepare_stages()
        except BaseException:
            cls.tearDownClass()
            raise

    prepare_stages = vars(verify_tests.VerifyStageTest)["prepare_stages"]
    tearDownClass = vars(verify_tests.VerifyStageTest)["tearDownClass"]
    verify = verify_tests.VerifyStageTest.verify

    def setUp(self):
        verify_tests.activate(self.fx)

    def scenario(self, name: str, toml_extra: str = "", gold_files: dict | None = None) -> tuple:
        """(configuration, output directory) of a copy of the demo configuration and its gold files, with toml_extra
        appended and gold_files ({gold id: SQL}) written, next to copies of build's and cube's results."""
        base = self.tmp / name
        shutil.copytree(pgfixture.DEMO_CONFIG.parent / "gold", base / "gold")
        text = pgfixture.DEMO_CONFIG.read_text(encoding="utf-8")
        (base / "crosscheck.toml").write_text(text + toml_extra, encoding="utf-8", newline="\n")
        for gid, sql in (gold_files or {}).items():
            (base / "gold" / f"{gid}.sql").write_text(sql, encoding="utf-8", newline="\n")
        out = base / "out"
        for stage in ("build", "cube"):
            shutil.copytree(self.out / stage, out / stage)
        return config.load(base / "crosscheck.toml"), out

    def run_verify(self, title: str, cfg, out: Path, only: list[str]) -> dict:
        """verify as a result dictionary, its measured values printed before any assertion, then the relationships
        every verify result must satisfy."""
        result = self.verify(out, only, cfg).to_dict()
        print_measured(title, verify_measures(result))
        assert_verify_evidence(self, result, out, cfg)
        return result


@unittest.skipUnless(READY, SKIP)
class ScenarioTest(DemoStages):
    NAME, VARIANT = "sxc_t_evidence_clean", "clean"

    def test_gold_that_reads_a_generated_relation_by_its_bare_name_is_generated(self):
        admin = pgfixture._admin()
        try:
            admin.run(f'ALTER ROLE "{self.fx.check}" IN DATABASE "{self.fx.name}" SET search_path = public, sxc_out')
        finally:
            admin.close()
        self.addCleanup(self.reset_search_path)
        cfg, out = self.scenario("bare_name", BARE_GOLD, {BARE: BARE_SQL})
        result = self.run_verify(self.id(), cfg, out, ["v1", "v2"])
        checks = {c["id"]: c for c in result["checks"]}
        gold = {g["id"]: g for g in result["evidence"]["gold"]}
        self.assertEqual((gold[BARE]["kind"], gold[BARE]["generated_text"]),
                         ("generated", ["relation:has_component_closure"]))
        self.assertEqual({gid: gold[gid]["kind"] for gid in DEMO_GOLDS}, dict.fromkeys(DEMO_GOLDS, "authored"))
        self.assertNotEqual(checks[f"v1:{BARE}"]["status"], "error",
                            "the check role's search_path names the output schema, so the bare name resolves")
        self.assertIn(f"gold:{BARE}: the gold SQL names the generated relation has_component_closure; v1:{BARE} and "
                      f"v2:{BARE}:* {unit.GENERATED_TAIL}", result["not_proven"])
        golds, _overridden = evidence.gold_kinds(result["evidence"]["gold"])
        for cid in (f"v1:{BARE}", f"v2:{BARE}:all"):
            self.assertIn(cid, checks)
            self.assertEqual(evidence.check_kind("verify", cid, golds), "generated", cid)
        self.assertEqual(evidence.check_kind("verify", "v1:q04_depth_histogram", golds), "authored")

    def reset_search_path(self):
        admin = pgfixture._admin()
        try:
            admin.run(f'ALTER ROLE "{self.fx.check}" IN DATABASE "{self.fx.name}" RESET search_path')
        finally:
            admin.close()

    def test_f2_gold_with_a_copied_generated_filter_is_generated_evidence(self):
        cfg, out = self.scenario("f2_copied_filter", gold_files={"q03_stock": COPIED_Q03})
        cfg.verify.mutation_operators = ["measure_agg_to_max"]
        result = self.run_verify(self.id(), cfg, out, ["v1", "v2", "v3", "v4"])
        ev = result["evidence"]
        q03 = next(g for g in ev["gold"] if g["id"] == "q03_stock")
        self.assertEqual((q03["kind"], q03["generated_text"]), ("generated", ["filter:measures.on_hand"]))
        self.assertIn("gold:q03_stock: the gold SQL contains the generated filter of measures.on_hand verbatim; "
                      f"v1:q03_stock and v2:q03_stock:* {unit.GENERATED_TAIL}", result["not_proven"])
        golds, _overridden = evidence.gold_kinds(ev["gold"])
        for cid in ("v1:q03_stock", "v2:q03_stock:values"):
            self.assertEqual(evidence.check_kind("verify", cid, golds), "generated", cid)
        rows = {r["facet"]: r for r in ev["facets"]}
        for facet in ("m_on_hand.value", "m_consigned.value"):
            self.assertEqual((rows[facet]["status"], rows[facet]["reason"], rows[facet]["compared_by"]),
                             ("gap", "generated_text", ["q03_stock"]), facet)
        for element, facet in (("measures.on_hand", "m_on_hand.value"), ("measures.consigned", "m_consigned.value")):
            line = next(n for n in result["not_proven"] if n.startswith(f"{element}: no authored evidence for "))
            self.assertIn(f"{facet} (generated_text: q03_stock)", line)
        for cid, c in v4_mutants(result).items():
            if cid.endswith((":on_hand", ":consigned")):
                self.assertNotEqual(c["detail"].get("detection"), "authored", "only a generated gold reads them")

    def test_f3_gold_whose_comparisons_have_no_row_is_disclosed_without_a_status_change(self):
        cfg, out = self.scenario("f3_no_rows")
        q05 = next(g for g in cfg.gold if g.id == "q05_firm_window")
        q05.param_sets = [ps for ps in q05.param_sets if ps["date_from"].startswith("2030-")]
        self.assertEqual(len(q05.param_sets), 1, "the demo's parameter set without orders")
        cfg.verify.mutation_operators = ["closure_qty_single_path"]
        result = self.run_verify(self.id(), cfg, out, ["v1", "v2", "v3", "v4"])
        ev = result["evidence"]
        q05_entry = next(g for g in ev["gold"] if g["id"] == "q05_firm_window")
        self.assertEqual((q05_entry["comparisons"], q05_entry["nonempty_comparisons"]), (2, 0), "v1 and one v2 binding")
        self.assertEqual(ev["gold_no_rows"], ["q05_firm_window"])
        self.assertIn("gold:q05_firm_window: every comparison that ran (2) had zero rows on both sides; "
                      "v1:q05_firm_window and v2:q05_firm_window:* pass without comparing any row; add a binding, "
                      "stratum or parameter set with rows", result["not_proven"])
        self.assertLessEqual({"v1:q05_firm_window", "v2:q05_firm_window:param_sets"}, set(ev["zero_row_checks"]))
        self.assertIn("measures.firm_open_dated: no authored evidence for m_firm_open_dated.value (no_rows_compared: "
                      "q05_firm_window), m_firm_open_dated.ts (filter_only: q05_firm_window)" + unit.N4_TAIL,
                      result["not_proven"])
        self.assertEqual([(c["id"], c["status"]) for c in result["checks"] if c["status"] != "pass"], [],
                         "disclosure only: every status stays as it was")
        self.assertEqual(result["status"], "pass")

    def test_require_mode_on_the_clean_demo(self):
        cfg, out = self.scenario("require")
        cfg.verify.gold_observation = "require"
        result = self.run_verify(self.id(), cfg, out, ["v1", "v2", "v3", "v4"])
        ev = result["evidence"]
        self.assertEqual((ev["mode"], ev["scope"]), ("require", "full"))
        mutants = v4_mutants(result)
        for cid in AGG_TO_MAX:
            self.assertEqual((mutants[cid]["status"], mutants[cid]["detail"].get("reason"),
                              mutants[cid]["detail"].get("detection")),
                             ("fail", "generated_only", "generated_only"), cid)
        gold_evidence = {c["id"]: c for c in result["checks"] if c["id"].startswith("gold_evidence:")}
        self.assertEqual(len(gold_evidence), 6, "4 measures, 1 hierarchy and ontology.class_column")
        self.assertEqual(gold_evidence["gold_evidence:measures.firm_open"]["status"], "fail",
                         "part+m_firm_open has no gold reader")
        rewritten = {cid for cid, c in mutants.items()
                     if c["detail"].get("reason") in ("generated_only", "unattributed")}
        rewritten |= {f"v1:{g}" for g in ev["gold_no_rows"]}
        self.assertEqual([c["id"] for c in result["checks"] if c["status"] != "pass"
                          and c["id"] not in rewritten and c["id"] not in gold_evidence], [],
                         "only rewritten kills, zero-row v1 checks and gold_evidence checks leave pass")
        self.assertEqual(result["status"], "fail")
        # measured
        self.assertEqual({cid: (c["status"], c["detail"].get("reason")) for cid, c in gold_evidence.items()},
                         REQUIRE_GOLD_EVIDENCE)
        self.assertEqual([c["id"] for c in result["checks"] if c["status"] != "pass"],
                         AGG_TO_MAX + ["gold_evidence:measures.firm_open"])
        self.assertEqual((sum(c["status"] == "pass" for c in result["checks"]), len(result["checks"])), (49, 53))
        self.assertEqual(ev["mutation"], CLEAN_MUTATION, "a rewritten fail stays in its detection bucket")
        self.assertEqual(result["not_proven"], [f"{cid} equivalent on this data" for cid in CLEAN_EQUIVALENT],
                         "require mode writes no N4 or N5 line")


@unittest.skipUnless(READY, SKIP)
class DirtyF1Test(DemoStages):
    NAME, VARIANT = "sxc_t_evidence_dirty", "dirty"

    def test_f1_a_kill_by_a_generated_check_only_is_not_a_gold_detection(self):
        cfg, out = self.scenario("f1")
        cfg.gold = [g for g in cfg.gold if g.id != "q03_stock"]
        cfg.verify.mutation_operators = ["measure_normalization_drop"]
        result = self.run_verify(self.id(), cfg, out, ["v1", "v2", "v3", "v4"])
        self.assertEqual(result["evidence"]["scope"], "full")
        cid = "v4:measure_normalization_drop:on_hand"
        c = v4_mutants(result)[cid]
        d = c["detail"]
        self.assertEqual((c["status"], d["detection"], d["facet"], d["gold_comparing"]),
                         ("pass", "generated_only", "m_on_hand.value", []), d)
        self.assertTrue(d["killed_by"].startswith("v3:"), d["killed_by"])
        self.assertIn(f"{cid}: killed by {d['killed_by']}{N5_MIDDLE}measures.on_hand (no authored gold query compares "
                      f"m_on_hand.value); " + evidence.remedy("measure_normalization_drop", "on_hand"),
                      result["not_proven"])
        self.assertIn("measures.on_hand: no authored evidence for m_on_hand.value (no_gold_reads), part+m_on_hand "
                      "(no_gold_reads)" + unit.N4_TAIL, result["not_proven"])
        self.assertIn("v4:measure_normalization_drop:consigned equivalent on this data", result["not_proven"])


if __name__ == "__main__":
    unittest.main()
