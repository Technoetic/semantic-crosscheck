"""Unit tests of shapes.py on the demo configuration (no database).

Besides the generated SQL, Turtle and review DDL, one test checks the contract with the reason stage: on rows
exported as RDF with the naming.rdf_* IRIs, pySHACL must report exactly the rows the SQL views select (NULL is no
triple, a dangling reference is an untyped value node). export_graph() and focus_keys() are shared with
test_build.py, which runs the same comparison on the dirty demo in PostgreSQL.
"""

import copy
import logging
import unittest
from decimal import Decimal

import pgfixture
from semantic_crosscheck import canon, config, naming, shapes
from semantic_crosscheck.config import Shape
from semantic_crosscheck.result import EXIT_CONFIG, StageError

try:
    import rdflib
except ImportError:
    rdflib = None
try:
    import pyshacl
except ImportError:
    pyshacl = None

SH = "http://www.w3.org/ns/shacl#"
XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


def demo_config():
    return config.load(pgfixture.DEMO_CONFIG)


def with_shapes(cfg, *extra):
    c = copy.deepcopy(cfg)
    c.shapes.extend(extra)
    return c


def export_graph(cfg, rows: dict):
    """RDF of entity rows as the reason stage exports them: every row typed with its entity class and, for each
    shape with a SHACL form on its entity, the path value as the referenced row's IRI (class) or a decimal
    literal (bounds), and for a shape with class and bounds also the decimal on the value predicate; NULL gives
    no triple. rows: {entity: [row dict]}."""
    from rdflib import Literal, URIRef
    g = rdflib.Graph()
    rdf_type = URIRef(RDF_TYPE)
    for entity, entity_rows in rows.items():
        ent = cfg.entities[entity]
        paths = {s.path: s for s in cfg.shapes if s.target == entity and s.path and not shapes.no_shacl_reason(s)}
        both = {s.path for s in cfg.shapes if s.target == entity and s.class_ and shapes.has_bounds(s)
                and not shapes.no_shacl_reason(s)}
        cls = URIRef(naming.rdf_entity_class(cfg, entity))
        for row in entity_rows:
            node = URIRef(naming.rdf_node(cfg, entity, [row[k] for k in ent.key]))
            g.add((node, rdf_type, cls))
            for path, s in paths.items():
                value = row.get(path)
                if value is None:
                    continue
                obj = URIRef(naming.rdf_node(cfg, s.class_, [value])) if s.class_ else Literal(Decimal(value))
                g.add((node, URIRef(naming.rdf_predicate(cfg, entity, path)), obj))
                if path in both:   # class and bounds: the bounds read the same value as a number on <P>/value
                    g.add((node, URIRef(naming.rdf_value_predicate(cfg, entity, path)), Literal(Decimal(value))))
    return g


def focus_keys(cfg, data_graph, ttl: str) -> tuple[dict, dict]:
    """Run pySHACL without inference. Returns ({shape id: {canonical key tuple}}, {shape id: {severity IRI}})."""
    from rdflib import URIRef
    shapes_graph = rdflib.Graph().parse(data=ttl, format="turtle")
    _conforms, results, _text = pyshacl.validate(data_graph, shacl_graph=shapes_graph, inference="none")
    by_prop = {naming.rdf_property_shape(cfg, s.id): s for s in cfg.shapes}
    by_prop.update({naming.rdf_bound_shape(cfg, s.id): s for s in cfg.shapes})
    keys, severities = {}, {}
    for r in results.subjects(URIRef(RDF_TYPE), URIRef(SH + "ValidationResult")):
        s = by_prop[str(results.value(r, URIRef(SH + "sourceShape")))]
        focus = str(results.value(r, URIRef(SH + "focusNode")))
        keys.setdefault(s.id, set()).add(tuple(naming.rdf_key(cfg, s.target, focus)))
        severities.setdefault(s.id, set()).add(str(results.value(r, URIRef(SH + "resultSeverity"))))
    return keys, severities


def key(*values) -> tuple:
    return tuple(canon.canon(v) for v in values)


class ViolationSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = demo_config()
        cls.by_id = {s.id: s for s in cls.cfg.shapes}

    def test_class_shape_is_a_not_exists_on_the_class_key(self):
        sql = shapes.violation_sql(self.cfg, self.by_id["child_exists"])
        self.assertEqual(sql, 'SELECT t."parent_id", t."child_id", t."line_no"\n'
                              'FROM "sxc_demo"."bom_line" t\n'
                              'WHERE (t."child_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_demo"."part" c '
                              'WHERE c."part_id" = t."child_id"))')

    def test_bound_shapes_negate_the_valid_comparison(self):
        sql = shapes.violation_sql(self.cfg, self.by_id["qty_positive"])
        self.assertIn('WHERE (t."qty_per" IS NOT NULL AND NOT (t."qty_per" > 0))', sql)
        sql = shapes.violation_sql(self.cfg, self.by_id["stock_nonnegative"])
        self.assertTrue(sql.startswith('SELECT t."part_id", t."location_id"\nFROM "sxc_demo"."stock" t\n'))
        self.assertIn('NOT (t."qty" >= 0)', sql)

    def test_trusted_predicate_is_kept_on_its_own_lines(self):
        sql = shapes.violation_sql(self.cfg, self.by_id["open_le_total"])
        self.assertEqual(sql, 'SELECT t."order_id"\nFROM "sxc_demo"."sales_order" t\nWHERE (\nt.qty_open > t.qty_total\n)')
        commented = Shape(id="commented", target="sales_order", sql_violation="t.qty_open > t.qty_total -- note")
        sql = shapes.violation_sql(with_shapes(self.cfg, commented), commented)
        self.assertTrue(sql.endswith("WHERE (\nt.qty_open > t.qty_total -- note\n)"))

    def test_constraints_of_one_shape_are_ored(self):
        s = Shape(id="range", target="stock", path="qty", min_inclusive=Decimal("0"), max_inclusive=Decimal("1E+3"),
                  sql_violation="t.location_id = ''")
        sql = shapes.violation_sql(with_shapes(self.cfg, s), s)
        where = sql.split("WHERE ", 1)[1]
        self.assertEqual(where, '(t."qty" IS NOT NULL AND NOT (t."qty" >= 0))\n'
                                '   OR (t."qty" IS NOT NULL AND NOT (t."qty" <= 1000))\n'
                                "   OR (\nt.location_id = ''\n)")

    def test_bounds_are_plain_numerals(self):
        self.assertEqual(shapes.numeral(Decimal("1E+2")), "100")
        self.assertEqual(shapes.numeral(Decimal("-0.50")), "-0.50")
        self.assertEqual(shapes.numeral(Decimal("-0")), "0")
        self.assertEqual(shapes.numeral(Decimal("1E-7")), "0.0000001")
        self.assertEqual(shapes.numeral(0.1), "0.1")
        self.assertEqual(shapes.numeral(-3), "-3")
        for bad in (Decimal("NaN"), Decimal("Infinity"), float("-inf"), True, "1; DROP TABLE x", None):
            with self.assertRaises(ValueError):
                shapes.numeral(bad)

    def test_a_non_finite_bound_is_a_configuration_error(self):
        s = Shape(id="bad_bound", target="stock", path="qty", min_inclusive=Decimal("NaN"))
        cfg = with_shapes(self.cfg, s)
        for fn in (lambda: shapes.violation_sql(cfg, s), lambda: shapes.shapes_ttl(cfg)):
            with self.assertRaises(StageError) as e:
                fn()
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
            self.assertIn("shapes[4] bad_bound", str(e.exception))

    def test_kind_and_config_ref(self):
        self.assertEqual([shapes.kind(s) for s in self.cfg.shapes], ["class", "bound", "bound", "sql"])
        self.assertEqual(shapes.config_ref(self.cfg, self.by_id["stock_nonnegative"]), "shapes[2] stock_nonnegative")
        mixed = Shape(id="mixed", target="bom_line", path="child_id", class_="part", sql_violation="false")
        self.assertEqual(shapes.kind(mixed), "class+sql")


class ShapesTtlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = demo_config()
        cls.ttl, cls.skipped = shapes.shapes_ttl(cls.cfg)

    def test_shapes_without_a_shacl_form_are_listed(self):
        self.assertEqual(self.skipped, ["open_le_total"])
        self.assertIn("# open_le_total: no SHACL form (sql_violation)", self.ttl)
        extra = [Shape(id="class_and_bound", target="bom_line", path="line_no", class_="part", min_inclusive=Decimal(1)),
                 Shape(id="bound_and_sql", target="stock", path="qty", min_inclusive=Decimal(0), sql_violation="false"),
                 Shape(id="all_three", target="bom_line", path="child_id", class_="part", max_inclusive=Decimal(9),
                       sql_violation="false")]
        ttl, skipped = shapes.shapes_ttl(with_shapes(self.cfg, *extra))
        self.assertEqual(skipped, ["open_le_total", "bound_and_sql", "all_three"], "class with bounds has a SHACL form")
        self.assertEqual([shapes.no_shacl_reason(s) for s in extra], ["", "sql_violation", "sql_violation"])
        self.assertIn("# all_three: no SHACL form (sql_violation)", ttl)
        self.assertNotIn("class_and_bounds", ttl)

    @unittest.skipUnless(rdflib, "rdflib is not installed")
    def test_class_and_bounds_become_two_property_shapes(self):
        """One node shape, two property shapes: sh:class on the column's predicate, the bounds on its value
        predicate, each with its own IRI (pySHACL's sh:sourceShape) and the shape's severity."""
        from rdflib import Literal, URIRef
        s = Shape(id="line_ref", target="bom_line", path="line_no", class_="part", min_exclusive=Decimal(0),
                  max_inclusive=Decimal("9.5"), severity="warning")
        cfg = with_shapes(self.cfg, s)
        ttl, skipped = shapes.shapes_ttl(cfg)
        self.assertEqual(skipped, ["open_le_total"])
        g = rdflib.Graph().parse(data=ttl, format="turtle")
        sh = lambda name: URIRef(SH + name)  # noqa: E731
        node = URIRef(naming.rdf_node_shape(cfg, "line_ref"))
        prop, bound = URIRef(naming.rdf_property_shape(cfg, "line_ref")), URIRef(naming.rdf_bound_shape(cfg, "line_ref"))
        self.assertNotEqual(prop, bound)
        self.assertEqual(set(g.objects(node, sh("property"))), {prop, bound})
        self.assertEqual(g.value(node, sh("targetClass")), URIRef(naming.rdf_entity_class(cfg, "bom_line")))
        self.assertEqual(g.value(node, sh("severity")), sh("Warning"))
        pred = URIRef(naming.rdf_predicate(cfg, "bom_line", "line_no"))
        value = URIRef(naming.rdf_value_predicate(cfg, "bom_line", "line_no"))
        self.assertNotEqual(pred, value)
        decimal = lambda text: Literal(text, datatype=URIRef(XSD_DECIMAL))  # noqa: E731
        self.assertEqual(set(g.predicate_objects(prop)), {
            (URIRef(RDF_TYPE), sh("PropertyShape")), (sh("path"), pred),
            (sh("class"), URIRef(naming.rdf_entity_class(cfg, "part"))), (sh("severity"), sh("Warning"))})
        self.assertEqual(set(g.predicate_objects(bound)), {
            (URIRef(RDF_TYPE), sh("PropertyShape")), (sh("path"), value), (sh("minExclusive"), decimal("0")),
            (sh("maxInclusive"), decimal("9.5")), (sh("severity"), sh("Warning"))})
        # a shape with only one of the two keeps its one property shape, as before
        for sid in ("child_exists", "qty_positive", "stock_nonnegative"):
            self.assertEqual(len(set(g.objects(URIRef(naming.rdf_node_shape(cfg, sid)), sh("property")))), 1, sid)
            self.assertNotIn(f"<{naming.rdf_bound_shape(cfg, sid)}>", ttl)

    def test_text_names_every_shape_iri(self):
        for sid in ("child_exists", "qty_positive", "stock_nonnegative"):
            self.assertIn(f"<{naming.rdf_node_shape(self.cfg, sid)}>", self.ttl)
            self.assertIn(f"sh:property <{naming.rdf_property_shape(self.cfg, sid)}> ;", self.ttl)
        self.assertNotIn("open_le_total/p", self.ttl)
        self.assertIn('sh:minExclusive "0"^^xsd:decimal ;', self.ttl)

    @unittest.skipUnless(rdflib, "rdflib is not installed")
    def test_turtle_parses_to_the_configured_shapes(self):
        from rdflib import Literal, URIRef
        g = rdflib.Graph().parse(data=self.ttl, format="turtle")
        sh = lambda name: URIRef(SH + name)  # noqa: E731
        nodes = set(g.subjects(URIRef(RDF_TYPE), sh("NodeShape")))
        self.assertEqual(nodes, {URIRef(naming.rdf_node_shape(self.cfg, s)) for s in ("child_exists", "qty_positive", "stock_nonnegative")})
        expect = {
            "child_exists": ("bom_line", "child_id", sh("class"), URIRef(naming.rdf_entity_class(self.cfg, "part")), "Violation"),
            "qty_positive": ("bom_line", "qty_per", sh("minExclusive"), Literal("0", datatype=URIRef(XSD_DECIMAL)), "Violation"),
            "stock_nonnegative": ("stock", "qty", sh("minInclusive"), Literal("0", datatype=URIRef(XSD_DECIMAL)), "Warning"),
        }
        for sid, (target, path, param, value, severity) in expect.items():
            node, prop = URIRef(naming.rdf_node_shape(self.cfg, sid)), URIRef(naming.rdf_property_shape(self.cfg, sid))
            self.assertEqual(g.value(node, sh("targetClass")), URIRef(naming.rdf_entity_class(self.cfg, target)))
            self.assertEqual(g.value(node, sh("property")), prop)
            self.assertEqual(g.value(prop, sh("path")), URIRef(naming.rdf_predicate(self.cfg, target, path)))
            self.assertEqual(g.value(prop, param), value)
            self.assertEqual(g.value(prop, sh("severity")), sh(severity))

    @unittest.skipUnless(rdflib, "rdflib is not installed")
    def test_unusual_iri_characters_round_trip(self):
        from rdflib import URIRef
        term_log = logging.getLogger("rdflib.term")
        self.addCleanup(term_log.setLevel, term_log.level)
        term_log.setLevel(logging.ERROR)   # rdflib warns that such IRIs are unusual; the exact round trip is the point
        cfg = copy.deepcopy(self.cfg)
        cfg.ontology.iri = 'https://example.org/a b"{x}|^`\\' + chr(1) + chr(0xE9) + "#"
        ttl, _ = shapes.shapes_ttl(cfg)
        g = rdflib.Graph().parse(data=ttl, format="turtle")
        self.assertIn((URIRef(naming.rdf_node_shape(cfg, "qty_positive")), URIRef(SH + "targetClass"),
                       URIRef(naming.rdf_entity_class(cfg, "bom_line"))), g)


