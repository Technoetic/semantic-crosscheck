"""Offline scalar boundary hints for the existing finite witness schema.

The fully consumed grammar is only ``column comparison literal``. It is not a
PostgreSQL parser, SQL sandbox, constraint solver or query-equivalence oracle.
Integer units, numeric quanta and dates are manipulated exactly; a literal that
would need rounding is unsupported. Only the explicit generated schema holds
scalar values. Results retain counts and source/generator/output hashes.
"""
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from . import constraints, witness
from .result import EXIT_CONFIG, Result, StageError

VERSION = 1
FILE_LIMIT = 65536
PREDICATE_LIMIT = 4096
LITERAL_LIMIT = 256
_FIELDS = {'version', 'table', 'column', 'type', 'rows', 'predicate_sql'}
_IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]{0,62}\Z')
_TYPE = re.compile(r'numeric\(([0-9]{1,2}),([0-9]{1,2})\)\Z')
_INTEGER = re.compile(r'[+-]?[0-9]+\Z')
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
_ATOM = re.compile(r'[ \t\r\n]*([a-z_][a-z0-9_]{0,62})[ \t\r\n]*'
                   r'(<=|>=|<>|!=|=|<|>)[ \t\r\n]*(.*?)[ \t\r\n]*\Z', re.DOTALL)
_DATE = re.compile(r"(?i:DATE)[ \t\r\n]+'([0-9]{4}-[0-9]{2}-[0-9]{2})'\Z")


def _error():
    return StageError('boundary-schema: spec must satisfy the bounded version 1 scalar comparison contract',
                      EXIT_CONFIG)


def _read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(FILE_LIMIT + 1)
    if not raw or len(raw) > FILE_LIMIT:
        raise ValueError('Bounded spec')
    return raw


@dataclass(frozen=True)
class Spec:
    source: Path
    sha256: str
    table: str
    column: str
    kind: str
    rows: tuple
    center: object                  # int units (integer/numeric) or datetime.date
    scale: int

    def unchanged(self):
        try:
            return hashlib.sha256(_read(self.source)).hexdigest() == self.sha256
        except (OSError, ValueError):
            return False


def _numeric_units(token, precision, scale):
    """An exact integer multiple of the declared quantum, without Decimal arithmetic.

    Inspect the bounded Decimal coefficient/exponent before powers or rendering.
    In particular, a billion-sized exponent cannot trigger a billion-byte fixed
    representation. No ambient precision, rounding mode or traps affect this.
    """
    if not _NUMBER.fullmatch(token):
        raise ValueError('Numeric literal')
    number = Decimal(token)
    sign, digits, exponent = number.as_tuple()
    if not number.is_finite() or abs(exponent) > 1000:
        raise ValueError('Bounded numeric exponent')
    if not any(digits):
        return 0
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    shift = exponent + scale
    if shift < 0 or len(digits) + shift > precision:
        raise ValueError('Exact numeric representation')
    units = int(''.join(map(str, digits))) * 10 ** shift
    return -units if sign else units


