"""BOM metamorphic relations: check one trusted closure SQL without a gold query.

Q(a, d) is the BOM path-product quantity: the sum over all paths a -> d of the product of the edge quantities
(bom_line.qty_per). The trusted SQL returns at least the columns ancestor, descendant and qty over the fixed
synthetic schema. Each relation transforms a generated bom_diamond fixture so that the expected answer follows
from the answer on the original fixture alone, runs the same SQL on both, and compares exact decimals:

  mr1_unfold     duplicate shared sub-assemblies until the DAG is a forest; for every instance i of part x and
                 original part d, the sum of Q'(i, c) over the copies c of d equals Q(x, d)
  mr2_scale      multiply one bom_line on a multi-path by K: Q'(a, d) = Q(a, d) + (K-1) q Q(a, u) Q(v, d)
  mr3_disjoint   add a fresh component containing a diamond: pairs of original ids are unchanged and no pair
                 crosses the components
  mr4_unit_node  replace (u, v, q) by (u, w, q) and (w, v, 1): original pairs are unchanged,
                 Q'(a, w) = Q(a, u) q and Q'(w, d) = Q(v, d)

Q(x, x) = 1 (the empty path) only inside these formulas; reflexive answer rows are not compared. An absent pair
is 0. Each fixture runs in its own read-only REPEATABLE READ transaction through the counterexample CTE wrapper,
with every value bound. Results hold counts, ids and hashes; only fixture.json holds generated rows.
The idea of transforming data so that the expected result is known follows EDC (Detecting Logic Bugs in DBMSs
via Equivalent Data Construction, PACMMOD 3(6), 2025); no code is borrowed.
"""
from collections import Counter, namedtuple
from decimal import Decimal, Inexact, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re

from . import canon, constraints, counterexample as ce, db
from .result import EXIT_CONFIG, Result, StageError

RELATIONS = ('mr1_unfold', 'mr2_scale', 'mr3_disjoint', 'mr4_unit_node')
CHECK_IDS = {'mr1_unfold': 'counterexample:metamorphic:mr1_unfold',
             'mr2_scale': 'counterexample:metamorphic:mr2_scale',
             'mr3_disjoint': 'counterexample:metamorphic:mr3_disjoint',
             'mr4_unit_node': 'counterexample:metamorphic:mr4_unit_node'}
SHAPE_ID = 'counterexample:metamorphic:shape'
FORMAT = 'sxc-synthetic-metamorphic'
SCENARIO = 'bom_diamond'
K = 2
COLUMNS = ('ancestor', 'descendant', 'qty')
QTY_TYPE = dict(ce.SCHEMA['bom_line'])['qty_per']
PRECISION = 4000   # exact for any product the bounded fixtures can produce; Inexact is trapped anyway

Plan = namedtuple('Plan', 'tables spec sha256 compare')


class ShapeError(ValueError):
    """The SQL answer is not closure-shaped; the reason never quotes a value."""

    def __init__(self, reason):
        super().__init__(f'closure answer shape: {reason}')
        self.reason = reason


class SizeBound(ValueError):
    """The SQL answer exceeded the row limit."""


