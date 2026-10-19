"""Tests of the reason stage: SHACL against the SQL views, OWL consistency and V5 (OWL against SQL).

Unit tests need no database: the reversible RDF row keys (naming.rdf_node / rdf_key, also through rdflib and pySHACL
results), the literal forms of bound values, which shapes are compared, the SQL definitions of the defined classes,
the stage's prerequisites and, when owlready2 and Java are available, HermiT on a small hand-made hierarchy.
Integration tests (skipped unless SXC_TEST_ADMIN_DSN is set; the OWL parts also need owlready2 and Java) run build and
then reason on the clean and the dirty demo, each class in its own throw-away database prefixed sxc_t_reason, and
compare with the demo's independent Python numbers (fx.data["expected"]).
"""

import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import canon, config, db, ident, naming, owl, shacl_check, shapes, stage_build, stage_reason
from semantic_crosscheck.config import Defined, Shape
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, EXIT_FAIL, EXIT_PASS, Result, StageError

try:
    import rdflib
except ImportError:
    rdflib = None
try:
    import pyshacl
except ImportError:
    pyshacl = None
try:
    import owlready2
except ImportError:
    owlready2 = None

PREFIX = "sxc_t_reason"
JAVA = owl.java_executable()
HAVE_SHACL = bool(rdflib and pyshacl)
HAVE_OWL = bool(owlready2 and JAVA)
ROW_VALUE_MARKERS = ("P-0", "SO-0", "P-ORPHAN")   # demo key prefixes: never allowed in results or console output
XSD = shapes.XSD


def demo_config():
    return config.load(pgfixture.DEMO_CONFIG)


def with_shapes(cfg, *extra):
    c = copy.deepcopy(cfg)
    c.shapes.extend(extra)
    return c


def get_check(res, check_id: str):
    found = [c for c in res.checks if c.id == check_id]
    if len(found) != 1:
        raise AssertionError(f"{check_id}: {len(found)} checks")
    return found[0]


def key(*values) -> tuple:
    return tuple(canon.key(v) for v in values)


