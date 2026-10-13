#!/usr/bin/env python3
"""Leak scanner for a public repository: secrets, personal e-mail addresses and private deny-listed terms.

Anything committed to a public repository is published for good, history included. This scanner runs before a
commit (.githooks/pre-commit: --staged), before a push (.githooks/pre-push: --history --push-refs -) and in CI
(--worktree --history on a full clone); --extra adds a pull request body or downloaded logs when the maintainer
scans them. Run it from the repository:

    python tools/leak_scan.py                  working tree: tracked files and untracked files git does not ignore
    python tools/leak_scan.py --staged         only the staged blobs
    python tools/leak_scan.py --history        everything reachable from any ref: every blob, commit and tag, every
                                               path name any commit had and every ref name
    python tools/leak_scan.py --push-refs -    --history plus what git hands a pre-push hook on standard input: the
                                               pushed commits (even those no local ref reaches) and remote ref
                                               names; a push that only deletes refs publishes nothing and is skipped
    python tools/leak_scan.py --extra FILE     additional files (repeatable); add --worktree to scan both

Each line, path name and ref name is checked for:
  - generic secrets: private key blocks; GitHub, AWS, Slack, npm, Google, Anthropic and Stripe tokens; URLs with
    user:password@; password assignments with a literal value; high-entropy api_key/secret/token assignments;
  - e-mail addresses outside the allowlist: noreply@anthropic.com, addresses at example.org and example.com, the
    entries of leak-allow.txt next to this script, the repository's configured user.email and every --allow-email
    (an address, or @domain for a whole domain); the user part of a URL (scheme://user@host) is not an address.
    leak-allow.txt is committed and lists the addresses that are public on purpose, such as the commit identities
    of the published history, so a fresh clone without a user.email of its own passes --history. One address or
    @domain per line; # starts a comment; anything else on a line is a usage error;
  - deny-listed terms from the file named by SXC_DENYLIST (UTF-8, or UTF-16 with a byte order mark; the value none
    means deliberately no deny-list): one literal term per line, lines starting with # are comments. A term matches
    as a case-insensitive substring after NFC and after NFKC normalisation, so decomposed Hangul and full-width
    letters match too. The deny-list never lives in the repository: a deny-list inside the working tree that git
    does not ignore is refused.

Content is never skipped as binary. It is read as UTF-8 (invalid bytes replaced), also as CP949 when it is not valid
UTF-8, and as UTF-16 or UTF-32 when it starts with a byte order mark, or as UTF-16 when its NUL bytes follow the
pattern of UTF-16 text (at least 1% of the code units, nearly all at one byte parity). Every line is also checked
with JSON \\uXXXX escapes, HTML character references and percent-encoding decoded (percent-encoded bytes as UTF-8,
and as CP949 when they are not UTF-8). gzip, bzip2, xz and zip data, recognised by magic bytes, is decompressed and
scanned as well, zip member names included, up to 64 MiB and three nested levels per file; compressed data that
cannot be scanned completely (corrupt, encrypted, unsupported, larger or deeper than that, or a zip of more than
10000 members) is a compressed_unscanned finding. One narrow exception: e-mail addresses on the `deprecated` lines of
npm lockfiles are registry messages of third-party packages and are not flagged (secrets and the deny-list still
apply to those lines).

Output is a location and a pattern id per finding, never the matching text, so the scan cannot copy a token into a
CI log. A deny-list hit is reported as denylist#<line number in the deny-list file>. A path or ref name that itself
holds a finding is never printed: it appears as <path sha256:...> or <ref sha256:...>, the first 12 hex digits of
the SHA-256 of the name's UTF-8 bytes, to be matched against `git ls-files`, `git log --all --name-only --format=`
or `git for-each-ref` output. History locations are `blob <id> (<path>)`, `commit <id>`, `tag <id>`, `path <name>
(commit <id>)` and `ref <name> (<object id>)`; their line numbers are those of `git cat-file -p <id>`. Pushed remote
ref names are `push ref <name>`. Decompressed content is located as `<location> [gzip]`, `[bz2]`, `[xz]` or
`[zip <member>]`, with the line numbers of the decompressed text.

Exit codes: 0 clean, 1 findings, 2 usage error (bad arguments, not a git repository, unusable deny-list or
allowlist, malformed --push-refs input).
Standard library only, Python 3.8 or newer: hooks run with whatever Python is on PATH.
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import gzip
import hashlib
import html
import io
import math
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

try:
    import bz2
except ImportError:          # a Python built without libbz2: bzip2 data becomes compressed_unscanned
    bz2 = None
try:
    import lzma
except ImportError:          # likewise without liblzma
    lzma = None

DEFAULT_ALLOWED_EMAILS = ("noreply@anthropic.com",)
DEFAULT_ALLOWED_DOMAINS = ("example.org", "example.com")
ALLOWLIST = Path(__file__).resolve().with_name("leak-allow.txt")
DENYLIST_ENV = "SXC_DENYLIST"
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".h",
                 ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala"}
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json"}
CHUNK_OBJECTS, CHUNK_BYTES = 500, 64 << 20
EXPAND_BYTES, EXPAND_DEPTH, ZIP_MEMBERS = 64 << 20, 3, 10000

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
ALLOW_ENTRY = re.compile(r"(?:[a-z0-9._%+-]{1,64})?@(?:[a-z0-9-]{1,63}\.){1,8}[a-z]{2,24}")
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PLACEHOLDERS = {"password", "passwd", "pass", "pwd", "pw", "secret", "token", "changeme", "redacted", "placeholder",
                "example", "dummy", "none", "null", "nil", "true", "false", "str", "bytes", "string", "optional",
                "required", "empty"}
_LINE_START = re.compile(r"\s*(?:export\s+|set\s+)?[\"']?")
_ATTRIBUTE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_DEPRECATED = re.compile(r'\s*"deprecated"\s*:')
_URL_AUTHORITY = re.compile(r"://(?:[^\s/@:]{1,256}:)?$")
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_PERCENT = re.compile(r"%[0-9a-fA-F]{2}")
_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))


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


# ---- text views: the forms in which a person would still recognise a term --------------------------------------
def fold(text: str) -> tuple[str, str]:
    """The NFC and the NFKC form of text, case-folded: deny-list terms and text are compared in both forms. NFKC
    runs before case folding as well as after it, since only NFKC turns a full-width or styled capital into a plain
    one that case folding can lower; text without compatibility characters has a single form."""
    if text.isascii():
        low = text.lower()
        return low, low
    nfc = unicodedata.normalize("NFC", text.casefold())
    if unicodedata.is_normalized("NFKC", nfc):
        return nfc, nfc
    return nfc, unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def utf16_order(data: bytes) -> str | None:
    """'utf-16-le' or 'utf-16-be' when the NUL bytes follow the pattern of UTF-16 text without a byte order mark:
    at least 1% of the code units hold a NUL (the high byte of ASCII characters) and nearly all NULs sit at one byte
    parity. Random or compressed binary has few NULs, padded binary has them at both parities."""
    even, odd = data[0::2].count(0), data[1::2].count(0)
    units = len(data) // 2
    if odd * 100 >= units and odd >= 4 * even and odd:
        return "utf-16-le"
    if even * 100 >= units and even >= 4 * odd and even:
        return "utf-16-be"
    return None


def decodings(data: bytes):
    """Yield every reading of data that could carry text: UTF-8, CP949 when the data is not valid UTF-8, and UTF-16
    or UTF-32 by byte order mark or by the NUL pattern of UTF-16 text. One reading at a time, so a large file holds
    a single decoded copy. Invalid bytes become U+FFFD rather than vanishing, so binary noise cannot splice separate
    fragments into a false match."""
    try:
        yield data.decode("utf-8")
    except UnicodeDecodeError:
        yield data.decode("utf-8", "replace")
        yield data.decode("cp949", "replace")
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            yield data[len(bom):].decode(encoding, "replace")
            return
    order = utf16_order(data) if b"\0" in data else None
    if order:
        yield data.decode(order, "replace")


def unescaped(line: str) -> list[str]:
    """The line with JSON \\uXXXX escapes, HTML character references and percent-encoding decoded, when that
    changes it; percent-encoded bytes are read as UTF-8, and also as CP949 when they are not valid UTF-8."""
    text = line
    if "\\u" in text:
        text = _JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if "&" in text:
        text = html.unescape(text)
    views = [text]
    if "%" in text and _PERCENT.search(text):
        try:
            views = [unquote(text, errors="strict")]
        except UnicodeDecodeError:
            views = [unquote(text, errors="replace"), unquote(text, encoding="cp949", errors="replace")]
    return [v for v in views if v != line]


def compression(data: bytes) -> str | None:
    """'gzip', 'bz2', 'xz' or 'zip' when the magic bytes say so."""
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:3] == b"BZh" and data[4:10] in (b"1AY&SY", b"\x17rE8P\x90"):
        return "bz2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    return None


def open_compressed(kind: str, data: bytes):
    """A decompressing reader over gzip, bzip2 or xz data; OSError when this Python lacks the codec."""
    buffer = io.BytesIO(data)
    if kind == "gzip":
        return gzip.GzipFile(fileobj=buffer)
    module = bz2 if kind == "bz2" else lzma
    if module is None:
        raise OSError(f"this Python has no {kind} support")
    return module.BZ2File(buffer) if kind == "bz2" else module.LZMAFile(buffer)


def zip_name(info: zipfile.ZipInfo) -> str:
    """A member name as raw bytes would read in git: zipfile decodes names without the UTF-8 flag as cp437, which
    hides the CP949 names of archives made on Korean Windows."""
    if info.flag_bits & 0x800:
        return info.orig_filename
    return info.orig_filename.encode("cp437").decode("utf-8", "surrogateescape")


def mask(name: str, kind: str = "path") -> str:
    """A printable stand-in for a name that holds a finding: the first 12 hex digits of its UTF-8 SHA-256."""
    return f"<{kind} sha256:{hashlib.sha256(name.encode('utf-8', 'surrogateescape')).hexdigest()[:12]}>"


class Rules:
    """Pattern set for one run: built-in patterns, the e-mail allowlist and the deny-list [(line number, term)]."""

    def __init__(self, deny=(), allow=()):
        self.deny = [(n, *fold(t)) for n, t in deny]
        self.emails = {e.lower() for e in DEFAULT_ALLOWED_EMAILS}
        self.domains = set(DEFAULT_ALLOWED_DOMAINS)
        for a in allow:
            a = a.strip().lower()
            if a.startswith("@"):
                self.domains.add(a[1:])
            elif a:
                self.emails.add(a)

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

    def deny_hits(self, text: str) -> list[str]:
        if not self.deny:
            return []
        nfc, nfkc = fold(text)
        return [f"denylist#{n}" for n, term_nfc, term_nfkc in self.deny if term_nfc in nfc or term_nfkc in nfkc]

    def _hits(self, line: str, kind: str) -> list[str]:
        """Every pattern id in one reading of a line; the assignment patterns run only when their key word occurs."""
        hits = [pid for pid, rx in TOKEN_PATTERNS if rx.search(line)]
        if "://" in line and any(not is_placeholder(m.group("pw")) for m in URL_CREDENTIALS.finditer(line)):
            hits.append("url_credentials")
        folded = line.casefold()
        if "pass" in folded and password_hit(line, kind == "code"):
            hits.append("password_assignment")
        if ("key" in folded or "secret" in folded or "token" in folded) and secret_hit(line):
            hits.append("secret_assignment")
        registry_text = kind == "lockfile" and _DEPRECATED.match(line)
        if "@" in line and not registry_text and self._email_hit(line):
            hits.append("email")
        return hits + self.deny_hits(line)

    def line_hits(self, line: str, kind: str = "text") -> list[str]:
        """Pattern ids in the line as written and in its unescaped views."""
        hits = self._hits(line, kind)
        for view in unescaped(line):
            hits += [h for h in self._hits(view, kind) if h not in hits]
        return hits

    def name_hits(self, name: str) -> list[str]:
        """Pattern ids in a path or ref name; name bytes that are not valid UTF-8 are also read as CP949."""
        hits = self.line_hits(name)
        raw = name.encode("utf-8", "surrogateescape")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            hits += [h for h in self.line_hits(raw.decode("cp949", "replace")) if h not in hits]
        return hits

    def scan_text(self, text: str, path: str = "") -> list[tuple[int, str]]:
        kind = file_kind(path)
        return [(n, pid) for n, line in enumerate(text.split("\n"), 1) for pid in self.line_hits(line.rstrip("\r"), kind)]


class Findings:
    def __init__(self, rules: Rules):
        self.rules = rules
        self.items: list[tuple[str, str, str]] = []
        self._seen: set = set()
        self._names: dict[str, list[str]] = {}
        self._budget = 0

    def add(self, location: str, line, pattern: str):
        key = (location, str(line), pattern)
        if key not in self._seen:
            self._seen.add(key)
            self.items.append(key)

    def name_hits(self, name: str) -> list[str]:
        if name not in self._names:
            self._names[name] = self.rules.name_hits(name)
        return self._names[name]

    def show(self, name: str, kind: str = "path") -> str:
        """The name as printed: masked when the name itself holds a finding, which printing it would echo."""
        return mask(name, kind) if self.name_hits(name) else name

    def scan_name(self, name: str, prefix: str = "", suffix: str = "", kind: str = "path") -> str:
        """Report the findings of a path or ref name; returns the location the name is printed as."""
        location = f"{prefix}{self.show(name, kind)}{suffix}"
        for pid in self.name_hits(name):
            self.add(location, "name", pid)
        return location

    def scan(self, location: str, data: bytes, path: str = ""):
        """Scan every decoding of data, and of the content compressed inside it, within EXPAND_BYTES in total."""
        self._budget = EXPAND_BYTES
        self._scan(location, data, path, 0)

    def _scan(self, location: str, data: bytes, path: str, depth: int):
        for text in decodings(data):
            for n, pid in self.rules.scan_text(text, path):
                self.add(location, n, pid)
        kind = compression(data)
        if kind and not self._expand(kind, location, data, path, depth):
            self.add(location, 0, "compressed_unscanned")

    def _expand(self, kind: str, location: str, data: bytes, path: str, depth: int) -> bool:
        """Scan the decompressed content; False when part of it could not be scanned."""
        if depth >= EXPAND_DEPTH:
            return False
        if kind == "zip":
            return self._expand_zip(location, data, depth)
        content, complete = self._inflate(lambda: open_compressed(kind, data))
        self._scan(f"{location} [{kind}]", content, path, depth + 1)
        return complete

    def _expand_zip(self, location: str, data: bytes, depth: int) -> bool:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except Exception:    # zipfile raises many types on a malformed archive; each means "not scanned"
            return False
        with archive:
            members = archive.infolist()
            complete = len(members) <= ZIP_MEMBERS
            for info in members[:ZIP_MEMBERS]:
                name = zip_name(info)
                where = self.scan_name(name, f"{location} [zip ", "]")
                if info.is_dir():
                    continue
                content, whole = self._inflate(lambda: archive.open(info))
                self._scan(where, content, name, depth + 1)
                complete = complete and whole
        return complete

    def _inflate(self, open_stream) -> tuple[bytes, bool]:
        """Decompressed bytes within the remaining budget, and whether they are all of them. read1 hands back each
        piece as it is decompressed, so a truncated stream still yields the prefix before its error."""
        out, complete = bytearray(), True
        try:
            with open_stream() as stream:
                while True:
                    chunk = stream.read1(min(1 << 20, self._budget + 1 - len(out)))
                    if not chunk:
                        break
                    out += chunk
                    if len(out) > self._budget:
                        complete = False
                        break
        except Exception:    # corrupt, truncated, encrypted or unsupported data: keep what was decompressed
            complete = False
        content = bytes(out[:self._budget])
        self._budget -= len(content)
        return content, complete


# ---- git access ------------------------------------------------------------------------------------
def git(root, *args: str, data: bytes | None = None, check: bool = True) -> bytes:
    """Run git on the real objects: replacement refs must not hide what a push publishes."""
    p = subprocess.run(["git", "--no-replace-objects", *args], cwd=root, input=data, capture_output=True)
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


def history_paths(root: Path, tips: bytes) -> dict[str, str]:
    """{path: a commit listing it} for every path a commit added, changed or deleted. Unlike the names of
    `rev-list --objects`, which name each object once, this lists a second path to the same blob too; -m includes
    paths that only a merge introduced."""
    out = git(root, "-c", "log.showRoot=true", "log", "--all", "--stdin", "-m", "--no-renames", "--name-only", "-z", "--no-color",
              "--no-show-signature", "--format=%x01%H", data=tips)
    paths: dict[str, str] = {}
    commit, header = "", False
    for token in out.split(b"\0"):
        if token[:1] == b"\x01" and OBJECT_ID.fullmatch(token[1:].decode("ascii", "replace")):
            commit, header = token[1:].decode(), True
            continue
        if header and token[:1] == b"\n":
            token = token[1:]
        header = False
        if token:
            paths.setdefault(token.decode("utf-8", "surrogateescape"), commit)
    return paths


def ref_names(root: Path) -> list[tuple[str, str]]:
    """[(object id, ref name)] for every ref: branch and tag names are published by a push too."""
    refs = []
    for line in git(root, "for-each-ref", "--format=%(objectname) %(refname)").split(b"\n"):
        oid, _, name = line.partition(b" ")
        if name:
            refs.append((oid.decode(), name.decode("utf-8", "surrogateescape")))
    return refs


class PushRefs:
    """What git hands a pre-push hook: lines `<local ref> <local oid> <remote ref> <remote oid>`. The local ref is
    the name or expression the user typed (local refs are scanned with every other ref); a deletion has an all-zero
    local id and publishes nothing."""

    def __init__(self, text: str):
        self.tips: list[str] = []
        self.remote_refs: list[str] = []
        self.deletions = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.rsplit(" ", 3)
            if len(parts) != 4 or not (OBJECT_ID.fullmatch(parts[1]) and OBJECT_ID.fullmatch(parts[3])):
                raise UsageError("--push-refs: expected lines of '<local ref> <local oid> <remote ref> <remote oid>'")
            if set(parts[1]) == {"0"}:
                self.deletions += 1
            else:
                self.tips.append(parts[1])
                self.remote_refs.append(parts[2])

    @property
    def deletes_only(self) -> bool:
        return self.deletions > 0 and not self.tips


# ---- sources -----------------------------------------------------------------------------------------
def scan_worktree(root: Path, found: Findings):
    listing = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for raw in sorted(set(listing.split(b"\0")) - {b""}):
        rel = raw.decode("utf-8", "surrogateescape")
        path = root / rel
        where = found.scan_name(rel)
        try:
            if path.is_symlink():
                data = os.readlink(path).encode("utf-8", "surrogateescape")
            elif path.is_file():
                data = path.read_bytes()
            else:
                continue
        except OSError:
            found.add(where, 0, "unreadable")
            continue
        found.scan(where, data, rel)


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
            found.scan(found.scan_name(rel, "staged "), content, rel)


def scan_history(root: Path, found: Findings, push: PushRefs | None = None):
    tips = "".join(f"{oid}\n" for oid in (push.tips if push else ())).encode()
    objects = {}
    for raw in git(root, "rev-list", "--objects", "--all", "--stdin", data=tips).split(b"\n"):
        if raw:
            oid, _, name = raw.partition(b" ")
            objects[oid.decode()] = name.decode("utf-8", "surrogateescape")
    if not objects:
        return
    paths = history_paths(root, tips)
    for name, commit in sorted(paths.items()):
        found.scan_name(name, "history path ", f" (commit {commit[:12]})")
    covered = {"/".join(parts[:i]) for parts in (p.split("/") for p in paths) for i in range(1, len(parts) + 1)}
    for name in sorted(set(objects.values()) - covered - {""}):
        found.scan_name(name, "history path ")
    for oid, ref in ref_names(root):
        found.scan_name(ref, "history ref ", f" ({oid[:12]})", kind="ref")
    for ref in (push.remote_refs if push else ()):
        found.scan_name(ref, "push ref ", kind="ref")
    info = object_info(root, list(objects))
    items = [(oid, size) for oid, (kind, size) in info.items() if kind in ("blob", "commit", "tag")]
    for oid, kind, content in read_objects(root, items):
        if kind == "blob":
            name = objects.get(oid, "")
            found.scan(f"history blob {oid[:12]} ({found.show(name)})", content, name)
        else:
            found.scan(f"history {kind} {oid[:12]}", content)


def scan_extra(paths: list[str], found: Findings):
    for p in paths:
        try:
            data = Path(p).read_bytes()
        except OSError as e:
            raise UsageError(f"--extra: cannot read {found.show(p)}") from e
        found.scan(f"extra {found.show(p)}", data, p)


# ---- configuration -----------------------------------------------------------------------------------
def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ignored(root: Path, path: Path) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", "--", str(path)], cwd=root, capture_output=True).returncode == 0


def allow_entry(value: str) -> str | None:
    """The normalised allowlist entry (an address, or @domain with at least one dot), or None when invalid."""
    entry = value.strip().lower()
    return entry if ALLOW_ENTRY.fullmatch(entry) else None


def load_allowlist(path: Path = ALLOWLIST) -> list[str]:
    """Entries of the committed allowlist: one address or @domain per line, # starts a comment. A missing file
    allows nothing more; an unreadable file or an invalid line is a usage error, never silently skipped."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(f"{path.name}: the allowlist must be a readable UTF-8 text file") from e
    entries = []
    for n, raw in enumerate(text.split("\n"), 1):
        value = raw.split("#", 1)[0].strip()
        if not value:
            continue
        entry = allow_entry(value)
        if entry is None:
            raise UsageError(f"{path.name} line {n}: expected an e-mail address or @domain")
        entries.append(entry)
    return entries


