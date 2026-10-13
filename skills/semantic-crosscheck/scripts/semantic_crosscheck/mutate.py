"""V4 mutation tests: deliberately broken copies of the generated Cube model.

A gold suite that passes proves little until it is shown to fail on a wrong model. Each operator patches
one element of a deep copy of the model (a closure-cube member, the WHERE clause of a measure cube, a
measure type, a join); a fresh Cube process serves the copy and the suite has to notice. Before Cube starts,
a witness query counts the rows of the current data on which the mutation changes a result. Witness 0 means
the mutant is equivalent on this data: no suite could kill it, so it goes to not_proven instead of being run.
A mutant with a positive witness that survives fails its check: the gold suite does not observe that
element, and the check names the configuration entry the element comes from.

  operator                    patched element                                  witness (current data)
  closure_depth_shift         <h>_closure.min_depth -> (min_depth) + 1          closure rows
  closure_path_count_one      <h>_closure.path_count -> 1                       pairs with path_count > 1
  closure_qty_single_path     <h>_closure.qty -> (qty) / (path_count)           pairs with path_count > 1 and qty <> 0
  measure_filter_drop         m_<m> SQL without the exact filter                rows the filter excludes
  measure_normalization_drop  m_<m> SQL comparing raw values                    rows only the normalised match keeps
  measure_agg_to_max          m_<m>.value type -> max (sum and count only)      keys with a non-zero value, when >= 2
  dated_shift_one_day         m_<m>.ts -> (ts) + interval '1 day'               rows dated on a gold window end or
                                                                                the day before a window start
  closure_join_swap           <h>_closure join to m_<m> on the other pair end   pairs whose ends differ in the measure
  closure_end_swap            <h>_closure join to <h>_descendant on ancestor    pairs whose ends differ in the ontology
                                                                                class column (all pairs without one)

The filter witnesses count only rows that can move the measure: a non-null value, non-zero for sums.
Filter mutants replace cube_gen.where_sql(measure) inside the measure cube's SQL with where_sql of a copy
changed by dataclasses.replace. That is exactly the SQL cube_gen.measure_cube generates for the copy, without
needing column types. When the model's SQL does not contain the configured clause, the model came from
another configuration and the mutant becomes an error check, never a silently different test.

The dated witness reads the date windows of the gold queries that filter that ts member (timeDimensions
dateRange, inDateRange/notInDateRange filters) for every binding listed in the configuration. When a window
cannot be listed without the database (a swept values_of or strata parameter, a relative range, another
operator on ts), it counts every dated row instead, so that equivalence is never claimed without evidence.

Witness SQL is read-only, takes identifiers from ident/naming, binds window dates as parameters and repeats
filter values only as the config-trusted literals of cube_gen.where_sql. Checks and notes carry counts and
ids only: the suite's kill reason must be a check id, and errors keep an exception type or SQLSTATE.
"""

import copy
import dataclasses
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import cube_gen, cube_run, db, ident, naming
from .result import EXIT_CONFIG, StageError

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
WINDOW_FILTERS = ("inDateRange", "notInDateRange")


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
    cube = _closure_cube(model, h)
    desc = naming.cube_descendant(h.name)

    def patch(mm):
        _swap_end(_element(mm, cube, "joins", desc), "descendant", "ancestor", f"{cube}.joins.{desc}")
    closure = _closure_sql(cfg, h)
    o = cfg.ontology
    if o.class_column and o.hierarchy in cfg.hierarchies and cfg.hierarchies[o.hierarchy].node == h.node:
        node = cfg.entity(h.node, f"hierarchies.{h.name}")
        table, key, cls = ident.qualified(node.table), ident.quote(node.key[0]), ident.quote(o.class_column)
        sql = (f"SELECT count(*) FROM {closure} p LEFT JOIN {table} a ON a.{key} = p.ancestor "
               f"LEFT JOIN {table} d ON d.{key} = p.descendant WHERE a.{cls} IS DISTINCT FROM d.{cls}")
    else:
        sql = f"SELECT count(*) FROM {closure}"
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
    """Rows selected by exactly one of the two WHERE clauses that can move the measure."""
    return (f"SELECT count(*) FROM {_table(cfg, m)} t "
            f"WHERE ((({cube_gen.where_sql(m)}) IS TRUE) <> (({cube_gen.where_sql(m2)}) IS TRUE)) AND {_counted(m)}")


