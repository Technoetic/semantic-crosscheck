"""The [data_rules] configuration table and the rule-file model (data_rules.py): loading, closed validation, types.

Table-driven, synthetic and without a database. Every refused case must raise a configuration error whose message
names a position and never a constant: the documents carry the marker DRPMARK in every set member, comparison value,
ref constant, example cell, spec text and pattern, and no message may contain it. The clean-room example
(examples/data-rules, the spec's Appendix A without its unused set) must load and type-check against its schema.
"""

import ast
import copy
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, data_rules, ident, naming
from semantic_crosscheck.data_rules import RuleSpecError
from semantic_crosscheck.result import EXIT_CONFIG, StageError

ROOT = pgfixture.ROOT
EXAMPLE = ROOT / "examples" / "data-rules"
MARK = "DRPMARK"
DROP = object()
CFG_PATH = Path(tempfile.gettempdir()) / "sxc-data-rules-spec" / "crosscheck.toml"

CONFIG = """version = 1
[source]
schemas = ["sxc_rules"]
output_schema = "sxc_out"
[entities.part]
table = "sxc_rules.part"
key = ["{node_key}"]
[entities.bom_line]
table = "sxc_rules.bom_line"
key = ["line_id"]
[entities.code_value]
table = "sxc_rules.code_value"
key = ["code_group", "code"]
{hierarchies}{extra}"""
HIERARCHY = """[hierarchies.{name}]
edge = "bom_line"
node = "part"
parent = "parent_id"
child = "child_id"
quantity = "qty_per"
max_depth = 16
"""
DATA_RULES = '[data_rules]\nfile = "data-rules.toml"\n'


def make_cfg(hierarchies=("has_component",), node_key="part_id", extra=DATA_RULES):
    text = CONFIG.format(node_key=node_key, hierarchies="".join(HIERARCHY.format(name=n) for n in hierarchies),
                         extra=extra)
    return config.loads(text.encode("utf-8"), CFG_PATH)


CFG = make_cfg()
CFG_NO_HIERARCHY = make_cfg(hierarchies=())
CFG_TWO_HIERARCHIES = make_cfg(hierarchies=("has_component", "uses_part"))
CFG_KEY_KIND = make_cfg(node_key="kind")
CFG_COLLIDING = make_cfg(hierarchies=("v_rule_a",))


# ---- TOML documents from Python values (inline tables only, which TOML 1.0 nests freely) ----------
def toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + " }" if value else "{}"
    raise TypeError(type(value).__name__)


def toml_doc(doc: dict) -> bytes:
    return "".join(f"{toml_key(k)} = {toml_value(v)}\n" for k, v in doc.items()).encode("utf-8")


# ---- a valid base document and kind templates (every constant carries the marker) -------------------
KIT_EXAMPLE = {"spec": f"{MARK} kit rule", "rows": {"part": [{"part_id": f"{MARK}-K", "issue": f"{MARK}-KIT"}],
                                                   "bom_line": []},
               "expect": [{"part_id": f"{MARK}-K"}]}
BASE = {
    "version": 1,
    "sets": {"kit": [f"{MARK}-KIT"], "units": [f"{MARK}-pc", f"{MARK}-box"]},
    "refs": {"codes": {"entity": "code_value", "column": "code", "where": {"code_group": f"{MARK}-GROUP"}}},
    "rule": [
        {"id": "kit_leaf", "kind": "row", "entity": "part", "severity": "violation",
         "where": [{"column": "issue", "op": "in", "set": "kit"}, {"has_children": False}], "subject": "part_id",
         "example": [KIT_EXAMPLE]},
        {"id": "unit_line", "kind": "row", "entity": "bom_line", "severity": "warning",
         "where": [{"end": "child", "column": "unit", "op": "in", "set": "units"}],
         "report": {"columns": ["parent_id", "child_id"]}, "subject": "parent_id"},
        {"id": "unknown_code", "kind": "row", "entity": "part", "severity": "warning",
         "where": [{"column": "procure", "op": "not_in_ref", "ref": "codes"}]},
    ],
}
ROW = {"id": "row_rule", "kind": "row", "entity": "part", "severity": "warning",
       "where": [{"column": "issue", "op": "eq", "value": f"{MARK}-V"}]}
DUP = {"id": "dup", "kind": "duplicate", "entity": "bom_line", "severity": "warning", "key": ["parent_id", "child_id"]}
MODE = {"id": "mode", "kind": "mode_outlier", "entity": "bom_line", "severity": "warning", "group_by": "child_id",
        "value": "qty_per", "min_mode_share": "0.75", "ratio": "20"}
SIG = {"id": "sig", "kind": "same_signature", "severity": "warning", "attrs": ["name"]}
HEIGHT = {"id": "height", "kind": "stored_metric", "severity": "warning", "metric": "height", "stored": "stored_level"}
TEXT = {"id": "text", "kind": "text_quantity", "entity": "bom_line", "severity": "warning", "end": "child",
        "text": "name", "pattern": f"{MARK} \\[([0-9]+) g\\]", "quantity": "qty_per", "rel_tol": "0.001"}
A = {"column": "issue", "op": "is_null"}           # a neutral column atom for the part entity
KIT_ATOM = {"column": "issue", "op": "in", "set": "kit"}
UNIT_ATOM = {"end": "child", "column": "unit", "op": "in", "set": "units"}


def base() -> dict:
    return copy.deepcopy(BASE)


def put(doc: dict, path: tuple, value) -> dict:
    target = doc
    for key in path[:-1]:
        target = target[key]
    if value is DROP:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return doc


def edit(*changes) -> dict:
    """The base document with (path, value) changes applied in order."""
    doc = base()
    for path, value in changes:
        put(doc, path, value)
    return doc


def kind(template: dict, **changes) -> dict:
    rule = copy.deepcopy(template)
    for key, value in changes.items():
        if value is DROP:
            rule.pop(key, None)
        else:
            rule[key] = value
    return rule


def single(*rules, sets=None, refs=None, **top) -> dict:
    doc = {"version": 1, **top}
    if sets:
        doc["sets"] = sets
    if refs:
        doc["refs"] = refs
    doc["rule"] = list(rules)
    return doc


def where0(*atoms) -> dict:
    return edit((("rule", 0, "where"), list(atoms)))


def where1(*atoms) -> dict:
    return edit((("rule", 1, "where"), list(atoms)))


def example0(**changes) -> dict:
    doc = base()
    for key, value in changes.items():
        put(doc, ("rule", 0, "example", 0, key), value)
    return doc


def kit_rows(n: int, extra_columns: int = 0) -> list:
    return [{"part_id": f"{MARK}-{i}", "issue": f"{MARK}-KIT", **{f"c{j}": j for j in range(extra_columns)}}
            for i in range(n)]


def many_rules(n: int) -> dict:
    return single(*[kind(ROW, id=f"r{i}", where=[{"column": "issue", "op": "in", "set": "kit"}]) for i in range(n)],
                  sets={"kit": [f"{MARK}-KIT"]})


def load(doc, cfg=CFG):
    raw = toml_doc(doc) if isinstance(doc, dict) else doc
    return data_rules.loads(raw, cfg)


