"""Closed, metadata-only OKF 0.2 producer and offline preview.

Filesystem checks observe boundaries; they are not a kernel sandbox, lock, or
proof against hostile ancestor replacement or ABA between observations.
"""
import ctypes
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from . import config, knowledge_sources as sources, provenance
from .result import Result, discard

PROFILE = 'sxc-verification-contract-metadata/v1'
SCHEMA = 'sxc-okf-profile/v1'
MAX_MANIFEST = MAX_DOCUMENT = 64 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_ENTRIES = 256
LOSSES = ['source_text_omitted', 'raw_sql_omitted', 'parameter_values_omitted',
          'row_values_omitted', 'runtime_receipts_not_in_bundle', 'trust_not_inferred']
NOT_PROVEN = ['runtime_execution', 'cross_layer_correctness', 'bundle_authenticity', 'import_application']
_ID = re.compile(r'[a-z_][a-z0-9_]{0,62}\Z')


class _Issue(Exception):
    def __init__(self, code, count=0, digest=None):
        self.code, self.count, self.digest = code, count, digest


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str).encode('ascii')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _absolute(path):
    return Path(os.path.abspath(path))


def _inspect(path, root=None, missing=False):
    path = _absolute(path)
    root = root if root is not None and path.is_relative_to(root) else Path(path.anchor)
    try:
        meta = root.lstat()
        if sources._linked(meta):
            raise _Issue('blocked_path')
        for component in path.relative_to(root).parts:
            root /= component
            meta = root.lstat()
            if sources._linked(meta) or (stat.S_ISREG(meta.st_mode) and meta.st_nlink != 1):
                raise _Issue('blocked_path')
        return meta
    except FileNotFoundError:
        if missing:
            return None
        raise _Issue('missing_input') from None


def _read(path, limit, root=None):
    meta = _inspect(path, root)
    if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
        raise _Issue('input_limit')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or sources._linked(before)
                or before.st_nlink != 1 or before.st_size > limit):
            raise _Issue('blocked_path')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    final = _inspect(path, root)
    identity = sources._identity(meta)
    if (len(raw) > limit or len(raw) != meta.st_size
            or any(sources._identity(m) != identity for m in (before, after, final))):
        raise _Issue('input_changed')
    return raw, identity


def _safe_relative(value):
    try:
        sources.validate_spec_source({'file': value, 'sha256': '0' * 64}, '')
    except Exception:
        raise _Issue('blocked_path') from None
    return value.replace('\\', '/')


def _inventory_path(value):
    # Generated concept names are public validated IDs, not paths to private
    # business files. Do not reinterpret an ID such as "secrets" as a source.
    parts = value.split('/')
    if (not value or len(value) > 512 or len(parts) > 2 or sources._controls(value)
            or any(c in value for c in '\\:*?<>|"')
            or any(not p or p.startswith('.') or p.endswith(('.', ' ')) or sources._DEVICE.match(p) for p in parts)):
        raise _Issue('unsupported_profile')


def _selected(cfg, ids):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 64
            or any(not isinstance(i, str) or not _ID.fullmatch(i) or sources._DEVICE.match(i) for i in ids)
            or len(set(ids)) != len(ids)):
        raise _Issue('invalid_selection')
    available = {g.id for g in cfg.gold}
    if not set(ids) <= available:
        raise _Issue('invalid_selection')
    return [(gi, g) for gi, g in enumerate(cfg.gold) if g.id in ids]


def _cases(selected):
    return [(gi, g, ci, c) for gi, g in selected for ci, c in enumerate(g.expect)]


def _paths(cfg, selected):
    root = Path(cfg.base_dir()).resolve()
    paths = [Path(cfg.path)]
    for _, gold in selected:
        paths.append(root / _safe_relative(gold.sql))
        paths.extend(root / _safe_relative(c.spec_source.file) for c in gold.expect if c.spec_source is not None)
    return paths


def _overlap(first, second):
    a, b = first.resolve(), second.resolve()
    return a.is_relative_to(b) or b.is_relative_to(a) or sources._alias(first, second)


