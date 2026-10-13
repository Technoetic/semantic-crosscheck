"""`cube-install`: install the pinned Cube adapter once per user.

Why it exists: the npm postinstall of @cubejs-backend/native downloads its native module from GitHub without
any integrity check. This installer runs no package scripts and verifies everything it fetches:

1. package.json and the committed package-lock.json (adapters/cube) are copied into the install home and
   `npm ci --ignore-scripts` installs exactly the locked tree (npm checks every tarball's sha512 integrity).
2. The release tarball of the native module for this platform is downloaded over https (or taken from
   --native-file) and accepted only when its size and SHA-256 equal native-sha256.json, before extraction.
3. Only the regular file native/index.node is extracted (the `native` directory entry is tolerated; any other
   member, any link, absolute or parent path rejects the archive) into node_modules/@cubejs-backend/native/,
   where the package loader looks for <package>/native/index.node.
4. A node smoke test loads the native module and the server packages from the install home.
5. server.js and installed.json are written last, so a failed install never looks installed to
   cube_run.installed() (which keys on server.js); a reinstall removes both first.

A later run is skipped while installed.json still matches: same platform, the pinned tarball hash, the same
index.node hash on disk and the same adapter files (server.js, package.json, package-lock.json). --force
reinstalls. Results carry check ids, counts, versions and hashes only.
"""

import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from . import VERSION, config, cube_run
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

PINS_FILE = "native-sha256.json"
ADAPTER_FILES = ("server.js", "package.json", "package-lock.json")
PACKAGES = ("@cubejs-backend/server-core", "@cubejs-backend/postgres-driver", "@cubejs-backend/native", "express")
PLATFORMS = ("win32-x64-unknown", "linux-x64-glibc", "linux-arm64-glibc", "darwin-x64-unknown", "darwin-arm64-unknown")
STEPS = ("npm_ci", "native_sha256", "extract", "smoke")
MEMBER = "native/index.node"
MEMBER_DIR = "native"
NATIVE_DIR = Path("node_modules") / "@cubejs-backend" / "native"
INDEX_NODE = NATIVE_DIR / "native" / "index.node"
RECORD = "installed.json"
MIN_NODE = 20
MAX_INDEX_BYTES = 512 << 20
CHUNK = 1 << 20
NPM_TIMEOUT_S = 1800
DOWNLOAD_TIMEOUT_S = 60
NODE_TIMEOUT_S = 180
_ARCH = {"amd64": "x64", "x86_64": "x64", "x64": "x64", "aarch64": "arm64", "arm64": "arm64"}
_NODE_INFO = "process.versions.node + ' ' + process.platform + '-' + process.arch"
_SMOKE = ("const n = require('@cubejs-backend/native'); n.loadNative(); "
          "require('@cubejs-backend/server-core'); require('@cubejs-backend/postgres-driver'); require('express'); "
          "console.log('SXC_SMOKE_OK ' + (n.isFallbackBuild() ? 'fallback' : 'full'));")


class NativeArchiveError(ValueError):
    """The native tarball does not have the expected layout; the message is a short reason enum."""


