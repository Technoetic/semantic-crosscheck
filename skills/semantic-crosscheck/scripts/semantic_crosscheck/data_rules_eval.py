"""Data rules, path 2: an independent Python evaluation of one rule over typed rows, without SQL.

The data-rules stage evaluates every rule twice on one snapshot: as compiled PostgreSQL SQL (path 1) and here, over
the rows the stage exported in the same transaction (path 2), and then compares the two canonical output multisets.
This module never sees SQL or the compiler; it implements the semantic contract of the rule file on its own.

evaluate(rule, tables, hierarchy, sets, refs, plan=None) returns an Outcome or NotRun:
  rule       one rule of the validated rule file (a data_rules.Rule, read through its attributes)
  tables     entity -> list of row dicts, column -> str, int, Decimal, bool or None, as the export delivers them
             (bpchar already without its pad spaces); every entity and column the rule reads must be present
  hierarchy  the rule file's hierarchy (data_rules.Hierarchy) or None; sets and refs as the rule file holds them
  plan       optional catalog type plan (data_rules.TypePlan); with it, every column the rule reads must hold values
             of its type class, and bpchar values must have arrived without trailing pad spaces
Outcome(columns, rows) holds the output column names and a Counter of canonical output tuples (duplicates count).
NotRun(reason) means a gate tripped; the reasons are those of the stage's SQL gates (GATE_REASONS). A value this
module cannot read exactly (a float, a column holding values of two types, a constant of another type) raises
TypeError, and an inexact arithmetic step raises decimal.Inexact: evaluator errors, never an answer.

The semantic contract, as implemented here:
- Values: text compares exactly (no trimming, case folding or Unicode normalization); numbers compare by value
  (2.0 = 2.000); booleans compare as booleans. Output cells are canonical: numbers as canon numbers (exact, without
  trailing zeros), text exactly, booleans as bool, NULL as None.
- NULL: a comparison atom with a NULL operand is false, for eq, ne, lt, le, gt, ge, in, not_in, starts_with and
  not_in_ref alike; is_null and is_not_null test NULL explicitly. NULL is never a violation otherwise.
- Edges are rows of the hierarchy edge entity whose parent and child are both non-NULL. has_children(x): an edge
  with parent x; has_parents(x): an edge with child x; in_hierarchy: either. Structure needs no node row.
- Endpoints: exists = false holds for a non-NULL endpoint without a node row, exists = true for a non-NULL endpoint
  with one. An endpoint attribute holds only for a non-NULL endpoint whose node row exists and satisfies the
  comparison. Endpoint structure is false for a NULL endpoint.
- Quantifiers range over the distinct neighbours along x's edges: all needs at least one neighbour and every
  neighbour existing and satisfying the sub-where; any needs one existing neighbour that satisfies it; none needs
  that no existing neighbour satisfies it (so it holds without neighbours).
- Reports: key (the entity's key columns), distinct (the non-NULL values of one column) or columns (the listed
  columns, the kind's derived columns and n, the number of selected rows with that content).
- duplicate: a row joins a group only when every key part is non-NULL (an endpoint part needs an existing endpoint
  row with a non-NULL attribute); the size counts rows or the distinct non-NULL values of a column; groups whose size
  is at least min qualify.
- mode_outlier: rows group by non-NULL group_by; N counts every row of a group, NULL values included; the mode is
  the most frequent non-NULL value and makes the group eligible when count(mode) * den >= num * N (min_mode_share =
  num/den > 1/2, so the mode is unique), mode > 0 and, with min_distinct, the column has at least n distinct
  non-NULL values in the group; a value v is flagged when v >= ratio * mode or ratio * v <= mode.
- same_signature: members are node rows with a child edge; the signature is the set of (child, sum of the
  quantities of the member's lines to that child); attributes compare exactly, NULL equal to NULL; classes of at
  least min_members members qualify; group_id is the smallest member (code point order for text, numeric order for
  numbers).
- stored_metric: the height is 0 without child edges, else 1 + the largest height of the children (a child without a
  node row is a leaf), found by an iterative post-order search with a grey set. A cycle or a height above max_depth
  anywhere in the edge graph is NotRun. Every node row whose stored value is non-NULL and differs from its height is
  output with the kind of the difference (HEIGHT_KINDS).
- text_quantity: the endpoint row must exist and satisfy where_end; the pattern's Python form (compiled with
  re.DOTALL) is searched leftmost in the endpoint's text; capture 1 must be a plain decimal numeral X > 0 and capture 2
  (when the pattern has one) digits N > 0, or N = 1 when it did not take part; anything else is no match. A row with
  a non-NULL quantity q is flagged when |q * N - X| <= rel_tol * X; the output adds numerator X and divisor N.
- Arithmetic uses only +, -, * and comparisons, on Decimal with precision 60 and Inexact trapped.

Gates, in this order: non_finite_values (NaN or an infinity in a column the rule reads), key_null and key_not_unique
(the node key when the rule reads node rows by key, then the key of an entity the rule reports by key), null_values
(same_signature: an edge with a NULL quantity), value_too_long (text_quantity: a text longer than max_text_length in
the node entity) and cycle_or_depth_limit (stored_metric). They guard authored examples the same way.

Authored examples: example_tables() gives an example's input rows as the export would deliver them, expected() its
authored output as an Outcome and example_verdict() names how the SQL, Python and authored outputs of one example
differ. subjects() rolls the agreed outputs of the rules that name a subject up into the rows of the subjects view,
in its order; sort_key() and ordered() give the typed order of the lookup views.

Imports: the standard library and canon only, so an application that already vendors canon can add this file. The
rule file's objects are read through their attributes. Messages name entities, columns and types, never a value.
"""

