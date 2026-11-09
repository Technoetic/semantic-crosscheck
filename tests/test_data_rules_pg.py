"""PostgreSQL 17 integration tests of the data-rules stage and the rule views of build (skipped unless
SXC_TEST_ADMIN_DSN is set; the strict CI job runs them with no skip).

One throw-away database (PgFixture "sxc_t_rules") holds the clean-room example: schema sxc_rules owned by the loader
role, loaded from examples/data-rules/fixture.sql by that role, and readable by the check role. Every scenario gets its
own source and output schemas in that database, so build and data-rules always run through sxc.main against real
tables and views. The expected outputs below are written by hand from the semantic contract of the data rules (spec
section 4), never computed with either evaluation, so both paths are checked against a third reference.

Covered (spec section 12.2 and more):
- build creates and marks every rule view and the subjects view; data-rules agrees on every rule of the clean-room
  example with hand-written statuses, counts, view rows and ord order, and its authored examples run as example
  statements (a common-mode example and an SQL mutant's example mismatch included); --rule narrowing;
- the variant with NULL endpoints and missing references, plus further rules covering every atom family, report form
  and kind option (boolean and NULL output cells, a composite report key, empty example tables and expectations);
  SQL mutants m1-m4 and Python mutants e1-e3 make the cross-check fail;
- other column types: char(n) keys with pad spaces, varchar keys of two lengths with smallint and bigint columns,
  integer keys of two integer types, deterministic ICU columns (ord and group_id stay in code-point order, checked by
  mutants), a nondeterministic collation (collation not_run), and two collations compared with each other (rule
  errors 42P22, or for parent and child a snapshot error 42P21, with a collation hint; never a verdict); rule constants with quotes, backslashes, colons, $1, -- and non-ASCII text in the view
  definitions; a reserved word as a column name; a pattern with escaped punctuation in a bracket class;
- gates and budgets: NaN and infinity (and an entity pointed at a view that clears them), NULL and duplicate node
  keys, a cycle and a shallow max_depth, NULL edge quantities, long text, an empty edge table, max_export_rows (no rule,
  lookup or export statement runs) and max_rule_rows;
- one snapshot: REPEATABLE READ against a concurrent commit (and READ COMMITTED as the counter-check), savepoints
  around a failing rule, a rule timeout and a failing example, and a timeout outside the savepoints;
- the views between build and data-rules: a stale rule file, 42P16 (drop and create), 42P01, a view changed by hand,
  orphan cleanup of marked views only;
- a check role without CREATE or TEMP whose transactions default to read only; a database that is not UTF8; row
  values that never reach results, consoles, rules.sql or the report.
"""

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import canon, config, data_rules, data_rules_eval as ev, data_rules_sql as dsql, db, report
from semantic_crosscheck import stage_data_rules as sdr

ROOT = pgfixture.ROOT
EXAMPLE = ROOT / "examples" / "data-rules"
FIXTURE = EXAMPLE / "fixture.sql"
DATABASE = "sxc_t_rules"
ASCII_DATABASE = "sxc_t_rules_ascii"
OWNER, CHECK, READER = "xc_owner", "xc_check", "xc_rules_reader"
CHECK_ENV, READER_ENV, ASCII_ENV = "SXC_PG_DSN", "SXC_T_RULES_READER_DSN", "SXC_T_RULES_ASCII_DSN"
VARIANT_MARK = "-- variant:"
U_ACUTE = chr(0xDA)                # U with acute accent: the fixture's non-ASCII key starts with it
ROW_MARKERS = ("GHOST-T", "LOST-R", "TMP-Bracket", "Washer [12.5 g]", "Bolt M6", U_ACUTE + "P4", "tw-a")
CONSTANT_MARKERS = ("TMP-", "PROCURE", "KIT", "0.3333", "0.6666", "[0-9.]", "500.000001")
FIXED_LITERALS = {"'UTF8'", "'hex'", "':'", "','", "'NaN'", "'Infinity'", "'-Infinity'",
                  *(f"'{kind}'" for kind in data_rules.HEIGHT_KINDS)}   # SQL grammar, never a configured value
KEY_NAMES = ("parent_id", "child_id", "part_id", "group_id", "member")

CONFIG = """version = 1
[source]
dsn_env = "{dsn_env}"
schemas = ["{schema}"]
output_schema = "{out_schema}"
statement_timeout_ms = {timeout_ms}
[entities.part]
table = "{schema}.part"
key = ["part_id"]
[entities.bom_line]
table = "{schema}.bom_line"
key = ["line_id"]
[entities.code_value]
table = "{schema}.code_value"
key = ["code_group", "code"]
[hierarchies.has_component]
edge = "bom_line"
node = "part"
parent = "parent_id"
child = "child_id"
quantity = "qty_per"
max_depth = {max_depth}
[data_rules]
file = "data-rules.toml"
views = {views}
require_examples = {require_examples}
max_export_rows = {max_export_rows}
max_rule_rows = {max_rule_rows}
"""
SETTINGS = {"dsn_env": CHECK_ENV, "timeout_ms": 120000, "max_depth": 16, "views": "true", "require_examples": "true",
            "max_export_rows": 200000, "max_rule_rows": 200000}

# ---- hand-written outputs of the clean-room rules on the base rows of fixture.sql ------------------------------------
# rule id: (rule status, violations, rows of v_rule_<id> in ord order, numbers as canonical numerals, NULL as None)
BASE = {
    "qty_over_limit": ("fail", 4, [("QR", "QB", "500.000001", "1"), ("QR", "QC", "750", "2"),
                                   ("QR", "QE", "1000", "1")]),
    "parent_unknown": ("fail", 2, [("GHOST",), ("GHOST-T",)]),
    "kit_without_bom": ("fail", 1, [("K1",)]),
    "retired_child_in_active_parent": ("fail", 8, [
        ("AC1", "AR1", "1"), ("AC1", "AR2", "1"), ("AC2", "AR1", "1"), ("AC3", "AR1", "1"), ("RA1", "RX", "2"),
        ("RUp", "RU1", "1"), ("RUp", "RU4", "1")]),
    "all_children_retired": ("fail", 1, [("AC1",)]),
    "retired_assembly_in_use": ("fail", 1, [("RU1",)]),
    "unknown_procure_code": ("pass", 5, [("UP1",), ("UP7",), ("Up2",), ("up3",), (U_ACUTE + "P4",)]),
    "temporary_name": ("pass", 1, [("TN1",)]),
    "truncated_third": ("pass", 2, [("TT", "T1", "0.3333", "1"), ("TT", "T2", "0.6666", "1")]),
    "repeated_line": ("pass", 6, [("DP", "DC", "3"), ("OA", "OT", "3"), ("QR", "QC", "2"), ("RA1", "RX", "2"),
                                  ("SP2", "SB1", "2"), ("TW1", "WS1", "2")]),
    "same_name_components": ("pass", 1, [("SP1",)]),
    "odd_quantity": ("pass", 2, [("OG", "OS", "40", "2", "1"), ("OH", "OS", "0.1", "2", "1")]),
    "twin_assemblies": ("pass", 4, [("TW-B", "TW-B"), ("TW-B", "tw-a"), ("TW1", "TW1"), ("TW1", "TW2")]),
    "stored_level_drift": ("pass", 6, [
        ("H0", "leaf_stored_nonzero", "2", "0"), ("H1", "stored_above", "5", "1"),
        ("H2", "has_children_stored_zero", "0", "2"), ("HB", "stored_below", "1", "3"),
        ("HD2", "has_children_stored_zero", "0", "1"), ("HO", "outside_stored_nonzero", "4", "0")]),
    "grams_in_count_unit": ("pass", 4, [
        ("GP", "GW1", "12.5", "12.5", "1", "1"), ("GP", "GW11", "2", "4", "2", "1"),
        ("GP", "GW2", "7.5", "30", "4", "1"), ("GP", "GW7", "8.008", "8", "1", "1")]),
}
BASE_DETAIL = {"twin_assemblies": {"groups": 2},
               "stored_level_drift": {"has_children_stored_zero": 2, "stored_below": 1, "stored_above": 1,
                                      "leaf_stored_nonzero": 1, "outside_stored_nonzero": 1}}
V, W = "violation", "warning"
BASE_SUBJECTS = [   # (subject, severity, severity_rank, rule_count, rules) in ord order
    ("AC1", V, 0, 2, ("retired_child_in_active_parent", "all_children_retired")),
    ("AC2", V, 0, 1, ("retired_child_in_active_parent",)),
    ("AC3", V, 0, 1, ("retired_child_in_active_parent",)),
    ("GHOST", V, 0, 1, ("parent_unknown",)),
    ("GHOST-T", V, 0, 1, ("parent_unknown",)),
    ("K1", V, 0, 1, ("kit_without_bom",)),
    ("QR", V, 0, 2, ("qty_over_limit", "repeated_line")),
    ("RA1", V, 0, 2, ("retired_child_in_active_parent", "repeated_line")),
    ("RU1", V, 0, 1, ("retired_assembly_in_use",)),
    ("RUp", V, 0, 1, ("retired_child_in_active_parent",)),
    ("DP", W, 1, 1, ("repeated_line",)),
    ("GP", W, 1, 1, ("grams_in_count_unit",)),
    ("OA", W, 1, 1, ("repeated_line",)),
    ("OG", W, 1, 1, ("odd_quantity",)),
    ("OH", W, 1, 1, ("odd_quantity",)),
    ("SP1", W, 1, 1, ("same_name_components",)),
    ("SP2", W, 1, 1, ("repeated_line",)),
    ("TN1", W, 1, 1, ("temporary_name",)),
    ("TT", W, 1, 1, ("truncated_third",)),
    ("TW-B", W, 1, 1, ("twin_assemblies",)),
    ("TW1", W, 1, 2, ("repeated_line", "twin_assemblies")),
    ("TW2", W, 1, 1, ("twin_assemblies",)),
    ("UP1", W, 1, 1, ("unknown_procure_code",)),
    ("UP7", W, 1, 1, ("unknown_procure_code",)),
    ("Up2", W, 1, 1, ("unknown_procure_code",)),
    ("tw-a", W, 1, 1, ("twin_assemblies",)),
    ("up3", W, 1, 1, ("unknown_procure_code",)),
    (U_ACUTE + "P4", W, 1, 1, ("unknown_procure_code",)),
]
BASE_ROWS = {"part": 118, "bom_line": 129, "code_value": 4}
NODE_KEY_RULES = ("parent_unknown", "kit_without_bom", "retired_child_in_active_parent", "all_children_retired",
                  "retired_assembly_in_use", "unknown_procure_code", "temporary_name", "truncated_third",
                  "same_name_components", "twin_assemblies", "stored_level_drift", "grams_in_count_unit")
QTY_RULES = ("qty_over_limit", "truncated_third", "odd_quantity", "twin_assemblies", "grams_in_count_unit")
NAME_RULES = ("temporary_name", "same_name_components", "twin_assemblies", "grams_in_count_unit")

