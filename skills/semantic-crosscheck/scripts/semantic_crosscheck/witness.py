"""Bounded synthetic fixtures satisfying a declared finite integrity domain.

Z3 solves cell-domain indices, row counts and the small constraints.Profile
language. SQL query expressions are deliberately absent from this encoding:
fixtures must be compared by the real PostgreSQL evaluator. Exhaustion or
UNSAT establishes nothing about general SQL equivalence. No database is read
or written here, and there is no brute-force fallback when Z3 is unavailable.
"""
from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import importlib
import json
from pathlib import Path
import re
import time
from types import MappingProxyType

from . import constraints
from .result import EXIT_CONFIG, StageError

VERSION = 1
FILE_LIMIT = 256 * 1024
TABLE_LIMIT = 4
COLUMN_LIMIT = 8
ROW_LIMIT = 4
DOMAIN_LIMIT = 8
CELL_LIMIT = 64
_IDENTIFIER = re.compile(r'[a-z_][a-z0-9_]{0,62}\Z')
_NUMERIC_TYPE = re.compile(r'numeric\(([0-9]{1,2}),([0-9]{1,2})\)\Z')


@dataclass(frozen=True)
class Spec:
    source: Path
    sha256: str
    schema: object
    domains: object
    row_bounds: object
    schema_sha256: str

    def unchanged(self):
        try:
            with self.source.open('rb') as stream:
                raw = stream.read(FILE_LIMIT + 1)
            return len(raw) <= FILE_LIMIT and hashlib.sha256(raw).hexdigest() == self.sha256
        except OSError:
            return False


@dataclass(frozen=True)
class Generation:
    """Bounded candidates, never a pass verdict or SQL-equivalence proof.

    Reasons are counts/ids only. The caller records solver_unavailable as
    not_run; every termination reason leaves query equivalence not_proven.
    """
    fixtures: tuple
    reason: str
    solver_checks: int


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Bounded integer')
    return value


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError('Bounded SQL identifier')
    return value


def _kind(value):
    if value in ('text', 'integer', 'date'):
        return value
    numeric = _NUMERIC_TYPE.fullmatch(value) if isinstance(value, str) else None
    if numeric:
        precision, scale = map(int, numeric.groups())
        if 1 <= precision <= 38 and 0 <= scale <= precision and value == f'numeric({precision},{scale})':
            return value
    raise ValueError('Unsupported finite-domain type')


def load(path):
    """Strict, hashed user schema; finite bounds checked before solver or DB use."""
    try:
        source = Path(path)
        with source.open('rb') as stream:
            raw = stream.read(FILE_LIMIT + 1)
        if not raw or len(raw) > FILE_LIMIT:
            raise ValueError('Spec size')
        document = constraints.decode_json(raw.decode('utf-8-sig'))
        constraints._fields(document, {'version', 'tables'}, {'version', 'tables'})
        _integer(document['version'], VERSION, VERSION)
        tables = document['tables']
        if not isinstance(tables, dict) or not 1 <= len(tables) <= TABLE_LIMIT:
            raise ValueError('Tables map')
        schema, domains, row_bounds, cells = {}, {}, {}, 0
        for table in sorted(tables):
            _identifier(table)
            if table == 'sxc_candidate':
                raise ValueError('Reserved internal CTE name')
            rules = tables[table]
            constraints._fields(rules, {'columns', 'rows'}, {'columns', 'rows'})
            columns = constraints._list(rules['columns'], COLUMN_LIMIT, nonempty=True)
            bounds = constraints._list(rules['rows'], 2, nonempty=True)
            if len(bounds) != 2:
                raise ValueError('Row bounds')
            low, high = (_integer(value, 0, ROW_LIMIT) for value in bounds)
            if low > high:
                raise ValueError('Ordered row bounds')
            row_bounds[table] = (low, high)
            cells += high * len(columns)
            if cells > CELL_LIMIT:
                raise ValueError('Total cell bound')
            typed, values, names = [], [], set()
            for column in columns:
                constraints._fields(column, {'name', 'type', 'values'}, {'name', 'type', 'values'})
                name, kind = _identifier(column['name']), _kind(column['type'])
                if name in names:
                    raise ValueError('Duplicate column')
                names.add(name)
                typed.append((name, kind))
                domain = []
                for value in constraints._list(column['values'], DOMAIN_LIMIT, nonempty=True):
                    stored = constraints._value(value, kind)
                    # PostgreSQL numeric casts can collapse distinct input literals.
                    if stored not in domain:
                        domain.append(stored)
                values.append(tuple(domain))
            schema[table], domains[table] = tuple(typed), tuple(values)
        schema_json = json.dumps(schema, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return Spec(source.resolve(), hashlib.sha256(raw).hexdigest(), MappingProxyType(schema),
                    MappingProxyType(domains), MappingProxyType(row_bounds), hashlib.sha256(schema_json).hexdigest())
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, InvalidOperation, RecursionError):
        raise StageError('counterexample: witness spec missing, too large or outside the supported version 1 contract',
                         EXIT_CONFIG) from None


