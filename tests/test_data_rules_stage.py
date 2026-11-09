"""The data-rules stage (stage_data_rules.py) and the rule views of build, without a database.

A fake pg8000 connection answers the catalog, gate, rule, lookup, export and example statements from canned rows:
the statement texts it knows are generated here with data_rules_sql for the same configuration, so a statement the
stage should not send fails the test. The canned SQL outputs are written by hand from the semantic contract, never
computed with the Python evaluation, so the Python path is checked against them. Covered: every status and reason of
the checks, the budget stop (no rule, lookup or export statement), the timeout hint, narrowing, the next_safe_action
order, the not_proven lines, the result boundary (synthetic row markers never reach result.json, the console or
rules.sql), the import independence of the two paths, Python mutants e1, e2 and e3 against correct SQL rows, and
build's view transaction (lock, savepoints, 42P16, orphans, the subjects view, build.sql). No database is needed.
"""

import ast
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import config, data_rules, data_rules_eval as ev, data_rules_sql as dsql, db, stage_build
from semantic_crosscheck import stage_data_rules as sdr
from semantic_crosscheck.result import Result, StageError

D = Decimal
MARK = "DRP-"                      # synthetic row-value marker: never in results, consoles or rules.sql
SECRET = "DRP-SECRET-MESSAGE"      # put into database error messages: never in results or consoles
CONFIG = """version = 1
[source]
schemas = ["sxc_rules"]
output_schema = "sxc_out"
statement_timeout_ms = 120000
[entities.part]
table = "sxc_rules.part"
key = ["part_id"]
[entities.bom_line]
table = "sxc_rules.bom_line"
key = ["line_id"]
[hierarchies.has_component]
edge = "bom_line"
node = "part"
parent = "parent_id"
child = "child_id"
quantity = "qty_per"
max_depth = 16
[data_rules]
file = "data-rules.toml"
"""
RULES = """version = 1
hierarchy = "has_component"

[sets]
thirds = ["0.3333"]

[[rule]]
id = "qty_over_limit"
kind = "row"
entity = "bom_line"
severity = "violation"
where = [{ column = "qty_per", op = "gt", value = "500" }]
report = { columns = ["parent_id", "child_id", "qty_per"] }
subject = "parent_id"
  [[rule.example]]
  spec = "a quantity above 500 is a typing error"
  rows.bom_line = [
    { line_id = 1, parent_id = "DRP-A", child_id = "DRP-B", qty_per = "500" },
    { line_id = 2, parent_id = "DRP-A", child_id = "DRP-C", qty_per = "500.5" },
  ]
  expect = [{ parent_id = "DRP-A", child_id = "DRP-C", qty_per = "500.5", n = 1 }]

[[rule]]
id = "parent_unknown"
kind = "row"
entity = "bom_line"
severity = "violation"
where = [{ end = "parent", exists = false }]
report = { distinct = "parent_id" }
subject = "parent_id"
  [[rule.example]]
  spec = "every parent is a part"
  rows.part = [{ part_id = "DRP-A" }]
  rows.bom_line = [
    { line_id = 1, parent_id = "DRP-A", child_id = "DRP-B" },
    { line_id = 2, parent_id = "DRP-X", child_id = "DRP-B" },
  ]
  expect = [{ parent_id = "DRP-X" }]

[[rule]]
id = "all_children_retired"
kind = "row"
entity = "part"
severity = "violation"
where = [
  { column = "retired", op = "eq", value = false },
  { children = "all", where = [{ column = "retired", op = "eq", value = true }] },
]
subject = "part_id"
  [[rule.example]]
  spec = "an active assembly needs an active component"
  rows.part = [
    { part_id = "DRP-A", retired = false }, { part_id = "DRP-R", retired = true },
    { part_id = "DRP-L", retired = false },
  ]
  rows.bom_line = [{ line_id = 1, parent_id = "DRP-A", child_id = "DRP-R" }]
  expect = [{ part_id = "DRP-A" }]

[[rule]]
id = "repeated_line"
kind = "duplicate"
entity = "bom_line"
severity = "warning"
key = ["parent_id", "child_id"]
subject = "parent_id"
  [[rule.example]]
  spec = "one parent lists one component once"
  rows.bom_line = [
    { line_id = 1, parent_id = "DRP-A", child_id = "DRP-B" },
    { line_id = 2, parent_id = "DRP-A", child_id = "DRP-B" },
  ]
  expect = [{ parent_id = "DRP-A", child_id = "DRP-B", n = 2 }]

[[rule]]
id = "third_qty"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ column = "qty_per", op = "in", set = "thirds" }]
  [[rule.example]]
  spec = "a truncated third"
  rows.bom_line = [{ line_id = 1, qty_per = "0.333300" }, { line_id = 2, qty_per = "0.3334" }]
  expect = [{ line_id = 1 }]

[[rule]]
id = "orphan_lines"
kind = "row"
entity = "bom_line"
severity = "warning"
where = [{ end = "parent", exists = false }]
  [[rule.example]]
  spec = "lines whose parent is not a part"
  rows.part = [{ part_id = "DRP-A" }]
  rows.bom_line = [
    { line_id = 1, parent_id = "DRP-A" }, { line_id = 2, parent_id = "DRP-X" },
    { line_id = 3, parent_id = { null = true } },
  ]
  expect = [{ line_id = 2 }]
"""
RULE_IDS = ("qty_over_limit", "parent_unknown", "all_children_retired", "repeated_line", "third_qty", "orphan_lines")
SUBJECT_RULES = ("qty_over_limit", "parent_unknown", "all_children_retired", "repeated_line")
SCHEMA = {"part": {"part_id": "text", "retired": "boolean"},
          "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text", "qty_per": "numeric(18,6)"}}


def part(pid, retired):
    return {"part_id": pid, "retired": retired}


def line(lid, parent, child, qty):
    return {"line_id": lid, "parent_id": parent, "child_id": child, "qty_per": None if qty is None else D(qty)}


# The snapshot's rows, as pg8000 returns them. Hand-written outputs below follow the semantic contract.
DATA = {
    "part": [part("DRP-A", False), part("DRP-B", True), part("DRP-C", True), part("DRP-D", False),
             part("DRP-E", False)],
    "bom_line": [line(1, "DRP-A", "DRP-B", "2.000000"), line(2, "DRP-A", "DRP-C", "600.500000"),
                 line(3, "DRP-A", "DRP-D", "1.000000"), line(4, "DRP-D", "DRP-B", "1.000000"),
                 line(5, "DRP-D", "DRP-C", "1.000000"), line(6, "DRP-X", "DRP-B", "3.000000"),
                 line(7, None, "DRP-E", "1.000000"), line(8, "DRP-D", "DRP-B", "1.000000"),
                 line(9, "DRP-A", "DRP-E", "0.333300")],
}
SQL_ROWS = {   # path 1: what the compiled SELECT of each rule returns on DATA
    "qty_over_limit": [["DRP-A", "DRP-C", D("600.500000"), 1]],    # line 2; the NULL-parent line 7 is 1.0
    "parent_unknown": [["DRP-X"]],                                  # line 6; line 7's NULL parent is no value
    "all_children_retired": [["DRP-D"]],      # D's children B and C are retired; E has no child; A has active D, E
    "repeated_line": [["DRP-D", "DRP-B", 2]],                       # lines 4 and 8
    "third_qty": [[9]],                                             # 0.333300 equals 0.3333 by value
    "orphan_lines": [[6]],                                          # X has no part row; line 7's parent is NULL
}
VIEW_ROWS = {rule_id: [row + [1] for row in rows] for rule_id, rows in SQL_ROWS.items()}   # each holds one row
SUBJECT_ROWS = [   # v_rules_subjects: worst severity, rule count, rule ids in configuration order; by rank, subject
    ["DRP-A", "violation", 0, 1, ["qty_over_limit"], 1],
    ["DRP-D", "violation", 0, 2, ["all_children_retired", "repeated_line"], 2],
    ["DRP-X", "violation", 0, 1, ["parent_unknown"], 3],
]
OIDS = {"text": (25, -1), "integer": (23, -1), "bigint": (20, -1), "boolean": (16, -1), "uuid": (2950, -1),
        "real": (700, -1), "numeric": (1700, -1)}


def catalog_type(decl: str) -> tuple:
    """(type_oid, type_mod, deterministic) of a declared type as the catalog holds it."""
    deterministic = not decl.endswith(" nondeterministic")
    decl = decl.removesuffix(" nondeterministic")
    if decl.startswith("numeric("):
        p, s = decl[8:-1].split(",")
        return 1700, ((int(p) << 16) | int(s)) + 4, deterministic
    if decl.startswith(("varchar(", "bpchar(")):
        n = int(decl.split("(")[1][:-1])
        return (1043 if decl.startswith("varchar") else 1042), n + 4, deterministic
    oid, mod = OIDS[decl]
    return oid, mod, deterministic


class Raise:
    """A canned answer that raises a database error with SQLSTATE code (its message holds a row-value marker)."""

    def __init__(self, code: str):
        self.code = code


def params_key(params: dict) -> tuple:
    return tuple(sorted(params.items()))


class FakeConn:
    """pg8000.native stand-in: answers by statement text (or text and parameters) and records every statement."""

    def __init__(self, world):
        self.world = world
        self.statements: list = []
        self.columns = None
        self.closed = False

    def sql(self) -> list:
        return [s for s, _p in self.statements]

    def run(self, sql: str, **params):
        self.statements.append((sql, dict(params)))
        self.columns = None
        hook = self.world.hooks.get(sql)
        if hook:
            hook()
        answer = self.world.answer(sql, params, self)
        if isinstance(answer, Raise):
            raise Exception({"C": answer.code, "M": f"failed near {SECRET}", "S": "ERROR"})
        return answer

    def close(self):
        self.closed = True


