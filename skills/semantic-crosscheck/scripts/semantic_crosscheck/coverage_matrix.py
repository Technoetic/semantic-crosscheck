"""Read-only, zero-path platform and CI verification coverage matrix for doctor --coverage.

Selectively adapts the explicit measured-vs-unmeasured scope disclosure pattern
informed by ai3inc/foldboost (commit ff7ad2208c, Apache-2.0), implemented from
scratch in the Python standard library. Reports declared support, pinned Cube
native targets, CI unit/integration verification boundaries, and structured Cube
install near-miss diagnostics without emitting raw local paths or secrets.
"""

import platform
import sys
from pathlib import Path

from . import config, cube_install, cube_run
from .result import StageError

SCHEMA = "semantic-crosscheck/capability-coverage/v1"
STATUSES = ("ci_integrated", "ci_unit_only", "supported", "not_measured", "unsupported")

# Explicit per-platform capability and CI verification matrix matching README.md
# and .github/workflows/tests.yml. Never conflates declared support with live
# CI integration testing.
PLATFORM_MATRIX: dict[str, dict[str, object]] = {
    "linux-x64-glibc": {
        "os": "linux",
        "arch": "x64",
        "libc": "glibc",
        "glibc_min": "2.34",
        "cube_native_pin": "supported",
        "ci_unit": "ci_unit_only",
        "pg17_integration": "ci_integrated",
        "reason_hermit_jena": "ci_integrated",
        "cube_e2e_integration": "ci_integrated",
        "notes": "Full CI integration target (Ubuntu x64, PG 17, Temurin Java 21, Jena 6.2.0, Node 22, Cube 1.8.3).",
    },
    "linux-arm64-glibc": {
        "os": "linux",
        "arch": "arm64",
        "libc": "glibc",
        "glibc_min": "2.30",
        "cube_native_pin": "supported",
        "ci_unit": "not_measured",
        "pg17_integration": "not_measured",
        "reason_hermit_jena": "not_measured",
        "cube_e2e_integration": "not_measured",
        "notes": "Cube native tarball is SHA-256 pinned (glibc >= 2.30), but Linux arm64 is not exercised in CI.",
    },
    "win32-x64-unknown": {
        "os": "windows",
        "arch": "x64",
        "libc": "msvc",
        "glibc_min": None,
        "cube_native_pin": "supported",
        "ci_unit": "ci_unit_only",
        "pg17_integration": "not_measured",
        "reason_hermit_jena": "not_measured",
        "cube_e2e_integration": "not_measured",
        "notes": "CI runs unit tests (Python 3.11/3.13); full PG 17 / Cube / HermiT integration is not run in CI.",
    },
    "darwin-x64-unknown": {
        "os": "macos",
        "arch": "x64",
        "libc": "apple",
        "glibc_min": None,
        "cube_native_pin": "supported",
        "ci_unit": "ci_unit_only",
        "pg17_integration": "not_measured",
        "reason_hermit_jena": "not_measured",
        "cube_e2e_integration": "not_measured",
        "notes": "CI runs unit tests on macos-15-intel (Python 3.11/3.13); Cube/PG integration is not run in CI.",
    },
    "darwin-arm64-unknown": {
        "os": "macos",
        "arch": "arm64",
        "libc": "apple",
        "glibc_min": None,
        "cube_native_pin": "supported",
        "ci_unit": "ci_unit_only",
        "pg17_integration": "not_measured",
        "reason_hermit_jena": "not_measured",
        "cube_e2e_integration": "not_measured",
        "notes": "CI runs unit tests on macos-15 arm64 (Python 3.11/3.13); Cube/PG integration is not run in CI.",
    },
    "linux-musl-or-unpinned": {
        "os": "linux",
        "arch": "any",
        "libc": "musl/other",
        "glibc_min": None,
        "cube_native_pin": "unsupported",
        "ci_unit": "not_measured",
        "pg17_integration": "not_measured",
        "reason_hermit_jena": "not_measured",
        "cube_e2e_integration": "unsupported",
        "notes": "musl and unpinned architectures have no Cube 1.8.3 native pin; pure Python stages remain unmeasured.",
    },
}


