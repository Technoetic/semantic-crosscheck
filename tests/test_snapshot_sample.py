"""One SQL snapshot per check.

  ReadOnlyIsolationTest   db.read_only opens READ COMMITTED by default and REPEATABLE READ on request; any other
                          isolation value is refused before a transaction is opened
  SnapshotCallersTest     pair_keys reads the key count and the sample in one REPEATABLE READ transaction, and so
                          does the catalog read of the cube stage; single-statement reads keep the default
  SnapshotPgTest          (PostgreSQL) the isolation level inside the transaction, and a concurrent delete between
                          the two pair_keys statements that a REPEATABLE READ snapshot does not see
"""

import types
import unittest
from unittest import mock

import pgfixture
from semantic_crosscheck import config, db, mutate, shape_q, stage_cube
from test_compare_unit import FakeConn, demo_cfg, demo_model
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
