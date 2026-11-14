"""data_rules_eval: the independent Python evaluation of data rules (path 2), without a database.

Table-driven and synthetic: the clean-room rule file of examples/data-rules (the spec's Appendix A), an authored suite
of further rules with examples (SUITE below) and invented rows. Covered:
- the 15 clean-room examples, against their authored output and against a hand-written copy of it;
- three-valued logic: NULL columns, NULL and missing endpoints, and ne, not_in, starts_with and not_in_ref on NULL;
- values: numbers by value across scales, exact text (trailing space, case, NFC against NFD), bpchar without pads;
- every kind at its boundaries, every gate, the Inexact trap and malformed input (evaluator errors, never answers);
- a fixture with NULL endpoints and missing references, with the hand-written output of all 15 clean-room rules and
  the hand-written subjects view;
- mutation tests: each planted evaluator defect makes at least one authored example disagree with its authored rows;
- the import boundary: the standard library and canon only.
"""

import ast
import datetime
import operator
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from collections import Counter
from decimal import Decimal, Inexact, InvalidOperation, getcontext, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pgfixture
from semantic_crosscheck import config, data_rules
from semantic_crosscheck import data_rules_eval as ev

ROOT = pgfixture.ROOT
EXAMPLE = ROOT / "examples" / "data-rules"
D = Decimal
CFG_PATH = Path(tempfile.gettempdir()) / "sxc-data-rules-eval" / "crosscheck.toml"
CONFIG = """version = 1
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
max_depth = MAX_DEPTH
[data_rules]
file = "data-rules.toml"
"""
SCHEMA = {   # the clean-room example's schema (examples/data-rules/crosscheck.toml)
    "part": {"part_id": "text", "name": "text", "unit": "text", "part_class": "text", "procure": "text",
             "issue": "text", "retired": "boolean", "stored_level": "integer"},
    "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text", "qty_per": "numeric(18,6)"},
    "code_value": {"code_group": "text", "code": "text"},
}


def make_cfg(max_depth: int = 16):
    return config.loads(CONFIG.replace("MAX_DEPTH", str(max_depth)).encode("utf-8"), CFG_PATH)


CFG = make_cfg()


def type_plan(**overrides) -> data_rules.TypePlan:
    """The schema's type plan; overrides map entity__column to a declared type."""
    columns = {(e, c): data_rules.declared_type(t) for e, cols in SCHEMA.items() for c, t in cols.items()}
    for name, declared in overrides.items():
        entity, column = name.split("__")
        columns[(entity, column)] = data_rules.declared_type(declared)
    return data_rules.TypePlan(columns)


PLAN = type_plan()
NUMBER_KEYS = type_plan(part__part_id="integer", bom_line__parent_id="integer", bom_line__child_id="integer")


def load(text: str, cfg=CFG, plan=PLAN) -> data_rules.RuleFile:
    """A rule file from TOML text, validated and type-checked: every rule must be runnable under plan."""
    spec = data_rules.loads(text.encode("utf-8"), cfg)
    not_run = data_rules.apply_types(spec, plan)
    if not_run:
        raise AssertionError(f"rules not runnable under the plan: {not_run}")
    return spec


def clean_room() -> data_rules.RuleFile:
    return config.load_data_rules(config.load(EXAMPLE / "crosscheck.toml"))


def part(part_id, **cells) -> dict:
    """A part row with every column of the schema, NULL unless given."""
    row = dict.fromkeys(SCHEMA["part"])
    row.update(part_id=part_id, **cells)
    return row


def line(line_id, parent_id, child_id, qty_per=None) -> dict:
    """A bom_line row; a quantity written as a string becomes a Decimal, as the export delivers numeric values."""
    return {"line_id": line_id, "parent_id": parent_id, "child_id": child_id,
            "qty_per": D(qty_per) if isinstance(qty_per, str) else qty_per}


def tables(parts=(), lines=(), codes=()) -> dict:
    return {"part": list(parts), "bom_line": list(lines),
            "code_value": [{"code_group": g, "code": c} for g, c in codes]}


def lines_of(*triples) -> list:
    """bom_line rows numbered from 1: (parent, child, quantity) or (parent, child)."""
    return [line(i, *t) for i, t in enumerate(triples, start=1)]


def run(spec, rule_id: str, data: dict, plan=PLAN):
    return ev.evaluate(spec.rule(rule_id), data, spec.hierarchy, spec.sets, spec.refs, plan)


def outcome(spec, rule_id: str, *rows) -> ev.Outcome:
    return ev.Outcome(spec.rule(rule_id).columns, Counter(rows))


def example_agrees(spec, rule, example, plan) -> bool:
    """Whether the evaluation of an authored example equals its authored rows; an exception never agrees."""
    try:
        got = ev.evaluate(rule, ev.example_tables(rule, example, plan), spec.hierarchy, spec.sets, spec.refs, plan)
        return got == ev.expected(rule, example, plan)
    except Exception:   # noqa: BLE001 (any evaluator error is a disagreement)
        return False


# ---- the clean-room examples ------------------------------------------------------------------------
APPENDIX = {   # hand-written copies of the authored outputs of examples/data-rules/data-rules.toml, canonical
    "qty_over_limit": [("A", "C", "500.000001", "2")],
    "parent_unknown": [("X",)],
    "kit_without_bom": [("K1",)],
    "retired_child_in_active_parent": [("A", "R", "1")],
    "all_children_retired": [("A",)],
    "retired_assembly_in_use": [("R",)],
    "unknown_procure_code": [("B",), ("C",), ("D",)],
    "temporary_name": [("A",)],
    "truncated_third": [("A", "P", "0.3333", "1")],
    "repeated_line": [("A", "B", "2")],
    "same_name_components": [("A",)],
    "odd_quantity": [("D", "S", "40", "2", "1")],
    "twin_assemblies": [("A1", "A1"), ("A1", "A2")],
    "stored_level_drift": [("L", "leaf_stored_nonzero", "1", "0"), ("M", "has_children_stored_zero", "0", "1"),
                           ("O", "outside_stored_nonzero", "3", "0")],
    "grams_in_count_unit": [("A", "W1", "12.5", "12.5", "1", "1"), ("A", "W2", "7.5", "30", "4", "1")],
}


class CleanRoomExamplesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = clean_room()

    def test_the_example_schema_runs_every_rule(self):
        self.assertEqual(data_rules.apply_types(self.spec, PLAN), {})

    def test_every_example_gives_its_authored_output(self):
        self.assertEqual([r.id for r in self.spec.rules], list(APPENDIX))
        for rule in self.spec.rules:
            (example,) = rule.examples
            with self.subTest(rule=rule.id):
                data = ev.example_tables(rule, example, PLAN)
                got = ev.evaluate(rule, data, self.spec.hierarchy, self.spec.sets, self.spec.refs, PLAN)
                authored = ev.expected(rule, example, PLAN)
                self.assertEqual(got, authored)
                self.assertEqual(got, ev.Outcome(rule.columns, Counter(APPENDIX[rule.id])))
                self.assertEqual(ev.example_verdict(got, got, authored), "")

    def test_an_evaluator_serves_several_rules(self):
        rules = [r for r in self.spec.rules if r.kind == "row" and r.entity == "part"]
        example = rules[0].examples[0]
        data = ev.example_tables(rules[0], example, PLAN)
        evaluator = ev.Evaluator(data, self.spec.hierarchy, self.spec.sets, self.spec.refs, PLAN)
        self.assertEqual(evaluator.evaluate(rules[0]), ev.expected(rules[0], example, PLAN))
        self.assertEqual(evaluator.evaluate(rules[0]), evaluator.evaluate(rules[0]))

    def test_empty_tables_give_empty_outputs_not_a_gate(self):
        for rule in self.spec.rules:
            with self.subTest(rule=rule.id):
                got = ev.evaluate(rule, tables(), self.spec.hierarchy, self.spec.sets, self.spec.refs, PLAN)
                self.assertEqual(got, ev.Outcome(rule.columns, Counter()))


