"""build stage: shape violation views and hierarchy closures in the output schema.

Order of work:
  1. Everything derived from the configuration alone (every view's SQL, shapes.ttl, fk_review.sql) first, so a
     bad entry fails with exit 2 before the database is touched.
  2. Every source table and column the shapes and hierarchies name must exist (a typo is exit 2) and be readable
     by the check role (exit 3), checked in the catalogs before anything is built.
  3. The output schema must exist and accept new objects from the check role. It is created when missing and
     the role may create it; otherwise the stage stops with exit 3 and the statement an administrator should run.
  4. All shape views in one write transaction, each counted (shape:<id>). A warning shape passes and reports its
     count. A database error in one view (a broken trusted predicate, a timeout) becomes an `error` check with
     its SQLSTATE while the other views still build (shapes.create_views).
  5. Each hierarchy closure in its own write transaction (closure:<h>, closure_apply:<h>; see closure.py), so a
     failing hierarchy leaves the others built. With --hierarchy h every other hierarchy gets closure:<other>
     not_run (reason excluded_by_option): its closure was not rebuilt with the current data, so reason and verify
     must not take a partial build for a full one, and the build itself is no pass.
With [data_rules] views = true (only then) the rule file is loaded before connecting (exit 2), the columns its rules
read join the source check, one catalog query gives their type plan and every rule view is rendered before the output
schema is touched (exit 2 on a type-incompatible rule). After the shape views, one more write transaction (lock first)
drops v_rules_subjects, creates or replaces v_rule_<id> for every rule under a savepoint (marked by a fixed comment;
rule_view:<id>), drops the marked rule views the rule file no longer generates (never an unmarked object, never
CASCADE) and creates v_rules_subjects over the subject rules' views (rule_subjects:view). sxc.py data-rules certifies
those views; build records them in build.sql in literal form. Without [data_rules] nothing of this runs.
Two runs on the same output schema (two terminals, two agents) would otherwise race on the same views and closure
tables and fail with a unique violation: every write transaction starts by taking a transaction-level advisory
lock on the output schema, so concurrent builds wait for each other's transactions instead.
build.sql records every statement executed, for review. Results keep counts and configuration ids only: the
violating keys stay in the views and example cycles in naming.cycles_table.
"""

from pathlib import Path

from . import closure, config, db, ident, naming, shapes
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

BUILD_SQL_HEADER = """\
-- semantic-crosscheck build: every statement this run executed, in order (generated from crosscheck.toml).
-- For review only: change crosscheck.toml and rerun `sxc.py build` instead of editing generated objects.
-- Statements with :name placeholders ran with bound parameters. A shape's sql_violation predicate is trusted
-- SQL copied from the configuration.
"""
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"
EXCLUDED = "excluded_by_option"
CONCURRENT_SQLSTATES = ("23505", "40001", "40P01", "42P06", "42P07")   # unique, serialization, deadlock, duplicates
# exact-name catalog lookup: to_regclass() raises instead of answering when the role lacks USAGE on the schema
SOURCE_SQL = """SELECT c.oid IS NOT NULL AS found, has_schema_privilege(n.oid, 'USAGE') AS usage,
       c.oid IS NOT NULL AND has_table_privilege(c.oid, 'SELECT') AS readable,
       coalesce((SELECT array_agg(a.attname::text) FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '{}') AS columns
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :t AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
WHERE n.nspname = :s"""


def selected_hierarchies(cfg, name: str) -> list:
    """Every hierarchy, or only the one named by --hierarchy (an unknown name is a configuration error)."""
    if name:
        return [cfg.hierarchy(name, "--hierarchy")]
    return list(cfg.hierarchies.values())


def source_columns(cfg, hierarchies) -> list[tuple[str, str, list[str]]]:
    """(config_ref, table, columns) for every source table the shapes and the given hierarchies read."""
    needed = []
    for shape in cfg.shapes:
        target = cfg.entities[shape.target]
        ref = shapes.config_ref(cfg, shape)
        needed.append((ref, target.table, target.key + ([shape.path] if shape.path else [])))
        if shape.class_:
            needed.append((ref, cfg.entities[shape.class_].table, cfg.entities[shape.class_].key[:1]))
    for h in hierarchies:
        needed.append((f"hierarchies.{h.name}", cfg.entities[h.edge].table, [h.parent, h.child, h.quantity]))
    return needed


