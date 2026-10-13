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
     failing hierarchy leaves the others built.
build.sql records every statement executed, for review. Results keep counts and configuration ids only: the
violating keys stay in the views and example cycles in naming.cycles_table.
"""

from pathlib import Path

from . import closure, db, ident, naming, shapes
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

BUILD_SQL_HEADER = """\
-- semantic-crosscheck build: every statement this run executed, in order (generated from crosscheck.toml).
-- For review only: change crosscheck.toml and rerun `sxc.py build` instead of editing generated objects.
-- Statements with :name placeholders ran with bound parameters. A shape's sql_violation predicate is trusted
-- SQL copied from the configuration.
"""
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
    found = {}
    for ref, table, cols in source_columns(cfg, hierarchies):
        if table not in found:
            schema, name = ident.split(table)
            r = db.rows(conn, SOURCE_SQL, s=schema, t=name)
            found[table] = r[0] if r else None
        info = found[table]
        if not info or not info["found"]:
            raise StageError(f"crosscheck.toml [{ref}]: table {table} does not exist in the connected database "
                             "(fix the entity, or load the data first)", EXIT_CONFIG)
        if not (info["usage"] and info["readable"]):
            raise StageError(f"the check role cannot read {table} (used by [{ref}]): grant it USAGE on the schema "
                             "and SELECT on the table", EXIT_ENV)
        missing = [c for c in cols if c not in info["columns"]]
        if missing:
            raise StageError(f"crosscheck.toml [{ref}]: column {missing[0]} not found in {table}", EXIT_CONFIG)


def _role_sql(name: str) -> str:
    """A role name written as an SQL identifier, for the statement suggested in a message."""
    try:
        return ident.check(name)
    except ident.IdentError:
        return '"' + name.replace('"', '""') + '"'


def ensure_output_schema(conn, cfg) -> list[str]:
    """Create the output schema when it is missing and the check role may; require USAGE and CREATE on it.

    Returns the statements executed. Raises StageError(exit 3) naming the statement an administrator should run.
    """
    schema = cfg.source.output_schema
    role = _role_sql(str(db.scalar(conn, "SELECT current_user")))
    executed = []
    if not conn.run("SELECT 1 FROM pg_namespace WHERE nspname = :s", s=schema):
        stmt = f"CREATE SCHEMA {ident.quote(schema)}"
        try:
            conn.run(stmt)
        except Exception as e:
            code = shapes.sqlstate(e)
            if not code:
                raise
            raise StageError(f"output schema {schema} is missing and the check role cannot create it (SQLSTATE {code}); "
                             f"create it for the check role: CREATE SCHEMA {schema} AUTHORIZATION {role}", EXIT_ENV) from e
        executed.append(stmt + ";")
    usage, create = conn.run("SELECT has_schema_privilege(CAST(:s AS text), 'USAGE'), "
                             "has_schema_privilege(CAST(:s AS text), 'CREATE')", s=schema)[0]
    if not (usage and create):
        raise StageError(f"the check role needs USAGE and CREATE on output schema {schema}; "
                         f"make it the owner: ALTER SCHEMA {schema} OWNER TO {role}", EXIT_ENV)
    return executed


def build_shapes(conn, cfg, res: Result, executed: list[str]) -> None:
    """Every shape view in one write transaction; one shape:<id> check each."""
    if not cfg.shapes:
        return
    with db.write_tx(conn, cfg.source.statement_timeout_ms):
        views = shapes.create_views(conn, cfg)
    executed.append("-- shapes (one transaction)")
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


def build_closure(conn, cfg, h, res: Result, executed: list[str]) -> None:
    """The closure of one hierarchy in its own write transaction; a database error becomes an `error` check."""
    ref = f"hierarchies.{h.name}"
    try:
        with db.write_tx(conn, cfg.source.statement_timeout_ms):
            checks, sql_log = closure.build(conn, cfg, h)
    except Exception as e:
        code = shapes.sqlstate(e)
        if not code:
            raise
        res.add(f"closure:{h.name}", "error", ref, sqlstate=code, closure_applied=False)
        executed.append(f"-- hierarchy {h.name}: SQLSTATE {code}, transaction rolled back (its statements are not recorded)")
        return
    executed.append(f"-- hierarchy {h.name} (one transaction)")
    executed.extend(sql_log)
    for check_id, status, config_ref, detail in checks:
        res.add(check_id, status, config_ref, **detail)
        if detail.get("dropped_cycle_edges"):
            res.not_proven.append(f"closure:{h.name}: {detail['dropped_cycle_edges']} edges inside cycles are left out "
                                  f"of the closure (on_cycle = report)")


def next_action(cfg, res: Result) -> str:
    """One sentence for the agent: the first database error, else every failure to look at, else the next stage."""
    if not res.checks:
        return "the configuration has no [[shapes]] or [hierarchies] entries to build: add some, then rerun build"
    errors = [c for c in res.checks if c.status == "error"]
    if errors:
        c = errors[0]
        code = c.detail.get("sqlstate", "")
        hint = ("raise source.statement_timeout_ms or simplify the entry" if code == "57014"
                else f"fix [{c.config_ref}] in crosscheck.toml or the check role's privileges")
        return f"{c.id} hit a database error (SQLSTATE {code}): {hint}, then rerun build"
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
    if clauses:
        return "; ".join(clauses) + "; then rerun build"
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
    executed: list[str] = []
    conn = db.connect(cfg.source.dsn_env)
    try:
        check_sources(conn, cfg, hierarchies)
        executed += ensure_output_schema(conn, cfg)
        build_shapes(conn, cfg, res, executed)
        for h in hierarchies:
            build_closure(conn, cfg, h, res, executed)
    finally:
        conn.close()
    write_files(out, executed, ttl, fk_sql)
    res.next_safe_action = next_action(cfg, res)
    return res
