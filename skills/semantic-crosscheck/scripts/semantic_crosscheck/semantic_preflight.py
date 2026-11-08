"""Guarded, offline dispatch for explicitly authored business contracts/plans.

No generic operational fingerprint is collected: this diagnostic never opens
Gold/revision SQL contents or a database/model connection. Local path observations
detect ordinary changes; they are not a lock against hostile ancestor races.
"""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys

from . import business_contracts, config
from .result import EXIT_CONFIG, Result, StageError

FILE_LIMIT = 256 * 1024
DEPTH_LIMIT = 16
NODE_LIMIT = 10000
STAGE = 'semantic_preflight'


class _Invalid(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise _Invalid()


def _identity(meta):
    # Windows lstat/fstat expose different ctime meanings on supported Python
    # versions. Byte/mtime/inode checks remain; treating those two clocks as
    # interchangeable rejects unchanged, freshly rewritten input files.
    return (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_size,
            meta.st_mtime_ns, None if os.name == 'nt' else meta.st_ctime_ns, meta.st_nlink)


def _darwin_alias(path, meta):
    targets = {'/var': '/private/var', '/tmp': '/private/tmp'}
    if sys.platform != 'darwin' or path.as_posix() not in targets or not stat.S_ISLNK(meta.st_mode):
        return False
    expected = Path(targets[path.as_posix()])
    target = path.readlink()
    if not target.is_absolute():
        target = path.parent / target
    actual = expected.lstat()
    return (target == expected and stat.S_ISDIR(actual.st_mode)
            and not bool(getattr(actual, 'st_file_attributes', 0) & 0x400)
            and path.resolve(strict=True) == expected)


def _inspect(path, *, directory=False, missing=False):
    """Reject observed link/reparse containers and non-single-link files."""
    path = Path(path).absolute()
    final = None
    for item in (*reversed(path.parents), path):
        try:
            meta = item.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise _Invalid() from None
        linked = stat.S_ISLNK(meta.st_mode) or bool(getattr(meta, 'st_file_attributes', 0) & 0x400)
        is_dir = item != path or directory
        if linked:
            if not is_dir or not _darwin_alias(item, meta):
                raise _Invalid()
        elif is_dir:
            if not stat.S_ISDIR(meta.st_mode):
                raise _Invalid()
        elif not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise _Invalid()
        if item == path:
            final = meta
    return final


def _read(path):
    meta = _inspect(path)
    if meta.st_size > FILE_LIMIT:
        raise _Invalid()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > FILE_LIMIT:
            raise _Invalid()
        raw = stream.read(FILE_LIMIT + 1)
        after = os.fstat(stream.fileno())
    final = _inspect(path)
    identity = _identity(meta)
    if (len(raw) > FILE_LIMIT or len(raw) != meta.st_size
            or any(_identity(value) != identity for value in (before, after, final))):
        raise _Invalid()
    return raw, identity


@dataclass(frozen=True)
class _Captured:
    path: Path
    raw: bytes
    identity: tuple

    @classmethod
    def read(cls, path):
        absolute = Path(path).absolute()
        raw, identity = _read(absolute)
        return cls(absolute, raw, identity)

    def unchanged(self):
        raw, identity = _read(self.path)
        if raw != self.raw or identity != self.identity:
            raise _Invalid()


def _aliases(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def _protect(inputs, out):
    out = Path(out).absolute()
    directory = out / STAGE
    _inspect(out, directory=True, missing=True)
    _inspect(directory, directory=True, missing=True)
    targets = [out / '.gitignore', directory / 'result.json', directory / 'result.json.part']
    for target in targets:
        _inspect(target, missing=True)
    for source in inputs:
        source.unchanged()
        if any(_aliases(source.path, target) for target in [out, directory, *targets]):
            raise _Invalid()
        # An input regular file can never be a parent of an output directory.
        if source.path.resolve() in out.resolve().parents:
            raise _Invalid()


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _Invalid()
            result[key] = value
        return result
    def constant(_value):
        raise _Invalid()
    doc = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=constant)
    stack, count = [(doc, 0)], 0
    while stack:
        value, depth = stack.pop()
        count += 1
        if count > NODE_LIMIT or depth > DEPTH_LIMIT:
            raise _Invalid()
        if isinstance(value, dict):
            count += len(value)  # keys also consume the budget
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return doc


def _arguments(argv):
    if not argv or argv[0] != 'semantic-preflight':
        raise _Invalid()
    parser = _Parser(prog='sxc.py semantic-preflight', allow_abbrev=False,
                     description='Check explicitly authored business definitions and a structured plan offline.')
    for name in ('config', 'contract', 'plan'):
        parser.add_argument('--' + name, action='append', required=True, metavar='FILE')
    parser.add_argument('--out', action='append', metavar='DIR')
    parser.add_argument('--quiet', action='count', default=0)
    args = parser.parse_args(argv[1:])
    for name in ('config', 'contract', 'plan', 'out'):
        values = getattr(args, name)
        if values is not None:
            if len(values) != 1 or not values[0] or '\x00' in values[0]:
                raise _Invalid()
            setattr(args, name, values[0])
    if args.quiet > 1:
        raise _Invalid()
    return args


def _failed():
    res = Result(STAGE)
    res.add('input_validation', 'error', reason='invalid_input')
    res.next_safe_action = 'Review the explicit inputs locally and rerun the diagnostic.'
    return res


def _publish(res, inputs, out):
    _protect(inputs, out)
    created = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    _protect(inputs, out)
    if created:
        # Exclusive creation never follows or truncates a pre-existing path.
        fd = os.open(out / '.gitignore', os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write('*\n')
    _protect(inputs, out)
    published = _Captured.read(res.write(out))
    expected = json.dumps(res.to_dict(), ensure_ascii=False, indent=1).encode('utf-8')
    if published.raw != expected:
        raise _Invalid()
    try:
        _protect(inputs, out)
        published.unchanged()
    except Exception:
        # Remove only this unchanged, guarded receipt. Preserve an output that
        # another writer replaced or aliased while publication was in flight.
        try:
            published.unchanged()
            published.path.unlink()
        except Exception:
            pass
        raise


def cli(argv, _parser, summary):
    """Dedicated route; parse errors/unsafe inputs perform no output mutations."""
    inputs, out, res = [], None, None
    try:
        args = _arguments(argv)
    except SystemExit as exc:
        return exc.code
    except Exception:
        sys.stderr.write('[sxc semantic_preflight] ERROR invalid_arguments\n')
        return EXIT_CONFIG
    try:
        inputs = [_Captured.read(getattr(args, name)) for name in ('config', 'contract', 'plan')]
        out = Path(args.out).absolute() if args.out else inputs[0].path.parent / '.sxc'
        _protect(inputs, out)
        # A prior verdict may be cleared only for the exact verified input bytes.
        (out / STAGE / 'result.json').unlink(missing_ok=True)
        try:
            cfg = config.loads(inputs[0].raw, inputs[0].path)
            res = business_contracts.evaluate(cfg, _decode(inputs[1].raw), _decode(inputs[2].raw))
        except (StageError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError, _Invalid):
            res = _failed()
        from .contract_evidence import fingerprints
        # Explicit selected paths are hashed as well as their captured bytes.
        # A passing diagnostic for other files cannot satisfy configured inputs.
        res.inputs = fingerprints(inputs)
        res.provenance = {'schema_version': 1, 'binding': 'not_applicable'}
        res.runtime_evidence = {'schema_version': 1, 'binding': 'not_applicable'}
        res._suppress_paths = True
        _publish(res, inputs, out)
    except Exception:
        sys.stderr.write('[sxc semantic_preflight] ERROR invalid_input\n')
        return EXIT_CONFIG
    print(summary(res, None, bool(args.quiet)))
    return EXIT_CONFIG if res.status == 'error' else res.exit_code()