import operator
import re
from collections import Counter
from dataclasses import dataclass
from decimal import (MAX_EMAX, MIN_EMIN, Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow,
                     localcontext)

from . import canon

PRECISION = 60
GATE_REASONS = ("non_finite_values", "key_null", "key_not_unique", "null_values", "value_too_long",
                "cycle_or_depth_limit")
SEVERITIES = ("violation", "warning")           # indexed by severity rank
HEIGHT_KINDS = ("has_children_stored_zero", "stored_below", "stored_above", "leaf_stored_nonzero",
                "outside_stored_nonzero")
TYPE_CLASSES = ("number", "text", "bpchar", "boolean")
EXAMPLE_VERDICTS = ("common_mode", "sql_mismatch", "py_mismatch", "both_mismatch")

_NUMERAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")   # a numeric constant or example cell written as a string
_AMOUNT = re.compile(r"[0-9]+(\.[0-9]+)?")      # text_quantity capture 1
_COUNT = re.compile(r"[0-9]+")                  # text_quantity capture 2
_COMPARE = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le, "gt": operator.gt,
            "ge": operator.ge}
_ORDER_OPS = ("lt", "le", "gt", "ge")
_TESTS = {**_COMPARE,
          "in": lambda value, members: value in members,
          "not_in": lambda value, members: value not in members,
          "starts_with": lambda value, prefixes: value.startswith(prefixes),
          "not_in_ref": lambda value, values: value not in values}
_EMPTY = frozenset()


def arithmetic() -> Context:
    """The context of every arithmetic step: precision 60 with Inexact trapped, and the exponent range at its limits
    so that precision alone decides. An inexact step raises decimal.Inexact instead of rounding."""
    return Context(prec=PRECISION, Emax=MAX_EMAX, Emin=MIN_EMIN,
                   traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])


@dataclass(frozen=True)
class Outcome:
    """A rule's output: the output column names and a Counter of canonical output tuples."""
    columns: tuple
    rows: Counter

    @property
    def outputs(self) -> int:
        """The number of distinct output tuples."""
        return len(self.rows)


@dataclass(frozen=True)
class NotRun:
    """A gate tripped, so the rule has no output on these rows; reason is one of GATE_REASONS."""
    reason: str


@dataclass(frozen=True)
class Subject:
    """One row of the subjects view: a node key, the worst severity of the rules that output it, that severity's rank,
    the number of those rules and their ids in configuration order."""
    subject: object
    severity: str
    severity_rank: int
    rule_count: int
    rules: tuple


def evaluate(rule, tables, hierarchy, sets, refs, plan=None):
    """Evaluate one rule over typed rows: an Outcome, or NotRun when a gate trips."""
    return Evaluator(tables, hierarchy, sets, refs, plan).evaluate(rule)


