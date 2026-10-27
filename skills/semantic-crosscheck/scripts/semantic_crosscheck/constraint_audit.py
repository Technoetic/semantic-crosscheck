"""Opt-in, source-declaration-bound constraint sensitivity diagnostics.

Z3 generates finite integrity-valid inputs. Only an actual PostgreSQL result
disagreement is evidence. Relaxed evidence must remain outside the full base
profile, including during row deletion and replay. No ordinary replay changes.
"""
from collections import Counter
import copy
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

from . import constraints, counterexample as ce, witness
from .result import EXIT_CONFIG, Result, StageError

FORMAT = 'sxc-synthetic-constraint-audit'
VERSION = 1
REPLAY_LIMIT = 1024 * 1024
SOURCES = ('catalog', 'human', 'observed', 'inferred')
SCOPE = 'Bounded conditional diagnostic; admissible row deletions preserve finite values and row bounds. No equivalence or global minimum proof.'


def _bad():
    return StageError('counterexample: invalid or mismatched constraint audit inputs; version 1 requires finite witness schema, constraints, two trusted SQL files and rows shrinking', EXIT_CONFIG)


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)  # Exact CHECK literals remain accepted decimal strings.
    raise TypeError('Unsupported audit JSON value')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      default=_json_default).encode('utf-8')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, limit):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError('Input size')
    return raw


def _write(path, raw):
    temporary = path.with_name(path.name + '.part')
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def validate_output_paths(args, out):
    """Reject aliases before either CLI result cleanup or audit artifact mutation."""
    try:
        directory = Path(out) / 'counterexample'
        generated = [directory / name for name in ('fixture.json', 'fixture.json.part', 'result.json', 'result.json.part')]
        generated += [directory / 'audit' / (name + suffix)
                      for name in ('declared-fixture.json', 'relaxed-fixture.json', 'relaxed-constraints.json')
                      for suffix in ('', '.part')]
        protected = [Path(value).resolve() for name in ('constraints', 'constraint_audit', 'witness_schema', 'reference_sql', 'candidate_sql',
                                                     'config', 'gold_sql', 'metamorphic_sql')
                     if (value := getattr(args, name, None)) is not None]
        if any(path.resolve() in protected for path in generated):
            raise ValueError('Output aliases input')
    except (OSError, ValueError, TypeError):
        raise _bad() from None


def prepare_run(args, out):
    """Capture same-output replay, then remove stale evidence before preflight."""
    try:
        validate_output_paths(args, out)
        replay_raw = None
        if args.replay:
            try:
                replay_raw = _read(args.replay, REPLAY_LIMIT)
            except (OSError, ValueError, TypeError):
                pass  # Later validation rejects it; stale evidence is still removed.
        directory = Path(out) / 'counterexample'
        for path in [directory / 'fixture.json', directory / 'fixture.json.part', directory / 'result.json', directory / 'result.json.part',
                     directory / 'audit/declared-fixture.json', directory / 'audit/relaxed-fixture.json',
                     directory / 'audit/declared-fixture.json.part', directory / 'audit/relaxed-fixture.json.part']:
            path.unlink(missing_ok=True)
        return replay_raw
    except (StageError, OSError, ValueError, TypeError):
        raise _bad() from None


def _catalog(document):
    """Canonical addresses resolve only real declarations in the original v1 file."""
    entries = {}
    for table, rules in document['tables'].items():
        escaped = table.replace('~', '~0').replace('/', '~1')
        prefix = '/tables/' + escaped + '/'
        if 'primary_key' in rules:
            entries[prefix + 'primary_key'] = ('table', table, 'primary_key', None)
        for name in ('unique', 'not_null'):
            for index in range(len(rules.get(name, []))):
                entries[prefix + name + '/' + str(index)] = ('table', table, name, index)
    for name in ('foreign_keys', 'checks'):
        for index in range(len(document.get(name, []))):
            entries['/' + name + '/' + str(index)] = ('root', None, name, index)
    if len(entries) > constraints.RULE_LIMIT:
        raise ValueError('Source rule bound')
    return entries


@dataclass(frozen=True)
class Plan:
    original: object
    relaxed: object
    manifest_path: Path
    manifest_sha256: str
    binding: dict
    summary: dict

    def unchanged(self):
        try:
            return (self.original.unchanged() and self.relaxed.unchanged()
                    and _digest(_read(self.manifest_path, constraints.FILE_LIMIT)) == self.manifest_sha256)
        except (OSError, ValueError):
            return False


