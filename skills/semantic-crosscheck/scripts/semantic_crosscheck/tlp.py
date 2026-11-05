"""Bounded SQL ternary-logic partition consistency on labelled synthetic data.

Trusted SELECT/expression text has the counterexample stage's trust boundary:
read-only is enforced, but fixture CTEs are not a SQL sandbox. Each comparison
materializes the relation and predicate once; a second evaluation must agree.
Optional bounded NoREC instead evaluates WHERE and CASE predicates independently
over a materialized relation, with separate full-row diagnostic stability checks.
Only explicit replay artifacts contain generated input rows. Results never do.
"""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from . import constraints, counterexample as ce, db, ident
from .result import EXIT_CONFIG, Result, StageError

VERSION = 1
FORMAT = 'sxc-synthetic-tlp'
CHECK_IDS = {
    'tlp': {'engine': 'tlp:engine', 'consistency': 'tlp:consistency', 'execution': 'tlp:execution'},
    'norec': {'engine': 'norec:engine', 'consistency': 'norec:consistency', 'execution': 'norec:execution'},
}
SPEC_LIMIT = 65536
REPLAY_LIMIT = 1024 * 1024
ANSWER_LIMIT = 16 * 1024 * 1024
_P = '__sxc_tlp_p'
_FIELDS = {'schema_version', 'scenario', 'relation_sql', 'predicate_sql', 'aggregate'}
_ANSWER_FIELDS = {'baseline', 'partitioned', 'baseline_aggregate', 'partition_aggregate', 'aggregate_type'}
_FORBIDDEN = {'WITH', 'RECURSIVE', 'INTO', 'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'COPY',
              'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'CALL', 'DO', 'GRANT', 'REVOKE', 'LOCK', 'FOR'}
_VOLATILE = {'RANDOM', 'CLOCK_TIMESTAMP', 'TIMEOFDAY', 'NEXTVAL', 'SETVAL', 'GEN_RANDOM_UUID'}


def _quoted_call_names(lexemes):
    """Exclude column-list aliases only in a lexical FROM/JOIN relation slot.

    Each delimiter scope has its own clause and relation state. In particular,
    commas inside SELECT lists, function arguments and arrays cannot introduce
    table aliases. This recognizes common relation forms, not arbitrary SQL.
    """
    aliases = set()
    stack = []
    clause, relation = None, None
    clause_ends = {'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET',
                   'FETCH', 'WINDOW', 'UNION', 'INTERSECT', 'EXCEPT', 'VALUES'}
    join_words = {'ON', 'USING', 'INNER', 'OUTER', 'LEFT', 'RIGHT', 'FULL',
                  'CROSS', 'NATURAL'}
    for index, (kind, value) in enumerate(lexemes):
        if value in ('(', '[') and kind == 'symbol':
            relation_open = value == '(' and clause == 'from' and relation in ('start', 'name')
            # A parenthesized relation may contain a joined table or SELECT;
            # a table function's arguments always begin in expression context.
            grouped_relation = relation_open and relation == 'start'
            stack.append((clause, 'alias' if relation_open else None))
            clause, relation = ('from', 'start') if grouped_relation else (None, None)
            continue
        if value in (')', ']') and kind == 'symbol':
            clause, relation = stack.pop() if stack else (None, None)
            continue
        if kind == 'word':
            if value == 'SELECT':
                clause, relation = 'select', None
                continue
            if value in clause_ends:
                clause, relation = None, None
                continue
            # IS [NOT] DISTINCT FROM is an expression, not a FROM clause.
            if (value == 'FROM' and clause == 'select'
                    and (index == 0 or lexemes[index - 1] != ('word', 'DISTINCT'))):
                clause, relation = 'from', 'start'
                continue
            if (value == 'FROM' and clause == 'from' and relation == 'name'
                    and lexemes[index - 1] == ('word', 'ROWS')):
                # ROWS FROM (...) is a table-function group, not a subquery.
                continue
            if clause == 'from' and value == 'JOIN':
                relation = 'start'
                continue
            if clause == 'from' and value in join_words:
                relation = None
                continue
        if clause != 'from':
            continue
        if (kind, value) == ('symbol', ','):
            relation = 'start'
        elif relation == 'start' and kind == 'word' and value in ('LATERAL', 'ONLY'):
            continue
        elif relation in ('start', 'qualified') and kind in ('word', 'quoted'):
            relation = 'name'
        elif relation == 'name' and (kind, value) == ('symbol', '.'):
            relation = 'qualified'
        elif relation == 'name' and (kind, value) == ('symbol', '*'):
            relation = 'alias'
        elif relation in ('name', 'alias') and (kind, value) == ('word', 'AS'):
            relation = 'explicit_alias'
        elif relation in ('name', 'alias', 'explicit_alias') and kind in ('word', 'quoted'):
            aliases.add(index)
            relation = None
        else:
            relation = None
    return [value for index, (kind, value) in enumerate(lexemes[:-1])
            if kind == 'quoted' and value in _VOLATILE and index not in aliases
            and lexemes[index + 1] == ('symbol', '(')]


def _read(path, maximum):
    with Path(path).open('rb') as stream:
        value = stream.read(maximum + 1)
    if not value or len(value) > maximum:
        raise ValueError('Bounded input file')
    return value


def _tokens(sql, quoted_calls=False):
    """Small, conservative lexical gate, deliberately not a PostgreSQL parser."""
    if not isinstance(sql, str) or not sql.strip() or '\0' in sql or len(sql) > SPEC_LIMIT:
        raise ValueError('SQL text')
    code = []
    lexemes = []
    i = 0
    while i < len(sql):
        character = sql[i]
        if character in "'\"":
            quote = character
            code.append(' ')
            i += 1
            start = i
            while i < len(sql):
                if sql[i] == '\\':
                    # Backslash strings depend on session settings; unsupported.
                    raise ValueError('Unsupported string escape')
                if sql[i] == quote:
                    i += 1
                    if i < len(sql) and sql[i] == quote:
                        i += 1
                        continue
                    break
                i += 1
            else:
                raise ValueError('Unclosed SQL quote')
            if quoted_calls:
                lexemes.append(('quoted', sql[start:i - 1].replace('""', '"').upper())
                               if quote == '"' else ('literal', ''))
            continue
        if character in ';$' or sql[i:i + 2] in ('--', '/*', '*/'):
            raise ValueError('Unsupported SQL lexical form')
        if quoted_calls and (character.isalpha() or character == '_'):
            start = i
            while i < len(sql) and (sql[i].isalnum() or sql[i] == '_'):
                i += 1
            word = sql[start:i]
            code.append(word)
            lexemes.append(('word', word.upper()))
            continue
        code.append(character)
        if quoted_calls and not character.isspace():
            lexemes.append(('symbol', character))
        i += 1
    calls = _quoted_call_names(lexemes) if quoted_calls else []
    return re.findall(r'[A-Za-z_][A-Za-z_0-9]*', ''.join(code).upper()) + calls


def load_spec(path):
    """Load exact version-1 fields and retain only a raw-file hash for binding."""
    try:
        raw = _read(path, SPEC_LIMIT)
        spec = constraints.decode_json(raw.decode('utf-8-sig'))
        if not isinstance(spec, dict) or set(spec) != _FIELDS:
            raise ValueError('Spec fields')
        if type(spec['schema_version']) is not int or spec['schema_version'] != VERSION:
            raise ValueError('Spec version')
        if spec['scenario'] not in ce.SCENARIOS:
            raise ValueError('Scenario')
        relation = _tokens(spec['relation_sql'])
        predicate = _tokens(spec['predicate_sql'])
        if not relation or relation[0] != 'SELECT' or set(relation) & _FORBIDDEN:
            raise ValueError('Restricted relational SELECT')
        if set(predicate) & (_FORBIDDEN | {'SELECT'}):
            raise ValueError('Restricted predicate')
        aggregate = spec['aggregate']
        if not isinstance(aggregate, dict) or set(aggregate) != {'function', 'column'}:
            raise ValueError('Aggregate fields')
        if aggregate['function'] not in ('count', 'sum'):
            raise ValueError('Aggregate function')
        column = aggregate['column']
        if column != '*':
            ident.check(column)
            if column == _P:
                raise ValueError('Reserved predicate alias')
        elif aggregate['function'] != 'count':
            raise ValueError('COUNT star only')
        spec['spec_sha256'] = hashlib.sha256(raw).hexdigest()
        return spec
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        raise StageError('tlp: spec must be a bounded strict version-1 relational SELECT/predicate/aggregate JSON document', EXIT_CONFIG) from None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


def _generator_sha():
    # Bind the actual generator implementation as well as its declared version.
    return hashlib.sha256(Path(ce.__file__).read_bytes()).hexdigest()


def make_replay(spec, seed, case_index, oracle='tlp'):
    tables = ce.generate(spec['scenario'], seed, case_index)
    case = dict(format=FORMAT, schema_version=VERSION, synthetic=True,
                spec_sha256=spec['spec_sha256'], generator_version=ce.VERSION,
                generator_sha256=_generator_sha(), seed=seed, scenario=spec['scenario'],
                case_index=case_index, tables=tables, tables_sha256=_hash(tables))
    if oracle == 'norec':
        case.update(format='sxc-synthetic-norec', oracle=oracle)
    return case


def decode_replay(raw, spec, seed, oracle='tlp'):
    try:
        if len(raw.encode('utf-8')) > REPLAY_LIMIT:
            raise ValueError('Replay size')
        payload = constraints.decode_json(raw)
        if not isinstance(payload, dict) or type(payload.get('case_index')) is not int:
            raise ValueError('Replay fields')
        expected = make_replay(spec, seed, payload['case_index'], oracle=oracle)
        if (_hash(payload) != _hash(expected) or type(payload.get('schema_version')) is not int
                or type(payload.get('generator_version')) is not int or type(payload.get('seed')) is not int
                or payload.get('synthetic') is not True):
            raise ValueError('Replay binding')
        return payload
    except (ValueError, TypeError, KeyError, RecursionError, StageError):
        raise StageError('tlp: replay does not match this exact spec, seed and generated synthetic fixture', EXIT_CONFIG) from None


def _partition_sql():
    return ('SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p '
            'UNION ALL SELECT * FROM sxc_tlp_evaluated WHERE NOT __sxc_tlp_p '
            'UNION ALL SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p IS NULL')


def _query(spec):
    # Known volatile inputs are unsupported even when a coincidental second
    # evaluation happens to agree. Custom functions remain trusted SQL.
    if (_VOLATILE & set(_tokens(spec['relation_sql'], quoted_calls=True)
                        + _tokens(spec['predicate_sql'], quoted_calls=True))):
        raise ValueError('Known unstable SQL input')
    aggregate = spec['aggregate']
    column = '*' if aggregate['column'] == '*' else ident.quote(aggregate['column'])
    expression = aggregate['function'].upper() + '(' + column + ')'
    partials = ' UNION ALL '.join(
        'SELECT ' + expression + ' AS value FROM sxc_tlp_evaluated WHERE ' + condition
        for condition in (_P, 'NOT ' + _P, _P + ' IS NULL'))
    return f'''WITH sxc_tlp_relation AS MATERIALIZED (
      SELECT * FROM ({spec['relation_sql']}) AS sxc_tlp_input LIMIT {ce.ROW_LIMIT + 1}
    ), sxc_tlp_evaluated AS MATERIALIZED (
      SELECT r.*, ({spec['predicate_sql']}) AS {_P} FROM sxc_tlp_relation AS r
    ), sxc_tlp_partitioned AS MATERIALIZED ({_partition_sql()}),
    sxc_tlp_aggregate AS MATERIALIZED (SELECT {expression} AS value FROM sxc_tlp_evaluated),
    sxc_tlp_partials AS MATERIALIZED ({partials})
    SELECT json_build_object(
      'baseline', (SELECT COALESCE(json_agg(row_to_json(e)), '[]'::json) FROM sxc_tlp_evaluated AS e),
      'partitioned', (SELECT COALESCE(json_agg(row_to_json(e)), '[]'::json) FROM sxc_tlp_partitioned AS e),
      'baseline_aggregate', (SELECT value FROM sxc_tlp_aggregate),
      'partition_aggregate', (SELECT SUM(value) FROM sxc_tlp_partials),
      'aggregate_type', (SELECT pg_typeof(value)::text FROM sxc_tlp_aggregate)
    )::text AS tlp_document'''


def compare(payload):
    """Compare bags and aggregates without converting exact decimals to float."""
    if not isinstance(payload, dict) or set(payload) != _ANSWER_FIELDS:
        raise ValueError('Incomplete TLP engine document')
    if payload['aggregate_type'] not in ('numeric', 'bigint', 'integer', 'smallint'):
        raise ValueError('Exact numeric aggregate required')
    rowsets = []
    columns = None
    for name in ('baseline', 'partitioned'):
        rows = payload[name]
        if not isinstance(rows, list) or len(rows) > ce.ROW_LIMIT:
            raise ValueError('Bounded TLP row bag')
        data = []
        for row in rows:
            if not isinstance(row, dict) or _P not in row or (row[_P] is not None and type(row[_P]) is not bool):
                raise ValueError('TLP row/predicate')
            keys = list(row)
            if columns is None:
                columns = keys
            if keys != columns:
                raise ValueError('Inconsistent TLP columns')
            data.append(list(row.values()))
        rowsets.append(data)
    aggregates = [payload[name] for name in ('baseline_aggregate', 'partition_aggregate')]
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, Decimal))
                                  or (isinstance(value, Decimal) and not value.is_finite())) for value in aggregates):
        raise ValueError('Exact finite aggregate')
    bag = ce.compare_answers(columns or [], rowsets[0], columns or [], rowsets[1])
    aggregate_equal = aggregates[0] == aggregates[1]
    return dict(equal=bag['equal'] and aggregate_equal, multiset_equal=bag['equal'],
                aggregate_equal=aggregate_equal, baseline_rows=len(rowsets[0]), partition_rows=len(rowsets[1]),
                baseline_aggregate=aggregates[0], partition_aggregate=aggregates[1])


