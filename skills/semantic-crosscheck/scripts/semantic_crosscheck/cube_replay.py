"""Actual synthetic Cube mutation replay, independent of operational V4.

Only generated rows reach the dedicated connection. The normal configuration
loader, closure compiler, Cube compiler, mutation implementation and Cube runtime
all run. Gold uses independently authored source SQL, including enumerated paths
rather than the closure builder's level-wise aggregation. No answer rows, SQL,
DSN, exception messages or temporary paths enter Result.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from . import (canon, closure, config, constraints, counterexample as ce, cube_gen,
               cube_run, db, ident, mutate, mutation_fixtures, naming, procutil, provenance)
from .result import EXIT_CONFIG, Result, StageError

FORMAT = 'sxc-synthetic-cube-replay'
VERSION = 1
FILE_LIMIT = 1024 * 1024
SUPPORTED_OPERATORS = mutate.OPERATORS


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _config_text(source, output, operator, dsn_env, timeout):
    text = f'''version = 1
[source]
dsn_env = "{dsn_env}"
owner_dsn_env = "{dsn_env}"
schemas = ["{source}"]
output_schema = "{output}"
statement_timeout_ms = {timeout}
[entities.part]
table = "{source}.part"
key = ["part_id"]
[entities.bom_line]
table = "{source}.bom_line"
key = ["parent_id", "child_id", "line_no"]
[entities.sales_order]
table = "{source}.sales_order"
key = ["order_id"]
[entities.stock]
table = "{source}.stock"
key = ["part_id", "location_id"]
[hierarchies.has_component]
edge = "bom_line"
node = "part"
parent = "parent_id"
child = "child_id"
quantity = "qty_per"
max_depth = 8
[cube]
query_timeout_s = {math.ceil(timeout / 1000)}
[verify]
mutation_operators = ["{operator}"]
'''
    if operator in ('measure_filter_drop', 'measure_normalization_drop', 'dated_shift_one_day'):
        text += '''[measures.orders]
entity = "sales_order"
column = "qty_open"
agg = "sum"
by = "part_id"
'''
        text += ('filter_normalized = { status = ["FIRM"] }\n' if operator == 'measure_normalization_drop'
                 else 'filter = { status = ["FIRM"] }\n')
        if operator == 'dated_shift_one_day':
            text += 'date_column = "due_date"\n'
    elif operator in ('measure_agg_to_max', 'closure_join_swap'):
        text += '''[measures.inventory]
entity = "stock"
column = "qty"
agg = "sum"
by = "part_id"
'''
        if operator == 'closure_join_swap':
            text += 'closure_join = "ancestor"\n'
    return text


def _fixture(operator, seed):
    fixture = mutation_fixtures.generate(operator, seed, 0)
    if operator == 'measure_agg_to_max':
        # The ordinary undated compiler aggregates each key first. Distinguish
        # reaggregation by querying two nonzero keys, not two rows of one key.
        fixture['stock'][1][0] = 'B'
    return fixture


def fixture_payload(operator, seed):
    """Exact full generated fixture; replay never accepts caller SQL or new rows."""
    if operator not in SUPPORTED_OPERATORS:
        raise StageError('cube-replay: unsupported operator', EXIT_CONFIG)
    ce._int(seed, -(2**63), 2**63 - 1, 'seed')
    fixture = _fixture(operator, seed)
    contract = {'config': _config_text('sxc_fixture_source', 'sxc_fixture_output', operator,
                                       'SXC_CUBE_REPLAY_DSN', 10000),
                'query': _spec(operator, 'sxc_fixture_source'),
                'generator_version': mutation_fixtures.GENERATOR_VERSION, 'version': VERSION}
    return {'format': FORMAT, 'schema_version': VERSION, 'synthetic': True,
            'mutation_operator': operator, 'seed': seed, 'case_index': 0,
            'contract_sha256': _hash(contract), 'engine_sha256': provenance.engine_fingerprint(),
            'tables_sha256': _hash(fixture), 'tables': fixture,
            'scope': 'Generated synthetic Cube fixture only; no operational V4 or general equivalence verdict.'}


def load_replay(raw, operator, seed):
    """Strict size/version/engine/template/fixture binding, including duplicate-key rejection."""
    try:
        if len(raw.encode('utf-8')) > FILE_LIMIT:
            raise ValueError('size')
        payload = constraints.decode_json(raw)
        if not isinstance(payload, dict):
            raise ValueError('object required')
        expected = fixture_payload(operator, seed)
        if (type(payload.get('schema_version')) is not int or type(payload.get('seed')) is not int
                or type(payload.get('case_index')) is not int or _hash(payload) != _hash(expected)):
            raise ValueError('binding')
        constraints.validate_fixture(payload['tables'], ce.SCHEMA, None)
        return payload
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise StageError('cube-replay: replay is not the matching generated engine-bound fixture', EXIT_CONFIG) from None


def _spec(operator, source):
    """Cube query and independent source gold with a fixed ordered typed projection."""
    table = lambda name: ident.qualified(source + '.' + name)
    prefix = f'''WITH RECURSIVE paths(a,d,depth,qty) AS (
      SELECT parent_id,child_id,1,qty_per::numeric FROM {table('bom_line')}
      UNION ALL SELECT p.a,e.child_id,p.depth+1,p.qty*e.qty_per
      FROM paths p JOIN {table('bom_line')} e ON e.parent_id=p.d WHERE p.depth < 8
    ) '''
    closure_name = 'has_component_closure'
    descendant = closure_name + '.descendant'
    filter_root = [{'member': closure_name + '.ancestor', 'operator': 'equals', 'values': ['A']}]
    projection = [('key', descendant, 'string')]
    if operator in ('closure_depth_shift', 'closure_path_count_one', 'closure_qty_single_path'):
        field, aggregate = {'closure_depth_shift': ('min_depth', 'min(depth)'),
                            'closure_path_count_one': ('path_count', 'count(*)'),
                            'closure_qty_single_path': ('qty', 'sum(qty)')}[operator]
        member = closure_name + '.' + field
        query = {'dimensions': [descendant], 'filters': filter_root}
        query.setdefault('measures' if field == 'qty' else 'dimensions', []).append(member)
        return query, projection + [('value', member, 'number')], prefix + (
            f"SELECT d AS key,{aggregate} AS value FROM paths WHERE a='A' GROUP BY d")
    if operator == 'closure_end_swap':
        member = 'has_component_descendant.part_class'
        query = {'dimensions': [descendant, member], 'filters': filter_root}
        return query, projection + [('value', member, 'string')], prefix + (
            f"SELECT DISTINCT p.d AS key,n.part_class AS value FROM paths p JOIN {table('part')} n ON n.part_id=p.d WHERE p.a='A'")
    if operator == 'closure_join_swap':
        member = 'm_inventory.value'
        query = {'dimensions': [descendant], 'measures': [member], 'filters': filter_root}
        return query, projection + [('value', member, 'number')], prefix + (
            f"SELECT p.d AS key,sum(s.qty) AS value FROM (SELECT DISTINCT a,d FROM paths WHERE a='A') p "
            f"JOIN {table('stock')} s ON s.part_id=p.a GROUP BY p.d")
    if operator == 'measure_agg_to_max':
        return {'measures': ['m_inventory.value']}, [('value', 'm_inventory.value', 'number')], (
            f"SELECT sum(qty) AS value FROM {table('stock')} WHERE part_id IS NOT NULL")
    member = 'm_orders.value'
    query = {'dimensions': ['m_orders.key'], 'measures': [member]}
    projection = [('key', 'm_orders.key', 'string'), ('value', member, 'number')]
    predicate = "upper(btrim(status))='FIRM'" if operator == 'measure_normalization_drop' else "status='FIRM'"
    if operator == 'dated_shift_one_day':
        query['timeDimensions'] = [{'dimension': 'm_orders.ts', 'dateRange': ['2026-01-01', '2026-01-31']}]
        predicate += " AND due_date >= DATE '2026-01-01' AND due_date < DATE '2026-02-01'"
    return query, projection, (f"SELECT part_id AS key,sum(qty_open) AS value FROM {table('sales_order')} "
                               f"WHERE part_id IS NOT NULL AND {predicate} GROUP BY part_id")


def compare(projection, gold_rows, cube_rows):
    """Full multiset comparison; require every requested member and exact typed decimals."""
    if not isinstance(cube_rows, list) or len(cube_rows) > ce.ROW_LIMIT:
        raise ValueError('invalid Cube rows')
    def cell(value, kind):
        if value is not None:
            if kind == 'string':
                if not isinstance(value, str):
                    raise ValueError('invalid text projection')
                value.encode('utf-8')
            elif kind == 'number':
                # json.loads may already have rounded a fractional JSON number.
                # PG numeric answers use strings; floats cannot prove exactness.
                if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
                    raise ValueError('invalid numeric projection')
                text = str(value)
                if len(text) > 128 or not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', text):
                    raise ValueError('invalid numeric representation')
                try:
                    number = Decimal(text)
                except InvalidOperation:
                    raise ValueError('invalid decimal') from None
                if not number.is_finite() or abs(number.adjusted()) > 1000:
                    raise ValueError('unbounded numeric projection')
        canonical = canon.canon(value, kind)
        return (type(canonical).__name__, canonical)
    if (not isinstance(gold_rows, (list, tuple))
            or any(not isinstance(row, (list, tuple)) or len(row) != len(projection) for row in gold_rows)):
        raise ValueError('invalid gold projection')
    expected = Counter(tuple(cell(value, field[2]) for value, field in zip(row, projection)) for row in gold_rows)
    actual = Counter()
    for row in cube_rows:
        if not isinstance(row, dict) or any(member not in row for _, member, _ in projection):
            raise ValueError('missing Cube projection')
        actual[tuple(cell(row[member], kind) for _, member, kind in projection)] += 1
    return {'equal': expected == actual, 'gold_rows': len(gold_rows), 'cube_rows': len(cube_rows),
            'gold_only_rows': sum((expected - actual).values()), 'cube_only_rows': sum((actual - expected).values())}


def comparison_status(original_gold_agree, mutant_gold_agree):
    return 'pass' if original_gold_agree and not mutant_gold_agree else 'fail'


def _load(client, query):
    """Validate the actual HTTP envelope, so missing data is never an empty answer.

    CubeClient.load applies the same envelope check; this copy keeps replay's own
    row limit and raises ValueError when there is no data list, and compare()
    raises it for a row that is not an object or lacks a projected member, so
    run() reports every malformed answer as a runtime error, never as a kill.
    """
    request = dict(query, limit=ce.ROW_LIMIT + 1)
    deadline = time.monotonic() + client.timeout_s
    while True:
        response = client._req('/cubejs-api/v1/load', {'query': request})
        if not isinstance(response, dict):
            raise ValueError('invalid Cube envelope')
        if response.get('error') == 'Continue wait':
            if time.monotonic() >= deadline:
                raise ValueError('Cube timeout')
            time.sleep(0.1)
            continue
        if 'error' in response or not isinstance(response.get('data'), list):
            raise ValueError('invalid Cube data')
        if len(response['data']) >= request['limit']:
            raise ValueError('Cube row limit')
        return response['data']


def _materialize(conn, cfg, fixture, timeout):
    source = cfg.source.schemas[0]
    with db.write_tx(conn, timeout):
        # closure.build uses temporary unqualified names. Never let these resolve
        # to public or to any existing application schema.
        conn.run(f'SET LOCAL search_path = pg_temp, {ident.quote(source)}, {ident.quote(cfg.source.output_schema)}')
        for name, columns in ce.SCHEMA.items():
            relation = ident.qualified(source + '.' + name)
            ddl = ','.join(ident.quote(col) + ' ' + kind + (' COLLATE "C"' if kind == 'text' else '') for col, kind in columns)
            conn.run(f'CREATE TABLE {relation} ({ddl})')
            for row in fixture[name]:
                params = {f'p{i}': value for i, value in enumerate(row)}
                values = ','.join(f'CAST(:p{i} AS {kind})' for i, (_, kind) in enumerate(columns))
                conn.run(f'INSERT INTO {relation} VALUES ({values})', **params)
        checks, _ = closure.build(conn, cfg, cfg.hierarchy())
        if not checks or any(check[1] != 'pass' for check in checks):
            raise ValueError('closure build failed')


def _relations(cfg):
    return [entity.table for entity in cfg.entities.values()] + [naming.closure_table(cfg, cfg.hierarchy().name)]


def _replay_unchanged(path, digest):
    if path is None:
        return True
    try:
        with path.open('rb') as stream:
            raw = stream.read(FILE_LIMIT + 1)
        return len(raw) <= FILE_LIMIT and hashlib.sha256(raw).hexdigest() == digest
    except OSError:
        return False


def _physical_digest(conn, cfg):
    # JSON text is generated by PostgreSQL for deterministic numeric/date types;
    # only its digest is retained. Relation names are private in-memory keys.
    state = []
    for relation in _relations(cfg):
        rows = conn.run(f'SELECT row_to_json(t)::text FROM {ident.qualified(relation)} t') or []
        state.append(sorted(row[0] for row in rows))
    return _hash(state)


def _cleanup(conn, owned, cfg, timeout):
    """No CASCADE: neither an existing schema nor external dependents can be deleted."""
    conn.run('ROLLBACK')
    with db.write_tx(conn, timeout):
        for name, oid in owned:
            if db.scalar(conn, 'SELECT oid FROM pg_namespace WHERE nspname=:name', name=name) != oid:
                raise ValueError('schema ownership changed')
        for relation in reversed(_relations(cfg)):
            conn.run(f'DROP TABLE IF EXISTS {ident.qualified(relation)}')
        for name, _ in reversed(owned):
            conn.run(f'DROP SCHEMA {ident.quote(name)}')


def run(cfg, args, out):
    """Dedicated synthetic-only optional stage. Missing dependencies never pass."""
    result = Result('cube_replay')
    directory = Path(out) / 'cube_replay'
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / 'fixture.json'
    operator = args.mutation_operator
    if cfg is not None:
        artifact.unlink(missing_ok=True)
        raise StageError('cube-replay: operational configuration is not accepted', EXIT_CONFIG)
    if operator not in SUPPORTED_OPERATORS:
        artifact.unlink(missing_ok=True)
        result.add('cube_replay:comparison', 'not_run', reason='unsupported_operator', synthetic_only=True)
        return result
    if getattr(args, 'allow_scratch_schema', False) is not True:
        artifact.unlink(missing_ok=True)
        raise StageError('cube-replay: --allow-scratch-schema is required for dedicated test schema writes', EXIT_CONFIG)
    try:
        seed = ce._int(args.seed, -(2**63), 2**63 - 1, 'seed')
        timeout = ce._int(args.statement_timeout_ms, 1000, 120000, 'statement_timeout_ms')
    except StageError:
        artifact.unlink(missing_ok=True)
        raise
    env_name = args.dsn_env
    if not isinstance(env_name, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', env_name):
        artifact.unlink(missing_ok=True)
        raise StageError('cube-replay: invalid DSN environment variable name', EXIT_CONFIG)
    payload = fixture_payload(operator, seed)
    replay_path = replay_digest = None
    if getattr(args, 'replay', None):
        try:
            replay_path = Path(args.replay)
            with replay_path.open('rb') as stream:
                raw = stream.read(FILE_LIMIT + 1)
            payload = load_replay(raw.decode('utf-8'), operator, seed)
            replay_digest = hashlib.sha256(raw).hexdigest()
        except StageError:
            artifact.unlink(missing_ok=True)
            raise
        except (OSError, UnicodeError):
            artifact.unlink(missing_ok=True)
            raise StageError('cube-replay: replay file is missing or invalid', EXIT_CONFIG) from None
    # Preserve in-place input until its final byte check; failure removes it.
    if replay_path is None or replay_path.resolve() != artifact.resolve():
        artifact.unlink(missing_ok=True)
    result.not_proven.extend([
        'Only this bounded generated fixture and query are checked; no general equivalence or production-domain proof.',
        'Synthetic Cube replay does not change real-data V4 or operational V1-V5 coverage.',
        'A dedicated test database is required; explicit scratch opt-in does not infer the safety of a caller-selected DSN.',
    ])
    conn = None
    owned = []
    runtime_cfg = None
    succeeded = False
    try:
        endpoint = _hash(db.dsn_from_env(env_name))
        if not cube_run.installed(cube_run.cube_home()) or not procutil.which('node'):
            result.add('cube_replay:runtime', 'not_run', reason='cube_dependency_unavailable', synthetic_only=True)
            return result
        conn = db.connect(env_name)
        prefix = 'sxc_replay_' + secrets.token_hex(12)
        source, output = prefix + '_src', prefix + '_out'
        # Create both atomically and record OIDs only after successful COMMIT.
        with db.write_tx(conn, timeout):
            for schema in (source, output):
                conn.run(f'CREATE SCHEMA {ident.quote(schema)}')
                conn.run(f'REVOKE ALL ON SCHEMA {ident.quote(schema)} FROM PUBLIC')
            schema_oids = [(name, db.scalar(conn, 'SELECT oid FROM pg_namespace WHERE nspname=:name', name=name))
                           for name in (source, output)]
        owned = schema_oids
        with tempfile.TemporaryDirectory(prefix='sxc_cube_replay_') as temporary:
            workspace = Path(temporary)
            config_path = workspace / 'crosscheck.toml'
            config_path.write_text(_config_text(source, output, operator, env_name, timeout), encoding='utf-8')
            config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
            runtime_cfg = config.load(config_path)
            _materialize(conn, runtime_cfg, payload['tables'], timeout)
            # Prevent other sessions changing the private committed inputs while
            # separate Cube processes read them. Only these tiny synthetic tables
            # are locked; no existing relation is touched.
            with db.write_tx(conn, timeout):
                conn.run('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                for relation in _relations(runtime_cfg):
                    conn.run(f'LOCK TABLE {ident.qualified(relation)} IN SHARE MODE')
                initial = _physical_digest(conn, runtime_cfg)
                model, notes = cube_gen.generate(runtime_cfg, conn)
                if notes:
                    raise ValueError('incomplete model')
                mutants = mutate.operators(runtime_cfg, model)
                if len(mutants) != 1 or mutants[0].operator != operator or mutants[0].model == model:
                    raise ValueError('mutation binding failed')
                query, projection, sql = _spec(operator, source)
                gold = conn.run(sql) or []
                if len(gold) > ce.ROW_LIMIT:
                    raise ValueError('gold row limit')
                comparisons = []
                runtime = None
                for label, served in [('original', model), ('mutant', mutants[0].model)]:
                    run_dir = workspace / label
                    cube_gen.write_model(served, run_dir / 'model')
                    with cube_run.serve(runtime_cfg, run_dir) as client:
                        answer = _load(client, query)
                        runtime = client.runtime
                    comparisons.append(compare(projection, gold, answer))
                    if (_physical_digest(conn, runtime_cfg) != initial
                            or _hash(db.dsn_from_env(env_name)) != endpoint
                            or not _replay_unchanged(replay_path, replay_digest)
                            or hashlib.sha256(config_path.read_bytes()).hexdigest() != config_hash
                            or provenance.engine_fingerprint() != payload['engine_sha256']):
                        raise ValueError('inputs changed')
                original, mutant = comparisons
                status = comparison_status(original['equal'], mutant['equal'])
                result.add('cube_replay:comparison', status, synthetic_only=True,
                           mutation_operator=operator, model_mutation_bound=True,
                           original_gold_agree=original['equal'], mutant_gold_agree=mutant['equal'],
                           original={k: v for k, v in original.items() if k != 'equal'},
                           mutant={k: v for k, v in mutant.items() if k != 'equal'},
                           contract_sha256=payload['contract_sha256'], engine_sha256=payload['engine_sha256'],
                           tables_sha256=payload['tables_sha256'], input_rows=ce._rows(payload['tables']),
                           **runtime)
                succeeded = status == 'pass'
    except StageError:
        result.add('cube_replay:runtime', 'not_run', reason='runtime_dependency_or_connection_failed', synthetic_only=True)
    except Exception:
        # Error messages, SQLSTATE payloads, paths and arbitrary class names are
        # deliberately omitted: external engines can include private text there.
        result.add('cube_replay:runtime', 'error', reason='runtime_execution_failed', synthetic_only=True)
    finally:
        if conn is not None:
            try:
                if owned:
                    if runtime_cfg is None:
                        # Config load may fail before a table exists. Only these
                        # successful CREATEs are eligible for deletion.
                        with db.write_tx(conn, timeout):
                            for name, oid in reversed(owned):
                                if db.scalar(conn, 'SELECT oid FROM pg_namespace WHERE nspname=:name', name=name) != oid:
                                    raise ValueError('schema ownership changed')
                                conn.run(f'DROP SCHEMA {ident.quote(name)}')
                    else:
                        _cleanup(conn, owned, runtime_cfg, timeout)
                    result.add('cube_replay:cleanup', 'pass', schemas_removed=len(owned), synthetic_only=True)
            except Exception:
                succeeded = False
                result.add('cube_replay:cleanup', 'error', reason='owned_schema_cleanup_failed', synthetic_only=True)
            finally:
                try:
                    conn.close()
                except Exception:
                    succeeded = False
                    result.add('cube_replay:cleanup', 'error', reason='connection_cleanup_failed', synthetic_only=True)
        if not succeeded or result.status != 'pass':
            artifact.unlink(missing_ok=True)
    if succeeded and result.status == 'pass':
        if not _replay_unchanged(replay_path, replay_digest):
            artifact.unlink(missing_ok=True)
            result.add('cube_replay:inputs', 'error', reason='replay_changed_during_run', synthetic_only=True)
            return result
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        temporary = directory / 'fixture.json.part'
        try:
            temporary.write_bytes(serialized)
            os.replace(temporary, artifact)
            result.checks[0].detail['fixture_sha256'] = hashlib.sha256(serialized).hexdigest()
        except OSError:
            temporary.unlink(missing_ok=True)
            artifact.unlink(missing_ok=True)
            result.add('cube_replay:artifact', 'error', reason='fixture_write_failed', synthetic_only=True)
    result.next_safe_action = 'Review the bound synthetic replay separately from operational V1-V5 results.'
    return result
