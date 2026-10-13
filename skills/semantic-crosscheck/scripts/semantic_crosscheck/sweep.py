"""Parameter bindings of a gold query for V1/V2 (design section 7).

  no parameters   one binding in stratum `all`
  param_sets      stratum `param_sets`, one binding per configured set
  values          stratum `values`, one binding per configured value
  values_of       stratum `values`, every distinct non-null value of entity.column (in value order)
  strata          hierarchy strata: k = ceil(sweep_k / number of strata) node ids per stratum, in the
                  deterministic order md5(id || seed), id ('deep': deepest subtree first)

Node ids are the node-entity keys plus the edge endpoints; edges with a NULL endpoint are ignored, as in the
closure. The subtree depth that orders `deep` comes from the closure table when it exists, otherwise from a
bottom-up recursion over the edges bounded by max_depth (so a cycle cannot make it run forever). The md5 text
is ordered with the "C" collation, so the same configuration samples the same ids on every database.

All strata of a gold query are sampled by one statement in a READ ONLY transaction. An empty stratum comes
back as (stratum, None): the caller reports it as not proven instead of failing it.
"""

import math

from . import db, ident, naming

PREDICATES = {
    "top": "f.children > 0 AND f.parents = 0",
    "mid": "f.children > 0 AND f.parents > 0",
    "leaf": "f.parents > 0 AND f.children = 0",
    "leaf_parent": "f.children > 0 AND f.inner_children = 0",
    "deep": "f.children > 0",
    "shared": "f.parents >= 2",
    "empty": "f.in_node_table AND f.children = 0",
}
_ORDER = 'md5(f.id::text || CAST(:seed AS text)) COLLATE "C", f.id'


def _unique(names: list) -> list:
    return list(dict.fromkeys(names))


def strata_names(gold) -> list[str]:
    """Stratum names of a gold query, without the database (for checks that cannot run)."""
    if gold.param_sets:
        return ["param_sets"]
    if not gold.params:
        return ["all"]
    p = next(iter(gold.params.values()))
    return _unique(p.strata) if p.strata else ["values"]


def per_stratum(cfg, gold) -> int:
    """k: node ids sampled per stratum."""
    p = next(iter(gold.params.values()))
    return math.ceil(cfg.verify.sweep_k / len(_unique(p.strata)))


def bindings(conn, cfg, gold) -> list[tuple[str, dict | None]]:
    """[(stratum, params)] in the deterministic order; (stratum, None) for an empty stratum."""
    if gold.param_sets:
        return [("param_sets", dict(ps)) for ps in gold.param_sets]
    if not gold.params:
        return [("all", {})]
    name, p = next(iter(gold.params.items()))
    if p.values:
        return [("values", {name: v}) for v in p.values]
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        if p.values_of:
            found = [("values", {name: v}) for v in _values_of(conn, cfg, gold, p)]
            return found or [("values", None)]
        return _strata(conn, cfg, gold, p)


def _values_of(conn, cfg, gold, p) -> list:
    ent_name, col = p.values_of.split(".")
    ent = cfg.entity(ent_name, f"gold {gold.id} params.{p.name}")
    c = ident.quote(col)
    rows = conn.run(f"SELECT DISTINCT {c} FROM {ident.qualified(ent.table)} WHERE {c} IS NOT NULL ORDER BY 1")
    return [r[0] for r in rows or []]


def _strata(conn, cfg, gold, p) -> list:
    h = cfg.hierarchy(p.hierarchy, f"gold {gold.id} params.{p.name}")
    strata = _unique(p.strata)
    has_closure = bool(db.scalar(conn, "SELECT to_regclass(:t) IS NOT NULL", t=naming.closure_table(cfg, h.name)))
    rows = conn.run(strata_sql(cfg, h, strata, has_closure), seed=str(cfg.verify.seed), k=per_stratum(cfg, gold),
                    max_depth=h.max_depth)
    picked: dict[str, list] = {s: [] for s in strata}
    for pos, node_id in rows or []:
        picked[strata[pos]].append(node_id)
    out = []
    for s in strata:
        out.extend([(s, {p.name: v}) for v in picked[s]] or [(s, None)])
    return out


def strata_sql(cfg, h, strata: list[str], has_closure: bool) -> str:
    """One statement returning (position of the stratum, node id), at most :k ids per stratum, in sample order."""
    ref = f"hierarchies.{h.name}"
    edge, node = cfg.entity(h.edge, ref), cfg.entity(h.node, ref)
    p, c, key = ident.quote(h.parent), ident.quote(h.child), ident.quote(node.key[0])
    ctes = [
        f"e AS (SELECT DISTINCT {p} AS parent, {c} AS child FROM {ident.qualified(edge.table)}"
        f" WHERE {p} IS NOT NULL AND {c} IS NOT NULL)",
        f"nt AS (SELECT DISTINCT {key} AS id FROM {ident.qualified(node.table)} WHERE {key} IS NOT NULL)",
        "ch AS (SELECT parent AS id, count(*) AS n FROM e GROUP BY parent)",
        "pa AS (SELECT child AS id, count(*) AS n FROM e GROUP BY child)",
        "ic AS (SELECT e.parent AS id, count(*) AS n FROM e JOIN ch ON ch.id = e.child GROUP BY e.parent)",
        "ids AS (SELECT id FROM nt UNION SELECT id FROM ch UNION SELECT id FROM pa)",
        "f AS (SELECT ids.id, coalesce(ch.n, 0) AS children, coalesce(pa.n, 0) AS parents,"
        " coalesce(ic.n, 0) AS inner_children, nt.id IS NOT NULL AS in_node_table"
        " FROM ids LEFT JOIN ch ON ch.id = ids.id LEFT JOIN pa ON pa.id = ids.id"
        " LEFT JOIN ic ON ic.id = ids.id LEFT JOIN nt ON nt.id = ids.id)",
    ]
    recursive = "deep" in strata and not has_closure
    if "deep" in strata and has_closure:
        ctes.append(f"d AS (SELECT ancestor AS id, max(max_depth) AS height FROM {naming.closure_table(cfg, h.name)}"
                    " GROUP BY ancestor)")
    elif recursive:
        ctes.append("up(id, height) AS (SELECT id, 0 FROM f WHERE parents > 0 AND children = 0"
                    " UNION SELECT e.parent, up.height + 1 FROM up JOIN e ON e.child = up.id"
                    " WHERE up.height < :max_depth)")
        ctes.append("d AS (SELECT id, max(height) AS height FROM up GROUP BY id)")
    parts = []
    for pos, s in enumerate(strata):
        if s == "deep":
            parts.append(f"SELECT {pos} AS pos, f.id, row_number() OVER (ORDER BY d.height DESC NULLS LAST, {_ORDER}) AS rn"
                         f" FROM f LEFT JOIN d ON d.id = f.id WHERE {PREDICATES[s]}")
        else:
            parts.append(f"SELECT {pos} AS pos, f.id, row_number() OVER (ORDER BY {_ORDER}) AS rn FROM f WHERE {PREDICATES[s]}")
    return (("WITH RECURSIVE " if recursive else "WITH ") + ",\n".join(ctes)
            + "\nSELECT pos, id FROM (\n" + "\nUNION ALL\n".join(parts) + "\n) s WHERE rn <= :k ORDER BY pos, rn")