def protect(cfg, selected, out, bundle, export):
    """No content reads; callable before cleanup and again on every write path."""
    root = Path(cfg.base_dir()).resolve()
    out, bundle = _absolute(out), _absolute(bundle)
    targets = [out, out / 'okf', out / 'okf/result.json', out / 'okf/result.json.part', bundle]
    for target in targets:
        meta = _inspect(target, root, missing=True)
        if meta is not None:
            directory = target in (out, out / 'okf', bundle)
            if directory != stat.S_ISDIR(meta.st_mode):
                raise _Issue('blocked_path')
    if _overlap(out, bundle):
        raise _Issue('protected_output')
    for path in _paths(cfg, selected):
        _inspect(path, root, missing=True)
        if _overlap(path, out) or _overlap(path, bundle):
            raise _Issue('protected_output')
        for target in targets:
            if sources._alias(path, target):
                raise _Issue('protected_output')
    # Declarations outside the selected set are never read. Lexical ancestry
    # still prevents a result directory from containing any declared input.
    declared = [Path(cfg.path), *[root / g.sql for g in cfg.gold]]
    if cfg.source.data_revision_sql_file:
        declared.append(root / cfg.source.data_revision_sql_file)
    declared.extend(root / c.spec_source.file for g in cfg.gold for c in g.expect if c.spec_source is not None)
    for path in declared:
        lexical = _absolute(path)
        if any(lexical.is_relative_to(t) or t.is_relative_to(lexical) for t in (out, bundle)):
            raise _Issue('protected_output')
    if export and os.path.lexists(bundle):
        raise _Issue('destination_exists')


def _observe(cfg, selected, cfg_sha):
    root = Path(cfg.base_dir()).resolve()
    raw, cfg_identity = _read(Path(cfg.path), MAX_TOTAL, root)
    if _sha(raw) != cfg_sha:
        raise _Issue('configuration_changed')
    contracts, identities = [], [cfg_identity]
    budget = MAX_TOTAL
    for _, gold in selected:
        sql, identity = _read(root / _safe_relative(gold.sql), min(MAX_TOTAL, budget), root)
        budget -= len(sql)
        identities.append(identity)
        contracts.append({'id': gold.id, 'sql_sha256': _sha(sql), 'members': sorted(set(gold.map.values())),
                          'case_count': len(gold.expect), 'omitted_count': len(gold.expect) + len(gold.params) + len(gold.param_sets),
                          'omitted_sha256': _sha(_json(asdict(gold)))})
    snapshot = sources.capture(cfg, _cases(selected))
    public = snapshot.public()
    if not snapshot.ok:
        states = {item['state'] for item in public['items'] if item['state'] != 'current'}
        raise _Issue('source_' + (next(iter(states)) if len(states) == 1 else 'noncurrent'), public['count'])
    identities.extend(e.identity for e in snapshot.entries)
    inputs = {'configuration_sha256': cfg_sha, 'engine_sha256': provenance.engine_fingerprint(), 'contracts': contracts}
    return inputs, public, identities


def _frontmatter(values, body):
    return ('---\n' + ''.join(k + ': ' + _json(v).decode('ascii') + '\n' for k, v in values.items())
            + '---\n\n' + body + '\n').encode('utf-8')


def _documents(ids, inputs, bindings):
    documents = {'index.md': _frontmatter({'okf_version': '0.2'},
                 '# SXC verification contracts\n\nMetadata only.\n\n' + '\n'.join(f'- [{i}](contracts/{i}.md)' for i in ids))}
    contracts = {c['id']: c for c in inputs['contracts']}
    for i in ids:
        c = contracts[i]
        documents[f'contracts/{i}.md'] = _frontmatter({
            'type': 'SXC Verification Contract', 'title': 'Verification contract ' + i,
            'resource': 'urn:sxc:verification-contract:' + i,
            'x_sxc': {'profile': PROFILE, 'id': i, 'sql_sha256': c['sql_sha256'],
                      'configuration_sha256': inputs['configuration_sha256'],
                      'engine_sha256': inputs['engine_sha256'], 'source_binding_sha256': bindings['digest']}},
            'Metadata only. No execution, correctness, authenticity or application is proven.')
    return documents


def _bundle(ids, inputs, bindings):
    docs = _documents(ids, inputs, bindings)
    manifest = {'schema': SCHEMA, 'profile': PROFILE, 'okf_version': '0.2', 'selected': ids,
                'inputs': inputs, 'source_bindings': bindings, 'losses': LOSSES,
                'documents': [{'path': p, 'id': 'index' if p == 'index.md' else Path(p).stem,
                               'bytes': len(raw), 'sha256': _sha(raw)} for p, raw in sorted(docs.items())]}
    raw = _json(manifest) + b'\n'
    if (len(raw) > MAX_MANIFEST or any(len(v) > MAX_DOCUMENT for v in docs.values())
            or len(raw) + sum(map(len, docs.values())) > MAX_TOTAL):
        raise _Issue('output_limit')
    return manifest, {'manifest.json': raw, **docs}


