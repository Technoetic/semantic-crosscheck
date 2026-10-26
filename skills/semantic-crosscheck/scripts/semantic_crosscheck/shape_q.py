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
The sample's equals filter leaves out the NULL key group, which a sampled test therefore never compares (sample_note
says so next to the detection bound, which counts the non-NULL keys only).
A complete sampled detail also carries the sample's detection bound (d95_keys, and d95_rate = d95_keys / total_keys): the
smallest number of mismatching keys of which a sample of n out of N keys holds at least one with probability 95% or
more, assuming the mismatching keys are independent of the fixed md5 order; fewer mismatching keys may all lie outside
the sample. Every requested key must occur in the together and every alone response: missing answers are not_run
(unless an observed mismatch or duplicate already fails the test), with no detection bound for an incomplete sample.

The baseline V3 run (run_test with verify.pair_key_budget) compares every key instead when there are no more keys than
the budget: the key count and every key, in the same md5 order, are read in one REPEATABLE READ snapshot and split
into consecutive chunks of PAIR_KEY_CAP, so the cost grows linearly (one sample's queries per chunk). Each chunk runs
the same filtered queries; then the NULL key group, which an equals filter leaves out and the unfiltered queries of at
most PAIR_KEY_CAP keys include, runs with a notSet filter. The counts add up over the chunks and the NULL key group
(whose own counts are null_key_group); the test passes only when all of them do and the together and every alone answer held every
key the chunks asked for (answered_keys = compared_keys): a key missing there is never compared by value, so without a
mismatch or a duplicate key the test is then not_run (keys_not_answered), never a pass. A query that cannot run in any
chunk makes it an error (chunks_compared: the chunks that finished). The detail says mode = chunked, chunks,
compared_keys, answered_keys, total_keys and elapsed_ms (from the key read to the last answer), never sampled_keys or a
detection bound. Each Cube query (the together query and every alone query of every chunk and of the NULL key group)
reads its own snapshot, shared with no other query and not with SQL (the key read; chunk_note). The chunked
alone_only_keys and duplicate_keys cover the key table's keys only (the alone queries are filtered to them) and
duplicates within one chunk. Every key up to the budget is read in one statement under source.statement_timeout_ms
and held in memory for the baseline run. Above the budget the test keeps the sample and its detail names the budget
(pair_key_budget). The V4 replays (run_test without a budget) compare the sample, the first chunk of the baseline's key
read, never every chunk. Details hold counts, flags and that duration only, never totals or keys.

The additive tests are bounded by the configuration: one per additive measure and cube with the class column that
Cube can join it to (in a generated model: the node cube of each measure with a node, and the ancestor and
descendant copies for `qty` and `pairs`), with as many groups as the column has values. The NULL group's SQL side
(null_group_sql) is read once per test, like a pair test's key sample, and reused by every replay; its detail holds
whether the two NULL groups are equal and the row counts of the SQL NULL group and of the source rows with no
dimension row (null_group_rows, orphan_rows; under a backward path the orphans are counted but are no NULL group).
"""

import math
import time
from decimal import Decimal, InvalidOperation

from . import canon, compare, config, cube_gen, db, ident, naming, sweep

PAIR_KEY_CAP = config.PAIR_KEY_CAP   # 2000; also the least verify.pair_key_budget
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


def pair_keys(conn, cfg, test: dict, budget: int | None = None,
              witness: "PairWitness | None" = None) -> tuple[int, list | None]:
    """(distinct non-NULL keys a pair test groups by, the keys to compare), read from test["keys_from"] (table, column)
    in one READ ONLY REPEATABLE READ transaction, so the count and the keys see the same rows. The keys are None when
    there are not more than PAIR_KEY_CAP, every key in the md5 order when there are not more than `budget` (the chunked
    baseline run), and otherwise a deterministic sample of PAIR_KEY_CAP of them: the first keys of that order. A
    `witness` (verify.existence_witness) reads its first witness of those keys in the same transaction; when one of
    its savepoint commands failed, a failure to end the transaction afterwards is the witness's, not the key read's."""
    table, column = test["keys_from"]
    col, source = f"t.{ident.quote(column)}", ident.qualified(table)
    keys = None
    try:
        with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
            total = int(db.scalar(conn, f"SELECT count(DISTINCT {col}) FROM {source} t") or 0)
            if total <= PAIR_KEY_CAP:
                return total, None
            n = total if budget is not None and total <= budget else PAIR_KEY_CAP
            rows = conn.run(f"SELECT s.k FROM (SELECT DISTINCT {col} AS k FROM {source} t WHERE {col} IS NOT NULL) s "
                            f"ORDER BY {sweep.md5_order('s.k')} LIMIT :n", seed=str(cfg.verify.seed), n=n)
            keys = [r[0] for r in rows or []]
            if witness is not None:
                witness.before(keys)
    except compare.expected_errors():
        if keys is None or witness is None or not witness.broken:
            raise
    return total, keys


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
    """The not_proven text of a sampled pair test's detail: how many keys it compared, its detection bound (over the
    non-NULL keys: the sample's equals filter leaves the NULL key group out, so it is never compared) and, when the
    detail names verify.pair_key_budget (the baseline run), that the key count exceeded it."""
    n, total = detail["sampled_keys"], detail["total_keys"]
    complete = detail.get("answered_keys", n) == n
    note = (f"compared {n} of {total} keys (a deterministic sample with verify.seed); the other keys were not compared"
            if complete else f"requested {n} of {total} keys, but only {detail.get('answered_keys', 0)} were answered "
            "by every Cube query; the sample is incomplete and no detection probability is claimed")
    d95 = detail.get("d95_keys")
    if not complete:
        d95 = None
    elif not d95:
        try:
            d95 = d95_keys(n, total)
        except ValueError:
            d95 = None
    if d95:
        note += (f"; a mismatch affecting at least {d95} keys ({100 * d95 / total:.3g}%) is detected with 95% "
                 "probability, assuming the mismatching keys are independent of the fixed md5 sample order; fewer "
                 "mismatching keys may be missed")
    note += ("; the NULL key group is not compared in a sample (its equals filter leaves it out), so a mismatch there "
             "is not detected")
    budget = detail.get("pair_key_budget")
    if budget is not None:
        note += (f"; the key count exceeds verify.pair_key_budget ({budget}), so the keys were not compared in chunks "
                 f"(a budget of at least {total} compares every key)")
    return note


def chunk_note(detail: dict) -> str:
    """The not_proven text of a chunked pair test's detail: how many keys were compared by value (answered_keys, the
    keys answered by the together and every alone query), and from which
    snapshots."""
    asked, answered, chunks = detail["compared_keys"], detail.get("answered_keys", 0), detail["chunks"]
    if answered >= asked:
        note = f"compared every key ({asked}) in {chunks} chunks of at most {PAIR_KEY_CAP} plus the NULL key group"
    else:
        note = (f"Cube's together and alone answers held {answered} of the {asked} keys asked for in {chunks} chunks of at most "
                f"{PAIR_KEY_CAP}, so {asked - answered} keys were not compared (the NULL key group was)")
    return (f"{note}; the key set comes from one REPEATABLE READ snapshot at the start, and each Cube query (the "
            "together query and every alone query of every chunk and of the NULL key group) reads its own snapshot, "
            "shared with no other query and not with SQL")


WITNESS_COUNTS = ("asked_keys", "incomplete_keys", "absent_no_facts", "absent_with_facts", "witness_value_mismatch",
                  "undecided", "together_absent", "not_witnessed", "witness_type", "witness_error", "witness_changed")
_SAVEPOINT = "sxc_witness"


def witness_id(test: dict) -> str:
    """The check id of a pair test's existence witness: v3w:pair_witness:<member>+<member>."""
    return "v3w:pair_witness:" + test["id"].removeprefix("pair:")


