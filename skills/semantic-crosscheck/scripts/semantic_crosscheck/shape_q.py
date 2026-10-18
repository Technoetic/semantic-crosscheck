"""V3 query shapes derived from the generated model (design section 7).

  total     each measure cube's `value` total equals SQL computed from the configuration (cube_gen.where_sql and
            the measure's aggregate); the closure cube's `qty` and `pairs` equal the closure table.
  additive  for every sum/count measure (the configuration's measures and the closure cube's `qty` and `pairs`) and
            every cube with the ontology class column that Cube joins to the measure's cube through many_to_one or
            one_to_one steps only (join_path, seen from the measure), the groups by that column, the NULL group
            included, add up to the measure's total, and the NULL group equals independent SQL over the measure
            source (null_group_sql). Which source rows form the NULL group depends on the cube Cube roots the
            grouped query at (the test's root): from the measure's cube (a forward path, e.g. closure ->
            <h>_ancestor) Cube LEFT JOINs the dimension, so the rows with no dimension row whose class is set (an
            anti-join); from the dimension's cube (a backward path, e.g. a generated node cube's one_to_one join
            to its measure cube) Cube drops the measure rows with no dimension row, so only the matched rows whose
            class is NULL. Measure rows Cube drops (keys missing from the node table) or a join that fans out
            break the sum; rows Cube puts in the wrong group break the NULL group. A path with a one_to_many (or
            unknown) step gets no test, because a row legitimately counts in several groups; not_applicable lists
            it for not_proven, and so it lists an additive path whose NULL group no configuration entry describes.
  pair      measures of cubes joined to the same parent, queried together, equal the same measures queried
            alone, per key: the closure `qty` with each closure-joined measure (grouped by the closure end the
            measure is joined on, `ancestor` in the demo) and every two measures joined to the same node cube
            (grouped by the node key). The alone value of a measure is read from its own cube by its own key, so
            a relationship that multiplies rows shows up as a mismatch (join fan-out).

Tests are derived once from the unmutated model and replayed against mutants, so they state what the
configuration means rather than what a possibly mutated model does. Values are compared with canon (exact); the
keys of a pair test in the form of their Cube member type (compare.member_kinds), so text keys such as '1.10' and
'1.1' stay two keys. A count over no rows is 0 in SQL but NULL as a sum of per-key counts, so a NULL count total
counts as 0 and is reported as `null_as_zero`.

A pair test groups by every key of its node entity or closure end. Cube joins the per-cube results of such a query
with a nested loop, so its cost grows with the square of the keys, and the client's row limit caps the answer.
Above PAIR_KEY_CAP keys the test therefore compares a deterministic sample of PAIR_KEY_CAP keys (sweep.md5_order with
verify.seed, read once from the node table or the closure table and reused by every replay), filtered in each of
its queries, and its detail says how many keys were compared out of how many (sampled_keys, total_keys). The key
count and the sample are read in one REPEATABLE READ snapshot, so the sample is drawn from the keys that were counted.
A sampled detail also carries the sample's detection bound (d95_keys, and d95_rate = d95_keys / total_keys): the
smallest number of mismatching keys of which a sample of n out of N keys holds at least one with probability 95% or
more, assuming the mismatching keys are independent of the fixed md5 order; fewer mismatching keys may all lie outside
the sample.
Details hold counts and flags only, never totals or keys.

The additive tests are bounded by the configuration: one per additive measure and cube with the class column that
Cube can join it to (in a generated model: the node cube of each measure with a node, and the ancestor and
descendant copies for `qty` and `pairs`), with as many groups as the column has values. The NULL group's SQL side
(null_group_sql) is read once per test, like a pair test's key sample, and reused by every replay; its detail holds
whether the two NULL groups are equal and the row counts of the SQL NULL group and of the source rows with no
dimension row (null_group_rows, orphan_rows; under a backward path the orphans are counted but are no NULL group).
"""

