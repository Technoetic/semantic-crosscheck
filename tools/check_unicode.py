"""Reject invisible Unicode smuggling and Bidi control characters in repository or explicit files.

Guards against Trojan Source bidirectional overrides/isolates/marks (USENIX Security 2023;
Unicode UTS #39) and dangerous Default_Ignorable smuggling code points (soft hyphen, combining
grapheme joiner, zero-width controls, invisible fillers/math operators, mid-file BOM, Unicode
Tags, Variation Selectors Supplement) while allowing normal UTF-8 text (including Korean).
Findings report path, line, column, and code point only — never file contents.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".sxc", "__pycache__", "node_modules", ".venv", "venv", ".ruff_cache"}


def _build_forbidden_codepoints() -> frozenset[int]:
    cps: set[int] = set()
    # Trojan Source Bidi controls (USENIX Security '23 / Unicode UTS #39)
    cps.update({0x061C, 0x200E, 0x200F})
    cps.update(range(0x202A, 0x202F))  # LRE, RLE, PDF, LRO, RLO
    cps.update(range(0x2066, 0x206A))  # LRI, RLI, FSI, PDI
    # Dangerous Default_Ignorable / invisible smuggling controls
    cps.update({
        0x00AD,  # SOFT HYPHEN
        0x034F,  # COMBINING GRAPHEME JOINER
        0x115F,  # HANGUL CHOSEONG FILLER
        0x1160,  # HANGUL JUNGSEONG FILLER
        0x180E,  # MONGOLIAN VOWEL SEPARATOR
        0x200B,  # ZERO WIDTH SPACE
        0x200C,  # ZERO WIDTH NON-JOINER
        0x200D,  # ZERO WIDTH JOINER
        0x2060,  # WORD JOINER
        0x3164,  # HANGUL FILLER
        0xFFA0,  # HALFWIDTH HANGUL FILLER
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
    })
    cps.update(range(0x2061, 0x2065))  # INVISIBLE FUNCTION/TIMES/SEPARATOR/PLUS
    cps.update(range(0xE0000, 0xE0080))  # Unicode Tag block (ASCII smuggling)
    cps.update(range(0xE0100, 0xE01F0))  # Variation Selectors Supplement
    return frozenset(cps)


FORBIDDEN_CODEPOINTS: frozenset[int] = _build_forbidden_codepoints()


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


def scan_text(text: str) -> list[tuple[int, int, int, str]]:
    """Return (line, col, codepoint, unicode_name) for each forbidden character."""
    hits: list[tuple[int, int, int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for col_no, ch in enumerate(line, start=1):
            cp = ord(ch)
            if cp in FORBIDDEN_CODEPOINTS:
                name = unicodedata.name(ch, f"U+{cp:04X}")
                hits.append((line_no, col_no, cp, name))
    return hits


def scan_file(path: Path) -> list[tuple[int, int, int, str]]:
    raw = path.read_bytes()
    if b"\x00" in raw:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return scan_text(text)


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
            hits = scan_file(path)
            if not hits:
                continue
            try:
                rel = path.resolve().relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                rel = str(path)
            for line_no, col_no, cp, name in hits:
                flagged.append(f"{rel}:{line_no}:{col_no}: U+{cp:04X} ({name})")
    except (OSError, RuntimeError) as exc:
        print(f"check_unicode: {exc}", file=sys.stderr)
        return 2

    if flagged:
        for item in flagged:
            print(f"invisible unicode detected: {item}", file=sys.stderr)
        return 1
    print(f"check_unicode: {len(targets)} files checked, 0 forbidden invisible/Bidi code points.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