# ---- further rules of the tests: every atom family and report form the clean-room rules leave out ------------------
EXTRA_RULES = r'''
[[rule]]
id = "child_unknown_line"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ end = "child", exists = false }]
  [[rule.example]]
  spec = "test suite: a line naming a child that is not a part; a NULL child names nothing"
  rows.part = [{ part_id = "A" }]
  rows.bom_line = [
    { line_id = 1, child_id = "A" }, { line_id = 2, child_id = "B" }, { line_id = 3, child_id = { null = true } },
  ]
  expect = [{ line_id = 2 }]
  [[rule.example]]
  spec = "test suite: without part rows every named child is unknown"
  rows.part = []
  rows.bom_line = [{ line_id = 1, child_id = "A" }, { line_id = 2, child_id = { null = true } }]
  expect = [{ line_id = 1 }]

[[rule]]
id = "procure_not_listed"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "procure", op = "not_in_ref", ref = "procure_codes" }]
report = { columns = ["procure"] }
  [[rule.example]]
  spec = "test suite: procurement codes outside the PROCURE group, counted; NULL is no code"
  rows.part = [
    { part_id = "A", procure = "BUY" }, { part_id = "B", procure = "XX" }, { part_id = "C", procure = "XX" },
    { part_id = "D", procure = { null = true } },
  ]
  rows.code_value = [{ code_group = "PROCURE", code = "BUY" }, { code_group = "OTHER", code = "XX" }]
  expect = [{ procure = "XX", n = 2 }]
  [[rule.example]]
  spec = "test suite: every code is listed, so nothing is reported"
  rows.part = [{ part_id = "A", procure = "BUY" }, { part_id = "B", procure = { null = true } }]
  rows.code_value = [{ code_group = "PROCURE", code = "BUY" }]
  expect = []

[[rule]]
id = "root_line_to_assembly"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [
  { end = "parent", exists = true },
  { end = "parent", has_parents = false },
  { end = "child", has_children = true },
  { end = "child", in_hierarchy = true },
]
report = { columns = ["parent_id", "child_id"] }
  [[rule.example]]
  spec = "test suite: lines from an existing root part to a subassembly"
  rows.part = [{ part_id = "R" }, { part_id = "S" }]
  rows.bom_line = [
    { line_id = 1, parent_id = "R", child_id = "S" }, { line_id = 2, parent_id = "S", child_id = "L" },
    { line_id = 3, parent_id = "X", child_id = "S" }, { line_id = 4, parent_id = "R", child_id = "L" },
  ]
  expect = [{ parent_id = "R", child_id = "S", n = 1 }]

[[rule]]
id = "qty_band"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [
  { column = "qty_per", op = "ge", value = "0.1" },
  { column = "qty_per", op = "lt", value = 1 },
  { column = "qty_per", op = "ne", value = "0.6666" },
  { column = "line_id", op = "le", value = 1600 },
]
report = { columns = ["qty_per"] }
  [[rule.example]]
  spec = "test suite: quantities from 0.1 up to 1, except two thirds, on the original lines"
  rows.bom_line = [
    { line_id = 1, qty_per = "0.1" }, { line_id = 2, qty_per = "1" }, { line_id = 3, qty_per = "0.6666" },
    { line_id = 4, qty_per = "0.0999" }, { line_id = 5, qty_per = "0.5" }, { line_id = 1601, qty_per = "0.5" },
    { line_id = 6, qty_per = { null = true } }, { line_id = 7, qty_per = "0.500" },
  ]
  expect = [{ qty_per = "0.1", n = 1 }, { qty_per = "0.5", n = 2 }]

[[rule]]
id = "unit_text_ops"
kind = "row"
entity = "part"
severity = "warning"
where = [
  { column = "unit", op = "not_in", set = "count_units" },
  { column = "issue", op = "ne", value = "KIT" },
  { column = "unit", op = "is_not_null" },
]
report = { distinct = "unit" }
  [[rule.example]]
  spec = "test suite: units outside the count units on parts that are not kits"
  rows.part = [
    { part_id = "A", unit = "kg", issue = "STD" }, { part_id = "B", unit = "pc", issue = "STD" },
    { part_id = "C", unit = "m", issue = "KIT" }, { part_id = "D", unit = { null = true }, issue = "STD" },
    { part_id = "E", unit = "kg", issue = { null = true } }, { part_id = "F", unit = "Kg", issue = "kit" },
  ]
  expect = [{ unit = "kg" }, { unit = "Kg" }]

[[rule]]
id = "quantifier_mix"
kind = "row"
entity = "part"
severity = "warning"
where = [
  { column = "part_class", op = "eq", value = "ASSY" },
  { children = "none", where = [{ column = "retired", op = "eq", value = true }] },
  { parents = "all", where = [{ column = "part_class", op = "eq", value = "ASSY" }, { has_parents = false }] },
]
  [[rule.example]]
  spec = "test suite: subassemblies of existing root assemblies without a retired component"
  rows.part = [
    { part_id = "R", part_class = "ASSY", retired = false }, { part_id = "S", part_class = "ASSY", retired = false },
    { part_id = "T", part_class = "ASSY", retired = false }, { part_id = "U", part_class = "ASSY", retired = false },
    { part_id = "V", part_class = "ASSY", retired = false }, { part_id = "X", part_class = "PART", retired = true },
  ]
  rows.bom_line = [
    { line_id = 1, parent_id = "R", child_id = "S" }, { line_id = 2, parent_id = "R", child_id = "T" },
    { line_id = 3, parent_id = "T", child_id = "X" }, { line_id = 4, parent_id = "S", child_id = "U" },
    { line_id = 5, parent_id = "M", child_id = "V" },
  ]
  expect = [{ part_id = "S" }]

[[rule]]
id = "dup_levels"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = [{ end = "parent", column = "part_class" }, { end = "child", column = "stored_level" }]
count = "rows"
min = 2
report = "group"
  [[rule.example]]
  spec = "test suite: lines grouped by the parent's class and the child's stored level"
  rows.part = [
    { part_id = "A", part_class = "ASSY", stored_level = 0 }, { part_id = "B", part_class = "ASSY", stored_level = 1 },
    { part_id = "L", part_class = "PART", stored_level = 0 },
    { part_id = "N", part_class = "PART", stored_level = { null = true } },
  ]
  rows.bom_line = [
    { line_id = 1, parent_id = "A", child_id = "L" }, { line_id = 2, parent_id = "B", child_id = "L" },
    { line_id = 3, parent_id = "A", child_id = "N" }, { line_id = 4, parent_id = "B", child_id = "N" },
    { line_id = 5, parent_id = "A", child_id = "B" }, { line_id = 6, parent_id = "X", child_id = "L" },
  ]
  expect = [{ parent_part_class = "ASSY", child_stored_level = 0, n = 2 }]

[[rule]]
id = "mode_key"
kind = "mode_outlier"
entity = "bom_line"
severity = "warning"
group_by = "child_id"
value = "qty_per"
min_mode_share = "0.75"
ratio = "20"
  [[rule.example]]
  spec = "test suite: outlier lines by line id, without a distinct-parent threshold"
  rows.bom_line = [
    { line_id = 1, child_id = "S", qty_per = "1" }, { line_id = 2, child_id = "S", qty_per = "1" },
    { line_id = 3, child_id = "S", qty_per = "1" }, { line_id = 4, child_id = "S", qty_per = "20" },
    { line_id = 5, child_id = "T", qty_per = "1" }, { line_id = 6, child_id = "T", qty_per = "20" },
  ]
  expect = [{ line_id = 4 }]

[[rule]]
id = "same_sig_three"
kind = "same_signature"
severity = "warning"
attrs = ["unit"]
min_members = 3
  [[rule.example]]
  spec = "test suite: three assemblies with one signature and one unit"
  rows.part = [
    { part_id = "A", unit = "pc" }, { part_id = "B", unit = "pc" }, { part_id = "C", unit = "pc" },
    { part_id = "D", unit = "kg" },
  ]
  rows.bom_line = [
    { line_id = 1, parent_id = "A", child_id = "S", qty_per = "1" },
    { line_id = 2, parent_id = "B", child_id = "S", qty_per = "1" },
    { line_id = 3, parent_id = "C", child_id = "S", qty_per = "0.5" },
    { line_id = 4, parent_id = "C", child_id = "S", qty_per = "0.5" },
    { line_id = 5, parent_id = "D", child_id = "S", qty_per = "1" },
  ]
  expect = [{ group_id = "A", member = "A" }, { group_id = "A", member = "B" }, { group_id = "A", member = "C" }]

[[rule]]
id = "text_one_capture"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "parent"
text = "name"
pattern = 'x([0-9]+)$'
quantity = "qty_per"
rel_tol = "0"
report = { distinct = "child_id" }
  [[rule.example]]
  spec = "test suite: a line quantity equal to the count at the end of the parent's name"
  rows.part = [{ part_id = "P", name = "Tray x4" }, { part_id = "Q", name = "Box x4 lid" }]
  rows.bom_line = [
    { line_id = 1, parent_id = "P", child_id = "C1", qty_per = "4" },
    { line_id = 2, parent_id = "P", child_id = "C2", qty_per = "4.001" },
    { line_id = 3, parent_id = "Q", child_id = "C3", qty_per = "4" },
  ]
  expect = [{ child_id = "C1" }]

[[rule]]
id = "children_any_parents_none"
kind = "row"
entity = "part"
severity = "warning"
where = [
  { children = "any", where = [{ column = "retired", op = "is_null" }] },
  { parents = "none", where = [{ column = "retired", op = "eq", value = true }] },
]
  [[rule.example]]
  spec = "test suite: a part with a component of unknown status and no retired user"
  rows.part = [
    { part_id = "A", retired = false }, { part_id = "N", retired = { null = true } },
    { part_id = "B", retired = false }, { part_id = "R", retired = true },
  ]
  rows.bom_line = [
    { line_id = 1, parent_id = "A", child_id = "N" }, { line_id = 2, parent_id = "B", child_id = "N" },
    { line_id = 3, parent_id = "R", child_id = "B" }, { line_id = 4, parent_id = "A", child_id = "MISSING" },
  ]
  expect = [{ part_id = "A" }]

[[rule]]
id = "child_procure_unknown"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [
  { end = "child", column = "procure", op = "not_in_ref", ref = "procure_codes" },
  { end = "parent", column = "issue", op = "ne", value = "KIT" },
]
  [[rule.example]]
  spec = "test suite: a line from a part that is no kit to a part with an unknown procurement code"
  rows.part = [
    { part_id = "P", issue = "STD", procure = "BUY" }, { part_id = "K", issue = "KIT", procure = "BUY" },
    { part_id = "X", issue = "STD", procure = "XX" }, { part_id = "N", issue = "STD", procure = { null = true } },
  ]
  rows.bom_line = [
    { line_id = 1, parent_id = "P", child_id = "X" }, { line_id = 2, parent_id = "K", child_id = "X" },
    { line_id = 3, parent_id = "P", child_id = "N" }, { line_id = 4, parent_id = "P", child_id = "MISSING" },
    { line_id = 5, parent_id = { null = true }, child_id = "X" },
  ]
  rows.code_value = [{ code_group = "PROCURE", code = "BUY" }]
  expect = [{ line_id = 1 }]

[[rule]]
id = "root_retired_flags"
kind = "row"
entity = "part"
severity = "warning"
where = [{ has_children = true }, { has_parents = false }]
report = { columns = ["retired"] }
  [[rule.example]]
  spec = "test suite: root assemblies counted by their retired flag, NULL included"
  rows.part = [
    { part_id = "R", retired = true }, { part_id = "S", retired = false }, { part_id = "T", retired = { null = true } },
    { part_id = "U", retired = false }, { part_id = "C", retired = false },
  ]
  rows.bom_line = [
    { line_id = 1, parent_id = "R", child_id = "C" }, { line_id = 2, parent_id = "S", child_id = "C" },
    { line_id = 3, parent_id = "T", child_id = "C" }, { line_id = 4, parent_id = "U", child_id = "S" },
  ]
  expect = [{ retired = true, n = 1 }, { retired = { null = true }, n = 1 }, { retired = false, n = 1 }]

[[rule]]
id = "outside_parts"
kind = "row"
entity = "part"
severity = "warning"
where = [{ in_hierarchy = false }, { column = "stored_level", op = "is_not_null" }]
  [[rule.example]]
  spec = "test suite: parts outside every BOM that still carry a stored level"
  rows.part = [
    { part_id = "A", stored_level = 0 }, { part_id = "B", stored_level = { null = true } },
    { part_id = "C", stored_level = 2 },
  ]
  rows.bom_line = [{ line_id = 1, parent_id = "C", child_id = "X" }]
  expect = [{ part_id = "A" }]

[[rule]]
id = "other_codes"
kind = "row"
entity = "code_value"
severity = "warning"
where = [{ column = "code_group", op = "ne", value = "PROCURE" }]
  [[rule.example]]
  spec = "test suite: codes of other groups, reported by their two-column key"
  rows.code_value = [{ code_group = "PROCURE", code = "BUY" }, { code_group = "OTHER", code = "XX" }]
  expect = [{ code_group = "OTHER", code = "XX" }]
'''

# ---- hand-written outputs on the variant (the base rows plus the variant rows of fixture.sql) -----------------------
VARIANT = dict(BASE)
VARIANT.update({
    "qty_over_limit": ("fail", 5, [("QR", None, "900", "1")] + BASE["qty_over_limit"][2]),
    "parent_unknown": ("fail", 3, [("GHOST",), ("GHOST-T",), ("GHOST-V",)]),
    "truncated_third": ("pass", 3, [(None, "T1", "0.3333", "1")] + BASE["truncated_third"][2]),
    "child_unknown_line": ("pass", 8, [("405",), ("504",), ("905",), ("1106",), ("1406",), ("1407",), ("1511",),
                                       ("1606",)]),
    "procure_not_listed": ("pass", 5, [("", "1"), ("BUY ", "1"), ("XX", "2"), ("buy", "1")]),
    "root_line_to_assembly": ("pass", 6, [("H3", "H2", "1"), ("HB", "H2", "1"), ("KP", "K2", "1"), ("RUp", "RU1", "1"),
                                          ("RUp", "RU5", "1"), ("RUq", "RU2", "1")]),
    "qty_band": ("pass", 6, [("0.1", "1"), ("0.3333", "4"), ("0.3334", "1")]),
    "unit_text_ops": ("pass", 2, [("PC",), ("kg",)]),
    "quantifier_mix": ("pass", 4, [("H2",), ("RU1",), ("RU2",), ("RU5",)]),
    "dup_levels": ("pass", 1, [("ASSY", "0", "2")]),
    "mode_key": ("pass", 3, [("1207",), ("1208",), ("1214",)]),
    "same_sig_three": ("pass", 7, [("RU1", "RU1"), ("RU1", "RU2"), ("RU1", "RU3"), ("RU1", "RU5"), ("TW1", "TW1"),
                                   ("TW1", "TW2"), ("TW1", "TW4")]),
    "text_one_capture": ("pass", 1, [("XC1",)]),
    "children_any_parents_none": ("pass", 2, [("AC6",), ("RA1",)]),
    "child_procure_unknown": ("pass", 4, [("701",), ("702",), ("704",), ("706",)]),
    "root_retired_flags": ("pass", 40, [(None, "1"), (False, "36"), (True, "3")]),
    "outside_parts": ("pass", 2, [("HO",), ("HZ",)]),
    "other_codes": ("pass", 2, [("OTHER", "XX"), ("OTHER", "buy")]),
})
VARIANT_DETAIL = dict(BASE_DETAIL, same_sig_three={"groups": 2})
VARIANT_SUBJECTS = BASE_SUBJECTS[:5] + [("GHOST-V", V, 0, 1, ("parent_unknown",))] + BASE_SUBJECTS[5:]
VARIANT_ROWS = {"part": 119, "bom_line": 136, "code_value": 4}


# ---- helpers ---------------------------------------------------------------------------------------------------------
def split_statements(text: str) -> list:
    """The statements of an SQL text in fixture.sql's format: full-line comments dropped, each statement ending with
    a semicolon at the end of a line (returned without it)."""
    out, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(current).rstrip()[:-1])
            current = []
    if current:
        raise AssertionError("fixture.sql ends inside a statement")
    return out


def fixture_statements(variant: bool = False) -> list:
    """The base statements of fixture.sql, or with variant the statements after the variant line."""
    text = FIXTURE.read_text(encoding="utf-8").replace("\r\n", "\n")
    base, mark, rest = text.partition("\n" + VARIANT_MARK)
    if not mark:
        raise AssertionError("fixture.sql has no variant section")
    return split_statements(rest.partition("\n")[2] if variant else base)


def in_schema(statements, schema: str) -> list:
    return [s.replace("sxc_rules.", f"{schema}.") for s in statements]