import math
from decimal import Decimal, InvalidOperation

from . import canon, compare, cube_gen, db, ident, naming, sweep

PAIR_KEY_CAP = 2000
MISS = 20          # the detection bound allows a miss probability of at most 1/MISS (5%)
ADDITIVE_STEPS = ("many_to_one", "one_to_one")
_RELATIONSHIPS = {"belongs_to": "many_to_one", "belongsTo": "many_to_one", "has_many": "one_to_many",
                  "hasMany": "one_to_many", "has_one": "one_to_one", "hasOne": "one_to_one"}
_INVERSE = {"many_to_one": "one_to_many", "one_to_many": "many_to_one", "one_to_one": "one_to_one"}


def _m(cube: str, name: str) -> str:
    return naming.member(cube, name)


def tests(cfg, model: dict) -> list[dict]:
    """Every V3 test of the model: dicts with id, kind, members (every member queried) and kind-specific keys."""
    cubes = {c["name"]: c for c in model.get("cubes", [])}
    kinds = compare.member_kinds(model)
    out = _totals(cfg, cubes)
    out += _additive(cfg, cubes)
    out += _closure_pairs(cfg, cubes, kinds)
    out += _node_pairs(cfg, cubes, kinds)
    return out


def _joins(cube: dict | None) -> dict:
    return {j["name"]: j for j in (cube or {}).get("joins", [])}


def _totals(cfg, cubes: dict) -> list[dict]:
    out = []
    for m in cfg.measures.values():
        mc = naming.cube_measure(m.name)
        if mc in cubes:
            member = _m(mc, naming.MEASURE_VALUE)
            out.append({"id": f"total:{member}", "kind": "total", "members": [member], "measure": m.name,
                        "config_ref": f"measures.{m.name}"})
    for h in cfg.hierarchies.values():
        cc = naming.cube_closure(h.name)
        if cc in cubes:
            for col in naming.CLOSURE_MEASURES:
                member = _m(cc, col)
                out.append({"id": f"total:{member}", "kind": "total", "members": [member], "hierarchy": h.name,
                            "column": col, "config_ref": f"hierarchies.{h.name}"})
    return out


def relationship(join: dict) -> str:
    """A join's relationship in snake_case (Cube's has_many / belongs_to / has_one aliases folded); any other value is
    kept as it is (an absent one as ''), so it is never taken for an additive step."""
    r = join.get("relationship") or ""
    return _RELATIONSHIPS.get(r, r)


def _directed(cubes: dict, start: str, goal: str) -> list[tuple] | None:
    """The shortest chain of declared joins start -> ... -> goal as [(cube, next cube, relationship)], or None."""
    prev: dict[str, tuple | None] = {start: None}
    todo = [start]
    while todo and goal not in prev:
        nxt = []
        for c in todo:
            for j in (cubes.get(c) or {}).get("joins", []):
                n = j.get("name")
                if n in cubes and n not in prev:
                    prev[n] = (c, relationship(j))
                    nxt.append(n)
        todo = nxt
    if goal == start or goal not in prev:
        return None
    steps, n = [], goal
    while prev[n] is not None:
        c, r = prev[n]
        steps.append((c, n, r))
        n = c
    return steps[::-1]


def join_path(cubes: dict, measure_cube: str, dim_cube: str) -> list[tuple] | None:
    """The join path of a query of a measure of `measure_cube` grouped by a dimension of `dim_cube`, seen from the
    measure: [(cube, next cube, relationship from the cube to the next)]. Cube joins the cubes of a query from one of
    them through declared joins, so either the measure's cube reaches the dimension's cube (relationships as
    declared) or the dimension's cube reaches the measure's (each step reversed and inverted: a dimension cube that
    declares one_to_many reaches measure rows that each belong to one of its rows). None when neither reaches the
    other: Cube cannot join the two cubes on their own, so there is no such query to test."""
    forward = _directed(cubes, measure_cube, dim_cube)
    if forward:
        return forward
    back = _directed(cubes, dim_cube, measure_cube)
    if back is None:
        return None
    return [(b, a, _INVERSE.get(r, r)) for a, b, r in reversed(back)]


