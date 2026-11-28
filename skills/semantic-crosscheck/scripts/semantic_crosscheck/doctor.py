"""doctor stage: is this machine and this database ready for the harness?

Doctor runs before anything else and always writes a complete result: a missing DSN or an unreachable server
becomes failing checks (and the database checks that could not run are `not_run`), never an exception, so one run
lists everything that needs fixing.

  python, pg8000, dsn, connect, pg_version   Python >= 3.11; pg8000 importable; the check-role DSN variable
                                              (source.dsn_env, or SXC_PG_DSN without a configuration) is set; a
                                              connection opens; server_version_num >= 170000
  privileges:temporary                        the check role may create temporary tables in the database (build's
                                              closure works in temporary tables)
With a configuration it also proves the security model of the check role (design section 3):
  privileges:select:<entity>      USAGE on the schema and SELECT on the source table
  privileges:no_write:<entity>    the check role cannot change the source table or its schema: no INSERT or UPDATE
                                  (on any column), DELETE, TRUNCATE or TRIGGER on the table; not the owner of the table
                                  or of its schema, nor a member of either owner's role (a member could SET ROLE to the
                                  owner, and a schema owner can drop every table in the schema); no CREATE on the schema
  output_schema                   the output schema exists with CREATE and USAGE, or the role may create it
Privileges are read by OID from the catalogs inside a read-only transaction, so a missing schema or table is
reported, never raised. A missing table gets its own hint: run the demo stage first (demo schema) or check
entities.<name>.table, instead of a grant the role may already hold.

Optional components named in --require become checks; the others are probed and, when missing, go to not_proven:
  reason:owlready2, reason:pyshacl, reason:java   importable packages; `java -version` (SXC_JAVA or PATH) is 11+
  cube:node, cube:adapter, cube:native            node >= cube_install.MIN_NODE on PATH; the adapter in cube_home(),
                                                  installed from the current adapter files; index.node with the
                                                  SHA-256 recorded in installed.json and the pinned tarball
Programs are looked up on PATH only (procutil.which), never in the current directory. Install hints name the
interpreter running doctor, the requirements files and sxc.py by absolute path, derived from where this plugin is
installed, so they work from any working directory (a relative requirements/core.txt only resolves from the plugin
root). They are the only local paths doctor writes; everything else names its source (SXC_CUBE_HOME or default).
"""

import contextlib
import importlib
import importlib.metadata
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from . import config, coverage_matrix, cube_install, cube_run, db, demo, ident, procutil
from .result import EXIT_CONFIG, Result, StageError

COMPONENTS = ("core", "reason", "cube")
MIN_PYTHON, MIN_PG, MIN_JAVA = (3, 11), 170000, 11
MIN_PG_NULLS_NOT_DISTINCT = 150000
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER")
PROBE_TIMEOUT_S = 60
PLUGIN_ROOT = Path(__file__).resolve().parents[4]          # holds requirements/ (the repository root)