def _load_json(raw):
    # Bound depth before json.loads allocates nested containers, including input
    # whose deeply nested content would otherwise cause a RecursionError.
    depth = 0
    quoted = escaped = False
    for b in raw:
        if quoted:
            if escaped:
                escaped = False
            elif b == 92:
                escaped = True
            elif b == 34:
                quoted = False
        elif b == 34:
            quoted = True
        elif b in (91, 123):
            depth += 1
            if depth > 16:
                raise _Issue('input_limit')
        elif b in (93, 125):
            depth -= 1
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise _Issue('unsupported_profile')
            result[k] = v
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(_Issue('unsupported_profile')))
    except (ValueError, UnicodeError, RecursionError):
        raise _Issue('unsupported_profile', digest=_sha(raw)) from None


def _inventory(bundle):
    root = _absolute(bundle)
    top = _inspect(root)
    if not stat.S_ISDIR(top.st_mode):
        raise _Issue('unsupported_profile')
    files, directories, count, total, identities = {}, set(), 0, 0, {}
    def visit(directory, level):
        nonlocal count, total
        before = _inspect(directory, root)
        identities[directory.relative_to(root).as_posix()] = sources._identity(before)
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES:
                    raise _Issue('input_limit')
                path = directory / entry.name
                rel = path.relative_to(root).as_posix()
                _inventory_path(rel)
                meta = _inspect(path, root)
                if stat.S_ISDIR(meta.st_mode):
                    if level != 0 or rel != 'contracts':
                        raise _Issue('unsupported_profile')
                    directories.add(rel)
                    visit(path, level + 1)
                elif stat.S_ISREG(meta.st_mode):
                    limit = MAX_MANIFEST if rel == 'manifest.json' else MAX_DOCUMENT
                    total += meta.st_size
                    if meta.st_size > limit or total > MAX_TOTAL:
                        raise _Issue('input_limit')
                    files[rel] = (meta.st_size, sources._identity(meta))
                else:
                    raise _Issue('blocked_path')
        if sources._identity(_inspect(directory, root)) != sources._identity(before):
            raise _Issue('input_changed')
    visit(root, 0)
    if len({p.casefold() for p in files}) != len(files) or directories != {'contracts'}:
        raise _Issue('unsupported_profile')
    return files, identities


def _preview_read(bundle):
    inventory = _inventory(bundle)
    files = inventory[0]
    if 'manifest.json' not in files:
        raise _Issue('unsupported_profile')
    raw, identity = _read(bundle / 'manifest.json', MAX_MANIFEST, bundle)
    if identity != files['manifest.json'][1]:
        raise _Issue('input_changed')
    manifest = _load_json(raw)
    keys = {'schema', 'profile', 'okf_version', 'selected', 'inputs', 'source_bindings', 'documents', 'losses'}
    if (not isinstance(manifest, dict) or set(manifest) != keys or manifest.get('schema') != SCHEMA
            or manifest.get('profile') != PROFILE or manifest.get('okf_version') != '0.2'):
        raise _Issue('unsupported_profile', len(manifest) if isinstance(manifest, dict) else 0, _sha(raw))
    return manifest, inventory, raw


def _validate_frontmatter(raw):
    try:
        text = raw.decode('utf-8')
        if not text.startswith('---\n') or '\n---\n' not in text[4:]:
            raise _Issue('unsupported_profile')
        header = text[4:text.index('\n---\n', 4)]
        keys = set()
        for line in header.split('\n'):
            key, sep, value = line.partition(': ')
            if not sep or key in keys or not re.fullmatch('[a-z_]+', key):
                raise _Issue('unsupported_profile')
            keys.add(key)
            _load_json(value.encode())
    except (UnicodeError, ValueError):
        raise _Issue('unsupported_profile') from None


def _publish(staging, destination):
    """OS no-replace directory rename. Never emulate using check-then-replace."""
    if sys.platform == 'win32':
        os.rename(staging, destination)  # Windows refuses an existing directory.
    elif sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise _Issue('publication_unavailable')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
            raise _Issue('publication_unavailable')
    elif sys.platform == 'darwin':
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renamex_np', None)
        if rename is None:
            raise _Issue('publication_unavailable')
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(os.fsencode(staging), os.fsencode(destination), 4) != 0:
            raise _Issue('publication_unavailable')
    else:
        raise _Issue('publication_unavailable')


