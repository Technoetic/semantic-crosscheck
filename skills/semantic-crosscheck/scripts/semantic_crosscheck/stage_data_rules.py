"""data-rules stage: every typed data rule evaluated twice on one snapshot, and its lookup views certified.

The rule file named by [data_rules] file (data_rules.py) holds rules of six kinds. Each selected rule is evaluated on
two independent paths, and it gets a verdict only when they agree:

  path 1  the rule compiled to PostgreSQL by data_rules_sql and run with bound parameters
  path 2  data_rules_eval, a standard-library Python evaluation of the rows this stage exported in the same
          transaction; it never sees SQL

Order of work (configuration errors fail before any row is read, as in build):
  1. The rules.sql of an earlier run is removed first, so a run that stops on a configuration error leaves none.
     [data_rules] and the rule file are loaded and validated (exit 2); --rule narrows the run (an unknown id is exit
     2). With views = true, build/result.json and build/build.sql are read: a lookup view counts only when build.sql
     holds the statement data-rules generates for it now (view_stale otherwise).
  2. Connect (exit 3). Every table and column the rules read must exist (exit 2) and be readable (exit 3). One catalog
     query gives the type plan; every atom, constant and example value is type-checked against it (exit 2). Rules that
     read a column of an unsupported type or a nondeterministic collation are not run.
  3. One READ ONLY REPEATABLE READ snapshot: the server encoding (UTF8), the rows of every entity the rules read
     (counted no further than max_export_rows + 1), the gates (NULL and duplicate keys, NaN and infinities, NULL edge
     quantities, unbounded heights, text length), every eligible rule under its own savepoint (at most max_rule_rows +
     1 rows), the lookups of v_rule_<id> and v_rules_subjects, each under a savepoint, and the export of the needed
     columns, whose row count must equal the budget count. A budget above max_export_rows stops the snapshot before
     any rule, lookup or export statement runs. The transaction is rolled back.
  4. A second READ ONLY transaction runs every authored example: the rule compiled over CTEs of its bound example
     rows, under a savepoint each. No table is read.
  5. Path 2 evaluates every rule whose gates passed over the exported rows, and every example over its own rows.
  6. The canonical output multisets are compared (duplicates count), the subjects are rolled up in Python, and the
     checks follow in this order: data_rules:snapshot, data_rules:key:<entity>, then per rule in configuration order
     rule:<id>, rule_xcheck:<id>, rule_example:<id> and rule_lookup:<id>, then rule_subjects:lookup, scope:rules and
     data_rules:inputs (crosscheck.toml or the rule file changed during the run).
  7. data_rules/rules.sql records every statement the run executed, with placeholders only.

rule:<id> passes when both paths agree, no authored example failed, and the rule found nothing or is a warning; it
fails when they agree and a violation rule found rows. A disagreement, a failed example, a tripped gate, a budget or a
path that could not run leaves it not_run; an SQL error or a Python exception makes it an error. Results keep counts,
ids and small enums only: the rows a rule finds stay in the database views build creates (views = true), to be
queried there with the user. This stage writes nothing to PostgreSQL; build creates the views.
"""

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import config, data_rules, data_rules_eval as ev, data_rules_sql as dsql, db, ident, shapes, stage_build
from .result import EXIT_CONFIG, Result, StageError, load_result

STAGE = "data_rules"
SAVEPOINT = "sxc_data_rule"
EXCLUDED = "excluded_by_option"
VIEW_MISSING = "42P01"
TIMEOUT = "57014"
COLUMNS_CHANGED = "42P16"            # CREATE OR REPLACE VIEW cannot change the view's columns
COLLATION_MISMATCH = ("42P21", "42P22")   # text columns of two different collations compared (42P22 in a rule,
                                          # 42P21 from the height gate's recursive query over parent and child)
DATA_GATES = ("resource_limit", "server_encoding", "column_type", "collation", "non_finite_values", "null_values",
              "key_null", "key_not_unique", "cycle_or_depth_limit", "value_too_long")
LOOKUP_REASONS = ("build_missing", "view_not_built", "view_stale", "view_missing", "rule_not_agreed", "incomplete")
AGREEMENT_NOTE = ("data rules: agreement of the PostgreSQL and Python evaluations does not show that a rule or its "
                  "constants are the right business rule; authored examples pin only the cases they contain")
VIEWS_OFF_NOTE = ("data rules: views = false, so no lookup view was certified and build persists no agreed rows; rule "
                  "views an earlier build created are not dropped and stay live in the output schema")


# ---- shared with build ------------------------------------------------------------------------------
def rule_sources(cfg, rules) -> list:
    """(rule position, table, columns) for every entity each rule reads, for stage_build.check_tables."""
    return [(rule.pos, cfg.entities[entity].table, list(columns))
            for rule in rules for entity, columns in rule.reads.items()]


def read_type_plan(conn, cfg, spec) -> tuple:
    """(TypePlan, the catalog Statement) of every entity the rule file reads: one catalog query."""
    statement = dsql.type_plan_statement(cfg, dsql.needed_entities(cfg, spec.rules))
    text, params = statement.parameterized()
    return dsql.type_plan(db.rows(conn, text, **params)), statement


def type_check(spec, plan) -> dict:
    """data_rules.apply_types with its errors as configuration errors (exit 2): {rule id: column_type | collation}
    for the rules that read a column of an unsupported type or a nondeterministic collation."""
    try:
        return data_rules.apply_types(spec, plan)
    except data_rules.RuleSpecError as e:
        raise StageError(str(e), EXIT_CONFIG) from None


