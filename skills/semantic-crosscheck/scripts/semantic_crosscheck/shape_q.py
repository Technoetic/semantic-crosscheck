"""V3 query shapes derived from the generated model (design section 7).

  total     each measure cube's `value` total equals SQL computed from the configuration (cube_gen.where_sql and
            the measure's aggregate); the closure cube's `qty` and `pairs` equal the closure table.
  additive  for sum/count measures joined one_to_one to their node cube, the groups by the ontology class column
            add up to the measure's total: measure keys missing from the node table, or a join that fans out,
            break the sum.
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
"""

import math
from decimal import Decimal, InvalidOperation

from . import canon, compare, cube_gen, db, ident, naming, sweep

PAIR_KEY_CAP = 2000
MISS = 20          # the detection bound allows a miss probability of at most 1/MISS (5%)


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


def _additive(cfg, cubes: dict) -> list[dict]:
    col = cfg.ontology.class_column
    if not col:
        return []
    out = []
    for m in cfg.measures.values():
        mc = naming.cube_measure(m.name)
        if m.agg not in ("sum", "count") or m.date_column or not m.node or mc not in cubes:
            continue
        nc = naming.cube_entity(m.node)
        join = _joins(cubes.get(nc)).get(mc)
        dims = {d["name"] for d in cubes.get(nc, {}).get("dimensions", [])}
        if not join or join.get("relationship") != "one_to_one" or col not in dims:
            continue
        measure, dim = _m(mc, naming.MEASURE_VALUE), _m(nc, col)
        out.append({"id": f"additive:{measure}:{dim}", "kind": "additive", "members": [measure, dim],
                    "measure": measure, "dimension": dim, "config_ref": f"measures.{m.name}"})
    return out


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


def _run_additive(conn, client, cfg, test: dict) -> tuple[str, dict]:
    measure, dim = test["measure"], test["dimension"]
    total = _single(client.load({"measures": [measure]}), measure)
    groups = client.load({"dimensions": [dim], "measures": [measure]})
    equal = canon.same(_sum([g.get(measure) for g in groups]), total)
    detail = {"equal": equal, "groups": len(groups), "null_class_groups": sum(1 for g in groups if g.get(dim) is None)}
    return ("pass" if equal else "fail"), detail


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
