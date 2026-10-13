#!/usr/bin/env python3
"""semantic-crosscheck command line.

    python sxc.py <stage> [--config crosscheck.toml] [--out DIR] [options]

Stages: doctor, demo, build, reason, cube, verify, report, cube-install.
Each stage writes <out>/<stage>/result.json (counts and check ids only, never row values) and exits with
0 pass, 1 a check failed or could not run, 2 configuration or usage error, 3 environment missing.

- --config names the configuration. Given explicitly, it must exist (a typo must not turn a check off); without it,
  ./crosscheck.toml is used, and only doctor and cube-install may run without one.
- The stage's previous result.json is removed before the stage runs, so an interrupted or killed run cannot leave an
  earlier verdict for report to trust. A new output directory gets a .gitignore of `*`: results, generated files and
  logs belong to the local run, not to the user's repository.
- An unexpected exception is reported by type and location only: exception messages (database errors in particular)
  can quote row values. SXC_DEBUG=1 prints the full traceback for local debugging.
"""

import sys

if sys.version_info < (3, 11):   # checked before importing the package, which needs tomllib (Python 3.11)
    sys.stderr.write(f"semantic-crosscheck needs Python 3.11 or newer (this is {sys.version_info[0]}.{sys.version_info[1]}): "
                     "run sxc.py with a newer python\n")
    sys.exit(3)

import argparse  # noqa: E402
import hashlib  # noqa: E402
import importlib  # noqa: E402
import os  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_crosscheck import VERSION, config  # noqa: E402
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_FAIL, Result, StageError, discard, inputs_fingerprint  # noqa: E402

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
DEFAULT_CONFIG = "crosscheck.toml"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sxc.py", description="Cross-check a PostgreSQL 17 closure, SHACL rules, OWL reasoning and a Cube semantic layer.")
    p.add_argument("--version", action="version", version=f"semantic-crosscheck {VERSION}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="path to crosscheck.toml; it must exist when given (default: "
                                                        "./crosscheck.toml, optional for doctor and cube-install)")
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
    s.add_argument("--home", default="", help="install directory (default: per-user cache, or SXC_CUBE_HOME); the other "
                                              "stages then need SXC_CUBE_HOME set to it")
    s.add_argument("--native-file", default="", help="use this already downloaded native tarball (still SHA-256 verified)")
    s.add_argument("--force", action="store_true", help="reinstall even when already installed")
    return p


def config_path(stage: str, given: str | None) -> Path | None:
    """The configuration to load: an explicit --config must exist; otherwise ./crosscheck.toml, which only doctor
    and cube-install may run without."""
    if given is not None:
        p = Path(given)
        if not p.is_file():
            raise StageError(f"--config {p.name}: no such file (check the path; nothing was run)", EXIT_CONFIG)
        return p
    p = Path(DEFAULT_CONFIG)
    if p.is_file():
        return p
    if stage in NEEDS_CONFIG:
        raise StageError(f"no {DEFAULT_CONFIG} in the current directory: pass --config PATH", EXIT_CONFIG)
    return None


def ensure_out(out: Path) -> None:
    """Create the output directory; a new one gets a .gitignore of `*` (a debug server.log can quote row values)."""
    if not out.exists():
        out.mkdir(parents=True)
        (out / ".gitignore").write_text("*\n", encoding="utf-8", newline="\n")


def _write(res: Result, out: Path | None) -> Path | None:
    if out is None:
        return None
    ensure_out(out)
    return res.write(out)


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
    return expand("\n".join(lines))


PLUGIN_ROOT = Path(__file__).resolve().parents[3]


def expand(text: str) -> str:
    """Console-only expansion of the path tokens that results use instead of local paths: <python> is the running
    interpreter and <plugin> the plugin root, so a printed hint can be pasted while result.json stays path-free."""
    python = f'"{sys.executable}"' if sys.executable else "python"
    return text.replace("<python>", python).replace("<plugin>", str(PLUGIN_ROOT))


def _db_error_code(e: BaseException) -> str:
    """SQLSTATE of a pg8000 error without its message (messages can quote row values)."""
    arg = e.args[0] if e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def _crash_note(e: BaseException) -> None:
    if os.environ.get("SXC_DEBUG") == "1":
        traceback.print_exception(e)
        return
    frames = traceback.extract_tb(e.__traceback__)
    where = f" in {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    print(f"[sxc] unexpected {type(e).__name__}{where}; set SXC_DEBUG=1 to print the traceback (its messages can "
          "quote row values: keep it local)", file=sys.stderr)


def discard_report_files(out: Path | None) -> None:
    """report.md and report.json of an earlier report: a report run that fails (even before the configuration is
    found) must not leave an earlier verdict behind."""
    if out is not None:
        for name in ("report.md", "report.json"):
            (out / name).unlink(missing_ok=True)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    stage = args.stage
    result_name = stage.replace("-", "_")
    cfg = None
    out = Path(args.out) if args.out else None
    try:
        cfg_file = config_path(stage, args.config)
        if out is None:
            out = (cfg_file.resolve().parent if cfg_file else Path.cwd()) / ".sxc"
        ensure_out(out)
        discard(out, result_name)
        if stage == "report":
            discard_report_files(out)
        inputs = {}
        if cfg_file is not None:
            before = hashlib.sha256(cfg_file.read_bytes()).hexdigest()
            cfg = config.load(cfg_file)
            # The fingerprint is taken before the stage runs: an edit during the run then leaves the result recorded
            # with the old inputs, so report sees it as stale instead of certifying files the stage never read.
            inputs = inputs_fingerprint(cfg)
            if inputs.get("config_sha256") != before:
                raise StageError(f"{cfg_file.name} changed while the stage was starting; rerun it", EXIT_CONFIG)
        mod = importlib.import_module(f"semantic_crosscheck.{STAGES[stage]}")
        res = mod.run(cfg, args, out)
        res.inputs = inputs
    except StageError as e:
        if stage == "report":
            discard_report_files(out)
        res = Result(result_name)
        res.add("stage", "error", "", message=str(e))
        res.next_safe_action = "fix the configuration or environment named in the message, then rerun this stage"
        print(_summary(res, _write(res, out), args.quiet), file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        if stage == "report":
            discard_report_files(out)
        res = Result(result_name)
        res.add("stage", "error", "", reason="interrupted")
        res.next_safe_action = "the run was interrupted before it finished: rerun this stage"
        print(_summary(res, _write(res, out), args.quiet), file=sys.stderr)
        return EXIT_FAIL
    except Exception as e:  # noqa: BLE001 - unexpected failure: keep only the type and SQLSTATE in the result file
        if stage == "report":
            discard_report_files(out)
        res = Result(result_name)
        res.add("stage", "error", "", exception=type(e).__name__, sqlstate=_db_error_code(e))
        _crash_note(e)
        print(_summary(res, _write(res, out), args.quiet), file=sys.stderr)
        return EXIT_FAIL
    path = res.write(out)
    print(_summary(res, path, args.quiet))
    return res.exit_code()


if __name__ == "__main__":
    sys.exit(main())