def load_spec(path):
    """Read one strict declaration; no database, solver or model is consulted."""
    try:
        source = Path(path).absolute()
        raw = _read(source)
        document = constraints.decode_json(raw.decode('utf-8-sig'))
        if not isinstance(document, dict) or set(document) != _FIELDS:
            raise ValueError('Exact fields')
        if type(document['version']) is not int or document['version'] != VERSION:
            raise ValueError('Version')
        table, column, kind = (document[key] for key in ('table', 'column', 'type'))
        if (not isinstance(table, str) or not _IDENTIFIER.fullmatch(table) or table == 'sxc_candidate'
                or not isinstance(column, str) or not _IDENTIFIER.fullmatch(column)):
            raise ValueError('Identifier')
        rows = document['rows']
        if (not isinstance(rows, list) or len(rows) != 2
                or any(type(value) is not int or not 0 <= value <= witness.ROW_LIMIT for value in rows)
                or rows[0] > rows[1]):
            raise ValueError('Row bounds')
        if not isinstance(kind, str):
            raise ValueError('Scalar type')
        numeric = _TYPE.fullmatch(kind)
        scale, precision = 0, None
        if numeric:
            precision, scale = map(int, numeric.groups())
            if not (1 <= precision <= 38 and 0 <= scale <= precision
                    and kind == f'numeric({precision},{scale})'):
                raise ValueError('Numeric type')
        elif kind not in ('integer', 'date'):
            raise ValueError('Scalar type')
        predicate = document['predicate_sql']
        if not isinstance(predicate, str) or not 1 <= len(predicate) <= PREDICATE_LIMIT:
            raise ValueError('Bounded predicate')
        atom = _ATOM.fullmatch(predicate)
        if atom is None or atom[1] != column:
            raise ValueError('Atomic scalar predicate')
        token = atom[3]
        if not 1 <= len(token) <= LITERAL_LIMIT:
            raise ValueError('Bounded literal')
        if kind == 'integer':
            if not _INTEGER.fullmatch(token) or len(token) > 12:
                raise ValueError('Integer syntax')
            center = int(token)
            if not -(2 ** 31) <= center < 2 ** 31:
                raise ValueError('Integer range')
        elif numeric:
            center = _numeric_units(token, precision, scale)
        else:
            match = _DATE.fullmatch(token)
            if match is None:
                raise ValueError('Date literal')
            center = dt.date.fromisoformat(match[1])
        return Spec(source, hashlib.sha256(raw).hexdigest(), table, column, kind, tuple(rows), center, scale)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, InvalidOperation, RecursionError):
        raise _error() from None


def _fixed(units, scale):
    digits = str(abs(units)).rjust(scale + 1, '0')
    value = digits[:-scale] + '.' + digits[-scale:] if scale else digits
    return ('-' if units < 0 else '') + value


def derive(spec):
    """Pure, bounded witness-v1 document; NULL then ordered representable neighbors."""
    if spec.kind == 'date':
        center = spec.center.toordinal()
        values = [dt.date.fromordinal(value).isoformat() for value in (center - 1, center, center + 1)
                  if dt.date.min.toordinal() <= value <= dt.date.max.toordinal()]
    else:
        if spec.kind == 'integer':
            low, high = -(2 ** 31), 2 ** 31 - 1
        else:
            precision = int(_TYPE.fullmatch(spec.kind)[1])
            high = 10 ** precision - 1
            low = -high
        values = [value for value in (spec.center - 1, spec.center, spec.center + 1) if low <= value <= high]
        if spec.kind != 'integer':
            values = [_fixed(value, spec.scale) for value in values]
    return {'version': VERSION, 'tables': {spec.table: {'rows': list(spec.rows), 'columns': [
        {'name': spec.column, 'type': spec.kind, 'values': [None] + values}]}}}


