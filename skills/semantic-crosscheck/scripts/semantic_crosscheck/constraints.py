"""Small, declarative integrity domain for the fixed synthetic SQL schema.

No SQL is generated or evaluated here. Values are compared after the casts
used by the fixture CTEs; CHECK constants keep their own numeric precision.
Text equality uses exact code points; constrained fixture CTEs explicitly use
PostgreSQL C collation. Production collations and text ordering are outside
this language.
This is a bounded profile language, not a PostgreSQL DDL interpreter.
"""
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import math
from pathlib import Path
import re

from .result import EXIT_CONFIG, StageError

VERSION = 1
FILE_LIMIT = 256 * 1024
RULE_LIMIT = 128
CELL_LIMIT = 4096
ROW_LIMIT = 10000
OPS = frozenset(('eq', 'ne', 'lt', 'le', 'gt', 'ge', 'in'))
_DECIMAL = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
_INTEGER = re.compile(r'[+-]?[0-9]+\Z')
_NUMERIC_TYPE = re.compile(r'numeric\(([0-9]+),([0-9]+)\)\Z')


class ConstraintViolation(ValueError):
    """A fixture is outside the declared domain; never include cell values."""


@dataclass(frozen=True)
class Profile:
    source: Path
    sha256: str
    not_null: tuple
    unique: tuple
    foreign_keys: tuple
    checks: tuple

    def unchanged(self):
        try:
            with self.source.open('rb') as stream:
                raw = stream.read(FILE_LIMIT + 1)
            return len(raw) <= FILE_LIMIT and hashlib.sha256(raw).hexdigest() == self.sha256
        except OSError:
            return False


def binding(profile):
    return {'mode': 'constrained' if profile else 'unconstrained', 'version': VERSION,
            'sha256': profile.sha256 if profile else None}


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Duplicate JSON key')
        obj[key] = value
    return obj


def _constant(_value):
    raise ValueError('Non-finite JSON number')


def decode_json(raw):
    """Strict JSON for profiles and replay: duplicate keys/nonfinite values fail."""
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant, parse_float=Decimal)


def _fields(value, required, allowed):
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed:
        raise ValueError('Object fields')


def _list(value, maximum=RULE_LIMIT, nonempty=False):
    if not isinstance(value, list) or len(value) > maximum or (nonempty and not value):
        raise ValueError('Bounded list')
    return value


def _table(name, schema):
    if not isinstance(name, str) or name not in schema:
        raise ValueError('Unknown table')
    return name


def _columns(table, names, schema):
    names = _list(names, len(schema[table]), nonempty=True)
    columns = {name: (index, kind) for index, (name, kind) in enumerate(schema[table])}
    if any(not isinstance(name, str) or name not in columns for name in names) or len(set(names)) != len(names):
        raise ValueError('Unknown or repeated column')
    return tuple(columns[name][0] for name in names)


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Numeric scalar')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Finite numeric scalar')
    text = str(value)
    if len(text) > 256 or not _DECIMAL.fullmatch(text):
        raise ValueError('Numeric syntax')
    number = Decimal(text)
    if not number.is_finite() or abs(number.as_tuple().exponent) > 1000:
        raise ValueError('Finite bounded numeric scalar')
    return number


def _value(value, kind, literal=False):
    if value is None:
        if literal:
            raise ValueError('Non-null CHECK literal')
        return None
    if kind == 'text':
        if not isinstance(value, str) or len(value) > CELL_LIMIT or '\0' in value:
            raise ValueError('Text scalar')
        # Also reject unpaired surrogates before the DB driver sees them.
        value.encode('utf-8')
        return value
    if kind == 'date':
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return value
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            raise ValueError('ISO date scalar')
        return dt.date.fromisoformat(value)
    if kind == 'integer':
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError('Integer scalar')
        text = str(value)
        if len(text) > 12 or not _INTEGER.fullmatch(text):
            raise ValueError('Integer syntax')
        number = int(text)
        if not -(2**31) <= number < 2**31:
            raise ValueError('Integer range')
        return number
    numeric = _NUMERIC_TYPE.fullmatch(kind)
    if numeric:
        number = _decimal(value)
        if literal:
            return number
        precision, scale = map(int, numeric.groups())
        with localcontext() as context:
            context.prec = max(precision + scale + 2, len(number.as_tuple().digits) + scale + 2)
            stored = number.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        if abs(stored) >= Decimal(10) ** (precision - scale):
            raise ValueError('Numeric range')
        return stored
    raise ValueError('Unsupported synthetic type')


