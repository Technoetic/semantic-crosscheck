"""Bounded raw-row SUM/COUNT diagnostic with an independently authored predicate.

Only fixed projection/catalog SQL is issued. The oracle does not consume the
configured measure's filters or import a model/query/SQL generator. A single
Cube measure query is authored separately in the spec. No input values, SQL,
paths, credentials, or exception messages enter its receipt.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

from . import config, cube_run, db, ident
from .cube_client import CubeRowLimitError
from .exact_numeric import NumericLimit, exact_sum
from .result import EXIT_CONFIG, Result, StageError, discard_own_verdict, inputs_fingerprint
from .result_contract import decode_result

STAGE = 'measure_oracle'
SPEC_LIMIT = 65536
MAX_ROWS = 50000
MAX_GROUPS = 10000
MAX_FILTERS = 32
MAX_VALUES = 256
TEXT_LIMIT = 4096
STATEMENT_CAP_MS = 30000
MODEL_LIMIT = 4 * 1024 * 1024
SNAPSHOT_NOTE = ('Raw source and Cube do not share an MVCC snapshot. A stable-data window or an '
                 'owner-maintained, non-reused revision advanced atomically with every relevant change '
                 'is required; matching answers alone do not establish that contract.')
SCOPE_NOTE = ('This optional diagnostic compares one authored query over complete bounded visible inputs. '
              'It does not prove arbitrary measures, business truth, hidden rows, general joins or time '
              'semantics, or unbounded equivalence and adds no report authority.')
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z', re.ASCII)
_TYPES = {'int2': 'integer', 'int4': 'integer', 'int8': 'integer',
          'numeric': 'numeric', 'text': 'text', 'varchar': 'text', 'bool': 'boolean'}
_KINDS = frozenset(_TYPES.values())
_TEMPORAL = frozenset({'date', 'timestamp', 'timestamptz'})
_CUBE_KIND = {'integer': 'number', 'numeric': 'number', 'text': 'string', 'boolean': 'boolean'}


class NotRun(ValueError):
    """An unsupported input or exhausted bound; never a partial comparison."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('measure-oracle: ' + reason)


class _Invalid(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('measure-oracle: ' + reason)


def _pairs(pairs):
    answer = {}
    for key, value in pairs:
        if key in answer:
            raise ValueError('duplicate key')
        answer[key] = value
    return answer


def _reject_number(_value):
    raise ValueError('unsupported JSON number')


def _decode(raw, *, spec=False):
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                      parse_constant=_reject_number,
                      parse_float=_reject_number if spec else Decimal)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        raise ValueError('fields')


def _text(value):
    if type(value) is not str or len(value.encode('utf-8')) > TEXT_LIMIT:
        raise ValueError('text')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('number type')
    if isinstance(value, str):
        if len(value) > TEXT_LIMIT or not _NUMBER.fullmatch(value):
            raise ValueError('numeric representation')
        value = Decimal(value)
    elif type(value) is int:
        value = Decimal(value)
    if not value.is_finite():
        raise ValueError('nonfinite')
    # Bound coefficients/exponents before any comparison or fixed-point expansion.
    exact_sum([value])
    return value


def _literal(kind, value):
    if kind == 'integer':
        if type(value) is not int:
            raise ValueError('integer literal')
        _number(value)
    elif kind == 'numeric':
        if type(value) is not str:
            raise ValueError('numeric literal must be text')
        _number(value)
    elif kind == 'text':
        _text(value)
    elif kind == 'boolean':
        if type(value) is not bool:
            raise ValueError('boolean literal')
    else:
        raise ValueError('literal type')


def _predicate(value):
    _keys(value, {'column', 'operator'}, {'type', 'value', 'values'})
    ident.check(value['column'])
    op = value['operator']
    if op in ('is_null', 'is_not_null'):
        _keys(value, {'column', 'operator'})
        if len(value) != 2:
            raise ValueError('null predicate fields')
    elif op in ('eq', 'ne', 'in'):
        field = 'values' if op == 'in' else 'value'
        _keys(value, {'column', 'operator', 'type', field})
        if len(value) != 4:
            raise ValueError('predicate fields')
        values = value[field] if op == 'in' else [value[field]]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
            raise ValueError('predicate values')
        for item in values:
            _literal(value['type'], item)
    else:
        raise ValueError('predicate operator')


