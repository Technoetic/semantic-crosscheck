"""V4 mutation tests: deliberately broken copies of the generated Cube model.

A gold suite that passes proves little until it is shown to fail on a wrong model. Each operator patches
one element of a deep copy of the model (a closure-cube member, the WHERE clause of a measure cube, a
measure type, a join); a fresh Cube process serves the copy and the suite has to notice. Before Cube starts,
a witness query counts what the mutation changes on the current data, whatever the suite happens to query.
Witness 0 means the mutant is equivalent on this data: no suite could kill it, so it goes to not_proven instead of
being run. A mutant with a positive witness that survives fails its check: the gold suite does not observe that
element, and the check names the configuration entry the element comes from.

  operator                    patched element                                  witness (current data)
  closure_depth_shift         <h>_closure.min_depth -> (min_depth) + 1          closure rows
  closure_path_count_one      <h>_closure.path_count -> 1                       pairs with path_count > 1
  closure_qty_single_path     <h>_closure.qty -> (qty) / (path_count)           pairs with path_count > 1 and qty <> 0
  measure_filter_drop         m_<m> SQL without the exact filter                rows the filter excludes
  measure_normalization_drop  m_<m> SQL comparing raw values                    rows only the normalised match keeps
  measure_agg_to_max          m_<m>.value type -> max (sum and count only)      groups of the cube's rows whose sum (or
                                                                                count) differs from their max: each key,
                                                                                the total, and two keys queried together
  dated_shift_one_day         m_<m>.ts -> (ts) + interval '1 day'               rows with a ts (each moves to another day)
  closure_join_swap           <h>_closure join to m_<m> on the other pair end   pairs whose ends differ in the measure
  closure_end_swap            <h>_closure join to <h>_descendant on ancestor    pairs whose ends are different node rows

The filter witnesses count rows that can move the measure (a non-null value, non-zero for sums) plus the grain
keys (key and day for a dated measure) that only one of the two filtered row sets contains, whatever their value.
Filter mutants replace cube_gen.where_sql(measure) inside the measure cube's SQL with where_sql of a copy
changed by dataclasses.replace. That is exactly the SQL cube_gen.measure_cube generates for the copy, without
needing column types. When the model's SQL does not contain the configured clause, the model came from
another configuration and the mutant becomes an error check, never a silently different test. The
measure_agg_to_max and dated_shift_one_day witnesses read the measure cube's own row SQL from the model.

The closure operators (and closure_join_swap, through the closure its measure joins) are gated like the V1-V3
checks: when build's closure:<h> did not pass, the closure table describes another graph than the source, so their
checks are not_run and listed in not_proven instead of judged.

The suite reports the first item that did not pass. Only a failing item kills the mutant; an item that errors
leaves the mutant unjudged (an error check with reason suite_error naming the item), because a query that could
not run proves nothing about the mutant.

Witness SQL is read-only, takes identifiers from ident/naming or the generated model, and repeats filter values
only as the config-trusted literals of cube_gen.where_sql. Checks and notes carry counts and ids only: the
suite's items must be check ids, and errors keep an exception type or SQLSTATE. A survivor of a suite whose V3 pair
items compare only a sample of their keys is marked sample_blind_possible (it may change only unsampled keys); the
marker never changes a status.

The witness is one statement, so it reads one snapshot in the default READ COMMITTED transaction.
"""

import copy
import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import cube_gen, cube_run, db, ident, naming
from .result import EXIT_CONFIG, StageError, check_status

OPERATORS = (
    "closure_depth_shift",
    "closure_path_count_one",
    "closure_qty_single_path",
    "measure_filter_drop",
    "measure_normalization_drop",
    "measure_agg_to_max",
    "dated_shift_one_day",
    "closure_join_swap",
    "closure_end_swap",
)
CLOSURE_OPERATORS = frozenset({"closure_depth_shift", "closure_path_count_one", "closure_qty_single_path",
                               "closure_join_swap", "closure_end_swap"})
OUTCOMES = ("killed", "survived", "equivalent", "error", "not_applicable", "not_run")
_CUBE_COLUMN = re.compile(r"^\{CUBE\}\.([a-z_][a-z0-9_]*)$")