class PairWitness:
    """The existence witness of one sampled or chunked pair test (verify.existence_witness, limitation M5): for every
    asked key the together answer holds but an alone answer leaves out, whether the configuration has qualifying fact
    rows for it. An alone query reads the measure cube by its own key column (cube_gen.measure_cube: SELECT by, agg
    FROM table t WHERE where_sql GROUP BY by), so the alone answer has a row for a key exactly when a row with that key
    satisfies cube_gen.where_sql(m). The witness statement is that condition in SQL:

      SELECT DISTINCT t.<by> FROM <entity table> t WHERE <cube_gen.where_sql(m)> AND t.<by> IN (:k0, :k1, ...)

    bound to the same texts Cube's equals filter receives (_filter_text) and sent untyped, as Cube's PostgreSQL
    driver sends them, so PostgreSQL casts them to the key column's type the same way; the answer is canonicalised
    with the alone dimension's kind. A statement that fails is undecided (witness_type for a failed cast, SQLSTATE
    class 22, witness_error otherwise, a lost connection included); it runs under a savepoint, so the transaction goes
    on, and a failed savepoint command or second-read transaction leaves the keys it covers witness_error. The
    witness catches whatever its own work raises (compare.expected_errors), so it never reaches the pair test, and
    its reads are left out of the pair test's elapsed_ms. The closure cube's qty
    gets no statement: its alone query reads the closure table by the column the keys were read from, in the key-read
    snapshot every asked key has closure rows, so its key read is its witness and an absence there has facts.

    The first read (before) runs inside pair_keys's READ ONLY REPEATABLE READ transaction, over every asked key in
    slices of PAIR_KEY_CAP; the second (observe) runs after the sample's or the chunk's Cube queries in a new READ ONLY
    REPEATABLE READ transaction, for the absent keys only. A key is classified only when both reads agree
    (otherwise witness_changed, undecided); Cube's queries each read their own snapshot, which the two reads bracket
    but never share. Labels, per asked key and measure whose alone answer left the key out:

      absent_no_facts         no witness row and the together value is empty (NULL, the value Cube gives a key
                              without facts, since count is re-aggregated as a sum: cube_gen.measure_cube)
      witness_value_mismatch  no witness row, but the together value is not empty
      absent_with_facts       a witness row (or the closure qty): Cube left out a key the configuration has rows for
      undecided               no witness statement (not_witnessed), a failed statement, or a change between reads
    An asked key missing from the together answer is undecided once (together_absent): a fact-table witness cannot
    tell why the together query left it out. The NULL key group is outside the witness (no equals filter asks it).

    result() is the separate check: pass when every incompletely answered key is absent_no_facts, fail on any
    absent_with_facts or witness_value_mismatch, otherwise not_run (witness_incomplete) when a key is undecided. The
    detail holds counts only, never keys. The witness never changes the pair test's own status or detail."""

    def __init__(self, conn, cfg, test: dict):
        self.conn, self.cfg, self.test = conn, cfg, test
        self.kinds = test.get("kinds", {})
        self.specs = {m: self._spec(m) for m in test["measures"]}
        self.pre: dict[str, dict] = {m: {} for m in test["measures"]}
        self.counts = dict.fromkeys(WITNESS_COUNTS, 0)
        self.armed = False
        self.broken = False     # a savepoint command of the current transaction failed
        self.seconds = 0.0      # time spent in before and observe, left out of the pair test's elapsed_ms
        self.mode = "sampled"   # _run_chunks makes it chunked

    def _spec(self, member: str):
        """'closure' for a closure cube's measure, None when no configuration measure backs the member, otherwise
        (statement head, alone dimension kind)."""
        cube = member.split(".", 1)[0]
        if cube in {naming.cube_closure(h) for h in self.cfg.hierarchies}:
            return "closure"
        m = next((m for m in self.cfg.measures.values()
                  if naming.cube_measure(m.name) == cube and not m.date_column), None)
        if m is None or member != _m(cube, naming.MEASURE_VALUE):
            return None
        table = ident.qualified(self.cfg.entity(m.entity, f"measures.{m.name}").table)
        by = f"t.{ident.quote(m.by)}"
        head = f"SELECT DISTINCT {by} FROM {table} t WHERE {cube_gen.where_sql(m)} AND {by} IN "
        return head, self.kinds.get(self.test["alone"][member])

    def _read(self, member: str, keys: list) -> dict:
        """{canonical alone key: True (a qualifying row) | False | a failure label} for keys, in slices of
        PAIR_KEY_CAP, inside the caller's transaction; each statement under a savepoint. A failed statement labels
        its slice (witness_type for SQLSTATE class 22, witness_error otherwise); once a savepoint command fails the
        transaction cannot go on (broken), and the remaining slices are witness_error without a statement."""
        head, kind = self.specs[member]
        out = {}
        for i in range(0, len(keys), PAIR_KEY_CAP):
            part = keys[i:i + PAIR_KEY_CAP]
            canonical = [canon.canon(k, kind) for k in part]
            if self.broken:
                out.update(dict.fromkeys(canonical, "witness_error"))
                continue
            params = {f"k{j}": _filter_text(k) for j, k in enumerate(part)}
            sql = head + "(" + ", ".join(f":{name}" for name in params) + ")"
            if not self._savepoint("SAVEPOINT"):
                out.update(dict.fromkeys(canonical, "witness_error"))
                continue
            try:
                rows = self.conn.run(sql, **params)
            except compare.expected_errors() as e:
                cast = compare.error_detail(e).get("sqlstate", "").startswith("22")
                out.update(dict.fromkeys(canonical, "witness_type" if cast else "witness_error"))
                self._savepoint("ROLLBACK TO SAVEPOINT")
                continue
            found = {canon.canon(r[0], kind) for r in rows or []}
            out.update({k: k in found for k in canonical})
            self._savepoint("RELEASE SAVEPOINT")
        return out

    def _savepoint(self, command: str) -> bool:
        """Runs a savepoint command; a failure marks the transaction broken (no more witness statements in it)."""
        try:
            self.conn.run(f"{command} {_SAVEPOINT}")
        except compare.expected_errors():
            self.broken = True
            return False
        return True

    def before(self, keys: list):
        """The first read, inside pair_keys's REPEATABLE READ transaction: every asked key of every measure with a
        witness statement. Nothing it raises reaches pair_keys: a measure whose read did not finish has no first
        read for its keys (witness_error when they are absent)."""
        self.armed, self.broken = True, False
        started = time.monotonic()
        try:
            for m, spec in self.specs.items():
                if isinstance(spec, tuple):
                    self.pre[m] = self._read(m, keys)
        except compare.expected_errors():
            # only an asked key that cannot be canonicalised (an unknown kind) gets here; _compare canonicalises the
            # same keys with the same kinds before observe, so the pair test is an error either way
            pass
        finally:
            self.seconds += time.monotonic() - started

    def observe(self, asked: list, together: dict, alones: dict):
        """Classifies the asked keys of one sample or chunk once its Cube queries have answered (see the class). The
        time it takes is left out of the pair test's elapsed_ms."""
        started = time.monotonic()
        try:
            self._observe(asked, together, alones)
        finally:
            self.seconds += time.monotonic() - started

    def _observe(self, asked: list, together: dict, alones: dict):
        group_kind = self.kinds.get(self.test["group_by"])
        c = self.counts
        c["asked_keys"] += len(asked)
        absent: list[tuple] = []          # (measure, asked key, canonical alone key, together value)
        for k in asked:
            tk = canon.canon(k, group_kind)
            missing = [(m, canon.canon(k, self.kinds.get(self.test["alone"][m]))) for m in self.test["measures"]]
            missing = [(m, ak) for m, ak in missing if ak not in alones[m]]
            if tk not in together:
                c["incomplete_keys"] += 1
                c["together_absent"] += 1
                c["undecided"] += 1
                continue
            if missing:
                c["incomplete_keys"] += 1
            absent += [(m, k, ak, together[tk].get(m)) for m, ak in missing]
        reread = {m: [k for mm, k, ak, _v in absent if mm == m and self.pre[m].get(ak) in (True, False)]
                  for m, spec in self.specs.items() if isinstance(spec, tuple)}
        post = self._second_read({m: ks for m, ks in reread.items() if ks})
        for m, _k, ak, value in absent:
            spec = self.specs[m]
            if spec == "closure":
                c["absent_with_facts"] += 1
                continue
            if spec is None:
                label = "not_witnessed"
            else:
                pre = self.pre[m].get(ak, "witness_error")   # no entry: the first read did not finish
                after = post.get(m, {}).get(ak, "witness_error") if isinstance(pre, bool) else pre
                if isinstance(pre, str) or isinstance(after, str):
                    label = pre if isinstance(pre, str) else after
                elif pre != after:
                    label = "witness_changed"
                elif pre:
                    label = "absent_with_facts"
                else:
                    label = "absent_no_facts" if canon.canon(value) is None else "witness_value_mismatch"
            c[label] += 1
            if label not in ("absent_with_facts", "absent_no_facts", "witness_value_mismatch"):
                c["undecided"] += 1

    def _second_read(self, reread: dict) -> dict:
        """{measure: _read of its keys} in a new READ ONLY REPEATABLE READ transaction. A transaction that cannot
        begin, be set up or end leaves the measures it has not read out (their keys are witness_error); what it read
        before stays, since each statement answered from the transaction's one snapshot."""
        post: dict[str, dict] = {}
        if not reread:
            return post
        self.broken = False
        try:
            with db.read_only(self.conn, self.cfg.source.statement_timeout_ms, isolation="repeatable read"):
                for m, ks in reread.items():
                    post[m] = self._read(m, ks)
        except compare.expected_errors():
            pass
        return post

    def result(self, pair_status: str) -> tuple[str, dict] | None:
        """(status, detail) of the separate check, or None for a pair test that compared unfiltered (no key read
        asked for keys, so there is nothing to witness). A pair test that could not run leaves it not_run."""
        if pair_status == "error":
            return "not_run", {"reason": "pair_error"}
        if not self.armed:
            return None
        mode = self.mode
        detail = {"mode": mode, **self.counts}
        if detail["absent_with_facts"] or detail["witness_value_mismatch"]:
            return "fail", detail
        if detail["undecided"]:
            return "not_run", {"mode": mode, "reason": "witness_incomplete", **self.counts}
        return "pass", detail


