"""V3 pair tests over every key in chunks (verify.pair_key_budget, design section 7).

  ChunkedBaselineTest  above shape_q.PAIR_KEY_CAP keys and within verify.pair_key_budget the baseline V3 run compares
                       every key: consecutive chunks of PAIR_KEY_CAP in the md5 order of one REPEATABLE READ key read,
                       then the NULL key group through a notSet filter; counts add up over the chunks, a mismatch in
                       any chunk or in the NULL key group fails the test and a query that cannot run makes it an error
  BudgetFallbackTest   above the budget the test keeps the sample and its d95 bound and says the budget was exceeded;
                       a mismatch on a key outside the sample is the false pass that a raised budget closes
  ReplaySampleTest     V4 replays a chunked test against its sample (the first chunk), never every chunk, and a survivor
                       is still marked sample_blind_possible
  UnchangedPathTest    at most PAIR_KEY_CAP keys the queries stay unfiltered (the NULL key group included), whatever the
                       budget; run_test without a budget (the replays) keeps the sample
  BudgetConfigTest     verify.pair_key_budget defaults to PAIR_KEY_CAP and is an integer of at least PAIR_KEY_CAP
  ChunkKeysPgTest      (PostgreSQL) every key within the budget comes from the snapshot of the count, in the order whose
                       first PAIR_KEY_CAP keys are the sample, without the NULL key
"""

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, db, shape_q, stage_verify
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import EXIT_CONFIG, StageError
from test_compare_unit import demo_cfg, demo_model
from test_snapshot_sample import Recording

PAIR = "pair:m_firm_open.value+m_on_hand.value"
A, B = "m_firm_open.value", "m_on_hand.value"
CAP = shape_q.PAIR_KEY_CAP


def keys(n: int) -> list[str]:
    """n distinct synthetic part keys, standing for keys already in the md5 sample order."""
    return [f"P{i:05d}" for i in range(n)]


class KeysConn:
    """Answers pair_keys from distinct non-NULL keys already in the md5 order: the count, then the first :n of them.
    `count` replaces the count (a key read that does not match it)."""

    def __init__(self, ordered: list, count: int | None = None):
        self.keys, self.count, self.queries = list(ordered), len(ordered) if count is None else count, []

    def run(self, sql, **params):
        if sql.startswith(("BEGIN", "SET LOCAL", "ROLLBACK")):
            return None
        self.queries.append((sql, params))
        return [[self.count]] if "count(DISTINCT" in sql else [[k] for k in self.keys[: params["n"]]]


class KeyedClient:
    """Cube for the demo pair test: every measure is 1 per key, alone and together, except the first measure queried
    together on the `bad` keys (2: a fan-out). `null` adds the NULL key group ("same", or "bad" for a fan-out there).
    The answer honours an equals filter (a chunk or the sample) or a notSet filter (the NULL key group) on the grouped
    member; `dup` keys appear twice in the together answer, `missing` keys not at all (alone-only keys), and query
    number `fail_at` raises a CubeError."""

    def __init__(self, every: list, bad=(), null=None, dup=(), missing=(), fail_at=None):
        self.keys, self.bad, self.null = list(every), set(bad), null
        self.dup, self.missing, self.fail_at = set(dup), set(missing), fail_at
        self.queries: list[dict] = []

    def load(self, query):
        self.queries.append(query)
        if self.fail_at == len(self.queries):
            raise CubeError("HTTP 500: P00001")
        (dim,), measures = query["dimensions"], query["measures"]
        together = len(measures) > 1
        filters = query.get("filters")
        if not filters:
            ks = self.keys + ([None] if self.null else [])
        else:
            (f,) = filters
            if f["member"] != dim:
                raise AssertionError(f"filter on {f['member']}, grouped by {dim}")
            if f["operator"] == "equals":
                ks = list(f["values"])
            elif f == {"member": dim, "operator": "notSet"}:
                ks = [None] if self.null else []
            else:
                raise AssertionError(f"unexpected filter {f}")
        rows = []
        for k in ks:
            if together and k in self.missing:
                continue
            fan = together and (self.null == "bad" if k is None else k in self.bad)
            row = {dim: k, **{m: "2" if fan and m == measures[0] else "1" for m in measures}}
            rows.append(row)
            if together and k in self.dup:
                rows.append(dict(row))
        return rows

    def filtered(self, operator: str) -> list[dict]:
        return [q for q in self.queries if any(f["operator"] == operator for f in q.get("filters", []))]