def _norec_reference_sql(predicate):
    return f'COALESCE(SUM(CASE WHEN ({predicate}) THEN 1 ELSE 0 END), 0)'


def _norec_query(spec):
    """Freeze only the relation: WHERE and CASE evaluate the predicate independently.

    Diagnostic rows are a third evaluation used for observable stability, not a shared predicate for the oracle.
    Materialization bounds the comparison but limits optimizer/index coverage relative to unrestricted NoREC.
    """
    if _VOLATILE & set(_tokens(spec['relation_sql'], quoted_calls=True)
                       + _tokens(spec['predicate_sql'], quoted_calls=True)):
        raise ValueError('Known unstable SQL input')
    predicate = spec['predicate_sql']
    return f'''WITH sxc_tlp_relation AS MATERIALIZED (
      SELECT * FROM ({spec['relation_sql']}) AS sxc_tlp_input LIMIT {ce.ROW_LIMIT + 1}
    ), sxc_norec_diagnostic AS MATERIALIZED (
      SELECT r.*, ({predicate}) AS {_P} FROM sxc_tlp_relation AS r
    ) SELECT json_build_object(
      'baseline', (SELECT COALESCE(json_agg(row_to_json(e)), '[]'::json) FROM sxc_norec_diagnostic AS e),
      'optimized_count', (SELECT COUNT(*) FROM sxc_tlp_relation AS r WHERE ({predicate})),
      'reference_count', (SELECT {_norec_reference_sql(predicate)} FROM sxc_tlp_relation AS r)
    )::text AS norec_document'''


