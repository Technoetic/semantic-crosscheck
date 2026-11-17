"""Optional bounded differential-plan experiment over a closed synthetic language.

No supplied SQL is accepted. Setup writes only an owned temporary table, then
the identical ordinary SELECT runs under two observed plans in one read-only
repeatable-read transaction. Public evidence contains hashes/counts only.
"""
from collections import Counter
from datetime import datetime
from decimal import Decimal, DecimalException
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

from . import db
from .result import EXIT_CONFIG, Result, StageError

SPEC_LIMIT = 256 * 1024
MAX_ROWS = 2000
MAX_OPERATIONS = 100000
TYPES = frozenset(('integer', 'numeric', 'text', 'boolean', 'timestamp', 'timestamptz'))
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,62}\Z')
ENVIRONMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
ZONE = re.compile(r'[A-Za-z0-9_+./:-]{1,128}\Z')
TABLE = 'sxc_plan_compare_data'
INDEX = 'sxc_plan_compare_index'
SEQUENTIAL = {'enable_seqscan': 'on', 'enable_indexscan': 'off',
              'enable_indexonlyscan': 'off', 'enable_bitmapscan': 'off'}
INDEXED = {'enable_seqscan': 'off', 'enable_indexscan': 'on',
           'enable_indexonlyscan': 'on', 'enable_bitmapscan': 'off'}
OPS = {'eq': '=', 'ne': '<>', 'lt': '<', 'le': '<=', 'gt': '>', 'ge': '>='}


def _darwin_alias(path, meta):
    """Permit only the verified standard macOS root-directory aliases."""
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
    """Reject filesystem links/reparse points before any cleanup or write."""
    path = Path(path).absolute()
    for item in (*reversed(path.parents), path):
        try:
            meta = item.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        is_dir = item != path or directory
        linked = stat.S_ISLNK(meta.st_mode) or bool(getattr(meta, 'st_file_attributes', 0) & 0x400)
        if linked:
            if not is_dir or not _darwin_alias(item, meta):
                raise ValueError('linked path')
        elif is_dir:
            if not stat.S_ISDIR(meta.st_mode):
                raise ValueError('directory required')
        elif not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise ValueError('unlinked regular file required')


