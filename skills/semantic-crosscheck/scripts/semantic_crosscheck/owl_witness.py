"""Small synthetic checks of configured OWL/SQL defined-class translations.

These are separate from V5 on source rows. Every member set and closure below is hand-specified, independently of
owl.definition and owl.defined_sql. A control is an individual whose membership is not entailed by this graph; it is
never a negative OWL assertion (the open-world assumption still applies). The original configuration goes to HermiT,
including disjointness and no_children. SQL changes only relation bindings to parameterized VALUES CTEs in the same
database, in a READ ONLY snapshot. Neither graphs nor SQL values enter a result.
Synthetic keys/classes are text: these checks cover logical membership translation, not source-column types.
"""

import copy
from dataclasses import dataclass, field

from . import canon, db, ident, naming, owl, shapes


@dataclass
class Witness:
    graph: owl.Graph | None = None
    expected: set = field(default_factory=set)
    closure: tuple = ()
    reason: str = ""


def plan(cfg, defined) -> Witness:
    """A bounded positive/control graph, or an explicit reason a positive cannot be supplied without changing axioms."""
    o = cfg.ontology
    h = cfg.hierarchies[o.hierarchy]
    node = cfg.entities[h.node]
    if (defined.kind in owl.CLASS_KINDS and (not o.class_column or defined.cls not in o.classes)
            or o.class_column == node.key[0] or h.parent == h.child):
        return Witness(reason="configuration_gap")
    if defined.kind == "class_with_child" and defined.cls in o.no_children:
        return Witness(reason="configured_class_unsatisfiable")
    if defined.kind in ("has_child", "has_parent"):
        graph = owl.Graph({"root": set(), "middle": set(), "leaf": set(), "isolated": set()},
                          [("root", "middle"), ("middle", "leaf")])
        expected = {"root", "middle"} if defined.kind == "has_child" else {"middle", "leaf"}
        return Witness(graph, expected, (("root", "middle"), ("middle", "leaf"), ("root", "leaf")))
    other = next((value for value in o.classes if value != defined.cls), None)
    if defined.kind == "contains_class":
        graph = owl.Graph({"root": set(), "middle": set(), "leaf": {defined.cls}, "isolated": {defined.cls},
                           "plain_parent": set(), "plain_child": {other} if other is not None else set()},
                          [("root", "middle"), ("middle", "leaf"), ("plain_parent", "plain_child")])
        return Witness(graph, {"root", "middle"}, (("root", "middle"), ("middle", "leaf"),
                                                   ("root", "leaf"), ("plain_parent", "plain_child")))
    if defined.kind == "class_with_child":
        graph = owl.Graph({"typed_parent": {defined.cls}, "typed_child": {defined.cls}, "plain_parent": set(),
                           "plain_child": {other} if other is not None else set(), "isolated": set()},
                          [("typed_parent", "plain_child"), ("plain_parent", "typed_child")])
        return Witness(graph, {"typed_parent"}, (("typed_parent", "plain_child"), ("plain_parent", "typed_child")))
    if defined.kind == "class_used_as_part":
        graph = owl.Graph({"parent": set(), "typed_child": {defined.cls}, "plain_parent": set(),
                           "plain_child": set(), "isolated": {defined.cls},
                           "other_class": {other} if other is not None else set()},
                          [("parent", "typed_child"), ("plain_parent", "plain_child")])
        return Witness(graph, {"typed_child"}, (("parent", "typed_child"), ("plain_parent", "plain_child")))
    return Witness(reason="configuration_gap")


def _values(name: str, columns: list, rows: list, params: dict) -> str:
    values = []
    for i, row in enumerate(rows):
        bound = []
        for j, value in enumerate(row):
            parameter = f"{name}_{i}_{j}"
            params[parameter] = value
            bound.append(f"CAST(:{parameter} AS text)")
        values.append("(" + ", ".join(bound) + ")")
    return f'{ident.quote(name)} ({", ".join(ident.quote(c) for c in columns)}) AS (VALUES {", ".join(values)})'