def load(path, schema):
    """Validate a whole profile before any connection; retain its exact byte hash."""
    try:
        source = Path(path)
        with source.open('rb') as stream:
            raw = stream.read(FILE_LIMIT + 1)
        if not raw or len(raw) > FILE_LIMIT:
            raise ValueError('Profile size')
        document = decode_json(raw.decode('utf-8-sig'))
        _fields(document, {'version', 'tables'}, {'version', 'tables', 'foreign_keys', 'checks'})
        if type(document['version']) is not int or document['version'] != VERSION:
            raise ValueError('Profile version')
        if not isinstance(document['tables'], dict) or len(document['tables']) > len(schema):
            raise ValueError('Tables map')
        not_null, unique, foreign_keys, checks = [], [], [], []
        for table, rules in document['tables'].items():
            _table(table, schema)
            _fields(rules, set(), {'primary_key', 'unique', 'not_null'})
            if 'not_null' in rules:
                names = _list(rules['not_null'], len(schema[table]))
                if names:
                    not_null.extend((table, index) for index in _columns(table, names, schema))
            if 'primary_key' in rules:
                key = _columns(table, rules['primary_key'], schema)
                unique.append((table, key))
                not_null.extend((table, index) for index in key)
            for names in _list(rules.get('unique', [])):
                key = (table, _columns(table, names, schema))
                if key in unique:
                    raise ValueError('Repeated key')
                unique.append(key)
        for rule in _list(document.get('foreign_keys', [])):
            _fields(rule, {'table', 'columns', 'references'}, {'table', 'columns', 'references'})
            table = _table(rule['table'], schema)
            columns = _columns(table, rule['columns'], schema)
            reference = rule['references']
            _fields(reference, {'table', 'columns'}, {'table', 'columns'})
            target = _table(reference['table'], schema)
            target_columns = _columns(target, reference['columns'], schema)
            if len(columns) != len(target_columns) or (target, target_columns) not in unique:
                raise ValueError('Foreign key requires declared target key')
            kinds = lambda name, indices: tuple(schema[name][index][1] for index in indices)
            # This language requires identical declared types; no implicit casts.
            if kinds(table, columns) != kinds(target, target_columns):
                raise ValueError('Foreign key types')
            foreign_keys.append((table, columns, target, target_columns))
        for rule in _list(document.get('checks', [])):
            _fields(rule, {'table', 'column', 'op', 'value'}, {'table', 'column', 'op', 'value'})
            table = _table(rule['table'], schema)
            column = _columns(table, [rule['column']], schema)[0]
            kind = schema[table][column][1]
            op = rule['op']
            if not isinstance(op, str) or op not in OPS:
                raise ValueError('CHECK operator')
            if kind == 'text' and op not in ('eq', 'ne', 'in'):
                raise ValueError('Ordered text CHECK requires an undeclared collation')
            values = _list(rule['value'], nonempty=True) if op == 'in' else [rule['value']]
            checks.append((table, column, op, tuple(_value(value, kind, literal=True) for value in values)))
        if len(not_null) + len(unique) + len(foreign_keys) + len(checks) > RULE_LIMIT:
            raise ValueError('Total constraint limit')
        return Profile(source.resolve(), hashlib.sha256(raw).hexdigest(), tuple(set(not_null)),
                       tuple(unique), tuple(foreign_keys), tuple(checks))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, InvalidOperation, RecursionError):
        raise StageError('counterexample: constraints profile missing, too large or outside the supported version 1 contract', EXIT_CONFIG) from None


def validate_fixture(fixture, schema, profile=None):
    """Raise on malformed data or a violated rule; SQL CHECK UNKNOWN passes."""
    try:
        if not isinstance(fixture, dict) or set(fixture) != set(schema):
            raise ValueError('Fixture tables')
        normalized, row_count = {}, 0
        for table, columns in schema.items():
            rows = _list(fixture[table], ROW_LIMIT)
            row_count += len(rows)
            if row_count > ROW_LIMIT:
                raise ValueError('Fixture row limit')
            normalized[table] = []
            for row in rows:
                if not isinstance(row, list) or len(row) != len(columns):
                    raise ValueError('Fixture row width')
                normalized[table].append(tuple(_value(value, kind) for value, (_, kind) in zip(row, columns)))
        if profile is None:
            return
        for table, column in profile.not_null:
            if any(row[column] is None for row in normalized[table]):
                raise ValueError('NOT NULL')
        for table, columns in profile.unique:
            seen = set()
            for row in normalized[table]:
                key = tuple(row[index] for index in columns)
                if None in key:  # PostgreSQL default NULLS DISTINCT, including composite keys.
                    continue
                if key in seen:
                    raise ValueError('UNIQUE')
                seen.add(key)
        for table, columns, target, target_columns in profile.foreign_keys:
            available = {tuple(row[index] for index in target_columns) for row in normalized[target]}
            for row in normalized[table]:
                key = tuple(row[index] for index in columns)
                if None not in key and key not in available:  # MATCH SIMPLE
                    raise ValueError('FOREIGN KEY')
        for table, column, op, values in profile.checks:
            for row in normalized[table]:
                value = row[column]
                if value is None:
                    continue  # SQL UNKNOWN satisfies CHECK; NOT NULL is independent.
                threshold = values[0]
                matches = (value in values if op == 'in' else value == threshold if op == 'eq' else
                           value != threshold if op == 'ne' else value < threshold if op == 'lt' else
                           value <= threshold if op == 'le' else value > threshold if op == 'gt' else
                           value >= threshold)
                if not matches:
                    raise ValueError('CHECK')
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, InvalidOperation):
        raise ConstraintViolation('Synthetic fixture is outside its supported integrity domain') from None
