"""A fixed synthetic source-contract replay; no engines or operational receipts.

The fixture declares operations only. The separately authored literal oracle
below must never be derived from the source classifier being checked.
"""
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from . import config, expect, knowledge_sources, provenance, report
from .result import EXIT_CONFIG, Result, StageError, inputs_fingerprint

_SCHEMA = 'sxc/knowledge-evolution-fixture/v1'
_OPERATIONS = ('normal', 'change_spec', 'delete_spec', 'restore_spec', 'retire_case')
_ORACLE = (
    ('T0', 'current', 'pass', 'pass'),
    ('T1', 'changed', 'not_run', 'pass'),
    ('T2', 'missing', 'not_run', 'pass'),
    ('T3', 'current', 'not_run', 'pass'),
    ('T4', 'retired', 'not_run', 'not_run'),
)
_RAW = b'Fixed synthetic definition v1\n'
_TIME = '2000-01-01T00:00:00+0000'
_UNBOUND = {'schema_version': 1, 'binding': 'unbound'}
_BASE = '''version = 1
[source]
schemas = ["synthetic"]
[entities.item]
table = "synthetic.item"
key = ["id"]
[[gold]]
id = "q01"
sql = "query.sql"
key = "id"
[gold.cube_query]
dimensions = ["item.id"]
[gold.map]
id = "item.id"
'''
_CASE = '''[[gold.expect]]
source = "authored"
spec = "fixed synthetic contract"
params = {}
rows = [{ id = 1 }]
spec_source = { file = "definition.md", sha256 = "%s" }
''' % hashlib.sha256(_RAW).hexdigest()


def fixture_bytes():
    """Read only the packaged fixture, with a hard byte bound before parsing."""
    with files(__package__).joinpath('resources/knowledge-evolution-v1.json').open('rb') as stream:
        return stream.read(65537)


def _fixture(raw):
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise ValueError()
    # Exact comparison also closes keys, order, IDs, types and operation names.
    def closed_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=closed_object)
    expected = {'schema': _SCHEMA, 'checkpoints': [
        {'id': f'T{i}', 'operation': op} for i, op in enumerate(_OPERATIONS)]}
    if value != expected:
        raise ValueError()
    return value


def synthetic_project(root):
    """Create the sole controlled project inside an invocation-owned temp root."""
    root = Path(root)
    (root / 'definition.md').write_bytes(_RAW)
    (root / 'query.sql').write_bytes(b'SELECT 1 AS id\n')
    path = root / 'crosscheck.toml'
    path.write_text(_BASE + _CASE, encoding='utf-8', newline='\n')
    return config.load(path)


def synthetic_receipts(inputs):
    """In-memory harness evidence, never persisted as operational execution."""
    receipts = {}
    for i, stage in enumerate(('build', 'cube', 'verify')):
        stamp = f'2000-01-01T00:00:0{i}+0000'
        receipts[stage] = {'status': 'pass', 'checks': [{'id': 'synthetic', 'status': 'pass'}],
                           'started_at': stamp, 'finished_at': stamp, 'inputs': dict(inputs),
                           'provenance': dict(_UNBOUND), 'runtime_evidence': dict(_UNBOUND)}
    return receipts