def witness_note() -> str:
    """The not_proven text of a pair test's existence witness (PairWitness)."""
    return ("the existence witness reuses crosscheck.toml's where_sql (cube_gen.where_sql) for every measure, so it "
            "shows that Cube and the source SQL agree on which asked keys have qualifying rows, not that the data is "
            "complete or the filters are right; the NULL key group is outside it; its two reads (one in the key-read "
            "REPEATABLE READ snapshot, one after the Cube queries) bracket the per-query Cube snapshots, which are not "
            "shared with them, so a change undone between the two reads is not seen")


def run_test(conn, client, cfg, model: dict, test: dict, budget: int | None = None,
             witness: "PairWitness | None" = None) -> tuple[str, dict]:
    """(status, detail) of one test; errors are reported by type, never by message. `budget` is verify.pair_key_budget,
    which only the baseline V3 run passes: a pair test over more than PAIR_KEY_CAP keys and not more than the budget
    then compares every key in chunks. Without it (the V4 replays) such a test compares its sample. `witness` (a
    PairWitness of a pair test, which only the baseline V3 run passes with verify.existence_witness) observes the
    sampled or chunked comparison; it never changes the status or the detail."""
    run = {"total": _run_total, "additive": _run_additive, "pair": _run_pair}[test["kind"]]
    extra = {"budget": budget} if run is _run_pair else {}
    if witness is not None and run is _run_pair:
        extra["witness"] = witness
    try:
        return run(conn, client, cfg, test, **extra)
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
    """pair_keys of the test, read once (or left by the baseline run's key read): the test dict keeps them in memory
    for the mutant replays. They are key values, so they never go into a detail or a result."""
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