def _plan(args, spec, directory):
    original = constraints.load(args.constraints, spec.schema)
    base_raw = _read(args.constraints, constraints.FILE_LIMIT)
    if _digest(base_raw) != original.sha256:
        raise ValueError('Base changed')
    document = constraints.decode_json(base_raw.decode('utf-8-sig'))
    catalog = _catalog(document)
    manifest_path = Path(args.constraint_audit)
    raw = _read(manifest_path, constraints.FILE_LIMIT)
    manifest = constraints.decode_json(raw.decode('utf-8-sig'))
    constraints._fields(manifest, {'version', 'constraints_sha256', 'provenance', 'relax'},
                        {'version', 'constraints_sha256', 'provenance', 'relax'})
    if type(manifest['version']) is not int or manifest['version'] != VERSION or manifest['constraints_sha256'] != original.sha256:
        raise ValueError('Manifest binding')
    provenance = {}
    for entry in constraints._list(manifest['provenance']):
        constraints._fields(entry, {'rule', 'source'}, {'rule', 'source'})
        rule, source = entry['rule'], entry['source']
        if not isinstance(rule, str) or rule not in catalog or rule in provenance or source not in SOURCES:
            raise ValueError('Provenance declaration')
        provenance[rule] = source
    if set(provenance) != set(catalog):
        raise ValueError('Exhaustive source provenance')
    selected = constraints._list(manifest['relax'], nonempty=True)
    if any(not isinstance(rule, str) or rule not in catalog for rule in selected) or len(set(selected)) != len(selected):
        raise ValueError('Selected declaration')
    projected = copy.deepcopy(document)
    # Rebuild lists from ORIGINAL indices. Removing one selector cannot shift another.
    for rule, (location, table, name, index) in catalog.items():
        if rule in selected and index is None:
            del projected['tables'][table][name]
    for table, rules in document['tables'].items():
        escaped = table.replace('~', '~0').replace('/', '~1')
        for name in ('unique', 'not_null'):
            if name in rules:
                projected['tables'][table][name] = [value for index, value in enumerate(rules[name])
                    if '/tables/' + escaped + '/' + name + '/' + str(index) not in selected]
    for name in ('foreign_keys', 'checks'):
        if name in document:
            projected[name] = [value for index, value in enumerate(document[name]) if '/' + name + '/' + str(index) not in selected]
    projection_path = directory / 'relaxed-constraints.json'
    # Protect caller inputs from accidental output/input aliases before mutation.
    protected = (args.constraints, args.constraint_audit, args.witness_schema, args.reference_sql, args.candidate_sql, args.replay)
    if any(value is not None and Path(value).resolve() == projection_path.resolve() for value in protected):
        raise ValueError('Output aliases input')
    projection_raw = _canonical(projected)
    _write(projection_path, projection_raw)
    relaxed = constraints.load(projection_path, spec.schema)  # includes retained FK target dependency validation
    deltas = {}
    for name in ('not_null', 'unique', 'foreign_keys', 'checks'):
        before, after = set(getattr(original, name)), set(getattr(relaxed, name))
        if not after <= before:
            raise ValueError('Projection widened declarations unexpectedly')
        deltas[name] = len(before - after)
    if not any(deltas.values()):
        raise ValueError('No effective relaxation')
    ids = {rule: f'r{index:04d}' for index, rule in enumerate(sorted(catalog), 1)}
    binding = {'version': VERSION, 'manifest_sha256': _digest(raw), 'original_constraints_sha256': original.sha256,
               'projected_constraints_sha256': relaxed.sha256, 'selected_declarations': sorted(selected),
               'selected_rule_ids': sorted(ids[rule] for rule in selected),
               'source_catalog_sha256': _digest(_canonical(sorted(catalog)))}
    summary = {key: value for key, value in binding.items() if key != 'selected_declarations'}
    summary.update(selected_rule_count=len(selected), source_rule_count=len(catalog), normalized_rule_deltas=deltas,
                   provenance_counts={source: list(provenance.values()).count(source) for source in SOURCES})
    return Plan(original, relaxed, manifest_path.resolve(), _digest(raw), binding, summary)


def _outside(tables, schema, original):
    try:
        constraints.validate_fixture(tables, schema, original)
    except constraints.ConstraintViolation:
        return True
    return False


