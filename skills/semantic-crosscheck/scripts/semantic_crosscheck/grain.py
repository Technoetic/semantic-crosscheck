"""Bounded, default-off authored grain contracts for undated node measures.

The contract is supplied independently of crosscheck.toml. Agreement with the
derived operation is only one obligation: actual visible source/node rows must
also satisfy the key, join and null preconditions in one read-only snapshot.
No manifest SQL is accepted, and no source values or identifiers reach results.
"""
import hashlib
from pathlib import Path

from . import constraints, db, ident
from .result import EXIT_CONFIG, Result, StageError

SPEC_LIMIT = 65536
MAX_CONTRACTS = 32
MAX_ROWS = 1000000
STATEMENT_CAP_MS = 30000
_CONTRACT_FIELDS = {'measure', 'source', 'group_by', 'aggregate', 'filters',
                    'output', 'node', 'join', 'null_policy'}
_AGGS = {'sum', 'count', 'min', 'max'}
_KEY_VIOLATIONS = ('source_null_key_rows', 'source_duplicate_groups',
                   'node_null_key_rows', 'node_duplicate_groups',
                   'orphan_groups', 'fanout_groups')


def _error():
    return StageError('grain: requires a bounded strict version-1 grain contract for supported undated preaggregate-to-node measures', EXIT_CONFIG)


def _read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(SPEC_LIMIT + 1)
    if not raw or len(raw) > SPEC_LIMIT:
        raise ValueError('input bound')
    return raw


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('fields')


def _key(value):
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError('key')
    for item in value:
        ident.check(item)
    if len(value) != len(set(value)):
        raise ValueError('duplicate key')


def _entity(value):
    _object(value, {'entity', 'table', 'key'})
    ident.check(value['entity'])
    if not isinstance(value['table'], str):
        raise ValueError('table type')
    ident.qualified(value['table'])
    _key(value['key'])


def _filters(value):
    _object(value, {'exact', 'normalized'})
    for kind, filters in value.items():
        if not isinstance(filters, dict) or len(filters) > 32:
            raise ValueError('filters')
        for column, values in filters.items():
            ident.check(column)
            if not isinstance(values, list) or not values or len(values) > 256:
                raise ValueError('filter values')
            for item in values:
                if type(item) not in (str, int):
                    raise ValueError('filter scalar')
                if kind == 'normalized' and (not isinstance(item, str) or item != item.strip().upper()):
                    raise ValueError('normalized filter')
    if set(value['exact']) & set(value['normalized']):
        raise ValueError('overlapping filters')


def load_spec(path):
    """Parse only authored identifiers, supported operations and scalar filters."""
    try:
        raw = _read(path)
        spec = constraints.decode_json(raw.decode('utf-8-sig'))
        _object(spec, {'schema_version', 'contracts'})
        if type(spec['schema_version']) is not int or spec['schema_version'] != 1:
            raise ValueError('version')
        contracts = spec['contracts']
        if not isinstance(contracts, list) or not 1 <= len(contracts) <= MAX_CONTRACTS:
            raise ValueError('scope')
        seen = set()
        for contract in contracts:
            _object(contract, _CONTRACT_FIELDS)
            ident.check(contract['measure'])
            if contract['measure'] in seen:
                raise ValueError('duplicate measure')
            seen.add(contract['measure'])
            _entity(contract['source'])
            _entity(contract['node'])
            _key(contract['group_by'])
            _object(contract['aggregate'], {'function', 'column'})
            if contract['aggregate']['function'] not in _AGGS:
                raise ValueError('aggregate')
            ident.check(contract['aggregate']['column'])
            _filters(contract['filters'])
            output = contract['output']
            _object(output, {'cube', 'key', 'value', 'reaggregate'})
            ident.check(output['cube'])
            ident.check(output['value'])
            _key(output['key'])
            if output['reaggregate'] not in _AGGS:
                raise ValueError('reaggregate')
            join = contract['join']
            _object(join, {'relationship', 'node_key', 'output_key'})
            if join['relationship'] not in ('one_to_one', 'many_to_one', 'one_to_many'):
                raise ValueError('relationship')
            _key(join['node_key'])
            _key(join['output_key'])
            nulls = contract['null_policy']
            _object(nulls, {'operation', 'allow_excluded_rows'})
            if nulls['operation'] not in ('exclude', 'retain') or type(nulls['allow_excluded_rows']) is not bool:
                raise ValueError('null policy')
        return dict(spec, spec_sha256=hashlib.sha256(raw).hexdigest())
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        raise _error() from None


