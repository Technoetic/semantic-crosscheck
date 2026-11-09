"""Path 1 of the data-rules stage (data_rules_sql.py): the compiled SQL text, without a database.

Goldens in tests/fixtures/data_rules/golden hold the statements compiled for the clean-room example (examples/
data-rules, the design's Appendix A: one file per rule with its parameterized SELECT, its view definition and its
example statement), for a synthetic rule file that uses every atom and report form, and for the same rules on a
bpchar schema and on a schema with numeric node keys, plus the snapshot gates, exports, lookups and views. They were
reviewed once; after a deliberate change, regenerate them with
    python -X utf8 tests/test_data_rules_sql.py --write-goldens
and review the diff. The other tests check properties: no configured constant in parameterized text (every constant
of the synthetic file carries a marker), exact literal escaping (also through pg8000's placeholder scanner), quoting,
bpchar casts, COLLATE "C" only on text, the recursion cap, the LIMIT wrapper, example relations, the type plan, the
gates and their reasons, the view statements, rules.sql and the import boundary. When pglast is installed
(requirements/sql-structure.txt) every statement is also parsed with its PostgreSQL 17 grammar in both renderings.
"""

import ast
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, data_rules, data_rules_sql as dsql, ident
from semantic_crosscheck.data_rules import RuleSpecError

ROOT = pgfixture.ROOT
EXAMPLE = ROOT / "examples" / "data-rules"
GOLDEN = ROOT / "tests" / "fixtures" / "data_rules" / "golden"
HAS_PGLAST = importlib.util.find_spec("pglast") is not None
HAS_PG8000 = importlib.util.find_spec("pg8000") is not None
MARK = "DRPMARK"
NUMBERS = ("97531", "8642.75", "13.579", "0.4321", "86420", "75319", "64208")   # marker constants
SHARE = "0.9753"                     # a mode share: bound as the integers 9753 and 10000, never as itself
BS = "\\"
NON_ASCII = chr(0xE9) + chr(0x4E2D) + chr(0x1F600)   # e-acute, a CJK ideograph, an emoji

# ---- schemas (type plans) ---------------------------------------------------------------------------
TEXT_SCHEMA = {
    "part": {"part_id": "text", "name": "text", "unit": "text", "part_class": "text", "procure": "text",
             "issue": "text", "retired": "boolean", "stored_level": "integer"},
    "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text", "qty_per": "numeric(18,6)"},
    "code_value": {"code_group": "text", "code": "text"},
}
BPCHAR = {("part", "part_id"): "character(12)", ("part", "name"): "character(24)",
          ("part", "unit"): "character(12)", ("part", "part_class"): "character(12)",
          ("part", "procure"): "character(12)", ("part", "issue"): "character(12)",
          ("bom_line", "parent_id"): "character(12)", ("bom_line", "child_id"): "character(12)",
          ("code_value", "code_group"): "character(16)", ("code_value", "code"): "character(12)"}
NUMERIC_KEYS = {("part", "part_id"): "integer", ("bom_line", "parent_id"): "bigint",
                ("bom_line", "child_id"): "integer"}


def make_plan(overrides=None) -> data_rules.TypePlan:
    columns = {(e, c): t for e, cols in TEXT_SCHEMA.items() for c, t in cols.items()}
    columns.update(overrides or {})
    return data_rules.TypePlan({k: data_rules.declared_type(t) for k, t in columns.items()})


TEXT_PLAN, BPCHAR_PLAN, NUMERIC_PLAN = make_plan(), make_plan(BPCHAR), make_plan(NUMERIC_KEYS)

# ---- the synthetic rule file: every atom and report form, every constant a marker ----------------------
CFG = config.loads(b"""version = 1
[source]
schemas = ["sxc_rules"]
output_schema = "sxc_out"
[entities.part]
table = "sxc_rules.part"
key = ["part_id"]
[entities.bom_line]
table = "sxc_rules.bom_line"
key = ["line_id"]
[entities.code_value]
table = "sxc_rules.code_value"
key = ["code_group", "code"]
[hierarchies.has_component]
edge = "bom_line"
node = "part"
parent = "parent_id"
child = "child_id"
quantity = "qty_per"
max_depth = 12
[data_rules]
file = "data-rules.toml"
""", Path(tempfile.gettempdir()) / "sxc-data-rules-sql" / "crosscheck.toml")


def m(text: str) -> str:
    return f"{MARK}-{text}"


ATOMS = {
    "version": 1,
    "hierarchy": "has_component",
    "sets": {"units": [m("pc"), m("box")], "classes": [m("ASSY"), m("PART")], "prefixes": [m("TMP-"), m("OLD-")],
             "levels": [97531, -97531], "quantities": ["8642.75", "13.579"]},
    "refs": {"procure_codes": {"entity": "code_value", "column": "code", "where": {"code_group": m("PROCURE")}},
             "unit_codes": {"entity": "code_value", "column": "code"}},
    "rule": [
        {"id": "column_ops", "kind": "row", "entity": "part", "severity": "violation", "subject": "part_id",
         "where": [{"column": "name", "op": "eq", "value": m("Frame")},
                   {"column": "unit", "op": "ne", "value": m("kg")},
                   {"column": "stored_level", "op": "lt", "value": 97531},
                   {"column": "stored_level", "op": "le", "value": "8642.75"},
                   {"column": "stored_level", "op": "gt", "value": -97531},
                   {"column": "stored_level", "op": "ge", "value": "-8642.75"},
                   {"column": "retired", "op": "eq", "value": True},
                   {"column": "retired", "op": "ne", "value": False}]},
        {"id": "set_ops", "kind": "row", "entity": "part", "severity": "warning", "report": {"distinct": "name"},
         "where": [{"column": "part_class", "op": "in", "set": "classes"},
                   {"column": "unit", "op": "not_in", "set": "units"},
                   {"column": "name", "op": "starts_with", "set": "prefixes"},
                   {"column": "stored_level", "op": "in", "set": "levels"},
                   {"column": "procure", "op": "is_null"},
                   {"column": "issue", "op": "is_not_null"}]},
        {"id": "ref_ops", "kind": "row", "entity": "part", "severity": "warning",
         "where": [{"column": "procure", "op": "not_in_ref", "ref": "procure_codes"}],
         "report": {"columns": ["procure"]}},
        {"id": "node_structure", "kind": "row", "entity": "part", "severity": "violation", "subject": "part_id",
         "where": [{"has_children": True}, {"has_parents": False}, {"in_hierarchy": True}],
         "example": [{"spec": m("a root assembly"),
                      "rows": {"part": [{"part_id": m("A")}],
                               "bom_line": [{"parent_id": m("A"), "child_id": m("B")}]},
                      "expect": [{"part_id": m("A")}]}]},
        {"id": "node_structure_negated", "kind": "row", "entity": "part", "severity": "warning",
         "where": [{"has_children": False}, {"has_parents": True}, {"in_hierarchy": False}]},
        {"id": "endpoint_atoms", "kind": "row", "entity": "bom_line", "severity": "violation",
         "where": [{"end": "parent", "exists": True}, {"end": "child", "exists": False},
                   {"end": "child", "column": "unit", "op": "ne", "value": m("kg")},
                   {"end": "parent", "column": "procure", "op": "not_in_ref", "ref": "procure_codes"},
                   {"end": "child", "has_children": True}, {"end": "parent", "has_parents": False},
                   {"end": "child", "in_hierarchy": True}, {"end": "parent", "in_hierarchy": False}]},
        {"id": "children_quantifiers", "kind": "row", "entity": "part", "severity": "violation",
         "subject": "part_id",
         "where": [{"children": "all", "where": [{"column": "retired", "op": "eq", "value": False},
                                                 {"has_children": True}]},
                   {"children": "any", "where": [{"column": "unit", "op": "not_in_ref", "ref": "unit_codes"}]},
                   {"children": "none", "where": [{"column": "name", "op": "is_null"}]}]},
        {"id": "parents_quantifiers", "kind": "row", "entity": "part", "severity": "warning",
         "where": [{"parents": "all", "where": [{"in_hierarchy": True}]},
                   {"parents": "any", "where": [{"column": "unit", "op": "eq", "value": m("pc")}]},
                   {"parents": "none", "where": [{"column": "retired", "op": "eq", "value": True}]}]},
        {"id": "disjunction", "kind": "row", "entity": "bom_line", "severity": "warning", "subject": "parent_id",
         "where": [{"column": "child_id", "op": "is_not_null"},
                   {"any": [[{"column": "qty_per", "op": "in", "set": "quantities"}],
                            [{"end": "child", "column": "unit", "op": "in", "set": "units"},
                             {"column": "qty_per", "op": "ge", "value": "0.4321"}],
                            [{"column": "line_id", "op": "is_null"}]]}],
         "report": {"columns": ["parent_id", "qty_per"]},
         "example": [{"spec": m("a listed quantity"),
                      "rows": {"bom_line": [{"line_id": 97531, "parent_id": m("P"), "child_id": m("C"),
                                             "qty_per": "8642.75"},
                                            {"line_id": {"null": True}, "parent_id": m("P"),
                                             "child_id": {"null": True}, "qty_per": {"null": True}}],
                               "part": [{"part_id": m("C"), "unit": m("pc")}]},
                      "expect": [{"parent_id": m("P"), "qty_per": "8642.75", "n": 1}]}]},
        {"id": "edge_distinct", "kind": "row", "entity": "bom_line", "severity": "warning", "subject": "child_id",
         "where": [{"column": "qty_per", "op": "lt", "value": "-0.4321"}], "report": {"distinct": "child_id"}},
        {"id": "code_rows", "kind": "row", "entity": "code_value", "severity": "warning",
         "where": [{"column": "code", "op": "starts_with", "set": "prefixes"},
                   {"column": "code_group", "op": "ne", "value": m("X")}]},
        {"id": "duplicate_endpoints", "kind": "duplicate", "entity": "bom_line", "severity": "warning",
         "key": [{"end": "parent", "column": "part_class"}, {"end": "child", "column": "name"}, "qty_per"],
         "count": "rows", "min": 86420, "report": "group"},
        {"id": "duplicate_distinct", "kind": "duplicate", "entity": "bom_line", "severity": "warning",
         "subject": "child_id", "key": ["child_id", {"end": "child", "column": "unit"}],
         "count": {"distinct": "parent_id"}, "min": 75319, "report": {"distinct": "child_id"}},
        {"id": "mode_plain", "kind": "mode_outlier", "entity": "bom_line", "severity": "warning",
         "group_by": "parent_id", "value": "qty_per", "min_mode_share": SHARE, "ratio": "13.579"},
        {"id": "mode_distinct", "kind": "mode_outlier", "entity": "bom_line", "severity": "warning",
         "subject": "parent_id", "group_by": "child_id", "value": "qty_per",
         "min_distinct": {"column": "line_id", "n": 64208}, "min_mode_share": "1", "ratio": "8642.75",
         "report": {"distinct": "parent_id"}},
        {"id": "same_attrs", "kind": "same_signature", "severity": "warning", "subject": "group_id",
         "attrs": ["unit"], "min_members": 86420},
        {"id": "stored_height", "kind": "stored_metric", "severity": "warning", "subject": "part_id",
         "metric": "height", "stored": "stored_level"},
        {"id": "text_one_capture", "kind": "text_quantity", "entity": "bom_line", "severity": "warning",
         "end": "parent", "text": "name", "pattern": f"{MARK}([0-9]+) ?kg$", "quantity": "qty_per",
         "rel_tol": "0.4321"},
        {"id": "text_distinct", "kind": "text_quantity", "entity": "bom_line", "severity": "warning",
         "subject": "parent_id", "end": "child", "text": "name",
         "where_end": [{"column": "unit", "op": "not_in", "set": "units"}],
         "pattern": f"{MARK}([0-9.]+)(?:/([0-9]+))?", "quantity": "qty_per", "rel_tol": "13.579",
         "max_text_length": 97531, "report": {"distinct": "parent_id"}},
    ],
}


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(type(value).__name__)