def _equals(keys: list | None):
    """The restriction of _compare to these keys (an equals filter; no filter for None)."""
    return lambda query, member: _restricted(query, member, keys)


def _null_keys(query: dict, member: str) -> dict:
    """The query limited to the NULL key group of `member`, which an equals filter leaves out."""
    return {**query, "filters": [{"member": member, "operator": "notSet"}]}


def _compare(client, test: dict, restrict, asked: list | None = None, witness: "PairWitness | None" = None) -> dict:
    """The together query and each alone query of a pair test, limited by restrict(query, member): the keys answered
    together, the mismatched keys and the alone-only keys per measure, and the duplicate keys of every answer. With
    `asked`, it also counts keys present in the together and every alone answer (answered_keys). Missing rows must
    not masquerade as NULL-valued measures. Expected keys use each queried dimension's canonical type. With `asked`
    and a `witness`, the witness classifies the asked keys missing from an answer once every query has answered."""
    measures, group, kinds = test["measures"], test["group_by"], test.get("kinds", {})
    together, dups = _keyed(client.load(restrict({"dimensions": [group], "measures": measures}, group)), group,
                            kinds.get(group))
    answered = ({i for i, k in enumerate(asked) if canon.canon(k, kinds.get(group)) in together}
                if asked is not None else None)
    mismatched, alone_only, alones = {}, {}, {}
    for m in measures:
        dim = test["alone"][m]
        alone, d = _keyed(client.load(restrict({"dimensions": [dim], "measures": [m]}, dim)), dim, kinds.get(dim))
        alones[m] = alone
        if answered is not None:
            answered.intersection_update(i for i, k in enumerate(asked) if canon.canon(k, kinds.get(dim)) in alone)
        dups += d
        key_of = ({canon.canon(k, kinds.get(group)): canon.canon(k, kinds.get(dim)) for k in asked}
                  if asked is not None else {})
        bad = sum(1 for k, row in together.items()
                  if (asked is None or key_of.get(k, k) in alone)
                  and not canon.same(row.get(m), alone.get(key_of.get(k, k), {}).get(m)))
        if bad:
            mismatched[m] = bad
        alone_only[m] = sum(1 for k in alone if k not in together)
    out = {"keys": len(together), "mismatched": mismatched, "duplicate_keys": dups, "alone_only_keys": alone_only}
    if asked is not None:
        out["answered_keys"] = len(answered)
        if witness is not None:
            witness.observe(asked, together, alones)
    return out