class World:
    """A temporary project (crosscheck.toml, rule file, output directory), its type plan and canned answers."""

    def __init__(self, rules=RULES, config_text=CONFIG, schema=None, data=None, *, settings="", build=True):
        self.dir = Path(tempfile.mkdtemp(prefix="sxc_data_rules_stage_"))
        text = config_text if not settings else config_text.replace('file = "data-rules.toml"\n',
                                                                    'file = "data-rules.toml"\n' + settings)
        (self.dir / "crosscheck.toml").write_text(text, encoding="utf-8", newline="\n")
        (self.dir / "data-rules.toml").write_text(rules, encoding="utf-8", newline="\n")
        self.out = self.dir / ".sxc"
        self.out.mkdir()
        self.cfg = config.load(self.dir / "crosscheck.toml")
        try:
            self.spec = data_rules.load(self.cfg)
        except data_rules.RuleSpecError:
            self.spec = None              # an invalid rule file or no [data_rules]: the stage must refuse it
        self.schema = {e: dict(cols) for e, cols in (schema or SCHEMA).items()}
        self.plan = data_rules.TypePlan({(e, c): self.column_type(e, c) for e, cols in self.schema.items()
                                         for c in cols})
        self.data = {e: [dict(r) for r in rows] for e, rows in (data or DATA).items()}
        self.encoding = "UTF8"
        self.sql_rows = {k: [list(r) for r in v] for k, v in SQL_ROWS.items()}
        self.view_rows = {k: [list(r) for r in v] for k, v in VIEW_ROWS.items()}
        self.subject_rows = [list(r) for r in SUBJECT_ROWS]
        self.example_rows: dict = {}      # (rule id, index) -> rows replacing the authored expect rows
        self.gate_rows: dict = {}         # (gate, entity) -> the gate's row replacing the computed one
        self.export_rows: dict = {}       # entity -> rows replacing DATA
        self.errors: dict = {}            # statement text -> SQLSTATE
        self.extra: dict = {}             # statement text -> answer (build tests)
        self.hooks: dict = {}             # statement text -> callable run before answering
        self.orphans: list = []           # relation names the orphan-view query returns (build)
        self.selection: list = []         # the --rule ids of the current run
        self.conn = None
        if build and self.spec is not None:
            try:
                self.write_build()
            except data_rules.RuleSpecError:
                pass                      # a rule the catalog refuses: the stage must stop with exit 2

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- the catalog ----------------------------------------------------------------------------------
    def column_type(self, entity: str, column: str):
        oid, mod, det = catalog_type(self.schema[entity][column])
        return dsql.column_type(oid, mod, det, "b")

    def usable(self, rules=None) -> tuple:
        """The rules the stage compiles: the selected ones (--rule) whose columns have supported types."""
        unusable = data_rules.apply_types(self.spec, self.plan)
        chosen = rules or tuple(r for r in self.spec.rules if not self.selection or r.id in self.selection)
        return tuple(r for r in chosen if r.id not in unusable)

    def statement(self, kind: str, rule_id: str = "", **kw):
        cfg, spec, plan = self.cfg, self.spec, self.plan
        s = cfg.data_rules
        if kind == "rule":
            return dsql.limited(dsql.rule_statement(cfg, spec, plan, spec.rule(rule_id)), s.max_rule_rows + 1)
        if kind == "lookup":
            return dsql.lookup_statement(cfg, spec.rule(rule_id), s.max_rule_rows + 1)
        if kind == "example":
            rule = spec.rule(rule_id)
            return dsql.example_statement(cfg, spec, plan, rule, rule.examples[kw.get("index", 0)])
        if kind == "export":
            columns = dsql.export_columns(cfg, spec, plan, kw.get("rules") or self.usable())[kw["entity"]]
            return dsql.export_statement(cfg, plan, kw["entity"], columns)
        if kind == "budget":
            return dsql.budget_statement(cfg, kw["entity"], s.max_export_rows + 1)
        raise AssertionError(kind)

    def sql_of(self, kind: str, rule_id: str = "", **kw) -> str:
        return self.statement(kind, rule_id, **kw).text

    # ---- answers --------------------------------------------------------------------------------------
    def answer(self, sql: str, params: dict, conn: FakeConn):
        if sql in self.errors:
            return Raise(self.errors[sql])
        if sql in self.extra:
            value = self.extra[sql]
            return value(params) if callable(value) else value
        if sql.startswith(("BEGIN", "SET LOCAL ", "ROLLBACK", "SAVEPOINT ", "RELEASE SAVEPOINT ", "COMMIT")):
            return []
        if sql.startswith(("CREATE OR REPLACE VIEW ", "CREATE VIEW ", "DROP VIEW ", "COMMENT ON VIEW ")):
            return []                                                     # build's view statements
        if sql == stage_build.LOCK_SQL:
            return [[None]]
        if sql.startswith("SELECT 1 FROM pg_namespace"):
            return [[1]]
        if sql.startswith("SELECT current_user"):
            return [["xc_check"]]
        if sql.startswith("SELECT has_schema_privilege"):
            return [[True, True]]
        if self.spec is not None and sql == dsql.orphan_statement(self.cfg).text:
            return [list(r) for r in self.orphans]
        if sql == stage_build.SOURCE_SQL:
            conn.columns = [{"name": n} for n in ("found", "usage", "readable", "columns")]
            entity = next((e for e, ent in self.cfg.entities.items() if ent.table == f"{params['s']}.{params['t']}"),
                          None)
            if entity is None or entity not in self.schema:
                return []
            return [[True, True, True, list(self.schema[entity])]]
        catalog = dsql.type_plan_statement(self.cfg, dsql.needed_entities(self.cfg, self.spec.rules))
        if sql == catalog.text:
            conn.columns = [{"name": n} for n in dsql._PLAN_COLUMNS]
            rows = []
            for entity, cols in self.schema.items():
                for column, decl in cols.items():
                    oid, mod, det = catalog_type(decl)
                    rows.append([entity, column, oid, mod, "b", det])
            return rows
        if sql == dsql.encoding_statement().text:
            return [[self.encoding]]
        return self.snapshot_answer(sql, params)

    def snapshot_answer(self, sql: str, params: dict):
        cfg, spec, plan = self.cfg, self.spec, self.plan
        s = cfg.data_rules
        for entity in cfg.entities:
            if sql == dsql.budget_statement(cfg, entity, 1).text:
                return [[min(len(self.data.get(entity, [])), params["limit"])]]
        for gate in dsql.snapshot_gates(cfg, spec, plan, self.usable(), s.max_export_rows):
            if gate.gate in ("encoding", "budget") or sql != gate.statement.text:
                continue
            if (gate.gate, gate.entity) in self.gate_rows:
                return [list(self.gate_rows[(gate.gate, gate.entity)])]
            return [self.gate_row(gate)]
        for rule in self.usable():
            if sql == self.sql_of("rule", rule.id):
                return self.sql_rows[rule.id]
            if sql == self.sql_of("lookup", rule.id):
                return self.view_rows[rule.id]
            for example in rule.examples:
                st = dsql.example_statement(cfg, spec, plan, rule, example)
                if sql == st.text and params == st.params:
                    return self.example_answer(rule, example)
        if sql == dsql.subjects_lookup_statement(cfg, 1).text:
            return self.subject_rows
        for entity, columns in dsql.export_columns(cfg, spec, plan, self.usable()).items():
            if sql == dsql.export_statement(cfg, plan, entity, columns).text:
                rows = self.export_rows.get(entity, self.data[entity])
                return [[r.get(c) for c in columns] for r in rows]
        raise AssertionError("fake: no answer for a statement the stage sent: " + sql[:120])

    def gate_row(self, gate) -> list:
        rows = self.data.get(gate.entity, [])
        if gate.gate == "key":
            keys = [tuple(r[c] for c in gate.columns) for r in rows]
            counts = Counter(k for k in keys if None not in k)
            return [sum(1 for k in keys if None in k), sum(1 for n in counts.values() if n > 1)]
        if gate.gate == "non_finite":
            return [sum(1 for r in rows if isinstance(r[c], Decimal) and not r[c].is_finite()) for c in gate.columns]
        if gate.gate == "null_quantity":
            h = self.spec.hierarchy
            return [sum(1 for r in rows
                        if r[h.parent] is not None and r[h.child] is not None and r[h.quantity] is None)]
        if gate.gate == "height":
            return [0]
        if gate.gate == "text_length":
            return [max((len(r[c]) for r in rows if r[c] is not None), default=None) for c in gate.columns]
        raise AssertionError(gate.gate)

    def example_answer(self, rule, example) -> list:
        if (rule.id, example.index) in self.example_rows:
            return self.example_rows[(rule.id, example.index)]
        classes = dsql.output_types(self.spec, self.plan, rule)
        out = []
        for row in example.expect:
            cells = []
            for name, cls in zip(rule.columns, classes):
                v = row[name]
                cells.append(D(v) if cls == "number" and isinstance(v, str) else v)
            out.append(cells)
        return out

    # ---- build evidence -------------------------------------------------------------------------------
    def write_build(self, missing=(), stale=(), statuses=None, subjects="pass"):
        """build/result.json and build/build.sql as build leaves them after creating every rule view."""
        res = Result("build")
        statements = []
        for rule in self.spec.rules:
            status = (statuses or {}).get(rule.id, "pass")
            if rule.id in missing:
                continue
            res.add(f"rule_view:{rule.id}", status, rule.config_ref, kind=rule.kind,
                    view=data_rules.view_name(self.cfg, rule.id))
            if rule.id not in stale and self.plan_usable(rule):
                statements.append(dsql.view_statement(self.cfg, self.spec, self.plan, rule))
        if self.spec.subject_rules and subjects is not None:
            res.add("rule_subjects:view", subjects, "data_rules")
            statements.append(dsql.subjects_view_statement(self.cfg, self.spec, self.plan))
        res.write(self.out)
        (self.out / "build" / "build.sql").write_text(stage_build.BUILD_SQL_HEADER + "\n" + "\n\n".join(statements)
                                                      + "\n", encoding="utf-8", newline="\n")

    def plan_usable(self, rule) -> bool:
        return rule.id not in data_rules.apply_types(self.spec, self.plan)

    # ---- running --------------------------------------------------------------------------------------
    def run(self, rules=()):
        self.conn = FakeConn(self)
        self.selection = list(rules)
        with mock.patch.object(db, "connect", return_value=self.conn):
            return sdr.run(self.cfg, types.SimpleNamespace(rule=list(rules)), self.out)

    def cli(self, *argv, env=True):
        """sxc.main for data-rules; env=False runs without SXC_PG_DSN and without the fake (db.connect refuses)."""
        self.conn = FakeConn(self)
        self.selection = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--rule"]
        buf = io.StringIO()
        connect = mock.patch.object(db, "connect", return_value=self.conn) if env else contextlib.nullcontext()
        with connect, mock.patch.dict("os.environ", {}, clear=False), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            os.environ.pop("SXC_PG_DSN", None)
            code = sxc.main(["data-rules", "--config", str(self.dir / "crosscheck.toml"), "--out", str(self.out),
                             *argv])
        return code, buf.getvalue()

    def result(self) -> dict:
        return json.loads((self.out / "data_rules" / "result.json").read_text(encoding="utf-8"))


