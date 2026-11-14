"""Data rules: the typed rule file named by [data_rules] file, validated into an immutable model.

A rule file (TOML, version 1, UTF-8 with an optional byte order mark, at most 256 KiB) holds named constant sets,
reference sets and 1..128 rules of six kinds: row, duplicate, mode_outlier, same_signature, stored_metric and
text_quantity. Every rule names its severity explicitly, may name a subject column (an output column holding node
keys) and may carry authored examples ([[rule.example]]: small typed input rows and the rows the rule must output,
written from the business rule). load() returns a RuleFile whose rules carry what the later stages need: the atoms
of their where lists, the report form and output columns, the entities and columns each rule reads, the restricted
pattern of a text_quantity rule in its Python and PostgreSQL ARE forms, and the examples as typed cells.

Validation is closed at every level. Unknown keys, wrong types, TOML floats, NUL characters, atoms placed on the wrong
entity, unused sets or refs, duplicate output columns and lookup-view names that equal a relation build already
generates are errors: RuleSpecError, a configuration error the caller reports with exit 2. Messages name positions
only (for example "data-rules.toml rule[2] kit_without_bom where[1]", indexes counted from 0 as in crosscheck.toml),
never a configured constant, a cell or the pattern, because messages reach results and consoles.

Types need the connected catalog: apply_types() checks every atom, set member, ref, endpoint column, kind column and
example value against a TypePlan (one ColumnType per entity column) and returns the rules that read a column of an
unsupported type or a nondeterministic collation, which are not run. Example values must fit their declared types
exactly, so that no rounding or truncation happens on either evaluation path.

cell() and sort_key() give the canonical comparison form of output cells and their typed order (NULL < false < true
< numbers by value < text by code point, which is UTF-8 byte order and COLLATE "C"). view_name() and SUBJECTS_VIEW
name the lookup views in the output schema.

This module imports only the standard library, canon and ident; it never imports a stage module, the compiler or the
evaluator.
"""

import hashlib
import os
import re
import string
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath

from . import canon, ident

FILE_VERSION = 1
MAX_FILE_BYTES = 256 * 1024
MAX_RULES = 128
MAX_ATOMS = 32                      # atoms per conjunction (a where list, an any branch, a sub-where, where_end)
MIN_BRANCHES, MAX_BRANCHES = 2, 8   # branches of one disjunction
MAX_SET_MEMBERS = 1000
MAX_EXAMPLES = 8                    # per rule
MAX_EXAMPLE_ROWS = 200              # input rows per example, all entities together (and expected rows per example)
MAX_EXAMPLE_CELLS = 4000            # input cells per example
MAX_PATTERN_LENGTH = 256
MAX_QUANTIFIERS = 16
MAX_REPEAT = 255                    # largest bound of {m}, {m,} and {m,n}
DEFAULT_MAX_TEXT_LENGTH = 4096
MAX_TEXT_LENGTH = 1000000
MAX_BIGINT = 2 ** 63 - 1
DEFAULT_LABEL = "data-rules.toml"

KINDS = ("row", "duplicate", "mode_outlier", "same_signature", "stored_metric", "text_quantity")
SEVERITIES = ("violation", "warning")
SEVERITY_RANK = {"violation": 0, "warning": 1}
COMPARE_OPS = ("eq", "ne", "lt", "le", "gt", "ge")
ORDER_OPS = ("lt", "le", "gt", "ge")
SET_OPS = ("in", "not_in", "starts_with")
NULL_OPS = ("is_null", "is_not_null")
OPS = COMPARE_OPS + SET_OPS + ("not_in_ref",) + NULL_OPS
OPERAND = {**dict.fromkeys(COMPARE_OPS, "value"), **dict.fromkeys(SET_OPS, "set"), "not_in_ref": "ref",
           **dict.fromkeys(NULL_OPS, "")}
ENDS = ("parent", "child")
STRUCTURE_TESTS = ("has_children", "has_parents", "in_hierarchy")
QUANTIFIERS = ("all", "any", "none")
DIRECTIONS = ("children", "parents")
HEIGHT_KINDS = ("has_children_stored_zero", "stored_below", "stored_above", "leaf_stored_nonzero",
                "outside_stored_nonzero")
VIEW_PREFIX = "v_rule_"
SUBJECTS_VIEW = "v_rules_subjects"   # 's' at index 6, where every v_rule_<id> has '_': the names never collide
TYPE_CLASSES = ("number", "text", "bpchar", "boolean", "other")
NOT_RUN_REASONS = ("column_type", "collation")
INTEGER_RANGES = {"int2": (-2 ** 15, 2 ** 15 - 1), "int4": (-2 ** 31, 2 ** 31 - 1), "int8": (-2 ** 63, 2 ** 63 - 1)}

COMMON_KEYS = ("id", "kind", "severity", "subject", "example")
KIND_KEYS = {
    "row": ("entity", "where", "report"),
    "duplicate": ("entity", "key", "count", "min", "report"),
    "mode_outlier": ("entity", "group_by", "value", "min_mode_share", "ratio", "min_distinct", "report"),
    "same_signature": ("attrs", "min_members"),
    "stored_metric": ("metric", "stored"),
    "text_quantity": ("entity", "end", "text", "where_end", "pattern", "quantity", "rel_tol", "max_text_length",
                      "report"),
}
REQUIRED_KEYS = {
    "row": ("entity", "where"),
    "duplicate": ("entity", "key"),
    "mode_outlier": ("entity", "group_by", "value", "min_mode_share", "ratio"),
    "same_signature": ("attrs",),
    "stored_metric": ("metric", "stored"),
    "text_quantity": ("entity", "end", "text", "pattern", "quantity", "rel_tol"),
}

_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RULE_ID = re.compile(r"[a-z_][a-z0-9_]{0,55}")      # v_rule_<id> stays a 63-character identifier
_NUMERAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_TOML_POSITION = re.compile(r"\(at (line \d+, column \d+|end of document)\)")


class RuleSpecError(ValueError):
    """An invalid rule file or rule use: a configuration error the caller reports with exit 2. The message names
    positions (rule index and id, atom index, set or ref name, column) and never a constant, a cell or the pattern."""


class PatternError(ValueError):
    """A pattern outside the supported subset; the message names the construct, never the pattern text."""


def _fail(label: str, pos: str, msg: str):
    raise RuleSpecError(f"{label} {pos}: {msg}" if pos else f"{label}: {msg}")


# ---- model -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Hierarchy:
    """The hierarchy the rules use, copied from crosscheck.toml so the evaluator needs no configuration object."""
    name: str
    node: str          # node entity
    edge: str          # edge entity
    key: str           # the node entity's single key column
    parent: str        # edge columns
    child: str
    quantity: str
    max_depth: int


@dataclass(frozen=True)
class Ref:
    """[refs.<name>]: the non-NULL values of entity.column on the rows whose where columns equal the constants."""
    name: str
    entity: str
    column: str
    where: tuple = ()  # ((column, constant), ...) in file order


@dataclass(frozen=True)
class Pattern:
    """A text_quantity pattern of the supported subset in its two forms, the same syntax written for each engine; the
    engines can still choose different matches (compile_subset)."""
    python: str        # Python `re` form; compile with re.DOTALL (compile())
    are: str           # PostgreSQL ARE form
    captures: int
    quantifiers: int

    def compile(self) -> re.Pattern:
        return re.compile(self.python, re.DOTALL)


@dataclass(frozen=True)
class ColumnAtom:
    """Comparison (value), set membership or prefix (set), reference (ref) or NULL test on a column. With end, the
    column belongs to the endpoint's node row (an endpoint attribute atom)."""
    pos: str
    column: str
    op: str
    value: object = None
    set: str = ""
    ref: str = ""
    end: str = ""


@dataclass(frozen=True)
class ExistsAtom:
    """Endpoint existence on an edge row: the endpoint is non-NULL and has (exists) or lacks a node row."""
    pos: str
    end: str
    exists: bool


@dataclass(frozen=True)
class StructureAtom:
    """has_children, has_parents or in_hierarchy of the node key (end "") or of an edge row's endpoint."""
    pos: str
    test: str
    value: bool
    end: str = ""


@dataclass(frozen=True)
class QuantifiedAtom:
    """all, any or none of the distinct children (or parents) of a node satisfy the sub-where (column and structure
    atoms on the neighbour's node row)."""
    pos: str
    direction: str
    quantifier: str
    where: tuple


@dataclass(frozen=True)
class AnyAtom:
    """A disjunction of 2..8 conjunctions."""
    pos: str
    branches: tuple


@dataclass(frozen=True)
class KeyPart:
    """A duplicate key part: an own column, or (end) a column of the endpoint's node row."""
    column: str
    end: str = ""

    @property
    def name(self) -> str:
        return f"{self.end}_{self.column}" if self.end else self.column


