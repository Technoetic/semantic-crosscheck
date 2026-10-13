"""DSN handling: malformed DSNs never reach output, sslmode is honoured, and old servers are refused.

- A password with an unencoded '/', '#' or '?' makes the URL parser read part of it as the port or the database.
  The parser's ValueError quoted it, redact() raised the same error while handling the first one, and the traceback
  printed the password. Now such a DSN is StageError(exit 3) naming the variable only, with a cause (DsnError) that
  doctor classifies as a bad DSN and that carries no part of the DSN; redact() never raises.
- sslmode maps onto pg8000's ssl_context the way libpq defines the modes: disable, allow/prefer (opportunistic),
  require (encrypted, unverified), verify-ca (chain checked against sslrootcert), verify-full (chain and host name).
- connect() refuses servers older than PostgreSQL 17 with exit 3 (closure needs MERGE ... RETURNING merge_action()).
No database or TLS server is needed: pg8000's Connection is replaced by a stand-in where a connection is involved.
"""

import os
import ssl
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import db
from semantic_crosscheck.result import EXIT_ENV, StageError

SECRET = "Hunter2TopSecret"
ENV = "SXC_T_SEC_DSN"
SHORT = "p0"                      # credentials are interpolated so no literal user:password@ appears in the source
ENCODED = "p%2Fw%23d%3F"          # p/w#d? percent-encoded
MALFORMED = [
    f"postgresql://xc_check:Hunter2/{SECRET}@127.0.0.1:5432/db",     # '/' ends the authority: port 'Hunter2'
    f"postgresql://xc_check:Hunter2#{SECRET}@127.0.0.1:5432/db",     # '#' starts a fragment
    f"postgresql://xc_check:Hunter2?{SECRET}@127.0.0.1:5432/db",     # '?' starts the query
    f"postgresql://xc_check:1234/{SECRET}@127.0.0.1:5432/db",        # digits parse as a port; the rest as the path
    f"postgresql://xc_check:12?s={SECRET}@127.0.0.1:5432/db",        # ... or as a parameter
    f"postgresql://xc_check:{SECRET}@127.0.0.1:99999/db",            # port out of range
    f"postgresql://xc_check:{SECRET}@127.0.0.1:0/db",
    f"postgresql://xc_check:{SECRET}%zz@127.0.0.1/db",               # '%' without two hex digits
    f"postgresql://xc_check:{SECRET}%ff@127.0.0.1/db",               # percent-encoded bytes that are not UTF-8
    f"postgresql://xc_check:{SECRET}%00@127.0.0.1/db",
    f"postgresql://xc_check:{SECRET}@[::1/db",                       # unbalanced IPv6 bracket
    f"postgresql://xc_check:{SECRET}@127.0.0.1/db?sslmode",          # parameter without '='
]


class FakeConnection:
    """pg8000.native.Connection stand-in: records its keyword arguments and answers SHOW server_version_num."""
    version = "170004"
    made: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        FakeConnection.made.append(self)

    def run(self, sql, **params):
        return [[self.version]] if sql == "SHOW server_version_num" else None

    def close(self):
        self.closed = True


class DsnCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PGPASSWORD", None)
        FakeConnection.made = []
        FakeConnection.version = "170004"

    def connect_with(self, dsn: str, **kw):
        os.environ[ENV] = dsn
        with mock.patch("pg8000.native.Connection", FakeConnection):
            return db.connect(ENV, **kw)