def _admissible(spec, profile, original, arm, tables):
    witness.validate_fixture(spec, tables, profile)
    if arm == 'relaxed' and not _outside(tables, spec.schema, original):
        raise constraints.ConstraintViolation('Audit diagnostic must remain outside the declared domain')


def _load_replay(raw, plan, spec, seed, digests, bounds):
    payload = constraints.decode_json(raw.decode('utf-8'))
    required = {'format', 'schema_version', 'synthetic', 'generator_version', 'arm', 'classification', 'scenario',
                'seed', 'case_index', 'sql_sha256', 'audit', 'generator', 'constraints', 'bounds', 'shrink_strategy',
                'original_tables', 'tables', 'shrink', 'scope'}
    constraints._fields(payload, required, required)
    arm = payload['arm']
    if arm not in ('declared', 'relaxed'):
        raise ValueError('Arm')
    profile = plan.original if arm == 'declared' else plan.relaxed
    fixed = {'format': FORMAT, 'schema_version': VERSION, 'synthetic': True, 'generator_version': VERSION,
             'scenario': 'constraint_witness', 'seed': seed, 'sql_sha256': digests, 'audit': plan.binding,
             'generator': witness.binding(spec, profile), 'constraints': constraints.binding(profile),
             'bounds': bounds, 'shrink_strategy': 'rows', 'scope': SCOPE,
             'classification': 'declared_domain_disagreement' if arm == 'declared' else 'outside_declared_domain'}
    for key, expected in fixed.items():
        if _canonical(payload[key]) != _canonical(expected):
            raise ValueError('Replay binding')
    ce._int(payload['case_index'], 0, bounds['max_examples'] - 1, 'case_index')
    for name in ('original_tables', 'tables'):
        _admissible(spec, profile, plan.original, arm, payload[name])
    for table in spec.schema:
        available = Counter(_canonical(row) for row in payload['original_tables'][table])
        wanted = Counter(_canonical(row) for row in payload['tables'][table])
        if wanted - available:
            raise ValueError('Replay is not row deletion')
    shrink = payload['shrink']
    fields = {'steps', 'accepted', 'shrink_errors', 'invalid_domain_trials', 'minimality_scope', 'deletion_1_minimal', 'budget_exhausted'}
    constraints._fields(shrink, fields, fields)  # no unvalidated structured trace in v1
    for key in ('steps', 'accepted', 'shrink_errors', 'invalid_domain_trials'):
        ce._int(shrink[key], 0, bounds['max_shrink_steps'], key)
    if (type(shrink['deletion_1_minimal']) is not bool or type(shrink['budget_exhausted']) is not bool
            or shrink['minimality_scope'] != 'admissible_single_row_deletions'):
        raise ValueError('Replay shrink scope')
    return payload


