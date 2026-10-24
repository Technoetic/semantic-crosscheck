"""Regression tests of the verification-soundness fixes: no database, no Cube.

  CanonKindTest        typed canonical forms (string, number, time, boolean) and the untyped fallback
  TypedCompareTest     compare_rows with the member types of the model: distinct text keys, timestamps with a time
                       of day, the type map derived from the generated model and used by the verify stage
  MutationSuiteTest    only a failing suite item kills a mutant; an erroring item is retried once, then leaves the
                       mutant unjudged (error, suite_error), also through the verify stage with the real run_mutants
  ClosureGateTest      V4 closure operators follow build's closure check; survivors of a suite with items left out
                       are not_run
  ScopeTest            verify --only/--gold and reason --only leave not_run scope:* checks, so a narrowed run never
                       passes; an unknown mutation operator is exit 2 before V1
  PairSampleTest       V3 pair tests above shape_q.PAIR_KEY_CAP compare a deterministic sample and say so
  NextActionTest       next_safe_action never sends the agent to a server.log
  RdfKeyTest           SQL view keys use the same canon.key form as the RDF row IRIs
"""

import argparse
import datetime as dt
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import canon, compare, config, cube_gen, cube_run, mutate, shacl_check, shape_q, stage_reason
from semantic_crosscheck import stage_verify
from semantic_crosscheck.result import EXIT_CONFIG, Check, Result, StageError
from test_compare_unit import FakeClient, FakeConn, demo_cfg, demo_model

UTC = dt.timezone.utc


def text_gold(sql_path: Path) -> config.Gold:
    """A gold query over the demo's part cube whose key is text (part_id) and whose SQL file lives outside the repo."""
    return config.Gold(id="g_text", sql=str(sql_path), key=["code"], map={"code": "part.part_id", "cls": "part.part_class"},
                       cube_query={"dimensions": ["part.part_id", "part.part_class"]})


class CanonKindTest(unittest.TestCase):
    def test_string_is_exact_text(self):
        for a, b in (("1.10", "1.1"), ("1e3", "1000"), ("01", "1"), ("2.50", "2.5"), (" a", "a"), ("A", "a")):
            self.assertFalse(canon.same(a, b, "string"), (a, b))
        self.assertEqual(canon.canon("true", "string"), "true")
        self.assertTrue(canon.same("1.10", "1.10", "string"))
        self.assertEqual(canon.canon(Decimal("2.50"), "string"), "2.5", "a non-text value keeps its untyped form")
        self.assertIsNone(canon.canon(None, "string"))
        self.assertFalse(canon.same(None, "", "string"))

    def test_number_is_an_exact_decimal(self):
        for a, b in (("2.50", Decimal("2.5")), ("1e3", 1000), ("-0.0", 0), (1.5, "1.50"), ("+7", 7), (".5", "0.5"),
                     (Decimal("NaN"), "NaN"), (float("inf"), "Infinity")):
            self.assertTrue(canon.same(a, b, "number"), (a, b))
        for a, b in (("2.5", "2.51"), (Decimal("0.1"), "0.10000001"), ("abc", "0")):
            self.assertFalse(canon.same(a, b, "number"), (a, b))
        self.assertEqual(canon.canon("abc", "number"), "abc")

    def test_time_is_one_utc_form(self):
        cube = "2026-01-01T10:30:00.000"
        self.assertEqual(canon.canon(cube, "time"), "2026-01-01T10:30:00.000")
        self.assertTrue(canon.same(dt.datetime(2026, 1, 1, 10, 30), cube, "time"))
        self.assertTrue(canon.same(dt.datetime(2026, 1, 1, 19, 30, tzinfo=dt.timezone(dt.timedelta(hours=9))), cube, "time"))
        self.assertTrue(canon.same(dt.datetime(2026, 1, 1, 10, 30, tzinfo=UTC), "2026-01-01T10:30:00Z", "time"))
        self.assertTrue(canon.same("2026-01-01 12:30:00+02:00", cube, "time"))
        self.assertTrue(canon.same("2026-01-01T11:30:00+0100", cube, "time"))
        self.assertTrue(canon.same(dt.date(2026, 1, 5), "2026-01-05T00:00:00.000", "time"))
        self.assertTrue(canon.same(dt.datetime(2026, 1, 5), "2026-01-05T00:00:00.000", "time"), "midnight timestamp")
        self.assertTrue(canon.same(dt.datetime(2026, 1, 5, 0, 0, 0, 500000), "2026-01-05T00:00:00.500", "time"))
        self.assertFalse(canon.same(dt.date(2026, 1, 5), "2026-01-05T00:00:01.000", "time"))
        self.assertTrue(canon.same(dt.datetime(2026, 1, 5, 0, 0, 0, 1), "2026-01-05T00:00:00.000", "time"),
                        "a microsecond is below the millisecond resolution Cube returns")
        self.assertFalse(canon.same(dt.datetime(2026, 1, 5, 0, 0, 0, 1000), "2026-01-05T00:00:00.000", "time"))
        for bad in ("2026-13-01", "yesterday", "2026-01-01T10:30:00.1234567"):
            self.assertEqual(canon.canon(bad, "time"), canon.canon(bad), bad)

    def test_boolean(self):
        for v in (True, "true", "t", "TRUE"):
            self.assertIs(canon.canon(v, "boolean"), True, v)
        for v in (False, "false", "f"):
            self.assertIs(canon.canon(v, "boolean"), False, v)
        self.assertEqual(canon.canon("yes", "boolean"), "yes")

    def test_untyped_keeps_the_rules_for_callers_without_a_model(self):
        self.assertTrue(canon.same("1.10", "1.1"))
        self.assertTrue(canon.same("true", True))
        self.assertTrue(canon.same(dt.date(2026, 1, 5), "2026-01-05T00:00:00.000"))
        self.assertFalse(canon.same("01", "1"))

    def test_key_form_and_unknown_kind(self):
        self.assertEqual([canon.key(v) for v in ("1.10", "true", Decimal("2.50"), 7, True, None, dt.date(2026, 1, 2))],
                         ["1.10", "true", "2.5", "7", True, None, "2026-01-02"])
        with self.assertRaises(ValueError):
            canon.canon("x", "geo")


