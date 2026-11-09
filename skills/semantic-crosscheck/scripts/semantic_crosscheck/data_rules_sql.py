"""Path 1 of the data-rules stage: every rule compiled to PostgreSQL, with its gates, exports, lookups and views.

Each rule of a validated rule file (data_rules.RuleFile) compiles to one SELECT whose text holds identifiers and SQL
grammar only: every configured constant is a typed Param slot of a Statement. A Statement renders in two ways:

  parameterized()  CAST(:pN AS numeric|text|boolean|bigint) per slot and a params dict (Decimal, str, bool, int) for
                   pg8000's native :name placeholders, as data-rules runs it and as rules.sql records it. The text
                   never contains '::', so no placeholder touches a cast operator.
  literal()        CAST(E'...' AS type) per slot, for the view definitions build creates and records in build.sql.
                   In the E-string every backslash is doubled and every single quote is preceded by a backslash, so
                   the text reads the same with standard_conforming_strings on or off; only a backslash at the very
                   end is written as the octal escape \\134, because pg8000's placeholder scanner (every statement
                   build runs passes through it) would take the closing quote after a doubled backslash for an
                   escaped quote and then rewrite a later ':' inside a literal as a placeholder. Numerals are
                   validated and NUL is refused (the rule file refuses it at load).

Relations come from a mapping: the configured tables (ident.qualified), or, for an authored example, CTEs named
"sxc_ex_<entity>" over the example rows, each value bound and cast to its column's declared type (numeric(p,s),
varchar(n), bpchar(n), ...) rendered from the catalog's (atttypid, atttypmod), never from catalog text.

The templates implement the semantic contract of the data rules, which data_rules_eval implements independently in
Python: an atom with a NULL operand is false (every comparison is guarded by IS NOT NULL), an edge is a row of the
hierarchy edge entity with both ends non-NULL, every reference to a bpchar column is CAST(col AS text) (the value
without pad spaces, as the export reads it), text compares exactly and COLLATE "C" orders text by code point (UTF-8
byte order), numbers compare by value in numeric, and arithmetic is +, -, * and comparison only. Kinds:

  row             SELECT over the entity (alias t) with the where atoms; report key (the key columns), distinct C
                  (SELECT DISTINCT, non-NULL) or columns (the columns, the derived columns and count(*) AS n).
  duplicate       GROUP BY the key parts (endpoint parts through an inner join to the endpoint's node row), all
                  non-NULL, HAVING count(*) (or count(DISTINCT C)) >= min.
  mode_outlier    per non-NULL group: N rows, the mode by the integer share test count(mode) * den >= num * N
                  (share = num/den > 1/2, so the mode is unique) and mode > 0; flags v >= ratio * mode or
                  ratio * v <= mode.
  same_signature  members are node rows with child edges; the signature is the collation-free text
                  string_agg(hex(UTF-8 child) || ':' || trim_scale(sum)) ordered by child COLLATE "C" (numbers by
                  value); a class of at least min_members members sharing signature and attributes (NULL equal to
                  NULL) gives (group_id = smallest member, member).
  stored_metric   heights by a WITH RECURSIVE capped at max_depth + 1 (a dangling child is a leaf); the height gate
                  counts nodes the cap cannot bound (heights at the cap and parents without a height: cycles and
                  deeper chains), so the rule runs only when every height is exact.
  text_quantity   regexp_match of the bound ARE pattern in the endpoint's text, captures checked by two bound
                  regexes, flags |q * N - X| <= rel_tol * X.

Every rule runs as SELECT * FROM (<rule>) r LIMIT CAST(:limit AS bigint) (limited()), with limit = max_rule_rows + 1.
The snapshot gates (server encoding, budgets counted with LIMIT max_export_rows + 1, NULL and duplicate keys, NaN and
infinities per numeric column, NULL edge quantities, unbounded heights, text length) and the exports are generated
here too, with gate_reasons() mapping their counts to the rules they leave not run. view_statement() is the exact
statement build records in build.sql and data-rules regenerates to detect stale views. rules_sql() writes the
placeholder-only record of a run.

This module generates and reads statements only: it opens no connection and imports neither the stages nor the
Python evaluation (data_rules_eval), so the two paths stay independent.
"""

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from . import data_rules, ident
from .data_rules import (AnyAtom, ColumnAtom, ColumnType, ExistsAtom, QuantifiedAtom, RuleSpecError,
                         StructureAtom, TypePlan)

PARAM_TYPES = {"number": "numeric", "integer": "bigint", "text": "text", "boolean": "boolean"}   # kind -> SQL type
MAX_PARAMS = 32767                   # bound values per statement (the wire protocol counts them in 16 bits)
LIMIT_PARAM = "limit"
VIEW_MARKER = "semantic-crosscheck data rule view: rows are cross-checked only by sxc.py data-rules"
ORD = "ord"                          # the order column every lookup view adds
EXAMPLE_PREFIX = "sxc_ex_"
NUM_CAPTURE = r"^[0-9]+(\.[0-9]+)?$"   # text_quantity capture 1 (the amount X)
DIV_CAPTURE = r"^[0-9]+$"              # text_quantity capture 2 (the divisor N)
SQL_OPS = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
TYPE_OIDS = {16: "bool", 20: "int8", 21: "int2", 23: "int4", 25: "text", 1042: "bpchar", 1043: "varchar",
             1700: "numeric"}        # the supported base types, by their fixed pg_type OIDs
TYPE_CLASSES = {"int2": "number", "int4": "number", "int8": "number", "numeric": "number", "text": "text",
                "varchar": "text", "bpchar": "bpchar", "bool": "boolean"}
TYPE_NAMES = {"int2": "smallint", "int4": "integer", "int8": "bigint", "text": "text", "bool": "boolean"}
VARHDRSZ = 4                         # atttypmod offset of numeric, varchar and bpchar modifiers
GATES = ("encoding", "budget", "key", "non_finite", "null_quantity", "height", "text_length")   # run order
GATE_REASONS = ("server_encoding", "resource_limit", "key_null", "key_not_unique", "non_finite_values",
                "null_values", "cycle_or_depth_limit", "value_too_long")                    # first one wins
HEIGHT_KIND_CASE = ("'has_children_stored_zero'", "'stored_below'", "'stored_above'", "'leaf_stored_nonzero'",
                    "'outside_stored_nonzero'")   # data_rules.HEIGHT_KINDS as SQL literals (a test keeps them equal)
RULES_SQL_HEADER = """\
-- semantic-crosscheck data-rules: the statements of this run, in order (generated from the rule file).
-- Every value is a bound parameter (:pN, :limit): this file holds identifiers and SQL grammar only, never a
-- configured constant, an example value or a row value. For review only: change the rule file and rerun
-- `sxc.py data-rules` instead of running these statements.
"""
_NUMERAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_SQL_TYPE = re.compile(r"[a-z]+(\([0-9]+(,-?[0-9]+)?\))?")
_PLAIN_ITEM = re.compile(r'[a-z][a-z0-9]*\."[a-z_][a-z0-9_]*"')
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_ ()\[\].:,=-]")


class CompileError(ValueError):
    """A rule that cannot be compiled with the given type plan (a column without a usable type, which apply_types
    reports as not run) or a statement used in a way the module does not support: a programming error."""


