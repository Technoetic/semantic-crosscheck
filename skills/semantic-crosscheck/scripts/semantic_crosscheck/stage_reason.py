"""reason stage: SHACL against the SQL views, OWL consistency and OWL against SQL (V5), design sections 6 and 7.

Why: build writes every data rule as an SQL view and as a SHACL shape, and its closure table answers what an OWL
ontology of the same rows answers by reasoning. Two layers that disagree mean one of them is wrong; this stage says
which, per shape, per hierarchy and per defined class.

  shacl:<shape>       pySHACL's focus nodes on the rows exported as RDF equal the keys in v_shape_<shape> (set
                      equality; detail: violations, shacl_only, sql_only, value_group). Shapes are validated per
                      value group (shacl_check.groups), so shapes needing different kinds of RDF value on one column
                      are all compared. Shapes without a SHACL form are listed in not_proven: they are checked only
                      in SQL. A shape whose view build did not produce is not_run; a shape whose value group pySHACL
                      could not validate is error.
  owl:consistency     HermiT finds the ontology of owl.py consistent (detail: individuals, edges, hermit_ms)
  v5:closure:<h>      the OWL closure pairs equal the closure table. not_run unless build's closure:<h> passed with no
                      cycle edges dropped: otherwise the table describes another graph than the one OWL reasons on
  v5:defined:<name>   the members of a defined class equal its SQL definition (owl.defined_sql); contains_class reads
                      the closure table and has the same precondition as v5:closure. A class without members on either
                      side passes vacuously and is listed in not_proven
  v5:witness:<name>   optional --synthetic-witnesses: configured HermiT and SQL translations each match independently
                      specified members of a small synthetic graph. Source-data vacuity remains not_proven; controls
                      mean absence of entailment, not negative OWL facts
  scope:<part>        not_run (reason excluded_by_only) for the part --only leaves out, so a narrowed run never passes
An inconsistent ontology entails every statement, so the V5 checks are then not_run and listed in not_proven.

Prerequisites: build/result.json and build/shapes.ttl of a build of the same configuration (a shapes.ttl that differs
from what the configuration generates is exit 2: rerun build). A missing component is exit 3 naming it, unless --only
leaves its part out: rdflib and pySHACL for shacl, owlready2 and Java (SXC_JAVA or PATH) for owl. The size limits
verify.shacl_max_rows and verify.owl_max_individuals are checked by counting first; a part above its limit leaves
explicit not_run checks and a not_proven explanation. Database reads are READ ONLY transactions; results keep
counts and ids only.
"""

import importlib
from pathlib import Path

from . import db, ident, naming, owl, owl_witness, shacl_check, shapes, stage_build
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError, require_stage

PARTS = ("shacl", "owl")
INSTALL = '<python> -m pip install -r "<plugin>/requirements/reason.txt"'
META_ID = "shacl:meta:generated"
BUILD_REASONS = ("shape_not_built", "view_missing", "closure_not_passed", "closure_table_missing")
WHY = {   # why the closure table cannot be compared with OWL ({h}: the hierarchy)
    "closure_not_passed": "closure:{h} did not pass in build",
    "cycle_edges_dropped": "build left the edges inside cycles out of the closure of {h} (on_cycle = report), while OWL "
                           "reasons on every edge",
    "closure_table_missing": "the closure table of {h} does not exist; rerun build",
}


