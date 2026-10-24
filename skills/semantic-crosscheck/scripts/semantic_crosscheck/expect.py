"""expect stage: literal expected rows ([[gold.expect]]) for the gold queries, on a dedicated fixture database.

Why: V1-V4 compare the gold SQL with Cube. When both share a mistake (the same dropped join, the same wrong quantity
column) they agree and V1 passes. Rows a person wrote from the business specification for a small fixture are a
third oracle that is neither SQL nor the configuration: the literal expected-row form of dbt and SQLMesh unit tests.
Hierarchy closures are recursive and cannot be mocked, so both sides really run on the fixture:

  1. Guards before anything connects. A configuration without cases is expect:cases not_run. Otherwise
     --allow-fixture-database is required (exit 2); the --dsn-env variable (default SXC_EXPECT_DSN) must be set
     (exit 3) and must not be, hold the value of, or name the host, port and database of source.dsn_env,
     source.owner_dsn_env or cube.dsn_env (exit 2; no value is printed).
  2. The fixture database is the user's: it already holds the source tables and the fixture rows. sxc executes no
     fixture SQL and never connects to an operational DSN. A copy of the configuration whose DSN variables all name
     the fixture variable runs the normal build and cube stages into <out>/expect/ (build writes its views and
     closure tables into the fixture database's output schema: what the consent flag allows), so the operational
     build/ and cube/ results are never touched. expect:build and expect:cube summarise them.
  3. One Cube process serves that model (log in expect/server.log). For every case the gold SQL (READ ONLY, the case's
     bound parameters) and the gold's cube_query each give rows that are compared with the expected rows
     (compare.compare_rows, typed by the model's member kinds): gold strictly, Cube with the declared zero_if_null
     equivalence, and gold with Cube as in V1. A Cube row without a requested member is an error, never a NULL. A
     partial case compares only the columns its rows name.
  4. expect:<gold>[<i>] passes when gold and Cube both equal the expected rows. Otherwise it fails: common_mode when
     they agree with each other but not with the rows of an authored case, regression when they agree but not with
     the rows of a captured case, gold_mismatch, cube_mismatch or both_mismatch when they disagree
     (gold_equals_expected and cube_equals_expected say which side equals the rows). A side that cannot run is an
     error. A case is not_run when its closure (build closure:<h>) or its Cube members (cube:gold_members:<gold>) did
     not pass or Cube did not serve the model (cube:meta).
  5. expect:fixture fingerprints the fixture's source and closure tables before and after the cases (row count and
     md5 computed in the database; only the hash leaves it). A change is an error and turns every passing case into
     not_run (fixture_changed): gold and Cube read the fixture in separate sessions.
Captured rows come from a run, not from a specification: a captured pass shows only that nothing changed since the
capture, carries independent = false and is listed in not_proven. The result is reported separately: report never
reads it, and no pass widens operational V1-V5 or report coverage. Results hold counts, ids and hashes only, never
rows, parameter values or DSNs.
"""

import dataclasses
import hashlib
import os
import re
from pathlib import Path
from types import SimpleNamespace

from . import compare, cube_gen, cube_run, db, ident, naming, stage_build, stage_cube
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEVERITY = ("error", "fail", "not_run")
OPERATIONAL = ("source.dsn_env", "source.owner_dsn_env", "cube.dsn_env")
DIGEST_SQL = "SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM {relation} t"
SCOPE_NOTE = ("expect ran the gold queries and the Cube model on the fixture database only; its result is reported "
              "separately and widens neither operational V1-V5 nor report coverage")
DSN_NOTE = ("the fixture database was told apart from the operational DSN variables by value and by host, port and "
            "database only; another name for the same server is not detected, so it must be a dedicated database")
AUTHORED_NOTE = ("authored expected rows are independent of the gold SQL and the configuration only as far as they "
                 "were written from the cited specification; a case covers its fixture rows and parameters, not the "
                 "operational data")
CAPTURED_NOTE = ("{n} captured case(s): their rows come from an earlier run, not from a specification, so a captured "
                 "pass shows only that nothing changed since the capture and is not independent evidence")


