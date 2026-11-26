"""Relocated, read-only, Python -O portability and tamper-rejection self-audit.

Selectively adapts the relocated-CWD, optimized-interpreter (-O), read-only
immutability, and negative tamper-rejection pattern informed by openai/math
(commit 3344da59, Apache-2.0, verification/portability_check.py), implemented
from scratch in the Python standard library for semantic-crosscheck offline
verifiers. Emits check names, counts and hashes only (never raw local paths).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "semantic-crosscheck" / "scripts"
SXC = SCRIPTS / "sxc.py"
PAIRED = ROOT / "tools" / "paired_release.py"
PAIRED_EXAMPLE = ROOT / "tools" / "run_paired_example.py"
EXAMPLE_DIR = ROOT / "examples" / "paired-release"
RELEASE_V30 = ROOT / "docs" / "releases" / "v0.30.0.md"

MONITORED_FILES = (
    "skills/semantic-crosscheck/scripts/sxc.py",
    "skills/semantic-crosscheck/scripts/semantic_crosscheck/report.py",
    "skills/semantic-crosscheck/scripts/semantic_crosscheck/result_contract.py",
    "skills/semantic-crosscheck/scripts/semantic_crosscheck/knowledge_evolution.py",
    "tools/paired_release.py",
    "tools/run_paired_example.py",
    "examples/paired-release/plan.json",
    "examples/paired-release/baseline.json",
    "examples/paired-release/candidate.json",
    "examples/paired-release/claims.json",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run_opt(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-O", "-B", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _check_knowledge_evolution_opt(base: Path, cwd: Path) -> dict[str, object]:
    out_dir = base / "ke-out"
    proc = _run_opt([str(SXC), "knowledge-evolution", "--out", str(out_dir)], cwd)
    _require(proc.returncode == 0, "relocated knowledge-evolution under -O failed")
    res_doc = json.loads((out_dir / "knowledge_evolution" / "result.json").read_text(encoding="utf-8"))
    _require(res_doc.get("status") == "pass", "knowledge-evolution result status was not pass")
    _require(len(res_doc.get("checks", [])) == 26, "knowledge-evolution contract count changed")
    return {"name": "relocated_knowledge_evolution_under_O", "passed": True}


def _check_paired_positive_opt(relocated: Path, cwd: Path) -> dict[str, object]:
    replay = _run_opt(
        [
            str(PAIRED_EXAMPLE),
            "--plan", str(relocated / "plan.json"),
            "--baseline", str(relocated / "baseline.json"),
            "--candidate", str(relocated / "candidate.json"),
        ],
        cwd,
    )
    _require(replay.returncode == 0, "relocated run_paired_example under -O failed")
    compare = _run_opt(
        [
            str(PAIRED),
            "--plan", str(relocated / "plan.json"),
            "--baseline", str(relocated / "baseline.json"),
            "--candidate", str(relocated / "candidate.json"),
            "--fixtures-dir", str(relocated / "cases"),
            "--check-fixture-leakage",
            "--claims", str(relocated / "claims.json"),
            "--check-markdown", str(RELEASE_V30),
        ],
        cwd,
    )
    _require(compare.returncode == 0, "relocated paired_release under -O failed")
    doc = json.loads(compare.stdout)
    audit = doc.get("fixture_leakage_audit") or {}
    _require(audit.get("passed") is True and audit.get("tree_membership_passed") is True,
             "relocated fixture audit did not pass cleanly under -O")
    return {"name": "relocated_paired_release_positive_under_O", "passed": True}


def _check_paired_tampers_opt(relocated: Path, cwd: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    # 1. Tamper baseline.json by 1 byte -> run_paired_example must reject under -O.
    baseline_path = relocated / "baseline.json"
    pristine_baseline = baseline_path.read_bytes()
    try:
        baseline_path.write_bytes(pristine_baseline + b" ")
        proc = _run_opt(
            [
                str(PAIRED_EXAMPLE),
                "--plan", str(relocated / "plan.json"),
                "--baseline", str(baseline_path),
                "--candidate", str(relocated / "candidate.json"),
            ],
            cwd,
        )
        _require(proc.returncode != 0, "tampered baseline receipt was not rejected under -O")
        results.append({"name": "tampered_receipt_rejected_under_O", "passed": True})
    finally:
        baseline_path.write_bytes(pristine_baseline)

    # 2. Plant unlisted extra fixture in cases/ -> both tools must reject under -O.
    extra = relocated / "cases" / "unlisted-tamper.json"
    try:
        extra.write_text('{"facts":[],"tags":[],"gold_mode":"all","model_mode":"all","expected_rows":[]}\n',
                         encoding="utf-8")
        proc_ex = _run_opt(
            [
                str(PAIRED_EXAMPLE),
                "--plan", str(relocated / "plan.json"),
                "--baseline", str(baseline_path),
                "--candidate", str(relocated / "candidate.json"),
            ],
            cwd,
        )
        _require(proc_ex.returncode != 0, "run_paired_example failed to reject unlisted case under -O")
        proc_pr = _run_opt(
            [
                str(PAIRED),
                "--plan", str(relocated / "plan.json"),
                "--baseline", str(baseline_path),
                "--candidate", str(relocated / "candidate.json"),
                "--fixtures-dir", str(relocated / "cases"),
                "--check-fixture-leakage",
            ],
            cwd,
        )
        _require(proc_pr.returncode == 1, "paired_release failed to reject unlisted case under -O")
        audit = json.loads(proc_pr.stdout).get("fixture_leakage_audit") or {}
        _require(audit.get("tree_membership_passed") is False and audit.get("extra_files") == ["unlisted-tamper.json"],
                 "paired_release did not report exact extra_files under -O")
        results.append({"name": "unlisted_fixture_file_rejected_under_O", "passed": True})
    finally:
        extra.unlink(missing_ok=True)

    # 3. Tamper numeric claim count -> paired_release must exit 1 under -O.
    claims_path = relocated / "claims.json"
    pristine_claims = claims_path.read_bytes()
    try:
        claims = json.loads(pristine_claims.decode("utf-8"))
        first_key = next(iter(claims))
        claims[first_key] = int(claims[first_key]) + 1
        claims_path.write_text(json.dumps(claims, indent=2) + "\n", encoding="utf-8")
        proc_cl = _run_opt(
            [
                str(PAIRED),
                "--plan", str(relocated / "plan.json"),
                "--baseline", str(baseline_path),
                "--candidate", str(relocated / "candidate.json"),
                "--claims", str(claims_path),
            ],
            cwd,
        )
        _require(proc_cl.returncode == 1, "altered numeric claim was not rejected under -O")
        results.append({"name": "altered_claim_rejected_under_O", "passed": True})
    finally:
        claims_path.write_bytes(pristine_claims)

    return results


def _check_result_contract_and_assumptions_opt(base: Path, cwd: Path) -> list[dict[str, object]]:
    # Run inline checks strictly inside a python -O -B subprocess from unrelated CWD.
    probe = (
        "import json, sys\nfrom pathlib import Path\n"
        "if __debug__:\n"
        "    raise SystemExit('optimize flag -O inactive')\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r}); "
        "from semantic_crosscheck import config, cube_run, report, result_contract; "
        "bad = False\n"
        "try:\n"
        "    result_contract.decode_result('{\"checks\":[],\"checks\":[]}')\n"
        "except result_contract.ResultContractError:\n"
        "    bad = True\n"
        "if not bad:\n"
        "    raise SystemExit('duplicate JSON key accepted under -O')\n"
        f"out = Path({str(base / 'rep-out')!r})\n"
        "facts = cube_run.runtime_facts(config.CUBE_VERSION)\n"
        "stages = [\n"
        "  ('build', '2026-11-27T00:00:00+0000', '2026-11-27T00:00:01+0000', [{'id':'closure:h','status':'pass','detail':{}}]),\n"
        "  ('reason', '2026-11-27T00:00:02+0000', '2026-11-27T00:00:03+0000', [{'id':'owl:consistency','status':'pass','detail':{}}]),\n"
        "  ('cube', '2026-11-27T00:00:02+0000', '2026-11-27T00:00:03+0000', [{'id':'cube:meta','status':'pass','detail':facts}]),\n"
        "  ('verify', '2026-11-27T00:00:04+0000', '2026-11-27T00:00:05+0000', [{'id':'v1:q','status':'pass','detail':{}}]),\n"
        "]\n"
        "for st, s0, s1, chks in stages:\n"
        "    d = out / st; d.mkdir(parents=True, exist_ok=True)\n"
        "    (d / 'result.json').write_text(json.dumps({'schema_version':1,'stage':st,'status':'pass',"
        "'started_at':s0,'finished_at':s1,'checks':chks,'not_proven':[],'next_safe_action':'','inputs':{},"
        "'provenance':{'schema_version':1,'binding':'unbound'},'runtime_evidence':{'schema_version':1,'binding':'unbound'}}))\n"
        "args = type('A', (), {'require_doctor': False, 'permitted_assumptions': 'none'})()\n"
        "res = report.run(None, args, out)\n"
        "policy_failed = any(c.id == 'assumptions:permitted' and c.status == 'fail' for c in res.checks)\n"
        "if res.status != 'fail' or res.exit_code() != 1 or not policy_failed:\n"
        "    raise SystemExit('disallowed assumptions not rejected under -O')\n"
        "print('OK')\n"
    )
    proc = _run_opt(["-c", probe], cwd)
    _require(proc.returncode == 0 and proc.stdout.strip() == "OK",
             "result_contract or assumption-policy rejection failed under -O")
    return [
        {"name": "duplicate_json_key_rejected_under_O", "passed": True},
        {"name": "disallowed_assumptions_rejected_under_O", "passed": True},
    ]


def run_portability_checks() -> dict[str, object]:
    originals = {rel: _sha256(ROOT / rel) for rel in MONITORED_FILES}
    checks: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="sxc_portability_") as temp_dir:
        base = Path(temp_dir)
        other_cwd = base / "unrelated-cwd"
        other_cwd.mkdir()
        relocated = base / "paired-release"
        shutil.copytree(EXAMPLE_DIR, relocated)

        checks.append(_check_knowledge_evolution_opt(base, other_cwd))
        checks.append(_check_paired_positive_opt(relocated, other_cwd))
        checks.extend(_check_paired_tampers_opt(relocated, other_cwd))
        checks.extend(_check_result_contract_and_assumptions_opt(base, other_cwd))

    after = {rel: _sha256(ROOT / rel) for rel in MONITORED_FILES}
    _require(after == originals, "portability checks mutated repository source files")
    checks.append({"name": "source_files_unchanged_readonly", "passed": True})
    return {
        "schema": "semantic-crosscheck/portability-check/v1",
        "optimize_flag_verified": True,
        "monitored_files": len(originals),
        "checks": checks,
        "checks_passed": len(checks),
        "all_passed": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print structured JSON summary")
    args = parser.parse_args(argv)
    try:
        summary = run_portability_checks()
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        msg = type(exc).__name__ if isinstance(exc, OSError) else str(exc)
        if args.json:
            print(json.dumps({"schema": "semantic-crosscheck/portability-check/v1",
                              "all_passed": False, "error": msg}, indent=2))
        else:
            print(f"portability_check: FAIL: {msg}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"portability_check: PASS ({summary['checks_passed']} checks under python -O -B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