def run(cfg, args, out: Path) -> Result:
    out = Path(out)
    only = _only(args)
    if getattr(args, "synthetic_witnesses", False) and not cfg.ontology.hierarchy:
        raise StageError("--synthetic-witnesses requires a configured [ontology] hierarchy", EXIT_CONFIG)
    build = require_stage(out, "build")
    planned, skipped = shacl_check.plan(cfg) if "shacl" in only else ([], {})
    use_owl = "owl" in only and bool(cfg.ontology.hierarchy)
    if use_owl:
        owl.check_config(cfg)
    ttl = shapes_ttl(cfg, out) if planned else ""
    java = components(bool(planned), use_owl)
    res = Result("reason")
    shacl_ready = bool(planned) and _generated_shapes_preflight(ttl, res)
    if planned and not shacl_ready:
        for s in planned:
            res.add(f"shacl:{s.id}", "not_run", shapes.config_ref(cfg, s), reason="generated_shapes_invalid")
        res.not_proven.append("SHACL row comparisons did not run because generated shapes did not pass the "
                              "shacl-shacl syntax preflight")
    if "shacl" in only:
        _shacl_not_proven(cfg, skipped, res)
    if "owl" in only and not use_owl:
        res.not_proven.append("owl: the configuration has no [ontology] section; the OWL checks were not run")
    if shacl_ready or use_owl:
        conn = db.connect(cfg.source.dsn_env)
        try:
            if use_owl:
                check_owl_sources(conn, cfg)
            if shacl_ready:
                _shacl(conn, cfg, build, planned, ttl, res)
            if use_owl:
                _owl(conn, cfg, build, java, res)
                if getattr(args, "synthetic_witnesses", False):
                    owl_witness.run(conn, cfg, java, res)
        finally:
            conn.close()
    for part in PARTS:
        if part not in only:
            res.add(f"scope:{part}", "not_run", "", reason="excluded_by_only")
    res.next_safe_action = next_action(cfg, res)
    return res


def _generated_shapes_preflight(ttl: str, res: Result) -> bool:
    try:
        conforms, count = shacl_check.validate_generated_shapes(ttl)
    except Exception:
        res.add(META_ID, "error", "shapes", reason="meta_validation_error")
        return False
    res.add(META_ID, "pass" if conforms else "fail", "shapes", violations=count, profile="shacl-shacl")
    res.not_proven.append("Generated SHACL passed only the available shacl-shacl syntax subset when conformant; "
                          "this does not prove complete syntax coverage or business-rule semantics")
    return conforms


# ---- prerequisites --------------------------------------------------------------------------------------
def _only(args) -> list[str]:
    only = list(getattr(args, "only", None) or PARTS)
    bad = [x for x in only if x not in PARTS]
    if bad:
        raise StageError(f"--only accepts {','.join(PARTS)} (got {','.join(bad)})", EXIT_CONFIG)
    if getattr(args, "synthetic_witnesses", False) and "owl" not in only:
        raise StageError("--synthetic-witnesses requires OWL checks: include owl in --only", EXIT_CONFIG)
    return only


def shapes_ttl(cfg, out: Path) -> str:
    """build/shapes.ttl, which must be what the configuration generates now (otherwise the views are stale too)."""
    f = Path(out) / "build" / "shapes.ttl"
    if not f.is_file():
        raise StageError("run `sxc.py build` first (no build/shapes.ttl in the output directory)", EXIT_CONFIG)
    text = f.read_text(encoding="utf-8")
    if text != shapes.shapes_ttl(cfg)[0]:
        raise StageError("build/shapes.ttl does not match crosscheck.toml (the shapes changed after build): rerun "
                         "`sxc.py build`, then reason", EXIT_CONFIG)
    return text


def _require(module: str, name: str, other: str) -> None:
    try:
        importlib.import_module(module)
    except Exception as e:  # noqa: BLE001 - a package that cannot be imported is as missing as an absent one
        raise StageError(f"{name} is not installed or cannot be imported ({type(e).__name__}): {INSTALL}, or run "
                         f"with --only {other}", EXIT_ENV) from e


def components(shacl: bool, use_owl: bool) -> str | None:
    """Import the packages the selected parts need (exit 3 naming the missing one); returns the java executable."""
    if shacl:
        _require("rdflib", "rdflib", "owl")
        _require("pyshacl", "pySHACL", "owl")
    if not use_owl:
        return None
    _require("owlready2", "owlready2", "shacl")
    java = owl.java_executable()
    if not java:
        raise StageError("Java is not available for HermiT: set SXC_JAVA to the java executable or put java on PATH "
                         "(Java 11 or later), or run with --only shacl", EXIT_ENV)
    return java


