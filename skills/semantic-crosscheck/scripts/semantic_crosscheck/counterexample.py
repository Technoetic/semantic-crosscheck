"""Find real PostgreSQL two-query counterexamples on bounded synthetic CTE data.

No tables are created, loaded or changed. Both trusted SQL files run in one
read-only REPEATABLE READ transaction per fixture, with every generated value
bound. SQL can explicitly name other relations: this is not a SQL sandbox or
a proof that queries read only fixture CTEs. Results contain counts/ids only;
only fixture.json contains generated rows, never source database answer rows.
"""
from collections import Counter
import copy
import datetime as dt
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

from . import canon, constraints, db
from .result import EXIT_CONFIG, Result, StageError

SCENARIOS = ('nulls', 'duplicate_join', 'bom_diamond', 'date_boundary')
SCHEMA = {
    'part': (('part_id', 'text'), ('part_class', 'text'), ('unit', 'text'), ('procure', 'text'), ('name', 'text')),
    'bom_line': (('parent_id', 'text'), ('child_id', 'text'), ('line_no', 'integer'), ('qty_per', 'numeric(18,6)')),
    'sales_order': (('order_id', 'text'), ('part_id', 'text'), ('status', 'text'), ('qty_total', 'numeric(18,3)'),
                    ('qty_open', 'numeric(18,3)'), ('due_date', 'date')),
    'stock': (('part_id', 'text'), ('location_id', 'text'), ('qty', 'numeric(18,3)')),
}
FORMAT = 'sxc-synthetic-counterexample'
VERSION = 1
ROW_LIMIT = 10000