def _diagnostic_payload(first, second=None):
    return dict(baseline=first, partitioned=first if second is None else second,
                baseline_aggregate=0, partition_aggregate=0, aggregate_type='bigint')


def compare_norec(payload):
    """Strict bounded integer predicate counts; diagnostic rows never leave the evaluator."""
    if not isinstance(payload, dict) or set(payload) != {'baseline', 'optimized_count', 'reference_count'}:
        raise ValueError('Incomplete NoREC engine document')
    # Reuse the exact, bounded row/predicate contract and bag validator.
    rows = compare(_diagnostic_payload(payload['baseline']))['baseline_rows']
    counts = [payload[key] for key in ('optimized_count', 'reference_count')]
    if any(type(value) is not int or not 0 <= value <= rows for value in counts):
        raise ValueError('Bounded integer NoREC counts required')
    return dict(equal=counts[0] == counts[1], baseline_rows=rows,
                optimized_count=counts[0], reference_count=counts[1])


def _fetch(conn, fixture, spec, oracle='tlp'):
    sql, params = ce.wrap_sql(_norec_query(spec) if oracle == 'norec' else _query(spec), fixture)
    prepared = conn.prepare(sql)
    try:
        rows = prepared.run(**params)
        if not isinstance(rows, list) or len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
            raise ValueError('Incomplete PostgreSQL TLP response')
        text = rows[0][0]
        if len(text.encode('utf-8')) > ANSWER_LIMIT:
            raise ValueError('TLP response bound')
        return constraints.decode_json(text)
    finally:
        prepared.close()