def _member(value):
    if not isinstance(value, str) or len(value.split('.')) != 2:
        raise ValueError('member')
    for name in value.split('.'):
        ident.check(name)
    return value.split('.')[0]


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('calendar date')
    return date.fromisoformat(value)


def _query(query, *, grouped=False, dated=False):
    required = {'measures'}
    if grouped:
        required.add('dimensions')
    if dated:
        required.add('timeDimensions')
    _keys(query, required, {'filters'})
    measures = query['measures']
    if not isinstance(measures, list) or len(measures) != 1:
        raise ValueError('single measure required')
    cube = _member(measures[0])
    if grouped:
        dims = query['dimensions']
        if (not isinstance(dims, list) or len(dims) != 1 or _member(dims[0]) != cube
                or dims[0] == measures[0]):
            raise ValueError('single group dimension')
    if dated:
        timed = query['timeDimensions']
        if not isinstance(timed, list) or len(timed) != 1:
            raise ValueError('single time dimension')
        td = timed[0]
        _keys(td, {'dimension', 'dateRange'})
        bounds = td['dateRange']
        if (_member(td['dimension']) != cube or td['dimension'] in [*measures, *query.get('dimensions', [])]
                or not isinstance(bounds, list) or len(bounds) != 2 or _day(bounds[0]) > _day(bounds[1])):
            raise ValueError('Cube calendar range')
    filters = query.get('filters', [])
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise ValueError('query filters')
    for flt in filters:
        _keys(flt, {'member', 'operator'}, {'values'})
        if _member(flt['member']) != cube:
            raise ValueError('cross-cube query')
        if flt['operator'] in ('set', 'notSet'):
            if set(flt) != {'member', 'operator'}:
                raise ValueError('query null filter')
        elif flt['operator'] in ('equals', 'notEquals'):
            values = flt.get('values')
            if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
                raise ValueError('query values')
            for value in values:
                _text(value)
        else:
            raise ValueError('query operator')


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
    path = Path(path).absolute()
    final = None
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
        if item == path:
            final = meta
    return final


def _read(path, limit):
    path = Path(path)
    _inspect(path)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError('file limit')
    return raw


def _aliases(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False


def prepare_run(args, out, *, protected_paths=()):
    """Read-only input/output guards before CLI cleanup or result publication.

    These observations do not lock the filesystem against hostile ancestor
    races. A caller may provide all raw option occurrences in
    `_measure_oracle_inputs`, so repeated flags remain protected.
    """
    try:
        if not getattr(args, 'spec', None):
            raise ValueError('spec required')
        directory = Path(out) / STAGE
        _inspect(out, directory=True, missing=True)
        _inspect(directory, directory=True, missing=True)
        targets = [Path(out) / '.gitignore', directory / 'result.json', directory / 'result.json.part']
        for target in targets:
            _inspect(target, missing=True)
        inputs = [args.spec, getattr(args, 'config', None),
                  *getattr(args, '_measure_oracle_inputs', ()), *protected_paths]
        for item in inputs:
            if item is None:
                continue
            path = Path(item)
            _inspect(path, missing=True)
            if any(_aliases(path, target) for target in (Path(out), directory, *targets)):
                raise ValueError('output alias')
    except Exception:
        raise StageError('measure-oracle: invalid_or_protected_inputs', EXIT_CONFIG) from None


def load_spec(path):
    """Read a bounded, closed contract; decimals are explicit strings."""
    try:
        raw = _read(path, SPEC_LIMIT)
        spec = _decode(raw, spec=True)
        common = {'schema_version', 'entity', 'column', 'aggregation', 'where', 'cube_query'}
        version = spec['schema_version']
        if type(version) is not int or version not in (1, 2):
            raise ValueError('version')
        if version == 2:
            from . import measure_oracle_join
            _keys(spec, common | {'related_exists'}, {'max_rows'})
            measure_oracle_join.validate(spec)
        else:
            _keys(spec, common, {'max_rows', 'group_by', 'time_window'})
        if 'group_by' in spec:
            ident.check(spec['group_by'])
        if 'time_window' in spec:
            window = spec['time_window']
            _keys(window, {'column', 'start', 'end'})
            ident.check(window['column'])
            if _day(window['start']) >= _day(window['end']):
                raise ValueError('empty time window')
        ident.check(spec['entity'])
        ident.check(spec['column'])
        if spec['aggregation'] not in ('sum', 'count'):
            raise ValueError('aggregation')
        max_rows = spec.get('max_rows', 10000)
        if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
            raise ValueError('row bound')
        if not isinstance(spec['where'], list) or len(spec['where']) > MAX_FILTERS:
            raise ValueError('predicates')
        for predicate in spec['where']:
            _predicate(predicate)
        _query(spec['cube_query'], grouped='group_by' in spec, dated='time_window' in spec)
        return dict(spec, max_rows=max_rows, spec_sha256=hashlib.sha256(raw).hexdigest())
    except Exception:
        raise StageError('measure-oracle: invalid_spec', EXIT_CONFIG) from None


def _raw(value, kind):
    if value is None:
        return None
    if kind == 'integer' and type(value) is int:
        return _number(value)
    if kind == 'numeric' and (type(value) is int or isinstance(value, Decimal)):
        return _number(value)
    if kind == 'text' and type(value) is str:
        return value
    if kind == 'boolean' and type(value) is bool:
        return value
    raise ValueError('source type')


def _utc_day(value, kind):
    if value is None:
        return None
    if kind == 'date' and type(value) is date:
        return value
    if type(value) is datetime:
        if kind == 'timestamp' and value.tzinfo is None:
            return value.date()
        if kind == 'timestamptz' and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).date()
    raise ValueError('temporal source type')


