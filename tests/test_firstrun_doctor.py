"""doctor: hints that work from any directory, PATH-only program lookup, and the full no-write check.

Unit tests (no database): install hints name the requirements files and sxc.py by absolute path, node and java
planted in the current directory are never run, the Node.js minimum comes from cube_install, an adapter installed
from other adapter files is not ready, and the missing-table hint. Integration tests (skipped unless
SXC_TEST_ADMIN_DSN names a superuser connection) change one privilege at a time in a throw-away database named
sxc_f_first_doctor and restore it afterwards.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import config, cube_install, cube_run, doctor

DB_NAME = "sxc_f_first_doctor"


def must_not_run(argv, **_kw):
    raise AssertionError(f"must not run {argv}")


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_firstrun_doctor_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class InstallHints(TempDirTest):
    def test_pip_hints_use_path_tokens_that_the_console_expands_to_absolute_paths(self):
        for name in ("core", "reason"):
            path = doctor.PLUGIN_ROOT / "requirements" / f"{name}.txt"
            self.assertTrue(path.is_absolute() and path.is_file(), path)
            hint = doctor.pip_hint(name)
            self.assertEqual(hint, f'<python> -m pip install -r "<plugin>/requirements/{name}.txt"')
            shown = sxc.expand(hint)
            self.assertNotIn("<plugin>", shown)
            self.assertTrue(Path(shown.split('-r "', 1)[1].rstrip('"')).resolve() == path.resolve(), shown)
        self.assertEqual(doctor.probe_module("sxc_no_such_module")[2], doctor.pip_hint("reason"))

    def test_a_missing_requirements_file_is_an_incomplete_installation(self):
        with mock.patch.object(doctor, "PLUGIN_ROOT", self.tmp):
            self.assertIn("installation is incomplete", doctor.pip_hint("core"))

    def test_missing_pg8000_points_at_core_requirements(self):
        stub = lambda *_a, **_k: (True, {}, "")   # noqa: E731
        with mock.patch.object(doctor, "_have_pg8000", return_value=False), \
                mock.patch.multiple(doctor, probe_module=stub, probe_java=stub, probe_node=stub, probe_adapter=stub,
                                    probe_native=stub), \
                mock.patch.dict(os.environ, {}), contextlib.chdir(self.tmp):
            os.environ.pop("SXC_PG_DSN", None)
            res = doctor.run(None, argparse.Namespace(require=["core"]), self.tmp)
        self.assertEqual(next(c.status for c in res.checks if c.id == "pg8000"), "fail")
        self.assertEqual(res.next_safe_action, doctor.pip_hint("core"))

    def test_cube_hints_name_sxc_through_the_plugin_token(self):
        ok, detail, hint = doctor.probe_adapter(self.tmp / "empty-home")
        self.assertEqual((ok, detail["reason"]), (False, "missing"))
        token = '"<plugin>/skills/semantic-crosscheck/scripts/sxc.py" cube-install'
        self.assertIn(token, hint)
        self.assertIn(token, doctor.probe_native(self.tmp / "empty-home")[2])
        script = sxc.expand(token).split('"')[1]
        self.assertTrue(Path(script).is_file() and Path(script).resolve() == Path(sxc.__file__).resolve(), script)


class ProgramLookup(TempDirTest):
    """node or java planted in the working directory must not be found, let alone run."""

    def plant(self, directory: Path, name: str):
        for suffix in ("", ".bat", ".cmd", ".exe"):
            p = directory / (name + suffix)
            p.write_text("planted\n", encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR)

    def test_programs_in_the_current_directory_are_ignored(self):
        work, empty_path = self.tmp / "project", self.tmp / "empty-path"
        work.mkdir()
        empty_path.mkdir()
        self.plant(work, "node")
        self.plant(work, "java")
        with contextlib.chdir(work), mock.patch.dict(os.environ, {"PATH": str(empty_path)}):
            os.environ.pop("SXC_JAVA", None)
            node = doctor.probe_node(runner=must_not_run)
            java = doctor.probe_java(runner=must_not_run)
            relative = doctor.probe_java(env={"SXC_JAVA": os.path.join(".", "java")}, runner=must_not_run)
        self.assertEqual(node[:2], (False, {"found": False}))
        self.assertEqual(java[:2], (False, {"found": False, "source": "PATH"}))
        self.assertEqual(relative[:2], (False, {"found": False, "source": "SXC_JAVA"}))

    def test_the_node_minimum_comes_from_cube_install(self):
        self.assertFalse(hasattr(doctor, "MIN_NODE"))
        with mock.patch.object(cube_install, "MIN_NODE", 99):
            ok, _detail, hint = doctor.probe_node(which=lambda _n: "node",
                                                  runner=lambda *_a, **_k: mock.Mock(returncode=0, stdout="v98.1.0\n"))
        self.assertFalse(ok)
        self.assertIn("Node.js 99", hint)


class AdapterFreshness(TempDirTest):
    def install(self, files_sha256: dict) -> Path:
        home = self.tmp / "cube-home"
        (home / "node_modules" / "@cubejs-backend" / "server-core").mkdir(parents=True)
        index = home / cube_install.INDEX_NODE
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b"synthetic native module")
        (home / "server.js").write_text("// installed adapter\n", encoding="utf-8")
        (home / cube_install.RECORD).write_text(json.dumps({"files_sha256": files_sha256}), encoding="utf-8")
        return home

    def test_an_install_of_the_current_adapter_files_is_ready(self):
        home = self.install(cube_install.adapter_hashes())
        ok, detail, hint = doctor.probe_adapter(home)
        self.assertEqual((ok, detail["reason"], hint), (True, "current", ""))

    def test_an_install_of_other_adapter_files_is_not_ready(self):
        home = self.install(cube_install.adapter_hashes())
        changed = self.tmp / "adapter"
        shutil.copytree(cube_run.ADAPTER_DIR, changed)
        with open(changed / "server.js", "a", encoding="utf-8", newline="\n") as f:
            f.write("// one more line after an upgrade\n")
        ok, detail, hint = doctor.probe_adapter(home, adapter_dir=changed)
        self.assertEqual((ok, detail["reason"], detail["changed"]), (False, "adapter_files_changed", "server.js"))
        self.assertIn("cube-install", hint)
        self.assertEqual(doctor.changed_adapter_files(home, changed), ["server.js"])

    def test_an_install_without_a_record_is_not_ready(self):
        home = self.install({})
        (home / cube_install.RECORD).unlink()
        ok, detail, _hint = doctor.probe_adapter(home)
        self.assertEqual((ok, detail["reason"]), (False, "no_install_record"))


class PrivilegeFindings(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(pgfixture.DEMO_CONFIG)

    def row(self, **changes) -> dict:
        base = {"table_exists": True, "schema_exists": True, "can_use": True, "can_select": True, "can_insert": False,
                "can_update": False, "can_delete": False, "can_truncate": False, "can_trigger": False,
                "member_of_owner": False, "schema_owner": False, "schema_create": False}
        return {**base, **changes}

    def test_every_way_to_change_a_source_is_reported(self):
        self.assertEqual(doctor.write_findings(self.row()),
                         {"held": "", "member_of_owner": False, "schema_owner": False, "schema_create": False})
        self.assertEqual(doctor.write_findings(self.row(can_trigger=True, can_insert=True))["held"], "INSERT,TRIGGER")
        self.assertTrue(doctor.write_findings(self.row(schema_owner=True))["schema_owner"])
        self.assertTrue(doctor.write_findings(self.row(schema_create=True))["schema_create"])

    def test_missing_tables_get_a_load_hint_not_a_grant(self):
        missing = self.row(table_exists=False)
        self.assertIn("run `sxc.py demo` first", doctor.select_hint(self.cfg, "stock", missing))
        self.cfg.entities["stock"].table = "sxc_demo.stock_history"
        self.assertIn("check entities.stock.table", doctor.select_hint(self.cfg, "stock", missing))
        self.cfg.entities["stock"].table = "sxc_demo.stock"
        self.assertIn("grant the check role USAGE", doctor.select_hint(self.cfg, "stock", self.row(can_select=False)))


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class NoWriteIntegration(TempDirTest):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(DB_NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)

    def setUp(self):
        super().setUp()
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)

    def admin(self, *statements: str):
        a = pgfixture._admin(self.fx.name)
        try:
            for s in statements:
                a.run(s)
        finally:
            a.close()

    def change(self, apply: str, *restore: str):
        self.admin(apply)
        self.addCleanup(self.admin, *restore)

    def run_doctor(self) -> tuple[int, dict, dict]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(["doctor", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(self.tmp)])
        saved = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        return code, {c["id"]: c for c in saved["checks"]}, saved

    def test_the_prepared_check_role_passes(self):
        code, checks, saved = self.run_doctor()
        self.assertEqual(code, 0, saved)
        self.assertEqual(checks["privileges:temporary"]["status"], "pass")

    def test_owning_the_source_schema_fails(self):
        # moving the owner back merges the check role's grant into the owner's entry, so USAGE is granted again
        self.change(f'ALTER SCHEMA sxc_demo OWNER TO "{self.fx.check}"', f'ALTER SCHEMA sxc_demo OWNER TO "{self.fx.owner}"',
                    f'GRANT USAGE ON SCHEMA sxc_demo TO "{self.fx.check}"')
        code, checks, saved = self.run_doctor()
        self.assertEqual(code, 1)
        for e in self.cfg.entities:
            check = checks[f"privileges:no_write:{e}"]
            self.assertEqual((check["status"], check["detail"]["schema_owner"], check["detail"]["held"]), ("fail", True, ""))
        self.assertIn("make another role own the tables and the schema", saved["next_safe_action"])

    def test_create_on_the_source_schema_fails(self):
        self.change(f'GRANT CREATE ON SCHEMA sxc_demo TO "{self.fx.check}"',
                    f'REVOKE CREATE ON SCHEMA sxc_demo FROM "{self.fx.check}"')
        _code, checks, _saved = self.run_doctor()
        detail = checks["privileges:no_write:part"]["detail"]
        self.assertEqual((checks["privileges:no_write:part"]["status"], detail["schema_create"], detail["schema_owner"]),
                         ("fail", True, False))

    def test_trigger_on_a_source_table_fails(self):
        self.change(f'GRANT TRIGGER ON sxc_demo.part TO "{self.fx.check}"',
                    f'REVOKE TRIGGER ON sxc_demo.part FROM "{self.fx.check}"')
        _code, checks, _saved = self.run_doctor()
        self.assertEqual((checks["privileges:no_write:part"]["status"], checks["privileges:no_write:part"]["detail"]["held"]),
                         ("fail", "TRIGGER"))
        self.assertEqual(checks["privileges:no_write:stock"]["status"], "pass")

    def test_without_temporary_privilege_doctor_fails(self):
        self.change(f'REVOKE TEMPORARY ON DATABASE "{self.fx.name}" FROM PUBLIC',
                    f'GRANT TEMPORARY ON DATABASE "{self.fx.name}" TO PUBLIC')
        code, checks, saved = self.run_doctor()
        self.assertEqual((code, checks["privileges:temporary"]["status"]), (1, "fail"))
        self.assertIn("GRANT TEMPORARY ON DATABASE", saved["next_safe_action"])

    def test_a_missing_demo_table_asks_for_the_demo_stage(self):
        self.change("ALTER TABLE sxc_demo.stock RENAME TO stock_parked", "ALTER TABLE sxc_demo.stock_parked RENAME TO stock")
        _code, checks, saved = self.run_doctor()
        self.assertEqual((checks["privileges:select:stock"]["status"], checks["privileges:no_write:stock"]["status"]),
                         ("fail", "not_run"))
        self.assertIn("for the demo, run `sxc.py demo` first", saved["next_safe_action"])
        self.assertNotIn("grant", saved["next_safe_action"])


if __name__ == "__main__":
    unittest.main()