def _filter_identity(filters):
    # IN lists are unordered sets; preserve the scalar type (1 differs from "1").
    return {kind: {column: sorted(set((type(v).__name__, str(v)) for v in values))
                   for column, values in columns.items()} for kind, columns in filters.items()}


def derive(cfg, contract):
    """Describe actual configured operations, without using the authored claims."""
    try:
        measure = cfg.measures[contract['measure']]
        if measure.date_column or measure.closure_join or not measure.node:
            raise ValueError('unsupported fragment')
        source = cfg.entities[measure.entity]
        node = cfg.entities[measure.node]
        if len(node.key) != 1 or measure.agg not in _AGGS:
            raise ValueError('unsupported key')
        actual = {
            'source': {'entity': source.name, 'table': source.table, 'key': source.key},
            'group_by': [measure.by],
            'aggregate': {'function': measure.agg, 'column': measure.column},
            'filters': {'exact': measure.filter, 'normalized': measure.filter_normalized},
            'output': {'cube': 'm_' + measure.name, 'key': ['key'], 'value': 'value',
                       'reaggregate': 'sum' if measure.agg == 'count' else measure.agg},
            'node': {'entity': node.name, 'table': node.table, 'key': node.key},
            'join': {'relationship': 'one_to_one', 'node_key': node.key, 'output_key': ['key']},
            'null_policy': {'operation': 'exclude'}}
        # Config instances normally come from config.load; keep this SQL boundary
        # defensive without reflecting an unsafe identifier through IdentError.
        _entity(actual['source'])
        _entity(actual['node'])
        _key(actual['group_by'])
        ident.check(measure.name)
        ident.check(measure.column)
        _filters(actual['filters'])
        return actual
    except (AttributeError, KeyError, TypeError, ValueError):
        raise _error() from None


def mismatches(actual, contract):
    changed = []
    for name, value in actual.items():
        expected = contract[name]
        if name == 'filters':
            equal = _filter_identity(value) == _filter_identity(expected)
        elif name == 'null_policy':
            equal = value['operation'] == expected['operation']
        else:
            equal = value == expected
        if not equal:
            changed.append(name)
    return changed


def _alias(first, second):
    if first.resolve() == second.resolve():
        return True
    try:
        return first.samefile(second)
    except (OSError, ValueError):
        return False


def prepare_run(args, out):
    """CLI preflight before any result cleanup or error-result write.

    It reads no files and mutates nothing. The CLI additionally protects gold and
    revision inputs after loading cfg; run() repeats that complete protection.
    """
    try:
        inputs = [Path(args.spec)]
        if getattr(args, 'config', None):
            inputs.append(Path(args.config))
        stage = Path(out) / 'grain'
        targets = [stage / name for name in ('result.json', 'result.json.part')]
        if any(_alias(item, target) for item in inputs for target in targets):
            raise ValueError('alias')
        if stage.is_symlink() or any(target.is_symlink() for target in targets):
            raise ValueError('artifact symlink')
    except (OSError, TypeError, ValueError, AttributeError):
        raise _error() from None