# ---- three-valued logic -------------------------------------------------------------------------------
THREE_VALUED_DATA = tables(
    parts=[part("A", issue="STD", retired=False, procure="BUY", name="Pump"),
           part("B"),                                                         # every attribute NULL
           part("C", issue="KIT", retired=True, procure="XX", name="Kit")],
    lines=[line(1, "A", "C"), line(2, None, "C"), line(3, "X", "B"), line(4, "B", None), line(5, "C", "Y")],
    codes=[("PROCURE", "BUY")],
)   # X and Y have no part row; the edges are lines 1, 3 and 5
THREE_VALUED = [   # (rule id, entity, atom, keys of the selected rows)
    ("issue_eq", "part", '{ column = "issue", op = "eq", value = "STD" }', ["A"]),
    ("issue_ne", "part", '{ column = "issue", op = "ne", value = "STD" }', ["C"]),
    ("issue_in", "part", '{ column = "issue", op = "in", set = "std_kit" }', ["A", "C"]),
    ("issue_not_in", "part", '{ column = "issue", op = "not_in", set = "std" }', ["C"]),
    ("name_starts_with", "part", '{ column = "name", op = "starts_with", set = "initials" }', ["A", "C"]),
    ("procure_not_in_ref", "part", '{ column = "procure", op = "not_in_ref", ref = "procure_codes" }', ["C"]),
    ("issue_is_null", "part", '{ column = "issue", op = "is_null" }', ["B"]),
    ("issue_is_not_null", "part", '{ column = "issue", op = "is_not_null" }', ["A", "C"]),
    ("retired_eq_false", "part", '{ column = "retired", op = "eq", value = false }', ["A"]),
    ("retired_ne_true", "part", '{ column = "retired", op = "ne", value = true }', ["A"]),
    ("has_children", "part", '{ has_children = true }', ["A", "C"]),
    ("no_children", "part", '{ has_children = false }', ["B"]),
    ("has_parents", "part", '{ has_parents = true }', ["B", "C"]),
    ("outside", "part", '{ in_hierarchy = false }', []),
    ("children_all_retired", "part",
     '{ children = "all", where = [{ column = "retired", op = "eq", value = true }] }', ["A"]),
    ("children_any_issue", "part", '{ children = "any", where = [{ column = "issue", op = "is_not_null" }] }',
     ["A"]),
    ("parents_any_std", "part", '{ parents = "any", where = [{ column = "issue", op = "eq", value = "STD" }] }',
     ["C"]),
    ("parents_none_std", "part", '{ parents = "none", where = [{ column = "issue", op = "eq", value = "STD" }] }',
     ["A", "B"]),
    ("std_or_null", "part",
     '{ any = [[{ column = "issue", op = "eq", value = "STD" }], [{ column = "issue", op = "is_null" }]] }',
     ["A", "B"]),
    ("parent_exists", "bom_line", '{ end = "parent", exists = true }', [1, 4, 5]),
    ("parent_missing", "bom_line", '{ end = "parent", exists = false }', [3]),
    ("child_missing", "bom_line", '{ end = "child", exists = false }', [5]),
    ("child_exists", "bom_line", '{ end = "child", exists = true }', [1, 2, 3]),
    ("parent_issue_eq", "bom_line", '{ end = "parent", column = "issue", op = "eq", value = "STD" }', [1]),
    ("parent_issue_ne", "bom_line", '{ end = "parent", column = "issue", op = "ne", value = "KIT" }', [1]),
    ("parent_issue_not_in", "bom_line", '{ end = "parent", column = "issue", op = "not_in", set = "std" }', [5]),
    ("parent_issue_is_null", "bom_line", '{ end = "parent", column = "issue", op = "is_null" }', [4]),
    ("child_retired", "bom_line", '{ end = "child", column = "retired", op = "eq", value = true }', [1, 2]),
    ("child_name_starts_with", "bom_line",
     '{ end = "child", column = "name", op = "starts_with", set = "initials" }', [1, 2]),
    ("child_procure_not_in_ref", "bom_line",
     '{ end = "child", column = "procure", op = "not_in_ref", ref = "procure_codes" }', [1, 2]),
    ("parent_has_children", "bom_line", '{ end = "parent", has_children = true }', [1, 3, 5]),
    ("parent_without_children", "bom_line", '{ end = "parent", has_children = false }', [4]),
    ("child_has_parents", "bom_line", '{ end = "child", has_parents = true }', [1, 2, 3, 5]),
    ("child_outside", "bom_line", '{ end = "child", in_hierarchy = false }', []),
    ("line_parent_is_null", "bom_line", '{ column = "parent_id", op = "is_null" }', [2]),
    ("line_parent_ne", "bom_line", '{ column = "parent_id", op = "ne", value = "A" }', [3, 4, 5]),
]
THREE_VALUED_FILE = (
    'version = 1\n[sets]\nstd_kit = ["STD", "KIT"]\nstd = ["STD"]\ninitials = ["P", "K"]\n'
    '[refs.procure_codes]\nentity = "code_value"\ncolumn = "code"\nwhere = { code_group = "PROCURE" }\n'
    + "".join(f'[[rule]]\nid = "{rid}"\nkind = "row"\nentity = "{entity}"\nseverity = "warning"\nwhere = [{atom}]\n'
              for rid, entity, atom, _keys in THREE_VALUED))


class ThreeValuedLogicTest(unittest.TestCase):
    def test_null_columns_and_null_or_missing_endpoints(self):
        spec = load(THREE_VALUED_FILE)
        for rid, entity, _atom, keys in THREE_VALUED:
            with self.subTest(rule=rid):
                key_class = "number" if entity == "bom_line" else "text"
                expected = Counter((ev.cell(k, key_class),) for k in keys)
                self.assertEqual(run(spec, rid, THREE_VALUED_DATA).rows, expected)


# ---- values -------------------------------------------------------------------------------------------
NFC_E = unicodedata.lookup("LATIN SMALL LETTER E WITH ACUTE")              # one code point
NFD_E = "e" + unicodedata.lookup("COMBINING ACUTE ACCENT")                 # the same letter, decomposed
VALUE_RULES = """version = 1
[sets]
amounts = ["0.3333", "2"]
names = ["Bolt", "e\\u0301"]
prefix = ["Bo"]

[[rule]]
id = "qty_is_two"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "eq", value = "2" }]

[[rule]]
id = "qty_in_amounts"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "in", set = "amounts" }]

[[rule]]
id = "qty_below_two"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "lt", value = 2 }]

[[rule]]
id = "same_quantity"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = ["qty_per"]

[[rule]]
id = "quantities"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "is_not_null" }]
report = { columns = ["qty_per"] }

[[rule]]
id = "named_bolt"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "name", op = "eq", value = "Bolt" }]

[[rule]]
id = "named_in_set"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "name", op = "in", set = "names" }]

[[rule]]
id = "named_bo"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "name", op = "starts_with", set = "prefix" }]

[[rule]]
id = "same_name"
kind = "duplicate"
entity = "part"
severity = "warning"
key = ["name"]
"""
BPCHAR_RULE = """version = 1
[[rule]]
id = "kit_issue"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "issue", op = "eq", value = "KIT" }]
  [[rule.example]]
  spec = "values: bpchar values are read without their trailing pad spaces"
  rows.part = [
    { part_id = "P1", issue = "KIT " }, { part_id = "P2", issue = "KIT" }, { part_id = "P3", issue = " KIT" },
  ]
  expect = [{ part_id = "P1 " }, { part_id = "P2" }]
"""


class ValuesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load(VALUE_RULES)

    def test_numbers_compare_by_value_across_scales(self):
        data = tables(lines=[line(1, "A", "B", "2.000"), line(2, "A", "C", 2), line(3, "A", "D", "2.5"),
                             line(4, "A", "E", "0.333300"), line(5, "A", "F", "-0.0")])
        self.assertEqual(run(self.spec, "qty_is_two", data), outcome(self.spec, "qty_is_two", ("1",), ("2",)))
        self.assertEqual(run(self.spec, "qty_in_amounts", data),
                         outcome(self.spec, "qty_in_amounts", ("1",), ("2",), ("4",)))
        self.assertEqual(run(self.spec, "qty_below_two", data),
                         outcome(self.spec, "qty_below_two", ("4",), ("5",)))
        self.assertEqual(run(self.spec, "same_quantity", data), outcome(self.spec, "same_quantity", ("2", "2")))
        self.assertEqual(run(self.spec, "quantities", data),
                         outcome(self.spec, "quantities", ("2", "2"), ("2.5", "1"), ("0.3333", "1"), ("0", "1")))

    def test_text_compares_exactly(self):
        self.assertNotEqual(NFC_E, NFD_E)
        self.assertEqual(unicodedata.normalize("NFC", NFD_E), NFC_E)
        names = ["Bolt", "Bolt ", "bolt", "BOLT", " Bolt", NFC_E, NFD_E, "Bolt"]
        data = tables(parts=[part(f"P{i}", name=n) for i, n in enumerate(names)])
        self.assertEqual(run(self.spec, "named_bolt", data), outcome(self.spec, "named_bolt", ("P0",), ("P7",)))
        self.assertEqual(run(self.spec, "named_in_set", data),
                         outcome(self.spec, "named_in_set", ("P0",), ("P6",), ("P7",)))
        self.assertEqual(run(self.spec, "named_bo", data),
                         outcome(self.spec, "named_bo", ("P0",), ("P1",), ("P7",)))
        self.assertEqual(run(self.spec, "same_name", data), outcome(self.spec, "same_name", ("Bolt", "2")))

    def test_bpchar_values_arrive_without_pad_spaces(self):
        bpchar = type_plan(part__part_id="character(4)", part__issue="character(4)")
        spec = load(BPCHAR_RULE, plan=bpchar)
        rule = spec.rule("kit_issue")
        (example,) = rule.examples
        data = ev.example_tables(rule, example, bpchar)
        self.assertEqual(data, {"part": [{"issue": "KIT", "part_id": "P1"}, {"issue": "KIT", "part_id": "P2"},
                                         {"issue": " KIT", "part_id": "P3"}]})
        got = ev.evaluate(rule, data, spec.hierarchy, spec.sets, spec.refs, bpchar)
        self.assertEqual(got, ev.expected(rule, example, bpchar))
        self.assertEqual(got, outcome(spec, "kit_issue", ("P1",), ("P2",)))
        # the same example on text columns: the trailing space is part of the value
        text = load(BPCHAR_RULE)
        rule = text.rule("kit_issue")
        got = ev.evaluate(rule, ev.example_tables(rule, example, PLAN), text.hierarchy, text.sets, text.refs, PLAN)
        self.assertEqual(got, outcome(text, "kit_issue", ("P2",)))
        self.assertNotEqual(got, ev.expected(rule, example, PLAN))
        # an export that kept the pad spaces of a bpchar column is refused, not compared
        padded = tables(parts=[part("P1", issue="KIT ")])
        with self.assertRaises(TypeError):
            ev.evaluate(spec.rule("kit_issue"), padded, spec.hierarchy, spec.sets, spec.refs, bpchar)

    def test_booleans_compare_as_booleans(self):
        spec = load(THREE_VALUED_FILE)
        data = tables(parts=[part("A", retired=False), part("B", retired=True), part("C")])
        self.assertEqual(run(spec, "retired_eq_false", data), outcome(spec, "retired_eq_false", ("A",)))
        self.assertEqual(run(spec, "retired_ne_true", data), outcome(spec, "retired_ne_true", ("A",)))


# ---- mode_outlier ---------------------------------------------------------------------------------------
MODE_RULES = """version = 1
[[rule]]
id = "far_from_usual"
kind = "mode_outlier"
entity = "bom_line"
severity = "warning"
group_by = "child_id"
value = "qty_per"
min_mode_share = "0.75"
ratio = "20"
report = { columns = ["parent_id", "child_id", "qty_per"] }

[[rule]]
id = "far_lines"
kind = "mode_outlier"
entity = "bom_line"
severity = "warning"
group_by = "child_id"
value = "qty_per"
min_mode_share = "0.75"
ratio = "20"

[[rule]]
id = "far_parents"
kind = "mode_outlier"
entity = "bom_line"
severity = "warning"
group_by = "child_id"
value = "qty_per"
min_distinct = { column = "parent_id", n = 2 }
min_mode_share = "0.75"
ratio = "20"
report = { distinct = "parent_id" }

[[rule]]
id = "wide_ratio"
kind = "mode_outlier"
entity = "bom_line"
severity = "warning"
group_by = "child_id"
value = "qty_per"
min_mode_share = "1"
ratio = "1.00000000000000000000000000000000000001"
"""


def repeated(child: str, values, parent: str = "P") -> list:
    """(parent, child, quantity) triples, one per value."""
    return [(parent, child, v) for v in values]


class ModeOutlierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load(MODE_RULES)

    def far(self, *triples, rule="far_from_usual"):
        return run(self.spec, rule, tables(lines=lines_of(*triples)))

    def test_share_at_the_threshold_is_eligible(self):
        got = self.far(("A", "S", "2"), ("B", "S", "2.000"), ("C", "S", "2"), ("D", "S", "40"))
        self.assertEqual(got, outcome(self.spec, "far_from_usual", ("D", "S", "40", "2", "1")))
        exact = self.far(*repeated("T", ["2"] * 75 + ["40"] * 25))          # 75 of 100
        self.assertEqual(exact, outcome(self.spec, "far_from_usual", ("P", "T", "40", "2", "25")))
        below = self.far(*repeated("U", ["2"] * 74 + ["40"] * 26))          # 74 of 100
        self.assertEqual(below.rows, Counter())

    def test_null_values_count_in_the_group_size(self):
        self.assertEqual(self.far(*repeated("S", ["2", "2", "2", None, "40"])).rows, Counter())
        self.assertEqual(self.far(*repeated("S", ["2", "2", "2", "40"])).outputs, 1)

    def test_a_mode_of_zero_or_below_gives_no_outlier(self):
        self.assertEqual(self.far(*repeated("Z", ["0", "0", "0", "5"])).rows, Counter())
        self.assertEqual(self.far(*repeated("M", ["-1", "-1", "-1", "50"])).rows, Counter())

    def test_ratio_boundaries_are_inclusive(self):
        values = ["2"] * 12 + ["40", "39.999999", "0.1", "0.100001"]      # 12 of 16: eligible, mode 2
        got = self.far(*repeated("R", values))
        self.assertEqual(got, outcome(self.spec, "far_from_usual", ("P", "R", "40", "2", "1"),
                                      ("P", "R", "0.1", "2", "1")))

    def test_key_and_distinct_reports(self):
        triples = [("A", "S", "2"), ("B", "S", "2"), ("C", "S", "2"), ("D", "S", "40"),
                   ("E", "T", "1"), ("E", "T", "1"), ("E", "T", "1"), ("E", "T", "30")]
        self.assertEqual(self.far(*triples, rule="far_lines"), outcome(self.spec, "far_lines", ("4",), ("8",)))
        # far_parents needs two distinct parents: group T has one
        self.assertEqual(self.far(*triples, rule="far_parents"), outcome(self.spec, "far_parents", ("D",)))

    def test_an_inexact_product_raises(self):
        with self.assertRaises(Inexact):
            self.far(*repeated("S", ["1.23456789012345678901234567890123"] * 2), rule="wide_ratio")


# ---- same_signature -------------------------------------------------------------------------------------
SIGNATURE_RULES = """version = 1
[[rule]]
id = "twins"
kind = "same_signature"
severity = "warning"
attrs = ["name", "unit"]

[[rule]]
id = "triplets"
kind = "same_signature"
severity = "warning"
attrs = ["name"]
min_members = 3
"""


class SameSignatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load(SIGNATURE_RULES)

    def twins(self, parts, triples, rule="twins", plan=PLAN, spec=None):
        spec = spec or self.spec
        return run(spec, rule, tables(parts=parts, lines=lines_of(*triples)), plan)

    def test_split_lines_are_summed_and_line_order_is_irrelevant(self):
        parts = [part(p, name="Frame", unit="pc") for p in ("A1", "A2", "A3")]
        got = self.twins(parts, [("A1", "S", "1"), ("A1", "S", "1"), ("A1", "T", "3"),
                                 ("A2", "T", "3.000"), ("A2", "S", "2.0"),
                                 ("A3", "S", "2"), ("A3", "T", "3.5")])
        self.assertEqual(got, outcome(self.spec, "twins", ("A1", "A1"), ("A1", "A2")))

    def test_null_attributes_group_together(self):
        parts = [part("B1", unit="pc"), part("B2", unit="pc"), part("B3")]
        got = self.twins(parts, [("B1", "S", "1"), ("B2", "S", "1"), ("B3", "S", "1")])
        self.assertEqual(got, outcome(self.spec, "twins", ("B1", "B1"), ("B1", "B2")))

    def test_min_members(self):
        parts = [part(p, name="Frame") for p in ("A", "B", "C")]
        two = [("A", "S", "1"), ("B", "S", "1")]
        self.assertEqual(self.twins(parts, two, rule="triplets").rows, Counter())
        got = self.twins(parts, two + [("C", "S", "1")], rule="triplets")
        self.assertEqual(got, outcome(self.spec, "triplets", ("A", "A"), ("A", "B"), ("A", "C")))

    def test_group_id_is_the_smallest_member(self):
        parts = [part(p, name="Frame") for p in ("b", NFC_E, "B")]
        got = self.twins(parts, [(p, "S", "1") for p in ("b", NFC_E, "B")])
        self.assertEqual(got, outcome(self.spec, "twins", ("B", "B"), ("B", "b"), ("B", NFC_E)))
        numbers = load(SIGNATURE_RULES, plan=NUMBER_KEYS)
        parts = [part(p, name="Frame") for p in (10, 9, 100)]
        got = self.twins(parts, [(p, 1, "1") for p in (10, 9, 100)], plan=NUMBER_KEYS, spec=numbers)
        self.assertEqual(got, outcome(numbers, "twins", ("9", "9"), ("9", "10"), ("9", "100")))

    def test_members_need_a_part_row_and_dangling_children_count(self):
        parts = [part(p, name="Frame") for p in ("A", "B")]
        got = self.twins(parts, [("A", "LOST", "1"), ("B", "LOST", "1"), ("GHOST", "LOST", "1"),
                                 ("A", "S", "1"), ("B", "S", "1"), ("GHOST", "S", "1")])
        self.assertEqual(got, outcome(self.spec, "twins", ("A", "A"), ("A", "B")))
        other = self.twins(parts, [("A", "LOST", "1"), ("B", "GONE", "1")])
        self.assertEqual(other.rows, Counter())

    def test_a_null_edge_quantity_is_not_run(self):
        parts = [part("A"), part("B")]
        self.assertEqual(self.twins(parts, [("A", "S", "1"), ("B", "S", None)]), ev.NotRun("null_values"))
        lines_without_edges = [("A", "S", "1"), ("B", "S", "1"), (None, "S", None), ("A", None, None)]
        self.assertEqual(self.twins(parts, lines_without_edges).outputs, 2)

    def test_an_inexact_sum_raises(self):
        parts = [part("A"), part("B")]
        with self.assertRaises(Inexact):
            self.twins(parts, [("A", "S", "1" + "0" * 40), ("A", "S", "0." + "0" * 29 + "1"), ("B", "S", "1")])


# ---- duplicate ------------------------------------------------------------------------------------------
DUPLICATE_RULES = """version = 1
[[rule]]
id = "repeated"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = ["parent_id", "child_id"]

[[rule]]
id = "same_child_name"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = ["parent_id", { end = "child", column = "name" }]
count = { distinct = "child_id" }

[[rule]]
id = "thrice"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = ["parent_id"]
min = 3
report = { distinct = "parent_id" }
"""


class DuplicateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load(DUPLICATE_RULES)

    def test_a_null_key_part_forms_no_group(self):
        data = tables(lines=lines_of(("A", "B"), ("A", "B"), (None, "C"), (None, "C"), ("A", None), ("A", None)))
        self.assertEqual(run(self.spec, "repeated", data), outcome(self.spec, "repeated", ("A", "B", "2")))

    def test_an_endpoint_part_needs_an_existing_row_with_a_value(self):
        parts = [part("B", name="Bolt"), part("C", name="Bolt"), part("N")]
        data = tables(parts=parts, lines=lines_of(("A", "B"), ("A", "C"), ("D", "B"), ("D", "B"), ("E", "B"),
                                                  ("E", "MISSING"), ("F", "N"), ("F", "N"), ("G", "N"), ("G", "B")))
        self.assertEqual(run(self.spec, "same_child_name", data),
                         outcome(self.spec, "same_child_name", ("A", "Bolt", "2")))

    def test_min_and_the_distinct_report(self):
        data = tables(lines=lines_of(("A", "B"), ("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"), (None, "C"),
                                     (None, "D"), (None, "E")))
        self.assertEqual(run(self.spec, "thrice", data), outcome(self.spec, "thrice", ("A",)))


# ---- stored_metric --------------------------------------------------------------------------------------
HEIGHT_RULE = """version = 1
[[rule]]
id = "levels"
kind = "stored_metric"
severity = "warning"
metric = "height"
stored = "stored_level"
"""