def denylist_setting() -> str:
    return os.environ.get(DENYLIST_ENV, "").strip()


def load_denylist(root: Path | None) -> list[tuple[int, str]]:
    name = denylist_setting()
    if not name or name.lower() == "none":
        return []
    path = Path(name).expanduser().resolve()
    if not path.is_file():
        raise UsageError(f"{DENYLIST_ENV} does not name a readable file")
    if root is not None and _inside(path, root) and not _ignored(root, path):
        raise UsageError(f"{DENYLIST_ENV} names a file inside the working tree that git does not ignore; "
                         "keep the deny-list outside the repository")
    try:
        raw = path.read_bytes()
        utf16 = raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
        text = raw.decode("utf-16") if utf16 else raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(f"{DENYLIST_ENV}: the deny-list must be readable UTF-8 (or UTF-16 with BOM) text") from e
    terms = []
    for n, raw_line in enumerate(text.split("\n"), 1):
        term = raw_line.strip()
        if term and not term.startswith("#"):
            terms.append((n, term))
    return terms


def read_push_refs(source: str) -> PushRefs:
    try:
        data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    except (OSError, AttributeError) as e:
        raise UsageError("--push-refs: cannot read the input") from e
    return PushRefs(data.decode("utf-8", "surrogateescape"))


