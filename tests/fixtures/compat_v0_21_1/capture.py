"""Compatibility goldens of v0.21.1 for a configuration without [data_rules] (demo/crosscheck.toml).

The goldens next to this file were written by this script on v0.21.1, before any data-rules code existed:

    python -X utf8 tests/fixtures/compat_v0_21_1/capture.py --write     # write the goldens (done once, in P0)
    python -X utf8 tests/fixtures/compat_v0_21_1/capture.py             # compare the current code with them

The comparison allows one difference by design, as tests/test_data_rules_compat.py does: static/stages.txt may gain
the stages added since v0.21.1 (NEW_STAGES) and nothing else. It is reported as expected and does not fail the run.

tests/test_data_rules_compat.py calls collect() and compares every golden byte for byte. Everything runs offline:
  - static/: shapes.violation_sql per shape, shapes.shapes_ttl, shapes.fk_review_sql, the sorted sxc.STAGES, the
    inputs_fingerprint key names, report.TRUSTED_NOTE and stage_build.BUILD_SQL_HEADER;
  - build_<scenario>/ and report_<scenario>/: `sxc.py build` and then `sxc.py report` through sxc.main, with the
    database connection replaced by RecordingConnection, which records every statement build sends and answers it
    with fixed synthetic counts. Scenario "pass" has no violating rows; scenario "fail" has failing shapes and a
    warning shape with rows. Captured: every statement with its parameters, build.sql, shapes.ttl, fk_review.sql,
    result.json, report.md, report.json and the console summaries with their exit codes;
  - checks.txt: the check ids and statuses of those runs.
Only what differs between runs or machines is normalised: run times, the engine fingerprint (new modules change it
by design, provenance.engine_fingerprint) and the output paths in console lines.
"""

import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRIPTS = ROOT / "skills" / "semantic-crosscheck" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import sxc  # noqa: E402
from semantic_crosscheck import config, db, report, shapes, stage_build  # noqa: E402
from semantic_crosscheck.result import inputs_fingerprint  # noqa: E402

DEMO = ROOT / "demo" / "crosscheck.toml"
# Columns of the demo tables as semantic_crosscheck.demo.TABLES defines them at v0.21.1 (answer to the catalog query).
DEMO_COLUMNS = {
    "part": ["part_id", "part_class", "unit", "procure", "name"],
    "bom_line": ["parent_id", "child_id", "line_no", "qty_per"],
    "sales_order": ["order_id", "part_id", "status", "qty_total", "qty_open", "due_date"],
    "stock": ["part_id", "location_id", "qty"],
}
# violating rows per shape view in each scenario
SCENARIOS = {
    "pass": {"child_exists": 0, "qty_positive": 0, "stock_nonnegative": 0, "open_le_total": 0},
    "fail": {"child_exists": 2, "qty_positive": 0, "stock_nonnegative": 1, "open_le_total": 3},
}
STAGES = "static/stages.txt"
NEW_STAGES = {"data-rules"}                     # stages added since v0.21.1: the only allowed golden difference
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{4}|Z)?")
ENGINE = re.compile(r'"engine_sha256": "[0-9a-f]{64}"')
NO_ROWS = ("BEGIN", "COMMIT", "ROLLBACK", "SET LOCAL ", "SAVEPOINT ", "RELEASE SAVEPOINT ", "CREATE ", "DROP ",
           "ALTER ", "ANALYZE ", "INSERT ")


