"""Result contract shared by every stage.

Each stage writes <out>/<stage>/result.json and returns an exit code:
  0 pass, 1 a check failed, 2 configuration error, 3 environment missing.
Only statistics go into results (counts, pass/fail, check ids): never row values.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXIT_PASS, EXIT_FAIL, EXIT_CONFIG, EXIT_ENV = 0, 1, 2, 3
STATUSES = ("pass", "fail", "error", "not_run")


@dataclass
class Check:
    id: str
    status: str                      # pass | fail | error | not_run
    detail: dict = field(default_factory=dict)   # counts and ids only
    config_ref: str = ""             # the crosscheck.toml entry to fix when it fails

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"bad status {self.status}")


@dataclass
class Result:
    stage: str
    checks: list = field(default_factory=list)
    not_proven: list = field(default_factory=list)
    next_safe_action: str = ""
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    finished_at: str = ""

    def add(self, check_id: str, status: str, config_ref: str = "", **detail) -> Check:
        c = Check(check_id, status, detail, config_ref)
        self.checks.append(c)
        return c

    @property
    def status(self) -> str:
        """Fail-closed: only an all-pass stage passes; anything not run is not a pass."""
        if not self.checks:
            return "not_run"
        states = {c.status for c in self.checks}
        if "error" in states:
            return "error"
        if "fail" in states:
            return "fail"
        if "not_run" in states:
            return "not_run"
        return "pass"

    def exit_code(self) -> int:
        return {"pass": EXIT_PASS, "fail": EXIT_FAIL, "not_run": EXIT_FAIL}.get(self.status, EXIT_FAIL)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status
        return d

    def write(self, out_dir: str | Path) -> Path:
        self.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        p = Path(out_dir) / self.stage
        p.mkdir(parents=True, exist_ok=True)
        f = p / "result.json"
        f.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return f


class StageError(Exception):
    """Raised for configuration (exit 2) or environment (exit 3) problems."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def load_result(out_dir: str | Path, stage: str) -> dict | None:
    """The result.json written by an earlier stage, or None."""
    f = Path(out_dir) / stage / "result.json"
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def check_status(out_dir: str | Path, stage: str, check_id: str) -> str | None:
    """Status of one check of an earlier stage ('pass', 'fail', ...), or None when absent."""
    r = load_result(out_dir, stage)
    if not r:
        return None
    for c in r.get("checks", []):
        if c.get("id") == check_id:
            return c.get("status")
    return None


def require_stage(out_dir: str | Path, stage: str) -> dict:
    """Result of a prerequisite stage; raises StageError(exit 2) when that stage has not been run."""
    r = load_result(out_dir, stage)
    if r is None:
        raise StageError(f"run `sxc.py {stage}` first (no {stage}/result.json in {out_dir})", EXIT_CONFIG)
    return r
