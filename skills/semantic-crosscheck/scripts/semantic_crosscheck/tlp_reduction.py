"""Bounded discrepancy-preserving reduction for the existing synthetic schema.

Hashes bind local bytes, not authenticity or the cause of an optimizer defect.
Only explicit replay artifacts contain rows or reduction operations.
"""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import re

from . import constraints, counterexample as ce, reduction

VERSION = 2
LIMIT = 1024 * 1024
_SOURCES = ('tlp.py', 'tlp_reduction.py', 'reduction.py', 'constraints.py', 'canon.py', 'db.py')
_FIELDS = {'format', 'schema_version', 'synthetic', 'oracle', 'original', 'bindings', 'budget',
           'tables', 'tables_sha256', 'signature', 'trace', 'search'}
_SEARCH = {'steps', 'accepted', 'shrink_errors', 'invalid_domain_trials', 'minimality_scope',
           'deletion_1_minimal', 'budget_exhausted', 'supported_operations', 'operation_counts'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


def bindings():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in _SOURCES}


def _relation(left, right):
    if left is None or right is None:
        return 'equal' if left is right else 'baseline_null' if left is None else 'partition_null'
    return 'equal' if left == right else 'less' if left < right else 'greater'


def signature(payload, oracle):
    """Hash signed discrepancy support, ignoring duplicate multiplicities.

    Aggregate/count relations retain only equality, direction and NULL side.
    This identifies an observable failure class, never the same engine bug.
    """
    from . import tlp
    if oracle == 'norec':
        answer = tlp.compare_norec(payload)
        return None if answer['equal'] else {'version': 1, 'oracle': oracle,
            'count_relation': _relation(answer['optimized_count'], answer['reference_count'])}
    if oracle != 'tlp':
        raise ValueError('Unsupported reduction oracle')
    answer = tlp.compare(payload)
    if answer['equal']:
        return None
    bags = []
    for name in ('baseline', 'partitioned'):
        bags.append(Counter(json.dumps([(key, ce._cell(value)) for key, value in row.items()],
                                       ensure_ascii=False, separators=(',', ':')) for row in payload[name]))
    signed = sorted((row, 'baseline') for row in bags[0] - bags[1])
    signed += sorted((row, 'partition') for row in bags[1] - bags[0])
    return {'version': 1, 'oracle': oracle, 'bag_support_sha256': _hash(signed),
            'aggregate_relation': _relation(payload['baseline_aggregate'], payload['partition_aggregate'])}