class Evaluator:
    """Evaluates rules over one set of typed rows. Input checks and indexes (node rows by key, edges, ref values) are
    built once and reused by every rule, so the rows must not change while the evaluator is in use."""

    def __init__(self, tables, hierarchy, sets, refs, plan=None):
        self.tables, self.h, self.sets, self.refs, self.plan = tables, hierarchy, sets, refs, plan
        self._rows_of: dict = {}
        self._columns: dict = {}       # (entity, column) -> (value class or "" when every value is NULL, finite)
        self._keys: dict = {}          # (entity, key columns) -> gate reason or ""
        self._ref_sets: dict = {}      # ref name -> (value class, frozenset of values)
        self._nodes = None             # node key -> node row
        self._edges = None             # (children by parent, parents by child)

    def evaluate(self, rule):
        with localcontext(arithmetic()):
            reason = self._gates(rule)
            if reason:
                return NotRun(reason)
            kind = _KINDS.get(rule.kind)
            if kind is None:
                raise ValueError(f"rule {rule.id}: unknown kind")
            return kind(self, rule)

    # ---- inputs and gates ----------------------------------------------------------------------------
    def _rows(self, entity: str):
        rows = self._rows_of.get(entity)
        if rows is None:
            rows = self.tables[entity]
            if not isinstance(rows, (list, tuple)):
                rows = list(rows)
            self._rows_of[entity] = rows
        return rows

    def _column(self, entity: str, column: str) -> tuple:
        """(value class, finite) of one column, checking every value once: str, int, Decimal, bool or None, one class
        per column, and with a type plan the column's own class (bpchar values without trailing pad spaces)."""
        found = self._columns.get((entity, column))
        if found is None:
            seen, finite, padded = "", True, False
            for row in self._rows(entity):
                value = row[column]
                if value is None:
                    continue
                cls = _value_class(value)
                if cls != seen:
                    if seen:
                        raise TypeError(f"entity {entity} column {column} holds values of more than one type")
                    seen = cls
                if cls == "number":
                    finite = finite and (not isinstance(value, Decimal) or value.is_finite())
                elif cls == "text":
                    padded = padded or value.endswith(" ")
            if self.plan is not None:
                _planned(self.plan, entity, column, seen, padded)
            found = self._columns[(entity, column)] = (seen, finite)
        return found

    def _class(self, entity: str, column: str) -> str:
        return self._column(entity, column)[0]

    def _gates(self, rule) -> str:
        non_finite = False
        for entity, columns in rule.reads.items():
            for column in columns:
                non_finite |= not self._column(entity, column)[1]
        self._check_types(rule)
        if non_finite:
            return "non_finite_values"
        for entity, columns in self._key_columns(rule):
            reason = self._key_gate(entity, columns)
            if reason:
                return reason
        if rule.kind == "same_signature" and self._null_edge_quantity():
            return "null_values"
        if rule.kind == "text_quantity" and self._too_long(rule.text, rule.max_text_length):
            return "value_too_long"
        return ""

    def _check_types(self, rule):
        """The value classes the rule relies on: endpoints and node keys alike, numbers and text where kinds compute."""
        h = self.h
        if h is None:
            return
        reads = rule.reads
        found = {self._class(h.edge, c) for c in (h.parent, h.child) if c in reads.get(h.edge, ())}
        if h.key in reads.get(h.node, ()):
            found.add(self._class(h.node, h.key))
        if len(found - {""}) > 1:
            raise TypeError(f"hierarchy {h.name}: the parent, child and node key columns hold values of different "
                            "types")
        wanted = []
        if rule.kind == "mode_outlier":
            wanted.append((rule.entity, rule.value, "number"))
        elif rule.kind == "stored_metric":
            wanted.append((rule.entity, rule.stored, "number"))
        elif rule.kind == "same_signature":
            wanted.append((h.edge, h.quantity, "number"))
        elif rule.kind == "text_quantity":
            wanted += [(rule.entity, rule.quantity, "number"), (h.node, rule.text, "text")]
        for entity, column, cls in wanted:
            if self._class(entity, column) not in ("", cls):
                raise TypeError(f"entity {entity} column {column} must hold {cls} values")

    def _key_columns(self, rule) -> list:
        """(entity, key columns) that must be unique and non-NULL: the node key when the rule reads node rows by key,
        and the key of the entity the rule reports by key."""
        h, keys = self.h, []
        if h is not None and h.key in rule.reads.get(h.node, ()):
            keys.append((h.node, (h.key,)))
        report = getattr(rule, "report", None)
        if report is not None and report.form == "key":
            keys.append((rule.entity, tuple(report.columns)))
        return keys

    def _key_gate(self, entity: str, columns: tuple) -> str:
        found = self._keys.get((entity, columns))
        if found is None:
            seen, null, duplicate = set(), False, False
            for row in self._rows(entity):
                key = tuple(_canonical(row[c]) for c in columns)
                if None in key:
                    null = True
                elif key in seen:
                    duplicate = True
                else:
                    seen.add(key)
            found = self._keys[(entity, columns)] = "key_null" if null else "key_not_unique" if duplicate else ""
        return found

    def _null_edge_quantity(self) -> bool:
        h = self.h
        return any(row[h.parent] is not None and row[h.child] is not None and row[h.quantity] is None
                   for row in self._rows(h.edge))

    def _too_long(self, column: str, limit: int) -> bool:
        return any(row[column] is not None and len(row[column]) > limit for row in self._rows(self.h.node))

    # ---- indexes -------------------------------------------------------------------------------------
    def _node_index(self) -> dict:
        if self._nodes is None:
            key = self.h.key
            self._nodes = {row[key]: row for row in self._rows(self.h.node)}
        return self._nodes

    def _adjacency(self) -> tuple:
        if self._edges is None:
            h = self.h
            self._edges = _edges(self._rows(h.edge), h.parent, h.child)
        return self._edges

    def _endpoint(self, end: str) -> str:
        return self.h.parent if end == "parent" else self.h.child

    def _ref_values(self, name: str, cls: str) -> frozenset:
        """The non-NULL values of the ref column on the rows whose where columns equal their constants."""
        ref = self.refs[name]
        found = self._ref_sets.get(name)
        if found is None:
            conditions = [(column, _operand(constant, self._class(ref.entity, column), "eq"))
                          for column, constant in ref.where]
            values = set()
            for row in self._rows(ref.entity):
                value = row[ref.column]
                if value is not None and all(_column_test("eq", row[c], operand) for c, operand in conditions):
                    values.add(value)
            found = self._ref_sets[name] = (self._class(ref.entity, ref.column), frozenset(values))
        ref_class, values = found
        if cls and ref_class and cls != ref_class:
            raise TypeError(f"ref {name}: column {ref.column} holds values of another type than the compared column")
        return values

    # ---- atoms ---------------------------------------------------------------------------------------
    def _own_key(self, rule) -> str:
        """The node key column when the rule's own rows are node rows read by key (structure and quantified atoms)."""
        h = self.h
        if h is not None and rule.entity == h.node and h.key in rule.reads.get(h.node, ()):
            return h.key
        return ""

    def _conjunction(self, atoms, entity: str):
        """A test (row, x) -> bool of an AND-list of atoms on rows of entity; x is the row's node key, if any."""
        tests = [self._atom(atom, entity) for atom in atoms]
        if len(tests) == 1:
            return tests[0]

        def conjunction(row, x):
            for test in tests:
                if not test(row, x):
                    return False
            return True
        return conjunction

    def _atom(self, atom, entity: str):
        if hasattr(atom, "branches"):
            branches = [self._conjunction(branch, entity) for branch in atom.branches]

            def disjunction(row, x):
                for branch in branches:
                    if branch(row, x):
                        return True
                return False
            return disjunction
        if hasattr(atom, "quantifier"):
            return self._quantified_atom(atom)
        if hasattr(atom, "exists"):
            endpoint, nodes, exists = self._endpoint(atom.end), self._node_index(), atom.exists
            return lambda row, x: _endpoint_exists(row[endpoint], nodes, exists)
        if hasattr(atom, "test"):
            return self._structure_atom(atom)
        if hasattr(atom, "op"):
            return self._column_atom(atom, entity)
        raise TypeError(f"unknown atom form {type(atom).__name__}")

    def _column_atom(self, atom, entity: str):
        column = atom.column
        if not atom.end:
            test = self._value_test(atom, entity)
            return lambda row, x: test(row[column])
        test = self._value_test(atom, self.h.node)
        endpoint, nodes = self._endpoint(atom.end), self._node_index()
        return lambda row, x: _endpoint_attribute(row[endpoint], nodes, column, test)

    def _value_test(self, atom, entity: str):
        """A test value -> bool of one column atom, its constant read as the column's values are."""
        op, cls = atom.op, self._class(entity, atom.column)
        if op in ("is_null", "is_not_null"):
            operand = None
        elif op == "not_in_ref":
            operand = self._ref_values(atom.ref, cls)
        elif op in ("in", "not_in", "starts_with"):
            operand = _members(self.sets[atom.set], cls, op)
        elif op in _COMPARE:
            operand = _operand(atom.value, cls, op)
        else:
            raise ValueError(f"unknown op {op}")
        return lambda value: _column_test(op, value, operand)

    def _structure_atom(self, atom):
        kids, parents = self._adjacency()
        test, value = atom.test, atom.value
        if atom.end:
            endpoint = self._endpoint(atom.end)
            return lambda row, x: _endpoint_structure(row[endpoint], test, value, kids, parents)
        return lambda row, x: _structure(x, test, kids, parents) == value

    def _quantified_atom(self, atom):
        kids, parents = self._adjacency()
        near = kids if atom.direction == "children" else parents
        nodes = self._node_index()
        sub = self._conjunction(atom.where, self.h.node)
        quantifier = atom.quantifier
        return lambda row, x: _quantified(quantifier, near.get(x, _EMPTY), nodes, sub)

    # ---- outputs -------------------------------------------------------------------------------------
    @staticmethod
    def _outcome(rule, columns: tuple, rows: Counter) -> Outcome:
        if columns != tuple(o.name for o in rule.outputs):
            raise ValueError(f"rule {rule.id}: the evaluated columns differ from the rule's output columns")
        return Outcome(columns, rows)

    def _report(self, rule, matched: list, derived: tuple = ()) -> Outcome:
        """The output of the kinds that select rows (row, mode_outlier, text_quantity). matched holds (row, derived
        values) per selected row; derived names the kind's derived columns."""
        report = rule.report
        own = tuple(report.columns)
        if report.form == "key":
            return self._outcome(rule, own, Counter(tuple(_canonical(row[c]) for c in own) for row, _d in matched))
        if report.form == "distinct":
            values = _distinct(row[own[0]] for row, _d in matched)
            return self._outcome(rule, own, Counter({(v,): 1 for v in values}))
        if report.form == "columns":
            content = Counter(tuple(_canonical(row[c]) for c in own) + tuple(_canonical(v) for v in values)
                              for row, values in matched)
            return self._outcome(rule, own + tuple(derived) + ("n",), _with_counts(content))
        raise ValueError(f"rule {rule.id}: unknown report form")

    # ---- kinds ---------------------------------------------------------------------------------------
    def _kind_row(self, rule) -> Outcome:
        test = self._conjunction(rule.where, rule.entity)
        key = self._own_key(rule)
        rows = self._rows(rule.entity)
        if key:
            matched = [(row, ()) for row in rows if test(row, row[key])]
        else:
            matched = [(row, ()) for row in rows if test(row, None)]
        return self._report(rule, matched)

    def _kind_duplicate(self, rule) -> Outcome:
        getters, names = [], []
        for part in rule.key:
            if part.end:
                getters.append(self._endpoint_getter(part.end, part.column))
                names.append(f"{part.end}_{part.column}")
            else:
                getters.append(operator.itemgetter(part.column))
                names.append(part.column)
        groups: dict = {}
        for row in self._rows(rule.entity):
            key = tuple(get(row) for get in getters)
            if _complete(key):
                groups.setdefault(tuple(_canonical(v) for v in key), []).append(row)
        sizes = {}
        for key, members in groups.items():
            size = len(_distinct(row[rule.count] for row in members)) if rule.count else len(members)
            if size >= rule.min:
                sizes[key] = size
        if rule.report.form == "group":
            return self._outcome(rule, tuple(names) + ("n",),
                                 Counter({key + (_canonical(size),): 1 for key, size in sizes.items()}))
        column = rule.report.columns[0]
        i = next(j for j, part in enumerate(rule.key) if not part.end and part.column == column)
        return self._outcome(rule, (column,), Counter({(key[i],): 1 for key in sizes}))

    def _endpoint_getter(self, end: str, column: str):
        endpoint, nodes = self._endpoint(end), self._node_index()
        return lambda row: _endpoint_value(row[endpoint], nodes, column)

    def _kind_mode_outlier(self, rule) -> Outcome:
        num, den = Decimal(rule.min_mode_share).as_integer_ratio()
        ratio = Decimal(rule.ratio)
        value = rule.value
        groups: dict = {}
        for row in self._rows(rule.entity):
            g = row[rule.group_by]
            if g is not None:
                groups.setdefault(_canonical(g), []).append(row)
        matched = []
        for group in groups.values():
            if rule.min_distinct:
                column, least = rule.min_distinct
                if len(_distinct(row[column] for row in group)) < least:
                    continue
            mode = _mode(group, value, num, den)
            if mode is None:
                continue
            for row in group:
                v = row[value]
                if v is not None and _far_from_mode(v, mode, ratio):
                    matched.append((row, (mode,)))
        return self._report(rule, matched, ("mode",))

    def _kind_same_signature(self, rule) -> Outcome:
        h = self.h
        nodes = self._node_index()
        kids, _parents = self._adjacency()
        sums = _line_sums(self._rows(h.edge), h.parent, h.child, h.quantity)
        classes: dict = {}
        for parent, children in kids.items():
            node = nodes.get(parent)
            if node is None:
                continue
            classes.setdefault((_signature(parent, children, sums), _attributes(node, rule.attrs)), []).append(parent)
        rows = Counter()
        for members in classes.values():
            if len(members) >= rule.min_members:
                group_id = _canonical(_group_id(members))
                for member in members:
                    rows[(group_id, _canonical(member))] += 1
        return self._outcome(rule, ("group_id", "member"), rows)

    def _kind_stored_metric(self, rule) -> Outcome | NotRun:
        h = self.h
        kids, parents = self._adjacency()
        heights = _heights(kids, h.max_depth)
        if heights is None:
            return NotRun("cycle_or_depth_limit")
        rows = Counter()
        for node in self._rows(h.node):
            x, stored = node[h.key], node[rule.stored]
            height = heights.get(x, 0)
            kind = _height_kind(stored, height, x in kids, x in parents)
            if kind:
                rows[(_canonical(x), kind, _canonical(stored), _canonical(height))] += 1
        return self._outcome(rule, (h.key, "kind", "stored", "height"), rows)

    def _kind_text_quantity(self, rule) -> Outcome:
        h = self.h
        nodes = self._node_index()
        endpoint = self._endpoint(rule.end)
        where_end = self._conjunction(rule.where_end, h.node) if rule.where_end else None
        rx = re.compile(rule.pattern.python, re.DOTALL)
        captures = rule.pattern.captures
        tolerance = Decimal(rule.rel_tol)
        text, quantity = rule.text, rule.quantity
        matched = []
        for row in self._rows(rule.entity):
            e = row[endpoint]
            node = None if e is None else nodes.get(e)
            if node is None or (where_end is not None and not where_end(node, e)):
                continue
            q, t = row[quantity], node[text]
            if q is None or t is None:
                continue
            amounts = _amounts(rx.search(t), captures)
            if amounts is not None and _near(q, amounts[0], amounts[1], tolerance):
                matched.append((row, amounts))
        return self._report(rule, matched, ("numerator", "divisor"))