def check_owl_sources(conn, cfg) -> None:
    """The node and edge tables of the ontology's hierarchy exist with the configured columns (exit 2) and the check
    role can read them (exit 3), checked in the catalogs before any row is read."""
    h = cfg.hierarchies[cfg.ontology.hierarchy]
    node, edge = cfg.entities[h.node], cfg.entities[h.edge]
    needed = [(f"entities.{h.node}", node.table, node.key[:1]), (f"hierarchies.{h.name}", edge.table, [h.parent, h.child])]
    if cfg.ontology.class_column:
        needed.append(("ontology.class_column", node.table, [cfg.ontology.class_column]))
    found = {}
    for ref, table, cols in needed:
        if table not in found:
            schema, name = ident.split(table)
            r = db.rows(conn, stage_build.SOURCE_SQL, s=schema, t=name)
            found[table] = r[0] if r else None
        info = found[table]
        if not info or not info["found"]:
            raise StageError(f"crosscheck.toml [{ref}]: table {table} does not exist in the connected database", EXIT_CONFIG)
        if not (info["usage"] and info["readable"]):
            raise StageError(f"the check role cannot read {table} (used by [{ref}]): grant it USAGE on the schema "
                             "and SELECT on the table", EXIT_ENV)
        missing = [c for c in cols if c not in info["columns"]]
        if missing:
            raise StageError(f"crosscheck.toml [{ref}]: column {missing[0]} not found in {table}", EXIT_CONFIG)


def _build_check(build: dict, check_id: str) -> dict:
    for c in build.get("checks", []):
        if isinstance(c, dict) and c.get("id") == check_id:
            return c
    return {}


def _db_error(e: Exception) -> str:
    """SQLSTATE of a database error; any other exception propagates."""
    code = shapes.sqlstate(e)
    if not code:
        raise e
    return code


# ---- SHACL ------------------------------------------------------------------------------------------------
def _shacl_not_proven(cfg, skipped: dict, res: Result) -> None:
    if not cfg.shapes:
        res.not_proven.append("shacl: the configuration has no [[shapes]] entries")
    for s in cfg.shapes:
        if s.id in skipped:
            res.not_proven.append(f"shacl:{s.id}: no SHACL form ({skipped[s.id]}); checked only in SQL (shape:{s.id} in build)")


def _shacl(conn, cfg, build: dict, planned: list, ttl: str, res: Result) -> None:
    try:
        compared = shacl_check.run(conn, cfg, ttl)
    except shacl_check.TooManyRows as e:
        for shape in planned:
            res.add(f"shacl:{shape.id}", "not_run", shapes.config_ref(cfg, shape),
                    reason="resource_limit", rows=e.rows, limit=e.limit)
        res.not_proven.append(f"shacl: {e.rows} rows to export exceed verify.shacl_max_rows = {e.limit}; no shape was "
                              "compared with SHACL")
        return
    except Exception as e:  # noqa: BLE001 - database errors become error checks with their SQLSTATE
        code = _db_error(e)
        for s in planned:
            res.add(f"shacl:{s.id}", "error", shapes.config_ref(cfg, s), sqlstate=code)
        return
    for s in planned:
        ref, got = shapes.config_ref(cfg, s), compared[s.id]
        if "validation_error" in got:   # only the shapes of the value group pySHACL could not validate
            res.add(f"shacl:{s.id}", "error", ref, reason="validation_failure", exception=got["validation_error"],
                    value_group=got["group"])
            continue
        built = _build_check(build, f"shape:{s.id}").get("status")
        if built not in ("pass", "fail"):
            res.add(f"shacl:{s.id}", "not_run", ref, reason="shape_not_built", build_status=built or "absent")
            res.not_proven.append(f"shacl:{s.id}: not compared (shape:{s.id} in build is {built or 'absent'}, so there "
                                  "is no view to compare with)")
            continue
        if got["sql_keys"] is None:
            res.add(f"shacl:{s.id}", "not_run", ref, reason="view_missing", build_status=built)
            res.not_proven.append(f"shacl:{s.id}: not compared ({naming.shape_view(cfg, s.id)} does not exist; rerun build)")
            continue
        flagged, viewed = got["shacl_keys"], got["sql_keys"]
        res.add(f"shacl:{s.id}", "pass" if flagged == viewed else "fail", ref, violations=len(viewed),
                shacl_focus_nodes=len(flagged), shacl_only=len(flagged - viewed), sql_only=len(viewed - flagged),
                severity=s.severity, kind=shapes.kind(s), rows_exported=got["rows"], value_group=got["group"],
                group_rows=got["group_rows"], view=naming.shape_view(cfg, s.id))