class StoredMetricTest(unittest.TestCase):
    def levels(self, parts, pairs, max_depth=16):
        spec = load(HEIGHT_RULE, cfg=make_cfg(max_depth))
        return spec, run(spec, "levels", tables(parts=parts, lines=lines_of(*pairs)))

    def test_heights_of_a_diamond(self):
        parts = [part(p, stored_level=9) for p in ("A", "B", "C", "D", "E", "O")]
        spec, got = self.levels(parts, [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"), ("D", "E")])
        self.assertEqual(got, outcome(spec, "levels", ("A", "stored_above", "9", "3"), ("B", "stored_above", "9", "2"),
                                      ("C", "stored_above", "9", "2"), ("D", "stored_above", "9", "1"),
                                      ("E", "leaf_stored_nonzero", "9", "0"),
                                      ("O", "outside_stored_nonzero", "9", "0")))

    def test_a_chain_up_to_max_depth_runs_and_a_deeper_one_does_not(self):
        chain = [("N3", "N2"), ("N2", "N1"), ("N1", "N0")]
        parts = [part(p, stored_level=int(p[1])) for p in ("N0", "N1", "N2", "N3")]
        spec, got = self.levels(parts, chain, max_depth=3)
        self.assertEqual(got, outcome(spec, "levels"))
        _spec, deeper = self.levels(parts, [("N4", "N3")] + chain, max_depth=3)
        self.assertEqual(deeper, ev.NotRun("cycle_or_depth_limit"))
        # a chain of parts without rows counts too: the gate covers every edge
        _spec, ghosts = self.levels([part("Q", stored_level=0)], [("G1", "G2"), ("G2", "G3"), ("G3", "G4"),
                                                                  ("G4", "G5")], max_depth=3)
        self.assertEqual(ghosts, ev.NotRun("cycle_or_depth_limit"))

    def test_cycles_are_not_run(self):
        parts = [part(p, stored_level=0) for p in ("A", "B", "L")]
        for pairs in ([("A", "B"), ("B", "A"), ("A", "L")],      # a cycle that reaches a leaf
                      [("A", "B"), ("B", "A")],                  # a cycle without a leaf
                      [("A", "A"), ("A", "L")],                  # a self-loop
                      [("A", "L"), ("X", "Y"), ("Y", "X")]):     # a cycle of parts without rows
            with self.subTest(pairs=pairs):
                self.assertEqual(self.levels(parts, pairs)[1], ev.NotRun("cycle_or_depth_limit"))

    def test_kinds_null_stored_and_equal_values(self):
        parts = [part("Z0", stored_level=0), part("BELOW", stored_level=1), part("NEG", stored_level=-1),
                 part("ABOVE", stored_level=5), part("LEAF", stored_level=2), part("OUT", stored_level=1),
                 part("NULL"), part("SAME", stored_level=D("2.0")), part("M", stored_level=1),
                 part("T", stored_level=1)]
        pairs = [("Z0", "M"), ("BELOW", "M"), ("NEG", "M"), ("ABOVE", "M"), ("M", "LEAF"), ("SAME", "M"),
                 ("NULL", "M"), ("T", "X")]                      # M has height 1; X has no row and is a leaf
        spec, got = self.levels(parts, pairs)
        self.assertEqual(got, outcome(spec, "levels", ("Z0", "has_children_stored_zero", "0", "2"),
                                      ("BELOW", "stored_below", "1", "2"), ("NEG", "stored_below", "-1", "2"),
                                      ("ABOVE", "stored_above", "5", "2"), ("LEAF", "leaf_stored_nonzero", "2", "0"),
                                      ("OUT", "outside_stored_nonzero", "1", "0")))


# ---- text_quantity --------------------------------------------------------------------------------------
TEXT_RULES = r"""version = 1
[[rule]]
id = "grams"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "child"
text = "name"
pattern = '\[([0-9.]+) g(?: / ([0-9]+))?\]'
quantity = "qty_per"
rel_tol = "0.001"
max_text_length = 100
report = { columns = ["parent_id", "child_id", "qty_per"] }

[[rule]]
id = "parent_grams"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "parent"
text = "name"
pattern = '([0-9]+) g'
quantity = "qty_per"
rel_tol = "0"
"""
OVERLAP = "([0-9]+)(?:[0-9]g)?"   # inside the subset; on 123g PostgreSQL captures 12, Python 123
OVERLAP_RULES = f"""version = 1
[[rule]]
id = "overlap"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "child"
text = "name"
pattern = '{OVERLAP}'
quantity = "qty_per"
rel_tol = "0"
report = {{ columns = ["line_id"] }}

[[rule]]
id = "anchored"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "child"
text = "name"
pattern = '^{OVERLAP}$'
quantity = "qty_per"
rel_tol = "0"
report = {{ columns = ["line_id"] }}
"""


class TextQuantityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load(TEXT_RULES)

    def grams(self, names: dict, triples, rule="grams"):
        parts = [part(p, name=n) for p, n in names.items()]
        return run(self.spec, rule, tables(parts=parts, lines=lines_of(*triples)))

    def test_captures(self):
        names = {"W1": "Washer [12.5 g]", "W2": "Shim [30 g / 4]", "W3": "Plate [1.2.3 g]", "W4": "Dot [. g]",
                 "W5": "Pin [30 g / 0]", "W6": "Air [0 g]", "W7": "Foil [0.000 g]", "W8": "Two [5 g] [7 g]"}
        triples = [("A", "W1", "12.5"), ("A", "W2", "7.5"), ("A", "W3", "1.2"), ("A", "W4", "1"), ("A", "W5", "30"),
                   ("A", "W6", "0"), ("A", "W7", "0"), ("A", "W8", "5"), ("B", "W8", "7")]
        self.assertEqual(self.grams(names, triples),
                         outcome(self.spec, "grams", ("A", "W1", "12.5", "12.5", "1", "1"),
                                 ("A", "W2", "7.5", "30", "4", "1"), ("A", "W8", "5", "5", "1", "1")))

    def test_the_tolerance_is_inclusive(self):
        triples = [("A", "P", "999"), ("A", "P", "1001"), ("A", "P", "998.999999"), ("A", "P", "1001.000001")]
        self.assertEqual(self.grams({"P": "Plate [1000 g]"}, triples),
                         outcome(self.spec, "grams", ("A", "P", "999", "1000", "1", "1"),
                                 ("A", "P", "1001", "1000", "1", "1")))

    def test_null_and_missing_inputs_are_no_match(self):
        names = {"P": "Plate [10 g]", "Q": None}
        triples = [("A", "P", None), ("A", "Q", "10"), ("A", None, "10"), ("A", "MISSING", "10"), ("A", "P", "10")]
        self.assertEqual(self.grams(names, triples), outcome(self.spec, "grams", ("A", "P", "10", "10", "1", "1")))

    def test_one_capture_and_the_parent_end(self):
        names = {"BOX": "Box 25 g", "BAG": "Bag 25 g"}
        triples = [("BOX", "C", "25"), ("BAG", "C", "25.000001"), (None, "C", "25")]
        self.assertEqual(self.grams(names, triples, rule="parent_grams"), outcome(self.spec, "parent_grams", ("1",)))

    def test_long_text_is_not_run(self):
        at_limit = {"P": "Plate [10 g]", "LONG": "x" * 100}
        self.assertEqual(self.grams(at_limit, [("A", "P", "10")]).outputs, 1)
        over = {"P": "Plate [10 g]", "LONG": "x" * 101}                  # LONG is on no line: still a gate
        self.assertEqual(self.grams(over, [("A", "P", "10")]), ev.NotRun("value_too_long"))

    def test_an_inexact_product_raises(self):
        names = {"P": "Big [1 g / 123456789012345678901234567890123456789]"}
        with self.assertRaises(Inexact):
            self.grams(names, [("A", "P", "1234567890123456789012.345678")])

    def test_python_takes_the_first_match_its_backtracking_finds(self):
        """The subset accepts OVERLAP, but the engines choose different matches on 123g from the same leftmost
        position: PostgreSQL the longest one, 123g with capture 12 (test_data_rules_pg.RegexEnginesTest), Python's re
        the first one its backtracking finds, 123 with capture 123. The evaluator reads X = 123, so it flags the line
        with quantity 123 and not the one with quantity 12. Anchored, the whole text is the match and X = 12."""
        pattern = data_rules.compile_subset(OVERLAP)
        self.assertEqual((pattern.captures, pattern.quantifiers), (1, 2))
        spec = load(OVERLAP_RULES)
        data = tables(parts=[part("G", name="123g")], lines=lines_of(("A", "G", "123"), ("A", "G", "12")))
        self.assertEqual(run(spec, "overlap", data), outcome(spec, "overlap", ("1", "123", "1", "1")))
        self.assertEqual(run(spec, "anchored", data), outcome(spec, "anchored", ("2", "12", "1", "1")))


# ---- gates ----------------------------------------------------------------------------------------------
REPORT_KEY_RULE = """version = 1
[[rule]]
id = "big_lines"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "gt", value = "500" }]
"""


class GatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = clean_room()

    def outcomes(self, data):
        return {r.id: ev.evaluate(r, data, self.spec.hierarchy, self.spec.sets, self.spec.refs, PLAN)
                for r in self.spec.rules}

    def test_node_key_gates_stop_only_the_rules_that_read_node_rows(self):
        reads_part = {r.id for r in self.spec.rules if "part" in r.reads}
        self.assertEqual(sorted(set(APPENDIX) - reads_part), ["odd_quantity", "qty_over_limit", "repeated_line"])
        for parts, reason in (([part("A"), part(None)], "key_null"),
                              ([part("A"), part("A")], "key_not_unique"),
                              ([part("A"), part("A"), part(None)], "key_null")):
            with self.subTest(reason=reason):
                got = self.outcomes(tables(parts=parts, lines=lines_of(("A", "B", "600"))))
                self.assertEqual({i for i, o in got.items() if o == ev.NotRun(reason)}, reads_part)
                self.assertEqual(got["qty_over_limit"].outputs, 1)

    def test_report_key_gates(self):
        spec = load(REPORT_KEY_RULE)
        for lines, reason in (([line(1, "A", "B", "600"), line(None, "A", "C", "1")], "key_null"),
                              ([line(1, "A", "B", "600"), line(1, "A", "C", "1")], "key_not_unique")):
            with self.subTest(reason=reason):
                self.assertEqual(run(spec, "big_lines", tables(lines=lines)), ev.NotRun(reason))
        self.assertEqual(run(spec, "big_lines", tables(lines=[line(1, "A", "B", "600")])),
                         outcome(spec, "big_lines", ("1",)))

    def test_non_finite_numbers_stop_the_rules_that_read_them(self):
        reads_qty = {r.id for r in self.spec.rules if "qty_per" in r.reads.get("bom_line", ())}
        self.assertEqual(sorted(reads_qty), ["grams_in_count_unit", "odd_quantity", "qty_over_limit",
                                             "truncated_third", "twin_assemblies"])
        for value in ("NaN", "Infinity", "-Infinity", "sNaN"):
            with self.subTest(value=value):
                data = tables(parts=[part("A"), part("B")], lines=lines_of(("A", "B", value), ("A", "B", "1")))
                got = self.outcomes(data)
                self.assertEqual({i for i, o in got.items() if o == ev.NotRun("non_finite_values")}, reads_qty)
                self.assertEqual(got["repeated_line"].outputs, 1)

    def test_gate_order(self):
        data = tables(parts=[part(None)], lines=lines_of(("A", "B", "NaN")))
        got = self.outcomes(data)
        self.assertEqual(got["twin_assemblies"], ev.NotRun("non_finite_values"))
        self.assertEqual(got["kit_without_bom"], ev.NotRun("key_null"))
        self.assertEqual(set(ev.GATE_REASONS), {"non_finite_values", "key_null", "key_not_unique", "null_values",
                                                "value_too_long", "cycle_or_depth_limit"})


SELF_CONFIG = b"""version = 1
[source]
schemas = ["sxc_rules"]
output_schema = "sxc_out"
[entities.emp]
table = "sxc_rules.emp"
key = ["emp_id"]
[hierarchies.reports_to]
edge = "emp"
node = "emp"
parent = "manager_id"
child = "emp_id"
quantity = "fte"
max_depth = 8
[data_rules]
file = "data-rules.toml"
"""
SELF_RULES = """version = 1
[[rule]]
id = "orphan"
kind = "row"
entity = "emp"
severity = "violation"
where = [{ end = "parent", exists = false }]

[[rule]]
id = "leaf_managed"
kind = "row"
entity = "emp"
severity = "warning"
where = [{ has_children = false }, { has_parents = true }]

[[rule]]
id = "depth"
kind = "stored_metric"
severity = "warning"
metric = "height"
stored = "level"
"""
COMPOSITE_KEY_RULE = """version = 1
[[rule]]
id = "blank_codes"
kind = "row"
entity = "code_value"
severity = "warning"
where = [{ column = "code", op = "eq", value = "" }]
"""


class ShapesOfConfigurationTest(unittest.TestCase):
    def test_a_hierarchy_whose_edge_entity_is_its_node_entity(self):
        cfg = config.loads(SELF_CONFIG, CFG_PATH)
        declared = {"emp_id": "text", "manager_id": "text", "fte": "numeric", "level": "integer"}
        plan = data_rules.TypePlan({("emp", c): data_rules.declared_type(t) for c, t in declared.items()})
        spec = load(SELF_RULES, cfg=cfg, plan=plan)
        people = (("E1", None, 2), ("E2", "E1", 1), ("E3", "E2", 0), ("E4", "GHOST", 0), ("E5", "E1", 1))
        data = {"emp": [{"emp_id": e, "manager_id": m, "fte": D(1), "level": lv} for e, m, lv in people]}
        self.assertEqual(run(spec, "orphan", data, plan), outcome(spec, "orphan", ("E4",)))
        self.assertEqual(run(spec, "leaf_managed", data, plan),
                         outcome(spec, "leaf_managed", ("E3",), ("E4",), ("E5",)))
        self.assertEqual(run(spec, "depth", data, plan),
                         outcome(spec, "depth", ("E5", "leaf_stored_nonzero", "1", "0")))

    def test_a_composite_report_key(self):
        spec = load(COMPOSITE_KEY_RULE)
        codes = [("PROCURE", ""), ("OTHER", ""), ("OTHER", "XX")]
        self.assertEqual(run(spec, "blank_codes", tables(codes=codes)),
                         outcome(spec, "blank_codes", ("PROCURE", ""), ("OTHER", "")))
        self.assertEqual(run(spec, "blank_codes", tables(codes=codes + [(None, "")])), ev.NotRun("key_null"))
        self.assertEqual(run(spec, "blank_codes", tables(codes=codes + [("OTHER", "")])), ev.NotRun("key_not_unique"))


# ---- malformed input and the arithmetic context ---------------------------------------------------------
class InputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = clean_room()

    def evaluate(self, rule_id, data, plan=None):
        return ev.evaluate(self.spec.rule(rule_id), data, self.spec.hierarchy, self.spec.sets, self.spec.refs, plan)

    def test_unsupported_and_mixed_values_raise(self):
        for value in (0.5, datetime.date(2026, 1, 1), b"1", [1]):
            with self.subTest(value=type(value).__name__), self.assertRaises(TypeError):
                self.evaluate("qty_over_limit", tables(lines=[line(1, "A", "B", value)]))
        mixed = tables(lines=[line(1, "A", "B", "1"), line(2, "A", "C")])
        mixed["bom_line"][1]["qty_per"] = "1"                # a str among Decimals
        with self.assertRaises(TypeError):
            self.evaluate("qty_over_limit", mixed)

    def test_missing_entities_and_columns_raise(self):
        with self.assertRaises(KeyError):
            self.evaluate("qty_over_limit", {"part": []})
        with self.assertRaises(KeyError):
            self.evaluate("qty_over_limit", {"bom_line": [{"parent_id": "A", "child_id": "B"}]})

    def test_values_of_the_wrong_type_raise(self):
        text_qty = tables(lines=[line(1, "A", "B", D("600"))])
        text_qty["bom_line"][0]["qty_per"] = "600"
        with self.assertRaises(TypeError):                   # an order comparison on text
            self.evaluate("qty_over_limit", text_qty)
        truncated = tables(parts=[part("P", unit="pc")], lines=[line(1, "A", "P", D("0.3333"))])
        truncated["bom_line"][0]["qty_per"] = "0.3333"
        self.assertEqual(self.evaluate("truncated_third", truncated).outputs, 1)   # text compared with text
        with self.assertRaises(TypeError):                   # the type plan catches the export's mistake
            self.evaluate("truncated_third", truncated, PLAN)
        numbers = tables(parts=[part("A", retired=1)], lines=[])
        with self.assertRaises(TypeError):                   # a boolean constant against numbers
            self.evaluate("all_children_retired", numbers)

    def test_endpoint_and_ref_types_must_agree(self):
        endpoints = tables(parts=[part(1)], lines=[line(1, "1", "2")])
        with self.assertRaises(TypeError):
            self.evaluate("parent_unknown", endpoints)
        codes = tables(parts=[part("A", procure="BUY")], lines=lines_of(("A", "B")))
        codes["code_value"] = [{"code_group": "PROCURE", "code": 7}]
        with self.assertRaises(TypeError):
            self.evaluate("unknown_procure_code", codes)

    def test_the_callers_decimal_context_does_not_matter_and_is_kept(self):
        data = tables(parts=[part("W", name="Washer [123456789012.5 g]", unit="pc")],
                      lines=[line(1, "A", "W", "123456789012.5")])
        with localcontext() as ctx:
            ctx.prec = 5
            ctx.traps[Inexact] = False
            got = self.evaluate("grams_in_count_unit", data)
            self.assertEqual((getcontext().prec, getcontext().traps[Inexact]), (5, False))
        self.assertEqual(got.rows, Counter({("A", "W", "123456789012.5", "123456789012.5", "1", "1"): 1}))

    def test_unknown_forms_raise(self):
        with self.assertRaises(ValueError):
            ev.Evaluator(tables(), None, {}, {}).evaluate(SimpleNamespace(id="x", kind="other", reads={}, report=None))
        with self.assertRaises(ValueError):
            ev._quantified("most", (), {}, None)
        with self.assertRaises(ValueError):
            ev.cell("x", "date")


# ---- authored-example helpers and canonical forms -------------------------------------------------------
class ExampleHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = clean_room()

    def test_example_tables_hold_the_columns_read_as_the_export_types_them(self):
        rule = self.spec.rule("stored_level_drift")
        data = ev.example_tables(rule, rule.examples[0], PLAN)
        self.assertEqual(sorted(data), ["bom_line", "part"])
        self.assertEqual(data["part"][4], {"part_id": "U", "stored_level": None})
        self.assertEqual(data["bom_line"][0], {"parent_id": "T", "child_id": "M"})
        rule = self.spec.rule("qty_over_limit")
        rows = ev.example_tables(rule, rule.examples[0], PLAN)["bom_line"]
        self.assertEqual([type(r["qty_per"]) for r in rows], [Decimal, Decimal, Decimal, type(None)])
        self.assertEqual(rows[1]["qty_per"], D("500.000001"))

    def test_expected_rows_and_output_classes(self):
        drift = self.spec.rule("stored_level_drift")
        self.assertEqual(ev.output_classes(drift, PLAN), ("text", "text", "number", "number"))
        self.assertEqual(ev.output_classes(self.spec.rule("grams_in_count_unit"), PLAN),
                         ("text", "text", "number", "number", "number", "number"))
        self.assertEqual(ev.expected(drift, drift.examples[0], PLAN).rows,
                         Counter(APPENDIX["stored_level_drift"]))
        with self.assertRaises(ValueError):
            ev.output_classes(drift, type_plan(part__stored_level="date"))

    def test_example_verdicts(self):
        a = ev.Outcome(("k",), Counter({("1",): 1}))
        b = ev.Outcome(("k",), Counter({("2",): 1}))
        c = ev.Outcome(("k",), Counter({("3",): 1}))
        self.assertEqual(ev.example_verdict(a, a, a), "")
        self.assertEqual(ev.example_verdict(a, a, b), "common_mode")
        self.assertEqual(ev.example_verdict(b, a, a), "sql_mismatch")
        self.assertEqual(ev.example_verdict(a, b, a), "py_mismatch")
        self.assertEqual(ev.example_verdict(a, b, c), "both_mismatch")
        self.assertEqual(set(ev.EXAMPLE_VERDICTS), {"common_mode", "sql_mismatch", "py_mismatch", "both_mismatch"})
        doubled = ev.Outcome(("k",), Counter({("1",): 2}))
        self.assertEqual(ev.example_verdict(doubled, a, a), "sql_mismatch")      # duplicates count
        with self.assertRaises(TypeError):
            ev.example_verdict(ev.NotRun("key_null"), a, a)

    def test_cells_agree_with_the_rule_file_model(self):
        cases = {"number": [0, 7, -12, "2.500", "-0.0", "500.000001", D("1E+3"), D("2.000")],
                 "text": ["", "Bolt ", NFD_E, "x"], "bpchar": ["AB  ", "AB", "   "], "boolean": [True, False]}
        for cls, values in cases.items():
            for value in values + [None]:
                with self.subTest(cls=cls, value=value):
                    self.assertEqual(ev.cell(value, cls), data_rules.cell(value, cls))
        for value, cls in ((True, "number"), ("1e3", "number"), (0.5, "number"), (D("NaN"), "number"),
                           (1, "text"), ("true", "boolean"), (1, "boolean")):
            with self.subTest(refused=(value, cls)), self.assertRaises(TypeError):
                ev.cell(value, cls)

    def test_typed_order_agrees_with_the_rule_file_model(self):
        rows = [("B", "10", True), ("a", "9", False), (None, None, None), ("B", "9.5", None), (NFC_E, "-1", True),
                ("B", "10", False)]
        classes = ("text", "number", "boolean")
        self.assertEqual(sorted(rows, key=lambda r: ev.sort_key(r, classes)),
                         sorted(rows, key=lambda r: data_rules.sort_key(r, classes)))
        got = ev.ordered(ev.Outcome(("a", "b", "c"), Counter({rows[0]: 2, rows[1]: 1, rows[2]: 1})), classes)
        self.assertEqual(got, [rows[2], rows[0], rows[0], rows[1]])

    def test_constants_agree_with_the_rule_file_model(self):
        self.assertEqual(ev.HEIGHT_KINDS, data_rules.HEIGHT_KINDS)
        self.assertEqual({s: i for i, s in enumerate(ev.SEVERITIES)}, data_rules.SEVERITY_RANK)


# ---- the subjects view ----------------------------------------------------------------------------------
class SubjectsTest(unittest.TestCase):
    RULES = [SimpleNamespace(id="r0", subject="code", severity="warning"),
             SimpleNamespace(id="r1", subject="", severity="violation"),
             SimpleNamespace(id="r2", subject="parent", severity="violation"),
             SimpleNamespace(id="r3", subject="code", severity="warning")]

    def test_roll_up_order_and_null_subjects(self):
        outcomes = {
            "r3": ev.Outcome(("code",), Counter({("10",): 1, ("9",): 1})),
            "r2": ev.Outcome(("parent", "n"), Counter({("100", "2"): 1, ("9", "1"): 1, ("9", "3"): 1,
                                                      (None, "1"): 1})),
            "r0": ev.Outcome(("code",), Counter({("10",): 1})),
        }
        number = ev.subjects(self.RULES, outcomes, "number")
        self.assertEqual([(s.subject, s.severity, s.severity_rank, s.rule_count, s.rules) for s in number],
                         [("9", "violation", 0, 2, ("r2", "r3")), ("100", "violation", 0, 1, ("r2",)),
                          ("10", "warning", 1, 2, ("r0", "r3"))])
        text = ev.subjects(self.RULES, outcomes, "text")
        self.assertEqual([s.subject for s in text], ["100", "9", "10"])

    def test_every_subject_rule_needs_its_agreed_output(self):
        outcomes = {"r0": ev.Outcome(("code",), Counter()), "r2": ev.Outcome(("parent", "n"), Counter())}
        with self.assertRaises(ValueError):
            ev.subjects(self.RULES, outcomes, "text")
        outcomes["r3"] = ev.NotRun("key_null")
        with self.assertRaises(ValueError):
            ev.subjects(self.RULES, outcomes, "text")
        outcomes["r3"] = ev.Outcome(("code",), Counter())
        self.assertEqual(ev.subjects(self.RULES, outcomes, "text"), ())


# ---- a fixture with NULL endpoints and missing references -----------------------------------------------
def fixture() -> dict:
    """A small BOM: lines with a NULL parent, a NULL child and both NULL; lines to a missing part (LOST) and from a
    missing part (GHOST); procurement codes outside the ref's code group (XX, MAKE) or blank; NULL attributes (N)."""
    parts = [
        part("A", name="Frame", unit="pc", part_class="ASSY", procure="BUY", issue="STD", retired=False,
             stored_level=3),
        part("B", name="Frame", unit="pc", part_class="ASSY", procure="BUY", issue="STD", retired=False,
             stored_level=2),
        part("C", name="Frame", unit="pc", part_class="ASSY", procure="BUY", issue="STD", retired=False,
             stored_level=0),
        part("D", name="Dolly", unit="pc", part_class="ASSY", procure="BUY", issue="STD", retired=False,
             stored_level=3),
        part("K", name="Kit [5 g]", unit="pc", part_class="PART", procure="XX", issue="KIT", retired=False,
             stored_level=0),
        part("J", name="Jig", unit="pc", part_class="PART", procure="MAKE", issue="KIT", retired=False,
             stored_level=0),
        part("R", name="TMP-Bolt", unit="box", part_class="PART", procure=None, issue="STD", retired=True,
             stored_level=1),
        part("N", procure=""),
        part("L", name="Washer [2 g / 2]", unit="pc", part_class="PART", procure="BUY", issue="STD", retired=True,
             stored_level=1),
        part("S", name="Stand", unit="pc", part_class="ASSY", procure="BUY", issue="STD", retired=True,
             stored_level=2),
        part("Z", name="tmp-spare", unit="kg", part_class="PART", procure="XX", issue="KIT", retired=False,
             stored_level=5),
    ]
    lines = lines_of(
        ("A", "B", "1"), ("A", "K", "0.666600"), ("A", "R", "600"), ("A", "LOST", "1"), ("GHOST", "J", "2"),
        ("GHOST", "J", "2"), (None, "Z", "600"), ("B", "L", "1"), ("B", None, "3"), (None, None, None),
        ("B", "L", "1.000"), ("A", "N", "0.6666"), ("C", "R", "1"), ("C", "L", "2"), ("D", "R", "1"),
        ("D", "LOST", "1"), ("S", "K", "1"), ("GHOST", "S", "1"), ("L", "N", "1"), ("D", "B", "1"), ("D", "A", "1"),
        ("B", "R", "1"))
    return tables(parts, lines, [("PROCURE", "BUY"), ("OTHER", "XX"), ("OTHER", "MAKE")])


FIXTURE_OUTPUTS = {   # written by hand from the semantic contract
    "qty_over_limit": [("A", "R", "600", "1"), (None, "Z", "600", "1")],
    "parent_unknown": [("GHOST",)],
    "kit_without_bom": [("J",), ("K",)],
    "retired_child_in_active_parent": [("A", "R", "1"), ("B", "L", "2"), ("C", "R", "1"), ("C", "L", "1"),
                                       ("D", "R", "1"), ("B", "R", "1")],
    "all_children_retired": [("B",), ("C",)],
    "retired_assembly_in_use": [("L",)],
    "unknown_procure_code": [("J",), ("K",), ("N",), ("R",)],
    "temporary_name": [("R",)],
    "truncated_third": [("A", "K", "0.6666", "1")],
    "repeated_line": [("GHOST", "J", "2"), ("B", "L", "2")],
    "same_name_components": [("D",)],
    "odd_quantity": [("A", "R", "600", "1", "1")],
    "twin_assemblies": [("B", "B"), ("B", "C")],
    "stored_level_drift": [("C", "has_children_stored_zero", "0", "2"), ("D", "stored_below", "3", "4"),
                           ("R", "leaf_stored_nonzero", "1", "0"), ("S", "stored_above", "2", "1"),
                           ("Z", "outside_stored_nonzero", "5", "0")],
    "grams_in_count_unit": [("B", "L", "1", "2", "2", "2")],
}
FIXTURE_SUBJECTS = [   # (subject, severity, rank, rule count, rules), in the order of the subjects view
    ("A", "violation", 0, 4, ("qty_over_limit", "retired_child_in_active_parent", "truncated_third", "odd_quantity")),
    ("B", "violation", 0, 5, ("retired_child_in_active_parent", "all_children_retired", "repeated_line",
                              "twin_assemblies", "grams_in_count_unit")),
    ("C", "violation", 0, 3, ("retired_child_in_active_parent", "all_children_retired", "twin_assemblies")),
    ("D", "violation", 0, 2, ("retired_child_in_active_parent", "same_name_components")),
    ("GHOST", "violation", 0, 2, ("parent_unknown", "repeated_line")),
    ("J", "violation", 0, 2, ("kit_without_bom", "unknown_procure_code")),
    ("K", "violation", 0, 2, ("kit_without_bom", "unknown_procure_code")),
    ("L", "violation", 0, 1, ("retired_assembly_in_use",)),
    ("N", "warning", 1, 1, ("unknown_procure_code",)),
    ("R", "warning", 1, 2, ("unknown_procure_code", "temporary_name")),
]


class NullEndpointFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = clean_room()
        cls.data = fixture()

    def test_every_rule_gives_its_hand_written_output(self):
        for rule in self.spec.rules:
            with self.subTest(rule=rule.id):
                got = ev.evaluate(rule, self.data, self.spec.hierarchy, self.spec.sets, self.spec.refs, PLAN)
                self.assertEqual(got, ev.Outcome(rule.columns, Counter(FIXTURE_OUTPUTS[rule.id])))

    def test_one_evaluator_for_every_rule_gives_the_same_outputs_and_subjects(self):
        evaluator = ev.Evaluator(self.data, self.spec.hierarchy, self.spec.sets, self.spec.refs, PLAN)
        outcomes = {rule.id: evaluator.evaluate(rule) for rule in self.spec.rules}
        for rule in self.spec.rules:
            self.assertEqual(outcomes[rule.id].rows, Counter(FIXTURE_OUTPUTS[rule.id]), rule.id)
        got = ev.subjects(self.spec.rules, outcomes, "text")
        self.assertEqual([(s.subject, s.severity, s.severity_rank, s.rule_count, s.rules) for s in got],
                         FIXTURE_SUBJECTS)


# ---- mutation tests -------------------------------------------------------------------------------------
SUITE = r"""version = 1
hierarchy = "has_component"

[sets]
count_units = ["pc", "box"]

[refs.procure_codes]
entity = "code_value"
column = "code"
where = { code_group = "PROCURE" }

[[rule]]
id = "issue_not_std"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "issue", op = "ne", value = "STD" }]
  [[rule.example]]
  spec = "suite 1: ne is false on NULL"
  rows.part = [
    { part_id = "P1", issue = "STD" }, { part_id = "P2", issue = "KIT" }, { part_id = "P3", issue = { null = true } },
  ]
  expect = [{ part_id = "P2" }]

[[rule]]
id = "unit_not_counted"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "unit", op = "not_in", set = "count_units" }]
  [[rule.example]]
  spec = "suite 2: not_in is false on NULL"
  rows.part = [
    { part_id = "P1", unit = "pc" }, { part_id = "P2", unit = "kg" }, { part_id = "P3", unit = { null = true } },
  ]
  expect = [{ part_id = "P2" }]

[[rule]]
id = "procure_not_listed"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "procure", op = "not_in_ref", ref = "procure_codes" }]
  [[rule.example]]
  spec = "suite 3: not_in_ref is false on NULL and reads only the rows of the ref's code group"
  rows.part = [
    { part_id = "P1", procure = "BUY" }, { part_id = "P2", procure = "XX" },
    { part_id = "P3", procure = { null = true } }, { part_id = "P4", procure = "MAKE" },
  ]
  rows.code_value = [
    { code_group = "PROCURE", code = "BUY" }, { code_group = "OTHER", code = "XX" },
    { code_group = { null = true }, code = "MAKE" },
  ]
  expect = [{ part_id = "P2" }, { part_id = "P4" }]

[[rule]]
id = "lines_of_unknown_parents"
kind = "row"
entity = "bom_line"
severity = "violation"
where = [{ end = "parent", exists = false }]
report = { columns = ["parent_id", "child_id"] }
  [[rule.example]]
  spec = "suite 4: a NULL parent is no unknown parent"
  rows.part = [{ part_id = "A" }]
  rows.bom_line = [
    { parent_id = "A", child_id = "B" },
    { parent_id = "X", child_id = "B" },
    { parent_id = { null = true }, child_id = "B" },
  ]
  expect = [{ parent_id = "X", child_id = "B", n = 1 }]

[[rule]]
id = "lines_to_leaves"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ end = "child", has_children = false }]
report = { columns = ["parent_id", "child_id"] }
  [[rule.example]]
  spec = "suite 5: endpoint structure is false for a NULL endpoint, and a NULL end makes no edge"
  rows.bom_line = [
    { parent_id = "A", child_id = "B" },
    { parent_id = "B", child_id = "C" },
    { parent_id = "C", child_id = { null = true } },
    { parent_id = "D", child_id = { null = true } },
  ]
  expect = [{ parent_id = "B", child_id = "C", n = 1 }]

[[rule]]
id = "assembly_without_retired_parts"
kind = "row"
entity = "part"
severity = "warning"
where = [{ has_children = true }, { children = "none", where = [{ column = "retired", op = "eq", value = true }] }]
  [[rule.example]]
  spec = "suite 6: none holds when no existing child satisfies the sub-where"
  rows.part = [
    { part_id = "A", retired = false }, { part_id = "B", retired = false }, { part_id = "C", retired = false },
    { part_id = "D", retired = false }, { part_id = "R", retired = true }, { part_id = "N", retired = { null = true } },
  ]
  rows.bom_line = [
    { parent_id = "A", child_id = "R" },
    { parent_id = "A", child_id = "N" },
    { parent_id = "B", child_id = "N" },
    { parent_id = "C", child_id = "MISSING" },
  ]
  expect = [{ part_id = "B" }, { part_id = "C" }]

[[rule]]
id = "big_quantity_parents"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "gt", value = "500" }]
report = { distinct = "parent_id" }
  [[rule.example]]
  spec = "suite 7: quantities compare by value, not as text, and distinct drops NULL"
  rows.bom_line = [
    { parent_id = "A", qty_per = "90" },
    { parent_id = "B", qty_per = "1000" },
    { parent_id = { null = true }, qty_per = "600" },
    { parent_id = "C", qty_per = "500.0" },
  ]
  expect = [{ parent_id = "B" }]

[[rule]]
id = "lopsided_quantity"
kind = "mode_outlier"
entity = "bom_line"
severity = "warning"
group_by = "child_id"
value = "qty_per"
min_distinct = { column = "parent_id", n = 3 }
min_mode_share = "0.75"
ratio = "10"
report = { columns = ["parent_id", "child_id", "qty_per"] }
  [[rule.example]]
  spec = "suite 8: NULL values count in the group size, few distinct parents or a mode of 0 give no mode"
  rows.bom_line = [
    { parent_id = "A", child_id = "S", qty_per = "2" },
    { parent_id = "B", child_id = "S", qty_per = "2" },
    { parent_id = "C", child_id = "S", qty_per = "2" },
    { parent_id = "D", child_id = "S", qty_per = { null = true } },
    { parent_id = "E", child_id = "S", qty_per = "40" },
    { parent_id = "A", child_id = "T", qty_per = "1" },
    { parent_id = "A", child_id = "T", qty_per = "1" },
    { parent_id = "B", child_id = "T", qty_per = "1" },
    { parent_id = "B", child_id = "T", qty_per = "30" },
    { parent_id = "A", child_id = "U", qty_per = "5" },
    { parent_id = "B", child_id = "U", qty_per = "5" },
    { parent_id = "C", child_id = "U", qty_per = "5.0" },
    { parent_id = "D", child_id = "U", qty_per = "0.5" },
    { parent_id = "A", child_id = "V", qty_per = "0" },
    { parent_id = "B", child_id = "V", qty_per = "0" },
    { parent_id = "C", child_id = "V", qty_per = "0" },
    { parent_id = "D", child_id = "V", qty_per = "5" },
  ]
  expect = [{ parent_id = "D", child_id = "U", qty_per = "0.5", mode = "5", n = 1 }]

[[rule]]
id = "twin_parts"
kind = "same_signature"
severity = "warning"
attrs = ["unit"]
  [[rule.example]]
  spec = "suite 9: NULL attributes are equal, and a parent without a part row is no member"
  rows.part = [
    { part_id = "B1", unit = { null = true } }, { part_id = "B2", unit = { null = true } },
    { part_id = "B3", unit = "pc" },
  ]
  rows.bom_line = [
    { parent_id = "B1", child_id = "S", qty_per = "1" },
    { parent_id = "B2", child_id = "S", qty_per = "1" },
    { parent_id = "B3", child_id = "S", qty_per = "1" },
    { parent_id = "GHOST", child_id = "S", qty_per = "1" },
  ]
  expect = [{ group_id = "B1", member = "B1" }, { group_id = "B1", member = "B2" }]

[[rule]]
id = "parents_with_two_quantities"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = ["parent_id"]
count = { distinct = "qty_per" }
min = 2
  [[rule.example]]
  spec = "suite 10: distinct counts skip NULL values and compare numbers by value"
  rows.bom_line = [
    { parent_id = "A", qty_per = "1" },
    { parent_id = "A", qty_per = { null = true } },
    { parent_id = "B", qty_per = "1" },
    { parent_id = "B", qty_per = "1.000" },
    { parent_id = "C", qty_per = "1" },
    { parent_id = "C", qty_per = "2" },
  ]
  expect = [{ parent_id = "C", n = 2 }]

[[rule]]
id = "levels"
kind = "stored_metric"
severity = "warning"
metric = "height"
stored = "stored_level"
  [[rule.example]]
  spec = "suite 11: a diamond takes its longest chain, and a child without a part row is a leaf"
  rows.part = [
    { part_id = "D1", stored_level = 3 }, { part_id = "D2", stored_level = 1 }, { part_id = "D3", stored_level = 2 },
    { part_id = "D4", stored_level = 1 }, { part_id = "D5", stored_level = 1 }, { part_id = "T", stored_level = 1 },
  ]
  rows.bom_line = [
    { parent_id = "D1", child_id = "D2" }, { parent_id = "D1", child_id = "D3" },
    { parent_id = "D2", child_id = "D4" }, { parent_id = "D3", child_id = "D4" },
    { parent_id = "D4", child_id = "D5" }, { parent_id = "T", child_id = "X" },
  ]
  expect = [
    { part_id = "D2", kind = "stored_below", stored = 1, height = 2 },
    { part_id = "D5", kind = "leaf_stored_nonzero", stored = 1, height = 0 },
  ]

[[rule]]
id = "grams_near_quantity"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "child"
text = "name"
pattern = '\[([0-9.]+) g\]'
quantity = "qty_per"
rel_tol = "0.001"
report = { columns = ["parent_id", "child_id", "qty_per"] }
  [[rule.example]]
  spec = "suite 12: the tolerance is inclusive; a NULL text, a NULL quantity or a missing part is no match"
  rows.part = [{ part_id = "P", name = "Plate [1000 g]" }, { part_id = "Q", name = { null = true } }]
  rows.bom_line = [
    { parent_id = "A", child_id = "P", qty_per = "999" },
    { parent_id = "A", child_id = "P", qty_per = "998.999999" },
    { parent_id = "B", child_id = "P", qty_per = "1001" },
    { parent_id = "A", child_id = "P", qty_per = { null = true } },
    { parent_id = "A", child_id = "Q", qty_per = "1000" },
    { parent_id = "A", child_id = "MISSING", qty_per = "1000" },
  ]
  expect = [
    { parent_id = "A", child_id = "P", qty_per = "999", numerator = "1000", divisor = "1", n = 1 },
    { parent_id = "B", child_id = "P", qty_per = "1001", numerator = "1000", divisor = "1", n = 1 },
  ]
"""

_COLUMN_TEST = ev._column_test
_STRUCTURE = ev._structure
_QUANTIFIED = ev._quantified
_MODE = ev._mode
_HEIGHT_KIND = ev._height_kind
_AMOUNTS = ev._amounts
_NUMBER_OPS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le, "gt": operator.gt,
               "ge": operator.ge}


def null_guard_dropped(op, value, operand):
    """ne, not_in and not_in_ref written as NOT (x op c) without the NULL guard, so they hold on NULL."""
    if value is None and op in ("ne", "not_in", "not_in_ref"):
        return True
    return _COLUMN_TEST(op, value, operand)


def numbers_as_text(op, value, operand):
    """Numbers compared by their text: Decimal compared as text, a numeric IN compared as text (e3)."""
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool) and op in (*_NUMBER_OPS, "in", "not_in"):
        if op in ("in", "not_in"):
            return (str(value) in {str(m) for m in operand}) == (op == "in")
        return _NUMBER_OPS[op](str(value), str(operand))
    return _COLUMN_TEST(op, value, operand)