def toml_doc(doc: dict) -> bytes:
    return "".join(f"{k} = {toml_value(v)}\n" for k, v in doc.items()).encode("utf-8")


def atoms_spec(examples: bool = True) -> data_rules.RuleFile:
    doc = json.loads(json.dumps(ATOMS))
    if not examples:
        for rule in doc["rule"]:
            rule.pop("example", None)
    return data_rules.loads(toml_doc(doc), CFG)


ATOMS_SPEC = atoms_spec()
ATOMS_NO_EXAMPLES = atoms_spec(examples=False)
APPENDIX_CFG = config.load(EXAMPLE / "crosscheck.toml")
APPENDIX = config.load_data_rules(APPENDIX_CFG)


# ---- helpers ----------------------------------------------------------------------------------------
def wire(text: str) -> str:
    """:name placeholders as pg8000 sends them: $1, $2, ... in order of first appearance, a repeated name reusing
    its number."""
    names: list = []

    def number(match):
        if match.group(1) not in names:
            names.append(match.group(1))
        return "$" + str(names.index(match.group(1)) + 1)
    return re.sub(r":([a-z_][a-z0-9_]*)", number, text)


def decode_e_string(literal: str) -> str:
    """The value of an E'' literal written by dsql.e_string (escapes \\\\, \\' and \\134 only)."""
    assert literal.startswith("E'") and literal.endswith("'"), literal
    body, out, i = literal[2:-1], [], 0
    while i < len(body):
        ch = body[i]
        if ch == BS:
            if body[i + 1] in (BS, "'"):
                out.append(body[i + 1])
                i += 2
            elif body[i + 1:i + 4] == "134":
                out.append(BS)
                i += 4
            else:
                raise AssertionError(f"unexpected escape in {literal!r}")
        elif ch == "'":
            raise AssertionError(f"unescaped quote in {literal!r}")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def strip_literals(text: str) -> str:
    """The text with the contents of string literals ('...', E'...') removed."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "'" or (text[i] == "E" and text[i + 1:i + 2] == "'" and (i == 0 or not text[i - 1].isalnum())):
            escaped = text[i] == "E"
            i += 2 if escaped else 1
            while True:
                if escaped and text[i] == BS:
                    i += 2
                elif text[i] == "'" and text[i + 1:i + 2] == "'":
                    i += 2
                elif text[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            out.append("''")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def statements(cfg, spec, plan, examples=True):
    """(name, Statement) for every rule, limited rule and example statement of spec."""
    out = []
    for rule in spec.rules:
        st = dsql.rule_statement(cfg, spec, plan, rule)
        out += [(rule.id, st), (f"{rule.id} limited", dsql.limited(st, 200001))]
        for ex in rule.examples if examples else ():
            out.append((f"{rule.id} example", dsql.example_statement(cfg, spec, plan, rule, ex)))
    return out


def param_kinds(st) -> str:
    """name type, ... of a statement's placeholders, in order (never the values)."""
    seen, out = set(), []
    for part in st.sql.parts:
        if isinstance(part, dsql.Param) and id(part) not in seen:
            seen.add(id(part))
            out.append(part.sql_type)
    names = list(st.params)
    return ", ".join(f"{n} {t}" for n, t in zip(names, out)) or "none"


# ---- goldens ----------------------------------------------------------------------------------------
def rule_block(cfg, spec, plan, rule, view=True, examples=True) -> list:
    st = dsql.rule_statement(cfg, spec, plan, rule)
    blocks = [f"-- {st.label}: parameterized; {param_kinds(st)}\n{st.text};"]
    if view:
        blocks.append(f"-- rule {rule.id}: view\n{dsql.view_statement(cfg, spec, plan, rule)}")
    for ex in rule.examples if examples else ():
        est = dsql.example_statement(cfg, spec, plan, rule, ex)
        blocks.append(f"-- {est.label}: parameterized; {len(est.params)} parameters\n{est.text};")
    return blocks


def render_goldens() -> dict:
    files = {}
    for rule in APPENDIX.rules:
        files[f"appendix_a/{rule.id}.sql"] = rule_block(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule)
    files["atoms.sql"] = [b for r in ATOMS_SPEC.rules for b in rule_block(CFG, ATOMS_SPEC, TEXT_PLAN, r)]
    bpchar = [b for r in ATOMS_SPEC.rules for b in rule_block(CFG, ATOMS_SPEC, BPCHAR_PLAN, r)]
    bpchar += [b for r in APPENDIX.rules for b in rule_block(APPENDIX_CFG, APPENDIX, BPCHAR_PLAN, r)]
    bpchar.append("-- subjects view\n" + dsql.subjects_view_statement(APPENDIX_CFG, APPENDIX, BPCHAR_PLAN))
    files["bpchar.sql"] = bpchar
    numeric = [b for r in ATOMS_NO_EXAMPLES.rules
               for b in rule_block(CFG, ATOMS_NO_EXAMPLES, NUMERIC_PLAN, r, examples=False)]
    numeric.append("-- subjects view\n" + dsql.subjects_view_statement(CFG, ATOMS_NO_EXAMPLES, NUMERIC_PLAN))
    files["numeric_keys.sql"] = numeric
    snapshot = []
    for gate in dsql.snapshot_gates(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules, 200000):
        snapshot.append(f"-- {gate.statement.label}: columns {list(gate.columns)}; {param_kinds(gate.statement)}\n"
                        f"{gate.statement.text};")
    for entity, columns in dsql.export_columns(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules).items():
        st = dsql.export_statement(APPENDIX_CFG, TEXT_PLAN, entity, columns)
        snapshot.append(f"-- {st.label}\n{st.text};")
    for st in (dsql.type_plan_statement(APPENDIX_CFG, list(APPENDIX_CFG.entities)),
               dsql.lookup_statement(APPENDIX_CFG, APPENDIX.rule("odd_quantity"), 200001),
               dsql.subjects_lookup_statement(APPENDIX_CFG, 200001), dsql.orphan_statement(APPENDIX_CFG)):
        snapshot.append(f"-- {st.label}: {param_kinds(st)}\n{st.text};")
    files["snapshot.sql"] = snapshot
    views = ["-- subjects view\n" + dsql.subjects_view_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN)]
    views.append("-- marker comment\n" + dsql.comment_statement(data_rules.view_name(APPENDIX_CFG, "qty_over_limit"))
                 + ";")
    views.append("-- drop\n" + dsql.drop_view_statement(data_rules.subjects_view_name(APPENDIX_CFG)) + ";")
    files["views.sql"] = views
    rule = APPENDIX.rule("grams_in_count_unit")
    record = [dsql.encoding_statement(), dsql.limited(dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule),
                                                      200001),
              dsql.example_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule, rule.examples[0])]
    files["rules_sql.sql"] = [dsql.rules_sql(record).rstrip("\n")]
    return {name: "\n\n".join(blocks) + "\n" for name, blocks in files.items()}


