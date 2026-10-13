"""demo stage: load the synthetic parts / BOM / orders / stock data with the loader role.

The demo is the harness's known-answer test. demo.build() generates the rows together with reference numbers
computed independently in Python; this stage loads exactly those rows so that build, reason, cube and verify can
be judged against demo/expected.json. Loading uses the loader role (source.owner_dsn_env), so the check role that
every other stage uses stays read-only on the source schema.

Checks:
  demo:rows:<table>    the table holds exactly the generated rows (for sales_order: the valid orders)
  demo:copy_on_error   PostgreSQL 17 `COPY ... ON_ERROR ignore` skipped exactly the malformed order rows
expected.json is the generator's reference plus variant, seed and scale. The data is synthetic, so this is the
one output that may hold values; results still hold counts only.
"""

import contextlib
import json
from pathlib import Path

from . import db, demo
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

GENERATED = {"part": "parts", "bom_line": "edges", "sales_order": "orders", "stock": "stock"}


def _sqlstate(e: BaseException) -> str:
    arg = e.args[0] if e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def _load(cfg, data: dict, res: Result) -> dict | None:
    """Load in one transaction; a database error becomes an `error` check (SQLSTATE only, never the message)."""
    conn = db.connect(cfg.source.owner_dsn_env)
    from pg8000.exceptions import DatabaseError, InterfaceError
    try:
        if db.server_version_num(conn) < 170000:
            raise StageError("the demo needs PostgreSQL 17 or newer (COPY ... ON_ERROR ignore)", EXIT_ENV)
        with db.write_tx(conn):
            return demo.load(conn, data)
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
        res.next_safe_action = ("the load was rolled back: check that the loader role in "
                                f"{cfg.source.owner_dsn_env} owns schema {demo.SCHEMA} (or may create it), then rerun demo")
        return res

    for table, key in GENERATED.items():
        n, generated = int(loaded[table]), len(data[key])
        res.add(f"demo:rows:{table}", "pass" if n == generated else "fail", "", loaded=n, generated=generated)
    skipped, malformed = int(loaded["sales_order_skipped"]), len(data["malformed"])
    res.add("demo:copy_on_error", "pass" if skipped == malformed else "fail", "", skipped=skipped, malformed=malformed)

    expected_file.parent.mkdir(parents=True, exist_ok=True)
    expected = {**data["expected"], "variant": args.variant, "seed": args.seed, "scale": args.scale}
    expected_file.write_text(json.dumps(expected, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")

    if res.status != "pass":
        res.next_safe_action = (f"the loaded row counts differ from the generator: make sure nothing else writes to "
                                f"schema {demo.SCHEMA}, then rerun demo")
    elif args.variant == "dirty":
        res.next_safe_action = ("run `sxc.py build` with the same --config and --out; the dirty variant fails on purpose "
                                "(a cycle and rule violations), and demo/expected.json lists what each stage should find")
    else:
        res.next_safe_action = ("run `sxc.py build` with the same --config and --out, then reason, cube and verify; "
                                "demo/expected.json holds the reference numbers")
    return res
