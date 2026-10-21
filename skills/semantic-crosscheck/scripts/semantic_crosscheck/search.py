"""Opt-in typed bounded search and trace-validated structured replay.

The solver supplies integrity-valid synthetic inputs, not SQL semantics or an
equivalence proof. Actual PostgreSQL disagreement remains the witness predicate.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re

from . import constraints, counterexample as ce
from .result import EXIT_CONFIG, Result, StageError


def _bad():
    return StageError('counterexample: invalid or mismatched extended search inputs', EXIT_CONFIG)


def _load_replay(path, scenario, seed, digests, profile, spec, strategy, schema):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Replay size')
        payload = constraints.decode_json(raw.decode('utf-8'))
        from . import witness
        generator = witness.binding(spec, profile) if spec else {'mode': 'scenario', 'version': ce.VERSION}
        if (payload['format'] != ce.FORMAT or type(payload['schema_version']) is not int or payload['schema_version'] != 2
                or payload['synthetic'] is not True or type(payload['generator_version']) is not int or payload['generator_version'] != 1
                or payload['scenario'] != scenario or type(payload['seed']) is not int or payload['seed'] != seed
                or payload['sql_sha256'] != digests or payload['constraints'] != constraints.binding(profile)
                or payload['generator'] != generator or type(payload['generator']['version']) is not int
                or type(payload['constraints']['version']) is not int or payload['shrink_strategy'] != strategy):
            raise ValueError('Replay binding')
        index = ce._int(payload['case_index'], 0, 999, 'case_index')
        original = payload['original_tables']
        if spec:
            witness.validate_fixture(spec, original, profile)
        elif original != ce.generate(scenario, seed, index):
            raise ValueError('Original scenario')
        constraints.validate_fixture(payload['tables'], schema, profile)
        if spec:
            witness.validate_fixture(spec, payload['tables'], profile)
        if strategy == 'structured':
            from . import reduction
            replayed = reduction.replay_trace(original, payload['shrink']['trace'], schema=schema, profile=profile,
                                              admissible=(lambda f: witness.validate_fixture(spec, f, profile)) if spec else None)
            if replayed != payload['tables']:
                raise ValueError('Trace result')
        else:
            for table in schema:
                available = Counter(json.dumps(row, sort_keys=True) for row in original[table])
                wanted = Counter(json.dumps(row, sort_keys=True) for row in payload['tables'][table])
                if wanted - available:
                    raise ValueError('Not a deletion')
        return payload
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise _bad() from None


def run(args, out):
    if any(getattr(args, name, None) is not None for name in ('metamorphic_sql', 'mutation_operator', 'gold_sql')):
        raise StageError('counterexample: witness schema and structured shrinking require the two-query search mode', EXIT_CONFIG)
    if args.reference_sql is None or args.candidate_sql is None:
        raise StageError('counterexample: pass both trusted SQL files for extended search', EXIT_CONFIG)
    seed = ce._int(args.seed, -(2**63), 2**63 - 1, 'seed')
    maximum = ce._int(args.max_examples, 1, 1000, 'max_examples')
    budget = ce._int(args.max_shrink_steps, 0, 10000, 'max_shrink_steps')
    timeout = ce._int(getattr(args, 'statement_timeout_ms', 10000), 1000, 120000, 'statement_timeout_ms')
    solver_timeout = ce._int(getattr(args, 'solver_timeout_ms', 3000), 1, 60000, 'solver_timeout_ms')
    strategy = getattr(args, 'shrink_strategy', 'rows')
    if strategy not in ('rows', 'structured') or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.dsn_env):
        raise _bad()
    from . import witness
    spec_path = getattr(args, 'witness_schema', None)
    spec = witness.load(spec_path) if spec_path is not None else None
    if spec is None and solver_timeout != 3000:
        raise StageError('counterexample: --solver-timeout-ms requires --witness-schema', EXIT_CONFIG)
    if spec and getattr(args, 'scenario', None) is not None:
        raise StageError('counterexample: --witness-schema replaces --scenario', EXIT_CONFIG)
    scenario = 'constraint_witness' if spec else (args.scenario or 'nulls')
    if not spec and scenario not in ce.SCENARIOS:
        raise _bad()
    schema = spec.schema if spec else ce.SCHEMA
    profile_path = getattr(args, 'constraints', None)
    profile = constraints.load(profile_path, schema) if profile_path is not None else None
    reference, reference_sha = ce._read_sql(args.reference_sql)
    candidate, candidate_sha = ce._read_sql(args.candidate_sql)
    digests = {'reference': reference_sha, 'candidate': candidate_sha}
    replay = _load_replay(args.replay, scenario, seed, digests, profile, spec, strategy, schema) if args.replay else None
    directory = Path(out) / 'counterexample'
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / 'fixture.json'
    artifact.unlink(missing_ok=True)
    result = Result('counterexample')
    result.not_proven.append('Trusted SQL can explicitly read real relations; synthetic CTEs are not a SQL sandbox.')
    result.not_proven.append('Only declared finite domains, row bounds and integrity rules are searched; SQL query conditions are not encoded in Z3. No SQL equivalence is proved.')
    if spec is not None or profile is not None:
        result.not_proven.append('Declared text uses exact equality and C collation; unsupported types, collation and arbitrary PostgreSQL DDL are outside this scope.')
    else:
        result.not_proven.append('Unconstrained built-in scenarios retain database-default text collation; no declared text integrity domain is enforced.')
    if profile is None:
        result.not_proven.append('No integrity profile was declared; keys, foreign keys and CHECK rules are not inferred.')
    generation = None
    if replay:
        cases = [(replay['case_index'], replay['tables'])]
    elif spec:
        generation = witness.generate(spec, profile, max_examples=maximum, timeout_ms=solver_timeout)
        cases = enumerate(generation.fixtures)
    else:
        cases = ((index, ce.generate(scenario, seed, index)) for index in range(maximum))
    if generation:
        result.not_proven.append('Constraint witness generation is bounded; an exhausted, unsatisfiable or interrupted domain is not a general absence proof.')
    checked = attempted = invalid = 0
    conn = None
    def evaluate(tables):
        if spec is None:
            return ce.evaluate(conn, tables, reference, candidate, timeout, profile)
        return ce.evaluate(conn, tables, reference, candidate, timeout, profile, schema=schema)
    def admissible(tables):
        if spec is not None:
            witness.validate_fixture(spec, tables, profile)
    try:
        found = None
        for index, tables in cases:
            attempted += 1
            try:
                constraints.validate_fixture(tables, schema, profile)
            except constraints.ConstraintViolation:
                invalid += 1
                continue
            if conn is None:
                conn = ce.db.connect(args.dsn_env)
            answer = evaluate(tables)
            checked += 1
            if not answer['equal']:
                found = index, tables
                break
        if found is None:
            result.add('counterexample:search', 'not_run', '', found=False, bounded=True, examples=checked,
                       attempted_examples=attempted, invalid_domain_examples=invalid,
                       reason=generation.reason if generation else 'no_disagreement_in_bounded_search',
                       solver_checks=generation.solver_checks if generation else 0)
            result.not_proven.append('No disagreement found proves neither equivalence nor correctness on other inputs.')
            result.next_safe_action = 'Review the supported domain, exact SQL and resource bounds.'
            return result
        index, tables = found
        original = replay['original_tables'] if replay else tables
        # Recompute all claims. Saved shrink statistics are untrusted metadata.
        prior_trace = replay['shrink']['trace'] if replay and strategy == 'structured' else []
        reduced = ce.shrink(tables, lambda trial: not evaluate(trial)['equal'], min(budget, 10000 - len(prior_trace)),
                            profile=profile, schema=schema, strategy=strategy, admissible=admissible if spec else None)
        if prior_trace:
            reduced['trace'] = prior_trace + reduced['trace']
        constraints.validate_fixture(reduced['tables'], schema, profile)
        admissible(reduced['tables'])
        final = evaluate(reduced['tables'])
        if final['equal']:
            result.add('counterexample:recheck', 'error', '', reason='witness_not_stable')
            return result
        changed = (ce._read_sql(args.reference_sql)[1] != reference_sha or ce._read_sql(args.candidate_sql)[1] != candidate_sha
                   or (profile is not None and not profile.unchanged()) or (spec is not None and not spec.unchanged()))
        if changed:
            result.add('counterexample:inputs', 'error', '', reason='inputs_changed_during_run')
            return result
        payload = {'format': ce.FORMAT, 'schema_version': 2, 'synthetic': True, 'generator_version': 1,
                   'scenario': scenario, 'seed': seed, 'case_index': index, 'sql_sha256': digests,
                   'constraints': constraints.binding(profile), 'shrink_strategy': strategy,
                   'generator': witness.binding(spec, profile) if spec else {'mode': 'scenario', 'version': ce.VERSION},
                   'original_tables': original, 'tables': reduced['tables'],
                   'shrink': {key: value for key, value in reduced.items() if key != 'tables'},
                   'scope': 'Synthetic bounded witness; structured transformations are trace-validated. No global minimum or general equivalence proof.'}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        if len(raw) > 1024 * 1024:
            result.add('counterexample:artifact', 'error', '', reason='replay_size_limit')
            return result
        temporary = directory / 'fixture.json.part'
        temporary.write_bytes(raw)
        os.replace(temporary, artifact)
        result.add('counterexample:search', 'fail', '', found=True, bounded=True, examples=checked,
                   attempted_examples=attempted, invalid_domain_examples=invalid,
                   original_rows=ce._rows(original), minimized_rows=ce._rows(reduced['tables']),
                   shrink_strategy=strategy, shrink_steps=reduced['steps'], shrink_errors=reduced['shrink_errors'],
                   shrink_invalid_domain_trials=reduced['invalid_domain_trials'],
                   minimality_scope=reduced['minimality_scope'], deletion_1_minimal=reduced['deletion_1_minimal'],
                   budget_exhausted=reduced['budget_exhausted'], operation_counts=reduced.get('operation_counts', {}),
                   generator_reason=generation.reason if generation else 'replay' if replay else 'scenario',
                   spec_sha256=spec.sha256 if spec else None, fixture_sha256=hashlib.sha256(raw).hexdigest(),
                   **{key: value for key, value in final.items() if key != 'equal'})
        result.not_proven.append('Only admissible single-row deletion minimality may be established; no global or all-transformation minimum is claimed.')
        if reduced['budget_exhausted'] or reduced['shrink_errors']:
            result.not_proven.append('An exhausted shrink budget or query errors leave deletion minimality unproven.')
        result.next_safe_action = 'Replay with the exact SQL, schema, profile and shrink strategy, then review the disagreement.'
    except StageError:
        raise
    except Exception as error:
        result.add('counterexample:query', 'error', '', exception=type(error).__name__, examples=checked)
        result.next_safe_action = 'Review the query or environment; errors are not counterexamples.'
    finally:
        if conn is not None:
            conn.close()
    return result