def starts_with_folded(op, value, operand):
    """starts_with on case-folded text."""
    if op == "starts_with" and value is not None:
        return value.casefold().startswith(tuple(p.casefold() for p in operand))
    return _COLUMN_TEST(op, value, operand)


def starts_with_trimmed(op, value, operand):
    """starts_with on trimmed text."""
    if op == "starts_with" and value is not None:
        return value.strip().startswith(operand)
    return _COLUMN_TEST(op, value, operand)


def null_endpoint_dangling(endpoint, nodes, exists):
    """A NULL endpoint taken for an endpoint without a node row (e1)."""
    return (endpoint is not None and endpoint in nodes) == exists


def attribute_as_not_exists(endpoint, nodes, column, test):
    """The endpoint attribute written as NOT EXISTS (a node row where the comparison fails) (m3)."""
    node = None if endpoint is None else nodes.get(endpoint)
    return node is None or node[column] is None or test(node[column])


def structure_of_null_endpoint(endpoint, test, value, kids, parents):
    """Endpoint structure of a NULL endpoint evaluated like a key without edges."""
    return _STRUCTURE(endpoint, test, kids, parents) == value


def edges_with_null_end(rows, parent, child):
    """A line with a NULL end still makes an edge."""
    kids, parents = {}, {}
    for row in rows:
        kids.setdefault(row[parent], set()).add(row[child])
        parents.setdefault(row[child], set()).add(row[parent])
    return kids, parents