def by_id(res: Result) -> dict:
    return {c.id: c for c in res.checks}


def statuses(res: Result) -> dict:
    return {c.id: c.status for c in res.checks}


class StageCase(unittest.TestCase):
    def world(self, *args, **kwargs) -> World:
        w = World(*args, **kwargs)
        self.addCleanup(w.close)
        return w


# ---- the whole flow -------------------------------------------------------------------------------------
class AgreementTest(StageCase):
    def test_every_rule_agrees_and_the_checks_follow_the_contract_order(self):
        w = self.world()
        res = w.run()
        expected = ["data_rules:snapshot", "data_rules:key:part", "data_rules:key:bom_line"]
        for rule_id in RULE_IDS:
            expected += [f"rule:{rule_id}", f"rule_xcheck:{rule_id}", f"rule_example:{rule_id}",
                         f"rule_lookup:{rule_id}"]
        expected.append("rule_subjects:lookup")
        self.assertEqual([c.id for c in res.checks], expected)
        got = statuses(res)
        for rule_id in RULE_IDS:
            with self.subTest(rule=rule_id):
                self.assertEqual(got[f"rule_xcheck:{rule_id}"], "pass")
                self.assertEqual(got[f"rule_example:{rule_id}"], "pass")
                self.assertEqual(got[f"rule_lookup:{rule_id}"], "pass")
        for rule_id in ("qty_over_limit", "parent_unknown", "all_children_retired"):   # violations with rows
            self.assertEqual(got[f"rule:{rule_id}"], "fail")
        for rule_id in ("repeated_line", "third_qty", "orphan_lines"):                  # warnings pass, counted
            self.assertEqual(got[f"rule:{rule_id}"], "pass")
        self.assertEqual(got["rule_subjects:lookup"], "pass")
        self.assertEqual(got["data_rules:snapshot"], "pass")
        self.assertEqual(res.status, "fail")
        checks = by_id(res)
        self.assertEqual(checks["data_rules:snapshot"].detail, {"rows_part": 5, "rows_bom_line": 9, "rows_total": 14,
                                                                "limit": 200000})
        self.assertEqual(checks["rule:qty_over_limit"].detail,
                         {"kind": "row", "severity": "violation", "violations": 1, "outputs": 1})
        self.assertEqual(checks["rule:repeated_line"].detail["violations"], 1)
        self.assertEqual(checks["rule_xcheck:third_qty"].detail["sql_outputs"], 1)
        self.assertEqual(checks["rule_subjects:lookup"].detail["subjects"], 3)
        self.assertEqual(checks["rule:qty_over_limit"].config_ref, "data_rules.rule[0] qty_over_limit")
        self.assertIn(sdr.AGREEMENT_NOTE, res.not_proven)
        self.assertIn("query sxc_out.v_rules_subjects ORDER BY ord", res.next_safe_action)
        self.assertEqual(res.inputs["data_rules_sha256"], w.spec.sha256)
        self.assertTrue(w.conn.closed)

    def test_the_snapshot_is_one_read_only_repeatable_read_transaction(self):
        w = self.world()
        w.run()
        sql = w.conn.sql()
        begins = [i for i, s in enumerate(sql) if s.startswith("BEGIN")]
        self.assertEqual([sql[i] for i in begins], ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                                                    "BEGIN TRANSACTION READ ONLY"])
        snapshot = sql[begins[0]:begins[1]]
        self.assertEqual(snapshot[-1], "ROLLBACK")
        rules = [w.sql_of("rule", r) for r in RULE_IDS]
        lookups = [w.sql_of("lookup", r) for r in RULE_IDS]
        exports = [w.sql_of("export", entity=e) for e in ("part", "bom_line")]
        for text in rules + lookups + exports:
            self.assertEqual(snapshot.count(text), 1)
        self.assertLess(max(snapshot.index(t) for t in rules), min(snapshot.index(t) for t in lookups))
        self.assertLess(max(snapshot.index(t) for t in lookups), min(snapshot.index(t) for t in exports))
        for text in rules + lookups:                                      # each under its own savepoint
            i = snapshot.index(text)
            self.assertEqual((snapshot[i - 1], snapshot[i + 1]),
                             (f"SAVEPOINT {sdr.SAVEPOINT}", f"RELEASE SAVEPOINT {sdr.SAVEPOINT}"))
        examples = sql[begins[1]:]
        self.assertEqual(examples[-1], "ROLLBACK")
        for rule_id in RULE_IDS:
            self.assertIn(w.sql_of("example", rule_id), examples)
        self.assertFalse(any(s.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "COMMENT", "ALTER"))
                             for s in sql))

    def test_rules_sql_records_every_statement_with_placeholders_only(self):
        w = self.world()
        w.run()
        text = (w.out / "data_rules" / "rules.sql").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(dsql.RULES_SQL_HEADER))
        for rule_id in RULE_IDS:
            for kind in ("rule", "lookup", "example"):
                self.assertIn(w.sql_of(kind, rule_id) + ";", text)
        self.assertIn("-- catalog type plan", text)
        for value in (MARK, "0.3333", "500.5", "'500'"):
            self.assertNotIn(value, text)

    def test_a_stale_rules_sql_is_removed_before_the_run(self):
        w = self.world()
        (w.out / "data_rules").mkdir()
        (w.out / "data_rules" / "rules.sql").write_text("old", encoding="utf-8")
        with mock.patch.object(sdr, "take_snapshot", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                w.run()
        self.assertFalse((w.out / "data_rules" / "rules.sql").exists())


# ---- budgets, gates and snapshot errors ---------------------------------------------------------------
class GateTest(StageCase):
    def test_a_budget_overflow_issues_no_rule_lookup_or_export_statement(self):
        w = self.world(settings="max_export_rows = 13\n")
        res = w.run()
        got = statuses(res)
        self.assertEqual(got["data_rules:snapshot"], "not_run")
        self.assertEqual(by_id(res)["data_rules:snapshot"].detail["reason"], "resource_limit")
        for rule_id in RULE_IDS:
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "resource_limit")
            self.assertEqual(by_id(res)[f"rule_xcheck:{rule_id}"].detail["reason"], "resource_limit")
            self.assertEqual(by_id(res)[f"rule_lookup:{rule_id}"].detail["reason"], "rule_not_agreed")
            self.assertEqual(got[f"rule_example:{rule_id}"], "pass")         # examples read no table
        self.assertNotIn("data_rules:key:part", got)
        sent = set(w.conn.sql())
        for rule_id in RULE_IDS:
            self.assertNotIn(w.sql_of("rule", rule_id), sent)
            self.assertNotIn(w.sql_of("lookup", rule_id), sent)
        for entity in ("part", "bom_line"):
            self.assertNotIn(w.sql_of("export", entity=entity), sent)
        rules_sql = (w.out / "data_rules" / "rules.sql").read_text(encoding="utf-8")
        self.assertNotIn("-- rule ", rules_sql)
        self.assertNotIn("-- export ", rules_sql)
        self.assertTrue(any("max_export_rows = 13" in n for n in res.not_proven))
        self.assertIn("max_export_rows", res.next_safe_action)

    def test_max_rule_rows_stops_only_the_rule_that_returns_more(self):
        w = self.world(settings="max_rule_rows = 1\n")
        w.sql_rows["third_qty"] = [[9], [10]]
        res = w.run()
        self.assertEqual(by_id(res)["rule:third_qty"].detail["reason"], "resource_limit")
        self.assertEqual(by_id(res)["rule_xcheck:third_qty"].detail["reason"], "resource_limit")
        self.assertNotIn(w.sql_of("lookup", "third_qty"), w.conn.sql())
        self.assertEqual(statuses(res)["rule_xcheck:qty_over_limit"], "pass")
        self.assertEqual(statuses(res)["data_rules:snapshot"], "pass")
        self.assertIn("data rules not evaluated (resource_limit: more than data_rules.max_rule_rows = 1 output rows): "
                      "third_qty", res.not_proven)

    def test_a_server_encoding_other_than_utf8_runs_nothing(self):
        w = self.world()
        w.encoding = "LATIN1"
        res = w.run()
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot.status, snapshot.detail["reason"], snapshot.detail["encoding"]),
                         ("not_run", "server_encoding", "LATIN1"))
        for rule_id in RULE_IDS:
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "server_encoding")
            self.assertEqual(by_id(res)[f"rule_example:{rule_id}"].detail,
                             {"reason": "example_not_evaluable", "gate": "server_encoding", "examples": 1})
        self.assertFalse(any(s == w.sql_of("budget", entity="part") for s in w.conn.sql()))

    def test_duplicate_node_keys_fail_the_key_check_and_leave_dependent_rules_not_run(self):
        w = self.world()
        w.gate_rows[("key", "part")] = [0, 1]
        res = w.run()
        key = by_id(res)["data_rules:key:part"]
        self.assertEqual((key.status, key.detail), ("fail", {"null_keys": 0, "duplicate_keys": 1,
                                                             "reason": "duplicate_keys"}))
        self.assertEqual(key.config_ref, "entities.part")
        for rule_id in ("parent_unknown", "all_children_retired", "orphan_lines"):    # they read the node key
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "key_not_unique")
            self.assertNotIn(w.sql_of("rule", rule_id), w.conn.sql())
        for rule_id in ("qty_over_limit", "repeated_line", "third_qty"):
            self.assertEqual(statuses(res)[f"rule_xcheck:{rule_id}"], "pass")
        self.assertEqual(by_id(res)["rule_subjects:lookup"].detail["reason"], "incomplete")
        self.assertIn("duplicate keys", res.next_safe_action)

    def test_null_keys(self):
        w = self.world()
        w.gate_rows[("key", "bom_line")] = [2, 0]
        res = w.run()
        self.assertEqual(by_id(res)["data_rules:key:bom_line"].detail["reason"], "null_keys")
        for rule_id in ("third_qty", "orphan_lines"):                        # they report bom_line by key
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "key_null")

    def test_nan_in_a_numeric_column_leaves_the_rules_reading_it_not_run(self):
        w = self.world()
        w.gate_rows[("non_finite", "bom_line")] = [1]
        res = w.run()
        for rule_id in ("qty_over_limit", "third_qty"):
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "non_finite_values")
            self.assertEqual(by_id(res)[f"rule_xcheck:{rule_id}"].detail["reason"], "non_finite_values")
        self.assertEqual(statuses(res)["rule_xcheck:repeated_line"], "pass")
        self.assertTrue(any(n.startswith("data rules not evaluated (non_finite_values): qty_over_limit, third_qty")
                            for n in res.not_proven))

    def test_unsupported_types_and_nondeterministic_collations_are_not_run(self):
        schema = {"part": {"part_id": "text", "retired": "uuid"},
                  "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text nondeterministic",
                               "qty_per": "numeric(18,6)"}}
        w = self.world(schema=schema)
        res = w.run()
        retired = by_id(res)["rule:all_children_retired"]
        self.assertEqual((retired.status, retired.detail["reason"]), ("not_run", "column_type"))
        self.assertEqual(by_id(res)["rule_xcheck:all_children_retired"].detail["reason"], "column_type")
        self.assertEqual(by_id(res)["rule_example:all_children_retired"].detail,
                         {"reason": "example_not_evaluable", "gate": "column_type", "examples": 1})
        for rule_id in ("qty_over_limit", "repeated_line"):                  # they read child_id
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "collation")
        self.assertEqual(statuses(res)["rule_xcheck:third_qty"], "pass")
        self.assertEqual(by_id(res)["rule_lookup:all_children_retired"].detail["reason"], "view_stale")

    def test_a_python_gate_that_trips_after_the_sql_gates_passed_is_not_run(self):
        w = self.world()
        w.data["part"] = DATA["part"] + [part("DRP-A", True)]             # a duplicate node key in the export ...
        w.gate_rows[("key", "part")] = [0, 0]                             # ... that the canned SQL gate missed
        res = w.run()
        check = by_id(res)["rule_xcheck:all_children_retired"]
        self.assertEqual((check.status, check.detail["reason"], check.detail["not_evaluated"]),
                         ("not_run", "key_not_unique", "python"))
        self.assertEqual(by_id(res)["rule:all_children_retired"].detail["reason"], "key_not_unique")

    def test_a_timeout_in_a_rule_is_an_error_with_the_timeout_hint(self):
        w = self.world()
        w.errors[w.sql_of("rule", "parent_unknown")] = "57014"
        res = w.run()
        rule = by_id(res)["rule:parent_unknown"]
        self.assertEqual((rule.status, rule.detail["sqlstate"], rule.detail["timeout_ms"]), ("error", "57014", 120000))
        xcheck = by_id(res)["rule_xcheck:parent_unknown"]
        self.assertEqual((xcheck.status, xcheck.detail["reason"], xcheck.detail["failed"]),
                         ("not_run", "path_error", "sql"))
        self.assertEqual(by_id(res)["rule_lookup:parent_unknown"].detail["reason"], "rule_not_agreed")
        self.assertEqual(statuses(res)["rule_xcheck:qty_over_limit"], "pass")   # the others still ran
        self.assertIn("raise source.statement_timeout_ms (now 120000)", res.next_safe_action)
        self.assertIn("rule:parent_unknown hit a database error (SQLSTATE 57014)", res.next_safe_action)
        self.assertEqual(res.status, "error")

    def test_a_database_error_outside_the_savepoints_ends_the_snapshot(self):
        w = self.world()
        w.errors[w.sql_of("export", entity="bom_line")] = "40001"
        res = w.run()
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot.status, snapshot.detail["sqlstate"]), ("error", "40001"))
        for rule_id in RULE_IDS:
            self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "snapshot_not_read")
            self.assertEqual(statuses(res)[f"rule_example:{rule_id}"], "pass")
        self.assertIn("data_rules:snapshot hit a database error (SQLSTATE 40001)", res.next_safe_action)

    def test_an_export_with_another_row_count_than_the_budget_is_an_error(self):
        w = self.world()
        w.export_rows["bom_line"] = DATA["bom_line"][:-1]
        res = w.run()
        snapshot = by_id(res)["data_rules:snapshot"]
        self.assertEqual((snapshot.status, snapshot.detail["reason"], snapshot.detail["entity"]),
                         ("error", "export_incomplete", "bom_line"))
        self.assertEqual(by_id(res)["rule:third_qty"].detail["reason"], "snapshot_not_read")