def subjects_statement(cfg, spec, plan) -> str:
    """CREATE OR REPLACE VIEW of v_rules_subjects, '' when there is none to build: no rule names a subject, or the
    node key has no supported type."""
    h = spec.hierarchy
    if not spec.subject_rules or h is None:
        return ""
    key = plan.column(h.node, h.key)
    if key is None or not key.usable:
        return ""
    return dsql.subjects_view_statement(cfg, spec, plan)


def view_statements(cfg, spec, plan) -> tuple:
    """(unusable {rule id: reason}, {rule id: CREATE OR REPLACE VIEW statement} of every usable rule, the subjects
    view statement or ''), exactly as build runs and records them. Type errors and view names that collide with the
    order column are configuration errors (exit 2)."""
    unusable = type_check(spec, plan)
    try:
        views = {r.id: dsql.view_statement(cfg, spec, plan, r) for r in spec.rules if r.id not in unusable}
    except data_rules.RuleSpecError as e:
        raise StageError(str(e), EXIT_CONFIG) from None
    return unusable, views, subjects_statement(cfg, spec, plan)


def attempt(conn, statements) -> tuple:
    """Run (sql, params) pairs under a savepoint: ('', rows of the last) or (SQLSTATE, []) after rolling the savepoint
    back, so one failing statement leaves the caller's transaction usable (shapes._attempt with bound parameters)."""
    conn.run(f"SAVEPOINT {SAVEPOINT}")
    try:
        rows = []
        for sql, params in statements:
            rows = conn.run(sql, **params)
    except Exception as e:  # noqa: BLE001 - only database errors (with a SQLSTATE) are caught
        code = shapes.sqlstate(e)
        if not code:
            raise
        conn.run(f"ROLLBACK TO SAVEPOINT {SAVEPOINT}")
        conn.run(f"RELEASE SAVEPOINT {SAVEPOINT}")
        return code, []
    conn.run(f"RELEASE SAVEPOINT {SAVEPOINT}")
    return "", list(rows or [])


# ---- inputs ------------------------------------------------------------------------------------------
def select_rules(spec, ids) -> tuple:
    """The rules named by --rule (configuration order), or every rule. An unknown id is a configuration error."""
    wanted = list(dict.fromkeys(ids or ()))
    known = {r.id for r in spec.rules}
    if any(i not in known for i in wanted):
        raise StageError(f"--rule names a rule id that {spec.label} does not define: use the id of one of its "
                         "[[rule]] tables", EXIT_CONFIG)
    return tuple(r for r in spec.rules if r.id in wanted) if wanted else spec.rules


def _sha256(path: Path) -> str:
    try:
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read(data_rules.MAX_FILE_BYTES + 1)).hexdigest()
    except OSError:
        return ""


def _config_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclass
class Build:
    """What build left in the output directory: its result (None when absent or unreadable) and build.sql."""
    result: dict | None
    sql: str

    @classmethod
    def read(cls, out: Path) -> "Build":
        try:
            result = load_result(out, "build")
        except (OSError, ValueError):
            result = None
        f = Path(out) / "build" / "build.sql"
        try:
            text = f.read_bytes().decode("utf-8") if f.is_file() else ""
        except (OSError, UnicodeError):
            text = ""
        return cls(result, text)

    def status(self, check_id: str) -> str | None:
        for c in (self.result or {}).get("checks", []):
            if c.get("id") == check_id:
                return c.get("status")
        return None


def lookup_states(build: Build | None, spec, selected, views: dict, subjects_sql: str) -> tuple:
    """({rule id: '' or the reason its lookup cannot be certified}, the same for the subjects view or None when no
    rule names a subject): build_missing, view_not_built (build did not create it), view_stale (build created it from
    another rule file or configuration: build.sql lacks the statement data-rules regenerates now)."""
    states = {}
    for rule in selected:
        if build is None or build.result is None:
            states[rule.id] = "build_missing"
        elif build.status(f"rule_view:{rule.id}") != "pass":
            states[rule.id] = "view_not_built"
        elif rule.id not in views or views[rule.id] not in build.sql:
            states[rule.id] = "view_stale"
        else:
            states[rule.id] = ""
    if not spec.subject_rules:
        return states, None
    if build is None or build.result is None:
        subjects = "build_missing"
    elif (build.status("rule_subjects:view") != "pass"
          or any(build.status(f"rule_view:{r.id}") != "pass" for r in spec.subject_rules)):
        subjects = "view_not_built"
    elif (not subjects_sql or subjects_sql not in build.sql
          or any(views.get(r.id, "\0") not in build.sql for r in spec.subject_rules)):
        subjects = "view_stale"
    else:
        subjects = ""
    return states, subjects


# ---- statements of the run -------------------------------------------------------------------------------
@dataclass
class Prepared:
    """Everything compiled before a row is read."""
    selected: tuple                  # the rules of this run, in configuration order
    usable: tuple                    # the selected rules whose columns all have supported types
    unusable: dict                   # rule id -> column_type | collation (every rule of the file)
    gates: list                      # data_rules_sql.Gate, in run order
    rules: dict                      # rule id -> limited Statement
    exports: dict                    # entity -> (columns, Statement)
    examples: dict                   # (rule id, example index) -> Statement
    classes: dict                    # rule id -> output type classes
    views: dict                      # rule id -> view statement (views = true; every usable rule of the file)
    subjects_sql: str                # the subjects view statement, '' when none
    lookups: dict                    # rule id -> lookup Statement (views = true)