def in_hierarchy_children_only(x, test, kids, parents):
    """in_hierarchy read as has_children."""
    return x in kids if test == "in_hierarchy" else _STRUCTURE(x, test, kids, parents)


def all_without_neighbours(quantifier, near, nodes, sub):
    """all without its test for at least one neighbour (e2)."""
    if quantifier == "all":
        return all(n in nodes and sub(nodes[n], n) for n in near)
    return _QUANTIFIED(quantifier, near, nodes, sub)


def none_as_not_all(quantifier, near, nodes, sub):
    """none read as not all."""
    if quantifier == "none":
        return not _QUANTIFIED("all", near, nodes, sub)
    return _QUANTIFIED(quantifier, near, nodes, sub)


def null_key_part_groups(key):
    """A duplicate key with a NULL part still forms a group."""
    return True


def distinct_counts_null(values):
    """NULL counted as a distinct value."""
    return {ev._canonical(v) for v in values}


def n_always_one(content):
    """Rows of equal content reported once with n = 1."""
    return Counter({row + ("1",): 1 for row in content})


def share_strict(group, value, num, den):
    """The mode share compared with > instead of >=."""
    counts = Counter(row[value] for row in group if row[value] is not None)
    if not counts:
        return None
    mode, top = max(counts.items(), key=lambda item: item[1])
    return mode if top * den > num * len(group) and mode > 0 else None


