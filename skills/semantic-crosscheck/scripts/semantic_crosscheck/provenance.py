"""Bounded evidence for an owner-maintained data revision; no raw probe data is retained.

The owner must advance the revision atomically with every relevant source change and
never reuse a revision. Equal boundary probes do not provide a shared MVCC snapshot
and cannot detect ABA changes when that owner contract is violated.
"""

from importlib import metadata
import hashlib
import json
from pathlib import Path
import sys

from . import db
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

DATA_STAGES = ('build', 'reason', 'cube', 'verify')
UNBOUND_NOTE = ('data revision is unbound: no owner-maintained revision was checked; separate database sessions '
                'and cached gold answers are not proven to observe one data version')
BOUND_NOTE = ('data revision binding relies on the owner advancing a non-reused revision atomically with every '
              'relevant change; boundary probes are not a shared MVCC snapshot and do not prove an ABA-free run')


class RevisionError(StageError):
    """Only fixed reason identifiers are exposed, including in debug tracebacks."""

    def __init__(self, reason: str, exit_code=EXIT_ENV):
        self.reason = reason
        super().__init__('source.data_revision_sql_file: revision check could not establish stable data (' + reason + ')', exit_code)


def configured(cfg) -> bool:
    return bool(cfg is not None and getattr(cfg.source, 'data_revision_sql_file', ''))


def _query_bytes(cfg) -> bytes:
    if not configured(cfg):
        return b''
    try:
        path = Path(cfg.source.data_revision_sql_file)
        if not path.is_absolute():
            path = cfg.path.parent / path
        data = path.read_bytes()
        if not data.decode('utf-8-sig').strip():
            raise ValueError()
        return data
    except Exception:
        raise RevisionError('query_unreadable', EXIT_CONFIG) from None


def revision_query_fingerprint(cfg) -> str:
    return hashlib.sha256(_query_bytes(cfg)).hexdigest() if configured(cfg) else ''


def _engine_files():
    package = Path(__file__).resolve().parent
    scripts = package.parent
    adapter = scripts.parent / 'adapters' / 'cube'
    return ([('python/' + path.name, path) for path in sorted(package.glob('*.py'))]
            + [('scripts/sxc.py', scripts / 'sxc.py')]
            + [('cube/' + name, adapter / name) for name in
               ('server.js', 'package.json', 'package-lock.json', 'native-sha256.json')])


