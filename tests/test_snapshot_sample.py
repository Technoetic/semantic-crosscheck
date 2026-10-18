"""One SQL snapshot per check, and the detection bound of the V3 pair-test sample.

  ReadOnlyIsolationTest   db.read_only opens READ COMMITTED by default and REPEATABLE READ on request; any other
                          isolation value is refused before a transaction is opened
  SnapshotCallersTest     pair_keys reads the key count and the sample in one REPEATABLE READ transaction, and so
                          does the catalog read of the cube stage; single-statement reads keep the default
  D95Test                 the smallest number of mismatching keys a sample of n out of N detects with 95%
                          probability (zero-acceptance hypergeometric sampling), exactly
  SampleBoundTest         sampled pair tests carry d95_keys and d95_rate, and their not_proven line states the bound
  SampleBlindSurvivorTest a V4 survivor of a suite with a sampled pair test is marked sample_blind_possible, without
                          any status change
  SnapshotPgTest          (PostgreSQL) the isolation level inside the transaction, and a concurrent delete between
                          the two pair_keys statements that a REPEATABLE READ snapshot does not see
"""

import contextlib
import math
import tempfile
import types
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, cube_run, db, mutate, shape_q, stage_cube, stage_verify
from semantic_crosscheck.result import StageError
from test_compare_unit import FakeClient, FakeConn, demo_cfg, demo_model
from test_soundness_unit import SampleConn

RR_BEGIN = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"


class Recorder:
    """Records every statement; answers nothing."""

    def __init__(self):
        self.sql: list[str] = []

    def run(self, sql, **params):
        self.sql.append(sql)
        return None


class ReadOnlyIsolationTest(unittest.TestCase):
    def test_default_is_a_plain_read_only_transaction(self):
        conn = Recorder()
        with db.read_only(conn, 5000):
            pass
        self.assertEqual(conn.sql, ["BEGIN TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 5000", "ROLLBACK"])

    def test_repeatable_read_opens_one_snapshot_transaction(self):
        conn = Recorder()
        with db.read_only(conn, 5000, isolation="repeatable read"):
            conn.run("SELECT 1")
        self.assertEqual(conn.sql, [RR_BEGIN, "SET LOCAL statement_timeout = 5000", "SELECT 1", "ROLLBACK"])

    def test_rolled_back_also_when_the_body_raises(self):
        conn = Recorder()
        with self.assertRaises(KeyError), db.read_only(conn, isolation="repeatable read"):
            raise KeyError("x")
        self.assertEqual(conn.sql[-1], "ROLLBACK")

    def test_other_isolation_values_are_refused_before_any_statement(self):
        for bad in ("serializable", "read committed", "REPEATABLE READ", "repeatable read; COMMIT", "", "rr", 1, True):
            conn = Recorder()
            with self.subTest(isolation=bad), self.assertRaises(ValueError):
                with db.read_only(conn, isolation=bad):
                    conn.run("SELECT 1")
            self.assertEqual(conn.sql, [], "no transaction was opened")


class SnapshotCallersTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.test = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["kind"] == "pair")

    def test_pair_keys_reads_count_and_sample_in_one_repeatable_read_transaction(self):
        sample = [f"K{i}" for i in range(shape_q.PAIR_KEY_CAP)]
        conn = Recording(SampleConn(shape_q.PAIR_KEY_CAP + 1, sample))
        total, keys = shape_q.pair_keys(conn, self.cfg, self.test)
        self.assertEqual((total, len(keys)), (shape_q.PAIR_KEY_CAP + 1, shape_q.PAIR_KEY_CAP))
        kinds = conn.kinds()
        self.assertEqual(kinds, ["rr_begin", "timeout", "count", "sample", "rollback"],
                         "both statements share one REPEATABLE READ snapshot")

    def test_pair_keys_below_the_cap_stays_in_the_same_transaction_form(self):
        conn = Recording(SampleConn(3, []))
        self.assertEqual(shape_q.pair_keys(conn, self.cfg, self.test), (3, None))
        self.assertEqual(conn.kinds(), ["rr_begin", "timeout", "count", "rollback"])

    def test_cube_model_generation_reads_the_catalog_in_one_snapshot(self):
        conn = FakeConn(rows=[["id", "text"]])
        with mock.patch.object(stage_cube.db, "connect", return_value=conn), \
                mock.patch.object(stage_cube.cube_gen, "generate", return_value=({"cubes": []}, [])):
            stage_cube.generate(self.cfg)
        self.assertEqual(conn.statements()[0], RR_BEGIN)
        self.assertEqual(conn.statements()[-1], "ROLLBACK")

    def test_single_statement_reads_keep_the_default(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        mu = types.SimpleNamespace(witness_sql="SELECT 1", witness_params={})
        conn = FakeConn(scalar=2)
        self.assertEqual(mutate.witness(conn, cfg, mu), 2)
        self.assertEqual(conn.statements()[0], "BEGIN TRANSACTION READ ONLY")


class Recording:
    """Wraps a fake connection and classifies its statements."""

    def __init__(self, inner):
        self.inner, self.sql = inner, []

    def run(self, sql, **params):
        self.sql.append(sql)
        return self.inner.run(sql, **params)

    def kinds(self) -> list[str]:
        out = []
        for s in self.sql:
            if s == RR_BEGIN:
                out.append("rr_begin")
            elif s.startswith("BEGIN"):
                out.append("begin")
            elif s.startswith("SET LOCAL statement_timeout"):
                out.append("timeout")
            elif "count(DISTINCT" in s:
                out.append("count")
            elif "ORDER BY md5(" in s:
                out.append("sample")
            elif s == "ROLLBACK":
                out.append("rollback")
            else:
                out.append(s)
        return out


def miss(n: int, total: int, d: int) -> Fraction:
    """C(total - d, n) / C(total, n), exactly."""
    return Fraction(math.comb(total - d, n), math.comb(total, n))


class D95Test(unittest.TestCase):
    def test_known_values_for_a_sample_of_2000(self):
        for total, d95 in ((5_000, 6), (20_000, 29), (100_000, 149), (1_000_000, 1_496)):
            with self.subTest(total=total):
                self.assertEqual(shape_q.d95_keys(2000, total), d95)

    def test_bound_is_the_smallest_with_at_most_5_percent_miss_probability(self):
        for n, total in ((1, 2), (1, 100), (40, 61), (40, 1000), (2000, 2001), (2000, 5000), (300, 7000)):
            with self.subTest(n=n, total=total):
                d = shape_q.d95_keys(n, total)
                self.assertGreaterEqual(d, 1)
                self.assertLessEqual(miss(n, total, d), Fraction(1, 20))
                if d > 1:
                    self.assertGreater(miss(n, total, d - 1), Fraction(1, 20))

    def test_no_sampling_without_more_keys_than_the_sample(self):
        for n, total in ((2000, 2000), (2000, 10), (5, 0), (1, 1)):
            self.assertIsNone(shape_q.d95_keys(n, total), (n, total))

    def test_monotone(self):
        previous = 0
        for total in range(2001, 2600, 7):
            d = shape_q.d95_keys(2000, total)
            self.assertGreaterEqual(d, previous, total)
            previous = d
        previous = None
        for n in (10, 50, 200, 1000, 4000, 9000):
            d = shape_q.d95_keys(n, 10_000)
            if previous is not None:
                self.assertLessEqual(d, previous, n)
            previous = d

    def test_input_validation(self):
        for n, total in ((0, 10), (-1, 10), (5, -1), (True, 10), (5, True), (2.0, 10), (5, 10.0), ("5", 10), (5, None)):
            with self.subTest(n=n, total=total), self.assertRaises(ValueError):
                shape_q.d95_keys(n, total)

    def test_large_totals_stay_fast_and_exact(self):
        d = shape_q.d95_keys(2000, 10**9)
        self.assertLessEqual(miss(2000, 10**9, d), Fraction(1, 20))
        self.assertGreater(miss(2000, 10**9, d - 1), Fraction(1, 20))


class SampleBoundTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.test = next(t for t in shape_q.tests(self.cfg, demo_model(self.cfg)) if t["id"] == "pair:m_firm_open.value+m_on_hand.value")

    def answers(self, keys):
        a, b = self.test["measures"]
        return {(("part.part_id",), (a, b)): [{"part.part_id": k, a: "1", b: "2"} for k in keys],
                (("m_firm_open.key",), (a,)): [{"m_firm_open.key": k, a: "1"} for k in keys],
                (("m_on_hand.key",), (b,)): [{"m_on_hand.key": k, b: "2"} for k in keys]}

    def test_sampled_detail_carries_the_detection_bound(self):
        sample = [f"K{i:05d}" for i in range(shape_q.PAIR_KEY_CAP)]
        st, detail = shape_q.run_test(SampleConn(20_000, sample), FakeClient(answers=self.answers(sample)), self.cfg, {},
                                      self.test)
        self.assertEqual((st, detail["sampled_keys"], detail["total_keys"], detail["d95_keys"]),
                         ("pass", 2000, 20_000, 29))
        self.assertEqual(detail["d95_rate"], 29 / 20_000)

    def test_a_sampled_mismatch_still_fails(self):
        sample = [f"K{i:05d}" for i in range(shape_q.PAIR_KEY_CAP)]
        answers = self.answers(sample)
        a, _b = self.test["measures"]
        answers[(("m_firm_open.key",), (a,))][0][a] = "9"
        st, detail = shape_q.run_test(SampleConn(1_000_000, sample), FakeClient(answers=answers), self.cfg, {}, self.test)
        self.assertEqual((st, detail["mismatched"], detail["d95_keys"]), ("fail", {a: 1}, 1496))

    def test_unsampled_detail_has_no_bound(self):
        st, detail = shape_q.run_test(FakeConn(scalar=3), FakeClient(answers=self.answers(["1", "2", "3"])), self.cfg, {},
                                      self.test)
        self.assertEqual(st, "pass")
        self.assertFalse({"d95_keys", "d95_rate", "sampled_keys"} & set(detail))

    def test_not_proven_keeps_the_prefix_and_states_the_bound(self):
        note = shape_q.sample_note({"sampled_keys": 2000, "total_keys": 1_000_000, "d95_keys": 1496, "d95_rate": 0.001496})
        self.assertTrue(note.startswith("compared 2000 of 1000000 keys (a deterministic sample with verify.seed); the "
                                        "other keys were not compared"), note)
        self.assertIn("a mismatch affecting at least 1496 keys (0.15%) is detected with 95% probability", note)
        self.assertIn("independent of the fixed md5 sample order", note)
        self.assertIn("fewer mismatching keys may be missed", note)

    def test_verify_lists_the_bound_in_not_proven(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        v = stage_verify._Verify(self.cfg, Path(tmp.name), demo_model(self.cfg), ["v3"])
        detail = {"keys": 2000, "sampled_keys": 2000, "total_keys": 5000, "d95_keys": 6, "d95_rate": 0.0012}
        with mock.patch.object(shape_q, "run_test", return_value=("pass", detail)):
            v.v3(None, None, self.test, None)
        (line,) = v.res.not_proven
        self.assertTrue(line.startswith(f"v3:{self.test['id']}: compared 2000 of 5000 keys (a deterministic sample "
                                        "with verify.seed); the other keys were not compared"), line)
        self.assertIn("at least 6 keys (0.12%) is detected with 95% probability", line)

    def test_note_computes_the_bound_when_the_detail_lacks_it(self):
        self.assertEqual(shape_q.sample_note({"sampled_keys": 2000, "total_keys": 2500}),
                         "compared 2000 of 2500 keys (a deterministic sample with verify.seed); the other keys were "
                         "not compared; a mismatch affecting at least 2 keys (0.08%) is detected with 95% probability, "
                         "assuming the mismatching keys are independent of the fixed md5 sample order; fewer "
                         "mismatching keys may be missed")


class SampleBlindSurvivorTest(unittest.TestCase):
    def run_with(self, sampled_items, survivors=("measure_agg_to_max__",)):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        _cfg, model = _demo_model()

        @contextlib.contextmanager
        def fake_serve(cfg_, run_dir, log_path=None):
            yield types.SimpleNamespace(run_dir=Path(run_dir))

        def suite(client):
            return ("pass", "") if client.run_dir.name.startswith(survivors) else ("fail", "v1:q01_explosion")

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(mutate, "witness", lambda conn, c, mu: 3), mock.patch.object(cube_run, "serve", fake_serve):
            checks, _np = mutate.run_mutants(cfg, Path(tmp.name), None, model, suite, closure={"has_component": "pass"},
                                             sampled_items=sampled_items)
        return {cid: (st, det) for cid, st, _ref, det in checks}

    def test_survivors_of_a_sampled_suite_are_marked_without_status_change(self):
        plain = self.run_with(())
        marked = self.run_with(["v3:pair:a+b"])
        self.assertEqual({k: st for k, (st, _d) in plain.items()}, {k: st for k, (st, _d) in marked.items()})
        survivors = [k for k, (st, _d) in marked.items() if st == "fail"]
        self.assertTrue(survivors)
        for cid, (st, det) in marked.items():
            if st == "fail":
                self.assertEqual((det["sample_blind_possible"], det["sampled_items"]), (True, ["v3:pair:a+b"]), cid)
            else:
                self.assertNotIn("sample_blind_possible", det, cid)
        self.assertFalse(any("sample_blind_possible" in det for _st, det in plain.values()))

    def test_verify_passes_only_replayed_sampled_pair_items(self):
        cfg = demo_cfg()
        tests = [t for t in shape_q.tests(cfg, demo_model(cfg)) if t["kind"] == "pair"][:3]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        v = stage_verify._Verify(cfg, Path(tmp.name), demo_model(cfg), ["v4"])
        ids = [f"v3:{t['id']}" for t in tests]
        v.items = [(cid, cid, lambda client: "pass") for cid in ids]
        v.baseline = {ids[0]: "pass", ids[1]: "pass", ids[2]: "fail"}
        v.suite_tests = tests
        tests[0]["total_keys"], tests[0]["sample_keys"] = 2500, ["K1"] * 2000      # sampled, replayed
        tests[1]["total_keys"], tests[1]["sample_keys"] = 3, None                 # every key, replayed
        tests[2]["total_keys"], tests[2]["sample_keys"] = 2500, ["K1"] * 2000      # sampled, not replayed
        with mock.patch.object(stage_verify.mutate, "run_mutants", return_value=([], [])) as run:
            v.v4(FakeConn())
        self.assertEqual(run.call_args.kwargs["sampled_items"], [ids[0]])


def _demo_model():
    from test_mutate import demo_model as catalog_model
    return catalog_model()


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class SnapshotPgTest(unittest.TestCase):
    NAME = "sxc_snapshot_pg"

    @classmethod
    def setUpClass(cls):
        a = pgfixture._admin()
        try:
            a.run(f'DROP DATABASE IF EXISTS "{cls.NAME}" WITH (FORCE)')
            a.run(f'CREATE DATABASE "{cls.NAME}"')
        finally:
            a.close()

    @classmethod
    def tearDownClass(cls):
        a = pgfixture._admin()
        try:
            a.run(f'DROP DATABASE IF EXISTS "{cls.NAME}" WITH (FORCE)')
        finally:
            a.close()

    def connect(self):
        conn = pgfixture._admin(self.NAME)
        self.addCleanup(conn.close)
        return conn

    def test_isolation_inside_the_transaction(self):
        conn = self.connect()
        with db.read_only(conn, isolation="repeatable read"):
            self.assertEqual(db.scalar(conn, "SHOW transaction_isolation"), "repeatable read")
            self.assertEqual(db.scalar(conn, "SHOW transaction_read_only"), "on")
        with db.read_only(conn):
            self.assertEqual(db.scalar(conn, "SHOW transaction_isolation"), "read committed")
            self.assertEqual(db.scalar(conn, "SHOW transaction_read_only"), "on")

    def test_pair_keys_does_not_see_a_delete_between_its_statements(self):
        writer = self.connect()
        writer.run("DROP TABLE IF EXISTS public.snap_keys")
        writer.run("CREATE TABLE public.snap_keys (k text)")
        writer.run("INSERT INTO public.snap_keys SELECT 'k' || g FROM generate_series(1, 15) g")
        reader = self.connect()
        cfg = types.SimpleNamespace(source=types.SimpleNamespace(statement_timeout_ms=60000),
                                    verify=types.SimpleNamespace(seed=7))
        test = {"keys_from": ["public.snap_keys", "k"]}
        original = reader.run

        def run(sql, **params):
            out = original(sql, **params)
            if "count(DISTINCT" in sql:                      # another session commits between the two statements
                writer.run("DELETE FROM public.snap_keys WHERE k <> 'k1'")
            return out
        with mock.patch.object(shape_q, "PAIR_KEY_CAP", 10), mock.patch.object(reader, "run", side_effect=run):
            total, sample = shape_q.pair_keys(reader, cfg, test)
        self.assertEqual((total, len(sample)), (15, 10), "the sample comes from the snapshot the count saw")
        self.assertEqual(db.scalar(reader, "SELECT count(*) FROM public.snap_keys"), 1, "the delete did commit")


if __name__ == "__main__":
    unittest.main()