@dataclass
class Mutant:
    operator: str
    target: str            # hierarchy or measure name
    model: dict            # mutated deep copy of the generated model
    witness_sql: str       # read-only SQL returning one count
    witness_params: dict   # bound parameters of witness_sql
    config_ref: str        # crosscheck.toml entry the mutated element comes from

    @property
    def check_id(self) -> str:
        return f"v4:{self.operator}:{self.target}"


class MutationError(ValueError):
    """The model lacks the element an operator patches (it was generated from another configuration)."""


class NotApplicable(Exception):
    """The target is configured but absent from this model (a closure cube that was not generated)."""


@dataclass
class _Target:
    operator: str
    target: str
    config_ref: str
    subject: object        # the Hierarchy or Measure being mutated
    make: Callable         # (cfg, model, subject) -> Mutant
    hierarchy: str = ""    # the hierarchy whose closure the mutant reads ("" for none)

    def build(self, cfg, model: dict) -> Mutant:
        return self.make(cfg, model, self.subject)


# ---- model navigation ----------------------------------------------------------------------------
def _named(items: list, name: str, where: str) -> dict:
    for it in items:
        if it.get("name") == name:
            return it
    raise MutationError(f"{where}.{name}")


def _cube(model: dict, name: str) -> dict:
    return _named(model.get("cubes", []), name, "cubes")


def _element(model: dict, cube: str, section: str, name: str) -> dict:
    return _named(_cube(model, cube).get(section, []), name, f"{cube}.{section}")


def _mutated(model: dict, patch: Callable[[dict], None]) -> dict:
    m = copy.deepcopy(model)
    patch(m)
    return m


def _closure_cube(model: dict, h) -> str:
    name = naming.cube_closure(h.name)
    if not any(c.get("name") == name for c in model.get("cubes", [])):
        raise NotApplicable("no closure cube in the model (build the closure, then rerun cube)")
    return name


def _swap_end(join: dict, end: str, other: str, where: str) -> None:
    """Point a closure join at the other end of the pair: '{CUBE}.<end>' must occur exactly once."""
    pattern = re.compile(re.escape("{CUBE}." + end) + r"(?![a-z0-9_])")
    sql = join.get("sql", "")
    if len(pattern.findall(sql)) != 1:
        raise MutationError(where)
    join["sql"] = pattern.sub(lambda _m: "{CUBE}." + other, sql)


def _row_sql(model: dict, cube: str) -> str:
    """The row SQL of a measure cube in the model (what Cube aggregates)."""
    sql = _cube(model, cube).get("sql")
    if not sql:
        raise MutationError(f"{cube}.sql")
    return sql


# ---- witness SQL helpers -------------------------------------------------------------------------
def _closure_sql(cfg, h) -> str:
    return ident.qualified(naming.closure_table(cfg, h.name))


def _table(cfg, m) -> str:
    return ident.qualified(cfg.entity(m.entity, f"measures.{m.name}").table)


def _counted(m, alias: str = "t") -> str:
    """Rows whose value can move the measure: non-null, and non-zero for sums."""
    col = f"{alias}.{ident.quote(m.column)}"
    return f"{col} IS NOT NULL" + (f" AND {col} <> 0" if m.agg == "sum" else "")


def _per_key(cfg, m) -> str:
    """The configured measure per grain key, as SQL with columns k and v (m.agg is validated by config)."""
    by = f"t.{ident.quote(m.by)}"
    return (f"SELECT {by} AS k, {m.agg}(t.{ident.quote(m.column)}) AS v FROM {_table(cfg, m)} t "
            f"WHERE {cube_gen.where_sql(m)} GROUP BY {by}")


# ---- closure operators ---------------------------------------------------------------------------
def _depth_shift(cfg, model: dict, h) -> Mutant:
    cube = _closure_cube(model, h)

    def patch(mm):
        d = _element(mm, cube, "dimensions", "min_depth")
        d["sql"] = f"({d['sql']}) + 1"
    return Mutant("closure_depth_shift", h.name, _mutated(model, patch),
                  f"SELECT count(*) FROM {_closure_sql(cfg, h)}", {}, f"hierarchies.{h.name}")