class RdfNamingTest(unittest.TestCase):
    """The value predicate and the bound property shape never share an IRI with any other generated name: the
    identifiers they are made of never contain "." or "/", which separate the parts."""

    IDS = ("a", "b", "p", "shape", "value", "a_b", "shape_p", "p_value", "x1", "_")

    def test_generated_iris_never_collide(self):
        cfg = demo_config()
        seen = {}
        names = [("entity_class", (e,), naming.rdf_entity_class(cfg, e)) for e in self.IDS]
        names += [("node_shape", (i,), naming.rdf_node_shape(cfg, i)) for i in self.IDS]
        names += [("property_shape", (i,), naming.rdf_property_shape(cfg, i)) for i in self.IDS]
        names += [("bound_shape", (i,), naming.rdf_bound_shape(cfg, i)) for i in self.IDS]
        names += [("predicate", (e, c), naming.rdf_predicate(cfg, e, c)) for e in self.IDS for c in self.IDS]
        names += [("value_predicate", (e, c), naming.rdf_value_predicate(cfg, e, c)) for e in self.IDS for c in self.IDS]
        names += [("node", (e, k), naming.rdf_node(cfg, e, [k])) for e in self.IDS for k in self.IDS + ("p/b", "x.value")]
        for kind, args, iri in names:
            with self.subTest(kind=kind, args=args):
                self.assertNotIn(iri, seen, f"{kind}{args} collides with {seen.get(iri)}")
                seen[iri] = (kind, args)
        self.assertEqual(naming.rdf_bound_shape(cfg, "s"), naming.rdf_node_shape(cfg, "s") + "/b")
        self.assertEqual(naming.rdf_value_predicate(cfg, "e", "c"), naming.rdf_predicate(cfg, "e", "c") + "/value")
        for bad in ("a.b", "a/b", "A"):
            with self.assertRaises(ValueError):
                naming.rdf_value_predicate(cfg, "e", bad)
            with self.assertRaises(ValueError):
                naming.rdf_bound_shape(cfg, bad)


