"""Bounded structured reduction of synthetic witnesses, with auditable replay.

Every accepted operation either removes rows or lowers a cell's finite rank.
After each acceptance, single-row deletion starts again. Dependency groups
are reverse foreign-key closures; values in declared keys are never edited.
This is a deterministic local reduction, not a global-minimum algorithm.
Only ``tables`` and the fixture-only ``trace`` may contain cell values.
"""
import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib

from . import constraints

LIMIT = 10000
OPERATIONS = ('chunk_deletion', 'row_deletion', 'dependency_group_deletion', 'value_simplification')
_DDMIN_MIN_ROWS = 4


def _schema(schema):
    if not isinstance(schema, Mapping) or not schema:
        raise ValueError('Structured reduction requires an explicit schema')
    return dict(schema)


def _protected_columns(schema, profile):
    protected = {table: set() for table in schema}
    if profile is not None:
        for table, columns in profile.unique:
            protected[table].update(columns)
        for table, columns, target, target_columns in profile.foreign_keys:
            protected[table].update(columns)
            protected[target].update(target_columns)
    return protected


def _simplifications(value, kind):
    """Yield only lower-ranked canonical values, preserving numeric strings.

    NULL has rank zero, followed by the canonical values in the order below.
    Every other value has the next rank. Thus even predicates accepting both
    1 and -1 cannot make simplification oscillate. No operation adds a cell,
    column or row; the lexicographic (row count, sum of cell ranks) descends.
    """
    if kind == 'text':
        canonical = (None, '', 'a')
    elif kind == 'date':
        canonical = (None, '2000-01-01')
    elif kind == 'integer' or constraints._NUMERIC_TYPE.fullmatch(kind):
        numbers = ('0', '1', '-1') if isinstance(value, str) else (0, 1, -1)
        canonical = (None,) + numbers
    else:
        return
    normalized = constraints._value(value, kind)
    valid = []
    for target in canonical:
        try:
            stored = constraints._value(target, kind)
        except (ValueError, ArithmeticError):
            continue  # E.g. numeric(p,p) cannot store either 1 or -1.
        valid.append((target, stored))
    rank = next((index for index, (_, target) in enumerate(valid) if normalized == target), len(valid))
    yield from (target for target, _ in valid[:rank])


def _dependencies(fixture, schema, profile):
    """Map each referenced row to rows which cannot survive its deletion."""
    nodes = [(table, index) for table in schema for index in range(len(fixture[table]))]
    incoming = {node: set() for node in nodes}
    if profile is None or not profile.foreign_keys:
        return nodes, incoming
    normalized = {
        table: [tuple(constraints._value(value, kind) for value, (_, kind) in zip(row, columns))
                for row in fixture[table]] for table, columns in schema.items()
    }
    for table, columns, target, target_columns in profile.foreign_keys:
        available = {tuple(row[index] for index in target_columns): (target, row_index)
                     for row_index, row in enumerate(normalized[target])}
        for row_index, row in enumerate(normalized[table]):
            key = tuple(row[index] for index in columns)
            if None not in key:  # MATCH SIMPLE: nullable references make no edge.
                incoming[available[key]].add((table, row_index))
    return nodes, incoming


def _closure(seed, incoming):
    group, pending = {seed}, [seed]
    while pending:
        for dependant in incoming[pending.pop()]:
            if dependant not in group:
                group.add(dependant)
                pending.append(dependant)
    return group


def _group_indices(group, schema):
    return {table: sorted(index for name, index in group if name == table)
            for table in schema if any(name == table for name, _ in group)}


def _groups(fixture, schema, profile):
    if profile is None or not profile.foreign_keys:
        return
    nodes, incoming = _dependencies(fixture, schema, profile)
    ordinals = {node: index for index, node in enumerate(nodes)}
    seen = set()
    for seed in nodes:
        group = _closure(seed, incoming)
        if len(group) <= 1:
            continue
        # Store fixed-size digests, not quadratic copies of large closures.
        digest = hashlib.sha256()
        for index in sorted(ordinals[node] for node in group):
            digest.update(index.to_bytes(4, 'big'))
        signature = digest.digest()
        if signature not in seen:
            seen.add(signature)
            yield {'operation': 'dependency_group_deletion', 'indices': _group_indices(group, schema)}