def binding(spec, profile=None):
    """Exact declaration, typed-schema and constraint-profile identity for replay."""
    return {'mode': 'constraint_witness', 'version': VERSION, 'spec_sha256': spec.sha256,
            'schema_sha256': spec.schema_sha256, 'profile_sha256': profile.sha256 if profile else None}


def validate_fixture(spec, fixture, profile=None):
    """Validate replay/model rows, cast-domain membership and declared integrity.

    Membership is after the same casts as the synthetic PostgreSQL CTEs. Row
    bounds apply to original solver fixtures; a reduced witness may have fewer
    rows and is checked separately by the caller's constraint-aware reducer.
    """
    try:
        constraints.validate_fixture(fixture, spec.schema, profile)
        for table, columns in spec.schema.items():
            low, high = spec.row_bounds[table]
            if not low <= len(fixture[table]) <= high:
                raise ValueError('Fixture row count outside declared bounds')
            for row in fixture[table]:
                for index, (value, (_, kind)) in enumerate(zip(row, columns)):
                    if constraints._value(value, kind) not in spec.domains[table][index]:
                        raise ValueError('Fixture value outside finite domain')
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, InvalidOperation):
        raise constraints.ConstraintViolation('Synthetic fixture is outside its declared finite witness domain') from None


def _json_value(value):
    if isinstance(value, Decimal):
        return format(abs(value) if value == 0 else value, 'f')
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _matches(value, op, values):
    if value is None:
        return True  # SQL CHECK UNKNOWN passes.
    threshold = values[0]
    return (value in values if op == 'in' else value == threshold if op == 'eq' else
            value != threshold if op == 'ne' else value < threshold if op == 'lt' else
            value <= threshold if op == 'le' else value > threshold if op == 'gt' else value >= threshold)


class _Stopped(Exception):
    def __init__(self, reason):
        self.reason = reason