def _prepare(cfg, args, out):
    """Validate aliases before deleting stale artifacts, even on invalid input."""
    try:
        prepare_run(args, out)
        source = Path(args.spec)
        inputs = [source, Path(cfg.path)]
        # The stage does not consume gold/revision SQL, but CLI fingerprinting
        # does. Protect those files from result cleanup as well.
        inputs += [g.sql_path(cfg) for g in cfg.gold]
        if cfg.source.data_revision_sql_file:
            inputs.append(cfg.base_dir() / cfg.source.data_revision_sql_file)
        targets = [Path(out) / 'grain' / name for name in ('result.json', 'result.json.part')]
        if any(_alias(item, target) for item in inputs for target in targets):
            raise ValueError('alias')
        if any(target.is_symlink() for target in targets) or (Path(out) / 'grain').is_symlink():
            raise ValueError('artifact symlink')
        for target in targets:
            target.unlink(missing_ok=True)
        rows = args.max_rows
        if type(rows) is not int or not 1 <= rows <= MAX_ROWS:
            raise ValueError('rows')
        timeout = cfg.source.statement_timeout_ms
        if type(timeout) is not int or timeout < 1000:
            raise ValueError('timeout')
        config_raw = Path(cfg.path).read_bytes()
        config_sha = hashlib.sha256(config_raw).hexdigest()
        expected = getattr(args, '_diagnostic_configuration_sha', None)
        if expected is not None and config_sha != expected:
            raise ValueError('configuration changed before dispatch')
        return source, rows, min(timeout, STATEMENT_CAP_MS), config_sha
    except (OSError, TypeError, ValueError, AttributeError):
        raise _error() from None


def _where(actual):
    clauses, params = [], {}
    for kind, filters in actual['filters'].items():
        for column, values in filters.items():
            names = []
            for value in values:
                name = 'f' + str(len(params))
                params[name] = value
                names.append(':' + name)
            column_sql = 't.' + ident.quote(column)
            if kind == 'normalized':
                column_sql = 'upper(btrim(' + column_sql + '))'
            clauses.append(column_sql + ' IN (' + ', '.join(names) + ')')
    return ' AND '.join(clauses) or 'TRUE', params


def evaluate(conn, actual, max_rows):
    """Return bounded counts only; caller supplies a read-only repeatable snapshot.

    At most five statements: two physical-relation provenance checks, two bounded
    row counts, then one materialized aggregation/key/join count query. Relation
    counts inspect max_rows+1; an overflow never executes partial evidence.
    """
    for side in ('source', 'node'):
        schema, table = ident.split(actual[side]['table'])
        kind = db.scalar(conn, '''SELECT c.relkind FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = :schema AND c.relname = :table''', schema=schema, table=table)
        if kind not in ('r', 'p'):
            return {'reason': 'unsupported_relation', 'complete': False}
    sizes = {}
    for side in ('source', 'node'):
        table = ident.qualified(actual[side]['table'])
        sizes[side + '_rows'] = int(db.scalar(conn,
            f'SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT :row_limit) AS bounded',
            row_limit=max_rows + 1))
        if sizes[side + '_rows'] > max_rows:
            return dict(sizes, reason='row_budget', complete=False)
    source, node = ident.qualified(actual['source']['table']), ident.qualified(actual['node']['table'])
    by = ident.quote(actual['group_by'][0])
    value = ident.quote(actual['aggregate']['column'])
    source_key = ', '.join(ident.quote(k) for k in actual['source']['key'])
    null_source = ' OR '.join(ident.quote(k) + ' IS NULL' for k in actual['source']['key'])
    node_key = ident.quote(actual['node']['key'][0])
    where, params = _where(actual)
    aggregate = actual['aggregate']['function'].upper()
    query = f'''WITH filtered AS MATERIALIZED (
      SELECT t.{by} AS grain_key, t.{value} AS measure_value FROM {source} t WHERE {where}
    ), grouped AS MATERIALIZED (
      SELECT grain_key, {aggregate}(measure_value) AS aggregate_value
      FROM filtered WHERE grain_key IS NOT NULL GROUP BY grain_key
    ), nodes AS MATERIALIZED (
      SELECT {node_key} AS node_key, count(*) AS multiplicity FROM {node} GROUP BY {node_key}
    ), source_keys AS MATERIALIZED (
      SELECT count(*) AS multiplicity FROM {source} GROUP BY {source_key}
    ), matches AS MATERIALIZED (
      SELECT g.grain_key, n.multiplicity FROM grouped g
      LEFT JOIN nodes n ON g.grain_key = n.node_key
    ) SELECT
      (SELECT count(*) FROM {source} WHERE {null_source}) AS source_null_key_rows,
      (SELECT count(*) FROM source_keys WHERE multiplicity > 1) AS source_duplicate_groups,
      (SELECT count(*) FROM {node} WHERE {node_key} IS NULL) AS node_null_key_rows,
      (SELECT count(*) FROM nodes WHERE multiplicity > 1) AS node_duplicate_groups,
      (SELECT count(*) FROM filtered WHERE grain_key IS NOT NULL) AS selected_rows,
      (SELECT count(*) FROM filtered WHERE grain_key IS NULL) AS excluded_null_rows,
      (SELECT count(*) FROM filtered WHERE grain_key IS NOT NULL AND measure_value IS NULL) AS null_value_rows,
      (SELECT count(*) FROM grouped) AS output_groups,
      (SELECT count(*) FROM grouped WHERE aggregate_value IS NULL) AS null_output_groups,
      (SELECT count(*) FROM matches WHERE multiplicity IS NULL) AS orphan_groups,
      (SELECT count(*) FROM matches WHERE multiplicity > 1) AS fanout_groups,
      (SELECT coalesce(sum(multiplicity), 0) FROM matches) AS joined_rows,
      (SELECT count(*) FROM nodes n WHERE n.node_key IS NOT NULL AND NOT EXISTS
        (SELECT 1 FROM grouped g WHERE g.grain_key = n.node_key)) AS nodes_without_output'''
    rows = db.rows(conn, query, **params)
    if len(rows) != 1:
        raise ValueError('evidence shape')
    counts = {key: int(value) for key, value in rows[0].items()}
    return dict(sizes, **counts, complete=True)