# ---- OWL and V5 -------------------------------------------------------------------------------------------
def _closure_reason(conn, cfg, build: dict, h) -> str:
    """'' when the closure table can be compared with OWL, else why not (a key of WHY)."""
    c = _build_check(build, f"closure:{h.name}")
    if c.get("status") != "pass":
        return "closure_not_passed"
    if (c.get("detail") or {}).get("dropped_cycle_edges"):
        return "cycle_edges_dropped"
    if not owl.closure_table_exists(conn, cfg):
        return "closure_table_missing"
    return ""


def _v5_ids(cfg) -> list[tuple[str, str]]:
    h = cfg.ontology.hierarchy
    return [(f"v5:closure:{h}", f"hierarchies.{h}")] + [(f"v5:defined:{d.name}", f"ontology.defined[{j}]")
                                                        for j, d in enumerate(cfg.ontology.defined)]


def _v5_not_run(cfg, res: Result, reason: str) -> None:
    for cid, ref in _v5_ids(cfg):
        res.add(cid, "not_run", ref, reason=reason)


def _owl(conn, cfg, build: dict, java: str, res: Result) -> None:
    h = cfg.hierarchies[cfg.ontology.hierarchy]
    why = _closure_reason(conn, cfg, build, h)
    try:
        graph, sql_pairs, sql_members = owl.read(conn, cfg, closure_ready=not why)
    except owl.TooManyIndividuals as e:
        res.add("owl:consistency", "not_run", "ontology", reason="resource_limit",
                individuals=e.individuals, limit=e.limit)
        _v5_not_run(cfg, res, "resource_limit")
        res.not_proven.append(f"owl: {e.individuals} individuals exceed verify.owl_max_individuals = {e.limit}; the OWL "
                              "checks were not run")
        return
    except Exception as e:  # noqa: BLE001 - database errors become an error check with their SQLSTATE
        res.add("owl:consistency", "error", "ontology", sqlstate=_db_error(e))
        _v5_not_run(cfg, res, "owl_not_run")
        res.not_proven.append("v5: owl:consistency could not run, so the V5 comparisons were not run")
        return
    try:
        r = owl.reason(cfg, graph, java)
    except owl.ReasonerError as e:
        res.add("owl:consistency", "error", "ontology", reason="reasoner_error", exception=str(e))
        _v5_not_run(cfg, res, "owl_not_run")
        res.not_proven.append("v5: HermiT gave no answer, so the V5 comparisons were not run")
        return
    res.add("owl:consistency", "pass" if r.consistent else "fail", "ontology", individuals=r.individuals,
            node_rows=r.node_rows, outside_node_table=r.outside_node_table, edges=r.edges, hermit_ms=r.hermit_ms)
    if not r.consistent:
        _v5_not_run(cfg, res, "inconsistent")
        res.not_proven.append("v5: the ontology is inconsistent and so entails every statement; the V5 comparisons "
                              "were not run")
        return
    cid, ref = _v5_ids(cfg)[0]
    if why:
        res.add(cid, "not_run", ref, reason=why)
        res.not_proven.append(f"{cid}: not compared ({WHY[why].format(h=h.name)})")
    else:
        res.add(cid, "pass" if r.closure == sql_pairs else "fail", ref, owl_pairs=len(r.closure), sql_pairs=len(sql_pairs),
                owl_only=len(r.closure - sql_pairs), sql_only=len(sql_pairs - r.closure))
    for (cid, ref), d in zip(_v5_ids(cfg)[1:], cfg.ontology.defined):
        if d.name not in sql_members:
            res.add(cid, "not_run", ref, reason=why, kind=d.kind)
            res.not_proven.append(f"{cid}: not compared, its SQL definition reads the closure table "
                                  f"({WHY[why].format(h=h.name)})")
            continue
        found, defined = r.members[d.name], sql_members[d.name]
        res.add(cid, "pass" if found == defined else "fail", ref, kind=d.kind, owl_members=len(found),
                sql_members=len(defined), owl_only=len(found - defined), sql_only=len(defined - found),
                outside_node_table=r.outside[d.name])
        if not found and not defined:
            res.not_proven.append(f"{cid}: the class has no members in OWL or in SQL, so the comparison holds vacuously")