def _add(summed: dict, part: dict):
    """Adds the counts of one chunk (or of the NULL key group) to those of a chunked pair test."""
    summed["keys"] += part["keys"]
    summed["answered_keys"] += part.get("answered_keys", 0)
    summed["duplicate_keys"] += part["duplicate_keys"]
    for name in ("mismatched", "alone_only_keys"):
        for m, n in part[name].items():
            summed[name][m] = summed[name].get(m, 0) + n


def _run_pair(conn, client, cfg, test: dict, budget: int | None = None,
              witness: "PairWitness | None" = None) -> tuple[str, dict]:
    started = time.monotonic()
    if budget is not None and "keys_from" in test:
        # the baseline run reads the keys within the budget and leaves the first PAIR_KEY_CAP of them, the sample, to
        # the replays (_sample), so they replay what the baseline compared first and never read the keys again
        total, keys = pair_keys(conn, cfg, test, budget, **({"witness": witness} if witness is not None else {}))
        test["total_keys"], test["sample_keys"] = total, None if keys is None else keys[:PAIR_KEY_CAP]
        if keys is not None and total <= budget:
            return _run_chunks(client, test, total, keys, started, **({"witness": witness} if witness is not None else {}))
    else:
        witness = None   # no key read here to share its snapshot with the first witness read
    total, sample = _sample(conn, cfg, test)
    detail = _compare(client, test, _equals(sample), sample, **({"witness": witness} if witness is not None else {}))
    ok = not (detail["mismatched"] or detail["duplicate_keys"])
    if sample is not None:
        detail.update(sampled_keys=len(sample), total_keys=total)
        complete = detail["answered_keys"] == len(sample)
        d95 = d95_keys(len(sample), total) if sample and complete else None
        if d95:
            detail.update(d95_keys=d95, d95_rate=d95 / total)
        if budget is not None:
            detail["pair_key_budget"] = budget
        if not complete:
            return ("fail" if not ok else "not_run"), {**detail, "reason": "keys_not_answered"}
    return ("pass" if ok else "fail"), detail


