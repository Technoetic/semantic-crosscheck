"""expect stage: literal expected rows ([[gold.expect]]) for the gold queries, on a dedicated fixture database.

Why: V1-V4 compare the gold SQL with Cube. When both share a mistake (the same dropped join, the same wrong quantity
column) they agree and V1 passes. Rows a person wrote from the business specification for a small fixture are a
third oracle that is neither SQL nor the configuration: the literal expected-row form of dbt and SQLMesh unit tests.
Hierarchy closures are recursive and cannot be mocked, so both sides really run on the fixture:

  1. Guards before anything connects. A configuration without cases is expect:cases not_run. Otherwise
     --allow-fixture-database is required (exit 2); the --dsn-env variable (default SXC_EXPECT_DSN) must be set
     (exit 3) and must not be, hold the value of, or name the host, port and database of source.dsn_env,
     source.owner_dsn_env or cube.dsn_env as set in this environment (exit 2; no value is printed). Another name for
     the same server, or an operational variable that is not set, is not detected: expect:fixture counts the
     variables compared and not_proven names the unset ones, so the fixture must be a dedicated database.
  2. The fixture database is the user's: it already holds the source tables and the fixture rows. sxc executes no
     fixture SQL and never connects to an operational DSN. The source tables are fingerprinted first (fixture_sha256,
     row text in COLLATE "C" order under fixed session settings, so a pin depends on the rows and column types, not
     on database defaults); a case that pins another fixture_sha256 is not_run (fixture_mismatch). A copy of the
     configuration whose DSN variables all name the fixture variable then runs the normal build and cube stages into
     <out>/expect/ (build writes its views and closure tables into the fixture database's output schema: what the
     consent flag allows; they stay there, nothing is cleaned up), so the operational build/ and cube/ results are
     never touched. expect:build and expect:cube summarise them.
  3. One Cube process serves the cases (log in expect/server.log; the inner cube stage starts its own for cube:meta).
     For every case the gold SQL (READ ONLY, the case's bound parameters) and the gold's cube_query each give rows
     that are compared with the expected rows (compare.compare_rows, typed by the model's member kinds): gold
     strictly, Cube with the declared zero_if_null equivalence, and gold with Cube as in V1. A Cube row without a
     requested member is an error, never a NULL. A partial case compares only the columns its rows name.
  4. expect:<gold>[<i>] passes when gold and Cube both equal the expected rows. Otherwise it fails: common_mode when
     they agree with each other but not with the rows of an authored case, regression when they agree but not with
     the rows of a captured case, gold_mismatch, cube_mismatch or both_mismatch when they disagree
     (gold_equals_expected and cube_equals_expected say which side equals the rows). A side that cannot run, or
     expected rows whose keys are equal in their typed form, is an error. A case is not_run when its closure (build
     closure:<h> or closure_apply:<h>) or its Cube members (cube:gold_members:<gold>) did not pass or Cube did not
     serve the model (cube:meta); every not_run case gets a not_proven line.
  5. expect:fixture compares the source-table digest taken before the build with the digests of the source and built
     closure tables taken before and after the cases (row count and md5 computed in the database; only the hash
     leaves it), while a second session holds SHARE locks on those tables when the role can take them (as cube_replay
     does), so that writers wait; otherwise locked is false and not_proven says so. A change is an error and turns
     every passing case into not_run (fixture_changed). The digests show equality at those points, not one shared
     snapshot, and cover no other table; gold and Cube read in separate sessions: not_proven says so.
Captured rows come from a run, not from a specification: a captured pass shows only that nothing changed since the
capture, carries independent = false and is listed in not_proven. The result is reported separately: report never
reads it, and no pass widens operational V1-V5 or report coverage. Results hold counts, ids and hashes only, never
rows, parameter values or DSNs.
"""

import contextlib
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
DIGEST_SQL = ("SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text COLLATE \"C\"), '')) "
              "FROM {relation} t")
# A case stores fixture_sha256 as a pin and compares it across runs and databases, so the digest must depend on the
# rows and the column types, not on the database's defaults: the row text is ordered by COLLATE "C" (byte order, not
# the database's default collation), and these settings, which change the text of timestamptz, date and time,
# interval, float, bytea and money values, are fixed for the digest's transaction only.
DIGEST_SETTINGS = ("SET LOCAL TimeZone = 'UTC'", "SET LOCAL DateStyle = 'ISO, YMD'",
                   "SET LOCAL IntervalStyle = 'postgres'", "SET LOCAL extra_float_digits = 1",
                   "SET LOCAL bytea_output = 'hex'", "SET LOCAL lc_monetary = 'C'")
