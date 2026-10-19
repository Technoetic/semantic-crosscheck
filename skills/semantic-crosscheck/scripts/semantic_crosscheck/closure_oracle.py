"""Bounded, independent closure verification from raw edges using exact Python arithmetic.

The builder aggregates frontiers by depth in PostgreSQL. This oracle instead folds descendant summaries in reverse
topological order, using Fraction rather than Decimal's context-sensitive arithmetic. Duplicate source lines form
one summed logical edge; NULL quantities still contribute paths but only non-NULL products contribute to SUM.
No generated SQL, temporary builder table or recomputation helper participates in the expected result.
expected_quantities() exposes the quantities of the same fold to the counterexample stage (bom_exhaustive oracle).

Resource limits and unsupported inputs produce not_run, never a partial PASS. Cycles fail error mode. Report mode
removes every logical edge whose parent and child lie in the same strongly connected component (self-loops
included), the builder's rule, found here independently by an iterative Tarjan search (Tarjan, "Depth-First Search
and Linear Graph Algorithms", SIAM J. Comput. 1(2), 1972) charged to the operation budget, and then folds what is
left. The removal counts are informational only: the verdict is the exact closure comparison, never agreement with
build's statistics. Results contain counts and fixed reasons only, never endpoint keys or quantities. run() reads
raw edges and the generated closure in one short READ ONLY, REPEATABLE READ transaction, without waiting for Cube.
"""

from collections import deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from . import compare, db, ident, naming


@dataclass(frozen=True)
class Limits:
    max_raw_rows: int = 50_000
    max_closure_rows: int = 100_000
    max_nodes: int = 10_000
    max_pairs: int = 100_000
    max_operations: int = 2_000_000
    max_numeric_bits: int = 65_536
    max_decimal_digits: int = 4_096


class _NotProven(Exception):
    def __init__(self, reason, **detail):
        self.detail = {"reason": reason, **detail}


class _DepthLimit(Exception):
    pass


class _CycleError(Exception):
    pass


class _InternalError(Exception):
    """An invariant of the oracle itself broke; reported as error, never folded into a verdict."""

    def __init__(self, reason):
        self.reason = reason