# ---- platform and pins ----------------------------------------------------------------------------
def platform_key(system: str | None = None, machine: str | None = None, libc: str | None = None) -> str:
    """Release asset key '<os>-<arch>-<libc>' of this machine; StageError(exit 3) when Cube ships no build for it."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    arch = _ARCH.get(machine, machine or "unknown")
    if system.startswith("linux"):
        libc = platform.libc_ver()[0] if libc is None else libc
        key = f"linux-{arch}-{'glibc' if libc == 'glibc' else (libc or 'non-glibc')}"
    elif system in ("win32", "darwin"):
        key = f"{system}-{arch}-unknown"
    else:
        key = f"{system}-{arch}"
    if key not in PLATFORMS:
        raise StageError(f"unsupported platform {key}: Cube {config.CUBE_VERSION} native modules are pinned only for "
                         f"{', '.join(PLATFORMS)} (Linux needs glibc; musl is not supported)", EXIT_ENV)
    return key


def load_pins(adapter_dir: Path = cube_run.ADAPTER_DIR) -> dict:
    return json.loads((Path(adapter_dir) / PINS_FILE).read_text(encoding="utf-8"))


def pinned_asset(pins: dict, key: str) -> dict:
    """The {url, sha256, size} pin of one platform, validated so a damaged pin file cannot widen what is accepted."""
    a = (pins.get("assets") or {}).get(key)
    prefix = f"https://github.com/cube-js/cube/releases/download/v{config.CUBE_VERSION}/"
    if (pins.get("cube_version") != config.CUBE_VERSION or not isinstance(a, dict)
            or not str(a.get("url", "")).startswith(prefix)
            or not re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256", "")))
            or isinstance(a.get("size"), bool) or not isinstance(a.get("size"), int) or a["size"] <= 0):
        raise StageError(f"{PINS_FILE} has no valid pin for {key} (Cube {config.CUBE_VERSION})", EXIT_ENV)
    return a


# ---- hashes and the install record ------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def adapter_hashes(adapter_dir: Path = cube_run.ADAPTER_DIR) -> dict:
    return {name: sha256_file(Path(adapter_dir) / name) for name in ADAPTER_FILES}


def installed_record(home: Path, key: str, asset: dict, files: dict) -> dict | None:
    """installed.json of a complete install that still matches the pins and the adapter files, else None."""
    if not cube_run.installed(home):
        return None
    try:
        rec = json.loads((home / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("native"), dict):
        return None
    native = rec["native"]
    if rec.get("platform") != key or native.get("tarball_sha256") != asset["sha256"] or rec.get("files_sha256") != files:
        return None
    index = home / INDEX_NODE
    if not index.is_file() or sha256_file(index) != native.get("index_node_sha256"):
        return None
    return rec


def _package_version(home: Path, name: str) -> str | None:
    try:
        return json.loads((home / "node_modules" / name / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


# ---- native tarball ---------------------------------------------------------------------------------
class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise urllib.error.URLError("redirect to a non-https URL refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, dest: Path, size: int) -> int:
    """Stream an https URL to dest (redirects must stay on https), reading at most size + 1 bytes."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise urllib.error.URLError("only https downloads are allowed")
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": f"semantic-crosscheck/{VERSION}"})
    got = 0
    with opener.open(req, timeout=DOWNLOAD_TIMEOUT_S) as r, open(dest, "wb") as f:
        while got <= size:
            chunk = r.read(min(CHUNK, size + 1 - got))
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
    return got


def verify_tarball(path: Path, sha256: str, size: int) -> dict:
    """Size and SHA-256 of the tarball against its pin; the hash is computed only when the size matches."""
    actual = Path(path).stat().st_size
    digest = sha256_file(path) if actual == size else None
    return {"ok": actual == size and digest == sha256, "size": actual, "size_ok": actual == size,
            "sha256_ok": digest == sha256, "sha256": digest}


def member_problem(m: tarfile.TarInfo) -> str | None:
    """Why a tar member is not acceptable, or None for the `native` directory and the regular native/index.node."""
    name = m.name
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return "absolute_path"
    if ".." in re.split(r"[\\/]", name):
        return "parent_path"
    if m.issym() or m.islnk():
        return "link"
    if (name == MEMBER_DIR and m.isdir()) or (name == MEMBER and m.isreg()):
        return None
    return "unexpected_member"


def extract_index_node(tar_path: Path, dest: Path) -> dict:
    """Write the single regular member native/index.node of a verified tarball to dest.

    Raises NativeArchiveError (reason enum) before writing anything when any member is not acceptable, when
    index.node is missing, duplicated or larger than MAX_INDEX_BYTES."""
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            problem = member_problem(m)
            if problem:
                raise NativeArchiveError(problem)
        files = [m for m in members if m.name == MEMBER]
        if len(files) != 1:
            raise NativeArchiveError("missing_member" if not files else "duplicate_member")
        member = files[0]
        if member.size > MAX_INDEX_BYTES:
            raise NativeArchiveError("member_too_large")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        h = hashlib.sha256()
        try:
            with tar.extractfile(member) as src, open(part, "wb") as out:
                for chunk in iter(lambda: src.read(CHUNK), b""):
                    h.update(chunk)
                    out.write(chunk)
            os.chmod(part, 0o755)
            os.replace(part, dest)
        finally:
            part.unlink(missing_ok=True)
    return {"members": len(members), "bytes": member.size, "index_node_sha256": h.hexdigest()}