def cli(*argv) -> tuple[int, str]:
    """sxc.main with the console output captured."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = sxc.main(list(argv))
    return code, buf.getvalue()


# ---- unit tests (no database) ---------------------------------------------------------------------------
class RdfKeyTest(unittest.TestCase):
    KEYS = [["P-000001"], ["a b/c#d%e?f&g=h"], ["e" + chr(0x301), chr(0xE9), chr(0x1F600)],
            ['quote"', "back" + chr(92) + "slash", "tab\tline\n"], [1, Decimal("2.50"), "007"], [True, None], ["1.50", "-0"],
            ["1.10", "1e3", "true"]]

    def setUp(self):
        self.cfg = demo_config()

    def test_keys_round_trip_with_text_exact(self):
        for k in self.KEYS:
            iri = naming.rdf_node(self.cfg, "bom_line", k)
            self.assertEqual(naming.rdf_key(self.cfg, "bom_line", iri), [canon.key(v) for v in k], k)
            suffix = iri[len(naming.rdf_entity_class(self.cfg, "bom_line")) + 1:]
            self.assertRegex(suffix, r"^[A-Za-z0-9%._~-]+$")   # only IRI-unreserved characters and escapes
        self.assertEqual(naming.rdf_key(self.cfg, "part", naming.rdf_node(self.cfg, "part", ["1.10"])), ["1.10"])
        self.assertEqual(naming.rdf_key(self.cfg, "part", naming.rdf_node(self.cfg, "part", [Decimal("2.50")])), ["2.5"])

    def test_two_text_keys_never_share_an_iri(self):
        for a, b in (("1.10", "1.1"), ("1e3", "1000"), ("true", True), ("-0", "0"), ("2.50", "2.5")):
            self.assertNotEqual(naming.rdf_node(self.cfg, "part", [a]), naming.rdf_node(self.cfg, "part", [b]), (a, b))
        self.assertEqual(naming.rdf_node(self.cfg, "part", [Decimal("2.50")]), naming.rdf_node(self.cfg, "part", [Decimal("2.5")]),
                         "numbers keep their canonical form")

    def test_an_iri_of_another_entity_is_rejected(self):
        with self.assertRaises(ValueError):
            naming.rdf_key(self.cfg, "part", naming.rdf_node(self.cfg, "stock", ["P-000001", "MAIN"]))

    @unittest.skipUnless(rdflib, "rdflib is not installed")
    def test_keys_survive_rdflib_serialisation(self):
        from rdflib import URIRef
        g = rdflib.Graph()
        for k in self.KEYS:
            g.add((URIRef(naming.rdf_node(self.cfg, "bom_line", k)), URIRef(shacl_check.RDF_TYPE),
                   URIRef(naming.rdf_entity_class(self.cfg, "bom_line"))))
        for fmt in ("nt", "turtle"):
            back = rdflib.Graph().parse(data=g.serialize(format=fmt), format=fmt)
            keys = {tuple(naming.rdf_key(self.cfg, "bom_line", str(s))) for s in back.subjects()}
            self.assertEqual(keys, {tuple(canon.key(v) for v in k) for k in self.KEYS}, fmt)


class LiteralTest(unittest.TestCase):
    @unittest.skipUnless(rdflib, "rdflib is not installed")
    def test_bound_values_become_typed_literals(self):
        cases = [
            (5, "5", XSD + "integer"), (-3, "-3", XSD + "integer"),
            (Decimal("2.500"), "2.500", XSD + "decimal"), (Decimal("-0.0"), "0", XSD + "decimal"),
            (Decimal("1E+3"), "1000", XSD + "decimal"), (0.1, "0.1", XSD + "decimal"), (1e-07, "0.0000001", XSD + "decimal"),
            (Decimal("NaN"), "NaN", XSD + "double"), (Decimal("-Infinity"), "-INF", XSD + "double"),
            (float("inf"), "INF", XSD + "double"),
            (True, "true", XSD + "boolean"), (date(2026, 1, 2), "2026-01-02", XSD + "date"),
        ]
        for value, lexical, datatype in cases:
            lit = shacl_check.literal(value)
            self.assertEqual((str(lit), str(lit.datatype)), (lexical, datatype), repr(value))
        self.assertIsNone(shacl_check.literal("12").datatype)   # text is never compared as a number


class PlanTest(unittest.TestCase):
    def test_demo_plan(self):
        planned, skipped = shacl_check.plan(demo_config())
        self.assertEqual([s.id for s in planned], ["child_exists", "qty_positive", "stock_nonnegative"])
        self.assertEqual(skipped, {"open_le_total": "sql_violation"})

    def test_a_column_shared_by_value_kinds_is_split_into_groups(self):
        cfg = with_shapes(demo_config(),
                          Shape(id="child_small", target="bom_line", path="child_id", max_inclusive=Decimal(9)),
                          Shape(id="qty_capped", target="bom_line", path="qty_per", max_inclusive=Decimal(1000)),
                          Shape(id="child_is_stock", target="bom_line", path="child_id", class_="stock"),
                          Shape(id="class_and_bound", target="stock", path="qty", class_="part", min_inclusive=Decimal(0)))
        planned, skipped = shacl_check.plan(cfg)
        self.assertEqual([s.id for s in planned], ["child_exists", "qty_positive", "stock_nonnegative", "child_small",
                                                   "qty_capped", "child_is_stock", "class_and_bound"])
        self.assertEqual(skipped, {"open_le_total": "sql_violation"})
        grouped = shacl_check.groups(planned)
        # child_small needs literals on child_id, which "main" already carries as part IRIs; child_is_stock needs
        # stock IRIs there, which neither "main" nor "extra:1" (literals) allows; qty_capped fits "main";
        # class_and_bound needs part IRIs on stock.qty (its bounds read their own value predicate), which "main"
        # carries as literals, so it joins "extra:1", where stock.qty carries nothing yet
        self.assertEqual([(name, [s.id for s in members]) for name, members in grouped], [
            ("main", ["child_exists", "qty_positive", "stock_nonnegative", "qty_capped"]),
            ("extra:1", ["child_small", "class_and_bound"]),
            ("extra:2", ["child_is_stock"]),
        ])
        for name, members in grouped:   # each group carries one kind of value per column: the single-kind export
            kinds = {}
            for s in members:
                kinds.setdefault((s.target, s.path), set()).add(shacl_check.value_kind(s))
            self.assertEqual({column: len(k) for column, k in kinds.items()}, dict.fromkeys(kinds, 1), name)
        self.assertEqual([p for p, _s in shacl_check.paths(grouped[0][1], "bom_line")], ["child_id", "qty_per"])
        self.assertEqual([p for p, _s in shacl_check.paths(grouped[1][1], "bom_line")], ["child_id"])
        self.assertEqual([shacl_check.value_kind(s) for _n, members in grouped for s in members if s.path == "child_id"],
                         ["class:part", "bound", "class:stock"])

    def test_class_and_bounds_is_compared_and_sql_violation_is_not(self):
        """A shape with class and bounds has a SHACL form (two property shapes) and needs row IRIs on its column;
        any shape with sql_violation stays SQL-only, also when it has class and bounds as well."""
        cfg = with_shapes(demo_config(),
                          Shape(id="child_capped", target="bom_line", path="child_id", class_="part", max_inclusive=Decimal(9)),
                          Shape(id="all_three", target="bom_line", path="child_id", class_="part",
                                min_inclusive=Decimal(0), sql_violation="false"))
        planned, skipped = shacl_check.plan(cfg)
        self.assertEqual([s.id for s in planned], ["child_exists", "qty_positive", "stock_nonnegative", "child_capped"])
        self.assertEqual(skipped, {"open_le_total": "sql_violation", "all_three": "sql_violation"})
        self.assertNotIn("class_and_bounds", skipped.values())
        self.assertEqual(shacl_check.value_kind(planned[-1]), "class:part")
        self.assertEqual([(n, [s.id for s in m]) for n, m in shacl_check.groups(planned)],
                         [("main", ["child_exists", "qty_positive", "stock_nonnegative", "child_capped"])],
                         "the same row IRIs on child_id as child_exists: no new group")
        self.assertEqual([p for p, _s in shacl_check.paths(planned, "bom_line")], ["child_id", "qty_per"])
        self.assertEqual(shacl_check.value_paths(planned, "bom_line"), {"child_id"})
        self.assertEqual(shacl_check.value_paths(planned, "stock"), set())

    def test_a_configuration_without_collisions_has_one_group(self):
        """Without a column that needs two kinds of value every shape stays in "main": one graph, as in 0.3.0."""
        self.assertEqual(shacl_check.groups([]), [])
        cfg = demo_config()
        planned, _ = shacl_check.plan(cfg)
        self.assertEqual([(n, [s.id for s in m]) for n, m in shacl_check.groups(planned)],
                         [("main", ["child_exists", "qty_positive", "stock_nonnegative"])])
        more = with_shapes(cfg, Shape(id="qty_capped", target="bom_line", path="qty_per", max_inclusive=Decimal(9)),
                           Shape(id="child_known", target="bom_line", path="child_id", class_="part"))
        self.assertEqual([(n, len(m)) for n, m in shacl_check.groups(shacl_check.plan(more)[0])], [("main", 5)],
                         "shapes needing the same kind of value on one column share it")

    def test_only_a_colliding_shape_opens_a_group_and_nothing_is_skipped(self):
        cfg = with_shapes(demo_config(), Shape(id="child_small", target="bom_line", path="child_id",
                                               max_inclusive=Decimal(9)))
        planned, skipped = shacl_check.plan(cfg)
        self.assertNotIn("shared_path", skipped.values())
        self.assertEqual([(n, [s.id for s in m]) for n, m in shacl_check.groups(planned)],
                         [("main", ["child_exists", "qty_positive", "stock_nonnegative"]), ("extra:1", ["child_small"])])
        # labels are numbered by the order groups are opened, whichever column collides (here stock.qty)
        swapped = with_shapes(demo_config(), Shape(id="zz_small", target="stock", path="qty", class_="part"))
        self.assertEqual([n for n, _m in shacl_check.groups(shacl_check.plan(swapped)[0])], ["main", "extra:1"])


@unittest.skipUnless(HAVE_SHACL, "rdflib and pyshacl are not installed")
class GroupIsolationTest(unittest.TestCase):
    """shacl_check.run with the snapshot reads replaced: whatever validating one group raises leaves only that
    group's shapes without an answer (the exception type, never its message), and errors raised before validation
    (the row cap, the database) are not caught per group."""

    def setUp(self):
        self.cfg = with_shapes(demo_config(), Shape(id="child_small", target="bom_line", path="child_id",
                                                    max_inclusive=Decimal(9)))
        grouped = shacl_check.groups(shacl_check.plan(self.cfg)[0])
        self.assertEqual([n for n, _m in grouped], ["main", "extra:1"])
        self.fetched = ({name: ({}, {}) for name, _m in grouped}, {name: 1 for name, _m in grouped})
        self.conn = types.SimpleNamespace(run=lambda *_a, **_k: None)

    def run_shacl(self, validate=None, fetch=None, sql_keys=None):
        with mock.patch.object(shacl_check, "_limited_fetch", **(fetch or {"return_value": self.fetched})), \
             mock.patch.object(shacl_check, "sql_keys", **(sql_keys or {"return_value": set()})), \
             mock.patch.object(shacl_check, "validate", side_effect=validate or shacl_check.validate):
            return shacl_check.run(self.conn, self.cfg, shapes.shapes_ttl(self.cfg)[0])

    def test_any_exception_from_one_group_errors_only_that_group(self):
        real, seen = shacl_check.validate, []

        def validate(graph, shacl):
            seen.append(graph)
            if len(seen) == 2:   # the extra:1 graph
                raise RuntimeError("P-000123 quoted in a message")
            return real(graph, shacl)

        got = self.run_shacl(validate)
        self.assertEqual((got["child_small"]["validation_error"], got["child_small"]["shacl_keys"],
                          got["child_small"]["group"]), ("RuntimeError", None, "extra:1"))
        for sid in ("child_exists", "qty_positive", "stock_nonnegative"):
            self.assertNotIn("validation_error", got[sid], sid)
            self.assertEqual((got[sid]["shacl_keys"], got[sid]["group"]), (set(), "main"), sid)
        self.assertNotIn("P-000123", repr(got))

    def test_errors_before_validation_are_not_caught_per_group(self):
        cap = shacl_check.TooManyRows(10, 9)
        with self.assertRaises(shacl_check.TooManyRows):
            self.run_shacl(fetch={"side_effect": cap})

        class DatabaseError(Exception):
            pass
        with self.assertRaises(DatabaseError):
            self.run_shacl(sql_keys={"side_effect": DatabaseError({"C": "57014"})})


@unittest.skipUnless(HAVE_SHACL, "rdflib and pyshacl are not installed")
class ShaclOnRowsTest(unittest.TestCase):
    """Hand-made rows through build_graph, pySHACL and focus_keys: NULLs, a dangling reference, bounds at the limit,
    an integer column against a decimal bound and keys with characters that need escaping."""

    ODD = 'odd "key"/' + chr(0xE9)

    def test_focus_nodes_map_back_to_the_violating_keys(self):
        cfg = with_shapes(demo_config(), Shape(id="line_positive", target="bom_line", path="line_no", min_exclusive=Decimal(0)))
        planned, _ = shacl_check.plan(cfg)
        self.assertEqual([p for p, _s in shacl_check.paths(planned, "bom_line")], ["child_id", "qty_per", "line_no"])
        lines = [   # parent_id, child_id, line_no | paths: child_id, qty_per, line_no
            ["P1", "P2", 1, "P2", Decimal("2"), 1],
            ["P1", "P9", 1, "P9", Decimal("1"), 1],                 # dangling reference
            [self.ODD, "P2", 2, "P2", Decimal("0"), 2],             # qty not > 0
            ["P2", "007", 0, "007", Decimal("-0.5"), 0],            # qty not > 0, line_no not > 0
            ["P2", None, 1, None, Decimal("1"), 1],                 # NULL reference: no violation
            ["P2", "P1", 3, "P1", None, -1],                        # NULL quantity: no violation; line_no not > 0
            ["P2", self.ODD, 2, self.ODD, Decimal("0.000001"), 2],
        ]
        stock = [["P1", "MAIN", Decimal("5")], ["P1", "SUB", Decimal("0")], ["P2", "MAIN", Decimal("-3")], ["P2", "SUB", None]]
        graph = shacl_check.build_graph(cfg, planned, {"bom_line": lines, "stock": stock}, {"part": ["P1", "P2", "007", self.ODD]})
        found = shacl_check.focus_keys(cfg, planned, shacl_check.validate(graph, shapes.shapes_ttl(cfg)[0]))
        self.assertEqual(found, {
            "child_exists": {key("P1", "P9", 1)},
            "qty_positive": {key(self.ODD, "P2", 2), key("P2", "007", 0)},
            "stock_nonnegative": {key("P2", "MAIN")},
            "line_positive": {key("P2", "007", 0), key("P2", "P1", 3)},
        })

    def test_each_value_group_validates_its_own_graph(self):
        """line_no carries a class (line) and a bound: one graph per group, each with one kind of value."""
        cfg = with_shapes(demo_config(), Shape(id="line_positive", target="bom_line", path="line_no", min_exclusive=Decimal(0)),
                          Shape(id="line_known", target="bom_line", path="line_no", class_="line"))
        cfg.entities["line"] = config.Entity("line", "sxc_demo.line", ["line_no"])
        planned, skipped = shacl_check.plan(cfg)
        self.assertEqual(skipped, {"open_le_total": "sql_violation"})
        lines = [   # parent_id, child_id, line_no | paths of the group
            ["P1", "P2", 1], ["P1", "P3", 2], ["P2", "P3", 0], ["P2", "P1", 7], ["P3", "P1", None],
        ]
        ttl = shapes.shapes_ttl(cfg)[0]
        found, grouped = {}, shacl_check.groups(planned)
        self.assertEqual([(n, [s.id for s in m if s.path == "line_no"]) for n, m in grouped],
                         [("main", ["line_positive"]), ("extra:1", ["line_known"])])
        for name, members in grouped:
            is_line = any(s.class_ == "line" for s in members)
            rows = {"bom_line": [r + [r[2]] * len(shacl_check.paths(members, "bom_line")) for r in lines]}
            keys = {"line": [1, 2, 3]} if is_line else {}
            graph = shacl_check.build_graph(cfg, members, rows, keys)
            values = set(graph.objects(None, rdflib.URIRef(naming.rdf_predicate(cfg, "bom_line", "line_no"))))
            self.assertEqual({type(v) for v in values}, {rdflib.URIRef if is_line else rdflib.Literal}, name)
            got = shacl_check.focus_keys(cfg, members, shacl_check.validate(graph, ttl))
            found.update({sid: got[sid] for sid in ("line_positive", "line_known") if sid in got})
        self.assertEqual(found, {"line_positive": {key("P2", "P3", 0)},
                                 "line_known": {key("P2", "P3", 0), key("P2", "P1", 7)}})


@unittest.skipUnless(HAVE_SHACL, "rdflib and pyshacl are not installed")
class ClassAndBoundsOnRowsTest(unittest.TestCase):
    """A shape with class and bounds (cb_ref) on cb_item.code next to a class-only shape (cb_known, same value group)
    and a bound-only shape (cb_small, its own group). The rows cover dangling and out of range (1), dangling and in
    range (2), existing and out of range (3), existing and in range (4) and NULL (5); the expected keys follow from
    the rows: cb_ref's view ORs the dangling reference with the bound, whether or not the reference exists."""

    ITEMS = [[1, 99], [2, 4], [3, 10], [4, 1], [5, None]]
    CODES = [1, 10]
    EXPECTED = {"cb_ref": {key(1), key(2), key(3)}, "cb_known": {key(1), key(2)}, "cb_small": {key(1)}}

    @classmethod
    def config(cls):
        cfg = with_shapes(demo_config(),
                          Shape(id="cb_ref", target="cb_item", path="code", class_="cb_code", max_inclusive=Decimal(5)),
                          Shape(id="cb_known", target="cb_item", path="code", class_="cb_code"),
                          Shape(id="cb_small", target="cb_item", path="code", max_inclusive=Decimal(50)))
        cfg.entities["cb_code"] = config.Entity("cb_code", "sxc_demo.cb_code", ["code"])
        cfg.entities["cb_item"] = config.Entity("cb_item", "sxc_demo.cb_item", ["item_id"])
        return cfg

    def setUp(self):
        self.cfg = self.config()
        self.planned, _ = shacl_check.plan(self.cfg)
        self.grouped = dict(shacl_check.groups(self.planned))
        self.ttl = shapes.shapes_ttl(self.cfg)[0]
        self.pred = rdflib.URIRef(naming.rdf_predicate(self.cfg, "cb_item", "code"))
        self.value = rdflib.URIRef(naming.rdf_value_predicate(self.cfg, "cb_item", "code"))

    def graph(self, group: str):
        members = self.grouped[group]
        keys = {"cb_code": self.CODES} if any(s.class_ == "cb_code" for s in members) else {}
        return shacl_check.build_graph(self.cfg, members, {"cb_item": self.ITEMS}, keys)

    def node(self, item):
        return rdflib.URIRef(naming.rdf_node(self.cfg, "cb_item", [item]))

    def test_groups(self):
        self.assertEqual({n: [s.id for s in m if s.target == "cb_item"] for n, m in self.grouped.items()},
                         {"main": ["cb_ref", "cb_known"], "extra:1": ["cb_small"]})

    def test_the_value_predicate_carries_every_non_null_value_whether_or_not_the_reference_exists(self):
        g = self.graph("main")
        for item, code in self.ITEMS:
            refs, values = set(g.objects(self.node(item), self.pred)), set(g.objects(self.node(item), self.value))
            if code is None:
                self.assertEqual((refs, values), (set(), set()), "NULL writes neither triple")
                continue
            self.assertEqual(refs, {rdflib.URIRef(naming.rdf_node(self.cfg, "cb_code", [code]))}, item)
            self.assertEqual(values, {shacl_check.literal(code)}, item)
            self.assertEqual((rdflib.URIRef(naming.rdf_node(self.cfg, "cb_code", [code])), rdflib.URIRef(shacl_check.RDF_TYPE),
                              rdflib.URIRef(naming.rdf_entity_class(self.cfg, "cb_code"))) in g, code in self.CODES, item)
        # the bound-only group carries literals on the column itself and no value predicate
        g = self.graph("extra:1")
        self.assertEqual(set(g.triples((None, self.value, None))), set())
        self.assertEqual({type(v) for v in g.objects(None, self.pred)}, {rdflib.Literal})

    def test_shacl_focus_keys_equal_the_ored_view(self):
        found = {}
        for name, members in self.grouped.items():
            got = shacl_check.focus_keys(self.cfg, members, shacl_check.validate(self.graph(name), self.ttl))
            found.update({sid: got[sid] for sid in self.EXPECTED if sid in got})
        self.assertEqual(found, self.EXPECTED)

    def test_a_wrong_bound_is_seen(self):
        """A shapes graph whose bound differs from the configuration's (test-only text) finds other keys."""
        self.assertEqual(self.ttl.count('sh:maxInclusive "5"^^xsd:decimal'), 1)
        wrong = self.ttl.replace('sh:maxInclusive "5"^^xsd:decimal', 'sh:maxInclusive "100"^^xsd:decimal')
        got = shacl_check.focus_keys(self.cfg, self.grouped["main"], shacl_check.validate(self.graph("main"), wrong))
        self.assertEqual(got["cb_ref"], {key(1), key(2)})

    def test_focus_keys_unite_both_property_shapes(self):
        from rdflib import BNode, URIRef
        sh = lambda name: URIRef(shapes.SH + name)  # noqa: E731
        results = rdflib.Graph()
        for source, item in ((naming.rdf_property_shape(self.cfg, "cb_ref"), 2),
                             (naming.rdf_bound_shape(self.cfg, "cb_ref"), 3),
                             (naming.rdf_bound_shape(self.cfg, "cb_ref"), 2),
                             (naming.rdf_property_shape(self.cfg, "cb_known"), 1),
                             (naming.rdf_bound_shape(self.cfg, "cb_known"), 4),     # no such shape: ignored
                             (naming.rdf_bound_shape(self.cfg, "cb_small"), 5)):    # another group: ignored
            r = BNode()
            results.add((r, URIRef(shacl_check.RDF_TYPE), sh("ValidationResult")))
            results.add((r, sh("sourceShape"), URIRef(source)))
            results.add((r, sh("focusNode"), self.node(item)))
        got = shacl_check.focus_keys(self.cfg, self.grouped["main"], results)
        self.assertEqual((got["cb_ref"], got["cb_known"]), ({key(2), key(3)}, {key(1)}))
        self.assertNotIn("cb_small", got)


