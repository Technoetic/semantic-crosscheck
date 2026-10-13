"""report stage: one summary of every stage result, for people (report.md) and machines (report.json).

Each stage judges only its own checks, but the question a reviewer or an agent asks is "is the whole pipeline
green for the current data and model, and if not, what do I fix first?". A report that passes because a stage
never ran, or ran before the data it checked was reloaded, would answer that question wrongly. So:
  - Required stages are build, cube and verify, plus reason when the configuration has an [ontology] or any
    [[shapes]] (every stage when there is no configuration to tell). Each gets a `stage:<name>` check: its status
    recomputed from its checks (fail-closed, so a hand-edited result cannot look better), `not_run` with
    reason=missing when it has no result, and at least `not_run` with reason=stale when it is older than a stage it
    depends on. A stage whose own result is not_run (a narrowed run, a build with nothing to build) is no pass.
  - Staleness follows DEPENDS: build after demo; reason and cube after build; verify after build and cube. A stage
    that started before a dependency finished its latest run is stale, so is every stage that depends on a stale
    one, and so is a stage whose run times cannot be read (freshness cannot be shown).
  - doctor and demo are optional: a stage that did not run is not required, but a result that is present and failed
    still fails the report (a broken privilege model or a failed load is real). An optional stage without a
    decisive result (not_run, or stale) is listed as not proven and does not count.
  - The report lists the checks that did not pass with the crosscheck.toml entry to fix, every not_proven line and
    every next_safe_action, and names the trusted code: `sql_violation` predicates and gold SQL run as written.
The overall status can therefore only be pass when every required stage ran, is current and passed.

Everything comes from the stage results, which hold counts and ids only: the report never touches the database.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from .result import STATUSES, Result, inputs_fingerprint, load_result

PIPELINE = ("doctor", "demo", "build", "reason", "cube", "verify")
DEPENDS = {"build": ("demo",), "reason": ("build",), "cube": ("build",), "verify": ("build", "cube")}
ALWAYS_REQUIRED = ("build", "cube", "verify")
SEVERITY = {"pass": 0, "not_run": 1, "fail": 2, "error": 3}
STALE_WHY = {
    "newer": "{stage} ran before the latest {dep} run finished",
    "stale_dependency": "{stage} depends on {dep}, which is stale",
    "no_timestamp": "{stage} or {dep} has no readable run time, so {stage} cannot be shown to be current",
    "inputs_changed": "{stage} ran with a different {dep} than the current files",
}
# which recorded inputs a stage depends on (result.inputs_fingerprint keys)
INPUTS = {"build": ("config_sha256",), "reason": ("config_sha256",), "cube": ("config_sha256",),
          "verify": ("config_sha256", "gold_sha256")}
_INPUT_NAMES = {"config_sha256": "crosscheck.toml", "gold_sha256": "gold SQL"}
TRUSTED_NOTE = ("`sql_violation` predicates and gold SQL files are trusted code from the configuration: the harness "
                "runs them as written (values only as bound parameters) and does not generate or validate them, so "
                "review them like code.")
_TIME = "%Y-%m-%dT%H:%M:%S%z"


def _list(r: dict, key: str) -> list:
    v = r.get(key)
    return v if isinstance(v, list) else []


def _checks(r: dict) -> list[dict]:
    return [c for c in _list(r, "checks") if isinstance(c, dict)]


def _worst(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


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


def required_stages(cfg) -> list[str]:
    """build, cube and verify; reason too when the configuration has an [ontology] or [[shapes]] (or is unknown)."""
    reason = cfg is None or bool(cfg.ontology.hierarchy) or bool(cfg.shapes)
    return [s for s in PIPELINE if s in ALWAYS_REQUIRED or (s == "reason" and reason)]


def _brief(detail) -> str:
    if not isinstance(detail, dict):
        return ""
    return " ".join(f"{k}={v}" for k, v in detail.items() if not isinstance(v, (dict, list)))[:200]


def _when(text) -> datetime | None:
    try:
        return datetime.strptime(text, _TIME)
    except (TypeError, ValueError):
        return None


def staleness(results: dict, current_inputs: dict | None = None) -> dict:
    """{stage: {"after": dependency, "why": key of STALE_WHY}} for every stale stage, decided in pipeline order so
    that a stage depending on a stale stage is stale too. With current_inputs (result.inputs_fingerprint of the
    current configuration), a stage that recorded different or no inputs is stale as well."""
    stale = {}
    for stage in PIPELINE:
        if stage not in results:
            continue
        if current_inputs:
            recorded = results[stage].get("inputs") or {}
            changed = [k for k in INPUTS.get(stage, ()) if recorded.get(k) != current_inputs.get(k)]
            if changed:
                stale[stage] = {"after": _INPUT_NAMES[changed[0]], "why": "inputs_changed"}
                continue
        started = _when(results[stage].get("started_at"))
        for dep in DEPENDS.get(stage, ()):
            if dep not in results:
                continue
            finished = _when(results[dep].get("finished_at"))
            if started is None or finished is None:
                why = "no_timestamp"
            elif finished > started:
                why = "newer"
            elif dep in stale:
                why = "stale_dependency"
            else:
                continue
            stale[stage] = {"after": dep, "why": why}
            break
    return stale


def stale_text(stage: str, info: dict) -> str:
    return STALE_WHY[info["why"]].format(stage=stage, dep=info["after"])


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


def counted_status(status: str, required: bool, stale: dict | None) -> str | None:
    """The status a stage contributes to the overall verdict, or None when an optional stage does not count."""
    if required:
        return _worst(status, "not_run") if stale else status
    if stale or status == "not_run":
        return None
    return status


def _trusted(cfg) -> dict:
    if cfg is None:
        return {"shapes": [], "gold": []}
    return {"shapes": [s.id for s in cfg.shapes if s.sql_violation],
            "gold": [{"id": g.id, "sql": g.sql} for g in cfg.gold]}


def _add_stage_checks(res: Result, results: dict, statuses: dict, required: list[str], stale: dict,
                      counted: dict) -> None:
    for stage in PIPELINE:
        if stage not in results:
            if stage in required:
                res.add(f"stage:{stage}", "not_run", "", reason="missing", required=True)
            elif stage == "doctor":
                res.not_proven.append("stage doctor was not run, so the check role's privileges (read-only on the "
                                      "source tables) and the optional components were not verified")
            continue
        r = results[stage]
        if counted[stage] is None:
            why = stale_text(stage, stale[stage]) if stage in stale else f"its own status is {statuses[stage]}"
            res.not_proven.append(f"stage {stage} is optional for this configuration and has no decisive result ({why}); "
                                  "it does not count towards the overall status")
            continue
        first_bad = next((c for c in _checks(r) if c.get("status") != "pass"), {})
        detail = {**counts(r), "not_proven": len(_list(r, "not_proven")), "required": stage in required}
        if stage in stale:
            detail.update(reason="stale", after=stale[stage]["after"], why=stale[stage]["why"], last_status=statuses[stage])
        res.add(f"stage:{stage}", counted[stage], str(first_bad.get("config_ref", "")), **detail)


def _next_action(results: dict, counted: dict, required: list[str], stale: dict) -> str:
    """The first thing to do, in pipeline order: fix a stage that is current and did not pass, or (re)run the stages
    that are stale or missing."""
    if not results:
        return f"run `sxc.py doctor`, then {', '.join(required)} (for the demo: demo before build), then rerun report"
    todo = [s for s in PIPELINE if (s in required and s not in results) or (s in stale and counted.get(s) is not None)]
    for stage in PIPELINE:
        if stage in todo:
            return _rerun_action(todo, results)
        status = counted.get(stage)
        if status not in (None, "pass"):
            hint = results[stage].get("next_safe_action") or f"fix its failing checks and rerun `sxc.py {stage}`"
            return f"fix {stage} first ({status}): {hint}"
    return ("every required stage ran after the stages it depends on and passed; rerun the pipeline after changing "
            "crosscheck.toml or the source data")


def _rerun_action(todo: list[str], results: dict) -> str:
    stale = [s for s in todo if s in results]
    missing = [s for s in todo if s not in results]
    if stale and missing:
        return (f"rerun the stale stages ({', '.join(stale)}) and run the stages that have not run yet "
                f"({', '.join(missing)}), in the order {', '.join(todo)}, then rerun report")
    if stale:
        return f"rerun the stale stages ({', '.join(stale)}), in this order, then rerun report"
    return f"run the stages that have not run yet ({', '.join(missing)}), in this order, then rerun report"


def _stage_summary(r: dict, status: str, required: bool, stale: dict | None, counted: str | None) -> dict:
    return {
        "status": status,
        "counted_as": counted,
        "required": required,
        "stale": dict(stale) if stale else None,
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


def _status_cell(stage: str, summary: dict) -> str:
    s = summary["stages"].get(stage)
    if s is None:
        return "not run (required)" if stage in summary["required"] else "not run (optional)"
    cell = s["status"]
    if s["stale"]:
        cell += f", stale (after {s['stale']['after']})"
    if s["counted_as"] is None:
        cell += ", optional, not counted"
    return cell


def render_md(summary: dict) -> str:
    lines = ["# semantic-crosscheck report", "",
             f"Overall: **{summary['status'].upper()}**"
             + (f", config `{_md(summary['config'])}`" if summary["config"] else "")
             + f", generated {summary['generated_at']}", "",
             f"Required stages: {', '.join(summary['required'])}. The overall status passes only when each of them "
             "ran after the stages it depends on and passed.", "",
             "| Stage | Status | Pass | Fail | Error | Not run | Not proven | Finished |",
             "|---|---|---:|---:|---:|---:|---:|---|"]
    for stage in PIPELINE:
        s = summary["stages"].get(stage)
        if s is None:
            lines.append(f"| {stage} | {_status_cell(stage, summary)} | | | | | | |")
            continue
        c = s["counts"]
        lines.append(f"| {stage} | {_md(_status_cell(stage, summary))} | {c['pass']} | {c['fail']} | {c['error']} "
                     f"| {c['not_run']} | {len(s['not_proven'])} | {_md(s['finished_at'])} |")

    lines += ["", "## Missing or stale stages", ""]
    stale = {item["stage"]: item for item in summary["stale"]}
    blockers = []
    for stage in PIPELINE:
        if stage in summary["missing_required"]:
            blockers.append(f"- {stage}: required, but it has not run (no {stage}/result.json)")
        elif stage in stale and summary["stages"][stage]["counted_as"] is not None:
            blockers.append(f"- {stage}: stale ({_md(stale_text(stage, stale[stage]))}); rerun it")
    lines += blockers or ["None."]

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
    required = required_stages(cfg)
    stale = staleness(results, inputs_fingerprint(cfg))
    statuses = {stage: stage_status(r) for stage, r in results.items()}
    counted = {stage: counted_status(statuses[stage], stage in required, stale.get(stage)) for stage in results}
    _add_stage_checks(res, results, statuses, required, stale, counted)
    res.next_safe_action = _next_action(results, counted, required, stale)

    summary = {
        "status": res.status,
        "generated_at": time.strftime(_TIME),
        "config": cfg.path.name if cfg is not None else "",
        "required": required,
        "stages": {stage: _stage_summary(r, statuses[stage], stage in required, stale.get(stage), counted[stage])
                   for stage, r in results.items()},
        "not_run": [s for s in PIPELINE if s not in results],
        "missing_required": [s for s in required if s not in results],
        "stale": [{"stage": s, **info} for s, info in stale.items()],
        "not_proven": res.not_proven,
        "trusted_sql": _trusted(cfg),
        "next_safe_action": res.next_safe_action,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    (out / "report.md").write_text(render_md(summary), encoding="utf-8", newline="\n")
    return res
