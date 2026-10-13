"""Unit tests for the Cube stage helpers and the adapter installer: no database, no network, no npm.

They pin the security-relevant behaviour of `cube-install` (platform mapping, hash check before extraction,
strict tar member policy, committed pins that agree with each other) and the member bookkeeping of the
`cube` stage (visibility of primary keys, members named by gold queries, /meta comparison).
"""

import argparse
import hashlib
import io
import json
import random
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import config, cube_install, cube_run, stage_cube
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, Result, StageError

ADAPTER = cube_run.ADAPTER_DIR
PAYLOAD = b"\x7fELF synthetic native module for tests\n" * 64
try:
    KEY = cube_install.platform_key()
except StageError:
    KEY = None


def make_tarball(path: Path, entries) -> Path:
    """entries: (name, kind, payload) with kind file | dir | symlink | hardlink (payload = bytes or link target)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, payload in entries:
            ti = tarfile.TarInfo(name)
            ti.mode = 0o755
            if kind == "file":
                ti.size = len(payload)
                tar.addfile(ti, io.BytesIO(payload))
                continue
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
            else:
                ti.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                ti.linkname = payload
            tar.addfile(ti)
    return path


def asset_for(path: Path, sha256: str | None = None, size: int | None = None) -> dict:
    data = path.read_bytes()
    return {"url": "https://example.invalid/native.tar.gz", "sha256": sha256 or hashlib.sha256(data).hexdigest(),
            "size": len(data) if size is None else size}


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_cube_unit_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class PlatformKeyTest(unittest.TestCase):
    def test_supported_platforms(self):
        cases = [
            (("win32", "AMD64", ""), "win32-x64-unknown"),
            (("win32", "x86_64", ""), "win32-x64-unknown"),
            (("linux", "x86_64", "glibc"), "linux-x64-glibc"),
            (("linux", "aarch64", "glibc"), "linux-arm64-glibc"),
            (("linux", "arm64", "glibc"), "linux-arm64-glibc"),
            (("darwin", "x86_64", ""), "darwin-x64-unknown"),
            (("darwin", "arm64", ""), "darwin-arm64-unknown"),
        ]
        for args, key in cases:
            with self.subTest(args=args):
                self.assertEqual(cube_install.platform_key(*args), key)

    def test_unsupported_platforms_are_environment_errors(self):
        cases = [("linux", "x86_64", "musl"), ("linux", "x86_64", ""), ("linux", "riscv64", "glibc"),
                 ("win32", "ARM64", ""), ("win32", "x86", ""), ("freebsd14", "amd64", ""), ("cygwin", "x86_64", ""),
                 ("darwin", "", "")]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(StageError) as caught:
                    cube_install.platform_key(*args)
                self.assertEqual(caught.exception.exit_code, EXIT_ENV)
                self.assertIn("unsupported platform", str(caught.exception))

    def test_platform_list_matches_the_pins(self):
        self.assertEqual(set(cube_install.PLATFORMS), set(cube_install.load_pins()["assets"]))


class PinsTest(unittest.TestCase):
    def setUp(self):
        self.pkg = json.loads((ADAPTER / "package.json").read_text(encoding="utf-8"))
        self.lock = json.loads((ADAPTER / "package-lock.json").read_text(encoding="utf-8"))
        self.pins = cube_install.load_pins()

    def test_native_pins_cover_the_five_platforms(self):
        self.assertEqual(self.pins["cube_version"], config.CUBE_VERSION)
        self.assertEqual(self.pins["member"], cube_install.MEMBER)
        self.assertEqual(sorted(self.pins["assets"]), sorted(["win32-x64-unknown", "linux-x64-glibc", "linux-arm64-glibc",
                                                              "darwin-x64-unknown", "darwin-arm64-unknown"]))
        for key, a in self.pins["assets"].items():
            with self.subTest(key=key):
                self.assertEqual(a["url"], f"https://github.com/cube-js/cube/releases/download/v{config.CUBE_VERSION}/"
                                           f"native-{key}-fallback.tar.gz")
                self.assertRegex(a["sha256"], r"^[0-9a-f]{64}$")
                self.assertIsInstance(a["size"], int)
                self.assertGreater(a["size"], 1_000_000)
                self.assertEqual(cube_install.pinned_asset(self.pins, key), a)
        self.assertEqual(len({a["sha256"] for a in self.pins["assets"].values()}), 5)

    def test_index_node_goes_where_the_package_loader_looks(self):
        # @cubejs-backend/native loads <package>/index.node or <package>/native/index.node; the tarball holds native/index.node
        self.assertEqual(cube_install.INDEX_NODE.as_posix(), "node_modules/@cubejs-backend/native/native/index.node")
        self.assertEqual(cube_install.MEMBER, self.pins["member"])

    def test_damaged_pins_are_refused(self):
        key = "linux-x64-glibc"
        damage = [("url", "http://github.com/cube-js/cube/releases/download/v1.8.3/x.tar.gz"),
                  ("url", "https://example.org/cube-js/cube/releases/download/v1.8.3/x.tar.gz"),
                  ("sha256", "abc"), ("sha256", "F" * 64), ("size", True), ("size", 0), ("size", "22213579")]
        for field, value in damage:
            with self.subTest(field=field, value=value):
                pins = json.loads(json.dumps(self.pins))
                pins["assets"][key][field] = value
                with self.assertRaises(StageError) as caught:
                    cube_install.pinned_asset(pins, key)
                self.assertEqual(caught.exception.exit_code, EXIT_ENV)
        with self.assertRaises(StageError):
            cube_install.pinned_asset({**self.pins, "cube_version": "0.0.1"}, key)

    def test_package_json_pins_exact_versions(self):
        self.assertIs(self.pkg["private"], True)
        self.assertEqual(self.pkg["engines"], {"node": f">={cube_install.MIN_NODE}"})
        self.assertEqual(cube_install.MIN_NODE, 22, "packages in the locked tree declare engines.node >= 22")
        deps = self.pkg["dependencies"]
        self.assertEqual(set(deps), {"@cubejs-backend/server-core", "@cubejs-backend/postgres-driver", "express"})
        for name, version in deps.items():
            with self.subTest(name=name):
                self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(deps["@cubejs-backend/server-core"], config.CUBE_VERSION)
        self.assertEqual(deps["@cubejs-backend/postgres-driver"], config.CUBE_VERSION)
        self.assertTrue(deps["express"].startswith("4."))
        self.assertFalse(self.pkg.get("scripts"))

    def test_lockfile_agrees_with_package_json(self):
        self.assertEqual(self.lock["lockfileVersion"], 3)
        self.assertEqual(self.lock["name"], self.pkg["name"])
        root = self.lock["packages"][""]
        self.assertEqual(root["dependencies"], self.pkg["dependencies"])
        for name, version in self.pkg["dependencies"].items():
            with self.subTest(name=name):
                self.assertEqual(self.lock["packages"][f"node_modules/{name}"]["version"], version)
        self.assertEqual(self.lock["packages"]["node_modules/@cubejs-backend/native"]["version"], config.CUBE_VERSION)
        for path, entry in self.lock["packages"].items():
            if not path or entry.get("link"):
                continue
            with self.subTest(package=path):
                self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"))
                self.assertTrue(entry["integrity"].startswith("sha512-"))


class TarballTest(TempDirCase):
    def test_extracts_only_index_node(self):
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native", "dir", None), ("native/index.node", "file", PAYLOAD)])
        dest = self.tmp / "pkg" / "native" / "index.node"
        info = cube_install.extract_index_node(tb, dest)
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertEqual(info, {"members": 2, "bytes": len(PAYLOAD), "index_node_sha256": hashlib.sha256(PAYLOAD).hexdigest()})
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["index.node"])

    def test_directory_entry_is_optional(self):
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native/index.node", "file", PAYLOAD)])
        dest = self.tmp / "out" / "index.node"
        self.assertEqual(cube_install.extract_index_node(tb, dest)["members"], 1)

    def test_rejected_layouts_write_nothing(self):
        ok_file = ("native/index.node", "file", PAYLOAD)
        cases = {
            "extra_member": ([("native", "dir", None), ok_file, ("native/README", "file", b"x")], "unexpected_member"),
            "top_level_file": ([ok_file, ("index.node", "file", PAYLOAD)], "unexpected_member"),
            "dot_prefix": ([("./native/index.node", "file", PAYLOAD)], "unexpected_member"),
            "native_as_file": ([("native", "file", b"x"), ok_file], "unexpected_member"),
            "symlink": ([("native/index.node", "symlink", "/etc/passwd")], "link"),
            "hardlink": ([("native/index.node", "hardlink", "native/other")], "link"),
            "absolute": ([("/native/index.node", "file", PAYLOAD)], "absolute_path"),
            "drive": ([("C:/native/index.node", "file", PAYLOAD)], "absolute_path"),
            "parent": ([("../native/index.node", "file", PAYLOAD)], "parent_path"),
            "inner_parent": ([ok_file, ("native/../../evil.node", "file", PAYLOAD)], "parent_path"),
            "backslash_parent": ([ok_file, ("native\\..\\evil.node", "file", PAYLOAD)], "parent_path"),
            "missing": ([("native", "dir", None)], "missing_member"),
            "duplicate": ([ok_file, ok_file], "duplicate_member"),
        }
        for label, (entries, reason) in cases.items():
            with self.subTest(case=label):
                tb = make_tarball(self.tmp / f"{label}.tar.gz", entries)
                dest = self.tmp / label / "index.node"
                with self.assertRaises(cube_install.NativeArchiveError) as caught:
                    cube_install.extract_index_node(tb, dest)
                self.assertEqual(str(caught.exception), reason)
                self.assertFalse(dest.parent.exists())

    def test_hash_and_size_verification(self):
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native/index.node", "file", PAYLOAD)])
        good = asset_for(tb)
        v = cube_install.verify_tarball(tb, good["sha256"], good["size"])
        self.assertEqual((v["ok"], v["size_ok"], v["sha256_ok"], v["sha256"]), (True, True, True, good["sha256"]))
        v = cube_install.verify_tarball(tb, "0" * 64, good["size"])
        self.assertEqual((v["ok"], v["size_ok"], v["sha256_ok"]), (False, True, False))
        v = cube_install.verify_tarball(tb, good["sha256"], good["size"] + 1)
        self.assertEqual((v["ok"], v["size_ok"], v["sha256_ok"], v["sha256"]), (False, False, False, None))


class InstallNativeTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / cube_install.NATIVE_DIR).mkdir(parents=True)
        self.index = self.home / cube_install.INDEX_NODE

    def _run(self, tarball: Path, asset: dict):
        res = Result("cube_install")
        native = cube_install.install_native(res, self.home, asset, str(tarball))
        return res, native, {c.id: c for c in res.checks}

    def test_verified_tarball_is_extracted(self):
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native", "dir", None), ("native/index.node", "file", PAYLOAD)])
        res, native, checks = self._run(tb, asset_for(tb))
        self.assertEqual([c.status for c in res.checks], ["pass", "pass"])
        self.assertEqual(self.index.read_bytes(), PAYLOAD)
        self.assertEqual(native["index_node_sha256"], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(native["tarball_sha256"], asset_for(tb)["sha256"])
        self.assertEqual(checks["cube_install:native_sha256"].detail["source"], "file")
        self.assertTrue(tb.is_file(), "a --native-file given by the user is never deleted")

    def test_hash_mismatch_stops_before_extraction(self):
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native/index.node", "file", PAYLOAD)])
        res, native, checks = self._run(tb, asset_for(tb, sha256="1" * 64))
        self.assertIsNone(native)
        self.assertEqual(checks["cube_install:native_sha256"].status, "fail")
        self.assertFalse(checks["cube_install:native_sha256"].detail["sha256_ok"])
        self.assertNotIn("cube_install:extract", checks)
        self.assertFalse(self.index.exists())

    def test_size_mismatch_stops_before_extraction(self):
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native/index.node", "file", PAYLOAD)])
        res, native, checks = self._run(tb, asset_for(tb, size=tb.stat().st_size - 1))
        self.assertIsNone(native)
        self.assertFalse(checks["cube_install:native_sha256"].detail["size_ok"])
        self.assertFalse(self.index.exists())

    def test_bad_layout_is_refused_even_when_the_hash_matches(self):
        tb = make_tarball(self.tmp / "evil.tar.gz", [("native/index.node", "file", PAYLOAD), ("native/run.sh", "file", b"x")])
        res, native, checks = self._run(tb, asset_for(tb))
        self.assertIsNone(native)
        self.assertEqual(checks["cube_install:native_sha256"].status, "pass")
        self.assertEqual((checks["cube_install:extract"].status, checks["cube_install:extract"].detail["reason"]),
                         ("fail", "unexpected_member"))
        self.assertFalse(self.index.exists())

    def test_corrupt_archive_with_matching_hash_is_refused(self):
        noise = random.Random(7).randbytes(200_000)
        whole = make_tarball(self.tmp / "whole.tar.gz", [("native/index.node", "file", noise)]).read_bytes()
        truncated = self.tmp / "truncated.tar.gz"
        truncated.write_bytes(whole[: len(whole) // 2])
        garbage = self.tmp / "garbage.tar.gz"
        garbage.write_bytes(b"not a gzip stream" * 10)
        for tb in (truncated, garbage):
            with self.subTest(tarball=tb.name):
                _res, native, checks = self._run(tb, asset_for(tb))
                self.assertIsNone(native)
                self.assertEqual(checks["cube_install:native_sha256"].status, "pass")
                self.assertEqual((checks["cube_install:extract"].status, checks["cube_install:extract"].detail["reason"]),
                                 ("fail", "bad_archive"))
                self.assertFalse(self.index.exists())
                leftovers = list(self.index.parent.glob("*.part")) if self.index.parent.exists() else []
                self.assertEqual(leftovers, [], "a partly written index.node is removed")

    def test_missing_native_package_fails_extract(self):
        shutil.rmtree(self.home / cube_install.NATIVE_DIR)
        tb = make_tarball(self.tmp / "ok.tar.gz", [("native/index.node", "file", PAYLOAD)])
        _res, native, checks = self._run(tb, asset_for(tb))
        self.assertIsNone(native)
        self.assertEqual(checks["cube_install:extract"].detail["reason"], "native_package_missing")


@unittest.skipUnless(KEY, "no pinned native module for this platform")
class InstalledRecordTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        (self.home / "node_modules" / "@cubejs-backend" / "server-core").mkdir(parents=True)
        (self.home / "server.js").write_bytes((ADAPTER / "server.js").read_bytes())
        index = self.home / cube_install.INDEX_NODE
        index.parent.mkdir(parents=True)
        index.write_bytes(PAYLOAD)
        self.asset = cube_install.pinned_asset(cube_install.load_pins(), KEY)
        self.files = cube_install.adapter_hashes()
        self.record = {"cube_version": config.CUBE_VERSION, "platform": KEY, "files_sha256": self.files,
                       "native": {"tarball_sha256": self.asset["sha256"], "index_node_sha256": hashlib.sha256(PAYLOAD).hexdigest()}}
        self._write(self.record)

    def _write(self, record):
        (self.home / cube_install.RECORD).write_text(json.dumps(record), encoding="utf-8")

    def test_matching_install_is_skipped_without_node_or_network(self):
        args = argparse.Namespace(home=str(self.home), force=False, native_file="", quiet=True)
        res = cube_install.run(None, args, self.tmp / "out")
        self.assertEqual([(c.id, c.status) for c in res.checks], [("cube_install:already_installed", "pass")])
        self.assertEqual(res.checks[0].detail["platform"], KEY)

    def test_changes_invalidate_the_record(self):
        self.assertIsNotNone(cube_install.installed_record(self.home, KEY, self.asset, self.files))
        self.assertIsNone(cube_install.installed_record(self.home, KEY, self.asset, {**self.files, "server.js": "0" * 64}))
        self.assertIsNone(cube_install.installed_record(self.home, KEY, {**self.asset, "sha256": "2" * 64}, self.files))
        other = next(k for k in cube_install.PLATFORMS if k != KEY)
        self.assertIsNone(cube_install.installed_record(self.home, other, self.asset, self.files))
        (self.home / cube_install.INDEX_NODE).write_bytes(PAYLOAD + b"tampered")
        self.assertIsNone(cube_install.installed_record(self.home, KEY, self.asset, self.files))

    def test_incomplete_install_is_not_installed(self):
        (self.home / "server.js").unlink()
        self.assertIsNone(cube_install.installed_record(self.home, KEY, self.asset, self.files))
        (self.home / "server.js").write_text("x", encoding="utf-8")
        (self.home / cube_install.RECORD).write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(cube_install.installed_record(self.home, KEY, self.asset, self.files))


class HomeCheckTest(TempDirCase):
    def test_empty_missing_and_own_homes_are_accepted(self):
        cube_install.check_home(self.tmp / "missing")
        (self.tmp / "empty").mkdir()
        cube_install.check_home(self.tmp / "empty")
        own = self.tmp / "own"
        own.mkdir()
        shutil.copyfile(ADAPTER / "package.json", own / "package.json")
        (own / "node_modules").mkdir()
        cube_install.check_home(own)

    def test_foreign_directories_are_refused(self):
        other = self.tmp / "project"
        other.mkdir()
        (other / "package.json").write_text('{"name": "some-project"}', encoding="utf-8")
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "notes.txt").write_text("x", encoding="utf-8")
        broken = self.tmp / "broken"
        broken.mkdir()
        (broken / "package.json").write_text("[]", encoding="utf-8")
        for home in (other, busy, broken):
            with self.subTest(home=home.name):
                with self.assertRaises(StageError) as caught:
                    cube_install.check_home(home)
                self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)


MODEL = {"cubes": [
    {"name": "part", "dimensions": [
        {"name": "part_id", "sql": "{CUBE}.part_id", "type": "string", "primary_key": True, "public": True},
        {"name": "part_class", "sql": "{CUBE}.part_class", "type": "string"},
        {"name": "secret_col", "sql": "{CUBE}.secret_col", "type": "string", "public": False}],
     "measures": [{"name": "count", "type": "count"}]},
    {"name": "stock", "dimensions": [
        {"name": "sxc_pk", "sql": "x", "type": "string", "primary_key": True},
        {"name": "qty", "sql": "{CUBE}.qty", "type": "number"}],
     "measures": [{"name": "count", "type": "count"}, {"name": "hidden_total", "sql": "{CUBE}.qty", "type": "sum", "public": False}]},
    {"name": "hidden_cube", "public": False, "dimensions": [{"name": "a", "sql": "a", "type": "string"}]},
]}


class StageCubeHelpersTest(unittest.TestCase):
    def test_public_members_hide_primary_keys_without_public_flag(self):
        self.assertEqual(stage_cube.public_members(MODEL),
                         {"part.part_id", "part.part_class", "part.count", "stock.qty", "stock.count"})

    def test_query_members_cover_every_query_section(self):
        q = {"dimensions": ["a.x"], "measures": ["a.m"], "segments": ["a.s"],
             "timeDimensions": [{"dimension": "b.ts", "dateRange": [":from", ":to"]}],
             "filters": [{"member": "c.f", "operator": "equals", "values": [":v"]},
                         {"or": [{"dimension": "d.g", "operator": "set"}, {"and": [{"member": "e.h", "operator": "set"}]}]},
                         {"operator": "equals", "values": ["x"]}],
             "order": {"a.x": "asc"}}
        members, malformed = stage_cube.query_members(q)
        self.assertEqual(members, {"a.x", "a.m", "a.s", "b.ts", "c.f", "d.g", "e.h"})
        self.assertEqual(malformed, 1)
        members, malformed = stage_cube.query_members({"order": [["f.o", "desc"]], "timeDimensions": ["bad"]})
        self.assertEqual((members, malformed), ({"f.o"}, 1))

    def _gold(self, query: dict, mapping: dict):
        return config.Gold(id="g", sql="g.sql", key=list(mapping)[:1], cube_query=query, map=mapping)

    def test_gold_check(self):
        everything = {"part.part_id", "part.part_class", "part.count", "part.secret_col", "stock.sxc_pk", "stock.qty"}
        public = stage_cube.public_members(MODEL)
        ok = self._gold({"dimensions": ["part.part_id"], "measures": ["part.count"],
                         "filters": [{"member": "part.part_class", "operator": "equals", "values": [":c"]}]},
                        {"part_id": "part.part_id", "n": "part.count"})
        self.assertEqual(stage_cube.gold_check(ok, everything, public), ("pass", {"members": 3, "missing": 0, "hidden": 0, "malformed": 0}))
        bad = self._gold({"dimensions": ["part.part_id", "stock.sxc_pk"], "measures": ["part.nope"],
                          "filters": [{"member": "part.secret_col", "operator": "set"}]},
                         {"part_id": "part.part_id", "pk": "stock.sxc_pk", "n": "part.nope"})
        status, detail = stage_cube.gold_check(bad, everything, public)
        self.assertEqual(status, "fail")
        self.assertEqual((detail["missing"], detail["hidden"]), (1, 2))
        self.assertEqual(detail["missing_members"], ["part.nope"])
        self.assertEqual(detail["hidden_members"], ["part.secret_col", "stock.sxc_pk"])

    def test_meta_check(self):
        public = stage_cube.public_members(MODEL)
        listed = {"cubes": [
            {"name": "part", "dimensions": [{"name": "part.part_id"}, {"name": "part.part_class"}],
             "measures": [{"name": "part.count"}]},
            {"name": "stock", "dimensions": [{"name": "stock.qty"}, {"name": "stock.sxc_pk", "isVisible": False}],
             "measures": [{"name": "stock.count"}]}]}
        self.assertEqual(stage_cube.meta_check(public, listed),
                         ("pass", {"model_public": 5, "meta_members": 5, "missing": 0, "extra": 0}))
        listed["cubes"][1]["dimensions"] = [{"name": "stock.sxc_pk"}]
        status, detail = stage_cube.meta_check(public, listed)
        self.assertEqual(status, "fail")
        self.assertEqual((detail["missing"], detail["extra"], detail["missing_members"], detail["extra_members"]),
                         (1, 1, ["stock.qty"], ["stock.sxc_pk"]))

    def test_error_helpers_keep_status_and_config_ref_only(self):
        self.assertEqual(stage_cube.http_status(Exception('HTTP 500: {"error": "value P-1"}')), 500)
        self.assertIsNone(stage_cube.http_status(Exception("timed out waiting for Cube")))
        msg = stage_cube.config_message(ValueError("entities.part: key column 'x' not found in sxc_demo.part"))
        self.assertEqual(msg, "crosscheck.toml [entities.part]: key column 'x' not found in sxc_demo.part (Cube model generation)")
        self.assertTrue(stage_cube.config_message(ValueError("odd")).startswith("crosscheck.toml: "))


if __name__ == "__main__":
    unittest.main()