def write_goldens() -> None:
    for name, text in render_goldens().items():
        path = GOLDEN / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


class GoldenSqlTest(unittest.TestCase):
    """Every golden is byte-identical to the current compilation."""

    @classmethod
    def setUpClass(cls):
        cls.current = render_goldens()

    def test_every_golden_is_byte_identical(self):
        for name, text in self.current.items():
            with self.subTest(golden=name):
                self.assertEqual(text, (GOLDEN / name).read_bytes().decode("utf-8"))

    def test_no_golden_is_missing_or_unexpected(self):
        found = {p.relative_to(GOLDEN).as_posix() for p in GOLDEN.rglob("*.sql")}
        self.assertEqual(found, set(self.current))

    def test_the_appendix_rules_cover_every_kind(self):
        self.assertEqual({r.kind for r in APPENDIX.rules}, set(data_rules.KINDS))
        self.assertEqual({r.kind for r in ATOMS_SPEC.rules}, set(data_rules.KINDS))


# ---- statement model --------------------------------------------------------------------------------
TRICKY = ["plain", "it's", BS, "a" + BS, BS + "'", "'" + BS, "a" + BS + BS + "b", "''", "x:y $1 --", NON_ASCII,
          "line\nbreak\ttab", BS + "n", "E'x'", ""]


class StatementModelTest(unittest.TestCase):
    def test_parameterized_numbers_slots_in_order_and_reuses_one_param(self):
        a, b = dsql.Param("x", "text"), dsql.Param(5, "integer")
        st = dsql.Statement(dsql.Sql("SELECT ", a, ", ", b, ", ", a, " LIMIT ",
                                     dsql.Param(9, "integer", name="limit")))
        text, params = st.parameterized()
        self.assertEqual(text, "SELECT CAST(:p1 AS text), CAST(:p2 AS bigint), CAST(:p1 AS text) LIMIT "
                               "CAST(:limit AS bigint)")
        self.assertEqual(params, {"p1": "x", "p2": 5, "limit": 9})
        self.assertNotIn("::", text)

    def test_value_types(self):
        self.assertEqual(dsql.Param("12.50", "number").value, Decimal("12.50"))
        self.assertEqual(dsql.Param(7, "number").value, Decimal(7))
        self.assertIsNone(dsql.Param(None, "text", "varchar(3)").value)
        for value, kind in ((True, "number"), (1.5, "number"), ("1e3", "number"), (Decimal("NaN"), "number"),
                            (True, "integer"), ("7", "integer"), (7, "text"), ("a\x00", "text"), (1, "boolean"),
                            ("x", "date")):
            with self.subTest(value=value, kind=kind), self.assertRaises(dsql.CompileError):
                dsql.Param(value, kind)
        for sql_type in ("text; DROP", "numeric(1,2,3)", "Text", ""):
            if sql_type:
                with self.subTest(sql_type=sql_type), self.assertRaises(dsql.CompileError):
                    dsql.Param("x", "text", sql_type)
        for name in ("p1", "Limit", "x-y"):
            with self.subTest(name=name), self.assertRaises(dsql.CompileError):
                dsql.Param(1, "integer", name=name)

    def test_repr_never_shows_the_value(self):
        self.assertNotIn(MARK, repr(dsql.Param(m("secret"), "text")))

    def test_one_name_bound_to_two_values_is_refused(self):
        st = dsql.Statement(dsql.Sql(dsql.Param(1, "integer", name="limit"), dsql.Param(2, "integer", name="limit")))
        with self.assertRaises(dsql.CompileError):
            st.parameterized()

    def test_literal_rendering(self):
        st = dsql.Statement(dsql.Sql("SELECT ", dsql.Param("it's", "text"), ", ", dsql.Param("0.333300", "number"),
                                     ", ", dsql.Param(-12, "integer"), ", ", dsql.Param(False, "boolean"), ", ",
                                     dsql.Param(None, "number", "numeric(18,6)")))
        self.assertEqual(st.literal(), "SELECT CAST(E'it\\'s' AS text), CAST(E'0.333300' AS numeric), "
                                       "CAST(E'-12' AS bigint), CAST(E'false' AS boolean), "
                                       "CAST(NULL AS numeric(18,6))")
        self.assertEqual(dsql.Statement(dsql.Sql(dsql.Param(Decimal("1E+2"), "number"))).literal(),
                         "CAST(E'100' AS numeric)")

    def test_e_string_escapes_exactly(self):
        self.assertEqual(dsql.e_string("a" + BS + "b'c"), "E'a" + BS + BS + "b" + BS + "'c'")
        self.assertEqual(dsql.e_string("x" + BS), "E'x" + BS + "134'")       # a final backslash: octal escape
        self.assertEqual(dsql.e_string(NON_ASCII), "E'" + NON_ASCII + "'")       # non-ASCII as is
        for text in TRICKY:
            with self.subTest(text=text):
                literal = dsql.e_string(text)
                self.assertEqual(decode_e_string(literal), text)
                self.assertFalse(literal[:-1].endswith(BS), "the closing quote never follows a backslash")
        with self.assertRaises(dsql.CompileError):
            dsql.e_string("a\x00b")

    @unittest.skipUnless(HAS_PG8000, "pg8000 not installed")
    def test_literals_survive_pg8000_placeholder_scanning(self):
        """build runs view definitions through pg8000.native, whose scanner rewrites :name outside literals; a
        closing quote after a doubled backslash would make it read the next literal's ':' as a placeholder."""
        from pg8000.native import to_statement
        items = []
        for text in TRICKY:
            items += [dsql.Param(text, "text"), dsql.Param("a:b $1 :p1", "text")]
        sql = dsql.Statement(dsql.Sql("SELECT ", dsql.Sql.join(", ", items))).literal()
        converted, make_values = to_statement(sql)
        self.assertEqual(converted, sql)
        self.assertEqual(make_values({}), ())
        for rule in APPENDIX.rules + ATOMS_SPEC.rules:
            spec, cfg = (APPENDIX, APPENDIX_CFG) if rule in APPENDIX.rules else (ATOMS_SPEC, CFG)
            view = dsql.view_statement(cfg, spec, TEXT_PLAN, rule)
            self.assertEqual(to_statement(view)[0], view, rule.id)

    @unittest.skipUnless(HAS_PG8000, "pg8000 not installed")
    def test_parameterized_text_converts_as_pg8000_converts_it(self):
        from pg8000.native import to_statement
        for name, st in statements(APPENDIX_CFG, APPENDIX, TEXT_PLAN) + statements(CFG, ATOMS_SPEC, BPCHAR_PLAN):
            with self.subTest(statement=name):
                converted, make_values = to_statement(st.text)
                self.assertEqual(converted, wire(st.text))
                self.assertEqual(len(make_values(st.params)), len(st.params))

    def test_sql_fragments(self):
        p = dsql.Param(1, "integer")
        frag = "a" + dsql.Sql("b", p) + "c"
        self.assertEqual(frag.parts, ("ab", p, "c"))
        self.assertEqual(dsql.Sql.join(", ", ["x", dsql.Sql("y")]).parts, ("x, y",))
        with self.assertRaises(dsql.CompileError):
            dsql.Sql(3)


# ---- constants, quoting, casts ----------------------------------------------------------------------
def configured_names(cfg) -> set:
    names = {cfg.source.output_schema}
    for entity in cfg.entities.values():
        names |= set(entity.table.split("."))
    for cols in TEXT_SCHEMA.values():
        names |= set(cols)
    return names