# ---- statement model ----------------------------------------------------------------------------------
class Param:
    """One bound value: kind number (Decimal), integer (int), text (str) or boolean (bool); None is NULL. sql_type
    is the type written in its cast (the kind's type by default; examples use the column's declared type). name
    fixes the placeholder name (LIMIT_PARAM); other slots are numbered p1, p2, ... in order of first appearance.
    The same Param object used twice renders as one placeholder."""
    __slots__ = ("value", "kind", "sql_type", "name")

    def __init__(self, value, kind: str, sql_type: str = "", name: str = ""):
        if kind not in PARAM_TYPES:
            raise CompileError(f"unknown parameter kind {kind!r}")
        sql_type = sql_type or PARAM_TYPES[kind]
        if not _SQL_TYPE.fullmatch(sql_type):
            raise CompileError(f"unsupported parameter type {sql_type!r}")
        if name and (not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or re.fullmatch(r"p[0-9]+", name)):
            raise CompileError(f"invalid placeholder name {name!r}")
        self.value = None if value is None else _typed(value, kind)
        self.kind, self.sql_type, self.name = kind, sql_type, name

    def __repr__(self) -> str:
        return f"Param({self.kind}, {self.sql_type})"   # never the value: reprs reach logs


def _typed(value, kind: str):
    if kind == "number":
        if type(value) is int:
            return Decimal(value)
        if type(value) is str and _NUMERAL.fullmatch(value):
            return Decimal(value)
        if isinstance(value, Decimal) and value.is_finite():
            return value
    elif kind == "integer":
        if type(value) is int:
            return value
    elif kind == "text":
        if type(value) is str and "\x00" not in value:
            return value
    elif type(value) is bool:
        return value
    raise CompileError(f"a {kind} parameter got a value of type {type(value).__name__}")


def e_string(text: str) -> str:
    """text as an E'' literal: backslashes doubled, single quotes as \\', a final backslash as \\134 (see the module
    docstring). Exact under any standard_conforming_strings setting; NUL is refused."""
    if not isinstance(text, str) or "\x00" in text:
        raise CompileError("a text literal must be a string without NUL")
    body = text.replace("\\", "\\\\").replace("'", "\\'")
    if text.endswith("\\"):
        body = body[:-2] + "\\134"
    return "E'" + body + "'"


def _literal(p: Param) -> str:
    if p.value is None:
        return f"CAST(NULL AS {p.sql_type})"
    if p.kind == "number":
        text = format(p.value, "f")
        if not _NUMERAL.fullmatch(text):
            raise CompileError("a numeric parameter is not a plain decimal numeral")
    elif p.kind == "integer":
        text = str(p.value)
    elif p.kind == "boolean":
        text = "true" if p.value else "false"
    else:
        text = p.value
    return f"CAST({e_string(text)} AS {p.sql_type})"


class Sql:
    """An SQL fragment: text interleaved with Param slots. Immutable; + and Sql.join compose fragments."""
    __slots__ = ("parts",)

    def __init__(self, *items):
        parts: list = []
        for item in items:
            if isinstance(item, Sql):
                seq = item.parts
            elif isinstance(item, (str, Param)):
                seq = (item,)
            else:
                raise CompileError(f"an SQL fragment cannot hold {type(item).__name__}")
            for part in seq:
                if isinstance(part, str):
                    if not part:
                        continue
                    if parts and isinstance(parts[-1], str):
                        parts[-1] += part
                        continue
                parts.append(part)
        self.parts = tuple(parts)

    def __add__(self, other) -> "Sql":
        return Sql(self, other)

    def __radd__(self, other) -> "Sql":
        return Sql(other, self)

    @staticmethod
    def join(separator: str, items) -> "Sql":
        out: list = []
        for i, item in enumerate(items):
            if i:
                out.append(separator)
            out.append(item)
        return Sql(*out)


class Statement:
    """One statement: an Sql fragment and a label (ids and enums only) that rules.sql writes above it."""
    __slots__ = ("sql", "label")

    def __init__(self, sql, label: str = ""):
        self.sql = sql if isinstance(sql, Sql) else Sql(sql)
        self.label = label

    def parameterized(self) -> tuple:
        """(text with CAST(:name AS type) placeholders, {name: value})."""
        names: dict = {}
        params: dict = {}
        types: dict = {}
        out: list = []
        count = 0
        for part in self.sql.parts:
            if isinstance(part, str):
                out.append(part)
                continue
            name = names.get(id(part))
            if name is None:
                if part.name:
                    name = part.name
                    if name in params and (params[name] != part.value or types[name] != part.sql_type):
                        raise CompileError(f"placeholder {name} is bound to two different values")
                else:
                    count += 1
                    name = f"p{count}"
                names[id(part)] = name
                params[name], types[name] = part.value, part.sql_type
            out.append(f"CAST(:{name} AS {part.sql_type})")
        return "".join(out), params

    def literal(self) -> str:
        """The text with every slot written as a literal (view definitions)."""
        return "".join(part if isinstance(part, str) else _literal(part) for part in self.sql.parts)

    @property
    def text(self) -> str:
        return self.parameterized()[0]

    @property
    def params(self) -> dict:
        return self.parameterized()[1]


# ---- type plan ----------------------------------------------------------------------------------------
def type_plan_statement(cfg, entities) -> Statement:
    """One catalog query (pg_attribute, pg_type, pg_collation) for the columns of the entities' tables: entity,
    column_name, type_oid, type_mod, type_kind, deterministic per column, in attribute order. Read the rows with
    type_plan()."""
    entities = list(entities)
    if not entities:
        raise CompileError("the type plan needs at least one entity")
    values = []
    for entity in entities:
        schema, table = ident.split(cfg.entities[entity].table)
        values.append(Sql("(", Param(ident.check(entity), "text"), ", ", Param(schema, "text"), ", ",
                          Param(table, "text"), ")"))
    return Statement(Sql(
        "SELECT w.entity, CAST(a.attname AS text) AS column_name, CAST(a.atttypid AS bigint) AS type_oid, "
        "a.atttypmod AS type_mod, CAST(ty.typtype AS text) AS type_kind, coll.collisdeterministic AS deterministic "
        "FROM (VALUES ", Sql.join(", ", values), ") AS w(entity, schema_name, table_name) "
        "JOIN pg_catalog.pg_namespace ns ON ns.nspname = w.schema_name "
        "JOIN pg_catalog.pg_class c ON c.relnamespace = ns.oid AND c.relname = w.table_name "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "JOIN pg_catalog.pg_type ty ON ty.oid = a.atttypid "
        "LEFT JOIN pg_catalog.pg_collation coll ON coll.oid = a.attcollation "
        "ORDER BY w.entity, a.attnum"), "catalog type plan")


def column_type(type_oid, type_mod, deterministic=True, type_kind="b") -> ColumnType:
    """The ColumnType of one catalog column: number (int2, int4, int8, numeric), text (text, varchar), bpchar or
    boolean by the type's OID; every other type, domains included, is other (reason column_type). A text or bpchar
    column with a nondeterministic collation keeps its class with reason collation. Precision, scale and length come
    from atttypmod (-1: unconstrained)."""
    base = TYPE_OIDS.get(type_oid) if type(type_oid) is int else None
    if base is None or type_kind not in ("b", None):
        return ColumnType("other", reason="column_type")
    precision = scale = length = None
    mod = type_mod if type(type_mod) is int else -1
    if base == "numeric" and mod >= VARHDRSZ:
        precision = ((mod - VARHDRSZ) >> 16) & 0xFFFF
        scale = (((mod - VARHDRSZ) & 0x7FF) ^ 1024) - 1024
    elif base in ("varchar", "bpchar") and mod >= VARHDRSZ:
        length = mod - VARHDRSZ
    cls = TYPE_CLASSES[base]
    reason = "collation" if cls in ("text", "bpchar") and deterministic is False else ""
    return ColumnType(cls, base, precision, scale, length, reason)