def _operations(fixture, schema, profile):
    for table in schema:
        for index in range(len(fixture[table])):
            yield {'operation': 'row_deletion', 'table': table, 'index': index}
    yield from _groups(fixture, schema, profile)
    protected = _protected_columns(schema, profile)
    for table, columns in schema.items():
        for row_index, row in enumerate(fixture[table]):
            for column, (value, (_, kind)) in enumerate(zip(row, columns)):
                if column not in protected[table]:
                    for replacement in _simplifications(value, kind):
                        yield {'operation': 'value_simplification', 'table': table,
                               'row': row_index, 'column': column, 'value': replacement}


def _apply(fixture, operation):
    trial = copy.deepcopy(fixture)
    name = operation['operation']
    if name == 'chunk_deletion':
        deleted = set(operation['indices'])
        table = operation['table']
        trial[table] = [row for index, row in enumerate(trial[table]) if index not in deleted]
    elif name == 'row_deletion':
        del trial[operation['table']][operation['index']]
    elif name == 'dependency_group_deletion':
        for table, indices in operation['indices'].items():
            deleted = set(indices)
            trial[table] = [row for index, row in enumerate(trial[table]) if index not in deleted]
    else:
        trial[operation['table']][operation['row']][operation['column']] = operation['value']
    return trial


def _validate(fixture, schema, profile, admissible):
    constraints.validate_fixture(fixture, schema, profile)
    if admissible is not None:
        admissible(fixture)


def _chunk_slices(length, granularity):
    """Balanced contiguous chunk boundaries of size >= 2 for DDMIN complement trials."""
    chunks = []
    for part in range(granularity):
        start = (part * length) // granularity
        end = ((part + 1) * length) // granularity
        if end - start >= 2:
            chunks.append((start, end))
    return chunks


@dataclass
class _ReductionState:
    steps: int = 0
    accepted: int = 0
    errors: int = 0
    invalid_domain: int = 0
    counts: dict[str, dict[str, int]] = field(
        default_factory=lambda: {name: {'attempted': 0, 'accepted': 0} for name in OPERATIONS}
    )
    trace: list[dict[str, object]] = field(default_factory=list)


def _run_trial(current, operation, predicate, schema, profile, admissible, state: _ReductionState):
    """Execute one budgeted trial operation and update shared reduction counters."""
    state.steps += 1
    name = operation['operation']
    state.counts[name]['attempted'] += 1
    trial = _apply(current, operation)
    try:
        _validate(trial, schema, profile, admissible)
    except constraints.ConstraintViolation:
        state.invalid_domain += 1
        return current, False
    try:
        remains_witness = predicate(copy.deepcopy(trial))
    except Exception:  # Exception text can quote values: retain only a count.
        state.errors += 1
        return current, False
    if remains_witness:
        state.accepted += 1
        state.counts[name]['accepted'] += 1
        state.trace.append(operation)
        return trial, True
    return current, False


def _ddmin_prelude(current, predicate, max_steps, schema, profile, admissible, state: _ReductionState):
    """Bounded DDMIN binary-chunk deletion prelude for FK-free tables (Zeller & Hildebrandt 2002)."""
    if (profile is not None and profile.foreign_keys) or max_steps <= 0:
        return current, False
    for table in schema:
        granularity = 2
        while len(current[table]) >= _DDMIN_MIN_ROWS:
            chunks = _chunk_slices(len(current[table]), granularity)
            if not chunks:
                break
            reduced = False
            for start, end in chunks:
                if state.steps >= max_steps:
                    return current, True
                operation = {'operation': 'chunk_deletion', 'table': table, 'indices': list(range(start, end))}
                current, ok = _run_trial(current, operation, predicate, schema, profile, admissible, state)
                if ok:
                    granularity = max(granularity - 1, 2)
                    reduced = True
                    break
            if not reduced:
                if granularity * 2 > len(current[table]):
                    break
                granularity *= 2
    return current, False


