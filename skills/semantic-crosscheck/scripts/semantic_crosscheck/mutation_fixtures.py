"""Bounded operator-directed SQL fixtures, separate from real-data V4 mutation verdicts.

The nine templates target the fixed counterexample four-table schema. They do
not infer a configuration, compile a model, run Cube, or prove that supplied
SQL implements the named mutation. Detection requires an actual successful
SQL bag comparison. A separately supplied gold SQL observes a distinction
only when original == gold and mutant != gold in the same read-only snapshot.
No generated values or SQL answer rows enter Result; only fixture.json holds
generated input rows. Trusted SQL can still explicitly name source relations.
"""
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import re

from . import constraints, counterexample as ce, db, mutate
from .result import EXIT_CONFIG, Result, StageError

FORMAT = 'sxc-synthetic-mutation'
GENERATOR_VERSION = 1
SCHEMA_VERSION = 1
SUPPORTED_OPERATORS = mutate.OPERATORS
FILE_LIMIT = 1024 * 1024


def generate(operator, seed, case_index):
    """Small positive-valued templates; seed/case change magnitudes, never the signal.

    Closure templates have a diamond with unequal positive path products.
    Filter templates use FIRM/excluded rows or exact/case/space variants.
    Aggregation has repeated positive values; date rows straddle fixed January
    2026 boundaries. Join/end templates have asymmetric node and stock values.
    Other predicates, dates, identifiers and schemas can be outside this bound.
    """
    if operator not in SUPPORTED_OPERATORS:
        raise StageError('counterexample: unsupported mutation fixture operator', EXIT_CONFIG)
    ce._int(seed, -(2**63), 2**63 - 1, 'seed')
    ce._int(case_index, 0, 999, 'case_index')
    n = 2 + (seed + case_index) % 7
    fixture = {table: [] for table in ce.SCHEMA}
    fixture['part'] = [[name, kind, 'unit', 'make', 'synthetic_' + name]
                       for name, kind in [('A', 'root'), ('B', 'leaf'), ('C', 'branch'), ('D', 'leaf')]]

    def order(identifier, status, quantity, date='2026-01-01'):
        return [identifier, 'A', status, str(quantity), str(quantity), date]

    if operator in ('closure_depth_shift', 'closure_path_count_one', 'closure_qty_single_path'):
        fixture['bom_line'] = [['A', 'B', 1, str(n)], ['B', 'D', 1, str(n + 1)],
                               ['A', 'C', 2, str(n + 3)], ['C', 'D', 1, str(n + 5)]]
    elif operator == 'measure_filter_drop':
        fixture['sales_order'] = [order('o1', 'FIRM', n), order('o2', 'CANCELLED', n + 1)]
    elif operator == 'measure_normalization_drop':
        fixture['sales_order'] = [order('o1', 'FIRM', n), order('o2', ' firm ', n + 1), order('o3', 'Firm', n + 3)]
    elif operator == 'measure_agg_to_max':
        fixture['stock'] = [['A', 'L0', str(n)], ['A', 'L1', str(n + 1)]]
    elif operator == 'dated_shift_one_day':
        fixture['sales_order'] = [order('o1', 'FIRM', n, '2026-01-01'), order('o2', 'FIRM', n + 1, '2026-01-31'),
                                  order('o3', 'FIRM', n + 3, '2026-02-01'), order('o4', 'FIRM', n + 5, '2025-12-31')]
    else:  # Both closure joins are directed from A to a different node B.
        fixture['bom_line'] = [['A', 'B', 1, str(n)]]
        fixture['stock'] = [['A', 'L0', str(n)], ['B', 'L0', str(n + 7)]]
    return fixture