_PLAN_COLUMNS = ("entity", "column_name", "type_oid", "type_mod", "type_kind", "deterministic")


def type_plan(rows) -> TypePlan:
    """The TypePlan of the type_plan_statement() rows (dicts as db.rows returns them, or sequences in its column
    order)."""
    columns = {}
    for row in rows:
        r = row if isinstance(row, dict) else dict(zip(_PLAN_COLUMNS, row))
        columns[(r["entity"], r["column_name"])] = column_type(r["type_oid"], r["type_mod"], r["deterministic"],
                                                               r["type_kind"])
    return TypePlan(columns)


def declared_sql_type(t: ColumnType) -> str:
    """The declared type of a usable column as SQL (numeric(18,6), varchar(20), bpchar(3), integer, text, ...)."""
    if t is None or not t.usable:
        raise CompileError("a declared type needs a usable column type")
    if t.base == "numeric":
        return f"numeric({t.precision},{t.scale})" if t.precision is not None else "numeric"
    if t.base in ("varchar", "bpchar"):
        return f"{t.base}({t.length})" if t.length is not None else t.base
    return TYPE_NAMES[t.base]


def output_types(spec, plan: TypePlan, rule) -> tuple:
    """The type class of each output column (number, text, bpchar or boolean): the plan's class for a column read
    from an entity, number for n, height, numerator and divisor, text for kind. A bpchar output is text in SQL (it
    is cast), so it orders like text."""
    out = []
    for o in rule.outputs:
        if o.source == "column":
            t = plan.column(o.entity, o.column)
            if t is None or not t.usable:
                raise CompileError(f"{rule.pos}: output column {o.name} has no usable type")
            out.append(t.type_class)
        else:
            out.append("text" if o.source == "text" else "number")
    return tuple(out)


# ---- relations ----------------------------------------------------------------------------------------
def example_relation(entity: str) -> str:
    """The CTE name of an entity's rows in example statements (an identifier of at most 63 characters)."""
    name = EXAMPLE_PREFIX + ident.check(entity)
    if len(name) > 63:
        digest = hashlib.sha256(entity.encode("utf-8")).hexdigest()[:8]
        name = f"{EXAMPLE_PREFIX}{entity[:47]}_{digest}"
    return ident.check(name)


class _Relations:
    """entity -> relation in SQL text: the configured table, or the example CTE. Records the entities used."""

    def __init__(self, cfg, example: bool = False):
        self.cfg, self.example = cfg, example
        self.used: set = set()

    def __call__(self, entity: str) -> str:
        self.used.add(entity)
        if self.example:
            return ident.quote(example_relation(entity))
        return ident.qualified(self.cfg.entities[entity].table)


def _type(plan: TypePlan, entity: str, column: str) -> ColumnType:
    t = plan.column(entity, column)
    if t is None or not t.usable:
        raise CompileError(f"column {column} of entity {entity} has no usable type in the type plan")
    return t


def _column(plan: TypePlan, alias: str, entity: str, column: str) -> str:
    """alias."column", or CAST(alias."column" AS text) for a bpchar column."""
    ref = f"{alias}.{ident.quote(column)}"
    return f"CAST({ref} AS text)" if _type(plan, entity, column).type_class == "bpchar" else ref


def _comparable(t: ColumnType) -> str:
    return "text" if t.type_class == "bpchar" else t.type_class


def _item(expr: str, name: str) -> str:
    """A select-list item named name: the bare column reference when it already has that name."""
    quoted = ident.quote(name)
    if _PLAIN_ITEM.fullmatch(expr) and expr.endswith("." + quoted):
        return expr
    return f"{expr} AS {quoted}"


# ---- the rule compiler --------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Scope:
    """The row atoms are evaluated on: alias and entity. Subqueries inside a quantified sub-where (depth 1) get their
    own aliases (e2, r2), so no alias ever shadows one of an enclosing query."""
    alias: str
    entity: str
    depth: int = 0

    def sub(self, letter: str) -> str:
        return letter if self.depth == 0 else f"{letter}{self.depth + 1}"