def additive_path(steps: list | None) -> bool:
    """Whether every row of the measure's cube reaches at most one row of the dimension's cube: a non-empty path made
    of many_to_one and one_to_one steps only."""
    return bool(steps) and all(r in ADDITIVE_STEPS for _a, _b, r in steps)


def _path_text(steps: list) -> str:
    return steps[0][0] + "".join(f" -> {b} ({r or 'no relationship'})" for _a, b, r in steps)


def _measure_type(cube: dict, name: str) -> str:
    return next((m.get("type", "") for m in cube.get("measures", []) if m.get("name") == name), "")


def _additive_measures(cfg, cubes: dict) -> list[tuple]:
    """(cube, measure name, aggregation, config_ref, source) of every measure whose groups must add up: sum and count
    measures of the configuration and the closure's qty and pairs. Other aggregations (min, max, count_distinct,
    avg, ...) are not additive and get no test."""
    out = []
    for m in cfg.measures.values():
        mc = naming.cube_measure(m.name)
        if m.agg not in ("sum", "count") or m.date_column or mc not in cubes:
            continue
        if _measure_type(cubes[mc], naming.MEASURE_VALUE) not in ("sum", "count"):
            continue
        out.append((mc, naming.MEASURE_VALUE, m.agg, f"measures.{m.name}", {"measure": m.name}))
    for h in cfg.hierarchies.values():
        cc = naming.cube_closure(h.name)
        for col in naming.CLOSURE_MEASURES if cc in cubes else ():
            agg = _measure_type(cubes[cc], col)
            if agg in ("sum", "count"):
                out.append((cc, col, agg, f"hierarchies.{h.name}", {"hierarchy": h.name, "closure_column": col}))
    return out


def _null_source(cfg, source: dict, dim_cube: str) -> dict | None:
    """The configuration's description of the join from a measure source to the cube `dim_cube` (dimension table, its
    key and the source column that refers to it), or None when no configuration entry describes that join."""
    if "measure" in source:
        m = cfg.measures[source["measure"]]
        node = cfg.entities.get(m.node)
        if node and len(node.key) == 1 and dim_cube == naming.cube_entity(m.node):
            return {**source, "dim_table": node.table, "dim_key": node.key[0], "fk": m.by}
        return None
    h = cfg.hierarchies[source["hierarchy"]]
    node = cfg.entities[h.node]
    for end, cube in (("ancestor", naming.cube_ancestor(h.name)), ("descendant", naming.cube_descendant(h.name))):
        if dim_cube == cube:
            return {**source, "dim_table": node.table, "dim_key": node.key[0], "fk": end}
    return None


def _additive_plan(cfg, cubes: dict) -> tuple[list[dict], list[str]]:
    """(additive tests, not_proven lines for the measure / class-column pairs Cube can join but that get no test)."""
    col = cfg.ontology.class_column
    if not col:
        return [], []
    dims = [name for name, c in cubes.items() if any(d.get("name") == col for d in c.get("dimensions", []))]
    out, notes = [], []
    for mc, name, agg, ref, source in _additive_measures(cfg, cubes):
        for dc in dims:
            steps = None if dc == mc else join_path(cubes, mc, dc)
            if steps is None:
                continue
            measure, dim = _m(mc, name), _m(dc, col)
            tid = f"additive:{measure}:{dim}"
            if not additive_path(steps):
                notes.append(f"v3:{tid}: not applicable: the join path {_path_text(steps)} has a step that is not "
                             "many_to_one or one_to_one, so a row can count in several groups; no additivity check")
                continue
            null_source = _null_source(cfg, source, dc)
            if null_source is None:
                notes.append(f"v3:{tid}: not compared: no configuration entry describes the join path "
                             f"{_path_text(steps)}, so its NULL group has no independent SQL; no additivity check")
                continue
            # the cube Cube roots the grouped query at, as join_path chose the direction: the measure's cube when it
            # reaches the dimension's cube itself, otherwise the dimension's cube (null_group_sql depends on it)
            root = "measure" if _directed(cubes, mc, dc) else "dimension"
            out.append({"id": tid, "kind": "additive", "members": [measure, dim], "measure": measure,
                        "dimension": dim, "agg": agg, "path": [list(s) for s in steps], "root": root,
                        "null_source": {**null_source, "class_column": col}, "config_ref": ref})
    return out, notes


