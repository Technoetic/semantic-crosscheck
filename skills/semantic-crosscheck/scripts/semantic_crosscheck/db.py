"""PostgreSQL access through pg8000 (pure Python). DSNs come only from environment variables.

DSN format: postgresql://user[:password]@host[:port]/database  (password may also come from PGPASSWORD)
"""

import os
import urllib.parse
from contextlib import contextmanager

from .result import EXIT_ENV, StageError


def _parse(dsn: str) -> dict:
    u = urllib.parse.urlparse(dsn)
    if u.scheme not in ("postgresql", "postgres"):
        raise StageError("DSN must start with postgresql://", EXIT_ENV)
    return {
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password) if u.password else os.environ.get("PGPASSWORD"),
        "host": u.hostname or "127.0.0.1",
        "port": u.port or 5432,
        "database": (u.path or "/").lstrip("/") or "postgres",
    }


def redact(dsn: str) -> str:
    u = urllib.parse.urlparse(dsn)
    netloc = (u.username or "") + ("@" if u.username else "") + (u.hostname or "") + (f":{u.port}" if u.port else "")
    return f"{u.scheme}://{netloc}{u.path}"


def connect(env_name: str):
    """Open a connection from the DSN in env_name. Raises StageError(exit 3) if missing or unreachable."""
    dsn = os.environ.get(env_name)
    if not dsn:
        raise StageError(f"environment variable {env_name} is not set (PostgreSQL DSN)", EXIT_ENV)
    try:
        import pg8000.native
    except ImportError as e:
        raise StageError("python package pg8000 is missing: pip install -r requirements/core.txt", EXIT_ENV) from e
    try:
        conn = pg8000.native.Connection(**_parse(dsn))
    except Exception as e:  # noqa: BLE001 - surface connection problems as environment errors without the DSN
        raise StageError(f"cannot connect to {redact(dsn)}: {type(e).__name__}", EXIT_ENV) from e
    conn.run("SET application_name = 'semantic-crosscheck'")
    return conn


def rows(conn, sql: str, **params) -> list[dict]:
    """Run SQL with :named parameters and return a list of dicts."""
    data = conn.run(sql, **params)
    cols = [c["name"] for c in (conn.columns or [])]
    return [dict(zip(cols, r)) for r in (data or [])]


def scalar(conn, sql: str, **params):
    r = conn.run(sql, **params)
    return r[0][0] if r else None


@contextmanager
def read_only(conn, timeout_ms: int = 60000):
    """READ ONLY transaction with a statement timeout; always rolled back."""
    conn.run("BEGIN TRANSACTION READ ONLY")
    try:
        conn.run(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        yield conn
    finally:
        conn.run("ROLLBACK")


@contextmanager
def write_tx(conn, timeout_ms: int = 600000):
    conn.run("BEGIN")
    try:
        conn.run(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        yield conn
        conn.run("COMMIT")
    except Exception:
        conn.run("ROLLBACK")
        raise


def server_version_num(conn) -> int:
    return int(scalar(conn, "SHOW server_version_num"))
