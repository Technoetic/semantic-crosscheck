"""Local, bounded freshness evidence for explicitly configured business inputs.

Hashes detect ordinary input/generator changes, not authenticity or approval.
Recomputed declarations protect against a saved passing subset; they do not
prove that SQL implements a business definition or that engines share a snapshot.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tomllib

from . import business_contracts, config, semantic_preflight as guarded
from .result import EXIT_CONFIG, Result, StageError
from .result_contract import validate_result

STAGE = guarded.STAGE
NOTE = ('Business contract evidence compares authored declarations only. Local hashes are not authenticity; '
        'reviewed is an assertion, not signed approval. Matching declarations do not prove SQL/business truth '
        'or a shared cross-engine snapshot.')
_TIME = '%Y-%m-%dT%H:%M:%S%z'
_GENERATOR_FILES = ('business_contracts.py', 'config.py', 'contract_evidence.py', 'naming.py',
                    'result.py', 'result_contract.py', 'semantic_preflight.py')


def generator_fingerprint():
    """Pin the local parser, evaluator, input guards and evidence implementation."""
    digest = hashlib.sha256()
    for name in _GENERATOR_FILES:
        digest.update(name.encode('ascii') + b'\0')
        digest.update(Path(__file__).with_name(name).read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def fingerprints(inputs):
    """Content plus exact selected file identities; retain no plaintext paths."""
    result = {name + '_sha256': hashlib.sha256(source.raw).hexdigest()
              for name, source in zip(('config', 'contract', 'plan'), inputs)}
    paths = [os.path.normcase(str(source.path.resolve(strict=True))) for source in inputs]
    result['selected_binding_sha256'] = hashlib.sha256(
        json.dumps(paths, ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
    result['generator_sha256'] = generator_fingerprint()
    return result


def protect_paths(paths, out):
    """Reject input aliases of every CLI cleanup/write target before mutation."""
    try:
        output = Path(out).absolute()
        stage = output / 'report'
        directories = (output, stage)
        targets = (output / '.gitignore', output / 'report.json', output / 'report.md',
                   stage / 'result.json', stage / 'result.json.part')
        for directory in directories:
            guarded._inspect(directory, directory=True, missing=True)
        for target in targets:
            guarded._inspect(target, missing=True)
        for path in paths:
            source = Path(path).absolute()
            guarded._inspect(source, missing=True)
            if any(guarded._aliases(source, target) for target in (*directories, *targets)):
                raise guarded._Invalid()
            if source.resolve() in output.resolve().parents:
                raise guarded._Invalid()
    except Exception:
        raise StageError('report: invalid_or_protected_inputs', EXIT_CONFIG) from None


def preflight(cfg, out):
    """Protect declared inputs without requiring readable sidecar contents."""
    if cfg is None:
        return
    protect_paths((cfg.path,), out)
    # Reparse before every deletion/publication, including a Config that used to
    # have no selection: current selected inputs may differ from the old scope.
    try:
        current = config.load(cfg.path)
    except Exception:
        raise StageError('report: invalid_or_protected_inputs', EXIT_CONFIG) from None
    if current.semantic_preflight != cfg.semantic_preflight:
        raise StageError('report: configuration_changed', EXIT_CONFIG)
    if current.semantic_preflight is not None:
        protect_paths((cfg.path, *cfg.semantic_preflight.paths(cfg)), out)


def preflight_configuration(raw, path, out):
    """Protect valid selections even when unrelated config validation fails.

    An ordinary malformed legacy config keeps its existing cleanup behavior.
    An uninterpretable configuration mentioning the opt-in cannot authorize
    cleanup, because its selected input scope cannot be established.
    """
    try:
        document = tomllib.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, tomllib.TOMLDecodeError):
        if b'semantic_preflight' in raw:
            raise StageError('report: invalid_or_protected_inputs', EXIT_CONFIG) from None
        return
    if 'semantic_preflight' in document:
        selection = config._semantic_preflight(document['semantic_preflight'])
        protect_paths((path, *(Path(path).parent / item for item in (selection.contract, selection.plan))), out)


def capture_outputs(paths):
    return [guarded._Captured.read(path) for path in paths]


def remove_unchanged(captures):
    """Remove only the unchanged files this publication actually produced."""
    for source in captures:
        try:
            source.unchanged()
            source.path.unlink()
        except Exception:
            pass


def _error(reason):
    result = Result(STAGE)
    result.add('contract_evidence', 'error', 'semantic_preflight', reason=reason)
    result.next_safe_action = ('Review the configured contract and plan locally, rerun '
                               '`sxc.py semantic-preflight` with those files, then rerun report.')
    return result.to_dict()


@dataclass
class Evidence:
    result: dict | None = None
    stale: dict | None = None
    captures: list = field(default_factory=list)
    generator: str = ''
    invalidated: bool = False

    def refresh(self):
        """Recheck every captured input/result immediately before consuming it."""
        if self.invalidated:
            return self
        try:
            for source in self.captures:
                source.unchanged()
            if self.generator and self.generator != generator_fingerprint():
                raise guarded._Invalid()
        except Exception:
            self.result = _error('changed_during_report')
            self.stale = None
            self.invalidated = True
        return self


def inspect(cfg, out):
    """Read/recompute only selected configuration, contract, plan and Result.

    Never open Gold/revision SQL contents or probe a database/network here.
    Callers must refresh after other report work before using this receipt.
    """
    evidence = Evidence()
    try:
        paths = (cfg.path, *cfg.semantic_preflight.paths(cfg))
        sources = [guarded._Captured.read(path) for path in paths]
        evidence.captures.extend(sources)
        captured_cfg = config.loads(sources[0].raw, sources[0].path)
        if captured_cfg.semantic_preflight != cfg.semantic_preflight:
            raise guarded._Invalid()
        expected = business_contracts.evaluate(captured_cfg, guarded._decode(sources[1].raw),
                                              guarded._decode(sources[2].raw))
        current = fingerprints(sources)
        evidence.generator = current['generator_sha256']
        target = Path(out) / STAGE / 'result.json'
        if guarded._inspect(target, missing=True) is None:
            return evidence.refresh()
        saved = guarded._Captured.read(target)
        evidence.captures.append(saved)
        recorded = validate_result(guarded._decode(saved.raw), expected_stage=STAGE)
        if recorded.get('schema_version') != 1 or recorded.get('stage') != STAGE:
            raise guarded._Invalid()
        # Regenerate user-visible explanations from the current declarations;
        # arbitrary saved strings are not report instructions or trusted text.
        safe = expected.to_dict()
        severity = {'pass': 0, 'not_run': 1, 'fail': 2, 'error': 3}
        saved_statuses = [check['status'] for check in recorded['checks']]
        saved_statuses.append(recorded.get('status', 'not_run' if not recorded['checks'] else 'pass'))
        safe['status'] = max([*saved_statuses, expected.status], key=severity.__getitem__)
        for key in ('started_at', 'finished_at'):
            safe[key] = recorded.get(key, '')
        safe['inputs'] = current
        evidence.result = safe
        if any(recorded.get('inputs', {}).get(key) != value for key, value in current.items()):
            evidence.stale = {'after': 'configured business inputs', 'why': 'contract_evidence_changed'}
            # Preserve saved severity/counts without repeating arbitrary saved
            # identifiers, explanations or values from evidence for other bytes.
            safe['checks'] += [{'id': f'saved_check:{index}', 'status': check['status'],
                               'config_ref': 'semantic_preflight', 'detail': {'reason': 'stale_saved_check'}}
                              for index, check in enumerate(recorded['checks'])]
        else:
            started = datetime.strptime(safe['started_at'], _TIME)
            finished = datetime.strptime(safe['finished_at'], _TIME)
            if finished < started or recorded['checks'] != safe['checks']:
                raise guarded._Invalid()
    except Exception:
        evidence.result = _error('invalid_or_unavailable_evidence')
        evidence.stale = None
    return evidence.refresh()