def _path_count_one(cfg, model: dict, h) -> Mutant:
    cube = _closure_cube(model, h)

    def patch(mm):
        _element(mm, cube, "dimensions", "path_count")["sql"] = "1"
    return Mutant("closure_path_count_one", h.name, _mutated(model, patch),
                  f"SELECT count(*) FROM {_closure_sql(cfg, h)} WHERE path_count > 1", {}, f"hierarchies.{h.name}")


def _qty_single_path(cfg, model: dict, h) -> Mutant:
    cube = _closure_cube(model, h)

    def patch(mm):
        qty = _element(mm, cube, "measures", "qty")
        paths = _element(mm, cube, "dimensions", "path_count")
        qty["sql"] = f"({qty['sql']}) / ({paths['sql']})"
    return Mutant("closure_qty_single_path", h.name, _mutated(model, patch),
                  f"SELECT count(*) FROM {_closure_sql(cfg, h)} WHERE path_count > 1 AND qty <> 0", {},
                  f"hierarchies.{h.name}")


def _end_swap(cfg, model: dict, h) -> Mutant:
    """Every column of the descendant copy, its key included, then shows the ancestor's node row: each pair whose two
    ends are different node rows (one of them may be missing from the node table) changes."""
    cube = _closure_cube(model, h)
    desc = naming.cube_descendant(h.name)

    def patch(mm):
        _swap_end(_element(mm, cube, "joins", desc), "descendant", "ancestor", f"{cube}.joins.{desc}")
    node = cfg.entity(h.node, f"hierarchies.{h.name}")
    table, key = ident.qualified(node.table), ident.quote(node.key[0])
    sql = (f"SELECT count(*) FROM {_closure_sql(cfg, h)} p LEFT JOIN {table} a ON a.{key} = p.ancestor "
           f"LEFT JOIN {table} d ON d.{key} = p.descendant WHERE a.{key} IS DISTINCT FROM d.{key}")
    return Mutant("closure_end_swap", h.name, _mutated(model, patch), sql, {}, f"hierarchies.{h.name}")


# ---- measure operators ---------------------------------------------------------------------------
def _rewrite_where(model: dict, m, m2) -> dict:
    """Replace the WHERE clause of m's cube SQL by the one generated for m2 (exactly one occurrence)."""
    cube = naming.cube_measure(m.name)
    old, new = " WHERE " + cube_gen.where_sql(m), " WHERE " + cube_gen.where_sql(m2)
    pattern = re.compile(re.escape(old) + r"(?= GROUP BY |$)")

    def patch(mm):
        c = _cube(mm, cube)
        sql = c.get("sql", "")
        if len(pattern.findall(sql)) != 1:
            raise MutationError(f"{cube}.sql (WHERE clause of measures.{m.name})")
        c["sql"] = pattern.sub(lambda _m: new, sql)
    return _mutated(model, patch)


def _filter_witness(cfg, m, m2) -> str:
    """Rows selected by exactly one of the two WHERE clauses that can move the measure, plus the result rows that only
    one clause produces whatever their value: grain keys (and, for a dated measure, key and day pairs, since a date
    window then shows a key it did not show before) present in one filtered row set and not in the other. A row worth
    0 that adds a key changes the result as much as a row that changes a value."""
    table, w1, w2 = _table(cfg, m), cube_gen.where_sql(m), cube_gen.where_sql(m2)
    grain = [f"t.{ident.quote(m.by)}"] + ([f"(t.{ident.quote(m.date_column)})::date"] if m.date_column else [])
    cols = ", ".join(grain)
    only = ("(SELECT count(*) FROM (SELECT {c} FROM {t} t WHERE ({a}) IS TRUE "
            "EXCEPT SELECT {c} FROM {t} t WHERE ({b}) IS TRUE) x)")
    return (f"SELECT (SELECT count(*) FROM {table} t WHERE ((({w1}) IS TRUE) <> (({w2}) IS TRUE)) AND {_counted(m)})"
            f" + {only.format(c=cols, t=table, a=w1, b=w2)} + {only.format(c=cols, t=table, a=w2, b=w1)}")


def _filter_drop(cfg, model: dict, m) -> Mutant:
    m2 = dataclasses.replace(m, filter={})
    return Mutant("measure_filter_drop", m.name, _rewrite_where(model, m, m2), _filter_witness(cfg, m, m2), {},
                  f"measures.{m.name}")