SCOPE_NOTE = ("expect ran the gold queries and the Cube model on the fixture database only; its result is reported "
              "separately and widens neither operational V1-V5 nor report coverage")
DSN_NOTE = ("the fixture DSN was compared with the {n} operational DSN variable(s) set in this environment, by value "
            "and by host, port and database only; another name for the same server is not detected, so the fixture "
            "must be a dedicated database")
UNSET_NOTE = ("operational DSN variable(s) not set in this environment, so never compared with the fixture DSN: "
              "{names}")
FIXTURE_NOTE = ("expect:fixture shows the fixture's source tables equal before the build and after the cases, and its "
                "source and built closure tables equal before and after the cases: equality at those points, not one "
                "shared snapshot; it covers only the entity and built closure tables, not other tables the gold SQL "
                "reads, and the gold SQL and Cube read the fixture in separate sessions")
LOCK_NOTE = ("the fixture's tables could not be locked against writes during the cases (for example a role without "
             "UPDATE, DELETE, TRUNCATE or MAINTAIN on them, or a conflicting lock held past "
             "source.statement_timeout_ms), so only the digests show that they did not change")
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
                         "closure tables in the output schema of the fixture database named by --dsn-env, and holds "
                         "SHARE locks on the fixture's source and closure tables while the cases run, so writers to "
                         "them wait", EXIT_CONFIG)
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


def operational_variables(cfg) -> tuple[list[str], list[str]]:
    """The operational DSN variable names that are set in this environment (the guard compared the fixture DSN with
    them) and those that are not (never compared)."""
    names = sorted({n for n in (cfg.source.dsn_env, cfg.source.owner_dsn_env, cfg.cube.dsn_env) if n})
    compared = [n for n in names if os.environ.get(n)]
    return compared, [n for n in names if n not in compared]


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
    if cube_side["duplicate_keys_gold"] or gold_side["duplicate_keys_cube"]:
        reason = "expected_duplicate_keys"   # keys distinct as written but equal in their typed form (1 and "1")
    elif gold_ok and cube_ok and agree:
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
    if reason == "expected_duplicate_keys":
        return "error", detail
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
def fixture_relations(cfg, hierarchies=()) -> list[str]:
    """The source (entity) tables and the closure tables of `hierarchies`."""
    return sorted({e.table for e in cfg.entities.values()} | {naming.closure_table(cfg, h) for h in hierarchies})


def fixture_digest(conn, cfg, hierarchies=()) -> dict:
    """Digests of the fixture read in one REPEATABLE READ snapshot: fixture_sha256 over the row count and md5 of every
    source table (what a case pins), tables_sha256 over those and the closure tables of `hierarchies`, and the total
    row count. The row text is ordered by COLLATE "C" under DIGEST_SETTINGS, so a pin depends on the rows and the
    column types (and the database encoding the text is hashed in), not on the default collation or the session's
    settings (v2; v1 depended on both). Only hashes and counts leave the database."""
    sources = {e.table for e in cfg.entities.values()}
    fixture, tables, rows = hashlib.sha256(b"sxc-expect-fixture-v2\0"), hashlib.sha256(b"sxc-expect-tables-v2\0"), 0
    with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
        for setting in DIGEST_SETTINGS:
            conn.run(setting)
        for relation in fixture_relations(cfg, hierarchies):
            n, md5 = conn.run(DIGEST_SQL.format(relation=ident.qualified(relation)))[0]
            rows += int(n)
            part = f"{relation}\0{n}\0{md5}\0".encode("utf-8")
            tables.update(part)
            if relation in sources:
                fixture.update(part)
    return {"fixture_sha256": fixture.hexdigest(), "tables_sha256": tables.hexdigest(), "rows": rows}