def allowlist_label(root: Path | None) -> str:
    """The allowlist as the user would name it: relative to the repository when it lives there."""
    if root is not None:
        with contextlib.suppress(ValueError):
            return ALLOWLIST.relative_to(root.resolve()).as_posix()
    return ALLOWLIST.name


def parse_args(argv):
    p = argparse.ArgumentParser(prog="leak_scan.py", description="Scan for secrets, personal e-mail addresses and deny-listed terms (prints locations only).")
    p.add_argument("--worktree", action="store_true", help="scan the working tree (the default when no other source is given)")
    p.add_argument("--staged", action="store_true", help="scan only the staged blobs (pre-commit)")
    p.add_argument("--history", action="store_true", help="scan blobs, metadata, path and ref names reachable from any ref (pre-push, CI)")
    p.add_argument("--push-refs", metavar="FILE", help="pre-push input ('-' = stdin): also the pushed commits and remote ref names; implies --history")
    p.add_argument("--extra", action="append", default=[], metavar="PATH", help="also scan this file, e.g. a pull request body or a CI log (repeatable)")
    p.add_argument("--allow-email", action="append", default=[], metavar="ADDR", help="allow this e-mail address, or @domain for a whole domain (repeatable)")
    return p.parse_args(argv)


def summary(found: Findings, sources: list[str], allowlist: list[str], deny: list, root: Path | None) -> list[str]:
    if deny:
        deny_note = f"deny-list: {len(deny)} terms"
    elif denylist_setting().lower() == "none":
        deny_note = f"deny-list: none ({DENYLIST_ENV}=none)"
    else:
        deny_note = f"deny-list: not configured ({DENYLIST_ENV})"
    lines = [f"leak_scan: {len(found.items)} finding(s) in {', '.join(sources)}; "
             f"allowlist: {len(allowlist)} entries; {deny_note}"]
    if found.items:
        lines.append("leak_scan: remove each finding; a finding in history means rewriting those commits before pushing")
    if any(pattern == "email" for _, _, pattern in found.items):
        lines.append("leak_scan: an address that is public on purpose (a commit identity) belongs in "
                     + allowlist_label(root))
    return lines


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    history = args.history or args.push_refs is not None
    worktree = args.worktree or not (args.staged or history or args.extra)
    try:
        for n, a in enumerate(args.allow_email, 1):
            if allow_entry(a) is None:
                raise UsageError(f"--allow-email needs an e-mail address or @domain (occurrence {n})")
        root = toplevel()
        if root is None and (worktree or args.staged or history):
            raise UsageError("not inside a git repository (run from the repository root)")
        allowlist = load_allowlist()
        allow = allowlist + list(args.allow_email)
        if root is not None:
            configured = git(root, "config", "--get", "user.email", check=False).decode("utf-8", "replace").strip()
            if configured:
                allow.append(configured)
        deny = load_denylist(root)
        push = read_push_refs(args.push_refs) if args.push_refs is not None else None
        found = Findings(Rules(deny, allow))
        sources = []
        if worktree:
            scan_worktree(root, found)
            sources.append("worktree")
        if args.staged:
            scan_staged(root, found)
            sources.append("staged")
        if history and push is not None and push.deletes_only:
            sources.append("history (the push only deletes refs, nothing to scan)")
        elif history:
            scan_history(root, found, push)
            sources.append("history")
        if args.extra:
            scan_extra(args.extra, found)
            sources.append("extra")
    except UsageError as e:
        print(f"leak_scan: {e}", file=sys.stderr)
        return 2
    for location, line, pattern in found.items:
        print(f"{location}:{line}: {pattern}")
    for line in summary(found, sources, allowlist, deny, root):
        print(line, file=sys.stderr)
    return 1 if found.items else 0


if __name__ == "__main__":
    sys.exit(main())