def _filter_drop(cfg, model: dict, m) -> Mutant:
    m2 = dataclasses.replace(m, filter={})
    return Mutant("measure_filter_drop", m.name, _rewrite_where(model, m, m2), _filter_witness(cfg, m, m2), {},
                  f"measures.{m.name}")


def _normalization_drop(cfg, model: dict, m) -> Mutant:
    m2 = dataclasses.replace(m, filter={**m.filter, **m.filter_normalized}, filter_normalized={})
    return Mutant("measure_normalization_drop", m.name, _rewrite_where(model, m, m2), _filter_witness(cfg, m, m2), {},
                  f"measures.{m.name}")


def _agg_to_max(cfg, model: dict, m) -> Mutant:
    cube = naming.cube_measure(m.name)

    def patch(mm):
        _element(mm, cube, "measures", naming.MEASURE_VALUE)["type"] = "max"
    sql = f"SELECT CASE WHEN count(*) >= 2 THEN count(*) ELSE 0 END FROM ({_per_key(cfg, m)}) k WHERE k.v <> 0"
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


# ---- dated shift ---------------------------------------------------------------------------------
def _flat_filters(filters) -> list[dict]:
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        for logical in ("or", "and"):
            out.extend(_flat_filters(f.get(logical)))
        if "member" in f or "dimension" in f:
            out.append(f)
    return out


def _bindings(g) -> list[dict] | None:
    """Parameter bindings listed in the configuration; None for a values_of or strata sweep (needs the database)."""
    if g.param_sets:
        return [dict(ps) for ps in g.param_sets]
    if g.params:
        (p,) = g.params.values()
        return [{p.name: v} for v in p.values] if p.values else None
    return [{}]


def _subst(obj, binding: dict):
    if isinstance(obj, str) and obj.startswith(":") and obj[1:] in binding:
        return binding[obj[1:]]
    if isinstance(obj, list):
        return [_subst(x, binding) for x in obj]
    return obj


def _window(rng) -> tuple[str, str] | None:
    """A bound [from, to] date range as ISO dates; None for relative ranges and unbound parameters."""
    if not isinstance(rng, list) or len(rng) != 2:
        return None
    try:
        start, end = (dt.date.fromisoformat(str(x)[:10]).isoformat() for x in rng)
    except ValueError:
        return None
    return start, end


def _gold_windows(cfg, ts_member: str) -> tuple[list[tuple[str, str]], bool, bool]:
    """Date windows the gold queries apply to ts_member: (windows, used, unresolved)."""
    found, used, unresolved = [], False, False
    for g in cfg.gold:
        q = g.cube_query
        ranges = [td.get("dateRange") for td in q.get("timeDimensions", [])
                  if td.get("dimension") == ts_member and "dateRange" in td]
        for f in _flat_filters(q.get("filters")):
            if f.get("member", f.get("dimension")) != ts_member:
                continue
            if f.get("operator") in WINDOW_FILTERS:
                ranges.append(f.get("values"))
            else:
                used = unresolved = True
        if ts_member in q.get("dimensions", []):
            used = unresolved = True
        if not ranges:
            continue
        used = True
        bindings = _bindings(g)
        if bindings is None:
            unresolved = True
            continue
        for rng in ranges:
            for b in bindings:
                w = _window(_subst(rng, b))
                if w is None:
                    unresolved = True
                else:
                    found.append(w)
    return found, used, unresolved


