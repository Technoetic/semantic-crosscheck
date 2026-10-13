"""SHACL against SQL: pySHACL's focus nodes must be exactly the keys each shape view selects.

Why: build writes every data rule twice, as an SQL violation view and as a SHACL shape (shapes.py). Two
formulations of one rule that disagree on any row mean one of them is wrong (NULL handling, a dangling reference,
a bound compared on another type), and only a row-by-row comparison of the two key sets shows it.

The rows a shape needs are exported as RDF in memory with the naming.rdf_* IRIs, following the semantics shapes.py
documents:
  - every target row: <row> rdf:type <target class>, and its path value as <row> <target.path> <value>
  - class shapes: the value is the IRI of the referenced row, and every key of the class entity is typed with the
    class entity's class, so a dangling reference is a node without a type (an sh:class violation)
  - bound shapes: integers as xsd:integer, other numbers as xsd:decimal (non-finite ones as xsd:double, which has
    NaN and INF); other types keep rdflib's default datatype
  - NULL gives no triple: never a violation, as the views test IS NOT NULL
One RDF predicate carries one kind of value. When shapes on the same target column need different kinds (a class
and a bound, or two class entities), each value would violate the other shape's constraint, so none of those shapes
is compared (plan() names them "shared_path").

Counting, exporting and reading the views happen in one READ ONLY transaction at REPEATABLE READ, so the RDF and
the views see the same snapshot; pySHACL then runs without inference after the transaction has ended. Keys are
compared in canonical form (canon), which is also what rdf_node encodes, so a focus node maps back with rdf_key.
rdflib and pyshacl are imported lazily: only the reason stage needs them.
"""

from decimal import Decimal

from . import canon, db, ident, naming, shapes

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SNAPSHOT_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"


class TooManyRows(Exception):
    """The rows the export needs exceed verify.shacl_max_rows: nothing is exported or compared."""

    def __init__(self, rows: int, limit: int):
        super().__init__(f"{rows} rows exceed verify.shacl_max_rows = {limit}")
        self.rows, self.limit = rows, limit


class ValidationError(Exception):
    """pySHACL could not validate (it returns a failure instead of a results graph)."""


# ---- which shapes are compared ------------------------------------------------------------------------
def _value_kind(shape) -> tuple:
    return ("class", shape.class_) if shape.class_ else ("bound",)


def plan(cfg) -> tuple[list, dict]:
    """(shapes compared with SHACL, {shape id: reason} for the others) in configuration order. Reasons:
    shapes.no_shacl_reason() ("sql_violation", "class_and_bounds") or "shared_path"."""
    skipped, kinds = {}, {}
    for s in cfg.shapes:
        reason = shapes.no_shacl_reason(s)
        if reason:
            skipped[s.id] = reason
        else:
            kinds.setdefault((s.target, s.path), set()).add(_value_kind(s))
    for s in cfg.shapes:
        if s.id not in skipped and len(kinds[(s.target, s.path)]) > 1:
            skipped[s.id] = "shared_path"
    return [s for s in cfg.shapes if s.id not in skipped], skipped


# ---- RDF ----------------------------------------------------------------------------------------------
def literal(value):
    """A bound shape's path value as a typed literal: xsd:integer for integers, xsd:decimal for other finite
    numbers, xsd:double for NaN and infinities; anything else (text, dates, booleans) keeps rdflib's datatype."""
    from rdflib import Literal, URIRef
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return Literal(value)
    if isinstance(value, int):
        return Literal(str(value), datatype=URIRef(shapes.XSD + "integer"))
    d = Decimal(repr(value)) if isinstance(value, float) else value
    if not d.is_finite():   # normalize=False keeps the XSD spelling (rdflib would write Python's "nan" / "inf")
        lexical = "NaN" if d.is_nan() else ("-INF" if d.is_signed() else "INF")
        return Literal(lexical, datatype=URIRef(shapes.XSD + "double"), normalize=False)
    return Literal(shapes.numeral(d), datatype=URIRef(shapes.XSD + "decimal"))


def paths(planned, entity: str) -> list[tuple[str, object]]:
    """(path, shape giving the value kind) of the planned shapes on an entity, one entry per path in shape order:
    the order of the path columns after the key columns in fetch() and build_graph()."""
    out = {}
    for s in planned:
        if s.target == entity:
            out.setdefault(s.path, s)
    return list(out.items())


def _targets(planned) -> list[str]:
    return list(dict.fromkeys(s.target for s in planned))


def _class_entities(planned) -> list[str]:
    return list(dict.fromkeys(s.class_ for s in planned if s.class_))


def build_graph(cfg, planned, target_rows: dict, class_keys: dict):
    """The RDF data graph. target_rows: {entity: rows as [*key values, *path values in paths() order]};
    class_keys: {class entity: its key values}."""
    import rdflib
    from rdflib import URIRef
    g = rdflib.Graph()
    rdf_type = URIRef(RDF_TYPE)
    for entity, rows in target_rows.items():
        n = len(cfg.entities[entity].key)
        cls = URIRef(naming.rdf_entity_class(cfg, entity))
        preds = [(n + i, URIRef(naming.rdf_predicate(cfg, entity, path)), s) for i, (path, s) in enumerate(paths(planned, entity))]
        for row in rows:
            node = URIRef(naming.rdf_node(cfg, entity, list(row[:n])))
            g.add((node, rdf_type, cls))
            for i, pred, s in preds:
                if row[i] is None:
                    continue
                value = URIRef(naming.rdf_node(cfg, s.class_, [row[i]])) if s.class_ else literal(row[i])
                g.add((node, pred, value))
    for entity, keys in class_keys.items():
        cls = URIRef(naming.rdf_entity_class(cfg, entity))
        for k in keys:
            g.add((URIRef(naming.rdf_node(cfg, entity, [k])), rdf_type, cls))
    return g