@dataclass(frozen=True)
class Report:
    """key (the entity key columns), distinct (the non-NULL values of one own column), columns (listed own columns,
    the kind's derived columns and n) or group (duplicate: the key parts and n)."""
    form: str
    columns: tuple = ()


@dataclass(frozen=True)
class Output:
    """One output column. source: column (values of entity.column), count (n, a positive integer), integer (height),
    number (numerator, divisor) or text (kind)."""
    name: str
    source: str
    entity: str = ""
    column: str = ""


@dataclass(frozen=True)
class Example:
    """One [[rule.example]]: rows maps every entity the rule reads to its input rows, expect lists the output rows.
    Cells are str, int or bool as written, None for { null = true }."""
    index: int
    pos: str
    spec: str
    rows: dict
    expect: tuple


@dataclass(frozen=True)
class Rule:
    index: int
    id: str
    kind: str
    severity: str
    entity: str                 # the entity whose rows the rule evaluates (the node entity for same_signature and
                                # stored_metric)
    outputs: tuple              # Output, in output order
    reads: dict                 # entity -> columns the rule reads, in first-use order
    subject: str = ""
    where: tuple = ()           # row
    report: Report | None = None
    key: tuple = ()             # duplicate: KeyPart
    count: str = ""             # duplicate: "" counts rows, otherwise the column whose distinct non-NULL values count
    min: int = 2                # duplicate
    group_by: str = ""          # mode_outlier
    value: str = ""
    min_mode_share: str = ""    # decimal string in (1/2, 1]
    ratio: str = ""             # decimal string > 1
    min_distinct: tuple = ()    # (column, n) or ()
    attrs: tuple = ()           # same_signature
    min_members: int = 2
    metric: str = ""            # stored_metric
    stored: str = ""
    end: str = ""               # text_quantity
    text: str = ""
    where_end: tuple = ()
    pattern: Pattern | None = None
    quantity: str = ""
    rel_tol: str = ""           # decimal string >= 0
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH
    examples: tuple = ()

    @property
    def pos(self) -> str:
        return f"rule[{self.index}] {self.id}"

    @property
    def config_ref(self) -> str:
        return f"data_rules.rule[{self.index}] {self.id}"

    @property
    def columns(self) -> tuple:
        return tuple(o.name for o in self.outputs)


@dataclass(frozen=True)
class RuleFile:
    label: str                  # the rule file's name, used in messages
    sha256: str                 # of the exact bytes that were parsed
    hierarchy: Hierarchy | None
    sets: dict                  # name -> tuple of members
    refs: dict                  # name -> Ref
    rules: tuple

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    @property
    def subject_rules(self) -> tuple:
        """The rules that feed v_rules_subjects, in configuration order."""
        return tuple(r for r in self.rules if r.subject)


# ---- names in the output schema ---------------------------------------------------------------------
def view_name(cfg, rule_id: str) -> str:
    """schema.view of a rule's lookup view: its output columns plus ord, in the output schema."""
    return f"{ident.check(cfg.source.output_schema)}.{ident.check(VIEW_PREFIX + rule_id)}"


def subjects_view_name(cfg) -> str:
    """schema.view of the subjects view (one row per subject key over the rules that name a subject)."""
    return f"{ident.check(cfg.source.output_schema)}.{SUBJECTS_VIEW}"


def reserved_names(cfg) -> set:
    """Relation names naming.py gives the objects build already creates in the output schema: the shape views and
    each hierarchy's closure and cycle tables (naming.shape_view, closure_table, cycles_table; tests keep the two in
    step). This module does not import naming."""
    names = {f"v_shape_{s.id}" for s in cfg.shapes}
    for h in cfg.hierarchies:
        names |= {f"{h}_closure", f"{h}_cycles"}
    return names


# ---- loading ----------------------------------------------------------------------------------------
def file_label(cfg) -> str:
    return PurePosixPath(cfg.data_rules.file.replace("\\", "/")).name or DEFAULT_LABEL


def rule_file_path(cfg) -> Path:
    if getattr(cfg, "data_rules", None) is None:
        raise RuleSpecError("crosscheck.toml: [data_rules] is not configured")
    return cfg.base_dir() / cfg.data_rules.file


def inside(path, folder) -> bool:
    """Whether path equals or lies inside folder once both are resolved (symlinks, 8.3 names, case on Windows)."""
    target, root = Path(path).resolve(), Path(folder).resolve()
    t, r = os.path.normcase(str(target)), os.path.normcase(str(root))
    if t == r or t.startswith(r.rstrip("\\/") + os.sep):
        return True
    if root.exists():
        for candidate in (target, *target.parents):
            try:
                if candidate.exists() and os.path.samefile(candidate, root):
                    return True
            except OSError:
                continue
    return False


def load(cfg, out=None) -> RuleFile:
    """Read and validate the rule file of cfg.data_rules. With out (the stage's output directory), a rule file inside
    it is refused: an output must never be an input. Raises RuleSpecError."""
    path = rule_file_path(cfg)
    label = file_label(cfg)
    if out is not None and inside(path, out):
        _fail(label, "", "the rule file must not lie inside the output directory")
    if not path.is_file():
        _fail(label, "", "the rule file named by [data_rules] file was not found (it is relative to crosscheck.toml)")
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        _fail(label, "", "the rule file could not be read")
    return loads(raw, cfg, label)