def size_without_nulls(group, value, num, den):
    """The group size N counted without the rows whose value is NULL."""
    return _MODE([row for row in group if row[value] is not None], value, num, den)


def mode_zero_allowed(group, value, num, den):
    """A mode of 0 accepted."""
    counts = Counter(row[value] for row in group if row[value] is not None)
    if not counts:
        return None
    mode, top = max(counts.items(), key=lambda item: item[1])
    return mode if top * den >= num * len(group) and mode >= 0 else None


def ratio_strict(v, mode, ratio):
    """The ratio test with strict comparisons."""
    return v > ratio * mode or ratio * v < mode


def last_line_only(rows, parent, child, quantity):
    """The last line's quantity instead of the sum of a pair's lines."""
    sums = {}
    for row in rows:
        if row[parent] is not None and row[child] is not None:
            sums[(row[parent], row[child])] = row[quantity]
    return sums


def sums_as_text(parent, children, sums):
    """Summed quantities compared by their text."""
    return frozenset((ev._canonical(c), str(sums[(parent, c)])) for c in children)


def null_attributes_differ(node, attrs):
    """NULL attributes compared as unequal."""
    return tuple(object() if node[a] is None else ev._canonical(node[a]) for a in attrs)


def largest_group_id(members):
    """group_id from the largest member."""
    return max(members)