def _arm(args, arm, profile, plan, spec, reference, candidate, digests, bounds, replay):
    detail = dict(found=False, bounded=True, attempted_examples=0, examples=0, skipped_declared_examples=0,
                  invalid_domain_examples=0, solver_checks=0)
    conn = None
    try:
        if replay is not None and replay['arm'] != arm:
            return 'not_run', dict(detail, reason='not_replayed'), None
        if replay is not None:
            cases, reason = [(replay['case_index'], replay['tables'])], 'replay'
        else:
            generation = witness.generate(spec, profile, max_examples=bounds['max_examples'], timeout_ms=bounds['solver_timeout_ms'])
            detail['solver_checks'] = generation.solver_checks
            cases, reason = enumerate(generation.fixtures), generation.reason
        detail['reason'] = reason
        complete = reason in ('space_exhausted', 'unsatisfiable', 'example_limit', 'replay')
        detail['classification'] = 'bounded_no_witness' if complete else 'incomplete'
        if not complete:
            # No prefix of an interrupted generation is published as audit evidence.
            return 'not_run', detail, None

        def admissible(tables):
            _admissible(spec, profile, plan.original, arm, tables)

        def evaluate(tables):
            return ce.evaluate(conn, tables, reference, candidate, bounds['statement_timeout_ms'], profile, schema=spec.schema)

        found = None
        for index, tables in cases:
            detail['attempted_examples'] += 1
            try:
                witness.validate_fixture(spec, tables, profile)
            except constraints.ConstraintViolation:
                detail['invalid_domain_examples'] += 1
                continue
            if arm == 'relaxed' and not _outside(tables, spec.schema, plan.original):
                detail['skipped_declared_examples'] += 1
                continue
            if conn is None:
                conn = ce.db.connect(args.dsn_env)
            answer = evaluate(tables)
            detail['examples'] += 1
            if not answer['equal']:
                found = (index, tables)
                break
        if found is None:
            return 'not_run', detail, None
        index, tables = found
        original = replay['original_tables'] if replay is not None else tables
        _admissible(spec, profile, plan.original, arm, original)
        reduced = ce.shrink(tables, lambda trial: not evaluate(trial)['equal'], bounds['max_shrink_steps'],
                            profile=profile, schema=spec.schema, admissible=admissible)
        admissible(reduced['tables'])
        final = evaluate(reduced['tables'])
        if final['equal']:
            return 'error', dict(detail, reason='witness_not_stable'), None
        classification = 'declared_domain_disagreement' if arm == 'declared' else 'outside_declared_domain'
        pending = {'format': FORMAT, 'schema_version': VERSION, 'synthetic': True, 'generator_version': VERSION,
                   'arm': arm, 'classification': classification, 'scenario': 'constraint_witness', 'seed': args.seed,
                   'case_index': index, 'sql_sha256': digests, 'audit': plan.binding, 'generator': witness.binding(spec, profile),
                   'constraints': constraints.binding(profile), 'bounds': bounds, 'shrink_strategy': 'rows',
                   'original_tables': original, 'tables': reduced['tables'],
                   'shrink': {key: value for key, value in reduced.items() if key != 'tables'}, 'scope': SCOPE}
        raw = _canonical(pending)
        if len(raw) > REPLAY_LIMIT:
            return 'error', dict(detail, reason='replay_size_limit'), None
        detail.update(found=True, classification=classification, original_rows=ce._rows(original),
                      minimized_rows=ce._rows(reduced['tables']), shrink_steps=reduced['steps'],
                      shrink_errors=reduced['shrink_errors'], shrink_invalid_domain_trials=reduced['invalid_domain_trials'],
                      deletion_1_minimal=reduced['deletion_1_minimal'], budget_exhausted=reduced['budget_exhausted'])
        # A failed shrink trial was not a witness. Conservatively surface incomplete reduction as error.
        if reduced['shrink_errors']:
            return 'error', dict(detail, found=False, reason='shrink_query_error'), None
        return 'fail' if arm == 'declared' else 'not_run', detail, raw
    except Exception as error:
        # Never emit exception messages/classes controlled by query text or data.
        arg = error.args[0] if error.args else None
        code = arg.get('C', '') if isinstance(arg, dict) else ''
        return 'error', dict(detail, found=False, reason='execution_error',
                             sqlstate=code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else ''), None
    finally:
        if conn is not None:
            conn.close()


