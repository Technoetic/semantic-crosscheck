"""Opt-in SHACL property-pair comparison of sql_violation predicates t.<a> <op> t.<b> (verify.shacl_pairs).

Unit tests need no database: the strict whole-string parser, the operator table (each of the four operators on
hand-made rows through the generated pair shapes and pySHACL, against the predicate evaluated in Python), the pinned
pySHACL behaviour that the preconditions rely on (NaN conforms, other datatypes ordered by datatype IRI, integer 1
equal to decimal 1.0), the configuration option and the generated pair shapes against the shacl-shacl preflight.
Integration tests (skipped unless SXC_TEST_ADMIN_DSN is set) run build and reason on the dirty demo, whose
open_le_total rule (t.qty_open > t.qty_total) has 3 injected violating rows, plus small tables for the four
operators, NaN and infinities, a double precision column and duplicate keys. Expected keys come from the rows,
not from the code under test.
"""

import copy
import importlib.util
import json
import os
import shutil
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import canon, config, db, naming, shacl_check, shapes, stage_build, stage_reason
from semantic_crosscheck.config import Entity, Shape
from semantic_crosscheck.result import EXIT_CONFIG, StageError

HAVE_SHACL = all(importlib.util.find_spec(m) for m in ("rdflib", "pyshacl"))
SH = shapes.SH
XSD = shapes.XSD
OLD_LINE = "shacl:{id}: no SHACL form (sql_violation); checked only in SQL (shape:{id} in build)"


def demo_config():
    return config.load(pgfixture.DEMO_CONFIG)


def key(*values) -> tuple:
    return tuple(canon.key(v) for v in values)


def get_check(res, check_id: str):
    found = [c for c in res.checks if c.id == check_id]
    if len(found) != 1:
        raise AssertionError(f"{check_id}: {len(found)} checks")
    return found[0]


def violates(a, op, b) -> bool:
    """The SQL predicate t.a <op> t.b as a WHERE clause keeps it: NULL on either side is unknown, not a violation."""
    if a is None or b is None:
        return False
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


# ---- parser ------------------------------------------------------------------------------------------------
class PairFormTest(unittest.TestCase):
    def shape(self, predicate, **kw):
        return Shape(id="s", target="sales_order", sql_violation=predicate, **kw)

    def test_accepts_only_the_exact_two_column_comparison(self):
        for predicate, expected in (
                ("t.qty_open > t.qty_total", ("qty_open", ">", "qty_total")),
                ("t.a>=t.b", ("a", ">=", "b")),
                ("  t.a <t.b  ", ("a", "<", "b")),
                ("\nt.a <= t.b\n", ("a", "<=", "b")),
                ("\tt._x9 > t.y_\r\n", ("_x9", ">", "y_"))):
            with self.subTest(predicate=predicate):
                self.assertEqual(shapes.pair_form(self.shape(predicate)), expected)

    def test_rejects_everything_else(self):
        long_name = "a" * 64
        for predicate in (
                "t.qty_open > t.qty_total -- note",                 # the comment fixture of test_shapes_unit
                "t.qty_open > t.qty_total -- open above total",     # the comment fixture of test_build
                "t.no_such_column > 0", "t.qty_total / 0 > 1", "t.location_id = ''", "false", "",
                "t.a = t.b", "t.a <> t.b", "t.a != t.b", "t.a => t.b", "t.a > = t.b", "t.a >> t.b",
                "(t.a > t.b)", "t.a::numeric > t.b", "CAST(t.a AS numeric) > t.b", "t.a > t.b::integer",
                "t.a > t.a", "T.a > t.b", "t.A > t.b", 't."a" > t.b', "s.a > t.b", "t . a > t.b", "a > b",
                "t.a > t.b AND t.b > 0", "t.a > t.b;", "t.a > t.b /* c */", "t.a >= t.b\n-- c",
                "t.a > -t.b", "t.a > t.b + 0", "t.a > t.b", "t.a > t.b ", f"t.{long_name} > t.b",
                "t.a > t.b\nOR true", "NOT t.a > t.b"):
            with self.subTest(predicate=predicate):
                self.assertIsNone(shapes.pair_form(self.shape(predicate)))

    def test_shapes_with_another_constraint_keep_no_pair_form(self):
        for extra in ({"path": "qty_open"}, {"path": "part_id", "class_": "part"},
                      {"path": "qty_open", "min_inclusive": Decimal(0)}):
            with self.subTest(extra=sorted(extra)):
                self.assertIsNone(shapes.pair_form(self.shape("t.qty_open > t.qty_total", **extra)))

    def test_operator_table_maps_the_violation_to_the_constraint_valid_rows_satisfy(self):
        self.assertEqual(shapes.PAIR_OPS, {">": ("a", "lessThanOrEquals", "b"), ">=": ("a", "lessThan", "b"),
                                           "<": ("b", "lessThanOrEquals", "a"), "<=": ("b", "lessThan", "a")})
        for predicate, expected in (("t.x > t.y", ("x", "lessThanOrEquals", "y")),
                                    ("t.x >= t.y", ("x", "lessThan", "y")),
                                    ("t.x < t.y", ("y", "lessThanOrEquals", "x")),
                                    ("t.x <= t.y", ("y", "lessThan", "x"))):
            with self.subTest(predicate=predicate):
                self.assertEqual(shapes.pair_constraint(self.shape(predicate)), expected)
        self.assertIsNone(shapes.pair_constraint(self.shape("t.x = t.y")))

    def test_build_outputs_do_not_change(self):
        """shapes.ttl, its skipped ids and the view SQL are the same whatever verify.shacl_pairs says."""
        cfg = demo_config()
        on = copy.deepcopy(cfg)
        on.verify.shacl_pairs = True
        self.assertEqual(shapes.shapes_ttl(on), shapes.shapes_ttl(cfg))
        self.assertIn("# open_le_total: no SHACL form (sql_violation); checked by its SQL view only", shapes.shapes_ttl(on)[0])
        self.assertEqual([shapes.violation_sql(on, s) for s in on.shapes], [shapes.violation_sql(cfg, s) for s in cfg.shapes])
        self.assertEqual(shacl_check.plan(on), shacl_check.plan(cfg))
        self.assertEqual([s.id for s in shacl_check.pair_plan(on)], ["open_le_total"])