class ConstantTest(unittest.TestCase):
    """The parameterized text holds no configured constant: every value is bound."""

    def test_no_marker_in_parameterized_text(self):
        bound = []
        for name, st in statements(CFG, ATOMS_SPEC, TEXT_PLAN) + statements(CFG, ATOMS_SPEC, BPCHAR_PLAN):
            text, params = st.parameterized()
            with self.subTest(statement=name):
                self.assertNotIn(MARK, text)
                for number in NUMBERS + (SHARE, "9753"):
                    self.assertIsNone(re.search(rf"(?<![0-9p]){re.escape(number)}(?![0-9])", text), number)
            bound += [str(v) for v in params.values()]
        joined = " ".join(bound)
        self.assertIn(MARK, joined)                                    # positive control: the values are bound
        for number in NUMBERS + ("9753", "10000"):
            self.assertIn(number, joined)
        self.assertNotIn(SHARE, joined)

    def test_constants_appear_only_in_literal_views(self):
        views = " ".join(dsql.view_statement(CFG, ATOMS_SPEC, TEXT_PLAN, r) for r in ATOMS_SPEC.rules)
        self.assertIn(f"E'{MARK}-Frame'", views)
        self.assertIn("CAST(E'8642.75' AS numeric)", views)
        self.assertIn("CAST(E'-97531' AS numeric)", views)

    def test_rules_sql_holds_placeholders_only(self):
        record = [st for _name, st in statements(CFG, ATOMS_SPEC, TEXT_PLAN)]
        record += [g.statement for g in dsql.snapshot_gates(CFG, ATOMS_SPEC, TEXT_PLAN, ATOMS_SPEC.rules, 7)]
        text = dsql.rules_sql(record)
        self.assertTrue(text.startswith(dsql.RULES_SQL_HEADER + "\n-- rule column_ops (row)\nSELECT "))
        self.assertNotIn(MARK, text)
        self.assertNotIn("E'", text)
        self.assertEqual(text.count(";\n"), len(record))
        self.assertTrue(text.endswith(";\n"))

    def test_labels_cannot_inject_lines(self):
        text = dsql.rules_sql([dsql.Statement("SELECT 1", "x\nDROP TABLE t; --")])
        self.assertIn("-- x?DROP TABLE t? --\nSELECT 1;", text)


class QuotingTest(unittest.TestCase):
    def test_every_configured_identifier_is_quoted(self):
        cases = statements(CFG, ATOMS_SPEC, TEXT_PLAN) + statements(APPENDIX_CFG, APPENDIX, TEXT_PLAN)
        cases += [(g.statement.label, g.statement)
                  for g in dsql.snapshot_gates(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules, 9)]
        texts = [(name, st.text) for name, st in cases]
        texts += [(r.id + " view", dsql.view_statement(CFG, ATOMS_SPEC, TEXT_PLAN, r)) for r in ATOMS_SPEC.rules]
        texts.append(("subjects", dsql.subjects_view_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN)))
        names = configured_names(CFG)
        for name, text in texts:
            stripped = strip_literals(text)
            for identifier in names:
                with self.subTest(statement=name, identifier=identifier):
                    bare = re.findall(rf'(?<![A-Za-z0-9_"]){identifier}(?![A-Za-z0-9_"])', stripped)
                    self.assertEqual(bare, [])

    def test_example_statements_read_no_table(self):
        for name, st in statements(APPENDIX_CFG, APPENDIX, TEXT_PLAN) + statements(CFG, ATOMS_SPEC, BPCHAR_PLAN):
            if not name.endswith("example"):
                continue
            with self.subTest(statement=name):
                self.assertNotIn('"sxc_rules".', st.text)
                relations = set(re.findall(r'(?:FROM|JOIN) ("[a-z0-9_]+")', st.text))
                self.assertTrue(relations)
                self.assertTrue(all(r.startswith('"sxc_ex_') for r in relations), relations)


_BASE_REF = re.compile(r'(?<!CAST\()\b(?:t|n|e|r|en|np|nc|e2|r2|k)\."(?P<col>[a-z_][a-z0-9_]*)"(?! AS text\))')


class BpcharTest(unittest.TestCase):
    def test_every_bpchar_reference_is_cast_to_text(self):
        bpchar = {c for (_e, c) in BPCHAR}
        cases = statements(CFG, ATOMS_SPEC, BPCHAR_PLAN) + statements(APPENDIX_CFG, APPENDIX, BPCHAR_PLAN)
        cases += [(g.statement.label, g.statement)
                  for g in dsql.snapshot_gates(APPENDIX_CFG, APPENDIX, BPCHAR_PLAN, APPENDIX.rules, 9)]
        cases += [(f"export {e}", dsql.export_statement(APPENDIX_CFG, BPCHAR_PLAN, e, c)) for e, c in
                  dsql.export_columns(APPENDIX_CFG, APPENDIX, BPCHAR_PLAN, APPENDIX.rules).items()]
        casts = 0
        for name, st in cases:
            text = st.text
            with self.subTest(statement=name):
                bare = [mt.group("col") for mt in _BASE_REF.finditer(text) if mt.group("col") in bpchar]
                self.assertEqual(bare, [])
            casts += text.count(" AS text)")
        self.assertGreater(casts, 100)
        self.assertEqual(data_rules.apply_types(APPENDIX, BPCHAR_PLAN), {})
        self.assertEqual(data_rules.apply_types(ATOMS_SPEC, BPCHAR_PLAN), {})

    def test_text_plan_casts_nothing(self):
        for name, st in statements(CFG, ATOMS_SPEC, TEXT_PLAN):
            with self.subTest(statement=name):
                self.assertNotRegex(st.text, r'CAST\([a-z0-9]+\."')

    def test_exports_cast_bpchar_and_keep_the_name(self):
        st = dsql.export_statement(APPENDIX_CFG, BPCHAR_PLAN, "part", ("part_id", "retired"))
        self.assertEqual(st.text, 'SELECT CAST(t."part_id" AS text) AS "part_id", t."retired" FROM '
                                  '"sxc_rules"."part" t')


class CollateTest(unittest.TestCase):
    def order_items(self, view: str) -> list:
        head = re.search(r"row_number\(\) OVER \(ORDER BY (.*?)\) AS ord FROM \(", view).group(1)
        return head.split(", ")

    def test_collate_c_only_on_text_outputs(self):
        for cfg, spec, plan in ((CFG, ATOMS_SPEC, TEXT_PLAN), (CFG, ATOMS_SPEC, BPCHAR_PLAN),
                                (CFG, ATOMS_NO_EXAMPLES, NUMERIC_PLAN), (APPENDIX_CFG, APPENDIX, TEXT_PLAN)):
            for rule in spec.rules:
                items = self.order_items(dsql.view_statement(cfg, spec, plan, rule))
                classes = dsql.output_types(spec, plan, rule)
                self.assertEqual(len(items), len(rule.columns))
                for item, name, cls in zip(items, rule.columns, classes):
                    with self.subTest(rule=rule.id, column=name):
                        collate = ' COLLATE "C"' if cls in ("text", "bpchar") else ""
                        self.assertEqual(item, f'r."{name}"{collate} NULLS FIRST')

    def test_numeric_keys_sort_by_value(self):
        rule = ATOMS_NO_EXAMPLES.rule("same_attrs")
        view = dsql.view_statement(CFG, ATOMS_NO_EXAMPLES, NUMERIC_PLAN, rule)
        self.assertNotIn("COLLATE", view)
        self.assertIn("CAST(e.kid AS text)", view)
        self.assertIn("min(k.member) OVER w", view)
        subjects = dsql.subjects_view_statement(CFG, ATOMS_NO_EXAMPLES, NUMERIC_PLAN)
        self.assertTrue(subjects.endswith("(ORDER BY g.severity_rank, g.subject) AS ord FROM g;"))
        text = dsql.view_statement(CFG, ATOMS_SPEC, TEXT_PLAN, ATOMS_SPEC.rule("same_attrs"))
        self.assertIn("encode(convert_to(e.kid, 'UTF8'), 'hex')", text)
        self.assertIn('ORDER BY e.kid COLLATE "C"', text)
        self.assertIn('min(k.member COLLATE "C") OVER w', text)

    def test_numeric_children_signature_trims_the_scale(self):
        plan = make_plan({("part", "part_id"): "numeric(10,2)", ("bom_line", "parent_id"): "numeric(10,2)",
                          ("bom_line", "child_id"): "numeric(10,2)"})
        st = dsql.rule_statement(CFG, ATOMS_NO_EXAMPLES, plan, ATOMS_NO_EXAMPLES.rule("same_attrs"))
        self.assertIn("string_agg(CAST(trim_scale(e.kid) AS text) || ':' || CAST(trim_scale(e.qsum) AS text), ','"
                      " ORDER BY e.kid)", st.text)