# ---- the two paths ----------------------------------------------------------------------------------------
_EXISTS = ev._endpoint_exists
_QUANTIFIED = ev._quantified
_COLUMN_TEST = ev._column_test


def e1_null_endpoint_dangling(endpoint, nodes, exists):
    """(e1) a NULL endpoint treated as an endpoint without a node row."""
    return (endpoint in nodes) == exists


def e2_all_without_nonempty(quantifier, near, nodes, sub):
    """(e2) all without its test for at least one neighbour."""
    if quantifier == "all":
        return all(n in nodes and sub(nodes[n], n) for n in near)
    return _QUANTIFIED(quantifier, near, nodes, sub)


def e3_decimal_as_text(op, value, operand):
    """(e3) a Decimal compared as text (a numeric IN by its text)."""
    if isinstance(value, Decimal) and op in ("in", "not_in"):
        return (str(value) in {str(m) for m in operand}) == (op == "in")
    if isinstance(value, Decimal) and op in ("eq", "ne", "lt", "le", "gt", "ge"):
        return _COLUMN_TEST(op, str(value), str(operand))
    return _COLUMN_TEST(op, value, operand)


class PathsTest(StageCase):
    def test_python_mutants_make_the_cross_check_fail_against_correct_sql_rows(self):
        cases = {"e1": ("_endpoint_exists", e1_null_endpoint_dangling, "orphan_lines"),
                 "e2": ("_quantified", e2_all_without_nonempty, "all_children_retired"),
                 "e3": ("_column_test", e3_decimal_as_text, "third_qty")}
        for name, (attribute, mutant, rule_id) in cases.items():
            with self.subTest(mutant=name):
                w = self.world()
                with mock.patch.object(ev, attribute, mutant):
                    res = w.run()
                check = by_id(res)[f"rule_xcheck:{rule_id}"]
                self.assertEqual(check.status, "fail")
                self.assertGreater(check.detail["sql_only"] + check.detail["py_only"], 0)
                self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], "paths_disagree")
                self.assertEqual(by_id(res)[f"rule_lookup:{rule_id}"].detail["reason"], "rule_not_agreed")

    def test_without_a_mutant_the_same_world_agrees(self):
        res = self.world().run()
        for rule_id in ("orphan_lines", "all_children_retired", "third_qty"):
            self.assertEqual(statuses(res)[f"rule_xcheck:{rule_id}"], "pass")

    def test_a_disagreement_leaves_the_rule_not_run_and_counts_both_sides(self):
        w = self.world()
        w.sql_rows["parent_unknown"] = [["DRP-X"], ["DRP-Y"]]
        res = w.run()
        check = by_id(res)["rule_xcheck:parent_unknown"]
        self.assertEqual((check.status, check.detail["sql_only"], check.detail["py_only"],
                          check.detail["sql_outputs"], check.detail["py_outputs"]), ("fail", 1, 0, 2, 1))
        self.assertEqual(by_id(res)["rule:parent_unknown"].detail["reason"], "paths_disagree")
        self.assertEqual(by_id(res)["rule_subjects:lookup"].detail["reason"], "incomplete")
        self.assertIn("disagree on parent_unknown", res.next_safe_action)

    def test_numbers_compare_by_value_and_text_exactly(self):
        w = self.world()
        w.sql_rows["qty_over_limit"] = [["DRP-A", "DRP-C", D("600.5"), 1]]          # another scale, same value
        res = w.run()
        self.assertEqual(statuses(res)["rule_xcheck:qty_over_limit"], "pass")
        w2 = self.world()
        w2.sql_rows["parent_unknown"] = [["DRP-X "]]                                 # a trailing space differs
        self.assertEqual(statuses(w2.run())["rule_xcheck:parent_unknown"], "fail")

    def test_a_python_exception_is_an_evaluator_error(self):
        w = self.world()

        def broken(*_args):
            raise ArithmeticError(f"no {SECRET}")
        with mock.patch.object(ev, "_column_test", broken):
            res = w.run()
        rule = by_id(res)["rule:qty_over_limit"]
        self.assertEqual((rule.status, rule.detail["reason"], rule.detail["exception"]),
                         ("error", "evaluator", "ArithmeticError"))
        self.assertEqual(by_id(res)["rule_xcheck:qty_over_limit"].detail["failed"], "python")
        example = by_id(res)["rule_example:qty_over_limit"]
        self.assertEqual((example.status, example.detail["reason"], example.detail["exception"]),
                         ("error", "evaluator", "ArithmeticError"))
        self.assertEqual(statuses(res)["rule_xcheck:parent_unknown"], "pass")      # no column atom, no error
        self.assertIn("the Python evaluation raised ArithmeticError", res.next_safe_action)