def _aliases(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def prepare_run(args, out, *, protected_paths=()):
    """Read-only guards for CLI cleanup/publication, including unused config."""
    try:
        directory = Path(out) / 'plan_compare'
        _inspect(out, directory=True, missing=True)
        _inspect(directory, directory=True, missing=True)
        targets = [Path(out) / '.gitignore', directory / 'result.json', directory / 'result.json.part']
        for target in targets:
            _inspect(target, missing=True)
        inputs = [args.spec, getattr(args, 'config', None),
                  *getattr(args, '_plan_compare_inputs', ()), *protected_paths]
        for item in inputs:
            if item is None:
                continue
            source = Path(item)
            _inspect(source, missing=True)
            if any(_aliases(source, target) for target in (Path(out), directory, *targets)):
                raise ValueError('output alias')
    except Exception:
        raise StageError('plan-compare: invalid_or_protected_inputs', EXIT_CONFIG) from None


def _invalid():
    raise StageError('plan-compare: invalid or unsupported bounded synthetic specification', EXIT_CONFIG)


def _bounded_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()
    return value


def _read(path):
    _inspect(path)
    with Path(path).open('rb') as stream:
        raw = stream.read(SPEC_LIMIT + 1)
    if len(raw) > SPEC_LIMIT:
        _invalid()
    return raw


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _invalid()
        value[key] = item
    return value


def _constant(_value):
    _invalid()


def _decimal_token(value):
    """Bound coefficient/exponent text before asking Decimal to convert it."""
    if len(value) > 256:
        _invalid()
    match = re.fullmatch(r'[+-]?(?:([0-9]+)(?:\.([0-9]*))?|\.([0-9]+))(?:[eE]([+-]?[0-9]+))?', value.strip())
    if match is None:
        _invalid()
    whole, fraction, fraction_only, exponent = match.groups()
    fraction = fraction if fraction is not None else fraction_only or ''
    digits = ((whole or '') + fraction).lstrip('0') or '0'
    # The exponent conversion itself must stay bounded, even for a tiny value
    # with an enormous exponent token. Leading zeroes do not change magnitude.
    magnitude = (exponent or '0').lstrip('+-').lstrip('0') or '0'
    if len(digits) > 128 or len(magnitude) > 3:
        _invalid()
    effective_exponent = int(exponent or '0') - len(fraction)
    if abs(effective_exponent) > 512:
        _invalid()
    return Decimal(value)


def _integer_token(value):
    if len(value.lstrip('-')) > 128:
        _invalid()
    return int(value)


def _cell(value, kind, nullable=True):
    if value is None:
        if nullable:
            return None
        _invalid()
    if kind == 'integer':
        return _bounded_integer(value, -(2**63), 2**63 - 1)
    if kind == 'boolean':
        if type(value) is not bool:
            _invalid()
        return value
    if kind == 'text':
        if not isinstance(value, str) or len(value) > 4096 or '\0' in value:
            _invalid()
        return value
    if kind == 'numeric':
        if type(value) not in (str, int, Decimal) or isinstance(value, str) and len(value) > 256:
            _invalid()
        try:
            number = _decimal_token(value) if isinstance(value, str) else Decimal(value)
        except (DecimalException, ValueError):
            _invalid()
        if not number.is_finite() or len(number.as_tuple().digits) > 128 or abs(number.as_tuple().exponent) > 512:
            _invalid()
        return number
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?', value):
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        _invalid()
    if (kind == 'timestamp') != (parsed.tzinfo is None):
        _invalid()
    return parsed


def load_spec(path):
    """Read at most 256 KiB and validate every field before opening a connection."""
    try:
        raw = _read(path)
        spec = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                          parse_int=_integer_token, parse_float=_decimal_token, parse_constant=_constant)
        required = {'schema_version', 'columns', 'rows', 'projection', 'predicate', 'index', 'timezone'}
        if not isinstance(spec, dict) or not required <= spec.keys() or spec.keys() - required - {'max_rows', 'operation_budget'}:
            _invalid()
        if type(spec['schema_version']) is not int or spec['schema_version'] != 1:
            _invalid()
        columns = spec['columns']
        if not isinstance(columns, dict) or not 1 <= len(columns) <= 16:
            _invalid()
        for name, kind in columns.items():
            if not IDENTIFIER.fullmatch(name) or not isinstance(kind, str) or kind not in TYPES:
                _invalid()
        projection = spec['projection']
        if (not isinstance(projection, list) or not projection or len(projection) > len(columns)
                or any(not isinstance(c, str) or c not in columns for c in projection)
                or len(set(projection)) != len(projection)):
            _invalid()
        if not isinstance(spec['index'], str) or spec['index'] not in columns:
            _invalid()
        if not isinstance(spec['timezone'], str) or not ZONE.fullmatch(spec['timezone']):
            _invalid()
        rows = spec['rows']
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            _invalid()
        converted = []
        for row in rows:
            if isinstance(row, dict) and row.keys() == columns.keys():
                row = [row[c] for c in columns]
            if not isinstance(row, list) or len(row) != len(columns):
                _invalid()
            converted.append([_cell(cell, kind) for cell, kind in zip(row, columns.values())])
        spec['rows'] = converted
        terms = spec['predicate']
        if not isinstance(terms, list) or not 1 <= len(terms) <= 16:
            _invalid()
        for term in terms:
            if not isinstance(term, dict) or not isinstance(term.get('column'), str) or term['column'] not in columns:
                _invalid()
            op = term.get('op')
            if op in ('is_null', 'is_not_null'):
                if term.keys() != {'column', 'op'}:
                    _invalid()
                continue
            if not isinstance(op, str) or op not in OPS or term.keys() != {'column', 'op', 'value'}:
                _invalid()
            typed = term['value']
            if not isinstance(typed, dict) or typed.keys() != {'type', 'value'}:
                _invalid()
            kind = typed['type']
            if not isinstance(kind, str) or kind not in TYPES:
                _invalid()
            column_kind = columns[term['column']]
            compatible = kind == column_kind or {kind, column_kind} <= {'integer', 'numeric'} or {kind, column_kind} <= {'timestamp', 'timestamptz'}
            if not compatible:
                _invalid()
            typed['value'] = _cell(typed['value'], kind, nullable=False)
        spec['max_rows'] = _bounded_integer(spec.get('max_rows', MAX_ROWS), 1, MAX_ROWS)
        spec['operation_budget'] = _bounded_integer(spec.get('operation_budget', MAX_OPERATIONS), 1, MAX_OPERATIONS)
        spec['spec_sha256'] = hashlib.sha256(raw).hexdigest()
        return spec
    except StageError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, OverflowError, DecimalException):
        _invalid()


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _query(spec):
    expressions, values = [], {'row_limit': spec['max_rows'] + 1}
    for index, term in enumerate(spec['predicate']):
        column = _quote(term['column'])
        if term['op'] in ('is_null', 'is_not_null'):
            expressions.append(column + (' IS NULL' if term['op'] == 'is_null' else ' IS NOT NULL'))
        else:
            key = f'predicate_{index}'
            typed = term['value']
            values[key] = typed['value']
            kind = 'bigint' if typed['type'] == 'integer' else typed['type']
            expressions.append(f'{column} {OPS[term["op"]]} CAST(:{key} AS {kind})')
    # The cap+1 limit bounds materialization; partial answers are never compared.
    sql = (f'SELECT {", ".join(map(_quote, spec["projection"]))} FROM pg_temp.{_quote(TABLE)} '
           f'WHERE {" AND ".join(expressions)} LIMIT :row_limit')
    return sql, values