def _run_chunks(client, test: dict, total: int, keys: list, started: float,
                witness: "PairWitness | None" = None) -> tuple[str, dict]:
    """Every key of a pair test (pair_keys within the budget) in consecutive chunks of PAIR_KEY_CAP, then the NULL key
    group. A key read that does not match the count, or a query that cannot run, makes the test an error that says how
    many chunks finished (chunks_compared; all of them when the NULL key group failed). A mismatch or a duplicate key
    fails it; otherwise, when the together answers held fewer keys than the chunks asked for (answered_keys below
    compared_keys), the missing keys were never compared and the test is not_run (keys_not_answered), never a pass."""
    chunks = [keys[i:i + PAIR_KEY_CAP] for i in range(0, len(keys), PAIR_KEY_CAP)]
    mode = {"mode": "chunked", "chunks": len(chunks)}
    if witness is not None:
        witness.mode = "chunked"
    summed = {"keys": 0, "answered_keys": 0, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {}}
    done = 0
    try:
        if len(keys) != total:
            raise ValueError("the key read does not match the key count")
        for chunk in chunks:
            _add(summed, _compare(client, test, _equals(chunk), chunk, **({"witness": witness} if witness is not None else {})))
            done += 1
        null = _compare(client, test, _null_keys)
    except compare.expected_errors() as e:
        return "error", {**compare.error_detail(e), **mode, "chunks_compared": done, "total_keys": total}
    _add(summed, null)
    witnessed = witness.seconds if witness is not None else 0.0   # the witness reads are not the pair test's time
    detail = {**summed, **mode, "compared_keys": len(keys), "total_keys": total, "null_key_group": null,
              "elapsed_ms": int((time.monotonic() - started - witnessed) * 1000)}
    if summed["mismatched"] or summed["duplicate_keys"]:
        return "fail", detail
    if summed["answered_keys"] < len(keys):
        return "not_run", {**detail, "reason": "keys_not_answered"}
    return "pass", detail
