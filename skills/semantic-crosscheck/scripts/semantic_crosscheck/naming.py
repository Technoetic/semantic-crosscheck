"""Deterministic names shared by every stage: generated tables and views, Cube cubes and members, OWL names.

Stages never invent names on their own; they call these helpers so build, cube, reason, verify and the
mutation tests always agree. Every composed name is validated as a plain SQL identifier.
"""

from . import ident

# closure table / closure cube columns (in this order)
CLOSURE_COLUMNS = ("ancestor", "descendant", "min_depth", "max_depth", "path_count", "qty")
CLOSURE_DIMENSIONS = ("ancestor", "descendant", "min_depth", "max_depth", "path_count")
CLOSURE_MEASURES = ("qty", "pairs")          # qty: sum of summed path products; pairs: count of rows
# measure cube members; PK is the synthetic primary-key dimension of cubes with a composite key (hidden)
MEASURE_KEY, MEASURE_VALUE, MEASURE_TS, PK = "key", "value", "ts", "sxc_pk"


def _q(schema: str, name: str) -> str:
    return f"{ident.check(schema)}.{ident.check(name)}"


# ---- PostgreSQL objects (all in source.output_schema) --------------------------------------------
def closure_table(cfg, h: str) -> str:
    """schema.table of the closure of hierarchy h: CLOSURE_COLUMNS, one row per (ancestor, descendant)."""
    return _q(cfg.source.output_schema, f"{h}_closure")


def cycles_table(cfg, h: str) -> str:
    """schema.table listing example cycles of hierarchy h (start node, path array) when cycles exist."""
    return _q(cfg.source.output_schema, f"{h}_cycles")


def shape_view(cfg, shape_id: str) -> str:
    """schema.view selecting the key columns of the target rows that violate the shape."""
    return _q(cfg.source.output_schema, f"v_shape_{shape_id}")


# ---- Cube model -----------------------------------------------------------------------------------
def cube_entity(entity: str) -> str:
    return ident.check(entity)


def cube_closure(h: str) -> str:
    return ident.check(f"{h}_closure")


def cube_ancestor(h: str) -> str:
    """Copy of the node entity cube joined on closure.ancestor."""
    return ident.check(f"{h}_ancestor")


def cube_descendant(h: str) -> str:
    """Copy of the node entity cube joined on closure.descendant."""
    return ident.check(f"{h}_descendant")


def cube_measure(m: str) -> str:
    return ident.check(f"m_{m}")


def member(cube: str, name: str) -> str:
    return f"{ident.check(cube)}.{ident.check(name)}"


# ---- RDF / SHACL (data exported for pySHACL and the generated shapes.ttl) ------------------------
DEFAULT_IRI = "https://example.org/sxc#"


def _iri(cfg) -> str:
    return cfg.ontology.iri or DEFAULT_IRI


def rdf_entity_class(cfg, entity: str) -> str:
    """rdf:type of every exported row of an entity."""
    return _iri(cfg) + ident.check(entity)


def rdf_predicate(cfg, entity: str, column: str) -> str:
    return f"{_iri(cfg)}{ident.check(entity)}.{ident.check(column)}"


def rdf_node(cfg, entity: str, key_values: list) -> str:
    """IRI of one row: entity + percent-encoded JSON array of the canonical key values (reversible)."""
    import json
    import urllib.parse

    from . import canon
    return f"{_iri(cfg)}{ident.check(entity)}/" + urllib.parse.quote(json.dumps([canon.canon(v) for v in key_values], ensure_ascii=False), safe="")


def rdf_key(cfg, entity: str, node_iri: str) -> list:
    """Inverse of rdf_node: the canonical key values."""
    import json
    import urllib.parse
    prefix = f"{_iri(cfg)}{ident.check(entity)}/"
    if not node_iri.startswith(prefix):
        raise ValueError("node IRI of another entity")
    return json.loads(urllib.parse.unquote(node_iri[len(prefix):]))


def rdf_node_shape(cfg, shape_id: str) -> str:
    return f"{_iri(cfg)}shape/{ident.check(shape_id)}"


def rdf_property_shape(cfg, shape_id: str) -> str:
    """sh:sourceShape of results of a shape; maps a pySHACL result back to the shape id."""
    return f"{_iri(cfg)}shape/{ident.check(shape_id)}/p"


# ---- OWL ------------------------------------------------------------------------------------------
def owl_edge_property(h: str) -> str:
    """Object property asserted for every (parent, child) edge."""
    return ident.check(h)


def owl_closure_property(h: str) -> str:
    """Transitive super-property of the edge property."""
    return ident.check(f"{h}_plus")