def _setup(conn, spec, timeout):
    conn.run('BEGIN')
    try:
        conn.run(f'SET LOCAL statement_timeout = {timeout}')
        definitions = ', '.join(f'{_quote(name)} {"bigint" if kind == "integer" else kind}' for name, kind in spec['columns'].items())
        conn.run(f'CREATE TEMP TABLE {_quote(TABLE)} ({definitions}) ON COMMIT PRESERVE ROWS')
        # At most twenty insert statements; cells are always parameters, including text.
        for offset in range(0, len(spec['rows']), 100):
            bindings, tuples = {}, []
            for r, row in enumerate(spec['rows'][offset:offset + 100]):
                placeholders = []
                for c, (cell, kind) in enumerate(zip(row, spec['columns'].values())):
                    key = f'cell_{r}_{c}'
                    bindings[key] = cell
                    placeholders.append(f'CAST(:{key} AS {"bigint" if kind == "integer" else kind})')
                tuples.append('(' + ', '.join(placeholders) + ')')
            conn.run(f'INSERT INTO pg_temp.{_quote(TABLE)} VALUES ' + ', '.join(tuples), **bindings)
        conn.run(f'CREATE INDEX {_quote(INDEX)} ON pg_temp.{_quote(TABLE)} ({_quote(spec["index"])})')
        conn.run(f'ANALYZE pg_temp.{_quote(TABLE)}')
        conn.run('COMMIT')
    except Exception:
        conn.run('ROLLBACK')
        raise


def _structure(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get('Node Type'), str):
        raise ValueError('invalid plan')
    # Only physical attributes: never cost/rows/width/conditions/output/literals.
    keys = ('Node Type', 'Parent Relationship', 'Join Type', 'Strategy', 'Partial Mode',
            'Parallel Aware', 'Scan Direction')
    structure = {key: plan[key] for key in keys if key in plan}
    children = plan.get('Plans', [])
    if not isinstance(children, list):
        raise ValueError('invalid plan')
    if children:
        structure['Plans'] = [_structure(child) for child in children]
    return structure


