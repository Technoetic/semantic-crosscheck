"""Integration-test fixture: a throw-away database with the loader and check roles and the demo data.

Enabled only when SXC_TEST_ADMIN_DSN names a superuser connection (for example
postgresql://postgres@127.0.0.1:5432/postgres). SXC_TEST_ROLE_PASSWORD, when set, becomes the password of the
two test roles (needed where the server requires password authentication, such as a CI service container).
"""

import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "semantic-crosscheck" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from semantic_crosscheck import db, demo  # noqa: E402

ADMIN_ENV = "SXC_TEST_ADMIN_DSN"
PASSWORD_ENV = "SXC_TEST_ROLE_PASSWORD"
DEMO_CONFIG = ROOT / "demo" / "crosscheck.toml"


def available() -> bool:
    return bool(os.environ.get(ADMIN_ENV))


def dsn_for(user: str, database: str) -> str:
    base = urllib.parse.urlparse(os.environ[ADMIN_ENV])
    pw = os.environ.get(PASSWORD_ENV)
    auth = urllib.parse.quote(user) + (":" + urllib.parse.quote(pw, safe="") if pw else "")
    host = base.hostname or "127.0.0.1"
    port = f":{base.port}" if base.port else ""
    return f"postgresql://{auth}@{host}{port}/{database}"


def _admin(database: str | None = None):
    env = "SXC_TEST__ADMIN_TMP"
    base = os.environ[ADMIN_ENV]
    if database:
        u = urllib.parse.urlparse(base)
        base = urllib.parse.urlunparse(u._replace(path="/" + database))
    os.environ[env] = base
    try:
        return db.connect(env)
    finally:
        del os.environ[env]


class PgFixture:
    """setup() creates database `name`, roles xc_owner/xc_check, schemas sxc_demo (owner) and sxc_out (check),
    loads the demo variant as the loader role and points SXC_PG_DSN / SXC_PG_OWNER_DSN at the new database."""

    def __init__(self, name: str, variant: str = "clean", owner: str = "xc_owner", check: str = "xc_check"):
        self.name, self.variant, self.owner, self.check = name, variant, owner, check
        self.loaded = None
        self.data = None

    def setup(self) -> "PgFixture":
        pw = os.environ.get(PASSWORD_ENV)
        a = _admin()
        try:
            for role in (self.owner, self.check):
                if not a.run("SELECT 1 FROM pg_roles WHERE rolname = :r", r=role):
                    a.run(f'CREATE ROLE "{role}" LOGIN')
                if pw:
                    a.run(f"ALTER ROLE \"{role}\" PASSWORD '{pw.replace(chr(39), chr(39) * 2)}'")
            a.run(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
            a.run(f'CREATE DATABASE "{self.name}"')
        finally:
            a.close()
        a = _admin(self.name)
        try:
            a.run(f'REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            a.run(f'CREATE SCHEMA sxc_demo AUTHORIZATION "{self.owner}"')
            a.run(f'CREATE SCHEMA sxc_out AUTHORIZATION "{self.check}"')
            a.run(f'GRANT USAGE ON SCHEMA sxc_demo TO "{self.check}"')
            a.run(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{self.owner}" IN SCHEMA sxc_demo GRANT SELECT ON TABLES TO "{self.check}"')
        finally:
            a.close()
        os.environ["SXC_PG_OWNER_DSN"] = dsn_for(self.owner, self.name)
        os.environ["SXC_PG_DSN"] = dsn_for(self.check, self.name)
        self.data = demo.build(dirty=(self.variant == "dirty"))
        c = db.connect("SXC_PG_OWNER_DSN")
        try:
            with db.write_tx(c):
                self.loaded = demo.load(c, self.data)
        finally:
            c.close()
        return self

    def check_conn(self):
        return db.connect("SXC_PG_DSN")

    def teardown(self, drop: bool = True):
        if not drop:
            return
        a = _admin()
        try:
            a.run(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        finally:
            a.close()
