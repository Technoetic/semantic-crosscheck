"""Build a timezone-independent release ZIP archive from a Git commit using SOURCE_DATE_EPOCH.

Replaces timezone-dependent `git archive --format=zip` timestamps with UTC
timestamps derived from `SOURCE_DATE_EPOCH` (or the target commit timestamp),
sorted POSIX paths, fixed Unix metadata (`create_system = 3`), and deflate
compression level 9 so archives reproduce identically across time zones.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MIN_ZIP_EPOCH = 315532800  # 1980-01-01T00:00:00Z (minimum DOS ZIP timestamp)
MAX_ZIP_EPOCH = 4354819199  # 2107-12-31T23:59:59Z (maximum DOS ZIP timestamp)
_VERSION_RE = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()


def commit_epoch(repo: Path, commit: str) -> int:
    raw = _git(repo, "log", "-1", "--format=%ct", commit).decode("ascii").strip()
    return int(raw)


def resolve_version(repo: Path, commit: str) -> str | None:
    rel = "skills/semantic-crosscheck/scripts/semantic_crosscheck/__init__.py"
    try:
        text = _git(repo, "show", f"{commit}:{rel}").decode("utf-8")
    except RuntimeError:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def resolve_epoch(repo: Path, commit: str, cli_epoch: int | None) -> int:
    if cli_epoch is not None:
        epoch = cli_epoch
    elif "SOURCE_DATE_EPOCH" in os.environ and os.environ["SOURCE_DATE_EPOCH"].strip() != "":
        raw = os.environ["SOURCE_DATE_EPOCH"].strip()
        try:
            epoch = int(raw)
        except ValueError as exc:
            raise ValueError(f"invalid SOURCE_DATE_EPOCH: {raw!r}") from exc
    else:
        epoch = commit_epoch(repo, commit)
    if epoch < MIN_ZIP_EPOCH:
        raise ValueError(f"SOURCE_DATE_EPOCH {epoch} is before 1980-01-01T00:00:00Z ({MIN_ZIP_EPOCH})")
    if epoch > MAX_ZIP_EPOCH:
        raise ValueError(f"SOURCE_DATE_EPOCH {epoch} exceeds 2107-12-31T23:59:59Z ({MAX_ZIP_EPOCH})")
    return epoch


def utc_zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    tm = time.gmtime(epoch)
    # DOS ZIP timestamps store 2-second granularity; round down deterministically.
    return (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min, (tm.tm_sec // 2) * 2)


def normalize_prefix(prefix: str) -> str:
    if prefix == "":
        return ""
    if "\\" in prefix or ":" in prefix or prefix.startswith("/"):
        raise ValueError(f"unsafe archive prefix: {prefix!r}")
    raw_segments = [seg for seg in prefix.rstrip("/").split("/")]
    if not raw_segments or any(seg in ("", ".", "..") for seg in raw_segments):
        raise ValueError(f"unsafe archive prefix: {prefix!r}")
    return prefix if prefix.endswith("/") else f"{prefix}/"


def validate_member_path(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise RuntimeError(f"unsafe archive member path: {name!r}")
    if ".." in PurePosixPath(name).parts:
        raise RuntimeError(f"unsafe archive member path: {name!r}")
    rel = name
    while rel.startswith("./"):
        rel = rel.removeprefix("./")
    if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
        raise RuntimeError(f"unsafe archive member path: {name!r}")
    return rel


def build_release_zip(
    repo: Path,
    ref: str,
    output: Path,
    prefix: str | None = None,
    source_date_epoch: int | None = None,
) -> dict[str, object]:
    commit = resolve_commit(repo, ref)
    epoch = resolve_epoch(repo, commit, source_date_epoch)
    if prefix is None:
        version = resolve_version(repo, commit)
        resolved_prefix = f"semantic-crosscheck-{version}/" if version else "semantic-crosscheck/"
    else:
        resolved_prefix = normalize_prefix(prefix)

    raw_tar = _git(repo, "archive", "--format=tar", commit)
    entries: list[tuple[str, int, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise RuntimeError(f"unsupported non-regular archive entry: {member.name!r}")
            rel = validate_member_path(member.name)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"cannot extract member bytes: {member.name!r}")
            data = extracted.read()
            mode = 0o100755 if (member.mode & 0o111) else 0o100644
            entries.append((rel, mode, data))

    entries.sort(key=lambda item: item[0].encode("utf-8"))
    dt = utc_zip_datetime(epoch)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, mode, data in entries:
            info = zipfile.ZipInfo(filename=f"{resolved_prefix}{rel}", date_time=dt)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix metadata, independent of build host OS
            info.external_attr = mode << 16
            zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "ref": ref,
        "commit": commit,
        "source_date_epoch": epoch,
        "utc_datetime": list(dt),
        "prefix": resolved_prefix,
        "files": len(entries),
        "bytes": output.stat().st_size,
        "sha256": digest,
        "output": str(output),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Git ref or commit to archive (default: HEAD)")
    parser.add_argument(
        "--prefix",
        default=None,
        help="Archive directory prefix (default: semantic-crosscheck-<VERSION>/)",
    )
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=None,
        help="Override SOURCE_DATE_EPOCH (seconds since 1970-01-01T00:00:00Z; default: env or commit epoch)",
    )
    parser.add_argument("--repo", type=Path, default=ROOT, help="Git repository root")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output ZIP file path")
    parser.add_argument("--json", action="store_true", help="Print archive metadata as JSON")
    args = parser.parse_args(argv)

    try:
        summary = build_release_zip(
            repo=args.repo,
            ref=args.ref,
            output=args.output,
            prefix=args.prefix,
            source_date_epoch=args.source_date_epoch,
        )
    except (OSError, OverflowError, RuntimeError, ValueError) as exc:
        print(f"build_release_zip: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            f"build_release_zip: {summary['files']} files, {summary['bytes']} bytes, "
            f"SOURCE_DATE_EPOCH={summary['source_date_epoch']}, sha256={summary['sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