def run(args, out):
    """Two equally bounded searches; fixture bytes publish only after stable input checks."""
    directory = Path(out) / 'counterexample' / 'audit'
    try:
        # The CLI may already have prepared before parser/config preflight.
        # Direct API callers retain the same capture-before-cleanup protection.
        replay_raw = (args._constraint_audit_replay_raw if hasattr(args, '_constraint_audit_replay_raw')
                      else prepare_run(args, out))
        directory.mkdir(parents=True, exist_ok=True)
        if (any(getattr(args, name, None) is not None for name in ('scenario', 'metamorphic_sql', 'mutation_operator', 'gold_sql'))
                or any(getattr(args, name, None) is None for name in ('constraints', 'witness_schema', 'reference_sql', 'candidate_sql'))
                or getattr(args, 'shrink_strategy', 'rows') != 'rows'
                or not isinstance(args.dsn_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.dsn_env)):
            raise ValueError('Audit mode contract')
        seed = ce._int(args.seed, -(2**63), 2**63 - 1, 'seed')
        bounds = {'max_examples': ce._int(args.max_examples, 1, 1000, 'max_examples'),
                  'max_shrink_steps': ce._int(args.max_shrink_steps, 0, 10000, 'max_shrink_steps'),
                  'statement_timeout_ms': ce._int(args.statement_timeout_ms, 1000, 120000, 'statement_timeout_ms'),
                  'solver_timeout_ms': ce._int(args.solver_timeout_ms, 1, 60000, 'solver_timeout_ms')}
        spec = witness.load(args.witness_schema)
        reference, reference_sha = ce._read_sql(args.reference_sql)
        candidate, candidate_sha = ce._read_sql(args.candidate_sql)
        digests = {'reference': reference_sha, 'candidate': candidate_sha}
        plan = _plan(args, spec, directory)
        replay = _load_replay(replay_raw, plan, spec, seed, digests, bounds) if args.replay and replay_raw else None
        if args.replay and replay_raw is None:
            raise ValueError('Replay unavailable')
    except (StageError, OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise _bad() from None
    result = Result('counterexample')
    result.inputs = {'constraint_audit': {'mode': 'constraint_audit', **plan.summary, 'sql_sha256': digests,
                                        'spec_sha256': spec.sha256, 'schema_sha256': spec.schema_sha256,
                                        'seed': seed, 'per_arm_bounds': bounds}}
    result.not_proven.extend([
        'Two bounded searches can approach twice one search and shrink budget; finite values, types and row bounds stay fixed. SQL conditions are not encoded in Z3.',
        'Finding no witness proves neither equivalence nor correctness, including finite space exhaustion. Solver interruption leaves the search incomplete.',
        'Provenance categories are caller claims; catalog does not mean runtime catalog confirmation. No constraint category is automatically relaxed.',
        'Outside-declared-domain disagreement is conditional diagnostic evidence, not proof that a declared constraint is false or production is defective.',
        'Arm ordering and bounded prefixes can differ, so arm outcomes do not establish causal necessity or exhaustive disappearance.',
        'Each evaluated fixture uses a read-only REPEATABLE READ transaction; the whole run has no shared snapshot. Trusted SQL may read external real relations.',
        'Text uses exact equality and C collation in both domains. Shrinking preserves finite values, original row bounds and relaxed witnesses outside the declared domain.'
    ])
    outcomes = []
    for arm, profile in (('declared', plan.original), ('relaxed', plan.relaxed)):
        outcomes.append((arm, *_arm(args, arm, profile, plan, spec, reference, candidate, digests, bounds, replay)))
    confirmed = []
    for arm, status, detail, raw in outcomes:
        conn = None
        try:
            if raw is not None:
                payload = constraints.decode_json(raw.decode('utf-8'))
                profile = plan.original if arm == 'declared' else plan.relaxed
                _admissible(spec, profile, plan.original, arm, payload['tables'])
                conn = ce.db.connect(args.dsn_env)
                answer = ce.evaluate(conn, payload['tables'], reference, candidate,
                                     bounds['statement_timeout_ms'], profile, schema=spec.schema)
                if answer['equal']:
                    status, detail, raw = 'error', dict(detail, found=False, reason='witness_not_stable'), None
        except Exception:
            status, detail, raw = 'error', dict(detail, found=False, reason='confirmation_error'), None
        finally:
            if conn is not None:
                conn.close()
        confirmed.append((arm, status, detail, raw))
    outcomes = confirmed
    try:
        unchanged = (spec.unchanged() and plan.unchanged() and ce._read_sql(args.reference_sql)[1] == reference_sha
                     and ce._read_sql(args.candidate_sql)[1] == candidate_sha)
    except StageError:
        unchanged = False
    if not unchanged:
        result.add('counterexample:audit_inputs', 'error', '', reason='inputs_changed_during_run', bounded=True)
        result.next_safe_action = 'Restore exact inputs or rerun the audit.'
        return result
    # Both searches and their final PostgreSQL confirmations have completed;
    # every original input and the canonical projection was checked afterwards.
    try:
        for arm, status, detail, raw in outcomes:
            if raw is not None:
                payload = constraints.decode_json(raw.decode('utf-8'))
                profile = plan.original if arm == 'declared' else plan.relaxed
                _admissible(spec, profile, plan.original, arm, payload['tables'])
        for arm, status, detail, raw in outcomes:
            if raw is not None:
                _write(directory / (arm + '-fixture.json'), raw)
                detail['fixture_sha256'] = _digest(raw)
            result.add('counterexample:audit_' + arm, status, '', **detail)
    except (OSError, ValueError, TypeError):
        for arm in ('declared', 'relaxed'):
            (directory / (arm + '-fixture.json')).unlink(missing_ok=True)
        result.add('counterexample:audit_publication', 'error', '', reason='artifact_publication_error')
    result.next_safe_action = 'Review bounded diagnostics; replay a witness with the exact bound inputs and options.'
    return result
