"""Result contract shared by every stage.

Each stage writes <out>/<stage>/result.json and returns an exit code:
  0 pass, 1 a check failed, 2 configuration error, 3 environment missing.
Only statistics go into results (counts, pass/fail, check ids): never row values, secrets or local paths.
A result file is replaced atomically and removed before its stage runs again, so a reader (report, an agent) never
sees a half-written verdict or an earlier verdict left behind by an interrupted run.
"""

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .result_contract import SCHEMA_VERSION, STATUSES, ResultContractError, decode_result

EXIT_PASS, EXIT_FAIL, EXIT_CONFIG, EXIT_ENV = 0, 1, 2, 3


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
    inputs: dict = field(default_factory=dict)   # inputs_fingerprint() of the configuration the stage ran with
    provenance: dict = field(default_factory=lambda: {"schema_version": 1, "binding": "unbound"})
    runtime_evidence: dict = field(default_factory=lambda: {"schema_version": 1, "binding": "unbound"})

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
        """The result file's content. `evidence` (what verify observed about its gold queries) is not a field: it is
        written only when a stage set it, so the files of every other stage stay as they were."""
        d = asdict(self)
        d["schema_version"] = SCHEMA_VERSION
        d["status"] = self.status
        evidence = getattr(self, "evidence", None)
        if evidence is not None:
            d["evidence"] = evidence
        return d

    def write(self, out_dir: str | Path) -> Path:
        self.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        p = Path(out_dir) / self.stage
        p.mkdir(parents=True, exist_ok=True)
        f = p / "result.json"
        # Exclusive creation avoids truncating an input hardlinked at the old
        # predictable result.json.part path. Replacement still stays atomic.
        fd, name = tempfile.mkstemp(prefix='.result-', suffix='.part', dir=p)
        part = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(json.dumps(self.to_dict(), ensure_ascii=False, indent=1))
            os.replace(part, f)
        finally:
            part.unlink(missing_ok=True)
        return f


class StageError(Exception):
    """Raised for configuration (exit 2) or environment (exit 3) problems."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def inputs_fingerprint(cfg) -> dict:
    """SHA-256 of the configuration file and of the gold SQL files (in gold order). report compares a stage's recorded
    fingerprint with the current files, so editing crosscheck.toml or a gold query without rerunning makes the
    earlier results stale instead of letting them pass."""
    import hashlib
    from .provenance import engine_fingerprint, revision_query_fingerprint

    if cfg is None:
        return {}
    config = hashlib.sha256(Path(cfg.path).read_bytes()).hexdigest()
    gold = hashlib.sha256()
    for g in cfg.gold:
        gold.update(g.id.encode("utf-8") + b"\0")
        p = g.sql_path(cfg)
        gold.update(p.read_bytes() if p.is_file() else b"<missing>")
        gold.update(b"\0")
    return {"config_sha256": config, "gold_sha256": gold.hexdigest(),
            "engine_sha256": engine_fingerprint(), "revision_query_sha256": revision_query_fingerprint(cfg)}


def discard(out_dir: str | Path, stage: str) -> None:
    """Remove a stage's previous result.json before the stage runs again."""
    (Path(out_dir) / stage / "result.json").unlink(missing_ok=True)


def load_result(out_dir: str | Path, stage: str) -> dict | None:
    """Validated legacy/v1 result, or None; malformed evidence raises safe ValueError."""
    f = Path(out_dir) / stage / "result.json"
    if not f.is_file():
        return None
    try:
        raw = f.read_text(encoding="utf-8")
    except UnicodeError:
        raise ResultContractError() from None
    return decode_result(raw, expected_stage=stage)


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
        raise StageError(f"run `sxc.py {stage}` first (no {stage}/result.json in the output directory)", EXIT_CONFIG)
    return r


_MAX_OWN_VERDICT_BYTES = 4 * 1024 * 1024


def argv_option(argv, names: tuple[str, ...]) -> str | None:
    """Extract the last value of an option in `names` from raw CLI tokens without invoking argparse."""
    tokens = list(argv or ())
    if "--" in tokens:
        tokens = tokens[:tokens.index("--")]
    found = None
    for index, token in enumerate(tokens):
        if not isinstance(token, str):
            continue
        opt, sep, val = token.partition("=")
        if opt in names:
            if sep:
                found = val
            elif index + 1 < len(tokens) and isinstance(tokens[index + 1], str) and not tokens[index + 1].startswith("-"):
                found = tokens[index + 1]
    return found


