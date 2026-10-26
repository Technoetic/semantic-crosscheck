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
  - shapes with class and bounds: the row IRI on <target.path> as for a class shape, and the same value as a
    bound literal on <target.path>/value (naming.rdf_value_predicate), which the shape's second property shape
    bounds; both triples for every non-NULL value, whether or not the referenced row exists, as the view ORs the
    reference with the bounds. Both property shapes map back to the shape (focus_keys), so its SHACL keys are the
    union of their focus nodes
  - NULL gives no triple: never a violation, as the views test IS NOT NULL
Only shapes with an sql_violation have no SHACL form and are not compared (plan()); with verify.shacl_pairs, those
whose predicate is exactly t.<a> <op> t.<b> are compared separately as SHACL property pairs (run_pairs, below).
One RDF predicate carries one kind of value. Shapes on the same target column can need different kinds (a class and
a bound, or two class entities), and in one graph each value would violate the other shape's constraint. So the
compared shapes are split into value groups (groups()): each shape goes, in configuration order, to the first group
in which its column carries no other kind of value. "main" holds every shape that needs no split, so a configuration
without such a column exports one graph, as 0.3.0 did (plus the value-predicate triples of shapes that combine class
with bounds); a shape whose column already carries another kind in
every group opens the next one ("extra:1", "extra:2", ...). Every group gets its own data graph holding only what its
shapes need (their targets and paths; the keys of the class entities they reference), pySHACL validates each group
graph with the same shapes graph, and the focus nodes of a group are read only for that group's shapes. Within a
group every column carries one kind of value, so each group keeps the single-kind semantics above.

Counting, exporting and reading the views happen in one READ ONLY transaction at REPEATABLE READ, so the RDF of every
group and the views see the same snapshot; pySHACL then runs without inference after the transaction has ended.
verify.shacl_max_rows bounds the sum of the rows every group exports (a table read by two groups counts twice), so
the cap does not grow with the number of groups, and an extra group adds the rows it exports again. A group whose
validation fails (pySHACL returns no results graph, or raises any exception) leaves only its own shapes without an
answer, recorded by the exception type only; the other groups are still compared. Keys are
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
MAIN_GROUP = "main"


def value_kind(shape) -> str:
    """The kind of RDF value a shape with a SHACL form needs on its column: "class:<entity>" (row IRIs of that entity)
    or "bound" (literals). A shape with class and bounds is "class:<entity>": its bounds read the value predicate,
    which carries literals only."""
    return f"class:{shape.class_}" if shape.class_ else "bound"


def plan(cfg) -> tuple[list, dict]:
    """(shapes compared with SHACL, {shape id: reason} for the others) in configuration order. Reason:
    shapes.no_shacl_reason() ("sql_violation"). Every shape with a SHACL form is compared, class and bounds
    included; groups() splits them so that shapes needing different kinds of value on one column are validated
    apart."""
    reasons = {s.id: shapes.no_shacl_reason(s) for s in cfg.shapes}
    skipped = {sid: reason for sid, reason in reasons.items() if reason}
    return [s for s in cfg.shapes if s.id not in skipped], skipped


def groups(planned) -> list[tuple[str, list]]:
    """[(value group, its planned shapes)]. Each shape goes, in configuration order, to the first group in which its
    column (target, path) carries no other kind of value (value_kind); a shape whose column carries another kind in
    every group so far opens a new group. Every column of a group therefore carries one kind of value. Labels: "main"
    for the first group, "extra:<n>" (n = 1, 2, ...) for the others in the order they are opened. Without a column
    that needs two kinds of value, every shape stays in "main": one graph, as in 0.3.0 (plus value-predicate triples)."""
    out = []   # [(label, {(target, path): value kind}, shapes)]
    for s in planned:
        column, kind = (s.target, s.path), value_kind(s)
        for _label, kinds, members in out:
            if kinds.setdefault(column, kind) == kind:
                members.append(s)
                break
        else:
            out.append((f"extra:{len(out)}" if out else MAIN_GROUP, {column: kind}, [s]))
    return [(label, members) for label, _kinds, members in out]


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


def value_paths(planned, entity: str) -> set[str]:
    """Paths of an entity that also carry their value as a literal on naming.rdf_value_predicate: the paths of the
    planned shapes with class and bounds."""
    return {s.path for s in planned if s.target == entity and s.class_ and shapes.has_bounds(s)}


def _targets(planned) -> list[str]:
    return list(dict.fromkeys(s.target for s in planned))


def _class_entities(planned) -> list[str]:
    return list(dict.fromkeys(s.class_ for s in planned if s.class_))