class StaleShapesTtlTest(unittest.TestCase):
    """A shapes.ttl written before class and bounds had a SHACL form (the shape only as a comment) no longer matches
    the configuration: reason stops with exit 2 and asks for a new build instead of comparing nothing."""

    def test_an_old_shapes_ttl_needs_a_new_build(self):
        cfg = ClassAndBoundsOnRowsTest.config()

        def old_reason(shape):   # 0.5.0
            if shape.sql_violation:
                return "sql_violation"
            return "class_and_bounds" if shape.class_ and shapes.has_bounds(shape) else ""

        with mock.patch.object(shapes, "no_shacl_reason", old_reason):
            old_text, old_skipped = shapes.shapes_ttl(cfg)
        self.assertEqual(old_skipped, ["open_le_total", "cb_ref"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "build").mkdir()
            ttl = Path(tmp) / "build" / "shapes.ttl"
            ttl.write_text(old_text, encoding="utf-8")
            with self.assertRaises(StageError) as e:
                stage_reason.shapes_ttl(cfg, Path(tmp))
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
            self.assertIn("rerun `sxc.py build`", str(e.exception))
            ttl.write_text(shapes.shapes_ttl(cfg)[0], encoding="utf-8")
            self.assertEqual(stage_reason.shapes_ttl(cfg, Path(tmp)), shapes.shapes_ttl(cfg)[0])


class DefinedSqlTest(unittest.TestCase):
    NODES = '"sxc_demo"."part"'
    EDGES = '"sxc_demo"."bom_line"'
    HAS_CHILD = f'EXISTS (SELECT 1 FROM {EDGES} e WHERE e."parent_id" = n."part_id" AND e."child_id" IS NOT NULL)'
    HAS_PARENT = f'EXISTS (SELECT 1 FROM {EDGES} e WHERE e."child_id" = n."part_id" AND e."parent_id" IS NOT NULL)'

    def setUp(self):
        self.cfg = demo_config()

    def sql(self, kind: str, cls: str = "") -> tuple[str, dict]:
        return owl.defined_sql(self.cfg, Defined(name="X", kind=kind, cls=cls))

    def select(self, where: str) -> str:
        return f'SELECT DISTINCT n."part_id" FROM {self.NODES} n WHERE n."part_id" IS NOT NULL AND {where}'

    def test_existential_kinds(self):
        self.assertEqual(self.sql("has_child"), (self.select(self.HAS_CHILD), {}))
        self.assertEqual(self.sql("has_parent"), (self.select(self.HAS_PARENT), {}))

    def test_class_kinds_bind_the_class_value(self):
        of_class = 'CAST(n."part_class" AS text) = :cls'
        self.assertEqual(self.sql("class_with_child", "product"), (self.select(f"{of_class} AND {self.HAS_CHILD}"), {"cls": "product"}))
        self.assertEqual(self.sql("class_used_as_part", "raw"), (self.select(f"{of_class} AND {self.HAS_PARENT}"), {"cls": "raw"}))
        sql, params = self.sql("contains_class", "raw")
        self.assertEqual(sql, self.select(f'EXISTS (SELECT 1 FROM "sxc_out"."has_component_closure" x JOIN {self.NODES} m '
                                          f'ON m."part_id" = x.descendant WHERE x.ancestor = n."part_id" '
                                          f'AND CAST(m."part_class" AS text) = :cls)'))
        self.assertEqual(params, {"cls": "raw"})

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self.sql("has_cousin")

    def test_graph_queries(self):
        self.assertEqual(owl.nodes_sql(self.cfg), f'SELECT n."part_id", CAST(n."part_class" AS text) FROM {self.NODES} n '
                                                  'WHERE n."part_id" IS NOT NULL ORDER BY 1')
        self.assertEqual(owl.edges_sql(self.cfg), f'SELECT DISTINCT e."parent_id", e."child_id" FROM {self.EDGES} e '
                                                  'WHERE e."parent_id" IS NOT NULL AND e."child_id" IS NOT NULL ORDER BY 1, 2')
        self.assertIn("UNION SELECT e.\"child_id\"", owl.individuals_sql(self.cfg))
        self.assertEqual(owl.closure_pairs_sql(self.cfg), 'SELECT ancestor, descendant FROM "sxc_out"."has_component_closure"')


class OwlConfigTest(unittest.TestCase):
    def test_names_share_one_namespace(self):
        for patch in ({"classes": {"product": "part"}}, {"defined": [Defined("has_component_plus", "has_child")]}):
            cfg = demo_config()
            for k, v in patch.items():
                setattr(cfg.ontology, k, v)
            with self.assertRaises(StageError) as e:
                owl.check_config(cfg)
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
            self.assertIn("is used twice", str(e.exception))

    def test_classes_need_the_class_column(self):
        cfg = demo_config()
        cfg.ontology.class_column = ""
        with self.assertRaises(StageError) as e:
            owl.check_config(cfg)
        self.assertIn("[ontology.class_column]", str(e.exception))
        owl.check_config(demo_config())

    def test_java_lookup(self):
        on_path = {"java": "fake-path/java", "java21": "fake-path/java21"}   # stands in for shutil.which
        self.assertEqual(owl.java_executable({"SXC_JAVA": sys.executable}, on_path.get), sys.executable)
        self.assertEqual(owl.java_executable({"SXC_JAVA": "java21"}, on_path.get), "fake-path/java21")
        self.assertIsNone(owl.java_executable({"SXC_JAVA": "no-such-java"}, on_path.get))
        self.assertEqual(owl.java_executable({}, on_path.get), "fake-path/java")
        self.assertIsNone(owl.java_executable({}, {}.get))


class StagePrerequisiteTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_config()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def test_build_must_have_run(self):
        with self.assertRaises(StageError) as e:
            stage_reason.run(self.cfg, types.SimpleNamespace(only=["shacl", "owl"]), self.out)
        self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
        self.assertIn("sxc.py build", str(e.exception))

    def test_unknown_part_is_a_usage_error(self):
        with self.assertRaises(StageError) as e:
            stage_reason.run(self.cfg, types.SimpleNamespace(only=["shacl", "pellet"]), self.out)
        self.assertEqual(e.exception.exit_code, EXIT_CONFIG)

    def test_shapes_ttl_must_match_the_configuration(self):
        with self.assertRaises(StageError):
            stage_reason.shapes_ttl(self.cfg, self.out)
        (self.out / "build").mkdir()
        ttl = shapes.shapes_ttl(self.cfg)[0]
        (self.out / "build" / "shapes.ttl").write_text(ttl, encoding="utf-8", newline="\n")
        self.assertEqual(stage_reason.shapes_ttl(self.cfg, self.out), ttl)
        changed = copy.deepcopy(self.cfg)
        changed.shapes[1].min_exclusive = Decimal(1)
        with self.assertRaises(StageError) as e:
            stage_reason.shapes_ttl(changed, self.out)
        self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
        self.assertIn("rerun `sxc.py build`", str(e.exception))

    def test_missing_components_are_environment_errors(self):
        def without(name):
            def fake(module):   # every other package counts as installed, whatever this environment has
                if module == name:
                    raise ImportError(module)
                return types.ModuleType(module)
            return fake

        for module, label, args in (("pyshacl", "pySHACL", (True, False)), ("owlready2", "owlready2", (False, True))):
            with mock.patch.object(stage_reason.importlib, "import_module", without(module)):
                with self.assertRaises(StageError) as e:
                    stage_reason.components(*args)
            self.assertEqual(e.exception.exit_code, EXIT_ENV)
            self.assertIn(label, str(e.exception))
        with mock.patch.object(stage_reason.importlib, "import_module", lambda module: None), \
                mock.patch.object(owl, "java_executable", lambda: None):
            with self.assertRaises(StageError) as e:
                stage_reason.components(False, True)
            self.assertIn("Java is not available", str(e.exception))
            self.assertIsNone(stage_reason.components(False, False))

    def test_next_action(self):
        res = Result("reason")
        self.assertIn("nothing was compared", stage_reason.next_action(self.cfg, res))
        res.add("shacl:qty_positive", "fail", "shapes[1] qty_positive", view="sxc_out.v_shape_qty_positive")
        res.add("owl:consistency", "fail", "ontology")
        action = stage_reason.next_action(self.cfg, res)
        self.assertIn("sxc_out.v_shape_qty_positive", action)
        self.assertIn("ontology.no_children", action)
        res.add("owl:consistency", "error", "ontology", reason="reasoner_error")
        self.assertIn("doctor --require reason", stage_reason.next_action(self.cfg, res))
        ok = Result("reason")
        ok.add("shacl:qty_positive", "pass", "")
        self.assertIn("sxc.py cube", stage_reason.next_action(self.cfg, ok))
        ok.add("v5:closure:has_component", "not_run", "", reason="closure_not_passed")
        self.assertIn("rerun `sxc.py build`", stage_reason.next_action(self.cfg, ok))


@unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java (SXC_JAVA or java on PATH)")
class OwlReasonerTest(unittest.TestCase):
    """HermiT on a hand-made hierarchy. X is a parent and Z a child missing from the node table."""

    NODES = {"A": {"product"}, "B": {"assembly"}, "C": {"component"}, "R": {"raw"}, "S": {"raw"}, "U": set()}
    EDGES = [("X", "A"), ("A", "B"), ("B", "C"), ("C", "R"), ("B", "R"), ("C", "Z")]

    def setUp(self):
        self.cfg = demo_config()
        self.cfg.ontology.defined.append(Defined("ProductWithChild", "class_with_child", "product"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def reason(self, nodes, edges):
        """owl.reason with tempfile.tempdir pointing at an empty directory, which must be empty again afterwards.
        self.private lists what the private HermiT directory held when it was removed."""
        java_before, real_rmtree, self.private = owlready2.JAVA_EXE, shutil.rmtree, []

        def rmtree(path, *args, **kwargs):
            self.private.append(sorted(os.listdir(path)))
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(tempfile, "tempdir", self.tmp), mock.patch.object(owl.shutil, "rmtree", rmtree):
            r = owl.reason(self.cfg, owl.Graph(nodes, edges), JAVA)
            self.assertEqual(tempfile.tempdir, self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertEqual(owlready2.JAVA_EXE, java_before)
        self.assertEqual(len(self.private), 1)
        return r

    def test_answers_on_node_table_individuals(self):
        r = self.reason(self.NODES, self.EDGES)
        self.assertEqual(self.private, [[]])   # owlready2 removed its input file itself
        self.assertTrue(r.consistent)
        self.assertEqual((r.individuals, r.node_rows, r.outside_node_table, r.edges), (8, 6, 2, 6))
        below = {"X": "ABCRZ", "A": "BCRZ", "B": "CRZ", "C": "RZ"}
        self.assertEqual(r.closure, {(a, d) for a, ds in below.items() for d in ds})
        self.assertEqual(r.members, {"Composite": set("ABC"), "UsedAsPart": set("ABCR"), "ContainsRaw": set("ABC"),
                                     "ProductUsedAsPart": {"A"}, "ProductWithChild": {"A"}})
        self.assertEqual(r.outside, {"Composite": 1, "UsedAsPart": 1, "ContainsRaw": 1, "ProductUsedAsPart": 0,
                                     "ProductWithChild": 0})
        self.assertGreaterEqual(r.hermit_ms, 0)

    def test_a_raw_item_with_a_child_is_inconsistent(self):
        r = self.reason(self.NODES, self.EDGES + [("S", "A")])
        self.assertFalse(r.consistent)
        self.assertEqual((r.closure, r.members), (set(), {}))
        self.assertEqual(len(self.private[0]), 1)   # the input file owlready2 leaves behind, removed with the directory

    def test_a_key_with_two_disjoint_classes_is_inconsistent(self):
        nodes = dict(self.NODES, A={"product", "assembly"})
        self.assertFalse(self.reason(nodes, self.EDGES).consistent)

    def test_reasoner_failure_is_reported_without_output(self):
        with self.assertRaises(owl.ReasonerError) as e:
            owl.reason(self.cfg, owl.Graph(self.NODES, self.EDGES), str(Path(self.tmp) / "no-such-java"))
        self.assertEqual(str(e.exception), "FileNotFoundError")


# ---- integration tests (PostgreSQL 17) -------------------------------------------------------------------
@unittest.skipUnless(pgfixture.available() and HAVE_SHACL, "needs SXC_TEST_ADMIN_DSN, rdflib and pyshacl")
class _ReasonCase(unittest.TestCase):
    """One fixture database, build run and reason run per class (cls.res); OWL is left out without Java."""

    variant = "clean"
    database = ""

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.database, cls.variant)
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.out = cls.tmp / "run"
        stage_build.run(cls.cfg, types.SimpleNamespace(hierarchy=""), cls.out).write(cls.out)
        cls.only = list(stage_reason.PARTS) if HAVE_OWL else ["shacl"]
        cls.res = stage_reason.run(cls.cfg, types.SimpleNamespace(only=cls.only), cls.out)

    def setUp(self):
        # another test class may have pointed the check-role DSN at its own database
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)

    def reason(self, cfg=None, out=None, only=None):
        return stage_reason.run(cfg or self.cfg, types.SimpleNamespace(only=only or self.only), out or self.out)

    def copy_out(self, name: str) -> Path:
        """A copy of the build output whose build result a test may edit."""
        out = self.tmp / name
        shutil.copytree(self.out / "build", out / "build")
        return out

    def edit_build_check(self, out: Path, check_id: str, status: str, **detail):
        f = out / "build" / "result.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        c = next(c for c in data["checks"] if c["id"] == check_id)
        c["status"] = status
        c["detail"].update(detail)
        f.write_text(json.dumps(data), encoding="utf-8")

    def assert_no_row_values(self, *texts: str):
        for text in texts:
            for marker in ROW_VALUE_MARKERS:
                self.assertNotIn(marker, text)

    def run_cli(self) -> tuple[int, str, str]:
        code, console = cli("reason", "--config", str(pgfixture.DEMO_CONFIG), "--out", str(self.out), "--only", ",".join(self.only))
        return code, console, (self.out / "reason" / "result.json").read_text(encoding="utf-8")


class ReasonCleanTest(_ReasonCase):
    database = f"{PREFIX}_clean"

    def test_every_shacl_check_passes(self):
        for s in self.cfg.shapes[:3]:
            c = get_check(self.res, f"shacl:{s.id}")
            self.assertEqual((c.status, c.config_ref, c.detail["violations"], c.detail["shacl_only"], c.detail["sql_only"]),
                             ("pass", shapes.config_ref(self.cfg, s), 0, 0, 0), s.id)
            self.assertEqual(c.detail["view"], naming.shape_view(self.cfg, s.id))
            exp = self.fx.data["expected"]
            rows = exp["bom_lines"] + exp["parts"] + exp["stock_rows"]   # targets and class entity once, as in 0.3.0
            self.assertEqual((c.detail["value_group"], c.detail["group_rows"], c.detail["rows_exported"]),
                             ("main", rows, rows), "no column needs two kinds of value: one group, no extra rows")
        self.assertFalse([c for c in self.res.checks if c.id == "shacl:open_le_total"])
        self.assertIn("shacl:open_le_total: no SHACL form (sql_violation); checked only in SQL (shape:open_le_total in build)",
                      self.res.not_proven)

    @unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java")
    def test_owl_is_consistent_and_agrees_with_sql(self):
        exp = self.fx.data["expected"]
        c = get_check(self.res, "owl:consistency")
        self.assertEqual((c.status, c.detail["individuals"], c.detail["node_rows"], c.detail["edges"]),
                         ("pass", exp["parts"], exp["parts"], exp["edges"]))
        closure = get_check(self.res, "v5:closure:has_component")
        self.assertEqual((closure.status, closure.detail["owl_pairs"], closure.detail["sql_pairs"], closure.detail["owl_only"],
                          closure.detail["sql_only"]), ("pass", exp["closure_pairs"], exp["closure_pairs"], 0, 0))
        for j, d in enumerate(self.cfg.ontology.defined):
            v = get_check(self.res, f"v5:defined:{d.name}")
            self.assertEqual((v.status, v.config_ref, v.detail["owl_only"], v.detail["sql_only"]), ("pass", f"ontology.defined[{j}]", 0, 0))
        self.assertGreater(get_check(self.res, "v5:defined:ContainsRaw").detail["owl_members"], 0)
        self.assertIn("v5:defined:ProductUsedAsPart: the class has no members in OWL or in SQL, so the comparison holds "
                      "vacuously", self.res.not_proven)
        self.assertEqual(self.res.status, "pass")
        self.assertIn("sxc.py cube", self.res.next_safe_action)

    def test_sql_definitions_equal_an_independent_computation(self):
        parts = {p["part_id"]: p["part_class"] for p in self.fx.data["parts"]}
        edges = {(p, c) for p, c, _ln, _q in self.fx.data["edges"]}
        children: dict = {}
        for p, c in edges:
            children.setdefault(p, set()).add(c)

        def below(node, seen):
            for c in children.get(node, ()):
                if c not in seen:
                    seen.add(c)
                    below(c, seen)
            return seen

        contains_raw = {p for p in parts if any(parts.get(d) == "raw" for d in below(p, set()))}
        expected = {"has_child": {p for p, _c in edges if p in parts}, "has_parent": {c for _p, c in edges if c in parts},
                    ("contains_class", "raw"): contains_raw,
                    ("class_with_child", "assembly"): {p for p, _c in edges if parts.get(p) == "assembly"},
                    ("class_used_as_part", "raw"): {c for _p, c in edges if parts.get(c) == "raw"}}
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn):
                for spec, keys in expected.items():
                    kind, cls = spec if isinstance(spec, tuple) else (spec, "")
                    got = owl.sql_defined_members(conn, self.cfg, Defined("X", kind, cls))
                    self.assertEqual(got, {canon.canon(k) for k in keys}, spec)
                    self.assertTrue(keys, spec)
        finally:
            conn.close()

    def test_changed_shapes_need_a_new_build(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.shapes[1].min_exclusive = Decimal(1)
        with self.assertRaises(StageError) as e:
            self.reason(cfg)
        self.assertEqual(e.exception.exit_code, EXIT_CONFIG)

    def test_limits_leave_parts_unproven(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.shacl_max_rows = cfg.verify.owl_max_individuals = 10
        res = self.reason(cfg, self.copy_out("limits"))
        checks = [c for c in res.checks if not c.id.startswith("scope:")]
        self.assertTrue(checks)
        self.assertEqual(res.status, "not_run")
        self.assertTrue(all(c.status == "not_run" and c.detail.get("reason") == "resource_limit" for c in checks))
        self.assertTrue(any(n.startswith("shacl: ") and "verify.shacl_max_rows = 10" in n for n in res.not_proven))
        if HAVE_OWL:
            self.assertTrue(any(n.startswith("owl: ") and "verify.owl_max_individuals = 10" in n for n in res.not_proven))
        self.assertIn("resource limit", res.next_safe_action)
        self.assertIn("rerun reason", res.next_safe_action)

    @unittest.skipUnless(owlready2, "owlready2 is not installed")
    def test_missing_java_is_an_environment_error(self):
        with mock.patch.dict(os.environ, {"SXC_JAVA": str(self.tmp / "no-such-java")}):
            with self.assertRaises(StageError) as e:
                self.reason(only=["owl"])
            self.assertEqual(e.exception.exit_code, EXIT_ENV)
            self.assertIn("Java is not available", str(e.exception))
            res = self.reason(out=self.copy_out("no_java"), only=["shacl"])
        self.assertTrue(all(c.status == "pass" for c in res.checks if c.id.startswith("shacl:")))
        self.assertEqual([(c.id, c.status, c.detail) for c in res.checks if not c.id.startswith("shacl:")],
                         [("scope:owl", "not_run", {"reason": "excluded_by_only"})])
        self.assertEqual(res.status, "not_run", "a run narrowed by --only never passes")
        self.assertIn("without --only", res.next_safe_action)

    @unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java")
    def test_v5_needs_a_closure_built_from_every_edge(self):
        for status, detail, reason in (("fail", {"reason": "cycle"}, "closure_not_passed"),
                                       ("pass", {"dropped_cycle_edges": 2}, "cycle_edges_dropped")):
            out = self.copy_out(f"gate_{reason}")
            self.edit_build_check(out, "closure:has_component", status, **detail)
            res = self.reason(out=out)
            for cid in ("v5:closure:has_component", "v5:defined:ContainsRaw"):
                c = get_check(res, cid)
                self.assertEqual((c.status, c.detail["reason"]), ("not_run", reason), cid)
            for name in ("Composite", "UsedAsPart", "ProductUsedAsPart"):
                self.assertEqual(get_check(res, f"v5:defined:{name}").status, "pass")
            self.assertEqual(get_check(res, "owl:consistency").status, "pass")
            self.assertEqual(res.status, "not_run")
            self.assertTrue(any(n.startswith("v5:closure:has_component: not compared") for n in res.not_proven))

    def test_a_shape_without_a_view_is_not_run(self):
        out = self.copy_out("no_view")
        self.edit_build_check(out, "shape:qty_positive", "error", sqlstate="57014", violations=None)
        res = self.reason(out=out, only=["shacl"])
        c = get_check(res, "shacl:qty_positive")
        self.assertEqual((c.status, c.detail["reason"], c.detail["build_status"]), ("not_run", "shape_not_built", "error"))
        self.assertEqual(get_check(res, "shacl:child_exists").status, "pass")
        self.assertIn("rerun `sxc.py build`", res.next_safe_action)

    def test_cli_passes_without_row_values(self):
        code, console, result = self.run_cli()
        self.assertEqual(code, EXIT_PASS if HAVE_OWL else EXIT_FAIL, console)   # without Java the run is narrowed
        self.assertEqual(json.loads(result)["status"], "pass" if HAVE_OWL else "not_run")
        self.assert_no_row_values(result, console)
        self.assertNotIn("Owlready", console)


class ReasonDirtyTest(_ReasonCase):
    variant = "dirty"
    database = f"{PREFIX}_dirty"

    def test_shacl_and_sql_agree_on_the_planted_violations(self):
        exp = self.fx.data["expected"]
        for sid, n in (("child_exists", exp["orphan_child_refs"]), ("qty_positive", exp["zero_qty_lines"]),
                       ("stock_nonnegative", exp["negative_stock_rows"])):
            c = get_check(self.res, f"shacl:{sid}")
            self.assertGreater(n, 0)
            self.assertEqual((c.status, c.detail["violations"], c.detail["shacl_focus_nodes"], c.detail["shacl_only"],
                              c.detail["sql_only"]), ("pass", n, n, 0, 0), sid)
        self.assertEqual(get_check(self.res, "shacl:stock_nonnegative").detail["severity"], "warning")

    def test_one_group_exports_every_row_once_and_leaves_dangling_references_untyped(self):
        """The demo has no column that needs two kinds of value: one graph, the export 0.3.0 made."""
        from rdflib import URIRef
        conn = self.fx.check_conn()
        try:
            graphs = shacl_check.export_graphs(conn, self.cfg)
        finally:
            conn.close()
        self.assertEqual(list(graphs), ["main"])
        exp, rdf_type, g = self.fx.data["expected"], URIRef(shacl_check.RDF_TYPE), graphs["main"]
        child, qty = (URIRef(naming.rdf_predicate(self.cfg, "bom_line", p)) for p in ("child_id", "qty_per"))
        rows = {"bom_line": exp["bom_lines"], "part": exp["parts"], "stock": exp["stock_rows"]}
        typed = {e: len(set(g.subjects(rdf_type, URIRef(naming.rdf_entity_class(self.cfg, e))))) for e in rows}
        self.assertEqual(typed, rows)
        children = set(g.objects(None, child))
        self.assertEqual(len([o for o in children if (o, rdf_type, None) not in g]), exp["orphan_child_refs"])
        self.assertFalse([o for o in children if not isinstance(o, URIRef)], "a class column carries row IRIs only")
        quantities = list(g.objects(None, qty))
        self.assertEqual((len(quantities), {str(q.datatype) for q in quantities}), (exp["bom_lines"], {XSD + "decimal"}))
        c = get_check(self.res, "shacl:child_exists")
        self.assertEqual((c.detail["value_group"], c.detail["group_rows"], c.detail["rows_exported"]),
                         ("main", sum(rows.values()), sum(rows.values())))

    @unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java")
    def test_a_raw_part_with_a_child_makes_the_ontology_inconsistent(self):
        exp = self.fx.data["expected"]
        c = get_check(self.res, "owl:consistency")
        self.assertEqual((c.status, c.detail["outside_node_table"], c.detail["edges"]), ("fail", exp["orphan_child_refs"], exp["edges"]))
        v5 = [x for x in self.res.checks if x.id.startswith("v5:")]
        self.assertEqual(len(v5), 1 + len(self.cfg.ontology.defined))
        self.assertTrue(all((x.status, x.detail["reason"]) == ("not_run", "inconsistent") for x in v5))
        self.assertTrue(any(n.startswith("v5: the ontology is inconsistent") for n in self.res.not_proven))
        self.assertEqual(self.res.status, "fail")
        self.assertIn("ontology.no_children", self.res.next_safe_action)

    def test_cli_reports_without_row_values(self):
        code, console, result = self.run_cli()
        self.assertEqual(code, EXIT_FAIL, console)   # the inconsistent ontology, or without Java the narrowed run
        self.assert_no_row_values(result, console)
        self.assertFalse(re.search(r"P-\d{6}|SO-\d{6}", result))


@unittest.skipUnless(pgfixture.available() and HAVE_SHACL, "needs SXC_TEST_ADMIN_DSN, rdflib and pyshacl")
class ReasonValueGroupsTest(unittest.TestCase):
    """One column, vg_item.code, checked by two class shapes (vg_code, vg_grade) and a bound next to the demo shapes:
    the demo shapes and code_known share "main", code_small (literals on the column) opens "extra:1" and code_graded
    (vg_grade IRIs, which neither allows) "extra:2"; each group is exported as its own data graph and validated on its
    own, with a seeded violation for every vg shape. The expected keys come from the hand-made rows, not from the code
    under test."""

    database = f"{PREFIX}_groups"
    ITEMS = [("i1", 1), ("i2", 2), ("i3", 3), ("i4", 7), ("i5", None), ("i6", 0)]
    CODES, GRADES = (1, 2, 3), (1, 2)
    EXPECTED = {"code_known": {key("i4"), key("i6")}, "code_small": {key("i4")},
                "code_graded": {key("i3"), key("i4"), key("i6")}}
    GROUPS = {"main": ["child_exists", "qty_positive", "stock_nonnegative", "code_known"], "extra:1": ["code_small"],
              "extra:2": ["code_graded"]}

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.database, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        conn = db.connect("SXC_PG_OWNER_DSN")
        try:
            with db.write_tx(conn):
                conn.run("CREATE TABLE sxc_demo.vg_code (code integer PRIMARY KEY)")
                conn.run("CREATE TABLE sxc_demo.vg_grade (grade integer PRIMARY KEY)")
                conn.run("CREATE TABLE sxc_demo.vg_item (item_id text PRIMARY KEY, code integer)")
                for c in cls.CODES:
                    conn.run("INSERT INTO sxc_demo.vg_code VALUES (:c)", c=c)
                for g in cls.GRADES:
                    conn.run("INSERT INTO sxc_demo.vg_grade VALUES (:g)", g=g)
                for item, code in cls.ITEMS:
                    conn.run("INSERT INTO sxc_demo.vg_item VALUES (:i, CAST(:c AS integer))", i=item, c=code)
        finally:
            conn.close()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cfg = config.load(pgfixture.DEMO_CONFIG)
        cfg.entities["vg_code"] = config.Entity("vg_code", "sxc_demo.vg_code", ["code"])
        cfg.entities["vg_grade"] = config.Entity("vg_grade", "sxc_demo.vg_grade", ["grade"])
        cfg.entities["vg_item"] = config.Entity("vg_item", "sxc_demo.vg_item", ["item_id"])
        cfg.shapes += [Shape(id="code_known", target="vg_item", path="code", class_="vg_code"),
                       Shape(id="code_small", target="vg_item", path="code", max_inclusive=Decimal(3)),
                       Shape(id="code_graded", target="vg_item", path="code", class_="vg_grade")]
        cls.cfg = cfg
        cls.out = cls.tmp / "run"
        stage_build.run(cfg, types.SimpleNamespace(hierarchy=""), cls.out).write(cls.out)
        cls.res = stage_reason.run(cfg, types.SimpleNamespace(only=["shacl"]), cls.out)
        exp, items = cls.fx.data["expected"], len(cls.ITEMS)
        cls.group_rows = {"main": exp["bom_lines"] + exp["parts"] + exp["stock_rows"] + items + len(cls.CODES),
                          "extra:1": items, "extra:2": items + len(cls.GRADES)}
        cls.distinct_rows = exp["bom_lines"] + exp["parts"] + exp["stock_rows"] + items + len(cls.CODES) + len(cls.GRADES)

    def setUp(self):
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)

    def copy_out(self, name: str) -> Path:
        out = self.tmp / name
        shutil.copytree(self.out / "build", out / "build")
        return out

    def shacl_run(self, cfg=None, spy=None):
        cfg = cfg or self.cfg
        conn = self.fx.check_conn()
        try:
            if spy is not None:
                real = conn.run
                conn.run = lambda sql, **kw: (spy.append(sql), real(sql, **kw))[1]
            return shacl_check.run(conn, cfg, shapes.shapes_ttl(cfg)[0])
        finally:
            conn.close()

    def test_every_shape_on_the_shared_column_is_compared_with_shacl(self):
        self.assertFalse([n for n in self.res.not_proven if "code_" in n or "shared_path" in n], self.res.not_proven)
        for sid, keys in self.EXPECTED.items():
            c = get_check(self.res, f"shacl:{sid}")
            self.assertGreater(len(keys), 0, "a seeded violation in every vg group")
            self.assertEqual((c.status, c.detail["violations"], c.detail["shacl_focus_nodes"], c.detail["shacl_only"],
                              c.detail["sql_only"]), ("pass", len(keys), len(keys), 0, 0), sid)
        for name, ids in self.GROUPS.items():
            for sid in ids:
                c = get_check(self.res, f"shacl:{sid}")
                self.assertEqual((c.status, c.detail["value_group"], c.detail["group_rows"], c.detail["rows_exported"]),
                                 ("pass", name, self.group_rows[name], sum(self.group_rows.values())), sid)
        self.assertEqual(self.res.status, "not_run", "--only shacl narrows the run")

    def test_focus_keys_equal_the_view_keys_in_one_snapshot(self):
        statements = []
        got = self.shacl_run(spy=statements)
        for sid, keys in self.EXPECTED.items():
            self.assertEqual((got[sid]["shacl_keys"], got[sid]["sql_keys"]), (keys, keys), sid)
        self.assertEqual(set(got), {s for ids in self.GROUPS.values() for s in ids})
        self.assertEqual(statements.count("BEGIN TRANSACTION READ ONLY"), 1)
        self.assertEqual(statements.count(shacl_check.SNAPSHOT_SQL), 1)
        self.assertEqual(statements.count("ROLLBACK"), 1)
        self.assertEqual(statements[-1], "ROLLBACK", "every read happens inside the one transaction")

    def test_the_row_cap_applies_to_the_sum_over_groups(self):
        total = sum(self.group_rows.values())
        self.assertGreater(total - 1, self.distinct_rows, "each table counted once would stay under the cap")
        self.assertGreater(total - 1, max(self.group_rows.values()), "every group alone stays under the cap")
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.shacl_max_rows = total
        self.assertEqual({v["rows"] for v in self.shacl_run(cfg).values()}, {total})
        cfg.verify.shacl_max_rows = total - 1
        with self.assertRaises(shacl_check.TooManyRows) as e:
            self.shacl_run(cfg)
        self.assertEqual((e.exception.rows, e.exception.limit), (total, total - 1))
        res = stage_reason.run(cfg, types.SimpleNamespace(only=["shacl"]), self.copy_out("cap"))
        checks = [c for c in res.checks if c.id.startswith("shacl:")]
        self.assertEqual(len(checks), sum(len(ids) for ids in self.GROUPS.values()))
        self.assertTrue(all((c.status, c.detail["reason"], c.detail["rows"]) == ("not_run", "resource_limit", total)
                            for c in checks))
        self.assertIn(f"shacl: {total} rows to export exceed verify.shacl_max_rows = {total - 1}; no shape was "
                      "compared with SHACL", res.not_proven)

    def test_a_failing_group_leaves_the_other_groups_judged(self):
        from rdflib import URIRef
        grade = URIRef(naming.rdf_entity_class(self.cfg, "vg_grade"))
        real = shacl_check.validate
        for error, recorded in ((shacl_check.ValidationError("Literal"), "Literal"),
                                (RuntimeError("P-ORPHAN i4 quoted by pySHACL"), "RuntimeError")):
            with self.subTest(error=type(error).__name__):
                def validate(graph, shapes_graph):
                    if (None, URIRef(shacl_check.RDF_TYPE), grade) in graph:
                        raise error
                    return real(graph, shapes_graph)

                with mock.patch.object(shacl_check, "validate", validate):
                    res = stage_reason.run(self.cfg, types.SimpleNamespace(only=["shacl"]),
                                           self.copy_out(f"failing_group_{recorded}"))
                c = get_check(res, "shacl:code_graded")
                self.assertEqual((c.status, c.detail["reason"], c.detail["exception"], c.detail["value_group"]),
                                 ("error", "validation_failure", recorded, "extra:2"))
                for sid in ("child_exists", "qty_positive", "stock_nonnegative", "code_small", "code_known"):
                    self.assertEqual(get_check(res, f"shacl:{sid}").status, "pass", sid)
                self.assertEqual(get_check(res, "shacl:code_known").detail["violations"], 2)
                self.assertEqual(res.status, "error")
                self.assertIn("pySHACL could not validate", res.next_safe_action)
                self.assertNotIn("P-ORPHAN", json.dumps(res.to_dict()), "only the exception type is recorded")

    def test_a_disagreement_in_one_group_fails_only_that_shape(self):
        view = ident.qualified(naming.shape_view(self.cfg, "code_small"))
        shape = next(s for s in self.cfg.shapes if s.id == "code_small")
        conn = self.fx.check_conn()
        try:
            with db.write_tx(conn):   # the view loses the seeded violation that SHACL still finds
                conn.run(f'CREATE OR REPLACE VIEW {view} AS SELECT t."item_id" FROM "sxc_demo"."vg_item" t WHERE false')
            res = stage_reason.run(self.cfg, types.SimpleNamespace(only=["shacl"]), self.copy_out("disagree"))
        finally:
            with db.write_tx(conn):
                conn.run(f"CREATE OR REPLACE VIEW {view} AS {shapes.violation_sql(self.cfg, shape)}")
            conn.close()
        c = get_check(res, "shacl:code_small")
        self.assertEqual((c.status, c.detail["shacl_only"], c.detail["sql_only"]), ("fail", 1, 0))
        for sid in ("code_known", "code_graded", "child_exists", "qty_positive", "stock_nonnegative"):
            self.assertEqual(get_check(res, f"shacl:{sid}").status, "pass", sid)
        self.assertEqual(res.status, "fail")


@unittest.skipUnless(pgfixture.available() and HAVE_SHACL, "needs SXC_TEST_ADMIN_DSN, rdflib and pyshacl")
class ReasonClassAndBoundsTest(unittest.TestCase):
    """A shape with class and bounds (cb_ref) compared with SHACL in PostgreSQL, next to a class-only shape on the
    same column (cb_known, the same value group) and a bound-only shape (cb_small, its own group). cb_item rows:
    1 dangling and out of range, 2 dangling and in range, 3 existing and out of range, 4 existing and in range,
    5 NULL. The expected keys come from these rows, not from the code under test."""

    database = f"{PREFIX}_class_bounds"
    ITEMS, CODES, EXPECTED = ClassAndBoundsOnRowsTest.ITEMS, ClassAndBoundsOnRowsTest.CODES, ClassAndBoundsOnRowsTest.EXPECTED
    GROUPS = {"main": ["child_exists", "qty_positive", "stock_nonnegative", "cb_ref", "cb_known"], "extra:1": ["cb_small"]}

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.database, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        conn = db.connect("SXC_PG_OWNER_DSN")
        try:
            with db.write_tx(conn):
                conn.run("CREATE TABLE sxc_demo.cb_code (code integer PRIMARY KEY)")
                conn.run("CREATE TABLE sxc_demo.cb_item (item_id integer PRIMARY KEY, code integer)")
                for c in cls.CODES:
                    conn.run("INSERT INTO sxc_demo.cb_code VALUES (:c)", c=c)
                for item, code in cls.ITEMS:
                    conn.run("INSERT INTO sxc_demo.cb_item VALUES (:i, CAST(:c AS integer))", i=item, c=code)
        finally:
            conn.close()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.cfg = ClassAndBoundsOnRowsTest.config()
        cls.out = cls.tmp / "run"
        stage_build.run(cls.cfg, types.SimpleNamespace(hierarchy=""), cls.out).write(cls.out)
        cls.res = stage_reason.run(cls.cfg, types.SimpleNamespace(only=["shacl"]), cls.out)
        exp, items = cls.fx.data["expected"], len(cls.ITEMS)
        cls.group_rows = {"main": exp["bom_lines"] + exp["parts"] + exp["stock_rows"] + items + len(cls.CODES),
                          "extra:1": items}

    def setUp(self):
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)

    def copy_out(self, name: str) -> Path:
        out = self.tmp / name
        shutil.copytree(self.out / "build", out / "build")
        return out

    def shacl_run(self, cfg=None, ttl=None):
        cfg = cfg or self.cfg
        conn = self.fx.check_conn()
        try:
            return shacl_check.run(conn, cfg, ttl or shapes.shapes_ttl(cfg)[0])
        finally:
            conn.close()

    def test_the_class_and_bounds_shape_is_compared_not_sql_only(self):
        self.assertFalse([n for n in self.res.not_proven if "cb_" in n or "class_and_bounds" in n], self.res.not_proven)
        self.assertIn("shacl:open_le_total: no SHACL form (sql_violation); checked only in SQL (shape:open_le_total in build)",
                      self.res.not_proven)
        for name, ids in self.GROUPS.items():
            for sid in ids:
                c = get_check(self.res, f"shacl:{sid}")
                n = len(self.EXPECTED.get(sid, ()))
                self.assertEqual((c.status, c.detail["violations"], c.detail["shacl_focus_nodes"], c.detail["shacl_only"],
                                  c.detail["sql_only"], c.detail["value_group"], c.detail["group_rows"],
                                  c.detail["rows_exported"]),
                                 ("pass", n, n, 0, 0, name, self.group_rows[name], sum(self.group_rows.values())), sid)
        self.assertEqual(get_check(self.res, "shacl:cb_ref").detail["kind"], "class+bound")
        self.assertEqual(self.res.status, "not_run", "--only shacl narrows the run")

    def test_focus_keys_equal_the_view_keys(self):
        got = self.shacl_run()
        for sid, keys in self.EXPECTED.items():
            self.assertEqual((got[sid]["shacl_keys"], got[sid]["sql_keys"]), (keys, keys), sid)
        self.assertEqual(self.EXPECTED["cb_ref"], {key(1), key(2), key(3)})

    def test_the_row_cap_still_counts_the_sum_over_groups(self):
        total = sum(self.group_rows.values())
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.shacl_max_rows = total
        self.assertEqual({v["rows"] for v in self.shacl_run(cfg).values()}, {total})
        cfg.verify.shacl_max_rows = total - 1
        with self.assertRaises(shacl_check.TooManyRows) as e:
            self.shacl_run(cfg)
        self.assertEqual((e.exception.rows, e.exception.limit), (total, total - 1))

    def test_a_wrong_bound_in_the_shapes_graph_disagrees(self):
        ttl = shapes.shapes_ttl(self.cfg)[0]
        self.assertEqual(ttl.count('sh:maxInclusive "5"^^xsd:decimal'), 1)
        got = self.shacl_run(ttl=ttl.replace('sh:maxInclusive "5"^^xsd:decimal', 'sh:maxInclusive "100"^^xsd:decimal'))
        self.assertEqual((got["cb_ref"]["shacl_keys"], got["cb_ref"]["sql_keys"]),
                         ({key(1), key(2)}, {key(1), key(2), key(3)}))
        conn = self.fx.check_conn()
        res = Result("reason")
        try:
            with mock.patch.object(shacl_check, "run", return_value=got):
                stage_reason._shacl(conn, self.cfg, stage_reason.require_stage(self.out, "build"),
                                    shacl_check.plan(self.cfg)[0], ttl, res)
        finally:
            conn.close()
        c = get_check(res, "shacl:cb_ref")
        self.assertEqual((c.status, c.detail["shacl_only"], c.detail["sql_only"]), ("fail", 0, 1))

    def test_a_corrupted_view_fails_the_shape(self):
        view = ident.qualified(naming.shape_view(self.cfg, "cb_ref"))
        shape = next(s for s in self.cfg.shapes if s.id == "cb_ref")
        conn = self.fx.check_conn()
        try:
            with db.write_tx(conn):   # the view keeps only the class part: key 3 (existing, out of range) is lost
                conn.run(f'CREATE OR REPLACE VIEW {view} AS SELECT t."item_id" FROM "sxc_demo"."cb_item" t WHERE '
                         't."code" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_demo"."cb_code" c WHERE c."code" = t."code")')
            res = stage_reason.run(self.cfg, types.SimpleNamespace(only=["shacl"]), self.copy_out("corrupted"))
        finally:
            with db.write_tx(conn):
                conn.run(f"CREATE OR REPLACE VIEW {view} AS {shapes.violation_sql(self.cfg, shape)}")
            conn.close()
        c = get_check(res, "shacl:cb_ref")
        self.assertEqual((c.status, c.detail["violations"], c.detail["shacl_only"], c.detail["sql_only"]), ("fail", 2, 1, 0))
        for sid in ("cb_known", "cb_small", "child_exists", "qty_positive", "stock_nonnegative"):
            self.assertEqual(get_check(res, f"shacl:{sid}").status, "pass", sid)
        self.assertEqual(res.status, "fail")


del _ReasonCase   # the base class has no tests of its own

if __name__ == "__main__":
    unittest.main()