class PairOptionTest(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "crosscheck.toml"
            p.write_text('version = 1\n[source]\nschemas = ["s"]\n[verify]\n' + text, encoding="utf-8")
            return config.load(p)

    def test_off_by_default_and_strictly_boolean(self):
        self.assertIs(demo_config().verify.shacl_pairs, False)
        self.assertIs(self.load("").verify.shacl_pairs, False)
        self.assertIs(self.load("shacl_pairs = true\n").verify.shacl_pairs, True)
        self.assertIs(self.load("shacl_pairs = false\n").verify.shacl_pairs, False)
        for bad in ("1", '"true"', "0", "[true]"):
            with self.subTest(value=bad):
                with self.assertRaises(StageError) as e:
                    self.load(f"shacl_pairs = {bad}\n")
                self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
                self.assertIn("verify.shacl_pairs", str(e.exception))


# ---- pySHACL ----------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_SHACL, "rdflib and pyshacl are not installed")
class PinnedPyShaclPairBehaviourTest(unittest.TestCase):
    """The pySHACL 0.40.1 / rdflib 7.6.0 behaviour the preconditions exist for (cases of the C26 lab). SHACL 2017
    4.5.3-4.5.4 makes incomparable values a violation; pySHACL compares rdflib Literals, so NaN counts as less than
    everything and literals of unrelated datatypes are ordered by datatype IRI. If any of these change, the
    preconditions (numeric base types only, no NaN or infinities) must be reviewed before relaxing them."""

    PREFIX = (f"@prefix ex: <http://example.org/lab#> .\n@prefix xsd: <{XSD}> .\n@prefix sh: <{SH}> .\n")

    def violations(self, component, a_values, b_values) -> int:
        import pyshacl
        import rdflib
        shapes_ttl = self.PREFIX + (f"ex:S a sh:NodeShape ; sh:targetClass ex:R ; sh:property ex:P .\n"
                                    f"ex:P a sh:PropertyShape ; sh:path ex:a ; sh:{component} ex:b .\n")
        data = self.PREFIX + "ex:r a ex:R .\n" + "".join(f"ex:r ex:a {v} .\n" for v in a_values) \
            + "".join(f"ex:r ex:b {v} .\n" for v in b_values)
        _conforms, report, _text = pyshacl.validate(rdflib.Graph().parse(data=data, format="turtle"),
                                                    shacl_graph=rdflib.Graph().parse(data=shapes_ttl, format="turtle"),
                                                    inference="none", allow_warnings=True)
        return len(set(report.subjects(rdflib.RDF.type, rdflib.URIRef(SH + "ValidationResult"))))

    def both(self, a, b) -> tuple[int, int]:
        return self.violations("lessThan", a, b), self.violations("lessThanOrEquals", a, b)

    def test_finite_integers_and_decimals_follow_numeric_order(self):
        cases = {
            ('"1"^^xsd:integer', '"2"^^xsd:integer'): (0, 0),
            ('"2"^^xsd:integer', '"2"^^xsd:integer'): (1, 0),
            ('"3"^^xsd:integer', '"2"^^xsd:integer'): (1, 1),
            ('"1"^^xsd:integer', '"1.0"^^xsd:decimal'): (1, 0),     # integer 1 equals decimal 1.0
            ('"1.0"^^xsd:decimal', '"1"^^xsd:integer'): (1, 0),
            ('"1.000"^^xsd:decimal', '"1"^^xsd:decimal'): (1, 0),   # numeric(18,3) against a shorter scale
            ('"1.5"^^xsd:decimal', '"2"^^xsd:integer'): (0, 0),
            ('"2"^^xsd:integer', '"1.5"^^xsd:decimal'): (1, 1),
        }
        for (a, b), expected in cases.items():
            with self.subTest(a=a, b=b):
                self.assertEqual(self.both([a], [b]), expected)

    def test_a_missing_value_is_never_a_violation(self):
        for a, b in (([], ['"5"^^xsd:integer']), (['"5"^^xsd:integer'], []), ([], [])):
            with self.subTest(a=a, b=b):
                self.assertEqual(self.both(a, b), (0, 0))

    def test_nan_conforms_in_both_directions(self):
        """SHACL and PostgreSQL would both report these; pySHACL does not, hence non_finite_values."""
        nan, five = '"NaN"^^xsd:double', '"5"^^xsd:integer'
        self.assertEqual(self.both([nan], [five]), (0, 0))
        self.assertEqual(self.both([five], [nan]), (0, 0))
        self.assertEqual(self.both([nan], [nan]), (0, 0))
        self.assertEqual(self.both(['"INF"^^xsd:double'], [nan]), (0, 0))

    def test_unrelated_datatypes_are_ordered_by_datatype_iri(self):
        """date against integer conforms one way and violates the other: not 'incomparable is a violation',
        hence column_type for anything but smallint, integer, bigint and numeric."""
        date, five = '"2026-01-01"^^xsd:date', '"5"^^xsd:integer'
        self.assertEqual(self.both([date], [five]), (0, 0))
        self.assertEqual(self.both([five], [date]), (1, 1))
        self.assertEqual(self.both(['"a"'], [five]), (1, 1))


@unittest.skipUnless(HAVE_SHACL, "rdflib and pyshacl are not installed")
class PairShapesOnRowsTest(unittest.TestCase):
    """Each operator on the same hand-made rows: the generated pair shapes, the pair data graph and pySHACL must
    flag exactly the rows the predicate selects (evaluated here in Python, NULL as unknown)."""

    ROWS = [   # id, a (integer), b (numeric)
        (1, 1, Decimal("1.00")), (2, 2, Decimal("1.50")), (3, 1, Decimal("2.00")), (4, None, Decimal("1")),
        (5, 1, None), (6, None, None), (7, -3, Decimal("-3.000")), (8, 0, Decimal("0")), (9, 10, Decimal("9.99")),
    ]
    OPS = {"mix_gt": ">", "mix_ge": ">=", "mix_lt": "<", "mix_le": "<="}

    @classmethod
    def config(cls, severity="violation"):
        cfg = demo_config()
        cfg.entities["pair_mix"] = Entity("pair_mix", "sxc_demo.pair_mix", ["id"])
        cfg.shapes += [Shape(id=sid, target="pair_mix", sql_violation=f"t.a {op} t.b", severity=severity)
                       for sid, op in cls.OPS.items()]
        cfg.verify.shacl_pairs = True
        return cfg

    @classmethod
    def expected(cls) -> dict:
        return {sid: {key(i) for i, a, b in cls.ROWS if violates(a, op, b)} for sid, op in cls.OPS.items()}

    def run_pairs(self, cfg, ttl=None):
        pairs = [s for s in shacl_check.pair_plan(cfg) if s.target == "pair_mix"]
        columns = shacl_check.pair_columns(pairs, "pair_mix")
        self.assertEqual(columns, ["a", "b"])
        rows = [[i, a, b] for i, a, b in self.ROWS]
        graph = shacl_check.build_pair_graph(cfg, pairs, {"pair_mix": rows})
        return shacl_check.pair_focus_keys(cfg, pairs, shacl_check.validate(graph, ttl or shapes.pair_shapes_ttl(cfg, pairs)))

    def test_every_operator_flags_exactly_the_rows_its_predicate_selects(self):
        expected = self.expected()
        self.assertEqual(expected, {"mix_gt": {key(2), key(9)}, "mix_ge": {key(1), key(2), key(7), key(8), key(9)},
                                    "mix_lt": {key(3)}, "mix_le": {key(1), key(3), key(7), key(8)}})
        self.assertEqual(self.run_pairs(self.config()), expected)
        self.assertEqual(self.run_pairs(self.config("warning")), expected, "severity does not change which rows violate")

    def test_the_graph_holds_types_and_non_null_literals_only(self):
        import rdflib
        cfg = self.config()
        pairs = [s for s in shacl_check.pair_plan(cfg) if s.target == "pair_mix"]
        graph = shacl_check.build_pair_graph(cfg, pairs, {"pair_mix": [[i, a, b] for i, a, b in self.ROWS]})
        a_pred = rdflib.URIRef(naming.rdf_value_predicate(cfg, "pair_mix", "a"))
        b_pred = rdflib.URIRef(naming.rdf_value_predicate(cfg, "pair_mix", "b"))
        self.assertEqual(len(set(graph.subjects(rdflib.RDF.type, rdflib.URIRef(naming.rdf_entity_class(cfg, "pair_mix"))))), 9)
        self.assertEqual(len(list(graph.objects(None, a_pred))), 7)
        self.assertEqual(len(list(graph.objects(None, b_pred))), 7)
        self.assertEqual({str(v.datatype) for v in graph.objects(None, a_pred)}, {XSD + "integer"})
        self.assertEqual({str(v.datatype) for v in graph.objects(None, b_pred)}, {XSD + "decimal"})
        self.assertEqual(len(graph), 9 + 7 + 7)

    def test_pair_shapes_pass_the_generated_shape_preflight(self):
        cfg = self.config()
        pairs = shacl_check.pair_plan(cfg)
        self.assertEqual([s.id for s in pairs], ["open_le_total", *self.OPS])
        ttl = shapes.pair_shapes_ttl(cfg, pairs)
        self.assertEqual(shacl_check.validate_generated_shapes(ttl), (True, 0))
        self.assertEqual(ttl.count("sh:lessThanOrEquals"), 3)
        self.assertEqual(ttl.count("sh:lessThan "), 2)
        self.assertNotIn("sh:equals", ttl)
        self.assertNotIn("sh:disjoint", ttl)
        self.assertIn(shapes.turtle_iri(naming.rdf_pair_shape(cfg, "open_le_total")), ttl)
        self.assertEqual(ttl, shapes.pair_shapes_ttl(cfg, pairs), "deterministic")

    def test_a_wrong_direction_is_visible(self):
        """Swapping lessThan for lessThanOrEquals changes the flagged rows exactly on equal values."""
        cfg = self.config()
        pairs = [s for s in shacl_check.pair_plan(cfg) if s.target == "pair_mix"]
        ttl = shapes.pair_shapes_ttl(cfg, pairs).replace("sh:lessThanOrEquals", "sh:lessThan")
        got = self.run_pairs(cfg, ttl)
        self.assertEqual(got["mix_gt"], self.expected()["mix_ge"])
        self.assertNotEqual(got["mix_gt"], self.expected()["mix_gt"])


# ---- PostgreSQL ------------------------------------------------------------------------------------------
@unittest.skipUnless(pgfixture.available() and HAVE_SHACL, "needs SXC_TEST_ADMIN_DSN, rdflib and pyshacl")
class ReasonShaclPairsTest(unittest.TestCase):
    """build and reason on the dirty demo plus small tables. pair_mix: the four operators on integer against
    numeric(10,3) values (equal values at different scales, NULLs). pair_nan / pair_inf: numeric columns with NaN /
    -Infinity in one row. pair_float: double precision columns. pair_dup: no primary key and two rows with key 1.
    commented: the demo rule with a trailing comment, outside the strict form."""

    database = "sxc_t_shacl_pairs"
    MIX = PairShapesOnRowsTest.ROWS
    EXTRA_SHAPES = [("nan_gt", "pair_nan"), ("inf_gt", "pair_inf"), ("float_gt", "pair_float"), ("dup_gt", "pair_dup")]

    @classmethod
    def config(cls, pairs=True):
        cfg = demo_config()
        for name in ("pair_mix", "pair_nan", "pair_inf", "pair_float", "pair_dup"):
            cfg.entities[name] = Entity(name, f"sxc_demo.{name}", ["id"])
        cfg.shapes += [Shape(id=sid, target="pair_mix", sql_violation=f"t.a {op} t.b")
                       for sid, op in PairShapesOnRowsTest.OPS.items()]
        cfg.shapes += [Shape(id=sid, target=target, sql_violation="t.lo > t.hi") for sid, target in cls.EXTRA_SHAPES]
        cfg.shapes.append(Shape(id="commented", target="sales_order", sql_violation="t.qty_open > t.qty_total -- note"))
        cfg.verify.shacl_pairs = pairs
        return cfg

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.database, "dirty")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        conn = db.connect("SXC_PG_OWNER_DSN")
        try:
            with db.write_tx(conn):
                conn.run("CREATE TABLE sxc_demo.pair_mix (id integer PRIMARY KEY, a integer, b numeric(10,3))")
                for i, a, b in cls.MIX:
                    conn.run("INSERT INTO sxc_demo.pair_mix VALUES (:i, CAST(:a AS integer), CAST(:b AS numeric))",
                             i=i, a=a, b=None if b is None else str(b))
                conn.run("CREATE TABLE sxc_demo.pair_nan (id integer PRIMARY KEY, lo numeric, hi numeric)")
                conn.run("INSERT INTO sxc_demo.pair_nan VALUES (1, 'NaN', 1), (2, 2, 1), (3, 1, 2)")
                conn.run("CREATE TABLE sxc_demo.pair_inf (id integer PRIMARY KEY, lo numeric, hi numeric)")
                conn.run("INSERT INTO sxc_demo.pair_inf VALUES (1, 1, '-Infinity'), (2, 2, 1)")
                conn.run("CREATE TABLE sxc_demo.pair_float (id integer PRIMARY KEY, lo double precision, hi double precision)")
                conn.run("INSERT INTO sxc_demo.pair_float VALUES (1, 0.1, 0.3), (2, 2, 1)")
                conn.run("CREATE TABLE sxc_demo.pair_dup (id integer, lo integer, hi integer)")
                conn.run("INSERT INTO sxc_demo.pair_dup VALUES (1, 1, 2), (1, 3, 2), (2, 5, 1)")
        finally:
            conn.close()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.cfg = cls.config()
        cls.out = cls.tmp / "run"
        stage_build.run(cls.cfg, types.SimpleNamespace(hierarchy=""), cls.out).write(cls.out)
        cls.res = stage_reason.run(cls.cfg, types.SimpleNamespace(only=["shacl"]), cls.out)
        cls.off = stage_reason.run(cls.config(pairs=False), types.SimpleNamespace(only=["shacl"]), cls.copy_out("off"))

    @classmethod
    def copy_out(cls, name: str) -> Path:
        out = cls.tmp / name
        shutil.copytree(cls.out / "build", out / "build")
        return out

    def setUp(self):
        os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(self.fx.check, self.fx.name)

    def reason(self, cfg, name):
        return stage_reason.run(cfg, types.SimpleNamespace(only=["shacl"]), self.copy_out(name))

    def injected_keys(self) -> set:
        return {key(o["order_id"]) for o in self.fx.data["orders"] if int(o["qty_open"]) > int(o["qty_total"])}

    def test_demo_rule_agrees_with_its_view_on_the_three_injected_rows(self):
        injected = self.injected_keys()
        self.assertEqual(len(injected), self.fx.data["expected"]["open_gt_total"])
        self.assertEqual(len(injected), 3)
        c = get_check(self.res, "shacl:pair:open_le_total")
        self.assertEqual((c.status, c.config_ref), ("pass", shapes.config_ref(self.cfg, self.cfg.shapes[3])))
        d = c.detail
        self.assertEqual((d["op"], d["constraint"], d["path_column"], d["compare_column"], d["column_types"]),
                         (">", "lessThanOrEquals", "qty_open", "qty_total", ["numeric", "numeric"]))
        self.assertEqual((d["violations"], d["shacl_focus_nodes"], d["shacl_only"], d["sql_only"]), (3, 3, 0, 0))
        self.assertEqual((d["rows_exported"], d["severity"], d["view"]),
                         (self.fx.loaded["sales_order"] + len(self.MIX), "violation",
                          naming.shape_view(self.cfg, "open_le_total")))
        conn = self.fx.check_conn()
        try:
            got = shacl_check.run_pairs(conn, self.cfg, shacl_check.pair_plan(self.cfg),
                                        shapes.pair_shapes_ttl(self.cfg, shacl_check.pair_plan(self.cfg)))
        finally:
            conn.close()
        self.assertEqual((got["open_le_total"]["shacl_keys"], got["open_le_total"]["sql_keys"]), (injected, injected))
        self.assertEqual(json.dumps(c.detail).count("SO-"), 0, "no key values in the detail")

    def test_all_four_operators_agree_on_integer_against_numeric(self):
        expected = PairShapesOnRowsTest.expected()
        for sid, op in PairShapesOnRowsTest.OPS.items():
            c = get_check(self.res, f"shacl:pair:{sid}")
            n = len(expected[sid])
            self.assertEqual((c.status, c.detail["op"], c.detail["column_types"], c.detail["violations"],
                              c.detail["shacl_focus_nodes"], c.detail["shacl_only"], c.detail["sql_only"]),
                             ("pass", op, ["integer", "numeric"], n, n, 0, 0), sid)
            self.assertEqual(OLD_LINE.format(id=sid) in self.res.not_proven, False, sid)
            self.assertTrue(any(line.startswith(f"shacl:{sid}: ") and f"shacl:pair:{sid}" in line
                                for line in self.res.not_proven), sid)

    def test_non_finite_values_float_columns_and_duplicate_keys_are_not_run(self):
        for sid, reason in (("nan_gt", "non_finite_values"), ("inf_gt", "non_finite_values"),
                            ("float_gt", "column_type"), ("dup_gt", "duplicate_keys")):
            c = get_check(self.res, f"shacl:pair:{sid}")
            self.assertEqual((c.status, c.detail["reason"]), ("not_run", reason), sid)
            self.assertIn(OLD_LINE.format(id=sid), self.res.not_proven)
            self.assertTrue(any(line.startswith(f"shacl:pair:{sid}: not compared") for line in self.res.not_proven), sid)
        self.assertEqual(get_check(self.res, "shacl:pair:nan_gt").detail["non_finite_rows"], 1)
        self.assertEqual(get_check(self.res, "shacl:pair:inf_gt").detail["non_finite_rows"], 1)
        self.assertEqual(get_check(self.res, "shacl:pair:float_gt").detail["column_types"],
                         ["double precision", "double precision"])
        self.assertEqual(get_check(self.res, "shacl:pair:dup_gt").detail["duplicate_key_rows"], 1)
        self.assertEqual(self.res.status, "not_run")
        self.assertIn("shacl:pair", self.res.next_safe_action)

    def test_a_predicate_outside_the_form_keeps_the_sql_only_line(self):
        self.assertFalse([c for c in self.res.checks if c.id == "shacl:pair:commented"])
        self.assertIn(OLD_LINE.format(id="commented"), self.res.not_proven)

    def test_with_the_option_off_nothing_changes(self):
        self.assertFalse([c for c in self.off.checks if c.id.startswith("shacl:pair:")])
        sql_only = [s.id for s in self.cfg.shapes if s.sql_violation]
        self.assertEqual([n for n in self.off.not_proven if n.startswith("shacl:") and "no SHACL form" in n],
                         [OLD_LINE.format(id=sid) for sid in sql_only])
        on = [(c.id, c.status, c.config_ref, c.detail) for c in self.res.checks if not c.id.startswith("shacl:pair:")]
        off = [(c.id, c.status, c.config_ref, c.detail) for c in self.off.checks]
        self.assertEqual(on, off, "the option adds checks and never changes an existing one")

    def test_the_row_cap_counts_pair_rows_after_the_shape_export(self):
        main = get_check(self.off, "shacl:child_exists").detail["rows_exported"]
        pair_rows = get_check(self.res, "shacl:pair:open_le_total").detail["rows_exported"]
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.shacl_max_rows = main + pair_rows - 1
        res = self.reason(cfg, "cap")
        for sid in ("open_le_total", "mix_gt"):
            c = get_check(res, f"shacl:pair:{sid}")
            self.assertEqual((c.status, c.detail["reason"], c.detail["rows"], c.detail["limit"]),
                             ("not_run", "resource_limit", main + pair_rows, main + pair_rows - 1))
        for c in res.checks:
            if c.id.startswith("shacl:") and not c.id.startswith("shacl:pair:"):
                self.assertEqual((c.status, c.detail), (get_check(self.off, c.id).status, get_check(self.off, c.id).detail))
        cfg.verify.shacl_max_rows = main + pair_rows
        self.assertEqual(get_check(self.reason(cfg, "cap_ok"), "shacl:pair:open_le_total").status, "pass")

    def test_a_failed_pair_preflight_leaves_every_pair_check_not_run(self):
        original = shacl_check.validate_generated_shapes

        def preflight(ttl):
            return (False, 2) if "sh:lessThan" in ttl else original(ttl)

        with mock.patch.object(shacl_check, "validate_generated_shapes", preflight):
            res = self.reason(self.cfg, "preflight")
        pairs = [c for c in res.checks if c.id.startswith("shacl:pair:")]
        self.assertEqual(len(pairs), 9)
        self.assertTrue(all(c.status == "not_run" and c.detail["reason"] == "generated_shapes_invalid" for c in pairs))
        self.assertEqual(get_check(res, stage_reason.META_ID).status, "pass")
        self.assertEqual(get_check(res, "shacl:child_exists").status, get_check(self.off, "shacl:child_exists").status)

    def test_a_predicate_changed_after_build_is_not_compared(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.shapes[3].sql_violation = "t.qty_open >= t.qty_total"
        res = self.reason(cfg, "stale")
        c = get_check(res, "shacl:pair:open_le_total")
        self.assertEqual((c.status, c.detail["reason"]), ("not_run", "view_stale"))
        self.assertIn("rerun `sxc.py build`", res.next_safe_action)

    def test_a_shape_not_built_is_not_compared(self):
        out = self.copy_out("not_built")
        f = out / "build" / "result.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        next(c for c in data["checks"] if c["id"] == "shape:mix_gt")["status"] = "error"
        f.write_text(json.dumps(data), encoding="utf-8")
        res = stage_reason.run(self.cfg, types.SimpleNamespace(only=["shacl"]), out)
        c = get_check(res, "shacl:pair:mix_gt")
        self.assertEqual((c.status, c.detail["reason"], c.detail["build_status"]), ("not_run", "shape_not_built", "error"))
        self.assertEqual(get_check(res, "shacl:pair:mix_ge").status, "pass")

    def test_pairs_run_when_no_shape_has_a_shacl_form(self):
        """Only sql_violation shapes: no shapes.ttl comparison and no preflight of it, but the pair checks run."""
        cfg = copy.deepcopy(self.cfg)
        cfg.shapes = [s for s in cfg.shapes if s.id == "open_le_total" or s.id.startswith("mix_")]
        out = self.tmp / "only_pairs"
        stage_build.run(cfg, types.SimpleNamespace(hierarchy=""), out).write(out)
        self.assertEqual(shacl_check.plan(cfg)[0], [])
        res = stage_reason.run(cfg, types.SimpleNamespace(only=["shacl"]), out)
        self.assertEqual([(c.id, c.status) for c in res.checks],
                         [(f"shacl:pair:{s.id}", "pass") for s in cfg.shapes] + [("scope:owl", "not_run")])
        self.assertEqual(get_check(res, "shacl:pair:open_le_total").detail["violations"], 3)
        self.assertIn(stage_reason.PAIR_NOTE, res.not_proven)

    def test_a_wrong_translation_fails(self):
        with mock.patch.dict(shapes.PAIR_OPS, {">": ("a", "lessThan", "b")}):
            res = self.reason(self.cfg, "wrong")
        c = get_check(res, "shacl:pair:mix_gt")
        self.assertEqual((c.status, c.detail["shacl_only"], c.detail["sql_only"]), ("fail", 3, 0))
        self.assertIn(naming.shape_view(self.cfg, "mix_gt"), res.next_safe_action)


if __name__ == "__main__":
    unittest.main()