class TypedCompareTest(unittest.TestCase):
    def setUp(self):
        self.gold = config.Gold(id="g", sql="unused.sql", key=["k"], map={"k": "c.k", "v": "c.v", "at": "c.at"},
                                zero_if_null=["v"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_distinct_text_keys_stay_distinct(self):
        gold = [{"k": "1.1", "v": 1, "at": None}, {"k": "1.10", "v": 2, "at": None}, {"k": "1e3", "v": 3, "at": None},
                {"k": "1000", "v": 4, "at": None}]
        cube = [{"k": "1000", "v": "4", "at": None}, {"k": "1e3", "v": "3", "at": None}, {"k": "1.10", "v": "2", "at": None},
                {"k": "1.1", "v": "1", "at": None}]
        typed = compare.compare_rows(self.gold, gold, cube, {"k": "string", "v": "number", "at": "time"})
        self.assertTrue(typed["pass"], typed)
        self.assertEqual((typed["matched_rows"], typed["duplicate_keys_gold"]), (4, 0))
        untyped = compare.compare_rows(self.gold, gold, cube)
        self.assertEqual((untyped["pass"], untyped["duplicate_keys_gold"], untyped["duplicate_keys_cube"]), (False, 2, 2))

    def test_text_values_differing_in_spelling_fail(self):
        gold, cube = [{"k": "a", "v": 1, "at": "2.10"}], [{"k": "a", "v": "1", "at": "2.1"}]
        self.assertFalse(compare.compare_rows(self.gold, gold, cube, {"at": "string"})["pass"])
        self.assertTrue(compare.compare_rows(self.gold, gold, cube)["pass"], "untyped folds numeric-looking text")

    def test_timestamps_with_a_time_of_day_match_cube(self):
        gold = [{"k": "a", "v": Decimal("2.50"), "at": dt.datetime(2026, 1, 5, 10, 30)}]
        cube = [{"k": "a", "v": "2.5", "at": "2026-01-05T10:30:00.000"}]
        self.assertTrue(compare.compare_rows(self.gold, gold, cube, {"k": "string", "v": "number", "at": "time"})["pass"])
        self.assertFalse(compare.compare_rows(self.gold, gold, cube)["pass"], "untyped never matched a time of day")

    def test_zero_if_null_with_a_number_type(self):
        r = compare.compare_rows(self.gold, [{"k": "a", "v": 0, "at": None}], [{"k": "a", "v": None, "at": None}], {"v": "number"})
        self.assertEqual((r["pass"], r["declared_null_as_zero"]), (True, 1))

    def test_kinds_come_from_the_model(self):
        cfg = demo_cfg()
        model = demo_model(cfg)
        kinds = compare.member_kinds(model)
        self.assertEqual({m: kinds[m] for m in ("part.part_id", "has_component_closure.min_depth", "has_component_closure.qty",
                                                "m_firm_open_dated.ts", "m_firm_open.value", "has_component_closure.pairs")},
                         {"part.part_id": "string", "has_component_closure.min_depth": "number",
                          "has_component_closure.qty": "number", "m_firm_open_dated.ts": "time",
                          "m_firm_open.value": "number", "has_component_closure.pairs": "number"})
        q01 = next(g for g in cfg.gold if g.id == "q01_explosion")
        self.assertEqual(compare.gold_kinds(q01, kinds), {"part_id": "string", "part_class": "string", "min_depth": "number",
                                                          "max_depth": "number", "path_count": "number", "qty": "number"})
        self.assertEqual(compare.gold_kinds(q01, {}), {})

    def run_text_gold(self, sql_text: str, cube_rows: list, gold_rows: list):
        cfg = demo_cfg()
        sql = self.tmp / "g_text.sql"
        sql.write_bytes(sql_text.encode("utf-8"))
        cfg.gold = [text_gold(sql)]
        out = self.tmp / "out"
        model = demo_model(cfg)
        cube_gen.write_model(model, out / "cube" / "model")
        Result("cube").write(out)
        conn = FakeConn(cols=["code", "cls"], data=gold_rows)
        client = FakeClient(data=cube_rows)
        with mock.patch.object(stage_verify.db, "connect", return_value=conn), \
                mock.patch.object(stage_verify.cube_run, "serve", return_value=contextlib_value(client)):
            res = stage_verify.run(cfg, argparse.Namespace(only=["v1", "v2"], gold=[]), out)
        return res, conn

    def test_verify_compares_in_the_member_types_and_reads_a_bom(self):
        gold_rows = [("1.10", "assembly"), ("1.1", "raw"), ("true", "raw")]
        cube_rows = [{"part.part_id": "true", "part.part_class": "raw"}, {"part.part_id": "1.1", "part.part_class": "raw"},
                     {"part.part_id": "1.10", "part.part_class": "assembly"}]
        res, conn = self.run_text_gold(chr(0xFEFF) + "SELECT 1 AS code, 2 AS cls", cube_rows, gold_rows)
        v1 = next(c for c in res.checks if c.id == "v1:g_text")
        self.assertEqual((v1.status, v1.detail["matched_rows"], v1.detail["duplicate_keys_gold"]), ("pass", 3, 0))
        prepared = next(c[1] for c in conn.calls if c[0] == "prepare")
        self.assertTrue(prepared.startswith("SELECT"), "the BOM is not part of the SQL")


def contextlib_value(value):
    """A context manager yielding value, standing in for cube_run.serve."""
    cm = mock.MagicMock()
    cm.__enter__.return_value = value
    cm.__exit__.return_value = False
    return cm


def stage_v4(case: unittest.TestCase, item_status: str, closure_status: str = "pass", operators=("measure_filter_drop",)):
    """_Verify.v4 with the real run_mutants: witnesses stubbed to 4, Cube stubbed, every suite item answering item_status
    against a mutant. Returns the _Verify and the names of the mutant directories Cube was started for."""
    cfg = demo_cfg()
    cfg.verify.mutation_operators = list(operators)
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    case.addCleanup(tmp.cleanup)
    out = Path(tmp.name)
    build = Result("build")
    build.add("closure:has_component", closure_status, "hierarchies.has_component")
    build.write(out)
    v = stage_verify._Verify(cfg, out, demo_model(cfg), ["v4"])
    v.items = [("v1:q03_stock", "v1:q03_stock", lambda client: "pass" if client == "original" else item_status),
               ("v3:total:m_on_hand.value", "v3:total:m_on_hand.value", lambda client: "pass")]
    v.baseline = {"v1:q03_stock": "pass", "v3:total:m_on_hand.value": "pass"}
    served = []

    def serve(cfg_, run_dir, log_path=None):
        served.append(Path(run_dir).name)
        return contextlib_value("mutant")

    with mock.patch.object(mutate, "witness", lambda conn, cfg_, mu: 4), mock.patch.object(cube_run, "serve", serve):
        v.v4(FakeConn())
    return v, served


class MutationSuiteTest(unittest.TestCase):
    def test_first_item_that_does_not_pass_decides(self):
        calls = []

        def item(name, *statuses):
            answers = iter(statuses)

            def fn(client):
                calls.append(name)
                return next(answers)
            return name, fn

        suite = stage_verify.mutation_suite([item("v1:a", "pass"), item("v2:b:top[0]", "error", "error"), item("v3:c", "fail")])
        self.assertEqual(suite("client"), ("error", "v2:b:top[0]"))
        self.assertEqual(calls, ["v1:a", "v2:b:top[0]", "v2:b:top[0]"], "an erroring item is retried once, then the suite stops")
        calls.clear()
        suite = stage_verify.mutation_suite([item("v1:a", "error", "pass"), item("v3:c", "fail")])
        self.assertEqual(suite("client"), ("fail", "v3:c"), "a transient error is not a verdict")
        self.assertEqual(calls, ["v1:a", "v1:a", "v3:c"])
        self.assertEqual(stage_verify.mutation_suite([item("v1:a", "pass")])("client"), ("pass", ""))

    def test_an_erroring_item_makes_the_mutant_an_error_never_a_kill(self):
        v, served = stage_v4(self, "error")
        self.assertEqual(served, ["measure_filter_drop__firm_open", "measure_filter_drop__firm_open_dated"])
        for c in v.res.checks:
            self.assertEqual((c.status, c.detail["reason"], c.detail["item"]), ("error", "suite_error", "v1:q03_stock"), c.id)
            self.assertNotIn("killed_by", c.detail)
        self.assertEqual(v.res.status, "error")
        self.assertIn("suite item v1:q03_stock", stage_verify._next_action(v.res))
        v, _served = stage_v4(self, "fail")
        self.assertEqual({(c.status, c.detail["killed_by"]) for c in v.res.checks}, {("pass", "v1:q03_stock")})
        v, _served = stage_v4(self, "pass")
        self.assertEqual({(c.status, c.detail["killed_by"]) for c in v.res.checks}, {("fail", "")}, "a complete suite: survivors fail")


class ClosureGateTest(unittest.TestCase):
    def test_closure_operators_are_not_run_when_the_closure_did_not_pass(self):
        v, served = stage_v4(self, "fail", closure_status="fail",
                             operators=("closure_depth_shift", "closure_end_swap", "closure_join_swap", "measure_filter_drop"))
        by_id = {c.id: c for c in v.res.checks}
        for cid in ("v4:closure_depth_shift:has_component", "v4:closure_end_swap:has_component", "v4:closure_join_swap:firm_open"):
            c = by_id[cid]
            self.assertEqual((c.status, c.config_ref, c.detail["reason"], c.detail["build_check"]),
                             ("not_run", "hierarchies.has_component", "closure_not_passed", "fail"), cid)
            self.assertIn(f"{cid}: closure:has_component did not pass in build (fail)", v.res.not_proven)
        self.assertEqual(served, ["measure_filter_drop__firm_open", "measure_filter_drop__firm_open_dated"])
        self.assertEqual(by_id["v4:measure_filter_drop:firm_open"].status, "pass")
        self.assertEqual(v.res.status, "not_run")

    def test_survivors_of_an_incomplete_suite_are_not_run(self):
        cfg = demo_cfg()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name)
        v = stage_verify._Verify(cfg, out, demo_model(cfg), ["v4"], golds=[(2, cfg.gold[2])])
        self.assertEqual(v.excluded, {"excluded_by_gold"})
        v.items, v.baseline = [("v1:q03_stock", "v1:q03_stock", lambda client: "pass")], {"v1:q03_stock": "pass"}

        def run_mutants(cfg_, out_, conn, model, suite, closure=None, sampled_items=()):
            return [("v4:measure_filter_drop:firm_open", "fail", "measures.firm_open", {"witness": 2, "killed_by": ""}),
                    ("v4:measure_filter_drop:firm_open_dated", "pass", "measures.firm_open_dated",
                     {"witness": 2, "killed_by": "v1:q03_stock"})], []

        with mock.patch.object(stage_verify.mutate, "run_mutants", run_mutants):
            v.v4(FakeConn())
        survivor, killed = v.res.checks
        self.assertEqual((survivor.status, survivor.detail["reason"], survivor.detail["excluded"]),
                         ("not_run", "suite_incomplete", ["excluded_by_gold"]))
        self.assertEqual(killed.status, "pass", "a kill by part of the suite is still a kill")
        self.assertTrue(any(n.startswith("v4:measure_filter_drop:firm_open: survived a suite") for n in v.res.not_proven))

    def test_gated_golds_mark_the_suite_incomplete(self):
        cfg = demo_cfg()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name)
        build = Result("build")
        build.add("closure:has_component", "fail", "hierarchies.has_component")
        build.write(out)
        v = stage_verify._Verify(cfg, out, demo_model(cfg), ["v4"])
        v.plan_gold(FakeConn(), 0, cfg.gold[0])
        self.assertEqual(v.excluded, {"closure_not_passed"})


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def write_stages(self):
        cube_gen.write_model(demo_model(self.cfg), self.out / "cube" / "model")
        Result("cube").write(self.out)
        build = Result("build")
        build.add("closure:has_component", "fail", "hierarchies.has_component")
        build.write(self.out)

    def verify(self, only, gold):
        with mock.patch.object(stage_verify.db, "connect", return_value=FakeConn()), \
                mock.patch.object(stage_verify.cube_run, "serve", side_effect=AssertionError("Cube must not start")):
            return stage_verify.run(self.cfg, argparse.Namespace(only=only, gold=gold), self.out)

    def test_narrowed_verify_leaves_scope_checks_and_never_passes(self):
        self.write_stages()
        res = self.verify(["v1"], ["q01_explosion", "q04_depth_histogram"])
        scope = [(c.id, c.status, c.config_ref, c.detail) for c in res.checks if c.id.startswith("scope:")]
        self.assertEqual(scope, [("scope:v2", "not_run", "", {"reason": "excluded_by_only"}),
                                 ("scope:v3", "not_run", "", {"reason": "excluded_by_only"}),
                                 ("scope:v4", "not_run", "", {"reason": "excluded_by_only"}),
                                 ("scope:gold:q02_where_used", "not_run", "", {"reason": "excluded_by_gold"}),
                                 ("scope:gold:q03_stock", "not_run", "", {"reason": "excluded_by_gold"}),
                                 ("scope:gold:q05_firm_window", "not_run", "", {"reason": "excluded_by_gold"})])
        self.assertNotEqual(res.status, "pass")

    def test_everything_kept_passing_still_is_not_a_pass(self):
        res = Result("verify")
        res.add("v1:q03_stock", "pass", "gold[2] q03_stock")
        res.add("scope:v4", "not_run", "", reason="excluded_by_only")
        self.assertEqual(res.status, "not_run")
        self.assertIn("rerun verify without them", stage_verify._next_action(res))

    def test_unknown_mutation_operator_is_rejected_before_any_check(self):
        self.cfg.verify.mutation_operators = ["closure_depth_shift", "closure_depth_shfit"]
        for only in (["v1", "v2", "v3", "v4"], ["v1"]):
            with mock.patch.object(stage_verify.db, "connect", side_effect=AssertionError("no database work")), \
                    self.assertRaises(StageError) as e:
                stage_verify.run(self.cfg, argparse.Namespace(only=only, gold=[]), self.out)
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
            self.assertIn("closure_depth_shfit", str(e.exception))
        self.assertFalse((self.out / "cube").exists(), "rejected before the cube stage is even looked for")

    def reason(self, only, cfg):
        Result("build").write(self.out)
        return stage_reason.run(cfg, types.SimpleNamespace(only=only), self.out)

    def test_narrowed_reason_leaves_a_scope_check(self):
        cfg = demo_cfg()
        cfg.shapes = []
        res = self.reason(["shacl"], cfg)
        self.assertEqual([(c.id, c.status, c.detail) for c in res.checks], [("scope:owl", "not_run", {"reason": "excluded_by_only"})])
        self.assertEqual(res.status, "not_run")
        self.assertIn("without --only", res.next_safe_action)
        cfg = demo_cfg()
        cfg.ontology = config.Ontology()
        res = self.reason(["owl"], cfg)
        self.assertEqual([c.id for c in res.checks], ["scope:shacl"])
        self.assertIn("owl: the configuration has no [ontology] section; the OWL checks were not run", res.not_proven)

    def test_reason_next_action_with_a_scope_check(self):
        res = Result("reason")
        res.add("shacl:qty_positive", "pass", "")
        res.add("scope:owl", "not_run", "", reason="excluded_by_only")
        self.assertIn("rerun reason without --only", stage_reason.next_action(demo_cfg(), res))


class PairSampleTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.test = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"] == "pair:m_firm_open.value+m_on_hand.value")

    def answers(self, keys):
        a, b = self.test["measures"]
        return {(("part.part_id",), (a, b)): [{"part.part_id": k, a: "1", b: "2"} for k in keys],
                (("m_firm_open.key",), (a,)): [{"m_firm_open.key": k, a: "1"} for k in keys],
                (("m_on_hand.key",), (b,)): [{"m_on_hand.key": k, b: "2"} for k in keys]}

    def test_test_names_its_key_source_and_key_types(self):
        self.assertEqual(self.test["keys_from"], ["sxc_demo.part", "part_id"])
        self.assertEqual(self.test["kinds"], {"part.part_id": "string", "m_firm_open.key": "string", "m_on_hand.key": "string"})
        closure = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"].startswith("pair:has_component_closure"))
        self.assertEqual(closure["keys_from"], ["sxc_out.has_component_closure", "ancestor"])

    def test_few_keys_compare_every_key_without_filters(self):
        conn = FakeConn(scalar=3)
        client = FakeClient(answers=self.answers(["1.1", "1.10", "x"]))
        st, detail = shape_q.run_test(conn, client, self.cfg, {}, self.test)
        self.assertEqual((st, detail["keys"], detail["duplicate_keys"]), ("pass", 3, 0), "text keys stay distinct")
        self.assertNotIn("sampled_keys", detail)
        self.assertTrue(all("filters" not in q for q in client.queries))

    def test_many_keys_compare_a_deterministic_sample(self):
        sample = [f"K{i:05d}" for i in range(shape_q.PAIR_KEY_CAP)]
        conn = SampleConn(shape_q.PAIR_KEY_CAP + 500, sample)
        client = FakeClient(answers=self.answers(sample))
        st, detail = shape_q.run_test(conn, client, self.cfg, {}, self.test)
        self.assertEqual((st, detail["sampled_keys"], detail["total_keys"], detail["keys"]),
                         ("pass", shape_q.PAIR_KEY_CAP, shape_q.PAIR_KEY_CAP + 500, shape_q.PAIR_KEY_CAP))
        for q in client.queries:
            (flt,) = q["filters"]
            self.assertEqual((flt["member"], flt["operator"], flt["values"]), (q["dimensions"][0], "equals", sample))
        count_sql, sample_sql = conn.queries
        self.assertEqual(count_sql, ('SELECT count(DISTINCT t."part_id") FROM "sxc_demo"."part" t', {}))
        self.assertIn('ORDER BY md5(s.k::text || CAST(:seed AS text)) COLLATE "C", s.k LIMIT :n', sample_sql[0])
        self.assertEqual(sample_sql[1], {"seed": "7", "n": shape_q.PAIR_KEY_CAP})
        self.assertNotIn("K00001", repr(detail))
        shape_q.run_test(conn, client, self.cfg, {}, self.test)
        self.assertEqual(len(conn.queries), 2, "the sample is read once and reused by every replay")

    def test_verify_lists_a_sampled_pair_test_as_not_proven(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        v = stage_verify._Verify(self.cfg, Path(tmp.name), demo_model(self.cfg), ["v3"])
        with mock.patch.object(shape_q, "run_test", return_value=("pass", {"keys": 2000, "sampled_keys": 2000, "total_keys": 2500})):
            v.v3(None, None, self.test, None)
        self.assertEqual(v.res.not_proven, [f"v3:{self.test['id']}: compared 2000 of 2500 keys (a deterministic sample with "
                                            "verify.seed); the other keys were not compared; a mismatch affecting at "
                                            "least 2 keys (0.08%) is detected with 95% probability, assuming the "
                                            "mismatching keys are independent of the fixed md5 sample order; fewer "
                                            "mismatching keys may be missed; the NULL key group is not compared in a "
                                            "sample (its equals filter leaves it out), so a mismatch there is not "
                                            "detected"])

    def test_v4_says_when_its_pair_tests_were_sampled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        v = stage_verify._Verify(self.cfg, Path(tmp.name), demo_model(self.cfg), ["v4"])
        cid = f"v3:{self.test['id']}"
        v.items, v.baseline, v.suite_tests = [(cid, cid, lambda client: "pass")], {cid: "pass"}, [self.test]
        with mock.patch.object(stage_verify.mutate, "run_mutants", return_value=([], [])):
            v.v4(FakeConn())
            self.assertEqual(v.res.not_proven, [])
            self.test["total_keys"], self.test["sample_keys"] = 2500, ["K1"]
            v.v4(FakeConn())
        self.assertEqual(v.res.not_proven, [f"v4: the suite's pair tests compare a sample of their keys ({cid}), so a mutant "
                                            "that changes only other keys can survive them"])


class SampleConn:
    """Answers pair_keys: the key count, then the sample, inside the read-only transaction statements."""

    def __init__(self, total: int, sample: list):
        self.total, self.sample, self.queries = total, sample, []

    def run(self, sql, **params):
        if sql.startswith(("BEGIN", "SET LOCAL", "ROLLBACK")):
            return None
        self.queries.append((sql, params))
        return [[self.total]] if "count(DISTINCT" in sql else [[k] for k in self.sample]


class NextActionTest(unittest.TestCase):
    ERRORS = [
        Check("v1:q01_explosion", "error", {"side": "cube", "error": "CubeError", "cube": "query_error"}, "gold[0] q01_explosion"),
        Check("v1:q01_explosion", "error", {"side": "cube", "error": "CubeError", "http_status": 400}, "gold[0] q01_explosion"),
        Check("v1:q01_explosion", "error", {"side": "cube", "error": "CubeError", "cube": "timeout"}, "gold[0] q01_explosion"),
        Check("v1:q01_explosion", "error", {"side": "cube", "error": "CubeError", "cube": "row_limit"}, "gold[0] q01_explosion"),
        Check("v1:q01_explosion", "error", {"side": "gold", "error": "DatabaseError", "sqlstate": "57014"}, "gold[0] q01_explosion"),
        Check("v4:closure_depth_shift:has_component", "error", {"reason": "suite_error", "item": "v1:q01_explosion"}, "x"),
        Check("v4:closure_depth_shift:has_component", "error", {"reason": "startup", "exit_code": 3}, "x"),
        Check("v4:closure_depth_shift:has_component", "error", {"reason": "model_mismatch"}, "x"),
        Check("v1:q01_explosion", "error", {"reason": "members_not_in_model"}, "gold[0] q01_explosion"),
    ]

    def test_no_action_sends_the_agent_to_a_server_log(self):
        for c in self.ERRORS:
            res = Result("verify")
            res.checks.append(c)
            action = stage_verify._next_action(res)
            self.assertNotIn("server.log", action, c.detail)
            self.assertTrue(action.endswith(("rerun verify", "rerun `sxc.py cube`")), action)
        res = Result("verify")
        res.add("v4:measure_filter_drop:firm_open", "fail", "measures.firm_open", witness=3, killed_by="")
        self.assertIn("add a gold query", stage_verify._next_action(res))


class RdfKeyTest(unittest.TestCase):
    def test_view_keys_use_the_rdf_form(self):
        cfg = demo_cfg()
        shape = next(s for s in cfg.shapes if s.id == "child_exists")
        conn = FakeConn(rows=[("1.10", "x", 1), ("1.1", "x", 1), ("true", "x", 1)])
        keys = shacl_check.sql_keys(conn, cfg, shape)
        self.assertEqual(keys, {("1.10", "x", "1"), ("1.1", "x", "1"), ("true", "x", "1")})


if __name__ == "__main__":
    unittest.main()