class _Compiler:
    def __init__(self, cfg, spec, plan: TypePlan, rel: _Relations):
        self.cfg, self.spec, self.plan, self.rel = cfg, spec, plan, rel
        self.h = spec.hierarchy
        self.columns: set = set()          # (entity, column) pairs the compiled text references

    # ---- columns and constants --------------------------------------------------------------------
    def col(self, alias: str, entity: str, column: str) -> str:
        self.columns.add((entity, column))
        return _column(self.plan, alias, entity, column)

    def const(self, value, entity: str, column: str) -> Param:
        cls = _comparable(_type(self.plan, entity, column))
        if cls == "number":
            return Param(value, "number")
        return Param(value, "boolean" if cls == "boolean" else "text")

    def endpoint(self, alias: str, end: str) -> str:
        return self.col(alias, self.h.edge, self.h.parent if end == "parent" else self.h.child)

    def node_key(self, alias: str) -> str:
        return self.col(alias, self.h.node, self.h.key)

    # ---- atoms -------------------------------------------------------------------------------------
    def atoms(self, atoms: tuple, s: _Scope) -> list:
        return [self.atom(a, s) for a in atoms]

    def atom(self, a, s: _Scope) -> Sql:
        if isinstance(a, ColumnAtom):
            if a.end:
                return self.endpoint_attribute(a, s)
            return self.column_test(a, s.alias, s.entity, s)
        if isinstance(a, ExistsAtom):
            return self.endpoint_exists(a, s)
        if isinstance(a, StructureAtom):
            if a.end:
                x = self.endpoint(s.alias, a.end)
                return Sql(f"({x} IS NOT NULL AND ", self.structure(a.test, a.value, x, s), ")")
            expr = self.structure(a.test, a.value, self.node_key(s.alias), s)
            return expr if a.test == "in_hierarchy" and a.value else Sql("(", expr, ")")
        if isinstance(a, QuantifiedAtom):
            return self.quantified(a, s)
        if isinstance(a, AnyAtom):
            return self.disjunction(a, s)
        raise CompileError(f"unknown atom {type(a).__name__}")

    def column_test(self, a: ColumnAtom, alias: str, entity: str, s: _Scope) -> Sql:
        """A column atom on alias (entity's row): comparisons, sets and refs are false for NULL."""
        x = self.col(alias, entity, a.column)
        if a.op == "is_null":
            return Sql(f"({x} IS NULL)")
        if a.op == "is_not_null":
            return Sql(f"({x} IS NOT NULL)")
        if a.op in SQL_OPS:
            return Sql(f"({x} IS NOT NULL AND {x} {SQL_OPS[a.op]} ", self.const(a.value, entity, a.column), ")")
        if a.op in ("in", "not_in"):
            members = Sql.join(", ", [self.const(m, entity, a.column) for m in self.spec.sets[a.set]])
            negation = "NOT " if a.op == "not_in" else ""
            return Sql(f"({x} IS NOT NULL AND {x} {negation}IN (", members, "))")
        if a.op == "starts_with":
            tests = [Sql(f"starts_with({x}, ", Param(m, "text"), ")") for m in self.spec.sets[a.set]]
            return Sql(f"({x} IS NOT NULL AND (", Sql.join(" OR ", tests), "))")
        if a.op == "not_in_ref":
            ref = self.spec.refs[a.ref]
            r = s.sub("r")
            conds = [Sql(f"{self.col(r, ref.entity, ref.column)} = {x}")]
            conds += [Sql(f"{self.col(r, ref.entity, c)} = ", self.const(v, ref.entity, c)) for c, v in ref.where]
            return Sql(f"({x} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {self.rel(ref.entity)} {r} WHERE ",
                       Sql.join(" AND ", conds), "))")
        raise CompileError(f"unknown op {a.op}")

    def structure(self, test: str, value: bool, x: str, s: _Scope) -> Sql:
        """has_children / has_parents / in_hierarchy of the non-NULL key expression x (no outer parentheses)."""
        h, e = self.h, s.sub("e")
        edges = self.rel(h.edge)
        p, c = self.col(e, h.edge, h.parent), self.col(e, h.edge, h.child)
        down = f"EXISTS (SELECT 1 FROM {edges} {e} WHERE {p} = {x} AND {c} IS NOT NULL)"
        up = f"EXISTS (SELECT 1 FROM {edges} {e} WHERE {c} = {x} AND {p} IS NOT NULL)"
        expr = {"has_children": down, "has_parents": up, "in_hierarchy": f"({down} OR {up})"}[test]
        return Sql(expr if value else "NOT " + expr)

    def endpoint_exists(self, a: ExistsAtom, s: _Scope) -> Sql:
        h, n = self.h, s.sub("n")
        x = self.endpoint(s.alias, a.end)
        negation = "" if a.exists else "NOT "
        return Sql(f"({x} IS NOT NULL AND {negation}EXISTS (SELECT 1 FROM {self.rel(h.node)} {n} "
                   f"WHERE {self.node_key(n)} = {x}))")

    def endpoint_attribute(self, a: ColumnAtom, s: _Scope) -> Sql:
        """True only when the endpoint is non-NULL, its node row exists and the comparison holds on that row."""
        h, n = self.h, s.sub("n")
        x = self.endpoint(s.alias, a.end)
        test = self.column_test(a, n, h.node, s)
        return Sql(f"(EXISTS (SELECT 1 FROM {self.rel(h.node)} {n} WHERE {self.node_key(n)} = {x} AND ", test, "))")

    def quantified(self, a: QuantifiedAtom, s: _Scope) -> Sql:
        """all: at least one neighbour and every neighbour has a node row satisfying the sub-where; any: some
        neighbour's node row satisfies it; none: no neighbour's node row satisfies it (true without neighbours)."""
        h = self.h
        e, n = s.sub("e"), s.sub("n")
        down = a.direction == "children"
        key = self.node_key(s.alias)
        edges, nodes = self.rel(h.edge), self.rel(h.node)
        near = self.col(e, h.edge, h.parent if down else h.child)     # the end that is the node itself
        far = self.col(e, h.edge, h.child if down else h.parent)      # the neighbour
        sub = self.conjunction(a.where, _Scope(n, h.node, s.depth + 1))
        neighbour_key = self.node_key(n)
        if a.quantifier == "all":
            nonempty = self.structure("has_children" if down else "has_parents", True, key, s)
            return Sql("(", nonempty, f" AND NOT EXISTS (SELECT 1 FROM {edges} {e} WHERE {near} = {key} AND {far} "
                       f"IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {nodes} {n} WHERE {neighbour_key} = {far} AND ",
                       sub, ")))")
        exists = Sql(f"EXISTS (SELECT 1 FROM {edges} {e} JOIN {nodes} {n} ON {neighbour_key} = {far} "
                     f"WHERE {near} = {key} AND ", sub, ")")
        return Sql("(", exists, ")") if a.quantifier == "any" else Sql("(NOT ", exists, ")")

    def conjunction(self, atoms: tuple, s: _Scope) -> Sql:
        """Atoms joined by AND; parenthesized when there are several (every atom is parenthesized itself)."""
        compiled = self.atoms(atoms, s)
        joined = Sql.join(" AND ", compiled)
        return joined if len(compiled) == 1 else Sql("(", joined, ")")

    def disjunction(self, a: AnyAtom, s: _Scope) -> Sql:
        return Sql("(", Sql.join(" OR ", [self.conjunction(branch, s) for branch in a.branches]), ")")

    # ---- report forms ------------------------------------------------------------------------------
    def project(self, rule, source: Sql, where: Sql, derived: tuple = ()) -> Sql:
        """The report form of a row-wise rule over alias t: key, distinct or columns (+ derived columns and n)."""
        entity, report = rule.entity, rule.report
        if report.form == "key":
            items = ", ".join(_item(self.col("t", entity, c), c) for c in report.columns)
            return Sql(f"SELECT {items} ", source, " WHERE ", where)
        if report.form == "distinct":
            column = report.columns[0]
            x = self.col("t", entity, column)
            return Sql(f"SELECT DISTINCT {_item(x, column)} ", source, " WHERE ", where, f" AND {x} IS NOT NULL")
        if report.form != "columns":
            raise CompileError(f"{rule.pos}: report form {report.form} does not apply to kind {rule.kind}")
        exprs = [(self.col("t", entity, c), c) for c in report.columns] + list(derived)
        items = ", ".join(_item(x, name) for x, name in exprs)
        group = ", ".join(x for x, _name in exprs)
        return Sql(f'SELECT {items}, count(*) AS "n" ', source, " WHERE ", where, f" GROUP BY {group}")

    # ---- kinds -------------------------------------------------------------------------------------
    def rule(self, rule) -> Sql:
        return getattr(self, f"kind_{rule.kind}")(rule)

    def kind_row(self, rule) -> Sql:
        where = Sql.join(" AND ", self.atoms(rule.where, _Scope("t", rule.entity)))
        return self.project(rule, Sql(f"FROM {self.rel(rule.entity)} t"), where)

    def kind_duplicate(self, rule) -> Sql:
        entity, h = rule.entity, self.h
        joins: dict = {}                      # end -> alias of the endpoint's node row, in order of first use
        keys = []                             # (expression, output name)
        for part in rule.key:
            if part.end:
                alias = joins.setdefault(part.end, "np" if part.end == "parent" else "nc")
                keys.append((self.col(alias, h.node, part.column), part.name))
            else:
                keys.append((self.col("t", entity, part.column), part.name))
        source = f"FROM {self.rel(entity)} t"
        for end, alias in joins.items():
            source += f" JOIN {self.rel(h.node)} {alias} ON {self.node_key(alias)} = {self.endpoint('t', end)}"
        size = f"count(DISTINCT {self.col('t', entity, rule.count)})" if rule.count else "count(*)"
        notnull = " AND ".join(f"{x} IS NOT NULL" for x, _name in keys)
        group = ", ".join(x for x, _name in keys)
        base = Sql(f"{source} WHERE {notnull} GROUP BY {group} HAVING {size} >= ", Param(rule.min, "integer"))
        if rule.report.form == "group":
            items = ", ".join(_item(x, name) for x, name in keys)
            return Sql(f'SELECT {items}, {size} AS "n" ', base)
        column = ident.quote(rule.report.columns[0])
        return Sql(f"SELECT DISTINCT d.{column} FROM (SELECT {self.col('t', entity, rule.report.columns[0])} AS "
                   f"{column} ", base, f") d WHERE d.{column} IS NOT NULL")

    def kind_mode_outlier(self, rule) -> Sql:
        entity = rule.entity
        table = self.rel(entity)
        g, v = self.col("t", entity, rule.group_by), self.col("t", entity, rule.value)
        num, den = Decimal(rule.min_mode_share).as_integer_ratio()
        distinct = ""
        conds = []
        if rule.min_distinct:
            column, n = rule.min_distinct
            distinct = f", count(DISTINCT {self.col('t', entity, column)}) AS n_distinct"
            conds.append(Sql("g.n_distinct >= ", Param(n, "integer")))
        conds.append(Sql("f.n_val * ", Param(den, "number"), " >= ", Param(num, "number"), " * g.n_rows"))
        conds.append(Sql("f.val > 0"))
        ratio = Param(rule.ratio, "number")
        ctes = Sql(f"WITH g AS (SELECT {g} AS grp, count(*) AS n_rows{distinct} FROM {table} t WHERE {g} IS NOT NULL "
                   f"GROUP BY {g}), f AS (SELECT {g} AS grp, {v} AS val, count(*) AS n_val FROM {table} t WHERE "
                   f"{g} IS NOT NULL AND {v} IS NOT NULL GROUP BY {g}, {v}), m AS (SELECT f.grp, f.val AS mode_value "
                   "FROM f JOIN g ON g.grp = f.grp WHERE ", Sql.join(" AND ", conds), ") ")
        flag = Sql(f"{v} IS NOT NULL AND ({v} >= ", ratio, " * m.mode_value OR ", ratio, f" * {v} <= m.mode_value)")
        body = self.project(rule, Sql(f"FROM {table} t JOIN m ON m.grp = {g}"), flag,
                            (("m.mode_value", "mode"),))
        return Sql(ctes, body)

    def kind_same_signature(self, rule) -> Sql:
        h = self.h
        edges, nodes = self.rel(h.edge), self.rel(h.node)
        p, c, q = (self.col("t", h.edge, x) for x in (h.parent, h.child, h.quantity))
        key = self.node_key("n")
        child = _type(self.plan, h.edge, h.child)
        if _comparable(child) == "text":
            kid_text, kid_order = "encode(convert_to(e.kid, 'UTF8'), 'hex')", 'e.kid COLLATE "C"'
        elif child.base == "numeric":
            kid_text, kid_order = "CAST(trim_scale(e.kid) AS text)", "e.kid"
        else:
            kid_text, kid_order = "CAST(e.kid AS text)", "e.kid"
        text_key = _comparable(_type(self.plan, h.node, h.key)) == "text"
        smallest = 'min(k.member COLLATE "C")' if text_key else "min(k.member)"
        attrs = ", ".join(f"{self.col('n', h.node, a)} AS a{i}" for i, a in enumerate(rule.attrs, 1))
        partition = ", ".join(f"k.a{i}" for i in range(1, len(rule.attrs) + 1))
        return Sql(f"WITH e AS (SELECT {p} AS node, {c} AS kid, sum({q}) AS qsum FROM {edges} t WHERE {p} IS NOT NULL "
                   f"AND {c} IS NOT NULL GROUP BY {p}, {c}), s AS (SELECT e.node, string_agg({kid_text} || ':' || "
                   f"CAST(trim_scale(e.qsum) AS text), ',' ORDER BY {kid_order}) AS sig FROM e GROUP BY e.node), "
                   f"k AS (SELECT {key} AS member, s.sig, {attrs} FROM s JOIN {nodes} n ON {key} = s.node), "
                   f"c AS (SELECT k.member, {smallest} OVER w AS group_id, count(*) OVER w AS members FROM k "
                   f"WINDOW w AS (PARTITION BY k.sig, {partition})) "
                   'SELECT c.group_id AS "group_id", c.member AS "member" FROM c WHERE c.members >= ',
                   Param(rule.min_members, "integer"))

    def endpoints(self) -> tuple:
        """The parent and child expressions of an edge row (alias t) with one type: a recursive CTE needs the same
        column type in both of its terms, so when the two columns' types differ (varchar(20) and text, integer and
        bigint) both are cast to their class's common type (text; bigint for integers, numeric otherwise)."""
        h = self.h
        p, c = self.col("t", h.edge, h.parent), self.col("t", h.edge, h.child)
        types = [_type(self.plan, h.edge, x) for x in (h.parent, h.child)]
        rendered = {"text" if t.type_class == "bpchar" else declared_sql_type(t) for t in types}
        if len(rendered) == 1:
            return p, c
        if _comparable(types[0]) == "text":
            common = "text"
        else:
            common = "bigint" if all(t.base in ("int2", "int4", "int8") for t in types) else "numeric"
        return tuple(x if x.endswith(f" AS {common})") else f"CAST({x} AS {common})" for x in (p, c))

    def heights(self, cap: Param) -> Sql:
        """WITH RECURSIVE edge (distinct non-NULL edges), h (node, length of a path down to a leaf; a dangling child
        is a leaf), ht (node, height): paths are followed while shorter than cap, so a height reaches cap only when
        the cap could not bound it."""
        h = self.h
        p, c = self.endpoints()
        return Sql(f"WITH RECURSIVE edge AS (SELECT DISTINCT {p} AS p, {c} AS c FROM {self.rel(h.edge)} t WHERE {p} "
                   f"IS NOT NULL AND {c} IS NOT NULL), h(node, d) AS (SELECT e.c, 0 FROM edge e WHERE NOT EXISTS "
                   "(SELECT 1 FROM edge x WHERE x.p = e.c) UNION SELECT e.p, h.d + 1 FROM edge e JOIN h ON "
                   "e.c = h.node WHERE h.d < ", cap, "), ht AS (SELECT h.node, max(h.d) AS height FROM h GROUP BY "
                   "h.node)")

    def height_cap(self) -> Param:
        return Param(self.h.max_depth + 1, "integer")

    def kind_stored_metric(self, rule) -> Sql:
        h = self.h
        key, stored = self.node_key("n"), self.col("n", h.node, rule.stored)
        zero, below, above, leaf, outside = HEIGHT_KIND_CASE
        return Sql(self.heights(self.height_cap()),
                   f" SELECT {_item(key, h.key)}, CASE WHEN EXISTS (SELECT 1 FROM edge x WHERE x.p = {key}) THEN CASE "
                   f"WHEN {stored} = 0 THEN {zero} WHEN {stored} < ht.height THEN {below} ELSE {above} END WHEN EXISTS "
                   f"(SELECT 1 FROM edge x WHERE x.c = {key}) THEN {leaf} ELSE {outside} END AS \"kind\", "
                   f"{stored} AS \"stored\", COALESCE(ht.height, 0) AS \"height\" FROM {self.rel(h.node)} n LEFT JOIN "
                   f"ht ON ht.node = {key} WHERE {stored} IS NOT NULL AND {stored} <> COALESCE(ht.height, 0)")

    def kind_text_quantity(self, rule) -> Sql:
        h = self.h
        quantity = self.col("t", h.edge, rule.quantity)
        text = self.col("en", h.node, rule.text)
        if rule.pattern.captures == 2:
            divisor = Sql("CASE WHEN rx.g[2] IS NULL THEN CAST(1 AS numeric) WHEN rx.g[2] ~ ",
                          Param(DIV_CAPTURE, "text"), " THEN CAST(rx.g[2] AS numeric) END")
        else:
            divisor = Sql("CAST(1 AS numeric)")
        source = Sql(f"FROM {self.rel(h.edge)} t JOIN {self.rel(h.node)} en ON {self.node_key('en')} = "
                     f"{self.endpoint('t', rule.end)} CROSS JOIN LATERAL (SELECT regexp_match({text}, ",
                     Param(rule.pattern.are, "text"), ") AS g) rx CROSS JOIN LATERAL (SELECT CASE WHEN rx.g[1] ~ ",
                     Param(NUM_CAPTURE, "text"), " THEN CAST(rx.g[1] AS numeric) END AS numerator, ", divisor,
                     " AS divisor) x")
        flag = Sql(f"{quantity} IS NOT NULL AND rx.g IS NOT NULL AND x.numerator > 0 AND x.divisor > 0 AND "
                   f"abs({quantity} * x.divisor - x.numerator) <= ", Param(rule.rel_tol, "number"), " * x.numerator")
        where = Sql.join(" AND ", self.atoms(rule.where_end, _Scope("en", h.node)) + [flag])
        return self.project(rule, source, where, (("x.numerator", "numerator"), ("x.divisor", "divisor")))


