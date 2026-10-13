#!/usr/bin/env python3
"""Leak scanner for a public repository: secrets, personal e-mail addresses and private deny-listed terms.

Anything committed to a public repository is published for good, history included. This scanner runs before a
commit (.githooks/pre-commit: --staged), before a push (.githooks/pre-push: --history) and in CI (--history, plus
--extra for a pull request body or downloaded logs; fetch the full history first). Run it from the repository:

    python tools/leak_scan.py                  working tree: tracked files and untracked files git does not ignore
    python tools/leak_scan.py --staged         only the staged blobs
    python tools/leak_scan.py --history        every blob reachable from any ref, plus commit and tag metadata
    python tools/leak_scan.py --extra FILE     additional files (repeatable); add --worktree to scan both

Each line (and each path name) is checked for:
  - generic secrets: private key blocks; GitHub, AWS, Slack, npm, Google, Anthropic and Stripe tokens; URLs with
    user:password@; password assignments with a literal value; high-entropy api_key/secret/token assignments;
  - e-mail addresses outside the allowlist: noreply@anthropic.com, addresses at example.org and example.com, the
    repository's configured user.email and every --allow-email (an address, or @domain for a whole domain); the
    user part of a URL (scheme://user@host) is not an address;
  - deny-listed terms from the file named by SXC_DENYLIST: one literal term per line, matched as a
    case-insensitive substring, lines starting with # are comments. The deny-list never lives in the repository:
    a deny-list inside the working tree that git does not ignore is refused.
Binary content is decoded as UTF-8 with errors ignored. One narrow exception: e-mail addresses on the `deprecated`
lines of npm lockfiles are registry messages of third-party packages and are not flagged (secrets and the deny-list
still apply to those lines).

Output is a location and a pattern id per finding, never the matching text, so the scan cannot copy a token into a
CI log. A deny-list hit is reported as denylist#<line number in the deny-list file>, and a deny-listed term inside
a printed location is replaced by that marker too. History locations are `blob <id> (<path>)`, `commit <id>` and
`tag <id>`; their line numbers are those of `git cat-file -p <id>`.

Exit codes: 0 clean, 1 findings, 2 usage error (bad arguments, not a git repository, unusable deny-list).
Standard library only, Python 3.8 or newer: hooks run with whatever Python is on PATH.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

DEFAULT_ALLOWED_EMAILS = ("noreply@anthropic.com",)
DEFAULT_ALLOWED_DOMAINS = ("example.org", "example.com")
DENYLIST_ENV = "SXC_DENYLIST"
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".h",
                 ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala"}
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json"}
CHUNK_OBJECTS, CHUNK_BYTES = 500, 64 << 20

TOKEN_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,250}")),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9_/-]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("stripe_key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
)
URL_CREDENTIALS = re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://"
                             r"(?P<user>[^\s/:@'\"<>{}\[\]`]{1,256}):(?P<pw>[^\s/@'\"<>{}\[\]`]{1,256})@")
PASSWORD_ASSIGN = re.compile(r"""(?ix)
    (?<![a-z0-9_.-])
    (?P<key>[a-z0-9_.-]{0,40}?(?:password|passwd|passphrase))
    ["']?\s*[:=]\s*
    (?P<val>"[^"\n]*"|'[^'\n]*'|[^\s"',;)}\]#]+)
""")
SECRET_ASSIGN = re.compile(r"""(?ix)
    (?<![a-z0-9_.-])
    (?P<key>[a-z0-9_.-]{0,40}?(?:api[_-]?key|secret|token|access[_-]?key|private[_-]?key))
    ["']?\s*[:=]\s*["']?
    (?P<val>[a-z0-9_+/=.-]{20,512})
""")
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}(?![A-Za-z0-9-])")
PLACEHOLDERS = {"password", "passwd", "pass", "pwd", "pw", "secret", "token", "changeme", "redacted", "placeholder",
                "example", "dummy", "none", "null", "nil", "true", "false", "str", "bytes", "string", "optional",
                "required", "empty"}
_LINE_START = re.compile(r"\s*(?:export\s+|set\s+)?[\"']?")
_ATTRIBUTE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_DEPRECATED = re.compile(r'\s*"deprecated"\s*:')
_URL_AUTHORITY = re.compile(r"://(?:[^\s/@:]{1,256}:)?$")


class UsageError(Exception):
    pass


def entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def is_placeholder(value: str) -> bool:
    low = value.strip().lower()
    if not low or low in PLACEHOLDERS or re.fullmatch(r"[x*._#-]+", low):
        return True
    return low.startswith(("$", "<", "{", "[", "%s", "%(")) or "${" in low or "{{" in low


def file_kind(path: str) -> str:
    """'code', 'lockfile' or 'text': code never counts unquoted `password=x` (that passes a variable), and the
    `deprecated` messages of npm lockfiles are third-party registry text, not repository content."""
    p = Path(path)
    if p.name.lower() in LOCKFILES:
        return "lockfile"
    return "code" if p.suffix.lower() in CODE_SUFFIXES else "text"


def password_hit(line: str, code: bool) -> bool:
    """A literal password: quoted anywhere, or unquoted at the start of a config-style line or in a URL query
    (never unquoted in code, where `password=pw` passes a variable)."""
    for m in PASSWORD_ASSIGN.finditer(line):
        val = m.group("val")
        if val[0] in "\"'":
            if len(val) - 2 >= 4 and not is_placeholder(val[1:-1]):
                return True
            continue
        if code:
            continue
        start = m.start()
        if not (_LINE_START.fullmatch(line[:start]) or (start and line[start - 1] in "?&")):
            continue
        if len(val) >= 4 and not is_placeholder(val) and not re.search(r"[()\[\]{}<>]", val) and not _ATTRIBUTE.fullmatch(val):
            return True
    return False


def secret_hit(line: str) -> bool:
    for m in SECRET_ASSIGN.finditer(line):
        val = m.group("val")
        if (not is_placeholder(val) and re.search(r"[0-9]", val) and re.search(r"[A-Za-z]", val)
                and entropy(val) >= 3.5):
            return True
    return False


class Rules:
    """Pattern set for one run: built-in patterns, the e-mail allowlist and the deny-list [(line number, term)]."""

    def __init__(self, deny=(), allow=()):
        self.deny = [(n, t.casefold()) for n, t in deny]
        self.emails = {e.lower() for e in DEFAULT_ALLOWED_EMAILS}
        self.domains = set(DEFAULT_ALLOWED_DOMAINS)
        for a in allow:
            a = a.strip().lower()
            if a.startswith("@"):
                self.domains.add(a[1:])
            elif a:
                self.emails.add(a)
        self._deny_rx = [(n, re.compile(re.escape(t), re.IGNORECASE)) for n, t in self.deny]

    def email_allowed(self, address: str) -> bool:
        a = address.lower()
        domain = a.rsplit("@", 1)[1]
        return a in self.emails or any(domain == d or domain.endswith("." + d) for d in self.domains)

    def _email_hit(self, line: str) -> bool:
        """An address outside the allowlist; `user@host` right after `scheme://` is URL userinfo, not an address."""
        for m in EMAIL.finditer(line):
            if not self.email_allowed(m.group()) and not _URL_AUTHORITY.search(line, max(0, m.start() - 300), m.start()):
                return True
        return False

    def line_hits(self, line: str, kind: str = "text") -> list[str]:
        hits = [pid for pid, rx in TOKEN_PATTERNS if rx.search(line)]
        if "://" in line and any(not is_placeholder(m.group("pw")) for m in URL_CREDENTIALS.finditer(line)):
            hits.append("url_credentials")
        if password_hit(line, kind == "code"):
            hits.append("password_assignment")
        if secret_hit(line):
            hits.append("secret_assignment")
        registry_text = kind == "lockfile" and _DEPRECATED.match(line)
        if "@" in line and not registry_text and self._email_hit(line):
            hits.append("email")
        if self.deny:
            low = line.casefold()
            hits += [f"denylist#{n}" for n, term in self.deny if term in low]
        return hits

    def scan_text(self, text: str, path: str = "") -> list[tuple[int, str]]:
        kind = file_kind(path)
        return [(n, pid) for n, line in enumerate(text.split("\n"), 1) for pid in self.line_hits(line.rstrip("\r"), kind)]

    def safe(self, location: str) -> str:
        """The location with every deny-listed term replaced by its marker (whole location if folding differs)."""
        for n, rx in self._deny_rx:
            location = rx.sub(f"<denylist#{n}>", location)
        hidden = [n for n, term in self.deny if term in location.casefold()]
        return f"<location hidden: denylist#{hidden[0]}>" if hidden else location


class Findings:
    def __init__(self, rules: Rules):
        self.rules = rules
        self.items: list[tuple[str, str, str]] = []
        self._seen: set = set()

    def add(self, location: str, line, pattern: str):
        key = (self.rules.safe(location), str(line), pattern)
        if key not in self._seen:
            self._seen.add(key)
            self.items.append(key)

    def scan(self, location: str, data: bytes, path: str = ""):
        for n, pid in self.rules.scan_text(data.decode("utf-8", "ignore"), path):
            self.add(location, n, pid)

    def scan_name(self, location: str, name: str):
        for pid in self.rules.line_hits(name):
            self.add(location, "name", pid)


# ---- git access ------------------------------------------------------------------------------------
def git(root, *args: str, data: bytes | None = None, check: bool = True) -> bytes:
    p = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True)
    if check and p.returncode != 0:
        raise UsageError(f"git {args[0]} failed with exit code {p.returncode}")
    return p.stdout


def toplevel() -> Path | None:
    try:
        p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True)
    except OSError as e:
        raise UsageError("git is not installed or not on PATH") from e
    if p.returncode != 0:
        return None
    return Path(p.stdout.decode("utf-8", "surrogateescape").strip())


def object_info(root: Path, ids: list[str]) -> dict[str, tuple[str, int]]:
    """{object id: (type, size)} through `git cat-file --batch-check`."""
    out = git(root, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", data=("\n".join(ids) + "\n").encode())
    info = {}
    for line in out.decode().splitlines():
        parts = line.split()
        if len(parts) == 3:
            info[parts[0]] = (parts[1], int(parts[2]))
    return info


def read_objects(root: Path, items: list[tuple[str, int]]):
    """Yield (id, type, content) for [(id, size)], in batches bounded by count and bytes."""
    batch, total = [], 0
    for oid, size in items:
        batch.append(oid)
        total += size
        if len(batch) >= CHUNK_OBJECTS or total >= CHUNK_BYTES:
            yield from _batch(root, batch)
            batch, total = [], 0
    if batch:
        yield from _batch(root, batch)


def _batch(root: Path, ids: list[str]):
    data = git(root, "cat-file", "--batch", data=("\n".join(ids) + "\n").encode())
    pos = 0
    for _ in ids:
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise UsageError("unexpected output from git cat-file --batch")
        head = data[pos:nl].split(b" ")
        pos = nl + 1
        if len(head) != 3:
            continue
        size = int(head[2])
        yield head[0].decode(), head[1].decode(), data[pos:pos + size]
        pos += size + 1


# ---- sources -----------------------------------------------------------------------------------------
def scan_worktree(root: Path, found: Findings):
    listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for raw in sorted(set(listing.split(b"\0")) - {b""}):
        rel = raw.decode("utf-8", "surrogateescape")
        path = root / rel
        found.scan_name(rel, rel)
        try:
            if path.is_symlink():
                data = os.readlink(path).encode("utf-8", "surrogateescape")
            elif path.is_file():
                data = path.read_bytes()
            else:
                continue
        except OSError:
            found.add(rel, 0, "unreadable")
            continue
        found.scan(rel, data, rel)


def scan_staged(root: Path, found: Findings):
    raw = git(root, "diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "--diff-filter=d")
    parts = raw.split(b"\0")
    blobs = {}
    for meta, name in zip(parts[0::2], parts[1::2]):
        fields = meta.decode().lstrip(":").split()
        if len(fields) < 5 or fields[1] == "160000" or set(fields[3]) == {"0"}:
            continue
        blobs.setdefault(fields[3], []).append(name.decode("utf-8", "surrogateescape"))
    if not blobs:
        return
    info = object_info(root, list(blobs))
    for oid, kind, content in read_objects(root, [(o, info[o][1]) for o in blobs if o in info]):
        for rel in blobs[oid]:
            found.scan_name(f"staged {rel}", rel)
            found.scan(f"staged {rel}", content, rel)


def scan_history(root: Path, found: Findings):
    names = {}
    for raw in git(root, "rev-list", "--all", "--objects").split(b"\n"):
        if raw:
            oid, _, name = raw.partition(b" ")
            names[oid.decode()] = name.decode("utf-8", "surrogateescape")
    if not names:
        return
    for name in sorted(set(names.values()) - {""}):
        found.scan_name(f"history path {name}", name)
    info = object_info(root, list(names))
    items = [(oid, size) for oid, (kind, size) in info.items() if kind in ("blob", "commit", "tag")]
    for oid, kind, content in read_objects(root, items):
        if kind == "blob":
            found.scan(f"history blob {oid[:12]} ({names.get(oid, '')})", content, names.get(oid, ""))
        else:
            found.scan(f"history {kind} {oid[:12]}", content)


def scan_extra(paths: list[str], found: Findings):
    for p in paths:
        try:
            data = Path(p).read_bytes()
        except OSError as e:
            raise UsageError(f"--extra: cannot read {found.rules.safe(p)}") from e
        found.scan(f"extra {p}", data, p)


# ---- configuration -----------------------------------------------------------------------------------
def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ignored(root: Path, path: Path) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", "--", str(path)], cwd=root, capture_output=True).returncode == 0


def load_denylist(root: Path | None) -> list[tuple[int, str]]:
    name = os.environ.get(DENYLIST_ENV)
    if not name:
        return []
    path = Path(name).expanduser().resolve()
    if not path.is_file():
        raise UsageError(f"{DENYLIST_ENV} does not name a readable file")
    if root is not None and _inside(path, root) and not _ignored(root, path):
        raise UsageError(f"{DENYLIST_ENV} names a file inside the working tree that git does not ignore; "
                         "keep the deny-list outside the repository")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(f"{DENYLIST_ENV}: the deny-list must be a readable UTF-8 text file") from e
    terms = []
    for n, raw in enumerate(text.split("\n"), 1):
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append((n, term))
    return terms


def parse_args(argv):
    p = argparse.ArgumentParser(prog="leak_scan.py", description="Scan for secrets, personal e-mail addresses and deny-listed terms (prints locations only).")
    p.add_argument("--worktree", action="store_true", help="scan the working tree (the default when no other source is given)")
    p.add_argument("--staged", action="store_true", help="scan only the staged blobs (pre-commit)")
    p.add_argument("--history", action="store_true", help="scan every blob reachable from any ref plus commit and tag metadata (pre-push, CI)")
    p.add_argument("--extra", action="append", default=[], metavar="PATH", help="also scan this file, e.g. a pull request body or a CI log (repeatable)")
    p.add_argument("--allow-email", action="append", default=[], metavar="ADDR", help="allow this e-mail address, or @domain for a whole domain (repeatable)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    worktree = args.worktree or not (args.staged or args.history or args.extra)
    try:
        for a in args.allow_email:
            if "@" not in a:
                raise UsageError(f"--allow-email needs an address or @domain, got {a!r}")
        root = toplevel()
        if root is None and (worktree or args.staged or args.history):
            raise UsageError("not inside a git repository (run from the repository root)")
        allow = list(args.allow_email)
        if root is not None:
            configured = git(root, "config", "--get", "user.email", check=False).decode("utf-8", "replace").strip()
            if configured:
                allow.append(configured)
        deny = load_denylist(root)
        found = Findings(Rules(deny, allow))
        sources = []
        if worktree:
            scan_worktree(root, found)
            sources.append("worktree")
        if args.staged:
            scan_staged(root, found)
            sources.append("staged")
        if args.history:
            scan_history(root, found)
            sources.append("history")
        if args.extra:
            scan_extra(args.extra, found)
            sources.append("extra")
    except UsageError as e:
        print(f"leak_scan: {e}", file=sys.stderr)
        return 2
    for location, line, pattern in found.items:
        print(f"{location}:{line}: {pattern}")
    deny_note = f"deny-list: {len(deny)} terms" if deny else f"deny-list: not configured ({DENYLIST_ENV})"
    print(f"leak_scan: {len(found.items)} finding(s) in {', '.join(sources)}; {deny_note}", file=sys.stderr)
    if found.items:
        print("leak_scan: remove each finding; a finding in history means rewriting those commits before pushing", file=sys.stderr)
    return 1 if found.items else 0


if __name__ == "__main__":
    sys.exit(main())
