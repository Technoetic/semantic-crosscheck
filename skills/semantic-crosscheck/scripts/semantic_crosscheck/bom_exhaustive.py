"""Bounded-exhaustive BOM fixtures for the metamorphic mode (scenario bom_exhaustive).

Instead of seeded samples of one diamond topology, this generator enumerates, without randomness, every connected
acyclic BOM within a small scope, one canonical representative per isomorphism class (relabelling parts):

  2..4 parts, 1..5 bom_line rows, at most 2 rows per (parent, child) pair, qty_per in {1, 2}.

There are 1,743 such structures (2 parts: 5, 3 parts: 153, 4 parts: 1,585). Every structure runs under two label
placements, because part ids are values in SQL and a label-dependent filter (say parent_id < child_id) would pass if
parents always got the smaller labels: `topological` (parents smaller) and `reverse` (children smaller). Case i is
structure i // 2 under placement i % 2, so the case count is known in advance and the run is exhaustive, not sampled.

Canonical form: the structure's lines as a sorted tuple of (parent, child, qty) over node numbers 0..n-1; the
representative is the smallest such tuple over every relabelling that keeps each line pointing from a smaller to a
larger number (a topological numbering). Isomorphic structures share that set of relabellings, so they share the
minimum. The method follows Marinov, Andoni, Daniliuc, Khurshid and Rinard, "An Evaluation of Exhaustive Testing for
Data Structures" (MIT CSAIL technical report, 2003): every valid input within a small scope, one per isomorphism
class; no code is borrowed.
"""
from functools import lru_cache
import hashlib
import itertools
import json

SCENARIO = 'bom_exhaustive'
MIN_PARTS, MAX_PARTS = 2, 4
MAX_LINES = 5
MAX_LINES_PER_PAIR = 2
QUANTITIES = (1, 2)
PLACEMENTS = ('topological', 'reverse')
LABELS = ('A', 'B', 'C', 'D')
NOISE = 'noise'


def scope():
    """The tested scope, recorded in results so that a reader sees what 'exhaustive' covers."""
    return {'min_parts': MIN_PARTS, 'max_parts': MAX_PARTS, 'max_bom_lines': MAX_LINES,
            'max_lines_per_pair': MAX_LINES_PER_PAIR, 'qty_per': list(QUANTITIES), 'placements': list(PLACEMENTS)}


def _connected(parts, lines):
    neighbours = {node: set() for node in range(parts)}
    for parent, child, _quantity in lines:
        neighbours[parent].add(child)
        neighbours[child].add(parent)
    seen, stack = {0}, [0]
    while stack:
        for node in neighbours[stack.pop()] - seen:
            seen.add(node)
            stack.append(node)
    return len(seen) == parts


def _canonical(parts, lines):
    best = None
    for numbering in itertools.permutations(range(parts)):
        if all(numbering[parent] < numbering[child] for parent, child, _quantity in lines):
            relabelled = tuple(sorted((numbering[parent], numbering[child], quantity) for parent, child, quantity in lines))
            if best is None or relabelled < best:
                best = relabelled
    return best


def _enumerate():
    """((parts, lines), ...) ordered by part count, line count and canonical lines; no cache, for determinism tests."""
    options = [()] + [tuple(choice) for size in range(1, MAX_LINES_PER_PAIR + 1)
                      for choice in itertools.combinations_with_replacement(QUANTITIES, size)]
    found = set()
    for parts in range(MIN_PARTS, MAX_PARTS + 1):
        # Every DAG has a topological numbering, so lines from smaller to larger numbers reach every class.
        pairs = [(parent, child) for parent in range(parts) for child in range(parent + 1, parts)]
        for choice in itertools.product(options, repeat=len(pairs)):
            lines = [(parent, child, quantity) for (parent, child), quantities in zip(pairs, choice) for quantity in quantities]
            if 1 <= len(lines) <= MAX_LINES and _connected(parts, lines):
                found.add((parts, _canonical(parts, lines)))
    return tuple(sorted(found, key=lambda structure: (structure[0], len(structure[1]), structure[1])))


@lru_cache(maxsize=1)
def structures():
    return _enumerate()


def cases_total():
    return len(structures()) * len(PLACEMENTS)


def _fixture(parts, lines, placement):
    label = (lambda node: LABELS[node]) if placement == 'topological' else (lambda node: LABELS[parts - 1 - node])
    fixture = {'part': [], 'bom_line': [], 'sales_order': [], 'stock': []}
    fixture['part'] = [[name, 'synthetic', 'unit', 'make', 'synthetic_' + name]
                       for name in sorted(label(node) for node in range(parts))] + [
                      [NOISE, 'synthetic', 'unit', 'make', 'synthetic_' + NOISE]]
    fixture['bom_line'] = [[label(parent), label(child), number + 1, str(quantity)]
                           for number, (parent, child, quantity) in enumerate(lines)]
    fixture['stock'] = [[NOISE, 'L0', '1']]
    return fixture


def case(index):
    """(fixture, info) of case `index`: structure index // 2 under placement index % 2; a fresh copy every call."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < cases_total():
        raise ValueError('bom_exhaustive case index out of range')
    structure_index, placement = divmod(index, len(PLACEMENTS))
    parts, lines = structures()[structure_index]
    return _fixture(parts, lines, PLACEMENTS[placement]), {
        'structure_index': structure_index, 'placement': PLACEMENTS[placement], 'parts': parts, 'bom_lines': len(lines)}


@lru_cache(maxsize=1)
def enumeration_sha256():
    """SHA-256 of every case fixture in order: binds a replay fixture to this exact enumeration."""
    digest = hashlib.sha256()
    for index in range(cases_total()):
        digest.update(json.dumps(case(index)[0], sort_keys=True, separators=(',', ':')).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()