@contextlib.contextmanager
def share_locks(env: str, cfg, relations):
    """SHARE locks on `relations`, held from a second session of the fixture DSN until the block ends (as cube_replay
    does): the gold SQL and Cube can still read them, a writer waits. Yields False when a lock could not be taken
    (for example a role without UPDATE, DELETE, TRUNCATE or MAINTAIN on a table, or a conflicting lock held past
    source.statement_timeout_ms); the digests are then the only check."""
    conn = db.connect(env)
    try:
        held = contextlib.ExitStack()
        try:
            held.enter_context(db.read_only(conn, cfg.source.statement_timeout_ms))
            for relation in relations:
                conn.run(f"LOCK TABLE {ident.qualified(relation)} IN SHARE MODE")
            locked = True
        except compare.expected_errors():
            held.close()
            locked = False
        with held:
            yield locked
    finally:
        conn.close()


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
    """The build stage on the fixture: expect:build, and per hierarchy the worse of closure:<h> and closure_apply:<h>
    (None when closure:<h> is missing)."""
    build = stage_build.run(fcfg, SimpleNamespace(hierarchy=""), work)
    closures = [c for c in build.checks if c.id.startswith(("closure:", "closure_apply:"))]
    status = _worst(c.status for c in closures)
    first = next((c for c in closures if c.status != "pass"), None)
    res.add("expect:build", status, first.config_ref if first else "", **_counts(build), closures=len(closures),
            shape_checks_not_passed=sum(1 for c in build.checks if c.id.startswith("shape:") and c.status != "pass"))
    res.not_proven.extend(f"expect build: {n}" for n in build.not_proven)
    status = {}
    for h in fcfg.hierarchies:
        own = [c.status for c in build.checks if c.id in (f"closure:{h}", f"closure_apply:{h}")]
        status[h] = _worst(own) if any(c.id == f"closure:{h}" for c in build.checks) else None
    return status


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


def _notes(res: Result, cases: list, compared: list, unset: list) -> None:
    res.not_proven += [SCOPE_NOTE, DSN_NOTE.format(n=len(compared))]
    if unset:
        res.not_proven.append(UNSET_NOTE.format(names=", ".join(unset)))
    if any(c.source == "authored" for _gi, _g, _i, c in cases):
        res.not_proven.append(AUTHORED_NOTE)
    captured = sum(1 for _gi, _g, _i, c in cases if c.source == "captured")
    if captured:
        res.not_proven.append(CAPTURED_NOTE.format(n=captured))


# ---- stage -------------------------------------------------------------------------------------------------
def _not_run(res: Result, cid: str, ref: str, case, **detail) -> None:
    """A case that did not run, with its not_proven line (as verify does for gated items)."""
    res.add(cid, "not_run", ref, source=case.source, **detail)
    where = f", hierarchy {detail['hierarchy']}" if detail.get("hierarchy") else ""
    res.not_proven.append(f"{cid}: not run ({detail['reason']}{where})")


def run(cfg, args, out: Path) -> Result:
    res = Result("expect")
    cases = [(gi, g, i, c) for gi, g in enumerate(cfg.gold) for i, c in enumerate(g.expect)]
    if not cases:
        res.add("expect:cases", "not_run", "gold", reason="no_cases", gold=len(cfg.gold))
        res.next_safe_action = next_action(res)
        return res
    env = fixture_env(cfg, args)
    compared, unset = operational_variables(cfg)
    fcfg = fixture_config(cfg, env)
    _notes(res, cases, compared, unset)
    conn = db.connect(env)
    try:
        _stage(res, conn, fcfg, env, Path(out) / "expect", cases, {"operational_variables_compared": len(compared)})
    finally:
        conn.close()
    res.next_safe_action = next_action(res)
    return res


def _stage(res: Result, conn, fcfg, env: str, work: Path, cases: list, fixture: dict) -> None:
    """Fingerprint the fixture before build writes to it, run the inner stages, gate the cases and run the rest."""
    try:
        source = fixture_digest(conn, fcfg)
    except compare.expected_errors() as e:
        res.add("expect:fixture", "error", "", reason="fingerprint_failed", **fixture, **compare.error_detail(e))
        for gi, g, i, case in cases:
            _not_run(res, f"expect:{g.id}[{i}]", f"gold[{gi}] {g.id} expect[{i}]", case, reason="fixture_unreadable")
        return
    fixture.update(fixture_sha256=source["fixture_sha256"])
    closure = _build(res, fcfg, work)
    served, cube_checks = _cube(res, fcfg, work)
    kinds = compare.member_kinds(cube_gen.load_model(work))
    planned = []
    for gi, g, i, case in cases:
        cid, ref = f"expect:{g.id}[{i}]", f"gold[{gi}] {g.id} expect[{i}]"
        if case.fixture_sha256 and case.fixture_sha256 != source["fixture_sha256"]:
            gate = {"reason": "fixture_mismatch"}
        else:
            gate = _gate(closure, served, cube_checks.get(f"cube:gold_members:{g.id}"), g)
        if gate:
            _not_run(res, cid, ref, case, **gate)
        else:
            planned.append((cid, ref, g, case))
    if not planned:
        res.add("expect:fixture", "not_run", "", reason="no_case_ran", **fixture)
        return
    _run_cases(res, conn, fcfg, env, work, planned, kinds, [h for h, st in closure.items() if st == "pass"],
               source, fixture)