def _matches(value, predicate, literals):
    op = predicate['operator']
    if op == 'is_null':
        return value is None
    if op == 'is_not_null':
        return value is not None
    # SQL's UNKNOWN does not qualify a row, including NULL <> a non-NULL value.
    if value is None:
        return False
    return value not in literals if op == 'ne' else value in literals


def evaluate(spec, types, rows, *, on_qualified=None):
    """Compute an exact ephemeral answer from explicit raw values, never config filters.

    `types` contains catalog-derived scalar and temporal kinds. Scalar values
    and group maps stay in memory; run() serializes counts and fixed reasons.
    The optional observer receives each qualifying raw row once in memory.
    """
    if not isinstance(rows, list):
        raise ValueError('source rows')
    if len(rows) > spec['max_rows']:
        raise NotRun('row_limit')
    group = spec.get('group_by')
    window = spec.get('time_window')
    columns = {spec['column']} | {p['column'] for p in spec['where']}
    if group:
        columns.add(group)
    if any(types.get(name) not in _KINDS for name in columns):
        raise NotRun('unsupported_type')
    if spec['aggregation'] == 'sum' and types[spec['column']] not in ('integer', 'numeric'):
        raise NotRun('unsupported_aggregation_type')
    if window:
        if types.get(window['column']) not in _TEMPORAL:
            raise NotRun('unsupported_time_type')
        start, end = _day(window['start']), _day(window['end'])
    for p in spec['where']:
        if 'type' in p and not (p['type'] == types[p['column']] or
                                p['type'] == 'numeric' and types[p['column']] == 'integer'):
            raise NotRun('predicate_type_mismatch')
    # The global empty aggregate has one result. An empty group set has none.
    buckets = {} if group else {None: []}
    qualifying = present = 0
    try:
        predicates = []
        for predicate in spec['where']:
            literals = predicate.get('values', [predicate['value']] if 'value' in predicate else [])
            if predicate.get('type') in ('integer', 'numeric'):
                literals = [_number(value) for value in literals]
            predicates.append((predicate, frozenset(literals)))
        for row in rows:
            if (not isinstance(row, dict) or not columns <= row.keys()
                    or (window and window['column'] not in row)):
                raise ValueError('source projection')
            converted = {name: _raw(row[name], types[name]) for name in columns}
            if window:
                day = _utc_day(row[window['column']], types[window['column']])
                if day is None or not start <= day < end:
                    continue
            if not all(_matches(converted[p['column']], p, literals) for p, literals in predicates):
                continue
            if on_qualified is not None:
                on_qualified(row)
            key = converted[group] if group else None
            value = converted[spec['column']]
            buckets.setdefault(key, []).append(value)
            if len(buckets) > MAX_GROUPS:
                raise NotRun('group_limit')
            qualifying += 1
            present += int(value is not None)
        answers = {key: exact_sum(values) if spec['aggregation'] == 'sum' else sum(v is not None for v in values)
                   for key, values in buckets.items()}
    except NotRun:
        raise
    except NumericLimit:
        raise NotRun('numeric_limit') from None
    except (ValueError, InvalidOperation, OverflowError):
        raise NotRun('unsupported_value') from None
    totals = {'source_rows': len(rows), 'qualifying_rows': qualifying, 'non_null_values': present}
    if group:
        return dict(totals, groups=answers, group_kind=types[group])
    return dict(totals, value=answers[None])