# name, document builder, message fragment, configuration
REFUSED = [
    # top level
    ("version missing", lambda: edit((("version",), DROP)), "version: is required and must be 1", CFG),
    ("version 2", lambda: edit((("version",), 2)), "version: is required", CFG),
    ("version boolean", lambda: edit((("version",), True)), "version: is required", CFG),
    ("version string", lambda: edit((("version",), "1")), "version: is required", CFG),
    ("unknown top-level key", lambda: edit((("rules",), [])), "unknown key (allowed: version, hierarchy", CFG),
    ("no rule table", lambda: edit((("rule",), DROP)), "needs 1..128 [[rule]] tables", CFG),
    ("empty rule list", lambda: edit((("rule",), [])), "needs 1..128 [[rule]] tables", CFG),
    ("129 rules", lambda: many_rules(129), "needs 1..128 [[rule]] tables", CFG),
    ("rule not a table", lambda: edit((("rule",), [1])), "rule[0]: must be a table", CFG),
    ("unknown hierarchy", lambda: edit((("hierarchy",), "nope")), "hierarchy: must name a [hierarchies.<name>]", CFG),
    ("hierarchy not a string", lambda: edit((("hierarchy",), 3)), "hierarchy: must name", CFG),
    ("hierarchy required with two", lambda: base(), "hierarchy: is required: crosscheck.toml has several",
     CFG_TWO_HIERARCHIES),
    # sets
    ("sets not a table", lambda: edit((("sets",), [1])), "sets: must be a table of named lists", CFG),
    ("set name upper case", lambda: edit((("sets", "Kit"), [f"{MARK}"])), "sets: a set name must be", CFG),
    ("set empty", lambda: edit((("sets", "kit"), [])), "sets.kit: must be a list of 1..1000 members", CFG),
    ("set 1001 members", lambda: edit((("sets", "kit"), [f"{MARK}{i}" for i in range(1001)])), "1..1000", CFG),
    ("set not a list", lambda: edit((("sets", "kit"), f"{MARK}")), "sets.kit: must be a list", CFG),
    ("set mixed string integer", lambda: edit((("sets", "kit"), [f"{MARK}", 7])), "members must be all strings", CFG),
    ("set mixed integer boolean", lambda: edit((("sets", "kit"), [7, True])), "members must be all strings", CFG),
    ("set float member", lambda: edit((("sets", "kit"), [0.5])), "sets.kit[0]: TOML floats are refused", CFG),
    ("set NUL member", lambda: edit((("sets", "kit"), [f"{MARK}\x00"])), "sets.kit[0]: strings must not contain NUL",
     CFG),
    ("set nested list", lambda: edit((("sets", "kit"), [[f"{MARK}"]])), "sets.kit[0]: a member must be", CFG),
    ("set table member", lambda: edit((("sets", "kit"), [{"x": f"{MARK}"}])), "sets.kit[0]: a member must be", CFG),
    ("unused set", lambda: edit((("sets", "spare"), [f"{MARK}"])), "sets.spare: is not used by any rule", CFG),
    ("starts_with an integer set", lambda: edit((("sets", "kit"), [1, 2]),
                                                (("rule", 0, "where"), [{"column": "name", "op": "starts_with",
                                                                         "set": "kit"}])),
     "starts_with needs a set of strings", CFG),
    # refs
    ("refs not a table", lambda: edit((("refs",), [1])), "refs: must be a table", CFG),
    ("ref name upper case", lambda: edit((("refs", "Codes"), {"entity": "code_value", "column": "code"})),
     "refs: a ref name must be", CFG),
    ("ref not a table", lambda: edit((("refs", "codes"), 3)), "refs.codes: must be a table", CFG),
    ("ref unknown entity", lambda: edit((("refs", "codes", "entity"), "nope")), "refs.codes: entity must name", CFG),
    ("ref unknown key", lambda: edit((("refs", "codes", "note"), f"{MARK}")), "refs.codes: unknown key", CFG),
    ("ref column invalid", lambda: edit((("refs", "codes", "column"), "Code")), "refs.codes: column must be", CFG),
    ("ref where empty", lambda: edit((("refs", "codes", "where"), {})), "refs.codes where: must be a non-empty", CFG),
    ("ref where float", lambda: edit((("refs", "codes", "where"), {"code_group": 0.5})),
     "refs.codes where.code_group: TOML floats are refused", CFG),
    ("ref where NUL", lambda: edit((("refs", "codes", "where"), {"code_group": f"{MARK}\x00"})),
     "strings must not contain NUL", CFG),
    ("ref where names its column", lambda: edit((("refs", "codes", "where"), {"code": f"{MARK}"})),
     "names the ref column code itself", CFG),
    ("unused ref", lambda: edit((("refs", "more"), {"entity": "code_value", "column": "code"})),
     "refs.more: is not used by any rule", CFG),
    # rule keys
    ("id missing", lambda: edit((("rule", 0, "id"), DROP)), "rule[0]: id is required", CFG),
    ("id upper case", lambda: edit((("rule", 0, "id"), "Kit")), "rule[0]: id is required", CFG),
    ("id of 57 characters", lambda: edit((("rule", 0, "id"), "a" * 57)), "rule[0]: id is required", CFG),
    ("duplicate id", lambda: edit((("rule", 1, "id"), "kit_leaf")), "rule[1] kit_leaf: duplicate rule id", CFG),
    ("kind missing", lambda: edit((("rule", 0, "kind"), DROP)), "rule[0] kit_leaf: kind is required", CFG),
    ("kind unknown", lambda: edit((("rule", 0, "kind"), "rows")), "kind is required: one of row, duplicate", CFG),
    ("severity missing", lambda: edit((("rule", 0, "severity"), DROP)), "severity is required: violation or warning",
     CFG),
    ("severity unknown", lambda: edit((("rule", 0, "severity"), "error")), "severity is required", CFG),
    ("unknown rule key", lambda: edit((("rule", 0, "note"), f"{MARK}")), "rule[0] kit_leaf: unknown key", CFG),
    ("key of another kind", lambda: edit((("rule", 0, "attrs"), ["name"])), "rule[0] kit_leaf: unknown key", CFG),
    ("where missing", lambda: edit((("rule", 0, "where"), DROP)), "where is required for kind row", CFG),
    ("entity unknown", lambda: edit((("rule", 0, "entity"), "nope")), "rule[0] kit_leaf: entity must name", CFG),
    # atoms
    ("where empty", lambda: where0(), "rule[0] kit_leaf where: must be a non-empty list of atoms", CFG),
    ("where a table", lambda: edit((("rule", 0, "where"), A)), "where: must be a non-empty list of atoms", CFG),
    ("33 atoms", lambda: where0(*[A] * 33), "where: has more than 32 atoms", CFG),
    ("atom not a table", lambda: where0(1), "where[0]: an atom must be a non-empty table", CFG),
    ("empty atom", lambda: where0({}), "where[0]: an atom must be a non-empty table", CFG),
    ("unknown op", lambda: where0({"column": "issue", "op": "like", "value": f"{MARK}"}), "where[0]: op must be one of",
     CFG),
    ("comparison without value", lambda: where0({"column": "issue", "op": "eq"}),
     "op eq takes exactly the keys column, op, value", CFG),
    ("comparison with a set", lambda: where0({"column": "issue", "op": "eq", "set": "kit"}), "op eq takes exactly", CFG),
    ("in with a value", lambda: where0({"column": "issue", "op": "in", "value": f"{MARK}"}),
     "op in takes exactly the keys column, op, set", CFG),
    ("null test with a value", lambda: where0({"column": "issue", "op": "is_null", "value": f"{MARK}"}),
     "op is_null takes exactly the keys column, op", CFG),
    ("unknown set", lambda: where0({"column": "issue", "op": "in", "set": "nope"}), "where[0]: set must name", CFG),
    ("unknown ref", lambda: edit((("rule", 2, "where", 0, "ref"), "nope")), "where[0]: ref must name", CFG),
    ("value float", lambda: where0({"column": "issue", "op": "eq", "value": 1.5}),
     "where[0] value: TOML floats are refused", CFG),
    ("value NUL", lambda: where0({"column": "issue", "op": "eq", "value": f"{MARK}\x00"}),
     "where[0] value: strings must not contain NUL", CFG),
    ("value list", lambda: where0({"column": "issue", "op": "eq", "value": [f"{MARK}"]}),
     "value: must be a string, an integer or a boolean", CFG),
    ("column invalid", lambda: where0({"column": "Issue", "op": "is_null"}), "where[0]: column must be", CFG),
    ("endpoint atom on the node entity", lambda: where0({"end": "parent", "exists": True}),
     "endpoint atoms (end) apply to the hierarchy edge entity", CFG),
    ("structure atom on the edge entity", lambda: where1({"has_children": True}),
     "structure atoms apply to the hierarchy node entity", CFG),
    ("quantified atom on the edge entity", lambda: where1({"children": "all", "where": [A]}),
     "children and parents atoms apply to the hierarchy node entity", CFG),
    ("endpoint atom in a sub-where", lambda: where0({"children": "all", "where": [{"end": "parent", "exists": True}]}),
     "endpoint atoms (end) are not allowed in a quantified sub-where", CFG),
    ("nested quantified atom", lambda: where0({"children": "any", "where": [{"parents": "all", "where": [A]}]}),
     "a quantified atom (children, parents) cannot be nested", CFG),
    ("disjunction in a sub-where", lambda: where0({"children": "any", "where": [{"any": [[A], [A]]}]}),
     "a disjunction (any) belongs directly in a rule's where list", CFG),
    ("nested disjunction", lambda: where0({"any": [[{"any": [[A], [A]]}], [A]]}),
     "where[0] any[0][0]: a disjunction (any) belongs directly", CFG),
    ("disjunction with one branch", lambda: where0({"any": [[A]]}), "any must be a list of 2..8 atom lists", CFG),
    ("disjunction with nine branches", lambda: where0({"any": [[A]] * 9}), "any must be a list of 2..8", CFG),
    ("disjunction with an empty branch", lambda: where0({"any": [[], [A]]}),
     "where[0] any[0]: must be a non-empty list of atoms", CFG),
    ("disjunction with another key", lambda: where0({"any": [[A], [A]], "column": "issue"}),
     "a disjunction has the single key any", CFG),
    ("branch with 33 atoms", lambda: where0({"any": [[A] * 33, [A]]}), "any[0]: has more than 32 atoms", CFG),
    ("unknown quantifier", lambda: where0({"children": "some", "where": [A]}), "quantifier must be all, any or none",
     CFG),
    ("quantified atom with another key", lambda: where0({"children": "all", "where": [A], "column": "issue"}),
     "a quantified atom has exactly the keys children and where", CFG),
    ("quantified atom with both directions", lambda: where0({"children": "all", "parents": "all", "where": [A]}),
     "a quantified atom has exactly the keys", CFG),
    ("empty sub-where", lambda: where0({"children": "all", "where": []}), "where[0] where: must be a non-empty list",
     CFG),
    ("exists not a boolean", lambda: where1({"end": "parent", "exists": "yes"}), "exists must be true or false", CFG),
    ("end unknown", lambda: where1({"end": "middle", "exists": True}), "end must be parent or child", CFG),
    ("endpoint atom with two tests", lambda: where1({"end": "parent", "has_children": True, "has_parents": True}),
     "an endpoint atom needs exists, a column comparison or exactly one of", CFG),
    ("endpoint existence with a column", lambda: where1({"end": "parent", "exists": True, "column": "unit"}),
     "an endpoint existence atom has exactly the keys end and exists", CFG),
    ("endpoint attribute with an extra key", lambda: where1({"end": "child", "column": "unit", "op": "is_null",
                                                              "exists": True}),
     "an endpoint existence atom has exactly the keys end and exists", CFG),
    ("structure value not a boolean", lambda: where0({"has_children": 1}), "has_children must be true or false", CFG),
    ("two structure tests", lambda: where0({"has_children": True, "in_hierarchy": True}),
     "a structure atom has exactly one of the keys", CFG),
    ("unknown atom form", lambda: where0({"exists": True}), "where[0]: unknown atom form", CFG),
    ("structure atom without a hierarchy", lambda: single(kind(ROW, where=[{"has_children": True}])),
     "structure atoms apply to the hierarchy node entity", CFG_NO_HIERARCHY),
    ("endpoint atom without a hierarchy", lambda: single(kind(ROW, entity="bom_line",
                                                              where=[{"end": "parent", "exists": True}])),
     "endpoint atoms (end) apply to the hierarchy edge entity", CFG_NO_HIERARCHY),
    ("where_end with a structure atom", lambda: single(kind(TEXT, where_end=[{"has_children": True}])),
     "text where_end[0]: where_end holds column atoms only", CFG),
    ("where_end with an endpoint atom", lambda: single(kind(TEXT, where_end=[{"end": "child", "column": "unit",
                                                                               "op": "is_null"}])),
     "where_end holds column atoms only", CFG),
    ("where_end empty", lambda: single(kind(TEXT, where_end=[])), "where_end: must be a non-empty list of atoms", CFG),
    # reports and output columns
    ("row report group", lambda: edit((("rule", 1, "report"), "group")),
     'report: must be "key" or { distinct = C } or { columns = [C, ...] }', CFG),
    ("row report unknown form", lambda: edit((("rule", 1, "report"), {"rows": ["parent_id"]})), "report: must be",
     CFG),
    ("report columns empty", lambda: edit((("rule", 1, "report"), {"columns": []})),
     "report: columns must be a non-empty list", CFG),
    ("report columns twice", lambda: edit((("rule", 1, "report"), {"columns": ["parent_id", "parent_id"]})),
     "report: lists a column twice", CFG),
    ("report columns with n", lambda: edit((("rule", 1, "report"), {"columns": ["parent_id", "n"]})),
     "output column n appears twice", CFG),
    ("report distinct invalid", lambda: edit((("rule", 1, "report"), {"distinct": "Parent"})), "distinct must be",
     CFG),
    ("mode_outlier columns with mode", lambda: single(kind(MODE, report={"columns": ["child_id", "mode"]})),
     "output column mode appears twice", CFG),
    ("text_quantity columns with numerator", lambda: single(kind(TEXT, report={"columns": ["numerator"]})),
     "output column numerator appears twice", CFG),
    ("duplicate key part twice", lambda: single(kind(DUP, key=["parent_id", "parent_id"])),
     "output column parent_id appears twice", CFG),
    ("duplicate key part n", lambda: single(kind(DUP, key=["n"])), "output column n appears twice", CFG),
    ("duplicate own and endpoint names", lambda: single(kind(DUP, key=["child_name", {"end": "child",
                                                                                       "column": "name"}])),
     "output column child_name appears twice", CFG),
    ("duplicate distinct not a key part", lambda: single(kind(DUP, key=["parent_id"], report={"distinct": "child_id"})),
     "report: distinct must name an own-column key part", CFG),
    ("duplicate distinct on an endpoint part", lambda: single(kind(DUP, key=[{"end": "child", "column": "name"}],
                                                                   report={"distinct": "name"})),
     "distinct must name an own-column key part", CFG),
    ("duplicate report key", lambda: single(kind(DUP, report="key")), 'report: must be "group" or { distinct = C }',
     CFG),
    ("duplicate endpoint part on the node entity", lambda: single(kind(DUP, entity="part",
                                                                       key=[{"end": "child", "column": "name"}])),
     "dup key[0]: an endpoint key part applies to the hierarchy edge entity", CFG),
    ("duplicate key empty", lambda: single(kind(DUP, key=[])), "dup key: must be a non-empty list", CFG),
    ("duplicate key part a number", lambda: single(kind(DUP, key=[3])), "a key part is an own column name", CFG),
    ("duplicate endpoint part without column", lambda: single(kind(DUP, key=[{"end": "child"}])),
     "dup key[0]: column must be", CFG),
    ("duplicate endpoint part end unknown", lambda: single(kind(DUP, key=[{"end": "side", "column": "name"}])),
     "end must be parent or child", CFG),
    ("endpoint key part name too long", lambda: single(kind(DUP, key=[{"end": "child", "column": "c" * 58}])),
     "dup: an output column name is longer than 63 characters", CFG),
    ("duplicate min 1", lambda: single(kind(DUP, min=1)), "min must be an integer >= 2", CFG),
    ("duplicate min boolean", lambda: single(kind(DUP, min=True)), "min must be an integer >= 2", CFG),
    ("duplicate count string", lambda: single(kind(DUP, count="distinct")), 'count: must be "rows" or { distinct = C }',
     CFG),
    ("stored_metric node key named kind", lambda: single(HEIGHT), "output column kind appears twice", CFG_KEY_KIND),
    # kind parameters
    ("mode share one half", lambda: single(kind(MODE, min_mode_share="0.5")),
     "min_mode_share must be greater than 1/2 and at most 1", CFG),
    ("mode share above one", lambda: single(kind(MODE, min_mode_share="1.01")), "min_mode_share must be greater", CFG),
    ("mode share float", lambda: single(kind(MODE, min_mode_share=0.75)),
     'min_mode_share must be a decimal string such as "0.75"', CFG),
    ("mode share integer", lambda: single(kind(MODE, min_mode_share=1)), "min_mode_share must be a decimal string",
     CFG),
    ("mode share text", lambda: single(kind(MODE, min_mode_share=f"{MARK}")), "min_mode_share must be a decimal", CFG),
    ("mode ratio one", lambda: single(kind(MODE, ratio="1")), "ratio must be greater than 1", CFG),
    ("mode ratio exponent", lambda: single(kind(MODE, ratio="1e3")), "ratio must be a decimal string", CFG),
    ("mode min_distinct n 0", lambda: single(kind(MODE, min_distinct={"column": "parent_id", "n": 0})),
     "min_distinct: n must be an integer >= 1", CFG),
    ("mode min_distinct without n", lambda: single(kind(MODE, min_distinct={"column": "parent_id"})),
     "min_distinct: needs column and n", CFG),
    ("mode min_distinct unknown key", lambda: single(kind(MODE, min_distinct={"column": "parent_id", "n": 2,
                                                                              "x": 1})),
     "min_distinct: unknown key", CFG),
    ("mode value missing", lambda: single(kind(MODE, value=DROP)), "value is required for kind mode_outlier", CFG),
    ("signature attrs empty", lambda: single(kind(SIG, attrs=[])), "sig attrs: must be a non-empty list", CFG),
    ("signature attrs twice", lambda: single(kind(SIG, attrs=["name", "name"])), "sig attrs: lists a column twice",
     CFG),
    ("signature min_members 1", lambda: single(kind(SIG, min_members=1)), "min_members must be an integer >= 2", CFG),
    ("signature without a hierarchy", lambda: single(SIG), "same_signature needs a hierarchy", CFG_NO_HIERARCHY),
    ("stored_metric metric depth", lambda: single(kind(HEIGHT, metric="depth")), 'metric must be "height"', CFG),
    ("stored_metric stored missing", lambda: single(kind(HEIGHT, stored=DROP)), "stored is required", CFG),
    ("text_quantity on the node entity", lambda: single(kind(TEXT, entity="part")),
     "entity must be the hierarchy edge entity", CFG),
    ("text_quantity end unknown", lambda: single(kind(TEXT, end="middle")), "end must be parent or child", CFG),
    ("text_quantity rel_tol negative", lambda: single(kind(TEXT, rel_tol="-0.1")), "rel_tol must be at least 0", CFG),
    ("text_quantity rel_tol float", lambda: single(kind(TEXT, rel_tol=0.1)), "rel_tol must be a decimal string", CFG),
    ("text_quantity max_text_length 0", lambda: single(kind(TEXT, max_text_length=0)),
     "max_text_length must be an integer 1..1000000", CFG),
    ("text_quantity max_text_length too large", lambda: single(kind(TEXT, max_text_length=1000001)),
     "max_text_length must be an integer 1..1000000", CFG),
    ("text_quantity pattern not a string", lambda: single(kind(TEXT, pattern=3)), "text pattern: must be a string",
     CFG),
    ("text_quantity no capture", lambda: single(kind(TEXT, pattern=f"{MARK} [0-9]+ g")),
     "pattern: needs 1 or 2 capturing groups", CFG),
    ("text_quantity three captures", lambda: single(kind(TEXT, pattern=f"{MARK}([0-9]+)([0-9]+)([0-9]+)")),
     "pattern: needs 1 or 2 capturing groups", CFG),
    ("text_quantity refused pattern", lambda: single(kind(TEXT, pattern=f"{MARK}([0-9]+)|x")),
     "text pattern: refused (alternation)", CFG),
    ("text_quantity nested repetition", lambda: single(kind(TEXT, pattern=f"{MARK}((?:[0-9]+)+)")),
     "refused (repetition over a group that contains a quantifier)", CFG),
    # subjects
    ("subject not an output column", lambda: edit((("rule", 0, "subject"), "issue")),
     "subject: must name an output column of this rule", CFG),
    ("subject without node keys", lambda: edit((("rule", 0, "report"), {"columns": ["issue", "part_id"]}),
                                               (("rule", 0, "subject"), "issue"),
                                               (("rule", 0, "example"), DROP)),
     "subject: must be an output column holding node keys", CFG),
    ("edge subject not an endpoint column", lambda: edit((("rule", 1, "report"), {"columns": ["parent_id", "qty_per"]}),
                                                         (("rule", 1, "subject"), "qty_per")),
     "must be an output column holding node keys", CFG),
    ("signature subject node key", lambda: single(kind(SIG, subject="part_id")),
     "subject: must name an output column of this rule", CFG),
    ("stored_metric subject kind", lambda: single(kind(HEIGHT, subject="kind")),
     "must be an output column holding node keys", CFG),
    ("subject on another entity", lambda: single(kind(ROW, entity="code_value", where=[{"column": "code",
                                                                                         "op": "is_null"}],
                                                      subject="code")),
     "must be an output column holding node keys", CFG),
    ("subject not a string", lambda: edit((("rule", 0, "subject"), 3)), "subject: subject must be", CFG),
    # examples
    ("example spec missing", lambda: example0(spec=DROP), "example[0]: spec is required", CFG),
    ("example spec blank", lambda: example0(spec="  "), "example[0]: spec is required", CFG),
    ("example unknown key", lambda: example0(note=f"{MARK}"), "example[0]: unknown key", CFG),
    ("example a table", lambda: edit((("rule", 0, "example"), KIT_EXAMPLE)), "example: must be an array of tables",
     CFG),
    ("nine examples", lambda: edit((("rule", 0, "example"), [KIT_EXAMPLE] * 9)), "at most 8 examples per rule", CFG),
    ("example rows missing an entity", lambda: example0(rows={"part": KIT_EXAMPLE["rows"]["part"]}),
     "rows.bom_line is required: the rule reads that entity", CFG),
    ("example rows extra entity", lambda: example0(rows={**KIT_EXAMPLE["rows"], "code_value": []}),
     "rows names an entity the rule does not read", CFG),
    ("example rows not a table", lambda: example0(rows=[1]), "rows must be a table", CFG),
    ("example entity rows not a list", lambda: example0(rows={"part": {"part_id": f"{MARK}"}, "bom_line": []}),
     "rows.part: must be a list of tables", CFG),
    ("example row missing a read column", lambda: example0(rows={"part": [{"part_id": f"{MARK}-K"}], "bom_line": []}),
     "rows.part[0]: column issue is missing", CFG),
    ("example row not a table", lambda: example0(rows={"part": [f"{MARK}"], "bom_line": []}),
     "rows.part[0]: must be a table of column = value", CFG),
    ("example column invalid", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": f"{MARK}",
                                                                 "Bad": 1}], "bom_line": []}),
     "rows.part[0]: a column must be", CFG),
    ("example cell float", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": 0.5}], "bom_line": []}),
     "rows.part[0] issue: TOML floats are refused", CFG),
    ("example cell null false", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": {"null": False}}],
                                                       "bom_line": []}),
     "the only table value is { null = true }", CFG),
    ("example cell null with another key", lambda: example0(rows={"part": [{"part_id": f"{MARK}",
                                                                            "issue": {"null": True, "x": 1}}],
                                                                  "bom_line": []}),
     "the only table value is { null = true }", CFG),
    ("example cell list", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": [f"{MARK}"]}],
                                                 "bom_line": []}),
     "a cell is a string, an integer, a boolean or { null = true }", CFG),
    ("example cell NUL", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": f"{MARK}\x00"}],
                                                "bom_line": []}),
     "rows.part[0] issue: strings must not contain NUL", CFG),
    ("example node key NULL", lambda: example0(rows={"part": [{"part_id": {"null": True}, "issue": f"{MARK}"}],
                                                     "bom_line": []}),
     "rows.part[0]: the key must not be NULL", CFG),
    ("example node key twice", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": "a"},
                                                               {"part_id": f"{MARK}", "issue": "b"}],
                                                      "bom_line": []}),
     "rows.part: rows[0] and rows[1] have the same key", CFG),
    ("example reported key twice", lambda: single(kind(ROW, entity="code_value", where=[{"column": "code",
                                                                                          "op": "is_null"}],
                                                       example=[{"spec": "s", "rows": {"code_value": [
                                                           {"code_group": f"{MARK}", "code": f"{MARK}"},
                                                           {"code_group": f"{MARK}", "code": f"{MARK}"}]},
                                                           "expect": []}])),
     "rows.code_value: rows[0] and rows[1] have the same key", CFG),
    ("example 201 rows", lambda: example0(rows={"part": kit_rows(201), "bom_line": []}), "more than 200 input rows",
     CFG),
    ("example 4001 cells", lambda: example0(rows={"part": kit_rows(97, 40), "bom_line": []}),
     "more than 4000 input cells", CFG),
    ("example expect missing", lambda: example0(expect=DROP), "expect is required", CFG),
    ("example expect a table", lambda: example0(expect={"part_id": f"{MARK}"}), "expect: must be a list of tables",
     CFG),
    ("example expect row missing a column", lambda: example0(expect=[{}]),
     "expect[0]: output column part_id is missing", CFG),
    ("example expect row not a table", lambda: example0(expect=[f"{MARK}"]),
     "expect[0]: must be a table keyed by output column", CFG),
    ("example expect n not a number", lambda: single(kind(DUP, example=[{"spec": "s", "rows": {"bom_line": []},
                                                                         "expect": [{"parent_id": "p", "child_id": "c",
                                                                                     "n": f"{MARK}"}]}])),
     "expect[0] n: n must be an integer or a decimal string", CFG),
    ("example expect row extra column", lambda: example0(expect=[{"part_id": f"{MARK}", "issue": f"{MARK}"}]),
     "expect[0]: names a column that is not an output column", CFG),
    ("example expect rows twice", lambda: example0(expect=[{"part_id": f"{MARK}"}, {"part_id": f"{MARK}"}]),
     "expect: expect[0] and expect[1] are the same row", CFG),
    ("example expect 201 rows", lambda: example0(expect=[{"part_id": f"{MARK}{i}"} for i in range(201)]),
     "expect: has more than 200 rows", CFG),
    ("example expect n 0", lambda: single(kind(DUP, example=[{"spec": "s", "rows": {"bom_line": []},
                                                              "expect": [{"parent_id": f"{MARK}", "child_id": "c",
                                                                          "n": 0}]}])),
     "expect[0] n: n must be an integer >= 1", CFG),
    ("example expect n fraction", lambda: single(kind(DUP, example=[{"spec": "s", "rows": {"bom_line": []},
                                                                     "expect": [{"parent_id": f"{MARK}", "child_id": "c",
                                                                                 "n": "1.5"}]}])),
     "n must be an integer >= 1", CFG),
    ("example expect n NULL", lambda: single(kind(DUP, example=[{"spec": "s", "rows": {"bom_line": []},
                                                                 "expect": [{"parent_id": f"{MARK}", "child_id": "c",
                                                                             "n": {"null": True}}]}])),
     "n is never NULL", CFG),
    ("example expect unknown kind", lambda: single(kind(HEIGHT, example=[{"spec": "s", "rows": {
        "part": [{"part_id": f"{MARK}", "stored_level": 1}], "bom_line": []},
        "expect": [{"part_id": f"{MARK}", "kind": f"{MARK}", "stored": 1, "height": 0}]}])),
     "kind must be one of has_children_stored_zero", CFG),
    ("example expect negative height", lambda: single(kind(HEIGHT, example=[{"spec": "s", "rows": {
        "part": [{"part_id": f"{MARK}", "stored_level": 1}], "bom_line": []},
        "expect": [{"part_id": f"{MARK}", "kind": "stored_above", "stored": 1, "height": -1}]}])),
     "height must be an integer >= 0", CFG),
    ("example expect divisor 0", lambda: single(kind(TEXT, report={"columns": ["parent_id"]}, example=[{
        "spec": "s", "rows": {"bom_line": [], "part": []},
        "expect": [{"parent_id": f"{MARK}", "numerator": "1", "divisor": "0", "n": 1}]}])),
     "divisor must be an integer >= 1", CFG),
    ("example expect numerator 0", lambda: single(kind(TEXT, report={"columns": ["parent_id"]}, example=[{
        "spec": "s", "rows": {"bom_line": [], "part": []},
        "expect": [{"parent_id": f"{MARK}", "numerator": "0", "divisor": "1", "n": 1}]}])),
     "numerator must be greater than 0", CFG),
    # names in the output schema
    ("view name equals a closure table", lambda: single(kind(ROW, id="a_closure")),
     "rule[0] a_closure: its lookup view v_rule_a_closure would have the name of a relation build", CFG_COLLIDING),
    ("view name equals a cycles table", lambda: single(kind(ROW, id="a_cycles")), "its lookup view v_rule_a_cycles",
     CFG_COLLIDING),
]