def check_sources(conn, cfg, hierarchies) -> None:
    """A missing table or column is a configuration error (exit 2); a table the check role cannot read is an
    environment error (exit 3). Columns used only inside a trusted sql_violation predicate are not checked."""
    check_tables(conn, source_columns(cfg, hierarchies))


def check_tables(conn, needed, origin: str = "crosscheck.toml") -> None:
    """check_sources for any (ref, table, columns) list; origin names the file the refs belong to (the data-rules
    stage checks the rule file's columns with it)."""
    found = {}
    for ref, table, cols in needed:
        if table not in found:
            schema, name = ident.split(table)
            r = db.rows(conn, SOURCE_SQL, s=schema, t=name)
            found[table] = r[0] if r else None
        info = found[table]
        if not info or not info["found"]:
            raise StageError(f"{origin} [{ref}]: table {table} does not exist in the connected database "
                             "(fix the entity, or load the data first)", EXIT_CONFIG)
        if not (info["usage"] and info["readable"]):
            raise StageError(f"the check role cannot read {table} (used by [{ref}]): grant it USAGE on the schema "
                             "and SELECT on the table", EXIT_ENV)
        missing = [c for c in cols if c not in info["columns"]]
        if missing:
            raise StageError(f"{origin} [{ref}]: column {missing[0]} not found in {table}", EXIT_CONFIG)


def _role_sql(name: str) -> str:
    """A role name written as an SQL identifier, for the statement suggested in a message."""
    try:
        return ident.check(name)
    except ident.IdentError:
        return '"' + name.replace('"', '""') + '"'


def lock_key(cfg) -> str:
    return f"semantic-crosscheck build {cfg.source.output_schema}"


def lock_output_schema(conn, cfg) -> str:
    """Wait for the output schema's build lock, held until the current transaction ends; returns the statement as
    build.sql records it. The wait counts against source.statement_timeout_ms like any statement."""
    conn.run(LOCK_SQL, key=lock_key(cfg))
    return f"{LOCK_SQL};  -- key = '{lock_key(cfg)}'"


def ensure_output_schema(conn, cfg) -> list[str]:
    """Create the output schema when it is missing and the check role may; require USAGE and CREATE on it.

    Returns the statements executed. Raises StageError(exit 3) naming the statement an administrator should run.
    A missing schema is created under the build lock, so two builds starting together do not both create it.
    """
    schema = cfg.source.output_schema
    role = _role_sql(str(db.scalar(conn, "SELECT current_user")))
    executed = []
    if not conn.run("SELECT 1 FROM pg_namespace WHERE nspname = :s", s=schema):
        with db.write_tx(conn, cfg.source.statement_timeout_ms):
            executed.append(lock_output_schema(conn, cfg))
            if not conn.run("SELECT 1 FROM pg_namespace WHERE nspname = :s", s=schema):
                stmt = f"CREATE SCHEMA {ident.quote(schema)}"
                try:
                    conn.run(stmt)
                except Exception as e:
                    code = shapes.sqlstate(e)
                    if not code:
                        raise
                    raise StageError(f"output schema {schema} is missing and the check role cannot create it (SQLSTATE "
                                     f"{code}); create it for the check role: CREATE SCHEMA {schema} AUTHORIZATION {role}",
                                     EXIT_ENV) from e
                executed.append(stmt + ";")
    usage, create = conn.run("SELECT has_schema_privilege(CAST(:s AS text), 'USAGE'), "
                             "has_schema_privilege(CAST(:s AS text), 'CREATE')", s=schema)[0]
    if not (usage and create):
        raise StageError(f"the check role needs USAGE and CREATE on output schema {schema}; "
                         f"make it the owner: ALTER SCHEMA {schema} OWNER TO {role}", EXIT_ENV)
    return executed