# ---- kinds and atoms (structure) --------------------------------------------------------------------
class TemplateTest(unittest.TestCase):
    def text(self, rule_id: str, spec=APPENDIX, cfg=APPENDIX_CFG, plan=TEXT_PLAN) -> str:
        return dsql.rule_statement(cfg, spec, plan, spec.rule(rule_id)).text

    def test_null_guards(self):
        text = self.text("unknown_procure_code")
        self.assertIn('(t."procure" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."code_value" r WHERE '
                      'r."code" = t."procure" AND r."code_group" = CAST(:p1 AS text)))', text)
        self.assertIn('(t."parent_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE '
                      'n."part_id" = t."parent_id"))', self.text("parent_unknown"))
        atoms = self.text("set_ops", ATOMS_SPEC, CFG)
        self.assertIn('(t."unit" IS NOT NULL AND t."unit" NOT IN (CAST(:p3 AS text), CAST(:p4 AS text)))', atoms)
        self.assertIn('(t."name" IS NOT NULL AND (starts_with(t."name", CAST(:p5 AS text)) OR starts_with(t."name", '
                      'CAST(:p6 AS text))))', atoms)
        self.assertIn('(t."procure" IS NULL) AND (t."issue" IS NOT NULL)', atoms)

    def test_all_requires_a_neighbour(self):
        text = self.text("all_children_retired")
        self.assertIn('(EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."parent_id" = t."part_id" AND '
                      'e."child_id" IS NOT NULL) AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE '
                      'e."parent_id" = t."part_id" AND e."child_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM '
                      '"sxc_rules"."part" n WHERE n."part_id" = e."child_id" AND (n."retired" IS NOT NULL AND '
                      'n."retired" = CAST(:p2 AS boolean)))))', text)

    def test_quantified_sub_where_aliases_never_shadow(self):
        text = self.text("children_quantifiers", ATOMS_SPEC, CFG)
        self.assertIn('(EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e2 WHERE e2."parent_id" = n."part_id" AND '
                      'e2."child_id" IS NOT NULL))', text)
        self.assertIn('NOT EXISTS (SELECT 1 FROM "sxc_rules"."code_value" r2 WHERE r2."code" = n."unit")', text)
        self.assertIn('(NOT EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e JOIN "sxc_rules"."part" n ON n."part_id" '
                      '= e."child_id" WHERE e."parent_id" = t."part_id" AND (n."name" IS NULL)))', text)
        parents = self.text("parents_quantifiers", ATOMS_SPEC, CFG)
        self.assertIn('(EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."child_id" = t."part_id" AND '
                      'e."parent_id" IS NOT NULL) AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE '
                      'e."child_id" = t."part_id" AND e."parent_id" IS NOT NULL AND NOT EXISTS', parents)

    def test_endpoint_atoms(self):
        text = self.text("endpoint_atoms", ATOMS_SPEC, CFG)
        expected = [
            '(t."parent_id" IS NOT NULL AND EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE n."part_id" = '
            't."parent_id"))',
            '(t."child_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE n."part_id" = '
            't."child_id"))',
            '(EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE n."part_id" = t."child_id" AND (n."unit" IS NOT NULL '
            'AND n."unit" <> CAST(:p1 AS text))))',
            '(t."child_id" IS NOT NULL AND EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."parent_id" = '
            't."child_id" AND e."child_id" IS NOT NULL))',
            '(t."parent_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."child_id" = '
            't."parent_id" AND e."parent_id" IS NOT NULL))',
            '(t."parent_id" IS NOT NULL AND NOT (EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."parent_id" '
            '= t."parent_id" AND e."child_id" IS NOT NULL) OR EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE '
            'e."child_id" = t."parent_id" AND e."parent_id" IS NOT NULL)))',
        ]
        for fragment in expected:
            self.assertIn(fragment, text)
        self.assertTrue(text.startswith('SELECT t."line_id" FROM "sxc_rules"."bom_line" t WHERE '))

    def test_disjunction(self):
        text = self.text("disjunction", ATOMS_SPEC, CFG)
        self.assertIn('AND ((t."qty_per" IS NOT NULL AND t."qty_per" IN (CAST(:p1 AS numeric), CAST(:p2 AS numeric))) '
                      'OR ((EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE n."part_id" = t."child_id" AND '
                      '(n."unit" IS NOT NULL AND n."unit" IN (CAST(:p3 AS text), CAST(:p4 AS text))))) AND '
                      '(t."qty_per" IS NOT NULL AND t."qty_per" >= CAST(:p5 AS numeric))) OR (t."line_id" IS NULL))',
                      text)

    def test_report_forms(self):
        self.assertTrue(self.text("code_rows", ATOMS_SPEC, CFG).startswith(
            'SELECT t."code_group", t."code" FROM "sxc_rules"."code_value" t WHERE '))
        self.assertTrue(self.text("set_ops", ATOMS_SPEC, CFG).endswith(' AND t."name" IS NOT NULL'))
        self.assertTrue(self.text("ref_ops", ATOMS_SPEC, CFG).startswith('SELECT t."procure", count(*) AS "n" FROM '))
        self.assertTrue(self.text("ref_ops", ATOMS_SPEC, CFG).endswith(' GROUP BY t."procure"'))

    def test_duplicate_joins_each_end_once(self):
        text = self.text("duplicate_endpoints", ATOMS_SPEC, CFG)
        self.assertEqual(text, 'SELECT np."part_class" AS "parent_part_class", nc."name" AS "child_name", t."qty_per", '
                               'count(*) AS "n" FROM "sxc_rules"."bom_line" t JOIN "sxc_rules"."part" np ON '
                               'np."part_id" = t."parent_id" JOIN "sxc_rules"."part" nc ON nc."part_id" = t."child_id" '
                               'WHERE np."part_class" IS NOT NULL AND nc."name" IS NOT NULL AND t."qty_per" IS NOT '
                               'NULL GROUP BY np."part_class", nc."name", t."qty_per" HAVING count(*) >= CAST(:p1 AS '
                               'bigint)')

    def test_mode_share_is_an_integer_test(self):
        rule = APPENDIX.rule("odd_quantity")
        st = dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule)
        self.assertIn("f.n_val * CAST(:p2 AS numeric) >= CAST(:p3 AS numeric) * g.n_rows AND f.val > 0", st.text)
        self.assertEqual(st.params, {"p1": 3, "p2": Decimal(4), "p3": Decimal(3), "p4": Decimal(20)})
        plain = dsql.rule_statement(CFG, ATOMS_SPEC, TEXT_PLAN, ATOMS_SPEC.rule("mode_plain"))
        self.assertNotIn("n_distinct", plain.text)
        self.assertEqual((plain.params["p1"], plain.params["p2"]), (Decimal(10000), Decimal(9753)))  # den, num
        self.assertEqual(plain.params["p1"] * Decimal(SHARE), plain.params["p2"])

    def test_text_quantity_binds_the_patterns(self):
        st = dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rule("grams_in_count_unit"))
        params = st.params
        self.assertEqual(params["p1"], APPENDIX.rule("grams_in_count_unit").pattern.are)
        self.assertEqual((params["p2"], params["p3"]), (dsql.NUM_CAPTURE, dsql.DIV_CAPTURE))
        self.assertNotIn("[0-9]", st.text)
        one = dsql.rule_statement(CFG, ATOMS_SPEC, TEXT_PLAN, ATOMS_SPEC.rule("text_one_capture"))
        self.assertIn("CAST(1 AS numeric) AS divisor", one.text)
        self.assertNotIn("rx.g[2]", one.text)
        self.assertNotIn(dsql.DIV_CAPTURE, one.params.values())

    def test_recursion_cap_is_max_depth_plus_one_and_the_gate_exists(self):
        st = dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rule("stored_level_drift"))
        self.assertIn("WHERE h.d < CAST(:p1 AS bigint)", st.text)
        self.assertEqual(st.params, {"p1": APPENDIX.hierarchy.max_depth + 1})
        self.assertEqual(APPENDIX.hierarchy.max_depth + 1, 17)
        gate = dsql.height_gate_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN)
        self.assertEqual(gate.params, {"p1": 17})
        self.assertEqual(gate.text.count("CAST(:p1 AS bigint)"), 2)
        self.assertTrue(gate.text.endswith("SELECT (SELECT count(*) FROM ht WHERE ht.height >= CAST(:p1 AS bigint)) "
                                           "+ (SELECT count(*) FROM (SELECT DISTINCT edge.p FROM edge) x WHERE NOT "
                                           "EXISTS (SELECT 1 FROM ht WHERE ht.node = x.p)) AS unbounded"))
        gates = dsql.snapshot_gates(APPENDIX_CFG, APPENDIX, TEXT_PLAN, [APPENDIX.rule("stored_level_drift")], 5)
        self.assertIn("height", [g.gate for g in gates])
        self.assertEqual(sorted(data_rules.HEIGHT_KINDS), sorted(k.strip("'") for k in dsql.HEIGHT_KIND_CASE))

    def test_height_recursion_has_one_endpoint_type(self):
        """A recursive CTE needs one column type in both terms (else SQLSTATE 42804 at run time, which no parser
        sees): differing parent and child types are cast to their class's common type."""
        rule = ATOMS_NO_EXAMPLES.rule("stored_height")
        cases = {
            ("text", "text", "text"): ('t."parent_id"', 't."child_id"'),
            ("text", "character varying(20)", "text"): ('CAST(t."parent_id" AS text)', 'CAST(t."child_id" AS text)'),
            ("text", "character(12)", "text"): ('CAST(t."parent_id" AS text)', 't."child_id"'),
            ("text", "character(12)", "character varying(12)"): ('CAST(t."parent_id" AS text)',
                                                                  'CAST(t."child_id" AS text)'),
            ("integer", "integer", "integer"): ('t."parent_id"', 't."child_id"'),
            ("integer", "integer", "bigint"): ('CAST(t."parent_id" AS bigint)', 'CAST(t."child_id" AS bigint)'),
            ("integer", "numeric(10,0)", "integer"): ('CAST(t."parent_id" AS numeric)',
                                                      'CAST(t."child_id" AS numeric)'),
        }
        for (key, parent, child), (p, c) in cases.items():
            plan = make_plan({("part", "part_id"): key, ("bom_line", "parent_id"): parent,
                              ("bom_line", "child_id"): child})
            with self.subTest(parent=parent, child=child):
                for st in (dsql.rule_statement(CFG, ATOMS_NO_EXAMPLES, plan, rule),
                           dsql.height_gate_statement(CFG, ATOMS_NO_EXAMPLES, plan)):
                    self.assertTrue(st.text.startswith(f"WITH RECURSIVE edge AS (SELECT DISTINCT {p} AS p, {c} AS c "
                                                       f'FROM "sxc_rules"."bom_line" t WHERE {p} IS NOT NULL AND {c} '
                                                       "IS NOT NULL), "), st.text[:160])

    def test_limit_wrapper(self):
        st = dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rule("kit_without_bom"))
        limited = dsql.limited(st, 200001)
        self.assertEqual(limited.text, f"SELECT * FROM ({st.text}) r LIMIT CAST(:limit AS bigint)")
        self.assertEqual(limited.params, {"p1": "KIT", "limit": 200001})
        self.assertEqual(limited.label, st.label)
        budget = dsql.budget_statement(APPENDIX_CFG, "part", 200001)
        self.assertEqual(budget.text, 'SELECT count(*) FROM (SELECT 1 FROM "sxc_rules"."part" LIMIT CAST(:limit AS '
                                      'bigint)) AS bounded')
        self.assertEqual(budget.params, {"limit": 200001})

    def test_columns_are_the_rule_outputs(self):
        """The select list of every compiled rule names the rule's output columns in order."""
        for spec, cfg in ((APPENDIX, APPENDIX_CFG), (ATOMS_SPEC, CFG)):
            for rule in spec.rules:
                lookup = dsql.lookup_statement(cfg, rule, 5).text
                with self.subTest(rule=rule.id):
                    names = re.findall(r'v\."([a-z_][a-z0-9_]*)"', lookup)
                    self.assertEqual(tuple(names), rule.columns)

    def test_a_rule_without_usable_types_does_not_compile(self):
        plan = make_plan({("part", "issue"): "uuid"})
        self.assertEqual(data_rules.apply_types(APPENDIX, plan), {"kit_without_bom": "column_type"})
        with self.assertRaises(dsql.CompileError):
            dsql.rule_statement(APPENDIX_CFG, APPENDIX, plan, APPENDIX.rule("kit_without_bom"))

    def test_parameter_budget(self):
        rule = APPENDIX.rule("truncated_third")
        with mock.patch.object(dsql, "MAX_PARAMS", 3):
            with self.assertRaises(RuleSpecError) as caught:
                dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule)
        self.assertEqual(str(caught.exception), "data-rules.toml rule[8] truncated_third: binds more than 3 values "
                                                "in one statement: use smaller sets or fewer example rows")


