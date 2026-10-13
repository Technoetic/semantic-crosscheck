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
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

from . import config, cube_install, cube_run, db, demo, ident, procutil
from .result import EXIT_CONFIG, Result, StageError

COMPONENTS = ("core", "reason", "cube")
MIN_PYTHON, MIN_PG, MIN_JAVA = (3, 11), 170000, 11
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER")
PROBE_TIMEOUT_S = 60
PLUGIN_ROOT = Path(__file__).resolve().parents[4]          # holds requirements/ (the repository root)

_PRIVILEGES_SQL = """
    SELECT n.oid IS NOT NULL AS schema_exists, c.oid IS NOT NULL AS table_exists,
           coalesce(has_schema_privilege(n.oid, 'USAGE'), false) AS can_use,
           coalesce(has_schema_privilege(n.oid, 'CREATE'), false) AS schema_create,
           coalesce(pg_has_role(n.nspowner, 'MEMBER'), false) AS schema_owner,
           coalesce(has_table_privilege(c.oid, 'SELECT'), false) AS can_select,
           coalesce(has_any_column_privilege(c.oid, 'INSERT'), false) AS can_insert,
           coalesce(has_any_column_privilege(c.oid, 'UPDATE'), false) AS can_update,
           coalesce(has_table_privilege(c.oid, 'DELETE'), false) AS can_delete,
           coalesce(has_table_privilege(c.oid, 'TRUNCATE'), false) AS can_truncate,
           coalesce(has_table_privilege(c.oid, 'TRIGGER'), false) AS can_trigger,
           coalesce(pg_has_role(c.relowner, 'MEMBER'), false) AS member_of_owner
    FROM (SELECT 1) AS one
    LEFT JOIN pg_namespace n ON n.nspname = :schema
    LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :table AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
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
    try:
        rec = json.loads((home / cube_install.RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


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
    try:
        rec = json.loads((home / cube_install.RECORD).read_text(encoding="utf-8"))
        recorded = rec["native"]["index_node_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
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
    """What to do when privileges:select:<name> fails: load or name the missing table, else grant the privileges."""
    table = cfg.entities[name].table
    schema, relname = ident.split(table)
    if not row["table_exists"]:
        if schema == demo.SCHEMA and relname in demo.TABLES:
            return (f"table {table} does not exist yet: for the demo, run `sxc.py demo` first (it creates and loads the "
                    "demo tables), then rerun doctor")
        return (f"table {table} does not exist in the connected database: check entities.{name}.table in "
                "crosscheck.toml (or load the data first), then rerun doctor")
    return f"grant the check role USAGE on the schema and SELECT on {table}, then rerun doctor"


def write_findings(row: dict) -> dict:
    """The detail of privileges:no_write: every way the check role could change the source table or its schema."""
    return {"held": ",".join(p for p in WRITE_PRIVILEGES if row[f"can_{p.lower()}"]),
            "member_of_owner": row["member_of_owner"], "schema_owner": row["schema_owner"],
            "schema_create": row["schema_create"]}


def _privileges(conn, cfg, res: Result, hints: dict):
    rows = {}
    for name, ent in cfg.entities.items():
        schema, table = ident.split(ent.table)
        rows[name] = db.rows(conn, _PRIVILEGES_SQL, schema=schema, table=table)[0]
    for name, r in rows.items():
        ok = r["table_exists"] and r["can_use"] and r["can_select"]
        res.add(f"privileges:select:{name}", _st(ok), f"entities.{name}", schema_exists=r["schema_exists"],
                table_exists=r["table_exists"], schema_usage=r["can_use"], select=r["can_select"])
        if not ok:
            hints.setdefault(f"privileges:select:{name}", select_hint(cfg, name, r))
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
            res.add("pg_version", _st(pg_version_ok(num)), ref, server_version_num=num)
            if not pg_version_ok(num):
                hints["pg_version"] = "use PostgreSQL 17 or newer (the closure uses MERGE ... RETURNING merge_action())"
            _temporary(conn, ref, res, hints)
            if cfg:
                _privileges(conn, cfg, res, hints)
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
    res.next_safe_action = _next_action(cfg, res, hints)
    return res