def prepare(cfg, spec, plan, selected) -> Prepared:
    settings = cfg.data_rules
    views, subjects_sql = {}, ""
    if settings.views:
        unusable, views, subjects_sql = view_statements(cfg, spec, plan)
    else:
        unusable = type_check(spec, plan)
    usable = tuple(r for r in selected if r.id not in unusable)
    try:
        gates = dsql.snapshot_gates(cfg, spec, plan, usable, settings.max_export_rows)
        rules = {r.id: dsql.limited(dsql.rule_statement(cfg, spec, plan, r), settings.max_rule_rows + 1)
                 for r in usable}
        exports = {e: (cols, dsql.export_statement(cfg, plan, e, cols))
                   for e, cols in dsql.export_columns(cfg, spec, plan, usable).items()}
        examples = {(r.id, x.index): dsql.example_statement(cfg, spec, plan, r, x) for r in usable for x in r.examples}
    except data_rules.RuleSpecError as e:
        raise StageError(str(e), EXIT_CONFIG) from None
    lookups = ({r.id: dsql.lookup_statement(cfg, r, settings.max_rule_rows + 1) for r in usable}
               if settings.views else {})
    classes = {r.id: dsql.output_types(spec, plan, r) for r in usable}
    return Prepared(selected, usable, unusable, gates, rules, exports, examples, classes, views, subjects_sql, lookups)


# ---- the snapshot ------------------------------------------------------------------------------------------
@dataclass
class SqlOutput:
    """Path 1 of one rule: its rows, or the SQLSTATE of its error, or over_limit (more than max_rule_rows rows)."""
    rows: list | None = None
    sqlstate: str = ""
    over_limit: bool = False
    ms: int = 0


@dataclass
class Lookup:
    rows: list | None = None
    sqlstate: str = ""
    limit: int = 0


@dataclass
class Snapshot:
    status: str = "pass"             # pass | not_run | error
    reason: str = ""                 # server_encoding | resource_limit | sqlstate | export_incomplete
    sqlstate: str = ""
    entity: str = ""                 # the entity whose export differed from its budget count
    encoding: str = ""
    budgets: dict = field(default_factory=dict)      # entity -> rows counted (at most max_export_rows + 1)
    keys: dict = field(default_factory=dict)         # entity -> (null_keys, duplicate_keys)
    gates_read: bool = False
    gated: dict = field(default_factory=dict)        # rule id -> gate reason
    sql: dict = field(default_factory=dict)          # rule id -> SqlOutput
    lookups: dict = field(default_factory=dict)      # rule id -> Lookup
    subjects: Lookup | None = None
    tables: dict | None = None                       # entity -> exported row dicts, when the snapshot is complete

    @property
    def total(self) -> int:
        return sum(self.budgets.values())


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def _one(conn, statement, log: list):
    """The first row of a statement run outside any savepoint (an error ends the transaction)."""
    log.append(statement)
    text, params = statement.parameterized()
    result = conn.run(text, **params)
    return list(result[0]) if result else None


def _all(conn, statement, log: list) -> list:
    log.append(statement)
    text, params = statement.parameterized()
    return list(conn.run(text, **params) or [])


def _guarded(conn, statement, log: list) -> tuple:
    log.append(statement)
    return attempt(conn, [statement.parameterized()])


def take_snapshot(conn, cfg, spec, plan, prep: Prepared, states: dict, subjects_state, log: list) -> Snapshot:
    """Every read of the one READ ONLY REPEATABLE READ transaction. A database error outside the savepoints ends it:
    the snapshot is then an error and no rule is decided on it."""
    snap = Snapshot()
    try:
        with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
            _snapshot_reads(conn, cfg, spec, plan, prep, states, subjects_state, snap, log)
    except Exception as e:  # noqa: BLE001 - database errors become the snapshot's error with their SQLSTATE
        code = shapes.sqlstate(e)
        if not code:
            raise
        snap.status, snap.reason, snap.sqlstate, snap.tables = "error", "sqlstate", code, None
    return snap


def _snapshot_reads(conn, cfg, spec, plan, prep, states, subjects_state, snap: Snapshot, log: list) -> None:
    settings = cfg.data_rules
    gates = prep.gates
    rows = [_one(conn, gates[0].statement, log)]                  # SHOW server_encoding
    snap.encoding = str(rows[0][0]) if rows[0] else ""
    if snap.encoding != "UTF8":
        snap.status, snap.reason = "not_run", "server_encoding"
        return
    budgets = [g for g in gates if g.gate == "budget"]
    for g in budgets:
        row = _one(conn, g.statement, log)
        snap.budgets[g.entity] = int(row[0])
        rows.append(row)
    if snap.total > settings.max_export_rows:
        snap.status, snap.reason = "not_run", "resource_limit"
        return
    for g in gates[1 + len(budgets):]:
        row = _one(conn, g.statement, log)
        rows.append(row)
        if g.gate == "key":
            snap.keys[g.entity] = (int(row[0]), int(row[1]))
    snap.gated = dsql.gate_reasons(cfg, spec, plan, prep.usable, gates, rows, settings.max_export_rows)
    snap.gates_read = True
    for rule in prep.usable:
        if rule.id in snap.gated:
            continue
        start = time.perf_counter()
        code, result = _guarded(conn, prep.rules[rule.id], log)
        took = _ms(start)
        if code:
            snap.sql[rule.id] = SqlOutput(sqlstate=code, ms=took)
        elif len(result) > settings.max_rule_rows:
            snap.sql[rule.id] = SqlOutput(over_limit=True, ms=took)
        else:
            snap.sql[rule.id] = SqlOutput(rows=result, ms=took)
    if settings.views:
        _lookups(conn, cfg, spec, prep, states, subjects_state, snap, log)
    tables = {}
    for entity, (columns, statement) in prep.exports.items():
        result = _all(conn, statement, log)
        if len(result) != snap.budgets.get(entity):
            snap.status, snap.reason, snap.entity = "error", "export_incomplete", entity
            return
        tables[entity] = [dict(zip(columns, r)) for r in result]
    snap.tables = tables


def _has_rows(snap: Snapshot, rule_id: str) -> bool:
    out = snap.sql.get(rule_id)
    return out is not None and out.rows is not None


