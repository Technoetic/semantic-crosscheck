"""report stage: one summary of every stage result, for people (report.md) and machines (report.json).

Each stage judges only its own checks, but the question a reviewer or an agent asks is "is the whole pipeline
green, and if not, what do I fix first?". The report therefore:
  - mirrors each stage status in a `stage:<name>` check (fail-closed: the report passes only when every stage
    that ran passed; a status is recomputed from the checks so a hand-edited result cannot look better);
  - lists the checks that did not pass with the crosscheck.toml entry to fix, every not_proven line and every
    next_safe_action;
  - flags results that are older than a stage they read from (a rerun build makes an earlier cube run stale);
  - names the trusted code: `sql_violation` predicates and gold SQL run as written.
Stages that never ran are not proven rather than failed, so a report can be written at any point; a report with
no stage results at all is `not_run`.

Everything comes from the stage results, which hold counts and ids only: the report never touches the database.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from .result import STATUSES, Result, load_result

PIPELINE = ("doctor", "demo", "build", "reason", "cube", "verify")
DEPENDS = {"build": ("demo",), "reason": ("demo", "build"), "cube": ("demo", "build"), "verify": ("demo", "build", "cube")}
SEVERITY = {"pass": 0, "not_run": 1, "fail": 2, "error": 3}
TRUSTED_NOTE = ("`sql_violation` predicates and gold SQL files are trusted code from the configuration: the harness "
                "runs them as written (values only as bound parameters) and does not generate or validate them, so "
                "review them like code.")
_TIME = "%Y-%m-%dT%H:%M:%S%z"


def _list(r: dict, key: str) -> list:
    v = r.get(key)
    return v if isinstance(v, list) else []


def _checks(r: dict) -> list[dict]:
    return [c for c in _list(r, "checks") if isinstance(c, dict)]


def stage_status(r: dict) -> str:
    """Fail-closed status recomputed from the checks; the recorded status can only make it worse."""
    states = {c.get("status") for c in _checks(r)}
    if not states:
        status = "not_run"
    elif states - set(STATUSES):
        status = "error"
    else:
        status = max(states, key=SEVERITY.__getitem__)
    recorded = r.get("status")
    if recorded in SEVERITY and SEVERITY[recorded] > SEVERITY[status]:
        return recorded
    return status


def counts(r: dict) -> dict:
    out = {s: 0 for s in STATUSES}
    for c in _checks(r):
        s = c.get("status")
        out[s if s in out else "error"] += 1
    return out


def _brief(detail) -> str:
    if not isinstance(detail, dict):
        return ""
    return " ".join(f"{k}={v}" for k, v in detail.items() if not isinstance(v, (dict, list)))[:200]


def _when(text) -> datetime | None:
    try:
        return datetime.strptime(text, _TIME)
    except (TypeError, ValueError):
        return None


def stale_pairs(results: dict) -> list[tuple[str, str]]:
    """(stage, dependency) where the stage started before the dependency finished its latest run."""
    pairs = []
    for stage, deps in DEPENDS.items():
        if stage not in results:
            continue
        started = _when(results[stage].get("started_at"))
        for dep in deps:
            finished = _when(results.get(dep, {}).get("finished_at"))
            if started and finished and finished > started:
                pairs.append((stage, dep))
    return pairs


def read_results(out: Path) -> dict:
    """Stage results present in out, in pipeline order; an unreadable result becomes a synthetic error result."""
    results = {}
    for stage in PIPELINE:
        try:
            r = load_result(out, stage)
        except (OSError, ValueError):
            r = False
        if r is None:
            continue
        if not isinstance(r, dict):
            r = {"checks": [{"id": "result_file", "status": "error", "config_ref": "", "detail": {"reason": "unreadable"}}],
                 "next_safe_action": f"rerun `sxc.py {stage}`: its result.json could not be read"}
        results[stage] = r
    return results


def _trusted(cfg) -> dict:
    if cfg is None:
        return {"shapes": [], "gold": []}
    return {"shapes": [s.id for s in cfg.shapes if s.sql_violation],
            "gold": [{"id": g.id, "sql": g.sql} for g in cfg.gold]}


def _next_action(statuses: dict, results: dict, missing: list[str], stale: list[tuple[str, str]]) -> str:
    for stage, status in statuses.items():
        if status != "pass":
            hint = results[stage].get("next_safe_action") or f"fix its failing checks and rerun `sxc.py {stage}`"
            return f"fix {stage} first ({status}): {hint}"
    if not statuses:
        return "run `sxc.py doctor`, then build, reason, cube and verify, then rerun report"
    todo = [s for s in missing if s != "demo"]
    if todo:
        return f"run the stages that have not run yet ({', '.join(todo)}), in this order, then rerun report"
    if stale:
        rerun = [s for s in PIPELINE if s in {stage for stage, _dep in stale}]
        return f"rerun the stale stages ({', '.join(rerun)}), in this order, then rerun report"
    return "every stage passed; rerun the pipeline after changing crosscheck.toml or the source data"


def _stage_summary(r: dict, status: str) -> dict:
    return {
        "status": status,
        "counts": counts(r),
        "failing": [{"id": str(c.get("id", "")), "status": str(c.get("status", "")),
                     "config_ref": str(c.get("config_ref", "")), "detail": _brief(c.get("detail"))}
                    for c in _checks(r) if c.get("status") != "pass"],
        "not_proven": [str(n) for n in _list(r, "not_proven")],
        "next_safe_action": str(r.get("next_safe_action", "")),
        "started_at": str(r.get("started_at", "")),
        "finished_at": str(r.get("finished_at", "")),
    }


def _md(text) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_md(summary: dict) -> str:
    lines = ["# semantic-crosscheck report", "",
             f"Overall: **{summary['status'].upper()}**"
             + (f", config `{_md(summary['config'])}`" if summary["config"] else "")
             + f", generated {summary['generated_at']}", "",
             "| Stage | Status | Pass | Fail | Error | Not run | Not proven | Finished |",
             "|---|---|---:|---:|---:|---:|---:|---|"]
    for stage in PIPELINE:
        s = summary["stages"].get(stage)
        if s is None:
            lines.append(f"| {stage} | not run | | | | | | |")
            continue
        c = s["counts"]
        lines.append(f"| {stage} | {s['status']} | {c['pass']} | {c['fail']} | {c['error']} | {c['not_run']} "
                     f"| {len(s['not_proven'])} | {_md(s['finished_at'])} |")

    lines += ["", "## Checks that did not pass", ""]
    failing = [(stage, f) for stage, s in summary["stages"].items() for f in s["failing"]]
    for stage, f in failing:
        fix = f", fix `{_md(f['config_ref'])}`" if f["config_ref"] else ""
        brief = f": {_md(f['detail'])}" if f["detail"] else ""
        lines.append(f"- {stage}: `{f['status']}` `{_md(f['id'])}`{fix}{brief}")
    if not failing:
        lines.append("None.")

    lines += ["", "## Not proven", ""]
    gaps = [f"{stage}: {n}" for stage, s in summary["stages"].items() for n in s["not_proven"]]
    gaps += [f"report: {n}" for n in summary["not_proven"]]
    lines += [f"- {_md(n)}" for n in gaps] or ["Nothing listed."]

    lines += ["", "## Next safe actions", ""]
    actions = [f"{stage}: {s['next_safe_action']}" for stage, s in summary["stages"].items() if s["next_safe_action"]]
    actions.append(f"report: {summary['next_safe_action']}")
    lines += [f"- {_md(a)}" for a in actions]

    lines += ["", "## Trusted code", "", TRUSTED_NOTE, ""]
    trusted = summary["trusted_sql"]
    lines += [f"- shape `{_md(s)}` (sql_violation)" for s in trusted["shapes"]]
    lines += [f"- gold `{_md(g['id'])}`: `{_md(g['sql'])}`" for g in trusted["gold"]]
    if not trusted["shapes"] and not trusted["gold"]:
        lines.append("- none in this configuration")
    return "\n".join(lines) + "\n"


def run(cfg, args, out: Path) -> Result:
    out = Path(out)
    res = Result("report")
    results = read_results(out)
    statuses = {}
    for stage, r in results.items():
        statuses[stage] = stage_status(r)
        first_bad = next((c for c in _checks(r) if c.get("status") != "pass"), {})
        res.add(f"stage:{stage}", statuses[stage], str(first_bad.get("config_ref", "")), **counts(r),
                not_proven=len(_list(r, "not_proven")))
    if not statuses:
        res.add("stages", "not_run", "", found=0)
    missing = [s for s in PIPELINE if s not in statuses]
    stale = stale_pairs(results)
    res.not_proven = [f"stage {s} was not run (no {s}/result.json)" for s in missing]
    res.not_proven += [f"{s} ran before the latest {dep} run finished; rerun {s}" for s, dep in stale]
    res.next_safe_action = _next_action(statuses, results, missing, stale)

    summary = {
        "status": res.status,
        "generated_at": time.strftime(_TIME),
        "config": cfg.path.name if cfg is not None else "",
        "stages": {stage: _stage_summary(r, statuses[stage]) for stage, r in results.items()},
        "not_run": missing,
        "stale": [{"stage": s, "after": dep} for s, dep in stale],
        "not_proven": res.not_proven,
        "trusted_sql": _trusted(cfg),
        "next_safe_action": res.next_safe_action,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    (out / "report.md").write_text(render_md(summary), encoding="utf-8", newline="\n")
    return res
