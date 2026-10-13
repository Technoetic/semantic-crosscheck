"""OWL ontology of one hierarchy, reasoned with HermiT, and the SQL definitions it is compared with (V5).

Why: the closure table and the membership questions an analyst asks ("which items contain a raw material?") are
answered by SQL; an OWL ontology answers the same questions from axioms. Building the ontology from the same rows and
checking HermiT's answers against SQL catches a closure or a definition that is wrong in one of the layers, and
HermiT's consistency check catches rows that contradict the model (an item of a no_children class with a child).

The ontology (design section 7), in a fresh in-memory owlready2 World per run:
  - a class named after the node entity (its IRI is naming.rdf_entity_class of the node entity) and one subclass per
    ontology.classes value, pairwise disjoint
  - the edge property naming.owl_edge_property(h), domain and range the node class, a sub-property of the transitive
    naming.owl_closure_property(h)
  - one individual per node key and per edge endpoint, with generated names in a namespace of their own (row values
    never become IRIs); endpoints missing from the node table are plain Things
  - `C SubClassOf not (<h> some Thing)` for every ontology.no_children value
  - every ontology.defined class equivalent to the expression of its kind (definition())
HermiT runs through owlready2's sync_reasoner_hermit(world, infer_property_values=True) while tempfile.tempdir points
at a private directory that is removed afterwards (owlready2 leaves its input file behind when the ontology is
inconsistent). The Pellet reasoner bundled with owlready2 is never called.

Answers: the OWL closure is the inferred <h>_plus values united with the asserted <h> edges (the inferred values omit
pairs already asserted through the sub-property). Defined-class members are compared on node-table individuals only,
like their SQL definitions (defined_sql): the range axiom makes an endpoint missing from the node table a node too, so
such members are counted apart instead of compared. Keys are compared in canonical form (canon).
owlready2 is imported lazily: only the reason stage needs it.
"""

import contextlib
import os
import shutil
import tempfile
import time
import types
from dataclasses import dataclass, field

from . import canon, db, ident, naming, procutil
from .result import EXIT_CONFIG, StageError

CLASS_KINDS = ("contains_class", "class_with_child", "class_used_as_part")
EDGE_ATTR, CLOSURE_ATTR = "sxc_edge", "sxc_closure"   # Python attribute names of the two properties, never IRIs
INDIVIDUALS = "node/"                                # individuals live under <iri>node/, apart from classes
SNAPSHOT_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"


class TooManyIndividuals(Exception):
    """The node keys and edge endpoints exceed verify.owl_max_individuals: no ontology is built."""

    def __init__(self, individuals: int, limit: int):
        super().__init__(f"{individuals} individuals exceed verify.owl_max_individuals = {limit}")
        self.individuals, self.limit = individuals, limit


class ReasonerError(Exception):
    """HermiT gave no answer (Java missing or failing). The message is the exception type only: Java's output is not
    kept."""


@dataclass
class Graph:
    """The rows an ontology is built from."""
    nodes: dict          # node key (raw value) -> set of class column values (as text)
    edges: list          # distinct (parent, child) pairs, both non-NULL

    def outside(self) -> list:
        """Edge endpoints that are not node keys, in first-seen order."""
        found = {}
        for pair in self.edges:
            for k in pair:
                if k not in self.nodes:
                    found.setdefault(k, None)
        return list(found)


@dataclass
class Reasoned:
    """HermiT's answers; closure and members are filled only when the ontology is consistent."""
    consistent: bool
    individuals: int
    node_rows: int                 # individuals of node-table keys
    outside_node_table: int        # individuals of edge endpoints missing from the node table
    edges: int
    hermit_ms: int
    closure: set = field(default_factory=set)       # canonical (ancestor, descendant) pairs
    members: dict = field(default_factory=dict)     # defined class name -> canonical node keys
    outside: dict = field(default_factory=dict)     # defined class name -> members outside the node table


@dataclass
class _Model:
    world: object
    closure: object                # the transitive super-property
    defined: dict                  # defined class name -> owlready2 class
    keys: dict                     # individual IRI -> raw key
    node_iris: set                 # IRIs of the node-table individuals