def cell(value):
    """A cell read from PostgreSQL in the form of the hand-written rows: numbers as canonical numerals."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, Decimal)):
        return canon.canon(value, "number")
    raise TypeError(f"unexpected cell type {type(value).__name__}")


def admin(*statements, database: str = DATABASE, role: str = "") -> list:
    """Run statements as the superuser (or, with role, after SET ROLE); the rows of the last statement."""
    a = pgfixture._admin(database)
    try:
        if role:
            a.run(f'SET ROLE "{role}"')
        rows = []
        for statement in statements:
            rows = a.run(statement) or []
        return rows
    finally:
        a.close()


def as_check(*statements, env: str = CHECK_ENV) -> list:
    """Statements run by the check role in autocommit mode; the rows of the last one."""
    conn = db.connect(env)
    try:
        rows = []
        for statement in statements:
            rows = conn.run(statement) or []
        return rows
    finally:
        conn.close()


def source_schema(name: str, database: str = DATABASE, reader: str = CHECK) -> None:
    """A source schema owned by the loader role; the reader gets USAGE and SELECT on every table the owner adds."""
    admin(f'CREATE SCHEMA "{name}" AUTHORIZATION "{OWNER}"', f'GRANT USAGE ON SCHEMA "{name}" TO "{reader}"',
          f'ALTER DEFAULT PRIVILEGES FOR ROLE "{OWNER}" IN SCHEMA "{name}" GRANT SELECT ON TABLES TO "{reader}"',
          database=database)


def output_schema(name: str) -> None:
    admin(f'CREATE SCHEMA "{name}" AUTHORIZATION "{CHECK}"')


def copy_schema(name: str, *changes: str, source: str = "sxc_rules") -> None:
    """A new source schema with copies of the base tables (no constraints), then changes run by the loader role."""
    source_schema(name)
    admin(*[f"CREATE TABLE {name}.{t} AS TABLE {source}.{t}" for t in ("part", "bom_line", "code_value")],
          *changes, role=OWNER)


def ensure_role(name: str) -> None:
    pw = os.environ.get(pgfixture.PASSWORD_ENV)
    a = pgfixture._admin()
    try:
        if not a.run("SELECT 1 FROM pg_roles WHERE rolname = :r", r=name):
            a.run(f'CREATE ROLE "{name}" LOGIN')
        if pw:
            a.run(f"ALTER ROLE \"{name}\" PASSWORD '{pw.replace(chr(39), chr(39) * 2)}'")
    finally:
        a.close()


def cli(*argv) -> tuple:
    """sxc.main with the console output captured."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = sxc.main(list(argv))
    return code, buf.getvalue()


def by_id(result: dict) -> dict:
    return {c["id"]: c for c in result["checks"]}