# documents that must load (limits at their bounds and every optional form)
ACCEPTED = [
    ("base document", lambda: base(), CFG),
    ("id of 56 characters", lambda: edit((("rule", 0, "id"), "a" * 56)), CFG),
    ("128 rules", lambda: many_rules(128), CFG),
    ("32 atoms", lambda: where0(KIT_ATOM, {"has_children": False}, *[A] * 30), CFG),
    ("8 branches", lambda: where0(KIT_ATOM, {"any": [[{"has_children": False}]] + [[A]] * 7}), CFG),
    ("1000 set members", lambda: edit((("sets", "kit"), [f"{MARK}{i}" for i in range(1000)])), CFG),
    ("integer and boolean sets", lambda: single(kind(ROW, where=[{"column": "stored_level", "op": "in", "set": "ints"},
                                                                 {"column": "retired", "op": "eq", "value": False}]),
                                                sets={"ints": [1, 2]}), CFG),
    ("8 examples", lambda: edit((("rule", 0, "example"), [KIT_EXAMPLE] * 8)), CFG),
    ("200 input rows", lambda: example0(rows={"part": kit_rows(200), "bom_line": []}, expect=[]), CFG),
    ("4000 input cells", lambda: example0(rows={"part": kit_rows(100, 38), "bom_line": []}, expect=[]), CFG),
    ("extra example columns", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": f"{MARK}",
                                                              "name": f"{MARK}"}],
                                                     "bom_line": [{"line_id": 1, "parent_id": f"{MARK}",
                                                                   "child_id": f"{MARK}-c", "qty_per": "1"}]}), CFG),
    ("empty expect", lambda: example0(expect=[]), CFG),
    ("named hierarchy of two", lambda: edit((("hierarchy",), "has_component")), CFG_TWO_HIERARCHIES),
    ("no hierarchy for column rules", lambda: single(ROW), CFG_NO_HIERARCHY),
    ("quantified atom in a branch", lambda: where0(KIT_ATOM, {"any": [[{"children": "none",
                                                                        "where": [A, {"has_children": True}]}], [A]]}),
     CFG),
    ("endpoint structure", lambda: where1(UNIT_ATOM, {"end": "child", "has_parents": False}), CFG),
    ("endpoint null test", lambda: where1(UNIT_ATOM, {"end": "child", "column": "unit", "op": "is_not_null"}), CFG),
    ("ref without where", lambda: edit((("refs", "codes", "where"), DROP)), CFG),
    ("edge subject child", lambda: edit((("rule", 1, "subject"), "child_id")), CFG),
    ("report key on the edge entity", lambda: edit((("rule", 1, "report"), "key"), (("rule", 1, "subject"), DROP)),
     CFG),
    ("mode share 1 and ratio 1.5", lambda: single(kind(MODE, min_mode_share="1", ratio="1.5",
                                                       report={"columns": ["parent_id"]}, subject="parent_id")), CFG),
    ("duplicate with an endpoint part", lambda: single(kind(DUP, key=["parent_id", {"end": "child", "column": "name"}],
                                                            count={"distinct": "child_id"},
                                                            report={"distinct": "parent_id"}, subject="parent_id")),
     CFG),
    ("same_signature with group_id subject", lambda: single(kind(SIG, subject="group_id", min_members=3)), CFG),
    ("stored_metric with subject", lambda: single(kind(HEIGHT, subject="part_id")), CFG),
    ("text_quantity bounds", lambda: single(kind(TEXT, rel_tol="0", max_text_length=1)), CFG),
    ("text_quantity largest text", lambda: single(kind(TEXT, max_text_length=1000000, end="parent",
                                                      where_end=[{"column": "unit", "op": "is_not_null"}])), CFG),
]