class MalformedDsnTest(DsnCase):
    def test_malformed_dsns_are_environment_errors_naming_only_the_variable(self):
        for dsn in MALFORMED:
            with self.subTest(dsn=dsn.replace(SECRET, "<secret>")):
                with self.assertRaises(StageError) as caught:
                    self.connect_with(dsn)
                e = caught.exception
                self.assertEqual(e.exit_code, EXIT_ENV)
                self.assertTrue(str(e).startswith(f"{ENV} holds a malformed DSN"), str(e))
                self.assertIsInstance(e.__cause__, db.DsnError)
                self.assertIsInstance(e.__cause__, ValueError, "doctor reports a ValueError cause as bad_dsn")
                self.assertIsNone(e.__context__, "no exception carrying the DSN is chained")
                printed = "".join(traceback.format_exception(e))
                for part in (SECRET, "Hunter2", "xc_check", "127.0.0.1"):
                    self.assertNotIn(part, printed)
                self.assertEqual(FakeConnection.made, [], "no connection is attempted")
                self.assertEqual(db.redact(dsn), "postgresql://<malformed>")

    def test_wrong_scheme_and_missing_variable(self):
        for dsn in (f"mysql://u:{SECRET}@h/db", f"host=h user=u password={SECRET}", f"postgresql:u:{SECRET}@h/db"):
            with self.subTest(dsn=dsn.split(":")[0]):
                with self.assertRaises(StageError) as caught:
                    self.connect_with(dsn)
                self.assertEqual(caught.exception.exit_code, EXIT_ENV)
                self.assertIn(f"{ENV}: the DSN must start with postgresql:// or postgres://", str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))
        os.environ.pop(ENV, None)
        with self.assertRaises(StageError) as caught:
            db.connect(ENV)
        self.assertEqual((caught.exception.exit_code, str(caught.exception)),
                         (EXIT_ENV, f"environment variable {ENV} is not set (PostgreSQL DSN)"))

    def test_redact_never_raises(self):
        for value in MALFORMED + ["", "postgresql://", f"postgresql://u:{SHORT}@h:5432/db?sslmode=require", "\x00", "://@"]:
            with self.subTest(value=value.replace(SECRET, "<secret>")):
                text = db.redact(value)
                self.assertNotIn(SECRET, text)
                self.assertNotIn(f":{SHORT}@", text)
        self.assertEqual(db.redact(f"postgresql://u:{ENCODED}@[::1]:5433/my%20db?sslmode=require"),
                         "postgresql://u@[::1]:5433/my%20db")
        self.assertEqual(db.redact(None), "postgresql://<malformed>")


class ParseTest(DsnCase):
    def test_parts_are_percent_decoded(self):
        p = db.parse_dsn(f"postgresql://us%40er:{ENCODED}@db.example.org:6543/my%20db", ENV)
        self.assertEqual((p["user"], p["password"], p["host"], p["port"], p["database"]),
                         ("us@er", "p/w#d?", "db.example.org", 6543, "my db"))
        p = db.parse_dsn("postgres://u@h", ENV)
        self.assertEqual((p["user"], p["password"], p["port"], p["database"], p["sslmode"]), ("u", None, 5432, "postgres", ""))
        os.environ["PGPASSWORD"] = "from-env"
        self.assertEqual(db.parse_dsn("postgresql://u@h/db", ENV)["password"], "from-env")

    def test_unknown_or_unsupported_ssl_settings_are_refused(self):
        cases = {"sslmode=verify_full": "sslmode must be one of", "sslmode=": None, "sslcert=client.pem": "sslcert",
                 "sslkey=k.pem": "sslkey", "sslcrl=list.crl": "sslcrl"}
        for query, expected in cases.items():
            with self.subTest(query=query):
                dsn = f"postgresql://u:{SECRET}@h/db?{query}"
                if expected is None:
                    self.assertEqual(db.parse_dsn(dsn, ENV)["sslmode"], "")
                    continue
                with self.assertRaises(StageError) as caught:
                    db.parse_dsn(dsn, ENV)
                self.assertEqual(caught.exception.exit_code, EXIT_ENV)
                self.assertIn(expected, str(caught.exception))
                self.assertIsInstance(caught.exception.__cause__, db.DsnError)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_sslrootcert_is_made_absolute_and_plus_stays_plus(self):
        p = db.parse_dsn("postgresql://u@h/db?sslmode=verify-ca&sslrootcert=certs/ca+root.pem", ENV)
        self.assertTrue(Path(p["sslrootcert"]).is_absolute())
        self.assertEqual(Path(p["sslrootcert"]).name, "ca+root.pem")
        self.assertEqual(db.parse_dsn("postgresql://u@h/db?sslmode=verify-full&sslrootcert=system", ENV)["sslrootcert"],
                         "system")