def _additive(cfg, cubes: dict) -> list[dict]:
    return _additive_plan(cfg, cubes)[0]


def not_applicable(cfg, model: dict) -> list[str]:
    """not_proven lines for the measure / class-column pairs of the model that get no additive test although Cube can
    join them: a join path with a step that is not many_to_one or one_to_one, or one no configuration entry
    describes."""
    return _additive_plan(cfg, {c["name"]: c for c in model.get("cubes", [])})[1]


def _closure_pairs(cfg, cubes: dict, kinds: dict) -> list[dict]:
    out = []
    for h in cfg.hierarchies.values():
        cc = naming.cube_closure(h.name)
        joins = _joins(cubes.get(cc))
        for m in cfg.measures.values():
            mc = naming.cube_measure(m.name)
            if not m.closure_join or mc not in cubes or mc not in joins:
                continue
            qty, value = _m(cc, "qty"), _m(mc, naming.MEASURE_VALUE)
            end, key = _m(cc, m.closure_join), _m(mc, naming.MEASURE_KEY)
            out.append({"id": f"pair:{qty}+{value}", "kind": "pair", "members": [qty, value, end, key],
                        "measures": [qty, value], "group_by": end, "alone": {qty: end, value: key},
                        "kinds": {k: kinds.get(k) for k in (end, key)},
                        "keys_from": [naming.closure_table(cfg, h.name), m.closure_join],
                        "config_ref": f"measures.{m.name}"})
    return out


def _node_pairs(cfg, cubes: dict, kinds: dict) -> list[dict]:
    out = []
    by_cube = {naming.cube_measure(m.name): m for m in cfg.measures.values() if not m.date_column}
    for name, ent in cfg.entities.items():
        nc = naming.cube_entity(name)
        if nc not in cubes or len(ent.key) != 1:
            continue
        joined = [j for j, spec in _joins(cubes[nc]).items()
                  if j in by_cube and j in cubes and spec.get("relationship") == "one_to_one"]
        node_key = _m(nc, ent.key[0])
        for i, a in enumerate(joined):
            for b in joined[i + 1:]:
                va, vb = _m(a, naming.MEASURE_VALUE), _m(b, naming.MEASURE_VALUE)
                ka, kb = _m(a, naming.MEASURE_KEY), _m(b, naming.MEASURE_KEY)
                out.append({"id": f"pair:{va}+{vb}", "kind": "pair", "members": [va, vb, node_key, ka, kb],
                            "measures": [va, vb], "group_by": node_key, "alone": {va: ka, vb: kb},
                            "kinds": {k: kinds.get(k) for k in (node_key, ka, kb)},
                            "keys_from": [ent.table, ent.key[0]],
                            "config_ref": f"measures.{by_cube[a].name}, measures.{by_cube[b].name}"})
    return out


def measure_total_sql(cfg, m) -> str:
    """The configuration's definition of a measure's total (independent of the Cube model)."""
    ent = cfg.entity(m.entity, f"measures.{m.name}")
    return (f"SELECT {m.agg}(t.{ident.quote(m.column)}) FROM {ident.qualified(ent.table)} t"
            f" WHERE {cube_gen.where_sql(m)}")