def _signature(value, oracle):
    keys = ({'version', 'oracle', 'bag_support_sha256', 'aggregate_relation'} if oracle == 'tlp'
            else {'version', 'oracle', 'count_relation'})
    if (not isinstance(value, dict) or set(value) != keys or type(value['version']) is not int
            or value['version'] != 1 or value['oracle'] != oracle):
        raise ValueError('Reduction signature')
    if oracle == 'tlp':
        if (not isinstance(value['bag_support_sha256'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', value['bag_support_sha256'])
                or value['aggregate_relation'] not in ('equal', 'less', 'greater', 'baseline_null', 'partition_null')):
            raise ValueError('Reduction signature')
    elif value['count_relation'] not in ('less', 'greater'):
        raise ValueError('Reduction signature')


def _witness(evaluator, fixture, expected):
    answer, observed = evaluator(copy.deepcopy(fixture))
    if not isinstance(answer, dict) or type(answer.get('equal')) is not bool:
        raise ValueError('Reduction evaluator')
    return answer, not answer['equal'] and observed == expected


def reduce(fixture, evaluator, expected, budget):
    """Reuse the structured reducer; errors are counted and never accepted."""
    return reduction.shrink(fixture, lambda trial: _witness(evaluator, trial, expected)[1],
                            budget, schema=ce.SCHEMA)


def build(original, summary, expected, oracle, budget, source_bindings):
    return {'format': f'sxc-synthetic-{oracle}-reduction', 'schema_version': VERSION, 'synthetic': True,
            'oracle': oracle, 'original': copy.deepcopy(original), 'bindings': dict(source_bindings),
            'budget': budget, 'tables': copy.deepcopy(summary['tables']),
            'tables_sha256': _hash(summary['tables']), 'signature': dict(expected),
            'trace': copy.deepcopy(summary['trace']), 'search': {key: summary[key] for key in _SEARCH}}


def _search(value, trace, budget):
    if not isinstance(value, dict) or set(value) != _SEARCH:
        raise ValueError('Reduction search fields')
    for key in ('steps', 'accepted', 'shrink_errors', 'invalid_domain_trials'):
        if type(value[key]) is not int or not 0 <= value[key] <= budget:
            raise ValueError('Reduction search counts')
    if (value['accepted'] != len(trace)
            or sum(value[key] for key in ('accepted', 'shrink_errors', 'invalid_domain_trials')) > value['steps']
            or value['minimality_scope'] != 'single_row_deletions_after_structured_reduction'
            or type(value['deletion_1_minimal']) is not bool or type(value['budget_exhausted']) is not bool
            or value['deletion_1_minimal'] != (not value['budget_exhausted'] and value['shrink_errors'] == 0)
            or (value['budget_exhausted'] and value['steps'] != budget)
            or value['supported_operations'] != list(reduction.OPERATIONS)):
        raise ValueError('Reduction search claims')
    counts = value['operation_counts']
    if not isinstance(counts, dict) or set(counts) != set(reduction.OPERATIONS):
        raise ValueError('Reduction operation counts')
    traced = Counter(operation['operation'] for operation in trace)
    for name, count in counts.items():
        if (not isinstance(count, dict) or set(count) != {'attempted', 'accepted'}
                or any(type(count[key]) is not int for key in count)
                or not 0 <= count['accepted'] <= count['attempted'] <= budget
                or count['accepted'] != traced[name]):
            raise ValueError('Reduction operation counts')
    if (sum(count['attempted'] for count in counts.values()) != value['steps']
            or sum(count['accepted'] for count in counts.values()) != value['accepted']):
        raise ValueError('Reduction operation totals')


def decode(raw, spec, seed, oracle, budget):
    """Validate the complete closed artifact and legal trace before DB access."""
    from . import tlp
    try:
        if len(raw.encode('utf-8')) > LIMIT:
            raise ValueError('Reduction replay bound')
        value = constraints.decode_json(raw)
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or value['format'] != f'sxc-synthetic-{oracle}-reduction'
                or type(value['schema_version']) is not int or value['schema_version'] != VERSION
                or value['synthetic'] is not True or value['oracle'] != oracle
                or type(value['budget']) is not int or value['budget'] != budget
                or not 0 <= budget <= reduction.LIMIT or value['bindings'] != bindings()):
            raise ValueError('Reduction replay binding')
        original = tlp.decode_replay(json.dumps(value['original']), spec, seed, oracle=oracle)
        _signature(value['signature'], oracle)
        trace = value['trace']
        if not isinstance(trace, list) or len(trace) > budget:
            raise ValueError('Reduction trace bound')
        rebuilt = reduction.replay_trace(original['tables'], trace, schema=ce.SCHEMA)
        if _hash(rebuilt) != _hash(value['tables']) or value['tables_sha256'] != _hash(rebuilt):
            raise ValueError('Reduction table binding')
        _search(value['search'], trace, budget)
        return value
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError, tlp.StageError):
        raise tlp.StageError('tlp: reduced replay is outside its supported bound or binding', tlp.EXIT_CONFIG) from None


def replay(value, evaluator):
    """Recheck original, every accepted step and final fixture in linear steps.

    Original search statistics are not reexecuted; replay never reclaims deletion
    minimality. Every evaluator call itself checks two complete observations.
    """
    current = copy.deepcopy(value['original']['tables'])
    expected = value['signature']
    answer, matches = _witness(evaluator, current, expected)
    if not matches:
        raise ValueError('Original reduction witness changed')
    for operation in value['trace']:
        current = reduction.replay_trace(current, [operation], schema=ce.SCHEMA)
        answer, matches = _witness(evaluator, current, expected)
        if not matches:
            raise ValueError('Reduction trace witness changed')
    if _hash(current) != value['tables_sha256']:
        raise ValueError('Reduced replay changed')
    answer, matches = _witness(evaluator, current, expected)
    if not matches:
        raise ValueError('Final reduction witness changed')
    return answer


def public(summary, replayed=False):
    return {'scope': 'accepted_trace_replay_only' if replayed else summary['minimality_scope'],
            'steps': summary['steps'], 'accepted': summary['accepted'],
            'shrink_errors': summary['shrink_errors'], 'invalid_domain_trials': summary['invalid_domain_trials'],
            'budget_exhausted': summary['budget_exhausted'],
            'deletion_1_minimal': False if replayed else summary['deletion_1_minimal']}


def encode(value):
    text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if len(text.encode('utf-8')) > LIMIT:
        raise ValueError('Reduction replay size bound')
    return text