class NotApplicable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _spec_sha(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _plan(tables, spec, compare):
    return Plan(tables, spec, _spec_sha(spec), compare)


def closure_map(columns, rows):
    """{(ancestor, descendant): Decimal} of a closure-shaped answer; reflexive rows are left out."""
    columns = list(columns)
    if len(columns) != len(set(columns)):
        raise ShapeError('duplicate_columns')
    if not set(COLUMNS) <= set(columns):
        raise ShapeError('missing_columns')
    a, d, q = (columns.index(name) for name in COLUMNS)
    out = {}
    for row in rows:
        ancestor, descendant, quantity = row[a], row[d], row[q]
        if ancestor is None or descendant is None or quantity is None:
            raise ShapeError('null_value')
        if not isinstance(ancestor, str) or not isinstance(descendant, str):
            raise ShapeError('non_text_id')
        if isinstance(quantity, bool) or not isinstance(quantity, (int, Decimal)) or (
                isinstance(quantity, Decimal) and not quantity.is_finite()):
            raise ShapeError('non_numeric_qty')   # floats are inexact: no exact relation can be checked
        if ancestor == descendant:
            continue
        if (ancestor, descendant) in out:
            raise ShapeError('duplicate_pair')
        out[(ancestor, descendant)] = Decimal(quantity)
    return out


def answer(conn, fixture, sql, timeout_ms, profile=None):
    """Columns and rows of the trusted SQL on one fixture, in its own read-only REPEATABLE READ transaction."""
    with db.read_only(conn, timeout_ms, isolation='repeatable read'):
        wrapped, params = ce.wrap_sql(sql, fixture, profile=profile)
        prepared = conn.prepare(wrapped)
        try:
            rows = prepared.run(**params) or []
            columns = [column['name'] for column in (prepared.columns or [])]
        finally:
            prepared.close()
    if len(rows) > ce.ROW_LIMIT:
        raise SizeBound('SQL result row limit exceeded')
    return columns, rows


# ---- comparison ----------------------------------------------------------------------------------------------

def _same(left, right):
    return canon.canon(left, 'number') == canon.canon(right, 'number')


def _diff(expected, actual, keys):
    """(pairs compared, mismatched pairs); absent pairs are 0."""
    keys = {key for key in keys if key[0] != key[1]}
    return len(keys), sum(1 for key in keys if not _same(expected(key), actual.get(key, Decimal(0))))


def _arith(function):
    """Exact decimal arithmetic for expected values; an inexact result is never compared."""
    def wrapped(*args):
        with localcontext() as context:
            context.prec = PRECISION
            context.traps[Inexact] = True
            try:
                return function(*args)
            except (Inexact, InvalidOperation):
                raise NotApplicable('inexact_arithmetic') from None
    return wrapped


# ---- graph helpers -------------------------------------------------------------------------------------------

def _bom(fixture):
    """[(row index, parent, child, stored qty)] in fixture order and a topological order of the bom nodes."""
    rows = []
    for index, row in enumerate(fixture['bom_line']):
        parent, child, _line, quantity = row
        if parent is None or child is None or quantity is None:
            raise NotApplicable('null_bom_value')
        try:   # the value PostgreSQL stores after CAST(... AS numeric(18,6))
            rows.append((index, parent, child, constraints._value(quantity, QTY_TYPE)))
        except (ValueError, TypeError, InvalidOperation):
            raise NotApplicable('invalid_bom_value') from None
    if not rows:
        raise NotApplicable('no_bom_lines')
    nodes = sorted({row[1] for row in rows} | {row[2] for row in rows})
    indegree = Counter(row[2] for row in rows)
    order, ready = [], sorted(node for node in nodes if indegree[node] == 0)
    while ready:
        node = ready.pop(0)
        order.append(node)
        for _index, parent, child, _quantity in rows:
            if parent == node:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
    if len(order) != len(nodes):
        raise NotApplicable('cycle')
    return rows, order


def _path_counts(rows, order):
    """{x: {d: number of paths x -> d}} for the acyclic bom graph."""
    counts = {node: Counter() for node in order}
    for node in reversed(order):
        for _index, parent, child, _quantity in rows:
            if parent == node:
                counts[node][child] += 1
                for descendant, number in counts[child].items():
                    counts[node][descendant] += number
    return counts


def _ids(fixture):
    used = {row[0] for row in fixture['part']}
    used |= {row[1] for row in fixture['bom_line']} | {row[0] for row in fixture['bom_line']}
    used |= {row[1] for row in fixture['sales_order']} | {row[0] for row in fixture['stock']}
    return used


def _fresh(prefix, used):
    number = 0
    while f'{prefix}{number}' in used:
        number += 1
    identifier = f'{prefix}{number}'
    used.add(identifier)
    return identifier


def _copy(fixture):
    return {table: [list(row) for row in rows] for table, rows in fixture.items()}


def _synthetic_part(identifier):
    return [identifier, 'synthetic', 'unit', 'make', 'synthetic_' + identifier]


def _pick(candidates, seed, case_index, name):
    digest = hashlib.sha256(f'{ce.VERSION}|metamorphic|{seed}|{case_index}|{name}'.encode()).digest()
    return candidates[int.from_bytes(digest[:8], 'big') % len(candidates)]


def _with_identity(table):
    return lambda x, y: Decimal(1) if x == y else table.get((x, y), Decimal(0))


# ---- the four transforms -------------------------------------------------------------------------------------

def unfold(fixture, limit=ce.ROW_LIMIT):
    """MR1: every bom node keeps its id for its first instance (roots in id order, depth first in row order);
    every further instance is a fresh copy of the part's rows."""
    rows, order = _bom(fixture)
    reach = Counter()   # number of root paths reaching each node (roots: 1)
    roots = [node for node in order if not any(row[2] == node for row in rows)]
    for root in roots:
        reach[root] = 1
    for node in order:
        for _index, parent, child, _quantity in rows:
            if parent == node:
                reach[child] += reach[node]
    instance_edges = sum(reach[parent] for _index, parent, _child, _quantity in rows)
    others = sum(len(value) for table, value in fixture.items() if table != 'bom_line')
    if instance_edges + others > limit:
        raise NotApplicable('size_bound')
    used = _ids(fixture)
    tables = _copy(fixture)
    tables['bom_line'] = []
    copies, taken = [], set()
    parts = {}
    for row in fixture['part']:
        parts.setdefault(row[0], []).append(row)
    for root in roots:
        taken.add(root)
        stack = [(root, root)]
        while stack:
            instance, original = stack.pop()
            children = []
            for index, parent, child, _quantity in rows:
                if parent != original:
                    continue
                if child in taken:
                    copy_id = _fresh('sxc_mr1_', used)
                    copies.append([copy_id, child])
                    tables['part'] += [[copy_id] + list(row[1:]) for row in parts.get(child, [])]
                else:
                    copy_id = child
                    taken.add(child)
                line = fixture['bom_line'][index]
                tables['bom_line'].append([instance, copy_id, line[2], line[3]])
                children.append((copy_id, child))
            stack.extend(reversed(children))
    if sum(len(value) for value in tables.values()) > limit:
        raise NotApplicable('size_bound')
    mapping = dict(copies)

    @_arith
    def compare(original, transformed):
        origin = lambda identifier: mapping.get(identifier, identifier)
        sums = {}
        for (ancestor, descendant), value in transformed.items():
            key = (ancestor, origin(descendant))
            sums[key] = sums.get(key, Decimal(0)) + value
        expected = {}
        ancestors = {ancestor for ancestor, _ in sums} | set(mapping) | {ancestor for ancestor, _ in original}
        for ancestor in ancestors:
            for (source, descendant), value in original.items():
                if source == origin(ancestor):
                    expected[(ancestor, descendant)] = value
        # A copy of x below an instance of x would be a cycle: the original reflexive pair is 0, not skipped.
        keys = set(sums) | set(expected)
        lookup = lambda key: expected.get(key, Decimal(0))
        return (len(keys), sum(1 for key in keys if not _same(lookup(key), sums.get(key, Decimal(0)))))
    return _plan(tables, {'relation': 'mr1_unfold', 'copies': copies}, compare)


def scale(fixture, row, k=K):
    """MR2: multiply the quantity of bom_line row `row` by the integer k."""
    rows, _order = _bom(fixture)
    _index, u, v, q = rows[row]
    tables = _copy(fixture)
    with localcontext() as context:
        context.prec = PRECISION
        tables['bom_line'][row][3] = str(q * k)

    @_arith
    def compare(original, transformed):
        q_of = _with_identity(original)
        def expected(key):
            a, d = key
            return original.get(key, Decimal(0)) + (k - 1) * q * q_of(a, u) * q_of(v, d)
        keys = set(original) | set(transformed)
        keys |= {(a, d) for a in {u} | {x for x, y in original if y == u}
                 for d in {v} | {y for x, y in original if x == v}}
        return _diff(expected, transformed, keys)
    return _plan(tables, {'relation': 'mr2_scale', 'bom_line_row': row, 'k': k}, compare)


def disjoint(fixture, numbers):
    """MR3: a fresh diamond F0 -> F1 -> F3, F0 -> F2 -> F3 with the four given quantities."""
    used = _ids(fixture)
    fresh = [_fresh('sxc_mr3_', used) for _ in range(4)]
    tables = _copy(fixture)
    tables['part'] += [_synthetic_part(identifier) for identifier in fresh]
    edges = ((0, 1), (1, 3), (0, 2), (2, 3))
    lines = [[fresh[a], fresh[b], number + 1, str(numbers[number])] for number, (a, b) in enumerate(edges)]
    tables['bom_line'] += lines
    new = set(fresh)

    @_arith
    def compare(original, transformed):
        keys = {key for key in set(original) | set(transformed) if not (key[0] in new and key[1] in new)}
        return _diff(lambda key: Decimal(0) if new & set(key) else original.get(key, Decimal(0)), transformed, keys)
    return _plan(tables, {'relation': 'mr3_disjoint', 'parts': fresh, 'bom_lines': lines}, compare)


def unit_node(fixture, row):
    """MR4: replace bom_line row `row` (u, v, q) by (u, w, q) and (w, v, 1) through a fresh part w."""
    rows, _order = _bom(fixture)
    _index, u, v, q = rows[row]
    w = _fresh('sxc_mr4_', _ids(fixture))
    tables = _copy(fixture)
    line = tables['bom_line'][row]
    tables['bom_line'][row] = [u, w, line[2], line[3]]
    tables['bom_line'].append([w, v, 1, '1'])
    tables['part'].append(_synthetic_part(w))

    @_arith
    def compare(original, transformed):
        q_of = _with_identity(original)
        def expected(key):
            a, d = key
            if d == w:
                return q_of(a, u) * q
            if a == w:
                return q_of(v, d)
            return original.get(key, Decimal(0))
        keys = set(original) | set(transformed)
        keys |= {(a, w) for a in {u} | {x for x, y in original if y == u}}
        keys |= {(w, d) for d in {v} | {y for x, y in original if x == v}}
        return _diff(expected, transformed, keys)
    return _plan(tables, {'relation': 'mr4_unit_node', 'bom_line_row': row, 'node': w}, compare)


def transforms(fixture, seed, case_index, limit=ce.ROW_LIMIT):
    """{relation: Plan, or the not_run reason}; deterministic in the fixture, seed and case index."""
    try:
        rows, order = _bom(fixture)
    except NotApplicable as vacuous:
        return {name: vacuous.reason for name in RELATIONS}
    counts = _path_counts(rows, order)
    reaches = lambda x, y: x == y or counts[x][y] > 0
    diamonds = [(a, d) for a in order for d, number in counts[a].items() if number >= 2]
    plans = {}
    try:
        plans['mr1_unfold'] = unfold(fixture, limit) if diamonds else 'no_diamond'
    except NotApplicable as vacuous:
        plans['mr1_unfold'] = vacuous.reason
    multi = [index for index, u, v, q in rows if q != 0 and any(reaches(a, u) and reaches(v, d) for a, d in diamonds)]
    plans['mr2_scale'] = scale(fixture, _pick(multi, seed, case_index, 'mr2_scale')) if multi else 'no_multi_path_edge'
    digest = hashlib.sha256(f'{ce.VERSION}|metamorphic|{seed}|{case_index}|mr3_disjoint'.encode()).digest()
    plans['mr3_disjoint'] = disjoint(fixture, [1 + value % 9 for value in digest[:4]])
    non_unit = [index for index, _u, _v, q in rows if q != 1]
    plans['mr4_unit_node'] = (unit_node(fixture, _pick(non_unit, seed, case_index, 'mr4_unit_node'))
                              if non_unit else 'no_non_unit_quantity')
    return plans


def specs(plans):
    return {name: (plan.spec if isinstance(plan, Plan) else None) for name, plan in plans.items()}


# ---- one case ------------------------------------------------------------------------------------------------

def check_case(conn, fixture, sql, timeout_ms, profile, seed, case_index, limit=ce.ROW_LIMIT):
    """{relation: {status, reason, pairs_compared, mismatched_pairs, transform_sha256}} for one fixture.

    Raises ShapeError for a non-closure answer and lets query errors propagate (both are errors, not verdicts).
    """
    plans = transforms(fixture, seed, case_index, limit)
    outcome = {name: {'status': 'not_run', 'reason': plan, 'pairs_compared': 0, 'mismatched_pairs': 0,
                      'transform_sha256': None} for name, plan in plans.items() if not isinstance(plan, Plan)}
    runnable = [name for name in RELATIONS if isinstance(plans[name], Plan)]
    if not runnable:
        return outcome
    try:
        original = closure_map(*answer(conn, fixture, sql, timeout_ms, profile))
    except SizeBound:
        for name in runnable:
            outcome[name] = {'status': 'not_run', 'reason': 'size_bound', 'pairs_compared': 0, 'mismatched_pairs': 0,
                             'transform_sha256': plans[name].sha256}
        return outcome
    for name in runnable:
        plan = plans[name]
        entry = {'status': 'not_run', 'reason': None, 'pairs_compared': 0, 'mismatched_pairs': 0,
                 'transform_sha256': plan.sha256}
        outcome[name] = entry
        if sum(len(value) for value in plan.tables.values()) > limit:
            entry['reason'] = 'size_bound'
            continue
        try:
            constraints.validate_fixture(plan.tables, ce.SCHEMA, None)
        except constraints.ConstraintViolation:
            entry['reason'] = 'type_domain'   # e.g. a scaled quantity beyond numeric(18,6)
            continue
        try:
            constraints.validate_fixture(plan.tables, ce.SCHEMA, profile)
        except constraints.ConstraintViolation:
            entry['reason'] = 'integrity_profile'
            continue
        try:
            transformed = closure_map(*answer(conn, plan.tables, sql, timeout_ms, profile))
        except SizeBound:
            entry['reason'] = 'size_bound'
            continue
        try:
            compared, mismatched = plan.compare(original, transformed)
        except NotApplicable as vacuous:
            entry['reason'] = vacuous.reason
            continue
        entry.update(pairs_compared=compared, mismatched_pairs=mismatched)
        if compared == 0:
            entry['reason'] = 'no_pairs'
        else:
            entry['status'], entry['reason'] = ('fail' if mismatched else 'pass'), None
    return outcome


# ---- stage ---------------------------------------------------------------------------------------------------

def _replay(raw, seed, digest, profile):
    try:
        payload = constraints.decode_json(raw)
        if (payload['format'] != FORMAT or type(payload['schema_version']) is not int or payload['schema_version'] != ce.VERSION
                or type(payload['generator_version']) is not int or payload['generator_version'] != ce.VERSION
                or payload['synthetic'] is not True or payload['scenario'] != SCENARIO
                or type(payload['seed']) is not int or payload['seed'] != seed
                or payload['sql_sha256'] != {'metamorphic': digest}):
            raise ValueError('Replay contract')
        binding = payload['constraints']
        if not isinstance(binding, dict) or binding != constraints.binding(profile) or type(binding.get('version')) is not int:
            raise ValueError('Replay constraint profile/mode')
        index = ce._int(payload['case_index'], 0, 999, 'case_index')
        tables = payload['tables']
        constraints.validate_fixture(tables, ce.SCHEMA, profile)
        original = ce.generate(SCENARIO, seed, index)
        for table in ce.SCHEMA:
            available = Counter(json.dumps(row, sort_keys=True) for row in original[table])
            requested = Counter(json.dumps(row, sort_keys=True) for row in tables[table])
            if requested - available:
                raise ValueError('Replay contains non-generated rows')
        # The transforms are recomputed, never taken from the file: the recorded ones must be exactly these.
        recomputed = json.loads(json.dumps(specs(transforms(tables, seed, index))))
        failed = payload['failed']
        if (payload['transforms'] != recomputed or not isinstance(failed, list) or not failed
                or not set(failed) <= set(RELATIONS) or any(recomputed[name] is None for name in failed)):
            raise ValueError('Replay transforms')
        return payload
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, StageError):
        raise StageError('counterexample: replay is not a matching generated metamorphic fixture', EXIT_CONFIG) from None