def _normalization_drop(cfg, model: dict, m) -> Mutant:
    m2 = dataclasses.replace(m, filter={**m.filter, **m.filter_normalized}, filter_normalized={})
    return Mutant("measure_normalization_drop", m.name, _rewrite_where(model, m, m2), _filter_witness(cfg, m, m2), {},
                  f"measures.{m.name}")


def _agg_to_max(cfg, model: dict, m) -> Mutant:
    """The cube's value measure aggregates the rows of its SQL (pre-aggregated: one row per key; dated: the source
    rows). A query sees the mutation wherever the rows it aggregates give sum (count) != max: within one key, over
    all rows, or over two keys queried together, which happens for two non-zero values or a negative value next to
    another one. The witness counts such keys, plus one for the total and one for such a pair."""
    cube = naming.cube_measure(m.name)
    value = _element(model, cube, "measures", naming.MEASURE_VALUE)
    agg, column = value.get("type"), _CUBE_COLUMN.match(str(value.get("sql", "")))
    if agg not in ("sum", "count") or not column:
        raise MutationError(f"{cube}.measures.{naming.MEASURE_VALUE}")
    rows = _row_sql(model, cube)

    def patch(mm):
        _element(mm, cube, "measures", naming.MEASURE_VALUE)["type"] = "max"
    differs = f"{agg}(v) IS DISTINCT FROM max(v)"
    # A dated cube is queried through date windows, which aggregate any run of a key's rows: rows that cancel out
    # over the whole range (5 and -5) still differ inside a window, so there every key with two rows that can move
    # the value counts, not only keys whose rows differ in total.
    window = ""
    if m.date_column:
        moving = "v IS NOT NULL" if agg == "count" else "v IS NOT NULL AND v <> 0"
        window = f" OR count(*) FILTER (WHERE {moving}) >= 2" + (" OR count(v) >= 1" if agg == "count" else "")
    sql = (f"WITH r AS (SELECT c.{ident.quote(naming.MEASURE_KEY)} AS k, c.{ident.quote(column.group(1))} AS v "
           f"FROM ({rows}) c), g AS (SELECT {agg}(v) AS w, ({differs}{window}) AS differs FROM r GROUP BY k) "
           f"SELECT count(*) FILTER (WHERE differs)"
           f" + (SELECT CASE WHEN count(*) > 0 AND {differs} THEN 1 ELSE 0 END FROM r)"
           f" + CASE WHEN count(*) FILTER (WHERE w <> 0) >= 2"
           f" OR (count(*) FILTER (WHERE w < 0) >= 1 AND count(w) >= 2) THEN 1 ELSE 0 END FROM g")
    return Mutant("measure_agg_to_max", m.name, _mutated(model, patch), sql, {}, f"measures.{m.name}")


def _join_swap(cfg, model: dict, m) -> Mutant:
    h = cfg.hierarchy("", f"measures.{m.name}")
    cube = _closure_cube(model, h)
    mc = naming.cube_measure(m.name)
    other = "descendant" if m.closure_join == "ancestor" else "ancestor"

    def patch(mm):
        _swap_end(_element(mm, cube, "joins", mc), m.closure_join, other, f"{cube}.joins.{mc}")
    differ = "coalesce(a.v, 0) <> coalesce(d.v, 0)" if m.agg in ("sum", "count") else "a.v IS DISTINCT FROM d.v"
    sql = (f"WITH v AS ({_per_key(cfg, m)}) SELECT count(*) FROM {_closure_sql(cfg, h)} p "
           f"LEFT JOIN v a ON a.k = p.ancestor LEFT JOIN v d ON d.k = p.descendant WHERE {differ}")
    return Mutant("closure_join_swap", m.name, _mutated(model, patch), sql, {}, f"measures.{m.name}")


def _dated_shift(cfg, model: dict, m) -> Mutant:
    """Every row with a ts lands on another day, which a date window ending on that day (or a day grouping) sees."""
    cube = naming.cube_measure(m.name)

    def patch(mm):
        d = _element(mm, cube, "dimensions", naming.MEASURE_TS)
        d["sql"] = f"({d['sql']}) + interval '1 day'"
    sql = f"SELECT count(*) FROM ({_row_sql(model, cube)}) c WHERE c.{ident.quote(naming.MEASURE_TS)} IS NOT NULL"
    return Mutant("dated_shift_one_day", m.name, _mutated(model, patch), sql, {}, f"measures.{m.name}")