def _lookups(conn, cfg, spec, prep, states, subjects_state, snap: Snapshot, log: list) -> None:
    """v_rule_<id> for every rule whose view is current and whose SQL ran, and v_rules_subjects when every rule naming
    a subject ran in this snapshot (its limit is the number of distinct subjects those rules returned, plus one)."""
    for rule in prep.usable:
        if states.get(rule.id) or not _has_rows(snap, rule.id):
            continue
        statement = prep.lookups[rule.id]
        code, result = _guarded(conn, statement, log)
        snap.lookups[rule.id] = Lookup(None if code else result, code, cfg.data_rules.max_rule_rows + 1)
    if subjects_state != "" or not all(_has_rows(snap, r.id) for r in spec.subject_rules):
        return
    values = set()
    for rule in spec.subject_rules:
        i = rule.columns.index(rule.subject)
        cls = prep.classes[rule.id][i]
        values |= {data_rules.cell(row[i], cls) for row in snap.sql[rule.id].rows if row[i] is not None}
    limit = len(values) + 1
    code, result = _guarded(conn, dsql.subjects_lookup_statement(cfg, limit), log)
    snap.subjects = Lookup(None if code else result, code, limit)


# ---- authored examples -----------------------------------------------------------------------------------
@dataclass
class Examples:
    state: str = ""                  # '' | server_encoding | sqlstate
    sqlstate: str = ""
    sql: dict = field(default_factory=dict)          # (rule id, index) -> (SQLSTATE or '', rows)


def run_examples(conn, cfg, prep: Prepared, log: list) -> Examples:
    """Every example statement of the usable rules in a second READ ONLY transaction (default isolation), each under
    a savepoint. They read no table: their rows are bound parameters."""
    found = Examples()
    if not prep.examples:
        return found
    try:
        with db.read_only(conn, cfg.source.statement_timeout_ms):
            row = _one(conn, dsql.encoding_statement(), log)
            if not row or row[0] != "UTF8":
                found.state = "server_encoding"
                return found
            for key, statement in prep.examples.items():
                found.sql[key] = _guarded(conn, statement, log)
    except Exception as e:  # noqa: BLE001 - database errors become the examples' error with their SQLSTATE
        code = shapes.sqlstate(e)
        if not code:
            raise
        found.state, found.sqlstate = "sqlstate", code
    return found


# ---- path 2 ----------------------------------------------------------------------------------------------
def canonical(rows, classes) -> Counter:
    """The canonical output tuples of SQL rows (one value per output column, in order), duplicates counted."""
    out = Counter()
    for row in rows:
        if len(row) != len(classes):
            raise ValueError("an SQL row holds another number of columns than the rule outputs")
        out[tuple(data_rules.cell(v, c) for v, c in zip(row, classes))] += 1
    return out


def evaluate_rules(spec, plan, prep: Prepared, snap: Snapshot) -> dict:
    """{rule id: (Outcome, NotRun or the exception type name, milliseconds)} for every rule whose SQL ran on a complete
    snapshot. The rows were exported in the same transaction, so both paths read the same data."""
    out = {}
    if snap.tables is None:
        return out
    evaluator = ev.Evaluator(snap.tables, spec.hierarchy, spec.sets, spec.refs, plan)
    for rule in prep.usable:
        if not _has_rows(snap, rule.id):
            continue
        start = time.perf_counter()
        try:
            outcome = evaluator.evaluate(rule)
        except Exception as e:  # noqa: BLE001 - an evaluator error, reported by its type only
            outcome = type(e).__name__
        out[rule.id] = (outcome, _ms(start))
    return out


@dataclass
class ExampleResult:
    verdict: str = ""                # '' (all equal) | common_mode | sql_mismatch | py_mismatch | both_mismatch
    sqlstate: str = ""
    exception: str = ""
    gate: str = ""                   # the Python gate that tripped on the example's rows


def evaluate_examples(spec, plan, prep: Prepared, examples: Examples) -> dict:
    """{(rule id, index): ExampleResult} for the examples whose SQL statement ran."""
    out = {}
    for rule in prep.usable:
        for example in rule.examples:
            key = (rule.id, example.index)
            if key not in examples.sql:
                continue
            code, rows = examples.sql[key]
            if code:
                out[key] = ExampleResult(sqlstate=code)
                continue
            sql = ev.Outcome(rule.columns, canonical(rows, prep.classes[rule.id]))
            try:
                tables = ev.example_tables(rule, example, plan)
                py = ev.evaluate(rule, tables, spec.hierarchy, spec.sets, spec.refs, plan)
                if isinstance(py, ev.NotRun):
                    out[key] = ExampleResult(gate=py.reason)
                    continue
                expect = ev.expected(rule, example, plan)
                out[key] = ExampleResult(verdict=ev.example_verdict(sql, py, expect))
            except Exception as e:  # noqa: BLE001 - an evaluator error, reported by its type only
                out[key] = ExampleResult(exception=type(e).__name__)
    return out


# ---- decisions -------------------------------------------------------------------------------------------
@dataclass
class Evaluation:
    """How the two paths went for one rule. state: agreed, disagree, not_run, sql_error or py_error."""
    state: str
    reason: str = ""
    sqlstate: str = ""
    exception: str = ""
    sql: Counter | None = None
    py: Counter | None = None
    sql_ms: int | None = None
    py_ms: int | None = None
    python_gate: bool = False        # the SQL gates passed but the Python evaluation's gate tripped