def _not_proven(result, profile):
    result.not_proven.append('Trusted SQL may explicitly reference real tables; CTE fixtures are not a SQL sandbox or proof of source-independent execution.')
    if profile is None:
        result.not_proven.append('Synthetic fixture scope is unconstrained: no declared PK, UNIQUE, NOT NULL, foreign key or CHECK domain is enforced.')
    else:
        result.not_proven.append('Only this exact version 1 integrity profile is enforced; it does not represent every PostgreSQL DDL feature or production data rule.')
        result.not_proven.append('Constrained fixture text columns use PostgreSQL C collation and exact equality; production collation behavior is not reproduced and ordered text CHECKs are rejected.')
    result.not_proven.append('Metamorphic relations only test consistency with BOM path-product semantics: errors invariant under the four transforms '
                             '(edge direction, a wrong quantity column, a filter mapping) are not detected; '
                             'no gold query or business rule is checked.')
    result.not_proven.append('Only the given SQL file is tested; the generated closure SQL is tied to gold only through V1/V3/V5 on real data, '
                             'and passing relations do not upgrade any other check.')
    result.not_proven.append('Bounded generated bom_diamond fixtures share one diamond topology with seeded quantities; '
                             'reflexive (x, x) answer rows are not compared.')


def query_errors():
    """Exceptions meaning the trusted SQL could not be answered: the driver's (pg8000.exceptions.Error, database errors
    with their SQLSTATE and interface errors) and OSError (a reset or timed-out connection), the database part of
    compare.expected_errors. Anything else is a fault of this code: it propagates and sxc.main records stage:error."""
    try:
        from pg8000.exceptions import Error as PgError
    except ImportError:
        return (OSError,)
    return (OSError, PgError)