class _Budget:
    def __init__(self, limits):
        self.limits, self.operations = limits, 0

    def check(self, field, amount):
        if amount > getattr(self.limits, field):
            raise _NotProven("budget_exceeded", limit=field)

    def step(self):
        self.operations += 1
        self.check("max_operations", self.operations)

    def number(self, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise _NotProven("unsupported_quantity")
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise _NotProven("unsupported_quantity")
            _sign, digits, exponent = value.as_tuple()
            # Check before Fraction materializes powers of ten (Decimal accepts enormous exponents).
            self.check("max_decimal_digits", len(digits) + abs(exponent))
        else:
            self.check("max_numeric_bits", value.bit_length())
        result = Fraction(value)
        self.fraction(result)
        return result

    def fraction(self, value):
        if value is not None:
            self.check("max_numeric_bits", max(value.numerator.bit_length(), value.denominator.bit_length()))

    def add(self, a, b):
        # SQL SUM ignores NULL, and an all-NULL group remains NULL.
        if a is None or b is None:
            return b if a is None else a
        result = a + b
        self.fraction(result)
        return result

    def multiply(self, a, b):
        if a is None or b is None:
            return None
        # Bounds temporary products too; a conservative refusal is preferable to unbounded allocation.
        self.check("max_numeric_bits", max(a.numerator.bit_length() + b.numerator.bit_length(),
                                           a.denominator.bit_length() + b.denominator.bit_length()))
        result = a * b
        self.fraction(result)
        return result


def _key(value):
    if not isinstance(value, (str, int, Decimal, UUID, date)):
        raise _NotProven("unsupported_key")
    if isinstance(value, Decimal) and not value.is_finite():
        raise _NotProven("unsupported_key")
    return value


def _topological(nodes, logical, budget):
    """(children, Kahn's order); the order is shorter than nodes when a cycle (a self-loop included) remains."""
    children, indegree = {}, dict.fromkeys(nodes, 0)
    for (parent, child), quantity in logical.items():
        budget.step()
        children.setdefault(parent, []).append((child, quantity))
        indegree[child] += 1
    ready, order = deque(n for n, degree in indegree.items() if not degree), []
    while ready:
        node = ready.popleft()
        order.append(node)
        for child, _quantity in children.get(node, ()):
            budget.step()
            indegree[child] -= 1
            if not indegree[child]:
                ready.append(child)
    return children, order


def _strong_components(nodes, children, budget):
    """Tarjan's strongly connected components with an explicit stack (no recursion): {node: component id}.

    Every node visit, edge inspection and stack pop is one budgeted step, so the search is O(V + E) and bounded.
    """
    index, low, component, stack, on_stack = {}, {}, {}, [], set()

    def visit(node):
        budget.step()
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        return node, iter(children.get(node, ()))

    for root in nodes:
        if root in index:
            continue
        work = [visit(root)]
        while work:
            node, pending = work[-1]
            for child, _quantity in pending:
                budget.step()
                if child not in index:
                    work.append(visit(child))
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    while True:
                        budget.step()
                        member = stack.pop()
                        on_stack.discard(member)
                        component[member] = index[node]
                        if member == node:
                            break
    return component


def _expected(edges, max_depth, on_cycle, budget, detail):
    if on_cycle not in ("error", "report"):
        raise _NotProven("unsupported_on_cycle")   # refused whatever the graph, not only when it has a cycle
    logical, nodes = {}, set()
    nulls = 0
    for parent, child, quantity in edges:
        budget.step()
        if parent is None or child is None:
            nulls += 1
            continue
        pair = (_key(parent), _key(child))
        nodes.update(pair)
        budget.check("max_nodes", len(nodes))
        logical[pair] = budget.add(logical.get(pair), budget.number(quantity))
    detail.update(logical_edges=len(logical), null_endpoint_lines=nulls,
                  null_quantity_edges=sum(q is None for q in logical.values()), nodes=len(nodes))
    children, order = _topological(nodes, logical, budget)
    if len(order) != len(nodes):
        if on_cycle == "error":
            raise _CycleError()
        component = _strong_components(nodes, children, budget)
        inside, looped, sizes = [], set(), {}
        for parent, child in logical:
            budget.step()
            if component[parent] == component[child]:
                inside.append((parent, child))
                if parent == child:
                    looped.add(parent)
        for node in nodes:
            budget.step()
            sizes[component[node]] = sizes.get(component[node], 0) + 1
        for pair in inside:
            del logical[pair]
        # The builder's cycle_nodes: nodes that reach themselves (a component of two or more nodes, or a self-loop).
        detail.update(cycle_handling="scc_edge_removal", dropped_cycle_edges=len(inside),
                      cycle_nodes=sum(sizes[component[n]] > 1 or n in looped for n in nodes))
        children, order = _topological(nodes, logical, budget)
        if len(order) != len(nodes):
            raise _InternalError("scc_removal_left_cycle")

    summaries, pairs = {}, 0
    for parent in reversed(order):
        descendants = {}

        def merge(child, minimum, maximum, paths, quantity):
            nonlocal pairs
            budget.step()
            if maximum > max_depth:
                raise _DepthLimit()
            previous = descendants.get(child)
            if previous is None:
                pairs += 1
                budget.check("max_pairs", pairs)
                descendants[child] = (minimum, maximum, paths, quantity)
            else:
                lo, hi, count, total = previous
                count += paths
                budget.check("max_numeric_bits", count.bit_length())
                descendants[child] = (min(lo, minimum), max(hi, maximum), count, budget.add(total, quantity))

        for child, quantity in children.get(parent, ()):
            merge(child, 1, 1, 1, quantity)
            for descendant, (minimum, maximum, paths, total) in summaries[child].items():
                merge(descendant, minimum + 1, maximum + 1, paths, budget.multiply(quantity, total))
        summaries[parent] = descendants
    return {(a, d): metrics for a, descendants in summaries.items() for d, metrics in descendants.items()}


class OracleUnavailable(ValueError):
    """The fold cannot give an exact expected answer; `reason` is a fixed word, never a key or a value."""

    def __init__(self, reason):
        super().__init__(f"closure oracle unavailable: {reason}")
        self.reason = reason


def expected_quantities(edges, *, limits=None):
    """{(ancestor, descendant): Fraction} of raw (parent, child, quantity) lines, from the same fold compare_rows uses.

    Duplicate lines are one summed logical edge; reflexive pairs are absent (an acyclic graph has none). A pair whose
    paths all carry a NULL quantity maps to None, as SQL SUM would. Raises OracleUnavailable with reason `cycle`,
    `budget_exceeded`, `unsupported_quantity` or `unsupported_key`; nothing partial is returned.
    """
    budget = _Budget(limits or Limits())
    try:
        budget.check("max_raw_rows", len(edges))
        # A path in a DAG uses each logical edge at most once, so this depth bound never truncates.
        expected = _expected(edges, len(edges), "error", budget, {})
    except _NotProven as e:
        raise OracleUnavailable(e.detail["reason"]) from None
    except _CycleError:
        raise OracleUnavailable("cycle") from None
    return {pair: metrics[3] for pair, metrics in expected.items()}


def compare_rows(edges, rows, *, max_depth=32, on_cycle="error", limits=None):
    """Compare raw (parent, child, quantity) lines and CLOSURE_COLUMNS tuples; output counts only."""
    limits = limits or Limits()
    budget = _Budget(limits)
    detail = {"algorithm": "python_exact_reverse_topological", "raw_edge_rows": len(edges), "actual_rows": len(rows)}
    try:
        budget.check("max_raw_rows", len(edges))
        budget.check("max_closure_rows", len(rows))
        expected = _expected(edges, max_depth, on_cycle, budget, detail)
        actual, duplicates = {}, 0
        for ancestor, descendant, minimum, maximum, paths, quantity in rows:
            budget.step()
            # A NULL closure endpoint cannot match any valid edge path, but is still an extra actual pair.
            pair = tuple(None if x is None else _key(x) for x in (ancestor, descendant))
            duplicates += pair in actual
            actual[pair] = tuple(budget.number(x) for x in (minimum, maximum, paths, quantity))
        common = expected.keys() & actual.keys()
        mismatches = {}
        for column, position in (("min_depth", 0), ("max_depth", 1), ("path_count", 2), ("qty", 3)):
            count = sum(expected[pair][position] != actual[pair][position] for pair in common)
            if count:
                mismatches[column] = count
        detail.update(expected_pairs=len(expected), matched_pairs=len(common), missing_pairs=len(expected.keys() - actual.keys()),
                      extra_pairs=len(actual.keys() - expected.keys()), duplicate_pairs=duplicates, mismatched_columns=mismatches,
                      empty_graph=not expected, operations=budget.operations)
        failed = detail["missing_pairs"] or detail["extra_pairs"] or duplicates or mismatches
        return ("fail" if failed else "pass"), detail
    except _NotProven as e:
        return "not_run", {**detail, **e.detail, "operations": budget.operations}
    except _DepthLimit:
        return "fail", {**detail, "reason": "depth_limit", "max_depth": max_depth, "operations": budget.operations}
    except _CycleError:
        return "fail", {**detail, "reason": "cycle", "operations": budget.operations}
    except _InternalError as e:
        return "error", {**detail, "reason": e.reason, "operations": budget.operations}


def _supported_keys(conn, edge, h):
    """Python equality is safe only for these built-in, same-type PostgreSQL keys with deterministic collations.

    In particular bpchar's trailing-space equality, citext, domains and nondeterministic collations are refused.
    Metadata reads classify supported input; they never derive reachability or expected metrics.
    """
    rows = conn.run("""SELECT a.attname, t.typname, coalesce(c.collisdeterministic, true)
        FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
        LEFT JOIN pg_collation c ON c.oid = a.attcollation
        WHERE a.attrelid = to_regclass(:table) AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attname IN (:parent, :child)""", table=edge, parent=h.parent, child=h.child)
    supported = {"int2", "int4", "int8", "numeric", "text", "varchar", "uuid", "date", "timestamp", "timestamptz", "bool"}
    by_name = {name: (kind, deterministic) for name, kind, deterministic in rows}
    p, c = by_name.get(h.parent), by_name.get(h.child)
    return bool(p and c and p == c and p[0] in supported and p[1])


def run(conn, cfg, h, *, limits=None):
    """Read both inputs in one snapshot and compare them; safe sanitized errors use existing compare conventions."""
    limits = limits or Limits()
    edge = cfg.entity(h.edge, f"hierarchies.{h.name}").table
    target = naming.closure_table(cfg, h.name)
    try:
        with db.read_only(conn, timeout_ms=cfg.source.statement_timeout_ms, isolation="repeatable read"):
            if not _supported_keys(conn, edge, h):
                return "not_run", {"reason": "unsupported_key_type_or_collation"}
            raw = conn.run(f"SELECT {ident.quote(h.parent)}, {ident.quote(h.child)}, {ident.quote(h.quantity)} "
                           f"FROM {ident.qualified(edge)} LIMIT :row_limit", row_limit=limits.max_raw_rows + 1)
            actual = conn.run(f"SELECT {', '.join(naming.CLOSURE_COLUMNS)} FROM {target} LIMIT :row_limit",
                              row_limit=limits.max_closure_rows + 1)
            return compare_rows(raw, actual, max_depth=h.max_depth, on_cycle=h.on_cycle, limits=limits)
    except compare.expected_errors() as e:
        return "error", {"reason": "closure_oracle_query_failed", **compare.error_detail(e)}