def refused_message(test: unittest.TestCase, doc, cfg) -> str:
    with test.assertRaises(RuleSpecError) as caught:
        load(doc, cfg)
    return str(caught.exception)


class RuleFileValidationTest(unittest.TestCase):
    def test_refused_documents_name_positions_and_never_constants(self):
        names = [name for name, *_ in REFUSED]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(REFUSED), 70)
        for name, build, fragment, cfg in REFUSED:
            with self.subTest(case=name):
                message = refused_message(self, build(), cfg)
                self.assertIn(fragment, message)
                self.assertTrue(message.startswith("data-rules.toml"), message)
                self.assertNotIn(MARK, message)
                self.assertNotIn("\x00", message)

    def test_accepted_documents_load(self):
        for name, build, cfg in ACCEPTED:
            with self.subTest(case=name):
                spec = load(build(), cfg)
                self.assertGreaterEqual(len(spec.rules), 1)

    def test_rule_file_errors_are_configuration_errors_through_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "crosscheck.toml").write_text(CONFIG.format(node_key="part_id",
                                                                hierarchies=HIERARCHY.format(name="has_component"),
                                                                extra=DATA_RULES), encoding="utf-8")
            cfg = config.load(root / "crosscheck.toml")
            for name, build, fragment, _cfg in REFUSED[:40]:
                if _cfg is not CFG:
                    continue
                with self.subTest(case=name):
                    (root / "data-rules.toml").write_bytes(toml_doc(build()))
                    with self.assertRaises(StageError) as caught:
                        config.load_data_rules(cfg)
                    self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
                    self.assertIn(fragment, str(caught.exception))
                    self.assertNotIn(MARK, str(caught.exception))

    def test_dates_and_times_are_not_constants(self):
        for value in ("1979-05-27", "07:32:00", "1979-05-27T07:32:00"):
            with self.subTest(value=value):
                raw = toml_doc(base()).decode().replace(f'"{MARK}-KIT"]', f'{value}]', 1).encode()
                message = refused_message(self, raw, CFG)
                self.assertIn("sets.kit[0]: a member must be a string, an integer or a boolean", message)

    def test_unused_set_is_refused_in_the_verbatim_appendix(self):
        text = (EXAMPLE / "data-rules.toml").read_text(encoding="utf-8")
        verbatim = text.replace('count_units = ["pc", "box"]\n', 'count_units = ["pc", "box"]\nbought = ["BUY"]\n', 1)
        self.assertNotEqual(verbatim, text)
        cfg = config.load(EXAMPLE / "crosscheck.toml")
        message = refused_message(self, verbatim.encode("utf-8"), cfg)
        self.assertEqual(message, "data-rules.toml sets.bought: is not used by any rule: delete it or use it")


class RuleFileLoadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def config(self, rule_file: str = "data-rules.toml"):
        text = CONFIG.format(node_key="part_id", hierarchies=HIERARCHY.format(name="has_component"),
                             extra=f'[data_rules]\nfile = "{rule_file}"\n')
        (self.root / "crosscheck.toml").write_text(text, encoding="utf-8")
        return config.load(self.root / "crosscheck.toml")

    def refused(self, cfg, out=None) -> str:
        with self.assertRaises(StageError) as caught:
            config.load_data_rules(cfg, out)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        return str(caught.exception)

    def test_a_valid_file_records_its_hash_and_label(self):
        cfg = self.config("rules/sub/my-rules.toml")
        path = self.root / "rules" / "sub" / "my-rules.toml"
        path.parent.mkdir(parents=True)
        raw = toml_doc(base())
        path.write_bytes(raw)
        spec = config.load_data_rules(cfg)
        self.assertEqual(spec.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(spec.label, "my-rules.toml")
        self.assertEqual(cfg.data_rules.path(cfg), path.resolve())
        self.assertEqual([r.id for r in spec.subject_rules], ["kit_leaf", "unit_line"])
        self.assertEqual(spec.rule("unit_line").config_ref, "data_rules.rule[1] unit_line")

    def test_a_byte_order_mark_is_tolerated_and_hashed(self):
        cfg = self.config()
        raw = b"\xef\xbb\xbf" + toml_doc(base())
        (self.root / "data-rules.toml").write_bytes(raw)
        self.assertEqual(config.load_data_rules(cfg).sha256, hashlib.sha256(raw).hexdigest())

    def test_size_limit(self):
        cfg = self.config()
        raw = toml_doc(base())
        exact = raw + b"#" * (data_rules.MAX_FILE_BYTES - len(raw) - 1) + b"\n"
        self.assertEqual(len(exact), 256 * 1024)
        (self.root / "data-rules.toml").write_bytes(exact)
        config.load_data_rules(cfg)
        (self.root / "data-rules.toml").write_bytes(exact + b"\n")
        self.assertIn("larger than 256 KiB", self.refused(cfg))

    def test_missing_unreadable_and_malformed_files(self):
        cfg = self.config()
        self.assertIn("data-rules.toml: the rule file named by [data_rules] file was not found", self.refused(cfg))
        (self.root / "data-rules.toml").write_bytes(b"version = 1\n\xff\xfe" + MARK.encode())
        self.assertIn("is not UTF-8 text", self.refused(cfg))
        (self.root / "data-rules.toml").write_bytes(f'version = 1\nx = "{MARK}'.encode())
        message = self.refused(cfg)
        self.assertEqual(message, "data-rules.toml: is not valid TOML (end of document)")
        (self.root / "data-rules.toml").write_bytes(f'version = 1\nx = "{MARK}\x01"\n'.encode())
        message = self.refused(cfg)
        self.assertRegex(message, r"^data-rules\.toml: is not valid TOML \(line 2, column \d+\)$")
        self.assertNotIn("\x01", message)
        self.assertNotIn(MARK, message)

    def test_input_must_be_bytes_and_readable(self):
        with self.assertRaises(RuleSpecError) as caught:
            data_rules.loads(toml_doc(base()).decode(), CFG)
        self.assertEqual(str(caught.exception), "data-rules.toml: rule file input must be bytes")
        cfg = self.config()
        (self.root / "data-rules.toml").write_bytes(toml_doc(base()))
        with mock.patch.object(data_rules, "open", create=True, side_effect=PermissionError(13, MARK)):
            message = self.refused(cfg)
        self.assertEqual(message, "data-rules.toml: the rule file could not be read")

    def test_a_rule_file_inside_the_output_directory_is_refused(self):
        cfg = self.config(".sxc/data-rules.toml")
        (self.root / ".sxc").mkdir()
        (self.root / ".sxc" / "data-rules.toml").write_bytes(toml_doc(base()))
        for out in (self.root / ".sxc", self.root, self.root / ".sxc" / ".." / ".sxc"):
            with self.subTest(out=out.name):
                self.assertIn("must not lie inside the output directory", self.refused(cfg, out))
        self.assertEqual(len(config.load_data_rules(cfg, self.root / "elsewhere").rules), 3)
        self.assertEqual(len(config.load_data_rules(cfg).rules), 3)

    def test_without_a_data_rules_table(self):
        text = CONFIG.format(node_key="part_id", hierarchies="", extra="")
        (self.root / "crosscheck.toml").write_text(text, encoding="utf-8")
        cfg = config.load(self.root / "crosscheck.toml")
        self.assertIsNone(cfg.data_rules)
        self.assertIn("[data_rules]", self.refused(cfg))
        with self.assertRaises(RuleSpecError):
            data_rules.load(cfg)


class DataRulesConfigTest(unittest.TestCase):
    def parse(self, table: str):
        return config.loads((CONFIG.format(node_key="part_id", hierarchies="", extra="") + table).encode(), CFG_PATH)

    def refused(self, table: str) -> str:
        with self.assertRaises(StageError) as caught:
            self.parse(table)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        return str(caught.exception)

    def test_defaults(self):
        cfg = self.parse('[data_rules]\nfile = "data-rules.toml"\n')
        self.assertEqual(cfg.data_rules, config.DataRules("data-rules.toml", True, True, 200000, 200000))
        self.assertIsNone(self.parse("").data_rules)
        self.assertIn("data_rules", config.TABLES)

    def test_every_key(self):
        cfg = self.parse('[data_rules]\nfile = "r/x.toml"\nviews = false\nrequire_examples = false\n'
                         'max_export_rows = 1\nmax_rule_rows = 2000000\n')
        self.assertEqual(cfg.data_rules, config.DataRules("r/x.toml", False, False, 1, 2000000))

    def test_file_paths(self):
        values = ["", " ", "/abs/rules.toml", "C:/rules.toml", "C:\\rules.toml", "C:rules.toml",
                  "\\\\server\\share\\rules.toml", "../rules.toml", "rules/../x.toml", "./rules.toml", "a//rules.toml",
                  "rules.toml/", "a:b.toml", "https://example.org/rules.toml", "a\tb.toml", "a\u0001b.toml"]
        for value in values:
            with self.subTest(value=value):
                message = self.refused(f"[data_rules]\nfile = {json.dumps(value)}\n")
                self.assertIn("[data_rules.file]: must be a nonempty relative file path without traversal", message)
        for raw in ("true", "3", '["rules.toml"]'):
            with self.subTest(value=raw):
                self.assertIn("[data_rules.file]", self.refused(f"[data_rules]\nfile = {raw}\n"))
        self.assertIn("[data_rules.file]: is required", self.refused("[data_rules]\nviews = true\n"))

    def test_booleans_and_integers(self):
        cases = {"views = \"yes\"": "[data_rules.views]", "views = 1": "[data_rules.views]",
                 "require_examples = 0": "[data_rules.require_examples]",
                 "max_export_rows = 0": "[data_rules.max_export_rows]: must be an integer 1..2000000",
                 "max_export_rows = 2000001": "[data_rules.max_export_rows]",
                 "max_rule_rows = -1": "[data_rules.max_rule_rows]", "max_rule_rows = \"10\"": "[data_rules.max_rule_rows]",
                 "max_rule_rows = true": "[data_rules.max_rule_rows]", "max_rule_rows = 1.5": "[data_rules.max_rule_rows]"}
        for line, fragment in cases.items():
            with self.subTest(line=line):
                self.assertIn(fragment, self.refused(f'[data_rules]\nfile = "r.toml"\n{line}\n'))

    def test_closed_table(self):
        self.assertIn("[data_rules]: unknown configuration key", self.refused('[data_rules]\nfile = "r.toml"\nreport = 1\n'))
        raw = CONFIG.format(node_key="part_id", hierarchies="", extra="").replace("version = 1", "version = 1\ndata_rules = true")
        with self.assertRaises(StageError) as caught:
            config.loads(raw.encode(), CFG_PATH)
        self.assertIn("[data_rules]: must be a table", str(caught.exception))

    def test_semantic_preflight_paths_keep_their_messages(self):
        message = self.refused('[semantic_preflight]\ncontract = "../c.json"\nplan = "p.json"\n')
        self.assertEqual(message, "crosscheck.toml [semantic_preflight.contract]: must be a nonempty relative file path "
                                  "without traversal")


class PatternSubsetTest(unittest.TestCase):
    ACCEPT = {
        r"\[\s*([0-9.]+)\s*g\s*(?:/\s*([0-9]+)\s*)?\]": (2, 8),
        r"^SUB-([0-9]+)$": (1, 1),
        r"([0-9]{1,3}(?:\.[0-9]+)?)\s*mm": (1, 4),
        "치수([0-9]+)": (1, 1),
        r"x\d+": (0, 1),
        r"(?:a(?:b+))?c": (0, 2),
        r"x+y*": (0, 2),
        r"(?:ab){2}": (0, 1),
        r"(?:ab?){0,1}": (0, 2),
        r"(?:ab?){1}": (0, 2),
        r"[\[(]x[a-z0-9.&|~-]": (0, 0),
        "a?" * 16: (0, 16),
    }
    REFUSE = {
        r"(a|b)": "alternation", r"(\d)\1": "escape", r"(?=x)": "group extension", r"(?<=x)y": "group extension",
        r"(?P<n>x)": "group extension", r"(?i)x": "group extension", r"x*?": "lazy or possessive",
        r"x++": "lazy or possessive", r"x{2}?": "lazy or possessive", r"\w+": "escape", r"\W": "escape",
        r"\S": "escape", r"\D": "escape", r"\b": "escape", r"\B": "escape", r"\n": "escape",
        r"[[:alpha:]]": "posix class", r"[[.a.]]": "posix class", r"(x)*": "quantified capture",
        r"(x){2}": "quantified capture", r"x{300}": "bound range", r"x{3,2}": "bound range", r"x{,3}": "bound syntax",
        r"x{a}": "bound syntax", r"a]": "unescaped closer", r"a}": "unescaped closer", r"[a-Z]": "range",
        r"[0-z]": "range", r"x$y": "anchor position", r"x^": "anchor position", r"(x$)": "anchor position",
        r"(?:[0-9]+)+x": "repetition over a group that contains a quantifier",
        r"(?:ab*){2,3}": "repetition over a group that contains a quantifier",
        r"(?:a?)*": "repetition over a group that contains a quantifier",
        r"(?:(?:a+)?)+": "repetition over a group that contains a quantifier",
        r"(?:a+){2}": "repetition over a group that contains a quantifier",
        r"(?:a+){1,}": "repetition over a group that contains a quantifier",
        "a?" * 17: "more than 16 quantifiers", "a" * 257: "longer than 256 characters", "": "empty",
        "ab\\": "trailing backslash", "(ab": "unbalanced", "ab)": "unbalanced", "[ab": "unclosed class",
        "[]a]": "unescaped ] in class", "*a": "nothing to repeat", "a**": "nothing to repeat",
        "(*a)": "nothing to repeat", r"[\d]": "class escape", "a\x00": "NUL",
    }

    def test_accepted_patterns(self):
        for pattern, (captures, quantifiers) in self.ACCEPT.items():
            with self.subTest(pattern=pattern):
                p = data_rules.compile_subset(pattern)
                self.assertEqual((p.captures, p.quantifiers), (captures, quantifiers))
                p.compile()

    def test_refused_patterns_name_the_construct_only(self):
        for pattern, reason in self.REFUSE.items():
            with self.subTest(pattern=pattern):
                with self.assertRaises(data_rules.PatternError) as caught:
                    data_rules.compile_subset(pattern)
                self.assertEqual(str(caught.exception), reason)
        for pattern in (MARK + "(a|b)", MARK + r"\w", MARK + "(?:a+)+", MARK + "a{999}"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(data_rules.PatternError) as caught:
                    data_rules.compile_subset(pattern)
                self.assertNotIn(MARK, str(caught.exception))

    def test_a_form_python_cannot_compile_is_refused(self):
        with mock.patch.object(data_rules.Pattern, "compile", side_effect=re.error(MARK)):
            with self.assertRaises(data_rules.PatternError) as caught:
                data_rules.compile_subset("a")
        self.assertEqual(str(caught.exception), "unsupported construct")

    def test_python_and_are_forms(self):
        p = data_rules.compile_subset(r"^a\d\s.b$")
        self.assertEqual(p.python, "^a[0-9][ \\t\\n\\r\\f\\v].b\\Z")
        self.assertEqual(p.are, "^a[0-9][ \\t\\n\\r\\f\\v].b$")
        self.assertEqual(data_rules.compile_subset("a-b/c").python, "a\\-b\\/c")
        self.assertEqual(data_rules.compile_subset("[a-c&-]").are, "[a-c\\&\\-]")

    def test_python_form_semantics(self):
        rx = data_rules.compile_subset(r"a\sb").compile()
        self.assertIsNotNone(rx.search("a b"))
        self.assertIsNone(rx.search("a\u3000b"))                  # \s is ASCII whitespace only
        self.assertIsNone(data_rules.compile_subset(r"\d").compile().search("\u0663"))   # \d is ASCII digits only
        rx = data_rules.compile_subset("ab$").compile()
        self.assertIsNone(rx.search("ab\n"))                       # $ is the end of the text, as in ARE
        self.assertIsNotNone(rx.search("ab"))
        self.assertIsNotNone(data_rules.compile_subset("a.b").compile().search("a\nb"))   # . matches a newline
        groups = data_rules.compile_subset(r"\[\s*([0-9.]+)\s*g\s*(?:/\s*([0-9]+)\s*)?\]").compile()
        self.assertEqual(groups.search("Shim [30 g / 4]").groups(), ("30", "4"))
        self.assertEqual(groups.search("Washer [12.5 g]").groups(), ("12.5", None))


SCHEMA = {
    "part": {"part_id": "text", "name": "text", "unit": "text", "part_class": "text", "procure": "text",
             "issue": "text", "retired": "boolean", "stored_level": "integer", "code4": "character(4)",
             "small": "smallint", "note": "character varying(3)"},
    "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text", "qty_per": "numeric(18,6)"},
    "code_value": {"code_group": "text", "code": "text"},
}


def plan(**overrides) -> data_rules.TypePlan:
    """The schema's type plan; overrides map "entity.column" to a declared type or a ColumnType."""
    columns = {(e, c): data_rules.declared_type(t) for e, cols in SCHEMA.items() for c, t in cols.items()}
    for name, value in overrides.items():
        entity, column = name.split("__")
        columns[(entity, column)] = value if isinstance(value, data_rules.ColumnType) else data_rules.declared_type(value)
    return data_rules.TypePlan(columns)


def typed_refused(test, doc, type_plan, cfg=CFG) -> str:
    spec = load(doc, cfg)
    with test.assertRaises(RuleSpecError) as caught:
        data_rules.apply_types(spec, type_plan)
    return str(caught.exception)


def row_example(rows: dict, expect: list) -> list:
    return [{"spec": f"{MARK} spec", "rows": rows, "expect": expect}]


TYPE_REFUSED = [
    ("numeral expected", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "gt",
                                                                             "value": f"{MARK}"}])),
     "the value does not suit number column qty_per", {}),
    ("boolean on a number", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "eq",
                                                                                "value": True}])),
     "the value does not suit number column qty_per", {}),
    ("numeric set member", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "in",
                                                                               "set": "q"}]),
                                          sets={"q": ["0.5", f"{MARK}"]}),
     "set q member [1] does not suit number column qty_per", {}),
    ("order on text", lambda: single(kind(ROW, where=[{"column": "issue", "op": "lt", "value": f"{MARK}"}])),
     "op lt is not allowed on text column issue", {}),
    ("order on bpchar", lambda: single(kind(ROW, where=[{"column": "code4", "op": "ge", "value": f"{MARK}"}])),
     "op ge is not allowed on text column code4", {}),
    ("integer on text", lambda: single(kind(ROW, where=[{"column": "issue", "op": "eq", "value": 7}])),
     "the value does not suit text column issue", {}),
    ("in on a boolean", lambda: single(kind(ROW, where=[{"column": "retired", "op": "in", "set": "b"}]),
                                       sets={"b": [True]}),
     "op in is not allowed on boolean column retired", {}),
    ("text on a boolean", lambda: single(kind(ROW, where=[{"column": "retired", "op": "eq", "value": f"{MARK}"}])),
     "the value does not suit boolean column retired", {}),
    ("starts_with on a number", lambda: single(kind(ROW, where=[{"column": "stored_level", "op": "starts_with",
                                                                 "set": "p"}]), sets={"p": [f"{MARK}"]}),
     "starts_with needs a text column", {}),
    ("ref of another class", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per",
                                                                                 "op": "not_in_ref", "ref": "codes"}]),
                                            refs={"codes": {"entity": "code_value", "column": "code"}}),
     "ref codes column code must have the type class of column qty_per", {}),
    ("ref constant of another class", lambda: single(kind(ROW, where=[{"column": "procure", "op": "not_in_ref",
                                                                       "ref": "codes"}]),
                                                     refs={"codes": {"entity": "code_value", "column": "code",
                                                                     "where": {"code_group": 5}}}),
     "refs.codes where.code_group: the constant does not suit the text column", {}),
    ("endpoint class", lambda: base(), "the hierarchy columns parent_id and child_id must have the type class",
     {"bom_line__child_id": "integer"}),
    ("subject class", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "is_null"}],
                                          report={"columns": ["parent_id"]}, subject="parent_id")),
     "row_rule subject: column parent_id must have the type class of the node key part_id",
     {"bom_line__parent_id": "integer"}),
    ("mode value text", lambda: single(kind(MODE, value="child_id")), "value column child_id must be number", {}),
    ("stored text", lambda: single(kind(HEIGHT, stored="name")), "stored column name must be number", {}),
    ("text_quantity text number", lambda: single(kind(TEXT, text="stored_level")),
     "text column stored_level must be text", {}),
    ("text_quantity quantity text", lambda: single(kind(TEXT, quantity="child_id")),
     "quantity column child_id must be number", {}),
    ("signature quantity text", lambda: single(SIG), "the hierarchy quantity column qty_per must be number",
     {"bom_line__qty_per": "text"}),
    ("unknown column", lambda: single(kind(ROW, where=[{"column": "colour", "op": "is_null"}])),
     "column colour is not a column of entity part in the connected database", {}),
    ("unknown example column", lambda: example0(rows={"part": [{"part_id": f"{MARK}", "issue": f"{MARK}",
                                                                "colour": f"{MARK}"}], "bom_line": []}),
     "rows.part[0] colour: is not a column of entity part", {}),
    ("example scale", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "is_null"}],
                                          report={"columns": ["qty_per"]},
                                          example=row_example({"bom_line": [{"qty_per": "1.0000001"}]}, []))),
     "rows.bom_line[0] qty_per: does not fit the column's declared type", {}),
    ("example precision", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "is_null"}],
                                              report={"columns": ["qty_per"]},
                                              example=row_example({"bom_line": [{"qty_per": "1" + "0" * 12}]}, []))),
     "does not fit the column's declared type", {}),
    ("example integer fraction", lambda: single(kind(ROW, where=[{"column": "stored_level", "op": "is_null"}],
                                                     example=row_example({"part": [{"part_id": f"{MARK}",
                                                                                    "stored_level": "2.5"}]}, []))),
     "rows.part[0] stored_level: does not fit", {}),
    ("example smallint range", lambda: single(kind(ROW, where=[{"column": "small", "op": "is_null"}],
                                                   example=row_example({"part": [{"part_id": f"{MARK}",
                                                                                  "small": 32768}]}, []))),
     "rows.part[0] small: does not fit", {}),
    ("example varchar length", lambda: single(kind(ROW, where=[{"column": "note", "op": "is_null"}],
                                                   example=row_example({"part": [{"part_id": f"{MARK}",
                                                                                  "note": f"{MARK}"}]}, []))),
     "rows.part[0] note: does not fit", {}),
    ("example bpchar length", lambda: single(kind(ROW, where=[{"column": "code4", "op": "is_null"}],
                                                  example=row_example({"part": [{"part_id": f"{MARK}",
                                                                                 "code4": f"{MARK}"}]}, []))),
     "rows.part[0] code4: does not fit", {}),
    ("example text in a boolean", lambda: single(kind(ROW, where=[{"column": "retired", "op": "is_null"}],
                                                      example=row_example({"part": [{"part_id": f"{MARK}",
                                                                                     "retired": "true"}]}, []))),
     "rows.part[0] retired: does not fit", {}),
    ("example number in text", lambda: single(kind(ROW, where=[{"column": "issue", "op": "is_null"}],
                                                   example=row_example({"part": [{"part_id": f"{MARK}",
                                                                                  "issue": 3}]}, []))),
     "rows.part[0] issue: does not fit", {}),
    ("example numeric node keys equal by value", lambda: single(kind(ROW, where=[A], example=row_example(
        {"part": [{"part_id": 1, "issue": "a"}, {"part_id": "1.0", "issue": "b"}]}, []))),
     "rows.part: rows[0] and rows[1] have the same key once values are read", {"part__part_id": "integer",
                                                                              "bom_line__parent_id": "integer",
                                                                              "bom_line__child_id": "integer"}),
    ("example bpchar node keys equal without pads", lambda: single(kind(ROW, where=[A], example=row_example(
        {"part": [{"part_id": "AB", "issue": "a"}, {"part_id": "AB ", "issue": "b"}]}, []))),
     "have the same key once values are read", {"part__part_id": "character(3)", "bom_line__parent_id": "character(3)",
                                                "bom_line__child_id": "character(3)"}),
    ("expect rows equal by value", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per",
                                                                                       "op": "is_null"}],
                                                       report={"columns": ["qty_per"]},
                                                       example=row_example({"bom_line": []},
                                                                           [{"qty_per": "2", "n": 1},
                                                                            {"qty_per": "2.000", "n": 1}]))),
     "expect[0] and expect[1] are the same row once values are read", {}),
    ("expect cell does not fit", lambda: single(kind(ROW, entity="bom_line", where=[{"column": "qty_per",
                                                                                     "op": "is_null"}],
                                                     report={"columns": ["qty_per"]},
                                                     example=row_example({"bom_line": []},
                                                                         [{"qty_per": "0.0000001", "n": 1}]))),
     "expect[0] qty_per: does not fit the declared type of the column it comes from", {}),
]