def detect_host_platform(
    system: str | None = None,
    machine: str | None = None,
    libc: str | None = None,
) -> dict[str, object]:
    """Identify the current host platform key without leaking paths or hostnames."""
    sys_name = sys.platform if system is None else system
    mach_name = (platform.machine() if machine is None else machine).lower()
    glibc_ver = None
    if sys_name.startswith("linux"):
        libc_name = platform.libc_ver()[0] if libc is None else libc
        if libc_name == "glibc" and system is None and libc is None:
            gv = cube_install.glibc_version()
            glibc_ver = f"{gv[0]}.{gv[1]}" if gv else None
    else:
        libc_name = "unknown"
    try:
        key = cube_install.platform_key(sys_name, mach_name, libc_name if sys_name.startswith("linux") else None)
        supported = True
    except StageError:
        key = f"{sys_name}-{mach_name or 'unknown'}-{libc_name or 'unknown'}"
        supported = False
    matrix_key = key if key in PLATFORM_MATRIX else "linux-musl-or-unpinned"
    return {
        "platform_key": key,
        "cube_native_supported": supported,
        "matrix_entry": matrix_key,
        "python": platform.python_version(),
        "glibc": glibc_ver,
    }


def build_coverage_report(
    home: Path | None = None,
    *,
    adapter_dir: Path = cube_run.ADAPTER_DIR,
    system: str | None = None,
    machine: str | None = None,
    libc: str | None = None,
) -> dict[str, object]:
    """Build a read-only, zero-path coverage and Cube install diagnostic report."""
    host = detect_host_platform(system=system, machine=machine, libc=libc)
    resolved_home = cube_run.cube_home(config.CUBE_VERSION) if home is None else Path(home)
    pinned_sha: str | None = None
    plat_key = str(host["platform_key"]) if host["cube_native_supported"] else None
    if plat_key is not None:
        try:
            pinned_sha = cube_install.pinned_asset(cube_install.load_pins(adapter_dir), plat_key)["sha256"]
        except (StageError, OSError, ValueError):
            pinned_sha = None
    install_diag = cube_run.diagnose_install(
        resolved_home,
        adapter_dir=adapter_dir,
        platform_key=plat_key,
        pinned_tarball_sha256=pinned_sha,
    )
    entry = PLATFORM_MATRIX[str(host["matrix_entry"])]
    return {
        "schema": SCHEMA,
        "status_legend": list(STATUSES),
        "cube_version": config.CUBE_VERSION,
        "host": host,
        "host_capabilities": {
            "cube_native_pin": entry["cube_native_pin"],
            "ci_unit": entry["ci_unit"],
            "pg17_integration": entry["pg17_integration"],
            "reason_hermit_jena": entry["reason_hermit_jena"],
            "cube_e2e_integration": entry["cube_e2e_integration"],
        },
        "cube_install_diagnostics": {
            "home_source": cube_run.home_label(),
            **install_diag,
        },
        "runtime_boundaries": {
            "java_11_itself": "not_measured",
            "java_ci_tested": "Temurin 21",
            "node_min": cube_install.MIN_NODE,
            "python_min": "3.11",
            "postgresql_min": 17,
        },
        "platforms": {k: dict(v) for k, v in PLATFORM_MATRIX.items()},
    }


def format_coverage_summary(report: dict[str, object]) -> str:
    """Concise multi-line human summary for doctor --coverage (zero raw paths)."""
    host = report["host"]
    caps = report["host_capabilities"]
    diag = report["cube_install_diagnostics"]
    if not isinstance(host, dict) or not isinstance(caps, dict) or not isinstance(diag, dict):
        raise TypeError("coverage report sections must be dictionaries")
    near = diag.get("near_misses") or []
    near_text = ", ".join(str(item) for item in near) if isinstance(near, list) and near else "none"
    lines = [
        f"  COVERAGE  host={host['platform_key']} python={host['python']} "
        f"cube_pin={caps['cube_native_pin']} ci_unit={caps['ci_unit']} "
        f"pg17_int={caps['pg17_integration']} cube_e2e={caps['cube_e2e_integration']}",
        f"  INSTALL   state={diag['state']} manifest_integrity={diag['manifest_integrity']} "
        f"near_misses={near_text}",
    ]
    return "\n".join(lines)
