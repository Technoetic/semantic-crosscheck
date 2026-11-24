"""Compare two recorded release-development runs against a frozen plan, offline.

Selectively adapts pre-registered plan sealing, dual-verdict post-hoc amendment
disclosure, and treatment-activation / control zero-leakage silent no-op audits
informed by ai3inc/foldboost (commit ff7ad2208c, Apache-2.0), implemented from
scratch in the Python standard library.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from statistics import NormalDist


EXPOSURES = ("heldout", "development", "unknown")
EXECUTED = ("clear", "flag")
AMENDMENT_TIMINGS = ("pre_measurement", "post_hoc")
EXIT_ACTIVATION_FAILURE = 70
MAX_FIXTURE_BYTES = 1024 * 1024
_MAX_REFINEMENT_ROUNDS = 8
_MAX_INDIVIDUALIZE_STEPS = 16
_MAX_LIST_CONTEXT_ITEMS = 64
TRANSITIONS = {
    ("error", "clear", "flag"): "added_detections",
    ("clean", "clear", "flag"): "new_false_positives",
    ("error", "flag", "clear"): "lost_detections",
    ("clean", "flag", "clear"): "fixed_false_positives",
}
METRICS = ("cases", *TRANSITIONS.values(), "inconclusive")
REQUIRED_TRUTH = {metric: truth for (truth, _, _), metric in TRANSITIONS.items()}
MIN_DISCORDANT_MIDP_005 = 5
MIN_DISCORDANT_EXACT_005 = 6
MIN_DISCORDANT_E_005 = 7
_LOG_BETA_HALF_HALF = 2.0 * math.lgamma(0.5) - math.lgamma(1.0)
_MAX_SAFE_EXP = 700.0
Z_975 = NormalDist().inv_cdf(0.975)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CORE_KEYS = (
    "schema_version", "experiment", "added_check", "baseline_checks",
    "budget", "cases", "registered_claims", "claim_limit", "frozen_utc",
)


def unique_object(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError(f"duplicate JSON key: {key!r}")
        obj[key] = value
    return obj


def invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_input(path, name, digests):
    raw = path.read_bytes()
    digests[name] = hashlib.sha256(raw).hexdigest()
    return raw.decode("utf-8-sig")


def read_object(path, name, digests):
    try:
        data = json.loads(read_input(path, name, digests), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")
    return data


def identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def array(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def parse_utc(value, field) -> datetime:
    text = identifier(value, field)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset or Z")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a valid ISO-8601 UTC timestamp") from exc


def plan_core_sha256(plan: dict) -> str:
    """Canonical SHA-256 of the immutable pre-registered fields of a plan."""
    core = {key: plan[key] for key in _CORE_KEYS if key in plan}
    raw = json.dumps(core, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def check_ids(value, field):
    items = array(value, field)
    ids = {identifier(item, field) for item in items}
    if len(ids) != len(items):
        raise ValueError(f"duplicate check in {field}")
    return ids


def case_rows(value, field):
    rows = {}
    for row in array(value, field):
        if not isinstance(row, dict):
            raise ValueError(f"{field} must contain objects")
        case_id = identifier(row.get("id"), f"{field}.id")
        if case_id in rows:
            raise ValueError(f"duplicate case ID in {field}: {case_id!r}")
        rows[case_id] = row
    return rows


def validate_header(data, name):
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError(f"{name}.schema_version must be 1")
    if type(data.get("budget")) is not int or data["budget"] < 1:
        raise ValueError(f"{name}.budget must be a positive integer")


def claim_path(key):
    group, _, metric = identifier(key, "claim path").partition(".")
    if group not in EXPOSURES or metric not in METRICS:
        raise ValueError(f"unknown claim: {key!r}")
    return group, metric


def _validate_amendments(plan: dict, cases: dict, frozen_dt: datetime | None) -> list[dict]:
    raw_items = array(plan["amendments"], "plan.amendments")
    if "frozen_core_sha256" not in plan:
        raise ValueError("plan.amendments requires frozen_core_sha256 to seal pre-registered core")
    seen_ids: set[str] = set()
    parsed: list[dict] = []
    all_excluded: set[str] = set()
    for index, entry in enumerate(raw_items):
        prefix = f"plan.amendments[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{prefix} must be an object")
        aid = identifier(entry.get("id"), f"{prefix}.id")
        if aid in seen_ids:
            raise ValueError(f"duplicate amendment ID: {aid!r}")
        seen_ids.add(aid)
        timing = entry.get("timing")
        if not isinstance(timing, str) or timing not in AMENDMENT_TIMINGS:
            raise ValueError(f"{prefix}.timing must be one of {AMENDMENT_TIMINGS}")
        recorded_dt = parse_utc(entry.get("recorded_utc"), f"{prefix}.recorded_utc")
        if frozen_dt is not None and recorded_dt < frozen_dt:
            raise ValueError(f"{prefix}.recorded_utc precedes plan.frozen_utc")
        identifier(entry.get("what_changed"), f"{prefix}.what_changed")
        identifier(entry.get("why"), f"{prefix}.why")
        excluded = _validate_excluded_cases(entry.get("exclude_case_ids", []), cases, f"{prefix}.exclude_case_ids")
        all_excluded.update(excluded)
        parsed.append({"id": aid, "timing": timing, "recorded_utc": entry["recorded_utc"],
                       "recorded_dt": recorded_dt, "exclude_case_ids": excluded})
    if all_excluded and len(all_excluded) >= len(cases):
        raise ValueError("plan.amendments cannot exclude every case in plan")
    return parsed


def _validate_excluded_cases(value, cases: dict, field: str) -> list[str]:
    items = array(value, field)
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cid = identifier(item, field)
        if cid in seen:
            raise ValueError(f"duplicate case ID in {field}: {cid!r}")
        if cid not in cases:
            raise ValueError(f"unknown case ID in {field}: {cid!r}")
        seen.add(cid)
        out.append(cid)
    return out


def validate_plan(plan):
    validate_header(plan, "plan")
    identifier(plan.get("experiment"), "experiment")
    added = identifier(plan.get("added_check"), "added_check")
    checks = check_ids(plan.get("baseline_checks"), "baseline_checks")
    if added in checks:
        raise ValueError("added_check already belongs to baseline_checks")
    if "claim_limit" in plan:
        identifier(plan["claim_limit"], "plan.claim_limit")
    frozen_dt = parse_utc(plan["frozen_utc"], "plan.frozen_utc") if "frozen_utc" in plan else None
    if "registered_claims" in plan:
        registered = array(plan["registered_claims"], "registered_claims")
        paths = {claim_path(key) for key in registered}
        if not paths or len(paths) != len(registered):
            raise ValueError("registered_claims must contain at least one path and no duplicate paths")
    cases = case_rows(plan.get("cases"), "cases")
    if not cases:
        raise ValueError("plan must contain at least one case")
    seen = set()
    for case_id, case in cases.items():
        digest = case.get("fixture_sha256")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise ValueError(f"{case_id!r}: fixture_sha256 must be a lowercase SHA-256 hex digest")
        if digest in seen:
            raise ValueError(f"{case_id!r}: fixture_sha256 reused across case IDs")
        seen.add(digest)
        if case.get("expected") not in ("error", "clean") or case.get("exposure") not in EXPOSURES:
            raise ValueError(f"{case_id!r}: invalid expected or exposure")
    if "frozen_core_sha256" in plan:
        claimed_core = plan["frozen_core_sha256"]
        if not isinstance(claimed_core, str) or not _HEX64.fullmatch(claimed_core):
            raise ValueError("plan.frozen_core_sha256 must be a lowercase SHA-256 hex digest")
        if claimed_core != plan_core_sha256(plan):
            raise ValueError("plan.frozen_core_sha256 does not match pre-registered core fields")
    if "amendments" in plan:
        _validate_amendments(plan, cases, frozen_dt)
    return cases


def _validate_activation_field(row: dict, label: str) -> None:
    if "activation" not in row:
        return
    act = row["activation"]
    if not isinstance(act, dict):
        raise ValueError(f"{label}.activation must be an object")
    calls = act.get("added_check_calls")
    if type(calls) is not int or calls < 0:
        raise ValueError(f"{label}.activation.added_check_calls must be a nonnegative integer")
    if "fallback_calls" in act:
        fb = act["fallback_calls"]
        if type(fb) is not int or fb < 0:
            raise ValueError(f"{label}.activation.fallback_calls must be a nonnegative integer")


def validate_receipt(receipt, arm, plan, digest, cases):
    validate_header(receipt, arm)
    if receipt.get("arm") != arm:
        raise ValueError(f"{arm}.arm must be {arm!r}")
    if receipt.get("plan_sha256") != digest:
        raise ValueError(f"{arm}.plan_sha256 differs from supplied plan bytes")
    if receipt["budget"] != plan["budget"]:
        raise ValueError(f"{arm}.budget differs from plan")
    if "measured_utc" in receipt:
        measured_dt = parse_utc(receipt["measured_utc"], f"{arm}.measured_utc")
        if "frozen_utc" in plan and measured_dt < parse_utc(plan["frozen_utc"], "plan.frozen_utc"):
            raise ValueError(f"{arm}.measured_utc precedes plan.frozen_utc")
    checks = set(plan["baseline_checks"])
    if arm == "candidate":
        checks.add(plan["added_check"])
    if check_ids(receipt.get("checks"), f"{arm}.checks") != checks:
        raise ValueError(f"{arm}.checks differs from planned set")
    rows = case_rows(receipt.get("observations"), f"{arm}.observations")
    if rows.keys() != cases.keys():
        missing = sorted(cases.keys() - rows.keys())
        extra = sorted(rows.keys() - cases.keys())
        raise ValueError(f"{arm}.observations case IDs differ: missing={missing!r}, extra={extra!r}")
    for case_id, row in rows.items():
        if row.get("fixture_sha256") != cases[case_id]["fixture_sha256"]:
            raise ValueError(f"{arm} {case_id!r}: fixture_sha256 differs from plan")
        if row.get("status") not in (*EXECUTED, "not_run", "error"):
            raise ValueError(f"{arm} {case_id!r}: invalid observation status")
        _validate_activation_field(row, f"{arm} {case_id!r}")
    if sum(row["status"] != "not_run" for row in rows.values()) > plan["budget"]:
        raise ValueError(f"{arm}: attempted case count exceeds budget")
    return rows


def _check_amendment_chronology(amendments: list[dict], receipts: dict[str, dict]) -> None:
    measured_dts = [
        parse_utc(receipt["measured_utc"], f"{arm}.measured_utc")
        for arm, receipt in receipts.items()
        if "measured_utc" in receipt
    ]
    if not measured_dts:
        return
    earliest = min(measured_dts)
    for entry in amendments:
        if entry["timing"] == "pre_measurement" and entry["recorded_dt"] > earliest:
            raise ValueError(
                f"pre_measurement amendment {entry['id']!r} dated after receipt measured_utc"
            )


def _round6(value: float) -> float:
    rounded = round(value, 6)
    return 0.0 if rounded == 0.0 else rounded


def _pow2_ratio(numerator: int, exponent: int) -> float:
    """Compute numerator / 2**exponent without IEEE-754 overflow for large m."""
    if numerator == 0:
        return 0.0
    shift = max(0, numerator.bit_length() - 1000)
    return math.ldexp(float(numerator >> shift), shift - exponent)


def mcnemar_pvalues(b: int, c: int) -> tuple[float, float]:
    """Two-sided exact conditional and Mid-P McNemar p-values (Fagerland et al., 2013)."""
    if b < 0 or c < 0:
        raise ValueError("discordant counts must be nonnegative")
    m = b + c
    if m == 0:
        return 1.0, 1.0
    if b == c:
        exact_p = 1.0
        midp = 1.0 - _pow2_ratio(math.comb(m, b), m + 1)
        return exact_p, midp
    k = min(b, c)
    tail_strict = sum(math.comb(m, i) for i in range(k))
    point = math.comb(m, k)
    exact_p = min(1.0, _pow2_ratio(tail_strict + point, m - 1))
    midp = min(1.0, _pow2_ratio(2 * tail_strict + point, m))
    return exact_p, midp


def _wilson_bounds(successes: int, total: int, z: float) -> tuple[float, float]:
    p = successes / total
    z2 = z * z
    denom = 2.0 * (total + z2)
    center = 2.0 * total * p + z2
    half = z * math.sqrt(z2 + 4.0 * total * p * (1.0 - p))
    return (center - half) / denom, (center + half) / denom


def newcombe_paired_ci95(a: int, b: int, c: int, d: int) -> list[float]:
    """Newcombe (1998) Method 10 95% Wilson-score CI for candidate - baseline rate difference."""
    if a < 0 or b < 0 or c < 0 or d < 0:
        raise ValueError("cell counts must be nonnegative")
    n = a + b + c + d
    if n <= 0:
        raise ValueError("total count must be positive")
    p1 = (a + b) / n
    p2 = (a + c) / n
    diff = (b - c) / n
    l1, u1 = _wilson_bounds(a + b, n, Z_975)
    l2, u2 = _wilson_bounds(a + c, n, Z_975)
    denom_prod = (a + b) * (c + d) * (a + c) * (b + d)
    if denom_prod == 0:
        phi = 0.0
    else:
        cross = a * d - b * c
        num = max(cross - n / 2.0, 0.0) if cross > 0 else float(cross)
        phi = num / math.sqrt(denom_prod)
    delta_sq = (p1 - l1) ** 2 - 2.0 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2
    eps_sq = (u1 - p1) ** 2 - 2.0 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2
    lower = max(-1.0, diff - math.sqrt(max(0.0, delta_sq)))
    upper = min(1.0, diff + math.sqrt(max(0.0, eps_sq)))
    return [_round6(lower), _round6(upper)]


def safe_mcnemar_log_evalue(b: int, c: int) -> float:
    """Natural log of the exact Jeffreys Beta(1/2, 1/2) mixture e-value for H0: theta = 0.5."""
    if b < 0 or c < 0:
        raise ValueError("discordant counts must be nonnegative")
    m = b + c
    if m == 0:
        return 0.0
    hi, lo = (b, c) if b >= c else (c, b)
    return (m * math.log(2.0) + math.lgamma(hi + 0.5) + math.lgamma(lo + 0.5)
            - math.lgamma(m + 1.0) - _LOG_BETA_HALF_HALF)


def safe_mcnemar_evalue(b: int, c: int) -> tuple[float | None, float, float]:
    """Return (e_value_or_none_if_overflow, log_e_value, anytime_valid_p)."""
    log_e = safe_mcnemar_log_evalue(b, c)
    e_val = _round6(math.exp(log_e)) if log_e <= _MAX_SAFE_EXP else None
    anytime_p = _round6(min(1.0, math.exp(-log_e))) if log_e > 0.0 else 1.0
    return e_val, _round6(log_e), anytime_p


def _paired_stats(a: int, b: int, c: int, d: int, lost_key: str) -> dict[str, object]:
    n = a + b + c + d
    discordant = b + c
    base: dict[str, object] = {
        "n": n,
        "both_flag": a,
        "added": b,
        lost_key: c,
        "both_clear": d,
        "discordant": discordant,
        "baseline_rate": _round6((a + c) / n) if n else None,
        "candidate_rate": _round6((a + b) / n) if n else None,
        "rate_diff": _round6((b - c) / n) if n else None,
        "diff_ci_95": newcombe_paired_ci95(a, b, c, d) if n else None,
        "exact_mcnemar_p": None,
        "midp_mcnemar_p": None,
        "safe_e_value": None,
        "safe_e_log": None,
        "safe_anytime_p": None,
        "min_discordant_for_midp_005": MIN_DISCORDANT_MIDP_005,
        "min_discordant_for_exact_005": MIN_DISCORDANT_EXACT_005,
        "min_discordant_for_e_005": MIN_DISCORDANT_E_005,
        "underpowered_for_midp_005": discordant < MIN_DISCORDANT_MIDP_005,
        "underpowered_for_exact_005": discordant < MIN_DISCORDANT_EXACT_005,
        "underpowered_for_e_005": discordant < MIN_DISCORDANT_E_005,
    }
    if n:
        exact_p, midp = mcnemar_pvalues(b, c)
        e_val, log_e, anytime_p = safe_mcnemar_evalue(b, c)
        base["exact_mcnemar_p"] = exact_p
        base["midp_mcnemar_p"] = midp
        base["safe_e_value"] = e_val
        base["safe_e_log"] = log_e
        base["safe_anytime_p"] = anytime_p
    return base


def summarize(cases, baseline, candidate):
    members = {exposure: {metric: [] for metric in METRICS} for exposure in EXPOSURES}
    cells = {
        exposure: {truth: [0, 0, 0, 0] for truth in ("error", "clean")}
        for exposure in EXPOSURES
    }
    pairs = []
    for case_id, case in cases.items():
        before = baseline[case_id]["status"]
        after = candidate[case_id]["status"]
        truth = case["expected"]
        exposure = case["exposure"]
        if before in EXECUTED and after in EXECUTED:
            outcome = TRANSITIONS.get((truth, before, after), "unchanged")
            bucket = cells[exposure][truth]
            if before == "flag" and after == "flag":
                bucket[0] += 1
            elif before == "clear" and after == "flag":
                bucket[1] += 1
            elif before == "flag" and after == "clear":
                bucket[2] += 1
            else:
                bucket[3] += 1
        else:
            outcome = "inconclusive"
        members[exposure]["cases"].append(case_id)
        if outcome != "unchanged":
            members[exposure][outcome].append(case_id)
        pairs.append(dict(id=case_id, fixture_sha256=case["fixture_sha256"], expected=truth,
                          exposure=exposure, baseline=before, candidate=after, outcome=outcome))
    groups = {}
    for exposure, ids in members.items():
        counts = {metric: len(cases_for_metric) for metric, cases_for_metric in ids.items()}
        err = cells[exposure]["error"]
        cln = cells[exposure]["clean"]
        statistics = {
            "detections": _paired_stats(err[0], err[1], err[2], err[3], "lost"),
            "false_positives": _paired_stats(cln[0], cln[1], cln[2], cln[3], "fixed"),
        }
        groups[exposure] = dict(counts, case_ids=ids, statistics=statistics)
    return groups, pairs


def audit_activation(cases: dict, baseline: dict, candidate: dict) -> dict[str, object]:
    """Verify treatment activation on executed candidate cases and zero leakage in baseline."""
    missing_baseline = [cid for cid in cases if baseline[cid]["status"] in EXECUTED and "activation" not in baseline[cid]]
    missing_candidate = [cid for cid in cases if candidate[cid]["status"] in EXECUTED and "activation" not in candidate[cid]]
    base_calls = 0
    base_leaked: list[str] = []
    for cid in cases:
        act = baseline[cid].get("activation")
        if act is not None:
            calls = act["added_check_calls"]
            base_calls += calls
            if calls > 0:
                base_leaked.append(cid)
    cand_executed = [cid for cid in cases if candidate[cid]["status"] in EXECUTED]
    cand_calls = 0
    cand_fallbacks = 0
    cand_inactive: list[str] = []
    for cid in cases:
        act = candidate[cid].get("activation")
        if act is not None:
            calls = act["added_check_calls"]
            cand_calls += calls
            cand_fallbacks += act.get("fallback_calls", 0)
            if candidate[cid]["status"] in EXECUTED and calls == 0:
                cand_inactive.append(cid)
    control_leakage = bool(base_leaked)
    missing = bool(missing_baseline or missing_candidate)
    silent_noop = (not missing) and (not cand_executed or bool(cand_inactive) or cand_calls == 0)
    if missing:
        status = "missing_telemetry"
    elif silent_noop and control_leakage:
        status = "both_failed"
    elif silent_noop:
        status = "silent_noop"
    elif control_leakage:
        status = "control_leakage"
    else:
        status = "passed"
    return {
        "schema": "semantic-crosscheck/activation-audit/v1",
        "status": status,
        "passed": status == "passed",
        "baseline_added_check_calls": base_calls,
        "baseline_leaked_case_ids": base_leaked,
        "candidate_executed_cases": len(cand_executed),
        "candidate_added_check_calls": cand_calls,
        "candidate_fallback_calls": cand_fallbacks,
        "candidate_inactive_executed_case_ids": cand_inactive,
        "missing_baseline_case_ids": missing_baseline,
        "missing_candidate_case_ids": missing_candidate,
    }


def _build_amendment_audit(plan: dict, cases: dict, baseline: dict, candidate: dict,
                           original_groups: dict, amendments: list[dict]) -> dict[str, object]:
    pre_excluded = sorted({cid for a in amendments if a["timing"] == "pre_measurement" for cid in a["exclude_case_ids"]})
    post_excluded = sorted({cid for a in amendments if a["timing"] == "post_hoc" for cid in a["exclude_case_ids"]})
    all_excluded = set(pre_excluded) | set(post_excluded)
    kept_cases = {cid: row for cid, row in cases.items() if cid not in all_excluded}
    amended_groups, _ = summarize(kept_cases, baseline, candidate)
    has_post_hoc = any(a["timing"] == "post_hoc" for a in amendments)
    pre_only_cases = {cid: row for cid, row in cases.items() if cid not in set(pre_excluded)}
    pre_groups, _ = summarize(pre_only_cases, baseline, candidate)
    post_hoc_altered = has_post_hoc and (amended_groups != pre_groups or bool(post_excluded))
    return {
        "frozen_core_sha256": plan["frozen_core_sha256"],
        "count": len(amendments),
        "pre_measurement_count": sum(a["timing"] == "pre_measurement" for a in amendments),
        "post_hoc_count": sum(a["timing"] == "post_hoc" for a in amendments),
        "has_post_hoc": has_post_hoc,
        "pre_measurement_excluded_case_ids": pre_excluded,
        "post_hoc_excluded_case_ids": post_excluded,
        "post_hoc_altered_summary": post_hoc_altered,
        "original_groups": original_groups,
        "amended_groups": amended_groups,
    }


def verify_claims(claims, groups, pairs, registered):
    if not claims:
        raise ValueError("claims must contain at least one numeric claim")
    if registered is not None and set(registered) != claims.keys():
        raise ValueError("claim paths differ from plan.registered_claims")
    comparable = {(p["exposure"], p["expected"]) for p in pairs if p["outcome"] != "inconclusive"}
    observed = {}
    mismatches = {}
    for key, claimed in claims.items():
        group, metric = claim_path(key)
        if type(claimed) is not int or claimed < 0:
            raise ValueError(f"{key}: claim must be a nonnegative integer")
        if not groups[group]["cases"]:
            raise ValueError(f"{key}: group has no cases")
        if metric in REQUIRED_TRUTH and (group, REQUIRED_TRUTH[metric]) not in comparable:
            raise ValueError(f"{key}: no comparable {REQUIRED_TRUTH[metric]} case in group")
        observed[key] = groups[group][metric]
        if observed[key] != claimed:
            mismatches[key] = {"claimed": claimed, "observed": observed[key]}
    return observed, mismatches


def verify_markdown(document, observed):
    start = "<!-- paired-claims:begin -->"
    end = "<!-- paired-claims:end -->"
    lines = document.splitlines()
    for marker in (start, end):
        if document.count(marker) != 1 or marker not in lines:
            raise ValueError(f"Markdown requires one exact standalone {marker} marker")
    first, last = lines.index(start), lines.index(end)
    if first >= last:
        raise ValueError("Markdown claim markers are out of order")
    expected = ["| Claim | Count |", "| --- | ---: |"]
    expected.extend(f"| `{key}` | {observed[key]} |" for key in sorted(observed))
    return lines[first + 1:last] == expected


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and (getattr(st, "st_file_attributes", 0) & reparse_flag))


def _resolve_fixtures_dir(fixtures_dir: Path) -> Path:
    if _is_reparse_or_symlink(fixtures_dir):
        raise ValueError("fixtures-dir must not be a symlink or reparse point")
    try:
        st = os.lstat(fixtures_dir)
    except OSError as exc:
        raise ValueError("fixtures-dir does not exist or is unreadable") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError("fixtures-dir must be a directory")
    return fixtures_dir.resolve(strict=True)


def _safe_case_filename(case_id: str) -> str:
    if (not case_id or "/" in case_id or "\\" in case_id or ":" in case_id
            or "\x00" in case_id or case_id in (".", "..")):
        raise ValueError(f"{case_id!r}: unsafe case ID for fixture filename")
    filename = f"{case_id}.json"
    candidate = Path(filename)
    if candidate.is_absolute() or candidate.name != filename or ".." in candidate.parts:
        raise ValueError(f"{case_id!r}: unsafe case ID for fixture filename")
    return filename


def _load_verified_fixture(base: Path, case_id: str, expected_sha256: str) -> dict:
    filename = _safe_case_filename(case_id)
    path = base / filename
    if _is_reparse_or_symlink(path):
        raise ValueError(f"{case_id!r}: fixture file must not be a symlink or reparse point")
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{case_id!r}: fixture file missing in fixtures-dir") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{case_id!r}: fixture path must be a regular file")
    if path.resolve(strict=True).parent != base:
        raise ValueError(f"{case_id!r}: fixture path escapes fixtures-dir")
    if st.st_size > MAX_FIXTURE_BYTES:
        raise ValueError(f"{case_id!r}: fixture exceeds {MAX_FIXTURE_BYTES} byte limit")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FIXTURE_BYTES + 1)
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ValueError(f"{case_id!r}: fixture exceeds {MAX_FIXTURE_BYTES} byte limit")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(f"{case_id!r}: fixture file SHA-256 differs from plan")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{case_id!r}: invalid fixture JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{case_id!r}: fixture must be a JSON object")
    return data


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _typed_scalar(value: object) -> object:
    if value is None:
        return ["n"]
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, int):
        return ["i", value]
    if isinstance(value, float):
        return ["f", repr(0.0 if value == 0.0 else value)]
    return ["s", value]


def _typed_canonical(node: object) -> object:
    if isinstance(node, dict):
        return {k: _typed_canonical(v) for k, v in sorted(node.items())}
    if isinstance(node, list):
        return [_typed_canonical(item) for item in node]
    return _typed_scalar(node)


def canonical_json_sha256(data: object) -> str:
    """Whitespace- and key-order-invariant SHA-256 preserving exact JSON types and list order."""
    return hashlib.sha256(_compact_json(_typed_canonical(data)).encode("utf-8")).hexdigest()


def _mask_node(node: object, self_str: str, colors: dict[str, int] | None) -> object:
    if isinstance(node, dict):
        return {k: _mask_node(v, self_str, colors) for k, v in sorted(node.items())}
    if isinstance(node, list):
        items = [_mask_node(item, self_str, colors) for item in node]
        return sorted(items, key=_compact_json)
    if isinstance(node, str):
        if node == self_str:
            return ["$SELF"]
        return ["$OTHER"] if colors is None else ["$c", colors[node]]
    return _typed_scalar(node)


def _collect_contexts(node: object, path: tuple[str, ...], colors: dict[str, int] | None,
                      contexts: dict[str, list[str]], *, root: bool = False) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                if not root:
                    ctx = _compact_json([list(path) + [key], _mask_node(node, value, colors)])
                    contexts.setdefault(value, []).append(ctx)
            elif isinstance(value, (dict, list)):
                _collect_contexts(value, path + (key,), colors, contexts, root=False)
    elif isinstance(node, list):
        item_path = path + ("[]",)
        str_counts: dict[str, int] = {}
        for item in node:
            if isinstance(item, str):
                str_counts[item] = str_counts.get(item, 0) + 1
            elif isinstance(item, (dict, list)):
                _collect_contexts(item, item_path, colors, contexts, root=False)
        for token, count in str_counts.items():
            if len(node) <= _MAX_LIST_CONTEXT_ITEMS:
                ctx = _compact_json([list(item_path), _mask_node(node, token, colors)])
            else:
                ctx = _compact_json([list(item_path), len(node), count])
            contexts.setdefault(token, []).extend([ctx] * count)


def _refine_colors(data: object, colors: dict[str, int] | None) -> dict[str, int]:
    contexts: dict[str, list[str]] = {}
    _collect_contexts(data, (), colors, contexts, root=True)
    if not contexts:
        return {}
    signatures: dict[str, tuple[int, tuple[str, ...]]] = {
        token: (0 if colors is None else colors[token], tuple(sorted(ctxs)))
        for token, ctxs in contexts.items()
    }
    unique_sigs = sorted(set(signatures.values()))
    rank = {sig: idx for idx, sig in enumerate(unique_sigs)}
    return {token: rank[sig] for token, sig in signatures.items()}


def _stabilize_colors(data: object, colors: dict[str, int] | None) -> dict[str, int]:
    current = _refine_colors(data, colors)
    if not current:
        return {}
    for _ in range(_MAX_REFINEMENT_ROUNDS):
        refined = _refine_colors(data, current)
        if len(set(refined.values())) == len(set(current.values())):
            return refined
        current = refined
    return current


def _canonical_string_map(data: object) -> dict[str, str]:
    colors = _stabilize_colors(data, None)
    if not colors:
        return {}
    for _ in range(_MAX_INDIVIDUALIZE_STEPS):
        groups: dict[int, list[str]] = {}
        for token, cid in colors.items():
            groups.setdefault(cid, []).append(token)
        tied = next((cid for cid in sorted(groups) if len(groups[cid]) > 1), None)
        if tied is None:
            break
        bumped = dict(colors)
        bumped[groups[tied][0]] = max(colors.values()) + 1
        colors = _stabilize_colors(data, bumped)
    ordered = sorted(colors, key=lambda tok: colors[tok])
    return {tok: f"$s{idx}" for idx, tok in enumerate(ordered)}


def _alpha_normalize(node: object, mapping: dict[str, str], *, root: bool = False) -> object:
    if isinstance(node, dict):
        return {
            k: (["s_top", v] if root and isinstance(v, str) else _alpha_normalize(v, mapping, root=False))
            for k, v in sorted(node.items())
        }
    if isinstance(node, list):
        items = [_alpha_normalize(item, mapping, root=False) for item in node]
        return sorted(items, key=_compact_json)
    if isinstance(node, str):
        return ["s", mapping[node]]
    return _typed_scalar(node)


def alpha_structural_sha256(data: object) -> str:
    """Row-order-independent and domain-string alpha-renamed structural SHA-256."""
    mapping = _canonical_string_map(data)
    normalized = _alpha_normalize(data, mapping, root=True)
    return hashlib.sha256(_compact_json(normalized).encode("utf-8")).hexdigest()


def _duplicate_groups(buckets: dict[str, list[str]], cases: dict) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    for digest in sorted(buckets):
        ids = sorted(buckets[digest])
        if len(ids) < 2:
            continue
        exposures = sorted({cases[cid]["exposure"] for cid in ids})
        groups.append({
            "digest": digest,
            "case_ids": ids,
            "exposures": exposures,
            "cross_split": len(exposures) > 1,
        })
    return groups


def _collect_leakage_ids(all_groups: list[dict[str, object]], cases: dict) -> tuple[list[str], list[str]]:
    cross_ids: set[str] = set()
    within_ids: set[str] = set()
    for group in all_groups:
        ids = group["case_ids"]
        assert isinstance(ids, list)
        if group["cross_split"]:
            cross_ids.update(ids)
        by_exposure: dict[str, list[str]] = {}
        for cid in ids:
            by_exposure.setdefault(cases[cid]["exposure"], []).append(cid)
        for members in by_exposure.values():
            if len(members) >= 2:
                within_ids.update(members)
    return sorted(cross_ids), sorted(within_ids)


def audit_fixture_leakage(cases: dict, fixtures_dir: Path) -> dict[str, object]:
    """Detect cosmetic (canonical JSON) and alpha-renamed structural duplicate fixtures."""
    base = _resolve_fixtures_dir(fixtures_dir)
    canonical_buckets: dict[str, list[str]] = {}
    alpha_buckets: dict[str, list[str]] = {}
    for case_id, case in cases.items():
        data = _load_verified_fixture(base, case_id, case["fixture_sha256"])
        canonical_buckets.setdefault(canonical_json_sha256(data), []).append(case_id)
        alpha_buckets.setdefault(alpha_structural_sha256(data), []).append(case_id)
    canonical_dups = _duplicate_groups(canonical_buckets, cases)
    alpha_dups = _duplicate_groups(alpha_buckets, cases)
    cross_ids, within_ids = _collect_leakage_ids(canonical_dups + alpha_dups, cases)
    if cross_ids and within_ids:
        status = "both_failed"
    elif cross_ids:
        status = "cross_split_leakage"
    elif within_ids:
        status = "within_split_duplicate"
    else:
        status = "passed"
    return {
        "schema": "semantic-crosscheck/fixture-leakage-audit/v1",
        "status": status,
        "passed": status == "passed",
        "cases_checked": len(cases),
        "canonical_duplicate_groups": canonical_dups,
        "alpha_duplicate_groups": alpha_dups,
        "cross_split_case_ids": cross_ids,
        "within_split_case_ids": within_ids,
    }


def seal_plan(args) -> tuple[int, dict[str, object]]:
    if any((args.baseline, args.candidate, args.claims, args.check_markdown,
            args.check_activation, args.strict_original,
            getattr(args, "fixtures_dir", None), getattr(args, "check_fixture_leakage", False))):
        raise ValueError("--seal-plan cannot be combined with comparison options")
    digests: dict[str, str] = {}
    plan = read_object(args.plan, "plan", digests)
    validate_plan(plan)
    return 0, {
        "schema_version": 1,
        "experiment": plan["experiment"],
        "plan_sha256": digests["plan"],
        "frozen_core_sha256": plan_core_sha256(plan),
    }


def _has_any_activation(observations: dict[str, dict]) -> bool:
    return any("activation" in row for row in observations.values())


def compare(args):
    if getattr(args, "seal_plan", False):
        return seal_plan(args)
    if args.baseline is None or args.candidate is None:
        raise ValueError("--baseline and --candidate are required unless --seal-plan is set")
    digests = {}
    plan = read_object(args.plan, "plan", digests)
    cases = validate_plan(plan)
    frozen_dt = parse_utc(plan["frozen_utc"], "plan.frozen_utc") if "frozen_utc" in plan else None
    amendments = _validate_amendments(plan, cases, frozen_dt) if "amendments" in plan else []
    raw_receipts: dict[str, dict] = {}
    observations = {}
    for arm in ("baseline", "candidate"):
        receipt = read_object(getattr(args, arm), arm, digests)
        raw_receipts[arm] = receipt
        observations[arm] = validate_receipt(receipt, arm, plan, digests["plan"], cases)
    if amendments:
        _check_amendment_chronology(amendments, raw_receipts)
    groups, pairs = summarize(cases, **observations)
    report: dict[str, object] = dict(
        schema_version=1, experiment=plan["experiment"], added_check=plan["added_check"],
        baseline_checks=plan["baseline_checks"], budget=plan["budget"],
        input_sha256=digests, groups=groups, pairs=pairs,
    )
    if "claim_limit" in plan:
        report["claim_limit"] = plan["claim_limit"]
    if "frozen_core_sha256" in plan:
        report["frozen_core_sha256"] = plan["frozen_core_sha256"]
    amendment_audit = None
    if "amendments" in plan:
        amendment_audit = _build_amendment_audit(plan, cases, observations["baseline"], observations["candidate"],
                                                 groups, amendments)
        report["amendment_audit"] = amendment_audit
    check_activation = getattr(args, "check_activation", False)
    activation_report = None
    if check_activation or _has_any_activation(observations["baseline"]) or _has_any_activation(observations["candidate"]):
        activation_report = audit_activation(cases, observations["baseline"], observations["candidate"])
        report["activation_audit"] = activation_report
    if args.check_markdown and not args.claims:
        raise ValueError("--check-markdown requires --claims")
    check_fixture_leakage = getattr(args, "check_fixture_leakage", False)
    fixtures_dir = getattr(args, "fixtures_dir", None)
    if check_fixture_leakage and fixtures_dir is None:
        raise ValueError("--check-fixture-leakage requires --fixtures-dir")
    fixture_audit = None
    if fixtures_dir is not None:
        fixture_audit = audit_fixture_leakage(cases, fixtures_dir)
        report["fixture_leakage_audit"] = fixture_audit
    if args.claims:
        claims = read_object(args.claims, "claims", digests)
        observed, mismatches = verify_claims(claims, groups, pairs, plan.get("registered_claims"))
        report["claim_mismatches"] = mismatches
        if args.check_markdown:
            document = read_input(args.check_markdown, "markdown", digests)
            report["markdown_matches"] = verify_markdown(document, observed)
    if check_activation and activation_report is not None and not activation_report["passed"]:
        return EXIT_ACTIVATION_FAILURE, report
    strict_fail = (getattr(args, "strict_original", False) and amendment_audit is not None
                   and bool(amendment_audit["post_hoc_altered_summary"]))
    leakage_fail = check_fixture_leakage and fixture_audit is not None and not fixture_audit["passed"]
    failed = (bool(report.get("claim_mismatches")) or report.get("markdown_matches") is False
              or strict_fail or leakage_fail)
    return int(failed), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="frozen plan JSON")
    parser.add_argument("--seal-plan", action="store_true",
                        help="compute and print canonical frozen_core_sha256 for --plan and exit")
    for arm in ("baseline", "candidate"):
        parser.add_argument("--" + arm, type=Path, help=f"{arm} observations JSON")
    parser.add_argument("--claims", type=Path, help="optional group.metric to exact integer JSON object")
    parser.add_argument("--check-markdown", type=Path, help="release note with a marked table of the supplied claims")
    parser.add_argument("--check-activation", action="store_true",
                        help="require treatment added_check activation and zero baseline leakage (exit 70 on failure)")
    parser.add_argument("--strict-original", action="store_true",
                        help="fail with exit 1 when any post_hoc amendment alters the pre-registered summary")
    parser.add_argument("--fixtures-dir", type=Path,
                        help="directory holding <case_id>.json fixtures verified against plan fixture_sha256")
    parser.add_argument("--check-fixture-leakage", action="store_true",
                        help="fail with exit 1 when canonical JSON or alpha-renamed structural duplicate fixtures exist")
    try:
        code, report = compare(parser.parse_args())
    except (ValueError, OSError) as exc:
        code, report = 2, {"error": str(exc)}
    print(json.dumps(report, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