def evaluation(rule, prep: Prepared, snap: Snapshot, python: dict) -> Evaluation:
    if rule.id in prep.unusable:
        return Evaluation("not_run", prep.unusable[rule.id])
    if snap.status == "not_run":
        return Evaluation("not_run", snap.reason)
    if rule.id in snap.gated:
        return Evaluation("not_run", snap.gated[rule.id])
    out = snap.sql.get(rule.id)
    if out is None:
        return Evaluation("not_run", "snapshot_not_read")
    if out.sqlstate:
        return Evaluation("sql_error", sqlstate=out.sqlstate, sql_ms=out.ms)
    if out.over_limit:
        return Evaluation("not_run", "resource_limit", sql_ms=out.ms)
    if snap.status != "pass" or rule.id not in python:
        return Evaluation("not_run", "snapshot_not_read", sql_ms=out.ms)
    outcome, py_ms = python[rule.id]
    if isinstance(outcome, str):
        return Evaluation("py_error", exception=outcome, sql_ms=out.ms, py_ms=py_ms)
    if isinstance(outcome, ev.NotRun):
        return Evaluation("not_run", outcome.reason, sql_ms=out.ms, py_ms=py_ms, python_gate=True)
    sql = canonical(out.rows, prep.classes[rule.id])
    state = "agreed" if sql == outcome.rows else "disagree"
    return Evaluation(state, sql=sql, py=outcome.rows, sql_ms=out.ms, py_ms=py_ms)


def violation_counts(rule, rows: Counter) -> dict:
    """outputs (distinct tuples) and violations: keys, values or rows (the sum of n) for row, mode_outlier and
    text_quantity; groups or values for duplicate; members (plus groups) for same_signature; nodes (plus one count per
    kind) for stored_metric."""
    detail = {"violations": sum(rows.values()), "outputs": len(rows)}
    if rule.kind in ("row", "mode_outlier", "text_quantity") and rule.report.form == "columns":
        n = rule.columns.index("n")
        detail["violations"] = sum(int(Decimal(row[n])) * count for row, count in rows.items())
    elif rule.kind == "same_signature":
        detail["groups"] = len({row[0] for row in rows})
    elif rule.kind == "stored_metric":
        kinds = Counter()
        for row, count in rows.items():
            kinds[row[1]] += count
        detail.update({kind: kinds[kind] for kind in data_rules.HEIGHT_KINDS})
    return detail


def example_check(rule, prep: Prepared, examples: Examples, results: dict, require: bool):
    """(status, detail) of rule_example:<id>, or None when the rule has no example and examples are not required."""
    if not rule.examples:
        return ("not_run", {"reason": "no_example"}) if require else None
    count = {"examples": len(rule.examples)}
    if rule.id in prep.unusable:
        return "not_run", {"reason": "example_not_evaluable", "gate": prep.unusable[rule.id], **count}
    if examples.state == "server_encoding":
        return "not_run", {"reason": "example_not_evaluable", "gate": "server_encoding", **count}
    if examples.state == "sqlstate":
        return "error", {"sqlstate": examples.sqlstate, **count}
    found = [results[(rule.id, x.index)] for x in rule.examples]
    for i, r in enumerate(found):
        if r.sqlstate:
            return "error", {"sqlstate": r.sqlstate, "example": i, **count}
        if r.exception:
            return "error", {"reason": "evaluator", "exception": r.exception, "example": i, **count}
    verdicts = Counter(r.verdict for r in found if r.verdict)
    if verdicts:
        return "fail", {**count, **{v: verdicts[v] for v in ev.EXAMPLE_VERDICTS}}
    gates = [r.gate for r in found if r.gate]
    if gates:
        return "not_run", {"reason": "example_not_evaluable", "gate": gates[0], **count}
    return "pass", count


def lookup_check(cfg, rule, state: str, e: Evaluation, snap: Snapshot, classes: tuple):
    """(status, detail) of rule_lookup:<id>: v_rule_<id> returns exactly the agreed rows, in canonical order."""
    detail = {"view": data_rules.view_name(cfg, rule.id)}
    if state:
        return "not_run", {"reason": state, **detail}
    found = snap.lookups.get(rule.id)
    if found is None:
        return "not_run", {"reason": "rule_not_agreed", **detail}
    if found.sqlstate == VIEW_MISSING:
        return "not_run", {"reason": "view_missing", **detail}
    if found.sqlstate:
        return "error", {"sqlstate": found.sqlstate, **detail}
    if e.state != "agreed":
        return "not_run", {"reason": "rule_not_agreed", **detail}
    return compare_rows(found.rows, e.sql, len(classes),
                        lambda row: tuple(data_rules.cell(v, c) for v, c in zip(row, classes)),
                        lambda row: data_rules.sort_key(row, classes), detail)


def compare_rows(rows: list, agreed: Counter, width: int, cell, order, detail: dict) -> tuple:
    """A lookup (rows in ord order: width output cells, then ord) against the agreed multiset and its canonical
    order: lookup_only and agreed_only count the rows only one side holds; order_differs when the multisets are equal
    but the ord sequence is not 1..N in canonical order."""
    detail = {**detail, "lookup_rows": len(rows), "agreed_rows": sum(agreed.values())}
    if any(len(row) != width + 1 for row in rows):
        return "fail", {**detail, "columns_differ": True}
    body = [row[:-1] for row in rows]
    ords = [row[-1] for row in rows]
    got = Counter(cell(row) for row in body)
    lookup_only, agreed_only = sum((got - agreed).values()), sum((agreed - got).values())
    sequence = [cell(row) for row in body]
    expected = sorted((row for row, n in agreed.items() for _ in range(n)), key=order)
    in_order = sequence == expected and ords == list(range(1, len(rows) + 1))
    if lookup_only or agreed_only:
        return "fail", {**detail, "lookup_only": lookup_only, "agreed_only": agreed_only}
    if not in_order:
        return "fail", {**detail, "order_differs": True}
    return "pass", detail