def loads(raw: bytes, cfg, label: str = DEFAULT_LABEL) -> RuleFile:
    """Validate rule-file bytes against cfg (its entities and hierarchies). Raises RuleSpecError."""
    if not isinstance(raw, bytes):
        _fail(label, "", "rule file input must be bytes")
    if len(raw) > MAX_FILE_BYTES:
        _fail(label, "", f"the rule file is larger than {MAX_FILE_BYTES // 1024} KiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        _fail(label, "", "the rule file is not UTF-8 text")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        m = _TOML_POSITION.search(str(e))      # the position only: tomllib messages can quote a character
        _fail(label, "", "is not valid TOML" + (f" ({m.group(1)})" if m else ""))
    return _Parser(cfg, label, doc).parse(hashlib.sha256(raw).hexdigest())


class _Reads:
    """Entity -> columns a rule reads, in first-use order."""

    def __init__(self):
        self.columns: dict = {}

    def add(self, entity: str, *columns: str):
        cols = self.columns.setdefault(entity, [])
        cols.extend(c for c in columns if c not in cols)

    def freeze(self) -> dict:
        return {e: tuple(c) for e, c in self.columns.items()}


class _Parser:
    def __init__(self, cfg, label: str, doc: dict):
        self.cfg, self.label, self.doc = cfg, label, doc
        self.sets: dict = {}
        self.refs: dict = {}
        self.used_sets: set = set()
        self.used_refs: set = set()
        self.h: Hierarchy | None = None

    def fail(self, pos: str, msg: str):
        _fail(self.label, pos, msg)

    def closed(self, table, allowed: tuple, pos: str):
        """A table with only the allowed keys; the message lists those, never the unknown key text."""
        if not isinstance(table, dict):
            self.fail(pos, "must be a table")
        if set(table) - set(allowed):
            self.fail(pos, f"unknown key (allowed: {', '.join(allowed)})")

    def name(self, value, pos: str, what: str) -> str:
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            self.fail(pos, f"{what} must be a lower-case identifier (^[a-z_][a-z0-9_]*$, at most 63 characters)")
        return value

    def entity(self, value, pos: str) -> str:
        if not isinstance(value, str) or value not in self.cfg.entities:
            self.fail(pos, "entity must name an [entities.<name>] table of crosscheck.toml")
        return value

    def boolean(self, value, pos: str, what: str) -> bool:
        if type(value) is not bool:
            self.fail(pos, f"{what} must be true or false")
        return value

    def integer(self, value, pos: str, what: str, lo: int, hi: int = MAX_BIGINT) -> int:
        if type(value) is not int or not lo <= value <= hi:
            bound = f"an integer >= {lo}" if hi == MAX_BIGINT else f"an integer {lo}..{hi}"
            self.fail(pos, f"{what} must be {bound}")
        return value

    def decimal(self, value, pos: str, what: str) -> Decimal:
        if type(value) is not str or not _NUMERAL.fullmatch(value):
            self.fail(pos, f'{what} must be a decimal string such as "0.75" (TOML floats are refused)')
        return Decimal(value)

    def constant(self, value, pos: str):
        """A comparison constant: a string without NUL, an integer or a boolean."""
        if type(value) is float:
            self.fail(pos, 'TOML floats are refused: write a decimal string such as "0.5" or an integer')
        if type(value) is str:
            if "\x00" in value:
                self.fail(pos, "strings must not contain NUL")
            return value
        if type(value) in (int, bool):
            return value
        self.fail(pos, "must be a string, an integer or a boolean")

    def hierarchy_required(self, pos: str, what: str) -> Hierarchy:
        if self.h is None:
            self.fail(pos, f"{what} needs a hierarchy: crosscheck.toml has no [hierarchies] entry")
        return self.h

    # ---- top level ---------------------------------------------------------------------------------
    def parse(self, sha256: str) -> RuleFile:
        doc = self.doc
        self.closed(doc, ("version", "hierarchy", "sets", "refs", "rule"), "")
        if type(doc.get("version")) is not int or doc["version"] != FILE_VERSION:
            self.fail("version", f"is required and must be {FILE_VERSION}")
        self.h = self.hierarchy(doc)
        self.sets = self.parse_sets(doc.get("sets", {}))
        self.refs = self.parse_refs(doc.get("refs", {}))
        raw_rules = doc.get("rule")
        if not isinstance(raw_rules, list) or not 1 <= len(raw_rules) <= MAX_RULES:
            self.fail("rule", f"the file needs 1..{MAX_RULES} [[rule]] tables")
        ids: set = set()
        rules = tuple(self.rule(i, raw, ids) for i, raw in enumerate(raw_rules))
        for name in self.sets:
            if name not in self.used_sets:
                self.fail(f"sets.{name}", "is not used by any rule: delete it or use it")
        for name in self.refs:
            if name not in self.used_refs:
                self.fail(f"refs.{name}", "is not used by any rule: delete it or use it")
        reserved = reserved_names(self.cfg)
        for rule in rules:
            if VIEW_PREFIX + rule.id in reserved:
                self.fail(rule.pos, f"its lookup view {VIEW_PREFIX}{rule.id} would have the name of a relation build "
                                    "already generates in the output schema: rename the rule")
        if SUBJECTS_VIEW in reserved:
            self.fail("", f"the subjects view {SUBJECTS_VIEW} would have the name of a relation build already "
                          "generates in the output schema")
        return RuleFile(self.label, sha256, self.h, self.sets, self.refs, rules)

    def hierarchy(self, doc: dict) -> Hierarchy | None:
        hierarchies = self.cfg.hierarchies
        if "hierarchy" in doc:
            name = doc["hierarchy"]
            if not isinstance(name, str) or name not in hierarchies:
                self.fail("hierarchy", "must name a [hierarchies.<name>] entry of crosscheck.toml")
            h = hierarchies[name]
        elif len(hierarchies) == 1:
            h = next(iter(hierarchies.values()))
        elif hierarchies:
            self.fail("hierarchy", "is required: crosscheck.toml has several hierarchies")
        else:
            return None
        return Hierarchy(h.name, h.node, h.edge, self.cfg.entities[h.node].key[0], h.parent, h.child, h.quantity,
                         h.max_depth)

    def parse_sets(self, raw) -> dict:
        if not isinstance(raw, dict):
            self.fail("sets", "must be a table of named lists")
        out = {}
        for name, members in raw.items():
            self.name(name, "sets", "a set name")
            pos = f"sets.{name}"
            if not isinstance(members, list) or not 1 <= len(members) <= MAX_SET_MEMBERS:
                self.fail(pos, f"must be a list of 1..{MAX_SET_MEMBERS} members")
            kinds = {self.member_kind(m, f"{pos}[{j}]") for j, m in enumerate(members)}
            if len(kinds) != 1:
                self.fail(pos, "members must be all strings, all integers or all booleans")
            out[name] = tuple(members)
        return out

    def member_kind(self, member, pos: str) -> str:
        if type(member) is float:
            self.fail(pos, 'TOML floats are refused: write a decimal string such as "0.5" or an integer')
        if type(member) is str:
            if "\x00" in member:
                self.fail(pos, "strings must not contain NUL")
            return "string"
        if type(member) is int:
            return "integer"
        if type(member) is bool:
            return "boolean"
        self.fail(pos, "a member must be a string, an integer or a boolean")

    def parse_refs(self, raw) -> dict:
        if not isinstance(raw, dict):
            self.fail("refs", "must be a table of [refs.<name>] tables")
        out = {}
        for name, ref in raw.items():
            self.name(name, "refs", "a ref name")
            pos = f"refs.{name}"
            self.closed(ref, ("entity", "column", "where"), pos)
            entity = self.entity(ref.get("entity"), pos)
            column = self.name(ref.get("column"), pos, "column")
            where = ()
            if "where" in ref:
                if not isinstance(ref["where"], dict) or not ref["where"]:
                    self.fail(f"{pos} where", "must be a non-empty table of other_column = constant")
                for col, value in ref["where"].items():
                    self.name(col, f"{pos} where", "a where column")
                    if col == column:
                        self.fail(f"{pos} where", f"names the ref column {column} itself")
                    self.constant(value, f"{pos} where.{col}")
                where = tuple(ref["where"].items())
            out[name] = Ref(name, entity, column, where)
        return out

    # ---- rules -------------------------------------------------------------------------------------
    def rule(self, index: int, raw, ids: set) -> Rule:
        pos = f"rule[{index}]"
        if not isinstance(raw, dict):
            self.fail(pos, "must be a table ([[rule]])")
        rid = raw.get("id")
        if not isinstance(rid, str) or not _RULE_ID.fullmatch(rid):
            self.fail(pos, "id is required: a lower-case identifier of at most 56 characters (^[a-z_][a-z0-9_]*$)")
        pos = f"rule[{index}] {rid}"
        if rid in ids:
            self.fail(pos, "duplicate rule id")
        ids.add(rid)
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            self.fail(pos, f"kind is required: one of {', '.join(KINDS)}")
        self.closed(raw, COMMON_KEYS + KIND_KEYS[kind], pos)
        missing = [k for k in REQUIRED_KEYS[kind] if k not in raw]
        if missing:
            self.fail(pos, f"{missing[0]} is required for kind {kind}")
        severity = raw.get("severity")
        if not isinstance(severity, str) or severity not in SEVERITIES:
            self.fail(pos, "severity is required: violation or warning (there is no default)")
        fields = getattr(self, f"kind_{kind}")(pos, raw)
        self.unique_outputs(pos, fields["outputs"])
        subject = self.subject(pos, raw, kind, fields)
        examples = self.examples(pos, raw["example"], fields) if "example" in raw else ()
        return Rule(index=index, id=rid, kind=kind, severity=severity, subject=subject, examples=examples, **fields)

    def report(self, pos: str, raw, entity: str, reads: _Reads, forms: tuple) -> Report:
        rpos = f"{pos} report"
        if raw == "key" and "key" in forms:
            columns = tuple(self.cfg.entities[entity].key)
            reads.add(entity, *columns)
            return Report("key", columns)
        if raw == "group" and "group" in forms:
            return Report("group")
        if isinstance(raw, dict) and set(raw) == {"distinct"} and "distinct" in forms:
            column = self.name(raw["distinct"], rpos, "distinct")
            reads.add(entity, column)
            return Report("distinct", (column,))
        if isinstance(raw, dict) and set(raw) == {"columns"} and "columns" in forms:
            columns = raw["columns"]
            if not isinstance(columns, list) or not columns:
                self.fail(rpos, "columns must be a non-empty list of own columns")
            for c in columns:
                self.name(c, rpos, "a report column")
            if len(set(columns)) != len(columns):
                self.fail(rpos, "lists a column twice")
            reads.add(entity, *columns)
            return Report("columns", tuple(columns))
        shown = {"key": '"key"', "group": '"group"', "distinct": "{ distinct = C }", "columns": "{ columns = [C, ...] }"}
        self.fail(rpos, "must be " + " or ".join(shown[f] for f in forms))

    @staticmethod
    def report_outputs(entity: str, report: Report, derived: tuple = ()) -> tuple:
        own = tuple(Output(c, "column", entity, c) for c in report.columns)
        return own + derived + (Output("n", "count"),) if report.form == "columns" else own

    def unique_outputs(self, pos: str, outputs: tuple):
        seen = set()
        for o in outputs:
            if not _NAME.fullmatch(o.name):
                self.fail(pos, "an output column name is longer than 63 characters")
            if o.name in seen:
                self.fail(pos, f"output column {o.name} appears twice: rename or drop one")
            seen.add(o.name)

    def kind_row(self, pos: str, raw) -> dict:
        entity = self.entity(raw["entity"], pos)
        reads = _Reads()
        where = self.atoms(raw["where"], f"{pos} where", entity, reads, "where")
        report = self.report(pos, raw.get("report", "key"), entity, reads, ("key", "distinct", "columns"))
        return dict(entity=entity, where=where, report=report, outputs=self.report_outputs(entity, report),
                    reads=reads.freeze())

    def kind_duplicate(self, pos: str, raw) -> dict:
        entity = self.entity(raw["entity"], pos)
        h = self.h
        reads = _Reads()
        parts_raw = raw["key"]
        if not isinstance(parts_raw, list) or not parts_raw:
            self.fail(f"{pos} key", "must be a non-empty list of own columns or { end, column } tables")
        parts = []
        for j, p in enumerate(parts_raw):
            ppos = f"{pos} key[{j}]"
            if isinstance(p, str):
                parts.append(KeyPart(self.name(p, ppos, "a key column")))
                reads.add(entity, p)
            elif isinstance(p, dict):
                self.closed(p, ("end", "column"), ppos)
                if h is None or entity != h.edge:
                    self.fail(ppos, "an endpoint key part applies to the hierarchy edge entity")
                if p.get("end") not in ENDS:
                    self.fail(ppos, "end must be parent or child")
                column = self.name(p.get("column"), ppos, "column")
                parts.append(KeyPart(column, p["end"]))
                reads.add(entity, h.parent if p["end"] == "parent" else h.child)
                reads.add(h.node, h.key, column)
            else:
                self.fail(ppos, "a key part is an own column name or a { end, column } table")
        count = ""
        raw_count = raw.get("count", "rows")
        if raw_count != "rows":
            if not (isinstance(raw_count, dict) and set(raw_count) == {"distinct"}):
                self.fail(f"{pos} count", 'must be "rows" or { distinct = C }')
            count = self.name(raw_count["distinct"], f"{pos} count", "distinct")
            reads.add(entity, count)
        minimum = self.integer(raw.get("min", 2), pos, "min", 2)
        report = self.report(pos, raw.get("report", "group"), entity, reads, ("group", "distinct"))
        if report.form == "distinct" and KeyPart(report.columns[0]) not in parts:
            self.fail(f"{pos} report", "distinct must name an own-column key part")
        if report.form == "group":
            outputs = tuple(Output(p.name, "column", h.node if p.end else entity, p.column) for p in parts)
            outputs += (Output("n", "count"),)
        else:
            outputs = self.report_outputs(entity, report)
        return dict(entity=entity, key=tuple(parts), count=count, min=minimum, report=report, outputs=outputs,
                    reads=reads.freeze())

    def kind_mode_outlier(self, pos: str, raw) -> dict:
        entity = self.entity(raw["entity"], pos)
        reads = _Reads()
        group_by = self.name(raw["group_by"], pos, "group_by")
        value = self.name(raw["value"], pos, "value")
        reads.add(entity, group_by, value)
        share = self.decimal(raw["min_mode_share"], pos, "min_mode_share")
        if not Decimal("0.5") < share <= 1:
            self.fail(pos, "min_mode_share must be greater than 1/2 and at most 1")
        if not self.decimal(raw["ratio"], pos, "ratio") > 1:
            self.fail(pos, "ratio must be greater than 1")
        min_distinct = ()
        if "min_distinct" in raw:
            md = raw["min_distinct"]
            self.closed(md, ("column", "n"), f"{pos} min_distinct")
            if set(md) != {"column", "n"}:
                self.fail(f"{pos} min_distinct", "needs column and n")
            column = self.name(md["column"], f"{pos} min_distinct", "column")
            min_distinct = (column, self.integer(md["n"], f"{pos} min_distinct", "n", 1))
            reads.add(entity, column)
        report = self.report(pos, raw.get("report", "key"), entity, reads, ("key", "distinct", "columns"))
        derived = (Output("mode", "column", entity, value),)
        return dict(entity=entity, group_by=group_by, value=value, min_mode_share=raw["min_mode_share"],
                    ratio=raw["ratio"], min_distinct=min_distinct, report=report,
                    outputs=self.report_outputs(entity, report, derived), reads=reads.freeze())

    def kind_same_signature(self, pos: str, raw) -> dict:
        h = self.hierarchy_required(pos, "same_signature")
        attrs = raw["attrs"]
        if not isinstance(attrs, list) or not attrs:
            self.fail(f"{pos} attrs", "must be a non-empty list of node columns")
        for a in attrs:
            self.name(a, f"{pos} attrs", "an attribute")
        if len(set(attrs)) != len(attrs):
            self.fail(f"{pos} attrs", "lists a column twice")
        reads = _Reads()
        reads.add(h.node, h.key, *attrs)
        reads.add(h.edge, h.parent, h.child, h.quantity)
        outputs = (Output("group_id", "column", h.node, h.key), Output("member", "column", h.node, h.key))
        return dict(entity=h.node, attrs=tuple(attrs), min_members=self.integer(raw.get("min_members", 2), pos,
                                                                                "min_members", 2),
                    outputs=outputs, reads=reads.freeze())

    def kind_stored_metric(self, pos: str, raw) -> dict:
        h = self.hierarchy_required(pos, "stored_metric")
        if raw["metric"] != "height":
            self.fail(pos, 'metric must be "height"')
        stored = self.name(raw["stored"], pos, "stored")
        reads = _Reads()
        reads.add(h.node, h.key, stored)
        reads.add(h.edge, h.parent, h.child)
        outputs = (Output(h.key, "column", h.node, h.key), Output("kind", "text"),
                   Output("stored", "column", h.node, stored), Output("height", "integer"))
        return dict(entity=h.node, metric="height", stored=stored, outputs=outputs, reads=reads.freeze())

    def kind_text_quantity(self, pos: str, raw) -> dict:
        h = self.hierarchy_required(pos, "text_quantity")
        entity = self.entity(raw["entity"], pos)
        if entity != h.edge:
            self.fail(pos, "entity must be the hierarchy edge entity")
        end = raw["end"]
        if end not in ENDS:
            self.fail(pos, "end must be parent or child")
        text = self.name(raw["text"], pos, "text")
        quantity = self.name(raw["quantity"], pos, "quantity")
        if self.decimal(raw["rel_tol"], pos, "rel_tol") < 0:
            self.fail(pos, "rel_tol must be at least 0")
        max_len = self.integer(raw.get("max_text_length", DEFAULT_MAX_TEXT_LENGTH), pos, "max_text_length", 1,
                               MAX_TEXT_LENGTH)
        reads = _Reads()
        reads.add(entity, h.parent if end == "parent" else h.child, quantity)
        reads.add(h.node, h.key, text)
        where_end = self.atoms(raw["where_end"], f"{pos} where_end", h.node, reads, "end") if "where_end" in raw \
            else ()
        if not isinstance(raw["pattern"], str):
            self.fail(f"{pos} pattern", "must be a string")
        try:
            pattern = compile_subset(raw["pattern"])
        except PatternError as e:
            self.fail(f"{pos} pattern", f"refused ({e})")
        if pattern.captures not in (1, 2):
            self.fail(f"{pos} pattern", "needs 1 or 2 capturing groups (the amount, and optionally the divisor)")
        report = self.report(pos, raw.get("report", "key"), entity, reads, ("key", "distinct", "columns"))
        derived = (Output("numerator", "number"), Output("divisor", "number"))
        return dict(entity=entity, end=end, text=text, where_end=where_end, pattern=pattern, quantity=quantity,
                    rel_tol=raw["rel_tol"], max_text_length=max_len, report=report,
                    outputs=self.report_outputs(entity, report, derived), reads=reads.freeze())

    def subject(self, pos: str, raw, kind: str, fields: dict) -> str:
        if "subject" not in raw:
            return ""
        spos = f"{pos} subject"
        subject = self.name(raw["subject"], spos, "subject")
        if subject not in [o.name for o in fields["outputs"]]:
            self.fail(spos, "must name an output column of this rule")
        h = self.h
        allowed = set()
        if kind == "same_signature":
            allowed = {"group_id", "member"}
        elif h is not None and kind == "stored_metric":
            allowed = {h.key}
        elif h is not None:
            if fields["entity"] == h.node:
                allowed.add(h.key)
            if fields["entity"] == h.edge:
                allowed |= {h.parent, h.child}
        if subject not in allowed:
            self.fail(spos, "must be an output column holding node keys: the node key column (node entity rules, "
                            "stored_metric), the hierarchy parent or child column (edge entity rules), or "
                            "member or group_id (same_signature)")
        return subject

    # ---- atoms -------------------------------------------------------------------------------------
    def atoms(self, raw, pos: str, entity: str, reads: _Reads, scope: str) -> tuple:
        """A conjunction. scope: where (a rule's where list), branch (an any branch), sub (a quantified sub-where on
        the neighbour's node row), end (text_quantity where_end on the endpoint's node row)."""
        if not isinstance(raw, list) or not raw:
            self.fail(pos, "must be a non-empty list of atoms")
        if len(raw) > MAX_ATOMS:
            self.fail(pos, f"has more than {MAX_ATOMS} atoms")
        return tuple(self.atom(a, f"{pos}[{j}]", entity, reads, scope) for j, a in enumerate(raw))

    def atom(self, raw, pos: str, entity: str, reads: _Reads, scope: str):
        if not isinstance(raw, dict) or not raw:
            self.fail(pos, "an atom must be a non-empty table")
        keys = set(raw)
        h = self.h
        if scope == "end":
            if "column" not in keys or "end" in keys:
                self.fail(pos, "where_end holds column atoms only (the endpoint row is implied)")
            return self.column_atom(raw, pos, h.node, reads, "")
        node = h is not None and entity == h.node
        edge = h is not None and entity == h.edge
        if "any" in keys:
            if scope != "where":
                self.fail(pos, "a disjunction (any) belongs directly in a rule's where list: it cannot be nested or "
                               "used in a quantified sub-where")
            if keys != {"any"}:
                self.fail(pos, "a disjunction has the single key any")
            branches = raw["any"]
            if not isinstance(branches, list) or not MIN_BRANCHES <= len(branches) <= MAX_BRANCHES:
                self.fail(pos, f"any must be a list of {MIN_BRANCHES}..{MAX_BRANCHES} atom lists")
            return AnyAtom(pos, tuple(self.atoms(b, f"{pos} any[{k}]", entity, reads, "branch")
                                      for k, b in enumerate(branches)))
        directions = keys & set(DIRECTIONS)
        if directions:
            if scope == "sub":
                self.fail(pos, "a quantified atom (children, parents) cannot be nested")
            if not node:
                self.fail(pos, "children and parents atoms apply to the hierarchy node entity")
            direction = sorted(directions)[0]
            if keys != {direction, "where"}:
                self.fail(pos, f"a quantified atom has exactly the keys {direction} and where")
            quantifier = raw[direction]
            if not isinstance(quantifier, str) or quantifier not in QUANTIFIERS:
                self.fail(pos, "the quantifier must be all, any or none")
            reads.add(h.node, h.key)
            reads.add(h.edge, h.parent, h.child)
            sub = self.atoms(raw["where"], f"{pos} where", h.node, reads, "sub")
            return QuantifiedAtom(pos, direction, quantifier, sub)
        if "end" in keys:
            if scope == "sub":
                self.fail(pos, "endpoint atoms (end) are not allowed in a quantified sub-where")
            if not edge:
                self.fail(pos, "endpoint atoms (end) apply to the hierarchy edge entity")
            end = raw["end"]
            if not isinstance(end, str) or end not in ENDS:
                self.fail(pos, "end must be parent or child")
            endpoint = h.parent if end == "parent" else h.child
            if "exists" in keys:
                if keys != {"end", "exists"}:
                    self.fail(pos, "an endpoint existence atom has exactly the keys end and exists")
                reads.add(entity, endpoint)
                reads.add(h.node, h.key)
                return ExistsAtom(pos, end, self.boolean(raw["exists"], pos, "exists"))
            if "column" in keys:
                reads.add(entity, endpoint)
                reads.add(h.node, h.key)
                return self.column_atom(raw, pos, h.node, reads, end)
            tests = keys & set(STRUCTURE_TESTS)
            if len(tests) == 1 and keys == {"end"} | tests:
                test = tests.pop()
                reads.add(entity, endpoint, h.parent, h.child)
                return StructureAtom(pos, test, self.boolean(raw[test], pos, test), end)
            self.fail(pos, "an endpoint atom needs exists, a column comparison or exactly one of "
                           f"{', '.join(STRUCTURE_TESTS)}")
        if "column" in keys:
            return self.column_atom(raw, pos, entity, reads, "")
        tests = keys & set(STRUCTURE_TESTS)
        if tests:
            if not node:
                self.fail(pos, "structure atoms apply to the hierarchy node entity")
            if len(tests) != 1 or keys != tests:
                self.fail(pos, f"a structure atom has exactly one of the keys {', '.join(STRUCTURE_TESTS)}")
            test = tests.pop()
            reads.add(h.node, h.key)
            reads.add(h.edge, h.parent, h.child)
            return StructureAtom(pos, test, self.boolean(raw[test], pos, test))
        self.fail(pos, "unknown atom form (a column atom, end, children, parents, any or a structure test)")

    def column_atom(self, raw: dict, pos: str, entity: str, reads: _Reads, end: str) -> ColumnAtom:
        column = self.name(raw.get("column"), pos, "column")
        op = raw.get("op")
        if not isinstance(op, str) or op not in OPS:
            self.fail(pos, f"op must be one of {', '.join(OPS)}")
        operand = OPERAND[op]
        allowed = {"column", "op"} | ({"end"} if end else set()) | ({operand} if operand else set())
        if set(raw) != allowed:
            self.fail(pos, f"op {op} takes exactly the keys {', '.join(sorted(allowed))}")
        value, set_name, ref_name = None, "", ""
        if operand == "value":
            value = self.constant(raw["value"], f"{pos} value")
        elif operand == "set":
            set_name = raw["set"]
            if not isinstance(set_name, str) or set_name not in self.sets:
                self.fail(pos, "set must name a [sets] entry")
            self.used_sets.add(set_name)
            if op == "starts_with" and not all(type(m) is str for m in self.sets[set_name]):
                self.fail(pos, "starts_with needs a set of strings")
        elif operand == "ref":
            ref_name = raw["ref"]
            if not isinstance(ref_name, str) or ref_name not in self.refs:
                self.fail(pos, "ref must name a [refs.<name>] entry")
            self.used_refs.add(ref_name)
            ref = self.refs[ref_name]
            reads.add(ref.entity, ref.column, *(c for c, _v in ref.where))
        reads.add(entity, column)
        return ColumnAtom(pos, column, op, value, set_name, ref_name, end)

    # ---- examples ----------------------------------------------------------------------------------
    def examples(self, pos: str, raw, fields: dict) -> tuple:
        if not isinstance(raw, list) or not raw or not all(isinstance(e, dict) for e in raw):
            self.fail(f"{pos} example", "must be an array of tables ([[rule.example]])")
        if len(raw) > MAX_EXAMPLES:
            self.fail(f"{pos} example", f"at most {MAX_EXAMPLES} examples per rule")
        return tuple(self.example(f"{pos} example[{j}]", j, e, fields) for j, e in enumerate(raw))

    def cell(self, value, pos: str):
        if isinstance(value, dict):
            if set(value) != {"null"} or value["null"] is not True:
                self.fail(pos, "the only table value is { null = true } (NULL)")
            return None
        if type(value) is float:
            self.fail(pos, "TOML floats are refused: write numbers as integers or decimal strings")
        if type(value) is str:
            if "\x00" in value:
                self.fail(pos, "strings must not contain NUL")
            return value
        if type(value) in (int, bool):
            return value
        self.fail(pos, "a cell is a string, an integer, a boolean or { null = true }")

    def example(self, pos: str, index: int, raw: dict, fields: dict) -> Example:
        self.closed(raw, ("spec", "rows", "expect"), pos)
        spec = raw.get("spec")
        if not isinstance(spec, str) or not spec.strip() or "\x00" in spec:
            self.fail(pos, "spec is required: where in the business rules these rows come from")
        reads = fields["reads"]
        rows_raw = raw.get("rows", {})
        if not isinstance(rows_raw, dict):
            self.fail(pos, "rows must be a table of rows.<entity> lists")
        for entity in reads:
            if entity not in rows_raw:
                self.fail(pos, f"rows.{entity} is required: the rule reads that entity (rows.{entity} = [] for none)")
        if set(rows_raw) - set(reads):
            self.fail(pos, "rows names an entity the rule does not read")
        rows, total_rows, total_cells = {}, 0, 0
        for entity, columns in reads.items():
            lst = rows_raw[entity]
            if not isinstance(lst, list):
                self.fail(f"{pos} rows.{entity}", "must be a list of tables")
            parsed = []
            for r, row in enumerate(lst):
                rpos = f"{pos} rows.{entity}[{r}]"
                if not isinstance(row, dict):
                    self.fail(rpos, "must be a table of column = value")
                for col in row:
                    self.name(col, rpos, "a column")
                missing = [c for c in columns if c not in row]
                if missing:
                    self.fail(rpos, f"column {missing[0]} is missing: every column the rule reads is given "
                                    "({ null = true } for NULL)")
                parsed.append({col: self.cell(v, f"{rpos} {col}") for col, v in row.items()})
                total_rows += 1
                total_cells += len(row)
            rows[entity] = tuple(parsed)
        if total_rows > MAX_EXAMPLE_ROWS:
            self.fail(pos, f"has more than {MAX_EXAMPLE_ROWS} input rows")
        if total_cells > MAX_EXAMPLE_CELLS:
            self.fail(pos, f"has more than {MAX_EXAMPLE_CELLS} input cells")
        for entity, key in self.example_keys(fields):
            seen = {}
            for r, row in enumerate(rows[entity]):
                values = tuple(row[c] for c in key)
                if any(v is None for v in values):
                    self.fail(f"{pos} rows.{entity}[{r}]", "the key must not be NULL")
                typed = tuple((type(v).__name__, v) for v in values)
                if typed in seen:
                    self.fail(f"{pos} rows.{entity}", f"rows[{seen[typed]}] and rows[{r}] have the same key")
                seen[typed] = r
        if "expect" not in raw:
            self.fail(pos, "expect is required (expect = [] when the rule outputs nothing)")
        expect = self.expect_rows(pos, raw["expect"], fields["outputs"])
        return Example(index, pos, spec, rows, expect)

    def example_keys(self, fields: dict) -> list:
        """(entity, key columns) that must be unique and non-NULL in examples: the node key, when the rule reads
        node rows by key, and the key of an entity the rule reports by key."""
        keys, h, reads = [], self.h, fields["reads"]
        if h is not None and h.key in reads.get(h.node, ()):
            keys.append((h.node, (h.key,)))
        report = fields.get("report")
        if report is not None and report.form == "key" and not (h is not None and fields["entity"] == h.node):
            keys.append((fields["entity"], report.columns))
        return keys

    def expect_rows(self, pos: str, raw, outputs: tuple) -> tuple:
        if not isinstance(raw, list):
            self.fail(f"{pos} expect", "must be a list of tables keyed by output column (possibly empty)")
        if len(raw) > MAX_EXAMPLE_ROWS:
            self.fail(f"{pos} expect", f"has more than {MAX_EXAMPLE_ROWS} rows")
        names = [o.name for o in outputs]
        out, seen = [], {}
        for r, row in enumerate(raw):
            rpos = f"{pos} expect[{r}]"
            if not isinstance(row, dict):
                self.fail(rpos, "must be a table keyed by output column")
            missing = [n for n in names if n not in row]
            if missing:
                self.fail(rpos, f"output column {missing[0]} is missing: every row lists every output column")
            if set(row) - set(names):
                self.fail(rpos, f"names a column that is not an output column ({', '.join(names)})")
            cells = {}
            for o in outputs:
                v = self.cell(row[o.name], f"{rpos} {o.name}")
                self.derived_cell(v, o, f"{rpos} {o.name}")
                cells[o.name] = v
            typed = tuple((type(cells[n]).__name__, cells[n]) for n in names)
            if typed in seen:
                self.fail(f"{pos} expect", f"expect[{seen[typed]}] and expect[{r}] are the same row")
            seen[typed] = r
            out.append(cells)
        return tuple(out)

    def derived_cell(self, value, output: Output, pos: str):
        """Values a derived output column can never hold make an example that could only fail, so they are refused:
        n >= 1, height >= 0 and divisor >= 1 are integers, numerator > 0, kind is one of HEIGHT_KINDS."""
        name, source = output.name, output.source
        if source == "column":
            return
        if value is None:
            self.fail(pos, f"{name} is never NULL")
        if source == "text":
            if not isinstance(value, str) or value not in HEIGHT_KINDS:
                self.fail(pos, f"kind must be one of {', '.join(HEIGHT_KINDS)}")
            return
        number = _numeral(value)
        if number is None:
            self.fail(pos, f"{name} must be an integer or a decimal string")
        lowest = {"n": 1, "height": 0, "divisor": 1}.get(name)
        if lowest is not None and (number != number.to_integral_value() or number < lowest):
            self.fail(pos, f"{name} must be an integer >= {lowest}")
        if name == "numerator" and number <= 0:
            self.fail(pos, "numerator must be greater than 0")


# ---- pattern subset ---------------------------------------------------------------------------------
_PUNCT = frozenset(string.punctuation)
_DIGIT_CLASS = "[0-9]"
_SPACE_CLASS = "[ \\t\\n\\r\\f\\v]"
_BOUND = re.compile(r"\{([0-9]{1,3})(,([0-9]{0,3}))?\}")
_RANGES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_CLASS_ESCAPED = frozenset("^-[&~|")   # written escaped inside classes (Python warns about [[ && ~~ || --)


def compile_subset(pattern: str) -> Pattern:
    """Validate a text_quantity pattern against the supported subset and emit its Python and ARE forms.

    Allowed: literal characters (non-ASCII too), a backslash plus ASCII punctuation, '.', bracket classes (an optional
    leading '^', literal members, escaped punctuation, the ASCII ranges 0-9 a-z A-Z), \\d and \\s (outside classes,
    expanded to [0-9] and [ \\t\\n\\r\\f\\v]), greedy quantifiers * + ? {m} {m,} {m,n} with m <= n <= 255, capturing
    groups that are never quantified, (?:...), a leading ^ and a trailing $ (Python \\Z).
    Refused: alternation, backreferences, lookaround, named groups, inline flags, lazy or possessive quantifiers, any
    other letter escape (\\w \\W \\S \\D \\b \\B ...), POSIX classes, NUL, more than 256 characters, more than 16
    quantifiers, and a repetition (* + {m,} or an upper bound above 1) over a group that contains a quantifier; ?
    and {0,1} over such a group stay allowed.
    What the subset guarantees: both engines read its syntax the same way (a class, an escape, '.', a quantifier and
    an anchor mean the same in both), and it refuses the constructs above, among them the nested repetition that makes
    Python's backtracking exponential. It does not bound backtracking: neighbouring quantified items that can match the
    same characters are accepted and can backtrack polynomially on a long text that does not match. Nor does it make
    the engines choose the same match: from the leftmost position where a match starts, PostgreSQL takes the longest
    overall match (PostgreSQL 17 documentation, 9.7.3.5 "Regular Expression Matching Rules") and Python's re the first
    match its backtracking finds, so the whole match and the captures can differ. ([0-9]+)(?:[0-9]g)? on "123g" matches
    123g with capture 12 in PostgreSQL and 123 with capture 123 in Python. When such a difference changes the rule's
    output (whether a row is flagged, or its numerator or divisor), the two outputs differ, rule_xcheck:<id> fails and
    rule:<id> is not_run (paths_disagree). A difference that changes no output leaves both outputs equal and the
    verdict the same under either reading, so it is never a false pass. Keeping neighbouring items disjoint, or
    anchoring a pattern whose match is the whole text, avoids that example's difference, not every difference.
    Raises PatternError naming the construct, never the pattern."""
    if not isinstance(pattern, str) or not pattern:
        raise PatternError("empty")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError(f"longer than {MAX_PATTERN_LENGTH} characters")
    if "\x00" in pattern:
        raise PatternError("NUL")
    py: list = []
    are: list = []
    groups: list = []          # per open group: [capturing, contains a quantifier]
    captures = quantifiers = 0
    last = None                # None (nothing to repeat), "atom", "capture" or "group"
    last_group_quantified = False
    i, n = 0, len(pattern)
    if pattern.startswith("^"):
        py.append("^")
        are.append("^")
        i = 1

    def emit(token: str):
        py.append(token)
        are.append(token)

    while i < n:
        ch = pattern[i]
        if ch == "$":
            if i != n - 1 or groups:
                raise PatternError("anchor position")
            py.append("\\Z")
            are.append("$")
            i += 1
            last = None
            continue
        if ch == "|":
            raise PatternError("alternation")
        if ch == "\\":
            if i + 1 >= n:
                raise PatternError("trailing backslash")
            nx = pattern[i + 1]
            if nx == "d":
                emit(_DIGIT_CLASS)
            elif nx == "s":
                emit(_SPACE_CLASS)
            elif nx in _PUNCT:
                emit("\\" + nx)
            else:
                raise PatternError("escape")
            i += 2
            last = "atom"
            continue
        if ch == "[":
            j, members = i + 1, []
            if j < n and pattern[j] == "^":
                members.append("^")
                j += 1
            first = True
            while True:
                if j >= n:
                    raise PatternError("unclosed class")
                c = pattern[j]
                if c == "]" and not first:
                    break
                if c == "[" and j + 1 < n and pattern[j + 1] in ":.=":
                    raise PatternError("posix class")
                if c == "\\":
                    if j + 1 >= n or pattern[j + 1] not in _PUNCT:
                        raise PatternError("class escape")
                    members.append("\\" + pattern[j + 1])
                    j += 2
                elif c == "]":
                    raise PatternError("unescaped ] in class")
                elif j + 2 < n and pattern[j + 1] == "-" and pattern[j + 2] != "]":
                    lo, hi = c, pattern[j + 2]
                    if not any(lo in r and hi in r and lo <= hi for r in _RANGES):
                        raise PatternError("range")
                    members.append(f"{lo}-{hi}")
                    j += 3
                elif c in _CLASS_ESCAPED:
                    members.append("\\" + c)
                    j += 1
                else:
                    members.append(c)
                    j += 1
                first = False
            emit("[" + "".join(members) + "]")
            i = j + 1
            last = "atom"
            continue
        if ch == "(":
            if pattern.startswith("(?:", i):
                groups.append([False, False])
                emit("(?:")
                i += 3
            elif pattern.startswith("(?", i):
                raise PatternError("group extension")
            else:
                groups.append([True, False])
                captures += 1
                emit("(")
                i += 1
            last = None
            continue
        if ch == ")":
            if not groups:
                raise PatternError("unbalanced")
            capturing, quantified = groups.pop()
            if quantified and groups:
                groups[-1][1] = True
            emit(")")
            i += 1
            last = "capture" if capturing else "group"
            last_group_quantified = quantified
            continue
        if ch in "*+?{":
            if last is None:
                raise PatternError("nothing to repeat")
            if last == "capture":
                raise PatternError("quantified capture")
            if ch == "{":
                m = _BOUND.match(pattern, i)
                if not m:
                    raise PatternError("bound syntax")
                lo = int(m.group(1))
                hi = None if m.group(2) is not None and m.group(3) == "" else int(m.group(3) or m.group(1))
                if lo > MAX_REPEAT or (hi is not None and (hi > MAX_REPEAT or hi < lo)):
                    raise PatternError("bound range")
                token = m.group(0)
            else:
                hi = 1 if ch == "?" else None
                token = ch
            i += len(token)
            if i < n and pattern[i] in "?+":
                raise PatternError("lazy or possessive")
            if last == "group" and last_group_quantified and (hi is None or hi > 1):
                raise PatternError("repetition over a group that contains a quantifier")
            quantifiers += 1
            if quantifiers > MAX_QUANTIFIERS:
                raise PatternError(f"more than {MAX_QUANTIFIERS} quantifiers")
            if groups:
                groups[-1][1] = True
            emit(token)
            last = None
            continue
        if ch in "]}":
            raise PatternError("unescaped closer")
        if ch == ".":
            emit(".")
            i += 1
            last = "atom"
            continue
        if ch == "^":
            raise PatternError("anchor position")
        emit("\\" + ch if ch.isascii() and not ch.isalnum() and ch != " " else ch)
        i += 1
        last = "atom"
    if groups:
        raise PatternError("unbalanced")
    result = Pattern("".join(py), "".join(are), captures, quantifiers)
    try:
        result.compile()
    except re.error:
        raise PatternError("unsupported construct") from None
    return result


# ---- types ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ColumnType:
    """The catalog type of one column. type_class: number (int2, int4, int8, numeric), text (text, varchar), bpchar,
    boolean or other (every other type, domains included). reason is "" for a usable column, column_type for an
    unsupported type and collation for text with a nondeterministic collation: rules reading it are not run."""
    type_class: str
    base: str = ""                 # int2 int4 int8 numeric text varchar bpchar bool, "" for other
    precision: int | None = None   # numeric(p, s)
    scale: int | None = None
    length: int | None = None      # varchar(n), bpchar(n)
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.type_class != "other" and not self.reason


@dataclass(frozen=True)
class TypePlan:
    """{(entity, column): ColumnType} for the columns of the connected tables (built from the catalog)."""
    columns: dict

    def column(self, entity: str, column: str) -> ColumnType | None:
        return self.columns.get((entity, column))


_DECLARED = re.compile(r"(?P<base>[a-z ]+?)\s*(?:\(\s*(?P<a>-?[0-9]+)\s*(?:,\s*(?P<b>-?[0-9]+)\s*)?\))?")
_BASES = {"smallint": "int2", "int2": "int2", "integer": "int4", "int": "int4", "int4": "int4", "bigint": "int8",
          "int8": "int8", "numeric": "numeric", "decimal": "numeric", "text": "text", "character varying": "varchar",
          "varchar": "varchar", "character": "bpchar", "char": "bpchar", "bpchar": "bpchar", "boolean": "bool",
          "bool": "bool"}
_CLASS_OF = {"int2": "number", "int4": "number", "int8": "number", "numeric": "number", "text": "text",
             "varchar": "text", "bpchar": "bpchar", "bool": "boolean"}


def declared_type(declared: str, deterministic: bool = True) -> ColumnType:
    """ColumnType of a type as format_type() prints it (numeric(18,6), character varying(20), character(3), integer,
    boolean, ...). Any other type is other with reason column_type; text without a deterministic collation has
    reason collation. A bare character is character(1), as in PostgreSQL."""
    m = _DECLARED.fullmatch(declared.strip().lower()) if isinstance(declared, str) else None
    base = _BASES.get(m.group("base").strip()) if m else None
    if base is None:
        return ColumnType("other", reason="column_type")
    a, b = m.group("a"), m.group("b")
    precision = scale = length = None
    if base == "numeric" and a is not None:
        precision, scale = int(a), int(b or 0)
    elif base in ("varchar", "bpchar") and a is not None and b is None:
        length = int(a)
    elif a is not None:
        return ColumnType("other", reason="column_type")
    if base == "bpchar" and length is None and m.group("base").strip() in ("character", "char"):
        length = 1
    cls = _CLASS_OF[base]
    reason = "" if deterministic or cls not in ("text", "bpchar") else "collation"
    return ColumnType(cls, base, precision, scale, length, reason)


def _comparable(t: ColumnType) -> str:
    """bpchar values are compared as text (read without pad spaces)."""
    return "text" if t.type_class == "bpchar" else t.type_class


def _numeral(value) -> Decimal | None:
    """The value of an integer or a plain decimal numeral string, None for anything else."""
    if type(value) is int:
        return Decimal(value)
    if type(value) is str and _NUMERAL.fullmatch(value):
        return Decimal(value)
    return None


def fits(value, t: ColumnType) -> bool:
    """Whether a typed cell is stored by a column of type t without rounding or truncation (NULL always fits)."""
    if value is None:
        return True
    cls = t.type_class
    if cls == "boolean":
        return type(value) is bool
    if cls in ("text", "bpchar"):
        return type(value) is str and (t.length is None or len(value) <= t.length)
    if cls != "number":
        return False
    number = _numeral(value)
    if number is None:
        return False
    text = str(value).lstrip("-")
    whole, _, fraction = text.partition(".")
    whole, fraction = whole.lstrip("0"), fraction.rstrip("0")
    if t.base in INTEGER_RANGES:
        lo, hi = INTEGER_RANGES[t.base]
        return not fraction and lo <= int(number) <= hi
    if t.precision is None:
        return True
    scale = t.scale or 0
    if len(fraction) > max(scale, 0):
        return False
    if scale < 0 and whole and len(whole) - len(whole.rstrip("0")) < -scale:
        return False
    return abs(number) < Decimal(1).scaleb(t.precision - scale)


def _value_ok(value, t: ColumnType) -> bool:
    """Whether a comparison constant suits a column (no fit needed: comparisons never round)."""
    cls = _comparable(t)
    if cls == "boolean":
        return type(value) is bool
    if cls == "text":
        return type(value) is str
    return _numeral(value) is not None


def apply_types(spec: RuleFile, plan: TypePlan) -> dict:
    """Check every rule of spec against the catalog type plan.

    Raises RuleSpecError (exit 2) for an unknown column, a type-incompatible atom (lt/le/gt/ge on text, starts_with
    on a number, anything but eq/ne/null tests on a boolean, a value or set member of the wrong type, a ref column of
    another type class), hierarchy endpoint columns or a subject column of another type class than the node key, a
    kind column of the wrong type (mode_outlier value, stored_metric stored, text_quantity text and quantity, the
    hierarchy quantity of same_signature), or an example value that does not fit its declared type. Returns
    {rule id: reason} for the rules that read a column of an unsupported type (column_type) or a nondeterministic
    collation (collation)."""
    not_run = {}
    for rule in spec.rules:
        reason = _Types(spec, plan, rule).run()
        if reason:
            not_run[rule.id] = reason
    return not_run


class _Types:
    def __init__(self, spec: RuleFile, plan: TypePlan, rule: Rule):
        self.spec, self.plan, self.rule = spec, plan, rule
        self.reason = ""
        self.refs: list = []

    def fail(self, pos: str, msg: str):
        _fail(self.spec.label, pos, msg)

    def type(self, entity: str, column: str, pos: str) -> ColumnType | None:
        """The usable type of a column; None for an unusable one (the rule is not run)."""
        t = self.plan.column(entity, column)
        if t is None:
            self.fail(pos, f"column {column} is not a column of entity {entity} in the connected database")
        if not t.usable:
            self.reason = self.reason or (t.reason if t.reason in NOT_RUN_REASONS else "column_type")
            return None
        return t

    def run(self) -> str:
        rule, h = self.rule, self.spec.hierarchy
        for entity, columns in rule.reads.items():
            for column in columns:
                self.type(entity, column, rule.pos)
        if h is not None:
            self.endpoints(h)
            self.subject(h)
        node = h.node if h is not None else ""
        self.atoms(rule.where, rule.entity, node)
        self.atoms(rule.where_end, node, node)
        self.kind_columns(h)
        for name in dict.fromkeys(self.refs):
            ref = self.spec.refs[name]
            for column, value in ref.where:
                t = self.type(ref.entity, column, f"refs.{name}")
                if t is not None and not _value_ok(value, t):
                    self.fail(f"refs.{name} where.{column}", f"the constant does not suit the {t.type_class} column")
        for example in rule.examples:
            self.example(example, h)
        return self.reason

    def endpoints(self, h: Hierarchy):
        reads = self.rule.reads
        types = [self.type(h.edge, c, self.rule.pos) for c in (h.parent, h.child) if c in reads.get(h.edge, ())]
        if types and h.key in reads.get(h.node, ()):
            types.append(self.type(h.node, h.key, self.rule.pos))
        if len({_comparable(t) for t in types if t is not None}) > 1:
            self.fail(self.rule.pos, f"the hierarchy columns {h.parent} and {h.child} must have the type class of the "
                                     f"node key {h.key}")

    def subject(self, h: Hierarchy):
        """The subjects view unions the subject columns of every rule with the node keys: one type class."""
        rule = self.rule
        source = next((o for o in rule.outputs if o.name == rule.subject), None) if rule.subject else None
        if source is None or source.source != "column":
            return
        st, kt = self.plan.column(source.entity, source.column), self.plan.column(h.node, h.key)
        if st is not None and kt is not None and st.usable and kt.usable and _comparable(st) != _comparable(kt):
            self.fail(f"{rule.pos} subject", f"column {source.column} must have the type class of the node key {h.key}")

    def atoms(self, atoms: tuple, entity: str, node: str):
        for a in atoms:
            if isinstance(a, ColumnAtom):
                self.column_atom(a, node if a.end else entity)
            elif isinstance(a, QuantifiedAtom):
                self.atoms(a.where, node, node)
            elif isinstance(a, AnyAtom):
                for branch in a.branches:
                    self.atoms(branch, entity, node)

    def column_atom(self, a: ColumnAtom, entity: str):
        t = self.type(entity, a.column, a.pos)
        if a.op == "not_in_ref":
            self.refs.append(a.ref)
        if t is None or a.op in NULL_OPS:
            return
        cls = _comparable(t)
        if cls == "boolean" and a.op not in ("eq", "ne"):
            self.fail(a.pos, f"op {a.op} is not allowed on boolean column {a.column} (eq, ne, is_null, is_not_null)")
        if cls == "text" and a.op in ORDER_OPS:
            self.fail(a.pos, f"op {a.op} is not allowed on text column {a.column}")
        if cls != "text" and a.op == "starts_with":
            self.fail(a.pos, f"starts_with needs a text column ({a.column} is {t.type_class})")
        if a.op == "not_in_ref":
            ref = self.spec.refs[a.ref]
            rt = self.type(ref.entity, ref.column, a.pos)
            if rt is not None and _comparable(rt) != cls:
                self.fail(a.pos, f"ref {ref.name} column {ref.column} must have the type class of column {a.column}")
            return
        if a.set:
            for j, member in enumerate(self.spec.sets[a.set]):
                if not _value_ok(member, t):
                    self.fail(a.pos, f"set {a.set} member [{j}] does not suit {t.type_class} column {a.column}"
                                     + (" (a decimal numeral or an integer)" if cls == "number" else ""))
        elif not _value_ok(a.value, t):
            self.fail(a.pos, f"the value does not suit {t.type_class} column {a.column}"
                             + (" (a decimal numeral or an integer)" if cls == "number" else ""))

    def kind_columns(self, h: Hierarchy | None):
        rule = self.rule
        wanted = []
        if rule.kind == "mode_outlier":
            wanted.append((rule.entity, rule.value, ("number",), "value"))
        elif rule.kind == "stored_metric":
            wanted.append((rule.entity, rule.stored, ("number",), "stored"))
        elif rule.kind == "text_quantity":
            wanted += [(h.node, rule.text, ("text",), "text"), (rule.entity, rule.quantity, ("number",), "quantity")]
        elif rule.kind == "same_signature":
            wanted.append((h.edge, h.quantity, ("number",), "the hierarchy quantity"))
        for entity, column, classes, what in wanted:
            t = self.type(entity, column, rule.pos)
            if t is not None and _comparable(t) not in classes:
                self.fail(rule.pos, f"{what} column {column} must be {' or '.join(classes)}, not {t.type_class}")

    def output_classes(self) -> list:
        """(usable ColumnType or None, type class for cell()) per output column; "" leaves a cell as written."""
        out = []
        for o in self.rule.outputs:
            if o.source == "column":
                t = self.plan.column(o.entity, o.column)
                usable = t is not None and t.usable
                out.append((t if usable else None, t.type_class if usable else ""))
            else:
                out.append((None, "text" if o.source == "text" else "number"))
        return out

    def example(self, ex: Example, h: Hierarchy | None):
        for entity, rows in ex.rows.items():
            for r, row in enumerate(rows):
                for column, value in row.items():
                    pos = f"{ex.pos} rows.{entity}[{r}] {column}"
                    t = self.plan.column(entity, column)
                    if t is None:
                        self.fail(pos, f"is not a column of entity {entity} in the connected database")
                    if t.usable and not fits(value, t):
                        self.fail(pos, "does not fit the column's declared type (type, precision, scale or length): "
                                       "write a value the column stores without rounding or truncation")
        keys = []
        if h is not None and h.key in self.rule.reads.get(h.node, ()):
            keys.append((h.node, (h.key,)))
        report = self.rule.report
        if report is not None and report.form == "key" and not (h is not None and self.rule.entity == h.node):
            keys.append((self.rule.entity, report.columns))
        for entity, key in keys:
            types = [self.plan.column(entity, c) for c in key]
            if any(t is None or not t.usable for t in types):
                continue
            seen = {}
            for r, row in enumerate(ex.rows[entity]):
                canonical = tuple(cell(row[c], t.type_class) for c, t in zip(key, types))
                if canonical in seen:
                    self.fail(f"{ex.pos} rows.{entity}", f"rows[{seen[canonical]}] and rows[{r}] have the same key "
                                                        "once values are read as the column's type")
                seen[canonical] = r
        outputs = self.output_classes()
        seen = {}
        for r, row in enumerate(ex.expect):
            canonical = []
            for o, (t, cls) in zip(self.rule.outputs, outputs):
                value = row[o.name]
                if o.source == "column" and t is not None and not fits(value, t):
                    self.fail(f"{ex.pos} expect[{r}] {o.name}", "does not fit the declared type of the column it "
                                                                "comes from")
                canonical.append(cell(value, cls) if cls else value)
            canonical = tuple(canonical)
            if canonical in seen:
                self.fail(f"{ex.pos} expect", f"expect[{seen[canonical]}] and expect[{r}] are the same row once values "
                                              "are read as the column types")
            seen[canonical] = r


# ---- canonical cells and order ----------------------------------------------------------------------
def cell(value, type_class: str):
    """Canonical comparison form of one typed cell: numbers exact without trailing zeros (canon number), text exactly
    (bpchar without its trailing pad spaces, as PostgreSQL reads it), booleans as bool, NULL as None."""
    if value is None:
        return None
    if type_class == "number":
        return canon.canon(value, "number")
    if type_class == "boolean":
        return canon.canon(value, "boolean")
    if type_class == "bpchar":
        return canon.canon(value.rstrip(" ") if isinstance(value, str) else value, "string")
    if type_class == "text":
        return canon.canon(value, "string")
    raise ValueError(f"no canonical form for type class {type_class!r}")


def sort_key(row: tuple, classes: tuple) -> tuple:
    """Typed order of a canonical row (one type class per column): NULL < false < true < numbers by value < text by
    code point. Code-point order equals UTF-8 byte order equals COLLATE "C", the order of ord in the lookup views."""
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