def _verify_generated(bundle, files):
    inventory = _inventory(bundle)
    if set(inventory[0]) != set(files):
        raise _Issue('input_changed')
    for name, expected in files.items():
        raw, identity = _read(bundle / name, MAX_MANIFEST, bundle)
        if raw != expected or identity != inventory[0][name][1]:
            raise _Issue('input_changed')
    if _inventory(bundle) != inventory:
        raise _Issue('input_changed')
    return inventory


def _cleanup_owned(staging, identity, files):
    # Delete only the original owned directory and files with the exact bytes
    # this invocation wrote. Unexpected substitutions are left untouched.
    try:
        meta = _inspect(staging)
        if (meta.st_dev, meta.st_ino) != identity:
            return
        actual, _ = _inventory(staging)
        if set(actual) != set(files):
            return
        for name, expected in files.items():
            raw, _ = _read(staging / name, MAX_MANIFEST, staging)
            if raw != expected:
                return
        for name in files:
            (staging / name).unlink()
        (staging / 'contracts').rmdir()
        staging.rmdir()
    except (OSError, _Issue):
        return


def _result():
    return Result('okf', not_proven=list(NOT_PROVEN))


def _failure(res, issue):
    detail = {'reason': issue.code, 'omitted_count': issue.count}
    if issue.digest:
        detail['omitted_sha256'] = issue.digest
    res.add('okf:profile', 'error', '', **detail)
    return res


def _ensure_output(out):
    if not out.exists():
        out.mkdir(parents=True)
        with (out / '.gitignore').open('xb') as stream:
            stream.write(b'*\n')


def run(cfg, args, out):
    """Export or preview selected local contracts; never applies imported data."""
    res = _result()
    staging = None
    files = {}
    try:
        export = bool(getattr(args, 'export', None))
        preview = bool(getattr(args, 'import_preview', None))
        if export == preview or cfg is None or (preview and getattr(args, 'select', [])):
            raise _Issue('invalid_arguments')
        bundle = _absolute(args.export if export else args.import_preview)
        cfg_raw, _ = _read(Path(cfg.path), MAX_TOTAL, Path(cfg.base_dir()).resolve())
        cfg_sha = getattr(args, '_okf_configuration_sha', _sha(cfg_raw))
        if _sha(cfg_raw) != cfg_sha:
            raise _Issue('configuration_changed')
        # Direct API callers may retain a parsed Config across edits. Compare
        # against a fresh validated parse before binding its current byte hash.
        fresh = config.load(cfg.path)
        if fresh != cfg or _sha(_read(Path(cfg.path), MAX_TOTAL)[0]) != cfg_sha:
            raise _Issue('configuration_changed')
        cfg = fresh
        if preview:
            # Guard the bundle and output before touching imported bytes.
            protect(cfg, [], out, bundle, False)
            manifest, inventory, manifest_raw = _preview_read(bundle)
            ids = manifest['selected']
        else:
            ids = getattr(args, 'select', [])
        selected = _selected(cfg, ids)
        ids = [g.id for _, g in selected]
        protect(cfg, selected, out, bundle, export)
        initial = _observe(cfg, selected, cfg_sha)
        expected, files = _bundle(ids, *initial[:2])
        if preview:
            if _json(manifest) != _json(expected) or set(inventory[0]) != set(files):
                raise _Issue('unsupported_profile', len(manifest), _sha(manifest_raw))
            for name, expected_raw in files.items():
                raw, identity = _read(bundle / name, MAX_MANIFEST if name == 'manifest.json' else MAX_DOCUMENT, bundle)
                if identity != inventory[0][name][1]:
                    raise _Issue('input_changed')
                if name != 'manifest.json':
                    _validate_frontmatter(raw)
                    if raw != expected_raw:
                        raise _Issue('unsupported_profile', 1, _sha(raw))
                elif _json(_load_json(raw)) != _json(expected):
                    raise _Issue('input_changed')
            if _inventory(bundle) != inventory:
                raise _Issue('input_changed')
        else:
            bundle.parent.mkdir(parents=True, exist_ok=True)
            protect(cfg, selected, out, bundle, True)
            staging = Path(tempfile.mkdtemp(prefix='.sxc-okf-', dir=bundle.parent))
            meta = staging.lstat()
            owned = (meta.st_dev, meta.st_ino)
            (staging / 'contracts').mkdir()
            for name, raw in files.items():
                with (staging / name).open('xb') as stream:
                    stream.write(raw)
        if _observe(cfg, selected, cfg_sha) != initial:
            raise _Issue('input_changed')
        protect(cfg, selected, out, bundle, export)
        if export:
            _verify_generated(staging, files)
            _publish(staging, bundle)
            staging = None
            _verify_generated(bundle, files)
        final_inventory = _inventory(bundle)
        if _observe(cfg, selected, cfg_sha) != initial:
            raise _Issue('input_changed')
        res.inputs = {'okf_profile': {'profile': PROFILE, 'mode': 'export' if export else 'import_preview',
                                    'selected_count': len(ids), 'manifest_sha256': _sha(_json(expected)),
                                    'configuration_sha256': cfg_sha, 'engine_sha256': initial[0]['engine_sha256'],
                                    'source_binding_sha256': initial[1]['digest']}}
        res._okf_observation = initial
        res._okf_inventory = final_inventory
        res.add('okf:profile', 'pass', '', selected_count=len(ids), document_count=len(files) - 1)
        return res
    except _Issue as exc:
        return _failure(res, exc)
    except Exception:
        return _failure(res, _Issue('input_unavailable'))
    finally:
        if staging is not None:
            _cleanup_owned(staging, owned, files)