# ---- rule statements ----------------------------------------------------------------------------------
def _check_params(statement: Statement, label: str, pos: str) -> Statement:
    if len(statement.params) > MAX_PARAMS:
        raise RuleSpecError(f"{label} {pos}: binds more than {MAX_PARAMS} values in one statement: use smaller sets "
                            "or fewer example rows")
    return statement


def rule_statement(cfg, spec, plan: TypePlan, rule) -> Statement:
    """The compiled SELECT of a rule over the configured tables; its columns are rule.columns, in order."""
    sql = _Compiler(cfg, spec, plan, _Relations(cfg)).rule(rule)
    return _check_params(Statement(sql, f"rule {rule.id} ({rule.kind})"), spec.label, rule.pos)


def limited(statement: Statement, limit: int) -> Statement:
    """SELECT * FROM (<statement>) r LIMIT CAST(:limit AS bigint): at most limit rows (max_rule_rows + 1, so a
    rule with more output rows than the budget is detected without reading them all)."""
    return Statement(Sql("SELECT * FROM (", statement.sql, ") r LIMIT ", Param(limit, "integer", name=LIMIT_PARAM)),
                     statement.label)


def _cell_param(value, t: ColumnType) -> Param:
    """An example cell bound with its column's declared type (the value fits it: apply_types checked)."""
    declared = declared_sql_type(t)
    if t.type_class == "number":
        if value is None or t.base == "numeric":
            return Param(value, "number", declared)
        return Param(value if type(value) is int else int(Decimal(value)), "integer", declared)
    kind = "boolean" if t.type_class == "boolean" else "text"
    return Param(value, kind, declared)


