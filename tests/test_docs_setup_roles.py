"""docs/setup-roles.sql, run the way psql runs it.

Why: people run this template once, with psql, before anything else, so nothing else notices when it stops working:
a statement PostgreSQL refuses, a variable the README does not pass, a password added for convenience, or a grant
the stages turn out to need. psql may not be available where the tests run, so this module carries a minimal
interpreter of the psql features the file uses: variable interpolation (:'name' and :"name"), \\set ON_ERROR_STOP,
\\gexec and \\gset. Any other meta-command fails the test, so the file cannot drift into features the interpreter does
not emulate. CI runs the file with the real psql as well (.github/workflows/tests.yml).

Unit tests read the file and the documents that tell people how to run it. The integration tests (SXC_TEST_ADMIN_DSN,
a superuser) use database sxc_f_docs_roles and roles named sxc_f_docs_*: the file must run twice without error, stop
at its first statement when a variable is missing, create login roles without administrative attributes, support
the "existing data" path its header describes, and grant exactly what the stages need, which demo, doctor and build
then prove through the command line.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pgfixture

from semantic_crosscheck import config

ROOT = pgfixture.ROOT
SQL_FILE = ROOT / "docs" / "setup-roles.sql"
SXC = ROOT / "skills" / "semantic-crosscheck" / "scripts" / "sxc.py"
NAMES = ("loader_role", "check_role", "source_schema", "output_schema")
DATABASE = "sxc_f_docs_roles"
PREFIX = "sxc_f_docs_"
_IDENT = re.compile(r"[A-Za-z0-9_]+")


class PsqlError(Exception):
    """A statement failed while ON_ERROR_STOP was on; args[0] is its SQLSTATE."""


def quote_literal(value: str) -> str:
    """psql's :'name' (PQescapeLiteral): single quotes doubled, an E'' string when the value has a backslash."""
    body = value.replace("'", "''")
    if "\\" in value:
        return " E'" + body.replace("\\", "\\\\") + "'"
    return "'" + body + "'"


def quote_ident(value: str) -> str:
    """psql's :"name" (PQescapeIdentifier)."""
    return '"' + value.replace('"', '""') + '"'


def _closing(text: str, i: int) -> int:
    """Index after the quoted token starting at text[i] (a doubled quote character is part of the token)."""
    q, j = text[i], i + 1
    while j < len(text):
        if text[j] == q:
            if text[j + 1:j + 2] == q:
                j += 2
                continue
            return j + 1
        j += 1
    return len(text)


def _sqlstate(e: BaseException) -> str:
    arg = e.args[0] if e.args else None
    return str(arg.get("C", "")) if isinstance(arg, dict) else ""


class Psql:
    """The subset of psql that docs/setup-roles.sql uses, over a pg8000.native connection in autocommit mode (as psql).

    conn.run(sql) without parameters uses the simple query protocol, so the interpolated SQL reaches the server as
    psql would send it; an unset variable stays in the text and the server rejects it."""

    def __init__(self, conn, variables: dict):
        self.conn, self.vars = conn, dict(variables)
        self.executed: list[str] = []

    def run_file(self, text: str) -> None:
        buf: list[str] = []
        i, n = 0, len(text)
        while i < n:
            c, two = text[i], text[i:i + 2]
            if two == "--":
                j = text.find("\n", i)
                i = n if j < 0 else j
            elif two == "/*":
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
            elif c in "'\"":
                j = _closing(text, i)
                buf.append(text[i:j])
                i = j
            elif two == "::":
                buf.append(two)
                i += 2
            elif c == ":":
                token, i = self._interpolate(text, i)
                buf.append(token)
            elif c == ";":
                self._execute("".join(buf))
                buf, i = [], i + 1
            elif c == "\\":
                j = text.find("\n", i)
                j = n if j < 0 else j
                if self._meta(text[i + 1:j].split(), "".join(buf)):
                    buf = []
                i = j
            else:
                buf.append(c)
                i += 1
        if "".join(buf).strip():
            self._execute("".join(buf))

    def _interpolate(self, text: str, i: int) -> tuple[str, int]:
        quote = text[i + 1:i + 2]
        if quote in ("'", '"'):
            end = text.find(quote, i + 2)
            name = text[i + 2:end] if end > 0 else ""
            if _IDENT.fullmatch(name) and name in self.vars:
                value = self.vars[name]
                return (quote_literal(value) if quote == "'" else quote_ident(value)), end + 1
            return text[i:i + 2], i + 2
        m = _IDENT.match(text, i + 1)
        if m and m.group(0) in self.vars:
            return self.vars[m.group(0)], m.end()
        return ":", i + 1

    def _meta(self, words: list[str], query: str) -> bool:
        """Run one meta-command; True when it sent (and so consumed) the query buffer."""
        name, args = (words[0], words[1:]) if words else ("", [])
        if name == "set" and len(args) == 2:
            self.vars[args[0]] = args[1]
            return False
        if name == "gexec":
            for row in self._execute(query):
                for value in row:
                    if value is not None:
                        self._execute(str(value))
            return True
        if name == "gset":
            rows = self._execute(query)
            if len(rows) != 1:
                self._fail("gset")
                return True
            prefix = args[0] if args else ""
            for col, value in zip((c["name"] for c in self.conn.columns or []), rows[0]):
                if value is None:
                    self.vars.pop(prefix + col, None)
                else:
                    self.vars[prefix + col] = str(value)
            return True
        raise AssertionError(f"psql meta-command not emulated by this test: \\{' '.join(words)}")

    def _fail(self, sqlstate: str) -> None:
        if self.vars.get("ON_ERROR_STOP") == "on":
            raise PsqlError(sqlstate)

    def _execute(self, sql: str) -> list:
        sql = sql.strip()
        if not sql:
            return []
        self.executed.append(sql)
        try:
            return self.conn.run(sql) or []
        except Exception as e:  # noqa: BLE001 - a server error, as psql reports it
            code = _sqlstate(e)
            if not code:
                raise
            self._fail(code)
            return []