def pair_test(cfg) -> dict:
    return next(t for t in shape_q.tests(cfg, demo_model(cfg)) if t["id"] == PAIR)


def baseline(case: unittest.TestCase, budget, conn, client):
    """_Verify.v3 of the demo pair test, as the verify stage runs it, with verify.pair_key_budget = budget (None keeps
    the configured default): (the _Verify, the check, the test)."""
    cfg = demo_cfg()
    if budget is not None:
        cfg.verify.pair_key_budget = budget
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    v = stage_verify._Verify(cfg, Path(tmp.name), demo_model(cfg), ["v3", "v4"])
    test = pair_test(cfg)
    v.v3(conn, client, test, None)
    (check,) = v.res.checks
    return v, check, test


class ChunkedBaselineTest(unittest.TestCase):
    def test_one_key_over_the_cap_gives_two_chunks_the_last_of_one_key(self):
        every = keys(CAP + 1)
        conn, client = Recording(KeysConn(every)), KeyedClient(every)
        v, c, _t = baseline(self, 5000, conn, client)
        d = c.detail
        self.assertEqual(d.get("mode"), "chunked", d)
        self.assertEqual((c.status, d["chunks"], d["compared_keys"], d["total_keys"], d["keys"]),
                         ("pass", 2, CAP + 1, CAP + 1, CAP + 1))
        self.assertFalse({"sampled_keys", "d95_keys", "d95_rate", "pair_key_budget"} & set(d))
        self.assertIsInstance(d["elapsed_ms"], int)
        chunks = [q["filters"][0]["values"] for q in client.filtered("equals")]
        self.assertEqual([len(x) for x in chunks], [CAP] * 3 + [1] * 3, "together and both alone queries per chunk")
        self.assertEqual((chunks[0], chunks[3]), (every[:CAP], every[CAP:]), "consecutive chunks in the md5 order")
        notset = client.filtered("notSet")
        self.assertEqual([(q["dimensions"], q["measures"]) for q in notset],
                         [(["part.part_id"], [A, B]), (["m_firm_open.key"], [A]), (["m_on_hand.key"], [B])],
                         "the NULL key group: together on the group member, alone on each alone dimension")
        self.assertEqual(len(client.queries), 9)
        self.assertEqual(conn.kinds(), ["rr_begin", "timeout", "count", "sample", "rollback"],
                         "the count and every key share one REPEATABLE READ snapshot")
        self.assertEqual(conn.inner.queries[1][1], {"seed": "7", "n": CAP + 1}, "every key, in the sample order")
        self.assertNotIn("P00001", repr(d) + repr(v.res.not_proven))

    def test_an_exact_multiple_of_the_cap_has_no_empty_chunk_and_the_budget_is_inclusive(self):
        every = keys(2 * CAP)
        client = KeyedClient(every)
        _v, c, _t = baseline(self, 2 * CAP, KeysConn(every), client)
        self.assertEqual((c.status, c.detail.get("mode"), c.detail.get("chunks")), ("pass", "chunked", 2))
        self.assertEqual([len(q["filters"][0]["values"]) for q in client.filtered("equals")], [CAP] * 6)

    def test_a_mismatch_only_in_the_null_key_group_fails(self):
        every = keys(CAP + 1)
        _v, c, _t = baseline(self, 5000, KeysConn(every), KeyedClient(every, null="bad"))
        self.assertEqual(c.detail.get("mode"), "chunked", c.detail)
        self.assertEqual((c.status, c.detail["mismatched"], c.detail["null_key_group"]["mismatched"]),
                         ("fail", {A: 1}, {A: 1}))
        _v, c, _t = baseline(self, 5000, KeysConn(every), KeyedClient(every, null="same"))
        self.assertEqual((c.status, c.detail["keys"], c.detail["null_key_group"]),
                         ("pass", CAP + 2, {"keys": 1, "mismatched": {}, "duplicate_keys": 0,
                                            "alone_only_keys": {A: 0, B: 0}}))

    def test_the_unfiltered_path_compares_the_null_key_group_and_the_sample_does_not(self):
        """Why the chunks need the NULL key group: at most PAIR_KEY_CAP keys the queries are unfiltered and include
        it, while the equals filter of a chunk or of the sample leaves it out."""
        cfg = demo_cfg()
        every = keys(3)
        st, d = shape_q.run_test(KeysConn(every), KeyedClient(every, null="bad"), cfg, {}, pair_test(cfg))
        self.assertEqual((st, d["mismatched"]), ("fail", {A: 1}))
        every = keys(CAP + 1)
        _v, c, _t = baseline(self, CAP, KeysConn(every), KeyedClient(every, null="bad"))
        self.assertEqual((c.status, c.detail["sampled_keys"]), ("pass", CAP), "a sample cannot see the NULL key group")

    def test_counts_add_up_over_the_chunks(self):
        every = keys(2 * CAP + 10)
        client = KeyedClient(every, bad=[every[5], every[CAP + 5], every[-1]], dup=[every[CAP + 1]],
                             missing=[every[3], every[-2]])
        _v, c, _t = baseline(self, 5000, KeysConn(every), client)
        d = c.detail
        self.assertEqual(d.get("mode"), "chunked", d)
        self.assertEqual((c.status, d["chunks"], d["mismatched"], d["duplicate_keys"], d["alone_only_keys"]),
                         ("fail", 3, {A: 3}, 1, {A: 2, B: 2}))
        self.assertEqual((d["keys"], d["compared_keys"]), (2 * CAP + 8, 2 * CAP + 10))

    def test_a_duplicate_key_alone_fails(self):
        every = keys(CAP + 7)
        _v, c, _t = baseline(self, 5000, KeysConn(every), KeyedClient(every, dup=[every[-1]]))
        self.assertEqual((c.status, c.detail.get("mode"), c.detail.get("duplicate_keys")), ("fail", "chunked", 1))

    def test_an_error_in_any_chunk_is_an_error(self):
        every = keys(2 * CAP + 1)                      # queries 1-3, 4-6 and 7-9 are the chunks, 10-12 the NULL group
        for fail_at, done in ((1, 0), (5, 1), (9, 2), (10, 3), (12, 3)):
            with self.subTest(fail_at=fail_at):
                v, c, _t = baseline(self, 5000, KeysConn(every), KeyedClient(every, fail_at=fail_at))
                d = c.detail
                self.assertEqual((c.status, d.get("error"), d.get("http_status"), d.get("mode"), d.get("chunks"),
                                  d.get("chunks_compared")), ("error", "CubeError", 500, "chunked", 3, done))
                self.assertEqual(v.res.not_proven, [], "a comparison that did not finish claims nothing")
                self.assertNotIn("P00001", repr(d))

    def test_a_key_read_that_does_not_match_the_count_is_an_error(self):
        every = keys(CAP + 5)
        v, c, _t = baseline(self, 5000, KeysConn(every[:-1], count=CAP + 5), KeyedClient(every))
        self.assertEqual((c.status, c.detail.get("error"), c.detail.get("mode")), ("error", "ValueError", "chunked"))
        self.assertEqual(v.res.not_proven, [])

    def test_verify_lists_the_chunked_comparison_in_not_proven(self):
        every = keys(CAP + 1)
        v, c, _t = baseline(self, 5000, KeysConn(every), KeyedClient(every))
        self.assertEqual(v.res.not_proven,
                         [f"v3:{PAIR}: compared every key ({CAP + 1}) in 2 chunks of at most {CAP} plus the NULL key "
                          "group; the key set comes from one REPEATABLE READ snapshot at the start, and Cube reads each "
                          "chunk in its own session (no snapshot shared across chunks or with SQL)"])
        self.assertEqual(shape_q.chunk_note(c.detail), v.res.not_proven[0].split(": ", 1)[1])