def strings(value):
    """Every string inside a decoded JSON value (keys included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def without_examples(rules: str) -> str:
    """The rule file text without its [[rule.example]] tables (each one ends where the next rule or comment starts)."""
    out, skipping = [], False
    for line in rules.splitlines():
        if line.strip() == "[[rule.example]]":
            skipping = True
        elif line.startswith(("[[rule]]", "#")):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


CLEAN_RULES = (EXAMPLE / "data-rules.toml").read_text(encoding="utf-8").replace("\r\n", "\n")
VARIANT_RULES = CLEAN_RULES + EXTRA_RULES


class Project:
    """A configuration folder: crosscheck.toml, data-rules.toml and the output directory out/."""

    def __init__(self, folder: Path, schema: str = "", out_schema: str = "", rules: str = CLEAN_RULES,
                 config_text: str = "", **settings):
        folder.mkdir(parents=True)
        self.folder, self.config, self.out = folder, folder / "crosscheck.toml", folder / "out"
        self.schema, self.out_schema = schema, out_schema
        if not config_text:
            config_text = CONFIG.format(schema=schema, out_schema=out_schema, **{**SETTINGS, **settings})
        self.config.write_text(config_text, encoding="utf-8", newline="\n")
        self.write_rules(rules)

    def write_rules(self, text: str) -> None:
        (self.folder / "data-rules.toml").write_text(text, encoding="utf-8", newline="\n")

    @property
    def rules(self) -> str:
        return (self.folder / "data-rules.toml").read_text(encoding="utf-8")

    def stage(self, stage: str, *extra) -> tuple:
        """(exit code, console, result.json) of a stage run through sxc.main."""
        code, console = cli(stage, "--config", str(self.config), "--out", str(self.out), *extra)
        folder = "data_rules" if stage == "data-rules" else stage
        return code, console, json.loads((self.out / folder / "result.json").read_text(encoding="utf-8"))

    def build(self) -> tuple:
        return self.stage("build")

    def data_rules(self, *extra) -> tuple:
        return self.stage("data-rules", *extra)

    def file(self, *parts) -> str:
        return self.out.joinpath(*parts).read_text(encoding="utf-8")

    def loaded(self):
        """(cfg, rule file, type plan) as the stage reads them."""
        cfg = config.load(self.config)
        spec = config.load_data_rules(cfg)
        conn = db.connect(cfg.source.dsn_env)
        try:
            plan, _catalog = sdr.read_type_plan(conn, cfg, spec)
        finally:
            conn.close()
        return cfg, spec, plan


# ---- module fixture -------------------------------------------------------------------------------------------------
def setUpModule():
    if not pgfixture.available():
        return
    fx = pgfixture.PgFixture(DATABASE)
    unittest.addModuleCleanup(fx.teardown)
    fx.setup()
    source_schema("sxc_rules")
    conn = db.connect("SXC_PG_OWNER_DSN")            # the loader role loads the example rows, as its README does
    try:
        with db.write_tx(conn):
            for statement in fixture_statements():
                conn.run(statement)
    finally:
        conn.close()


@unittest.skipUnless(pgfixture.available(), "SXC_TEST_ADMIN_DSN is not set")
class _RulesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ[CHECK_ENV] = pgfixture.dsn_for(CHECK, DATABASE)
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)

    def setUp(self):
        os.environ[CHECK_ENV] = pgfixture.dsn_for(CHECK, DATABASE)

    @classmethod
    def project(cls, name: str, schema: str = "", out_schema: str = "", **kwargs) -> Project:
        return Project(cls.tmp / name, schema, out_schema, **kwargs)

    # ---- assertions ------------------------------------------------------------------------------------------
    def assert_rules(self, result: dict, expected: dict, detail: dict = None, lookups: bool = True):
        """rule:<id> status and counts, rule_xcheck pass with the output counts, rule_example and rule_lookup pass."""
        checks = by_id(result)
        for rule_id, (status, violations, rows) in expected.items():
            with self.subTest(rule=rule_id):
                rule = checks[f"rule:{rule_id}"]
                self.assertEqual((rule["status"], rule["detail"].get("violations"), rule["detail"].get("outputs")),
                                 (status, violations, len(rows)), rule)
                for key, value in (detail or {}).get(rule_id, {}).items():
                    self.assertEqual(rule["detail"][key], value, key)
                xcheck = checks[f"rule_xcheck:{rule_id}"]
                self.assertEqual((xcheck["status"], xcheck["detail"]["sql_outputs"], xcheck["detail"]["py_outputs"]),
                                 ("pass", len(rows), len(rows)), xcheck)
                self.assertEqual(checks[f"rule_example:{rule_id}"]["status"], "pass")
                if lookups:
                    lookup = checks[f"rule_lookup:{rule_id}"]
                    self.assertEqual((lookup["status"], lookup["detail"]["lookup_rows"]), ("pass", len(rows)), lookup)

    def view_rows(self, out_schema: str, rule_id: str) -> list:
        """v_rule_<id> in ord order as hand-written rows; ord must run 1..N."""
        rows = as_check(f"SELECT * FROM {out_schema}.v_rule_{rule_id} ORDER BY ord")
        self.assertEqual([r[-1] for r in rows], list(range(1, len(rows) + 1)), rule_id)
        return [tuple(cell(v) for v in r[:-1]) for r in rows]

    def subject_rows(self, out_schema: str) -> list:
        rows = as_check(f"SELECT subject, severity, severity_rank, rule_count, rules, ord FROM {out_schema}."
                        "v_rules_subjects ORDER BY ord")
        self.assertEqual([r[-1] for r in rows], list(range(1, len(rows) + 1)))
        return [(cell(s), severity, rank, count, tuple(rules)) for s, severity, rank, count, rules, _ord in rows]

    def assert_views(self, out_schema: str, expected: dict, subjects: list):
        for rule_id, (_status, _violations, rows) in expected.items():
            with self.subTest(view=rule_id):
                self.assertEqual(self.view_rows(out_schema, rule_id), rows)
        self.assertEqual(self.subject_rows(out_schema), subjects)

    def assert_not_run(self, result: dict, rule_ids, reason: str):
        checks = by_id(result)
        for rule_id in rule_ids:
            with self.subTest(rule=rule_id):
                self.assertEqual((checks[f"rule:{rule_id}"]["status"], checks[f"rule:{rule_id}"]["detail"]["reason"]),
                                 ("not_run", reason))
                self.assertEqual((checks[f"rule_xcheck:{rule_id}"]["status"],
                                  checks[f"rule_xcheck:{rule_id}"]["detail"]["reason"]), ("not_run", reason))

    def assert_agreed(self, result: dict, rule_ids):
        checks = by_id(result)
        for rule_id in rule_ids:
            with self.subTest(rule=rule_id):
                self.assertEqual(checks[f"rule_xcheck:{rule_id}"]["status"], "pass")
                self.assertIn(checks[f"rule:{rule_id}"]["status"], ("pass", "fail"))


# ---- the fixture file itself (no database) ---------------------------------------------------------------------------
class FixtureFormatTest(unittest.TestCase):
    def test_one_statement_per_line_ending_and_a_variant_of_inserts(self):
        base, variant = fixture_statements(), fixture_statements(variant=True)
        self.assertEqual([s.split()[0] for s in base], ["CREATE"] * 3 + ["INSERT"] * 3)
        self.assertEqual([s.split()[0] for s in variant], ["INSERT"] * 2)
        for statement in base + variant:
            self.assertNotIn(";", statement)
            self.assertIn("sxc_rules.", statement)
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(text.count(";"), len(base) + len(variant))

    def test_the_variant_rules_load(self):
        cfg = config.loads(CONFIG.format(schema="sxc_rules", out_schema="sxc_out", **SETTINGS).encode("utf-8"),
                           Path(tempfile.gettempdir()) / "sxc-data-rules-pg" / "crosscheck.toml")
        spec = data_rules.loads(VARIANT_RULES.encode("utf-8"), cfg)
        self.assertEqual([r.id for r in spec.rules], list(VARIANT))
        self.assertEqual([r.id for r in spec.rules[:len(BASE)]], list(BASE))


# ---- the clean-room example end to end -------------------------------------------------------------------------------
class CleanRoomExampleTest(_RulesCase):
    """examples/data-rules exactly as shipped (crosscheck.toml and data-rules.toml) on the base rows of fixture.sql."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.p = cls.project("clean", "sxc_rules", "sxc_out",
                            config_text=(EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8"))
        cls.build_code, cls.build_console, cls.build = cls.p.build()
        cls.code, cls.console, cls.res = cls.p.data_rules()

    def test_build_creates_and_marks_every_rule_view_and_the_subjects_view(self):
        checks = by_id(self.build)
        self.assertEqual(self.build_code, 0, self.build_console)
        for rule_id in BASE:
            view = checks[f"rule_view:{rule_id}"]
            self.assertEqual((view["status"], view["detail"]["view"]), ("pass", f"sxc_out.v_rule_{rule_id}"))
        self.assertEqual(checks["rule_subjects:view"]["status"], "pass")
        self.assertEqual((checks["closure:has_component"]["status"], checks["closure_apply:has_component"]["status"]),
                         ("pass", "pass"))
        self.assertIn("run `sxc.py data-rules`", self.build["next_safe_action"])
        build_sql = self.p.file("build", "build.sql")
        cfg, spec, plan = self.p.loaded()
        for rule in spec.rules:
            self.assertIn(dsql.view_statement(cfg, spec, plan, rule), build_sql)
        self.assertIn(dsql.subjects_view_statement(cfg, spec, plan), build_sql)
        marked = as_check("SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c JOIN pg_namespace n "
                          "ON n.oid = c.relnamespace WHERE n.nspname = 'sxc_out' AND c.relkind = 'v' ORDER BY 1")
        self.assertEqual(sorted(map(tuple, marked)), sorted([(f"v_rule_{r}", dsql.VIEW_MARKER) for r in BASE]
                                                            + [("v_rules_subjects", dsql.VIEW_MARKER)]))

    def test_every_rule_is_evaluated_on_both_paths_and_decided_by_severity(self):
        self.assertEqual(self.code, 1, self.console)
        self.assert_rules(self.res, BASE, BASE_DETAIL)
        checks = by_id(self.res)
        self.assertEqual(checks["rule_subjects:lookup"]["status"], "pass")
        self.assertEqual(checks["rule_subjects:lookup"]["detail"]["subjects"], len(BASE_SUBJECTS))
        self.assertEqual([c["id"] for c in self.res["checks"]][:2], ["data_rules:snapshot", "data_rules:key:part"])
        self.assertNotIn("scope:rules", checks)
        self.assertIn(sdr.AGREEMENT_NOTE, self.res["not_proven"])
        self.assertIn("v_rules_subjects ORDER BY ord", self.res["next_safe_action"])

    def test_the_snapshot_counted_every_row_and_the_node_key_is_sound(self):
        checks = by_id(self.res)
        snapshot = checks["data_rules:snapshot"]["detail"]
        self.assertEqual({k: snapshot[f"rows_{k}"] for k in BASE_ROWS}, BASE_ROWS)
        self.assertEqual((snapshot["rows_total"], snapshot["limit"]), (sum(BASE_ROWS.values()), 200000))
        key = checks["data_rules:key:part"]
        self.assertEqual((key["status"], key["detail"]["null_keys"], key["detail"]["duplicate_keys"]), ("pass", 0, 0))

    def test_every_lookup_view_holds_the_hand_written_rows_in_ord_order(self):
        self.assert_views("sxc_out", BASE, BASE_SUBJECTS)

    def test_rules_sql_records_every_statement_with_placeholders_only(self):
        rules_sql = self.p.file("data_rules", "rules.sql")
        self.assertTrue(rules_sql.startswith(dsql.RULES_SQL_HEADER))
        for label in ["catalog type plan", "gate encoding", "gate key part", "gate non_finite bom_line",
                      "gate null_quantity bom_line", "gate height bom_line", "gate text_length part", "lookup subjects",
                      "export part", "export bom_line", "export code_value"]:
            self.assertIn(f"-- {label}\n", rules_sql)
        for rule_id in BASE:
            for label in (f"-- lookup {rule_id}\n", f"-- example {rule_id}[0]\n"):
                self.assertIn(label, rules_sql)
            self.assertRegex(rules_sql, rf"-- rule {rule_id} \([a-z_]+\)\n")
        self.assertNotIn("::", rules_sql)
        for constant in CONSTANT_MARKERS:
            self.assertNotIn(constant, rules_sql)
        self.assertLessEqual(set(re.findall(r"'[^']*'", rules_sql)), FIXED_LITERALS)

    def test_row_values_stay_in_the_database(self):
        texts = [self.console, self.build_console, self.p.file("data_rules", "rules.sql"),
                 self.p.file("build", "build.sql"), "\n".join(strings(self.res)), "\n".join(strings(self.build))]
        for text in texts:
            for marker in ROW_MARKERS:
                self.assertNotIn(marker, text)
        self.assertIn(("GHOST-T",), self.view_rows("sxc_out", "parent_unknown"))

    def test_narrowing_with_rule_runs_only_the_named_rules(self):
        p = self.project("clean_narrow", config_text=(EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8"))
        shutil.copytree(self.p.out / "build", p.out / "build")
        code, console, res = p.data_rules("--rule", "twin_assemblies", "--rule", "qty_over_limit")
        self.assertEqual(code, 1, console)
        checks = by_id(res)
        kinds = ("rule", "rule_xcheck", "rule_example", "rule_lookup")
        rule_checks = [c for c in checks if c.partition(":")[0] in kinds]
        self.assertEqual(rule_checks, [f"{kind}:{r}" for r in ("qty_over_limit", "twin_assemblies") for kind in kinds])
        self.assert_rules(res, {r: BASE[r] for r in ("qty_over_limit", "twin_assemblies")}, BASE_DETAIL)
        self.assertEqual((checks["scope:rules"]["status"], checks["scope:rules"]["detail"]["rules_excluded"]),
                         ("not_run", len(BASE) - 2))
        self.assertEqual(checks["rule_subjects:lookup"]["detail"]["reason"], "excluded_by_option")
        rules_sql = p.file("data_rules", "rules.sql")
        self.assertEqual(sorted(re.findall(r"-- rule ([a-z_]+) ", rules_sql)), ["qty_over_limit", "twin_assemblies"])

    def test_report_names_the_separate_stage_and_holds_no_row_values(self):
        code, console = cli("report", "--config", str(self.p.config), "--out", str(self.p.out))
        self.assertEqual(code, 1, console)
        text = "\n".join([console, self.p.file("report.md"),
                          *strings(json.loads(self.p.file("report.json")))])
        self.assertIn(report.DATA_RULES_NOTE, text)
        for marker in ROW_MARKERS:
            self.assertNotIn(marker, text)
        for check_id in ("data_rules:snapshot", "rule_xcheck:", "rule_lookup:"):
            self.assertNotIn(check_id, text)


# ---- the variant, further rules and mutants --------------------------------------------------------------------------
_COLUMN_TEST = dsql._Compiler.column_test
_QUANTIFIED = dsql._Compiler.quantified
_PY_QUANTIFIED = ev._quantified
_PY_COLUMN_TEST = ev._column_test


def m1_endpoint_exists(self, a, s):
    """(m1) endpoint existence without its IS NOT NULL guard: a NULL endpoint counts as one without a node row."""
    n = s.sub("n")
    x = self.endpoint(s.alias, a.end)
    negation = "" if a.exists else "NOT "
    return dsql.Sql(f"({negation}EXISTS (SELECT 1 FROM {self.rel(self.h.node)} {n} WHERE {self.node_key(n)} = {x}))")


def m1_column_test(self, a, alias, entity, s):
    """(m1) not_in_ref without its IS NOT NULL guard: a NULL value counts as one outside the reference."""
    if a.op != "not_in_ref":
        return _COLUMN_TEST(self, a, alias, entity, s)
    x = self.col(alias, entity, a.column)
    ref, r = self.spec.refs[a.ref], s.sub("r")
    conds = [dsql.Sql(f"{self.col(r, ref.entity, ref.column)} = {x}")]
    conds += [dsql.Sql(f"{self.col(r, ref.entity, c)} = ", self.const(v, ref.entity, c)) for c, v in ref.where]
    return dsql.Sql(f"(NOT EXISTS (SELECT 1 FROM {self.rel(ref.entity)} {r} WHERE ", dsql.Sql.join(" AND ", conds),
                    "))")


def m2_quantified(self, a, s):
    """(m2) all without its EXISTS test for at least one neighbour: a node without neighbours satisfies it."""
    if a.quantifier != "all":
        return _QUANTIFIED(self, a, s)
    h, e, n = self.h, s.sub("e"), s.sub("n")
    down = a.direction == "children"
    key = self.node_key(s.alias)
    near = self.col(e, h.edge, h.parent if down else h.child)
    far = self.col(e, h.edge, h.child if down else h.parent)
    sub = self.conjunction(a.where, dsql._Scope(n, h.node, s.depth + 1))
    return dsql.Sql(f"(NOT EXISTS (SELECT 1 FROM {self.rel(h.edge)} {e} WHERE {near} = {key} AND {far} IS NOT NULL "
                    f"AND NOT EXISTS (SELECT 1 FROM {self.rel(h.node)} {n} WHERE {self.node_key(n)} = {far} AND ",
                    sub, ")))")


def m3_endpoint_attribute(self, a, s):
    """(m3) an endpoint attribute as NOT EXISTS of the opposite: a NULL or missing endpoint satisfies it."""
    n = s.sub("n")
    x = self.endpoint(s.alias, a.end)
    test = self.column_test(a, n, self.h.node, s)
    return dsql.Sql(f"(NOT EXISTS (SELECT 1 FROM {self.rel(self.h.node)} {n} WHERE {self.node_key(n)} = {x} AND NOT ",
                    test, "))")


def m4_column_test(self, a, alias, entity, s):
    """(m4) a numeric IN list compared as text: 0.333300 is then no member of (0.3333, 0.6666)."""
    if a.op not in ("in", "not_in") or dsql._type(self.plan, entity, a.column).type_class != "number":
        return _COLUMN_TEST(self, a, alias, entity, s)
    x = self.col(alias, entity, a.column)
    members = dsql.Sql.join(", ", [dsql.Param(str(m), "text") for m in self.spec.sets[a.set]])
    negation = "NOT " if a.op == "not_in" else ""
    return dsql.Sql(f"({x} IS NOT NULL AND CAST({x} AS text) {negation}IN (", members, "))")


def e1_endpoint_exists(endpoint, nodes, exists):
    """(e1) a NULL endpoint treated as an endpoint without a node row."""
    return (endpoint in nodes) == exists


def e2_quantified(quantifier, near, nodes, sub):
    """(e2) all without its test for at least one neighbour."""
    if quantifier == "all":
        return all(n in nodes and sub(nodes[n], n) for n in near)
    return _PY_QUANTIFIED(quantifier, near, nodes, sub)


def e3_column_test(op, value, operand):
    """(e3) a Decimal compared as text (comparisons and IN lists by their text)."""
    if isinstance(value, Decimal) and op in ("in", "not_in"):
        return (str(value) in {str(m) for m in operand}) == (op == "in")
    if isinstance(value, Decimal) and op in ("eq", "ne", "lt", "le", "gt", "ge"):
        return _PY_COLUMN_TEST(op, str(value), str(operand))
    return _PY_COLUMN_TEST(op, value, operand)


SQL_MUTANTS = {   # name: (patches on data_rules_sql._Compiler, rules whose cross-check must fail)
    "m1": ({"endpoint_exists": m1_endpoint_exists, "column_test": m1_column_test},
           ("child_unknown_line", "procure_not_listed")),
    "m2": ({"quantified": m2_quantified}, ("all_children_retired", "quantifier_mix")),
    "m3": ({"endpoint_attribute": m3_endpoint_attribute}, ("retired_child_in_active_parent", "truncated_third")),
    "m4": ({"column_test": m4_column_test}, ("truncated_third",)),
}
PY_MUTANTS = {    # name: (attribute of data_rules_eval, replacement, rules whose cross-check must fail)
    "e1": ("_endpoint_exists", e1_endpoint_exists, ("child_unknown_line",)),
    "e2": ("_quantified", e2_quantified, ("all_children_retired", "quantifier_mix")),
    "e3": ("_column_test", e3_column_test, ("qty_over_limit", "truncated_third")),
}


class VariantTest(_RulesCase):
    """The variant rows (NULL endpoints, missing references) with the clean-room rules plus the further rules."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source_schema("sxc_variant")
        output_schema("sxc_out_variant")
        admin(*in_schema(fixture_statements() + fixture_statements(variant=True), "sxc_variant"), role=OWNER)
        cls.p = cls.project("variant", "sxc_variant", "sxc_out_variant", rules=VARIANT_RULES)
        cls.build_code, cls.build_console, cls.build = cls.p.build()
        cls.code, cls.console, cls.res = cls.p.data_rules()

    def test_both_paths_agree_with_the_hand_written_outputs(self):
        self.assertEqual(self.build_code, 0, self.build_console)
        self.assertEqual(self.code, 1, self.console)
        self.assert_rules(self.res, VARIANT, VARIANT_DETAIL)
        snapshot = by_id(self.res)["data_rules:snapshot"]["detail"]
        self.assertEqual({k: snapshot[f"rows_{k}"] for k in VARIANT_ROWS}, VARIANT_ROWS)
        self.assertEqual([(c["id"], c["status"]) for c in self.res["checks"]][1:4],
                         [("data_rules:key:part", "pass"), ("data_rules:key:bom_line", "pass"),
                          ("data_rules:key:code_value", "pass")])
        self.assertEqual(by_id(self.res)["rule_subjects:lookup"]["status"], "pass")
        self.assert_views("sxc_out_variant", VARIANT, VARIANT_SUBJECTS)

    def assert_disagree(self, res: dict, rule_ids, sql_mutant: bool):
        checks = by_id(res)
        for rule_id in rule_ids:
            with self.subTest(rule=rule_id):
                xcheck = checks[f"rule_xcheck:{rule_id}"]
                self.assertEqual(xcheck["status"], "fail", xcheck)
                self.assertGreater(xcheck["detail"]["sql_only"] + xcheck["detail"]["py_only"], 0)
                self.assertEqual(checks[f"rule:{rule_id}"]["detail"]["reason"], "paths_disagree")
                lookup = checks[f"rule_lookup:{rule_id}"]["detail"]["reason"]
                self.assertEqual(lookup, "view_stale" if sql_mutant else "rule_not_agreed")
        self.assertIn("evaluations disagree on", res["next_safe_action"])

    def test_sql_mutants_make_the_cross_check_fail(self):
        for name, (patches, rule_ids) in SQL_MUTANTS.items():
            with self.subTest(mutant=name):
                with contextlib.ExitStack() as stack:
                    for attribute, replacement in patches.items():
                        stack.enter_context(mock.patch.object(dsql._Compiler, attribute, replacement))
                    code, console, res = self.p.data_rules()
                self.assertEqual(code, 1, console)
                self.assert_disagree(res, rule_ids, sql_mutant=True)
        checks = by_id(self.p.data_rules()[2])   # unpatched again: every rule agrees
        self.assertTrue(all(checks[f"rule_xcheck:{r}"]["status"] == "pass" for r in VARIANT))

    def test_an_sql_mutant_shows_in_the_authored_example(self):
        with mock.patch.object(dsql._Compiler, "quantified", m2_quantified):
            _code, _console, res = self.p.data_rules()
        example = by_id(res)["rule_example:all_children_retired"]
        self.assertEqual((example["status"], example["detail"]["sql_mismatch"], example["detail"]["common_mode"]),
                         ("fail", 1, 0))

    def test_python_mutants_make_the_cross_check_fail(self):
        for name, (attribute, replacement, rule_ids) in PY_MUTANTS.items():
            with self.subTest(mutant=name):
                with mock.patch.object(ev, attribute, replacement):
                    code, console, res = self.p.data_rules()
                self.assertEqual(code, 1, console)
                self.assert_disagree(res, rule_ids, sql_mutant=False)


# ---- the same rows under other column types ---------------------------------------------------------------------
BPCHAR_DDL = [
    "CREATE TABLE sxc_bpchar.part (part_id char(10) PRIMARY KEY, name text, unit text, part_class text, procure text, "
    "issue text, retired boolean, stored_level integer)",
    "CREATE TABLE sxc_bpchar.bom_line (line_id integer PRIMARY KEY, parent_id char(10), child_id char(10), "
    "qty_per numeric(18,6))",
    "CREATE TABLE sxc_bpchar.code_value (code_group char(8), code char(6), PRIMARY KEY (code_group, code))",
]
ICU = 'COLLATE "und-x-icu"'
ICU_DDL = [
    f"CREATE TABLE sxc_icu.part (part_id text {ICU} PRIMARY KEY, name text {ICU}, unit text {ICU}, "
    f"part_class text {ICU}, procure text {ICU}, issue text {ICU}, retired boolean, stored_level integer)",
    f"CREATE TABLE sxc_icu.bom_line (line_id integer PRIMARY KEY, parent_id text {ICU}, child_id text {ICU}, "
    "qty_per numeric(18,6))",
    f"CREATE TABLE sxc_icu.code_value (code_group text {ICU}, code text {ICU}, PRIMARY KEY (code_group, code))",
]
NONDETERMINISTIC_DDL = [
    "CREATE COLLATION sxc_nondet.case_blind (provider = icu, locale = 'und-u-ks-level2', deterministic = false)",
    "CREATE TABLE sxc_nondet.part (part_id text PRIMARY KEY, name text COLLATE sxc_nondet.case_blind, unit text, "
    "part_class text, procure text, issue text, retired boolean, stored_level integer)",
    "CREATE TABLE sxc_nondet.bom_line (line_id integer PRIMARY KEY, parent_id text, child_id text, "
    "qty_per numeric(18,6))",
    "CREATE TABLE sxc_nondet.code_value (code_group text, code text, PRIMARY KEY (code_group, code))",
]
VARCHAR_DDL = [   # varchar keys of two lengths, a text child column, a smallint stored level and a bigint line id
    "CREATE TABLE sxc_varchar.part (part_id varchar(12) PRIMARY KEY, name varchar(40), unit varchar(8), "
    "part_class text, procure varchar(8), issue text, retired boolean, stored_level smallint)",
    "CREATE TABLE sxc_varchar.bom_line (line_id bigint PRIMARY KEY, parent_id varchar(20), child_id text, "
    "qty_per numeric(12,6))",
    "CREATE TABLE sxc_varchar.code_value (code_group varchar(16), code varchar(8), PRIMARY KEY (code_group, code))",
]
UNSUPPORTED_DDL = [   # a domain over text and a floating-point column: types the rules do not read
    "CREATE DOMAIN sxc_types.unit_code AS text",
    "CREATE TABLE sxc_types.part (part_id text PRIMARY KEY, name text, unit sxc_types.unit_code, part_class text, "
    "procure text, issue text, retired boolean, stored_level double precision)",
    "CREATE TABLE sxc_types.bom_line (line_id integer PRIMARY KEY, parent_id text, child_id text, "
    "qty_per numeric(18,6))",
    "CREATE TABLE sxc_types.code_value (code_group text, code text, PRIMARY KEY (code_group, code))",
]
UNIT_OR_LEVEL_RULES = ("truncated_third", "twin_assemblies", "stored_level_drift", "grams_in_count_unit")
MIXED_DDL = [     # the node key and the edge endpoints in two different deterministic collations
    'CREATE TABLE sxc_mixed.part (part_id text COLLATE "C" PRIMARY KEY, name text, unit text, part_class text, '
    "procure text, issue text, retired boolean, stored_level integer)",
    f"CREATE TABLE sxc_mixed.bom_line (line_id integer PRIMARY KEY, parent_id text {ICU}, child_id text {ICU}, "
    "qty_per numeric(18,6))",
    "CREATE TABLE sxc_mixed.code_value (code_group text, code text, PRIMARY KEY (code_group, code))",
]
MIXED_ENDS_DDL = [   # the hierarchy parent and child columns in two different deterministic collations
    "CREATE TABLE sxc_mixends.part (part_id text PRIMARY KEY, name text, unit text, part_class text, procure text, "
    "issue text, retired boolean, stored_level integer)",
    'CREATE TABLE sxc_mixends.bom_line (line_id integer PRIMARY KEY, parent_id text COLLATE "C", child_id text '
    'COLLATE "POSIX", qty_per numeric(18,6))',
    "CREATE TABLE sxc_mixends.code_value (code_group text, code text, PRIMARY KEY (code_group, code))",
]
NUMERIC_DDL = [   # integer node keys, a bigint parent column and an integer child column, numbered in code-point order
    "CREATE TABLE sxc_numeric.key_map (k text PRIMARY KEY, n integer NOT NULL)",
    "INSERT INTO sxc_numeric.key_map SELECT s.k, row_number() OVER (ORDER BY s.k COLLATE \"C\") FROM (SELECT part_id "
    "FROM sxc_rules.part UNION SELECT parent_id FROM sxc_rules.bom_line UNION SELECT child_id FROM sxc_rules.bom_line) "
    "s(k) WHERE s.k IS NOT NULL",
    "CREATE TABLE sxc_numeric.part (part_id integer PRIMARY KEY, name text, unit text, part_class text, procure text, "
    "issue text, retired boolean, stored_level integer)",
    "INSERT INTO sxc_numeric.part SELECT m.n, p.name, p.unit, p.part_class, p.procure, p.issue, p.retired, "
    "p.stored_level FROM sxc_rules.part p JOIN sxc_numeric.key_map m ON m.k = p.part_id",
    "CREATE TABLE sxc_numeric.bom_line (line_id integer PRIMARY KEY, parent_id bigint, child_id integer, "
    "qty_per numeric(18,6))",
    "INSERT INTO sxc_numeric.bom_line SELECT b.line_id, mp.n, mc.n, b.qty_per FROM sxc_rules.bom_line b LEFT JOIN "
    "sxc_numeric.key_map mp ON mp.k = b.parent_id LEFT JOIN sxc_numeric.key_map mc ON mc.k = b.child_id",
    "CREATE TABLE sxc_numeric.code_value AS TABLE sxc_rules.code_value",
]


def view_query_without_collate_c(cfg, spec, plan, rule):
    """A mutant of the lookup views' ord: the column collations instead of COLLATE "C" (the rule itself unchanged)."""
    order, mark, rest = _VIEW_QUERY(cfg, spec, plan, rule).partition(f" AS {dsql.ORD} FROM (")
    return order.replace(' COLLATE "C"', "") + mark + rest


def same_signature_without_collate_c(self, rule):
    """A mutant of same_signature: group_id as the smallest member in the column's collation, not by code point."""
    sql = _SAME_SIGNATURE(self, rule)
    return dsql.Sql(*[p.replace(' COLLATE "C"', "") if isinstance(p, str) else p for p in sql.parts])


_VIEW_QUERY = dsql.view_query
_SAME_SIGNATURE = dsql._Compiler.kind_same_signature


class ColumnTypesTest(_RulesCase):
    """char(n) keys with pad spaces, integer keys, deterministic ICU columns and a nondeterministic collation."""

    def test_char_keys_with_pad_spaces_agree_and_their_lookups_pass(self):
        source_schema("sxc_bpchar")
        output_schema("sxc_out_bpchar")
        admin(*BPCHAR_DDL, *in_schema(fixture_statements()[3:], "sxc_bpchar"), role=OWNER)
        self.assertEqual(admin("SELECT octet_length(part_id) FROM sxc_bpchar.part WHERE part_id = 'K1'"), [[10]])
        p = self.project("bpchar", "sxc_bpchar", "sxc_out_bpchar")
        code, console, build = p.build()
        self.assertEqual(code, 0, console)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        self.assert_rules(res, BASE, BASE_DETAIL)
        self.assertEqual(by_id(res)["rule_subjects:lookup"]["status"], "pass")
        self.assert_views("sxc_out_bpchar", BASE, BASE_SUBJECTS)

    def test_varchar_keys_of_two_lengths_and_a_smallint_level_agree(self):
        source_schema("sxc_varchar")
        output_schema("sxc_out_varchar")
        admin(*VARCHAR_DDL, *in_schema(fixture_statements()[3:], "sxc_varchar"), role=OWNER)
        p = self.project("varchar", "sxc_varchar", "sxc_out_varchar")
        code, console, _build = p.build()
        self.assertEqual(code, 0, console)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        self.assert_rules(res, BASE, BASE_DETAIL)
        self.assert_views("sxc_out_varchar", BASE, BASE_SUBJECTS)

    def test_a_domain_or_a_float_column_leaves_the_rules_reading_it_not_run(self):
        source_schema("sxc_types")
        admin(*UNSUPPORTED_DDL, *in_schema(fixture_statements()[3:], "sxc_types"), role=OWNER)
        code, console, res = self.project("types", "sxc_types", "sxc_types_out", views="false").data_rules()
        self.assertEqual(code, 1, console)
        self.assert_not_run(res, UNIT_OR_LEVEL_RULES, "column_type")
        checks = by_id(res)
        for rule_id in UNIT_OR_LEVEL_RULES:
            example = checks[f"rule_example:{rule_id}"]
            self.assertEqual((example["status"], example["detail"]["gate"]), ("not_run", "column_type"))
        self.assert_rules(res, {k: v for k, v in BASE.items() if k not in UNIT_OR_LEVEL_RULES}, BASE_DETAIL,
                          lookups=False)
        self.assertIn("cast the column in a database view", res["next_safe_action"])

    def test_an_integer_quantity_column_agrees(self):
        """Quantities rounded into an integer column: sums are bigint in SQL (trim_scale reads them as numeric) and
        int in Python. The authored examples hold fractions such a column cannot store, so this run has none."""
        copy_schema("sxc_intqty", "ALTER TABLE sxc_intqty.bom_line ALTER COLUMN qty_per TYPE integer USING "
                                  "round(qty_per)")
        code, console, res = self.project("intqty", "sxc_intqty", "sxc_intqty_out", views="false",
                                          rules=without_examples(CLEAN_RULES), require_examples="false").data_rules()
        self.assertEqual(code, 1, console)
        self.assert_agreed(res, BASE)
        twins = by_id(res)["rule:twin_assemblies"]["detail"]
        self.assertEqual((twins["violations"], twins["groups"]), (4, 2))   # 1 + 1 against 2, as with decimals

    def test_columns_of_two_collations_are_an_sql_error_never_a_verdict(self):
        """Documents the current fail-closed behaviour: comparing the node key with an endpoint of another collation
        is SQLSTATE 42P22 in PostgreSQL, so those rules end as errors without a verdict (spec section 8), and
        next_safe_action names the collation mismatch instead of the generic privilege hint."""
        source_schema("sxc_mixed")
        admin(*MIXED_DDL, *in_schema(fixture_statements()[3:], "sxc_mixed"), role=OWNER)
        code, console, res = self.project("mixed", "sxc_mixed", "sxc_mixed_out", views="false").data_rules()
        self.assertEqual(code, 1, console)
        checks = by_id(res)
        compared = [r for r in NODE_KEY_RULES if r != "temporary_name"]
        for rule_id in compared:
            with self.subTest(rule=rule_id):
                self.assertEqual((checks[f"rule:{rule_id}"]["status"], checks[f"rule:{rule_id}"]["detail"]["sqlstate"]),
                                 ("error", "42P22"))
                self.assertEqual(checks[f"rule_xcheck:{rule_id}"]["detail"]["reason"], "path_error")
        self.assert_agreed(res, ["temporary_name", "qty_over_limit", "repeated_line", "odd_quantity"])
        self.assertIn("SQLSTATE 42P22", res["next_safe_action"])
        self.assertIn("text columns of different collations", res["next_safe_action"])
        self.assertNotIn("the check role's privileges", res["next_safe_action"])

    def test_parent_and_child_columns_of_two_collations_stop_the_snapshot(self):
        """The height gate of stored_metric follows parent and child in a recursive query: two different deterministic
        collations there are SQLSTATE 42P21 while the gate is parsed, outside the rule savepoints, so the snapshot is
        an error and no rule is decided (fail-closed), with the collation hint."""
        source_schema("sxc_mixends")
        admin(*MIXED_ENDS_DDL, *in_schema(fixture_statements()[3:], "sxc_mixends"), role=OWNER)
        code, console, res = self.project("mixends", "sxc_mixends", "sxc_mixends_out", views="false").data_rules()
        self.assertEqual(code, 1, console)
        checks = by_id(res)
        snapshot = checks["data_rules:snapshot"]
        self.assertEqual((snapshot["status"], snapshot["detail"]["sqlstate"]), ("error", "42P21"))
        for rule_id in BASE:
            with self.subTest(rule=rule_id):
                self.assertEqual((checks[f"rule:{rule_id}"]["status"], checks[f"rule:{rule_id}"]["detail"]["reason"]),
                                 ("not_run", "snapshot_not_read"))
        self.assertIn("data_rules:snapshot hit a database error (SQLSTATE 42P21)", res["next_safe_action"])
        self.assertIn("text columns of different collations", res["next_safe_action"])

    def test_integer_keys_of_two_integer_types_agree_and_keep_numeric_order(self):
        source_schema("sxc_numeric")
        output_schema("sxc_out_numeric")
        admin(*NUMERIC_DDL, role=OWNER)
        number = {k: str(n) for k, n in admin("SELECT k, n FROM sxc_numeric.key_map")}
        p = self.project("numeric", "sxc_numeric", "sxc_out_numeric", rules=without_examples(CLEAN_RULES),
                         require_examples="false")
        code, console, build = p.build()
        self.assertEqual(by_id(build)["rule_subjects:view"]["status"], "pass", console)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        _cfg, spec, _plan = p.loaded()
        expected = {}
        for rule_id, (status, violations, rows) in BASE.items():
            keys = [i for i, name in enumerate(spec.rule(rule_id).columns) if name in KEY_NAMES]
            mapped = [tuple(number[v] if i in keys and v is not None else v for i, v in enumerate(row)) for row in rows]
            expected[rule_id] = (status, violations, mapped)
        checks = by_id(res)
        for rule_id, (status, violations, rows) in expected.items():
            with self.subTest(rule=rule_id):
                rule, xcheck = checks[f"rule:{rule_id}"], checks[f"rule_xcheck:{rule_id}"]
                self.assertEqual((rule["status"], rule["detail"]["violations"], xcheck["status"],
                                  checks[f"rule_lookup:{rule_id}"]["status"]), (status, violations, "pass", "pass"))
                self.assertNotIn(f"rule_example:{rule_id}", checks)
                self.assertEqual(self.view_rows("sxc_out_numeric", rule_id), rows)
        subjects = [(number[s], *rest) for s, *rest in BASE_SUBJECTS]
        self.assertEqual(self.subject_rows("sxc_out_numeric"), subjects)

    def test_deterministic_icu_columns_keep_code_point_order(self):
        source_schema("sxc_icu")
        output_schema("sxc_out_icu")
        admin(*ICU_DDL, *in_schema(fixture_statements()[3:], "sxc_icu"), role=OWNER)
        icu_order = admin("SELECT array_agg(part_id ORDER BY part_id) FROM sxc_icu.part WHERE part_id IN "
                          "('UP1', 'UP7', 'Up2', 'up3')")[0][0]
        self.assertEqual(icu_order, ["UP1", "Up2", "up3", "UP7"])    # the columns' own order is not code-point order
        p = self.project("icu", "sxc_icu", "sxc_out_icu")
        code, console, build = p.build()
        self.assertEqual(code, 0, console)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        self.assert_rules(res, BASE, BASE_DETAIL)
        self.assert_views("sxc_out_icu", BASE, BASE_SUBJECTS)
        with mock.patch.object(dsql, "view_query", view_query_without_collate_c):
            code, console, build = p.build()
            self.assertEqual(code, 0, console)
            _code, _console, res = p.data_rules()
        checks = by_id(res)
        for rule_id in ("unknown_procure_code", "twin_assemblies"):
            lookup = checks[f"rule_lookup:{rule_id}"]
            self.assertEqual((lookup["status"], lookup["detail"].get("order_differs")), ("fail", True), lookup)
        self.assertEqual(checks["rule_lookup:repeated_line"]["status"], "pass")
        self.assertEqual(p.build()[0], 0)
        with mock.patch.object(dsql._Compiler, "kind_same_signature", same_signature_without_collate_c):
            _code, _console, res = p.data_rules()
        xcheck = by_id(res)["rule_xcheck:twin_assemblies"]
        self.assertEqual((xcheck["status"], xcheck["detail"]["sql_only"], xcheck["detail"]["py_only"]), ("fail", 2, 2))

    def test_a_nondeterministic_collation_leaves_the_rules_reading_it_not_run(self):
        source_schema("sxc_nondet")
        output_schema("sxc_out_nondet")
        admin(*NONDETERMINISTIC_DDL, *in_schema(fixture_statements()[3:], "sxc_nondet"), role=OWNER)
        p = self.project("nondet", "sxc_nondet", "sxc_out_nondet")
        code, console, build = p.build()
        self.assertEqual(code, 1, console)
        checks = by_id(build)
        for rule_id in BASE:
            status = checks[f"rule_view:{rule_id}"]
            self.assertEqual((status["status"], status["detail"].get("reason")),
                             ("not_run", "collation") if rule_id in NAME_RULES else ("pass", None))
        self.assertEqual((checks["rule_subjects:view"]["status"], checks["rule_subjects:view"]["detail"]["reason"]),
                         ("not_run", "rule_view_missing"))
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        self.assert_not_run(res, NAME_RULES, "collation")
        checks = by_id(res)
        for rule_id in NAME_RULES:
            example, lookup = checks[f"rule_example:{rule_id}"], checks[f"rule_lookup:{rule_id}"]
            self.assertEqual((example["status"], example["detail"]["reason"], example["detail"]["gate"]),
                             ("not_run", "example_not_evaluable", "collation"))
            self.assertEqual((lookup["status"], lookup["detail"]["reason"]), ("not_run", "view_not_built"))
        self.assert_rules(res, {k: v for k, v in BASE.items() if k not in NAME_RULES}, BASE_DETAIL)
        self.assertEqual(checks["rule_subjects:lookup"]["detail"]["reason"], "view_not_built")


# ---- gates and budgets -----------------------------------------------------------------------------------------------
class Recording:
    """A connection that records the text of every statement it runs."""

    def __init__(self, conn):
        self._conn, self.sql = conn, []

    def run(self, sql, **params):
        self.sql.append(sql)
        return self._conn.run(sql, **params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class GatesAndBudgetsTest(_RulesCase):
    """Data-dependent gates and budgets leave rules not_run, never pass (views = false: no build is needed)."""

    def gate_run(self, name: str, *changes: str, exit_code: int = 1, **settings) -> dict:
        copy_schema(name, *changes)
        code, console, res = self.project(name, name, name + "_out", views="false", **settings).data_rules()
        self.assertEqual(code, exit_code, console)
        self.assertEqual(by_id(res)["data_rules:snapshot"]["status"], "pass")
        self.assertIn(sdr.VIEWS_OFF_NOTE, res["not_proven"])
        return res

    def test_an_entity_pointed_at_a_view_that_clears_nan_runs_every_rule(self):
        """The remedy next_safe_action names: a database view that maps NaN to NULL (line 105 holds NULL already)."""
        copy_schema("sxc_gate_nanv", "UPDATE sxc_gate_nanv.bom_line SET qty_per = 'NaN' WHERE line_id = 105")
        source_schema("sxc_gate_view")
        admin("CREATE VIEW sxc_gate_view.part AS TABLE sxc_gate_nanv.part",
              "CREATE VIEW sxc_gate_view.bom_line AS SELECT line_id, parent_id, child_id, NULLIF(qty_per, 'NaN') AS "
              "qty_per FROM sxc_gate_nanv.bom_line",
              "CREATE VIEW sxc_gate_view.code_value AS TABLE sxc_gate_nanv.code_value", role=OWNER)
        code, console, res = self.project("viewed", "sxc_gate_view", "sxc_gate_view_out", views="false").data_rules()
        self.assertEqual(code, 1, console)
        self.assert_rules(res, BASE, BASE_DETAIL, lookups=False)

    def test_nan_and_infinity_stop_the_rules_that_read_the_column(self):
        nan = self.gate_run("sxc_gate_nan", "UPDATE sxc_gate_nan.bom_line SET qty_per = 'NaN' WHERE line_id = 105")
        infinity = self.gate_run("sxc_gate_inf", "ALTER TABLE sxc_gate_inf.bom_line ALTER COLUMN qty_per TYPE numeric",
                                 "UPDATE sxc_gate_inf.bom_line SET qty_per = '-Infinity' WHERE line_id = 1201")
        for res in (nan, infinity):
            self.assert_not_run(res, QTY_RULES, "non_finite_values")
            self.assert_agreed(res, [r for r in BASE if r not in QTY_RULES])
            self.assertIn("cast the column in a database view", res["next_safe_action"])

    def test_null_and_duplicate_node_keys_fail_the_key_gate(self):
        for name, change, reason, counts in (
                ("sxc_gate_dupkey", "INSERT INTO sxc_gate_dupkey.part SELECT * FROM sxc_gate_dupkey.part WHERE "
                 "part_id = 'KC'", "duplicate_keys", (0, 1)),
                ("sxc_gate_nullkey", "INSERT INTO sxc_gate_nullkey.part (part_id, name) VALUES (NULL, 'Nameless')",
                 "null_keys", (1, 0))):
            with self.subTest(gate=reason):
                res = self.gate_run(name, change)
                key = by_id(res)["data_rules:key:part"]
                detail = key["detail"]
                self.assertEqual((key["status"], detail["reason"], detail["null_keys"], detail["duplicate_keys"]),
                                 ("fail", reason, *counts))
                self.assert_not_run(res, NODE_KEY_RULES, "key_not_unique" if counts[1] else "key_null")
                self.assert_agreed(res, ["qty_over_limit", "repeated_line", "odd_quantity"])

    def test_a_cycle_or_a_shallow_max_depth_stops_the_height_rule(self):
        cycle = self.gate_run("sxc_gate_cycle", "INSERT INTO sxc_gate_cycle.bom_line VALUES (1990, 'H0', 'H3', 1)")
        code, console, shallow = self.project("shallow", "sxc_rules", "sxc_out_shallow", views="false",
                                              max_depth=2).data_rules()
        self.assertEqual(code, 1, console)
        for res in (cycle, shallow):
            self.assert_not_run(res, ["stored_level_drift"], "cycle_or_depth_limit")
            self.assert_agreed(res, [r for r in BASE if r != "stored_level_drift"])
            self.assertIn("see build's closure checks for cycles", res["next_safe_action"])
        code, console, exact = self.project("exact_depth", "sxc_rules", "sxc_out_exact", views="false",
                                            max_depth=3).data_rules()   # the longest chain has 3 edges
        self.assertEqual(code, 1, console)
        self.assert_rules(exact, {"stored_level_drift": BASE["stored_level_drift"]}, BASE_DETAIL, lookups=False)

    def test_a_null_edge_quantity_and_a_long_text_stop_their_kinds(self):
        nulls = self.gate_run("sxc_gate_nullqty",
                              "UPDATE sxc_gate_nullqty.bom_line SET qty_per = NULL WHERE line_id = 1001")
        self.assert_not_run(nulls, ["twin_assemblies"], "null_values")
        self.assert_agreed(nulls, [r for r in BASE if r != "twin_assemblies"])
        long = self.gate_run("sxc_gate_long", "UPDATE sxc_gate_long.part SET name = repeat('x', 4097) "
                                              "WHERE part_id = 'TN6'")
        self.assert_not_run(long, ["grams_in_count_unit"], "value_too_long")
        self.assert_agreed(long, [r for r in BASE if r != "grams_in_count_unit"])
        fits = self.gate_run("sxc_gate_fits", "UPDATE sxc_gate_fits.part SET name = repeat('x', 4096) "
                                              "WHERE part_id = 'TN6'")
        self.assert_agreed(fits, BASE)

    def test_an_empty_edge_table_gives_vacuous_agreements(self):
        res = self.gate_run("sxc_gate_empty", "DELETE FROM sxc_gate_empty.bom_line", exit_code=0)
        self.assertEqual(by_id(res)["data_rules:snapshot"]["detail"]["rows_bom_line"], 0)
        self.assert_agreed(res, BASE)
        self.assertEqual({c["status"] for c in res["checks"]}, {"pass"})   # no violation rule finds a row
        line_rules = [r for r in BASE if r in ("qty_over_limit", "parent_unknown", "retired_child_in_active_parent",
                                               "truncated_third", "repeated_line", "same_name_components",
                                               "odd_quantity", "grams_in_count_unit")]
        for rule_id in line_rules:
            self.assertIn(f"rule:{rule_id}: entity bom_line has 0 rows, so the agreement is vacuous", res["not_proven"])
        drift = by_id(res)["rule:stored_level_drift"]["detail"]     # without edges every stored level above 0 drifts
        self.assertEqual((drift["violations"], drift["outside_stored_nonzero"]), (6, 6))
        self.assertEqual(by_id(res)["rule:temporary_name"]["detail"]["violations"], 1)

    def recorded_run(self, p: Project) -> tuple:
        recordings = []
        connect = db.connect

        def recording_connect(*args, **kwargs):
            recordings.append(Recording(connect(*args, **kwargs)))
            return recordings[-1]

        with mock.patch.object(db, "connect", recording_connect):
            code, console, res = p.data_rules()
        return code, console, res, [sql for r in recordings for sql in r.sql]

    def test_an_export_budget_overflow_runs_no_rule_lookup_or_export_statement(self):
        p = self.project("budget", "sxc_rules", "sxc_out_budget", views="false", max_export_rows=100)
        code, console, res, executed = self.recorded_run(p)
        self.assertEqual(code, 1, console)
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot["status"], snapshot["detail"]["reason"], snapshot["detail"]["rows_part"],
                          snapshot["detail"]["rows_bom_line"], snapshot["detail"]["rows_code_value"],
                          snapshot["detail"]["rows_total"]), ("not_run", "resource_limit", 101, 101, 4, 206))
        self.assert_not_run(res, BASE, "resource_limit")
        checks = by_id(res)
        self.assertNotIn("data_rules:key:part", checks)
        self.assertTrue(all(checks[f"rule_example:{r}"]["status"] == "pass" for r in BASE))
        cfg, spec, plan = p.loaded()
        prep = sdr.prepare(cfg, spec, plan, spec.rules)
        texts = {name: {st.parameterized()[0] for st in group} for name, group in (
            ("rule", prep.rules.values()), ("export", [st for _cols, st in prep.exports.values()]),
            ("gate", [g.statement for g in prep.gates if g.gate not in ("encoding", "budget")]),
            ("budget", [g.statement for g in prep.gates if g.gate == "budget"]))}
        self.assertLessEqual(texts["budget"], set(executed))
        for name in ("rule", "export", "gate"):
            self.assertFalse(texts[name] & set(executed), name)
        rules_sql = p.file("data_rules", "rules.sql")
        self.assertNotIn("-- export ", rules_sql)
        self.assertNotIn("-- rule ", rules_sql)
        self.assertIn("so no rule, lookup or export statement ran", " ".join(res["not_proven"]))

    def test_a_rule_row_budget_stops_only_the_rules_above_it(self):
        p = self.project("rule_budget", "sxc_rules", "sxc_out_rule_budget", views="false", max_rule_rows=2)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        over = [r for r, (_s, _v, rows) in BASE.items() if len(rows) > 2]
        self.assertEqual(over, ["qty_over_limit", "retired_child_in_active_parent", "unknown_procure_code",
                                "repeated_line", "twin_assemblies", "stored_level_drift", "grams_in_count_unit"])
        self.assert_not_run(res, over, "resource_limit")
        self.assert_rules(res, {r: v for r, v in BASE.items() if r not in over}, lookups=False)
        self.assertIn("data rules not evaluated (resource_limit: more than data_rules.max_rule_rows = 2 output rows): "
                      + ", ".join(over), res["not_proven"])


# ---- one snapshot ----------------------------------------------------------------------------------------------------
class SnapshotTest(_RulesCase):
    """REPEATABLE READ against a concurrent commit, and savepoints around a failing rule and a timeout."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        copy_schema("sxc_snap")
        output_schema("sxc_out_snap")
        cls.p = cls.project("snap", "sxc_snap", "sxc_out_snap")
        code, console, _build = cls.p.build()
        if code != 0:
            raise AssertionError(console)

    def committed_meanwhile(self, isolation_kept: bool = True) -> dict:
        """data-rules with one bom_line row committed by another session after the rules ran and before the lookups
        and the export; isolation_kept False turns the snapshot into READ COMMITTED."""
        lookups, read_only = sdr._lookups, db.read_only

        def lookups_after_a_commit(*args, **kwargs):
            admin("INSERT INTO sxc_snap.bom_line VALUES (9001, 'QR', 'QD', 9999)", role=OWNER)
            return lookups(*args, **kwargs)

        def read_committed(conn, timeout_ms=60000, isolation=None):
            return read_only(conn, timeout_ms)

        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sdr, "_lookups", lookups_after_a_commit))
                if not isolation_kept:
                    stack.enter_context(mock.patch.object(db, "read_only", read_committed))
                code, console, res = self.p.data_rules()
        finally:
            admin("DELETE FROM sxc_snap.bom_line WHERE line_id = 9001", role=OWNER)
        self.assertEqual(code, 1, console)
        return res

    def test_a_concurrent_commit_stays_invisible_to_the_snapshot(self):
        res = self.committed_meanwhile()
        self.assert_rules(res, BASE, BASE_DETAIL)
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot["status"], snapshot["detail"]["rows_bom_line"]), ("pass", BASE_ROWS["bom_line"]))
        self.assertEqual(by_id(res)["rule_subjects:lookup"]["status"], "pass")

    def test_read_committed_would_mix_two_data_versions(self):
        res = self.committed_meanwhile(isolation_kept=False)
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot["status"], snapshot["detail"]["reason"], snapshot["detail"]["entity"]),
                         ("error", "export_incomplete", "bom_line"))
        self.assertEqual(by_id(res)["rule:qty_over_limit"]["detail"]["reason"], "snapshot_not_read")

    def replaced_rule(self, rule_id: str, sql: str, project: Project = None) -> dict:
        prepare = sdr.prepare

        def prepare_with_a_replaced_rule(cfg, spec, plan, selected):
            prep = prepare(cfg, spec, plan, selected)
            prep.rules[rule_id] = dsql.Statement(dsql.Sql(sql), prep.rules[rule_id].label)
            return prep

        with mock.patch.object(sdr, "prepare", prepare_with_a_replaced_rule):
            code, console, res = (project or self.p).data_rules()
        self.assertEqual(code, 1, console)
        return res

    def test_a_failing_rule_is_isolated_by_its_savepoint(self):
        res = self.replaced_rule("temporary_name", "SELECT CAST(1 AS bigint) / (SELECT count(*) FROM sxc_snap.part "
                                                   "WHERE false) AS part_id")
        checks = by_id(res)
        self.assertEqual((checks["rule:temporary_name"]["status"], checks["rule:temporary_name"]["detail"]["sqlstate"]),
                         ("error", "22012"))
        self.assertEqual((checks["rule_xcheck:temporary_name"]["detail"]["reason"],
                          checks["rule_xcheck:temporary_name"]["detail"]["failed"]), ("path_error", "sql"))
        self.assertEqual(checks["rule_lookup:temporary_name"]["detail"]["reason"], "rule_not_agreed")
        self.assertEqual(checks["rule_example:temporary_name"]["status"], "pass")
        self.assertEqual(checks["data_rules:snapshot"]["status"], "pass")
        self.assert_rules(res, {k: v for k, v in BASE.items() if k != "temporary_name"}, BASE_DETAIL)
        self.assertEqual(checks["rule_subjects:lookup"]["detail"]["reason"], "incomplete")
        self.assertTrue(res["next_safe_action"].startswith("rule:temporary_name hit a database error (SQLSTATE 22012)"))

    def test_a_timeout_outside_the_savepoints_ends_the_snapshot_but_not_the_examples(self):
        prepare = sdr.prepare

        def prepare_with_a_slow_gate(cfg, spec, plan, selected):
            prep = prepare(cfg, spec, plan, selected)
            prep.gates = [dsql.Gate(g.gate, g.entity, g.columns, dsql.Statement(dsql.Sql("SELECT pg_sleep(5)")))
                          if g.gate == "height" else g for g in prep.gates]
            return prep

        p = self.project("snap_gate_timeout", "sxc_snap", "sxc_out_snap", views="false", timeout_ms=1000)
        with mock.patch.object(sdr, "prepare", prepare_with_a_slow_gate):
            code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        checks = by_id(res)
        self.assertEqual((checks["data_rules:snapshot"]["status"], checks["data_rules:snapshot"]["detail"]["sqlstate"]),
                         ("error", "57014"))
        self.assertNotIn("data_rules:key:part", checks)
        for rule_id in BASE:
            self.assertEqual(checks[f"rule:{rule_id}"]["detail"]["reason"], "snapshot_not_read")
            self.assertEqual(checks[f"rule_example:{rule_id}"]["status"], "pass")    # next transaction, same connection
        self.assertTrue(res["next_safe_action"].startswith("data_rules:snapshot hit a database error (SQLSTATE 57014)"))

    def test_a_failing_example_statement_is_isolated_by_its_savepoint(self):
        prepare = sdr.prepare

        def prepare_with_a_failing_example(cfg, spec, plan, selected):
            prep = prepare(cfg, spec, plan, selected)
            prep.examples[("parent_unknown", 0)] = dsql.Statement(dsql.Sql(
                "SELECT CAST(1 / (SELECT count(*) FROM sxc_snap.part WHERE false) AS text) AS parent_id"))
            return prep

        with mock.patch.object(sdr, "prepare", prepare_with_a_failing_example):
            code, console, res = self.p.data_rules()
        self.assertEqual(code, 1, console)
        example = by_id(res)["rule_example:parent_unknown"]
        self.assertEqual((example["status"], example["detail"]["sqlstate"], example["detail"]["example"]),
                         ("error", "22012", 0))
        for rule_id in BASE:
            if rule_id != "parent_unknown":
                self.assertEqual(by_id(res)[f"rule_example:{rule_id}"]["status"], "pass")
        self.assert_rules(res, {k: v for k, v in BASE.items() if k != "parent_unknown"}, BASE_DETAIL)

    def test_a_statement_timeout_is_an_error_with_its_hint(self):
        p = self.project("snap_timeout", "sxc_snap", "sxc_out_snap", views="false", timeout_ms=1000)
        res = self.replaced_rule("kit_without_bom", "SELECT pg_sleep(5)", p)
        checks = by_id(res)
        rule = checks["rule:kit_without_bom"]
        self.assertEqual((rule["status"], rule["detail"]["sqlstate"], rule["detail"]["timeout_ms"]),
                         ("error", "57014", 1000))
        self.assert_rules(res, {k: v for k, v in BASE.items() if k != "kit_without_bom"}, BASE_DETAIL, lookups=False)
        self.assertIn("raise source.statement_timeout_ms (now 1000) or narrow the run with --rule",
                      res["next_safe_action"])


# ---- the views between build and data-rules --------------------------------------------------------------------------
def replaced(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise AssertionError(f"expected exactly one {old!r} in the rule file")
    return text.replace(old, new)


def without_rule(text: str, rule_id: str) -> str:
    start = text.index(f'[[rule]]\nid = "{rule_id}"')
    end = text.find("[[rule]]\n", start + 1)
    return text[:start] + (text[end:] if end != -1 else "")


class ViewLifecycleTest(_RulesCase):
    """Each test starts from the shipped rule file and a fresh build of its own schemas."""

    def fresh(self, name: str) -> Project:
        copy_schema(f"sxc_{name}")
        output_schema(f"sxc_out_{name}")
        p = self.project(name, f"sxc_{name}", f"sxc_out_{name}")
        code, console, _build = p.build()
        self.assertEqual(code, 0, console)
        return p

    def lookups(self, res: dict) -> dict:
        return {c["id"].partition(":")[2]: (c["status"], c["detail"].get("reason"))
                for c in res["checks"] if c["id"].startswith("rule_lookup:")}

    def test_a_rule_file_changed_after_build_leaves_its_lookup_stale(self):
        p = self.fresh("stale")
        p.write_rules(replaced(p.rules, 'temporary_prefix = ["TMP-"]', 'temporary_prefix = ["TMP-", "OLD-"]'))
        _code, _console, res = p.data_rules()
        self.assertEqual(self.lookups(res), {r: ("not_run", "view_stale") if r == "temporary_name" else ("pass", None)
                                             for r in BASE})
        self.assertEqual(by_id(res)["rule_subjects:lookup"]["detail"]["reason"], "view_stale")
        self.assertEqual(by_id(res)["rule_xcheck:temporary_name"]["status"], "pass")
        self.assertIn("rerun `sxc.py build`", res["next_safe_action"])
        self.assertEqual(p.build()[0], 0)
        _code, _console, res = p.data_rules()
        self.assertEqual(self.lookups(res), {r: ("pass", None) for r in BASE})

    def test_changed_output_columns_replace_the_view_by_drop_and_create(self):
        p = self.fresh("columns")
        p.write_rules(replaced(p.rules, 'report = { columns = ["parent_id", "child_id", "qty_per"] }\nsubject = '
                                        '"parent_id"\n\n  [[rule.example]]\n  spec = "example rules 1:',
                               'report = { columns = ["child_id", "parent_id", "qty_per"] }\nsubject = "parent_id"\n\n'
                               '  [[rule.example]]\n  spec = "example rules 1:'))
        code, console, build = p.build()
        self.assertEqual((code, by_id(build)["rule_view:qty_over_limit"]["status"]), (0, "pass"), console)
        build_sql = p.file("build", "build.sql")
        view = '"sxc_out_columns"."v_rule_qty_over_limit"'
        self.assertIn(f"CREATE OR REPLACE VIEW {view} AS\n", build_sql)
        self.assertIn(f"DROP VIEW {view};\nCREATE VIEW {view} AS\n", build_sql)
        columns = as_check("SELECT array_agg(attname::text ORDER BY attnum) FROM pg_attribute WHERE attrelid = "
                           "'sxc_out_columns.v_rule_qty_over_limit'::regclass AND attnum > 0")[0][0]
        self.assertEqual(columns, ["child_id", "parent_id", "qty_per", "n", "ord"])
        _code, _console, res = p.data_rules()
        self.assertEqual(self.lookups(res), {r: ("pass", None) for r in BASE})
        self.assertEqual(self.view_rows("sxc_out_columns", "qty_over_limit"),
                         [("QB", "QR", "500.000001", "1"), ("QC", "QR", "750", "2"), ("QE", "QR", "1000", "1")])

    def test_a_dropped_view_is_missing_until_build_runs_again(self):
        p = self.fresh("dropped")
        as_check("DROP VIEW sxc_out_dropped.v_rule_kit_without_bom CASCADE")
        _code, _console, res = p.data_rules()
        self.assertEqual(self.lookups(res), {r: ("not_run", "view_missing") if r == "kit_without_bom"
                                             else ("pass", None) for r in BASE})
        self.assertEqual(by_id(res)["rule_subjects:lookup"]["detail"]["reason"], "view_missing")
        self.assertEqual(p.build()[0], 0)
        _code, _console, res = p.data_rules()
        self.assertEqual(self.lookups(res), {r: ("pass", None) for r in BASE})
        self.assertEqual(by_id(res)["rule_subjects:lookup"]["status"], "pass")

    def test_a_view_changed_by_hand_fails_or_errs_its_lookup(self):
        """build.sql cannot show a view replaced by hand with the same columns; its rows can."""
        p = self.fresh("tamper")
        as_check("CREATE OR REPLACE VIEW sxc_out_tamper.v_rule_kit_without_bom AS SELECT r.*, row_number() OVER "
                 "(ORDER BY r.part_id) AS ord FROM (SELECT CAST('K9' AS text) AS part_id) r",
                 "CREATE OR REPLACE VIEW sxc_out_tamper.v_rule_temporary_name AS SELECT r.*, row_number() OVER () AS "
                 "ord FROM (SELECT CAST(1 / (SELECT count(*) FROM sxc_tamper.part WHERE false) AS text) AS part_id) r")
        _code, _console, res = p.data_rules()
        checks = by_id(res)
        kit = checks["rule_lookup:kit_without_bom"]
        self.assertEqual((kit["status"], kit["detail"]["lookup_only"], kit["detail"]["agreed_only"]), ("fail", 1, 1))
        name = checks["rule_lookup:temporary_name"]
        self.assertEqual((name["status"], name["detail"]["sqlstate"]), ("error", "22012"))
        self.assertEqual({r: s for r, s in self.lookups(res).items() if r not in ("kit_without_bom", "temporary_name")},
                         {r: ("pass", None) for r in BASE if r not in ("kit_without_bom", "temporary_name")})
        self.assertTrue(res["next_safe_action"].startswith("rule_lookup:temporary_name hit a database error"))

    def test_orphan_cleanup_drops_only_marked_rule_views(self):
        p = self.fresh("orphans")
        marker = dsql.VIEW_MARKER
        as_check("CREATE VIEW sxc_out_orphans.v_rule_kept_by_user AS SELECT 1 AS x",
                 "CREATE VIEW sxc_out_orphans.v_rule_noted AS SELECT 2 AS x",
                 "COMMENT ON VIEW sxc_out_orphans.v_rule_noted IS 'notes of a person'",
                 "CREATE VIEW sxc_out_orphans.marked_elsewhere AS SELECT 3 AS x",
                 f"COMMENT ON VIEW sxc_out_orphans.marked_elsewhere IS '{marker}'",
                 "CREATE VIEW sxc_out_orphans.v_rule_planted AS SELECT 4 AS x",
                 f"COMMENT ON VIEW sxc_out_orphans.v_rule_planted IS '{marker}'",
                 "CREATE VIEW sxc_out_orphans.uses_drift AS SELECT * FROM sxc_out_orphans.v_rule_stored_level_drift")
        p.write_rules(without_rule(p.rules, "stored_level_drift"))

        def views():
            return {r[0] for r in as_check("SELECT relname::text FROM pg_class c JOIN pg_namespace n ON n.oid = "
                                           "c.relnamespace WHERE n.nspname = 'sxc_out_orphans' AND c.relkind = 'v'")}

        code, console, build = p.build()
        self.assertEqual(code, 0, console)
        self.assertNotIn("rule_view:stored_level_drift", by_id(build))
        found = views()
        self.assertTrue({"v_rule_kept_by_user", "v_rule_noted", "marked_elsewhere", "uses_drift",
                         "v_rule_stored_level_drift"} <= found, found)    # a dependent view keeps it
        self.assertNotIn("v_rule_planted", found)
        self.assertIn("rule views: 1 marked view(s) the rule file no longer generates could not be dropped",
                      " ".join(build["not_proven"]))
        build_sql = p.file("build", "build.sql")
        self.assertIn('DROP VIEW IF EXISTS "sxc_out_orphans"."v_rule_planted";', build_sql)
        self.assertIn("-- a marked rule view the rule file no longer generates was not dropped (SQLSTATE 2BP01)",
                      build_sql)
        for kept in ("v_rule_kept_by_user", "v_rule_noted", "marked_elsewhere"):
            self.assertNotIn(kept, build_sql)
        as_check("DROP VIEW sxc_out_orphans.uses_drift")
        code, console, build = p.build()
        self.assertEqual(code, 0, console)
        found = views()
        self.assertNotIn("v_rule_stored_level_drift", found)
        self.assertTrue({"v_rule_kept_by_user", "v_rule_noted", "marked_elsewhere"} <= found, found)
        self.assertEqual(found & {f"v_rule_{r}" for r in BASE},
                         {f"v_rule_{r}" for r in BASE if r != "stored_level_drift"})

    def test_an_authored_example_that_differs_from_both_paths_is_common_mode(self):
        p = self.fresh("common")
        p.write_rules(replaced(p.rules, 'expect = [{ part_id = "K1" }]', 'expect = [{ part_id = "K2" }]'))
        _code, _console, res = p.data_rules()
        checks = by_id(res)
        example = checks["rule_example:kit_without_bom"]
        self.assertEqual((example["status"], example["detail"]["common_mode"], example["detail"]["sql_mismatch"]),
                         ("fail", 1, 0))
        rule = checks["rule:kit_without_bom"]
        self.assertEqual((rule["status"], rule["detail"]["reason"], rule["detail"]["violations"]),
                         ("not_run", "example_mismatch", 1))
        self.assertEqual(checks["rule_xcheck:kit_without_bom"]["status"], "pass")
        self.assertEqual(checks["rule_lookup:kit_without_bom"]["status"], "pass")
        self.assertIn("(common_mode)", res["next_safe_action"])


# ---- constants that are hard to write as SQL literals -----------------------------------------------------------
BS, QUOTE = chr(92), chr(39)
TRICKY = ("it" + QUOTE + "s", "k:" + BS + "set" + BS, "a:b $1 --x", chr(0xE9) + chr(0x4E2D) + chr(0x1F600), QUOTE * 2,
          BS + QUOTE, 'say "hi"')
NEAR = ("its", "k:" + BS + "set", "a:b $2 --x", "e" + chr(0x4E2D) + chr(0x1F600), QUOTE, BS + BS + QUOTE,
        "e" + chr(0x301) + chr(0x4E2D) + chr(0x1F600))    # the last one: the accented letter decomposed (NFD)
SEPARATORS = ("/", BS, "|", "&", "~", "-", "+")       # the last one is outside the pattern's bracket class
TRICKY_TEMPLATE = """version = 1
hierarchy = "has_component"

[sets]
tricky = @TRICKY@
tricky_prefixes = @PREFIXES@

[[rule]]
id = "tricky_names"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "name", op = "in", set = "tricky" }]
  [[rule.example]]
  spec = "test suite: names holding quotes, backslashes, colons, placeholders and non-ASCII text"
  rows.part = [
    { part_id = "A", name = @T0@ }, { part_id = "B", name = @T1@ }, { part_id = "C", name = @N1@ },
    { part_id = "D", name = { null = true } },
  ]
  expect = [{ part_id = "A" }, { part_id = "B" }]