class FakeConn:
    """Records statements; answers the role query (both roles missing) and current_database()."""

    def __init__(self, fail_on_placeholder: bool = False):
        self.sql: list[str] = []
        self.columns = None
        self.fail_on_placeholder = fail_on_placeholder

    def run(self, sql: str):
        self.sql.append(sql)
        if self.fail_on_placeholder and (":'" in sql or ':"' in sql):
            raise RuntimeError({"C": "42601"})
        if sql.startswith("SELECT format("):
            names = re.findall(r"\('([^']*)'\)", sql)
            return [[f'CREATE ROLE "{n}" LOGIN NOSUPERUSER'] for n in names]
        if sql.startswith("SELECT current_database()"):
            self.columns = [{"name": "sxc_database"}]
            return [["somedb"]]
        return []


def sql_text() -> str:
    return SQL_FILE.read_text(encoding="utf-8")


def code_part(text: str) -> str:
    """The file without its -- comments."""
    return "\n".join(line.split("--", 1)[0] for line in text.split("\n"))


def documented_runs(path: Path) -> list[dict]:
    """{variable: value} of every `psql ... -f docs/setup-roles.sql` command line in a document."""
    runs = []
    for line in path.read_text(encoding="utf-8").replace("\\\n", " ").split("\n"):
        command = line.strip().lstrip("-").strip()          # a command line, also inside an SQL comment
        if not command.startswith("psql ") or "setup-roles.sql" not in command:
            continue
        tokens = shlex.split(command.split(" #", 1)[0], posix=True)
        if "-f" not in tokens:
            continue
        values = {}
        for k, t in enumerate(tokens):
            if t == "-v" and k + 1 < len(tokens):
                name, _sep, value = tokens[k + 1].partition("=")
                values[name] = value
        runs.append(values)
    return runs