def _is_linked(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


def _real_existing_dir(path: Path) -> bool:
    try:
        cur = path.absolute()
        if not cur.exists() or _is_linked(cur) or not cur.is_dir():
            return False
        for parent in cur.parents:
            if _is_linked(parent):
                return False
        return True
    except OSError:
        return False


def _protected_resolved_set(protected_paths, config_hint) -> tuple[set[Path], bytes]:
    resolved: set[Path] = set()
    items = list(protected_paths or ())
    if config_hint is not None:
        items.append(config_hint)
    for item in items:
        if not item:
            continue
        try:
            p = Path(item)
            resolved.add(p.resolve())
            resolved.add(p.absolute())
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    cfg_bytes = b""
    if config_hint is not None:
        try:
            cp = Path(config_hint)
            if not _is_linked(cp) and cp.is_file():
                st = cp.stat()
                if st.st_size <= _MAX_OWN_VERDICT_BYTES:
                    cfg_bytes = cp.read_bytes()
        except OSError:
            cfg_bytes = b"result.json report.json report.md"
    return resolved, cfg_bytes


def _unlinked_regular_bytes(target: Path, protected: set[Path]) -> bytes | None:
    import stat as stat_mod
    try:
        if _is_linked(target):
            return None
        if target.resolve() in protected or target.absolute() in protected:
            return None
        st = os.lstat(target)
        if not stat_mod.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None
        if st.st_size <= 0 or st.st_size > _MAX_OWN_VERDICT_BYTES:
            return None
        return target.read_bytes()
    except OSError:
        return None


def discard_own_verdict(out_dir: str | Path | None, stage: str, *, protected_paths=(), config_hint=None) -> bool:
    """Remove <out>/<stage>/result.json only when it is an unlinked, non-aliased v1 Result receipt for `stage`."""
    if not out_dir or not stage:
        return False
    out_path = Path(out_dir)
    stage_dir = out_path / stage
    if not _real_existing_dir(out_path) or not _real_existing_dir(stage_dir):
        return False
    protected, cfg_bytes = _protected_resolved_set(protected_paths, config_hint)
    if b"result.json" in cfg_bytes:
        return False
    target = stage_dir / "result.json"
    raw = _unlinked_regular_bytes(target, protected)
    if raw is None:
        return False
    try:
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
            return False
        if not isinstance(doc.get("provenance"), dict) or not isinstance(doc.get("runtime_evidence"), dict):
            return False
        decode_result(raw.decode("utf-8"), expected_stage=stage)
    except (UnicodeError, ValueError, TypeError, ResultContractError):
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def discard_own_report(out_dir: str | Path | None, *, protected_paths=(), config_hint=None) -> None:
    """Remove prior own report.json, report.md and report/result.json when non-aliased and unlinked."""
    if not out_dir:
        return
    out_path = Path(out_dir)
    if not _real_existing_dir(out_path):
        return
    protected, cfg_bytes = _protected_resolved_set(protected_paths, config_hint)
    if b"report.json" not in cfg_bytes:
        rj = out_path / "report.json"
        raw = _unlinked_regular_bytes(rj, protected)
        if raw is not None:
            try:
                doc = json.loads(raw.decode("utf-8"))
                req_keys = {"status", "stages", "required", "trusted_sql", "provenance", "runtime_evidence"}
                if isinstance(doc, dict) and req_keys <= set(doc):
                    rj.unlink()
            except (UnicodeError, ValueError, TypeError, OSError):
                pass
    if b"report.md" not in cfg_bytes:
        rm = out_path / "report.md"
        raw = _unlinked_regular_bytes(rm, protected)
        if raw is not None:
            try:
                txt = raw.decode("utf-8")
                if txt.startswith("# semantic-crosscheck report\n") and "\n| Stage | Status |" in txt:
                    rm.unlink()
            except (UnicodeError, OSError):
                pass
    discard_own_verdict(out_path, "report", protected_paths=protected_paths, config_hint=config_hint)