# ---- examples ---------------------------------------------------------------------------------------
class ExampleStatementTest(unittest.TestCase):
    def test_example_rows_are_bound_with_declared_types(self):
        rule = APPENDIX.rule("qty_over_limit")
        st = dsql.example_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule, rule.examples[0])
        self.assertTrue(st.text.startswith('WITH "sxc_ex_bom_line"("qty_per", "parent_id", "child_id") AS (VALUES '
                                           '(CAST(:p1 AS numeric(18,6)), CAST(:p2 AS text), CAST(:p3 AS text)), '))
        self.assertTrue(st.text.endswith(' SELECT * FROM (SELECT t."parent_id", t."child_id", t."qty_per", count(*) '
                                         'AS "n" FROM "sxc_ex_bom_line" t WHERE (t."qty_per" IS NOT NULL AND '
                                         't."qty_per" > CAST(:p13 AS numeric)) GROUP BY t."parent_id", t."child_id", '
                                         't."qty_per") r'))
        params = st.params
        self.assertEqual(len(params), 13)
        self.assertEqual([params["p1"], params["p4"], params["p10"]], [Decimal("500"), Decimal("500.000001"), None])
        self.assertEqual(params["p13"], Decimal("500"))

    def test_bpchar_integer_and_empty_entities(self):
        rule = ATOMS_SPEC.rule("disjunction")
        st = dsql.example_statement(CFG, ATOMS_SPEC, BPCHAR_PLAN, rule, rule.examples[0])
        self.assertIn('"sxc_ex_bom_line"("child_id", "qty_per", "line_id", "parent_id") AS (VALUES (CAST(:p1 AS '
                      'bpchar(12)), CAST(:p2 AS numeric(18,6)), CAST(:p3 AS integer), CAST(:p4 AS bpchar(12))), ',
                      st.text)
        self.assertEqual(st.params["p3"], 97531)
        self.assertIsNone(st.params["p7"])
        spec = data_rules.loads(toml_doc({"version": 1, "rule": [
            {"id": "lonely", "kind": "row", "entity": "part", "severity": "warning", "where": [{"has_children": True}],
             "example": [{"spec": "no lines", "rows": {"part": [{"part_id": "A"}], "bom_line": []}, "expect": []}]}]}),
            CFG)
        lonely = spec.rule("lonely")
        st = dsql.example_statement(CFG, spec, TEXT_PLAN, lonely, lonely.examples[0])
        self.assertIn('"sxc_ex_bom_line"("parent_id", "child_id") AS (SELECT CAST(NULL AS text), CAST(NULL AS text) '
                      'WHERE false)', st.text)

    def test_example_relation_names(self):
        self.assertEqual(dsql.example_relation("part"), "sxc_ex_part")
        long_a, long_b = "a" * 57, "a" * 56 + "b"
        names = {dsql.example_relation(long_a), dsql.example_relation(long_b), dsql.example_relation("a" * 56)}
        self.assertEqual(len(names), 3)
        self.assertTrue(all(len(n) <= 63 and ident.check(n) for n in names))

    def test_example_parameter_budget(self):
        rule = APPENDIX.rule("qty_over_limit")
        with mock.patch.object(dsql, "MAX_PARAMS", 12):
            with self.assertRaises(RuleSpecError) as caught:
                dsql.example_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule, rule.examples[0])
        self.assertIn("rule[0] qty_over_limit example[0]: binds more than 12 values", str(caught.exception))


# ---- type plan --------------------------------------------------------------------------------------
class TypePlanTest(unittest.TestCase):
    def test_classification_by_oid_and_typmod(self):
        ct = dsql.column_type
        self.assertEqual(ct(1700, (18 << 16 | 6) + 4), data_rules.ColumnType("number", "numeric", 18, 6))
        self.assertEqual(ct(1700, -1), data_rules.ColumnType("number", "numeric"))
        self.assertEqual(ct(1700, (5 << 16 | (-2 & 0x7FF)) + 4), data_rules.ColumnType("number", "numeric", 5, -2))
        self.assertEqual(ct(1043, 24), data_rules.ColumnType("text", "varchar", length=20))
        self.assertEqual(ct(1042, 7), data_rules.ColumnType("bpchar", "bpchar", length=3))
        self.assertEqual(ct(25, -1, None), data_rules.ColumnType("text", "text"))
        self.assertEqual(ct(23, -1), data_rules.ColumnType("number", "int4"))
        self.assertEqual(ct(21, -1).base, "int2")
        self.assertEqual(ct(20, -1).base, "int8")
        self.assertEqual(ct(16, -1), data_rules.ColumnType("boolean", "bool"))
        self.assertEqual(ct(25, -1, False), data_rules.ColumnType("text", "text", reason="collation"))
        self.assertEqual(ct(1042, 9, False).reason, "collation")
        self.assertEqual(ct(23, -1, False).reason, "")                  # collation matters for text only
        for oid, kind in ((701, "b"), (2950, "b"), (16384, "d"), (25, "d"), (1009, "b"), ("25", "b")):
            with self.subTest(oid=oid, kind=kind):
                self.assertEqual(ct(oid, -1, True, kind), data_rules.ColumnType("other", reason="column_type"))

    def test_catalog_types_match_declared_types(self):
        cases = {"numeric(18,6)": (1700, (18 << 16 | 6) + 4), "character varying(20)": (1043, 24),
                 "character(3)": (1042, 7), "integer": (23, -1), "bigint": (20, -1), "smallint": (21, -1),
                 "text": (25, -1), "boolean": (16, -1), "numeric": (1700, -1)}
        for declared, (oid, mod) in cases.items():
            with self.subTest(declared=declared):
                t = dsql.column_type(oid, mod)
                self.assertEqual(t, data_rules.declared_type(declared))
                self.assertEqual(data_rules.declared_type(dsql.declared_sql_type(t)), t)

    def test_declared_sql_types(self):
        self.assertEqual(dsql.declared_sql_type(dsql.column_type(1700, (18 << 16 | 6) + 4)), "numeric(18,6)")
        self.assertEqual(dsql.declared_sql_type(dsql.column_type(1043, 24)), "varchar(20)")
        self.assertEqual(dsql.declared_sql_type(dsql.column_type(1042, 7)), "bpchar(3)")
        self.assertEqual(dsql.declared_sql_type(dsql.column_type(1042, -1)), "bpchar")
        self.assertEqual(dsql.declared_sql_type(dsql.column_type(21, -1)), "smallint")
        with self.assertRaises(dsql.CompileError):
            dsql.declared_sql_type(dsql.column_type(701, -1))

    def test_type_plan_rows(self):
        rows = [{"entity": "part", "column_name": "part_id", "type_oid": 25, "type_mod": -1, "type_kind": "b",
                 "deterministic": True},
                ("part", "name", 25, -1, "b", False), ("bom_line", "qty_per", 1700, (18 << 16 | 6) + 4, "b", None)]
        plan = dsql.type_plan(rows)
        self.assertEqual(plan.column("part", "part_id"), data_rules.ColumnType("text", "text"))
        self.assertEqual(plan.column("part", "name").reason, "collation")
        self.assertEqual(plan.column("bom_line", "qty_per"), data_rules.ColumnType("number", "numeric", 18, 6))

    def test_the_catalog_statement_binds_names(self):
        st = dsql.type_plan_statement(APPENDIX_CFG, ["part", "code_value"])
        self.assertEqual(st.params, {"p1": "part", "p2": "sxc_rules", "p3": "part", "p4": "code_value",
                                     "p5": "sxc_rules", "p6": "code_value"})
        self.assertNotIn("sxc_rules", st.text)
        self.assertNotIn("::", st.text)
        with self.assertRaises(dsql.CompileError):
            dsql.type_plan_statement(APPENDIX_CFG, [])