class TypePlanTest(unittest.TestCase):
    def test_type_incompatibilities_are_configuration_errors(self):
        for name, build, fragment, overrides in TYPE_REFUSED:
            with self.subTest(case=name):
                message = typed_refused(self, build(), plan(**overrides))
                self.assertIn(fragment, message)
                self.assertTrue(message.startswith("data-rules.toml"))
                self.assertNotIn(MARK, message)

    def test_unsupported_columns_make_rules_not_run(self):
        spec = load(base())
        self.assertEqual(data_rules.apply_types(spec, plan()), {})
        self.assertEqual(data_rules.apply_types(spec, plan(part__issue="timestamp without time zone")),
                         {"kit_leaf": "column_type"})
        self.assertEqual(data_rules.apply_types(spec, plan(part__procure=data_rules.declared_type("text", False))),
                         {"unknown_code": "collation"})
        self.assertEqual(data_rules.apply_types(spec, plan(code_value__code="uuid")), {"unknown_code": "column_type"})
        # a rule that is not run is still type-checked where it can be
        doc = single(kind(ROW, where=[{"column": "name", "op": "is_null"},
                                      {"column": "issue", "op": "lt", "value": f"{MARK}"}]))
        message = typed_refused(self, doc, plan(part__name="date"))
        self.assertIn("op lt is not allowed on text column issue", message)

    def test_numbers_accept_integers_and_numerals(self):
        doc = single(kind(ROW, entity="bom_line", where=[{"column": "qty_per", "op": "ge", "value": 3},
                                                         {"column": "qty_per", "op": "lt", "value": "-0.5"},
                                                         {"column": "line_id", "op": "in", "set": "ids"}]),
                     sets={"ids": ["1", "2.5", "-3"]})
        self.assertEqual(data_rules.apply_types(load(doc), plan()), {})