def closure_total_sql(cfg, h: str, column: str) -> str:
    agg = f"sum({ident.quote('qty')})" if column == "qty" else "count(*)"
    return f"SELECT {agg} FROM {naming.closure_table(cfg, h)}"


def null_group_sql(cfg, test: dict) -> str:
    """The NULL group of an additive test by the configuration, independent of the Cube model: (the measure over the
    source rows Cube puts in the NULL group, the number of those rows, the number of source rows with no dimension
    row at all). Which rows those are depends on the cube Cube roots the grouped query at (test["root"]):

      measure    the measure's cube reaches the dimension's cube (a forward path, e.g. closure -> <h>_ancestor):
                 Cube LEFT JOINs the dimension to every measure row, so the NULL group holds the rows with no
                 dimension row with a non-null class: rows with no dimension row at all (an anti-join, NOT EXISTS)
                 and rows whose dimension row has no class.
      dimension  only the dimension's cube reaches the measure's (a backward path, e.g. a generated node cube's
                 one_to_one join to its measure cube): Cube LEFT JOINs the measure rows to the dimension rows and
                 drops the measure rows with no dimension row, so the NULL group holds only the matched rows whose
                 class is NULL. The dropped rows are in the measure's total but in no group, so the sum catches
                 them; they are still counted (the third column).

    Any other root is a ValueError: no NULL group is guessed."""
    src = test["null_source"]
    dim, key, fk = ident.qualified(src["dim_table"]), ident.quote(src["dim_key"]), f"t.{ident.quote(src['fk'])}"
    matched = f"SELECT 1 FROM {dim} d WHERE d.{key} = {fk}"
    classified = f"EXISTS ({matched} AND d.{ident.quote(src['class_column'])} IS NOT NULL)"
    if test.get("root") == "measure":
        null_group = f"NOT {classified}"
    elif test.get("root") == "dimension":
        null_group = f"EXISTS ({matched}) AND NOT {classified}"
    else:
        raise ValueError("additive test without a known join root")
    if "measure" in src:
        m = cfg.measures[src["measure"]]
        table = ident.qualified(cfg.entity(m.entity, f"measures.{m.name}").table)
        value, where = f"{m.agg}(t.{ident.quote(m.column)})", f" WHERE {cube_gen.where_sql(m)}"
    else:
        table, where = naming.closure_table(cfg, src["hierarchy"]), ""
        value = f"sum(t.{ident.quote('qty')})" if src["closure_column"] == "qty" else "count(*)"
    return (f"SELECT {value} FILTER (WHERE {null_group}), count(*) FILTER (WHERE {null_group}), "
            f"count(*) FILTER (WHERE NOT EXISTS ({matched})) FROM {table} t{where}")


def pair_keys(conn, cfg, test: dict) -> tuple[int, list | None]:
    """(distinct keys a pair test groups by, a deterministic sample of PAIR_KEY_CAP of them or None when there are not
    more than that), read from test["keys_from"] (table, column) in one READ ONLY REPEATABLE READ transaction, so the
    count and the sample see the same rows."""
    table, column = test["keys_from"]
    col, source = f"t.{ident.quote(column)}", ident.qualified(table)
    with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
        total = int(db.scalar(conn, f"SELECT count(DISTINCT {col}) FROM {source} t") or 0)
        if total <= PAIR_KEY_CAP:
            return total, None
        rows = conn.run(f"SELECT s.k FROM (SELECT DISTINCT {col} AS k FROM {source} t WHERE {col} IS NOT NULL) s "
                        f"ORDER BY {sweep.md5_order('s.k')} LIMIT :n", seed=str(cfg.verify.seed), n=PAIR_KEY_CAP)
    return total, [r[0] for r in rows or []]


def _count(v, name: str) -> int:
    if type(v) is not int or v < 0:
        raise ValueError(f"d95_keys: {name} must be a non-negative integer")
    return v


