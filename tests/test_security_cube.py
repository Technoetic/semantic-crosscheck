"""Cube runtime safety: no proxies for the loopback adapter, a minimal child environment, current installs only.

- CubeClient used urllib's default opener, which applies HTTP_PROXY (or the system proxy) and does not bypass
  127.0.0.1 unless NO_PROXY lists it: the JWT and the query values went to the proxy. A local stand-in for the
  adapter must still be reached with a dead proxy configured.
- The Cube child gets no NODE_OPTIONS, no variable holding a postgres:// URL (the loader's DSN, a test admin DSN),
  no SXC_* variable it does not read, and PGOPTIONS with a statement timeout of cube.query_timeout_s, so abandoned
  queries stop in PostgreSQL. sslmode becomes CUBEJS_DB_SSL* settings (verification on for verify-ca/verify-full,
  where Cube's default would accept any certificate).
- cube_home() is absolute; an install counts only with the native module and a record of the current adapter files;
  messages say where the install was looked for, never the local path, and point at the server log for details.
- The cube stage's next action follows build's closure check instead of looping back to build, and does not send
  the agent into the server log.
No database, Cube install or network is needed (node is replaced by a stand-in where a process is started).
"""

import argparse
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, cube_run, stage_cube
from semantic_crosscheck.cube_client import CubeClient
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, Result, StageError, discard, require_stage

SECRET = "Hunter2TopSecret"
FROM_ENV = "value-of-the-pg-variable"     # interpolated: no literal password assignment in the source


def demo_cfg():
    return config.load(pgfixture.DEMO_CONFIG)


class EnvCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_sec_cube_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ---- the client -----------------------------------------------------------------------------------------
class _Adapter(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def _answer(self, body: dict):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Adapter.seen.append((self.command, self.path, bool(self.headers.get("Authorization"))))
        self._answer({"cubes": []} if self.path.endswith("/meta") else {"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        query = json.loads(self.rfile.read(length))["query"]
        _Adapter.seen.append((self.command, self.path, bool(self.headers.get("Authorization"))))
        self._answer({"data": [{"part.part_id": "x", "limit": query["limit"]}]})

    def log_message(self, *args):
        pass


def _dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ClientProxyTest(EnvCase):
    def setUp(self):
        super().setUp()
        _Adapter.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Adapter)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        dead = f"http://127.0.0.1:{_dead_port()}"
        for name in ("NO_PROXY", "no_proxy"):
            os.environ.pop(name, None)
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ[name] = dead

    def test_requests_reach_the_adapter_despite_a_proxy(self):
        with self.assertRaises(urllib.error.URLError, msg="the default opener uses the dead proxy"):
            urllib.request.urlopen(self.base + "/readyz", timeout=5)
        client = CubeClient(self.base, "s" * 64, timeout_s=10)
        self.assertTrue(client.ready())
        self.assertEqual(client.meta(), {"cubes": []})
        self.assertEqual(client.load({"dimensions": ["part.part_id"]}), [{"part.part_id": "x", "limit": 50000}])
        self.assertEqual([(m, p.rsplit("/", 1)[-1], auth) for m, p, auth in _Adapter.seen],
                         [("GET", "readyz", False), ("GET", "meta", True), ("POST", "load", True)])


# ---- the child environment -------------------------------------------------------------------------------
class ChildEnvTest(EnvCase):
    """Assertions compare variable names only: a failure message must never print the environment, which holds the
    secrets of whoever runs the tests."""

    def env_for(self, dsn: str = "postgresql://xc_check@127.0.0.1:5432/db", **cube) -> dict:
        cfg = demo_cfg()
        for k, v in cube.items():
            setattr(cfg.cube, k, v)
        os.environ[cfg.cube_dsn_env()] = dsn
        return cube_run.child_env(cfg, 0, "s" * 64)

    def assert_absent(self, env: dict, *names: str):
        self.assertEqual([n for n in names if n in env], [], "inherited by the Cube child")

    def test_secrets_and_debug_hooks_are_not_inherited(self):
        os.environ.update({
            "NODE_OPTIONS": "--inspect=0.0.0.0:9229",
            "MY_CUSTOM_OWNER_DSN": f"postgresql://loader:{SECRET}@127.0.0.1/db",
            "SXC_TEST_ADMIN_DSN": f"postgres://admin:{SECRET}@127.0.0.1/postgres",
            "APP_SETTINGS": f"url=POSTGRESQL://u:{SECRET}@h/db;mode=x",
            "SXC_PG_OWNER_DSN": "not-a-url-but-the-loader-variable",
            "SXC_CUBE_HOME": "somewhere", "SXC_JAVA": "java", "SXC_DEBUG": "1",
            "CUBEJS_DB_SSL_CA": "inherited", "PGPASSWORD": FROM_ENV, "PGHOST": "elsewhere",
            "PATH_LIKE_SETTING": "kept",
        })
        env = self.env_for()
        self.assertEqual([k for k, v in env.items() if SECRET in v], [], "a secret reached the Cube child")
        self.assert_absent(env, "NODE_OPTIONS", "MY_CUSTOM_OWNER_DSN", "SXC_TEST_ADMIN_DSN", "APP_SETTINGS",
                           "SXC_PG_OWNER_DSN", "SXC_PG_DSN", "SXC_CUBE_HOME", "SXC_JAVA", "SXC_DEBUG",
                           "CUBEJS_DB_SSL_CA", "PGPASSWORD", "PGHOST")
        self.assertEqual(env["PATH_LIKE_SETTING"], "kept")
        self.assertEqual(env["CUBEJS_DB_PASS"], FROM_ENV, "PGPASSWORD is a documented password source")
        self.assertEqual(sorted(k for k in env if k.upper().startswith("SXC_")), ["SXC_MODEL_DIR", "SXC_PORT"])

    def test_configured_dsn_variables_are_dropped_whatever_their_name(self):
        cfg = demo_cfg()
        cfg.source.owner_dsn_env = "LOADER_CONN"
        os.environ.update({"LOADER_CONN": "dbname=x password=y", cfg.cube_dsn_env(): "postgresql://u@h/db"})
        self.assert_absent(cube_run.child_env(cfg, 0, "s" * 64), "LOADER_CONN")

    def test_debug_flag_is_passed_only_when_set_to_1(self):
        self.assert_absent(self.env_for(), "SXC_CUBE_DEBUG")
        os.environ["SXC_CUBE_DEBUG"] = "yes"
        self.assert_absent(self.env_for(), "SXC_CUBE_DEBUG")
        os.environ["SXC_CUBE_DEBUG"] = "1"
        self.assertEqual(self.env_for()["SXC_CUBE_DEBUG"], "1")

    def test_sessions_are_read_only_with_the_query_timeout(self):
        env = self.env_for(query_timeout_s=45)
        self.assertEqual(env["PGOPTIONS"], "-c default_transaction_read_only=on -c statement_timeout=45000")
        self.assertEqual(env["CUBEJS_DB_QUERY_TIMEOUT"], "45", "Cube's driver sets statement_timeout from it per session")
        self.assertEqual(self.env_for()["PGOPTIONS"].rsplit("=", 1)[1], str(demo_cfg().cube.query_timeout_s * 1000))

    def test_sql_planner_is_fixed_to_tesseract_whatever_is_inherited(self):
        """Cube 1.8.3 reads CUBEJS_TESSERACT_SQL_PLANNER (default true); the runner sets it explicitly, so neither its
        implicit default nor an inherited `false` (the legacy planner) decides which planner serves a result."""
        env = self.env_for()
        self.assertEqual(env[cube_run.SQL_PLANNER_VAR], "true")
        self.assertEqual(cube_run.SQL_PLANNER_VAR, "CUBEJS_TESSERACT_SQL_PLANNER")
        self.assertEqual(cube_run.sql_planner(env), "tesseract")
        for name in ("CUBEJS_TESSERACT_SQL_PLANNER", "cubejs_tesseract_sql_planner"):
            with self.subTest(name=name):
                os.environ[name] = "false"
                env = self.env_for()
                self.assertEqual([k for k in env if k.upper() == cube_run.SQL_PLANNER_VAR], [cube_run.SQL_PLANNER_VAR])
                self.assertEqual(env[cube_run.SQL_PLANNER_VAR], "true", "an inherited legacy-planner switch reached Cube")
                os.environ.pop(name)

    def test_sql_planner_names(self):
        self.assertEqual(cube_run.sql_planner({cube_run.SQL_PLANNER_VAR: "true"}), "tesseract")
        self.assertEqual(cube_run.sql_planner({cube_run.SQL_PLANNER_VAR: "false"}), "legacy")
        for env in ({}, {cube_run.SQL_PLANNER_VAR: "yes"}, {cube_run.SQL_PLANNER_VAR: ""}):
            with self.subTest(env=env):
                self.assertEqual(cube_run.sql_planner(env), "unknown", "only the values the runner sets are named")
        self.assertEqual(cube_run.runtime_facts(config.CUBE_VERSION),
                         {"cube_version": config.CUBE_VERSION, "sql_planner": "tesseract"})

    def test_connection_and_password(self):
        env = self.env_for(f"postgresql://xc_check:{SECRET.replace('2', '%32')}@db.example.org:6543/ware")
        self.assertEqual((env["CUBEJS_DB_HOST"], env["CUBEJS_DB_PORT"], env["CUBEJS_DB_NAME"], env["CUBEJS_DB_USER"],
                          env["CUBEJS_DB_PASS"]), ("db.example.org", "6543", "ware", "xc_check", SECRET))

    def test_malformed_cube_dsn_names_the_variable(self):
        with self.assertRaises(StageError) as caught:
            self.env_for(f"postgresql://xc_check:a/{SECRET}@h/db")
        self.assertEqual(caught.exception.exit_code, EXIT_ENV)
        self.assertTrue(str(caught.exception).startswith("SXC_PG_DSN holds a malformed DSN"))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_sslmode_mapping(self):
        ca = str(self.tmp / "ca.pem")
        cases = {
            "": {"CUBEJS_DB_SSL": "false"},
            "?sslmode=disable": {"CUBEJS_DB_SSL": "false"},
            "?sslmode=prefer": {"CUBEJS_DB_SSL": "false"},
            "?sslmode=allow": {"CUBEJS_DB_SSL": "false"},
            "?sslmode=require": {"CUBEJS_DB_SSL": "true", "CUBEJS_DB_SSL_REJECT_UNAUTHORIZED": "false"},
            "?sslmode=verify-ca": {"CUBEJS_DB_SSL": "true", "CUBEJS_DB_SSL_REJECT_UNAUTHORIZED": "true"},
            "?sslmode=verify-full&sslrootcert=system": {"CUBEJS_DB_SSL": "true", "CUBEJS_DB_SSL_REJECT_UNAUTHORIZED": "true"},
            f"?sslmode=verify-full&sslrootcert={Path(ca).as_posix()}": {
                "CUBEJS_DB_SSL": "true", "CUBEJS_DB_SSL_REJECT_UNAUTHORIZED": "true", "CUBEJS_DB_SSL_CA": str(Path(ca).resolve())},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                env = self.env_for(f"postgresql://u@h/db{query}")
                self.assertEqual({k: v for k, v in env.items() if k.startswith("CUBEJS_DB_SSL")}, expected)


# ---- install location and state --------------------------------------------------------------------------
def fake_install(home: Path, files: dict | None = None, native: bool = True) -> Path:
    (home / cube_run.SERVER_CORE).mkdir(parents=True)
    (home / "server.js").write_text("// stand-in\n", encoding="utf-8")
    if native:
        (home / cube_run.INDEX_NODE).parent.mkdir(parents=True)
        (home / cube_run.INDEX_NODE).write_bytes(b"\x7fELF stand-in")
    record = {"files_sha256": cube_run.adapter_hashes() if files is None else files}
    (home / cube_run.RECORD).write_text(json.dumps(record), encoding="utf-8")
    return home


class InstallStateTest(EnvCase):
    def test_home_is_absolute(self):
        os.environ["SXC_CUBE_HOME"] = "relative-cube-home"
        home = cube_run.cube_home()
        self.assertTrue(home.is_absolute())
        self.assertEqual(home, (Path.cwd() / "relative-cube-home").resolve())
        os.environ.pop("SXC_CUBE_HOME")
        self.assertTrue(cube_run.cube_home().is_absolute())
        self.assertEqual(cube_run.cube_home().name, f"cube-{config.CUBE_VERSION}")
        self.assertEqual(cube_run.home_label(), "default per-user location")

    def test_states(self):
        self.assertEqual(cube_run.install_state(self.tmp / "nothing"), "missing")
        ok = fake_install(self.tmp / "ok")
        self.assertEqual(cube_run.install_state(ok), "ok")
        self.assertTrue(cube_run.installed(ok))
        no_native = fake_install(self.tmp / "no_native", native=False)
        self.assertEqual(cube_run.install_state(no_native), "incomplete")
        stale = fake_install(self.tmp / "stale", files={**cube_run.adapter_hashes(), "server.js": "0" * 64})
        self.assertEqual(cube_run.install_state(stale), "stale")
        self.assertFalse(cube_run.installed(stale))
        no_record = fake_install(self.tmp / "no_record")
        (no_record / cube_run.RECORD).unlink()
        self.assertEqual(cube_run.install_state(no_record), "incomplete")
        old = fake_install(self.tmp / "old_installer")
        (old / cube_run.RECORD).write_text("{}", encoding="utf-8")
        self.assertEqual(cube_run.install_state(old), "stale")


class ServeMessagesTest(EnvCase):
    def serve_error(self, home: Path, path_value: str | None = None) -> StageError:
        os.environ["SXC_CUBE_HOME"] = str(home)
        os.environ["SXC_PG_DSN"] = "postgresql://xc_check@127.0.0.1:5432/db"
        if path_value is not None:
            os.environ["PATH"] = path_value
        run_dir = self.tmp / "run"
        (run_dir / "model").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(StageError) as caught:
            with cube_run.serve(demo_cfg(), run_dir, run_dir / "server.log"):
                pass
        self.assertEqual(caught.exception.exit_code, EXIT_ENV)
        self.assertNotIn(str(self.tmp), str(caught.exception))
        self.assertNotIn(self.tmp.name, str(caught.exception))
        return caught.exception

    def test_missing_and_stale_installs_name_the_source_only(self):
        e = self.serve_error(self.tmp / "absent")
        self.assertEqual(str(e), "the Cube adapter is not installed (SXC_CUBE_HOME): run `sxc.py cube-install`")
        stale = fake_install(self.tmp / "stale", files={"server.js": "0" * 64})
        self.assertIn("rerun `sxc.py cube-install`", str(self.serve_error(stale)))
        self.assertIn("other adapter files", str(self.serve_error(stale)))

    def test_node_is_looked_up_on_path_only(self):
        home = fake_install(self.tmp / "home")
        planted = self.tmp / "cwd"
        planted.mkdir()
        (planted / ("node.cmd" if sys.platform == "win32" else "node")).write_text("planted\n", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(planted)
        try:
            e = self.serve_error(home, path_value="")
        finally:
            os.chdir(cwd)
        self.assertEqual(str(e), f"node (Node.js >= {cube_run.MIN_NODE}) is not on PATH")

    def test_startup_failure_quotes_only_adapter_lines_and_points_at_the_log(self):
        home = fake_install(self.tmp / "home")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        if sys.platform == "win32":
            (bin_dir / "node.cmd").write_text("@echo off\r\necho other output C:\\private\\path\r\n"
                                              "echo SXC_ERROR startup failed: Error: no model ^<v^>\r\nexit /b 1\r\n",
                                              encoding="utf-8")
        else:
            script = bin_dir / "node"
            script.write_text("#!/bin/sh\necho 'other output /home/private/path'\n"
                              "echo 'SXC_ERROR startup failed: Error: no model <v>'\nexit 1\n", encoding="utf-8")
            script.chmod(0o755)
        os.environ["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        e = self.serve_error(home, path_value=str(bin_dir) + os.pathsep + os.environ.get("SystemRoot", "/usr") +
                             (os.sep + "System32" if sys.platform == "win32" else os.sep + "bin"))
        self.assertEqual(str(e), "Cube adapter exited during startup (code 1): SXC_ERROR startup failed: Error: no "
                                 "model <v>; see run/server.log")
        self.assertIn("other output", (self.tmp / "run" / "server.log").read_text(encoding="utf-8"))

    def test_the_started_child_gets_tesseract_and_the_client_records_it(self):
        """A stand-in node prints the planner variable it was started with, reports ready and waits for stdin to close;
        an inherited CUBEJS_TESSERACT_SQL_PLANNER=false must not reach it, and the client records the planner the
        child was given next to the Cube version."""
        home = fake_install(self.tmp / "home")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        if sys.platform == "win32":
            (bin_dir / "node.cmd").write_text("@echo off\r\necho SXC_T_PLANNER [%CUBEJS_TESSERACT_SQL_PLANNER%]\r\n"
                                              "echo SXC_READY 1\r\nset /p SXC_T_WAIT=\r\nexit /b 0\r\n", encoding="utf-8")
            os.environ["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
            system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        else:
            script = bin_dir / "node"
            script.write_text('#!/bin/sh\necho "SXC_T_PLANNER [$CUBEJS_TESSERACT_SQL_PLANNER]"\necho "SXC_READY 1"\n'
                              "read SXC_T_WAIT\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            system = "/usr/bin" + os.pathsep + "/bin"
        os.environ.update({"SXC_CUBE_HOME": str(home), "SXC_PG_DSN": "postgresql://xc_check@127.0.0.1:5432/db",
                           "PATH": str(bin_dir) + os.pathsep + system, "CUBEJS_TESSERACT_SQL_PLANNER": "false"})
        run_dir = self.tmp / "run"
        (run_dir / "model").mkdir(parents=True)
        log = run_dir / "server.log"
        cfg = demo_cfg()
        with cube_run.serve(cfg, run_dir, log) as client:
            self.assertEqual(client.runtime, {"cube_version": cfg.cube.version, "sql_planner": "tesseract"})
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertIn("SXC_T_PLANNER [true]", [line.strip() for line in lines])


# ---- the cube stage's next action -------------------------------------------------------------------------
class NextActionTest(EnvCase):
    def action(self, build_checks, meta_ok: bool = True) -> str:
        cfg = demo_cfg()
        discard(self.tmp, "build")
        if build_checks is not None:
            build = Result("build")
            for status, detail in build_checks:
                build.add("closure:has_component", status, "hierarchies.has_component", **detail)
            build.write(self.tmp)
        res = Result("cube")
        res.add("cube:model", "pass")
        res.add("cube:gold_members:q01_explosion", "fail")
        res.add("cube:meta", "pass" if meta_ok else "error", "", http_status=500)
        cube_names = {"part", "bom_line", "sales_order", "stock"}             # no has_component_closure cube
        return stage_cube._next_action(res, cfg, cube_names, self.tmp)

    def test_closure_failures_point_at_the_hierarchy_not_back_at_build(self):
        cycle = self.action([("fail", {"reason": "cycle"})])
        self.assertIn("hierarchy has_component has cycles", cycle)
        self.assertIn('hierarchies.has_component.on_cycle = "report"', cycle)
        depth = self.action([("fail", {"reason": "depth_limit", "max_depth": 32})])
        self.assertIn("raise hierarchies.has_component.max_depth", depth)
        error = self.action([("error", {"sqlstate": "57014"})])
        self.assertIn("closure:has_component did not pass in build", error)
        self.assertEqual(self.action(None), "run `sxc.py build` so the closure tables exist, then rerun `sxc.py cube`")

    def test_meta_errors_do_not_send_the_agent_into_the_log(self):
        text = self.action(None, meta_ok=False)
        self.assertIn("compare cube/model with crosscheck.toml", text)
        self.assertIn("SXC_CUBE_DEBUG=1", text)
        self.assertNotIn("read the SXC_CUBE_ERROR lines", text)


class ResultPathsTest(EnvCase):
    def test_prerequisite_message_has_no_local_path(self):
        with self.assertRaises(StageError) as caught:
            require_stage(self.tmp, "cube")
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertEqual(str(caught.exception), "run `sxc.py cube` first (no cube/result.json in the output directory)")

    def test_result_is_written_atomically(self):
        res = Result("cube")
        res.add("x", "pass")
        path = res.write(self.tmp)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "pass")
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["result.json"])
        self.assertNotIn(b"\r\n", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