def generate(spec, profile=None, max_examples=64, timeout_ms=1000):
    """Enumerate lexicographic finite-domain models within one wall-clock budget.

    Row counts and active cell indices define order. Inactive cells are fixed
    to index zero, and cast-equivalent domain entries are collapsed at load,
    so a fixture cannot reappear through an unused or duplicate index.
    """
    try:
        _integer(max_examples, 1, 1000)
        _integer(timeout_ms, 1, 60000)
    except ValueError:
        raise StageError('counterexample: witness max examples must be 1..1000 and timeout milliseconds 1..60000',
                         EXIT_CONFIG) from None
    # perf_counter is monotonic and uses the high-resolution QPC on Windows;
    # Python 3.12's monotonic clock there has a 15.625 ms resolution.
    started = time.perf_counter()
    fixtures, checks = [], 0

    def finish(reason):
        if not spec.unchanged():
            return Generation((), 'spec_changed', checks)
        if profile is not None and not profile.unchanged():
            return Generation((), 'profile_changed', checks)
        return Generation(tuple(fixtures), reason, checks)

    if not spec.unchanged():
        return finish('spec_changed')
    if profile is not None:
        if not profile.unchanged():
            return finish('profile_changed')
        # Profile has no stored schema: validate its source against this schema
        # before using column indices supplied by another profile load.
        current = constraints.load(profile.source, spec.schema)
        if (current.sha256, set(current.not_null), current.unique, current.foreign_keys, current.checks) != (
                profile.sha256, set(profile.not_null), profile.unique, profile.foreign_keys, profile.checks):
            raise StageError('counterexample: witness constraint profile is bound to another schema', EXIT_CONFIG)
    try:
        z3 = importlib.import_module('z3')
    except (ImportError, OSError):
        return finish('solver_unavailable')

    deadline = started + timeout_ms / 1000

    def remaining():
        milliseconds = int((deadline - time.perf_counter()) * 1000)
        if milliseconds <= 0:
            raise _Stopped('solver_timeout')
        return milliseconds

    try:
        remaining()
        solver = z3.Solver()
        solver.set(random_seed=0)
        counts, cells, variables = {}, {}, []
        for table_number, (table, columns) in enumerate(spec.schema.items()):
            remaining()
            low, high = spec.row_bounds[table]
            count = z3.Int(f'count_{table_number}')
            counts[table] = count
            solver.add(count >= low, count <= high)
            variables.append((count, low, high))
            for row in range(high):
                for column in range(len(columns)):
                    remaining()
                    cell = z3.Int(f'cell_{table_number}_{row}_{column}')
                    cells[table, row, column] = cell
                    end = len(spec.domains[table][column]) - 1
                    solver.add(cell >= 0, cell <= end, z3.Implies(count <= row, cell == 0))
                    variables.append((cell, 0, end))

        def null(table, row, column):
            domain = spec.domains[table][column]
            return cells[table, row, column] == domain.index(None) if None in domain else z3.BoolVal(False)

        def equal(left, right):
            remaining()
            lt, lr, lc = left
            rt, rr, rc = right
            left_domain, right_domain = spec.domains[lt][lc], spec.domains[rt][rc]
            return z3.Or([z3.And(cells[left] == li, cells[right] == ri)
                          for li, lv in enumerate(left_domain) for ri, rv in enumerate(right_domain) if lv == rv])

        if profile is not None:
            for table, column in profile.not_null:
                for row in range(spec.row_bounds[table][1]):
                    remaining()
                    solver.add(z3.Implies(counts[table] > row, z3.Not(null(table, row, column))))
            for table, columns in profile.unique:
                for left in range(spec.row_bounds[table][1]):
                    for right in range(left + 1, spec.row_bounds[table][1]):
                        remaining()
                        nullable = [null(table, row, column) for row in (left, right) for column in columns]
                        same = z3.And([equal((table, left, column), (table, right, column)) for column in columns])
                        solver.add(z3.Implies(counts[table] > right, z3.Or(nullable + [z3.Not(same)])))
            for table, columns, target, target_columns in profile.foreign_keys:
                for row in range(spec.row_bounds[table][1]):
                    remaining()
                    nullable = [null(table, row, column) for column in columns]
                    matches = [z3.And([counts[target] > other] + [
                        equal((table, row, column), (target, other, target_column))
                        for column, target_column in zip(columns, target_columns)])
                        for other in range(spec.row_bounds[target][1])]
                    solver.add(z3.Implies(counts[table] > row, z3.Or(nullable + matches)))
            for table, column, op, values in profile.checks:
                remaining()
                allowed = [index for index, value in enumerate(spec.domains[table][column])
                           if _matches(value, op, values)]
                for row in range(spec.row_bounds[table][1]):
                    remaining()
                    solver.add(z3.Implies(counts[table] > row,
                                         z3.Or([cells[table, row, column] == index for index in allowed])))

        def check():
            nonlocal checks
            solver.set(timeout=remaining())
            checks += 1
            answer = solver.check()
            remaining()  # A solver timeout or overrun cannot expose its partial model.
            if answer == z3.unknown:
                reason = solver.reason_unknown().lower()
                raise _Stopped('solver_timeout' if 'timeout' in reason or 'canceled' in reason else 'solver_unknown')
            return answer

        while len(fixtures) < max_examples:
            if check() == z3.unsat:
                return finish('space_exhausted' if fixtures else 'unsatisfiable')
            solver.push()
            chosen = []
            # Each prefix is fixed only after a SAT check. Binary search finds
            # the minimum feasible integer without enumerating combinations.
            for variable, low, high in variables:
                while low < high:
                    middle = (low + high) // 2
                    solver.push()
                    solver.add(variable <= middle)
                    feasible = check() == z3.sat
                    solver.pop()
                    if feasible:
                        high = middle
                    else:
                        low = middle + 1
                solver.add(variable == low)
                chosen.append(low)
            if check() != z3.sat:
                raise _Stopped('model_validation_failed')
            model = solver.model()
            fixture = {}
            for table, columns in spec.schema.items():
                count = model.eval(counts[table], model_completion=True).as_long()
                fixture[table] = [[_json_value(spec.domains[table][column][
                    model.eval(cells[table, row, column], model_completion=True).as_long()])
                    for column in range(len(columns))] for row in range(count)]
            try:
                validate_fixture(spec, fixture, profile)
            except constraints.ConstraintViolation:
                raise _Stopped('model_validation_failed') from None
            remaining()
            solver.pop()
            fixtures.append(fixture)
            solver.add(z3.Or([variable != value for (variable, _, _), value in zip(variables, chosen)]))
        return finish('example_limit')
    except _Stopped as stopped:
        return finish(stopped.reason)
    except z3.Z3Exception:
        return finish('solver_error')