# ---- configuration --------------------------------------------------------------------------------------
def java_executable(env=None, which=procutil.which) -> str | None:
    """The java executable for HermiT: SXC_JAVA (an absolute file, or a command name looked up on PATH) when it is
    set, else java on PATH. Lookups never search the current directory (procutil.which)."""
    env = os.environ if env is None else env
    configured = env.get("SXC_JAVA")
    if configured:
        return configured if os.path.isabs(configured) and os.path.isfile(configured) else which(configured)
    return which("java")


def check_config(cfg) -> None:
    """Configuration errors config.load cannot see (exit 2): one OWL name used twice, classes without a class column."""
    o = cfg.ontology
    h = cfg.hierarchy(o.hierarchy, "ontology.hierarchy")
    names = [h.node, naming.owl_edge_property(h.name), naming.owl_closure_property(h.name), *o.classes.values(),
             *(d.name for d in o.defined)]
    for name in names:
        if names.count(name) > 1:
            raise StageError(f"crosscheck.toml [ontology]: the OWL name {name!r} is used twice (the node entity, the "
                             "hierarchy properties, ontology.classes and ontology.defined share one namespace)", EXIT_CONFIG)
    if o.classes and not o.class_column:
        raise StageError("crosscheck.toml [ontology.class_column]: ontology.classes needs the node-table column that "
                         "holds the class value", EXIT_CONFIG)


# ---- SQL --------------------------------------------------------------------------------------------------
def _hierarchy(cfg):
    h = cfg.hierarchies[cfg.ontology.hierarchy]
    return h, cfg.entities[h.node], cfg.entities[h.edge]


def _edges_where(h) -> str:
    return f"e.{ident.quote(h.parent)} IS NOT NULL AND e.{ident.quote(h.child)} IS NOT NULL"


def individuals_sql(cfg) -> str:
    """Count of the individuals: distinct node keys and edge endpoints."""
    h, node, edge = _hierarchy(cfg)
    k = ident.quote(node.key[0])
    edges = f"FROM {ident.qualified(edge.table)} e WHERE {_edges_where(h)}"
    return (f"SELECT count(*) FROM (SELECT n.{k} FROM {ident.qualified(node.table)} n WHERE n.{k} IS NOT NULL\n"
            f"  UNION SELECT e.{ident.quote(h.parent)} {edges}\n"
            f"  UNION SELECT e.{ident.quote(h.child)} {edges}) ids")


def nodes_sql(cfg) -> str:
    """Node keys with the class value as text (the SQL definitions compare the same text)."""
    _h, node, _edge = _hierarchy(cfg)
    k = ident.quote(node.key[0])
    cc = cfg.ontology.class_column
    cls = f"CAST(n.{ident.quote(cc)} AS text)" if cc else "CAST(NULL AS text)"
    return f"SELECT n.{k}, {cls} FROM {ident.qualified(node.table)} n WHERE n.{k} IS NOT NULL ORDER BY 1"


def edges_sql(cfg) -> str:
    h, _node, edge = _hierarchy(cfg)
    p, c = ident.quote(h.parent), ident.quote(h.child)
    return f"SELECT DISTINCT e.{p}, e.{c} FROM {ident.qualified(edge.table)} e WHERE {_edges_where(h)} ORDER BY 1, 2"


def closure_pairs_sql(cfg) -> str:
    h, _node, _edge = _hierarchy(cfg)
    return f"SELECT ancestor, descendant FROM {ident.qualified(naming.closure_table(cfg, h.name))}"


