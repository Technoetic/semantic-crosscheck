"""Bounded opaque-byte observations for authored expectations, never semantic approval.

The selected configuration directory is canonicalized once. Checks below that
root reject observed links and drift; these observations are not a filesystem
sandbox, a continuous lock, or proof against hostile ancestor races or ABA.
"""
from dataclasses import asdict, dataclass, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from .result import EXIT_CONFIG, StageError

MAX_CASES = 64
MAX_FILE = 256 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_METADATA = 64 * 1024
_DEVICE = re.compile(r'(?i)^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³]|conin\$|conout\$)(\.|$)')
_SECRET = re.compile(r'(?i)(^|[._-])(secrets?|credentials?|passwords?|passwd|tokens?|id_rsa|id_ed25519|env)([._-]|$)')


def _error():
    return StageError('expect: invalid or protected spec_source inputs', EXIT_CONFIG)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _controls(value):
    return any(unicodedata.category(c).startswith('C') for c in value)


@dataclass(frozen=True)
class SourceBinding:
    file: str
    sha256: str
    object_id: str = ''
    revision: str = ''


def validate_spec_source(value, ref):
    """Closed declaration; neither labels nor file bytes are interpreted."""
    # ref is intentionally not interpolated: callers may provide arbitrary text.
    if (not isinstance(value, dict) or set(value) - {'file', 'sha256', 'object_id', 'revision'}
            or not {'file', 'sha256'} <= set(value)):
        raise _error()
    file, pin = value['file'], value['sha256']
    if (not isinstance(file, str) or not 1 <= len(file) <= 512 or _controls(file)
            or not isinstance(pin, str) or re.fullmatch('[0-9a-f]{64}', pin) is None):
        raise _error()
    normalized = file.replace('\\', '/')
    if normalized.startswith('/') or any(c in normalized for c in ':*?<>|"'):
        raise _error()
    for part in normalized.split('/'):
        if (not part or part in {'.', '..'} or part.startswith('.') or part.endswith(('.', ' '))
                or _DEVICE.match(part) or _SECRET.search(part)
                or part.casefold().endswith(('.pem', '.key', '.pfx', '.p12'))):
            raise _error()
    for key in ('object_id', 'revision'):
        label = value.get(key, '')
        if not isinstance(label, str) or len(label) > 128 or _controls(label):
            raise _error()
    return SourceBinding(file, pin, value.get('object_id', ''), value.get('revision', ''))


class _Blocked(Exception):
    pass


def _linked(meta):
    return stat.S_ISLNK(meta.st_mode) or bool(getattr(meta, 'st_file_attributes', 0) & 0x400)


def _inspect(path, root):
    """lstat each component BELOW the selected canonical root. No content reads."""
    relative = path.relative_to(root)
    current = root
    last = None
    for part in relative.parts:
        current /= part
        last = current.lstat()
        if _linked(last) or (stat.S_ISREG(last.st_mode) and last.st_nlink > 1):
            raise _Blocked()
    return last or root.lstat()