class ModelTest(unittest.TestCase):
    def test_view_names(self):
        self.assertEqual(data_rules.view_name(CFG, "qty_over_limit"), "sxc_out.v_rule_qty_over_limit")
        self.assertEqual(data_rules.subjects_view_name(CFG), "sxc_out.v_rules_subjects")
        self.assertEqual(data_rules.SUBJECTS_VIEW, "v_rules_subjects")
        longest = data_rules.view_name(CFG, "a" * 56)
        self.assertEqual(len(longest.split(".")[1]), 63)
        with self.assertRaises(ident.IdentError):
            data_rules.view_name(CFG, "a" * 57)
        for rule_id in ("s_subjects", "s", "subjects", "_subjects"):
            self.assertNotEqual(data_rules.VIEW_PREFIX + rule_id, data_rules.SUBJECTS_VIEW)

    def test_reserved_names_are_the_names_naming_generates(self):
        text = CONFIG.format(node_key="part_id", hierarchies=HIERARCHY.format(name="has_component")
                             + HIERARCHY.format(name="uses_part"), extra=DATA_RULES)
        text += '[[shapes]]\nid = "qty_positive"\ntarget = "bom_line"\npath = "qty_per"\nmin_exclusive = 0\n'
        cfg = config.loads(text.encode(), CFG_PATH)
        generated = {naming.shape_view(cfg, s.id) for s in cfg.shapes}
        for h in cfg.hierarchies:
            generated |= {naming.closure_table(cfg, h), naming.cycles_table(cfg, h)}
        self.assertEqual({name.split(".", 1)[1] for name in generated}, data_rules.reserved_names(cfg))
        spec = data_rules.load(config.load(EXAMPLE / "crosscheck.toml"))
        views = {data_rules.view_name(CFG, r.id).split(".")[1] for r in spec.rules} | {data_rules.SUBJECTS_VIEW}
        self.assertEqual(len(views), len(spec.rules) + 1)
        self.assertFalse(views & data_rules.reserved_names(config.load(EXAMPLE / "crosscheck.toml")))

    def test_a_subjects_view_collision_is_refused(self):
        with mock.patch.object(data_rules, "reserved_names", return_value={data_rules.SUBJECTS_VIEW}):
            with self.assertRaises(RuleSpecError) as caught:
                load(base())
        self.assertIn("the subjects view v_rules_subjects would have the name of a relation build", str(caught.exception))

    def test_canonical_cells(self):
        cell = data_rules.cell
        self.assertEqual([cell(v, "number") for v in ("2.000", "-0.0", 7, "0.333300", "-12.50")],
                         ["2", "0", "7", "0.3333", "-12.5"])
        self.assertEqual([cell(v, "text") for v in (" a ", "A", "e\u0301", "\u00e9")], [" a ", "A", "e\u0301", "\u00e9"])
        self.assertEqual(cell("AB  ", "bpchar"), "AB")
        self.assertEqual(cell(" AB", "bpchar"), " AB")
        self.assertIs(cell(True, "boolean"), True)
        self.assertIsNone(cell(None, "number"))
        with self.assertRaises(ValueError):
            cell("x", "other")

    def test_typed_sort_order(self):
        def ordered(values, cls):
            return sorted(values, key=lambda v: data_rules.sort_key((v,), (cls,)))
        self.assertEqual(ordered(["10", "2", None, "-1", "2.5"], "number"), [None, "-1", "2", "2.5", "10"])
        self.assertEqual(ordered(["b", "B", None, "a", "\u00e9", "Z"], "text"), [None, "B", "Z", "a", "b", "\u00e9"])
        self.assertEqual(ordered([True, None, False], "boolean"), [None, False, True])
        rows = [("A", "2"), ("A", "10"), ("B", None)]
        self.assertEqual(sorted(rows, key=lambda r: data_rules.sort_key(r, ("text", "number"))),
                         [("A", "2"), ("A", "10"), ("B", None)])
        text = ["\u00e9", "z", "\U0001f600", "\uff21"]
        self.assertEqual(ordered(text, "text"), sorted(text, key=lambda s: s.encode("utf-8")))

    def test_declared_types(self):
        t = data_rules.declared_type
        self.assertEqual(t("numeric(18,6)"), data_rules.ColumnType("number", "numeric", 18, 6))
        self.assertEqual(t("numeric"), data_rules.ColumnType("number", "numeric"))
        self.assertEqual(t("character varying(20)"), data_rules.ColumnType("text", "varchar", length=20))
        self.assertEqual(t("character(3)"), data_rules.ColumnType("bpchar", "bpchar", length=3))
        self.assertEqual(t("character").length, 1)
        self.assertIsNone(t("bpchar").length)
        self.assertEqual([t(x).base for x in ("smallint", "integer", "bigint", "text", "boolean")],
                         ["int2", "int4", "int8", "text", "bool"])
        for other in ("date", "double precision", "integer[]", "uuid", "timestamp with time zone", "money"):
            with self.subTest(declared=other):
                self.assertEqual((t(other).type_class, t(other).reason, t(other).usable), ("other", "column_type", False))
        self.assertEqual(t("text", deterministic=False).reason, "collation")
        self.assertEqual(t("integer", deterministic=False).reason, "")

    def test_values_fit_their_declared_type(self):
        t = data_rules.declared_type
        fits = data_rules.fits
        cases = [
            ("numeric(18,6)", ["500.000001", "0.333300", 1, "-999999999999.999999", None],
             ["500.0000001", "1000000000000", True, "1e3", "abc", 1.5]),
            ("numeric(3,1)", ["99.9", "-99.9", "0.10"], ["100", "99.95"]),
            ("numeric(3,-1)", ["120", "9990", "0"], ["125", "1.5", "10000"]),
            ("numeric(2,3)", ["0.012", "-0.099"], ["0.1", "0.0123"]),
            ("numeric", ["1" + "0" * 40, "0." + "1" * 30], ["1e5"]),
            ("integer", [2147483647, "-2147483648", "2.000"], [2147483648, "2.5", False]),
            ("smallint", [32767], [32768, -32769]),
            ("bigint", [2 ** 63 - 1], [2 ** 63]),
            ("character varying(3)", ["abc", "", "가나다"], ["abcd", 3]),
            ("character(2)", ["AB", "A"], ["AB ", "ABC"]),
            ("text", ["anything at all"], [1, True]),
            ("boolean", [True, False], ["true", 1]),
        ]
        for declared, good, bad in cases:
            for value in good:
                with self.subTest(declared=declared, value=value):
                    self.assertTrue(fits(value, t(declared)))
            for value in bad:
                with self.subTest(declared=declared, value=value):
                    self.assertFalse(fits(value, t(declared)))


