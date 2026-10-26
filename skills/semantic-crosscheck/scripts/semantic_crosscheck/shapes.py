"""Data rules generated from the [[shapes]] entries: SQL violation views and the equivalent SHACL shapes.

Each shape becomes a view (naming.shape_view) selecting the key columns of the target rows that violate it,
and, when it has one, a SHACL node shape in shapes.ttl that the reason stage runs with pySHACL on rows exported
as RDF with the naming.rdf_* IRIs. The reason stage compares the two per shape, so both forms follow one
semantics:
  - class (referential): a non-NULL path value must be a key of the class entity. In RDF the value is the IRI of
    the referenced row and only rows that exist in the class entity carry rdf:type, so a dangling reference is a
    value node without a type, which violates sh:class.
  - bounds: a non-NULL path value must satisfy every bound (min_exclusive >, min_inclusive >=, max_inclusive <=).
  - class and bounds: one RDF value cannot be both a row IRI and a number (SHACL 4.3: a value node that cannot be
    compared with a bound violates it), so the node shape gets two property shapes: sh:class on the column's
    predicate (naming.rdf_property_shape) and the bounds on its value predicate (naming.rdf_bound_shape on
    naming.rdf_value_predicate, the same column value as a literal). The view applies the bounds whether or not the
    reference exists, and so does the export, which writes both triples for every non-NULL value.
  - NULL is never a violation: SQL tests IS NOT NULL, the RDF export writes no triple for NULL.
  - sql_violation: a trusted SQL predicate over alias t, copied from the configuration (reports label it).
The constraints of one shape are alternatives: the view ORs them; pySHACL reports every failing constraint and
the reason stage compares sets of focus nodes (the results of both property shapes of a shape count for it). A
shape with an sql_violation has no SHACL form; shapes_ttl() leaves it out and returns its id so it can be reported
as not proven instead of compared.
With verify.shacl_pairs the reason stage also compares a shape whose predicate is exactly t.<a> <op> t.<b>
(pair_form) through property-pair shapes generated at reason time (pair_shapes_ttl), never part of shapes.ttl.

shapes.ttl is written without rdflib (which only the reason stage needs), escaping IRIs and literals as the
Turtle grammar requires. fk_review_sql() proposes NOT VALID foreign keys for shapes with fk_ddl = "review_only":
a file for a person to review, never executed by the tool.
"""

import re
from decimal import Decimal, InvalidOperation

from . import ident, naming
from .result import EXIT_CONFIG, StageError

# (Shape attribute, SQL operator a valid value satisfies, SHACL constraint parameter)
BOUNDS = (
    ("min_exclusive", ">", "minExclusive"),
    ("min_inclusive", ">=", "minInclusive"),
    ("max_inclusive", "<=", "maxInclusive"),
)
SH = "http://www.w3.org/ns/shacl#"
XSD = "http://www.w3.org/2001/XMLSchema#"
_NUMERAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
_IRI_FORBIDDEN = frozenset('<>"{}|^`\\')
_ECHAR = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
_SAVEPOINT = "sxc_shape"


# ---- shape properties -----------------------------------------------------------------------------
def config_ref(cfg, shape) -> str:
    """The crosscheck.toml entry of a shape, named as config.load names it."""
    for i, s in enumerate(cfg.shapes):
        if s.id == shape.id:
            return f"shapes[{i}] {shape.id}"
    return f"shapes {shape.id}"


def has_bounds(shape) -> bool:
    return any(getattr(shape, attr) is not None for attr, _op, _prop in BOUNDS)


def kind(shape) -> str:
    """'class', 'bound', 'sql', or a '+'-joined combination of them."""
    parts = [name for name, present in (("class", bool(shape.class_)), ("bound", has_bounds(shape)),
                                        ("sql", bool(shape.sql_violation))) if present]
    return "+".join(parts)


def no_shacl_reason(shape) -> str:
    """Why a shape has no SHACL form equal to its view ('' when it has one)."""
    return "sql_violation" if shape.sql_violation else ""