def subjects_check(cfg, spec, prep: Prepared, state, evaluations: dict, snap: Snapshot, plan):
    """(status, detail) of rule_subjects:lookup: v_rules_subjects equals the Python roll-up of the agreed outputs of
    every rule naming a subject (subject, severity, rule_count, rules), in its order."""
    detail = {"view": data_rules.subjects_view_name(cfg), "subject_rules": len(spec.subject_rules)}
    selected = {r.id for r in prep.selected}
    if any(r.id not in selected for r in spec.subject_rules):
        return "not_run", {"reason": EXCLUDED, **detail}
    if state:
        return "not_run", {"reason": state, **detail}
    found = snap.subjects
    if found is not None and found.sqlstate == VIEW_MISSING:
        return "not_run", {"reason": "view_missing", **detail}
    if found is not None and found.sqlstate:
        return "error", {"sqlstate": found.sqlstate, **detail}
    if found is None or any(evaluations[r.id].state != "agreed" for r in spec.subject_rules):
        return "not_run", {"reason": "incomplete", **detail}
    h = spec.hierarchy
    key_class = plan.column(h.node, h.key).type_class
    outcomes = {r.id: ev.Outcome(r.columns, evaluations[r.id].py) for r in spec.subject_rules}
    union = ev.subjects(spec.rules, outcomes, key_class)
    agreed = Counter((s.subject, s.severity, s.severity_rank, s.rule_count, tuple(s.rules)) for s in union)

    def cell(row):
        subject, severity, rank, count, rules = row
        return (data_rules.cell(subject, key_class), severity, int(rank) if rank is not None else None,
                int(count) if count is not None else None, tuple(rules or ()))

    return compare_rows(found.rows, agreed, len(dsql.SUBJECT_COLUMNS) - 1, cell,
                        lambda row: (row[2], data_rules.sort_key((row[0],), (key_class,))),
                        {**detail, "subjects": len(union)})


# ---- the stage --------------------------------------------------------------------------------------------
def run(cfg, args, out: Path) -> Result:
    out = Path(out)
    (out / STAGE / "rules.sql").unlink(missing_ok=True)       # first: a configuration error must not leave it behind
    if getattr(cfg, "data_rules", None) is None:
        raise StageError("crosscheck.toml has no [data_rules] table: add [data_rules] with file = \"data-rules.toml\" "
                         "(the rule file, relative to crosscheck.toml) to run data-rules", EXIT_CONFIG)
    config_sha = _config_sha256(cfg.path)
    spec = config.load_data_rules(cfg, out)
    settings = cfg.data_rules
    selected = select_rules(spec, getattr(args, "rule", None))
    build = Build.read(out) if settings.views else None
    log: list = []
    conn = db.connect(cfg.source.dsn_env)
    try:
        stage_build.check_tables(conn, rule_sources(cfg, spec.rules), spec.label)
        plan, catalog = read_type_plan(conn, cfg, spec)
        log.append(catalog)
        prep = prepare(cfg, spec, plan, selected)
        states, subjects_state = (lookup_states(build, spec, selected, prep.views, prep.subjects_sql)
                                  if settings.views else ({}, None))
        snap = take_snapshot(conn, cfg, spec, plan, prep, states, subjects_state, log)
        examples = run_examples(conn, cfg, prep, log)
    finally:
        conn.close()
    python = evaluate_rules(spec, plan, prep, snap)
    example_results = evaluate_examples(spec, plan, prep, examples)
    res = Result(STAGE)
    evaluations = {r.id: evaluation(r, prep, snap, python) for r in selected}
    add_checks(cfg, spec, plan, prep, snap, examples, example_results, evaluations, states, subjects_state, res)
    if _config_sha256(cfg.path) != config_sha or _sha256(data_rules.rule_file_path(cfg)) != spec.sha256:
        res.add("data_rules:inputs", "error", "data_rules.file", reason="inputs_changed")
    d = out / STAGE
    d.mkdir(parents=True, exist_ok=True)
    (d / "rules.sql").write_text(dsql.rules_sql(log), encoding="utf-8", newline="\n")
    res.not_proven += not_proven(cfg, spec, prep, snap, evaluations)
    res.next_safe_action = next_action(cfg, res)
    res.inputs["data_rules_sha256"] = spec.sha256
    return res


def add_checks(cfg, spec, plan, prep, snap, examples, example_results, evaluations, states, subjects_state, res):
    settings = cfg.data_rules
    _snapshot_check(snap, settings, res)
    if snap.gates_read:
        for entity, (nulls, duplicates) in snap.keys.items():
            status = "fail" if nulls or duplicates else "pass"
            detail = {"null_keys": nulls, "duplicate_keys": duplicates}
            if status == "fail":
                detail["reason"] = "null_keys" if nulls else "duplicate_keys"
            res.add(f"data_rules:key:{entity}", status, f"entities.{entity}", **detail)
    for rule in prep.selected:
        e = evaluations[rule.id]
        example = example_check(rule, prep, examples, example_results, settings.require_examples)
        _rule_check(cfg, rule, e, example, res)
        _xcheck(rule, e, res)
        if example is not None:
            res.add(f"rule_example:{rule.id}", example[0], rule.config_ref, **example[1])
        if settings.views:
            status, detail = lookup_check(cfg, rule, states.get(rule.id, ""), e, snap, prep.classes.get(rule.id, ()))
            res.add(f"rule_lookup:{rule.id}", status, rule.config_ref, **detail)
    if settings.views and subjects_state is not None:
        status, detail = subjects_check(cfg, spec, prep, subjects_state, evaluations, snap, plan)
        res.add("rule_subjects:lookup", status, "data_rules", **detail)
    excluded = len(spec.rules) - len(prep.selected)
    if excluded:
        res.add("scope:rules", "not_run", "data_rules", reason=EXCLUDED, rules_excluded=excluded)