# ---- authored examples ------------------------------------------------------------------------------------
class ExampleTest(StageCase):
    def test_common_mode_fails_the_example_and_leaves_the_rule_not_run(self):
        rules = RULES.replace('expect = [{ parent_id = "DRP-X" }]', 'expect = [{ parent_id = "DRP-A" }]')
        w = self.world(rules)
        w.example_rows[("parent_unknown", 0)] = [["DRP-X"]]               # the correct SQL output of the example
        res = w.run()
        example = by_id(res)["rule_example:parent_unknown"]
        self.assertEqual((example.status, example.detail["common_mode"], example.detail["sql_mismatch"]),
                         ("fail", 1, 0))
        rule = by_id(res)["rule:parent_unknown"]
        self.assertEqual((rule.status, rule.detail["reason"]), ("not_run", "example_mismatch"))
        self.assertEqual(statuses(res)["rule_xcheck:parent_unknown"], "pass")    # the real rows still agreed
        self.assertIn("common_mode", res.next_safe_action)

    def test_an_example_the_sql_gets_wrong_is_an_sql_mismatch(self):
        w = self.world()
        w.example_rows[("third_qty", 0)] = []
        res = w.run()
        example = by_id(res)["rule_example:third_qty"]
        self.assertEqual((example.status, example.detail["sql_mismatch"]), ("fail", 1))
        self.assertEqual(by_id(res)["rule:third_qty"].detail["reason"], "example_mismatch")
        self.assertIn("disagree on third_qty", res.next_safe_action)

    def test_an_example_the_python_gets_wrong_is_a_py_mismatch(self):
        w = self.world()
        with mock.patch.object(ev, "_quantified", e2_all_without_nonempty):
            res = w.run()
        self.assertEqual(by_id(res)["rule_example:all_children_retired"].detail["py_mismatch"], 1)

    def test_an_example_both_paths_get_wrong_differently_is_a_both_mismatch(self):
        w = self.world()
        w.example_rows[("all_children_retired", 0)] = []                  # SQL: nothing; Python (e2): A and L
        with mock.patch.object(ev, "_quantified", e2_all_without_nonempty):
            res = w.run()
        example = by_id(res)["rule_example:all_children_retired"]
        self.assertEqual((example.status, example.detail["both_mismatch"], example.detail["common_mode"]),
                         ("fail", 1, 0))

    def test_a_rule_without_an_example(self):
        rules = RULES.split("[[rule]]\nid = \"orphan_lines\"")[0] + (
            '[[rule]]\nid = "orphan_lines"\nkind = "row"\nentity = "bom_line"\nseverity = "warning"\n'
            'where = [{ end = "parent", exists = false }]\n')
        w = self.world(rules)
        res = w.run()
        self.assertEqual(by_id(res)["rule_example:orphan_lines"].detail, {"reason": "no_example"})
        self.assertEqual(statuses(res)["rule:orphan_lines"], "pass")             # only the example check
        self.assertEqual(statuses(res)["rule_example:orphan_lines"], "not_run")
        w2 = self.world(rules, settings="require_examples = false\n")
        res2 = w2.run()
        self.assertNotIn("rule_example:orphan_lines", statuses(res2))
        self.assertTrue(any(n.startswith("rule_example:orphan_lines: the rule has no authored example")
                            for n in res2.not_proven))

    def test_an_example_statement_error_is_an_error(self):
        w = self.world()
        w.errors[w.sql_of("example", "repeated_line")] = "22P02"
        res = w.run()
        example = by_id(res)["rule_example:repeated_line"]
        self.assertEqual((example.status, example.detail["sqlstate"], example.detail["example"]), ("error", "22P02", 0))
        self.assertEqual(statuses(res)["rule:repeated_line"], "pass")

    def test_a_python_gate_on_the_example_rows_makes_it_not_evaluable(self):
        w = self.world()
        with mock.patch.object(ev.Evaluator, "_gates", lambda self, rule: "cycle_or_depth_limit"
                               if rule.id == "repeated_line" and len(self._rows("bom_line")) == 2 else ""):
            res = w.run()
        self.assertEqual(by_id(res)["rule_example:repeated_line"].detail,
                         {"reason": "example_not_evaluable", "gate": "cycle_or_depth_limit", "examples": 1})


# ---- lookups ----------------------------------------------------------------------------------------------
class LookupTest(StageCase):
    def test_build_missing(self):
        w = self.world(build=False)
        res = w.run()
        for rule_id in RULE_IDS:
            self.assertEqual(by_id(res)[f"rule_lookup:{rule_id}"].detail["reason"], "build_missing")
            self.assertNotIn(w.sql_of("lookup", rule_id), w.conn.sql())
        self.assertEqual(by_id(res)["rule_subjects:lookup"].detail["reason"], "build_missing")
        self.assertIn("rerun `sxc.py build`, then `sxc.py data-rules`", res.next_safe_action)

    def test_view_not_built_and_view_stale(self):
        w = self.world()
        w.write_build(missing=("third_qty",), stale=("orphan_lines",), statuses={"repeated_line": "error"})
        res = w.run()
        reasons = {r: by_id(res)[f"rule_lookup:{r}"].detail.get("reason") for r in RULE_IDS}
        self.assertEqual(reasons["third_qty"], "view_not_built")
        self.assertEqual(reasons["repeated_line"], "view_not_built")
        self.assertEqual(reasons["orphan_lines"], "view_stale")
        self.assertIsNone(reasons["qty_over_limit"])
        self.assertEqual(statuses(res)["rule_lookup:qty_over_limit"], "pass")
        # repeated_line names a subject: without its view the subjects view cannot be current
        self.assertEqual(by_id(res)["rule_subjects:lookup"].detail["reason"], "view_not_built")
        w.write_build(stale=("parent_unknown",))
        self.assertEqual(by_id(w.run())["rule_subjects:lookup"].detail["reason"], "view_stale")

    def test_a_rule_file_edited_after_build_makes_the_view_stale(self):
        w = self.world()
        (w.dir / "data-rules.toml").write_text(RULES.replace('value = "500"', 'value = "499"'), encoding="utf-8")
        w.spec = data_rules.load(w.cfg)
        res = w.run()
        self.assertEqual(by_id(res)["rule_lookup:qty_over_limit"].detail["reason"], "view_stale")
        self.assertEqual(statuses(res)["rule_lookup:third_qty"], "pass")

    def test_a_missing_view_and_a_lookup_error(self):
        w = self.world()
        w.errors[w.sql_of("lookup", "third_qty")] = "42P01"
        w.errors[w.sql_of("lookup", "orphan_lines")] = "42501"
        w.errors[dsql.subjects_lookup_statement(w.cfg, 1).text] = "42P01"
        res = w.run()
        self.assertEqual(by_id(res)["rule_lookup:third_qty"].detail["reason"], "view_missing")
        lookup = by_id(res)["rule_lookup:orphan_lines"]
        self.assertEqual((lookup.status, lookup.detail["sqlstate"]), ("error", "42501"))
        self.assertEqual(by_id(res)["rule_subjects:lookup"].detail["reason"], "view_missing")
        self.assertIn("USAGE on the output schema", res.next_safe_action)

    def test_lookup_rows_that_differ_fail(self):
        w = self.world()
        w.view_rows["third_qty"] = [[9, 1], [10, 2]]
        w.view_rows["orphan_lines"] = []
        res = w.run()
        first = by_id(res)["rule_lookup:third_qty"]
        self.assertEqual((first.status, first.detail["lookup_only"], first.detail["agreed_only"]), ("fail", 1, 0))
        second = by_id(res)["rule_lookup:orphan_lines"]
        self.assertEqual((second.status, second.detail["lookup_only"], second.detail["agreed_only"]), ("fail", 0, 1))
        self.assertIn("rerun `sxc.py build`", res.next_safe_action)

    def test_an_ord_order_other_than_the_canonical_one_fails(self):
        w = self.world()
        w.sql_rows["third_qty"] = [[9], [10]]
        w.data["bom_line"] = DATA["bom_line"] + [line(10, "DRP-A", "DRP-E", "0.333300")]
        w.view_rows["third_qty"] = [[10, 1], [9, 2]]
        res = w.run()
        self.assertEqual(statuses(res)["rule_xcheck:third_qty"], "pass")
        lookup = by_id(res)["rule_lookup:third_qty"]
        self.assertEqual((lookup.status, lookup.detail.get("order_differs")), ("fail", True))
        w.view_rows["third_qty"] = [[9, 1], [10, 3]]                   # right rows, ord with a gap
        self.assertEqual(by_id(w.run())["rule_lookup:third_qty"].detail.get("order_differs"), True)
        w.view_rows["third_qty"] = [[9, 1], [10, 2]]
        self.assertEqual(statuses(w.run())["rule_lookup:third_qty"], "pass")

    def test_subject_rows_must_match_the_python_roll_up_in_order(self):
        w = self.world()
        w.subject_rows = [SUBJECT_ROWS[1][:-1] + [1], SUBJECT_ROWS[0][:-1] + [2], SUBJECT_ROWS[2]]
        self.assertEqual(by_id(w.run())["rule_subjects:lookup"].detail.get("order_differs"), True)
        w.subject_rows = [list(r) for r in SUBJECT_ROWS]
        w.subject_rows[1][4] = ["repeated_line", "all_children_retired"]           # rules out of order
        check = by_id(w.run())["rule_subjects:lookup"]
        self.assertEqual((check.status, check.detail["lookup_only"], check.detail["agreed_only"]), ("fail", 1, 1))
        w.subject_rows = [list(r) for r in SUBJECT_ROWS]
        w.subject_rows[1][1] = "warning"
        self.assertEqual(statuses(w.run())["rule_subjects:lookup"], "fail")
        w.subject_rows = [list(r) for r in SUBJECT_ROWS]
        w.errors[dsql.subjects_lookup_statement(w.cfg, 1).text] = "42501"
        check = by_id(w.run())["rule_subjects:lookup"]
        self.assertEqual((check.status, check.detail["sqlstate"]), ("error", "42501"))

    def test_views_false_reads_no_view_and_says_so(self):
        w = self.world(settings="views = false\n", build=False)
        res = w.run()
        self.assertFalse([c for c in res.checks if c.id.startswith(("rule_lookup:", "rule_subjects:"))])
        self.assertIn(sdr.VIEWS_OFF_NOTE, res.not_proven)
        self.assertFalse(any("v_rule" in s for s in w.conn.sql()))
        self.assertIn("set data_rules.views = true", res.next_safe_action)