def compare_value(expected, cube_rows, member):
    """Strictly require one complete global scalar; floats and nonfinite fail closed."""
    if (not isinstance(cube_rows, list) or len(cube_rows) != 1
            or not isinstance(cube_rows[0], dict) or set(cube_rows[0]) != {member}):
        raise ValueError('invalid Cube scalar')
    value = cube_rows[0][member]
    try:
        actual = None if value is None else _number(value)
    except (ValueError, InvalidOperation, OverflowError):
        raise ValueError('invalid Cube number') from None
    return actual == expected


def _cube_key(value, kind):
    if value is None:
        return None
    if kind in ('integer', 'numeric'):
        number = _number(value)
        if kind == 'integer' and number != number.to_integral_value():
            raise ValueError('fractional integer key')
        return number
    if kind == 'text' and type(value) is str:
        return value
    if kind == 'boolean':
        if type(value) is bool:
            return value
        if isinstance(value, str) and value in ('true', 'false', 't', 'f'):
            return value in ('true', 't')
    raise ValueError('invalid Cube group key')


def _compare_groups(answer, rows, query):
    if not isinstance(rows, list):
        raise ValueError('invalid Cube groups')
    if len(rows) > MAX_GROUPS:
        raise NotRun('group_limit')
    member, dimension = query['measures'][0], query['dimensions'][0]
    actual = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {member, dimension}:
            raise ValueError('invalid Cube group projection')
        key = _cube_key(row[dimension], answer['group_kind'])
        if key in actual:
            raise _Invalid('duplicate_cube_group')
        actual[key] = None if row[member] is None else _number(row[member])
    expected = answer['groups']
    return {
        'source_groups': len(expected),
        'missing_groups': len(expected.keys() - actual.keys()),
        'extra_groups': len(actual.keys() - expected.keys()),
        'mismatched_groups': sum(expected[k] != actual[k] for k in expected.keys() & actual.keys()),
    }


@dataclass
class _Captured:
    path: Path
    raw: bytes
    identity: tuple
    limit: int

    @classmethod
    def read(cls, path, limit):
        path = Path(path)
        raw = _read(path, limit)
        st = path.stat()
        return cls(path, raw, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns), limit)

    def unchanged(self):
        try:
            again = self.read(self.path, self.limit)
        except Exception:
            raise _Invalid('inputs_changed') from None
        if again.raw != self.raw or again.identity != self.identity:
            raise _Invalid('inputs_changed')


def _model_files(out):
    directory = Path(out) / 'cube' / 'model'
    if not directory.is_dir() or directory.is_symlink() or bool(getattr(directory, 'is_junction', lambda: False)()):
        raise _Invalid('cube_required')
    files = sorted(directory.iterdir())
    if not 1 <= len(files) <= 256 or any(path.suffix != '.yml' for path in files):
        raise _Invalid('invalid_cube')
    return [Path(out) / 'cube' / 'result.json', Path(out) / 'cube' / 'model.json', *files]