def _observe(root):
    cfg = synthetic_project(root)
    receipts = synthetic_receipts(inputs_fingerprint(cfg))
    original = json.dumps(receipts, sort_keys=True)
    retained = Result('expect', started_at=_TIME, finished_at=_TIME)
    case = retained.add('expect:q01[0]', 'pass')
    observations = {}
    for i, operation in enumerate(_OPERATIONS):
        tid = f'T{i}'
        observation = {}
        observations[tid] = observation
        try:
            if operation == 'change_spec':
                (root / 'definition.md').write_bytes(b'Fixed synthetic definition v2\n')
            elif operation == 'delete_spec':
                (root / 'definition.md').unlink()
            elif operation == 'restore_spec':
                (root / 'definition.md').write_bytes(_RAW)
            elif operation == 'retire_case':
                Path(cfg.path).write_text(_BASE, encoding='utf-8', newline='\n')
                cfg = config.load(cfg.path)
            if operation == 'retire_case':
                empty = expect.run(cfg, SimpleNamespace(), root / 'unused-output')
                no_cases = (not any(g.expect for g in cfg.gold)
                            and empty.status == 'not_run'
                            and any(c.id == 'expect:cases' and c.status == 'not_run'
                                    and c.detail.get('reason') == 'no_cases' for c in empty.checks))
                observation['source'] = 'retired' if no_cases else 'unavailable'
                observation['empty_expect'] = empty.status
            else:
                cases = [(gi, g, ci, c) for gi, g in enumerate(cfg.gold) for ci, c in enumerate(g.expect)]
                fresh = knowledge_sources.capture(cfg, cases)
                final = knowledge_sources.finish(fresh, retained)
                observation['source'] = final.public()['items'][0]['state']
            observation['case'] = case.status
            stale = report.staleness(receipts, inputs_fingerprint(cfg), dict(_UNBOUND), dict(_UNBOUND), {})
            for stage in report.required_stages(cfg):
                receipt = receipts.get(stage)
                observation[stage] = report.counted_status(
                    report.stage_status(receipt) if receipt else 'not_run', True, stale.get(stage))
            if (json.dumps(receipts, sort_keys=True) != original or retained.started_at != _TIME
                    or retained.finished_at != _TIME or retained.checks[0] is not case):
                observation.clear()
        except Exception:
            # Each failed observation retains its complete original oracle slots.
            observation.clear()
    return observations


def evaluate():
    """Run exactly the bundled replay and compare to independent literal contracts."""
    res = Result('knowledge_evolution')
    raw = b''
    observed = {}
    try:
        raw = fixture_bytes()
        _fixture(raw)
        with tempfile.TemporaryDirectory(prefix='sxc_evolution_') as directory:
            observed = _observe(Path(directory))
    except Exception:
        pass  # Controlled errors below; never publish exception messages or paths.
    engine = ''
    try:
        engine = provenance.engine_fingerprint()
    except Exception:
        observed = {}  # No engine binding means no checkpoint can claim success.
    for tid, source, case, operational in _ORACLE:
        contracts = [('source', source), ('case', case),
                     ('build', operational), ('cube', operational), ('verify', operational)]
        if tid == 'T4':
            contracts.append(('empty_expect', 'not_run'))
        for contract, expected in contracts:
            actual = observed.get(tid, {}).get(contract, 'unavailable')
            allowed = ({'current', 'changed', 'missing', 'blocked', 'unavailable', 'retired'}
                       if contract == 'source' else {'pass', 'fail', 'error', 'not_run', 'unavailable'})
            if actual not in allowed:
                actual = 'unavailable'
            status = 'error' if actual == 'unavailable' else 'pass' if actual == expected else 'fail'
            res.add(f'knowledge_evolution:{tid}:{contract}', status, '', observed=actual)
    info = {'mode': 'synthetic_contract_replay', 'fixture_sha256': hashlib.sha256(raw).hexdigest(),
            'engine_sha256': engine, 'engine_state': 'current' if engine else 'unavailable',
            'checkpoints': len(_ORACLE),
            'contracts': len(res.checks), 'passed': sum(c.status == 'pass' for c in res.checks),
            'engines_executed': 0, 'database_queries': 0, 'model_calls': 0,
            'semantic_coverage_measured': False}
    res.inputs = {'knowledge_evolution': info}
    res.not_proven = [
        'Synthetic contract replay checks harness behavior only; business truth and semantic coverage are not measured.',
        'No database, engine or model execution; no new operational or expectation execution receipt.',
        'Exact restoration can make a source current without promoting retained not_run evidence.',
        'Source-only edits leave operational fingerprints current; case retirement changes configuration and stales old receipts.',
    ]
    return res


def prepare_run(args, out):
    if getattr(args, 'config', None) is not None or getattr(args, 'spec', None) is not None:
        raise StageError('knowledge-evolution uses only a fixed bundled fixture', EXIT_CONFIG)


def run(cfg=None, args=None, out=None):
    if cfg is not None:
        raise StageError('knowledge-evolution rejects configuration', EXIT_CONFIG)
    if args is not None:
        prepare_run(args, out)
    return evaluate()