def build_graph(cfg, planned, target_rows: dict, class_keys: dict):
    """The RDF data graph of one value group (planned: its shapes). target_rows: {entity: rows as [*key values,
    *path values in paths() order]}; class_keys: {class entity: its key values}. A path of a shape with class and
    bounds (value_paths) also gets the value as a literal on its value predicate, from the same column value."""
    import rdflib
    from rdflib import URIRef
    g = rdflib.Graph()
    rdf_type = URIRef(RDF_TYPE)
    for entity, rows in target_rows.items():
        n = len(cfg.entities[entity].key)
        cls = URIRef(naming.rdf_entity_class(cfg, entity))
        both = value_paths(planned, entity)
        preds = [(n + i, URIRef(naming.rdf_predicate(cfg, entity, path)), s,
                  URIRef(naming.rdf_value_predicate(cfg, entity, path)) if path in both else None)
                 for i, (path, s) in enumerate(paths(planned, entity))]
        for row in rows:
            node = URIRef(naming.rdf_node(cfg, entity, list(row[:n])))
            g.add((node, rdf_type, cls))
            for i, pred, s, value_pred in preds:
                if row[i] is None:
                    continue
                value = URIRef(naming.rdf_node(cfg, s.class_, [row[i]])) if s.class_ else literal(row[i])
                g.add((node, pred, value))
                if value_pred is not None:
                    g.add((node, value_pred, literal(row[i])))
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


def validate_generated_shapes(shapes_ttl_text: str) -> tuple[bool, int]:
    """The pinned shacl-shacl syntax subset, counts only; no invalid-shape report is logged.

    Keep this separate from validate: external conformance-corpus shapes must not be preflighted as generated ones.
    The public validate(meta_shacl=True) wrapper logs the complete failed report before raising, so call the pinned
    meta_validate helper directly and discard its report text. This cannot establish business-rule correctness.
    """
    try:
        import rdflib
        from pyshacl.entrypoints import meta_validate
        conforms, report, _text = meta_validate(shapes_graph(shapes_ttl_text), inference="none", advanced=False,
                                               abort_on_first=False, do_owl_imports=False)
        if type(conforms) is not bool or not isinstance(report, rdflib.Graph):
            raise ValueError("invalid meta report")
        rdf_type, sh = rdflib.RDF.type, rdflib.Namespace(shapes.SH)
        reports = list(report.subjects(rdf_type, sh.ValidationReport))
        if len(reports) != 1:
            raise ValueError("invalid meta report")
        stated = list(report.objects(reports[0], sh.conforms))
        if len(stated) != 1 or stated[0].toPython() is not conforms:
            raise ValueError("invalid meta verdict")
        count = len(set(report.subjects(rdf_type, sh.ValidationResult)))
        if conforms != (count == 0):
            raise ValueError("inconsistent meta report")
        return conforms, count
    except Exception:
        raise ValidationError("meta_validation_error") from None


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
    sh:focusNode. A shape with class and bounds has two property shapes; both map to it, so its keys are the union of
    their focus nodes, as its view ORs the two constraints. Results of shapes that are not planned (other groups'
    shapes, which also run on a group graph) are ignored."""
    from rdflib import URIRef
    by_source = {naming.rdf_property_shape(cfg, s.id): s for s in planned}
    by_source.update({naming.rdf_bound_shape(cfg, s.id): s for s in planned if s.class_ and shapes.has_bounds(s)})
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
    group graph with the same shapes graph. Any exception while one group is built, validated or read back (a
    ValidationError, or whatever pySHACL raises) leaves only that group's shapes without an answer: validation_error is
    the exception's type name (for a ValidationError the type pySHACL returned), never its message, which can quote
    values. TooManyRows and database errors are raised before validation and are not caught here."""
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
        except Exception as e:  # noqa: BLE001 - one group's failure must not hide the other groups' answers
            found, entry["validation_error"] = {}, str(e) if isinstance(e, ValidationError) else type(e).__name__
        for s in members:
            out[s.id] = dict(entry, shacl_keys=found.get(s.id), sql_keys=sql[s.id])
    return {s.id: out[s.id] for s in planned}