def shrink(fixture, predicate, max_steps, profile=None, schema=None, admissible=None):
    """Reduce a known witness under one shared trial budget (0..10000).

    The caller establishes that the initial fixture is a witness. Every trial
    is domain-validated before running the actual witness predicate. Errors
    are counted without their messages, and prevent a minimality claim.
    ``trace`` contains accepted operations for fixture-only replay; callers
    must omit it alongside ``tables`` from public count/id summaries.
    An optional ``admissible(fixture)`` validator raises ConstraintViolation
    for finite domains or row bounds beyond the integrity profile.
    """
    schema = _schema(schema)
    if type(max_steps) is not int or not 0 <= max_steps <= LIMIT:
        raise ValueError('Structured reduction budget must be an integer 0..10000')
    _validate(fixture, schema, profile, admissible)
    current = copy.deepcopy(fixture)
    state = _ReductionState()

    def summary(exhausted):
        return {'tables': current, 'steps': state.steps, 'accepted': state.accepted,
                'shrink_errors': state.errors, 'invalid_domain_trials': state.invalid_domain,
                'minimality_scope': ('admissible_single_row_deletions_after_structured_reduction' if profile or admissible is not None else
                                     'single_row_deletions_after_structured_reduction'),
                'deletion_1_minimal': not exhausted and state.errors == 0, 'budget_exhausted': exhausted,
                'supported_operations': list(OPERATIONS), 'operation_counts': state.counts, 'trace': state.trace}

    current, exhausted = _ddmin_prelude(current, predicate, max_steps, schema, profile, admissible, state)
    if exhausted:
        return summary(True)

    while True:
        for operation in _operations(current, schema, profile):
            if state.steps >= max_steps:
                return summary(True)
            current, ok = _run_trial(current, operation, predicate, schema, profile, admissible, state)
            if ok:
                break  # Restart single-row deletion after every accepted transform.
        else:
            return summary(False)


def _index(value, length):
    if type(value) is not int or not 0 <= value < length:
        raise ValueError('Replay index')


def _check_operation(fixture, operation, schema, profile):
    if not isinstance(operation, dict) or operation.get('operation') not in OPERATIONS:
        raise ValueError('Replay operation')
    name = operation['operation']
    if name == 'chunk_deletion':
        if (set(operation) != {'operation', 'table', 'indices'}
                or operation['table'] not in schema
                or (profile is not None and profile.foreign_keys)):
            raise ValueError('Replay chunk')
        indices = operation['indices']
        table = operation['table']
        if not isinstance(indices, list) or len(indices) < 2 or len(indices) > len(fixture[table]):
            raise ValueError('Replay chunk')
        for index in indices:
            _index(index, len(fixture[table]))
        if indices != list(range(indices[0], indices[0] + len(indices))):
            raise ValueError('Replay chunk')
        return
    if name == 'dependency_group_deletion':
        if set(operation) != {'operation', 'indices'} or not isinstance(operation['indices'], dict) or not operation['indices']:
            raise ValueError('Replay group')
        group = set()
        for table, indices in operation['indices'].items():
            if table not in schema or not isinstance(indices, list) or not indices:
                raise ValueError('Replay group')
            for index in indices:
                _index(index, len(fixture[table]))
            if indices != sorted(set(indices)):
                raise ValueError('Replay group')
            group.update((table, index) for index in indices)
        if len(group) <= 1 or profile is None or not profile.foreign_keys:
            raise ValueError('Replay group')
        nodes, incoming = _dependencies(fixture, schema, profile)
        if not any(_closure(seed, incoming) == group for seed in nodes if seed in group):
            raise ValueError('Replay dependency closure')
        return
    expected = ({'operation', 'table', 'index'} if name == 'row_deletion' else
                {'operation', 'table', 'row', 'column', 'value'})
    if set(operation) != expected or operation['table'] not in schema:
        raise ValueError('Replay operation fields')
    table = operation['table']
    if name == 'row_deletion':
        _index(operation['index'], len(fixture[table]))
        return
    _index(operation['row'], len(fixture[table]))
    column = operation['column']
    _index(column, len(schema[table]))
    if column in _protected_columns(schema, profile)[table]:
        raise ValueError('Replay protected column')
    original = fixture[table][operation['row']][column]
    kind = schema[table][column][1]
    value = operation['value']
    if not any(type(value) is type(candidate) and value == candidate for candidate in _simplifications(original, kind)):
        raise ValueError('Replay noncanonical simplification')


def replay_trace(original, trace, *, schema, profile=None, admissible=None):
    """Rebuild only supported descending transforms, validating every step.

    This verifies transformation provenance and the declared domain. It does
    not establish that any intermediate or final fixture is a SQL witness;
    the caller must evaluate the final fixture with its actual predicate.
    """
    try:
        schema = _schema(schema)
        if not isinstance(trace, list) or len(trace) > LIMIT:
            raise ValueError('Replay trace bound')
        _validate(original, schema, profile, admissible)
        current = copy.deepcopy(original)
        for operation in trace:
            _check_operation(current, operation, schema, profile)
            current = _apply(current, operation)
            _validate(current, schema, profile, admissible)
        return current
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError('Structured reduction replay is outside its supported contract') from None
