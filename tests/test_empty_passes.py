"""Comparisons of empty sets by generated checks are disclosed, and their statuses stay what they were.

Why: a check that compares two empty sets passes without having compared anything. Until 0.24.0, among the checks
between generated layers, only a defined class without members said so (V1 and V2 comparisons with no row, empty
strata and an empty closure-oracle graph were already disclosed); `shacl:<id>` when neither pySHACL nor the SQL view
flagged a key, `v5:closure:<h>` when neither OWL nor SQL had a closure pair and a V3 pair test that compared no key
passed without a word. Since 0.25.0 such a pass carries `empty = true` in its detail (the key V1 uses for a comparison
with no row on either side; on a pair test, no key of the together answer was compared, while its alone answers may
hold keys) and a not_proven line: one per reason run for the SHACL checks (`shacl:pair:<id>` included, which compares
two key sets the same way), naming those that had no row to check at all; one for the closure check; one per verify
run for the pair tests. A non-empty pass gets neither, a fail keeps its detail, and the verdicts are the same in both
gold_observation modes.

Unit tests, no database: the stages' own comparison functions, with only what they read mocked (shacl_check.run and
run_pairs, owl.read and owl.reason, Cube answers through a fake client). tests/test_reason.py runs the SHACL and V5
cases on PostgreSQL.
"""

import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import config, owl, shacl_check, shape_q, shapes, stage_reason, stage_verify
from semantic_crosscheck.result import Result
from test_compare_unit import FakeClient, FakeConn, demo_cfg, demo_model
from test_require_mode import set_mode
from test_verify_evidence import VerifyRun

SHAPES = ["child_exists", "qty_positive", "stock_nonnegative"]   # the demo shapes with a SHACL form
NOTE = stage_reason.EMPTY_SHACL_NOTE
NO_ROW = stage_reason.NO_ROW_NOTE
KEY = ("K1",)


def built(cfg, *shape_ids) -> dict:
    """A build result in which shape:<id> passed for every shape id given (the views exist)."""
    return {"checks": [{"id": f"shape:{sid}", "status": "pass"} for sid in shape_ids]}


def compared(flagged: set, viewed: set, group_rows: int = 40, rows: int = 40, group: str = "main") -> dict:
    """One shape's answer of shacl_check.run."""
    return {"shacl_keys": set(flagged), "sql_keys": set(viewed), "rows": rows, "group": group,
            "group_rows": group_rows}


def check(res: Result, cid: str):
    (found,) = [c for c in res.checks if c.id == cid]
    return found