def run(cfg, args, out):
    """Metamorphic mode of the counterexample stage: one trusted closure SQL, four relations per generated case."""
    seed = ce._int(args.seed, -(2**63), 2**63 - 1, 'seed')
    maximum = ce._int(args.max_examples, 1, 1000, 'max_examples')
    timeout = ce._int(getattr(args, 'statement_timeout_ms', 10000), 1000, 120000, 'statement_timeout_ms')
    if getattr(args, 'reference_sql', None) is not None or getattr(args, 'candidate_sql', None) is not None:
        raise StageError('counterexample: --metamorphic-sql checks one SQL file; do not combine it with '
                         '--reference-sql or --candidate-sql', EXIT_CONFIG)
    if (getattr(args, 'scenario', None) or SCENARIO) != SCENARIO:
        raise StageError('counterexample: --metamorphic-sql supports only --scenario bom_diamond', EXIT_CONFIG)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.dsn_env):
        raise StageError('counterexample: invalid DSN environment variable name', EXIT_CONFIG)
    profile_path = getattr(args, 'constraints', None)
    profile = constraints.load(profile_path, ce.SCHEMA) if profile_path is not None else None
    sql, sql_sha = ce._read_sql(args.metamorphic_sql)
    replay = None
    if args.replay:
        try:
            replay_path = Path(args.replay)
            if replay_path.stat().st_size > 1024 * 1024:
                raise ValueError('Replay file size')
            replay = _replay(replay_path.read_text(encoding='utf-8'), seed, sql_sha, profile)
        except (OSError, UnicodeError, ValueError):
            raise StageError('counterexample: replay file missing, too large or invalid', EXIT_CONFIG) from None
    directory = Path(out) / 'counterexample'
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / 'fixture.json'
    artifact_path.unlink(missing_ok=True)
    result = Result('counterexample')
    _not_proven(result, profile)
    tally = {name: {'cases_run': 0, 'reasons': Counter(), 'pairs': 0, 'mismatched': 0, 'hashes': []} for name in RELATIONS}
    checked = attempted = invalid_domain = 0
    found = None
    conn = None
    try:
        for index in ([replay['case_index']] if replay else range(maximum)):
            fixture = replay['tables'] if replay else ce.generate(SCENARIO, seed, index)
            attempted += 1
            try:
                constraints.validate_fixture(fixture, ce.SCHEMA, profile)
            except constraints.ConstraintViolation:
                invalid_domain += 1
                continue
            if conn is None:
                conn = db.connect(args.dsn_env)
            try:
                outcome = check_case(conn, fixture, sql, timeout, profile, seed, index)
            except ShapeError as shape:
                result.add(SHAPE_ID, 'error', '', reason=shape.reason, case_index=index, examples=checked)
                result.next_safe_action = 'Return one row per (ancestor, descendant) with text ids and an exact numeric qty; no fixture was saved.'
                return result
            checked += 1
            for name in RELATIONS:
                entry, counts = outcome[name], tally[name]
                if entry['status'] == 'not_run':
                    counts['reasons'][entry['reason']] += 1
                    continue
                counts['cases_run'] += 1
                counts['pairs'] += entry['pairs_compared']
                counts['mismatched'] += entry['mismatched_pairs']
                counts['hashes'].append(entry['transform_sha256'])
            failed = [name for name in RELATIONS if outcome[name]['status'] == 'fail']
            if failed:
                found = (index, fixture, outcome, failed)
                break
    except StageError:
        raise
    except query_errors() as error:   # error text can quote data: keep only the type and SQLSTATE
        arg = error.args[0] if error.args else None
        code = arg.get('C', '') if isinstance(arg, dict) else ''
        result.add('counterexample:query', 'error', '', exception=type(error).__name__,
                   sqlstate=code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else '', examples=checked)
        result.next_safe_action = 'Fix the query or environment; an execution error is not a relation verdict and no fixture was saved.'
        return result
    finally:
        if conn is not None:
            conn.close()
    # An edited SQL file or profile must neither pass nor produce a falsely digest-bound replay.
    if ce._read_sql(args.metamorphic_sql)[1] != sql_sha:
        result.add('counterexample:inputs', 'error', '', reason='sql_changed_during_run')
        result.next_safe_action = 'Rerun with an unchanged SQL file; no verdict or fixture was recorded.'
        return result
    if profile is not None and not profile.unchanged():
        result.add('counterexample:inputs', 'error', '', reason='constraint_profile_changed_during_run')
        result.next_safe_action = 'Restore the exact constraint profile or rerun with the changed profile; no fixture was saved.'
        return result
    common = dict(seed=seed, k=K, examples=checked, attempted_examples=attempted, invalid_domain_examples=invalid_domain,
                  constraints_mode=constraints.binding(profile)['mode'], constraint_profile_sha256=profile.sha256 if profile else None)
    fixture_sha = None
    if found is not None:
        index, fixture, outcome, failed = found
        payload = {'format': FORMAT, 'schema_version': ce.VERSION, 'synthetic': True, 'generator_version': ce.VERSION,
                   'scenario': SCENARIO, 'seed': seed, 'case_index': index, 'sql_sha256': {'metamorphic': sql_sha},
                   'constraints': constraints.binding(profile), 'tables': fixture, 'failed': failed,
                   'transforms': specs(transforms(fixture, seed, index)),
                   'scope': 'Original generated rows and the deterministic transform specification of a violated BOM '
                            'metamorphic relation for this trusted SQL; no gold comparison or general correctness claim.'}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        temporary = directory / 'fixture.json.part'
        temporary.write_bytes(serialized)
        os.replace(temporary, artifact_path)
        fixture_sha = hashlib.sha256(serialized).hexdigest()
    for name in RELATIONS:
        counts = tally[name]
        detail = dict(relation=name, cases_run=counts['cases_run'], cases_not_run=sum(counts['reasons'].values()),
                      not_run_reasons=dict(sorted(counts['reasons'].items())), pairs_compared=counts['pairs'],
                      mismatched_pairs=counts['mismatched'], **common)
        if found is not None and name in found[3]:
            index, _fixture, outcome, _failed = found
            detail.update(case_index=index, case_pairs_compared=outcome[name]['pairs_compared'],
                          case_mismatched_pairs=outcome[name]['mismatched_pairs'],
                          transform_sha256=outcome[name]['transform_sha256'], fixture_sha256=fixture_sha)
            result.add(CHECK_IDS[name], 'fail', '', **detail)
        elif counts['cases_run']:
            detail['transform_sha256'] = hashlib.sha256(''.join(counts['hashes']).encode()).hexdigest()
            result.add(CHECK_IDS[name], 'pass', '', **detail)
        else:
            detail['reason'] = 'no_admissible_generated_examples' if checked == 0 else 'vacuous_for_every_case'
            result.add(CHECK_IDS[name], 'not_run', '', **detail)
    if found is not None:
        result.next_safe_action = ('Replay fixture.json with the same --metamorphic-sql file, --seed and profile, then review how '
                                   'the SQL combines paths (sum over all paths of the product of qty_per).')
    elif result.status == 'pass':
        result.next_safe_action = 'Keep V1/V3/V5 gold comparisons as correctness evidence; the relations only bound this SQL file.'
    else:
        result.next_safe_action = 'Vacuous relations were not run: review not_run_reasons; nothing was proven for them.'
    return result
