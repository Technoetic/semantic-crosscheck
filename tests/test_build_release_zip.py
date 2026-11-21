"""Tests for tools/build_release_zip.py deterministic SOURCE_DATE_EPOCH release archives."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import pgfixture

from semantic_crosscheck import VERSION

ROOT = pgfixture.ROOT
SCRIPT = ROOT / "tools" / "build_release_zip.py"


def _ensure_repo(tmp_dir: Path) -> tuple[Path, str, bool]:
    """Use the live repository when .git exists; otherwise build a tiny synthetic repo."""
    if (ROOT / ".git").exists():
        return ROOT, f"semantic-crosscheck-{VERSION}/", True
    repo = tmp_dir / "synthetic_repo"
    repo.mkdir()
    pkg = repo / "skills" / "semantic-crosscheck" / "scripts" / "semantic_crosscheck"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f'VERSION = "{VERSION}"\n', encoding="utf-8")
    dot_dir = repo / ".claude-plugin"
    dot_dir.mkdir()
    (dot_dir / "plugin.json").write_text('{"name": "semantic-crosscheck"}\n', encoding="utf-8")
    (repo / "README.md").write_text("# Synthetic\n", encoding="utf-8")
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="noreply@github.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="noreply@github.com",
        GIT_AUTHOR_DATE="1795220420 +0000",
        GIT_COMMITTER_DATE="1795220420 +0000",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, env=env, check=True)
    return repo, f"semantic-crosscheck-{VERSION}/", False


class BuildReleaseZipTests(unittest.TestCase):
    def run_builder(
        self,
        repo: Path,
        *args: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("SOURCE_DATE_EPOCH", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_archive_is_byte_identical_across_timezones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, expected_prefix, _is_live = _ensure_repo(Path(tmp))
            hashes: dict[str, str] = {}
            sizes: dict[str, int] = {}
            meta_by_tz: dict[str, dict[str, object]] = {}
            for label, tz in (("kst", "KST-9"), ("utc", "UTC0"), ("est", "EST5")):
                out = Path(tmp) / f"sxc-{label}.zip"
                proc = self.run_builder(repo, "--ref", "HEAD", "-o", str(out), "--json", extra_env={"TZ": tz})
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                meta = json.loads(proc.stdout)
                meta_by_tz[label] = meta
                hashes[label] = hashlib.sha256(out.read_bytes()).hexdigest()
                sizes[label] = out.stat().st_size
                self.assertEqual(meta["sha256"], hashes[label])
                self.assertEqual(meta["prefix"], expected_prefix)

            self.assertEqual(hashes["kst"], hashes["utc"])
            self.assertEqual(hashes["utc"], hashes["est"])
            self.assertEqual(sizes["kst"], sizes["utc"])
            self.assertEqual(meta_by_tz["kst"]["utc_datetime"], meta_by_tz["utc"]["utc_datetime"])

    def test_archive_contents_preserve_dotfiles_modes_and_git_tree_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, prefix, is_live = _ensure_repo(Path(tmp))
            out = Path(tmp) / "release.zip"
            fixed_epoch = 1795220420  # 2026-11-21T00:20:20Z (even second)
            proc = self.run_builder(
                repo,
                "--ref",
                "HEAD",
                "-o",
                str(out),
                "--json",
                extra_env={"TZ": "KST-9", "SOURCE_DATE_EPOCH": str(fixed_epoch)},
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            meta = json.loads(proc.stdout)
            self.assertEqual(meta["source_date_epoch"], fixed_epoch)

            raw_tar = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", "HEAD"])
            expected: dict[str, tuple[int, bytes]] = {}
            with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as tf:
                for member in tf.getmembers():
                    if member.isfile():
                        extracted = tf.extractfile(member)
                        assert extracted is not None
                        rel_name = member.name.removeprefix("./")
                        mode = 0o100755 if (member.mode & 0o111) else 0o100644
                        expected[rel_name] = (mode, extracted.read())

            with zipfile.ZipFile(out, "r") as zf:
                self.assertIsNone(zf.testzip())
                infos = zf.infolist()
                self.assertEqual(len(infos), len(expected))
                names = [info.filename for info in infos]
                self.assertEqual(names, sorted(names, key=lambda n: n.encode("utf-8")))
                # Dotfiles must retain their leading dot.
                self.assertIn(f"{prefix}.claude-plugin/plugin.json", names)
                if is_live:
                    for required_dot in (
                        ".agents/plugins/marketplace.json",
                        ".claude-plugin/marketplace.json",
                        ".codex-plugin/plugin.json",
                        ".github/workflows/tests.yml",
                        ".githooks/pre-commit",
                        ".sqruff",
                    ):
                        self.assertIn(f"{prefix}{required_dot}", names)
                for info in infos:
                    self.assertTrue(info.filename.startswith(prefix))
                    rel = info.filename[len(prefix) :]
                    want_mode, want_bytes = expected[rel]
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                    self.assertEqual(info.create_system, 3)
                    self.assertEqual(info.external_attr, want_mode << 16)
                    self.assertEqual(info.date_time, (2026, 11, 21, 0, 20, 20))
                    self.assertEqual(zf.read(info.filename), want_bytes)

    def test_out_of_range_source_date_epoch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _prefix, _is_live = _ensure_repo(Path(tmp))
            for bad_epoch in ("1000", "999999999999999"):
                with self.subTest(epoch=bad_epoch):
                    out = Path(tmp) / f"bad-{bad_epoch}.zip"
                    proc = self.run_builder(repo, "--ref", "HEAD", "-o", str(out), "--source-date-epoch", bad_epoch)
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