def install_native(res: Result, home: Path, asset: dict, native_file: str = "") -> dict | None:
    """Fetch (or take) the native tarball, verify it, then extract index.node into the installed package.

    Adds cube_install:native_sha256 and cube_install:extract; returns the `native` part of installed.json or None."""
    tmp = None
    source = "file" if native_file else "download"
    try:
        if native_file:
            tarball = Path(native_file)
        else:
            fd, name = tempfile.mkstemp(prefix=".native-", suffix=".tar.gz", dir=home)
            os.close(fd)
            tmp = tarball = Path(name)
            try:
                download(asset["url"], tarball, asset["size"])
            except urllib.error.HTTPError as e:
                res.add("cube_install:native_sha256", "error", "", source=source, http_status=e.code)
                return None
            except (OSError, http.client.HTTPException) as e:   # URLError, timeouts, resets, truncated bodies
                res.add("cube_install:native_sha256", "error", "", source=source, reason=type(e).__name__)
                return None
        v = verify_tarball(tarball, asset["sha256"], asset["size"])
        ok = v.pop("ok")
        res.add("cube_install:native_sha256", "pass" if ok else "fail", "", source=source, expected_size=asset["size"], **v)
        if not ok:
            return None
        if not (home / NATIVE_DIR).is_dir():
            res.add("cube_install:extract", "fail", "", reason="native_package_missing")
            return None
        if (home / NATIVE_DIR / "index.node").exists():
            res.add("cube_install:extract", "fail", "", reason="shadowing_index_node")
            return None
        try:
            ex = extract_index_node(tarball, home / INDEX_NODE)
        except NativeArchiveError as e:
            res.add("cube_install:extract", "fail", "", reason=str(e))
            return None
        except (tarfile.TarError, EOFError, zlib.error) as e:
            res.add("cube_install:extract", "fail", "", reason="bad_archive", error=type(e).__name__)
            return None
        except OSError as e:                                    # a bad gzip header, or the disk refused the write
            res.add("cube_install:extract", "error", "", reason=type(e).__name__)
            return None
        res.add("cube_install:extract", "pass", "", **ex)
        return {"package": "@cubejs-backend/native", "build": "fallback", "member": MEMBER, "url": asset["url"],
                "tarball_sha256": v["sha256"], "tarball_size": v["size"], "index_node_sha256": ex["index_node_sha256"]}
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


# ---- node and npm -----------------------------------------------------------------------------------
def _tools() -> tuple[str, str]:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise StageError(f"Node.js >= {MIN_NODE} with npm must be on PATH to install the Cube adapter", EXIT_ENV)
    return node, npm


def check_node(node: str, key: str) -> str:
    """Node.js version; StageError(exit 3) when it is too old or runs on another platform/arch than the pin."""
    try:
        p = subprocess.run([node, "-p", _NODE_INFO], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=NODE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StageError(f"node could not be run ({type(e).__name__})", EXIT_ENV) from e
    parts = p.stdout.split()
    if p.returncode != 0 or len(parts) != 2:
        raise StageError("node did not report its version", EXIT_ENV)
    version, plat = parts
    major = version.split(".")[0]
    if not major.isdigit() or int(major) < MIN_NODE:
        raise StageError(f"Node.js {version} is too old: the pinned Cube packages need Node.js >= {MIN_NODE}", EXIT_ENV)
    if plat != key.rsplit("-", 1)[0]:
        raise StageError(f"node runs as {plat} but this machine maps to the {key} native module; "
                         "use a node binary built for the machine's architecture", EXIT_ENV)
    return version


def npm_ci(npm: str, home: Path, log: Path) -> int:
    """npm ci from the committed lockfile with package scripts disabled; output goes to the log file only."""
    env = {**os.environ, "npm_config_update_notifier": "false"}
    with open(log, "wb") as f:
        p = subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=str(home), env=env,
                           stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, timeout=NPM_TIMEOUT_S)
    return p.returncode


