"""Run the Cube adapter (adapters/cube/server.js) as a child process for one generated model.

    with cube_run.serve(cfg, run_dir, log_path) as client:      # run_dir contains model/*.yml
        client.load({...})

- The adapter is installed once per user by `sxc.py cube-install` into cube_home() (pinned package-lock and
  SHA-256-pinned native module); nothing is downloaded here. An install counts only when it is complete (server.js,
  the server packages and the native module) and its installed.json records the adapter files of this checkout:
  after an upgrade that changes server.js or the lockfile, an old install would otherwise keep serving the old adapter.
- cube_home() is absolute: the adapter runs with the run directory as working directory, where a relative
  SXC_CUBE_HOME would point somewhere else.
- Environment for the child: inherited variables minus every CUBEJS_* / PG* setting, NODE_OPTIONS (it can open a
  debugger port or preload code), every variable whose value holds a postgres:// URL (the loader's DSN, a test admin
  DSN) and every SXC_* variable (the adapter reads only SXC_PORT, SXC_MODEL_DIR and SXC_CUBE_DEBUG, set here); plus
  the connection parsed from the Cube DSN variable, a random per-run API secret, development mode and telemetry off,
  TLS settings from the DSN's sslmode, and a statement timeout of cube.query_timeout_s, so a query the client gave up
  on also stops in PostgreSQL instead of running on and holding Cube's queue. The timeout is set twice because Cube's Postgres driver runs `SET statement_timeout` from
  CUBEJS_DB_QUERY_TIMEOUT (default 10 minutes, also its queue's execution timeout) on every connection, overriding the
  PGOPTIONS default; PGOPTIONS also makes every Cube session read-only.
- It listens on 127.0.0.1 (port from [cube].port, 0 = ephemeral); the chosen port is read from "SXC_READY <port>".
- Stopping closes the child's stdin (the adapter exits) and kills it if it does not.
Messages name where the install was looked for (SXC_CUBE_HOME or the default location), never the local path.
"""

import collections
import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import db, procutil
from .cube_client import CubeClient
from .result import EXIT_ENV, StageError

ADAPTER_DIR = Path(__file__).resolve().parents[2] / "adapters" / "cube"
ADAPTER_FILES = ("server.js", "package.json", "package-lock.json")
RECORD = "installed.json"
SERVER_CORE = Path("node_modules") / "@cubejs-backend" / "server-core"
NATIVE_DIR = Path("node_modules") / "@cubejs-backend" / "native"
INDEX_NODE = NATIVE_DIR / "native" / "index.node"
MIN_NODE = 22                       # packages in the locked tree declare engines.node >= 22
STARTUP_TAGS = ("SXC_ERROR", "SXC_CUBE_ERROR")                   # redacted adapter lines (SXC_DEBUG lines are not)
CHUNK = 1 << 20
_DSN_VALUE = re.compile(r"postgres(?:ql)?://", re.IGNORECASE)
_STATE_HINT = {
    "missing": "the Cube adapter is not installed ({where}): run `sxc.py cube-install`",
    "incomplete": "the Cube adapter install ({where}) is incomplete: run `sxc.py cube-install`",
    "stale": "the Cube adapter install ({where}) was made from other adapter files than this checkout (an upgrade "
             "changed server.js or the lockfile): rerun `sxc.py cube-install` to update it",
}


def cube_home(version: str = "1.8.3") -> Path:
    """Absolute per-user install location of the adapter (override with SXC_CUBE_HOME; a relative value is resolved
    against the current directory)."""
    if os.environ.get("SXC_CUBE_HOME"):
        return Path(os.environ["SXC_CUBE_HOME"]).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return (base / "semantic-crosscheck" / f"cube-{version}").expanduser().resolve()


def home_label() -> str:
    """Where cube_home() came from, for messages and results (which never carry local paths)."""
    return "SXC_CUBE_HOME" if os.environ.get("SXC_CUBE_HOME") else "default per-user location"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def adapter_hashes(adapter_dir: Path = ADAPTER_DIR) -> dict:
    return {name: sha256_file(Path(adapter_dir) / name) for name in ADAPTER_FILES}