# ---- gates ------------------------------------------------------------------------------------------
NODE_KEY_RULES = {"parent_unknown", "kit_without_bom", "retired_child_in_active_parent", "all_children_retired",
                  "retired_assembly_in_use", "unknown_procure_code", "temporary_name", "truncated_third",
                  "same_name_components", "twin_assemblies", "stored_level_drift", "grams_in_count_unit"}
QTY_RULES = {"qty_over_limit", "truncated_third", "odd_quantity", "twin_assemblies", "grams_in_count_unit"}


class GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gates = dsql.snapshot_gates(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules, 200000)

    def rows(self, **changes) -> list:
        base = {"encoding": ["UTF8"], "budget": [10], "key": [0, 0], "non_finite": [0], "null_quantity": [0],
                "height": [0], "text_length": [4096]}
        base.update(changes)
        return [base[g.gate] for g in self.gates]

    def reasons(self, **changes) -> dict:
        return dsql.gate_reasons(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules, self.gates, self.rows(**changes),
                                 200000)

    def test_gate_list(self):
        found = [(g.gate, g.entity, g.columns) for g in self.gates]
        self.assertEqual(found, [("encoding", "", ()), ("budget", "part", ()), ("budget", "bom_line", ()),
                                 ("budget", "code_value", ()), ("key", "part", ("part_id",)),
                                 ("non_finite", "bom_line", ("qty_per",)), ("null_quantity", "bom_line", ()),
                                 ("height", "bom_line", ()), ("text_length", "part", ("name",))])
        self.assertEqual({g.statement.params.get("limit") for g in self.gates if g.gate == "budget"}, {200001})

    def test_gates_follow_the_selected_rules(self):
        rules = [APPENDIX.rule("qty_over_limit"), APPENDIX.rule("repeated_line")]
        gates = dsql.snapshot_gates(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rules, 9)
        self.assertEqual([(g.gate, g.entity) for g in gates], [("encoding", ""), ("budget", "bom_line"),
                                                               ("non_finite", "bom_line")])
        atoms = dsql.snapshot_gates(CFG, ATOMS_SPEC, TEXT_PLAN, ATOMS_SPEC.rules, 9)
        self.assertIn(("key", "bom_line", ("line_id",)), [(g.gate, g.entity, g.columns) for g in atoms])
        self.assertIn(("key", "code_value", ("code_group", "code")), [(g.gate, g.entity, g.columns) for g in atoms])

    def test_all_gates_pass(self):
        self.assertEqual(self.reasons(), {})

    def test_global_gates(self):
        everyone = {r.id for r in APPENDIX.rules}
        self.assertEqual(self.reasons(encoding=["SQL_ASCII"]), dict.fromkeys(everyone, "server_encoding"))
        self.assertEqual(self.reasons(budget=[66667]), dict.fromkeys(everyone, "resource_limit"))   # 3 x 66667
        self.assertEqual(self.reasons(budget=[66666]), {})                                          # 199998
        self.assertEqual(self.reasons(encoding=["LATIN1"], budget=[99999]), dict.fromkeys(everyone,
                                                                                          "server_encoding"))

    def test_rule_gates(self):
        self.assertEqual(self.reasons(key=[1, 0]), dict.fromkeys(NODE_KEY_RULES, "key_null"))
        self.assertEqual(self.reasons(key=[0, 2]), dict.fromkeys(NODE_KEY_RULES, "key_not_unique"))
        self.assertEqual(self.reasons(non_finite=[3]), dict.fromkeys(QTY_RULES, "non_finite_values"))
        both = self.reasons(key=[1, 1], non_finite=[3])
        self.assertEqual(both, {**dict.fromkeys(QTY_RULES, "non_finite_values"),
                                **dict.fromkeys(NODE_KEY_RULES, "key_null")})
        self.assertEqual(self.reasons(null_quantity=[1]), {"twin_assemblies": "null_values"})
        self.assertEqual(self.reasons(height=[2]), {"stored_level_drift": "cycle_or_depth_limit"})
        self.assertEqual(self.reasons(text_length=[4097]), {"grams_in_count_unit": "value_too_long"})
        self.assertEqual(self.reasons(text_length=[None]), {})

    def test_rows_must_align_with_gates(self):
        with self.assertRaises(ValueError):
            dsql.gate_reasons(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules, self.gates, self.rows()[:-1], 9)

    def test_gate_statements(self):
        texts = {g.statement.label: g.statement.text for g in self.gates}
        self.assertEqual(texts["gate encoding"], "SHOW server_encoding")
        self.assertEqual(texts["gate key part"], 'SELECT count(*) FILTER (WHERE t."part_id" IS NULL) AS null_keys, '
                                                 '(SELECT count(*) FROM (SELECT 1 FROM "sxc_rules"."part" k WHERE '
                                                 'k."part_id" IS NOT NULL GROUP BY k."part_id" HAVING count(*) > 1) d) '
                                                 'AS duplicate_keys FROM "sxc_rules"."part" t')
        self.assertEqual(texts["gate non_finite bom_line"],
                         "SELECT count(*) FILTER (WHERE t.\"qty_per\" IN (CAST('NaN' AS numeric), CAST('Infinity' AS "
                         "numeric), CAST('-Infinity' AS numeric))) AS \"qty_per\" FROM \"sxc_rules\".\"bom_line\" t")
        self.assertEqual(texts["gate null_quantity bom_line"], 'SELECT count(*) FROM "sxc_rules"."bom_line" t WHERE '
                                                               't."parent_id" IS NOT NULL AND t."child_id" IS NOT NULL '
                                                               'AND t."qty_per" IS NULL')
        self.assertEqual(texts["gate text_length part"], 'SELECT max(length(t."name")) AS "name" FROM '
                                                         '"sxc_rules"."part" t')
        composite = dsql.key_gate_statement(CFG, TEXT_PLAN, "code_value")
        self.assertIn('count(*) FILTER (WHERE t."code_group" IS NULL OR t."code" IS NULL)', composite.text)
        self.assertIn('GROUP BY k."code_group", k."code" HAVING count(*) > 1', composite.text)
        for text in texts.values():
            self.assertNotIn("::", text)
        with self.assertRaises(dsql.CompileError):
            dsql.non_finite_statement(CFG, TEXT_PLAN, "part", ["stored_level"])      # integers hold no NaN

    def test_export_columns(self):
        self.assertEqual(dsql.export_columns(APPENDIX_CFG, APPENDIX, TEXT_PLAN, APPENDIX.rules),
                         {"part": ("part_id", "issue", "retired", "procure", "name", "unit", "part_class",
                                   "stored_level"),
                          "bom_line": ("line_id", "parent_id", "child_id", "qty_per"),
                          "code_value": ("code_group", "code")})
        only = dsql.export_columns(APPENDIX_CFG, APPENDIX, TEXT_PLAN, [APPENDIX.rule("temporary_name")])
        self.assertEqual(only, {"part": ("part_id", "name")})
        plan = make_plan({("bom_line", "line_id"): "uuid"})
        self.assertEqual(dsql.export_columns(APPENDIX_CFG, APPENDIX, plan, [APPENDIX.rule("qty_over_limit")]),
                         {"bom_line": ("parent_id", "child_id", "qty_per")})