def smoke(node: str, home: Path) -> dict:
    """Load the native module (fallback build) and the server packages from the install home."""
    env = {**os.environ, "CUBEJS_TELEMETRY": "false"}
    try:
        p = subprocess.run([node, "-e", _SMOKE], cwd=str(home), env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=NODE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    marks = [ln.split() for ln in p.stdout.splitlines() if ln.startswith("SXC_SMOKE_OK ")]
    build = marks[0][1] if marks and len(marks[0]) == 2 else None
    return {"ok": p.returncode == 0 and build == "fallback", "exit_code": p.returncode, "build": build}


# ---- stage ------------------------------------------------------------------------------------------
def check_home(home: Path) -> None:
    """Refuse a directory that is neither empty nor an adapter install (npm ci replaces node_modules there)."""
    if not home.exists():
        return
    if not home.is_dir():
        raise StageError(f"{home} is not a directory", EXIT_CONFIG)
    ours = json.loads((cube_run.ADAPTER_DIR / "package.json").read_text(encoding="utf-8"))["name"]
    pkg = home / "package.json"
    if pkg.is_file():
        try:
            name = json.loads(pkg.read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError, AttributeError):
            name = None
        if name == ours:
            return
        raise StageError(f"{home} holds another package.json; install the Cube adapter into an empty directory", EXIT_CONFIG)
    if any(home.iterdir()):
        raise StageError(f"{home} is not empty and is not a Cube adapter install; choose an empty directory", EXIT_CONFIG)


def _progress(args, text: str) -> None:
    if not getattr(args, "quiet", False):
        print(f"[sxc cube-install] {text}", file=sys.stderr, flush=True)


def _mark_rest(res: Result) -> None:
    have = {c.id for c in res.checks}
    for step in STEPS:
        if f"cube_install:{step}" not in have:
            res.add(f"cube_install:{step}", "not_run", "", reason="an earlier step did not pass")


def _next_action(res: Result) -> str:
    failed = next((c.id for c in res.checks if c.status != "pass"), "")
    return {
        "": "run `sxc.py cube` to generate and serve the model",
        "cube_install:npm_ci": "read cube_install/npm-ci.log, fix npm registry access, then rerun `sxc.py cube-install`",
        "cube_install:native_sha256": "the native module was not accepted: check access to the Cube GitHub release "
                                      "(or pass a verified --native-file) and rerun `sxc.py cube-install`",
        "cube_install:extract": "the native tarball layout is unexpected and was not used: report it and do not bypass the check",
        "cube_install:smoke": "node could not load the adapter: check Node.js (>= 20, same architecture), then rerun "
                              "`sxc.py cube-install --force`",
    }.get(failed, "rerun `sxc.py cube-install --force`")


def run(cfg, args, out: Path) -> Result:
    res = Result("cube_install")
    version = cfg.cube.version if cfg else config.CUBE_VERSION
    home = Path(getattr(args, "home", "") or cube_run.cube_home(version)).expanduser().resolve()
    key = platform_key()
    asset = pinned_asset(load_pins(), key)
    files = adapter_hashes()
    if not getattr(args, "force", False):
        rec = installed_record(home, key, asset, files)
        if rec is not None:
            res.add("cube_install:already_installed", "pass", "", platform=key, cube_version=rec.get("cube_version"),
                    index_node_sha256=rec["native"].get("index_node_sha256"))
            res.next_safe_action = "run `sxc.py cube` (use --force to reinstall the adapter)"
            return res
    native_file = getattr(args, "native_file", "") or ""
    if native_file and not Path(native_file).is_file():
        raise StageError(f"--native-file {native_file} is not a file", EXIT_CONFIG)
    check_home(home)
    node, npm = _tools()
    node_version = check_node(node, key)

    home.mkdir(parents=True, exist_ok=True)
    for stale in (RECORD, "server.js"):
        (home / stale).unlink(missing_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(cube_run.ADAPTER_DIR / name, home / name)
    logs = Path(out) / "cube_install"
    logs.mkdir(parents=True, exist_ok=True)
    _progress(args, "npm ci --ignore-scripts from the pinned lockfile (a few minutes on a first install)")
    try:
        code = npm_ci(npm, home, logs / "npm-ci.log")
    except (OSError, subprocess.TimeoutExpired) as e:
        reason = "timeout" if isinstance(e, subprocess.TimeoutExpired) else type(e).__name__
        res.add("cube_install:npm_ci", "error", "", reason=reason, log="cube_install/npm-ci.log")
        code = None
    if code is not None:
        res.add("cube_install:npm_ci", "pass" if code == 0 else "fail", "", exit_code=code, log="cube_install/npm-ci.log",
                packages={name: _package_version(home, name) for name in PACKAGES})
    if code == 0:
        _progress(args, f"native module {key}: " + ("verifying --native-file" if native_file else "downloading and verifying"))
        native = install_native(res, home, asset, native_file)
        if native is not None:
            sm = smoke(node, home)
            ok = sm.pop("ok")
            res.add("cube_install:smoke", "pass" if ok else "fail", "", node=node_version, **sm)
            if ok:
                shutil.copyfile(cube_run.ADAPTER_DIR / "server.js", home / "server.js")
                record = {"installer": f"semantic-crosscheck {VERSION}", "cube_version": config.CUBE_VERSION,
                          "platform": key, "node": node_version,
                          "packages": {name: _package_version(home, name) for name in PACKAGES},
                          "native": native, "files_sha256": files,
                          "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                (home / RECORD).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8", newline="\n")
    _mark_rest(res)
    res.next_safe_action = _next_action(res)
    return res
