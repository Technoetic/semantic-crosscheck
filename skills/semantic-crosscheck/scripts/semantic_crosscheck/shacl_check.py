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
One RDF predicate carries one kind of value. Shapes on the same target column can need different kinds (a class and
a bound, or two class entities), and in one graph each value would violate the other shape's constraint. So the
compared shapes are split into value groups (groups()): one group for the bound shapes (literal values) and one per
referenced class entity. Every group gets its own data graph holding only what its shapes need (their targets and
paths; for a class group the keys of its class entity), pySHACL validates each group graph with the same shapes
graph, and the focus nodes of a group are read only for that group's shapes. Within a group every column carries one
kind of value, so each group keeps the single-kind semantics above.

Counting, exporting and reading the views happen in one READ ONLY transaction at REPEATABLE READ, so the RDF of every
group and the views see the same snapshot; pySHACL then runs without inference after the transaction has ended.
verify.shacl_max_rows bounds the sum of the rows every group exports (a table read by two groups counts twice), so
the cap does not grow with the number of groups. A group whose validation fails leaves only its own shapes without
an answer; the other groups are still compared. Keys are
compared in canon.key form (text exactly, other values canonical), which is also what rdf_node encodes, so a focus
node maps back with rdf_key and two text keys such as '1.10' and '1.1' never share an IRI.
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
def value_group(shape) -> str:
    """The value group of a shape with a SHACL form: "class:<entity>" (row IRIs of that entity) or "bound" (literals)."""
    return f"class:{shape.class_}" if shape.class_ else "bound"


def plan(cfg) -> tuple[list, dict]:
    """(shapes compared with SHACL, {shape id: reason} for the others) in configuration order. Reasons:
    shapes.no_shacl_reason() ("sql_violation", "class_and_bounds"). Every shape with a SHACL form is compared;
    groups() splits them so that shapes needing different kinds of value on one column are validated apart."""
    reasons = {s.id: shapes.no_shacl_reason(s) for s in cfg.shapes}
    skipped = {sid: reason for sid, reason in reasons.items() if reason}
    return [s for s in cfg.shapes if s.id not in skipped], skipped


def groups(planned) -> list[tuple[str, list]]:
    """[(value group, its planned shapes)] in the order of the first shape of each group. Every column of a group
    carries one kind of value: bound shapes literals, the shapes of a class group row IRIs of its class entity."""
    out = {}
    for s in planned:
        out.setdefault(value_group(s), []).append(s)
    return list(out.items())


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
    the order of the path columns after the key columns in fetch() and build_graph(). Called with the shapes of one
    value group, so every path has one kind of value."""
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
    """The RDF data graph of one value group (planned: its shapes). target_rows: {entity: rows as [*key values,
    *path values in paths() order]}; class_keys: {class entity: its key values}."""
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
def count_rows(conn, cfg, planned, counted: dict | None = None) -> int:
    """Rows of every entity one value group's export reads (its targets and class entity, each once). counted
    ({entity: rows}) keeps the counts across groups, so a table read by several groups is counted by one query."""
    counted = {} if counted is None else counted
    entities = dict.fromkeys(_targets(planned) + _class_entities(planned))
    for e in entities:
        if e not in counted:
            counted[e] = int(db.scalar(conn, f"SELECT count(*) FROM {ident.qualified(cfg.entities[e].table)}"))
    return sum(counted[e] for e in entities)


def fetch(conn, cfg, planned) -> tuple[dict, dict]:
    """(target_rows, class_keys) of one value group for build_graph. Columns are selected by position: a path may be
    a key column."""
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
    """Key tuples (canon.key form, as rdf_node encodes them) in the shape's view, or None when the view does not exist."""
    view = naming.shape_view(cfg, shape.id)
    if not db.scalar(conn, "SELECT to_regclass(:v) IS NOT NULL", v=view):
        return None
    cols = ", ".join(ident.quote(k) for k in cfg.entities[shape.target].key)
    return {tuple(canon.key(v) for v in r) for r in conn.run(f"SELECT {cols} FROM {ident.qualified(view)}") or []}