def d95_keys(sampled: int, total: int) -> int | None:
    """The smallest D >= 1 such that a uniform sample of `sampled` out of `total` keys misses all of D mismatching keys
    with probability C(total - D, sampled) / C(total, sampled) <= 5% (zero-acceptance hypergeometric sampling), or
    None when the sample is not smaller than the population (every key is compared). Exact: the comparison is done on
    integers (math.comb), with the symmetric form C(total - sampled, D) / C(total, D) while D < sampled, and the
    probability decreases in D, so a bisection over 1 .. total - sampled + 1 (where it is 0) finds the bound."""
    n, total = _count(sampled, "sampled"), _count(total, "total")
    if n < 1:
        raise ValueError("d95_keys: sampled must be at least 1")
    if total <= n:
        return None

    def detected(d: int) -> bool:
        if d < n:
            return MISS * math.comb(total - n, d) <= math.comb(total, d)
        return MISS * math.comb(total - d, n) <= math.comb(total, n)
    lo, hi = 1, total - n + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if detected(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def sample_note(detail: dict) -> str:
    """The not_proven text of a sampled pair test's detail: how many keys it compared and its detection bound."""
    n, total = detail["sampled_keys"], detail["total_keys"]
    note = f"compared {n} of {total} keys (a deterministic sample with verify.seed); the other keys were not compared"
    d95 = detail.get("d95_keys")
    if not d95:
        try:
            d95 = d95_keys(n, total)
        except ValueError:
            d95 = None
    if not d95:
        return note
    return (f"{note}; a mismatch affecting at least {d95} keys ({100 * d95 / total:.3g}%) is detected with 95% "
            "probability, assuming the mismatching keys are independent of the fixed md5 sample order; fewer "
            "mismatching keys may be missed")


def run_test(conn, client, cfg, model: dict, test: dict) -> tuple[str, dict]:
    """(status, detail) of one test; errors are reported by type, never by message."""
    run = {"total": _run_total, "additive": _run_additive, "pair": _run_pair}[test["kind"]]
    try:
        return run(conn, client, cfg, test)
    except compare.expected_errors() as e:
        return "error", compare.error_detail(e)


def _single(rows: list[dict], member: str):
    if len(rows) != 1:
        raise ValueError(f"expected one total row for {member}, got {len(rows)}")
    return rows[0].get(member)


def _sql_value(conn, cfg, sql: str):
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        return db.scalar(conn, sql)


def _run_total(conn, client, cfg, test: dict) -> tuple[str, dict]:
    member = test["members"][0]
    cube_v = _single(client.load({"measures": [member]}), member)
    detail = {}
    if "measure" in test:
        m = cfg.measures[test["measure"]]
        sql_v = _sql_value(conn, cfg, measure_total_sql(cfg, m))
        if m.agg == "count" and cube_v is None:
            cube_v, detail["null_as_zero"] = 0, True
    else:
        sql_v = _sql_value(conn, cfg, closure_total_sql(cfg, test["hierarchy"], test["column"]))
    equal = canon.same(cube_v, sql_v)
    return ("pass" if equal else "fail"), {"equal": equal, **detail}


def _number(v) -> Decimal:
    c = canon.canon(v)
    try:
        if not isinstance(c, str):
            raise TypeError
        return Decimal(c)
    except (InvalidOperation, TypeError) as e:
        raise ValueError("a measure returned a value that is not a number") from e


def _sum(values: list):
    present = [_number(v) for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


def _null_group(conn, cfg, test: dict) -> tuple:
    """null_group_sql of the test, read once: the test dict keeps (value, rows, orphan rows) in memory for the mutant
    replays. The value is a database quantity, so it never goes into a detail or a result."""
    if "sql_null_group" not in test:
        with db.read_only(conn, cfg.source.statement_timeout_ms):
            rows = conn.run(null_group_sql(cfg, test))
        if not rows or len(rows) != 1:
            raise ValueError("expected one row from the NULL group query")
        value, n, orphans = rows[0]
        test["sql_null_group"] = (value, int(n or 0), int(orphans or 0))
    return test["sql_null_group"]


def _run_additive(conn, client, cfg, test: dict) -> tuple[str, dict]:
    measure, dim = test["measure"], test["dimension"]
    total = _single(client.load({"measures": [measure]}), measure)
    groups = client.load({"dimensions": [dim], "measures": [measure]})
    sql_null, null_rows, orphans = _null_group(conn, cfg, test)
    cube_null = _sum([g.get(measure) for g in groups if g.get(dim) is None])
    if test["agg"] == "count":   # a count over no rows is 0 in SQL, NULL as a sum of per-key counts
        cube_null, sql_null = cube_null or 0, sql_null or 0
    equal = canon.same(_sum([g.get(measure) for g in groups]), total)
    null_equal = canon.same(cube_null, sql_null)
    detail = {"equal": equal, "groups": len(groups), "null_class_groups": sum(1 for g in groups if g.get(dim) is None),
              "null_group_equal": null_equal, "null_group_rows": null_rows, "orphan_rows": orphans}
    return ("pass" if equal and null_equal else "fail"), detail


def _keyed(rows: list[dict], key: str, kind: str | None) -> tuple[dict, int]:
    idx, dups = {}, set()
    for r in rows:
        k = canon.canon(r.get(key), kind)
        if k in idx:
            dups.add(k)
        else:
            idx[k] = r
    return idx, len(dups)


def _sample(conn, cfg, test: dict) -> tuple[int | None, list | None]:
    """pair_keys of the test, read once: the test dict keeps them in memory for the mutant replays. They are key
    values, so they never go into a detail or a result."""
    if "keys_from" not in test:
        return None, None
    if "sample_keys" not in test:
        test["total_keys"], test["sample_keys"] = pair_keys(conn, cfg, test)
    return test["total_keys"], test["sample_keys"]


def sampled(test: dict) -> bool:
    """Whether a pair test that has run compares a sample of its keys rather than every key."""
    return test.get("sample_keys") is not None


def _filter_text(v) -> str:
    """A key as a Cube filter value: text exactly, any other value in canonical form."""
    k = canon.key(v)
    return str(k).lower() if isinstance(k, bool) else str(k)


def _restricted(query: dict, member: str, sample: list | None) -> dict:
    """The query limited to the sampled keys of `member` (unchanged when every key is compared)."""
    if sample is None:
        return query
    return {**query, "filters": [{"member": member, "operator": "equals", "values": [_filter_text(v) for v in sample]}]}


def _run_pair(conn, client, cfg, test: dict) -> tuple[str, dict]:
    measures, group, kinds = test["measures"], test["group_by"], test.get("kinds", {})
    total, sample = _sample(conn, cfg, test)
    together_q = _restricted({"dimensions": [group], "measures": measures}, group, sample)
    together, dup = _keyed(client.load(together_q), group, kinds.get(group))
    mismatched, alone_only, alone_dups = {}, {}, 0
    for m in measures:
        dim = test["alone"][m]
        alone, d = _keyed(client.load(_restricted({"dimensions": [dim], "measures": [m]}, dim, sample)), dim, kinds.get(dim))
        alone_dups += d
        bad = sum(1 for k, row in together.items() if not canon.same(row.get(m), alone.get(k, {}).get(m)))
        if bad:
            mismatched[m] = bad
        alone_only[m] = sum(1 for k in alone if k not in together)
    ok = not (mismatched or dup or alone_dups)
    detail = {"keys": len(together), "mismatched": mismatched, "duplicate_keys": dup + alone_dups,
              "alone_only_keys": alone_only}
    if sample is not None:
        detail.update(sampled_keys=len(sample), total_keys=total)
        d95 = d95_keys(len(sample), total) if sample else None
        if d95:
            detail.update(d95_keys=d95, d95_rate=d95 / total)
    return ("pass" if ok else "fail"), detail
