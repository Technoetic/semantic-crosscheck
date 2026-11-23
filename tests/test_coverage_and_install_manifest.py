"""Unit tests for Cube install manifest_sha256 integrity, near_misses diagnostics, and doctor --coverage."""

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401
import sxc
from semantic_crosscheck import config, coverage_matrix, cube_install, cube_run, doctor

PAYLOAD = b"\x7fELF synthetic native module\n"


def _make_home(root: Path, *, sealed: bool = True, platform_key: str = "linux-x64-glibc") -> Path:
    home = root / "cube-home"
    (home / cube_run.SERVER_CORE).mkdir(parents=True, exist_ok=True)
    idx = home / cube_install.INDEX_NODE
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_bytes(PAYLOAD)
    shutil.copyfile(cube_run.ADAPTER_DIR / "server.js", home / "server.js")
    shutil.copyfile(cube_run.ADAPTER_DIR / "package.json", home / "package.json")
    pinned = cube_install.pinned_asset(cube_install.load_pins(), platform_key)["sha256"]
    rec = {
        "cube_version": config.CUBE_VERSION,
        "platform": platform_key,
        "files_sha256": cube_install.adapter_hashes(),
        "native": {
            "tarball_sha256": pinned,
            "index_node_sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        },
    }
    if sealed:
        rec = cube_run.seal_install_record(rec)
    (home / cube_install.RECORD).write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    return home


class InstallManifestIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_manifest_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_sealed_manifest_verifies_and_unsealed_legacy_stays_compatible(self):
        sealed_home = _make_home(self.tmp / "sealed", sealed=True)
        rec, integrity = cube_run.load_install_record(sealed_home)
        self.assertIsNotNone(rec)
        self.assertEqual(integrity, "verified")
        self.assertEqual(cube_run.install_state(sealed_home), "ok")

        legacy_home = _make_home(self.tmp / "legacy", sealed=False)
        rec_l, integrity_l = cube_run.load_install_record(legacy_home)
        self.assertIsNotNone(rec_l)
        self.assertEqual(integrity_l, "unsealed_legacy")
        self.assertEqual(cube_run.install_state(legacy_home), "ok")
        self.assertTrue(cube_run.installed(legacy_home))

    def test_tampered_manifest_sha256_fails_closed_across_cube_run_install_and_doctor(self):
        home = _make_home(self.tmp / "tampered", sealed=True)
        rec = json.loads((home / cube_install.RECORD).read_text(encoding="utf-8"))
        rec["cube_version"] = "9.9.9"  # modifies body without re-sealing
        (home / cube_install.RECORD).write_text(json.dumps(rec), encoding="utf-8")

        loaded, integrity = cube_run.load_install_record(home)
        self.assertIsNone(loaded)
        self.assertEqual(integrity, "manifest_digest_mismatch")
        self.assertEqual(cube_run.install_state(home), "corrupt")
        self.assertFalse(cube_run.installed(home))

        asset = cube_install.pinned_asset(cube_install.load_pins(), "linux-x64-glibc")
        self.assertIsNone(cube_install.installed_record(home, "linux-x64-glibc", asset, cube_install.adapter_hashes()))

        ok_a, det_a, hint_a = doctor.probe_adapter(home)
        self.assertFalse(ok_a)
        self.assertEqual(det_a["reason"], "manifest_digest_mismatch")
        self.assertIn("cube-install --force", hint_a)

        ok_n, det_n, hint_n = doctor.probe_native(home)
        self.assertFalse(ok_n)
        self.assertEqual(det_n["reason"], "manifest_digest_mismatch")
        self.assertIn("cube-install --force", hint_n)

    def test_duplicate_keys_nan_non_dict_and_oversize_records_rejected(self):
        home = _make_home(self.tmp / "bad", sealed=True)
        for bad_raw in (
            '{"platform": "linux-x64-glibc", "platform": "win32-x64-unknown"}',
            '{"platform": NaN}',
            '["not", "an", "object"]',
            '{"manifest_sha256": null}',
            '{"manifest_sha256": "not-hex"}',
            ' ' * (cube_run.MAX_RECORD_BYTES + 8),
        ):
            with self.subTest(bad_raw=bad_raw[:40]):
                (home / cube_install.RECORD).write_text(bad_raw, encoding="utf-8")
                rec, integrity = cube_run.load_install_record(home)
                self.assertIsNone(rec)
                self.assertIn(integrity, ("invalid_json", "manifest_digest_mismatch"))
                self.assertFalse(cube_run.installed(home))

    def test_diagnose_install_reports_structured_near_misses_without_paths(self):
        missing = self.tmp / "empty-dir"
        missing.mkdir()
        diag_empty = cube_run.diagnose_install(missing)
        self.assertEqual(diag_empty["state"], "missing")
        self.assertEqual(diag_empty["manifest_integrity"], "missing")
        self.assertIn("missing_server_js", diag_empty["near_misses"])
        self.assertIn("missing_install_record", diag_empty["near_misses"])

        home = _make_home(self.tmp / "drift", sealed=True, platform_key="linux-x64-glibc")
        (home / cube_install.INDEX_NODE).write_bytes(b"corrupted native")
        diag = cube_run.diagnose_install(
            home,
            platform_key="win32-x64-unknown",
            pinned_tarball_sha256="0" * 64,
        )
        self.assertEqual(diag["manifest_integrity"], "verified")
        self.assertIn("platform_mismatch", diag["near_misses"])
        self.assertIn("tarball_pin_mismatch", diag["near_misses"])
        self.assertIn("index_node_sha256_mismatch", diag["near_misses"])
        serialized = json.dumps(diag)
        self.assertNotIn(self.tmp.name, serialized)


class CoverageMatrixAndDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_cov_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_platform_matrix_covers_all_pinned_and_unpinned_targets(self):
        for plat in cube_install.PLATFORMS:
            self.assertIn(plat, coverage_matrix.PLATFORM_MATRIX)
        self.assertIn("linux-musl-or-unpinned", coverage_matrix.PLATFORM_MATRIX)

        lin64 = coverage_matrix.build_coverage_report(
            self.tmp / "h", system="linux", machine="x86_64", libc="glibc",
        )
        self.assertEqual(lin64["schema"], coverage_matrix.SCHEMA)
        self.assertEqual(lin64["host"]["platform_key"], "linux-x64-glibc")
        self.assertTrue(lin64["host"]["cube_native_supported"])
        self.assertEqual(lin64["host_capabilities"]["pg17_integration"], "ci_integrated")
        self.assertEqual(lin64["host_capabilities"]["cube_e2e_integration"], "ci_integrated")

        win64 = coverage_matrix.build_coverage_report(
            self.tmp / "h", system="win32", machine="AMD64",
        )
        self.assertEqual(win64["host"]["platform_key"], "win32-x64-unknown")
        self.assertEqual(win64["host_capabilities"]["ci_unit"], "ci_unit_only")
        self.assertEqual(win64["host_capabilities"]["cube_e2e_integration"], "not_measured")

        mac_arm = coverage_matrix.build_coverage_report(
            self.tmp / "h", system="darwin", machine="arm64",
        )
        self.assertEqual(mac_arm["host"]["platform_key"], "darwin-arm64-unknown")
        self.assertEqual(mac_arm["host_capabilities"]["cube_native_pin"], "supported")
        self.assertEqual(mac_arm["host_capabilities"]["cube_e2e_integration"], "not_measured")

        musl = coverage_matrix.build_coverage_report(
            self.tmp / "h", system="linux", machine="x86_64", libc="musl",
        )
        self.assertFalse(musl["host"]["cube_native_supported"])
        self.assertEqual(musl["host_capabilities"]["cube_native_pin"], "unsupported")
        self.assertEqual(musl["host_capabilities"]["cube_e2e_integration"], "unsupported")
        self.assertNotIn(self.tmp.name, json.dumps(musl))
        self.assertNotIn(self.tmp.name, coverage_matrix.format_coverage_summary(musl))

    def test_doctor_coverage_flag_attaches_matrix_without_altering_default_or_exit_code(self):
        stub = lambda *_a, **_k: (True, {}, "")  # noqa: E731
        with mock.patch.multiple(
            doctor,
            _have_pg8000=lambda: True,
            probe_module=stub,
            probe_java=stub,
            probe_node=stub,
            probe_adapter=stub,
            probe_native=stub,
        ), mock.patch.dict(os.environ, {"SXC_CUBE_HOME": str(self.tmp / "cube-home")}):
            os.environ.pop("SXC_PG_DSN", None)
            default_res = doctor.run(None, argparse.Namespace(require=["core"], coverage=False), self.tmp / "out1")
            cov_res = doctor.run(None, argparse.Namespace(require=["core"], coverage=True), self.tmp / "out2")

        default_dict = default_res.to_dict()
        cov_dict = cov_res.to_dict()
        self.assertNotIn("coverage", default_dict)
        self.assertIn("coverage", cov_dict)
        self.assertEqual(default_res.status, cov_res.status)
        self.assertEqual(default_res.exit_code(), cov_res.exit_code())
        self.assertEqual([c.id for c in default_res.checks], [c.id for c in cov_res.checks])
        self.assertEqual([c.status for c in default_res.checks], [c.status for c in cov_res.checks])
        self.assertNotIn(self.tmp.name, json.dumps(cov_dict["coverage"]))

    def test_sxc_cli_doctor_coverage_outputs_summary_and_writes_result(self):
        out_dir = self.tmp / "sxc-out"
        buf = io.StringIO()
        stub = lambda *_a, **_k: (True, {}, "")  # noqa: E731
        with mock.patch.multiple(
            doctor,
            _have_pg8000=lambda: True,
            probe_module=stub,
            probe_java=stub,
            probe_node=stub,
            probe_adapter=stub,
            probe_native=stub,
        ), mock.patch.dict(os.environ, {"SXC_CUBE_HOME": str(self.tmp / "cube-home")}):
            os.environ.pop("SXC_PG_DSN", None)
            with contextlib.redirect_stdout(buf):
                rc = sxc.main(["doctor", "--coverage", "--out", str(out_dir)])
        self.assertEqual(rc, 1)  # SXC_PG_DSN missing
        text = buf.getvalue()
        self.assertIn("COVERAGE", text)
        self.assertIn("INSTALL", text)
        saved = json.loads((out_dir / "doctor" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["coverage"]["schema"], coverage_matrix.SCHEMA)
        self.assertNotIn(self.tmp.name, json.dumps(saved["coverage"]))


if __name__ == "__main__":
    unittest.main()