# ---- reason: shacl:<id> ---------------------------------------------------------------------------------
class ShaclEmptyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.planned = shacl_check.plan(self.cfg)[0]
        self.assertEqual([s.id for s in self.planned], SHAPES)

    def reason(self, answers: dict, cfg=None, planned=None) -> Result:
        cfg, planned = cfg or self.cfg, planned or self.planned
        res = Result("reason")
        with mock.patch.object(shacl_check, "run", return_value=answers):
            stage_reason._shacl(None, cfg, built(cfg, *answers), planned, "", res)
        stage_reason._empty_shacl_note(res)
        return res

    def test_a_pass_where_neither_side_flagged_a_key_is_empty_and_listed(self):
        res = self.reason({sid: compared(set(), set()) for sid in SHAPES})
        for sid in SHAPES:
            c = check(res, f"shacl:{sid}")
            self.assertEqual((c.status, c.detail["violations"], c.detail["shacl_focus_nodes"], c.detail["empty"]),
                             ("pass", 0, 0, True), sid)
        self.assertEqual(res.not_proven, [f"shacl:child_exists, shacl:qty_positive, shacl:stock_nonnegative: {NOTE}"])

    def test_a_shape_with_no_row_to_check_is_named_from_group_rows(self):
        res = self.reason({"child_exists": compared(set(), set(), group_rows=0, rows=7),
                           "qty_positive": compared(set(), set(), group_rows=7, rows=7, group="extra:1"),
                           "stock_nonnegative": compared({KEY}, {KEY}, group_rows=7, rows=7, group="extra:1")})
        self.assertEqual(res.not_proven, [f"shacl:child_exists, shacl:qty_positive: {NOTE}; shacl:child_exists {NO_ROW}"])
        self.assertNotIn("empty", check(res, "shacl:stock_nonnegative").detail)

    def test_a_non_empty_pass_and_a_fail_are_unchanged(self):
        res = self.reason({"child_exists": compared({KEY}, {KEY}), "qty_positive": compared(set(), {KEY}),
                           "stock_nonnegative": compared({KEY}, set())})
        self.assertEqual(res.not_proven, [])
        self.assertEqual([(c.id, c.status) for c in res.checks],
                         [("shacl:child_exists", "pass"), ("shacl:qty_positive", "fail"),
                          ("shacl:stock_nonnegative", "fail")])
        for c in res.checks:
            self.assertEqual(list(c.detail), ["violations", "shacl_focus_nodes", "shacl_only", "sql_only", "severity",
                                              "kind", "rows_exported", "value_group", "group_rows", "view"], c.id)
        self.assertEqual((check(res, "shacl:qty_positive").detail["sql_only"],
                          check(res, "shacl:stock_nonnegative").detail["shacl_only"]), (1, 1))

    def test_more_than_ten_ids_are_cut(self):
        cfg = demo_cfg()
        cfg.shapes = [config.Shape(id=f"q{i:02d}", target="bom_line", path="qty_per", min_exclusive=0)
                      for i in range(12)]
        planned = shacl_check.plan(cfg)[0]
        res = self.reason({s.id: compared(set(), set(), group_rows=0) for s in planned}, cfg, planned)
        ids = ", ".join(f"shacl:q{i:02d}" for i in range(10))
        self.assertEqual(res.not_proven, [f"{ids}, and 2 more: {NOTE}; {ids}, and 2 more {NO_ROW}"])

    def test_a_shape_that_did_not_compare_gets_no_flag(self):
        answers = {sid: compared(set(), set()) for sid in SHAPES}
        answers["qty_positive"] = dict(answers["qty_positive"], sql_keys=None)      # the view is gone
        answers["stock_nonnegative"] = dict(answers["stock_nonnegative"], validation_error="ValidationError")
        res = self.reason(answers)
        self.assertEqual([(c.id, c.status, c.detail.get("empty")) for c in res.checks],
                         [("shacl:child_exists", "pass", True), ("shacl:qty_positive", "not_run", None),
                          ("shacl:stock_nonnegative", "error", None)])
        self.assertIn(f"shacl:child_exists: {NOTE}", res.not_proven)


