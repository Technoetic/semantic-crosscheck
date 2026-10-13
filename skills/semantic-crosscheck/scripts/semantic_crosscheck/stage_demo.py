"""demo stage: load the synthetic parts / BOM / orders / stock data with the loader role.

The demo is the harness's known-answer test. demo.build() generates the rows together with reference numbers
computed independently in Python; this stage loads exactly those rows so that build, reason, cube and verify can
be judged against demo/expected.json. Loading uses the loader role (source.owner_dsn_env), so the check role that
every other stage uses stays read-only on the source schema.

The demo can be rerun at any time, also after build: the tables are reloaded in place (demo.load), so the views
build created on them stay valid. Tables that exist with other columns than the demo defines stop the stage with
exit 2 before anything is written: they are not the demo's, and the stage never drops a table.

Checks:
  demo:rows:<table>    the table holds exactly the generated rows (for sales_order: the valid orders)
  demo:copy_on_error   PostgreSQL 17 `COPY ... ON_ERROR ignore` skipped exactly the malformed order rows
  demo:load            error (SQLSTATE only) when the load was rolled back; next_safe_action names the likely cause
expected.json (demo.reference) groups the generator's reference numbers by the checks they are compared with and
says which variant each group applies to. The data is synthetic, so this is the one output that may hold values;
results still hold counts only.
"""

import contextlib
import json
from pathlib import Path

from . import db, demo
from .result import EXIT_CONFIG, Result, StageError

GENERATED = {"part": "parts", "bom_line": "edges", "sales_order": "orders", "stock": "stock"}
# SQLSTATE -> why the load was rolled back ({env}: the loader DSN variable)
LOAD_HINTS = {
    "42501": "the loader role in {env} lacks a privilege: it must own the demo tables in schema " + demo.SCHEMA
             + " (and, while they are missing, own that schema or be allowed to create it); fix the roles with "
               "docs/setup-roles.sql instead of using a superuser",
    "0A000": "a table outside the demo references a demo table with a foreign key, so TRUNCATE refused to empty it; "
             "drop that constraint",
    "40P01": "another session (for example a build) was using the demo tables at the same time",
    "55P03": "another session held a lock on the demo tables for too long",
    "57014": "the load ran into the statement timeout, most likely waiting for another session that uses the demo tables",
}
CONSTRAINT_HINT = ("a constraint added to the demo tables rejects the generated rows (the dirty variant breaks references "
                   "on purpose, for example against a foreign key from fk_review.sql); drop it")


def _sqlstate(e: BaseException) -> str:
    arg = e.args[0] if e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def load_hint(code: str, env: str) -> str:
    """One sentence for a load rolled back with SQLSTATE code."""
    if code in LOAD_HINTS:
        why = LOAD_HINTS[code].format(env=env)
    elif code.startswith("23"):
        why = CONSTRAINT_HINT
    else:
        why = f"check the server log and the loader role in {env}"
    return f"the load was rolled back (SQLSTATE {code or 'unknown'}): {why}; then rerun demo"


def _load(cfg, data: dict, res: Result) -> dict | None:
    """Load in one transaction; a database error becomes an `error` check (SQLSTATE only, never the message)."""
    conn = db.connect(cfg.source.owner_dsn_env)
    from pg8000.exceptions import DatabaseError, InterfaceError
    try:
        with db.write_tx(conn):
            return demo.load(conn, data)
    except demo.TableMismatch as e:
        raise StageError(f"existing tables in schema {demo.SCHEMA} differ from the demo definition ({e}): they are not "
                         "the demo's tables, and demo never drops a table; drop them or load the demo into another "
                         "database, then rerun demo", EXIT_CONFIG) from e
    except (DatabaseError, InterfaceError) as e:
        res.add("demo:load", "error", "source.owner_dsn_env", exception=type(e).__name__, sqlstate=_sqlstate(e))
        return None
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def run(cfg, args, out: Path) -> Result:
    if demo.SCHEMA not in cfg.source.schemas:
        raise StageError(f"the demo loads schema {demo.SCHEMA}, which source.schemas does not list; "
                         "run it with demo/crosscheck.toml", EXIT_CONFIG)
    if args.scale < 1:
        raise StageError("--scale must be 1 or more", EXIT_CONFIG)
    res = Result("demo")
    expected_file = Path(out) / "demo" / "expected.json"
    expected_file.unlink(missing_ok=True)
    data = demo.build(args.variant == "dirty", args.seed, args.scale)
    loaded = _load(cfg, data, res)
    if loaded is None:
        code = next((c.detail.get("sqlstate", "") for c in res.checks if c.id == "demo:load"), "")
        res.next_safe_action = load_hint(code, cfg.source.owner_dsn_env)
        return res

    for table, key in GENERATED.items():
        n, generated = int(loaded[table]), len(data[key])
        res.add(f"demo:rows:{table}", "pass" if n == generated else "fail", "", loaded=n, generated=generated)
    skipped, malformed = int(loaded["sales_order_skipped"]), len(data["malformed"])
    res.add("demo:copy_on_error", "pass" if skipped == malformed else "fail", "", skipped=skipped, malformed=malformed)

    expected_file.parent.mkdir(parents=True, exist_ok=True)
    expected = demo.reference(data["expected"], args.variant, args.seed, args.scale)
    expected_file.write_text(json.dumps(expected, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")

    if res.status != "pass":
        res.next_safe_action = (f"the loaded row counts differ from the generator: make sure nothing else writes to "
                                f"schema {demo.SCHEMA}, then rerun demo")
    elif args.variant == "dirty":
        res.next_safe_action = ("run `sxc.py build` with the same --config and --out, then reason, cube and verify; the "
                                "dirty variant fails on purpose, and demo/expected.json (planted_defects) names the check "
                                "that shows each planted defect")
    else:
        res.next_safe_action = ("run `sxc.py build` with the same --config and --out, then reason, cube and verify; "
                                "demo/expected.json holds the reference numbers and the checks to compare them with")
    return res