def _dependency_versions():
    versions = {'python': '.'.join(map(str, sys.version_info[:3]))}
    for name in ('pg8000', 'owlready2', 'pyshacl', 'rdflib'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = 'not-installed'
    return versions


def engine_fingerprint() -> str:
    """Hash bounded runtime sources, adapter inputs and dependency metadata, using logical labels only."""
    digest = hashlib.sha256(b'sxc-engine-v1\0')
    try:
        for label, path in _engine_files():
            digest.update(label.encode('utf-8') + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(json.dumps(_dependency_versions(), sort_keys=True, separators=(',', ':')).encode('utf-8'))
    except Exception:
        raise StageError('verification engine fingerprint could not be read; repair the installation and rerun', EXIT_ENV) from None
    return digest.hexdigest()


def _read_revision(env_name: str, sql: str, timeout_ms: int) -> str:
    conn = prepared = None
    try:
        conn = db.connect(env_name)
        with db.read_only(conn, timeout_ms):
            # Extended-query preparation rejects multiple statements, as it does for gold SQL.
            prepared = conn.prepare(sql)
            rows = prepared.run()
            if (len(prepared.columns or []) != 1 or not isinstance(rows, (list, tuple)) or len(rows) != 1
                    or not isinstance(rows[0], (list, tuple)) or len(rows[0]) != 1
                    or type(rows[0][0]) is not str or not rows[0][0].strip()):
                raise RevisionError('invalid_text_scalar')
            return hashlib.sha256(b'sxc-data-revision-v1\0' + rows[0][0].encode('utf-8')).hexdigest()
    except RevisionError:
        raise
    except Exception:
        raise RevisionError('probe_failed') from None
    finally:
        # Cleanup errors are sanitized too: driver messages may quote SQL or row values.
        try:
            try:
                if prepared is not None:
                    prepared.close()
            finally:
                if conn is not None:
                    conn.close()
        except Exception:
            raise RevisionError('probe_cleanup_failed') from None


def current_probe(cfg) -> dict:
    """Fresh probes of both endpoint roles; result contains hashes and fixed identifiers only."""
    state = {'schema_version': 1, 'binding': 'unbound'}
    if not configured(cfg):
        return state
    raw_query = _query_bytes(cfg)
    sql = raw_query.decode('utf-8-sig')
    source = _read_revision(cfg.source.dsn_env, sql, cfg.source.statement_timeout_ms)
    cube = _read_revision(cfg.cube_dsn_env(), sql, cfg.source.statement_timeout_ms)
    if source != cube:
        raise RevisionError('endpoint_mismatch')
    state.update(binding='bound', revision_query_sha256=hashlib.sha256(raw_query).hexdigest(),
                 source_sha256=source, cube_sha256=cube)
    return state


def begin(cfg, stage: str) -> dict:
    """CLI preflight. Raises a sanitized StageError before a configured stage can run."""
    if stage not in DATA_STAGES:
        return {'schema_version': 1, 'binding': 'not_applicable', 'stage': stage}
    state = current_probe(cfg)
    if state['binding'] == 'bound':
        state = {'schema_version': 1, 'binding': 'bound', 'stage': stage,
                 'revision_query_sha256': state['revision_query_sha256'],
                 'start': {key: state[key] for key in ('source_sha256', 'cube_sha256')}}
    return state


def finish(cfg, res: Result, start: dict) -> Result:
    """CLI finalizer; a changed/error revision prevents reuse of even otherwise passing cached checks."""
    if res.stage == 'report':
        return res  # report.run owns its conditional current probe and provenance.
    if res.stage not in DATA_STAGES:
        res.provenance = {'schema_version': 1, 'binding': 'not_applicable'}
        return res
    res.provenance = dict(start)
    if not configured(cfg):
        res.provenance = {'schema_version': 1, 'binding': 'unbound'}
        if UNBOUND_NOTE not in res.not_proven:
            res.not_proven.append(UNBOUND_NOTE)
        return res
    try:
        end = current_probe(cfg)
        hashes = {key: end[key] for key in ('source_sha256', 'cube_sha256')}
        stable = (start.get('binding') == 'bound' and start.get('start') == hashes
                  and start.get('revision_query_sha256') == end['revision_query_sha256'])
        res.provenance.update(binding='bound' if stable else 'changed', end=hashes, stable=stable)
        res.add('data_revision:stable', 'pass' if stable else 'fail', 'source.data_revision_sql_file',
                reason='stable' if stable else 'changed', endpoint_roles=2)
    except RevisionError as problem:
        changed = problem.reason == 'endpoint_mismatch'
        res.provenance.update(binding='changed' if changed else 'error', stable=False, reason=problem.reason)
        res.add('data_revision:stable', 'fail' if changed else 'error', 'source.data_revision_sql_file', reason=problem.reason)
    if BOUND_NOTE not in res.not_proven:
        res.not_proven.append(BOUND_NOTE)
    if not res.provenance.get('stable'):
        res.next_safe_action = ('the data revision could not be held stable: do not reuse these comparisons or cached '
                                'answers; rerun build, reason, cube and verify on one stable owner-versioned dataset')
    return res


def evidence_staleness(recorded: dict, current: dict) -> str | None:
    """Return a fixed stale reason; never treat missing or unstable evidence as reusable."""
    if current.get('binding') == 'unbound':
        return None
    if current.get('binding') != 'bound':
        return 'data_revision_unavailable'
    if not isinstance(recorded, dict) or recorded.get('binding') != 'bound' or recorded.get('stable') is not True:
        return 'data_revision_missing'
    expected = {key: current.get(key) for key in ('source_sha256', 'cube_sha256')}
    if (recorded.get('start') != expected or recorded.get('end') != expected
            or recorded.get('revision_query_sha256') != current.get('revision_query_sha256')):
        return 'data_revision_changed'
    return None