class SetupRolesFileTest(unittest.TestCase):
    def test_runs_with_the_emulated_psql_features_only(self):
        conn = FakeConn()
        names = {"loader_role": "l1", "check_role": "c1", "source_schema": "s1", "output_schema": "o1"}
        Psql(conn, names).run_file(sql_text())
        guard = conn.sql[0]
        for value in names.values():
            self.assertIn(f"'{value}'", guard)
        self.assertEqual(conn.sql[1][:14], "SELECT format(")
        self.assertIn("('l1'), ('c1')", conn.sql[1])
        self.assertEqual(conn.sql[2:4], ['CREATE ROLE "l1" LOGIN NOSUPERUSER', 'CREATE ROLE "c1" LOGIN NOSUPERUSER'])
        statements = conn.sql[4:]
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "s1" AUTHORIZATION "l1"', statements)
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "o1" AUTHORIZATION "c1"', statements)
        self.assertIn('GRANT TEMPORARY ON DATABASE "somedb" TO "c1"', statements)
        for sql in conn.sql:
            with self.subTest(sql=sql[:60]):
                self.assertNotRegex(sql, r":['\"]?[A-Za-z_]", "a psql variable was left uninterpolated")

    def test_a_missing_variable_stops_at_the_first_statement(self):
        full = {"loader_role": "l1", "check_role": "c1", "source_schema": "s1", "output_schema": "o1"}
        for missing in NAMES:
            with self.subTest(missing=missing):
                conn = FakeConn(fail_on_placeholder=True)
                with self.assertRaises(PsqlError) as e:
                    Psql(conn, {k: v for k, v in full.items() if k != missing}).run_file(sql_text())
                self.assertEqual(e.exception.args[0], "42601")
                self.assertEqual(len(conn.sql), 1)

    def test_uses_the_four_documented_variables(self):
        code = code_part(sql_text())
        used = set(re.findall(r""":['"]([A-Za-z0-9_]+)['"]""", code))
        set_by_gset = {"sxc_database"}
        self.assertEqual(used - set_by_gset, set(NAMES))
        self.assertIn("\\set ON_ERROR_STOP on", code)

    def test_no_password_and_superuser_only(self):
        code = code_part(sql_text())
        self.assertNotRegex(code.upper(), r"\bPASSWORD\b")
        self.assertNotRegex(code.upper(), r"\bENCRYPTED\b")
        header = sql_text().split("\\set", 1)[0]
        self.assertIn("as a superuser", header)
        self.assertNotIn("or a role with CREATEROLE", header)
        self.assertIn("not tested", header)
        for attribute in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
            self.assertIn(attribute, code)

    def test_documents_pass_the_variables_the_file_uses(self):
        demo = config.load(ROOT / "demo" / "crosscheck.toml")
        sources = {"README.md": ROOT / "README.md", "tests.yml": ROOT / ".github" / "workflows" / "tests.yml",
                   "setup-roles.sql": SQL_FILE}
        for label, path in sources.items():
            runs = documented_runs(path)
            with self.subTest(document=label):
                self.assertTrue(runs, "no psql command line runs docs/setup-roles.sql")
                for values in runs:
                    self.assertEqual(set(values), set(NAMES))
                    self.assertIn(values["source_schema"], demo.source.schemas)
                    self.assertEqual(values["output_schema"], demo.source.output_schema)
                    self.assertNotEqual(values["loader_role"], values["check_role"])