def cli(argv, parser, summary):
    """Separate guarded dispatch: no all-gold fingerprint or revision probe."""
    res, out, cfg, cfg_sha, selected, bundle, args = _result(), None, None, None, [], None, None
    exit_code = None
    try:
        args = parser(audit_errors=True).parse_args(argv)
        if not args.config or (args.export and not args.select) or (args.import_preview and args.select):
            raise _Issue('invalid_arguments')
        path = _absolute(args.config)
        cfg_raw, _ = _read(path, MAX_TOTAL)
        cfg_sha = _sha(cfg_raw)
        cfg = config.load(path)
        args._okf_configuration_sha = cfg_sha
        candidate = _absolute(args.out) if args.out else path.parent / '.sxc'
        bundle = _absolute(args.export or args.import_preview)
        # Preview selection is untrusted until run validates the manifest. Guard
        # all declared inputs lexically here, selected file identities in run.
        if args.export:
            selected = _selected(cfg, args.select)
        else:
            manifest, _, _ = _preview_read(bundle)
            selected = _selected(cfg, manifest['selected'])
        protect(cfg, selected, candidate, bundle, bool(args.export))
        if _sha(_read(path, MAX_TOTAL)[0]) != cfg_sha:
            raise _Issue('configuration_changed')
        out = candidate
        _ensure_output(out)
        if _sha(_read(path, MAX_TOTAL)[0]) != cfg_sha:
            raise _Issue('configuration_changed')
        protect(cfg, selected, out, bundle, bool(args.export))
        discard(out, 'okf')
        res = run(cfg, args, out)
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            return exc.code
        if isinstance(exc, KeyboardInterrupt):
            res = _failure(res, _Issue('interrupted'))
            exit_code = 1
        else:
            res = _failure(res, exc if isinstance(exc, _Issue) else _Issue('invalid_arguments'))
            exit_code = 2
    path = None
    if out is not None:
        try:
            # Re-read preview selection only via the bounded parser. A failure
            # suppresses writes, preserving an unverified input alias.
            if args.import_preview:
                manifest, _, _ = _preview_read(bundle)
                selected = _selected(cfg, manifest['selected'])
            if _sha(_read(Path(cfg.path), MAX_TOTAL)[0]) != cfg_sha:
                raise _Issue('configuration_changed')
            # Publication succeeded: destination now exists and must be allowed.
            protect(cfg, selected, out, bundle, False)
            _ensure_output(out)
            if _sha(_read(Path(cfg.path), MAX_TOTAL)[0]) != cfg_sha:
                raise _Issue('configuration_changed')
            protect(cfg, selected, out, bundle, False)
            if res.status == 'pass':
                if (_observe(cfg, selected, cfg_sha) != res._okf_observation
                        or _inventory(bundle) != res._okf_inventory):
                    raise _Issue('input_changed')
            path = res.write(out)
        except Exception:
            _failure(res, _Issue('protected_output_unavailable'))
    print(summary(res, path, bool(args and args.quiet)))
    return exit_code if exit_code is not None else res.exit_code()