def _dated_shift(cfg, model: dict, m) -> Mutant:
    cube = naming.cube_measure(m.name)
    ts_member = naming.member(cube, naming.MEASURE_TS)

    def patch(mm):
        d = _element(mm, cube, "dimensions", naming.MEASURE_TS)
        d["sql"] = f"({d['sql']}) + interval '1 day'"
    mutated = _mutated(model, patch)
    found, used, unresolved = _gold_windows(cfg, ts_member)
    params: dict = {}
    if not used:
        sql = "SELECT 0"
    else:
        rows_sql = _cube(model, cube).get("sql")
        if not rows_sql:
            raise MutationError(f"{cube}.sql")
        ts = f"c.{ident.quote(naming.MEASURE_TS)}"
        # the cube's own row SQL: its ts is a timestamptz already, so the UTC day needs no column type
        base = f"SELECT count(*) FROM ({rows_sql}) c WHERE "
        if unresolved:
            sql = base + f"{ts} IS NOT NULL"
        else:
            day = f"({ts} AT TIME ZONE 'UTC')::date"
            sql = base + f"{day} = ANY(CAST(:ends AS date[])) OR {day} + 1 = ANY(CAST(:starts AS date[]))"
            params = {"ends": sorted({e for _s, e in found}), "starts": sorted({s for s, _e in found})}
    return Mutant("dated_shift_one_day", m.name, mutated, sql, params, f"measures.{m.name}")


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
    """Operators named by verify.mutation_operators ("default" = all), in catalogue order."""
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
            plan.extend(_Target(op, h.name, f"hierarchies.{h.name}", h, make) for h in cfg.hierarchies.values())
        else:
            plan.extend(_Target(op, m.name, f"measures.{m.name}", m, make) for m in cfg.measures.values() if applies(m))
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


def _sqlstate(e: BaseException) -> str:
    arg = e.args[0] if e.args else None
    return arg.get("C", "") if isinstance(arg, dict) else ""


def _rel(path: Path, out: Path) -> str:
    try:
        return path.relative_to(out).as_posix()
    except ValueError:
        return path.name


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
                killed, reason = suite(client)
            except Exception as e:  # noqa: BLE001 - the suite failed to run, which is not a kill
                return "error", {"witness": n, "reason": "suite", "exception": type(e).__name__}
    except StageError as e:
        return "error", {"witness": n, "reason": "startup", "exit_code": e.exit_code, "log": _rel(log, out)}
    except Exception as e:  # noqa: BLE001 - one broken Cube process must not stop the other mutants
        return "error", {"witness": n, "reason": "cube", "exception": type(e).__name__, "log": _rel(log, out)}
    return ("pass" if killed else "fail"), {"witness": n, "killed_by": str(reason or "")[:200]}


def run_mutants(cfg, out: Path, conn, model: dict, suite) -> tuple[list[tuple], list[str]]:
    """Witness, serve and judge every selected mutant.

    suite(client) -> (killed, reason) runs the gold suite against one CubeClient and stops at the first
    difference; reason must be a check id because it is stored in the result. Each observable mutant is
    served from out/verify/mutants/<operator>__<target>/ (model/*.yml, model.json, server.log).
    Returns (checks, not_proven); checks are (check_id, status, config_ref, detail) tuples as in closure.build,
    and every detail carries the run's summary counts (killed, survived, equivalent, error, not_applicable).
    """
    out = Path(out)
    rows: list[tuple] = []
    not_proven: list[str] = []
    tally = {"killed": 0, "survived": 0, "equivalent": 0, "error": 0, "not_applicable": 0}
    for t in _plan(cfg):
        cid = f"v4:{t.operator}:{t.target}"
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
        tally[{"pass": "killed", "fail": "survived"}.get(status, "error")] += 1
        rows.append((cid, status, mutant.config_ref, detail))
    return [(cid, st, ref, {**detail, "summary": dict(tally)}) for cid, st, ref, detail in rows], not_proven