# ---- property pairs (verify.shacl_pairs) ----------------------------------------------------------------
# A shape whose sql_violation is exactly t.<a> <op> t.<b> (shapes.pair_form) is compared with SHACL 4.5 property
# pairs on a separate data graph and shapes graph built at reason time (shapes.pair_shapes_ttl), never with
# build's shapes.ttl. pySHACL 0.40.1 compares rdflib Literals: NaN counts as less than everything and literals of
# unrelated datatypes are ordered by datatype IRI, so it agrees with SHACL and PostgreSQL only on finite numbers.
# The preconditions keep everything else out (not_run, never pass): both columns of base type smallint, integer,
# bigint or numeric (column_type), no NaN or infinity in either column (non_finite_values) and no repeated target
# key (duplicate_keys: one RDF node would carry the values of several rows and pair them across rows).
PAIR_TYPES = ("smallint", "integer", "bigint", "numeric")
_PAIR_TYPE_SQL = (
    "SELECT format_type(a.atttypid, NULL), "
    "a.atttypid IN ('pg_catalog.int2'::regtype::oid, 'pg_catalog.int4'::regtype::oid, 'pg_catalog.int8'::regtype::oid, "
    "'pg_catalog.numeric'::regtype::oid), a.atttypid = 'pg_catalog.numeric'::regtype::oid "
    "FROM pg_catalog.pg_attribute a WHERE a.attrelid = to_regclass(:t) AND a.attname = :c AND a.attnum > 0 "
    "AND NOT a.attisdropped")


def pair_plan(cfg) -> list:
    """The shapes with a pair_form, in configuration order (whether or not verify.shacl_pairs is set)."""
    return [s for s in cfg.shapes if shapes.pair_form(s) is not None]


def pair_columns(pairs, entity: str) -> list[str]:
    """Columns of an entity the pair shapes compare, in shape order and predicate order (a, then b), each once: the
    order of the columns after the key columns in the pair export."""
    out = {}
    for s in pairs:
        if s.target == entity:
            a, _op, b = shapes.pair_form(s)
            out.setdefault(a)
            out.setdefault(b)
    return list(out)


def build_pair_graph(cfg, pairs, target_rows: dict):
    """The pair data graph. target_rows: {entity: rows as [*key values, *values in pair_columns() order]}. Every row
    gets rdf:type of its entity class, every non-NULL value its literal (literal()) on the column's value
    predicate, which carries nothing else; NULL gives no triple, so the pair has no value and no violation, as the
    SQL predicate is unknown there."""
    import rdflib
    from rdflib import URIRef
    g = rdflib.Graph()
    rdf_type = URIRef(RDF_TYPE)
    for entity, rows in target_rows.items():
        n = len(cfg.entities[entity].key)
        cls = URIRef(naming.rdf_entity_class(cfg, entity))
        preds = [(n + i, URIRef(naming.rdf_value_predicate(cfg, entity, c)))
                 for i, c in enumerate(pair_columns(pairs, entity))]
        for row in rows:
            node = URIRef(naming.rdf_node(cfg, entity, list(row[:n])))
            g.add((node, rdf_type, cls))
            for i, pred in preds:
                if row[i] is not None:
                    g.add((node, pred, literal(row[i])))
    return g


def pair_focus_keys(cfg, pairs, results) -> dict[str, set]:
    """{shape id: key tuples (canon.key form) of the focus nodes} of the pair property shapes (sh:sourceShape
    naming.rdf_pair_shape); results of other shapes are ignored."""
    from rdflib import URIRef
    by_source = {naming.rdf_pair_shape(cfg, s.id): s for s in pairs}
    keys = {s.id: set() for s in pairs}
    for r in results.subjects(URIRef(RDF_TYPE), URIRef(shapes.SH + "ValidationResult")):
        s = by_source.get(str(results.value(r, URIRef(shapes.SH + "sourceShape"))))
        if s is not None:
            focus = str(results.value(r, URIRef(shapes.SH + "focusNode")))
            keys[s.id].add(tuple(naming.rdf_key(cfg, s.target, focus)))
    return keys


def _column_type(conn, cfg, entity: str, column: str) -> tuple[str, bool, bool]:
    """(type name as format_type prints it without modifiers, or "absent"; one of PAIR_TYPES as a base type;
    numeric)."""
    r = conn.run(_PAIR_TYPE_SQL, t=ident.qualified(cfg.entities[entity].table), c=ident.check(column)) or []
    return (str(r[0][0]), bool(r[0][1]), bool(r[0][2])) if r else ("absent", False, False)


def _non_finite_rows(conn, cfg, entity: str, numeric: list[str]) -> int:
    """Rows of the entity with NaN or an infinity in one of the numeric columns (integer types have neither)."""
    if not numeric:
        return 0
    cond = " OR ".join(f"t.{ident.quote(c)} IN ('NaN', 'Infinity', '-Infinity')" for c in numeric)
    return int(db.scalar(conn, f"SELECT count(*) FROM {ident.qualified(cfg.entities[entity].table)} t WHERE {cond}"))