# ---- narrowing, inputs and the command line -----------------------------------------------------------
class NarrowTest(StageCase):
    def test_rule_narrows_the_run_and_never_passes(self):
        w = self.world()
        res = w.run(rules=["third_qty", "third_qty"])
        ids = [c.id for c in res.checks]
        self.assertEqual(ids, ["data_rules:snapshot", "data_rules:key:bom_line", "rule:third_qty",
                               "rule_xcheck:third_qty", "rule_example:third_qty", "rule_lookup:third_qty",
                               "rule_subjects:lookup", "scope:rules"])
        self.assertEqual(by_id(res)["scope:rules"].detail, {"reason": "excluded_by_option", "rules_excluded": 5})
        self.assertEqual(by_id(res)["rule_subjects:lookup"].detail["reason"], "excluded_by_option")
        self.assertEqual(by_id(res)["data_rules:snapshot"].detail, {"rows_bom_line": 9, "rows_total": 9,
                                                                    "limit": 200000})
        self.assertNotIn(w.sql_of("rule", "qty_over_limit"), w.conn.sql())
        self.assertEqual(res.status, "not_run")
        self.assertIn("rules found rows (third_qty)", res.next_safe_action)    # a warning rule's rows come first

    def test_narrowing_to_every_subject_rule_still_checks_the_subjects(self):
        res = self.world().run(rules=list(SUBJECT_RULES))
        self.assertEqual(statuses(res)["rule_subjects:lookup"], "pass")
        self.assertEqual(by_id(res)["scope:rules"].detail["rules_excluded"], 2)

    def test_an_input_changed_during_the_run_is_an_error(self):
        w = self.world()
        w.hooks[dsql.encoding_statement().text] = lambda: (w.dir / "data-rules.toml").write_text(
            RULES + "\n# edited\n", encoding="utf-8")
        res = w.run()
        check = by_id(res)["data_rules:inputs"]
        self.assertEqual((check.status, check.detail["reason"]), ("error", "inputs_changed"))
        self.assertEqual(res.checks[-1].id, "data_rules:inputs")
        self.assertIn("changed while data-rules ran", res.next_safe_action)


class CliTest(StageCase):
    def test_the_cli_writes_the_result_with_the_rule_file_hash(self):
        w = self.world()
        code, console = w.cli()
        self.assertEqual(code, 1)
        saved = w.result()
        self.assertEqual(saved["stage"], "data_rules")
        raw = (w.dir / "data-rules.toml").read_bytes()
        self.assertEqual(saved["inputs"]["data_rules_sha256"], hashlib.sha256(raw).hexdigest())
        for key in ("config_sha256", "gold_sha256", "engine_sha256", "revision_query_sha256"):
            self.assertIn(key, saved["inputs"])
        self.assertIn("[sxc data_rules] FAIL", console)

    def test_row_values_and_messages_never_reach_results_consoles_or_rules_sql(self):
        w = self.world()
        w.errors[w.sql_of("lookup", "third_qty")] = "42501"
        w.errors[w.sql_of("example", "repeated_line")] = "22P02"
        w.sql_rows["parent_unknown"] = [["DRP-X"], ["DRP-Y"]]
        code, console = w.cli()
        files = [w.out / "data_rules" / "result.json", w.out / "data_rules" / "rules.sql"]
        texts = [console] + [f.read_text(encoding="utf-8") for f in files]
        for text in texts:
            self.assertNotIn(MARK, text)
            self.assertNotIn("SECRET", text)
            self.assertNotIn("0.3333", text)
        saved = w.result()
        for check in saved["checks"]:
            for value in check["detail"].values():
                self.assertIsInstance(value, (str, int, bool), check["id"])

    def test_exit_codes(self):
        w = self.world()
        cases = []
        no_table = self.world(config_text=CONFIG.split("[data_rules]")[0])
        cases.append(("no [data_rules]", no_table, (), 2))
        bad = self.world(RULES.replace('severity = "warning"\nwhere = [{ column = "qty_per", op = "in"',
                                       'severity = "minor"\nwhere = [{ column = "qty_per", op = "in"'))
        cases.append(("invalid rule file", bad, (), 2))
        cases.append(("unknown --rule", w, ("--rule", "no_such_rule"), 2))
        text_lt = self.world(RULES.replace('{ column = "qty_per", op = "gt", value = "500" }',
                                           '{ column = "child_id", op = "lt", value = "DRP-M" }'))
        cases.append(("lt on a text column", text_lt, (), 2))
        missing = self.world(schema={"part": {"part_id": "text", "retired": "boolean"},
                                     "bom_line": {"line_id": "integer", "parent_id": "text", "qty_per": "numeric"}})
        cases.append(("a missing column", missing, (), 2))
        for label, world, argv, expected in cases:
            with self.subTest(case=label):
                code, console = world.cli(*argv)
                self.assertEqual(code, expected, console)
                self.assertNotIn(MARK, console)
                self.assertNotIn("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", world.conn.sql())
        code, console = w.cli(env=False)
        self.assertEqual(code, 3, console)
        self.assertIn("SXC_PG_DSN", console)

    def test_the_rule_file_must_not_lie_in_the_output_directory(self):
        w = self.world(config_text=CONFIG.replace('file = "data-rules.toml"', 'file = ".sxc/data-rules.toml"'))
        shutil.copy(w.dir / "data-rules.toml", w.out / "data-rules.toml")
        code, console = w.cli()
        self.assertEqual(code, 2)
        self.assertIn("must not lie inside the output directory", console)


# ---- next_safe_action and not_proven ------------------------------------------------------------------
class NextActionTest(unittest.TestCase):
    def setUp(self):
        self.w = World(build=False)
        self.addCleanup(self.w.close)

    def action(self, *checks) -> str:
        res = Result("data_rules")
        for check_id, status, detail in checks:
            res.add(check_id, status, "data_rules", **detail)
        return sdr.next_action(self.w.cfg, res)

    def test_first_match_wins(self):
        error = ("rule:a", "error", {"sqlstate": "42601"})
        disagree = ("rule_xcheck:b", "fail", {"sql_only": 1, "py_only": 0})
        common = ("rule_example:c", "fail", {"common_mode": 1, "sql_mismatch": 0, "py_mismatch": 0,
                                              "both_mismatch": 0})
        gate = ("rule:d", "not_run", {"reason": "cycle_or_depth_limit"})
        lookup = ("rule_lookup:e", "not_run", {"reason": "view_stale"})
        found = ("rule:f", "fail", {"violations": 3})
        order = [error, disagree, common, gate, lookup, found]
        expected = ["SQLSTATE 42601", "disagree on b", "common_mode", "closure checks", "rerun `sxc.py build`",
                    "rules found rows (f)"]
        for i, text in enumerate(expected):
            with self.subTest(step=i):
                self.assertIn(text, self.action(*order[i:]))
        self.assertEqual(self.action(), "rerun data-rules after changing the data or the rule file")
        self.assertIn("without --rule", self.action(("scope:rules", "not_run", {"reason": "excluded_by_option"})))
        self.assertIn("rules found rows (g)", self.action(("rule:g", "pass", {"violations": 2})))   # a warning
        self.assertIn("[[rule.example]]", self.action(("rule_example:h", "not_run", {"reason": "no_example"})))
        self.assertIn("the authored examples of i could not be evaluated",
                      self.action(("rule_example:i", "not_run", {"reason": "example_not_evaluable",
                                                                 "gate": "cycle_or_depth_limit"})))
        self.assertIn("raise data_rules.max_export_rows", self.action(("data_rules:snapshot", "not_run",
                                                                       {"reason": "resource_limit"})))


class NotProvenTest(StageCase):
    def test_vacuous_agreements_are_listed(self):
        w = self.world()
        w.sql_rows["third_qty"] = []
        w.view_rows["third_qty"] = []
        w.data["bom_line"] = [r for r in DATA["bom_line"] if r["line_id"] != 9]
        res = w.run()
        self.assertEqual(statuses(res)["rule_xcheck:third_qty"], "pass")
        self.assertIn("rule:third_qty: both evaluations output no rows, so the agreement is vacuous", res.not_proven)

    def test_an_empty_entity_is_vacuous(self):
        w = self.world()
        w.data["bom_line"] = []
        for rule_id in ("qty_over_limit", "parent_unknown", "repeated_line", "third_qty", "orphan_lines",
                        "all_children_retired"):
            w.sql_rows[rule_id] = []
            w.view_rows[rule_id] = []
        w.subject_rows = []
        res = w.run()
        self.assertIn("rule:third_qty: entity bom_line has 0 rows, so the agreement is vacuous", res.not_proven)
        self.assertEqual(statuses(res)["rule_subjects:lookup"], "pass")