def _privileges_sql(server_version_num: int = MIN_PG) -> str:
    """Catalog privilege, RLS, index and relation-hygiene query for one relation."""
    nulls_not_distinct = (
        "coalesce((SELECT bool_or(i.indisunique AND i.indisvalid AND i.indisready "
        "AND i.indpred IS NULL AND i.indexprs IS NULL AND i.indnullsnotdistinct) "
        "FROM pg_catalog.pg_index i WHERE i.indrelid = c.oid), false)"
        if int(server_version_num) >= MIN_PG_NULLS_NOT_DISTINCT
        else "false"
    )
    return f"""
    SELECT n.oid IS NOT NULL AS schema_exists, c.oid IS NOT NULL AS table_exists,
           coalesce(c.relkind::text, '') AS relkind,
           coalesce(c.relpersistence::text, 'p') AS relpersistence,
           coalesce(c.relispopulated, true) AS is_populated,
           coalesce(c.relhasrules, false) AS has_rules,
           coalesce(c.relkind = 'v' AND EXISTS (
             SELECT 1 FROM unnest(coalesce(c.reloptions, '{{}}'::text[])) opt
             WHERE lower(opt) IN (
               'security_invoker=true', 'security_invoker=on', 'security_invoker=1',
               'security_invoker=yes', 'security_invoker=y', 'security_invoker=t'
             )
           ), false) AS view_security_invoker,
           coalesce(c.relkind = 'v' AND EXISTS (
             SELECT 1 FROM unnest(coalesce(c.reloptions, '{{}}'::text[])) opt
             WHERE lower(opt) IN (
               'security_barrier=true', 'security_barrier=on', 'security_barrier=1',
               'security_barrier=yes', 'security_barrier=y', 'security_barrier=t'
             )
           ), false) AS view_security_barrier,
           coalesce(has_schema_privilege(n.oid, 'USAGE'), false) AS can_use,
           coalesce(has_schema_privilege(n.oid, 'CREATE'), false) AS schema_create,
           coalesce(pg_has_role(n.nspowner, 'MEMBER'), false) AS schema_owner,
           coalesce(has_table_privilege(c.oid, 'SELECT'), false) AS can_select,
           coalesce(has_any_column_privilege(c.oid, 'INSERT'), false) AS can_insert,
           coalesce(has_any_column_privilege(c.oid, 'UPDATE'), false) AS can_update,
           coalesce(has_table_privilege(c.oid, 'DELETE'), false) AS can_delete,
           coalesce(has_table_privilege(c.oid, 'TRUNCATE'), false) AS can_truncate,
           coalesce(has_table_privilege(c.oid, 'TRIGGER'), false) AS can_trigger,
           coalesce(pg_has_role(c.relowner, 'MEMBER'), false) AS member_of_owner,
           coalesce(c.relrowsecurity, false) AS row_security,
           coalesce(c.relforcerowsecurity, false) AS force_row_security,
           coalesce((SELECT r.rolbypassrls FROM pg_catalog.pg_roles r WHERE r.rolname = current_user), false) AS bypass_rls,
           {nulls_not_distinct} AS unique_nulls_not_distinct,
           coalesce((SELECT count(*) FROM pg_catalog.pg_constraint con
                     WHERE con.conrelid = c.oid AND con.contype IN ('c', 'f') AND NOT con.convalidated), 0)::integer
             AS unvalidated_constraints,
           coalesce((SELECT count(*) FROM pg_catalog.pg_index i
                     WHERE i.indrelid = c.oid AND (NOT i.indisvalid OR NOT i.indisready)), 0)::integer
             AS invalid_indexes,
           coalesce((SELECT count(*) FROM pg_catalog.pg_attribute a
                     WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                       AND a.attgenerated <> ''), 0)::integer
             AS generated_columns,
           coalesce((SELECT count(DISTINCT col.oid)
                     FROM pg_catalog.pg_attribute a
                     JOIN pg_catalog.pg_collation col ON col.oid = a.attcollation
                     WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                       AND col.collversion IS NOT NULL
                       AND col.collversion IS DISTINCT FROM pg_collation_actual_version(col.oid)), 0)::integer
             AS mismatched_column_collations,
           coalesce(children.child_partition_row_security, false) AS child_partition_row_security,
           coalesce(children.child_foreign_partitions, false) AS child_foreign_partitions,
           coalesce(children.child_unlogged_partitions, false) AS child_unlogged_partitions,
           coalesce(children.child_unvalidated_constraints, 0)::integer AS child_unvalidated_constraints,
           coalesce(children.child_invalid_indexes, 0)::integer AS child_invalid_indexes,
           coalesce(vdeps.view_volatile_functions, 0)::integer AS view_volatile_functions,
           coalesce(vdeps.view_dep_rls_tables, false) AS view_dep_rls_tables,
           coalesce(vdeps.view_dep_unlogged_tables, false) AS view_dep_unlogged_tables,
           coalesce(vdeps.view_dep_foreign_tables, false) AS view_dep_foreign_tables
    FROM (SELECT 1) AS one
    LEFT JOIN pg_namespace n ON n.nspname = :schema
    LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :table AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    LEFT JOIN LATERAL (
      WITH RECURSIVE parts(oid) AS (
        SELECT inh.inhrelid FROM pg_catalog.pg_inherits inh WHERE inh.inhparent = c.oid
        UNION
        SELECT inh.inhrelid FROM pg_catalog.pg_inherits inh JOIN parts p ON inh.inhparent = p.oid
      )
      SELECT bool_or(ch.relrowsecurity) AS child_partition_row_security,
             bool_or(ch.relkind = 'f') AS child_foreign_partitions,
             bool_or(ch.relpersistence = 'u') AS child_unlogged_partitions,
             (SELECT count(*)
              FROM pg_catalog.pg_constraint con
              WHERE con.conrelid IN (SELECT oid FROM parts)
                AND con.contype IN ('c', 'f')
                AND con.coninhcount = 0
                AND NOT con.convalidated) AS child_unvalidated_constraints,
             (SELECT count(*)
              FROM pg_catalog.pg_index i
              WHERE i.indrelid IN (SELECT oid FROM parts)
                AND (NOT i.indisvalid OR NOT i.indisready)) AS child_invalid_indexes
      FROM parts p
      JOIN pg_catalog.pg_class ch ON ch.oid = p.oid
    ) AS children ON c.oid IS NOT NULL
    LEFT JOIN LATERAL (
      WITH RECURSIVE vrel(oid) AS (
        SELECT c.oid WHERE c.relkind = 'v'
        UNION
        SELECT dep.refobjid
        FROM vrel vr
        JOIN pg_catalog.pg_class vc ON vc.oid = vr.oid AND vc.relkind = 'v'
        JOIN pg_catalog.pg_rewrite rw ON rw.ev_class = vc.oid AND rw.ev_type = '1'
        JOIN pg_catalog.pg_depend dep ON dep.objid = rw.oid
          AND dep.classid = 'pg_catalog.pg_rewrite'::regclass
          AND dep.refclassid = 'pg_catalog.pg_class'::regclass
          AND dep.deptype = 'n'
          AND dep.refobjid <> vr.oid
      ),
      vrel_all(oid) AS (
        SELECT vr.oid FROM vrel vr WHERE vr.oid <> c.oid
        UNION
        SELECT inh.inhrelid
        FROM vrel_all va
        JOIN pg_catalog.pg_inherits inh ON inh.inhparent = va.oid
      ),
      vfuncs AS (
        SELECT DISTINCT dep.refobjid AS func_oid
        FROM vrel vr
        JOIN pg_catalog.pg_class vc ON vc.oid = vr.oid AND vc.relkind = 'v'
        JOIN pg_catalog.pg_rewrite rw ON rw.ev_class = vc.oid AND rw.ev_type = '1'
        JOIN pg_catalog.pg_depend dep ON dep.objid = rw.oid
          AND dep.classid = 'pg_catalog.pg_rewrite'::regclass
          AND dep.refclassid = 'pg_catalog.pg_proc'::regclass
          AND dep.deptype = 'n'
      )
      SELECT (SELECT count(*)
              FROM vfuncs vf
              JOIN pg_catalog.pg_proc pr ON pr.oid = vf.func_oid
              WHERE pr.provolatile = 'v') AS view_volatile_functions,
             bool_or(dc.relrowsecurity) AS view_dep_rls_tables,
             bool_or(dc.relpersistence = 'u') AS view_dep_unlogged_tables,
             bool_or(dc.relkind = 'f') AS view_dep_foreign_tables
      FROM vrel_all va
      JOIN pg_catalog.pg_class dc ON dc.oid = va.oid
    ) AS vdeps ON c.oid IS NOT NULL AND c.relkind = 'v'
"""


