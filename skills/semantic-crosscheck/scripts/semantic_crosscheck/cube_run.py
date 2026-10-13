"""Run the Cube adapter (adapters/cube/server.js) as a child process for one generated model.

    with cube_run.serve(cfg, run_dir) as client:      # run_dir contains model/*.yml
        client.load({...})

- The adapter is installed once per user by `sxc.py cube-install` into cube_home() (pinned package-lock and
  SHA-256-pinned native module); nothing is downloaded here.
- Environment for the child: inherited variables minus every CUBEJS_* / PG* setting, plus the connection
  parsed from the Cube DSN variable, a random per-run API secret, development mode off, telemetry off and
  PGOPTIONS forcing read-only transactions for every Cube session.
- It listens on 127.0.0.1 (port from [cube].port, 0 = ephemeral); the chosen port is read from "SXC_READY <port>".
- Stopping closes the child's stdin (the adapter exits) and kills it if it does not.
"""

import collections
import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import db
from .cube_client import CubeClient
from .result import EXIT_ENV, StageError

ADAPTER_DIR = Path(__file__).resolve().parents[2] / "adapters" / "cube"


def cube_home(version: str = "1.8.3") -> Path:
    """Per-user install location of the adapter (override with SXC_CUBE_HOME)."""
    if os.environ.get("SXC_CUBE_HOME"):
        return Path(os.environ["SXC_CUBE_HOME"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "semantic-crosscheck" / f"cube-{version}"


def installed(home: Path) -> bool:
    return (home / "server.js").is_file() and (home / "node_modules" / "@cubejs-backend" / "server-core").is_dir()


def child_env(cfg, port: int, secret: str) -> dict:
    dsn = os.environ.get(cfg.cube_dsn_env())
    if not dsn:
        raise StageError(f"environment variable {cfg.cube_dsn_env()} is not set (Cube connection)", EXIT_ENV)
    p = db._parse(dsn)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CUBEJS_", "PG", "SXC_PG"))}
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
        "PGOPTIONS": "-c default_transaction_read_only=on",
        "SXC_PORT": str(port),
        "SXC_MODEL_DIR": "model",
    })
    if p.get("password"):
        env["CUBEJS_DB_PASS"] = p["password"]
    sslmode = urllib.parse.parse_qs(urllib.parse.urlparse(dsn).query).get("sslmode", ["disable"])[0]
    if sslmode in ("require", "verify-ca", "verify-full"):
        env["CUBEJS_DB_SSL"] = "true"
        if sslmode == "require":
            env["CUBEJS_DB_SSL_REJECT_UNAUTHORIZED"] = "false"
    else:
        env["CUBEJS_DB_SSL"] = "false"
    return env


@contextlib.contextmanager
def serve(cfg, run_dir: Path, log_path: Path | None = None):
    """Start the adapter with run_dir as working directory (model in run_dir/model); yield a CubeClient."""
    home = cube_home(cfg.cube.version)
    if not installed(home):
        raise StageError(f"Cube adapter is not installed in {home}: run `sxc.py cube-install`", EXIT_ENV)
    node = shutil.which("node")
    if not node:
        raise StageError("node (Node.js >= 18) is not on PATH", EXIT_ENV)
    run_dir = Path(run_dir)
    if not (run_dir / "model").is_dir():
        raise StageError(f"no model directory in {run_dir}", EXIT_ENV)
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
                tail = " | ".join(x for x in lines if x.startswith("SXC_"))[-800:]
                raise StageError(f"Cube adapter exited during startup (code {proc.returncode}): {tail}", EXIT_ENV)
            if time.time() > deadline:
                raise StageError("Cube adapter did not become ready in time", EXIT_ENV)
            time.sleep(0.1)
        client = CubeClient(f"http://127.0.0.1:{ready['port']}", secret, timeout_s=cfg.cube.query_timeout_s)
        client.errors = lines          # recent adapter error lines, for diagnostics
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
        if log:
            log.close()