def _limited_fetch(conn, cfg, grouped) -> tuple[dict, dict]:
    """({group: (target_rows, class_keys)}, {group: rows}) after counting every group first: TooManyRows when the sum
    over the groups exceeds verify.shacl_max_rows, before anything is fetched."""
    counted = {}
    rows = {name: count_rows(conn, cfg, members, counted) for name, members in grouped}
    total = sum(rows.values())
    if total > cfg.verify.shacl_max_rows:
        raise TooManyRows(total, cfg.verify.shacl_max_rows)
    return {name: fetch(conn, cfg, members) for name, members in grouped}, rows


def export_graphs(conn, cfg) -> dict:
    """{value group: its RDF data graph} for every shape with a SHACL form, read in one read-only transaction.
    Raises TooManyRows when the rows of all groups together exceed verify.shacl_max_rows."""
    grouped = groups(plan(cfg)[0])
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        conn.run(SNAPSHOT_SQL)
        fetched, _rows = _limited_fetch(conn, cfg, grouped)
    return {name: build_graph(cfg, members, *fetched[name]) for name, members in grouped}


# ---- pySHACL --------------------------------------------------------------------------------------------
def shapes_graph(shapes_ttl_text: str):
    """The shapes graph parsed once, validated against every group graph."""
    import rdflib
    return rdflib.Graph().parse(data=shapes_ttl_text, format="turtle")


def validate(data_graph, shacl):
    """pySHACL's results graph: no inference, warnings allowed (severity does not change which rows violate). shacl:
    the shapes graph, or its Turtle text."""
    import pyshacl
    import rdflib
    graph = shapes_graph(shacl) if isinstance(shacl, str) else shacl
    _conforms, report, _text = pyshacl.validate(data_graph, shacl_graph=graph, inference="none", allow_warnings=True)
    if not isinstance(report, rdflib.Graph):
        raise ValidationError(type(report).__name__)
    return report


def focus_keys(cfg, planned, results) -> dict[str, set]:
    """{shape id: key tuples (canon.key form) of the focus nodes} from sh:sourceShape (the property shape IRI) and
    sh:focusNode. Results of shapes that are not planned (other groups' shapes, which also run on a group graph) are
    ignored."""
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
    """Per planned shape {"shacl_keys": set, or None when its group's validation failed, "sql_keys": set or None (no
    view), "rows": rows exported by all groups, "group": its value group, "group_rows": rows of its group, and
    "validation_error" when pySHACL could not validate the group's graph}.

    One read-only REPEATABLE READ transaction counts the rows of every group (TooManyRows when their sum exceeds
    verify.shacl_max_rows, before anything is exported), exports them and reads the views; pySHACL then validates each
    group graph with the same shapes graph. A ValidationError leaves only that group's shapes without an answer."""
    planned, _skipped = plan(cfg)
    if not planned:
        return {}
    grouped = groups(planned)
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        conn.run(SNAPSHOT_SQL)
        fetched, rows = _limited_fetch(conn, cfg, grouped)
        sql = {s.id: sql_keys(conn, cfg, s) for s in planned}
    total, graph_of_shapes, out = sum(rows.values()), shapes_graph(shapes_ttl_text), {}
    for name, members in grouped:
        entry = {"rows": total, "group": name, "group_rows": rows[name]}
        try:
            found = focus_keys(cfg, members, validate(build_graph(cfg, members, *fetched.pop(name)), graph_of_shapes))
        except ValidationError as e:
            found, entry["validation_error"] = {}, str(e)
        for s in members:
            out[s.id] = dict(entry, shacl_keys=found.get(s.id), sql_keys=sql[s.id])
    return {s.id: out[s.id] for s in planned}