class BudgetFallbackTest(unittest.TestCase):
    def test_a_fan_out_outside_the_sample_passes_the_default_budget_and_fails_within_a_raised_one(self):
        """The false pass the budget closes: with the default budget the test compares the PAIR_KEY_CAP-key sample (the
        first keys in md5 order), so a fan-out on one key outside it passes; within a raised budget every key is
        compared and the test fails."""
        every = keys(CAP + 500)
        bad = [every[CAP + 250]]
        _v, c, _t = baseline(self, None, KeysConn(every), KeyedClient(every, bad=bad))
        self.assertEqual((c.status, c.detail["sampled_keys"], c.detail["mismatched"]), ("pass", CAP, {}),
                         "the old false pass, still there with the default budget")
        _v, c, _t = baseline(self, CAP + 500, KeysConn(every), KeyedClient(every, bad=bad))
        self.assertEqual((c.status, c.detail.get("mode"), c.detail["mismatched"]), ("fail", "chunked", {A: 1}))

    def test_above_the_budget_the_sample_and_its_bound_stay_and_the_budget_is_named(self):
        every = keys(CAP + 500)
        client = KeyedClient(every)
        v, c, _t = baseline(self, CAP + 499, KeysConn(every), client)
        d = c.detail
        self.assertEqual((c.status, d["sampled_keys"], d["total_keys"], d["d95_keys"], d.get("pair_key_budget")),
                         ("pass", CAP, CAP + 500, shape_q.d95_keys(CAP, CAP + 500), CAP + 499))
        self.assertNotIn("mode", d)
        self.assertEqual(len(client.queries), 3, "the sample once: no chunks, no NULL key group")
        (line,) = v.res.not_proven
        self.assertTrue(line.startswith(f"v3:{PAIR}: compared {CAP} of {CAP + 500} keys (a deterministic sample with "
                                        "verify.seed); the other keys were not compared; a mismatch affecting at "
                                        "least"), line)
        self.assertTrue(line.endswith(f"; the key count exceeds verify.pair_key_budget ({CAP + 499}), so the keys "
                                      f"were not compared in chunks (a budget of at least {CAP + 500} compares every "
                                      "key)"), line)

    def test_the_default_budget_names_itself_too(self):
        every = keys(CAP + 1)
        v, c, _t = baseline(self, None, KeysConn(every), KeyedClient(every))
        self.assertEqual(c.detail.get("pair_key_budget"), CAP)
        self.assertIn(f"verify.pair_key_budget ({CAP})", v.res.not_proven[0])