_KINDS = {"row": Evaluator._kind_row, "duplicate": Evaluator._kind_duplicate,
          "mode_outlier": Evaluator._kind_mode_outlier, "same_signature": Evaluator._kind_same_signature,
          "stored_metric": Evaluator._kind_stored_metric, "text_quantity": Evaluator._kind_text_quantity}


# ---- the semantics, one small function each ----------------------------------------------------------
def _value_class(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, Decimal)):
        return "number"
    if isinstance(value, str):
        return "text"
    raise TypeError(f"unsupported value type {type(value).__name__}")


def _canonical(value):
    """Canonical comparison form of a typed cell: numbers as canon numbers, text exactly, booleans, NULL as None."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, Decimal)):
        return canon.canon(value, "number")
    raise TypeError(f"unsupported value type {type(value).__name__}")


def _number(constant) -> Decimal:
    if isinstance(constant, bool):
        raise TypeError("a boolean constant is compared with a numeric column")
    if isinstance(constant, (int, Decimal)):
        return Decimal(constant)
    if isinstance(constant, str) and _NUMERAL.fullmatch(constant):
        return Decimal(constant)
    raise TypeError("a constant compared with a numeric column must be an integer or a decimal numeral")


def _operand(constant, cls: str, op: str):
    """A comparison constant read as the column's values are: a number by value, text exactly or a boolean. A
    constant of another type, an order comparison on text or booleans and any op but eq or ne on booleans raise."""
    if not cls:
        return None                     # the column holds no value, so every comparison is false
    if cls == "number":
        return _number(constant)
    if op in _ORDER_OPS:
        raise TypeError(f"op {op} needs a numeric column")
    if cls == "text" and isinstance(constant, str):
        return constant
    if cls == "boolean" and isinstance(constant, bool) and op in ("eq", "ne"):
        return constant
    raise TypeError(f"a constant does not suit a {cls} column")


def _members(members, cls: str, op: str):
    """A set's members read as the column's values are: prefixes for starts_with, else a frozenset."""
    if op == "starts_with":
        if cls not in ("", "text") or not all(isinstance(m, str) for m in members):
            raise TypeError("starts_with needs a text column and a set of strings")
        return tuple(members)
    if not cls:
        return _EMPTY
    if cls == "boolean":
        raise TypeError(f"op {op} is not allowed on a boolean column")
    return frozenset(_operand(m, cls, "eq") for m in members)


def _column_test(op: str, value, operand) -> bool:
    """One column atom on one value. A NULL value makes every comparison false (ne, not_in, starts_with and
    not_in_ref included); is_null and is_not_null test NULL explicitly."""
    if op == "is_null":
        return value is None
    if op == "is_not_null":
        return value is not None
    return value is not None and _TESTS[op](value, operand)


def _edges(rows, parent: str, child: str) -> tuple:
    """(children by parent, parents by child) of the edges: the rows whose parent and child are both non-NULL."""
    kids: dict = {}
    parents: dict = {}
    for row in rows:
        p, c = row[parent], row[child]
        if p is None or c is None:
            continue
        kids.setdefault(p, set()).add(c)
        parents.setdefault(c, set()).add(p)
    return kids, parents


def _structure(x, test: str, kids: dict, parents: dict) -> bool:
    if test == "has_children":
        return x in kids
    if test == "has_parents":
        return x in parents
    return x in kids or x in parents     # in_hierarchy


def _endpoint_structure(endpoint, test: str, value: bool, kids: dict, parents: dict) -> bool:
    """Endpoint structure: false for a NULL endpoint, else the structure test of the endpoint value (which needs no
    node row) compared with the atom's boolean."""
    return endpoint is not None and _structure(endpoint, test, kids, parents) == value