# ---- selection -----------------------------------------------------------------------------------
_OPS = {
    "closure_depth_shift": (None, _depth_shift),
    "closure_path_count_one": (None, _path_count_one),
    "closure_qty_single_path": (None, _qty_single_path),
    "measure_filter_drop": (lambda m: bool(m.filter), _filter_drop),
    "measure_normalization_drop": (lambda m: bool(m.filter_normalized), _normalization_drop),
    "measure_agg_to_max": (lambda m: m.agg in ("sum", "count"), _agg_to_max),
    "dated_shift_one_day": (lambda m: bool(m.date_column), _dated_shift),
    "closure_join_swap": (lambda m: bool(m.closure_join), _join_swap),
    "closure_end_swap": (None, _end_swap),
}


def selected(cfg) -> tuple[str, ...]:
    """Operators named by verify.mutation_operators ("default" = all), in catalogue order; StageError (exit 2) for
    anything else. The verify stage calls it before any check runs."""
    sel = cfg.verify.mutation_operators
    if sel == "default":
        return OPERATORS
    ref = "crosscheck.toml [verify.mutation_operators]"
    if not isinstance(sel, list) or not all(isinstance(s, str) for s in sel):
        raise StageError(f'{ref}: use "default" or a list of operator names', EXIT_CONFIG)
    unknown = sorted(set(sel) - set(OPERATORS))
    if unknown:
        raise StageError(f"{ref}: unknown operator(s) {unknown} (one of {list(OPERATORS)})", EXIT_CONFIG)
    return tuple(op for op in OPERATORS if op in sel)


def _plan(cfg) -> list[_Target]:
    plan = []
    for op in selected(cfg):
        applies, make = _OPS[op]
        if applies is None:
            plan.extend(_Target(op, h.name, f"hierarchies.{h.name}", h, make, h.name) for h in cfg.hierarchies.values())
            continue
        for m in cfg.measures.values():
            if applies(m):
                gate = cfg.hierarchy("", f"measures.{m.name}").name if op in CLOSURE_OPERATORS else ""
                plan.append(_Target(op, m.name, f"measures.{m.name}", m, make, gate))
    return plan


def operators(cfg, model: dict) -> list[Mutant]:
    """One mutant per applicable target of every selected operator; the model itself is never modified.

    Targets missing from the model (a closure cube that was not generated) are left out; run_mutants
    reports them in not_proven. A model inconsistent with the configuration raises MutationError.
    """
    out = []
    for t in _plan(cfg):
        try:
            out.append(t.build(cfg, model))
        except NotApplicable:
            continue
    return out


# ---- running -------------------------------------------------------------------------------------
def witness(conn, cfg, mutant: Mutant) -> int:
    """Rows of the current data on which the mutant changes a result (READ ONLY transaction, no open transaction)."""
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        return int(db.scalar(conn, mutant.witness_sql, **mutant.witness_params) or 0)


def closure_status(cfg, out: Path) -> dict[str, str | None]:
    """Status of build's closure:<h> for every hierarchy (None when build did not record it)."""
    return {h: check_status(out, "build", f"closure:{h}") for h in cfg.hierarchies}


def _sqlstate(e: BaseException) -> str:
    arg = e.args[0] if e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def _rel(path: Path, out: Path) -> str:
    try:
        return path.relative_to(out).as_posix()
    except ValueError:
        return path.name


def verdict(status: str, item: str) -> tuple[str, dict]:
    """Check status and detail of a suite outcome: a failing item kills the mutant (pass), a suite that passes lets it
    survive (fail), and anything else (an item that errored) leaves it unjudged (error)."""
    item = str(item or "")[:200]
    if status == "fail":
        return "pass", {"killed_by": item}
    if status == "pass":
        return "fail", {"killed_by": ""}
    return "error", {"reason": "suite_error", "item": item}