class MissingMember(ValueError):
    """A Cube row lacks a requested member; compare.cube_rows would otherwise read it as NULL."""


class _Members:
    """client.load that refuses a row without every member the gold maps."""

    def __init__(self, client, members):
        self.client, self.members = client, tuple(members)

    def load(self, query: dict) -> list[dict]:
        data = self.client.load(query)
        if any(not isinstance(r, dict) or any(m not in r for m in self.members) for r in data):
            raise MissingMember("a Cube row lacks a requested member")
        return data


# ---- guards and the fixture configuration ---------------------------------------------------------------
def _endpoint(value: str, name: str):
    """(host, port, database) of a DSN, or None when it does not parse (then only the values are compared)."""
    try:
        p = db.parse_dsn(value, name)
    except StageError:
        return None
    return str(p["host"]).lower(), int(p["port"]), p["database"]


def fixture_env(cfg, args) -> str:
    """The fixture DSN variable, after the guards that run before anything connects (StageError otherwise)."""
    if getattr(args, "allow_fixture_database", False) is not True:
        raise StageError("expect: --allow-fixture-database is required: the stage builds the configured views and "
                         "closure tables in the output schema of the fixture database named by --dsn-env", EXIT_CONFIG)
    name = getattr(args, "dsn_env", None)
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        raise StageError("expect: --dsn-env must be the NAME of an environment variable holding the fixture DSN",
                         EXIT_CONFIG)
    value = os.environ.get(name)
    if not value:
        raise StageError(f"environment variable {name} is not set (DSN of the dedicated expect fixture database)",
                         EXIT_ENV)
    mine = _endpoint(value, name)
    for ref, other in zip(OPERATIONAL, (cfg.source.dsn_env, cfg.source.owner_dsn_env, cfg.cube.dsn_env)):
        if not other:
            continue
        theirs = os.environ.get(other)
        same = bool(theirs) and (theirs == value or (mine is not None and mine == _endpoint(theirs, other)))
        if other == name or same:
            raise StageError(f"expect: {name} names the same database as {other} ({ref}); point it at a dedicated "
                             "fixture database, never an operational one", EXIT_CONFIG)
    return name


def fixture_config(cfg, env_name: str):
    """A copy of cfg whose every connection uses env_name. The data revision and runtime probes are off (they would
    probe the operational endpoints); everything else, the gold entries included, is shared."""
    source = dataclasses.replace(cfg.source, dsn_env=env_name, owner_dsn_env=env_name, data_revision_sql_file="",
                                 runtime_evidence=False)
    return dataclasses.replace(cfg, source=source, cube=dataclasses.replace(cfg.cube, dsn_env=env_name))


# ---- one case ---------------------------------------------------------------------------------------------
def compared_columns(gold, case) -> list[str]:
    """The gold columns a case compares, in map order: all of them, or the ones the rows of a partial case name (its
    key columns when it has no rows)."""
    if not case.partial:
        return list(gold.map)
    named = set(case.rows[0]) if case.rows else set(gold.key)
    return [c for c in gold.map if c in named]


def _pair(r: dict, a: str, b: str) -> dict:
    """One compare_rows result with its sides named (compare_rows calls its first side gold and its second cube)."""
    out = {"equal": r["pass"], "matched_rows": r["matched_rows"], f"only_in_{a}": r["missing_in_cube"],
           f"only_in_{b}": r["extra_in_cube"], f"duplicate_keys_{a}": r["duplicate_keys_gold"],
           f"duplicate_keys_{b}": r["duplicate_keys_cube"], "mismatched_columns": r["mismatched_columns"]}
    if r.get("gold_columns_missing"):
        out[f"{a}_columns_missing"] = r["gold_columns_missing"]
    if r.get("declared_null_as_zero"):
        out["declared_null_as_zero"] = r["declared_null_as_zero"]
    return out


