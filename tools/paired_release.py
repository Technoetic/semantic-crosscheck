"""Compare two recorded release-development runs against a frozen plan, offline."""

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from statistics import NormalDist


EXPOSURES = ("heldout", "development", "unknown")
EXECUTED = ("clear", "flag")
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


def validate_plan(plan):
    validate_header(plan, "plan")
    identifier(plan.get("experiment"), "experiment")
    added = identifier(plan.get("added_check"), "added_check")
    checks = check_ids(plan.get("baseline_checks"), "baseline_checks")
    if added in checks:
        raise ValueError("added_check already belongs to baseline_checks")
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
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"{case_id!r}: fixture_sha256 must be a lowercase SHA-256 hex digest")
        if digest in seen:
            raise ValueError(f"{case_id!r}: fixture_sha256 reused across case IDs")
        seen.add(digest)
        if case.get("expected") not in ("error", "clean") or case.get("exposure") not in EXPOSURES:
            raise ValueError(f"{case_id!r}: invalid expected or exposure")
    return cases


def validate_receipt(receipt, arm, plan, digest, cases):
    validate_header(receipt, arm)
    if receipt.get("arm") != arm:
        raise ValueError(f"{arm}.arm must be {arm!r}")
    if receipt.get("plan_sha256") != digest:
        raise ValueError(f"{arm}.plan_sha256 differs from supplied plan bytes")
    if receipt["budget"] != plan["budget"]:
        raise ValueError(f"{arm}.budget differs from plan")
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
    if sum(row["status"] != "not_run" for row in rows.values()) > plan["budget"]:
        raise ValueError(f"{arm}: attempted case count exceeds budget")
    return rows


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
    """Natural log of the exact Jeffreys Beta(1/2, 1/2) mixture e-value for H0: theta = 0.5.

    Exact anytime-valid test martingale under optional stopping and optional
    continuation (Grunwald, de Heide & Koolen, JRSS-B 2024; Ramdas et al.,
    Statistical Science 2023). Log-space evaluation via math.lgamma avoids
    overflow for large discordant counts and lets independent cohorts pool by
    adding log e-values.
    """
    if b < 0 or c < 0:
        raise ValueError("discordant counts must be nonnegative")
    m = b + c
    if m == 0:
        return 0.0
    return (m * math.log(2.0) + math.lgamma(b + 0.5) + math.lgamma(c + 0.5)
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


def compare(args):
    digests = {}
    plan = read_object(args.plan, "plan", digests)
    cases = validate_plan(plan)
    observations = {}
    for arm in ("baseline", "candidate"):
        receipt = read_object(getattr(args, arm), arm, digests)
        observations[arm] = validate_receipt(receipt, arm, plan, digests["plan"], cases)
    groups, pairs = summarize(cases, **observations)
    report = dict(schema_version=1, experiment=plan["experiment"], added_check=plan["added_check"],
                  baseline_checks=plan["baseline_checks"], budget=plan["budget"],
                  input_sha256=digests, groups=groups, pairs=pairs)
    if args.check_markdown and not args.claims:
        raise ValueError("--check-markdown requires --claims")
    if args.claims:
        claims = read_object(args.claims, "claims", digests)
        observed, mismatches = verify_claims(claims, groups, pairs, plan.get("registered_claims"))
        report["claim_mismatches"] = mismatches
        if args.check_markdown:
            document = read_input(args.check_markdown, "markdown", digests)
            report["markdown_matches"] = verify_markdown(document, observed)
    failed = bool(report.get("claim_mismatches")) or report.get("markdown_matches") is False
    return int(failed), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="frozen plan JSON")
    for arm in ("baseline", "candidate"):
        parser.add_argument("--" + arm, required=True, type=Path, help=f"{arm} observations JSON")
    parser.add_argument("--claims", type=Path, help="optional group.metric to exact integer JSON object")
    parser.add_argument("--check-markdown", type=Path, help="release note with a marked table of the supplied claims")
    try:
        code, report = compare(parser.parse_args())
    except (ValueError, OSError) as exc:
        code, report = 2, {"error": str(exc)}
    print(json.dumps(report, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
