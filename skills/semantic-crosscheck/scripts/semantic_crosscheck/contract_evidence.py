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

from . import business_contracts, config, semantic_preflight as guarded
from .result import Result
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
        safe['status'] = recorded.get('status', expected.status)
        for key in ('started_at', 'finished_at'):
            safe[key] = recorded.get(key, '')
        safe['inputs'] = current
        evidence.result = safe
        if any(recorded.get('inputs', {}).get(key) != value for key, value in current.items()):
            evidence.stale = {'after': 'configured business inputs', 'why': 'contract_evidence_changed'}
        else:
            started = datetime.strptime(safe['started_at'], _TIME)
            finished = datetime.strptime(safe['finished_at'], _TIME)
            if finished < started or recorded['checks'] != safe['checks']:
                raise guarded._Invalid()
    except Exception:
        evidence.result = _error('invalid_or_unavailable_evidence')
        evidence.stale = None
    return evidence.refresh()
