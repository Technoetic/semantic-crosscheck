"""PostgreSQL access through pg8000 (pure Python). DSNs come only from environment variables.

DSN format: postgresql://user[:password]@host[:port]/database[?sslmode=...&sslrootcert=...]
(the password may also come from PGPASSWORD; reserved characters in the user name, password or database must be
percent-encoded). A malformed DSN is an environment error that names the variable, never the DSN: an unencoded '/',
'#' or '?' in a password makes the URL parser read part of the password as the port or the database, so the parser's
own error and even a "redacted" DSN could quote it. Such DSNs are refused before anything prints them.

sslmode follows libpq as far as pg8000 allows: disable (no TLS), allow, prefer or absent (TLS when the server offers
it, unverified: pg8000's default), require (TLS, unverified), verify-ca (TLS, certificate checked against sslrootcert
or the system store) and verify-full (verify-ca plus the host name). Client certificates and revocation lists are not
supported and are refused rather than silently ignored.
"""

import os
import re
import ssl
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from .result import EXIT_ENV, StageError

SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
UNSUPPORTED_SSL = ("sslcert", "sslkey", "sslpassword", "sslcrl", "sslcrldir", "sslcertmode")
MIN_SERVER_VERSION = 170000   # MERGE ... RETURNING merge_action(), WHEN NOT MATCHED BY SOURCE, COPY ON_ERROR
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})|%00")
_MALFORMED = ("holds a malformed DSN: use postgresql://user[:password]@host[:port]/database and percent-encode "
              "reserved characters (such as / # ? @ : %) in the user name and password")


class DsnError(ValueError):
    """Why a DSN was refused, as a short reason; never any part of the DSN."""


def _decode(part: str | None) -> str | None:
    if part is None:
        return None
    if _BAD_PERCENT.search(part):
        raise ValueError("bad percent-encoding")
    return urllib.parse.unquote(part, errors="strict")


def _params(query: str) -> dict:
    """key=value&... with percent-decoding only ('+' stays '+', as in libpq); the last value of a key wins."""
    out = {}
    for item in query.split("&") if query else ():
        key, eq, value = item.partition("=")
        if not eq or not key:
            raise ValueError("bad parameter")
        out[_decode(key)] = _decode(value)
    return out


def _scheme_ok(dsn: str) -> bool:
    return dsn.lower().startswith(("postgresql://", "postgres://"))


def _split(dsn: str) -> dict | None:
    """The parts of a structurally sound DSN, or None. An '@' after the host part or any '#' means the parser cut a
    password short (the rest of it would be read as the port, database or parameters), so those are refused too."""
    if not _scheme_ok(dsn):
        return None
    try:
        u = urllib.parse.urlsplit(dsn)
        port, host = u.port, u.hostname
        if "#" in dsn or "@" in u.path or "@" in u.query or port == 0:
            return None
        return {"scheme": u.scheme, "raw_user": u.username, "raw_path": u.path, "user": _decode(u.username),
                "password": _decode(u.password), "host": host, "port": port, "database": _decode(u.path[1:]),
                "params": _params(u.query)}
    except ValueError:                                     # bad port or brackets, bad percent-encoding
        return None


def parse_dsn(dsn: str, env_name: str) -> dict:
    """user, password, host, port, database, sslmode and sslrootcert of the DSN held by env_name. StageError(exit 3)
    names env_name and the problem, never the DSN or its password."""
    if not _scheme_ok(dsn):
        raise StageError(f"{env_name}: the DSN must start with postgresql:// or postgres://", EXIT_ENV) from DsnError("scheme")
    parts = _split(dsn)
    if parts is None:
        raise StageError(f"{env_name} {_MALFORMED}", EXIT_ENV) from DsnError("malformed")
    params = parts["params"]
    unsupported = sorted(set(params) & set(UNSUPPORTED_SSL))
    if unsupported:
        raise StageError(f"{env_name}: DSN parameter {unsupported[0]} is not supported (client certificates and "
                         "revocation lists are not implemented)", EXIT_ENV) from DsnError("unsupported_parameter")
    sslmode = params.get("sslmode", "")
    if sslmode and sslmode not in SSLMODES:
        raise StageError(f"{env_name}: sslmode must be one of {', '.join(SSLMODES)}", EXIT_ENV) from DsnError("sslmode")
    rootcert = params.get("sslrootcert", "")
    if rootcert and rootcert != "system":
        rootcert = str(Path(rootcert).expanduser().resolve())     # the Cube child runs from another directory
    return {"user": parts["user"] or "", "password": parts["password"] or os.environ.get("PGPASSWORD"),
            "host": parts["host"] or "127.0.0.1", "port": parts["port"] or 5432,
            "database": parts["database"] or "postgres", "sslmode": sslmode, "sslrootcert": rootcert}