def _endpoint_exists(endpoint, nodes: dict, exists: bool) -> bool:
    """exists = true: the endpoint is non-NULL and has a node row; exists = false: it is non-NULL and has none."""
    return endpoint is not None and (endpoint in nodes) == exists


def _endpoint_attribute(endpoint, nodes: dict, column: str, test) -> bool:
    """True only when the endpoint is non-NULL, its node row exists and the comparison holds on that row."""
    if endpoint is None:
        return False
    node = nodes.get(endpoint)
    return node is not None and test(node[column])


def _endpoint_value(endpoint, nodes: dict, column: str):
    """The attribute of an existing endpoint row; None for a NULL endpoint, a missing row or a NULL attribute."""
    if endpoint is None:
        return None
    node = nodes.get(endpoint)
    return None if node is None else node[column]


def _quantified(quantifier: str, near, nodes: dict, sub) -> bool:
    """all: at least one neighbour and every neighbour exists and satisfies sub; any: some neighbour exists and
    satisfies it; none: no neighbour exists and satisfies it (true without neighbours)."""
    hits = (n in nodes and sub(nodes[n], n) for n in near)
    if quantifier == "all":
        return bool(near) and all(hits)
    if quantifier == "any":
        return any(hits)
    if quantifier == "none":
        return not any(hits)
    raise ValueError(f"unknown quantifier {quantifier}")