# ---- reason: shacl:pair:<id> -----------------------------------------------------------------------------
class ShaclPairEmptyTest(unittest.TestCase):
    """shacl:pair:<id> compares pySHACL's focus-node keys with the view's keys as a set, as shacl:<id> does, so the
    same rule applies; the pair export (rows_exported, the rows of every pair entity compared in the run, shared by
    every pair check) says whether there was any row to check."""

    def setUp(self):
        self.cfg = demo_cfg()
        self.cfg.verify.shacl_pairs = True
        self.pairs = shacl_check.pair_plan(self.cfg)
        self.assertEqual([s.id for s in self.pairs], ["open_le_total"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        (self.out / "build").mkdir()
        (self.out / "build" / "build.sql").write_bytes(
            "\n".join(stage_reason._view_statement(self.cfg, s) for s in self.pairs).encode("utf-8"))

    def reason(self, pair: dict, shape_answers: dict | None = None) -> Result:
        res = Result("reason")
        build = built(self.cfg, *SHAPES, "open_le_total")
        if shape_answers is not None:
            with mock.patch.object(shacl_check, "run", return_value=shape_answers):
                stage_reason._shacl(None, self.cfg, build, shacl_check.plan(self.cfg)[0], "", res)
        with mock.patch.object(shacl_check, "run_pairs", return_value={"open_le_total": pair}):
            stage_reason._shacl_pairs(None, self.cfg, build, self.out, self.pairs, "", 0, res)
        stage_reason._empty_shacl_note(res)
        return res

    @staticmethod
    def pair(flagged: set, viewed: set, rows: int) -> dict:
        return {"shacl_keys": set(flagged), "sql_keys": set(viewed), "rows": rows, "column_types": ["numeric", "numeric"]}

    def test_an_empty_pair_pass_is_listed_with_the_shapes(self):
        res = self.reason(self.pair(set(), set(), rows=12), {sid: compared(set(), set()) for sid in SHAPES})
        c = check(res, "shacl:pair:open_le_total")
        self.assertEqual((c.status, c.detail["empty"], c.detail["rows_exported"]), ("pass", True, 12))
        lines = [n for n in res.not_proven if n.endswith(NOTE)]
        self.assertEqual(lines, [f"shacl:child_exists, shacl:qty_positive, shacl:stock_nonnegative, "
                                 f"shacl:pair:open_le_total: {NOTE}"])
        self.assertIn(stage_reason.PAIR_NOTE, res.not_proven)

    def test_an_empty_pair_export_had_no_row_to_check(self):
        res = self.reason(self.pair(set(), set(), rows=0))
        self.assertEqual([n for n in res.not_proven if NOTE in n],
                         [f"shacl:pair:open_le_total: {NOTE}; shacl:pair:open_le_total {NO_ROW}"])

    def test_a_non_empty_pair_pass_and_a_pair_fail_are_unchanged(self):
        for flagged, viewed, status in (({KEY}, {KEY}, "pass"), (set(), {KEY}, "fail")):
            with self.subTest(status=status):
                res = self.reason(self.pair(flagged, viewed, rows=12))
                c = check(res, "shacl:pair:open_le_total")
                self.assertEqual(c.status, status)
                self.assertNotIn("empty", c.detail)
                self.assertFalse([n for n in res.not_proven if NOTE in n])


# ---- reason: v5:closure:<h> and v5:defined:<name> ---------------------------------------------------------
class OwlEmptyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.defined = [d.name for d in self.cfg.ontology.defined]

    def reason(self, owl_pairs: set, sql_pairs: set, members: dict | None = None) -> Result:
        members = members or {name: set() for name in self.defined}
        reasoned = owl.Reasoned(True, 4, 4, 0, len(owl_pairs), 5, closure=set(owl_pairs), members=dict(members),
                                outside=dict.fromkeys(members, 0))
        res = Result("reason")
        with mock.patch.object(stage_reason, "_closure_reason", return_value=""), \
                mock.patch.object(owl, "read", return_value=(object(), set(sql_pairs), dict(members))), \
                mock.patch.object(owl, "reason", return_value=reasoned):
            stage_reason._owl(None, self.cfg, {}, "java", res)
        return res

    def test_a_closure_without_pairs_on_either_side_is_empty_and_listed(self):
        res = self.reason(set(), set())
        c = check(res, "v5:closure:has_component")
        self.assertEqual((c.status, c.detail["owl_pairs"], c.detail["sql_pairs"], c.detail["empty"]), ("pass", 0, 0, True))
        self.assertIn("v5:closure:has_component: the hierarchy has no closure pair in OWL or in SQL, so the comparison "
                      "holds vacuously", res.not_proven)

    def test_a_closure_with_pairs_and_a_closure_that_differs_are_unchanged(self):
        for owl_pairs, sql_pairs, status in (({("A", "B")}, {("A", "B")}, "pass"), (set(), {("A", "B")}, "fail")):
            with self.subTest(status=status):
                res = self.reason(owl_pairs, sql_pairs)
                c = check(res, "v5:closure:has_component")
                self.assertEqual(c.status, status)
                self.assertEqual(list(c.detail), ["owl_pairs", "sql_pairs", "owl_only", "sql_only"])
                self.assertFalse([n for n in res.not_proven if n.startswith("v5:closure:")])

    def test_a_defined_class_without_members_is_flagged_as_before_it_was_listed(self):
        members = {name: set() for name in self.defined}
        members["Composite"] = {"A"}
        res = self.reason({("A", "B")}, {("A", "B")}, members)
        self.assertNotIn("empty", check(res, "v5:defined:Composite").detail)
        for name in self.defined[1:]:
            with self.subTest(defined=name):
                self.assertTrue(check(res, f"v5:defined:{name}").detail["empty"])
                self.assertIn(f"v5:defined:{name}: the class has no members in OWL or in SQL, so the comparison holds "
                              "vacuously", res.not_proven)


# ---- verify: V3 pair tests ---------------------------------------------------------------------------------
class ComparedNoKeyTest(unittest.TestCase):
    def test_each_mode_reads_its_own_count(self):
        cases = [
            ({"keys": 0, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {"a": 0}}, True),
            ({"keys": 0, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {"a": 3}}, True),
            ({"keys": 4, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {"a": 0}}, False),
            ({}, False),                                                     # no count: says nothing
            ({"keys": 0, "sampled_keys": 2000, "total_keys": 2500, "answered_keys": 0}, True),
            ({"keys": 2000, "sampled_keys": 2000, "total_keys": 2500, "answered_keys": 2000}, False),
            ({"keys": 0, "mode": "chunked", "compared_keys": 2001, "answered_keys": 0,
              "null_key_group": {"keys": 0}}, True),
            ({"keys": 1, "mode": "chunked", "compared_keys": 2001, "answered_keys": 0,
              "null_key_group": {"keys": 1}}, False),
            ({"keys": 2001, "mode": "chunked", "compared_keys": 2001, "answered_keys": 2001,
              "null_key_group": {"keys": 0}}, False),
        ]
        for detail, expected in cases:
            with self.subTest(detail=detail):
                self.assertIs(shape_q.compared_no_key(detail), expected)


class VerifyPairEmptyTest(unittest.TestCase):
    """_Verify.v3 on a real pair test of the demo model; Cube's answers come from a fake client, the key count from a
    fake connection (0 keys: the unfiltered mode)."""

    def setUp(self):
        self.cfg = demo_cfg()
        self.model = demo_model(self.cfg)
        self.test = next(t for t in shape_q.tests(self.cfg, self.model) if t["id"] == "pair:m_firm_open.value+m_on_hand.value")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.v = stage_verify._Verify(self.cfg, Path(tmp.name), self.model, ["v3"])

    def answers(self, together=(), alone_a=(), alone_b=()):
        a, b = self.test["measures"]
        return {(("part.part_id",), (a, b)): [{"part.part_id": k, a: "1", b: "2"} for k in together],
                (("m_firm_open.key",), (a,)): [{"m_firm_open.key": k, a: "1"} for k in alone_a],
                (("m_on_hand.key",), (b,)): [{"m_on_hand.key": k, b: "2"} for k in alone_b]}

    def run_v3(self, **answers):
        self.v.v3(FakeConn(scalar=0), FakeClient(answers=self.answers(**answers)), self.test, None)
        self.v.empty_pair_line()
        return check(self.v.res, f"v3:{self.test['id']}")

    def test_a_pass_that_compared_no_key_is_empty_and_listed(self):
        c = self.run_v3()
        self.assertEqual((c.status, c.detail["keys"], c.detail["empty"]), ("pass", 0, True))
        self.assertEqual(self.v.baseline[c.id], "pass", "the V4 baseline is the pass")
        self.assertEqual(self.v.res.not_proven, [f"{c.id}: {shape_q.empty_note()}"])

    def test_alone_keys_without_a_together_key_are_still_no_comparison(self):
        c = self.run_v3(alone_a=["K1"])
        self.assertEqual((c.status, c.detail["keys"], c.detail["alone_only_keys"], c.detail["empty"]),
                         ("pass", 0, {"m_firm_open.value": 1, "m_on_hand.value": 0}, True))

    def test_a_non_empty_pass_and_a_fail_are_unchanged(self):
        c = self.run_v3(together=["K1"], alone_a=["K1"], alone_b=["K1"])
        self.assertEqual((c.status, c.detail), ("pass", {"keys": 1, "mismatched": {}, "duplicate_keys": 0,
                                                         "alone_only_keys": {"m_firm_open.value": 0,
                                                                             "m_on_hand.value": 0}}))
        self.assertEqual(self.v.res.not_proven, [])
        self.v = stage_verify._Verify(self.cfg, self.v.out, self.model, ["v3"])
        c = self.run_v3(alone_a=["K1", "K1"])                            # a duplicate key fails, with no key compared
        self.assertEqual((c.status, c.detail["keys"], c.detail["duplicate_keys"]), ("fail", 0, 1))
        self.assertNotIn("empty", c.detail)
        self.assertEqual(self.v.res.not_proven, [])

    def test_sampled_and_chunked_passes_compared_keys(self):
        details = [{"keys": 2000, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {}, "answered_keys": 2000,
                    "sampled_keys": 2000, "total_keys": 2500},
                   {"keys": 2001, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {}, "answered_keys": 2001,
                    "mode": "chunked", "chunks": 2, "compared_keys": 2001, "total_keys": 2001,
                    "null_key_group": {"keys": 0, "mismatched": {}, "duplicate_keys": 0, "alone_only_keys": {}},
                    "elapsed_ms": 1}]
        for detail in details:
            with self.subTest(mode=detail.get("mode", "sampled")):
                v = stage_verify._Verify(self.cfg, self.v.out, self.model, ["v3"])
                with mock.patch.object(shape_q, "run_test", return_value=("pass", dict(detail))):
                    v.v3(None, None, self.test, None)
                v.empty_pair_line()
                self.assertNotIn("empty", check(v.res, f"v3:{self.test['id']}").detail)
                self.assertFalse([n for n in v.res.not_proven if n.endswith(shape_q.empty_note())])


class VerifyModesTest(VerifyRun):
    """The whole verify stage with every pair test answering that it compared no key: in the default mode and in
    require mode the pair tests pass, carry empty = true and are named by one not_proven line."""

    def _run_test(self, conn, client, cfg, model, test, budget=None, witness=None):
        if test["kind"] == "pair":
            return "pass", {"keys": 0, "mismatched": {}, "duplicate_keys": 0,
                            "alone_only_keys": dict.fromkeys(test["measures"], 0)}
        return "pass", {"equal": True}

    def test_require_mode_keeps_the_verdicts_and_the_disclosure(self):
        outcomes = {}
        for mode in (None, "disclose", "require"):
            with self.subTest(mode=mode):
                set_mode(self, mode)
                shutil.rmtree(self.out, ignore_errors=True)
                res = self.verify()
                pairs = [c for c in res.checks if c.id.startswith("v3:pair:")]
                self.assertEqual(len(pairs), 4)
                self.assertEqual({(c.status, c.detail["empty"]) for c in pairs}, {("pass", True)})
                self.assertEqual([n for n in res.not_proven if n.endswith(shape_q.empty_note())],
                                 [f"{', '.join(c.id for c in pairs)}: {shape_q.empty_note()}"])
                outcomes[mode] = [(c.id, c.status) for c in res.checks if c.id.startswith("v3:")]
        self.assertEqual(outcomes[None], outcomes["require"])
        self.assertEqual(outcomes["disclose"], outcomes["require"])


if __name__ == "__main__":
    unittest.main()