APPENDIX_A = {
    # id: (kind, severity, subject, output columns)
    "qty_over_limit": ("row", "violation", "parent_id", ("parent_id", "child_id", "qty_per", "n")),
    "parent_unknown": ("row", "violation", "parent_id", ("parent_id",)),
    "kit_without_bom": ("row", "violation", "part_id", ("part_id",)),
    "retired_child_in_active_parent": ("row", "violation", "parent_id", ("parent_id", "child_id", "n")),
    "all_children_retired": ("row", "violation", "part_id", ("part_id",)),
    "retired_assembly_in_use": ("row", "violation", "part_id", ("part_id",)),
    "unknown_procure_code": ("row", "warning", "part_id", ("part_id",)),
    "temporary_name": ("row", "warning", "part_id", ("part_id",)),
    "truncated_third": ("row", "warning", "parent_id", ("parent_id", "child_id", "qty_per", "n")),
    "repeated_line": ("duplicate", "warning", "parent_id", ("parent_id", "child_id", "n")),
    "same_name_components": ("duplicate", "warning", "parent_id", ("parent_id",)),
    "odd_quantity": ("mode_outlier", "warning", "parent_id", ("parent_id", "child_id", "qty_per", "mode", "n")),
    "twin_assemblies": ("same_signature", "warning", "member", ("group_id", "member")),
    "stored_level_drift": ("stored_metric", "warning", "", ("part_id", "kind", "stored", "height")),
    "grams_in_count_unit": ("text_quantity", "warning", "parent_id",
                            ("parent_id", "child_id", "qty_per", "numerator", "divisor", "n")),
}
APPENDIX_SCHEMA = {
    "part": {"part_id": "text", "name": "text", "unit": "text", "part_class": "text", "procure": "text",
             "issue": "text", "retired": "boolean", "stored_level": "integer"},
    "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text", "qty_per": "numeric(18,6)"},
    "code_value": {"code_group": "text", "code": "text"},
}


class CleanRoomExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load(EXAMPLE / "crosscheck.toml")
        cls.spec = config.load_data_rules(cls.cfg)

    def test_configuration(self):
        self.assertEqual(self.cfg.data_rules, config.DataRules("data-rules.toml", True, True, 200000, 200000))
        self.assertEqual(self.spec.hierarchy, data_rules.Hierarchy("has_component", "part", "bom_line", "part_id",
                                                                   "parent_id", "child_id", "qty_per", 16))
        self.assertEqual(sorted(self.spec.sets), ["count_units", "kit", "temporary_prefix", "truncated_thirds"])
        self.assertEqual(self.spec.refs["procure_codes"],
                         data_rules.Ref("procure_codes", "code_value", "code", (("code_group", "PROCURE"),)))

    def test_rules(self):
        found = {r.id: (r.kind, r.severity, r.subject, r.columns) for r in self.spec.rules}
        self.assertEqual(found, APPENDIX_A)
        self.assertEqual([r.id for r in self.spec.rules], list(APPENDIX_A))
        self.assertEqual([len(r.examples) for r in self.spec.rules], [1] * 15)
        self.assertEqual(len(self.spec.subject_rules), 14)

    def test_rule_details(self):
        rule = self.spec.rule
        kit = rule("kit_without_bom")
        self.assertEqual([type(a).__name__ for a in kit.where], ["ColumnAtom", "StructureAtom", "StructureAtom"])
        self.assertEqual(kit.reads, {"part": ("issue", "part_id"), "bom_line": ("parent_id", "child_id")})
        unknown = rule("unknown_procure_code").where[1]
        self.assertIsInstance(unknown, data_rules.AnyAtom)
        self.assertEqual([[a.op for a in branch] for branch in unknown.branches], [["not_in_ref"], ["is_null"]])
        children = rule("all_children_retired").where[1]
        self.assertEqual((children.direction, children.quantifier), ("children", "all"))
        names = rule("same_name_components")
        self.assertEqual([p.name for p in names.key], ["parent_id", "child_name"])
        self.assertEqual(names.count, "child_id")
        mode = rule("odd_quantity")
        self.assertEqual((mode.min_mode_share, mode.ratio, mode.min_distinct), ("0.75", "20", ("parent_id", 3)))
        text = rule("grams_in_count_unit")
        self.assertEqual((text.pattern.captures, text.pattern.quantifiers, text.rel_tol, text.max_text_length),
                         (2, 8, "0.001", 4096))
        self.assertEqual(rule("stored_level_drift").outputs[3], data_rules.Output("height", "integer"))
        example = rule("qty_over_limit").examples[0]
        self.assertEqual(example.rows["bom_line"][3]["qty_per"], None)
        self.assertEqual(example.expect, ({"parent_id": "A", "child_id": "C", "qty_per": "500.000001", "n": 2},))

    def test_types_of_the_example_schema(self):
        type_plan = data_rules.TypePlan({(e, c): data_rules.declared_type(t)
                                         for e, cols in APPENDIX_SCHEMA.items() for c, t in cols.items()})
        self.assertEqual(data_rules.apply_types(self.spec, type_plan), {})

    def test_example_patterns_capture_as_written(self):
        rx = self.spec.rule("grams_in_count_unit").pattern.compile()
        found = {name: rx.search(name).groups() for name in ("Washer [12.5 g]", "Shim [30 g / 4]", "Plate [1.2.3 g]")}
        self.assertEqual(found, {"Washer [12.5 g]": ("12.5", None), "Shim [30 g / 4]": ("30", "4"),
                                 "Plate [1.2.3 g]": ("1.2.3", None)})


class ImportBoundaryTest(unittest.TestCase):
    def test_data_rules_imports_only_the_standard_library_canon_and_ident(self):
        source = Path(data_rules.__file__).read_text(encoding="utf-8")
        allowed_local = {"canon", "ident"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names, alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    self.assertEqual(node.module, None, "relative imports name modules, not packages")
                    self.assertLessEqual({a.name for a in node.names}, allowed_local)
                else:
                    self.assertIn(node.module.split(".")[0], sys.stdlib_module_names, node.module)


if __name__ == "__main__":
    unittest.main()