def _complete(key: tuple) -> bool:
    """A duplicate key forms a group only when every part is non-NULL."""
    return all(v is not None for v in key)


def _distinct(values) -> set:
    """The distinct non-NULL values, compared by their canonical form (numbers by value)."""
    return {_canonical(v) for v in values if v is not None}


def _with_counts(content: Counter) -> Counter:
    """Output rows of the columns report: each distinct content once, with n = the number of rows that have it."""
    return Counter({row + (_canonical(n),): 1 for row, n in content.items()})


def _mode(group: list, value: str, num: int, den: int):
    """The group's mode when the group is eligible, else None: the most frequent non-NULL value, whose count times den
    must be at least num times the rows of the group (NULL values included), and which must be greater than 0."""
    counts = Counter(row[value] for row in group if row[value] is not None)
    if not counts:
        return None
    mode, top = max(counts.items(), key=lambda item: item[1])
    if top * den < num * len(group):
        return None
    return mode if mode > 0 else None


def _far_from_mode(v, mode, ratio: Decimal) -> bool:
    return v >= ratio * mode or ratio * v <= mode


def _line_sums(rows, parent: str, child: str, quantity: str) -> dict:
    """(parent, child) -> the sum of the quantities of that pair's lines, over the edges."""
    sums: dict = {}
    for row in rows:
        p, c = row[parent], row[child]
        if p is None or c is None:
            continue
        q = row[quantity]
        sums[(p, c)] = sums[(p, c)] + q if (p, c) in sums else q
    return sums