# ---- the import boundary ----------------------------------------------------------------------------------
class ImportBoundaryTest(unittest.TestCase):
    def local_imports(self, module) -> set:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                found |= {a.name for a in node.names} if node.module is None else {node.module.split(".")[0]}
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in sys.stdlib_module_names:
                        found.add(alias.name.split(".")[0])
        return found

    def test_the_two_paths_never_import_each_other(self):
        self.assertNotIn("data_rules_sql", self.local_imports(ev))
        self.assertNotIn("data_rules_eval", self.local_imports(dsql))
        self.assertLessEqual(self.local_imports(ev), {"canon", "ident"})
        self.assertLessEqual(self.local_imports(dsql), {"data_rules", "ident"})
        self.assertLessEqual(self.local_imports(data_rules), {"canon", "ident"})
        for module in (ev, dsql, data_rules):
            with self.subTest(module=module.__name__):
                self.assertFalse(self.local_imports(module) & {"config", "db", "result", "shapes", "stage_build",
                                                               "stage_data_rules", "naming", "provenance"})

    def test_the_gate_reasons_of_python_are_reasons_of_the_stage(self):
        self.assertLessEqual(set(ev.GATE_REASONS), set(dsql.GATE_REASONS))
        self.assertLessEqual(set(dsql.GATE_REASONS), set(sdr.DATA_GATES))


# ---- build: the rule views ----------------------------------------------------------------------------------
EXAMPLE = pgfixture.ROOT / "examples" / "data-rules"
CLEAN_SCHEMA = {   # the tables the clean-room example names (examples/data-rules/crosscheck.toml)
    "part": {"part_id": "text", "name": "text", "unit": "text", "part_class": "text", "procure": "text",
             "issue": "text", "retired": "boolean", "stored_level": "integer"},
    "bom_line": {"line_id": "integer", "parent_id": "text", "child_id": "text", "qty_per": "numeric(18,6)"},
    "code_value": {"code_group": "text", "code": "text"},
}


def create_text(w: World, rule_id: str) -> str:
    """The CREATE OR REPLACE VIEW build runs for a rule (its view statement without the final ';')."""
    return dsql.view_statement(w.cfg, w.spec, w.plan, w.spec.rule(rule_id))[:-1]


