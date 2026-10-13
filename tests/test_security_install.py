"""cube-install: honest exit codes, PATH-only tools, a usable next step for --home, and early glibc errors.

- A --native-file that does not exist is exit 2 even when an install already exists (it used to report PASS).
- With --home outside the location the other stages look in, the result says they need SXC_CUBE_HOME (as the user
  wrote the path), and the console shows the absolute path.
- node and npm are looked up on PATH only: a node.cmd or npm.cmd in the current directory is never run.
- On Linux the glibc version is compared with what the pinned native module needs (2.34 on x64, 2.30 on arm64)
  before anything is installed; the smoke test's output goes to cube_install/smoke.log.
- Messages name the install directory by its role, never by its local path.
No npm, network or real node run is needed.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import config, cube_install, cube_run
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, StageError

try:
    KEY = cube_install.platform_key()
except StageError:
    KEY = None
PAYLOAD = b"\x7fELF stand-in native module\n"


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_sec_install_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SXC_CUBE_HOME", None)

    def run_stage(self, **args) -> tuple:
        ns = argparse.Namespace(**{"home": "", "force": False, "native_file": "", "quiet": False, **args})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res = cube_install.run(None, ns, self.tmp / "out")
        return res, err.getvalue()


def complete_install(home: Path) -> Path:
    """An install that installed_record() accepts: files, native module and a matching record."""
    (home / cube_run.SERVER_CORE).mkdir(parents=True)
    (home / cube_install.INDEX_NODE).parent.mkdir(parents=True)
    (home / cube_install.INDEX_NODE).write_bytes(PAYLOAD)
    shutil.copyfile(cube_run.ADAPTER_DIR / "server.js", home / "server.js")
    shutil.copyfile(cube_run.ADAPTER_DIR / "package.json", home / "package.json")
    asset = cube_install.pinned_asset(cube_install.load_pins(), KEY)
    record = {"cube_version": config.CUBE_VERSION, "platform": KEY, "files_sha256": cube_install.adapter_hashes(),
              "native": {"tarball_sha256": asset["sha256"], "index_node_sha256": hashlib.sha256(PAYLOAD).hexdigest()}}
    (home / cube_install.RECORD).write_text(json.dumps(record), encoding="utf-8")
    return home


@unittest.skipUnless(KEY, "no pinned native module for this platform")
class InstalledHomeTest(InstallCase):
    def test_missing_native_file_is_a_usage_error_even_when_installed(self):
        home = complete_install(self.tmp / "home")
        with self.assertRaises(StageError) as caught:
            self.run_stage(home=str(home), native_file=str(self.tmp / "downloads" / "native-typo.tar.gz"))
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertEqual(str(caught.exception), "--native-file native-typo.tar.gz: no such file")

    def test_home_outside_the_lookup_location_needs_sxc_cube_home(self):
        home = complete_install(self.tmp / "home")
        res, err = self.run_stage(home=str(home))
        self.assertEqual([(c.id, c.status) for c in res.checks], [("cube_install:already_installed", "pass")])
        self.assertEqual(res.next_safe_action, f"set SXC_CUBE_HOME={home} for every later stage (doctor, cube, verify), "
                                               "then run `sxc.py cube` (use --force to reinstall the adapter)")
        self.assertIn(f"SXC_CUBE_HOME={home.resolve()}", err)

    def test_relative_home_is_recorded_as_given(self):
        complete_install(self.tmp / "home")
        with contextlib.chdir(self.tmp):
            res, _err = self.run_stage(home="home")
        self.assertTrue(res.next_safe_action.startswith(
            "set SXC_CUBE_HOME=home (relative to the directory you run sxc.py from) for every later stage"))
        self.assertNotIn(self.tmp.name, json.dumps(res.to_dict()))

    def test_no_hint_when_the_other_stages_already_look_there(self):
        home = complete_install(self.tmp / "home")
        os.environ["SXC_CUBE_HOME"] = str(home)
        for given in ("", str(home)):
            with self.subTest(home=given or "(default)"):
                res, err = self.run_stage(home=given)
                self.assertEqual(res.next_safe_action, "run `sxc.py cube` (use --force to reinstall the adapter)")
                self.assertNotIn("SXC_CUBE_HOME=", err)


class GlibcTest(unittest.TestCase):
    def test_minimum_per_platform(self):
        cube_install.check_glibc("linux-x64-glibc", (2, 34))
        cube_install.check_glibc("linux-x64-glibc", (2, 39))
        cube_install.check_glibc("linux-arm64-glibc", (2, 30))
        cube_install.check_glibc("linux-x64-glibc", None)          # unknown: left to the smoke test
        cube_install.check_glibc("win32-x64-unknown", (1, 0))
        for key, have, need in (("linux-x64-glibc", (2, 33), "2.34"), ("linux-x64-glibc", (2, 28), "2.34"),
                                ("linux-arm64-glibc", (2, 29), "2.30")):
            with self.subTest(key=key, have=have):
                with self.assertRaises(StageError) as caught:
                    cube_install.check_glibc(key, have)
                self.assertEqual(caught.exception.exit_code, EXIT_ENV)
                self.assertIn(f"glibc {have[0]}.{have[1]} is too old", str(caught.exception))
                self.assertIn(f"needs glibc {need} or newer", str(caught.exception))

    def test_every_linux_pin_has_a_minimum(self):
        self.assertEqual(set(cube_install.GLIBC_MIN), {k for k in cube_install.PLATFORMS if k.startswith("linux-")})

    def test_version_detection(self):
        with mock.patch.object(cube_install.os, "confstr", create=True, return_value="glibc 2.31"):
            self.assertEqual(cube_install.glibc_version(), (2, 31))
        with mock.patch.object(cube_install.os, "confstr", create=True, side_effect=ValueError), \
                mock.patch.object(cube_install.platform, "libc_ver", return_value=("glibc", "2.35")):
            self.assertEqual(cube_install.glibc_version(), (2, 35))
        with mock.patch.object(cube_install.os, "confstr", create=True, return_value=None), \
                mock.patch.object(cube_install.platform, "libc_ver", return_value=("", "")):
            self.assertIsNone(cube_install.glibc_version())


class ToolsAndMessagesTest(InstallCase):
    def test_node_and_npm_come_from_path_only(self):
        for name in ("node.cmd", "npm.cmd", "node", "npm"):
            (self.tmp / name).write_text("planted\n", encoding="utf-8")
        os.environ["PATH"] = ""
        with contextlib.chdir(self.tmp):
            with self.assertRaises(StageError) as caught:
                cube_install._tools()
        self.assertEqual(caught.exception.exit_code, EXIT_ENV)
        self.assertEqual(str(caught.exception), f"Node.js >= {cube_install.MIN_NODE} with npm must be on PATH to install "
                                                "the Cube adapter")

    def test_minimum_node_is_22(self):
        self.assertEqual(cube_install.MIN_NODE, 22)
        pkg = json.loads((cube_run.ADAPTER_DIR / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["engines"], {"node": f">={cube_install.MIN_NODE}"})

    def test_home_messages_name_the_role_not_the_path(self):
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(StageError) as caught:
            cube_install.check_home(busy, "the --home directory")
        self.assertEqual(str(caught.exception),
                         "the --home directory is not empty and is not a Cube adapter install; choose an empty directory")
        with self.assertRaises(StageError) as caught:
            self.run_stage(home=str(busy), force=True)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertNotIn(self.tmp.name, str(caught.exception))

    def test_smoke_output_goes_to_the_log(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        if sys.platform == "win32":
            node = bin_dir / "node.cmd"
            node.write_text("@echo off\r\necho loader: version GLIBC_2.34 not found 1>&2\r\nexit /b 1\r\n", encoding="utf-8")
        else:
            node = bin_dir / "node"
            node.write_text("#!/bin/sh\necho 'loader: version GLIBC_2.34 not found' >&2\nexit 1\n", encoding="utf-8")
            node.chmod(0o755)
        log = self.tmp / "smoke.log"
        result = cube_install.smoke(str(node), self.tmp, log)
        self.assertEqual((result["ok"], result["exit_code"], result["build"]), (False, 1, None))
        self.assertIn("GLIBC_2.34 not found", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