def _snapshot_check(snap: Snapshot, settings, res: Result) -> None:
    detail = {f"rows_{entity}": count for entity, count in snap.budgets.items()}
    detail.update(rows_total=snap.total, limit=settings.max_export_rows)
    if snap.status == "pass":
        res.add("data_rules:snapshot", "pass", "data_rules", **detail)
    elif snap.status == "not_run":
        extra = {"encoding": snap.encoding} if snap.reason == "server_encoding" else {}
        res.add("data_rules:snapshot", "not_run", "data_rules", reason=snap.reason, **extra, **detail)
    elif snap.reason == "export_incomplete":
        res.add("data_rules:snapshot", "error", "data_rules", reason="export_incomplete", entity=snap.entity, **detail)
    else:
        res.add("data_rules:snapshot", "error", "data_rules", sqlstate=snap.sqlstate, **detail)


def _rule_check(cfg, rule, e: Evaluation, example, res: Result) -> None:
    base = {"kind": rule.kind, "severity": rule.severity}
    if e.state == "not_run":
        res.add(f"rule:{rule.id}", "not_run", rule.config_ref, reason=e.reason, **base)
    elif e.state == "sql_error":
        extra = {"timeout_ms": cfg.source.statement_timeout_ms} if e.sqlstate == TIMEOUT else {}
        res.add(f"rule:{rule.id}", "error", rule.config_ref, sqlstate=e.sqlstate, **extra, **base)
    elif e.state == "py_error":
        res.add(f"rule:{rule.id}", "error", rule.config_ref, reason="evaluator", exception=e.exception, **base)
    elif e.state == "disagree":
        res.add(f"rule:{rule.id}", "not_run", rule.config_ref, reason="paths_disagree", **base)
    elif example is not None and example[0] == "fail":
        res.add(f"rule:{rule.id}", "not_run", rule.config_ref, reason="example_mismatch", **base,
                **violation_counts(rule, e.sql))
    else:
        counts = violation_counts(rule, e.sql)
        status = "fail" if counts["violations"] and rule.severity == "violation" else "pass"
        res.add(f"rule:{rule.id}", status, rule.config_ref, **base, **counts)


def _xcheck(rule, e: Evaluation, res: Result) -> None:
    times = {k: v for k, v in (("sql_ms", e.sql_ms), ("py_ms", e.py_ms)) if v is not None}
    if e.state == "not_run":
        extra = {"not_evaluated": "python"} if e.python_gate else {}
        res.add(f"rule_xcheck:{rule.id}", "not_run", rule.config_ref, reason=e.reason, **extra, **times)
    elif e.state in ("sql_error", "py_error"):
        res.add(f"rule_xcheck:{rule.id}", "not_run", rule.config_ref, reason="path_error",
                failed="sql" if e.state == "sql_error" else "python", **times)
    else:
        detail = {"sql_outputs": len(e.sql), "py_outputs": len(e.py), **times}
        if e.state == "agreed":
            res.add(f"rule_xcheck:{rule.id}", "pass", rule.config_ref, **detail)
        else:
            res.add(f"rule_xcheck:{rule.id}", "fail", rule.config_ref, sql_only=sum((e.sql - e.py).values()),
                    py_only=sum((e.py - e.sql).values()), **detail)


# ---- not proven and next action ---------------------------------------------------------------------------
def not_proven(cfg, spec, prep: Prepared, snap: Snapshot, evaluations: dict) -> list:
    settings = cfg.data_rules
    lines = []
    agreed = [r for r in prep.selected if evaluations[r.id].state == "agreed"]
    if agreed:
        lines.append(AGREEMENT_NOTE)
    for rule in agreed:
        if snap.budgets.get(rule.entity) == 0:
            lines.append(f"rule:{rule.id}: entity {rule.entity} has 0 rows, so the agreement is vacuous")
        elif not evaluations[rule.id].sql:
            lines.append(f"rule:{rule.id}: both evaluations output no rows, so the agreement is vacuous")
    if not settings.require_examples:
        for rule in prep.selected:
            if not rule.examples:
                lines.append(f"rule_example:{rule.id}: the rule has no authored example (require_examples = false), "
                             "so nothing pins its intended output")
    if not settings.views:
        lines.append(VIEWS_OFF_NOTE)
    if snap.reason == "resource_limit":
        lines.append(f"data rules: the entities the rules read hold more than data_rules.max_export_rows = "
                     f"{settings.max_export_rows} rows together ({snap.total} counted, each entity at most "
                     f"{settings.max_export_rows + 1}), so no rule, lookup or export statement ran")
    gated = {}
    for rule in prep.selected:
        e = evaluations[rule.id]
        if e.state == "not_run" and e.reason in DATA_GATES and not (e.reason == "resource_limit"
                                                                    and snap.reason == "resource_limit"):
            gated.setdefault(e.reason, []).append(rule.id)
    for reason, ids in gated.items():
        why = (f"resource_limit: more than data_rules.max_rule_rows = {settings.max_rule_rows} output rows"
               if reason == "resource_limit" else reason)
        lines.append(f"data rules not evaluated ({why}): {', '.join(ids)}")
    return lines