def example_statement(cfg, spec, plan: TypePlan, rule, example) -> Statement:
    """The rule compiled over CTEs of one authored example's rows: WITH "sxc_ex_<entity>"(<columns the rule reads>)
    AS (VALUES (CAST(:pN AS <declared type>), ...), ...) per entity (an empty entity selects typed NULLs WHERE
    false), then SELECT * FROM (<rule>) r. No table is read."""
    rel = _Relations(cfg, example=True)
    compiler = _Compiler(cfg, spec, plan, rel)
    body = compiler.rule(rule)
    extra = sorted(f"{e}.{c}" for e, c in compiler.columns if c not in rule.reads.get(e, ()))
    if extra or not rel.used <= set(rule.reads):
        raise CompileError(f"{rule.pos}: the compiled rule reads columns its example rows do not hold ({extra})")
    ctes = []
    for entity, columns in rule.reads.items():
        types = [_type(plan, entity, c) for c in columns]
        head = f"{ident.quote(example_relation(entity))}({', '.join(ident.quote(c) for c in columns)}) AS "
        rows = example.rows.get(entity, ())
        if rows:
            values = [Sql("(", Sql.join(", ", [_cell_param(row[c], t) for c, t in zip(columns, types)]), ")")
                      for row in rows]
            ctes.append(Sql(head, "(VALUES ", Sql.join(", ", values), ")"))
        else:
            nulls = ", ".join(f"CAST(NULL AS {declared_sql_type(t)})" for t in types)
            ctes.append(Sql(head, f"(SELECT {nulls} WHERE false)"))
    sql = Sql("WITH ", Sql.join(", ", ctes), " SELECT * FROM (", body, ") r")
    return _check_params(Statement(sql, f"example {rule.id}[{example.index}]"), spec.label, example.pos)


# ---- snapshot gates and exports -----------------------------------------------------------------------
def _table(cfg, entity: str) -> str:
    return ident.qualified(cfg.entities[entity].table)


def encoding_statement() -> Statement:
    """SHOW server_encoding: the rules run only on a UTF8 server (code-point order and text equality)."""
    return Statement(Sql("SHOW server_encoding"), "gate encoding")


def budget_statement(cfg, entity: str, limit: int) -> Statement:
    """The entity's row count, read no further than limit rows (max_export_rows + 1)."""
    return Statement(Sql(f"SELECT count(*) FROM (SELECT 1 FROM {_table(cfg, entity)} LIMIT ",
                         Param(limit, "integer", name=LIMIT_PARAM), ") AS bounded"), f"gate budget {entity}")


def key_gate_statement(cfg, plan: TypePlan, entity: str, key=None) -> Statement:
    """null_keys (rows with a NULL key column) and duplicate_keys (non-NULL keys held by more than one row)."""
    key = tuple(key or cfg.entities[entity].key)
    table = _table(cfg, entity)
    nulls = " OR ".join(f"{_column(plan, 't', entity, c)} IS NULL" for c in key)
    full = [_column(plan, "k", entity, c) for c in key]
    notnull = " AND ".join(f"{x} IS NOT NULL" for x in full)
    return Statement(Sql(f"SELECT count(*) FILTER (WHERE {nulls}) AS null_keys, (SELECT count(*) FROM (SELECT 1 FROM "
                         f"{table} k WHERE {notnull} GROUP BY {', '.join(full)} HAVING count(*) > 1) d) AS "
                         f"duplicate_keys FROM {table} t"), f"gate key {entity}")


def non_finite_statement(cfg, plan: TypePlan, entity: str, columns) -> Statement:
    """Per numeric column, the rows holding NaN, Infinity or -Infinity (columns of base type numeric only)."""
    tests = []
    for c in columns:
        if _type(plan, entity, c).base != "numeric":
            raise CompileError(f"column {c} of entity {entity} is not numeric")
        tests.append(f"count(*) FILTER (WHERE t.{ident.quote(c)} IN (CAST('NaN' AS numeric), CAST('Infinity' AS "
                     f"numeric), CAST('-Infinity' AS numeric))) AS {ident.quote(c)}")
    if not tests:
        raise CompileError("the non-finite gate needs at least one column")
    return Statement(Sql(f"SELECT {', '.join(tests)} FROM {_table(cfg, entity)} t"), f"gate non_finite {entity}")


def null_quantity_statement(cfg, spec, plan: TypePlan) -> Statement:
    """Edges (both ends non-NULL) with a NULL hierarchy quantity (same_signature)."""
    h = spec.hierarchy
    p, c, q = (_column(plan, "t", h.edge, x) for x in (h.parent, h.child, h.quantity))
    return Statement(Sql(f"SELECT count(*) FROM {_table(cfg, h.edge)} t WHERE {p} IS NOT NULL AND {c} IS NOT NULL "
                         f"AND {q} IS NULL"), f"gate null_quantity {h.edge}")