def evaluate(conn, fixture, spec, timeout_ms, oracle='tlp'):
    """Freeze every per-comparison input and reject observable input changes."""
    with db.read_only(conn, timeout_ms, isolation='repeatable read'):
        if oracle == 'norec':
            first = _fetch(conn, fixture, spec, oracle=oracle)
            answer = compare_norec(first)
            second = _fetch(conn, fixture, spec, oracle=oracle)
            second_answer = compare_norec(second)
            stable = compare(_diagnostic_payload(first['baseline'], second['baseline']))
            if (not stable['equal'] or first['optimized_count'] != second['optimized_count']
                    or first['reference_count'] != second['reference_count']):
                raise ValueError('Unstable NoREC inputs')
            return answer if second_answer['equal'] else second_answer
        first = _fetch(conn, fixture, spec)
        answer = compare(first)
        second = _fetch(conn, fixture, spec)
        second_answer = compare(second)
        # Compare the actual baseline row bag, including predicate outcomes;
        # ordering is irrelevant and every decimal digit remains significant.
        stable = compare(dict(first, partitioned=second['baseline'],
                              partition_aggregate=second['baseline_aggregate']))
        if not stable['equal']:
            raise ValueError('Unstable TLP inputs')
        if not second_answer['equal']:
            return second_answer
        return answer