# ---- property pairs (verify.shacl_pairs) ----------------------------------------------------------
# Violation operator of t.<a> <op> t.<b> -> (column on sh:path, SHACL 4.5 parameter the valid rows satisfy, column
# the parameter names). sh:equals and sh:disjoint are never used: = and <> stay without a SHACL form.
PAIR_OPS = {
    ">": ("a", "lessThanOrEquals", "b"),
    ">=": ("a", "lessThan", "b"),
    "<": ("b", "lessThanOrEquals", "a"),
    "<=": ("b", "lessThan", "a"),
}
_WS = "[ \t\r\n]*"   # SQL whitespace only; any other character (a comment, a cast, a parenthesis) leaves no form
_PAIR = re.compile(f"{_WS}t\\.([a-z_][a-z0-9_]*){_WS}(>=|<=|>|<){_WS}t\\.([a-z_][a-z0-9_]*){_WS}")


def pair_form(shape) -> tuple[str, str, str] | None:
    """(column a, operator, column b) when the shape's only constraint is an sql_violation that is, as a whole,
    exactly t.<a> <op> t.<b> with op one of > >= < <=, two different plain identifiers (ident.check) and only
    whitespace around them; None for anything else: a shape with path, class or bounds, comments, parentheses,
    casts, constants, = <> !=, quoted or upper-case names."""
    if shape.path or shape.class_ or has_bounds(shape) or not isinstance(shape.sql_violation, str):
        return None
    m = _PAIR.fullmatch(shape.sql_violation)
    if not m:
        return None
    a, op, b = m.groups()
    try:
        ident.check(a), ident.check(b)
    except ident.IdentError:
        return None
    return (a, op, b) if a != b else None


def pair_constraint(shape) -> tuple[str, str, str] | None:
    """(path column, SHACL parameter, compared column) of a shape with a pair_form, from PAIR_OPS."""
    form = pair_form(shape)
    if form is None:
        return None
    a, op, b = form
    side = {"a": a, "b": b}
    path, param, other = PAIR_OPS[op]
    return side[path], param, side[other]


def numeral(value) -> str:
    """A bound as a plain decimal numeral (finite, no exponent): both an SQL numeric constant and an xsd:decimal
    lexical form. Raises ValueError for anything else."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError(f"not a number: {value!r}")
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}") from None
    if not d.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    text = "0" if d == 0 else format(d, "f")
    if not _NUMERAL.match(text):
        raise ValueError(f"not a plain decimal: {value!r}")
    return text


def bounds(cfg, shape) -> list[tuple[str, str, str]]:
    """[(sql_operator, shacl_parameter, numeral)] for the bounds the shape sets, in BOUNDS order."""
    out = []
    for attr, op, prop in BOUNDS:
        value = getattr(shape, attr)
        if value is None:
            continue
        try:
            out.append((op, prop, numeral(value)))
        except ValueError:
            raise StageError(f"crosscheck.toml [{config_ref(cfg, shape)}]: {attr} must be a finite number", EXIT_CONFIG) from None
    return out


# ---- SQL ------------------------------------------------------------------------------------------
def violation_sql(cfg, shape) -> str:
    """SELECT of the target entity's key columns for the rows of the target table (alias t) violating the shape."""
    ref = config_ref(cfg, shape)
    target = cfg.entity(shape.target, ref)
    path = f"t.{ident.quote(shape.path)}" if shape.path else ""
    violations = []
    if shape.class_:
        cls = cfg.entity(shape.class_, ref)
        violations.append(f"{path} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {ident.qualified(cls.table)} c "
                          f"WHERE c.{ident.quote(cls.key[0])} = {path})")
    for op, _prop, num in bounds(cfg, shape):
        violations.append(f"{path} IS NOT NULL AND NOT ({path} {op} {num})")
    if shape.sql_violation:
        # own lines, so a trailing "--" comment in the trusted predicate cannot swallow the closing parenthesis
        violations.append(f"\n{shape.sql_violation}\n")
    if not violations:
        raise StageError(f"crosscheck.toml [{ref}]: a shape needs class, a bound, or sql_violation", EXIT_CONFIG)
    keys = ", ".join(f"t.{ident.quote(k)}" for k in target.key)
    where = "\n   OR ".join(f"({v})" for v in violations)
    return f"SELECT {keys}\nFROM {ident.qualified(target.table)} t\nWHERE {where}"


def sqlstate(e: BaseException) -> str:
    """SQLSTATE of a database error, '' for any other exception (messages are never kept: they can quote values)."""
    arg = e.args[0] if e.args else None
    return str(arg.get("C", "")) if isinstance(arg, dict) else ""