def install_state(home: Path) -> str:
    """'ok', 'missing' (no adapter), 'incomplete' (packages, native module or install record missing) or 'stale'
    (installed from other adapter files than this checkout's)."""
    home = Path(home)
    if not (home / "server.js").is_file():
        return "missing"
    if not (home / SERVER_CORE).is_dir() or not (home / INDEX_NODE).is_file():
        return "incomplete"
    try:
        rec = json.loads((home / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "incomplete"
    try:
        current = adapter_hashes()
    except OSError:
        return "stale"
    return "ok" if isinstance(rec, dict) and rec.get("files_sha256") == current else "stale"


def installed(home: Path) -> bool:
    return install_state(home) == "ok"


def ssl_env(sslmode: str, rootcert: str) -> dict:
    """CUBEJS_DB_SSL* for the DSN's sslmode. Cube verifies the server certificate only with
    CUBEJS_DB_SSL_REJECT_UNAUTHORIZED=true (its default is false), and node then checks the host name as well, so
    verify-ca is as strict as verify-full in the Cube child."""
    if sslmode == "require":
        return {"CUBEJS_DB_SSL": "true", "CUBEJS_DB_SSL_REJECT_UNAUTHORIZED": "false"}
    if sslmode in ("verify-ca", "verify-full"):
        env = {"CUBEJS_DB_SSL": "true", "CUBEJS_DB_SSL_REJECT_UNAUTHORIZED": "true"}
        if rootcert and rootcert != "system":
            env["CUBEJS_DB_SSL_CA"] = rootcert
        return env
    return {"CUBEJS_DB_SSL": "false"}


def _inherited(name: str, value: str, dropped: set) -> bool:
    upper = name.upper()
    if upper.startswith(("CUBEJS_", "PG", "SXC_")) or upper == "NODE_OPTIONS" or upper in dropped:
        return False
    return not _DSN_VALUE.search(value)


def child_env(cfg, port: int, secret: str) -> dict:
    env_name = cfg.cube_dsn_env()
    p = db.dsn_from_env(env_name, "Cube connection")
    dropped = {n.upper() for n in (env_name, cfg.source.dsn_env, cfg.source.owner_dsn_env)}
    env = {k: v for k, v in os.environ.items() if _inherited(k, v, dropped)}
    timeout_s = int(cfg.cube.query_timeout_s)
    env.update({
        "CUBEJS_DB_TYPE": "postgres",
        "CUBEJS_DB_HOST": p["host"],
        "CUBEJS_DB_PORT": str(p["port"]),
        "CUBEJS_DB_NAME": p["database"],
        "CUBEJS_DB_USER": p["user"],
        "CUBEJS_API_SECRET": secret,
        "CUBEJS_DEV_MODE": "false",
        "CUBEJS_TELEMETRY": "false",
        "CUBEJS_LOG_LEVEL": "error",
        "CUBEJS_DB_QUERY_TIMEOUT": str(timeout_s),
        "PGOPTIONS": f"-c default_transaction_read_only=on -c statement_timeout={timeout_s * 1000}",
        "SXC_PORT": str(port),
        "SXC_MODEL_DIR": "model",
    })
    if os.environ.get("SXC_CUBE_DEBUG") == "1":
        env["SXC_CUBE_DEBUG"] = "1"
    if p.get("password"):
        env["CUBEJS_DB_PASS"] = p["password"]
    env.update(ssl_env(p["sslmode"], p["sslrootcert"]))
    return env


def _log_label(log_path: Path | None) -> str:
    return f"; see {Path(log_path).parent.name}/{Path(log_path).name}" if log_path else ""


@contextlib.contextmanager
def serve(cfg, run_dir: Path, log_path: Path | None = None):
    """Start the adapter with run_dir as working directory (model in run_dir/model); yield a CubeClient."""
    home = cube_home(cfg.cube.version)
    state = install_state(home)
    if state != "ok":
        raise StageError(_STATE_HINT[state].format(where=home_label()), EXIT_ENV)
    node = procutil.which("node")
    if not node:
        raise StageError(f"node (Node.js >= {MIN_NODE}) is not on PATH", EXIT_ENV)
    run_dir = Path(run_dir)
    if not (run_dir / "model").is_dir():
        raise StageError(f"no model directory in {run_dir.name}/: run `sxc.py cube` first", EXIT_ENV)
    secret = secrets.token_hex(32)
    env = child_env(cfg, int(cfg.cube.port), secret)
    proc = subprocess.Popen([node, str(home / "server.js")], cwd=str(run_dir), env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    lines: collections.deque = collections.deque(maxlen=200)
    ready = {"port": None}
    log = open(log_path, "a", encoding="utf-8") if log_path else None

    def pump():
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if log:
                log.write(line + "\n")
                log.flush()
            if line.startswith("SXC_READY ") and ready["port"] is None:
                ready["port"] = int(line.split()[1])

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        deadline = time.time() + cfg.cube.startup_timeout_s
        while ready["port"] is None:
            if proc.poll() is not None:
                t.join(timeout=5)              # the last lines explain why it stopped
                tail = " | ".join(x for x in lines if x.startswith(STARTUP_TAGS))[-800:] or "no adapter error line"
                raise StageError(f"Cube adapter exited during startup (code {proc.returncode}): {tail}"
                                 f"{_log_label(log_path)}", EXIT_ENV)
            if time.time() > deadline:
                raise StageError(f"Cube adapter did not become ready in time{_log_label(log_path)}", EXIT_ENV)
            time.sleep(0.1)
        client = CubeClient(f"http://127.0.0.1:{ready['port']}", secret, timeout_s=cfg.cube.query_timeout_s)
        client.errors = lines          # recent adapter lines (redacted unless SXC_CUBE_DEBUG=1), for diagnostics
        yield client
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        t.join(timeout=10)             # let the pump drain the pipe before its file and the log are closed
        proc.stdout.close()
        if log:
            log.close()
