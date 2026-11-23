"""Replay the repository's paired release example on local synthetic inputs."""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "paired-release"
sys.path.insert(0, str(ROOT / "skills" / "semantic-crosscheck" / "scripts"))
from semantic_crosscheck import compare, config, expect

AGREEMENT = "v1-row-agreement"
AUTHORED = "authored-expected-rows"
GOLD = config.Gold(id="units", sql="", key=["sku"], map={"sku": "stock.sku", "units": "stock.units"})
TYPES = {"sku": "string", "units": "number"}

# Gold aggregates fact rows directly. The model pre-aggregates at SKU grain, then applies tag multiplicity.
GOLD_SQL = {
    "all": "SELECT sku, SUM(units) AS units FROM facts GROUP BY sku",
    "tag_join": """SELECT f.sku, SUM(f.units) AS units
                   FROM facts AS f JOIN tags AS t ON f.sku = t.sku GROUP BY f.sku""",
}
MODEL_SQL = {
    "all": "SELECT sku, units FROM model_totals",
    "tag_join": """SELECT m.sku, m.units * t.cnt AS units FROM model_totals AS m
                   JOIN (SELECT sku, COUNT(*) AS cnt FROM tags GROUP BY sku) AS t ON m.sku = t.sku""",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_fixture(raw):
    fixture = json.loads(raw, object_pairs_hook=unique_object)
    fields = {"facts", "tags", "gold_mode", "model_mode", "expected_rows"}
    if not isinstance(fixture, dict) or set(fixture) != fields:
        raise ValueError("invalid fixture fields")
    for field, modes in (("gold_mode", GOLD_SQL), ("model_mode", MODEL_SQL)):
        if not isinstance(fixture[field], str) or fixture[field] not in modes:
            raise ValueError(f"unsupported {field}")
    for field in ("facts", "tags", "expected_rows"):
        value_name, value_type = ("tag", str) if field == "tags" else ("units", int)
        rows = fixture[field]
        if not isinstance(rows, list):
            raise ValueError(f"{field} must be a row list")
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"sku", value_name}
                    or type(row["sku"]) is not str or type(row[value_name]) is not value_type):
                raise ValueError(f"invalid {field} row")
    return fixture


def judge_fixture(raw, checks, *, return_activation: bool = False):
    fixture = load_fixture(raw)
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE facts (sku TEXT NOT NULL, units INTEGER NOT NULL);
            CREATE TABLE tags (sku TEXT NOT NULL, tag TEXT NOT NULL);
            CREATE VIEW model_totals AS SELECT sku, SUM(units) AS units FROM facts GROUP BY sku;
        """)
        db.executemany("INSERT INTO facts VALUES (:sku, :units)", fixture["facts"])
        db.executemany("INSERT INTO tags VALUES (:sku, :tag)", fixture["tags"])
        gold_rows = [dict(row) for row in db.execute(GOLD_SQL[fixture["gold_mode"]])]
        model_rows = [dict(row) for row in db.execute(MODEL_SQL[fixture["model_mode"]])]
    agreement = compare.compare_rows(GOLD, gold_rows, model_rows, TYPES)
    statuses = ["pass" if agreement["pass"] else "fail"]
    added_calls = 0
    if AUTHORED in checks:
        case = config.Expect(source="authored", rows=fixture["expected_rows"], spec="SKU unit totals")
        status, _ = expect.judge(GOLD, case, gold_rows, model_rows, TYPES)
        added_calls = 1
        statuses.append(status)
    verdict = "error" if "error" in statuses else ("flag" if "fail" in statuses else "clear")
    if return_activation:
        return verdict, {"added_check_calls": added_calls, "fallback_calls": 0}
    return verdict


def generate(plan_path, *, include_activation: bool = False):
    raw_plan = plan_path.read_bytes()
    plan = json.loads(raw_plan, object_pairs_hook=unique_object)
    cases = plan["cases"]
    if (type(plan["schema_version"]) is not int or plan["schema_version"] != 1
            or plan["baseline_checks"] != [AGREEMENT] or plan["added_check"] != AUTHORED
            or not isinstance(cases, list) or not cases or type(plan["budget"]) is not int
            or plan["budget"] < 1):
        raise ValueError("unsupported paired-example plan")
    ids = [case["id"] for case in cases]
    if any(not isinstance(i, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", i) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("invalid or repeated fixture ID")
    receipts = []
    for arm, checks in (("baseline", [AGREEMENT]), ("candidate", [AGREEMENT, AUTHORED])):
        observations = []
        for index, case in enumerate(cases):
            raw = (plan_path.parent / "cases" / (case["id"] + ".json")).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != case["fixture_sha256"]:
                raise ValueError(f"fixture hash mismatch: {case['id']}")
            if index < plan["budget"]:
                if include_activation:
                    status, act = judge_fixture(raw, checks, return_activation=True)
                    observations.append({"id": case["id"], "status": status,
                                         "fixture_sha256": digest, "activation": act})
                else:
                    status = judge_fixture(raw, checks)
                    observations.append({"id": case["id"], "status": status, "fixture_sha256": digest})
            else:
                row = {"id": case["id"], "status": "not_run", "fixture_sha256": digest}
                if include_activation:
                    row["activation"] = {"added_check_calls": 0, "fallback_calls": 0}
                observations.append(row)
        receipts.append({"schema_version": 1, "arm": arm, "plan_sha256": hashlib.sha256(raw_plan).hexdigest(),
                         "checks": checks, "budget": plan["budget"], "observations": observations})
    return receipts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=EXAMPLE / "plan.json")
    parser.add_argument("--baseline", type=Path, default=EXAMPLE / "baseline.json")
    parser.add_argument("--candidate", type=Path, default=EXAMPLE / "candidate.json")
    parser.add_argument("--write", action="store_true", help="write the two receipts (default: verify exact bytes)")
    parser.add_argument("--with-activation", action="store_true",
                        help="include per-observation added_check activation counters")
    args = parser.parse_args(argv)
    try:
        if len({p.resolve() for p in (args.plan, args.baseline, args.candidate)}) != 3:
            raise ValueError("plan and receipt paths must differ")
        for path, receipt in zip((args.baseline, args.candidate),
                                 generate(args.plan, include_activation=args.with_activation)):
            output = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")
            if args.write:
                path.write_bytes(output)
            elif path.read_bytes() != output:
                raise ValueError(f"replay differs from {receipt['arm']} receipt")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f"paired example: {exc}\n")
    print("Wrote both receipts." if args.write else "Replayed both arms; both receipts match byte for byte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