def defined_sql(cfg, defined) -> tuple[str, dict]:
    """(SELECT, parameters) of the SQL definition of a defined class (design section 7, node-table rows only):
      has_child           parents of at least one edge
      has_parent          children of at least one edge
      contains_class      ancestors in the closure table of a node of class C
      class_with_child    parents of class C
      class_used_as_part  children of class C
    Edges with a NULL end are no edges (as in the closure); the class value is compared as text, bound as :cls."""
    h, node, edge = _hierarchy(cfg)
    k, p, c = ident.quote(node.key[0]), ident.quote(h.parent), ident.quote(h.child)
    nodes, edges = ident.qualified(node.table), ident.qualified(edge.table)
    has_child = f"EXISTS (SELECT 1 FROM {edges} e WHERE e.{p} = n.{k} AND e.{c} IS NOT NULL)"
    has_parent = f"EXISTS (SELECT 1 FROM {edges} e WHERE e.{c} = n.{k} AND e.{p} IS NOT NULL)"
    params = {"cls": defined.cls} if defined.kind in CLASS_KINDS else {}

    def of_class(alias: str) -> str:
        return f"CAST({alias}.{ident.quote(cfg.ontology.class_column)} AS text) = :cls"

    if defined.kind == "has_child":
        where = has_child
    elif defined.kind == "has_parent":
        where = has_parent
    elif defined.kind == "contains_class":
        closure = ident.qualified(naming.closure_table(cfg, h.name))
        where = (f"EXISTS (SELECT 1 FROM {closure} x JOIN {nodes} m ON m.{k} = x.descendant "
                 f"WHERE x.ancestor = n.{k} AND {of_class('m')})")
    elif defined.kind == "class_with_child":
        where = f"{of_class('n')} AND {has_child}"
    elif defined.kind == "class_used_as_part":
        where = f"{of_class('n')} AND {has_parent}"
    else:
        raise ValueError(f"unknown defined kind {defined.kind!r}")
    return f"SELECT DISTINCT n.{k} FROM {nodes} n WHERE n.{k} IS NOT NULL AND {where}", params


def sql_defined_members(conn, cfg, defined) -> set:
    """Canonical node keys satisfying the SQL definition of a defined class (in the caller's transaction)."""
    sql, params = defined_sql(cfg, defined)
    return {canon.key(r[0]) for r in conn.run(sql, **params) or []}


def sql_closure_pairs(conn, cfg) -> set:
    """Canonical (ancestor, descendant) pairs of the closure table built by build."""
    return {(canon.key(a), canon.key(d)) for a, d in conn.run(closure_pairs_sql(cfg)) or []}


def closure_table_exists(conn, cfg) -> bool:
    h, _node, _edge = _hierarchy(cfg)
    return bool(db.scalar(conn, "SELECT to_regclass(:t) IS NOT NULL", t=naming.closure_table(cfg, h.name)))


def load_graph(conn, cfg) -> Graph:
    """Node keys with their class values and the distinct edges, in the caller's transaction. The individuals are
    counted first: TooManyIndividuals above verify.owl_max_individuals, before any row is read."""
    n = int(db.scalar(conn, individuals_sql(cfg)))
    if n > cfg.verify.owl_max_individuals:
        raise TooManyIndividuals(n, cfg.verify.owl_max_individuals)
    nodes: dict = {}
    for key, value in conn.run(nodes_sql(cfg)) or []:
        values = nodes.setdefault(key, set())
        if value is not None:
            values.add(value)
    return Graph(nodes, [(p, c) for p, c in conn.run(edges_sql(cfg)) or []])


def read(conn, cfg, closure_ready: bool) -> tuple[Graph, set | None, dict]:
    """Everything V5 compares, from one READ ONLY snapshot (REPEATABLE READ): the graph (TooManyIndividuals first),
    the closure table's pairs when closure_ready, and the SQL members of every defined class that can be evaluated
    (contains_class reads the closure table, so it also needs closure_ready)."""
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        conn.run(SNAPSHOT_SQL)
        graph = load_graph(conn, cfg)
        pairs = sql_closure_pairs(conn, cfg) if closure_ready else None
        members = {d.name: sql_defined_members(conn, cfg, d) for d in cfg.ontology.defined
                   if closure_ready or d.kind != "contains_class"}
    return graph, pairs, members


# ---- ontology ----------------------------------------------------------------------------------------------
def definition(kind: str, node_cls, cls, edge, closure):
    """The class expression a defined class is equivalent to (design section 7)."""
    from owlready2 import Inverse, Thing
    if kind == "has_child":
        return node_cls & edge.some(Thing)
    if kind == "has_parent":
        return node_cls & Inverse(edge).some(Thing)
    if kind == "contains_class":
        return node_cls & closure.some(cls)
    if kind == "class_with_child":
        return cls & edge.some(Thing)
    if kind == "class_used_as_part":
        return cls & Inverse(edge).some(Thing)
    raise ValueError(f"unknown defined kind {kind!r}")