def judge(gold, case, gold_rows: list[dict], cube_rows: list[dict], types: dict | None = None) -> tuple[str, dict]:
    """Status and count-only detail of one case from the rows of both sides. Gold is compared strictly (the gold SQL
    coalesces its zero_if_null columns itself), Cube with the declared zero_if_null equivalence, gold with Cube as V1."""
    cols = compared_columns(gold, case)
    view = SimpleNamespace(map={c: gold.map[c] for c in cols}, key=list(gold.key),
                           zero_if_null=[c for c in gold.zero_if_null if c in cols])
    strict = SimpleNamespace(map=view.map, key=view.key, zero_if_null=[])
    gold_side = compare.compare_rows(strict, gold_rows, case.rows, types)
    cube_side = compare.compare_rows(view, case.rows, cube_rows, types)
    both = compare.compare_rows(view, gold_rows, cube_rows, types)
    gold_ok, cube_ok, agree = gold_side["pass"], cube_side["pass"], both["pass"]
    if gold_ok and cube_ok and agree:
        reason = None
    elif agree:
        reason = "common_mode" if case.source == "authored" else "regression"
    elif gold_ok and not cube_ok:
        reason = "cube_mismatch"
    elif cube_ok and not gold_ok:
        reason = "gold_mismatch"
    else:
        reason = "both_mismatch"
    detail = {"source": case.source, "independent": case.source == "authored", "partial": case.partial,
              "columns": len(cols), "expected_rows": len(case.rows), "gold_rows": len(gold_rows),
              "cube_rows": len(cube_rows), "gold_equals_expected": gold_ok, "cube_equals_expected": cube_ok,
              "gold_equals_cube": agree, "gold_vs_expected": _pair(gold_side, "gold", "expected"),
              "cube_vs_expected": _pair(cube_side, "expected", "cube"), "gold_vs_cube": _pair(both, "gold", "cube")}
    if reason:
        detail["reason"] = reason
    return ("pass" if reason is None else "fail"), detail


def run_case(conn, client, cfg, gold, case, types: dict | None = None) -> tuple[str, dict]:
    """Run both sides of one case; a side that cannot run is an error with its type, SQLSTATE or HTTP status."""
    errors = compare.expected_errors()
    base = {"source": case.source, "independent": case.source == "authored", "partial": case.partial}
    try:
        g_rows = compare.gold_rows(conn, cfg, gold, case.params)
    except errors as e:
        return "error", {**base, "side": "gold", **compare.error_detail(e)}
    try:
        c_rows = compare.cube_rows(_Members(client, gold.map.values()), gold, case.params)
    except errors as e:
        return "error", {**base, "side": "cube", "gold_rows": len(g_rows), **compare.error_detail(e)}
    return judge(gold, case, g_rows, c_rows, types)


# ---- the fixture and the inner stages ----------------------------------------------------------------------
def fixture_digest(conn, cfg, hierarchies) -> tuple[str, int]:
    """SHA-256 over the row count and md5 of every source table and of the closure tables of `hierarchies`, read in
    one REPEATABLE READ snapshot; only the hash and the total row count leave the database."""
    relations = sorted({e.table for e in cfg.entities.values()} | {naming.closure_table(cfg, h) for h in hierarchies})
    digest, rows = hashlib.sha256(b"sxc-expect-fixture-v1\0"), 0
    with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
        for relation in relations:
            n, md5 = conn.run(DIGEST_SQL.format(relation=ident.qualified(relation)))[0]
            rows += int(n)
            digest.update(f"{relation}\0{n}\0{md5}\0".encode("utf-8"))
    return digest.hexdigest(), rows


def _worst(statuses) -> str:
    states = set(statuses)
    return next((s for s in _SEVERITY if s in states), "pass")


def _counts(res: Result) -> dict:
    out = dict.fromkeys(("pass", "fail", "error", "not_run"), 0)
    for c in res.checks:
        out[c.status] += 1
    return out


def _cubes(gold) -> set[str]:
    used, _malformed = stage_cube.query_members(gold.cube_query)
    return {m.split(".", 1)[0] for m in used | set(gold.map.values()) if isinstance(m, str)}


def _gate(closure: dict, served: bool, members, gold) -> dict | None:
    """Why a case of `gold` cannot run (not_run detail), or None."""
    for h, st in closure.items():
        if st != "pass" and _cubes(gold) & {naming.cube_closure(h), naming.cube_ancestor(h), naming.cube_descendant(h)}:
            return {"reason": "closure_not_passed", "hierarchy": h, "build_check": st or "missing"}
    if not served:
        return {"reason": "cube_not_passed"}
    if members is None or members.status != "pass":
        return {"reason": "gold_members_not_passed"}
    return None