_DB_COLLATION_VERSION_SQL = """
    SELECT coalesce(d.datcollversion IS DISTINCT FROM pg_database_collation_actual_version(d.oid), false)
             AS db_collation_version_mismatch
    FROM pg_catalog.pg_database d
    WHERE d.datname = current_database()
"""


_OUTPUT_SCHEMA_SQL = """
    SELECT n.oid IS NOT NULL AS schema_exists,
           coalesce(has_schema_privilege(n.oid, 'CREATE'), false) AS can_create,
           coalesce(has_schema_privilege(n.oid, 'USAGE'), false) AS can_use,
           has_database_privilege(current_database(), 'CREATE') AS may_create_schema
    FROM (SELECT 1) AS one
    LEFT JOIN pg_namespace n ON n.nspname = :schema
"""


# ---- version parsing (pure functions, unit tested with fakes) -----------------------------------------
def python_ok(version_info) -> bool:
    return tuple(version_info[:2]) >= MIN_PYTHON


def pg_version_ok(server_version_num: int) -> bool:
    return int(server_version_num) >= MIN_PG


def parse_java_major(text: str) -> int | None:
    """Major version from `java -version` output ('1.8.0_381' -> 8, '21.0.2' -> 21, '23-ea' -> 23)."""
    m = re.search(r'version\s+"(\d+)(?:\.(\d+))?', text) or re.search(r"^(?:openjdk|java)\s+(\d+)(?:\.(\d+))?", text, re.M)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2)) if major == 1 and m.group(2) else major


def parse_node_version(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ---- install hints ------------------------------------------------------------------------------------
# Results never carry local paths, so hints use the tokens <python> (the interpreter running doctor) and <plugin>
# (the plugin root); sxc.py expands them when it prints, which gives commands that work from any directory.
def pip_hint(requirements: str) -> str:
    """The pip command that installs requirements/<requirements>.txt into the interpreter running doctor."""
    if not (PLUGIN_ROOT / "requirements" / f"{requirements}.txt").is_file():
        return f"the installation is incomplete (requirements/{requirements}.txt is missing): reinstall semantic-crosscheck"
    return f'<python> -m pip install -r "<plugin>/requirements/{requirements}.txt"'


def sxc_hint(arguments: str) -> str:
    """An sxc.py command line with the interpreter running doctor and the plugin's sxc.py."""
    return f'<python> "<plugin>/skills/semantic-crosscheck/scripts/sxc.py" {arguments}'


# ---- component probes: (ok, detail, hint) --------------------------------------------------------------
def _dist_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def probe_module(name: str, requirements: str = "reason") -> tuple[bool, dict, str]:
    try:
        importlib.import_module(name)
    except Exception as e:  # noqa: BLE001 - any import failure means the component cannot be used
        return False, {"error": type(e).__name__}, pip_hint(requirements)
    return True, {"version": _dist_version(name)}, ""


def _run(runner, argv: list[str]):
    return runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL)