def build_shapes(conn, cfg, res: Result, executed: list[str]) -> None:
    """Every shape view in one write transaction; one shape:<id> check each. A database error outside the views'
    savepoints (the lock wait, the commit) rolls every view back and becomes an `error` check for each shape."""
    if not cfg.shapes:
        return
    try:
        with db.write_tx(conn, cfg.source.statement_timeout_ms):
            lock = lock_output_schema(conn, cfg)
            views = shapes.create_views(conn, cfg)
    except Exception as e:
        code = shapes.sqlstate(e)
        if not code:
            raise
        executed.append(f"-- shapes: SQLSTATE {code}, transaction rolled back (its statements are not recorded)")
        for shape in cfg.shapes:
            res.add(f"shape:{shape.id}", "error", shapes.config_ref(cfg, shape), sqlstate=code, violations=None,
                    severity=shape.severity, kind=shapes.kind(shape), view=naming.shape_view(cfg, shape.id))
        return
    executed += ["-- shapes (one transaction)", lock]
    for shape, view in zip(cfg.shapes, views):
        executed.append(view["sql"])
        detail = {"violations": view["violations"], "severity": shape.severity, "kind": shapes.kind(shape),
                  "view": naming.shape_view(cfg, shape.id)}
        if shape.sql_violation:
            detail["trusted_sql"] = True
        if view.get("sqlstate"):
            status = "error"
            detail["sqlstate"] = view["sqlstate"]
        elif view["violations"] == 0 or shape.severity == "warning":
            status = "pass"
        else:
            status = "fail"
        res.add(f"shape:{shape.id}", status, shapes.config_ref(cfg, shape), **detail)


def rule_file(cfg, out):
    """The validated rule file when build creates the data rule views ([data_rules] with views = true), else None.
    An invalid rule file is a configuration error (exit 2) found before the database is touched."""
    if getattr(cfg, "data_rules", None) is None or not cfg.data_rules.views:
        return None
    return config.load_data_rules(cfg, out)


def prepare_rule_views(conn, cfg, spec):
    """Before the output schema is touched: the columns the rules read must exist and be readable (exit 2 or 3), and
    every rule view is rendered from their catalog type plan (exit 2 on a type-incompatible rule). Returns the plan.
    No data-dependent gate runs here: data-rules applies them."""
    from . import stage_data_rules
    check_tables(conn, stage_data_rules.rule_sources(cfg, spec.rules), spec.label)
    plan, _catalog = stage_data_rules.read_type_plan(conn, cfg, spec)
    stage_data_rules.view_statements(cfg, spec, plan)
    return plan


def _rule_view(conn, cfg, spec, plan, rule, statement: str) -> tuple[str, str]:
    """Create or replace one rule view under a savepoint and mark it; (SQLSTATE or '', the statements as build.sql
    records them, the view statement verbatim). On an error the view is dropped, as shapes._create_view does."""
    from . import data_rules, data_rules_sql as dsql, stage_data_rules as sdr
    name = data_rules.view_name(cfg, rule.id)
    view = ident.qualified(name)
    log = [statement[:-1]]                 # the statement ends with ';', which the block below writes back
    code, _ = sdr.attempt(conn, [(log[0], {})])
    if code == "42P16":                    # the view's columns changed: replace it by drop and create
        log += [f"DROP VIEW {view}", f"CREATE VIEW {view} AS\n{dsql.view_query(cfg, spec, plan, rule)}"]
        code, _ = sdr.attempt(conn, [(log[1], {}), (log[2], {})])
    if not code:
        log.append(dsql.comment_statement(name))
        code, _ = sdr.attempt(conn, [(log[-1], {})])
    if code:
        log.append(f"DROP VIEW IF EXISTS {view}")
        sdr.attempt(conn, [(log[-1], {})])  # leave no stale or broken view behind
    return code, "\n".join(s + ";" for s in log)


