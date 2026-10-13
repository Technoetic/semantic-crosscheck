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
from semantic_crosscheck import canon, config, db, naming, owl, shacl_check, shapes, stage_build, stage_reason
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
    return tuple(canon.canon(v) for v in values)


def cli(*argv) -> tuple[int, str]:
    """sxc.main with the console output captured."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = sxc.main(list(argv))
    return code, buf.getvalue()


# ---- unit tests (no database) ---------------------------------------------------------------------------
class RdfKeyTest(unittest.TestCase):
    KEYS = [["P-000001"], ["a b/c#d%e?f&g=h"], ["e" + chr(0x301), chr(0xE9), chr(0x1F600)],
            ['quote"', "back" + chr(92) + "slash", "tab\tline\n"], [1, Decimal("2.50"), "007"], [True, None], ["1.50", "-0"]]

    def setUp(self):
        self.cfg = demo_config()

    def test_keys_round_trip_in_canonical_form(self):
        for k in self.KEYS:
            iri = naming.rdf_node(self.cfg, "bom_line", k)
            self.assertEqual(naming.rdf_key(self.cfg, "bom_line", iri), [canon.canon(v) for v in k], k)
            suffix = iri[len(naming.rdf_entity_class(self.cfg, "bom_line")) + 1:]
            self.assertRegex(suffix, r"^[A-Za-z0-9%._~-]+$")   # only IRI-unreserved characters and escapes

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
            self.assertEqual(keys, {tuple(canon.canon(v) for v in k) for k in self.KEYS}, fmt)


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

    def test_one_column_carries_one_kind_of_value(self):
        cfg = with_shapes(demo_config(),
                          Shape(id="child_small", target="bom_line", path="child_id", max_inclusive=Decimal(9)),
                          Shape(id="qty_capped", target="bom_line", path="qty_per", max_inclusive=Decimal(1000)),
                          Shape(id="class_and_bound", target="stock", path="qty", class_="part", min_inclusive=Decimal(0)))
        planned, skipped = shacl_check.plan(cfg)
        self.assertEqual([s.id for s in planned], ["qty_positive", "stock_nonnegative", "qty_capped"])
        self.assertEqual(skipped, {"child_exists": "shared_path", "open_le_total": "sql_violation",
                                   "child_small": "shared_path", "class_and_bound": "class_and_bounds"})
        self.assertEqual([p for p, _s in shacl_check.paths(planned, "bom_line")], ["qty_per"])


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
        self.assertEqual((res.checks, res.status), ([], "not_run"))
        self.assertTrue(any(n.startswith("shacl: ") and "verify.shacl_max_rows = 10" in n for n in res.not_proven))
        if HAVE_OWL:
            self.assertTrue(any(n.startswith("owl: ") and "verify.owl_max_individuals = 10" in n for n in res.not_proven))
        self.assertIn("nothing was compared", res.next_safe_action)

    @unittest.skipUnless(owlready2, "owlready2 is not installed")
    def test_missing_java_is_an_environment_error(self):
        with mock.patch.dict(os.environ, {"SXC_JAVA": str(self.tmp / "no-such-java")}):
            with self.assertRaises(StageError) as e:
                self.reason(only=["owl"])
            self.assertEqual(e.exception.exit_code, EXIT_ENV)
            self.assertIn("Java is not available", str(e.exception))
            res = self.reason(out=self.copy_out("no_java"), only=["shacl"])
        self.assertEqual(res.status, "pass")
        self.assertFalse([c for c in res.checks if not c.id.startswith("shacl:")])

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
        self.assertEqual(code, EXIT_PASS, console)
        self.assertEqual(json.loads(result)["status"], "pass")
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

    def test_export_types_every_row_and_leaves_dangling_references_untyped(self):
        from rdflib import URIRef
        conn = self.fx.check_conn()
        try:
            g = shacl_check.export_graph(conn, self.cfg)
        finally:
            conn.close()
        exp, rdf_type = self.fx.data["expected"], URIRef(shacl_check.RDF_TYPE)
        typed = {e: len(set(g.subjects(rdf_type, URIRef(naming.rdf_entity_class(self.cfg, e))))) for e in ("bom_line", "part", "stock")}
        self.assertEqual(typed, {"bom_line": exp["bom_lines"], "part": exp["parts"], "stock": exp["stock_rows"]})
        children = set(g.objects(None, URIRef(naming.rdf_predicate(self.cfg, "bom_line", "child_id"))))
        self.assertEqual(len([o for o in children if (o, rdf_type, None) not in g]), exp["orphan_child_refs"])
        quantities = list(g.objects(None, URIRef(naming.rdf_predicate(self.cfg, "bom_line", "qty_per"))))
        self.assertEqual((len(quantities), {str(q.datatype) for q in quantities}), (exp["bom_lines"], {XSD + "decimal"}))

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
        self.assertEqual(code, EXIT_FAIL if HAVE_OWL else EXIT_PASS, console)
        self.assert_no_row_values(result, console)
        self.assertFalse(re.search(r"P-\d{6}|SO-\d{6}", result))


del _ReasonCase   # the base class has no tests of its own

if __name__ == "__main__":
    unittest.main()