class BuildViewsTest(StageCase):
    def clean_room(self, rules=None, schema=None) -> World:
        text = rules if rules is not None else (EXAMPLE / "data-rules.toml").read_text(encoding="utf-8")
        return self.world(text, (EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8"), schema or CLEAN_SCHEMA,
                          build=False)

    def build(self, w: World) -> Result:
        w.conn = FakeConn(w)
        with mock.patch.object(db, "connect", return_value=w.conn), \
                mock.patch.object(stage_build.closure, "build", return_value=([], ["-- closure"])):
            return stage_build.run(w.cfg, types.SimpleNamespace(hierarchy=""), w.out)

    def build_sql(self, w: World) -> str:
        return (w.out / "build" / "build.sql").read_text(encoding="utf-8")

    def test_build_creates_every_rule_view_under_the_lock_and_records_it(self):
        w = self.clean_room()
        res = self.build(w)
        ids = [r.id for r in w.spec.rules]
        self.assertEqual(len(ids), 15)
        got = statuses(res)
        self.assertEqual([c.id for c in res.checks if c.id.startswith("rule_")],
                         [f"rule_view:{i}" for i in ids] + ["rule_subjects:view"])
        self.assertEqual({got[f"rule_view:{i}"] for i in ids}, {"pass"})
        self.assertEqual(got["rule_subjects:view"], "pass")
        self.assertEqual(by_id(res)["rule_subjects:view"].detail,
                         {"view": "sxc_out.v_rules_subjects", "subject_rules": 14})
        self.assertEqual(by_id(res)["rule_view:qty_over_limit"].detail,
                         {"kind": "row", "view": "sxc_out.v_rule_qty_over_limit"})
        sql = w.conn.sql()
        begin = sql.index("BEGIN")
        self.assertTrue(sql[begin + 1].startswith("SET LOCAL statement_timeout"))
        self.assertEqual(sql[begin + 2], stage_build.LOCK_SQL)
        self.assertEqual(w.conn.statements[begin + 2][1], {"key": "semantic-crosscheck build sxc_out"})
        self.assertEqual(sql[begin + 3], 'DROP VIEW IF EXISTS "sxc_out"."v_rules_subjects"')
        for rule_id in ids:
            i = sql.index(create_text(w, rule_id))
            self.assertEqual((sql[i - 1], sql[i + 1]),
                             (f"SAVEPOINT {sdr.SAVEPOINT}", f"RELEASE SAVEPOINT {sdr.SAVEPOINT}"))
            self.assertEqual(sql[i + 3], dsql.comment_statement(data_rules.view_name(w.cfg, rule_id)))
        subjects = dsql.subjects_view_statement(w.cfg, w.spec, w.plan)
        self.assertLess(sql.index(dsql.orphan_statement(w.cfg).text), sql.index(subjects[:-1]))
        self.assertEqual(sql[sql.index(subjects[:-1]):].index("COMMIT") > 0, True)
        self.assertFalse(any("CASCADE" in s for s in sql))
        recorded = self.build_sql(w)
        self.assertTrue(recorded.startswith(stage_build.BUILD_SQL_HEADER))
        for rule_id in ids:
            self.assertIn(dsql.view_statement(w.cfg, w.spec, w.plan, w.spec.rule(rule_id)), recorded)
        self.assertIn(subjects, recorded)
        self.assertLess(recorded.index("-- data rule views (one transaction)"), recorded.index("-- closure"))
        self.assertTrue(res.next_safe_action.endswith("; run `sxc.py data-rules` to cross-check the data rules and "
                                                      "their views"))

    def test_a_view_whose_columns_changed_is_dropped_and_created(self):
        w = self.clean_room()
        w.errors[create_text(w, "odd_quantity")] = "42P16"
        res = self.build(w)
        self.assertEqual(statuses(res)["rule_view:odd_quantity"], "pass")
        view = '"sxc_out"."v_rule_odd_quantity"'
        query = dsql.view_query(w.cfg, w.spec, w.plan, w.spec.rule("odd_quantity"))
        sql = w.conn.sql()
        i = sql.index(f"DROP VIEW {view}")
        self.assertEqual(sql[i + 1], f"CREATE VIEW {view} AS\n{query}")
        recorded = self.build_sql(w)
        self.assertIn(f"{create_text(w, 'odd_quantity')};\nDROP VIEW {view};\nCREATE VIEW {view} AS\n{query};",
                      recorded)

    def test_a_failing_view_is_dropped_and_leaves_the_subjects_view_unbuilt(self):
        w = self.clean_room()
        w.errors[create_text(w, "qty_over_limit")] = "42804"
        res = self.build(w)
        check = by_id(res)["rule_view:qty_over_limit"]
        self.assertEqual((check.status, check.detail["sqlstate"]), ("error", "42804"))
        self.assertIn('DROP VIEW IF EXISTS "sxc_out"."v_rule_qty_over_limit"', w.conn.sql())
        subjects = by_id(res)["rule_subjects:view"]
        self.assertEqual((subjects.status, subjects.detail["reason"]), ("not_run", "rule_view_missing"))
        self.assertNotIn(dsql.subjects_view_statement(w.cfg, w.spec, w.plan)[:-1], w.conn.sql())
        self.assertEqual(statuses(res)["rule_view:parent_unknown"], "pass")
        self.assertIn("rule_view:qty_over_limit hit a database error (SQLSTATE 42804): fix "
                      "[data_rules.rule[0] qty_over_limit] in the rule file", res.next_safe_action)

    def test_an_error_outside_the_savepoints_makes_every_rule_view_an_error(self):
        w = self.clean_room()
        w.errors['DROP VIEW IF EXISTS "sxc_out"."v_rules_subjects"'] = "40P01"
        res = self.build(w)
        self.assertEqual({c.status for c in res.checks if c.id.startswith("rule_")}, {"error"})
        self.assertEqual(by_id(res)["rule_subjects:view"].detail["sqlstate"], "40P01")
        self.assertIn("ROLLBACK", w.conn.sql())
        self.assertIn("-- data rule views: SQLSTATE 40P01, transaction rolled back", self.build_sql(w))
        self.assertNotIn("run `sxc.py data-rules`", res.next_safe_action)
        self.assertIn("another run was writing output schema sxc_out", res.next_safe_action)

    def test_only_marked_views_the_rule_file_no_longer_generates_are_dropped(self):
        w = self.clean_room()
        w.orphans = [["v_rule_old_rule"], ["v_rule_qty_over_limit"], ["v_rule_Bad-Name"], ["v_rule_kept"]]
        w.errors['DROP VIEW IF EXISTS "sxc_out"."v_rule_kept"'] = "2BP01"
        res = self.build(w)
        sql = w.conn.sql()
        self.assertIn('DROP VIEW IF EXISTS "sxc_out"."v_rule_old_rule"', sql)
        self.assertFalse(any(s.startswith("DROP") and "Bad-Name" in s for s in sql))
        self.assertEqual(sql.count('DROP VIEW IF EXISTS "sxc_out"."v_rule_qty_over_limit"'), 0)
        recorded = self.build_sql(w)
        self.assertIn('DROP VIEW IF EXISTS "sxc_out"."v_rule_old_rule";', recorded)
        self.assertIn("was not dropped (SQLSTATE 2BP01)", recorded)
        self.assertNotIn("v_rule_kept", recorded)
        self.assertTrue(any("1 marked view(s) the rule file no longer generates could not be dropped" in n
                            for n in res.not_proven))
        self.assertEqual(statuses(res)["rule_subjects:view"], "pass")

    def test_an_invalid_rule_file_stops_build_before_it_connects(self):
        w = self.clean_room(rules="version = 1\n[[rule]]\nid = \"x\"\n")
        with mock.patch.object(db, "connect", side_effect=AssertionError("connected")):
            with self.assertRaises(StageError) as e:
                stage_build.run(w.cfg, types.SimpleNamespace(hierarchy=""), w.out)
        self.assertEqual(e.exception.exit_code, 2)

    def test_a_type_incompatible_rule_stops_build_before_the_output_schema(self):
        text = (EXAMPLE / "data-rules.toml").read_text(encoding="utf-8").replace(
            '{ column = "qty_per", op = "gt", value = "500" }', '{ column = "parent_id", op = "gt", value = "M" }')
        w = self.clean_room(rules=text)
        with self.assertRaises(StageError) as e:
            self.build(w)
        self.assertEqual(e.exception.exit_code, 2)
        sql = w.conn.sql()
        self.assertIn(stage_build.SOURCE_SQL, sql)                       # the sources were checked ...
        self.assertNotIn("BEGIN", sql)                                   # ... but the output schema never touched
        self.assertFalse(any(s.startswith(("CREATE", "DROP", "SELECT 1 FROM pg_namespace", "SELECT has_schema"))
                             for s in sql))

    def test_a_rule_reading_an_unsupported_type_gets_no_view(self):
        schema = {**CLEAN_SCHEMA, "part": {**CLEAN_SCHEMA["part"], "retired": "uuid"}}
        w = self.clean_room(schema=schema)
        res = self.build(w)
        for rule_id in ("retired_child_in_active_parent", "all_children_retired", "retired_assembly_in_use"):
            check = by_id(res)[f"rule_view:{rule_id}"]
            self.assertEqual((check.status, check.detail["reason"]), ("not_run", "column_type"))
            self.assertFalse(any(f"v_rule_{rule_id}\" AS" in s for s in w.conn.sql()))
        self.assertEqual(by_id(res)["rule_subjects:view"].detail["reason"], "rule_view_missing")
        self.assertIn("no view was built for the data rules retired_child_in_active_parent", res.next_safe_action)

    def test_nondeterministic_collations_and_an_unsupported_node_key(self):
        schema = {**CLEAN_SCHEMA, "part": {**CLEAN_SCHEMA["part"], "name": "text nondeterministic"}}
        res = self.build(self.clean_room(schema=schema))
        for rule_id in ("temporary_name", "same_name_components", "twin_assemblies", "grams_in_count_unit"):
            self.assertEqual(by_id(res)[f"rule_view:{rule_id}"].detail["reason"], "collation")
        self.assertEqual(statuses(res)["rule_view:qty_over_limit"], "pass")
        schema = {**CLEAN_SCHEMA, "part": {**CLEAN_SCHEMA["part"], "part_id": "uuid"}}
        res = self.build(self.clean_room(schema=schema))
        self.assertEqual(statuses(res)["rule_view:qty_over_limit"], "pass")       # reads bom_line only
        self.assertEqual(by_id(res)["rule_view:parent_unknown"].detail["reason"], "column_type")
        self.assertEqual(by_id(res)["rule_subjects:view"].detail["reason"], "column_type")

    def test_views_false_builds_no_rule_view(self):
        config_text = (EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8").replace("views = true ",
                                                                                        "views = false")
        w = self.world((EXAMPLE / "data-rules.toml").read_text(encoding="utf-8"), config_text, CLEAN_SCHEMA,
                       build=False)
        self.assertFalse(w.cfg.data_rules.views)
        res = self.build(w)
        self.assertFalse([c for c in res.checks if c.id.startswith("rule_")])
        self.assertFalse(any("v_rule" in s for s in w.conn.sql()))
        self.assertNotIn("data-rules", res.next_safe_action)

    def test_build_then_data_rules_finds_its_views_current(self):
        w = self.world(build=False)
        res = self.build(w)
        res.write(w.out)
        self.assertEqual({c.status for c in res.checks if c.id.startswith("rule_")}, {"pass"})
        checked = w.run()
        for rule_id in RULE_IDS:
            self.assertEqual(statuses(checked)[f"rule_lookup:{rule_id}"], "pass")
        self.assertEqual(statuses(checked)["rule_subjects:lookup"], "pass")


class KindGateTest(StageCase):
    def test_null_quantities_cycles_and_long_texts_leave_their_kinds_not_run(self):
        w = self.world((EXAMPLE / "data-rules.toml").read_text(encoding="utf-8"),
                       (EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8"), CLEAN_SCHEMA, build=False)
        w.gate_rows[("null_quantity", "bom_line")] = [1]
        w.gate_rows[("height", "bom_line")] = [2]
        w.gate_rows[("text_length", "part")] = [4097]                 # max_text_length is 4096
        res = w.run(rules=["twin_assemblies", "stored_level_drift", "grams_in_count_unit"])
        expected = {"twin_assemblies": "null_values", "stored_level_drift": "cycle_or_depth_limit",
                    "grams_in_count_unit": "value_too_long"}
        for rule_id, reason in expected.items():
            with self.subTest(rule=rule_id):
                self.assertEqual(by_id(res)[f"rule:{rule_id}"].detail["reason"], reason)
                self.assertEqual(by_id(res)[f"rule_xcheck:{rule_id}"].detail["reason"], reason)
                self.assertEqual(statuses(res)[f"rule_example:{rule_id}"], "pass")    # examples read no table
        self.assertEqual(statuses(res)["data_rules:snapshot"], "pass")
        self.assertIn("closure checks for cycles", res.next_safe_action)
        self.assertIn("raise the rule's max_text_length", res.next_safe_action)
        self.assertIn("data rules not evaluated (cycle_or_depth_limit): stored_level_drift", res.not_proven)


class CleanRoomExamplesTest(StageCase):
    """The 15 authored examples of the clean-room rule file through the stage's example path, every kind: the fake
    answers each example statement with the authored rows (correct SQL); the budget stops the snapshot before any
    rule statement, while the examples still run in their own transaction."""

    def world_for_examples(self) -> World:
        config_text = (EXAMPLE / "crosscheck.toml").read_text(encoding="utf-8").replace("max_export_rows = 200000",
                                                                                        "max_export_rows = 1")
        return self.world((EXAMPLE / "data-rules.toml").read_text(encoding="utf-8"), config_text, CLEAN_SCHEMA,
                          build=False)

    def test_every_clean_room_example_passes(self):
        w = self.world_for_examples()
        res = w.run()
        self.assertEqual(by_id(res)["data_rules:snapshot"].detail["reason"], "resource_limit")
        self.assertEqual(len(w.spec.rules), 15)
        for rule in w.spec.rules:
            with self.subTest(rule=rule.id):
                self.assertEqual(by_id(res)[f"rule_example:{rule.id}"].detail, {"examples": 1})
                self.assertIn(w.sql_of("example", rule.id), w.conn.sql())
                self.assertNotIn(dsql.limited(dsql.rule_statement(w.cfg, w.spec, w.plan, rule), 200001).text,
                                 w.conn.sql())

    def test_wrong_sql_example_rows_are_mismatches_for_every_kind(self):
        wrong = {"stored_level_drift": [["L", "leaf_stored_nonzero", 1, 0], ["M", "has_children_stored_zero", 0, 2],
                                        ["O", "outside_stored_nonzero", 3, 0]],
                 "grams_in_count_unit": [["A", "W1", D("12.5"), D("12.5"), D("1"), 1]],
                 "odd_quantity": [["D", "S", D("40"), D("2.5"), 1]],
                 "twin_assemblies": [["A1", "A1"], ["A1", "A3"]],
                 "repeated_line": [["A", "B", 3]]}
        w = self.world_for_examples()
        for rule_id, rows in wrong.items():
            w.example_rows[(rule_id, 0)] = rows
        res = w.run()
        for rule_id in wrong:
            with self.subTest(rule=rule_id):
                example = by_id(res)[f"rule_example:{rule_id}"]
                self.assertEqual((example.status, example.detail["sql_mismatch"]), ("fail", 1))
        self.assertEqual(statuses(res)["rule_example:qty_over_limit"], "pass")


class ReportAndExpectTest(unittest.TestCase):
    def test_report_adds_its_line_only_with_data_rules(self):
        from semantic_crosscheck import report
        w = World(build=False)
        self.addCleanup(w.close)
        res = report.run(w.cfg, types.SimpleNamespace(), w.out)
        self.assertIn(report.DATA_RULES_NOTE, res.not_proven)
        self.assertIn(report.DATA_RULES_NOTE, (w.out / "report.md").read_text(encoding="utf-8"))
        plain = config.load(pgfixture.DEMO_CONFIG)
        out = w.dir / "plain"
        self.assertNotIn(report.DATA_RULES_NOTE, report.run(plain, types.SimpleNamespace(), out).not_proven)

    def test_report_never_reads_the_data_rules_result(self):
        from semantic_crosscheck import report
        w = World(build=False)
        self.addCleanup(w.close)
        failing = Result("data_rules")
        failing.add("rule:x", "fail", "data_rules.rule[0] x")
        failing.write(w.out)
        self.assertNotIn("data_rules", report.read_results(w.out))
        res = report.run(w.cfg, types.SimpleNamespace(), w.out)
        self.assertFalse([c for c in res.checks if "data_rules" in c.id])

    def test_the_expect_fixture_configuration_has_no_data_rules(self):
        from semantic_crosscheck import expect
        w = World(build=False)
        self.addCleanup(w.close)
        self.assertIsNotNone(w.cfg.data_rules)
        fixture = expect.fixture_config(w.cfg, "SXC_EXPECT_DSN")
        self.assertIsNone(fixture.data_rules)
        self.assertIsNotNone(w.cfg.data_rules)
        self.assertIsNone(stage_build.rule_file(fixture, w.out))