class RecordingConnection:
    """pg8000.native stand-in for build: records every statement with its parameters and answers the catalog,
    view and closure queries with fixed synthetic numbers (an acyclic hierarchy with three closure levels)."""

    def __init__(self, view_counts: dict):
        self.view_counts = view_counts
        self.statements: list[tuple[str, dict]] = []
        self.columns = None
        self.level_counts = [7, 4, 0]

    def run(self, sql: str, **params):
        self.statements.append((sql, dict(params)))
        self.columns = None
        text = sql.strip()
        if text.startswith("SELECT c.oid IS NOT NULL AS found"):
            self.columns = [{"name": n} for n in ("found", "usage", "readable", "columns")]
            return [[True, True, True, list(DEMO_COLUMNS[params["t"]])]]
        if text.startswith("SELECT current_user"):
            return [["xc_check"]]
        if text.startswith("SELECT 1 FROM pg_namespace"):
            return [[1]]
        if text.startswith("SELECT has_schema_privilege"):
            return [[True, True]]
        if text.startswith("SELECT pg_advisory_xact_lock"):
            return [[None]]
        view = re.fullmatch(r'SELECT count\(\*\) FROM "sxc_out"\."v_shape_([a-z0-9_]+)"', text)
        if view:
            return [[self.view_counts[view.group(1)]]]
        answers = {
            "SELECT count(*) FROM sxc_edge WHERE q IS NULL": [[0]],
            "SELECT count(*) FROM sxc_edge": [[7]],
            "SELECT count(*) FROM sxc_reach WHERE a = d": [[0]],
            "SELECT max_depth, count(*) FROM sxc_closure_new GROUP BY max_depth ORDER BY max_depth": [[1, 7], [2, 4]],
            "SELECT count(*) FROM sxc_closure_new WHERE path_count > 1": [[1]],
            "SELECT count(*) FROM sxc_closure_new": [[11]],
            "SELECT coalesce(sum(path_count), 0) FROM sxc_closure_new": [[12]],
            "SELECT coalesce(max(max_depth), 0) FROM sxc_closure_new": [[2]],
            "SELECT to_regclass(:t) IS NOT NULL": [[True]],
        }
        if text in answers:
            return answers[text]
        if text == "SELECT count(*) FROM sxc_level":
            return [[self.level_counts.pop(0)]]
        if text.startswith("SELECT count(*) FROM") and "IS NULL OR" in text:
            return [[0]]                                   # lines with a NULL endpoint
        if text.startswith("WITH m AS (") and "MERGE INTO" in text:
            return [["INSERT", 11]]
        if text.startswith("SELECT (SELECT count(*) FROM (SELECT"):
            return [[0]]                                   # closure table equals the recomputation
        if text.startswith(NO_ROWS):
            return []
        raise AssertionError("capture: no answer for a statement build sent: " + text[:80])

    def close(self):
        pass


def _no_connection(*_args, **_kwargs):
    raise AssertionError("capture: report must not connect to a database without a revision query")


def _cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = sxc.main(argv)
    return code, buf.getvalue()


def normalise(text: str) -> str:
    """Run times and the engine fingerprint, the only values that may differ between runs or releases."""
    return ENGINE.sub('"engine_sha256": "<engine>"', TIME.sub("<time>", text))


def _console(code: int, text: str, out: Path) -> str:
    for stage in ("build", "report"):
        text = text.replace(str(Path(str(out)) / stage / "result.json"), f"<out>/{stage}/result.json")
    text = text.replace(str(out), "<out>").replace(str(sxc.PLUGIN_ROOT), "<plugin>")
    return f"exit {code}\n" + normalise(text)


def _statements(conn: RecordingConnection) -> str:
    blocks = [f"-- statement {i}\n{sql}\n-- params {json.dumps(params, sort_keys=True)}"
              for i, (sql, params) in enumerate(conn.statements, 1)]
    return "\n".join(blocks) + "\n"


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _static(cfg) -> dict:
    files = {f"static/violation_{s.id}.sql": shapes.violation_sql(cfg, s) + "\n" for s in cfg.shapes}
    ttl, skipped = shapes.shapes_ttl(cfg)
    files["static/shapes.ttl"] = ttl
    files["static/shapes_skipped.txt"] = "".join(f"{sid}\n" for sid in skipped)
    files["static/fk_review.sql"] = shapes.fk_review_sql(cfg)
    files["static/stages.txt"] = "".join(f"{name}\n" for name in sorted(sxc.STAGES))
    files["static/inputs_keys.txt"] = "".join(f"{key}\n" for key in inputs_fingerprint(cfg))
    files["static/trusted_note.txt"] = report.TRUSTED_NOTE + "\n"
    files["static/build_sql_header.txt"] = stage_build.BUILD_SQL_HEADER
    return files