def dsn_from_env(env_name: str, purpose: str = "PostgreSQL DSN") -> dict:
    """parse_dsn() of the variable env_name; StageError(exit 3) when it is unset or malformed."""
    dsn = os.environ.get(env_name)
    if not dsn:
        raise StageError(f"environment variable {env_name} is not set ({purpose})", EXIT_ENV)
    return parse_dsn(dsn, env_name)


def ssl_context(sslmode: str, rootcert: str, env_name: str):
    """pg8000's ssl_context for an sslmode: False (no TLS), None (TLS if offered, unverified), True (TLS required,
    unverified) or a verifying ssl.SSLContext."""
    if sslmode == "disable":
        return False
    if sslmode in ("", "allow", "prefer"):
        return None
    if sslmode == "require":
        return True
    try:
        ctx = ssl.create_default_context(cafile=rootcert if rootcert and rootcert != "system" else None)
    except OSError:                                        # missing or unreadable file; ssl.SSLError is an OSError
        ctx = None
    if ctx is None:
        raise StageError(f"{env_name}: sslrootcert could not be loaded as a PEM certificate file",
                         EXIT_ENV) from DsnError("sslrootcert")
    if sslmode == "verify-ca":
        ctx.check_hostname = False                         # the certificate chain is still verified (CERT_REQUIRED)
    return ctx


def _parse(dsn: str, env_name: str) -> dict:
    """pg8000.native.Connection keyword arguments for the DSN held by env_name."""
    p = parse_dsn(dsn, env_name)
    return {"user": p["user"], "password": p["password"], "host": p["host"], "port": p["port"],
            "database": p["database"], "ssl_context": ssl_context(p["sslmode"], p["sslrootcert"], env_name)}


def redact(dsn: str) -> str:
    """scheme://user@host:port/database of a DSN, without password or parameters; never raises."""
    parts = _split(dsn) if isinstance(dsn, str) else None
    if parts is None:
        return "postgresql://<malformed>"
    host = parts["host"] or ""
    host = f"[{host}]" if ":" in host else host
    user = f"{parts['raw_user']}@" if parts["raw_user"] else ""
    port = f":{parts['port']}" if parts["port"] else ""
    return f"{parts['scheme']}://{user}{host}{port}{parts['raw_path']}"


def connect(env_name: str, require_pg17: bool = True):
    """Open a connection from the DSN in env_name. Raises StageError(exit 3) if missing, malformed or unreachable, and
    (unless require_pg17 is False, as in doctor which reports the version as a check) if the server is older
    than PostgreSQL 17."""
    dsn = os.environ.get(env_name)
    if not dsn:
        raise StageError(f"environment variable {env_name} is not set (PostgreSQL DSN)", EXIT_ENV)
    kwargs = _parse(dsn, env_name)
    try:
        import pg8000.native
    except ImportError as e:
        raise StageError('python package pg8000 is missing: <python> -m pip install -r "<plugin>/requirements/core.txt"', EXIT_ENV) from e
    try:
        conn = pg8000.native.Connection(**kwargs)
    except Exception as e:  # noqa: BLE001 - surface connection problems as environment errors without the DSN
        raise StageError(f"cannot connect to {redact(dsn)} ({env_name}): {type(e).__name__}", EXIT_ENV) from e
    conn.run("SET application_name = 'semantic-crosscheck'")
    if require_pg17:
        version = server_version_num(conn)
        if version < MIN_SERVER_VERSION:
            conn.close()
            raise StageError(f"PostgreSQL 17 or later is required (server_version_num {version})", EXIT_ENV)
    return conn


def rows(conn, sql: str, **params) -> list[dict]:
    """Run SQL with :named parameters and return a list of dicts."""
    data = conn.run(sql, **params)
    cols = [c["name"] for c in (conn.columns or [])]
    return [dict(zip(cols, r)) for r in (data or [])]


def scalar(conn, sql: str, **params):
    r = conn.run(sql, **params)
    return r[0][0] if r else None


ISOLATION_LEVELS = (None, "repeatable read")


@contextmanager
def read_only(conn, timeout_ms: int = 60000, isolation: str | None = None):
    """READ ONLY transaction with a statement timeout; always rolled back.

    isolation None keeps the server default (READ COMMITTED: every statement takes a new snapshot); "repeatable read"
    makes every statement of the transaction read one snapshot, for a check that combines the results of several
    statements. Any other value is a programming error (ValueError) raised before a transaction is opened. Keep such
    a transaction short: never wait on Cube inside it, since an open REPEATABLE READ snapshot holds back vacuum."""
    if isolation not in ISOLATION_LEVELS:
        raise ValueError(f"read_only: isolation must be one of {ISOLATION_LEVELS}")
    conn.run("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" if isolation else "BEGIN TRANSACTION READ ONLY")
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