class SslModeTest(DsnCase):
    def context_for(self, query: str):
        return self.connect_with(f"postgresql://u@db.example.org/db?{query}").kwargs["ssl_context"]

    def test_plain_modes(self):
        self.assertIs(self.context_for("sslmode=disable"), False)
        for query in ("sslmode=allow", "sslmode=prefer", "application_name=x"):
            with self.subTest(query=query):
                self.assertIsNone(self.context_for(query), "pg8000 default: TLS when offered, unverified")
        self.assertIs(self.context_for("sslmode=require"), True)

    def test_verify_modes_build_verifying_contexts(self):
        made = []
        real = ssl.create_default_context

        def recording(*args, **kwargs):
            made.append(kwargs)
            return real()

        with mock.patch.object(db.ssl, "create_default_context", recording):
            ca = self.context_for("sslmode=verify-ca&sslrootcert=ca.pem")
            full = self.context_for("sslmode=verify-full")
            system = self.context_for("sslmode=verify-full&sslrootcert=system")
        self.assertEqual(Path(made[0]["cafile"]).name, "ca.pem")
        self.assertTrue(Path(made[0]["cafile"]).is_absolute())
        self.assertEqual((made[1]["cafile"], made[2]["cafile"]), (None, None))
        self.assertEqual((ca.verify_mode, ca.check_hostname), (ssl.CERT_REQUIRED, False))
        self.assertEqual((full.verify_mode, full.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertEqual((system.verify_mode, system.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_unloadable_root_certificate_is_an_environment_error(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "not-a-cert.pem"
            bad.write_text("not a certificate\n", encoding="utf-8")
            for path in (bad, Path(d) / "missing.pem"):
                with self.subTest(file=path.name):
                    with self.assertRaises(StageError) as caught:
                        self.context_for(f"sslmode=verify-ca&sslrootcert={path.as_posix()}")
                    self.assertEqual(caught.exception.exit_code, EXIT_ENV)
                    self.assertEqual(str(caught.exception), f"{ENV}: sslrootcert could not be loaded as a PEM certificate file")
                    self.assertIsInstance(caught.exception.__cause__, db.DsnError)


class ConnectTest(DsnCase):
    def test_server_older_than_17_is_refused_and_closed(self):
        FakeConnection.version = "160004"
        with self.assertRaises(StageError) as caught:
            self.connect_with("postgresql://u@h/db")
        self.assertEqual(caught.exception.exit_code, EXIT_ENV)
        self.assertIn("PostgreSQL 17 or later is required", str(caught.exception))
        self.assertTrue(FakeConnection.made[0].closed)
        conn = self.connect_with("postgresql://u@h/db", require_pg17=False)
        self.assertFalse(conn.closed)

    def test_connection_arguments(self):
        conn = self.connect_with(f"postgresql://u:{SHORT}@db.example.org:6543/name?sslmode=disable")
        self.assertEqual(conn.kwargs, {"user": "u", "password": SHORT, "host": "db.example.org", "port": 6543,
                                       "database": "name", "ssl_context": False})

    def test_connection_failure_message_is_redacted(self):
        class Refusing(FakeConnection):
            def __init__(self, **kwargs):
                raise OSError("connection refused")

        os.environ[ENV] = f"postgresql://xc_check:{SECRET}@db.example.org:6543/name"
        with mock.patch("pg8000.native.Connection", Refusing):
            with self.assertRaises(StageError) as caught:
                db.connect(ENV)
        self.assertEqual(str(caught.exception), f"cannot connect to postgresql://xc_check@db.example.org:6543/name ({ENV}): OSError")


if __name__ == "__main__":
    unittest.main()