def _attempt(conn, statements: list[str]) -> tuple[str, list]:
    """Run statements under a savepoint. Returns ('', rows of the last statement) or (SQLSTATE, []) after rolling
    the savepoint back, so one failing shape leaves the caller's transaction usable."""
    conn.run(f"SAVEPOINT {_SAVEPOINT}")
    try:
        rows = []
        for sql in statements:
            rows = conn.run(sql)
    except Exception as e:
        code = sqlstate(e)
        if not code:
            raise
        conn.run(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
        conn.run(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        return code, []
    conn.run(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    return "", rows or []


def _create_view(conn, cfg, shape) -> dict:
    view = ident.qualified(naming.shape_view(cfg, shape.id))
    select = violation_sql(cfg, shape)
    log = [f"CREATE OR REPLACE VIEW {view} AS\n{select}"]
    code, _ = _attempt(conn, log)
    if code == "42P16":   # the view's columns changed (another target or key): replace it by drop and create
        log += [f"DROP VIEW {view}", f"CREATE VIEW {view} AS\n{select}"]
        code, _ = _attempt(conn, log[1:])
    count = None
    if not code:
        code, rows = _attempt(conn, [f"SELECT count(*) FROM {view}"])
        count = int(rows[0][0]) if not code else None
    if code:
        log.append(f"DROP VIEW IF EXISTS {view}")
        _attempt(conn, log[-1:])   # leave no stale or broken view behind; a failure here changes nothing reported
        return {"shape": shape.id, "violations": None, "sql": _script(log), "sqlstate": code}
    return {"shape": shape.id, "violations": count, "sql": _script(log)}


def _script(statements: list[str]) -> str:
    return "\n".join(s + ";" for s in statements)


def create_views(conn, cfg) -> list[dict]:
    """Create or replace every shape view inside the caller's transaction and count its rows.

    Returns [{"shape": id, "violations": n, "sql": ddl}] in configuration order. Each shape runs under a savepoint:
    a database error (a broken trusted predicate, a type mismatch, a timeout) gives {"violations": None,
    "sqlstate": code}, the shape's view is dropped so no stale definition survives, and the other shapes still build.
    """
    return [_create_view(conn, cfg, shape) for shape in cfg.shapes]


def fk_review_sql(cfg) -> str:
    """NOT VALID foreign keys for the shapes with fk_ddl = "review_only", for a person to review."""
    lines = [
        "-- semantic-crosscheck: foreign keys proposed by shapes with fk_ddl = \"review_only\" (generated from crosscheck.toml).",
        "-- FOR REVIEW ONLY: the tool never executes this file; a person decides whether to apply it.",
        "-- NOT VALID adds a constraint without checking existing rows. Fix the rows listed by the shape's view first,",
        "-- then run the commented VALIDATE CONSTRAINT. The referenced key needs a primary key or unique constraint.",
        "",
    ]
    for shape in cfg.shapes:
        if shape.fk_ddl != "review_only":
            continue
        ref = config_ref(cfg, shape)
        table = ident.qualified(cfg.entity(shape.target, ref).table)
        cls = cfg.entity(shape.class_, ref)
        name = ident.quote(f"sxc_fk_{shape.id}")
        lines += [
            f"-- {ref}: violating rows in {naming.shape_view(cfg, shape.id)}",
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({ident.quote(shape.path)})",
            f"  REFERENCES {ident.qualified(cls.table)} ({ident.quote(cls.key[0])}) NOT VALID;",
            f"-- ALTER TABLE {table} VALIDATE CONSTRAINT {name};",
            "",
        ]
    return "\n".join(lines)


# ---- SHACL (Turtle) -------------------------------------------------------------------------------
def _uchar(ch: str) -> str:
    cp = ord(ch)
    return f"\\u{cp:04X}" if cp <= 0xFFFF else f"\\U{cp:08X}"


def turtle_iri(iri: str) -> str:
    """<iri> for Turtle: characters the IRIREF production forbids (controls, space, <>"{}|^`\\) become UCHAR escapes."""
    return "<" + "".join(_uchar(ch) if ord(ch) <= 0x20 or ch in _IRI_FORBIDDEN else ch for ch in iri) + ">"


def turtle_literal(text: str) -> str:
    """Double-quoted Turtle string: quote, backslash and line breaks as ECHAR escapes, other controls as UCHAR."""
    out = []
    for ch in text:
        if ch in _ECHAR:
            out.append(_ECHAR[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(_uchar(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _shape_ttl(cfg, shape) -> str:
    """One node shape with one property shape, or with two for a shape with class and bounds: sh:class on the
    column's predicate, the bounds on its value predicate. A shape with only one of them is written as before."""
    severity = "sh:Warning" if shape.severity == "warning" else "sh:Violation"
    cls = [f"sh:class {turtle_iri(naming.rdf_entity_class(cfg, shape.class_))}"] if shape.class_ else []
    limits = [f"sh:{param} {turtle_literal(num)}^^xsd:decimal" for _op, param, num in bounds(cfg, shape)]
    path = naming.rdf_predicate(cfg, shape.target, shape.path)
    if cls and limits:
        props = [(naming.rdf_property_shape(cfg, shape.id), path, cls),
                 (naming.rdf_bound_shape(cfg, shape.id), naming.rdf_value_predicate(cfg, shape.target, shape.path), limits)]
    else:
        props = [(naming.rdf_property_shape(cfg, shape.id), path, cls + limits)]
    lines = [
        turtle_iri(naming.rdf_node_shape(cfg, shape.id)),
        "    a sh:NodeShape ;",
        f"    sh:targetClass {turtle_iri(naming.rdf_entity_class(cfg, shape.target))} ;",
    ]
    lines += [f"    sh:property {turtle_iri(iri)} ;" for iri, _path, _constraints in props]
    lines.append(f"    sh:severity {severity} .")
    for iri, path_iri, constraints in props:
        lines += ["", turtle_iri(iri), "    a sh:PropertyShape ;", f"    sh:path {turtle_iri(path_iri)} ;"]
        lines += [f"    {c} ;" for c in constraints]
        lines.append(f"    sh:severity {severity} .")
    return "\n".join(lines)


def shapes_ttl(cfg) -> tuple[str, list[str]]:
    """SHACL shapes graph for every shape that has a SHACL form, and the ids of the shapes left out."""
    blocks, skipped = [], []
    for shape in cfg.shapes:
        reason = no_shacl_reason(shape)
        if reason:
            skipped.append(shape.id)
            blocks.append(f"# {shape.id}: no SHACL form ({reason}); checked by its SQL view only")
        else:
            blocks.append(_shape_ttl(cfg, shape))
    header = [
        "# semantic-crosscheck: SHACL shapes generated from crosscheck.toml (edit the configuration, not this file).",
        "# Same semantics as the views v_shape_<id>: NULL is no triple (no violation); a referenced key that is not a",
        "# row of the class entity is a value node without rdf:type (sh:class violation).",
        f"@prefix sh: {turtle_iri(SH)} .",
        f"@prefix xsd: {turtle_iri(XSD)} .",
    ]
    return "\n".join(header) + "\n\n" + "\n\n".join(blocks) + ("\n" if blocks else ""), skipped


def pair_shapes_ttl(cfg, pairs: list) -> str:
    """The shapes graph of the property-pair comparison (verify.shacl_pairs): for each shape with a pair_form, a
    node shape on the target class with one property shape (naming.rdf_pair_shape) whose path and compared property
    are the columns' value predicates (naming.rdf_value_predicate, literals only). Generated at reason time and
    never written to shapes.ttl, so build's shapes.ttl and its comparison in reason do not change."""
    blocks = []
    for shape in pairs:
        path, param, other = pair_constraint(shape)
        severity = "sh:Warning" if shape.severity == "warning" else "sh:Violation"
        prop = turtle_iri(naming.rdf_pair_shape(cfg, shape.id))
        blocks.append("\n".join([
            turtle_iri(naming.rdf_node_shape(cfg, shape.id)),
            "    a sh:NodeShape ;",
            f"    sh:targetClass {turtle_iri(naming.rdf_entity_class(cfg, shape.target))} ;",
            f"    sh:property {prop} ;",
            f"    sh:severity {severity} .",
            "",
            prop,
            "    a sh:PropertyShape ;",
            f"    sh:path {turtle_iri(naming.rdf_value_predicate(cfg, shape.target, path))} ;",
            f"    sh:{param} {turtle_iri(naming.rdf_value_predicate(cfg, shape.target, other))} ;",
            f"    sh:severity {severity} .",
        ]))
    header = [
        "# semantic-crosscheck: property-pair shapes generated by reason from sql_violation predicates t.<a> <op> t.<b>",
        "# (verify.shacl_pairs); not part of build/shapes.ttl. NULL is no triple (no pair, no violation).",
        f"@prefix sh: {turtle_iri(SH)} .",
    ]
    return "\n".join(header) + "\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")