class TurtleEscapingTest(unittest.TestCase):
    TRICKY = 'q"b\\s\nl\rr\tt' + chr(1) + chr(0x7F) + chr(0xE9) + chr(0x1F600)

    def test_literal_escapes(self):
        bs = chr(92)
        self.assertEqual(shapes.turtle_literal('a"b'), '"a' + bs + '"b"')
        self.assertEqual(shapes.turtle_literal(bs), '"' + bs + bs + '"')
        self.assertEqual(shapes.turtle_literal("x\ny"), '"x' + bs + 'ny"')
        self.assertEqual(shapes.turtle_literal(chr(1)), '"' + bs + "u0001" + '"')
        self.assertEqual(shapes.turtle_literal(chr(0xE9)), '"' + chr(0xE9) + '"')

    def test_iri_escapes(self):
        bs = chr(92)
        self.assertEqual(shapes.turtle_iri("https://example.org/x#a"), "<https://example.org/x#a>")
        self.assertEqual(shapes.turtle_iri("http://e.org/a b"), "<http://e.org/a" + bs + "u0020b>")
        self.assertEqual(shapes.turtle_iri("http://e.org/<>"), "<http://e.org/" + bs + "u003C" + bs + "u003E>")
        self.assertEqual(shapes.turtle_iri("http://e.org/" + chr(0x10000)), "<http://e.org/" + chr(0x10000) + ">")

    @unittest.skipUnless(rdflib, "rdflib is not installed")
    def test_rdflib_reads_back_the_exact_text(self):
        g = rdflib.Graph().parse(data=f"<http://e.org/s> <http://e.org/p> {shapes.turtle_literal(self.TRICKY)} .", format="turtle")
        self.assertEqual([str(o) for o in g.objects()], [self.TRICKY])


class FkReviewSqlTest(unittest.TestCase):
    def test_review_only_shapes_become_not_valid_foreign_keys(self):
        sql = shapes.fk_review_sql(demo_config())
        self.assertIn("never executes this file", sql)
        self.assertEqual(sql.count("ADD CONSTRAINT"), 1)
        self.assertIn('ALTER TABLE "sxc_demo"."bom_line" ADD CONSTRAINT "sxc_fk_child_exists" FOREIGN KEY ("child_id")\n'
                      '  REFERENCES "sxc_demo"."part" ("part_id") NOT VALID;', sql)
        self.assertIn('-- ALTER TABLE "sxc_demo"."bom_line" VALIDATE CONSTRAINT "sxc_fk_child_exists";', sql)
        for line in sql.splitlines():
            self.assertTrue(line == "" or line.startswith("--") or line.startswith(("ALTER TABLE", "  REFERENCES")), line)

    def test_no_review_only_shape_gives_only_the_header(self):
        cfg = demo_config()
        for s in cfg.shapes:
            s.fk_ddl = "none"
        sql = shapes.fk_review_sql(cfg)
        self.assertNotIn("ALTER TABLE", sql.replace("-- ALTER TABLE", ""))
        self.assertTrue(all(line.startswith("--") or not line for line in sql.splitlines()))