def height_gate_statement(cfg, spec, plan: TypePlan) -> Statement:
    """unbounded: nodes whose height the cap max_depth + 1 cannot bound (a height that reached the cap, or a parent
    without any height: a cycle, or a chain deeper than max_depth) (stored_metric)."""
    compiler = _Compiler(cfg, spec, plan, _Relations(cfg))
    cap = compiler.height_cap()
    return Statement(Sql(compiler.heights(cap), " SELECT (SELECT count(*) FROM ht WHERE ht.height >= ", cap,
                         ") + (SELECT count(*) FROM (SELECT DISTINCT edge.p FROM edge) x WHERE NOT EXISTS "
                         "(SELECT 1 FROM ht WHERE ht.node = x.p)) AS unbounded"), f"gate height {spec.hierarchy.edge}")


def text_length_statement(cfg, plan: TypePlan, entity: str, columns) -> Statement:
    """Per text column, the longest value in characters (text_quantity's max_text_length)."""
    items = [f"max(length({_column(plan, 't', entity, c)})) AS {ident.quote(c)}" for c in columns]
    if not items:
        raise CompileError("the text length gate needs at least one column")
    return Statement(Sql(f"SELECT {', '.join(items)} FROM {_table(cfg, entity)} t"), f"gate text_length {entity}")


def export_statement(cfg, plan: TypePlan, entity: str, columns) -> Statement:
    """The entity's rows for the Python evaluation: the given columns, bpchar ones cast to text."""
    items = ", ".join(_item(_column(plan, "t", entity, c), c) for c in columns)
    if not items:
        raise CompileError("an export needs at least one column")
    return Statement(Sql(f"SELECT {items} FROM {_table(cfg, entity)} t"), f"export {entity}")


def needed_entities(cfg, rules) -> tuple:
    """The entities the rules read, in configuration order."""
    used = {e for r in rules for e in r.reads}
    return tuple(e for e in cfg.entities if e in used)


def export_columns(cfg, spec, plan: TypePlan, rules) -> dict:
    """{entity: columns} to export for the rules: the entity's key columns and its hierarchy columns when their
    types are usable, then every column the rules read."""
    h = spec.hierarchy
    out = {}
    for entity in needed_entities(cfg, rules):
        candidates = list(cfg.entities[entity].key)
        if h is not None and entity == h.node:
            candidates.append(h.key)
        if h is not None and entity == h.edge:
            candidates += [h.parent, h.child, h.quantity]
        columns: list = []
        for c in candidates:
            t = plan.column(entity, c)
            if t is not None and t.usable and c not in columns:
                columns.append(c)
        for rule in rules:
            columns += [c for c in rule.reads.get(entity, ()) if c not in columns]
        out[entity] = tuple(columns)
    return out


def rule_gates(cfg, spec, plan: TypePlan, rule) -> tuple:
    """The gates a rule depends on, as (gate, entity, column) keys: the key gate of the node entity when the rule
    reads node rows by key and of an entity it reports by key, the non-finite count of each numeric column it reads,
    the NULL edge quantity (same_signature), the height gate (stored_metric) and the text length (text_quantity)."""
    h = spec.hierarchy
    out = []
    if h is not None and h.key in rule.reads.get(h.node, ()):
        out.append(("key", h.node, ""))
    if rule.report is not None and rule.report.form == "key" and ("key", rule.entity, "") not in out:
        out.append(("key", rule.entity, ""))
    for entity, columns in rule.reads.items():
        out += [("non_finite", entity, c) for c in columns if _type(plan, entity, c).base == "numeric"]
    if rule.kind == "same_signature":
        out.append(("null_quantity", h.edge, ""))
    elif rule.kind == "stored_metric":
        out.append(("height", h.edge, ""))
    elif rule.kind == "text_quantity":
        out.append(("text_length", h.node, rule.text))
    return tuple(out)


@dataclass(frozen=True)
class Gate:
    """One snapshot gate statement: gate (GATES), the entity it reads ("" for encoding) and the columns its result
    row holds one value for (key, non_finite, text_length)."""
    gate: str
    entity: str
    columns: tuple
    statement: Statement


def snapshot_gates(cfg, spec, plan: TypePlan, rules, max_export_rows: int) -> list:
    """The gate statements of the snapshot for the rules, in run order: encoding, a budget per needed entity, the
    key gates, the non-finite counts per entity, the NULL edge quantity, the height gate and the text lengths."""
    gates = [Gate("encoding", "", (), encoding_statement())]
    gates += [Gate("budget", e, (), budget_statement(cfg, e, max_export_rows + 1)) for e in needed_entities(cfg, rules)]
    wanted: dict = {}
    for rule in rules:
        for key in rule_gates(cfg, spec, plan, rule):
            wanted.setdefault(key[:2], [])
            if key[2] and key[2] not in wanted[key[:2]]:
                wanted[key[:2]].append(key[2])
    for gate in GATES[2:]:
        for entity in cfg.entities:
            if (gate, entity) not in wanted:
                continue
            columns = tuple(wanted[(gate, entity)])
            if gate == "key":
                key = tuple(cfg.entities[entity].key)
                gates.append(Gate(gate, entity, key, key_gate_statement(cfg, plan, entity, key)))
            elif gate == "non_finite":
                gates.append(Gate(gate, entity, columns, non_finite_statement(cfg, plan, entity, columns)))
            elif gate == "null_quantity":
                gates.append(Gate(gate, entity, (), null_quantity_statement(cfg, spec, plan)))
            elif gate == "height":
                gates.append(Gate(gate, entity, (), height_gate_statement(cfg, spec, plan)))
            else:
                gates.append(Gate(gate, entity, columns, text_length_statement(cfg, plan, entity, columns)))
    return gates


def gate_reasons(cfg, spec, plan: TypePlan, rules, gates, rows, max_export_rows: int) -> dict:
    """{rule id: not_run reason} from the first result row of each gate (rows aligned with gates). The first reason
    in GATE_REASONS order decides: server_encoding (not UTF8) and resource_limit (budget total above
    max_export_rows) for every rule, then key_null, key_not_unique, non_finite_values, null_values,
    cycle_or_depth_limit and value_too_long (a value longer than the rule's max_text_length). Rules whose gates all
    passed are absent."""
    tripped: dict = {}
    lengths: dict = {}
    every = ""
    total = 0
    for gate, row in zip(gates, rows, strict=True):
        if gate.gate == "encoding":
            if row[0] != "UTF8":
                every = every or "server_encoding"
        elif gate.gate == "budget":
            total += int(row[0])
        elif gate.gate == "key":
            nulls, duplicates = int(row[0]), int(row[1])
            if nulls or duplicates:
                tripped[("key", gate.entity, "")] = "key_null" if nulls else "key_not_unique"
        elif gate.gate == "non_finite":
            for column, count in zip(gate.columns, row, strict=True):
                if int(count):
                    tripped[("non_finite", gate.entity, column)] = "non_finite_values"
        elif gate.gate == "null_quantity":
            if int(row[0]):
                tripped[("null_quantity", gate.entity, "")] = "null_values"
        elif gate.gate == "height":
            if int(row[0]):
                tripped[("height", gate.entity, "")] = "cycle_or_depth_limit"
        elif gate.gate == "text_length":
            for column, longest in zip(gate.columns, row, strict=True):
                lengths[(gate.entity, column)] = int(longest or 0)
        else:
            raise CompileError(f"unknown gate {gate.gate}")
    if not every and total > max_export_rows:
        every = "resource_limit"
    out = {}
    for rule in rules:
        if every:
            out[rule.id] = every
            continue
        found = []
        for key in rule_gates(cfg, spec, plan, rule):
            if key[0] == "text_length":
                if lengths.get(key[1:], 0) > rule.max_text_length:
                    found.append("value_too_long")
            elif key in tripped:
                found.append(tripped[key])
        if found:
            out[rule.id] = min(found, key=GATE_REASONS.index)
    return out