def _judge(cfg, out: Path, mutant: Mutant, n: int, suite) -> tuple[str, dict]:
    """Serve the mutant with a fresh Cube and run the suite: pass when killed, fail when it survives."""
    run_dir = out / "verify" / "mutants" / f"{mutant.operator}__{mutant.target}"
    log = run_dir / "server.log"
    try:
        cube_gen.write_model(mutant.model, run_dir / "model")
        log.unlink(missing_ok=True)
    except OSError as e:
        return "error", {"witness": n, "reason": "write", "exception": type(e).__name__}
    try:
        with cube_run.serve(cfg, run_dir, log) as client:
            try:
                status, item = suite(client)
            except Exception as e:  # noqa: BLE001 - the suite failed to run, which is not a kill
                return "error", {"witness": n, "reason": "suite_error", "exception": type(e).__name__}
    except StageError as e:
        return "error", {"witness": n, "reason": "startup", "exit_code": e.exit_code, "log": _rel(log, out)}
    except Exception as e:  # noqa: BLE001 - one broken Cube process must not stop the other mutants
        return "error", {"witness": n, "reason": "cube", "exception": type(e).__name__, "log": _rel(log, out)}
    check, detail = verdict(status, item)
    return check, {"witness": n, **detail}


def run_mutants(cfg, out: Path, conn, model: dict, suite, closure: dict | None = None,
                sampled_items=()) -> tuple[list[tuple], list[str]]:
    """Witness, serve and judge every selected mutant.

    suite(client) -> (status, item) runs the gold suite against one CubeClient and stops at the first item that does
    not pass: ("fail", item) kills the mutant, ("error", item) leaves it unjudged, ("pass", "") means it survived.
    item must be a check id because it is stored in the result. closure maps each hierarchy to the status of build's
    closure:<h> (default: read from out/build/result.json); the operators on a hierarchy whose closure did not pass
    are not_run. Each observable mutant is served from out/verify/mutants/<operator>__<target>/ (model/*.yml,
    model.json, server.log). Returns (checks, not_proven); checks are (check_id, status, config_ref, detail) tuples as
    in closure.build, and every detail carries the run's summary counts (OUTCOMES).

    sampled_items names the suite's V3 pair items that compare a sample of their keys (fewer than all): the detail of
    a mutant that survives such a suite gets sample_blind_possible = True and sampled_items, because the mutant may
    change only keys outside the samples. It is a marker only and never changes a status.
    """
    out = Path(out)
    closure = closure_status(cfg, out) if closure is None else closure
    sampled = sorted(str(i) for i in sampled_items or ())
    rows: list[tuple] = []
    not_proven: list[str] = []
    tally = dict.fromkeys(OUTCOMES, 0)
    for t in _plan(cfg):
        cid = f"v4:{t.operator}:{t.target}"
        built = closure.get(t.hierarchy) if t.hierarchy else "pass"
        if built != "pass":
            built = built or "missing"
            rows.append((cid, "not_run", f"hierarchies.{t.hierarchy}",
                         {"reason": "closure_not_passed", "hierarchy": t.hierarchy, "build_check": built}))
            not_proven.append(f"{cid}: closure:{t.hierarchy} did not pass in build ({built})")
            tally["not_run"] += 1
            continue
        try:
            mutant = t.build(cfg, model)
        except NotApplicable as e:
            not_proven.append(f"{cid} not run: {e}")
            tally["not_applicable"] += 1
            continue
        except MutationError as e:
            rows.append((cid, "error", t.config_ref, {"reason": "model_mismatch", "element": str(e)}))
            tally["error"] += 1
            continue
        try:
            n = witness(conn, cfg, mutant)
        except Exception as e:  # noqa: BLE001 - keep the type and SQLSTATE only (messages can quote values)
            rows.append((cid, "error", t.config_ref, {"reason": "witness", "exception": type(e).__name__,
                                                       "sqlstate": _sqlstate(e)}))
            tally["error"] += 1
            continue
        if n == 0:
            not_proven.append(f"{cid} equivalent on this data")
            tally["equivalent"] += 1
            continue
        status, detail = _judge(cfg, out, mutant, n, suite)
        if status == "fail" and sampled:
            detail = {**detail, "sample_blind_possible": True, "sampled_items": sampled}
        tally[{"pass": "killed", "fail": "survived"}.get(status, "error")] += 1
        rows.append((cid, status, mutant.config_ref, detail))
    return [(cid, st, ref, {**detail, "summary": dict(tally)}) for cid, st, ref, detail in rows], not_proven