def _signature(parent, children, sums: dict) -> frozenset:
    """The set of (child, summed quantity), compared by canonical values."""
    return frozenset((_canonical(child), _canonical(sums[(parent, child)])) for child in children)


def _attributes(node: dict, attrs: tuple) -> tuple:
    """A member's attributes, compared exactly, NULL equal to NULL."""
    return tuple(_canonical(node[a]) for a in attrs)


def _group_id(members: list):
    """The smallest member: code point order for text keys, numeric order for number keys."""
    return min(members)


def _heights(kids: dict, max_depth: int):
    """{node: height} over the edge graph by an iterative post-order depth-first search, or None when some height
    cannot be bounded by max_depth: a cycle (a child still on the search path, a self-loop included) or a height
    above max_depth. A node that is no parent (a child without a node row included) is a leaf of height 0."""
    height: dict = {}
    for root in kids:
        if root in height:
            continue
        path = {root}
        stack = [(root, iter(kids[root]))]
        while stack:
            node, pending = stack[-1]
            descended = False
            for child in pending:
                if child in height:
                    continue
                if child in path:
                    return None
                if child not in kids:
                    height[child] = 0
                    continue
                if len(stack) >= max_depth:      # the root's height would exceed max_depth
                    return None
                path.add(child)
                stack.append((child, iter(kids[child])))
                descended = True
                break
            if not descended:
                stack.pop()
                path.discard(node)
                value = 1 + max(height[c] for c in kids[node])
                if value > max_depth:
                    return None
                height[node] = value
    return height


def _height_kind(stored, height: int, has_children: bool, has_parents: bool) -> str:
    """"" when the stored value is NULL or equals the height, else the kind of the difference (HEIGHT_KINDS)."""
    if stored is None or stored == height:
        return ""
    if has_children:
        if stored == 0:
            return "has_children_stored_zero"
        return "stored_below" if stored < height else "stored_above"
    return "leaf_stored_nonzero" if has_parents else "outside_stored_nonzero"


def _amounts(match, captures: int):
    """(X, N) of the leftmost match, or None (a defined no-match): capture 1 a plain decimal numeral with X > 0, and
    capture 2, when the pattern has one, digits with N > 0, or N = 1 when that group did not take part."""
    if match is None:
        return None
    amount = match.group(1)
    if amount is None or not _AMOUNT.fullmatch(amount):
        return None
    divisor = match.group(2) if captures == 2 else None
    if divisor is None:
        n = Decimal(1)
    elif _COUNT.fullmatch(divisor):
        n = Decimal(divisor)
    else:
        return None
    x = Decimal(amount)
    if not (x > 0 and n > 0):
        return None
    return x, n


def _near(q, x: Decimal, n: Decimal, tolerance: Decimal) -> bool:
    """|q * N - X| <= rel_tol * X, with multiplication and subtraction only."""
    return abs(q * n - x) <= tolerance * x


# ---- canonical cells, order and authored examples ----------------------------------------------------
def cell(value, type_class: str):
    """Canonical form of one cell of a column of a type class, as an example writes it (numbers as an integer or a
    decimal numeral) or as the export delivers it: numbers as canon numbers, text exactly, bpchar without its
    trailing pad spaces (as PostgreSQL reads it), booleans as bool and NULL as None. A value the class cannot hold
    raises TypeError."""
    if value is None:
        return None
    if type_class == "number":
        if isinstance(value, bool) or not (isinstance(value, (int, Decimal))
                                           or (isinstance(value, str) and _NUMERAL.fullmatch(value))):
            raise TypeError("a number cell is an integer, a Decimal or a decimal numeral")
        if isinstance(value, Decimal) and not value.is_finite():
            raise TypeError("a number cell is finite")
        return canon.canon(value, "number")
    if type_class == "boolean":
        if not isinstance(value, bool):
            raise TypeError("a boolean cell is true or false")
        return value
    if type_class in ("text", "bpchar"):
        if not isinstance(value, str):
            raise TypeError("a text cell is a string")
        return value.rstrip(" ") if type_class == "bpchar" else value
    raise ValueError(f"unknown type class {type_class}")


def sort_key(row: tuple, classes: tuple) -> tuple:
    """Typed order of a canonical row, one type class per column: NULL < false < true < numbers by value < text by
    code point (UTF-8 byte order, COLLATE "C"), the order of ord in the lookup views."""
    out = []
    for value, cls in zip(row, classes):
        if value is None:
            out.append((0,))
        elif cls == "boolean":
            out.append((2,) if value else (1,))
        elif cls == "number":
            out.append((3, Decimal(value)))
        else:
            out.append((4, value))
    return tuple(out)