def _subjects_view(conn, cfg, statement: str) -> tuple[str, str]:
    from . import data_rules, data_rules_sql as dsql, stage_data_rules as sdr
    name = data_rules.subjects_view_name(cfg)
    log = [statement[:-1]]
    code, _ = sdr.attempt(conn, [(log[0], {})])
    if not code:
        log.append(dsql.comment_statement(name))
        code, _ = sdr.attempt(conn, [(log[-1], {})])
    if code:
        log.append(dsql.drop_view_statement(name))
        sdr.attempt(conn, [(log[-1], {})])
    return code, "\n".join(s + ";" for s in log)


def build_rule_views(conn, cfg, spec, plan, res: Result, executed: list[str]) -> None:
    """Every data rule view in one write transaction (lock first): drop v_rules_subjects, create or replace
    v_rule_<id> per rule under a savepoint with its marker comment (rule_view:<id>), drop the marked rule views the rule
    file no longer generates, then create v_rules_subjects when every subject rule's view exists (rule_subjects:view).
    A rule reading a column of an unsupported type or a nondeterministic collation gets no view (not_run). A database
    error outside the savepoints rolls everything back and makes every check an error, as in build_shapes."""
    from . import data_rules, data_rules_sql as dsql, stage_data_rules as sdr
    unusable, views, subjects_sql = sdr.view_statements(cfg, spec, plan)
    subjects_view = data_rules.subjects_view_name(cfg)
    outcomes, blocks, kept, subjects_code = {}, [], 0, None
    try:
        with db.write_tx(conn, cfg.source.statement_timeout_ms):
            lock = lock_output_schema(conn, cfg)
            drop = dsql.drop_view_statement(subjects_view)
            conn.run(drop)
            blocks.append(drop + ";")
            for rule in spec.rules:
                if rule.id not in unusable:
                    outcomes[rule.id], block = _rule_view(conn, cfg, spec, plan, rule, views[rule.id])
                    blocks.append(block)
            created = {data_rules.VIEW_PREFIX + rule_id for rule_id, code in outcomes.items() if not code}
            text, params = dsql.orphan_statement(cfg).parameterized()
            for row in conn.run(text, **params) or []:
                statement = "" if row[0] in created else sdr.drop_statement(cfg, row[0])
                if not statement:
                    continue
                code, _ = sdr.attempt(conn, [(statement, {})])
                kept += bool(code)
                blocks.append(f"-- a marked rule view the rule file no longer generates was not dropped (SQLSTATE "
                              f"{code})" if code else statement + ";")
            if spec.subject_rules and subjects_sql and all(outcomes.get(r.id) == "" for r in spec.subject_rules):
                subjects_code, block = _subjects_view(conn, cfg, subjects_sql)
                blocks.append(block)
    except Exception as e:
        code = shapes.sqlstate(e)
        if not code:
            raise
        executed.append(f"-- data rule views: SQLSTATE {code}, transaction rolled back (its statements are not "
                        "recorded)")
        for rule in spec.rules:
            res.add(f"rule_view:{rule.id}", "error", rule.config_ref, sqlstate=code, kind=rule.kind,
                    view=data_rules.view_name(cfg, rule.id))
        if spec.subject_rules:
            res.add("rule_subjects:view", "error", "data_rules", sqlstate=code, view=subjects_view,
                    subject_rules=len(spec.subject_rules))
        return
    executed += ["-- data rule views (one transaction)", lock] + blocks
    for rule in spec.rules:
        detail = {"kind": rule.kind, "view": data_rules.view_name(cfg, rule.id)}
        if rule.id in unusable:
            res.add(f"rule_view:{rule.id}", "not_run", rule.config_ref, reason=unusable[rule.id], **detail)
        elif outcomes[rule.id]:
            res.add(f"rule_view:{rule.id}", "error", rule.config_ref, sqlstate=outcomes[rule.id], **detail)
        else:
            res.add(f"rule_view:{rule.id}", "pass", rule.config_ref, **detail)
    if spec.subject_rules:
        detail = {"view": subjects_view, "subject_rules": len(spec.subject_rules)}
        if subjects_code is None:
            res.add("rule_subjects:view", "not_run", "data_rules",
                    reason="rule_view_missing" if subjects_sql else "column_type", **detail)
        elif subjects_code:
            res.add("rule_subjects:view", "error", "data_rules", sqlstate=subjects_code, **detail)
        else:
            res.add("rule_subjects:view", "pass", "data_rules", **detail)
    if kept:
        res.not_proven.append(f"rule views: {kept} marked view(s) the rule file no longer generates could not be "
                              "dropped (another object depends on them); drop them by hand")