def _prerequisite(cfg, spec, out, captured):
    try:
        paths = _model_files(out)
        snapshots = [_Captured.read(path, MODEL_LIMIT) for path in paths]
        recorded = decode_result(snapshots[0].raw.decode('utf-8'), expected_stage='cube')
        states = {item['id']: item['status'] for item in recorded['checks']}
        if (recorded.get('status') != 'pass' or any(item != 'pass' for item in states.values())
                or states.get('cube:model') != 'pass' or states.get('cube:meta') != 'pass'):
            raise _Invalid('cube_required')
        if recorded.get('inputs') != inputs_fingerprint(cfg):
            raise _Invalid('stale_cube')
        model = _decode(snapshots[1].raw)
        if not isinstance(model, dict) or not isinstance(model.get('cubes'), list):
            raise _Invalid('invalid_cube')
        cube_name, member = spec['cube_query']['measures'][0].split('.')
        definitions = [cube for cube in model['cubes'] if isinstance(cube, dict) and cube.get('name') == cube_name]
        if len(definitions) != 1:
            raise _Invalid('cube_member_missing')
        cube = definitions[0]
        if cube.get('public') is False or cube.get('shown') is False:
            raise _Invalid('cube_member_missing')
        measures = [m for m in cube.get('measures', []) if isinstance(m, dict) and m.get('name') == member
                    and m.get('public') is not False and m.get('shown') is not False]
        if len(measures) != 1:
            raise _Invalid('cube_member_missing')
        public = {}
        for section in ('dimensions', 'measures'):
            public[section] = {cube_name + '.' + m['name'] for m in cube.get(section, [])
                               if isinstance(m, dict) and isinstance(m.get('name'), str)
                               and m.get('public') is not False and m.get('shown') is not False
                               and not (section == 'dimensions' and m.get('primary_key') and m.get('public') is not True)}
        query = spec['cube_query']
        public_members = public['measures'] | public['dimensions']
        if any(f['member'] not in public_members for f in query.get('filters', [])):
            raise _Invalid('cube_member_missing')
        group_type = None
        requested = [(name, 'group') for name in query.get('dimensions', [])]
        requested += [(td['dimension'], 'time') for td in query.get('timeDimensions', [])]
        for name, role in requested:
            dims = [d for d in cube.get('dimensions', []) if isinstance(d, dict)
                    and d.get('name') == name.split('.')[1]]
            if name not in public['dimensions'] or len(dims) != 1:
                raise _Invalid('cube_member_missing')
            if role == 'group':
                group_type = dims[0].get('type')
            elif dims[0].get('type') != 'time':
                raise NotRun('unsupported_time_type')
        captured.extend(snapshots)
        return paths, group_type
    except (_Invalid, NotRun):
        raise
    except Exception:
        raise _Invalid('cube_required') from None


def _read_table(conn, table, columns, max_rows, *, time_column=None):
    schema, relation = ident.split(table)
    projection = ','.join(ident.quote(name) for name in sorted(columns))
    catalog = db.rows(conn, '''SELECT a.attname AS name, t.typname AS type, tn.nspname AS type_schema,
                co.collisdeterministic AS collation_deterministic
                FROM pg_catalog.pg_attribute a
                JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
                JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
                LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
                WHERE n.nspname = :schema AND c.relname = :table
                  AND a.attnum > 0 AND NOT a.attisdropped''', schema=schema, table=relation)
    types = {}
    for row in catalog:
        if row['name'] not in columns:
            continue
        kind = _TYPES.get(row['type'])
        if row['name'] == time_column:
            if row['type'] not in _TEMPORAL:
                raise NotRun('unsupported_time_type')
            kind = row['type']
        if row['type_schema'] != 'pg_catalog' or kind is None:
            raise NotRun('unsupported_type')
        if kind == 'text' and row.get('collation_deterministic') is not True:
            raise NotRun('unsafe_collation')
        types[row['name']] = kind
    if types.keys() != set(columns):
        raise _Invalid('column_missing')
    rows = db.rows(conn, f'SELECT {projection} FROM {ident.qualified(table)} LIMIT :row_limit', row_limit=max_rows + 1)
    if len(rows) > max_rows:
        raise NotRun('row_limit')
    return types, rows


def _source(cfg, spec):
    entity = cfg.entities.get(spec['entity'])
    if entity is None:
        raise _Invalid('entity_missing')
    conn = db.connect(cfg.source.dsn_env)
    try:
        with db.read_only(conn, min(cfg.source.statement_timeout_ms, STATEMENT_CAP_MS), isolation='repeatable read'):
            if 'related_exists' in spec:
                from . import measure_oracle_join
                return measure_oracle_join.source(cfg, spec, conn, _read_table)
            columns = {spec['column']} | {p['column'] for p in spec['where']}
            if 'group_by' in spec:
                columns.add(spec['group_by'])
            time_column = spec.get('time_window', {}).get('column')
            if time_column:
                columns.add(time_column)
            types, rows = _read_table(conn, entity.table, columns, spec['max_rows'], time_column=time_column)
            return evaluate(spec, types, rows)
    finally:
        conn.close()