def build(cfg, graph: Graph) -> _Model:
    """The ontology of the module docstring in a fresh owlready2 World."""
    import owlready2
    from owlready2 import AllDisjoint, Not, ObjectProperty, Thing, TransitiveProperty
    o = cfg.ontology
    h = cfg.hierarchies[o.hierarchy]
    base = o.iri or naming.DEFAULT_IRI
    world = owlready2.World()
    onto = world.get_ontology(base)
    individuals = onto.get_namespace(base + INDIVIDUALS)
    with onto:
        node_cls = types.new_class(h.node, (Thing,))
        classes = {value: types.new_class(name, (node_cls,)) for value, name in o.classes.items()}
        if len(classes) > 1:
            AllDisjoint(list(classes.values()))
        closure = types.new_class(naming.owl_closure_property(h.name), (ObjectProperty, TransitiveProperty),
                                  exec_body=lambda body: body.update(python_name=CLOSURE_ATTR))
        edge = types.new_class(naming.owl_edge_property(h.name), (closure,),
                               exec_body=lambda body: body.update(python_name=EDGE_ATTR))
        edge.domain = [node_cls]
        edge.range = [node_cls]
        for value in o.no_children:
            classes[value].is_a.append(Not(edge.some(Thing)))
        defined = {}
        for d in o.defined:
            defined[d.name] = types.new_class(d.name, (Thing,))
            defined[d.name].equivalent_to.append(definition(d.kind, node_cls, classes.get(d.cls), edge, closure))
        keys, by_key = {}, {}
        for key, values in graph.nodes.items():
            asserted = [classes[v] for v in sorted(values) if v in classes] or [node_cls]
            ind = asserted[0](f"n{len(keys)}", namespace=individuals)
            ind.is_a.extend(asserted[1:])
            keys[ind.iri], by_key[key] = key, ind
        node_iris = set(keys)
        for key in graph.outside():
            ind = Thing(f"n{len(keys)}", namespace=individuals)
            keys[ind.iri], by_key[key] = key, ind
        for parent, child in graph.edges:
            getattr(by_key[parent], EDGE_ATTR).append(by_key[child])
    return _Model(world, closure, defined, keys, node_iris)


def hermit(world, java: str) -> tuple[bool, int]:
    """(consistent, milliseconds) of one HermiT run in a private temporary directory, removed afterwards."""
    import owlready2
    previous = owlready2.JAVA_EXE, tempfile.tempdir
    private = tempfile.mkdtemp(prefix="sxc_hermit_")
    owlready2.JAVA_EXE, tempfile.tempdir = java, private
    started = time.monotonic()
    try:
        owlready2.sync_reasoner_hermit(world, infer_property_values=True, debug=0)
        consistent = True
    except owlready2.OwlReadyInconsistentOntologyError:
        consistent = False
    except (owlready2.OwlReadyJavaError, OSError) as e:
        raise ReasonerError(type(e).__name__) from e
    finally:
        owlready2.JAVA_EXE, tempfile.tempdir = previous
        shutil.rmtree(private, ignore_errors=True)
    return consistent, int((time.monotonic() - started) * 1000)


def _closure(model: _Model, graph: Graph) -> set:
    """Canonical pairs: the inferred <h>_plus values united with the asserted <h> edges."""
    inferred = {(model.keys[a.iri], model.keys[b.iri]) for a, b in model.closure.get_relations()}
    return {(canon.key(a), canon.key(d)) for a, d in inferred | set(graph.edges)}


def _members(model: _Model, cls) -> tuple[set, int]:
    """(canonical keys of the node-table members, number of members outside the node table) of a defined class."""
    found = {ind.iri for ind in cls.instances()}
    return {canon.key(model.keys[i]) for i in found & model.node_iris}, len(found - model.node_iris)


def reason(cfg, graph: Graph, java: str) -> Reasoned:
    """Build the ontology, run HermiT and read its answers (closure pairs and defined-class members when consistent).
    Raises ReasonerError when HermiT gives no answer."""
    model = build(cfg, graph)
    try:
        consistent, ms = hermit(model.world, java)
        r = Reasoned(consistent, len(model.keys), len(model.node_iris), len(model.keys) - len(model.node_iris),
                     len(graph.edges), ms)
        if consistent:
            r.closure = _closure(model, graph)
            for name, cls in model.defined.items():
                r.members[name], r.outside[name] = _members(model, cls)
        return r
    finally:
        with contextlib.suppress(Exception):
            model.world.close()