[[rule]]
id = "tricky_prefix"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "name", op = "starts_with", set = "tricky_prefixes" }]
subject = "part_id"
  [[rule.example]]
  spec = "test suite: a prefix ending in a backslash and a prefix holding a colon"
  rows.part = [{ part_id = "A", name = @CX@ }, { part_id = "B", name = @LCX@ }, { part_id = "C", name = "a:bc" }]
  expect = [{ part_id = "A" }, { part_id = "C" }]

[[rule]]
id = "tricky_value"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "issue", op = "eq", value = @XBS@ }]
  [[rule.example]]
  spec = "test suite: a value ending in a backslash"
  rows.part = [{ part_id = "A", issue = @XBS@ }, { part_id = "B", issue = @XBSBS@ }]
  expect = [{ part_id = "A" }]

[[rule]]
id = "tricky_pattern"
kind = "text_quantity"
entity = "bom_line"
severity = "warning"
end = "child"
text = "name"
pattern = '@PATTERN@'
quantity = "qty_per"
rel_tol = "0"
report = { distinct = "child_id" }
  [[rule.example]]
  spec = "test suite: escaped punctuation inside a bracket class"
  rows.part = [{ part_id = "K", name = @K82@ }, { part_id = "L", name = "Kit [8+2]" }]
  rows.bom_line = [
    { line_id = 1, parent_id = "P", child_id = "K", qty_per = "4" },
    { line_id = 2, parent_id = "P", child_id = "L", qty_per = "4" },
  ]
  expect = [{ child_id = "K" }]