def evaluate(conn, fixture, original_sql, mutant_sql, gold_sql, timeout_ms, profile=None):
    """Run the two or three queries in one read-only repeatable-read transaction."""
    answers = []
    with db.read_only(conn, timeout_ms, isolation='repeatable read'):
        for query in (original_sql, mutant_sql) + ((gold_sql,) if gold_sql is not None else ()):
            wrapped, params = ce.wrap_sql(query, fixture, profile=profile)
            prepared = conn.prepare(wrapped)
            try:
                rows = prepared.run(**params) or []
                columns = [column['name'] for column in (prepared.columns or [])]
                if len(rows) > ce.ROW_LIMIT:
                    raise ValueError('SQL result row limit exceeded')
                answers.append((columns, rows))
            finally:
                prepared.close()
    comparisons = {'original_mutant': ce.compare_answers(*answers[0], *answers[1])}
    if gold_sql is not None:
        comparisons['original_gold'] = ce.compare_answers(*answers[0], *answers[2])
        comparisons['mutant_gold'] = ce.compare_answers(*answers[1], *answers[2])
    return comparisons


def replay(raw, operator, seed, digests, profile=None):
    """Accept only exact-version, SQL/operator/profile-bound generated row deletions."""
    try:
        payload = constraints.decode_json(raw)
        if (payload['format'] != FORMAT or type(payload['schema_version']) is not int
                or payload['schema_version'] != SCHEMA_VERSION or type(payload['generator_version']) is not int
                or payload['generator_version'] != GENERATOR_VERSION or payload['synthetic'] is not True
                or payload['mutation_operator'] != operator or type(payload['seed']) is not int
                or payload['seed'] != seed or payload['sql_sha256'] != digests):
            raise ValueError('Replay contract')
        binding = payload['constraints']
        if not isinstance(binding, dict) or binding != constraints.binding(profile) or type(binding.get('version')) is not int:
            raise ValueError('Replay constraint binding')
        generated = generate(operator, seed, payload['case_index'])
        tables = payload['tables']
        constraints.validate_fixture(tables, ce.SCHEMA, profile)
        for table in ce.SCHEMA:
            available = Counter(json.dumps(row, sort_keys=True) for row in generated[table])
            requested = Counter(json.dumps(row, sort_keys=True) for row in tables[table])
            if requested - available:
                raise ValueError('Replay contains non-generated rows')
        return payload
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, StageError):
        raise StageError('counterexample: replay is not a matching operator-directed row-deletion fixture', EXIT_CONFIG) from None


def _observes(answer):
    return ('original_gold' in answer and answer['original_gold']['equal']
            and not answer['mutant_gold']['equal'] and not answer['original_mutant']['equal'])


def _observation(answer):
    if 'original_gold' not in answer:
        return 'not_proven'
    if not answer['original_gold']['equal']:
        return 'original_gold_disagree'
    return 'observed' if _observes(answer) else 'not_observed'