def probe_java(env=None, which=procutil.which, runner=subprocess.run) -> tuple[bool, dict, str]:
    """SXC_JAVA (an absolute path, or a command name looked up on PATH) when set, else java on PATH."""
    env = os.environ if env is None else env
    hint = (f"install Java {MIN_JAVA} or newer, then set SXC_JAVA to the absolute path of the java executable or put "
            "java on PATH")
    configured = env.get("SXC_JAVA")
    source = "SXC_JAVA" if configured else "PATH"
    java = which(configured or "java")
    if not java:
        return False, {"found": False, "source": source}, hint
    try:
        p = _run(runner, [java, "-version"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, {"found": True, "source": source, "error": type(e).__name__}, hint
    major = parse_java_major(f"{p.stderr or ''}\n{p.stdout or ''}")
    ok = p.returncode == 0 and major is not None and major >= MIN_JAVA
    return ok, {"found": True, "source": source, "major": major, "exit_code": p.returncode}, "" if ok else hint


def probe_node(which=procutil.which, runner=subprocess.run) -> tuple[bool, dict, str]:
    minimum = cube_install.MIN_NODE
    hint = f"install Node.js {minimum} or newer and put node on PATH"
    node = which("node")
    if not node:
        return False, {"found": False}, hint
    try:
        p = _run(runner, [node, "--version"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, {"found": True, "error": type(e).__name__}, hint
    v = parse_node_version(p.stdout or "")
    ok = p.returncode == 0 and v is not None and v[0] >= minimum
    return ok, {"found": True, "version": ".".join(map(str, v)) if v else "", "exit_code": p.returncode}, "" if ok else hint


def _home_source() -> str:
    return "SXC_CUBE_HOME" if os.environ.get("SXC_CUBE_HOME") else "default"


def _install_record(home: Path) -> dict | None:
    rec, _integrity = cube_run.load_install_record(home)
    return rec


def changed_adapter_files(home: Path, adapter_dir: Path = cube_run.ADAPTER_DIR) -> list[str] | None:
    """Adapter files (server.js, package.json, package-lock.json) whose hash differs between installed.json and the
    plugin's adapters/cube; None when home holds no server.js or no readable record of the installed files."""
    rec = _install_record(home) if (home / "server.js").is_file() else None
    recorded = rec.get("files_sha256") if rec else None
    if not isinstance(recorded, dict):
        return None
    current = cube_install.adapter_hashes(adapter_dir)
    return [name for name in current if recorded.get(name) != current[name]]


def probe_adapter(home: Path, adapter_dir: Path = cube_run.ADAPTER_DIR) -> tuple[bool, dict, str]:
    """The adapter is installed in home, from the adapter files this plugin ships now (installed.json files_sha256):
    after an upgrade that changed server.js or the lockfile, the old install would keep serving the old adapter."""
    install = f"run {sxc_hint('cube-install')}"
    reinstall = f"run {sxc_hint('cube-install --force')}"
    _rec, integrity = cube_run.load_install_record(home)
    if integrity == "manifest_digest_mismatch":
        return False, {"installed": (home / "server.js").is_file(), "reason": "manifest_digest_mismatch",
                       "home": _home_source()}, reinstall
    try:
        changed = changed_adapter_files(home, adapter_dir)
    except OSError:
        broken = "the plugin's adapters/cube files are missing: reinstall semantic-crosscheck"
        return False, {"installed": False, "reason": "adapter_files_missing"}, broken
    if changed:
        rerun = f"the installed adapter was built from other adapter files than this plugin ships: {install}"
        return False, {"installed": True, "reason": "adapter_files_changed", "changed": ",".join(changed),
                       "home": _home_source()}, rerun
    if changed == [] and cube_run.installed(home):
        return True, {"installed": True, "reason": "current", "home": _home_source()}, ""
    if not (home / "server.js").is_file():
        reason = "missing"
    else:
        reason = "no_install_record" if changed is None else "incomplete"
    return False, {"installed": False, "reason": reason, "home": _home_source()}, install


def probe_native(home: Path) -> tuple[bool, dict, str]:
    """index.node on disk hashes to the value cube-install recorded, and that record matches this platform's pin."""
    reinstall = f"run {sxc_hint('cube-install --force')} to reinstall the verified native module"
    rec, integrity = cube_run.load_install_record(home)
    if integrity == "manifest_digest_mismatch":
        return False, {"reason": "manifest_digest_mismatch", "home": _home_source()}, reinstall
    if rec is None:
        return False, {"reason": "no_install_record", "home": _home_source()}, f"run {sxc_hint('cube-install')}"
    try:
        recorded = rec["native"]["index_node_sha256"]
    except (KeyError, TypeError):
        return False, {"reason": "no_install_record", "home": _home_source()}, f"run {sxc_hint('cube-install')}"
    index = home / cube_install.INDEX_NODE
    if not index.is_file():
        return False, {"reason": "missing"}, reinstall
    if cube_install.sha256_file(index) != recorded:
        return False, {"reason": "hash_mismatch"}, reinstall
    try:
        key = cube_install.platform_key()
        pinned = cube_install.pinned_asset(cube_install.load_pins(), key)["sha256"]
    except (StageError, OSError, ValueError):
        return False, {"reason": "no_pin"}, reinstall
    if rec.get("platform") != key or rec["native"].get("tarball_sha256") != pinned:
        return False, {"reason": "pin_mismatch"}, reinstall
    return True, {"reason": "verified", "platform": key}, ""


# ---- database checks ---------------------------------------------------------------------------------
def _st(ok) -> str:
    return "pass" if ok else "fail"


def _sqlstate(e: BaseException | None) -> str:
    arg = e.args[0] if e is not None and e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def _db_checks(cfg) -> list[tuple[str, str]]:
    """(check id, config_ref) of every check that needs a connection, in report order."""
    ref = "source.dsn_env" if cfg else ""
    ids = [("connect", ref), ("pg_version", ref), ("privileges:temporary", ref)]
    if cfg:
        ids += [(f"privileges:select:{e}", f"entities.{e}") for e in cfg.entities]
        ids += [(f"privileges:no_write:{e}", f"entities.{e}") for e in cfg.entities]
        ids.append(("output_schema", "source.output_schema"))
    return ids


def select_hint(cfg, name: str, row: dict) -> str:
    """What to do when privileges:select:<name> fails: load missing table, grant privileges, or refresh MV."""
    table = cfg.entities[name].table
    schema, relname = ident.split(table)
    if not row["table_exists"]:
        if schema == demo.SCHEMA and relname in demo.TABLES:
            return (f"table {table} does not exist yet: for the demo, run `sxc.py demo` first (it creates and loads the "
                    "demo tables), then rerun doctor")
        return (f"table {table} does not exist in the connected database: check entities.{name}.table in "
                "crosscheck.toml (or load the data first), then rerun doctor")
    if not row.get("can_use", True) or not row.get("can_select", True):
        return f"grant the check role USAGE on the schema and SELECT on {table}, then rerun doctor"
    if row.get("relkind") == "m" and not row.get("is_populated", True):
        return (f"materialized view {table} is not populated (relispopulated=false): "
                f"run `REFRESH MATERIALIZED VIEW {table}`, then rerun doctor")
    return f"grant the check role USAGE on the schema and SELECT on {table}, then rerun doctor"


def write_findings(row: dict) -> dict:
    """The detail of privileges:no_write: every way the check role could change the source table or its schema."""
    return {"held": ",".join(p for p in WRITE_PRIVILEGES if row[f"can_{p.lower()}"]),
            "member_of_owner": row["member_of_owner"], "schema_owner": row["schema_owner"],
            "schema_create": row["schema_create"]}


def rls_visibility_warning(name: str, row: dict) -> str | None:
    """Disclose potential row-subset skew when PostgreSQL RLS is enabled on a source relation."""
    if not row.get("table_exists") or not row.get("row_security"):
        return None
    force = bool(row.get("force_row_security", False))
    bypass = bool(row.get("bypass_rls", False))
    return (f"entities.{name}: PostgreSQL row-level security is enabled (relrowsecurity=true, "
            f"relforcerowsecurity={str(force).lower()}, rolbypassrls={str(bypass).lower()}); "
            "visible rows for the check role can differ from Cube or another role")


def collation_version_warnings(row: dict) -> list[str]:
    """Disclose database-level PostgreSQL collation version drift (PG 15+)."""
    if not row.get("db_collation_version_mismatch"):
        return []
    return [
        "database collation version mismatch: pg_database.datcollversion differs from "
        "pg_database_collation_actual_version; text ordering, unique indexes and grouping "
        "can disagree with stored index order until affected indexes are rebuilt and "
        "ALTER DATABASE ... REFRESH COLLATION VERSION is run"
    ]


def _index_and_column_warnings(name: str, row: dict) -> list[str]:
    warnings: list[str] = []
    invalid_idx = int(row.get("invalid_indexes", 0))
    if invalid_idx > 0:
        warnings.append(
            f"entities.{name}: source relation has {invalid_idx} invalid or unready index(es) in pg_index "
            "(indisvalid=false or indisready=false); PostgreSQL ignores invalid indexes for uniqueness "
            "enforcement and query planning"
        )
    child_invalid_idx = int(row.get("child_invalid_indexes", 0))
    if child_invalid_idx > 0:
        warnings.append(
            f"entities.{name}: child partition(s) or inherited relation(s) have {child_invalid_idx} invalid or "
            "unready index(es) in pg_index (indisvalid=false or indisready=false)"
        )
    gen_cols = int(row.get("generated_columns", 0))
    if gen_cols > 0:
        warnings.append(
            f"entities.{name}: source relation defines {gen_cols} generated column(s) in pg_attribute "
            "(attgenerated<>''); derived column values depend on their PostgreSQL generation expression"
        )
    return warnings


def _view_dependency_warnings(name: str, row: dict) -> list[str]:
    if str(row.get("relkind", "")) != "v":
        return []
    warnings: list[str] = []
    volatile_funcs = int(row.get("view_volatile_functions", 0))
    if volatile_funcs > 0:
        warnings.append(
            f"entities.{name}: source view references {volatile_funcs} unpinned VOLATILE function(s) via "
            "pg_rewrite/pg_depend (provolatile='v'; pinned built-in pg_catalog functions are omitted by pg_depend); "
            "volatile functions can return different values across statements inside a single REPEATABLE READ snapshot"
        )
    if row.get("view_dep_rls_tables"):
        warnings.append(
            f"entities.{name}: source view depends on underlying relation(s) with PostgreSQL row-level security "
            "enabled (view_dep_rls_tables=true); visible rows can differ across roles depending on view "
            "security_invoker and underlying RLS policies"
        )
    if row.get("view_dep_unlogged_tables"):
        warnings.append(
            f"entities.{name}: source view depends on underlying UNLOGGED relation(s) "
            "(view_dep_unlogged_tables=true); unlogged tables are truncated automatically after an unclean "
            "shutdown or crash"
        )
    if row.get("view_dep_foreign_tables"):
        warnings.append(
            f"entities.{name}: source view depends on underlying foreign table(s) "
            "(view_dep_foreign_tables=true); foreign tables do not participate in PostgreSQL REPEATABLE READ "
            "snapshot isolation across statements or stages"
        )
    return warnings


def catalog_warnings(name: str, row: dict) -> list[str]:
    """Disclose zero-extra-round-trip PostgreSQL catalog risks for one existing source relation."""
    if not row.get("table_exists"):
        return []
    warnings: list[str] = []
    rls_warn = rls_visibility_warning(name, row)
    if rls_warn:
        warnings.append(rls_warn)
    elif row.get("child_partition_row_security"):
        warnings.append(
            f"entities.{name}: a child partition or inherited relation has PostgreSQL row-level security enabled "
            "(child_partition_row_security=true) while the parent does not (relrowsecurity=false); querying the "
            "parent vs. a child relation can enforce different row-visibility policies"
        )
    relkind = str(row.get("relkind", ""))
    if relkind == "f":
        warnings.append(
            f"entities.{name}: source relation is a foreign table (relkind='f'); foreign tables do not participate "
            "in PostgreSQL REPEATABLE READ snapshot isolation across statements or stages"
        )
    elif row.get("child_foreign_partitions"):
        warnings.append(
            f"entities.{name}: a child partition or inherited relation is a foreign table "
            "(child_foreign_partitions=true); foreign partitions do not participate in PostgreSQL "
            "REPEATABLE READ snapshot isolation across statements or stages"
        )
    if relkind == "v" and row.get("view_security_invoker") is False:
        warnings.append(
            f"entities.{name}: source view executes with view-owner privileges (relkind='v', "
            "view_security_invoker=false); underlying table permissions and row-level security "
            "policies are evaluated as the view owner rather than the check role"
        )
    relpersistence = str(row.get("relpersistence", "p"))
    if relpersistence == "u":
        warnings.append(
            f"entities.{name}: source relation is UNLOGGED (relpersistence='u'); unlogged tables are not "
            "WAL-logged and are truncated automatically after an unclean shutdown or crash"
        )
    elif row.get("child_unlogged_partitions"):
        warnings.append(
            f"entities.{name}: a child partition or inherited relation is UNLOGGED "
            "(child_unlogged_partitions=true); unlogged partitions are truncated automatically after "
            "an unclean shutdown or crash"
        )
    unvalidated = int(row.get("unvalidated_constraints", 0))
    if unvalidated > 0:
        warnings.append(
            f"entities.{name}: source relation has {unvalidated} unvalidated CHECK/FOREIGN KEY constraint(s) in "
            "pg_constraint (convalidated=false); existing rows are not checked against those constraints"
        )
    child_unvalidated = int(row.get("child_unvalidated_constraints", 0))
    if child_unvalidated > 0:
        warnings.append(
            f"entities.{name}: child partition(s) or inherited relation(s) have {child_unvalidated} local "
            "unvalidated CHECK/FOREIGN KEY constraint(s) in pg_constraint (convalidated=false); existing rows "
            "in those partitions are not checked against those constraints"
        )
    mismatched_colls = int(row.get("mismatched_column_collations", 0))
    if mismatched_colls > 0:
        warnings.append(
            f"entities.{name}: source relation uses {mismatched_colls} column collation(s) with a version "
            "mismatch in pg_collation (collversion differs from pg_collation_actual_version); text ordering, "
            "unique indexes and grouping can disagree with stored index order"
        )
    if relkind in ("r", "p") and row.get("has_rules"):
        warnings.append(
            f"entities.{name}: source table has PostgreSQL rewrite rules (relhasrules=true); "
            "rewrite rules can alter or suppress visible rows"
        )
    warnings.extend(_index_and_column_warnings(name, row))
    warnings.extend(_view_dependency_warnings(name, row))
    return warnings


def _privileges(conn, cfg, res: Result, hints: dict, server_version_num: int = MIN_PG):
    sql = _privileges_sql(server_version_num)
    rows = {}
    for name, ent in cfg.entities.items():
        schema, table = ident.split(ent.table)
        rows[name] = db.rows(conn, sql, schema=schema, table=table)[0]
    for name, r in rows.items():
        relkind = str(r.get("relkind", ""))
        relpersistence = str(r.get("relpersistence", "p"))
        is_populated = bool(r.get("is_populated", True))
        has_rules = bool(r.get("has_rules", False))
        view_security_invoker = bool(r.get("view_security_invoker", False))
        view_security_barrier = bool(r.get("view_security_barrier", False))
        unvalidated = int(r.get("unvalidated_constraints", 0))
        invalid_indexes = int(r.get("invalid_indexes", 0))
        child_invalid_indexes = int(r.get("child_invalid_indexes", 0))
        generated_columns = int(r.get("generated_columns", 0))
        child_rls = bool(r.get("child_partition_row_security", False))
        child_foreign = bool(r.get("child_foreign_partitions", False))
        child_unlogged = bool(r.get("child_unlogged_partitions", False))
        child_unvalidated = int(r.get("child_unvalidated_constraints", 0))
        mismatched_colls = int(r.get("mismatched_column_collations", 0))
        view_volatile_functions = int(r.get("view_volatile_functions", 0))
        view_dep_rls = bool(r.get("view_dep_rls_tables", False))
        view_dep_unlogged = bool(r.get("view_dep_unlogged_tables", False))
        view_dep_foreign = bool(r.get("view_dep_foreign_tables", False))
        unpopulated_mv = r["table_exists"] and relkind == "m" and not is_populated
        ok = r["table_exists"] and r["can_use"] and r["can_select"] and not unpopulated_mv
        res.add(f"privileges:select:{name}", _st(ok), f"entities.{name}", schema_exists=r["schema_exists"],
                table_exists=r["table_exists"], relkind=relkind, relpersistence=relpersistence,
                is_populated=is_populated, schema_usage=r["can_use"], select=r["can_select"],
                view_security_invoker=view_security_invoker, view_security_barrier=view_security_barrier,
                view_volatile_functions=view_volatile_functions,
                view_dep_rls_tables=view_dep_rls,
                view_dep_unlogged_tables=view_dep_unlogged,
                view_dep_foreign_tables=view_dep_foreign,
                row_security=bool(r.get("row_security", False)),
                force_row_security=bool(r.get("force_row_security", False)),
                bypass_rls=bool(r.get("bypass_rls", False)),
                child_partition_row_security=child_rls,
                child_foreign_partitions=child_foreign,
                child_unlogged_partitions=child_unlogged,
                unique_nulls_not_distinct=bool(r.get("unique_nulls_not_distinct", False)),
                invalid_indexes=invalid_indexes,
                child_invalid_indexes=child_invalid_indexes,
                generated_columns=generated_columns,
                unvalidated_constraints=unvalidated,
                child_unvalidated_constraints=child_unvalidated,
                mismatched_column_collations=mismatched_colls,
                has_rules=has_rules)
        if not ok:
            hints.setdefault(f"privileges:select:{name}", select_hint(cfg, name, r))
        res.not_proven.extend(catalog_warnings(name, r))
    for name, r in rows.items():
        if not r["table_exists"]:
            res.add(f"privileges:no_write:{name}", "not_run", f"entities.{name}", reason="table_missing")
            continue
        found = write_findings(r)
        ok = not (found["held"] or found["member_of_owner"] or found["schema_owner"] or found["schema_create"])
        res.add(f"privileges:no_write:{name}", _st(ok), f"entities.{name}", **found)
        if not ok:
            hints.setdefault(f"privileges:no_write:{name}",
                             "the check role can change source tables: revoke INSERT, UPDATE, DELETE, TRUNCATE and "
                             "TRIGGER on them and CREATE on their schema, make another role own the tables and the "
                             "schema (and drop the check role's membership in those owners), or point source.dsn_env "
                             "at a read-only role")


def _temporary(conn, ref: str, res: Result, hints: dict):
    ok = bool(db.scalar(conn, "SELECT has_database_privilege(current_database(), 'TEMPORARY')"))
    res.add("privileges:temporary", _st(ok), ref, temporary=ok)
    if not ok:
        hints["privileges:temporary"] = ("grant the check role TEMPORARY on the database (GRANT TEMPORARY ON DATABASE "
                                         "<database> TO <check role>): build computes the closure in temporary tables")


def _output_schema(conn, cfg, res: Result, hints: dict):
    schema = cfg.source.output_schema
    r = db.rows(conn, _OUTPUT_SCHEMA_SQL, schema=schema)[0]
    ok = (r["schema_exists"] and r["can_create"] and r["can_use"]) or (not r["schema_exists"] and r["may_create_schema"])
    res.add("output_schema", _st(ok), "source.output_schema", exists=r["schema_exists"], create=r["can_create"],
            usage=r["can_use"], may_create_schema=r["may_create_schema"])
    if not ok:
        hints["output_schema"] = (f"create schema {schema} owned by the check role (CREATE SCHEMA {schema} AUTHORIZATION "
                                  f"<check role>) or grant it CREATE and USAGE on {schema}, then rerun doctor")


def _database(cfg, res: Result, hints: dict, have_pg8000: bool):
    env = cfg.source.dsn_env if cfg else "SXC_PG_DSN"
    ref = "source.dsn_env" if cfg else ""
    pending = _db_checks(cfg)

    def skip(reason: str, done: int = 0):
        for cid, cref in pending[done:]:
            res.add(cid, "not_run", cref, reason=reason)

    if not os.environ.get(env):
        res.add("dsn", "fail", ref, env=env)
        hints["dsn"] = (f"set {env} to postgresql://<check role>@<host>:<port>/<database> (password in PGPASSWORD), "
                        "then rerun doctor")
        return skip("no_dsn")
    res.add("dsn", "pass", ref, env=env)
    if not have_pg8000:
        return skip("no_pg8000")
    from pg8000.exceptions import DatabaseError, InterfaceError
    try:
        conn = db.connect(env, require_pg17=False)
    except (StageError, DatabaseError, InterfaceError) as e:
        cause = e.__cause__ if isinstance(e, StageError) else e
        state = _sqlstate(cause)
        reason = "bad_dsn" if isinstance(cause, (StageError, ValueError)) else ("rejected" if state else "unreachable")
        res.add("connect", "fail", ref, reason=reason, exception=type(cause).__name__ if cause else "", sqlstate=state)
        hints["connect"] = (f"start PostgreSQL or fix {env} (scheme postgresql://, host, port, database and role; "
                            "password in PGPASSWORD), then rerun doctor")
        return skip("no_connection", 1)
    res.add("connect", "pass", ref)
    try:
        with db.read_only(conn, cfg.source.statement_timeout_ms if cfg else 60000):
            num = db.server_version_num(conn)
            coll_mismatch = False
            if int(num) >= MIN_PG_NULLS_NOT_DISTINCT:
                coll_rows = db.rows(conn, _DB_COLLATION_VERSION_SQL)
                coll_mismatch = bool(coll_rows and coll_rows[0].get("db_collation_version_mismatch", False))
            res.add("pg_version", _st(pg_version_ok(num)), ref, server_version_num=num,
                    db_collation_version_mismatch=coll_mismatch)
            if coll_mismatch:
                res.not_proven.extend(collation_version_warnings({"db_collation_version_mismatch": True}))
            if not pg_version_ok(num):
                hints["pg_version"] = "use PostgreSQL 17 or newer (the closure uses MERGE ... RETURNING merge_action())"
            _temporary(conn, ref, res, hints)
            if cfg:
                _privileges(conn, cfg, res, hints, num)
                _output_schema(conn, cfg, res, hints)
    except (DatabaseError, InterfaceError) as e:
        res.add("database", "error", ref, exception=type(e).__name__, sqlstate=_sqlstate(e))
        hints["database"] = "a catalog query failed; check the server log, then rerun doctor"
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# ---- stage ---------------------------------------------------------------------------------------------
def _components(cfg, require: set, res: Result, hints: dict):
    home = cube_run.cube_home(cfg.cube.version if cfg else config.CUBE_VERSION)
    probes = {
        "reason": (("owlready2", lambda: probe_module("owlready2")), ("pyshacl", lambda: probe_module("pyshacl")),
                   ("java", probe_java)),
        "cube": (("node", probe_node), ("adapter", lambda: probe_adapter(home)), ("native", lambda: probe_native(home))),
    }
    for comp, items in probes.items():
        for name, probe in items:
            ok, detail, hint = probe()
            cid = f"{comp}:{name}"
            if comp in require:
                res.add(cid, _st(ok), "", **detail)
                if not ok:
                    hints[cid] = hint
            elif not ok:
                res.not_proven.append(f"{cid} is not ready: {hint} (add --require {comp} to make it a check)")


def _next_action(cfg, res: Result, hints: dict) -> str:
    for c in res.checks:
        if c.status != "pass" and c.id in hints:
            return hints[c.id]
    if res.status != "pass":
        return "fix the checks listed above, then rerun doctor"
    if cfg is None:
        return ("rerun doctor with --config crosscheck.toml to check the check role's privileges and the output schema "
                "(for the demo, run `sxc.py demo` first: it creates the tables)")
    return "ready: run `sxc.py build`"


def _have_pg8000() -> bool:
    try:
        import pg8000.native  # noqa: F401
    except ImportError:
        return False
    return True


def run(cfg, args, out: Path) -> Result:
    require = set(getattr(args, "require", None) or ["core"])
    unknown = sorted(require - set(COMPONENTS))
    if unknown:
        raise StageError(f"--require: unknown component {', '.join(unknown)} (use {', '.join(COMPONENTS)})", EXIT_CONFIG)
    res = Result("doctor")
    hints: dict[str, str] = {}
    ok = python_ok(sys.version_info)
    res.add("python", _st(ok), "", version=platform.python_version())
    if not ok:
        hints["python"] = "use Python 3.11 or newer"
    have_pg8000 = _have_pg8000()
    if have_pg8000:
        res.add("pg8000", "pass", "", version=_dist_version("pg8000"))
    else:
        res.add("pg8000", "fail", "")
        hints["pg8000"] = pip_hint("core")
    _database(cfg, res, hints, have_pg8000)
    if cfg is None:
        res.not_proven.append("the check role's privileges on the source tables and the output schema were not checked "
                              "(no crosscheck.toml; rerun doctor with --config)")
    _components(cfg, require, res, hints)
    if getattr(args, "coverage", False):
        home = cube_run.cube_home(cfg.cube.version if cfg else config.CUBE_VERSION)
        setattr(res, "coverage", coverage_matrix.build_coverage_report(home))
    res.next_safe_action = _next_action(cfg, res, hints)
    return res