class ReplaySampleTest(unittest.TestCase):
    def test_v4_replays_the_first_chunk_as_its_sample(self):
        every = keys(2 * CAP + 1)
        conn = KeysConn(every)
        v, c, t = baseline(self, 5000, conn, KeyedClient(every))
        self.assertEqual((c.status, c.detail.get("mode")), ("pass", "chunked"))
        reads = len(conn.queries)
        replay = KeyedClient(every)
        self.assertEqual(v._test_fn(conn, t)(replay), "pass")
        self.assertEqual(len(conn.queries), reads, "the replay reads no keys: it reuses the baseline's key read")
        self.assertEqual([q["filters"][0]["values"] for q in replay.queries], [every[:CAP]] * 3,
                         "the sample is the first chunk, replayed alone")
        self.assertEqual(replay.filtered("notSet"), [])
        self.assertEqual(v._test_fn(conn, t)(KeyedClient(every, bad=[every[-1]])), "pass",
                         "a mutant that changes only keys outside the sample survives the replay")
        cid = f"v3:{PAIR}"
        v.items, v.suite_tests = [(cid, cid, v._test_fn(conn, t))], [t]
        with mock.patch.object(stage_verify.mutate, "run_mutants", return_value=([], [])) as run:
            v.v4(conn)
        self.assertEqual(run.call_args.kwargs["sampled_items"], [cid], "survivors are marked sample_blind_possible")
        self.assertIn(f"v4: the suite's pair tests compare a sample of their keys ({cid}), so a mutant that changes "
                      "only other keys can survive them", v.res.not_proven)