def _duplicate_key_rows(conn, cfg, entity: str) -> int:
    """Rows beyond the first of every key value the entity's table repeats (0 when the key is unique)."""
    ent = cfg.entities[entity]
    keys = ", ".join(f"t.{ident.quote(k)}" for k in ent.key)
    return int(db.scalar(conn, f"SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM "
                               f"{ident.qualified(ent.table)} t GROUP BY {keys} HAVING count(*) > 1) d"))


def run_pairs(conn, cfg, pairs, pair_ttl: str, rows_before: int = 0) -> dict[str, dict]:
    """Per pair shape, in the given order, one of:
      {"reason": not_run reason, ...}: view_missing, column_type (column_types), non_finite_values
        (non_finite_rows), duplicate_keys (duplicate_key_rows) or resource_limit (rows, limit);
      {"validation_error": exception type name, ...} when pySHACL could not validate the pair graph;
      {"shacl_keys": set, "sql_keys": set, "rows": rows exported, "column_types": [type of a, type of b]}.

    One read-only REPEATABLE READ transaction reads the views, checks the preconditions, counts the target rows of
    the shapes that meet them (rows_before, the rows the shape comparison counted, plus these may not exceed
    verify.shacl_max_rows, else every one is resource_limit) and exports them; pySHACL then validates the pair data
    graph with pair_ttl (inference none) after the transaction. A key repeated in the exported rows (one RDF node)
    is duplicate_keys even where SQL grouping saw none. Database errors are raised."""
    out, live, dup = {}, [], {}
    with db.read_only(conn, cfg.source.statement_timeout_ms, isolation="repeatable read"):
        for s in pairs:
            a, _op, b = shapes.pair_form(s)
            sql = sql_keys(conn, cfg, s)
            if sql is None:
                out[s.id] = {"reason": "view_missing"}
                continue
            found = [_column_type(conn, cfg, s.target, c) for c in (a, b)]
            types = [name for name, _ok, _numeric in found]
            if not all(ok for _name, ok, _numeric in found):
                out[s.id] = {"reason": "column_type", "column_types": types}
                continue
            bad = _non_finite_rows(conn, cfg, s.target, [c for c, f in zip((a, b), found) if f[2]])
            if bad:
                out[s.id] = {"reason": "non_finite_values", "column_types": types, "non_finite_rows": bad}
                continue
            if s.target not in dup:
                dup[s.target] = _duplicate_key_rows(conn, cfg, s.target)
            if dup[s.target]:
                out[s.id] = {"reason": "duplicate_keys", "column_types": types, "duplicate_key_rows": dup[s.target]}
                continue
            live.append((s, types, sql))
        live_shapes = [s for s, _t, _q in live]
        entities = list(dict.fromkeys(s.target for s in live_shapes))
        counted = {e: int(db.scalar(conn, f"SELECT count(*) FROM {ident.qualified(cfg.entities[e].table)}"))
                   for e in entities}
        rows = sum(counted.values())
        if rows_before + rows > cfg.verify.shacl_max_rows:
            for s, types, _sql in live:
                out[s.id] = {"reason": "resource_limit", "column_types": types, "rows": rows_before + rows,
                             "limit": cfg.verify.shacl_max_rows}
            return {s.id: out[s.id] for s in pairs}
        fetched = {}
        for e in entities:
            ent = cfg.entities[e]
            cols = ", ".join(f"t.{ident.quote(c)}" for c in ent.key + pair_columns(live_shapes, e))
            fetched[e] = conn.run(f"SELECT {cols} FROM {ident.qualified(ent.table)} t") or []
    for e, data in list(fetched.items()):
        n = len(cfg.entities[e].key)
        repeated = len(data) - len({naming.rdf_node(cfg, e, list(r[:n])) for r in data})
        if repeated:
            for s, types, _sql in live:
                if s.target == e:
                    out[s.id] = {"reason": "duplicate_keys", "column_types": types, "duplicate_key_rows": repeated}
            del fetched[e]
    live = [entry for entry in live if entry[0].target in fetched]
    if live:
        live_shapes = [s for s, _t, _q in live]
        try:
            found = pair_focus_keys(cfg, live_shapes, validate(build_pair_graph(cfg, live_shapes, fetched), pair_ttl))
        except Exception as e:  # noqa: BLE001 - the type name only: messages can quote values
            error = str(e) if isinstance(e, ValidationError) else type(e).__name__
            for s, types, _sql in live:
                out[s.id] = {"validation_error": error, "column_types": types, "rows": rows}
        else:
            for s, types, sql in live:
                out[s.id] = {"shacl_keys": found[s.id], "sql_keys": sql, "rows": rows, "column_types": types}
    return {s.id: out[s.id] for s in pairs}