def run(cfg, args, out: Path) -> Result:
    """Run the optional diagnostic. The normal CLI writes this Result atomically."""
    source_path = getattr(args, 'spec', None)
    # CLI fingerprints gold/revision inputs even though they do not enter this
    # calculation. Direct callers receive the same protection before cleanup.
    protected = [cfg.path, *[gold.sql_path(cfg) for gold in cfg.gold]]
    if cfg.source.data_revision_sql_file:
        protected.append(cfg.path.parent / cfg.source.data_revision_sql_file)
    prepare_run(args, out, protected_paths=protected)
    # Every declared path was checked above; passing them explicitly avoids a
    # filename-looking filter literal being mistaken for an undeclared input.
    discard_own_verdict(out, STAGE, protected_paths=(source_path, *protected))
    res = Result(STAGE, not_proven=[SNAPSHOT_NOTE, SCOPE_NOTE])
    res.next_safe_action = ('Review the independent raw predicate and Cube query locally, establish a stable-data '
                            'or owner revision contract, and rerun this bounded diagnostic.')
    captured = []
    try:
        spec = load_spec(source_path)
        selected = _Captured.read(source_path, SPEC_LIMIT)
        if hashlib.sha256(selected.raw).hexdigest() != spec['spec_sha256']:
            raise _Invalid('inputs_changed')
        cfg_source = _Captured.read(cfg.path, MODEL_LIMIT)
        current_cfg = config.loads(cfg_source.raw, cfg.path)
        if current_cfg != cfg:
            raise _Invalid('configuration_changed')
        captured.extend((selected, cfg_source))
        captured.extend(_Captured.read(path, MODEL_LIMIT) for path in protected[1:])
        model_paths, group_type = _prerequisite(current_cfg, spec, out, captured)
        res.inputs['measure_oracle'] = {'spec_sha256': spec['spec_sha256'],
                                        'config_sha256': hashlib.sha256(cfg_source.raw).hexdigest()}
        answer = _source(current_cfg, spec)
        query = spec['cube_query']
        grouped = 'group_by' in spec
        if grouped and _CUBE_KIND[answer['group_kind']] != group_type:
            raise NotRun('group_type_mismatch')
        detail = {name: answer[name] for name in ('source_rows', 'qualifying_rows', 'non_null_values')}
        if 'related_exists' in spec:
            for name in ('related_rows', 'matched_facts', 'multi_match_facts', 'qualifying_multi_match_facts',
                         'null_foreign_keys', 'orphan_related_rows'):
                detail[name] = answer[name]
        # End the raw database snapshot before starting/waiting for Cube.
        with cube_run.serve(current_cfg, Path(out) / 'cube') as client:
            try:
                cube_rows = client.load(dict(query, limit=MAX_GROUPS + 1) if grouped else query)
                if grouped:
                    groups = _compare_groups(answer, cube_rows, query)
                    equal = not any(groups[k] for k in ('missing_groups', 'extra_groups', 'mismatched_groups'))
                    detail.update(groups)
                else:
                    equal = compare_value(answer['value'], cube_rows, query['measures'][0])
            except CubeRowLimitError:
                if grouped:
                    raise NotRun('group_limit') from None
                raise _Invalid('cube_failed') from None
            except (NotRun, _Invalid):
                raise
            except Exception:
                raise _Invalid('cube_failed') from None
        if _model_files(out) != model_paths:
            raise _Invalid('inputs_changed')
        for source in captured:
            source.unchanged()
        if current_cfg != cfg:
            raise _Invalid('configuration_changed')
        def validate_for_publication():
            try:
                if _model_files(out) != model_paths or current_cfg != cfg:
                    return False
                for source in captured:
                    source.unchanged()
                return True
            except Exception:
                return False
        # Runtime-only: Result.to_dict/asdict serializes dataclass fields, not
        # this callback. CLI rechecks before and after atomic publication.
        res._diagnostic_validate = validate_for_publication
        detail.update(max_rows=spec['max_rows'], cube_rows=len(cube_rows),
                      reason='match' if equal else 'value_mismatch')
        if not answer['source_rows']:
            res.not_proven.append('The visible source was empty; this comparison exercised no source values.')
        elif not answer['qualifying_rows']:
            res.not_proven.append('No raw rows qualified; this comparison exercised no qualifying source values.')
        elif not answer['non_null_values']:
            res.not_proven.append('All qualifying measure values were NULL; no non-NULL measure value was exercised.')
        if 'related_exists' in spec and not answer['qualifying_multi_match_facts']:
            res.not_proven.append('No qualifying fact matched multiple related rows.')
        res.add('measure_oracle', 'pass' if equal else 'fail', **detail)
    except NotRun as exc:
        res.add('measure_oracle', 'not_run', reason=exc.reason)
    except _Invalid as exc:
        res.add('measure_oracle', 'error', reason=exc.reason)
    except StageError:
        res.add('measure_oracle', 'error', reason='invalid_spec' if not captured else 'execution_failed')
    except Exception:
        res.add('measure_oracle', 'error', reason='execution_failed')
    return res
