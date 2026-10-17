"""Optional full-pipeline runtime binding; never stores credentials, paths or raw probe output.

source.runtime_evidence opts every data stage into the same contract, including build:
both PostgreSQL endpoint roles, Node, Java, the HermiT classpath used by owlready2,
and the complete selected Cube installation must be available at both boundaries.
This is bounded observed evidence, not artifact attestation or a shared DB snapshot.
No runtime probe, subprocess or installation discovery runs when the option is off.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from . import cube_run, db, owl, procutil
from .result import EXIT_ENV, StageError

DATA_STAGES = ('build', 'reason', 'cube', 'verify')
UNBOUND_NOTE = ('runtime evidence is unbound: actual PostgreSQL, Node, Java, HermiT and selected Cube installation '
                'were not bound; local engine/package metadata alone does not establish the executing runtime')
BOUND_NOTE = ('runtime evidence binds observed full-pipeline versions and artifact hashes at boundaries; '
              'it is not runtime attestation, a shared MVCC snapshot or proof against changes between probes')
PG_SETTINGS = ('TimeZone', 'DateStyle', 'IntervalStyle', 'server_encoding', 'client_encoding', 'lc_collate',
               'lc_ctype', 'standard_conforming_strings', 'extra_float_digits', 'bytea_output',
               'default_text_search_config', 'search_path', 'transform_null_equals', 'collation_provider')
_PG_DATABASE_COLUMNS = {'lc_collate': 'datcollate', 'lc_ctype': 'datctype',
                        'collation_provider': 'datlocprovider::text'}
PG_NULLABLE_METADATA = ('datlocale', 'daticurules', 'datcollversion', 'pg_database_collation_actual_version(oid)')
# SQL is fixed, reads server metadata only, and is never provided by configuration.
PG_SQL = ('SELECT current_setting(\'server_version_num\'), '
          + ', '.join(_PG_DATABASE_COLUMNS.get(name, "current_setting('" + name + "')") for name in PG_SETTINGS)
          + ', ' + ', '.join(PG_NULLABLE_METADATA) + ' '
            'FROM pg_catalog.pg_database WHERE datname = current_database()')
PROBE_TIMEOUT_S = 5
MAX_OUTPUT_BYTES = 8192
MAX_METADATA_BYTES = 16 << 20
MAX_FILE_BYTES = 512 << 20
MAX_TREE_BYTES = 4 << 30
MAX_TREE_FILES = 100000
MAX_CLASSPATH_FILES = 10000
MAX_CLASSPATH_BYTES = 512 << 20
_HASH = re.compile(r'[0-9a-f]{64}')
_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){1,3}(?:[-+_][0-9A-Za-z][0-9A-Za-z._+-]{0,47})?')
_PACKAGES = ('@cubejs-backend/server-core', '@cubejs-backend/postgres-driver', '@cubejs-backend/native', 'express')


class RuntimeEvidenceError(StageError):
    """Only fixed reason identifiers reach result files and debug tracebacks."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('source.runtime_evidence: actual runtime could not be bound (' + reason + ')', EXIT_ENV)


def configured(cfg):
    return cfg is not None and getattr(cfg.source, 'runtime_evidence', False) is True


def _digest(value, domain=b'sxc-runtime-v1\0'):
    return hashlib.sha256(domain + json.dumps(value, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=True).encode('utf-8')).hexdigest()


def _connect(env_name):
    # db.connect has no connection deadline. Reuse its parser/TLS semantics, with a
    # socket deadline as well as the transaction's bounded statement timeout.
    import pg8000.native
    dsn = os.environ.get(env_name)
    if not dsn:
        raise RuntimeEvidenceError('postgres_unavailable')
    return pg8000.native.Connection(**db._parse(dsn, env_name), timeout=PROBE_TIMEOUT_S)


def _postgres(env_name, timeout_ms):
    conn = None
    try:
        conn = _connect(env_name)
        with db.read_only(conn, min(timeout_ms, PROBE_TIMEOUT_S * 1000)):
            rows = conn.run(PG_SQL)
            if (not isinstance(rows, (list, tuple)) or len(rows) != 1
                    or not isinstance(rows[0], (list, tuple))
                    or len(rows[0]) != 1 + len(PG_SETTINGS) + len(PG_NULLABLE_METADATA)):
                raise RuntimeEvidenceError('postgres_invalid_metadata')
            row = rows[0]
            if type(row[0]) is not str or not re.fullmatch(r'[0-9]{6,8}', row[0]) or int(row[0]) < db.MIN_SERVER_VERSION:
                raise RuntimeEvidenceError('postgres_invalid_version')
            values = row[1:]
            if any((type(v) is not str or not v or len(v.encode('utf-8')) > 4096)
                   for v in values[:-len(PG_NULLABLE_METADATA)]):
                raise RuntimeEvidenceError('postgres_invalid_metadata')
            if any(v is not None and (type(v) is not str or len(v.encode('utf-8')) > 4096)
                   for v in values[-len(PG_NULLABLE_METADATA):]):
                raise RuntimeEvidenceError('postgres_invalid_metadata')
            return {'server_version_num': int(row[0]), 'settings_sha256': _digest(values, b'sxc-pg-settings-v1\0')}
    except RuntimeEvidenceError:
        raise
    except Exception:
        raise RuntimeEvidenceError('postgres_probe_failed') from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                raise RuntimeEvidenceError('postgres_cleanup_failed') from None