class UnchangedPathTest(unittest.TestCase):
    def test_at_most_the_cap_the_queries_stay_unfiltered_whatever_the_budget(self):
        every = keys(CAP)
        for budget in (None, CAP, 5000):
            with self.subTest(budget=budget):
                conn, client = KeysConn(every), KeyedClient(every, null="same")
                v, c, _t = baseline(self, budget, conn, client)
                self.assertEqual((c.status, c.detail), ("pass", {"keys": CAP + 1, "mismatched": {}, "duplicate_keys": 0,
                                                                 "alone_only_keys": {A: 0, B: 0}}))
                self.assertTrue(all("filters" not in q for q in client.queries))
                self.assertEqual(len(conn.queries), 1, "only the count")
                self.assertEqual(v.res.not_proven, [])

    def test_without_a_budget_run_test_keeps_the_sample(self):
        cfg = demo_cfg()
        cfg.verify.pair_key_budget = 5000
        every = keys(CAP + 500)
        client = KeyedClient(every)
        st, d = shape_q.run_test(KeysConn(every), client, cfg, {}, pair_test(cfg))
        self.assertEqual((st, set(d)), ("pass", {"keys", "mismatched", "duplicate_keys", "alone_only_keys",
                                                 "sampled_keys", "total_keys", "d95_keys", "d95_rate"}))
        self.assertEqual(len(client.queries), 3)


class BudgetConfigTest(unittest.TestCase):
    BASE = 'version = 1\n[source]\nschemas = ["s"]\n'

    def load(self, value: str):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "crosscheck.toml"
            path.write_text(self.BASE + f"[verify]\npair_key_budget = {value}\n", encoding="utf-8")
            return config.load(path)

    def test_the_default_is_the_cap(self):
        self.assertEqual(config.Verify().pair_key_budget, CAP)
        self.assertEqual(demo_cfg().verify.pair_key_budget, CAP)
        self.assertEqual(config.PAIR_KEY_CAP, CAP)

    def test_an_integer_of_at_least_the_cap(self):
        for good in (CAP, CAP + 1, 10_000_000):
            self.assertEqual(self.load(str(good)).verify.pair_key_budget, good)
        for bad in (str(CAP - 1), "0", "-5", "true", "2000.0", '"5000"'):
            with self.subTest(value=bad), self.assertRaises(StageError) as e:
                self.load(bad)
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
            self.assertIn("verify.pair_key_budget", str(e.exception))


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class ChunkKeysPgTest(unittest.TestCase):
    NAME = "sxc_pair_chunks_pg"

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

    def test_every_key_within_the_budget_comes_from_the_count_snapshot_in_the_sample_order(self):
        writer = self.connect()
        writer.run("DROP TABLE IF EXISTS public.chunk_parts")
        writer.run("CREATE TABLE public.chunk_parts (part_id text)")
        writer.run("INSERT INTO public.chunk_parts SELECT 'p' || g FROM generate_series(1, 25) g")
        writer.run("INSERT INTO public.chunk_parts VALUES (NULL), ('p3')")
        reader = self.connect()
        cfg = types.SimpleNamespace(source=types.SimpleNamespace(statement_timeout_ms=60000),
                                    verify=types.SimpleNamespace(seed=7))
        test = {"keys_from": ["public.chunk_parts", "part_id"]}
        original = reader.run

        def run(sql, **params):
            out = original(sql, **params)
            if "count(DISTINCT" in sql:                      # another session commits between the two statements
                writer.run("DELETE FROM public.chunk_parts WHERE part_id IS DISTINCT FROM 'p1'")
            return out
        with mock.patch.object(shape_q, "PAIR_KEY_CAP", 10):
            total, sample = shape_q.pair_keys(reader, cfg, test)
            with mock.patch.object(reader, "run", side_effect=run):
                total_again, every = shape_q.pair_keys(reader, cfg, test, budget=30)
        self.assertEqual((total, len(sample), total_again, len(every)), (25, 10, 25, 25),
                         "every key, from the snapshot the count saw")
        self.assertEqual(len(set(every)), 25)
        self.assertNotIn(None, every)
        self.assertEqual(every[:10], sample, "the first chunk is the sample")
        with db.read_only(reader):
            self.assertEqual(db.scalar(reader, "SELECT count(*) FROM public.chunk_parts"), 1, "the delete did commit")


if __name__ == "__main__":
    unittest.main()