@unittest.skipUnless(rdflib and pyshacl, "rdflib and pyshacl are not installed")
class ShaclEqualsSqlTest(unittest.TestCase):
    """Hand-made rows covering each rule's edge: NULL values, a dangling reference, bounds at the limit."""

    PARTS = [{"part_id": p} for p in ("P1", "P2", "007")]
    LINES = [
        {"parent_id": "P1", "child_id": "P2", "line_no": 1, "qty_per": "2"},
        {"parent_id": "P1", "child_id": "P9", "line_no": 1, "qty_per": "1"},          # dangling reference
        {"parent_id": "P1", "child_id": "P2", "line_no": 2, "qty_per": "0"},          # not > 0
        {"parent_id": "P2", "child_id": "007", "line_no": 1, "qty_per": "-0.5"},      # not > 0
        {"parent_id": "P2", "child_id": None, "line_no": 1, "qty_per": "1"},          # NULL reference: no violation
        {"parent_id": "P2", "child_id": "P1", "line_no": 3, "qty_per": None},         # NULL quantity: no violation
        {"parent_id": "P2", "child_id": "P8", "line_no": 2, "qty_per": "0.000001"},   # dangling reference
    ]
    STOCK = [
        {"part_id": "P1", "location_id": "MAIN", "qty": "5"},
        {"part_id": "P1", "location_id": "SUB", "qty": "0"},                          # >= 0 holds
        {"part_id": "P2", "location_id": "MAIN", "qty": "-3"},
        {"part_id": "P2", "location_id": "SUB", "qty": None},
    ]

    def test_focus_nodes_are_the_rows_the_views_select(self):
        cfg = demo_config()
        ttl, _ = shapes.shapes_ttl(cfg)
        graph = export_graph(cfg, {"part": self.PARTS, "bom_line": self.LINES, "stock": self.STOCK})
        keys, severities = focus_keys(cfg, graph, ttl)
        self.assertEqual(keys, {
            "child_exists": {key("P1", "P9", 1), key("P2", "P8", 2)},
            "qty_positive": {key("P1", "P2", 2), key("P2", "007", 1)},
            "stock_nonnegative": {key("P2", "MAIN")},
        })
        self.assertEqual(severities["stock_nonnegative"], {SH + "Warning"})
        self.assertEqual(severities["child_exists"], {SH + "Violation"})

    def test_class_and_bounds_violate_like_the_ored_view(self):
        """A shape with class and bounds: its view ORs the dangling reference and the bound, whether or not the
        reference exists. Five lines cover dangling and out of range, dangling and in range, existing and out of
        range, existing and in range, and NULL; the expected keys follow from the rows, not from the code."""
        s = Shape(id="line_ref", target="bom_line", path="child_id", class_="part", max_inclusive=Decimal(5))
        cfg = with_shapes(demo_config(), s)
        parts = [{"part_id": p} for p in (7, 1)]
        lines = [{"parent_id": "L1", "child_id": 9, "line_no": 1, "qty_per": "1"},      # dangling, > 5
                 {"parent_id": "L2", "child_id": 4, "line_no": 1, "qty_per": "1"},      # dangling, in range
                 {"parent_id": "L3", "child_id": 7, "line_no": 1, "qty_per": "1"},      # existing, > 5
                 {"parent_id": "L4", "child_id": 1, "line_no": 1, "qty_per": "1"},      # existing, in range
                 {"parent_id": "L5", "child_id": None, "line_no": 1, "qty_per": "1"}]   # NULL: no violation
        ttl = shapes.shapes_ttl(cfg)[0]
        graph = export_graph(cfg, {"part": parts, "bom_line": lines})
        keys, severities = focus_keys(cfg, graph, ttl)
        self.assertEqual(keys["line_ref"], {key("L1", 9, 1), key("L2", 4, 1), key("L3", 7, 1)})
        self.assertEqual(keys["child_exists"], {key("L1", 9, 1), key("L2", 4, 1)}, "the class-only shape is unchanged")
        self.assertEqual(severities["line_ref"], {SH + "Violation"})
        # the value triples carry the bound: without them the bound sees no value and only the class part remains
        value = rdflib.URIRef(naming.rdf_value_predicate(cfg, "bom_line", "child_id"))
        self.assertEqual(len(list(graph.triples((None, value, None)))), 4)
        without = rdflib.Graph()
        for triple in graph:
            if triple[1] != value:
                without.add(triple)
        keys, _ = focus_keys(cfg, without, ttl)
        self.assertEqual(keys["line_ref"], {key("L1", 9, 1), key("L2", 4, 1)})


if __name__ == "__main__":
    unittest.main()