def ordered(outcome: Outcome, classes: tuple) -> list:
    """An outcome's rows, each as often as it occurs, in the typed order (sort_key)."""
    rows = [row for row, count in outcome.rows.items() for _ in range(count)]
    return sorted(rows, key=lambda row: sort_key(row, classes))


def output_classes(rule, plan) -> tuple:
    """The type class of every output column of a rule: a column output has its column's class in the catalog type
    plan (plan.column(entity, column) gives a type with type_class and usable); n, height, numerator and divisor are
    numbers; kind is text."""
    classes = []
    for output in rule.outputs:
        if output.source == "column":
            classes.append(_usable_class(plan, output.entity, output.column))
        else:
            classes.append("text" if output.source == "text" else "number")
    return tuple(classes)


def _usable_class(plan, entity: str, column: str) -> str:
    t = plan.column(entity, column)
    if t is None or not t.usable or t.type_class not in TYPE_CLASSES:
        raise ValueError(f"entity {entity} column {column} has no usable type in the type plan")
    return t.type_class


def _planned(plan, entity: str, column: str, seen: str, padded: bool):
    """With a type plan, a column must hold values of its own class (text, varchar and bpchar hold str), and bpchar
    values must have arrived without trailing pad spaces, as the export's CAST to text delivers them."""
    cls = _usable_class(plan, entity, column)
    if seen and seen != ("text" if cls == "bpchar" else cls):
        raise TypeError(f"entity {entity} column {column} holds {seen} values for a {cls} column")
    if cls == "bpchar" and padded:
        raise TypeError(f"entity {entity} column {column} holds bpchar values with trailing pad spaces")


def example_tables(rule, example, plan) -> dict:
    """An authored example's input rows as the data-rules export would deliver them: every column the rule reads, with
    numbers as int (integer cells) or Decimal (decimal numerals), text as written, bpchar without trailing pad spaces
    and booleans as bool. A cell its column cannot hold raises TypeError."""
    tables = {}
    for entity, columns in rule.reads.items():
        classes = {c: _usable_class(plan, entity, c) for c in columns}
        tables[entity] = [{c: _exported(row[c], classes[c]) for c in columns} for row in example.rows[entity]]
    return tables


def _exported(value, type_class: str):
    if type_class == "number" and value is not None:
        cell(value, "number")            # refuses what a numeric column cannot hold
        return Decimal(value) if isinstance(value, str) else value
    return cell(value, type_class)


def expected(rule, example, plan) -> Outcome:
    """An authored example's expected output as an Outcome: its expect rows as canonical tuples in output order."""
    names = tuple(o.name for o in rule.outputs)
    classes = output_classes(rule, plan)
    rows = Counter(tuple(cell(row[name], cls) for name, cls in zip(names, classes)) for row in example.expect)
    return Outcome(names, rows)


def example_verdict(sql: Outcome, py: Outcome, expect: Outcome) -> str:
    """How one example's outputs relate: "" when the SQL, Python and authored outputs are equal, common_mode when the
    two evaluations agree but differ from the authored rows, sql_mismatch when only the Python output equals them,
    py_mismatch when only the SQL output does, both_mismatch when neither does and the two differ."""
    if not all(isinstance(o, Outcome) for o in (sql, py, expect)):
        raise TypeError("example_verdict compares three Outcomes")
    if sql == py:
        return "" if py == expect else "common_mode"
    if py == expect:
        return "sql_mismatch"
    if sql == expect:
        return "py_mismatch"
    return "both_mismatch"


# ---- the subjects view -------------------------------------------------------------------------------
def subjects(rules, outcomes: dict, key_class: str) -> tuple:
    """The rows of the subjects view in its order: one Subject per non-NULL subject value over the rules that name a
    subject, with the worst severity, its rank, the rule count and the rule ids in configuration order, sorted by
    severity rank and then by subject (code point order for text keys, numeric order for number keys).

    rules: the rules in configuration order (rules without a subject are skipped); outcomes: rule id -> the agreed
    Outcome of every rule that names a subject; key_class: the node key's type class. A NULL subject cell names no
    node and feeds no row."""
    found: dict = {}
    for position, rule in enumerate(rules):
        if not rule.subject:
            continue
        outcome = outcomes.get(rule.id)
        if not isinstance(outcome, Outcome):
            raise ValueError(f"rule {rule.id}: the subjects need its agreed output")
        if rule.severity not in SEVERITIES:
            raise ValueError(f"rule {rule.id}: unknown severity")
        column = outcome.columns.index(rule.subject)
        for row in outcome.rows:
            if row[column] is not None:
                found.setdefault(row[column], {})[position] = rule
    out = []
    for value, by_position in found.items():
        chosen = [by_position[p] for p in sorted(by_position)]
        rank = min(SEVERITIES.index(r.severity) for r in chosen)
        out.append(Subject(value, SEVERITIES[rank], rank, len(chosen), tuple(r.id for r in chosen)))
    out.sort(key=lambda s: (s.severity_rank, sort_key((s.subject,), (key_class,))))
    return tuple(out)