def _scenario(name: str, counts: dict, base: Path, checks: list[str]) -> dict:
    out = base / name
    conn = RecordingConnection(counts)
    with mock.patch.object(db, "connect", return_value=conn):
        build_code, build_console = _cli(["build", "--config", str(DEMO), "--out", str(out)])
    with mock.patch.object(db, "connect", _no_connection):
        report_code, report_console = _cli(["report", "--config", str(DEMO), "--out", str(out)])
    files = {
        f"build_{name}/statements.sql": _statements(conn),
        f"build_{name}/build.sql": _read(out / "build" / "build.sql"),
        f"build_{name}/shapes.ttl": _read(out / "build" / "shapes.ttl"),
        f"build_{name}/fk_review.sql": _read(out / "build" / "fk_review.sql"),
        f"build_{name}/result.json": normalise(_read(out / "build" / "result.json")) + "\n",
        f"build_{name}/console.txt": _console(build_code, build_console, out),
        f"report_{name}/report.md": normalise(_read(out / "report.md")),
        f"report_{name}/report.json": normalise(_read(out / "report.json")),
        f"report_{name}/result.json": normalise(_read(out / "report" / "result.json")) + "\n",
        f"report_{name}/console.txt": _console(report_code, report_console, out),
    }
    for stage in ("build", "report"):
        result = json.loads(_read(out / stage / "result.json"))
        checks += [f"{name} {stage} {c['id']} {c['status']} [{c['config_ref']}]" for c in result["checks"]]
        checks.append(f"{name} {stage} status {result['status']}")
    return files


def collect() -> dict:
    """{golden path relative to this folder (POSIX): text} for the current code."""
    cfg = config.load(DEMO)
    files = _static(cfg)
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sxc-compat-") as tmp:
        for name, counts in SCENARIOS.items():
            files.update(_scenario(name, counts, Path(tmp), checks))
    files["checks.txt"] = "".join(f"{line}\n" for line in checks)
    return files


def write(files: dict, folder: Path = HERE) -> None:
    for rel, text in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def differences(files: dict, folder: Path = HERE) -> list[str]:
    """Golden paths whose bytes differ from files, plus goldens that files no longer produce."""
    found = {p.relative_to(folder).as_posix() for p in folder.rglob("*")
             if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts}
    changed = [rel for rel, text in files.items()
               if rel not in found or (folder / rel).read_bytes() != text.encode("utf-8")]
    return sorted(changed + sorted(found - set(files)))


def expected_difference(rel: str, files: dict, folder: Path = HERE) -> bool:
    """True when rel is static/stages.txt and the current stage list is the golden one plus stages of NEW_STAGES."""
    path = folder / rel
    if rel != STAGES or rel not in files or not path.is_file():
        return False
    golden = path.read_bytes().decode("utf-8").splitlines()
    current = files[rel].splitlines()
    return set(current) - set(golden) <= NEW_STAGES and [s for s in current if s not in NEW_STAGES] == golden


def main(argv: list[str]) -> int:
    files = collect()
    if argv[1:] == ["--write"]:
        write(files)
        print(f"wrote {len(files)} goldens")
        return 0
    if argv[1:]:
        print("usage: capture.py [--write]")
        return 2
    changed = differences(files)
    expected = [rel for rel in changed if expected_difference(rel, files)]
    unexpected = [rel for rel in changed if rel not in expected]
    for rel in expected:
        print(f"differs as expected: {rel} (the stage list gained only {', '.join(sorted(NEW_STAGES))})")
    for rel in unexpected:
        print(f"differs: {rel}")
    print(f"{len(files)} goldens, {len(unexpected)} differ" + (f", {len(expected)} as expected" if expected else ""))
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