def run(cfg, args, out):
    """Search, validate, shrink and replay synthetic SQL distinctions, never V4 kills."""
    operator = args.mutation_operator
    result = Result('counterexample')
    directory = Path(out) / 'counterexample'
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / 'fixture.json'
    if operator not in SUPPORTED_OPERATORS:
        artifact_path.unlink(missing_ok=True)
        result.add('counterexample:mutation:search', 'not_run', '', reason='unsupported_operator',
                   synthetic_only=True, synthetic_distinguished=False, gold_observes=False)
        result.next_safe_action = 'Choose a supported fixed-schema mutation fixture operator.'
        return result
    if getattr(args, 'metamorphic_sql', None) is not None or getattr(args, 'scenario', None) is not None:
        raise StageError('counterexample: mutation fixtures cannot combine with --scenario or --metamorphic-sql', EXIT_CONFIG)
    if getattr(args, 'reference_sql', None) is None or getattr(args, 'candidate_sql', None) is None:
        raise StageError('counterexample: mutation fixtures require original --reference-sql and mutant --candidate-sql', EXIT_CONFIG)
    seed = ce._int(args.seed, -(2**63), 2**63 - 1, 'seed')
    maximum = ce._int(args.max_examples, 1, 1000, 'max_examples')
    budget = ce._int(args.max_shrink_steps, 0, 10000, 'max_shrink_steps')
    timeout = ce._int(getattr(args, 'statement_timeout_ms', 10000), 1000, 120000, 'statement_timeout_ms')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.dsn_env):
        raise StageError('counterexample: invalid DSN environment variable name', EXIT_CONFIG)
    profile_path = getattr(args, 'constraints', None)
    profile = constraints.load(profile_path, ce.SCHEMA) if profile_path is not None else None
    paths = {'reference': args.reference_sql, 'candidate': args.candidate_sql}
    if getattr(args, 'gold_sql', None) is not None:
        paths['gold'] = args.gold_sql
    sql, digests = {}, {}
    for name, path in paths.items():
        sql[name], digests[name] = ce._read_sql(path)
    replayed = None
    if args.replay:
        try:
            replay_path = Path(args.replay)
            if replay_path.stat().st_size > FILE_LIMIT:
                raise ValueError('Replay size')
            replayed = replay(replay_path.read_text(encoding='utf-8'), operator, seed, digests, profile)
        except (OSError, UnicodeError, ValueError):
            raise StageError('counterexample: replay file missing, too large or invalid', EXIT_CONFIG) from None
    # --replay commonly names this same output. Bind and validate its bytes before
    # removing stale output; otherwise the command deletes its own input.
    artifact_path.unlink(missing_ok=True)
    result.not_proven.extend([
        'Synthetic SQL coverage only: these fixtures do not run Cube or change the real-data V4 verdict.',
        'Caller supplies the original/mutant SQL; correspondence to a compiled model and the named mutation is not verified.',
        'Only the fixed four-table schema, generated identifiers/statuses and January 2026 dates are searched; no disagreement is not equivalence.',
        'Trusted SQL can explicitly reference source relations; fixture CTEs are not a SQL sandbox or proof of source independence.',
        ('Only the exact bound integrity profile is enforced; text uses C collation and production domains are not inferred.' if profile else
         'Fixture scope is unconstrained; production integrity rules are not inferred.'),
    ])
    if 'gold' not in sql:
        result.not_proven.append('Gold observation is not proven: no independently authored gold SQL was supplied.')
    counters = dict(attempted_examples=0, examples=0, invalid_domain_examples=0, query_error_examples=0,
                    distinguished_examples=0, gold_observed_examples=0, original_gold_disagreement_examples=0)
    base = dict(mutation_operator=operator, generator_version=GENERATOR_VERSION, synthetic_only=True,
                coverage_scope='operator_directed_synthetic_sql', constraints_mode=constraints.binding(profile)['mode'])
    conn = None
    try:
        found = None
        for index in ([replayed['case_index']] if replayed else range(maximum)):
            fixture = copy.deepcopy(replayed['tables']) if replayed else generate(operator, seed, index)
            counters['attempted_examples'] += 1
            try:
                constraints.validate_fixture(fixture, ce.SCHEMA, profile)
            except constraints.ConstraintViolation:
                counters['invalid_domain_examples'] += 1
                continue
            if conn is None:
                conn = db.connect(args.dsn_env)
            answer = evaluate(conn, fixture, sql['reference'], sql['candidate'], sql.get('gold'), timeout, profile)
            counters['examples'] += 1
            if 'original_gold' in answer and not answer['original_gold']['equal']:
                counters['original_gold_disagreement_examples'] += 1
            if not answer['original_mutant']['equal']:
                found = index, fixture, answer
                break
        if found is None:
            result.add('counterexample:mutation:search', 'not_run', '', **base, **counters,
                       synthetic_distinguished=False, gold_observes=False, gold_observation='not_observed' if 'gold' in sql else 'not_proven',
                       reason='no_admissible_generated_examples' if counters['examples'] == 0 else 'no_disagreement_in_bounded_search')
            if counters['original_gold_disagreement_examples']:
                result.add('counterexample:mutation:gold_agreement', 'fail', '',
                           disagreement_examples=counters['original_gold_disagreement_examples'], synthetic_only=True)
            result.next_safe_action = 'Review the fixed-schema templates and supplied SQL; bounded non-detection does not prove equivalence.'
            return result
        index, fixture, initial = found
        expected_observation = _observation(initial)

        def predicate(trial):
            answer = evaluate(conn, trial, sql['reference'], sql['candidate'], sql.get('gold'), timeout, profile)
            return not answer['original_mutant']['equal'] and _observation(answer) == expected_observation

        reduced = ce.shrink(fixture, predicate, budget, profile=profile)
        constraints.validate_fixture(reduced['tables'], ce.SCHEMA, profile)
        final = evaluate(conn, reduced['tables'], sql['reference'], sql['candidate'], sql.get('gold'), timeout, profile)
        if final['original_mutant']['equal'] or _observation(final) != expected_observation:
            result.add('counterexample:mutation:recheck', 'error', '', **base, reason='witness_not_stable',
                       synthetic_distinguished=False, gold_observes=False)
            return result
        if any(ce._read_sql(paths[name])[1] != digests[name] for name in paths):
            result.add('counterexample:mutation:inputs', 'error', '', reason='sql_changed_during_run')
            return result
        if profile is not None and not profile.unchanged():
            result.add('counterexample:mutation:inputs', 'error', '', reason='constraint_profile_changed_during_run')
            return result
        payload = {'format': FORMAT, 'schema_version': SCHEMA_VERSION, 'generator_version': GENERATOR_VERSION,
                   'synthetic': True, 'mutation_operator': operator, 'seed': seed, 'case_index': index,
                   'sql_sha256': digests, 'constraints': constraints.binding(profile), 'tables': reduced['tables'],
                   'shrink': {key: value for key, value in reduced.items() if key != 'tables'},
                   'scope': 'Operator-directed synthetic SQL distinction only; no Cube runtime/V4 verdict or general equivalence proof.'}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        if len(serialized) > FILE_LIMIT:
            raise ValueError('Fixture file size')
        temporary = directory / 'fixture.json.part'
        temporary.write_bytes(serialized)
        os.replace(temporary, artifact_path)
        counters['distinguished_examples'] = 1
        counters['gold_observed_examples'] = int(_observes(final))
        result.add('counterexample:mutation:search', 'fail', '', **base, **counters, synthetic_distinguished=True,
                   gold_observes=_observes(final), gold_observation=_observation(final), case_index=index,
                   constraint_profile_sha256=profile.sha256 if profile else None, original_rows=ce._rows(fixture),
                   minimized_rows=ce._rows(reduced['tables']), shrink_steps=reduced['steps'],
                   shrink_errors=reduced['shrink_errors'], shrink_invalid_domain_trials=reduced['invalid_domain_trials'],
                   minimality_scope=reduced['minimality_scope'], deletion_1_minimal=reduced['deletion_1_minimal'],
                   budget_exhausted=reduced['budget_exhausted'], fixture_sha256=hashlib.sha256(serialized).hexdigest(),
                   **{key: value for key, value in final['original_mutant'].items() if key != 'equal'})
        if 'original_gold' in final:
            result.add('counterexample:mutation:gold_agreement', 'pass' if final['original_gold']['equal'] else 'fail', '',
                       synthetic_only=True, **{key: value for key, value in final['original_gold'].items() if key != 'equal'})
        if reduced['budget_exhausted'] or reduced['shrink_errors']:
            result.not_proven.append('Deletion 1-minimality is not proven when shrinking exhausts its budget or a trial query errors.')
        result.not_proven.append('Deletion 1-minimality covers only admissible single-row deletions, never a global minimum.')
        result.next_safe_action = 'Exit 1 records a synthetic SQL distinction; replay the same bound inputs and review gold agreement separately from real-data V4.'
    except StageError:
        raise
    except Exception as error:
        counters['query_error_examples'] += 1
        arg = error.args[0] if error.args else None
        code = arg.get('C', '') if isinstance(arg, dict) else ''
        unsupported = code in ('42P01', '42703')
        result.add('counterexample:mutation:query', 'not_run' if unsupported else 'error', '', **base, **counters,
                   reason='unsupported_sql_schema' if unsupported else 'query_execution_error', synthetic_distinguished=False,
                   gold_observes=False, exception=type(error).__name__,
                   sqlstate=code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else '')
        result.next_safe_action = ('Adapt the trusted SQL to the fixed four-table fixture schema; no fixture was saved.' if unsupported else
                                   'Fix the query or environment; errors are not synthetic detections and no fixture was saved.')
    finally:
        if conn is not None:
            conn.close()
    return result