@unittest.skipUnless(pgfixture.available(), "SXC_TEST_ADMIN_DSN not set")
class SetupRolesDatabaseTest(unittest.TestCase):
    """Integration: the file as a superuser, then the stages with the roles it created."""

    LOADER, CHECK = PREFIX + "loader", PREFIX + "check"
    OWNER, READER = PREFIX + "owner", PREFIX + "reader"
    UNSET = PREFIX + "unset"
    ROLES = (LOADER, CHECK, OWNER, READER, UNSET, UNSET + "_c")

    @classmethod
    def setUpClass(cls):
        cls._drop()
        a = pgfixture._admin()
        try:
            a.run(f'CREATE DATABASE "{DATABASE}"')
        finally:
            a.close()

    @classmethod
    def tearDownClass(cls):
        cls._drop()

    @classmethod
    def _drop(cls):
        a = pgfixture._admin()
        try:
            a.run(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
            for role in cls.ROLES:
                a.run(f'DROP ROLE IF EXISTS "{role}"')
        finally:
            a.close()

    def psql(self, names: dict) -> Psql:
        conn = pgfixture._admin(DATABASE)
        try:
            p = Psql(conn, names)
            p.run_file(sql_text())
            return p
        finally:
            conn.close()

    def admin_rows(self, sql: str, **params) -> list:
        conn = pgfixture._admin(DATABASE)
        try:
            return conn.run(sql, **params)
        finally:
            conn.close()

    def set_passwords(self, *roles):
        pw = os.environ.get(pgfixture.PASSWORD_ENV)
        if not pw:
            return
        conn = pgfixture._admin()
        try:
            for role in roles:
                conn.run(f"ALTER ROLE \"{role}\" PASSWORD '{pw.replace(chr(39), chr(39) * 2)}'")
        finally:
            conn.close()

    def sxc(self, out: str, *argv) -> tuple[int, dict]:
        env = dict(os.environ, SXC_PG_OWNER_DSN=pgfixture.dsn_for(self.LOADER, DATABASE),
                   SXC_PG_DSN=pgfixture.dsn_for(self.CHECK, DATABASE))
        stage = argv[0]
        p = subprocess.run([sys.executable, "-X", "utf8", str(SXC), *argv, "--config", str(pgfixture.DEMO_CONFIG),
                            "--out", out], env=env, capture_output=True, text=True, encoding="utf-8", timeout=900)
        result = json.loads((Path(out) / stage / "result.json").read_text(encoding="utf-8"))
        return p.returncode, result

    def test_roles_grants_and_the_first_stages(self):
        names = {"loader_role": self.LOADER, "check_role": self.CHECK, "source_schema": "sxc_demo",
                 "output_schema": "sxc_out"}
        self.psql(names)
        second = self.psql(names)                       # idempotent: a rerun neither fails nor creates roles
        self.assertEqual(len([s for s in second.executed if s.startswith("CREATE ROLE")]), 0)
        roles = self.admin_rows("SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                                "rolbypassrls FROM pg_roles WHERE rolname = ANY(:r) ORDER BY 1", r=[self.LOADER, self.CHECK])
        self.assertEqual([r[0] for r in roles], sorted([self.LOADER, self.CHECK]))
        for r in roles:
            self.assertEqual(r[1:], [True, False, False, False, False, False])
        owners = dict(self.admin_rows("SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
                                      "WHERE nspname IN ('sxc_demo', 'sxc_out')"))
        self.assertEqual(owners, {"sxc_demo": self.LOADER, "sxc_out": self.CHECK})
        self.set_passwords(self.LOADER, self.CHECK)
        with tempfile.TemporaryDirectory() as out:
            rc, demo = self.sxc(out, "demo", "--variant", "clean")
            self.assertEqual((rc, demo["status"]), (0, "pass"))
            rc, doctor = self.sxc(out, "doctor", "--require", "core")
            self.assertEqual((rc, doctor["status"]), (0, "pass"), [c for c in doctor["checks"] if c["status"] != "pass"])
            ids = {c["id"] for c in doctor["checks"]}
            self.assertLessEqual({"privileges:temporary", "privileges:select:part", "privileges:no_write:part",
                                  "output_schema"}, ids)
            rc, build = self.sxc(out, "build")
            self.assertEqual((rc, build["status"]), (0, "pass"), [c for c in build["checks"] if c["status"] != "pass"])

    def test_a_missing_variable_changes_nothing(self):
        full = {"loader_role": self.UNSET, "check_role": self.UNSET + "_c", "source_schema": PREFIX + "unset_src",
                "output_schema": PREFIX + "unset_out"}
        for missing in ("check_role", "source_schema"):
            with self.subTest(missing=missing):
                with self.assertRaises(PsqlError) as e:
                    self.psql({k: v for k, v in full.items() if k != missing})
                self.assertEqual(e.exception.args[0], "42601")
                self.assertEqual(self.admin_rows("SELECT count(*) FROM pg_roles WHERE rolname LIKE :p",
                                                 p=self.UNSET + "%"), [[0]])
                self.assertEqual(self.admin_rows("SELECT count(*) FROM pg_namespace WHERE nspname LIKE :p",
                                                 p=PREFIX + "unset%"), [[0]])

    def test_existing_data_keeps_the_owner_and_grants_read_access(self):
        conn = pgfixture._admin(DATABASE)
        try:
            conn.run(f'CREATE ROLE "{self.OWNER}" NOLOGIN')
            conn.run(f'CREATE SCHEMA existing_src AUTHORIZATION "{self.OWNER}"')
            conn.run(f'SET ROLE "{self.OWNER}"')
            conn.run("CREATE TABLE existing_src.item (id integer PRIMARY KEY)")
            conn.run("RESET ROLE")
        finally:
            conn.close()
        self.psql({"loader_role": self.OWNER, "check_role": self.READER, "source_schema": "existing_src",
                   "output_schema": "existing_out"})
        conn = pgfixture._admin(DATABASE)
        try:
            conn.run(f'SET ROLE "{self.OWNER}"')
            conn.run("CREATE TABLE existing_src.later (id integer PRIMARY KEY)")   # default privileges apply
            conn.run("RESET ROLE")
            rows = conn.run(
                "SELECT pg_get_userbyid(n.nspowner), has_schema_privilege(:r, 'existing_src', 'USAGE'), "
                "has_schema_privilege(:r, 'existing_src', 'CREATE'), has_table_privilege(:r, 'existing_src.item', "
                "'SELECT'), has_table_privilege(:r, 'existing_src.later', 'SELECT'), "
                "has_table_privilege(:r, 'existing_src.item', 'INSERT'), "
                "has_schema_privilege(:r, 'existing_out', 'CREATE') FROM pg_namespace n WHERE n.nspname = 'existing_src'",
                r=self.READER)
        finally:
            conn.close()
        self.assertEqual(rows, [[self.OWNER, True, False, True, True, False, True]])


if __name__ == "__main__":
    unittest.main()