def build_closure(conn, cfg, h, res: Result, executed: list[str]) -> None:
    """The closure of one hierarchy in its own write transaction; a database error becomes an `error` check."""
    ref = f"hierarchies.{h.name}"
    try:
        with db.write_tx(conn, cfg.source.statement_timeout_ms):
            lock = lock_output_schema(conn, cfg)
            checks, sql_log = closure.build(conn, cfg, h)
    except Exception as e:
        code = shapes.sqlstate(e)
        if not code:
            raise
        res.add(f"closure:{h.name}", "error", ref, sqlstate=code, closure_applied=False)
        executed.append(f"-- hierarchy {h.name}: SQLSTATE {code}, transaction rolled back (its statements are not recorded)")
        return
    executed += [f"-- hierarchy {h.name} (one transaction)", lock]
    executed.extend(sql_log)
    for check_id, status, config_ref, detail in checks:
        res.add(check_id, status, config_ref, **detail)
        if detail.get("dropped_cycle_edges"):
            res.not_proven.append(f"closure:{h.name}: {detail['dropped_cycle_edges']} edges inside cycles are left out "
                                  f"of the closure (on_cycle = report)")


def exclude_hierarchies(cfg, built: list, res: Result) -> None:
    """closure:<h> not_run for every hierarchy that --hierarchy left out of this run."""
    names = {h.name for h in built}
    for name in cfg.hierarchies:
        if name not in names:
            res.add(f"closure:{name}", "not_run", f"hierarchies.{name}", reason=EXCLUDED, closure_applied=False)


def error_hint(cfg, c) -> str:
    """What to do about a database error of a shape or closure check, by SQLSTATE."""
    code = c.detail.get("sqlstate", "")
    where = "the rule file" if c.id.startswith(("rule_view:", "rule_subjects:")) else "crosscheck.toml"
    if code == "57014":
        return ("raise source.statement_timeout_ms or simplify the entry (waiting for another build of the same output "
                "schema counts against the timeout too)")
    if code in CONCURRENT_SQLSTATES:
        return f"another run was writing output schema {cfg.source.output_schema} at the same time: rerun build"
    if code == "42501":
        needs = ("TEMPORARY on the database for the closure's temporary tables, " if c.id.startswith("closure:") else "")
        return (f"the check role lacks a privilege the build needs ({needs}SELECT on the source tables or CREATE on the "
                f"output schema): run `sxc.py doctor --config` to see which, or fix [{c.config_ref}] in {where}")
    return f"fix [{c.config_ref}] in {where} or the check role's privileges"


def next_action(cfg, res: Result) -> str:
    """One sentence for the agent: the first database error, else every failure to look at, else the next stage;
    when build created data rule views, data-rules comes next as well."""
    action = _next_action(cfg, res)
    if any(c.id.startswith("rule_view:") and c.status == "pass" for c in res.checks):
        action += "; run `sxc.py data-rules` to cross-check the data rules and their views"
    return action