def _aliases(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def _generator_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _darwin_root_alias(path, meta):
    """Only the two standard Darwin root aliases may stand for plain directories."""
    targets = {'/var': '/private/var', '/tmp': '/private/tmp'}
    if sys.platform != 'darwin' or path.as_posix() not in targets or not stat.S_ISLNK(meta.st_mode):
        return False
    try:
        expected = Path(targets[path.as_posix()])
        target = path.readlink()
        if not target.is_absolute():
            target = path.parent / target
        actual = expected.lstat()
        return (target == expected and stat.S_ISDIR(actual.st_mode)
                and not bool(getattr(actual, 'st_file_attributes', 0) & 0x400)
                and path.resolve(strict=True) == expected)
    except (OSError, ValueError, RuntimeError):
        return False


def _unlinked(path, *, directory):
    """Reject observed link/reparse containers before diagnostic output mutation."""
    path = Path(path).absolute()
    for item in (*reversed(path.parents), path):
        try:
            meta = item.lstat()
        except FileNotFoundError:
            continue
        linked = stat.S_ISLNK(meta.st_mode) or bool(getattr(meta, 'st_file_attributes', 0) & 0x400)
        is_directory = item != path or directory
        if linked:
            if not is_directory or not _darwin_root_alias(item, meta):
                raise _error()
            continue
        if is_directory and not stat.S_ISDIR(meta.st_mode):
            raise _error()
        if not is_directory and not stat.S_ISREG(meta.st_mode):
            raise _error()


def prepare_run(args, out):
    """Read-only protection before CLI discard, .gitignore creation or error writes.

    Check all occurrences of the declared input flags, including forbidden
    configuration and repeated --spec flags. These are bounded observations,
    not a filesystem lock or a defense against hostile ancestor races.
    """
    try:
        if not getattr(args, 'spec', None):
            raise _error()
        directory = Path(out) / 'boundary_schema'
        _unlinked(Path(out), directory=True)
        _unlinked(directory, directory=True)
        outputs = [Path(out), directory, Path(out) / '.gitignore',
                   *[directory / name for name in ('result.json', 'result.json.part', 'witness-schema.json')]]
        inputs = [args.spec, getattr(args, 'config', None), *getattr(args, '_boundary_inputs', ())]
        for value in inputs:
            if value is None:
                continue
            source = Path(value)
            _unlinked(source, directory=False)
            if any(_aliases(source, target) for target in outputs):
                raise _error()
    except StageError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise _error() from None


def run(cfg, args, out):
    """Write one explicit synthetic schema; Result contains hashes/counts only."""
    result = Result('boundary_schema')
    result.not_proven.extend([
        'This fully consumed scalar grammar is a supported subset, not a PostgreSQL parser or SQL sandbox.',
        'Derived finite domains are boundary hints; they do not prove query equivalence or match a caller SQL file.',
        'No PostgreSQL, Z3, model or production data was consulted; actual two-query witness search remains opt-in.',
    ])
    result.next_safe_action = 'Review the generated witness-schema.json, then opt into real PostgreSQL two-query witness search.'
    temporary = None
    try:
        prepare_run(args, out)
        source = Path(args.spec)
        directory = Path(out) / result.stage
        target = directory / 'witness-schema.json'
        if _aliases(source, target):
            raise _error()
        directory.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        generator_sha = _generator_sha()
        spec = load_spec(source)
        document = derive(spec)
        raw = (json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('ascii')
        fd, name = tempfile.mkstemp(prefix='.witness-', suffix='.part', dir=directory)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
        witness.load(temporary)  # Validate the actual output under its existing public consumer.
        if not spec.unchanged() or _generator_sha() != generator_sha:
            result.add('boundary_schema:inputs', 'error', reason='inputs_changed')
            return result
        os.replace(temporary, target)
        with target.open('rb') as stream:
            observed = stream.read(len(raw) + 1)
        if observed != raw:
            if not _aliases(spec.source, target):
                target.unlink(missing_ok=True)
            result.add('boundary_schema:artifact', 'error', reason='artifact_changed')
            return result
        if not spec.unchanged() or _generator_sha() != generator_sha:
            target.unlink(missing_ok=True)
            result.add('boundary_schema:inputs', 'error', reason='inputs_changed')
            return result
        values = document['tables'][spec.table]['columns'][0]['values']
        result.inputs = dict(source_sha256=spec.sha256, generator_sha256=generator_sha,
                             witness_schema_sha256=hashlib.sha256(raw).hexdigest())
        result.add('boundary_schema:derived', 'pass', version=VERSION, tables=1, columns=1,
                   min_rows=spec.rows[0], max_rows=spec.rows[1], domain_values=len(values),
                   non_null_values=len(values) - 1, source_sha256=spec.sha256,
                   generator_sha256=generator_sha, witness_schema_sha256=hashlib.sha256(raw).hexdigest())
        return result
    except StageError:
        raise
    except (OSError, ValueError, TypeError):
        result.add('boundary_schema:artifact', 'error', reason='artifact_unavailable')
        return result
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