def _alias(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except (OSError, ValueError):
        return False


def _bound(cases):
    entries = []
    for gi, gold, ci, case in cases:
        binding = getattr(case, 'spec_source', None)
        if binding is None:
            continue
        if case.source != 'authored' or not isinstance(binding, SourceBinding):
            raise _error()
        binding = validate_spec_source(asdict(binding), '')
        entries.append(_Entry(gi, gold.id, ci, binding))
        if len(entries) > MAX_CASES:
            raise _error()
    return entries


def _cases(cfg):
    return [(gi, gold, ci, case) for gi, gold in enumerate(cfg.gold) for ci, case in enumerate(gold.expect)]


def preflight(cfg, out):
    """Protect all declared inputs before CLI cleanup or any final/error write.

    Missing or unreadable evidence can still produce a nonpassing observation.
    Observed links, aliases and unusable output containers fail before writes.
    """
    entries = _bound(_cases(cfg))
    if not entries:
        return
    try:
        root = Path(cfg.base_dir()).resolve()
        output = Path(os.path.abspath(out))
        declared_root = Path(os.path.abspath(cfg.base_dir()))
        # Trust only the root canonicalization already selected by the caller,
        # including its ancestors; retain all output child components for lstat.
        for ancestor in (declared_root, *declared_root.parents):
            if output.is_relative_to(ancestor):
                output = ancestor.resolve() / output.relative_to(ancestor)
                break
        stage = output / 'expect'
        targets = [stage / 'result.json', stage / 'result.json.part']
        sources = [root / entry.binding.file.replace('\\', '/') for entry in entries]
        inputs = [root / Path(cfg.path).name, *[root / g.sql for g in cfg.gold], *sources]
        if cfg.source.data_revision_sql_file:
            inputs.append(root / cfg.source.data_revision_sql_file)
        for path in (output, stage, *targets):
            # Canonical root ancestors (e.g. macOS /tmp) are allowed. All
            # observed components below it, or below the volume root, are not.
            anchor = root if path.is_relative_to(root) else Path(path.anchor)
            try:
                meta = _inspect(path, anchor)
                if path in (output, stage) and not stat.S_ISDIR(meta.st_mode):
                    raise _Blocked()
                if path in targets and not stat.S_ISREG(meta.st_mode):
                    raise _Blocked()
            except FileNotFoundError:
                pass
        canonical_output = output.resolve()
        for path in inputs:
            canonical = path.resolve()
            if canonical.is_relative_to(canonical_output) or canonical_output.is_relative_to(canonical):
                raise _Blocked()
            if any(_alias(path, target) for target in targets):
                raise _Blocked()
            try:
                absolute = Path(os.path.abspath(path))
                anchor = root if absolute.is_relative_to(root) else Path(absolute.anchor)
                _inspect(absolute, anchor)
            except FileNotFoundError:
                pass
            except OSError:
                # I/O unavailability is classified by the bounded observer.
                pass
    except (OSError, ValueError, TypeError, _Blocked):
        raise _error() from None


def _identity(meta):
    return meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns


@dataclass(frozen=True)
class _Entry:
    gold_index: int
    gold_id: str
    case_index: int
    binding: SourceBinding
    state: str = 'unavailable'
    observed: str = ''
    identity: tuple = ()


def _observe(root, entry, budget):
    path = root / entry.binding.file.replace('\\', '/')
    consumed = 0
    try:
        meta = _inspect(path, root)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_FILE or meta.st_size > budget:
            raise _Blocked()
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (_linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink > 1
                    or before.st_size > MAX_FILE or before.st_size > budget):
                raise _Blocked()
            raw = stream.read(min(MAX_FILE, budget))
            consumed = len(raw)
            after = os.fstat(stream.fileno())
        if after.st_size > MAX_FILE or after.st_size > budget:
            raise _Blocked()
        final = _inspect(path, root)
        if not stat.S_ISREG(final.st_mode) or final.st_size > MAX_FILE or final.st_size > budget:
            raise _Blocked()
        observed = hashlib.sha256(raw).hexdigest()
        stable = (_identity(meta) == _identity(before) == _identity(after) == _identity(final)
                  and len(raw) == before.st_size)
        state = 'current' if stable and observed == entry.binding.sha256 else 'changed'
        return replace(entry, state=state, observed=observed, identity=_identity(final)), len(raw)
    except FileNotFoundError:
        state = 'missing'
    except _Blocked:
        state = 'blocked'
    except OSError as exc:
        state = 'blocked' if exc.errno == errno.ELOOP else 'unavailable'
    return replace(entry, state=state, observed='', identity=()), consumed


def _batch(root, entries):
    observed, used = [], 0
    for entry in entries:
        item, size = _observe(root, entry, MAX_TOTAL - used)
        observed.append(item)
        used += size
    return observed


def _retain(initial, latest):
    if initial.state != 'current':
        return initial
    if latest.state == 'current' and initial.identity != latest.identity:
        return replace(latest, state='changed')
    return latest


def _configuration(cfg):
    try:
        return hashlib.sha256(Path(cfg.path).read_bytes()).hexdigest()
    except OSError:
        raise _error() from None


@dataclass
class SourceSnapshot:
    root: Path
    entries: list
    cfg: object = None
    out: object = None
    configuration_sha256: str = ''

    @property
    def ok(self):
        return all(entry.state == 'current' for entry in self.entries)

    def public(self):
        items = [{'gold_index': e.gold_index, 'case_index': e.case_index, 'state': e.state,
                  'expected_sha256': e.binding.sha256, 'observed_sha256': e.observed,
                  'binding_sha256': _digest(asdict(e.binding))} for e in self.entries]
        public = {'schema': 'sxc-spec-sources/v1', 'count': len(items),
                  'current': sum(e.state == 'current' for e in self.entries), 'digest': _digest(items), 'items': items}
        if len(_json(public)) > MAX_METADATA:
            raise _error()
        return public


def capture(cfg, cases, out=None):
    entries = _bound(cases)
    root = Path(cfg.base_dir()).resolve()
    if entries and out is not None:
        preflight(cfg, out)
    before = _configuration(cfg) if entries else ''
    initial = _batch(root, entries)
    # A later source read can mutate an earlier source: recheck the entire batch.
    latest = _batch(root, initial)
    if entries and _configuration(cfg) != before:
        raise _error()
    snapshot = SourceSnapshot(root, [_retain(a, b) for a, b in zip(initial, latest)], cfg, out, before)
    snapshot.public()
    return snapshot


def finish(snapshot, result):
    """Reobserve; invalidate affected passes, never promote retained evidence."""
    if not snapshot.entries:
        return snapshot
    if _configuration(snapshot.cfg) != snapshot.configuration_sha256:
        raise _error()
    observed = _batch(snapshot.root, snapshot.entries)
    latest = [_retain(a, b) for a, b in zip(observed, _batch(snapshot.root, observed))]
    if _configuration(snapshot.cfg) != snapshot.configuration_sha256:
        raise _error()
    final = replace(snapshot, entries=[_retain(a, b) for a, b in zip(snapshot.entries, latest)])
    result.inputs['spec_sources'] = final.public()
    checks = {check.id: check for check in result.checks}
    for entry in final.entries:
        cid = f'expect:spec_source:{entry.gold_id}[{entry.case_index}]'
        status = 'pass' if entry.state == 'current' else 'not_run'
        if cid in checks:
            check = checks[cid]
            if check.status not in {'fail', 'error'}:
                check.status = status
            check.detail = {'state': entry.state}
        else:
            result.add(cid, status, '', state=entry.state)
        case = checks.get(f'expect:{entry.gold_id}[{entry.case_index}]')
        if entry.state != 'current' and case is not None and case.status == 'pass':
            case.status = 'not_run'
            case.detail['reason'] = 'spec_source_not_current'
    return final
