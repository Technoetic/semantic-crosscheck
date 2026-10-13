#!/usr/bin/env python3
"""semantic-crosscheck command line.

    python sxc.py <stage> --config crosscheck.toml [--out DIR] [options]

Stages: doctor, demo, build, reason, cube, verify, report, cube-install.
Each stage writes <out>/<stage>/result.json (counts and check ids only, never row values) and exits with
0 pass, 1 a check failed or could not run, 2 configuration or usage error, 3 environment missing.
"""

import argparse
import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_crosscheck import VERSION, config  # noqa: E402
from semantic_crosscheck.result import EXIT_FAIL, Result, StageError  # noqa: E402

STAGES = {
    "doctor": "doctor",
    "demo": "stage_demo",
    "build": "stage_build",
    "reason": "stage_reason",
    "cube": "stage_cube",
    "verify": "stage_verify",
    "report": "report",
    "cube-install": "cube_install",
}
NEEDS_CONFIG = {"demo", "build", "reason", "cube", "verify", "report"}


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sxc.py", description="Cross-check a PostgreSQL 17 closure, SHACL rules, OWL reasoning and a Cube semantic layer.")
    p.add_argument("--version", action="version", version=f"semantic-crosscheck {VERSION}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="crosscheck.toml", help="path to crosscheck.toml (default: ./crosscheck.toml)")
    common.add_argument("--out", default=None, help="output directory (default: .sxc next to the config)")
    common.add_argument("--quiet", action="store_true", help="print only the summary line")
    sub = p.add_subparsers(dest="stage", required=True, metavar="stage")

    s = sub.add_parser("doctor", parents=[common], help="check Python, PostgreSQL 17, role privileges and optional components")
    s.add_argument("--require", type=_csv, default=["core"], help="components that must be ready: core,reason,cube (default: core)")

    s = sub.add_parser("demo", parents=[common], help="load the synthetic demo data with the loader role")
    s.add_argument("--variant", choices=["clean", "dirty"], default="clean")
    s.add_argument("--seed", type=int, default=20261013)
    s.add_argument("--scale", type=int, default=1)

    s = sub.add_parser("build", parents=[common], help="shape violation views and hierarchy closures in the output schema")
    s.add_argument("--hierarchy", default="", help="build only this hierarchy")

    s = sub.add_parser("reason", parents=[common], help="SHACL (pySHACL) and OWL (HermiT) cross-checks against SQL")
    s.add_argument("--only", type=_csv, default=["shacl", "owl"], help="shacl,owl (default: both)")

    sub.add_parser("cube", parents=[common], help="generate the Cube model, start Cube and check its metadata")

    s = sub.add_parser("verify", parents=[common], help="V1 strict compare, V2 strata sweep, V3 query shapes, V4 mutation tests")
    s.add_argument("--only", type=_csv, default=["v1", "v2", "v3", "v4"], help="subset of v1,v2,v3,v4 (default: all)")
    s.add_argument("--gold", action="append", default=[], help="limit to this gold query id (repeatable)")

    sub.add_parser("report", parents=[common], help="summarise every stage result into report.md and report.json")

    s = sub.add_parser("cube-install", parents=[common], help="install the pinned Cube adapter (npm ci --ignore-scripts + SHA-256-pinned native module)")
    s.add_argument("--home", default="", help="install directory (default: per-user cache, or SXC_CUBE_HOME)")
    s.add_argument("--native-file", default="", help="use this already downloaded native tarball (still SHA-256 verified)")
    s.add_argument("--force", action="store_true", help="reinstall even when already installed")
    return p


def _brief(detail: dict) -> str:
    items = [f"{k}={v}" for k, v in detail.items() if not isinstance(v, (dict, list))]
    return " ".join(items)[:300]


def _summary(res: Result, path, quiet: bool) -> str:
    counts = {s: 0 for s in ("pass", "fail", "error", "not_run")}
    for c in res.checks:
        counts[c.status] += 1
    lines = [f"[sxc {res.stage}] {res.status.upper()}  checks: " + ", ".join(f"{n} {s}" for s, n in counts.items())
             + (f"  -> {path}" if path else "")]
    if not quiet:
        for c in res.checks:
            if c.status != "pass":
                lines.append(f"  {c.status.upper():7} {c.id}" + (f"  [{c.config_ref}]" if c.config_ref else "")
                             + (f"  {_brief(c.detail)}" if c.detail else ""))
        for n in res.not_proven:
            lines.append(f"  NOT PROVEN  {n}")
        if res.next_safe_action:
            lines.append(f"  next: {res.next_safe_action}")
    return "\n".join(lines)


def _db_error_code(e: BaseException) -> str:
    """SQLSTATE of a pg8000 error without its message (messages can quote row values)."""
    arg = e.args[0] if e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    stage = args.stage
    result_name = stage.replace("-", "_")
    cfg = None
    out = Path(args.out) if args.out else None
    try:
        if stage in NEEDS_CONFIG or Path(args.config).is_file():
            cfg = config.load(args.config)
        if out is None:
            out = (cfg.base_dir() if cfg else Path.cwd()) / ".sxc"
        mod = importlib.import_module(f"semantic_crosscheck.{STAGES[stage]}")
        res = mod.run(cfg, args, out)
    except StageError as e:
        res = Result(result_name)
        res.add("stage", "error", "", message=str(e))
        res.next_safe_action = "fix the configuration or environment named in the message, then rerun this stage"
        path = res.write(out) if out else None
        print(_summary(res, path, args.quiet), file=sys.stderr)
        return e.exit_code
    except Exception as e:  # noqa: BLE001 - unexpected failure: keep only the type and SQLSTATE in the result file
        res = Result(result_name)
        res.add("stage", "error", "", exception=type(e).__name__, sqlstate=_db_error_code(e))
        path = res.write(out) if out else None
        traceback.print_exc()
        print(_summary(res, path, args.quiet), file=sys.stderr)
        return EXIT_FAIL
    path = res.write(out)
    print(_summary(res, path, args.quiet))
    return res.exit_code()


if __name__ == "__main__":
    sys.exit(main())