def _next_action(cfg, res: Result) -> str:
    if not res.checks:
        return "the configuration has no [[shapes]] or [hierarchies] entries to build: add some, then rerun build"
    errors = [c for c in res.checks if c.status == "error"]
    if errors:
        c = errors[0]
        return f"{c.id} hit a database error (SQLSTATE {c.detail.get('sqlstate', '')}): {error_hint(cfg, c)}, then rerun build"
    clauses = []
    for c in res.checks:
        kind, _, name = c.id.partition(":")
        if c.status != "fail" or kind not in ("closure", "closure_apply"):
            continue
        h = cfg.hierarchies[name]
        edges = cfg.entities[h.edge].table
        if kind == "closure_apply":
            clauses.append(f"the closure table of {name} differs from its recomputation: rerun build and report a defect if it persists")
        elif c.detail.get("reason") == "cycle":
            clauses.append(f"hierarchy {name} has cycles: inspect {naming.cycles_table(cfg, name)}, then fix the rows of "
                           f"{edges} or set hierarchies.{name}.on_cycle = \"report\"")
        else:
            clauses.append(f"hierarchy {name} is deeper than max_depth = {h.max_depth}: raise hierarchies.{name}.max_depth "
                           f"(at most 64) or fix the rows of {edges}")
    views = [c.detail["view"] for c in res.checks if c.status == "fail" and c.id.startswith("shape:")]
    if views:
        clauses.append(f"shapes failed: inspect the violating keys in {', '.join(views)}, then fix the data or the [[shapes]] entries")
    unbuilt = [c.id.partition(":")[2] for c in res.checks if c.status == "not_run" and c.id.startswith("rule_view:")]
    if unbuilt:
        clauses.append(f"no view was built for the data rules {', '.join(unbuilt)}: they read a column of an "
                       "unsupported type or a nondeterministic collation, so cast the column in a database view, "
                       "change the rule, or set data_rules.views = false")
    elif any(c.id == "rule_subjects:view" and c.status == "not_run" for c in res.checks):
        clauses.append("v_rules_subjects was not built because the node key has no supported type: cast it in a "
                       "database view, or set data_rules.views = false")
    excluded = [c.id.partition(":")[2] for c in res.checks if c.detail.get("reason") == EXCLUDED]
    if clauses and excluded:
        return "; ".join(clauses) + "; then rerun build without --hierarchy, so every closure is current"
    if clauses:
        return "; ".join(clauses) + "; then rerun build"
    if excluded:
        return (f"this run left {', '.join(excluded)} out (--hierarchy): rerun build without --hierarchy before reason, "
                "cube and verify, so every closure is current")
    return "run `sxc.py reason` to cross-check the shapes and the hierarchy with SHACL and OWL, then `sxc.py cube`"


def write_files(out: Path, executed: list[str], ttl: str, fk_sql: str) -> None:
    d = Path(out) / "build"
    d.mkdir(parents=True, exist_ok=True)
    (d / "build.sql").write_text(BUILD_SQL_HEADER + "\n" + "\n\n".join(executed) + "\n", encoding="utf-8", newline="\n")
    (d / "shapes.ttl").write_text(ttl, encoding="utf-8", newline="\n")
    fk = d / "fk_review.sql"
    if fk_sql:
        fk.write_text(fk_sql, encoding="utf-8", newline="\n")
    else:
        fk.unlink(missing_ok=True)   # a file left by an earlier run would propose constraints no shape asks for


def run(cfg, args, out: Path) -> Result:
    res = Result("build")
    hierarchies = selected_hierarchies(cfg, getattr(args, "hierarchy", "") or "")
    for shape in cfg.shapes:
        shapes.violation_sql(cfg, shape)   # a bad entry stops here with exit 2, before the database is touched
    ttl, _no_shacl = shapes.shapes_ttl(cfg)   # shapes without a SHACL form are reported by the reason stage
    fk_sql = shapes.fk_review_sql(cfg) if any(s.fk_ddl == "review_only" for s in cfg.shapes) else ""
    rules = rule_file(cfg, out)        # [data_rules] views = true only; an invalid rule file stops here with exit 2
    executed: list[str] = []
    conn = db.connect(cfg.source.dsn_env)
    try:
        check_sources(conn, cfg, hierarchies)
        plan = prepare_rule_views(conn, cfg, rules) if rules is not None else None
        executed += ensure_output_schema(conn, cfg)
        build_shapes(conn, cfg, res, executed)
        if rules is not None:
            build_rule_views(conn, cfg, rules, plan, res, executed)
        for h in hierarchies:
            build_closure(conn, cfg, h, res, executed)
    finally:
        conn.close()
    exclude_hierarchies(cfg, hierarchies, res)
    write_files(out, executed, ttl, fk_sql)
    res.next_safe_action = next_action(cfg, res)
    return res