# ---- lookups ------------------------------------------------------------------------------------------
def lookup_statement(cfg, rule, limit: int) -> Statement:
    """v_rule_<id> in ord order, at most limit rows: the rule's output columns, then ord."""
    columns = ", ".join(f"v.{ident.quote(c)}" for c in rule.columns)
    return Statement(Sql(f"SELECT {columns}, v.{ORD} FROM {ident.qualified(data_rules.view_name(cfg, rule.id))} v "
                         f"ORDER BY v.{ORD} LIMIT ", Param(limit, "integer", name=LIMIT_PARAM)), f"lookup {rule.id}")


SUBJECT_COLUMNS = ("subject", "severity", "severity_rank", "rule_count", "rules", ORD)


def subjects_lookup_statement(cfg, limit: int) -> Statement:
    """v_rules_subjects in ord order, at most limit rows (SUBJECT_COLUMNS)."""
    columns = ", ".join(f"v.{c}" for c in SUBJECT_COLUMNS)
    return Statement(Sql(f"SELECT {columns} FROM {ident.qualified(data_rules.subjects_view_name(cfg))} v ORDER BY "
                         f"v.{ORD} LIMIT ", Param(limit, "integer", name=LIMIT_PARAM)), "lookup subjects")


# ---- views --------------------------------------------------------------------------------------------
def view_query(cfg, spec, plan: TypePlan, rule) -> str:
    """The SELECT of v_rule_<id>: the rule in literal form plus ord, the row number in canonical order (each output
    column NULLS FIRST, text by COLLATE "C", numbers by value, false before true)."""
    if ORD in rule.columns:
        raise RuleSpecError(f"{spec.label} {rule.pos}: an output column named {ORD} collides with the order column of "
                            "its lookup view: rename the column in a database view, or set views = false")
    order = []
    for name, cls in zip(rule.columns, output_types(spec, plan, rule)):
        collate = ' COLLATE "C"' if cls in ("text", "bpchar") else ""
        order.append(f"r.{ident.quote(name)}{collate} NULLS FIRST")
    inner = rule_statement(cfg, spec, plan, rule).literal()
    return f"SELECT r.*, row_number() OVER (ORDER BY {', '.join(order)}) AS {ORD} FROM ({inner}) r"


def view_statement(cfg, spec, plan: TypePlan, rule) -> str:
    """CREATE OR REPLACE VIEW of v_rule_<id>, exactly as build runs it and records it in build.sql (data-rules
    regenerates it to tell a current view from a stale one)."""
    view = ident.qualified(data_rules.view_name(cfg, rule.id))
    return f"CREATE OR REPLACE VIEW {view} AS\n{view_query(cfg, spec, plan, rule)};"


def subjects_view_query(cfg, spec, plan: TypePlan) -> str:
    """The SELECT of v_rules_subjects over the subject rules' views: one row per subject key with its worst severity
    (severity_rank 0 violation, 1 warning), rule_count, rules (text[] in configuration order) and ord (by severity
    rank, then subject: COLLATE "C" for text keys)."""
    rules = spec.subject_rules
    h = spec.hierarchy
    if not rules or h is None:
        raise CompileError("the subjects view needs at least one rule with a subject")
    branches = []
    for rule in rules:
        subject = ident.quote(rule.subject)
        branches.append(Sql(f"SELECT v.{subject} AS subject, ", Param(rule.id, "text"), " AS rule_id, ",
                            Param(rule.index, "integer"), " AS rule_ord, ",
                            Param(data_rules.SEVERITY_RANK[rule.severity], "integer"),
                            f" AS severity_rank FROM {ident.qualified(data_rules.view_name(cfg, rule.id))} v "
                            f"WHERE v.{subject} IS NOT NULL"))
    collate = ' COLLATE "C"' if _comparable(_type(plan, h.node, h.key)) == "text" else ""
    sql = Sql("WITH u AS (", Sql.join(" UNION ALL ", branches), "), d AS (SELECT DISTINCT u.subject, u.rule_id, "
              "u.rule_ord, u.severity_rank FROM u), g AS (SELECT d.subject, min(d.severity_rank) AS severity_rank, "
              "count(*) AS rule_count, array_agg(d.rule_id ORDER BY d.rule_ord) AS rules FROM d GROUP BY d.subject) "
              "SELECT g.subject, CASE g.severity_rank WHEN 0 THEN ", Param("violation", "text"), " ELSE ",
              Param("warning", "text"), " END AS severity, g.severity_rank, g.rule_count, g.rules, row_number() OVER "
              f"(ORDER BY g.severity_rank, g.subject{collate}) AS {ORD} FROM g")
    return Statement(sql).literal()


def subjects_view_statement(cfg, spec, plan: TypePlan) -> str:
    """CREATE OR REPLACE VIEW of v_rules_subjects, as build runs and records it."""
    view = ident.qualified(data_rules.subjects_view_name(cfg))
    return f"CREATE OR REPLACE VIEW {view} AS\n{subjects_view_query(cfg, spec, plan)};"


def comment_statement(view: str) -> str:
    """The marker comment on a generated view (view as schema.name): orphan cleanup drops only marked views."""
    return f"COMMENT ON VIEW {ident.qualified(view)} IS '{VIEW_MARKER}'"


def drop_view_statement(view: str) -> str:
    """DROP VIEW IF EXISTS for a generated view (schema.name); never CASCADE."""
    return f"DROP VIEW IF EXISTS {ident.qualified(view)}"


def orphan_statement(cfg) -> Statement:
    """Names of the views in the output schema that carry the exact marker comment and the v_rule_ prefix or the
    subjects view's name: the candidates build drops when the rule file no longer generates them."""
    return Statement(Sql(
        "SELECT CAST(c.relname AS text) AS relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace ns ON "
        "ns.oid = c.relnamespace JOIN pg_catalog.pg_description d ON d.objoid = c.oid AND d.classoid = "
        "CAST('pg_catalog.pg_class' AS regclass) AND d.objsubid = 0 WHERE ns.nspname = ",
        Param(ident.check(cfg.source.output_schema), "text"), " AND c.relkind = 'v' AND d.description = ",
        Param(VIEW_MARKER, "text"), " AND (starts_with(CAST(c.relname AS text), ",
        Param(data_rules.VIEW_PREFIX, "text"), ") OR CAST(c.relname AS text) = ",
        Param(data_rules.SUBJECTS_VIEW, "text"), ") ORDER BY 1"), "orphan views")


# ---- rules.sql ----------------------------------------------------------------------------------------
def rules_sql(statements) -> str:
    """The text of data_rules/rules.sql: the header, then each statement's label as a comment and its parameterized
    text (placeholders only, no values)."""
    blocks = []
    for st in statements:
        text, _params = st.parameterized()
        label = _LABEL_UNSAFE.sub("?", st.label)
        blocks.append((f"-- {label}\n" if label else "") + text + ";")
    return RULES_SQL_HEADER + "\n" + "\n\n".join(blocks) + "\n"