def _error_action(cfg, c) -> str:
    if c.id == "data_rules:inputs":
        return "crosscheck.toml or the rule file changed while data-rules ran: rerun data-rules"
    if c.detail.get("reason") == "export_incomplete":
        return ("the export read another number of rows than the snapshot counted: rerun data-rules, and report a "
                "defect if it persists")
    if c.detail.get("reason") == "evaluator":
        return (f"{c.id}: the Python evaluation raised {c.detail.get('exception', '')}: check the rule's column types "
                "and values against docs/data-rules.md, report a defect if the rule is plain, then rerun data-rules")
    code = c.detail.get("sqlstate", "")
    if code == TIMEOUT:
        hint = (f"raise source.statement_timeout_ms (now {cfg.source.statement_timeout_ms}) or narrow the run with "
                "--rule")
    elif code == "42501":
        hint = ("the check role lacks a privilege data-rules needs (SELECT on the source tables, USAGE on the output "
                "schema for the lookup views): run `sxc.py doctor --config` to see which")
    elif code in COLLATION_MISMATCH:
        hint = ("a statement compares text columns of different collations (the node key with the edge endpoints, the "
                "parent column with the child column, or a ref column with the column compared with it): give every "
                "compared column one collation, for example COLLATE \"C\" on each in database views the entities "
                "point at (rerun build when views = true)")
    else:
        hint = f"check [{c.config_ref}] and the check role's privileges"
    return f"{c.id} hit a database error (SQLSTATE {code}): {hint}, then rerun data-rules"


def _ids(checks, prefix: str) -> str:
    return ", ".join(c.id.partition(":")[2] for c in checks if c.id.startswith(prefix))


def next_action(cfg, res: Result) -> str:
    """One sentence for the agent, first match wins: an error, a disagreement, a common-mode example, a budget or
    gate, a lookup, violations found, otherwise a rerun hint."""
    checks = res.checks
    errors = [c for c in checks if c.status == "error"]
    if errors:
        return _error_action(cfg, errors[0])
    disagree = [c for c in checks if (c.id.startswith("rule_xcheck:") and c.status == "fail")
                or (c.id.startswith("rule_example:") and c.status == "fail"
                    and any(c.detail.get(v) for v in ("sql_mismatch", "py_mismatch", "both_mismatch")))]
    if disagree:
        ids = ", ".join(dict.fromkeys(c.id.partition(":")[2] for c in disagree))
        return (f"the PostgreSQL and Python evaluations disagree on {ids}: check those rules' column types, NULLs and "
                "pattern against docs/data-rules.md, report a defect if a rule is plain, then rerun data-rules")
    common = [c for c in checks if c.id.startswith("rule_example:") and c.detail.get("common_mode")]
    if common:
        return (f"both evaluations agree but differ from the authored example rows of {_ids(common, 'rule_example:')}"
                " (common_mode): check the examples' expected rows against the business rule first, then the rule, "
                "then rerun data-rules")
    gated = [c for c in checks if c.status in ("not_run", "fail") and (c.detail.get("reason") in DATA_GATES
                                                                       or c.id.startswith("data_rules:key:"))]
    if gated:
        reasons = {c.detail.get("reason") for c in gated}
        steps = []
        if "resource_limit" in reasons:
            steps.append("raise data_rules.max_export_rows or data_rules.max_rule_rows (at most 2000000) or narrow the "
                         "run with --rule")
        if reasons & {"column_type", "collation", "non_finite_values", "server_encoding"}:
            steps.append("cast the column in a database view (a supported type, a deterministic collation, no NaN) "
                         "and point the entity at it")
        if reasons & {"key_null", "key_not_unique", "null_keys", "duplicate_keys", "null_values"}:
            steps.append("fix the NULL or duplicate keys (or NULL edge quantities) in the source rows")
        if "cycle_or_depth_limit" in reasons:
            steps.append("see build's closure checks for cycles, or raise the hierarchy's max_depth")
        if "value_too_long" in reasons:
            steps.append("raise the rule's max_text_length")
        return "some rules were not evaluated: " + "; ".join(steps) + "; then rerun data-rules"
    lookups = [c for c in checks if (c.id.startswith("rule_lookup:") or c.id == "rule_subjects:lookup")
               and c.status != "pass" and c.detail.get("reason") not in ("rule_not_agreed", "incomplete", EXCLUDED)]
    if lookups:
        return ("the lookup views are missing, stale or differ from the agreed rows: rerun `sxc.py build`, then "
                "`sxc.py data-rules`; or set data_rules.views = false")
    found = [c for c in checks if c.id.startswith("rule:") and c.detail.get("violations")]
    if found:
        ids = _ids(found, "rule:")
        if cfg.data_rules.views:
            schema = cfg.source.output_schema
            return (f"rules found rows ({ids}): query {schema}.{data_rules.SUBJECTS_VIEW} ORDER BY ord and "
                    f"{schema}.{data_rules.VIEW_PREFIX}<id> ORDER BY ord in the database with the user, fix the data "
                    "or the rule file, then rerun build and data-rules")
        return (f"rules found rows ({ids}): set data_rules.views = true and rerun build and data-rules to look at them "
                "in the database with the user, or fix the data or the rule file")
    if any(c.id == "scope:rules" for c in checks):
        return "this run checked only the rules named by --rule: rerun data-rules without --rule for a complete result"
    if any(c.detail.get("reason") == "no_example" for c in checks):
        return ("add [[rule.example]] tables written from the business rule to the rules without one (or set "
                "data_rules.require_examples = false), then rerun data-rules")
    unevaluable = [c for c in checks if c.detail.get("reason") == "example_not_evaluable"]
    if unevaluable:
        return (f"the authored examples of {_ids(unevaluable, 'rule_example:')} could not be evaluated (a gate "
                "tripped on their rows, such as a cycle or a text longer than max_text_length): change those example "
                "rows, then rerun data-rules")
    return "rerun data-rules after changing the data or the rule file"


def drop_statement(cfg, name: str) -> str:
    """DROP VIEW IF EXISTS of a view in the output schema by its relation name (never CASCADE); '' for a name that
    is not a plain identifier (left alone)."""
    try:
        return dsql.drop_view_statement(f"{ident.check(cfg.source.output_schema)}.{ident.check(name)}")
    except ident.IdentError:
        return ""