def null_stored_flagged(stored, height, has_children, has_parents):
    """A NULL stored value reported."""
    if stored is None:
        return "outside_stored_nonzero"
    return _HEIGHT_KIND(stored, height, has_children, has_parents)


def divisor_ignored(match, captures):
    """Capture 2 ignored (N = 1 always)."""
    return _AMOUNTS(match, 1)


def absent_divisor_zero(match, captures):
    """A divisor group that did not take part read as 0."""
    if match is not None and captures == 2 and match.group(2) is None:
        return None
    return _AMOUNTS(match, captures)


def capture_unchecked(match, captures):
    """Capture 1 read without its numeral check (1.2.3 makes Decimal raise: an evaluator error, still no pass)."""
    if match is None:
        return None
    x = Decimal(match.group(1))
    n = Decimal(match.group(2)) if captures == 2 and match.group(2) is not None else Decimal(1)
    return (x, n) if x > 0 and n > 0 else None


def tolerance_strict(q, x, n, tolerance):
    """The tolerance test with a strict comparison."""
    return abs(q * n - x) < tolerance * x


def ref_without_where(self, name, cls):
    """The ref's values read without its where condition."""
    ref = self.refs[name]
    return frozenset(row[ref.column] for row in self._rows(ref.entity) if row[ref.column] is not None)


MUTANTS = {   # name: (owner, attribute, replacement, an authored example that must catch it)
    "null_guard_dropped": (ev, "_column_test", null_guard_dropped, "suite issue_not_std"),
    "numbers_as_text": (ev, "_column_test", numbers_as_text, "clean_room truncated_third"),
    "starts_with_folded": (ev, "_column_test", starts_with_folded, "clean_room temporary_name"),
    "starts_with_trimmed": (ev, "_column_test", starts_with_trimmed, "clean_room temporary_name"),
    "null_endpoint_dangling": (ev, "_endpoint_exists", null_endpoint_dangling, "suite lines_of_unknown_parents"),
    "attribute_as_not_exists": (ev, "_endpoint_attribute", attribute_as_not_exists,
                                "clean_room retired_child_in_active_parent"),
    "structure_of_null_endpoint": (ev, "_endpoint_structure", structure_of_null_endpoint, "suite lines_to_leaves"),
    "edges_with_null_end": (ev, "_edges", edges_with_null_end, "clean_room kit_without_bom"),
    "in_hierarchy_children_only": (ev, "_structure", in_hierarchy_children_only, "clean_room unknown_procure_code"),
    "all_without_neighbours": (ev, "_quantified", all_without_neighbours, "clean_room all_children_retired"),
    "none_as_not_all": (ev, "_quantified", none_as_not_all, "suite assembly_without_retired_parts"),
    "null_key_part_groups": (ev, "_complete", null_key_part_groups, "clean_room repeated_line"),
    "distinct_counts_null": (ev, "_distinct", distinct_counts_null, "suite parents_with_two_quantities"),
    "n_always_one": (ev, "_with_counts", n_always_one, "clean_room qty_over_limit"),
    "share_strict": (ev, "_mode", share_strict, "clean_room odd_quantity"),
    "size_without_nulls": (ev, "_mode", size_without_nulls, "suite lopsided_quantity"),
    "mode_zero_allowed": (ev, "_mode", mode_zero_allowed, "suite lopsided_quantity"),
    "ratio_strict": (ev, "_far_from_mode", ratio_strict, "clean_room odd_quantity"),
    "last_line_only": (ev, "_line_sums", last_line_only, "clean_room twin_assemblies"),
    "sums_as_text": (ev, "_signature", sums_as_text, "clean_room twin_assemblies"),
    "null_attributes_differ": (ev, "_attributes", null_attributes_differ, "suite twin_parts"),
    "largest_group_id": (ev, "_group_id", largest_group_id, "clean_room twin_assemblies"),
    "null_stored_flagged": (ev, "_height_kind", null_stored_flagged, "clean_room stored_level_drift"),
    "divisor_ignored": (ev, "_amounts", divisor_ignored, "clean_room grams_in_count_unit"),
    "absent_divisor_zero": (ev, "_amounts", absent_divisor_zero, "clean_room grams_in_count_unit"),
    "capture_unchecked": (ev, "_amounts", capture_unchecked, "clean_room grams_in_count_unit"),
    "tolerance_strict": (ev, "_near", tolerance_strict, "suite grams_near_quantity"),
    "ref_without_where": (ev.Evaluator, "_ref_values", ref_without_where, "clean_room unknown_procure_code"),
}


class MutationTest(unittest.TestCase):
    """Each mutant plants one defect a hand-written evaluator could have. Every mutant must make at least one authored
    example disagree with its authored rows, and the named example must be among them. An exception counts as a
    disagreement: the stage reports it as an evaluator error, never as a pass."""

    @classmethod
    def setUpClass(cls):
        suite = load(SUITE)
        cls.examples = [(f"{label} {rule.id}", spec, rule, example)
                        for label, spec in (("clean_room", clean_room()), ("suite", suite))
                        for rule in spec.rules for example in rule.examples]

    def disagreeing(self) -> list:
        return [label for label, spec, rule, example in self.examples
                if not example_agrees(spec, rule, example, PLAN)]

    def test_the_suite_and_the_clean_room_examples_agree(self):
        self.assertEqual(len(self.examples), 27)
        self.assertEqual(self.disagreeing(), [])

    def test_every_mutant_is_caught_by_an_authored_example(self):
        for name, (owner, attribute, replacement, catcher) in MUTANTS.items():
            with self.subTest(mutant=name), mock.patch.object(owner, attribute, replacement):
                caught = self.disagreeing()
                self.assertIn(catcher, caught, f"mutant {name} was not caught by {catcher} (caught by {caught})")

    def test_every_mutant_replaces_an_existing_function(self):
        for name, (owner, attribute, _replacement, _catcher) in MUTANTS.items():
            with self.subTest(mutant=name):
                self.assertTrue(callable(getattr(owner, attribute)))

    def test_a_raising_mutant_counts_as_caught(self):
        spec = clean_room()
        rule = spec.rule("grams_in_count_unit")
        with mock.patch.object(ev, "_amounts", capture_unchecked), self.assertRaises(InvalidOperation):
            ev.evaluate(rule, ev.example_tables(rule, rule.examples[0], PLAN), spec.hierarchy, spec.sets, spec.refs)


# ---- the import boundary --------------------------------------------------------------------------------
class ImportBoundaryTest(unittest.TestCase):
    def test_the_evaluator_imports_only_the_standard_library_and_canon(self):
        tree = ast.parse(Path(ev.__file__).read_text(encoding="utf-8"))
        local = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names, alias.name)
                    self.assertNotEqual(alias.name.split(".")[0], "importlib")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.assertIsNone(node.module, "relative imports name modules, not packages")
                    local |= {a.name for a in node.names}
                else:
                    self.assertIn(node.module.split(".")[0], sys.stdlib_module_names, node.module)
                    self.assertNotEqual(node.module.split(".")[0], "importlib")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("__import__", "exec", "eval"))
        self.assertLessEqual(local, {"canon", "ident"})
        self.assertFalse(local & {"data_rules_sql", "config", "db", "result", "shapes", "stage_build",
                                  "stage_data_rules", "naming"})

    def test_importing_the_evaluator_loads_no_other_plugin_module(self):
        code = ("import sys, semantic_crosscheck.data_rules_eval; "
                "print(sorted(m for m in sys.modules if m.startswith('semantic_crosscheck')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=pgfixture.SCRIPTS, capture_output=True, text=True,
                             check=True, timeout=60).stdout
        self.assertEqual(out.strip(),
                         "['semantic_crosscheck', 'semantic_crosscheck.canon', 'semantic_crosscheck.data_rules_eval']")


if __name__ == "__main__":
    unittest.main()