# ---- views and lookups ------------------------------------------------------------------------------
class ViewTest(unittest.TestCase):
    def test_view_statement_form(self):
        for rule in APPENDIX.rules:
            with self.subTest(rule=rule.id):
                view = dsql.view_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule)
                inner = dsql.rule_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule).literal()
                create = f'CREATE OR REPLACE VIEW "sxc_out"."v_rule_{rule.id}" AS\n'
                self.assertTrue(view.startswith(create + "SELECT r.*, row_number() OVER (ORDER BY "))
                self.assertTrue(view.endswith(f") AS ord FROM ({inner}) r;"))
                self.assertEqual(view.count("\n"), 1 + inner.count("\n"))
                self.assertEqual(dsql.view_query(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule), view[len(create):-1])

    def test_an_output_named_ord_is_refused(self):
        spec = data_rules.loads(toml_doc({"version": 1, "rule": [
            {"id": "ordered", "kind": "row", "entity": "part", "severity": "warning",
             "where": [{"column": "ord", "op": "is_null"}], "report": {"columns": ["ord"]}}]}), CFG)
        plan = make_plan({("part", "ord"): "integer"})
        dsql.rule_statement(CFG, spec, plan, spec.rules[0])                         # compiles: only the view clashes
        with self.assertRaises(RuleSpecError) as caught:
            dsql.view_statement(CFG, spec, plan, spec.rules[0])
        self.assertTrue(str(caught.exception).startswith("data-rules.toml rule[0] ordered: an output column named ord"))

    def test_subjects_view(self):
        view = dsql.subjects_view_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN)
        self.assertTrue(view.startswith('CREATE OR REPLACE VIEW "sxc_out"."v_rules_subjects" AS\nWITH u AS (SELECT '
                                        'v."parent_id" AS subject, CAST(E\'qty_over_limit\' AS text) AS rule_id, '
                                        "CAST(E'0' AS bigint) AS rule_ord, CAST(E'0' AS bigint) AS severity_rank FROM "
                                        '"sxc_out"."v_rule_qty_over_limit" v WHERE v."parent_id" IS NOT NULL '
                                        'UNION ALL '))
        self.assertTrue(view.endswith("row_number() OVER (ORDER BY g.severity_rank, g.subject COLLATE \"C\") AS ord "
                                      "FROM g;"))
        for rule in APPENDIX.rules:
            with self.subTest(rule=rule.id):
                count = view.count(f'"v_rule_{rule.id}" v')
                self.assertEqual(count, 1 if rule.subject else 0)
                if rule.subject:
                    rank = data_rules.SEVERITY_RANK[rule.severity]
                    self.assertIn(f"CAST(E'{rule.id}' AS text) AS rule_id, CAST(E'{rule.index}' AS bigint) AS "
                                  f"rule_ord, CAST(E'{rank}' AS bigint) AS severity_rank", view)
        self.assertIn("array_agg(d.rule_id ORDER BY d.rule_ord) AS rules", view)
        self.assertIn("SELECT DISTINCT u.subject, u.rule_id, u.rule_ord, u.severity_rank FROM u", view)
        no_subjects = data_rules.loads(toml_doc({"version": 1, "rule": [
            {"id": "plain", "kind": "row", "entity": "part", "severity": "warning",
             "where": [{"column": "name", "op": "is_null"}]}]}), CFG)
        with self.assertRaises(dsql.CompileError):
            dsql.subjects_view_query(CFG, no_subjects, TEXT_PLAN)

    def test_marker_comment_drop_and_orphans(self):
        view = data_rules.view_name(APPENDIX_CFG, "qty_over_limit")
        self.assertEqual(dsql.comment_statement(view), 'COMMENT ON VIEW "sxc_out"."v_rule_qty_over_limit" IS '
                                                       "'semantic-crosscheck data rule view: rows are cross-checked "
                                                       "only by sxc.py data-rules'")
        self.assertEqual(dsql.drop_view_statement(data_rules.subjects_view_name(APPENDIX_CFG)),
                         'DROP VIEW IF EXISTS "sxc_out"."v_rules_subjects"')
        orphans = dsql.orphan_statement(APPENDIX_CFG)
        self.assertEqual(orphans.params, {"p1": "sxc_out", "p2": dsql.VIEW_MARKER, "p3": "v_rule_",
                                          "p4": "v_rules_subjects"})
        self.assertIn("c.relkind = 'v'", orphans.text)
        self.assertNotIn("LIKE", orphans.text)
        self.assertNotIn("CASCADE", dsql.drop_view_statement(view))

    def test_lookups(self):
        rule = APPENDIX.rule("stored_level_drift")
        st = dsql.lookup_statement(APPENDIX_CFG, rule, 11)
        self.assertEqual(st.text, 'SELECT v."part_id", v."kind", v."stored", v."height", v.ord FROM '
                                  '"sxc_out"."v_rule_stored_level_drift" v ORDER BY v.ord LIMIT CAST(:limit AS bigint)')
        self.assertEqual(st.params, {"limit": 11})
        subjects = dsql.subjects_lookup_statement(APPENDIX_CFG, 12)
        self.assertEqual(subjects.text, 'SELECT v.subject, v.severity, v.severity_rank, v.rule_count, v.rules, v.ord '
                                        'FROM "sxc_out"."v_rules_subjects" v ORDER BY v.ord LIMIT CAST(:limit AS '
                                        'bigint)')


# ---- PostgreSQL 17 grammar --------------------------------------------------------------------------
def every_statement() -> list:
    """(name, text) of every statement this module generates for the test schemas, in both renderings."""
    out = []
    for cfg, spec, plan, examples in ((APPENDIX_CFG, APPENDIX, TEXT_PLAN, True), (CFG, ATOMS_SPEC, TEXT_PLAN, True),
                                      (APPENDIX_CFG, APPENDIX, BPCHAR_PLAN, True), (CFG, ATOMS_SPEC, BPCHAR_PLAN, True),
                                      (CFG, ATOMS_NO_EXAMPLES, NUMERIC_PLAN, False)):
        for name, st in statements(cfg, spec, plan, examples):
            out += [(name, wire(st.text)), (name + " literal", st.literal())]
        for rule in spec.rules:
            out.append((rule.id + " view", dsql.view_statement(cfg, spec, plan, rule)))
            out.append((rule.id + " lookup", wire(dsql.lookup_statement(cfg, rule, 3).text)))
        out.append(("subjects view", dsql.subjects_view_statement(cfg, spec, plan)))
        for gate in dsql.snapshot_gates(cfg, spec, plan, spec.rules, 100):
            out.append((gate.statement.label, wire(gate.statement.text)))
        for entity, columns in dsql.export_columns(cfg, spec, plan, spec.rules).items():
            out.append((f"export {entity}", wire(dsql.export_statement(cfg, plan, entity, columns).text)))
        out.append(("type plan", wire(dsql.type_plan_statement(cfg, list(cfg.entities)).text)))
        out.append(("orphans", wire(dsql.orphan_statement(cfg).text)))
        out.append(("subjects lookup", wire(dsql.subjects_lookup_statement(cfg, 3).text)))
        out.append(("comment", dsql.comment_statement(data_rules.view_name(cfg, spec.rules[0].id))))
        out.append(("drop", dsql.drop_view_statement(data_rules.subjects_view_name(cfg))))
    return out


@unittest.skipUnless(HAS_PGLAST, "optional PostgreSQL AST parser not installed (requirements/sql-structure.txt)")
class PostgresGrammarTest(unittest.TestCase):
    def test_every_statement_parses(self):
        import pglast
        found = every_statement()
        for name, text in found:
            with self.subTest(statement=name):
                self.assertEqual(len(pglast.parse_sql(text)), 1)
        self.assertGreater(len(found), 400)

    def test_literals_parse_to_their_exact_values(self):
        import pglast
        for text in TRICKY:
            with self.subTest(text=text):
                tree = pglast.parse_sql(f"SELECT {dsql.e_string(text)}, 1")
                self.assertEqual(tree[0].stmt.targetList[0].val.val.sval, text)
        rule = APPENDIX.rule("grams_in_count_unit")
        view = dsql.view_statement(APPENDIX_CFG, APPENDIX, TEXT_PLAN, rule)
        values = []

        def visit(node):
            if isinstance(node, pglast.ast.String):
                values.append(node.sval)
            elif isinstance(node, pglast.ast.Node):
                for attr in node:
                    visit(getattr(node, attr))
            elif isinstance(node, tuple):
                for item in node:
                    visit(item)
        visit(pglast.parse_sql(view)[0].stmt)
        self.assertIn(rule.pattern.are, values)
        self.assertIn(dsql.NUM_CAPTURE, values)


# ---- module boundary --------------------------------------------------------------------------------
class ImportBoundaryTest(unittest.TestCase):
    def test_imports_only_the_standard_library_data_rules_and_ident(self):
        source = Path(dsql.__file__).read_text(encoding="utf-8")
        local = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names, alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    local |= {node.module} if node.module else {a.name for a in node.names}
                else:
                    self.assertIn(node.module.split(".")[0], sys.stdlib_module_names, node.module)
        self.assertLessEqual(local, {"data_rules", "ident"})        # never data_rules_eval, a stage, db or result


if __name__ == "__main__":
    if "--write-goldens" in sys.argv:
        write_goldens()
        print(f"wrote {len(render_goldens())} goldens under {GOLDEN.relative_to(ROOT).as_posix()}")
    else:
        unittest.main()