def _build(res: Result, fcfg, work: Path) -> dict:
    """The build stage on the fixture: expect:build, and the closure:<h> status of every hierarchy."""
    build = stage_build.run(fcfg, SimpleNamespace(hierarchy=""), work)
    closures = [c for c in build.checks if c.id.startswith(("closure:", "closure_apply:"))]
    status = _worst(c.status for c in closures)
    first = next((c for c in closures if c.status != "pass"), None)
    res.add("expect:build", status, first.config_ref if first else "", **_counts(build), closures=len(closures),
            shape_checks_not_passed=sum(1 for c in build.checks if c.id.startswith("shape:") and c.status != "pass"))
    res.not_proven.extend(f"expect build: {n}" for n in build.not_proven)
    return {h: next((c.status for c in build.checks if c.id == f"closure:{h}"), None) for h in fcfg.hierarchies}


def _cube(res: Result, fcfg, work: Path) -> tuple[bool, dict]:
    """The cube stage on the fixture: expect:cube, whether Cube served the model, and the cube checks by id."""
    cube = stage_cube.run(fcfg, SimpleNamespace(), work)
    checks = {c.id: c for c in cube.checks}
    meta = checks.get("cube:meta")
    status = meta.status if meta is not None else "error"
    facts = {k: meta.detail[k] for k in ("cube_version", "sql_planner") if meta is not None and k in meta.detail}
    res.add("expect:cube", status, "", **_counts(cube), **facts)
    res.not_proven.extend(f"expect cube: {n}" for n in cube.not_proven)
    return status == "pass", checks


def _notes(res: Result, cases: list) -> None:
    res.not_proven += [SCOPE_NOTE, DSN_NOTE]
    if any(c.source == "authored" for _gi, _g, _i, c in cases):
        res.not_proven.append(AUTHORED_NOTE)
    captured = sum(1 for _gi, _g, _i, c in cases if c.source == "captured")
    if captured:
        res.not_proven.append(CAPTURED_NOTE.format(n=captured))


# ---- stage -------------------------------------------------------------------------------------------------
def run(cfg, args, out: Path) -> Result:
    res = Result("expect")
    cases = [(gi, g, i, c) for gi, g in enumerate(cfg.gold) for i, c in enumerate(g.expect)]
    if not cases:
        res.add("expect:cases", "not_run", "gold", reason="no_cases", gold=len(cfg.gold))
        res.next_safe_action = next_action(res)
        return res
    env = fixture_env(cfg, args)
    fcfg = fixture_config(cfg, env)
    work = Path(out) / "expect"
    _notes(res, cases)
    closure = _build(res, fcfg, work)
    served, cube_checks = _cube(res, fcfg, work)
    kinds = compare.member_kinds(cube_gen.load_model(work))
    planned = []
    for gi, g, i, case in cases:
        cid, ref = f"expect:{g.id}[{i}]", f"gold[{gi}] {g.id} expect[{i}]"
        gate = _gate(closure, served, cube_checks.get(f"cube:gold_members:{g.id}"), g)
        if gate:
            res.add(cid, "not_run", ref, source=case.source, **gate)
        else:
            planned.append((cid, ref, g, case))
    if planned:
        _run_cases(res, fcfg, env, work, planned, kinds, [h for h, st in closure.items() if st == "pass"])
    res.next_safe_action = next_action(res)
    return res