def _run_cases(res: Result, conn, fcfg, env: str, work: Path, planned: list, kinds: dict, built: list, source: dict,
               fixture: dict) -> None:
    log = work / "server.log"
    log.write_text("", encoding="utf-8")
    relations = fixture_relations(fcfg, built)
    ran, problem = [], {}
    with share_locks(env, fcfg, relations) as locked:
        try:
            start = fixture_digest(conn, fcfg, built)
        except compare.expected_errors() as e:
            res.add("expect:fixture", "error", "", reason="fingerprint_failed", **fixture, **compare.error_detail(e))
            for cid, ref, _g, case in planned:
                _not_run(res, cid, ref, case, reason="fixture_unreadable")
            return
        with cube_run.serve(fcfg, work / "cube", log) as client:
            for cid, ref, g, case in planned:
                status, detail = run_case(conn, client, fcfg, g, case, compare.gold_kinds(g, kinds))
                ran.append(res.add(cid, status, ref, **detail))
        try:
            end = fixture_digest(conn, fcfg, built)
        except compare.expected_errors() as e:
            end, problem = None, compare.error_detail(e)
    if not locked:
        res.not_proven.append(LOCK_NOTE)
    res.not_proven.append(FIXTURE_NOTE)
    fixture.update(relations=len(relations), rows=start["rows"], locked=locked)
    if end == start and start["fixture_sha256"] == source["fixture_sha256"]:
        res.add("expect:fixture", "pass", "", **fixture)
        return
    reason = "fingerprint_failed" if end is None else "fixture_changed"
    res.add("expect:fixture", "error", "", reason=reason, **fixture, **problem)
    for c in ran:   # gold and Cube may have read different fixture rows: no case is shown to have passed
        if c.status == "pass":
            c.status = "not_run"
            c.detail["reason"] = reason
            res.not_proven.append(f"{c.id}: not run ({reason})")


# ---- next action ---------------------------------------------------------------------------------------------
_ACTIONS = {
    "no_cases": "add [[gold.expect]] cases with rows written from the business specification to the [[gold]] entries "
                "(references/config.md), load their fixture into a dedicated database, then run expect",
    "common_mode": "{id}: the gold SQL and Cube agree with each other but not with the authored rows of {ref}: first "
                   "check that the fixture rows and the expected rows follow the cited specification (a wrongly loaded "
                   "fixture looks the same; pin fixture_sha256); only when both do, fix the gold SQL and the "
                   "configuration entry they share, otherwise correct the fixture or the rows, then rerun expect",
    "regression": "{id}: the gold SQL and Cube agree with each other but not with the captured rows of {ref}: first "
                  "check that the loaded fixture is the one the rows were captured on (compare expect:fixture's "
                  "fixture_sha256 with the case's pin, or pin it); only when it is, the gold SQL and Cube changed "
                  "since the capture: fix the change, or confirm it is intended and capture the rows again, then "
                  "rerun expect",
    "expected_duplicate_keys": "{id}: the expected rows of {ref} hold keys that are equal in the form of their Cube "
                               "type (for example 1 and \"1\"): make every key distinct, then rerun expect",
    "fixture_mismatch": "{id}: the fixture's source tables do not have the fixture_sha256 that {ref} pins: load the "
                        "fixture the case was written for, or check the loaded rows against the cited specification "
                        "before pinning expect:fixture's fixture_sha256, then rerun expect",
    "no_case_ran": "no case ran on the fixture: fix what the not_run cases report, then rerun expect",
    "gold_mismatch": "{id}: the gold SQL differs from the expected rows of {ref} while Cube matches them: fix the gold "
                     "SQL, then rerun expect",
    "cube_mismatch": "{id}: Cube differs from the expected rows of {ref} while the gold SQL matches them: fix the "
                     "crosscheck.toml entries its members come from, then rerun expect",
    "both_mismatch": "{id}: the gold SQL and Cube differ from the expected rows of {ref} and from each other: check "
                     "both against the cited specification, then rerun expect",
    "fixture_changed": "the fixture database changed while expect ran: keep it unchanged for the whole run, then rerun "
                       "expect",
    "fingerprint_failed": "the fixture's tables could not be fingerprinted (the detail has the SQLSTATE): load every "
                          "configured source table into the fixture and let the role of the fixture DSN read them and "
                          "the closure tables, then rerun expect",
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
        if all(c.detail.get("source") == "captured" for c in res.checks if "source" in c.detail):
            return ("every case matched its expected rows on the fixture, but every case is captured: they show only "
                    "that nothing changed since the capture, not that the rows follow a specification; review this "
                    "result separately from operational V1-V5 (report does not read it)")
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