[[rule]]
id = "reserved_word"
kind = "row"
entity = "part"
severity = "warning"
where = [{ column = "order", op = "eq", value = "1st" }]
report = { columns = ["order"] }
  [[rule.example]]
  spec = "test suite: a column whose name is an SQL reserved word"
  rows.part = [{ part_id = "A", order = "1st" }, { part_id = "B", order = "2nd" }, { part_id = "C", order = "1st" }]
  expect = [{ order = "1st", n = 2 }]
"""


def toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)     # a JSON string is a TOML basic string


def tricky_rules() -> str:
    values = {"@TRICKY@": "[" + ", ".join(toml_string(s) for s in TRICKY) + "]",
              "@PREFIXES@": f"[{toml_string('k:' + BS)}, {toml_string('a:b')}]",
              "@T0@": toml_string(TRICKY[0]), "@T1@": toml_string(TRICKY[1]), "@N1@": toml_string(NEAR[1]),
              "@CX@": toml_string("k:" + BS + "x"), "@LCX@": toml_string("K:" + BS + "x"),
              "@XBS@": toml_string("x" + BS), "@XBSBS@": toml_string("x" + BS + BS),
              "@PATTERN@": BS + "[([0-9]+)[" + BS + BS + "/|&~-]([0-9]+)" + BS + "]",
              "@K82@": toml_string("Kit [8" + BS + "2]")}
    text = TRICKY_TEMPLATE
    for key, value in values.items():
        text = text.replace(key, value)
    return text


TRICKY_EXPECTED = {
    "tricky_names": ("pass", 7, [(f"LT{i}",) for i in range(1, 8)]),
    "tricky_prefix": ("pass", 4, [("LN2",), ("LN3",), ("LT2",), ("LT3",)]),
    "tricky_value": ("pass", 1, [("LV1",)]),
    "tricky_pattern": ("pass", 6, [(f"LK{i}",) for i in range(1, 7)]),
    "reserved_word": ("pass", 2, [("1st", "2")]),
}


class LiteralsTest(_RulesCase):
    """Rule constants and example cells with quotes, backslashes (one at the end), colons, $1, -- and non-ASCII text:
    build writes them as E'' literals into the view definitions through pg8000, data-rules binds them, and both must
    select the same rows; a pattern with escaped punctuation in a bracket class runs as an ARE and as Python re."""

    def test_hard_constants_select_the_same_rows_in_views_and_both_paths(self):
        copy_schema("sxc_lit")
        output_schema("sxc_out_lit")
        parts = ([(f"LT{i}", name, None) for i, name in enumerate(TRICKY, 1)]
                 + [(f"LN{i}", name, None) for i, name in enumerate(NEAR, 1)]
                 + [("LV1", None, "x" + BS), ("LV2", None, "x" + BS + BS), ("LV3", None, "x"), ("LKP", "LKP Box", None)]
                 + [(f"LK{i}", f"Kit [12{sep}3]", None) for i, sep in enumerate(SEPARATORS, 1)])
        a = pgfixture._admin(DATABASE)
        try:
            a.run(f'SET ROLE "{OWNER}"')
            for part_id, name, issue in parts:
                a.run("INSERT INTO sxc_lit.part (part_id, name, issue) VALUES (:p, :n, :i)", p=part_id, n=name, i=issue)
            for i in range(1, len(SEPARATORS) + 1):
                a.run("INSERT INTO sxc_lit.bom_line VALUES (:l, 'LKP', :c, 4)", l=2000 + i, c=f"LK{i}")
            a.run('ALTER TABLE sxc_lit.part ADD COLUMN "order" text')
            a.run("""UPDATE sxc_lit.part SET "order" = CASE WHEN part_id IN ('LT1', 'LT2') THEN '1st' ELSE '2nd' END """
                  "WHERE part_id IN ('LT1', 'LT2', 'LT3')")
        finally:
            a.close()
        p = self.project("lit", "sxc_lit", "sxc_out_lit", rules=tricky_rules())
        code, console, build = p.build()
        self.assertEqual(code, 0, console)
        self.assertIn("E'" + "k:" + BS * 2 + "set" + BS + "134'", p.file("build", "build.sql"))
        code, console, res = p.data_rules()
        self.assertEqual(code, 0, console)
        self.assert_rules(res, TRICKY_EXPECTED)
        self.assert_views("sxc_out_lit", TRICKY_EXPECTED, [(s, W, 1, 1, ("tricky_prefix",)) for s, in
                                                           TRICKY_EXPECTED["tricky_prefix"][2]])
        decoded = "\n".join(strings(res))       # the result's strings as stored, not as JSON-escaped text
        for text in (p.file("data_rules", "rules.sql"), console, decoded):
            for value in TRICKY + NEAR[2:4]:
                if value != QUOTE * 2:
                    self.assertNotIn(value, text)


# ---- roles and databases ---------------------------------------------------------------------------------------------
class ReadOnlyTest(_RulesCase):
    """views = false needs no write privilege at all: a role without CREATE or TEMP whose transactions default to
    read only still runs every rule; a database whose encoding is not UTF8 runs none."""

    def test_a_role_without_create_or_temp_runs_every_rule(self):
        ensure_role(READER)
        self.addCleanup(admin, f'DROP ROLE IF EXISTS "{READER}"', database="postgres")
        admin(f'GRANT TEMPORARY ON DATABASE "{DATABASE}" TO "{CHECK}", "{OWNER}"',
              f'REVOKE TEMPORARY ON DATABASE "{DATABASE}" FROM PUBLIC',
              f'GRANT USAGE ON SCHEMA sxc_rules TO "{READER}"',
              f'GRANT SELECT ON ALL TABLES IN SCHEMA sxc_rules TO "{READER}"',
              f'ALTER ROLE "{READER}" IN DATABASE "{DATABASE}" SET default_transaction_read_only = on')
        self.addCleanup(admin, f'REVOKE ALL ON ALL TABLES IN SCHEMA sxc_rules FROM "{READER}"',
                        f'REVOKE ALL ON SCHEMA sxc_rules FROM "{READER}"',
                        f'ALTER ROLE "{READER}" IN DATABASE "{DATABASE}" RESET ALL',
                        f'GRANT TEMPORARY ON DATABASE "{DATABASE}" TO PUBLIC')
        os.environ[READER_ENV] = pgfixture.dsn_for(READER, DATABASE)
        self.addCleanup(os.environ.pop, READER_ENV, None)
        rights = as_check("SELECT has_database_privilege(current_user, current_database(), 'CREATE'), "
                          "has_database_privilege(current_user, current_database(), 'TEMP'), bool_or(has_schema_"
                          "privilege(current_user, n.oid, 'CREATE')), current_setting('default_transaction_read_only') "
                          "FROM pg_namespace n", env=READER_ENV)
        self.assertEqual(rights, [[False, False, False, "on"]])
        p = self.project("reader", "sxc_rules", "sxc_out_reader", views="false", dsn_env=READER_ENV)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        self.assert_rules(res, BASE, BASE_DETAIL, lookups=False)
        self.assertFalse([c for c in res["checks"] if c["id"].startswith(("rule_lookup:", "rule_subjects:"))])
        self.assertIn(sdr.VIEWS_OFF_NOTE, res["not_proven"])

    def test_a_database_that_is_not_utf8_runs_no_rule(self):
        admin(f'DROP DATABASE IF EXISTS "{ASCII_DATABASE}" WITH (FORCE)',
              f"CREATE DATABASE \"{ASCII_DATABASE}\" ENCODING 'SQL_ASCII' LOCALE_PROVIDER libc LC_COLLATE 'C' "
              "LC_CTYPE 'C' TEMPLATE template0",
              database="postgres")
        self.addCleanup(admin, f'DROP DATABASE IF EXISTS "{ASCII_DATABASE}" WITH (FORCE)', database="postgres")
        source_schema("sxc_rules", database=ASCII_DATABASE)
        # pg8000 talks SQL_ASCII to such a database, so the one non-ASCII key is loaded as ASCII here
        admin(*[s.replace(U_ACUTE, "U") for s in fixture_statements()], role=OWNER, database=ASCII_DATABASE)
        os.environ[ASCII_ENV] = pgfixture.dsn_for(CHECK, ASCII_DATABASE)
        self.addCleanup(os.environ.pop, ASCII_ENV, None)
        p = self.project("ascii", "sxc_rules", "sxc_out", views="false", dsn_env=ASCII_ENV)
        code, console, res = p.data_rules()
        self.assertEqual(code, 1, console)
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot["status"], snapshot["detail"]["reason"], snapshot["detail"]["encoding"]),
                         ("not_run", "server_encoding", "SQL_ASCII"))
        self.assert_not_run(res, BASE, "server_encoding")
        for rule_id in BASE:
            example = by_id(res)[f"rule_example:{rule_id}"]
            self.assertEqual((example["status"], example["detail"]["gate"]), ("not_run", "server_encoding"))
        self.assertEqual(p.file("data_rules", "rules.sql").count("SHOW server_encoding;"), 2)


if __name__ == "__main__":
    unittest.main()