def evaluate_evidence(conn, fixture, spec, timeout_ms, oracle='tlp'):
    """Opt-in private evidence: both complete oracle arms must be stable."""
    from . import tlp_reduction
    with db.read_only(conn, timeout_ms, isolation='repeatable read'):
        first = _fetch(conn, fixture, spec, **({'oracle': oracle} if oracle == 'norec' else {}))
        first_signature = tlp_reduction.signature(first, oracle)
        second = _fetch(conn, fixture, spec, **({'oracle': oracle} if oracle == 'norec' else {}))
        second_signature = tlp_reduction.signature(second, oracle)
        if oracle == 'norec':
            answer, other = compare_norec(first), compare_norec(second)
            stable = compare(_diagnostic_payload(first['baseline'], second['baseline']))['equal']
            stable = (stable and answer['optimized_count'] == other['optimized_count']
                      and answer['reference_count'] == other['reference_count'])
        else:
            answer = compare(first)
            stable = compare(dict(first, partitioned=second['baseline'],
                                  partition_aggregate=second['baseline_aggregate']))['equal']
            stable = stable and compare(dict(first, baseline=first['partitioned'],
                partitioned=second['partitioned'], baseline_aggregate=first['partition_aggregate'],
                partition_aggregate=second['partition_aggregate']))['equal']
        if not stable or first_signature != second_signature:
            raise ValueError('Unstable reduction evidence')
        return answer, first_signature


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise StageError('tlp: bounded integer CLI arguments required', EXIT_CONFIG)
    return value