def _run_cases(res: Result, fcfg, env: str, work: Path, planned: list, kinds: dict, built: list) -> None:
    log = work / "server.log"
    log.write_text("", encoding="utf-8")
    conn = db.connect(env)
    try:
        try:
            before = fixture_digest(conn, fcfg, built)
        except compare.expected_errors() as e:
            res.add("expect:fixture", "error", "", reason="fingerprint_failed", **compare.error_detail(e))
            for cid, ref, _g, case in planned:
                res.add(cid, "not_run", ref, source=case.source, reason="fixture_unreadable")
            return
        ran = []
        with cube_run.serve(fcfg, work / "cube", log) as client:
            for cid, ref, g, case in planned:
                status, detail = run_case(conn, client, fcfg, g, case, compare.gold_kinds(g, kinds))
                ran.append(res.add(cid, status, ref, **detail))
        try:
            after, problem = fixture_digest(conn, fcfg, built), {}
        except compare.expected_errors() as e:
            after, problem = None, compare.error_detail(e)
    finally:
        conn.close()
    detail = {"relations": len({e.table for e in fcfg.entities.values()}) + len(built), "rows": before[1],
              "fixture_sha256": before[0]}
    if after == before:
        res.add("expect:fixture", "pass", "", **detail)
        return
    reason = "fixture_changed" if after is not None else "fingerprint_failed"
    res.add("expect:fixture", "error", "", reason=reason, **detail, **problem)
    for c in ran:   # gold and Cube may have read different fixture rows: no case is shown to have passed
        if c.status == "pass":
            c.status = "not_run"
            c.detail["reason"] = reason


# ---- next action ---------------------------------------------------------------------------------------------
_ACTIONS = {
    "no_cases": "add [[gold.expect]] cases with rows written from the business specification to the [[gold]] entries "
                "(references/config.md), load their fixture into a dedicated database, then run expect",
    "common_mode": "{id}: the gold SQL and Cube agree with each other but not with the authored rows of {ref}: check "
                   "the cited specification; when it holds, fix the gold SQL and the configuration entry they share, "
                   "otherwise correct the rows, then rerun expect",
    "regression": "{id}: the gold SQL and Cube both changed since the rows of {ref} were captured: fix the change, or "
                  "confirm it is intended and capture the rows again, then rerun expect",
    "gold_mismatch": "{id}: the gold SQL differs from the expected rows of {ref} while Cube matches them: fix the gold "
                     "SQL, then rerun expect",
    "cube_mismatch": "{id}: Cube differs from the expected rows of {ref} while the gold SQL matches them: fix the "
                     "crosscheck.toml entries its members come from, then rerun expect",
    "both_mismatch": "{id}: the gold SQL and Cube differ from the expected rows of {ref} and from each other: check "
                     "both against the cited specification, then rerun expect",
    "fixture_changed": "the fixture database changed while expect ran: keep it unchanged for the whole run, then rerun "
                       "expect",
    "fingerprint_failed": "the fixture's tables could not be fingerprinted (the detail has the SQLSTATE): let the role "
                          "of the fixture DSN read the source and closure tables, then rerun expect",
    "closure_not_passed": "{id}: the fixture's closure did not build (see expect/build/build.sql): fix the fixture rows "
                          "or the hierarchy entry, then rerun expect",
    "cube_not_passed": "Cube did not serve the fixture's model as configured (see expect/cube): fix what expect:cube "
                       "reports, then rerun expect",
    "gold_members_not_passed": "{id}: the Cube model of the fixture lacks members {ref} uses: fix its cube_query and "
                               "map, or load every source table into the fixture, then rerun expect",
}


def next_action(res: Result) -> str:
    st = res.status
    if st == "pass":
        return ("every case matched its expected rows on the fixture: review this result separately from operational "
                "V1-V5 (report does not read it)")
    first = next(c for c in res.checks if c.status == st)
    text = _ACTIONS.get(first.detail.get("reason"))
    if text:
        return text.format(id=first.id, ref=first.config_ref or "its entry")
    if first.id == "expect:build":
        return ("a closure of the fixture did not build (see expect/build/build.sql): fix the fixture rows or the "
                f"hierarchy entry {first.config_ref}, then rerun expect")
    if first.id == "expect:cube":
        return "Cube did not serve the fixture's model as configured (see expect/cube): fix it, then rerun expect"
    why = first.detail.get("sqlstate") or first.detail.get("error") or first.detail.get("reason") or st
    return (f"{first.id} could not run ({why}; the detail has the error type, SQLSTATE or HTTP status): fix the gold "
            f"SQL, {first.config_ref or 'its entry'} or the fixture database, then rerun expect")