# ---- SQL (inside the caller's read-only transaction) --------------------------------------------------
def count_rows(conn, cfg, planned) -> int:
    """Rows of every entity the export reads (targets and class entities, each once)."""
    entities = dict.fromkeys(_targets(planned) + _class_entities(planned))
    return sum(int(db.scalar(conn, f"SELECT count(*) FROM {ident.qualified(cfg.entities[e].table)}")) for e in entities)


def fetch(conn, cfg, planned) -> tuple[dict, dict]:
    """(target_rows, class_keys) for build_graph. Columns are selected by position: a path may be a key column."""
    target_rows = {}
    for entity in _targets(planned):
        ent = cfg.entities[entity]
        cols = ", ".join(f"t.{ident.quote(c)}" for c in ent.key + [p for p, _s in paths(planned, entity)])
        target_rows[entity] = conn.run(f"SELECT {cols} FROM {ident.qualified(ent.table)} t") or []
    class_keys = {}
    for entity in _class_entities(planned):
        ent = cfg.entities[entity]
        k = ident.quote(ent.key[0])
        rows = conn.run(f"SELECT t.{k} FROM {ident.qualified(ent.table)} t WHERE t.{k} IS NOT NULL") or []
        class_keys[entity] = [r[0] for r in rows]
    return target_rows, class_keys


def sql_keys(conn, cfg, shape) -> set | None:
    """Canonical key tuples in the shape's view, or None when the view does not exist."""
    view = naming.shape_view(cfg, shape.id)
    if not db.scalar(conn, "SELECT to_regclass(:v) IS NOT NULL", v=view):
        return None
    cols = ", ".join(ident.quote(k) for k in cfg.entities[shape.target].key)
    return {tuple(canon.canon(v) for v in r) for r in conn.run(f"SELECT {cols} FROM {ident.qualified(view)}") or []}


def _limited_export(conn, cfg, planned) -> tuple[object, int]:
    rows = count_rows(conn, cfg, planned)
    if rows > cfg.verify.shacl_max_rows:
        raise TooManyRows(rows, cfg.verify.shacl_max_rows)
    return build_graph(cfg, planned, *fetch(conn, cfg, planned)), rows


def export_graph(conn, cfg):
    """The RDF data graph of every shape with a SHACL form, read in its own read-only transaction. Raises
    TooManyRows when the rows exceed verify.shacl_max_rows."""
    planned, _skipped = plan(cfg)
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        conn.run(SNAPSHOT_SQL)
        return _limited_export(conn, cfg, planned)[0]


# ---- pySHACL --------------------------------------------------------------------------------------------
def validate(data_graph, shapes_ttl_text: str):
    """pySHACL's results graph: no inference, warnings allowed (severity does not change which rows violate)."""
    import pyshacl
    import rdflib
    shapes_graph = rdflib.Graph().parse(data=shapes_ttl_text, format="turtle")
    _conforms, report, _text = pyshacl.validate(data_graph, shacl_graph=shapes_graph, inference="none", allow_warnings=True)
    if not isinstance(report, rdflib.Graph):
        raise ValidationError(type(report).__name__)
    return report


def focus_keys(cfg, planned, results) -> dict[str, set]:
    """{shape id: canonical key tuples of the focus nodes} from sh:sourceShape (the property shape IRI) and
    sh:focusNode. Results of shapes that are not planned are ignored."""
    from rdflib import URIRef
    by_source = {naming.rdf_property_shape(cfg, s.id): s for s in planned}
    keys = {s.id: set() for s in planned}
    for r in results.subjects(URIRef(RDF_TYPE), URIRef(shapes.SH + "ValidationResult")):
        s = by_source.get(str(results.value(r, URIRef(shapes.SH + "sourceShape"))))
        if s is not None:
            focus = str(results.value(r, URIRef(shapes.SH + "focusNode")))
            keys[s.id].add(tuple(naming.rdf_key(cfg, s.target, focus)))
    return keys


def run(conn, cfg, shapes_ttl_text: str) -> dict[str, dict]:
    """Per planned shape {"shacl_keys": set, "sql_keys": set or None (no view), "rows": rows exported}.

    One read-only REPEATABLE READ transaction counts the rows (TooManyRows above verify.shacl_max_rows, before
    anything is exported), exports them and reads the views; pySHACL validates afterwards."""
    planned, _skipped = plan(cfg)
    if not planned:
        return {}
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        conn.run(SNAPSHOT_SQL)
        graph, rows = _limited_export(conn, cfg, planned)
        sql = {s.id: sql_keys(conn, cfg, s) for s in planned}
    found = focus_keys(cfg, planned, validate(graph, shapes_ttl_text))
    return {s.id: {"shacl_keys": found[s.id], "sql_keys": sql[s.id], "rows": rows} for s in planned}