def plan_fingerprint(plan):
    return hashlib.sha256(json.dumps(_structure(plan), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _node_types(plan):
    yield plan['Node Type']
    for child in plan.get('Plans', []):
        yield from _node_types(child)


def _explain(conn, query, bindings):
    payload = conn.run('EXPLAIN (FORMAT JSON) ' + query, **bindings)[0][0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError('invalid plan')
    plan = payload[0]['Plan']
    _structure(plan)
    return plan


def _typed(value):
    if value is None:
        return ('null',)
    if type(value) is bool:
        return ('boolean', value)
    if type(value) is int:
        return ('integer', value)
    if isinstance(value, Decimal) and value.is_finite():
        return ('numeric', value)
    if isinstance(value, datetime):
        return ('timestamp' if value.tzinfo is None else 'timestamptz', value)
    if type(value) is str:
        return ('text', value)
    raise ValueError('unsupported result type')


def bags_equal(left, right):
    """Counter equality compares every typed projected cell and its multiplicity."""
    return Counter(tuple(_typed(cell) for cell in row) for row in left) == Counter(tuple(_typed(cell) for cell in row) for row in right)


def _cost(spec):
    # Conservative host-work units: setup cells, predicate evaluations and both
    # full projected result bags; fixed allowance covers bounded DB statements.
    return len(spec['rows']) * (len(spec['columns']) + 2 * len(spec['predicate']) + 2 * len(spec['projection'])) + 64


def evaluate(conn, spec, timeout):
    """Own setup/transaction only; returns sanitized evidence, never fetched rows."""
    _setup(conn, spec, timeout)
    query, bindings = _query(spec)
    plans, answers = [], []
    conn.run('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
    try:
        conn.run(f'SET LOCAL statement_timeout = {timeout}')
        conn.run("SELECT set_config('TimeZone', :timezone, true)", timezone=spec['timezone'])
        for settings in (SEQUENTIAL, INDEXED):
            for flag, value in settings.items():
                conn.run(f'SET LOCAL {flag} = {value}')
            plans.append(_explain(conn, query, bindings))
            rows = conn.run(query, **bindings)
            if not isinstance(rows, list) or any(not isinstance(row, (list, tuple)) or len(row) != len(spec['projection']) for row in rows):
                raise ValueError('invalid rows')
            answers.append(rows)
        hashes = [plan_fingerprint(plan) for plan in plans]
        detail = dict(sequential_plan_sha256=hashes[0], index_plan_sha256=hashes[1],
                      sequential_rows=len(answers[0]), index_rows=len(answers[1]),
                      sequential_flags=dict(SEQUENTIAL), index_flags=dict(INDEXED), bounded=True)
        if any(len(rows) > spec['max_rows'] for rows in answers):
            return 'not_run', dict(detail, reason='row_cap_exceeded')
        sequential_nodes, index_nodes = [set(_node_types(plan)) for plan in plans]
        index_scans = {'Index Scan', 'Index Only Scan', 'Bitmap Index Scan'}
        if (hashes[0] == hashes[1] or 'Seq Scan' not in sequential_nodes
                or sequential_nodes & index_scans or not index_nodes & index_scans):
            return 'not_run', dict(detail, reason='insufficient_plan_diversity')
        # Two empty answers exercise no comparison truth. A one-sided empty
        # answer is a genuine discrepancy and must remain a failure.
        if not any(answers):
            return 'not_run', dict(detail, reason='empty_answer')
        equal = bags_equal(*answers)
        return ('pass' if equal else 'fail'), dict(detail, multiset_equal=equal)
    finally:
        conn.run('ROLLBACK')


def run(cfg, args, out: Path):
    """Standalone diagnostic: no operational configuration or DSN fallback."""
    prepare_run(args, out, protected_paths=(getattr(cfg, 'path', None),))
    if cfg is not None:
        raise StageError('plan-compare: operational configuration is not supported', EXIT_CONFIG)
    name = getattr(args, 'dsn_env', 'SXC_TEST_ADMIN_DSN')
    if not isinstance(name, str) or not ENVIRONMENT.fullmatch(name):
        _invalid()
    timeout = _bounded_integer(getattr(args, 'statement_timeout_ms', 1000), 1, 120000)
    spec = load_spec(args.spec)
    result = Result('plan_compare')
    result._suppress_paths = True
    result.inputs = {'spec_sha256': spec['spec_sha256']}
    result.not_proven = [
        'A pass means the same full typed row bag under two observed physical plans for this synthetic case only; it does not establish global query correctness or business correctness.',
        'A mismatch identifies a disagreement, not which answer is true. This bounded optional experiment admits no free SQL, joins, aggregates, subqueries or other query classes.',
        'This version performs no automatic reduction and claims no minimality; future reduction must preserve both the answer difference and observed plan diversity.'
    ]
    result.next_safe_action = 'Review the original synthetic specification and observed plan evidence before extending the bounded experiment.'
    if _cost(spec) > spec['operation_budget']:
        result.add('plan_compare.consistency', 'not_run', reason='operation_budget_exceeded')
        return result
    if not spec['rows']:
        result.add('plan_compare.consistency', 'not_run', reason='empty_fixture')
        return result
    conn = None
    try:
        try:
            conn = db.connect(name, require_pg17=False)
        except StageError:
            result.add('plan_compare.consistency', 'not_run', reason='postgresql_unavailable')
            return result
        status, detail = evaluate(conn, spec, timeout)
        if hashlib.sha256(_read(args.spec)).hexdigest() != spec['spec_sha256']:
            result.add('plan_compare.consistency', 'error', reason='input_changed')
        else:
            result.add('plan_compare.consistency', status, **detail)
        return result
    except Exception as error:
        payload = error.args[0] if getattr(error, 'args', None) else None
        code = payload.get('C', '') if isinstance(payload, dict) else ''
        result.add('plan_compare.consistency', 'error', reason='execution_error',
                   sqlstate=code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else '')
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                result.add('plan_compare.cleanup', 'error', reason='connection_cleanup_error')