def _sql(cfg, defined, case: Witness) -> tuple[str, dict]:
    """Use the production SQL definition, replacing only its three relation bindings with synthetic CTEs."""
    bound = copy.deepcopy(cfg)
    h = bound.hierarchies[bound.ontology.hierarchy]
    node, edge = copy.deepcopy(bound.entities[h.node]), copy.deepcopy(bound.entities[h.edge])
    # Separate internal entries also support a configuration that uses one entity for nodes and edges.
    h.node, h.edge = "sxc_witness_node", "sxc_witness_edge"
    bound.entities[h.node], bound.entities[h.edge] = node, edge
    node.table, edge.table, bound.source.output_schema = "sxc_witness.nodes", "sxc_witness.edges", "sxc_witness"
    sql, params = owl.defined_sql(bound, defined)
    relations = {node.table: "sxc_witness_nodes", edge.table: "sxc_witness_edges",
                 naming.closure_table(bound, h.name): "sxc_witness_closure"}
    for relation, cte in relations.items():
        sql = sql.replace(ident.qualified(relation), ident.quote(cte))
    columns = [node.key[0]] + ([bound.ontology.class_column] if bound.ontology.class_column else [])
    nodes = [(key, next(iter(values), None)) if bound.ontology.class_column else (key,)
             for key, values in case.graph.nodes.items()]
    ctes = [_values(relations[node.table], columns, nodes, params),
            _values(relations[edge.table], [h.parent, h.child], case.graph.edges, params),
            _values(relations[naming.closure_table(bound, h.name)], ["ancestor", "descendant"], case.closure, params)]
    return "WITH " + ",\n".join(ctes) + "\n" + sql, params


def run(conn, cfg, java: str, res) -> None:
    """Append distinct witness checks; source-data V5 checks and their vacuity disclaimers are never changed."""
    cases = []
    for index, defined in enumerate(cfg.ontology.defined):
        cid, ref = f"v5:witness:{defined.name}", f"ontology.defined[{index}]"
        case = plan(cfg, defined)
        if case.reason:
            res.add(cid, "not_run", ref, reason=case.reason, kind=defined.kind, synthetic=True)
            res.not_proven.append(f"{cid}: no positive witness was run ({case.reason}); configured axioms were retained")
        elif len(case.graph.nodes) > cfg.verify.owl_max_individuals:
            res.add(cid, "not_run", ref, reason="resource_limit", kind=defined.kind, synthetic=True,
                    individuals=len(case.graph.nodes), limit=cfg.verify.owl_max_individuals)
            res.not_proven.append(f"{cid}: the synthetic graph exceeds verify.owl_max_individuals")
        else:
            cases.append((cid, ref, defined, case))
    if not cfg.ontology.defined:
        res.add("v5:witness:scope", "not_run", "ontology", reason="no_defined_classes", synthetic=True)
        res.not_proven.append("v5:witness:scope: no configured defined classes; no synthetic membership was compared")
    if not cases:
        return
    sql_members = {}
    try:
        with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
            for cid, _ref, defined, case in cases:
                sql, params = _sql(cfg, defined, case)
                sql_members[cid] = {canon.key(row[0]) for row in conn.run(sql, **params) or []}
    except Exception as e:  # noqa: BLE001 - SQLSTATE only; SQL text and parameters must not reach results
        code = shapes.sqlstate(e)
        if not code:
            raise
        for cid, ref, defined, _case in cases:
            res.add(cid, "error", ref, reason="database_error", sqlstate=code, kind=defined.kind, synthetic=True)
            res.not_proven.append(f"{cid}: membership was not compared (database_error)")
        return
    for cid, ref, defined, case in cases:
        try:
            answer = owl.reason(cfg, case.graph, java)
        except owl.ReasonerError as e:
            res.add(cid, "error", ref, reason="reasoner_error", exception=type(e).__name__, kind=defined.kind,
                    synthetic=True)
            res.not_proven.append(f"{cid}: membership was not compared (reasoner_error)")
            continue
        if not answer.consistent:
            res.add(cid, "not_run", ref, reason="synthetic_inconsistent", kind=defined.kind, synthetic=True)
            res.not_proven.append(f"{cid}: the synthetic ontology is inconsistent; membership was not compared")
            continue
        found, selected, expected = answer.members[defined.name], sql_members[cid], case.expected
        res.add(cid, "pass" if found == selected == expected else "fail", ref, kind=defined.kind, synthetic=True,
                individuals=answer.individuals, expected_members=len(expected),
                nonmember_controls=len(case.graph.nodes) - len(expected), owl_members=len(found), sql_members=len(selected),
                owl_missing=len(expected - found), owl_unexpected=len(found - expected),
                sql_missing=len(expected - selected), sql_unexpected=len(selected - expected),
                owl_only=len(found - selected), sql_only=len(selected - found))
    res.not_proven.append("v5:witness: synthetic checks cover only the bounded graphs, not source-data membership; "
                          "nonmember controls mean absence of entailment, not negative OWL facts")