# ---- next action ------------------------------------------------------------------------------------------
def _error_action(c) -> str:
    code = c.detail.get("sqlstate")
    if code:
        hint = ("raise source.statement_timeout_ms" if code == "57014"
                else f"fix [{c.config_ref}] in crosscheck.toml or the check role's privileges")
        return f"{c.id} hit a database error (SQLSTATE {code}): {hint}, then rerun reason"
    if c.detail.get("reason") == "reasoner_error":
        return ("HermiT did not run: check Java with `sxc.py doctor --require reason` (SXC_JAVA or java on PATH, Java 11 "
                "or later), then rerun reason")
    return f"{c.id}: pySHACL could not validate build/shapes.ttl: rerun build, then reason"


def next_action(cfg, res: Result) -> str:
    """One sentence for the agent: the first error, else every disagreement to look at, else what blocks a check."""
    checks = [c for c in res.checks if not c.id.startswith("scope:")]
    narrowed = len(checks) < len(res.checks)
    if not checks:
        return ("nothing was compared (see not_proven): add [[shapes]] with a SHACL form or an [ontology] section, or "
                "raise the verify limits, then rerun reason" + (" without --only" if narrowed else ""))
    errors = [c for c in checks if c.status == "error"]
    if errors:
        return _error_action(errors[0])
    failed = [c for c in checks if c.status == "fail"]
    clauses = []
    if any(c.id == META_ID for c in failed):
        clauses.append("generated SHACL failed the syntax preflight: review the shapes generator and [[shapes]] "
                       "configuration, then rerun build")
    if any(c.id == "owl:consistency" for c in failed):
        h = cfg.hierarchies[cfg.ontology.hierarchy]
        clauses.append(f"the rows contradict the ontology (an item of an ontology.no_children class with a child, or a key "
                       f"with two disjoint classes): fix the rows of {cfg.entities[h.edge].table} or "
                       f"{cfg.entities[h.node].table}, or the [ontology] entry")
    views = [c.detail["view"] for c in failed if c.id.startswith("shacl:") and c.id != META_ID]
    if views:
        clauses.append(f"SHACL and SQL disagree on which rows violate a shape: compare the keys in {', '.join(views)} with "
                       "the rule (NULL handling, key types, numeric column types; see references/pitfalls.md), fix the "
                       "[[shapes]] entries and rerun build")
    for c in failed:
        if c.id.startswith("v5:closure:"):
            clauses.append(f"the OWL closure differs from {naming.closure_table(cfg, c.id.split(':', 2)[2])}: rerun build, "
                           "and report a defect with the counts if it persists")
    refs = [c.config_ref for c in failed if c.id.startswith("v5:defined:")]
    if refs:
        clauses.append(f"defined classes differ from their SQL definitions: check ontology.class_column, ontology.classes "
                       f"and the kind and class of {', '.join(refs)}")
    if any(c.id.startswith("v5:witness:") for c in failed):
        clauses.append("synthetic defined-class translations differ from the independent expected memberships: "
                       "review the OWL and SQL definitions named by the v5:witness checks")
    if clauses:
        return "; ".join(clauses) + "; then rerun reason"
    reasons = {c.detail.get("reason") for c in checks if c.status == "not_run"}
    if reasons & set(BUILD_REASONS):
        return "some checks need a passing build: fix the build failures, rerun `sxc.py build`, then rerun reason"
    if "resource_limit" in reasons:
        return ("selected checks exceeded a resource limit: review verify.shacl_max_rows, verify.owl_max_individuals "
                "and the input size, then rerun reason" + (" without --only" if narrowed else ""))
    if "cycle_edges_dropped" in reasons:
        return ("the closure leaves out the edges inside cycles (on_cycle = report): fix the cycles in the data so V5 "
                "compares the whole hierarchy, then rerun build and reason")
    if reasons & {"configuration_gap", "configured_class_unsatisfiable", "synthetic_inconsistent", "no_defined_classes"}:
        return ("some synthetic witnesses could not run with the configured ontology: review the v5:witness reasons "
                "and the referenced ontology.defined entries, then rerun reason")
    if narrowed:
        return ("--only left a part of reason out (the scope:* check): rerun reason without --only before reporting a "
                "result")
    return "run `sxc.py cube` to generate the Cube model and check that Cube serves it"