def run(cfg, args, out: Path) -> Result:
    """Standalone opt-in stage; the CLI owns writing the returned Result."""
    source, max_rows, timeout, config_sha = _prepare(cfg, args, out)
    spec = load_spec(source)
    # Validate the entire requested scope before connecting or accumulating a
    # partial pass. Unsupported and unknown operations are configuration errors.
    operations = [derive(cfg, item) for item in spec['contracts']]
    result = Result('grain')
    result.inputs = {'grain_spec_sha256': spec['spec_sha256'], 'grain_config_sha256': config_sha}
    result.not_proven = [
        'Independent authored claims are not business truth or theorem certification.',
        'Scope is selected undated preaggregate-to-node operations and visible rows in one PostgreSQL snapshot.',
        'Date, closure, multi-hop and general DAG semantics are unsupported.',
        'No Cube runtime, generated model, aggregate-value oracle or future snapshot is certified.']
    result.next_safe_action = 'Review failed opaque contract obligations against the authored specification and rerun.'
    matches = []
    for index, (contract, actual) in enumerate(zip(spec['contracts'], operations), 1):
        changed = mismatches(actual, contract)
        matches.append(not changed)
        result.add(f'grain:c{index:04d}:contract', 'fail' if changed else 'pass',
                   matches=not changed, mismatched_fields=changed,
                   operation='undated_preaggregate_to_node', max_rows=max_rows,
                   statement_timeout_ms=timeout, statement_budget=5)
    if not all(matches):
        return result
    conn = None
    try:
        conn = db.connect(cfg.source.dsn_env)
        with db.read_only(conn, timeout, isolation='repeatable read'):
            for index, (contract, actual) in enumerate(zip(spec['contracts'], operations), 1):
                evidence = evaluate(conn, actual, max_rows)
                if not evidence['complete']:
                    status = 'not_run'
                elif any(evidence[key] for key in _KEY_VIOLATIONS):
                    status = 'fail'
                elif evidence['excluded_null_rows'] and not contract['null_policy']['allow_excluded_rows']:
                    status = 'fail'
                elif not evidence['source_rows'] or not evidence['node_rows'] or not evidence['selected_rows']:
                    status = 'not_run'
                    evidence['reason'] = 'empty_scope'
                else:
                    status = 'pass'
                result.add(f'grain:c{index:04d}:evidence', status,
                           null_exclusion_allowed=contract['null_policy']['allow_excluded_rows'], **evidence)
        # Bind completion to exactly the files checked at startup.
        if hashlib.sha256(_read(source)).hexdigest() != spec['spec_sha256'] or hashlib.sha256(Path(cfg.path).read_bytes()).hexdigest() != config_sha:
            raise ValueError('input changed')
    except Exception:  # no SQL diagnostics, identifiers, keys, paths or DSN text
        result.add('grain:execution', 'error', reason='execution_incomplete')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                result.add('grain:execution', 'error', reason='connection_cleanup')
    return result