def _probe_env():
    # Version probes must not preload Node modules/Java agents or inherit DSNs.
    keep = ('PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'LANG', 'LC_ALL',
            'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH')
    return {key: value for key, value in os.environ.items() if key.upper() in keep
            and not re.search(r'postgres(?:ql)?://', value, re.IGNORECASE)}


class _WindowsProbeJob:
    """An unnamed job owning only this probe and descendants created after assignment."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeEvidenceError('version_tree_unavailable')

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise RuntimeEvidenceError('version_tree_unavailable')

    def close(self):
        try:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise RuntimeEvidenceError('version_cleanup_failed')
        finally:
            if not self.api.CloseHandle(self.handle):
                raise RuntimeEvidenceError('version_cleanup_failed')


def _available_pipe_chunk(stream):
    """Read only bytes already available; None means wait and b'' means EOF."""
    if os.name == 'nt':
        import _winapi
        import msvcrt
        try:
            available, _remaining = _winapi.PeekNamedPipe(msvcrt.get_osfhandle(stream.fileno()), 0)
        except OSError as problem:
            if problem.winerror in (109, 232):  # ERROR_BROKEN_PIPE / ERROR_NO_DATA
                return b''
            raise
        return os.read(stream.fileno(), min(1024, available)) if available else None
    import select
    ready, _, _ = select.select([stream], [], [], 0)
    return os.read(stream.fileno(), 1024) if ready else None


def _capture_version(argv):
    """Literal argv, one bounded nonblocking reader/deadline and a private child tree."""
    process = tree = None
    output = bytearray()
    deadline = time.monotonic() + PROBE_TIMEOUT_S

    try:
        if os.name == 'nt':
            tree = _WindowsProbeJob()
            platform_options = {'creationflags': subprocess.CREATE_NO_WINDOW}
        else:
            platform_options = {'start_new_session': True}
        process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=_probe_env(), shell=False, bufsize=0,
                                   **platform_options)
        if tree is not None:
            tree.assign(process)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeEvidenceError('version_probe_failed')
            chunk = _available_pipe_chunk(process.stdout)
            if chunk == b'':
                if process.wait(timeout=remaining) != 0:
                    raise RuntimeEvidenceError('version_probe_failed')
                return bytes(output)
            if chunk is not None:
                if len(output) + len(chunk) > MAX_OUTPUT_BYTES:
                    raise RuntimeEvidenceError('version_probe_failed')
                output.extend(chunk)
            else:
                time.sleep(min(.01, remaining))
    except RuntimeEvidenceError:
        raise
    except Exception:
        raise RuntimeEvidenceError('version_probe_failed') from None
    finally:
        try:
            try:
                if tree is not None:
                    tree.close()
                elif process is not None and os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=1)
                    # No background read owns this unbuffered pipe, so closing it
                    # cannot wait for a descendant's inherited writer handle.
                    process.stdout.close()
        except Exception:
            raise RuntimeEvidenceError('version_cleanup_failed') from None


def _version(kind):
    executable = procutil.which('node') if kind == 'node' else owl.java_executable()
    if not executable:
        raise RuntimeEvidenceError(kind + '_missing')
    output = _capture_version([executable, '--version' if kind == 'node' else '-version'])
    try:
        text = output.decode('ascii').strip()
    except (UnicodeError, AttributeError):
        raise RuntimeEvidenceError(kind + '_invalid_version') from None
    if kind == 'node':
        match = re.fullmatch(r'v?([0-9]+\.[0-9]+\.[0-9]+)', text)
    else:
        match = re.search(r'(?m)^(?:openjdk|java)(?: version)? [\"]?('
                          + _VERSION.pattern + r')[\"]?(?:\s|$)', text)
    if not match:
        raise RuntimeEvidenceError(kind + '_invalid_version')
    version = match.group(1)
    if kind == 'node' and int(version.split('.')[0]) < cube_run.MIN_NODE:
        raise RuntimeEvidenceError('node_unsupported_version')
    return version


def _file_hash(path, max_bytes=MAX_FILE_BYTES):
    path = Path(path)
    if _linked(path) or not path.is_file() or path.stat().st_size > max_bytes:
        raise RuntimeEvidenceError('artifact_unavailable')
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        while True:
            chunk = stream.read(min(1 << 20, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeEvidenceError('artifact_size_limit')
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or size != before.st_size:
            raise RuntimeEvidenceError('artifact_changed_during_probe')
    return digest.hexdigest()


def _json_file(path):
    path = Path(path)
    if _linked(path) or path.stat().st_size > MAX_METADATA_BYTES:
        raise RuntimeEvidenceError('artifact_invalid_metadata')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeEvidenceError('artifact_invalid_metadata')
    return value


def _linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def _hermit():
    # This is owlready2's actual sync_reasoner_hermit classpath, not a guessed
    # distribution path. Its directory precedes the JAR and can contain patched
    # loose classes: bind every byte and the entry order, including those classes.
    import owlready2.reasoning as reasoning
    entries = [Path(item) for item in reasoning._HERMIT_CLASSPATH.split(os.pathsep) if item]
    jars = [path for path in entries if path.suffix.lower() == '.jar']
    if len(jars) != 1 or any(not p.exists() or (not p.is_dir() and p not in jars) for p in entries):
        raise RuntimeEvidenceError('hermit_classpath_unsupported')
    visited = count = total = 0
    digest = hashlib.sha256(b'sxc-hermit-classpath-v1\0')
    def unreadable(_problem):
        raise RuntimeEvidenceError('hermit_classpath_unreadable') from None
    for index, path in enumerate(entries):
        digest.update(str(index).encode('ascii') + b'\0')
        if path.is_dir():
            if _linked(path):
                raise RuntimeEvidenceError('hermit_classpath_unsupported')
            for folder, dirs, files in os.walk(path, followlinks=False, onerror=unreadable):
                dirs.sort()
                files.sort()
                visited += 1 + len(dirs) + len(files)
                if visited > MAX_CLASSPATH_FILES:
                    raise RuntimeEvidenceError('hermit_classpath_limit')
                for name in dirs + files:
                    entry = Path(folder) / name
                    if _linked(entry):
                        raise RuntimeEvidenceError('hermit_classpath_unsupported')
                for name in files:
                    entry = Path(folder) / name
                    count += 1
                    total += entry.stat().st_size
                    if total > MAX_CLASSPATH_BYTES:
                        raise RuntimeEvidenceError('hermit_classpath_limit')
                    digest.update(entry.relative_to(path).as_posix().encode('utf-8') + b'\0')
                    digest.update(bytes.fromhex(_file_hash(entry)))
        else:
            count += 1
            total += path.stat().st_size
            if total > MAX_CLASSPATH_BYTES:
                raise RuntimeEvidenceError('hermit_classpath_limit')
            digest.update(b'jar\0' + bytes.fromhex(_file_hash(path)))
    return {'jar_sha256': _file_hash(jars[0]), 'classpath_sha256': digest.hexdigest(), 'file_count': count}


def _cube(cfg):
    home = cube_run.cube_home(cfg.cube.version)
    if cube_run.install_state(home) != 'ok':
        raise RuntimeEvidenceError('cube_install_unavailable')
    expected = cube_run.adapter_hashes()
    actual = {name: _file_hash(home / name) for name in cube_run.ADAPTER_FILES}
    if actual != expected:
        raise RuntimeEvidenceError('cube_adapter_changed')
    record = _json_file(home / cube_run.RECORD)
    native = _file_hash(home / cube_run.INDEX_NODE)
    if native != record.get('native', {}).get('index_node_sha256'):
        raise RuntimeEvidenceError('cube_native_changed')
    lock = _json_file(home / 'package-lock.json').get('packages')
    if not isinstance(lock, dict):
        raise RuntimeEvidenceError('cube_invalid_metadata')
    versions = {}
    for name in _PACKAGES:
        package = _json_file(home / 'node_modules' / name / 'package.json')
        version = package.get('version')
        if (not isinstance(version, str) or not _VERSION.fullmatch(version)
                or package.get('name') != name or version != lock.get('node_modules/' + name, {}).get('version')):
            raise RuntimeEvidenceError('cube_package_version_mismatch')
        versions[name] = version
    # Hash actual installed bytes (including transitive dependencies), with only
    # relative labels fed into the hash. The logical file names never leave here.
    digest = hashlib.sha256(b'sxc-cube-installation-v1\0')
    count = total = directory_count = 0
    modules = home / 'node_modules'
    if _linked(modules):
        raise RuntimeEvidenceError('cube_layout_unsupported')
    def unreadable(_problem):
        raise RuntimeEvidenceError('cube_installation_unreadable') from None
    for folder, dirs, files in os.walk(modules, followlinks=False, onerror=unreadable):
        directory_count += 1
        if directory_count > MAX_TREE_FILES:
            raise RuntimeEvidenceError('cube_installation_limit')
        dirs.sort()
        files.sort()
        for name in dirs:
            if _linked(Path(folder) / name):
                raise RuntimeEvidenceError('cube_layout_unsupported')
        for name in files:
            path = Path(folder) / name
            # npm's .bin links are launch conveniences; this adapter uses node and
            # server.js directly. No other symlinks are accepted.
            if '.bin' in path.relative_to(modules).parts:
                continue
            count += 1
            total += path.stat().st_size
            if count > MAX_TREE_FILES or total > MAX_TREE_BYTES:
                raise RuntimeEvidenceError('cube_installation_limit')
            digest.update(path.relative_to(home).as_posix().encode('utf-8') + b'\0')
            digest.update(bytes.fromhex(_file_hash(path)))
    if not count:
        raise RuntimeEvidenceError('cube_install_unavailable')
    return {'installation_sha256': _digest({'adapter': actual, 'modules': digest.hexdigest(), 'versions': versions}),
            'package_versions': versions, 'native_sha256': native, 'file_count': count}


def current_probe(cfg):
    if not configured(cfg):
        return {'schema_version': 1, 'binding': 'unbound', 'scope_note': UNBOUND_NOTE}
    try:
        if any(os.environ.get(name) for name in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS')):
            raise RuntimeEvidenceError('java_environment_unsupported')
        source = _postgres(cfg.source.dsn_env, cfg.source.statement_timeout_ms)
        cube = _postgres(cfg.cube_dsn_env(), cfg.source.statement_timeout_ms)
        if source != cube:
            raise RuntimeEvidenceError('postgres_endpoint_mismatch')
        components = {'postgres': {'source': source, 'cube': cube},
                      'node_version': _version('node'), 'java_version': _version('java'),
                      'hermit': _hermit(), 'cube': _cube(cfg)}
        return {'schema_version': 1, 'binding': 'bound', 'scope': 'full_pipeline',
                'sha256': _digest(components), 'components': components}
    except RuntimeEvidenceError:
        raise
    except Exception:
        raise RuntimeEvidenceError('runtime_probe_failed') from None


def begin(cfg, stage):
    if stage not in DATA_STAGES:
        return {'schema_version': 1, 'binding': 'not_applicable'}
    probe = current_probe(cfg)
    if probe['binding'] != 'bound':
        return probe
    return {'schema_version': 1, 'binding': 'bound', 'scope': 'full_pipeline', 'stage': stage,
            'start_sha256': probe['sha256'], 'components': probe['components']}


def finish(cfg, result, start):
    if result.stage == 'report':
        return result
    if result.stage not in DATA_STAGES:
        result.runtime_evidence = {'schema_version': 1, 'binding': 'not_applicable'}
        return result
    if not configured(cfg):
        result.runtime_evidence = {'schema_version': 1, 'binding': 'unbound'}
        result.not_proven.append(UNBOUND_NOTE)
        return result
    result.runtime_evidence = dict(start or {})
    try:
        end = current_probe(cfg)
        stable = (start.get('binding') == 'bound' and end.get('binding') == 'bound'
                  and start.get('scope') == end.get('scope') == 'full_pipeline'
                  and start.get('start_sha256') == end.get('sha256'))
        result.runtime_evidence.update(binding='bound' if stable else 'changed', stable=stable,
                                       end_sha256=end.get('sha256'), components=end.get('components', {}))
        result.add('runtime_evidence:stable', 'pass' if stable else 'fail', 'source.runtime_evidence',
                   reason='stable' if stable else 'changed')
    except RuntimeEvidenceError as problem:
        result.runtime_evidence.update(binding='error', stable=False, reason=problem.reason)
        result.add('runtime_evidence:stable', 'error', 'source.runtime_evidence', reason=problem.reason)
    result.not_proven.append(BOUND_NOTE)
    if not result.runtime_evidence.get('stable'):
        result.next_safe_action = ('the actual runtime could not be held stable: repair the configured runtime and '
                                   'rerun build, reason, cube and verify before reusing cached comparisons')
    return result


def evidence_staleness(recorded, current):
    if current.get('binding') == 'unbound':
        return None
    if current.get('binding') != 'bound':
        return 'runtime_evidence_unavailable'
    if (not isinstance(recorded, dict) or recorded.get('binding') != 'bound' or recorded.get('stable') is not True
            or recorded.get('scope') != 'full_pipeline'):
        return 'runtime_evidence_missing'
    fingerprint = current.get('sha256')
    if (not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint)
            or recorded.get('start_sha256') != fingerprint or recorded.get('end_sha256') != fingerprint):
        return 'runtime_evidence_changed'
    return None
