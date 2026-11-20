"""Reject UTF-8 BOM (0xEF 0xBB 0xBF) in tracked repository or specified files."""

import argparse
import subprocess
import sys
from pathlib import Path


UTF8_BOM = b"\xef\xbb\xbf"
ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".sxc", "__pycache__", "node_modules", ".venv", "venv", ".ruff_cache"}


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed: {stderr}")
    paths: list[Path] = []
    for raw in result.stdout.split(b"\x00"):
        if not raw:
            continue
        candidate = root / raw.decode("utf-8")
        if candidate.is_file():
            paths.append(candidate)
    return paths


def walk_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            paths.append(path)
    return paths


def has_utf8_bom(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(3) == UTF8_BOM


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tracked",
        action="store_true",
        help="scan git-tracked files only (fails if not a git checkout)",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="explicit files to scan")
    args = parser.parse_args(argv)

    try:
        if args.paths:
            targets = list(args.paths)
            for path in targets:
                if not path.is_file():
                    raise FileNotFoundError(f"not a file: {path}")
        elif args.tracked:
            targets = tracked_files(ROOT)
        elif (ROOT / ".git").exists():
            targets = tracked_files(ROOT)
        else:
            targets = walk_files(ROOT)

        flagged: list[str] = []
        for path in targets:
            if has_utf8_bom(path):
                try:
                    rel = path.resolve().relative_to(ROOT.resolve()).as_posix()
                except ValueError:
                    rel = str(path)
                flagged.append(rel)
    except (OSError, RuntimeError) as exc:
        print(f"check_bom: {exc}", file=sys.stderr)
        return 2

    if flagged:
        for item in flagged:
            print(f"UTF-8 BOM detected: {item}", file=sys.stderr)
        return 1
    print(f"check_bom: {len(targets)} files checked, 0 UTF-8 BOMs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