def _int(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise StageError(f'counterexample: {name} must be an integer {low}..{high}', EXIT_CONFIG)
    return value


def generate(scenario, seed, case_index):
    """Four small, deterministic families; no source reads, random module or Faker."""
    if scenario not in SCENARIOS:
        raise StageError('counterexample: unsupported scenario', EXIT_CONFIG)
    _int(seed, -(2**63), 2**63 - 1, 'seed')
    _int(case_index, 0, 999, 'case_index')
    digest = hashlib.sha256(f'{VERSION}|{scenario}|{seed}|{case_index}'.encode()).digest()
    numbers = [1 + value % 9 for value in digest]
    fixture = {table: [] for table in SCHEMA}
    fixture['part'] = [[name, 'synthetic', 'unit', 'make', 'synthetic_' + name] for name in ('A', 'B', 'C', 'D', 'noise')]
    order = lambda identifier, product, day, quantity: [identifier, product, 'FIRM', str(quantity), str(quantity), day]
    if scenario == 'nulls':
        fixture['stock'] = [['A', 'L0', None], ['A', 'L1', str(numbers[0])], ['noise', 'L2', '0']]
    elif scenario == 'duplicate_join':
        fixture['stock'] = [['A', 'L0', str(numbers[0])], ['A', 'L1', str(numbers[1])], ['noise', 'L2', '0']]
        fixture['sales_order'] = [order('o1', 'A', '2026-01-10', numbers[2]), order('o2', 'A', '2026-01-11', numbers[3])]
    elif scenario == 'bom_diamond':
        fixture['bom_line'] = [[a, b, i + 1, str(numbers[i])] for i, (a, b) in enumerate((('A', 'B'), ('B', 'D'), ('A', 'C'), ('C', 'D')))]
        fixture['stock'] = [['noise', 'L0', str(numbers[4])]]
    else:
        fixture['sales_order'] = [order('o' + str(i), 'A', day, numbers[i]) for i, day in enumerate(
            ('2025-12-31', '2026-01-01', '2026-01-31', '2026-02-01', '2026-02-02'))]
        fixture['stock'] = [['noise', 'L0', str(numbers[5])]]
    return fixture


def wrap_sql(query, fixture, profile=None):
    """Typed VALUES CTEs plus a nested candidate CTE; values never interpolated."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError('Empty trusted SQL')
    query = query.strip()
    if query.endswith(';'):
        query = query[:-1]
    parts, params = [], {}
    def cast(expression, data_type):
        # Explicit deterministic equality for the integrity profile's text keys
        # and checks, including databases with nondeterministic default collation.
        suffix = ' COLLATE "C"' if profile is not None and data_type == 'text' else ''
        return f'CAST({expression} AS {data_type})' + suffix
    for table, columns in SCHEMA.items():
        rows = fixture[table]
        values = []
        for row_number, row in enumerate(rows):
            if len(row) != len(columns):
                raise ValueError('Synthetic row width')
            cells = []
            for column_number, ((name, data_type), value) in enumerate(zip(columns, row)):
                parameter = f'sxc_{table}_{row_number}_{column_number}'
                params[parameter] = value
                cells.append(cast(':' + parameter, data_type))
            values.append('(' + ', '.join(cells) + ')')
        names = ', '.join(column[0] for column in columns)
        body = ('VALUES ' + ', '.join(values)) if rows else (
            'SELECT ' + ', '.join(cast('NULL', data_type) for _, data_type in columns) + ' WHERE FALSE')
        parts.append(f'{table}({names}) AS ({body})')
    return ('WITH ' + ',\n'.join(parts) + '\n, sxc_candidate AS (\n' + query +
            f'\n) SELECT * FROM sxc_candidate LIMIT {ROW_LIMIT + 1}'), params


def _cell(value):
    if value is None:
        return ('null',)
    if isinstance(value, bool):
        return ('boolean', canon.canon(value, 'boolean'))
    if isinstance(value, (int, float, Decimal)):
        return ('number', canon.canon(value, 'number'))
    if isinstance(value, str):
        return ('string', canon.canon(value, 'string'))
    if isinstance(value, dt.datetime):
        if value.utcoffset() is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        # SQL-to-SQL preserves microseconds; Cube's millisecond time conversion
        # would hide a difference and is intentionally not used here.
        return ('datetime', canon.canon(value))
    if isinstance(value, dt.date):
        return ('date', canon.canon(value))
    if isinstance(value, (bytes, bytearray)):
        return ('bytes', bytes(value))
    if isinstance(value, (list, tuple)):
        return ('array', tuple(_cell(item) for item in value))
    if isinstance(value, dict):
        return ('object', tuple(sorted((str(key), _cell(item)) for key, item in value.items())))
    raise ValueError('Unsupported SQL result value type')


def compare_answers(reference_columns, reference_rows, candidate_columns, candidate_rows):
    """Order-independent bag comparison, with exact ordered projection names."""
    for columns in (reference_columns, candidate_columns):
        if len(columns) != len(set(columns)):
            raise ValueError('Duplicate SQL projection aliases')
    reference = Counter(tuple(_cell(value) for value in row) for row in reference_rows)
    candidate = Counter(tuple(_cell(value) for value in row) for row in candidate_rows)
    columns_match = list(reference_columns) == list(candidate_columns)
    return {'equal': columns_match and reference == candidate,
            'column_names_match': columns_match, 'reference_columns': len(reference_columns),
            'candidate_columns': len(candidate_columns), 'reference_rows': len(reference_rows),
            'candidate_rows': len(candidate_rows), 'reference_only_rows': sum((reference - candidate).values()),
            'candidate_only_rows': sum((candidate - reference).values())}


def evaluate(conn, fixture, reference_sql, candidate_sql, timeout_ms, profile=None):
    answers = []
    with db.read_only(conn, timeout_ms):
        conn.run('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        for query in (reference_sql, candidate_sql):
            sql, params = wrap_sql(query, fixture, profile=profile)
            prepared = conn.prepare(sql)
            try:
                rows = prepared.run(**params) or []
                columns = [column['name'] for column in (prepared.columns or [])]
                if len(rows) > ROW_LIMIT:
                    raise ValueError('SQL result row limit exceeded')
                answers.append((columns, rows))
            finally:
                prepared.close()
    return compare_answers(*answers[0], *answers[1])


def shrink(fixture, predicate, max_steps, profile=None):
    """Greedy row deletion. Invalid trials are skipped, never accepted as witnesses.

    A query error makes deletion 1-minimality unproven even when the remaining
    successfully evaluable deletions have reached a fixed point.
    """
    constraints.validate_fixture(fixture, SCHEMA, profile)
    current = copy.deepcopy(fixture)
    steps = accepted = errors = invalid_domain = 0
    def summary(exhausted):
        return {'tables': current, 'steps': steps, 'accepted': accepted, 'shrink_errors': errors,
                'invalid_domain_trials': invalid_domain,
                'minimality_scope': 'admissible_single_row_deletions' if profile else 'single_row_deletions',
                'deletion_1_minimal': not exhausted and errors == 0, 'budget_exhausted': exhausted}
    while True:
        changed = False
        for table in SCHEMA:
            for index in range(len(current[table])):
                if steps >= max_steps:
                    return summary(True)
                trial = copy.deepcopy(current)
                del trial[table][index]
                steps += 1
                try:
                    constraints.validate_fixture(trial, SCHEMA, profile)
                except constraints.ConstraintViolation:
                    invalid_domain += 1
                    continue
                try:
                    remains_witness = predicate(trial)
                except Exception:  # Error text can quote data; keep only the count.
                    errors += 1
                    continue
                if remains_witness:
                    current, changed = trial, True
                    accepted += 1
                    break
            if changed:
                break
        if not changed:
            return summary(False)


def _rows(fixture):
    return sum(len(value) for value in fixture.values())


def _read_sql(path):
    try:
        raw = Path(path).read_bytes()
        if not raw or len(raw) > 1024 * 1024:
            raise ValueError('SQL file size')
        text = raw.decode('utf-8-sig')
        if not text.strip():
            raise ValueError('blank SQL file')
        return text, hashlib.sha256(raw).hexdigest()
    except (OSError, UnicodeError, ValueError, TypeError):
        raise StageError('counterexample: trusted SQL file missing, blank, too large or not UTF-8', EXIT_CONFIG) from None


def _replay(raw, scenario, seed, digests, profile=None):
    try:
        payload = constraints.decode_json(raw)
        if (payload['format'] != FORMAT or type(payload['schema_version']) is not int or payload['schema_version'] != VERSION
                or type(payload['generator_version']) is not int or payload['generator_version'] != VERSION or payload['synthetic'] is not True
                or payload['scenario'] != scenario or type(payload['seed']) is not int or payload['seed'] != seed or payload['sql_sha256'] != digests):
            raise ValueError('Replay contract')
        expected = constraints.binding(profile)
        binding = payload.get('constraints')
        if binding is None and 'constraints' not in payload and profile is None:
            pass  # Original version 1 fixture, explicitly legacy/unconstrained mode only.
        elif (not isinstance(binding, dict) or binding != expected or type(binding.get('version')) is not int):
            raise ValueError('Replay constraint profile/mode')
        original = generate(scenario, seed, payload['case_index'])
        tables = payload['tables']
        constraints.validate_fixture(tables, SCHEMA, profile)
        if set(tables) != set(SCHEMA):
            raise ValueError('Replay schema')
        for table in SCHEMA:
            if not isinstance(tables[table], list):
                raise ValueError('Replay table')
            available = Counter(json.dumps(row, sort_keys=True) for row in original[table])
            requested = Counter(json.dumps(row, sort_keys=True) for row in tables[table])
            if requested - available:
                raise ValueError('Replay contains non-generated rows')
        return payload
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise StageError('counterexample: replay is not a matching generated row-deletion fixture', EXIT_CONFIG) from None


def run(cfg, args, out):
    """Search/row-delete shrink actual SQL disagreements; no operational config needed.

    With --metamorphic-sql the stage instead checks one closure SQL against BOM metamorphic relations.
    """
    if getattr(args, 'gold_sql', None) is not None and getattr(args, 'mutation_operator', None) is None:
        raise StageError('counterexample: --gold-sql requires --mutation-operator', EXIT_CONFIG)
    if getattr(args, 'mutation_operator', None) is not None:
        from . import mutation_fixtures
        return mutation_fixtures.run(cfg, args, out)
    if getattr(args, 'metamorphic_sql', None) is not None:
        from . import metamorphic   # imported here: metamorphic builds on this module
        return metamorphic.run(cfg, args, out)
    if args.reference_sql is None or args.candidate_sql is None:
        raise StageError('counterexample: pass --reference-sql and --candidate-sql, or one --metamorphic-sql file', EXIT_CONFIG)
    if getattr(args, 'scenario', None) == 'bom_exhaustive':
        raise StageError('counterexample: --scenario bom_exhaustive is supported only with --metamorphic-sql', EXIT_CONFIG)
    seed = _int(args.seed, -(2**63), 2**63 - 1, 'seed')
    maximum = _int(args.max_examples, 1, 1000, 'max_examples')
    budget = _int(args.max_shrink_steps, 0, 10000, 'max_shrink_steps')
    timeout = _int(getattr(args, 'statement_timeout_ms', 10000), 1000, 120000, 'statement_timeout_ms')
    scenario = args.scenario or 'nulls'
    if scenario not in SCENARIOS or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.dsn_env):
        raise StageError('counterexample: invalid scenario or DSN environment variable name', EXIT_CONFIG)
    profile_path = getattr(args, 'constraints', None)
    profile = constraints.load(profile_path, SCHEMA) if profile_path is not None else None
    reference, reference_sha = _read_sql(args.reference_sql)
    candidate, candidate_sha = _read_sql(args.candidate_sql)
    digests = {'reference': reference_sha, 'candidate': candidate_sha}
    replay = None
    if args.replay:
        try:
            replay_path = Path(args.replay)
            if replay_path.stat().st_size > 1024 * 1024:
                raise ValueError('Replay file size')
            replay = _replay(replay_path.read_text(encoding='utf-8'), scenario, seed, digests, profile)
        except (OSError, UnicodeError, ValueError):
            raise StageError('counterexample: replay file missing, too large or invalid', EXIT_CONFIG) from None
    directory = Path(out) / 'counterexample'
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / 'fixture.json'
    artifact_path.unlink(missing_ok=True)
    result = Result('counterexample')
    result.not_proven.append('Trusted SQL may explicitly reference real tables; CTE fixtures are not a SQL sandbox or proof of source-independent execution.')
    if profile is None:
        result.not_proven.append('Synthetic fixture scope is unconstrained: no declared PK, UNIQUE, NOT NULL, foreign key or CHECK domain is enforced.')
    else:
        result.not_proven.append('Only this exact version 1 integrity profile is enforced; it does not represent every PostgreSQL DDL feature or production data rule.')
        result.not_proven.append('Constrained fixture text columns use PostgreSQL C collation and exact equality; production collation behavior is not reproduced and ordered text CHECKs are rejected.')
    checked = attempted = invalid_domain = 0
    conn = None
    try:
        found = None
        for index in ([replay['case_index']] if replay else range(maximum)):
            fixture = replay['tables'] if replay else generate(scenario, seed, index)
            attempted += 1
            try:
                constraints.validate_fixture(fixture, SCHEMA, profile)
            except constraints.ConstraintViolation:
                invalid_domain += 1
                continue
            if conn is None:
                conn = db.connect(args.dsn_env)
            answer = evaluate(conn, fixture, reference, candidate, timeout, profile)
            checked += 1
            if not answer['equal']:
                found = (index, fixture, answer)
                break
        if found is None:
            result.add('counterexample:search', 'not_run', '', found=False, examples=checked, bounded=True,
                       attempted_examples=attempted, invalid_domain_examples=invalid_domain,
                       constraints_mode=constraints.binding(profile)['mode'],
                       reason='no_admissible_generated_examples' if checked == 0 else 'no_disagreement_in_bounded_search')
            result.not_proven.append('No disagreement within the generated examples proves neither SQL equivalence nor correctness on other inputs.')
            result.next_safe_action = 'Review the supported synthetic scenarios and trusted SQL, or increase the bounded search budget.'
            return result
        index, fixture, answer = found
        def predicate(trial):
            return not evaluate(conn, trial, reference, candidate, timeout, profile)['equal']
        reduced = shrink(fixture, predicate, budget, profile=profile)
        constraints.validate_fixture(reduced['tables'], SCHEMA, profile)
        final_answer = evaluate(conn, reduced['tables'], reference, candidate, timeout, profile)
        if final_answer['equal']:
            result.add('counterexample:recheck', 'error', '', reason='witness_not_stable')
            result.next_safe_action = 'Review nondeterministic or external-table SQL; no fixture was saved.'
            return result
        # SQL edits during a run must not produce a falsely digest-bound replay.
        if _read_sql(args.reference_sql)[1] != reference_sha or _read_sql(args.candidate_sql)[1] != candidate_sha:
            result.add('counterexample:inputs', 'error', '', reason='sql_changed_during_run')
            return result
        if profile is not None and not profile.unchanged():
            result.add('counterexample:inputs', 'error', '', reason='constraint_profile_changed_during_run')
            result.next_safe_action = 'Restore the exact constraint profile or rerun with the changed profile; no fixture was saved.'
            return result
        payload = {'format': FORMAT, 'schema_version': VERSION, 'synthetic': True, 'generator_version': VERSION,
                   'scenario': scenario, 'seed': seed, 'case_index': index, 'sql_sha256': digests,
                   'constraints': constraints.binding(profile),
                   'tables': reduced['tables'], 'shrink': {key: value for key, value in reduced.items() if key != 'tables'},
                   'scope': ('Row-deletion witness within this exact constraint profile; minimality considers only admissible single-row deletions.' if profile else
                             'Unconstrained row-deletion witness for these trusted SQL files; no declared integrity domain.') +
                            ' No global minimality or general equivalence proof.'}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        temporary = directory / 'fixture.json.part'
        temporary.write_bytes(serialized)
        os.replace(temporary, artifact_path)
        result.add('counterexample:search', 'fail', '', found=True, examples=checked, case_index=index,
                   attempted_examples=attempted, invalid_domain_examples=invalid_domain,
                   constraints_mode=constraints.binding(profile)['mode'], constraint_profile_sha256=profile.sha256 if profile else None,
                   original_rows=_rows(fixture), minimized_rows=_rows(reduced['tables']), shrink_steps=reduced['steps'],
                   shrink_errors=reduced['shrink_errors'],
                   shrink_invalid_domain_trials=reduced['invalid_domain_trials'], minimality_scope=reduced['minimality_scope'],
                   deletion_1_minimal=reduced['deletion_1_minimal'], budget_exhausted=reduced['budget_exhausted'],
                   fixture_sha256=hashlib.sha256(serialized).hexdigest(), **{k: v for k, v in final_answer.items() if k != 'equal'})
        if reduced['budget_exhausted']:
            result.not_proven.append('Row-deletion shrinking exhausted its budget; deletion 1-minimality is not proven.')
        if reduced['shrink_errors']:
            result.not_proven.append('Some row deletions could not execute; those trials were never accepted and deletion 1-minimality is not proven.')
        result.not_proven.append(('Deletion 1-minimality covers only admissible single-row deletions within the bound profile, not simultaneous deletions or a global minimum.' if profile else
                                  'Deletion 1-minimality is not a global minimum; only these four synthetic scenario families are searched.'))
        result.next_safe_action = 'Replay fixture.json with the same two trusted SQL files, then review the SQL disagreement.'
    except StageError:
        raise
    except Exception as error:
        arg = error.args[0] if error.args else None
        code = arg.get('C', '') if isinstance(arg, dict) else ''
        result.add('counterexample:query', 'error', '', exception=type(error).__name__,
                   sqlstate=code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else '', examples=checked)
        result.next_safe_action = 'Fix the query or environment; an execution error is not a counterexample and no fixture was saved.'
    finally:
        if conn is not None:
            conn.close()
    return result