def run(cfg, args, out):
    """Run standalone bounded consistency checks; this is not query correctness."""
    if cfg is not None:
        raise StageError('tlp: operational configuration is not supported', EXIT_CONFIG)
    oracle = getattr(args, 'oracle', 'tlp')
    if oracle not in ('tlp', 'norec'):
        raise StageError('tlp: oracle must be tlp or norec', EXIT_CONFIG)
    seed = _integer(args.seed, -(2**63), 2**63 - 1)
    maximum = _integer(args.max_examples, 1, 1000)
    timeout = _integer(args.statement_timeout_ms, 1, 120000)
    shrink = getattr(args, 'shrink', False)
    if type(shrink) is not bool:
        raise StageError('tlp: --shrink must be boolean', EXIT_CONFIG)
    shrink_budget = _integer(getattr(args, 'max_shrink_steps', 200), 0, 10000)
    from . import tlp_reduction
    reduction_bindings = tlp_reduction.bindings() if shrink else None
    if not isinstance(args.dsn_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.dsn_env):
        raise StageError('tlp: invalid DSN environment variable name', EXIT_CONFIG)
    spec = load_spec(args.spec)
    if oracle == 'norec' and spec['aggregate'] != {'function': 'count', 'column': '*'}:
        raise StageError('tlp: norec supports only count(*)', EXIT_CONFIG)
    replay = None
    replay_sha = None
    if args.replay:
        try:
            raw_replay = _read(args.replay, REPLAY_LIMIT)
            replay_sha = hashlib.sha256(raw_replay).hexdigest()
            raw_text = raw_replay.decode('utf-8')
            envelope = constraints.decode_json(raw_text)
            reduced_replay = isinstance(envelope, dict) and envelope.get('schema_version') == 2
            if reduced_replay:
                if not shrink:
                    raise StageError('tlp: reduced replay requires --shrink', EXIT_CONFIG)
                replay = tlp_reduction.decode(raw_text, spec, seed, oracle, shrink_budget)
            else:
                replay = decode_replay(raw_text, spec, seed, oracle=oracle)
        except (OSError, UnicodeError, ValueError, TypeError):
            raise StageError('tlp: replay file missing, too large or invalid', EXIT_CONFIG) from None
    directory = Path(out) / 'tlp'
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / 'fixture.json'
    # In-place replay keeps its input until the final changed-file check.
    if not args.replay or Path(args.replay).resolve() != artifact.resolve():
        artifact.unlink(missing_ok=True)
    result = Result('tlp')
    result.inputs = {'spec_sha256': spec['spec_sha256'], 'generator_sha256': _generator_sha(), 'oracle': oracle}
    if replay_sha is not None:
        result.inputs['replay_sha256'] = replay_sha
    if shrink:
        result.inputs['reduction'] = {'bindings': reduction_bindings, 'budget': shrink_budget}
    result.not_proven.extend([
        'A bounded TLP pass proves internal partition consistency only; it proves neither business-query correctness nor operational V1-V5 coverage.',
        'Trusted relational SQL may explicitly reference real tables or custom functions; synthetic CTEs and a read-only transaction are not a SQL sandbox.',
        'Each comparison freezes the relation and predicate and rechecks observable stability; this does not establish function immutability or exclude every source of nondeterminism.',
        'The generated unconstrained fixture families do not reproduce all production data, integrity constraints, collations or PostgreSQL types.'
    ])
    if oracle == 'norec':
        result.not_proven[0] = ('A bounded NoREC pass proves only predicate-count consistency of the tested WHERE and '
                                'CASE forms; it proves neither business-query correctness nor operational V1-V5 coverage.')
        result.not_proven[2] = ('NoREC materializes the relation but independently evaluates the predicate in WHERE and '
                                'CASE, with a separate diagnostic evaluation and a second statement for observable '
                                'stability; this limits optimizer/index coverage and does not prove determinism.')
    if shrink:
        result.not_proven.append('Reduction preserves only signed row-discrepancy support and aggregate relation '
            '(TLP), or count-gap direction (NoREC), not the same optimizer bug. Structured search is bounded; '
            'replay rechecks accepted steps but does not repeat rejected trials or prove deletion minimality.')
    conn = None
    checked = rows = 0
    last = None
    try:
        try:
            conn = db.connect(args.dsn_env)
        except StageError:
            result.add(CHECK_IDS[oracle]['engine'], 'not_run', reason='postgresql_unavailable', oracle=oracle)
            return result
        evaluator = lambda fixture: evaluate_evidence(conn, fixture, spec, timeout, oracle=oracle)
        replay_v2 = replay is not None and replay.get('schema_version') == 2
        indices = [replay['original']['case_index'] if replay_v2 else replay['case_index']] if replay else range(maximum)
        reduction_detail = None
        for index in indices:
            case = replay if replay else make_replay(spec, seed, index, oracle=oracle)
            extra = {'oracle': oracle} if oracle == 'norec' else {}
            if replay_v2:
                answer = tlp_reduction.replay(case, evaluator)
                reduction_detail = tlp_reduction.public(case['search'], replayed=True)
            elif shrink:
                answer, discrepancy = evaluator(case['tables'])
                if not answer['equal']:
                    summary = tlp_reduction.reduce(case['tables'], evaluator, discrepancy, shrink_budget)
                    answer, confirmed = evaluator(summary['tables'])
                    if answer['equal'] or confirmed != discrepancy:
                        raise ValueError('Final reduction witness changed')
                    case = tlp_reduction.build(case, summary, discrepancy, oracle, shrink_budget, reduction_bindings)
                    reduction_detail = tlp_reduction.public(summary)
            else:
                answer = evaluate(conn, case['tables'], spec, timeout, **extra)
            checked += 1
            rows += answer['baseline_rows']
            last = case
            if not answer['equal']:
                break
        # Changed file input cannot retain a pass or produce replay evidence.
        if hashlib.sha256(_read(args.spec, SPEC_LIMIT)).hexdigest() != spec['spec_sha256']:
            raise ValueError('Spec changed during run')
        if _generator_sha() != result.inputs['generator_sha256']:
            raise ValueError('Generator changed during run')
        if shrink and tlp_reduction.bindings() != reduction_bindings:
            raise ValueError('Reduction implementation changed during run')
        if replay_sha is not None and hashlib.sha256(_read(args.replay, REPLAY_LIMIT)).hexdigest() != replay_sha:
            raise ValueError('Replay changed during run')
        artifact.write_text(tlp_reduction.encode(last) if last.get('schema_version') == 2 else
                            json.dumps(last, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        detail = ({'optimized_count': answer['optimized_count'], 'reference_count': answer['reference_count']}
                  if oracle == 'norec' else {'multiset_equal': answer['multiset_equal'],
                                            'aggregate_equal': answer['aggregate_equal']})
        if reduction_detail is not None:
            detail['reduction'] = reduction_detail
        result.add(CHECK_IDS[oracle]['consistency'], 'pass' if answer['equal'] else 'fail', bounded=True, oracle=oracle,
                   examples=checked, relation_rows=rows,
                   case_index=last['original']['case_index'] if last.get('schema_version') == 2 else last['case_index'],
                   fixture_sha256=last['tables_sha256'], spec_sha256=spec['spec_sha256'], **detail)
        result.next_safe_action = ('Review bounded synthetic coverage and trusted SQL before treating this as evidence '
                                   f'beyond {"predicate-count" if oracle == "norec" else "partition"} consistency.')
        return result
    except Exception as error:
        artifact.unlink(missing_ok=True)
        detail = error.args[0] if getattr(error, 'args', None) else None
        code = detail.get('C', '') if isinstance(detail, dict) else ''
        result.add(CHECK_IDS[oracle]['execution'], 'error', reason='invalid_or_unstable_execution', examples=checked, oracle=oracle,
                   sqlstate=code if isinstance(code, str) and re.fullmatch(r'[A-Z0-9]{5}', code) else '')
        return result
    finally:
        if conn is not None:
            conn.close()
