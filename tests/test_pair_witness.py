"""Existence witness for keys missing from a pair test's alone answers (verify.existence_witness, design section 7).

  WitnessConfigTest  verify.existence_witness defaults to false and must be a boolean
  WitnessOffTest     with the option off nothing changes (one V3 check, no witness statement); with it on the V3 check
                     keeps its status, reason and counts and the witness is a separate check v3w:pair_witness:<pair>
  WitnessLabelTest   sampled tests: absent_no_facts with an empty together value passes; absent_with_facts and
                     witness_value_mismatch fail; a key missing from the together answer, a failed cast and a change
                     between the two witness reads are undecided (not_run witness_incomplete) unless a known fail wins;
                     the closure qty gets no first statement (its key read is its first witness) but is read again
                     after the Cube queries like every measure; details hold counts only, asked_keys,
                     incomplete_keys, undecided_keys and together_absent per key, the other labels per key and
                     measure an alone answer left out
  WitnessChunkTest   chunked tests read the witness of every key inside the key-read REPEATABLE READ transaction, in
                     slices of PAIR_KEY_CAP, and the second read after each chunk's Cube queries, for its absent keys only
  WitnessScopeTest   the unfiltered path at or below the cap gets no witness, not even when it errors, nor does a pair
                     test whose key read failed; V4 replays run none, and a sampled or chunked pair test that errors
                     after its key read leaves the witness not_run (pair_error)
  WitnessFailureTest whatever the witness's own work raises (a failed statement, savepoint or second-read transaction,
                     a lost connection, a key it cannot canonicalise) stays in the witness: the V3 check, its detail
                     (elapsed_ms, which leaves the witness reads out, included) and the V4 baseline are the option-off
                     run's
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pg8000.exceptions import DatabaseError, InterfaceError

import pgfixture
from semantic_crosscheck import config, shape_q, stage_verify
from semantic_crosscheck.cube_client import CubeError
from semantic_crosscheck.result import EXIT_CONFIG, StageError
from test_compare_unit import demo_cfg, demo_model

CAP = shape_q.PAIR_KEY_CAP
A, B = "m_firm_open.value", "m_on_hand.value"
QTY = "has_component_closure.qty"
PAIR = f"pair:{A}+{B}"
CLOSURE_PAIR = f"pair:{QTY}+{A}"
WID = f"v3w:pair_witness:{A}+{B}"
CLOSURE_WID = f"v3w:pair_witness:{QTY}+{A}"
RR_BEGIN = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
# the demo measures' where_sql, told apart by their filter values, and the closure qty's reread of its table
MEASURE_OF = (('_closure" t', QTY), ("'FIRM'", A), ("'CONSIGN'", "m_consigned.value"), ("'MAIN'", B))
COUNTS = ("asked_keys", "incomplete_keys", "undecided_keys", "together_absent", "absent_no_facts", "absent_with_facts",
          "witness_value_mismatch", "not_witnessed", "witness_type", "witness_error", "witness_changed")


def keys(n: int) -> list[str]:
    """n distinct synthetic part keys, standing for keys already in the md5 sample order."""
    return [f"P{i:05d}" for i in range(n)]


def counts(**nonzero) -> dict:
    return {name: nonzero.get(name, 0) for name in COUNTS}


class WitnessConn:
    """pair_keys from keys already in the md5 order, and the witness statements of the demo measures: a statement
    answers the asked keys that have qualifying rows (`facts`, per measure member; `later` from the first Cube query
    on, a change between the two reads), or raises the SQLSTATE `fail` names for its measure. Every statement goes to
    `log`, shared with the client, so their order can be checked. `trap(conn, sql)` may return an exception for any
    statement to raise instead of answering; `clock`, a FakeClock, advances one second per statement."""

    def __init__(self, ordered: list, facts=None, later=None, fail=None, log=None, trap=None, clock=None):
        self.keys = list(ordered)
        self.facts = {m: set(v) for m, v in (facts or {}).items()}
        self.later = {m: set(v) for m, v in (later or {}).items()}
        self.fail = dict(fail or {})
        self.log = log if log is not None else []
        self.witness: list[tuple[str, list]] = []
        self.cube_started = lambda: False
        self.trap, self.clock = trap, clock

    def run(self, sql, **params):
        self.log.append(("sql", sql))
        if self.clock is not None:
            self.clock.now += 1.0
        error = self.trap(self, sql) if self.trap is not None else None
        if error is not None:
            raise error
        if sql.startswith(("BEGIN", "SET LOCAL", "ROLLBACK", "SAVEPOINT", "RELEASE")):
            return None
        if "count(DISTINCT" in sql:
            return [[len(self.keys)]]
        if "ORDER BY md5(" in sql:
            return [[k] for k in self.keys[: params["n"]]]
        if sql.startswith("SELECT DISTINCT"):
            member = next(m for marker, m in MEASURE_OF if marker in sql)
            asked = [params[f"k{i}"] for i in range(len(params))]
            self.witness.append((member, asked))
            if member in self.fail:
                raise DatabaseError({"S": "ERROR", "C": self.fail[member], "M": "invalid input syntax"})
            facts = self.later[member] if self.cube_started() and member in self.later else self.facts.get(member, set())
            return [[k] for k in asked if k in facts]
        raise AssertionError(f"unexpected SQL {sql[:80]}")

    def witness_sql(self) -> list[str]:
        return [s for kind, s in self.log if kind == "sql" and s.startswith("SELECT DISTINCT")]


class WitnessClient:
    """Cube for the demo pair tests. Alone, a key has the value '1' unless it is in alone_missing[member] (no row).
    Together, a key in together_missing has no row, and a measure's value is '1' where its alone answer has the key and
    None (the empty value) where it has not, unless together_value[(key, member)] says otherwise. The answer honours an
    equals filter (a chunk or the sample) and a notSet filter (the NULL key group, empty here)."""

    def __init__(self, every: list, alone_missing=None, together_missing=(), together_value=None, log=None,
                 fail=False):
        self.every = list(every)
        self.alone_missing = {m: set(v) for m, v in (alone_missing or {}).items()}
        self.together_missing, self.together_value = set(together_missing), dict(together_value or {})
        self.log = log if log is not None else []
        self.fail, self.queries = fail, []

    def load(self, query):
        self.queries.append(query)
        self.log.append(("cube", query))
        if self.fail:
            raise CubeError("HTTP 500: P00001")
        (dim,), measures = query["dimensions"], query["measures"]
        filters = query.get("filters")
        if not filters:
            ks = list(self.every)
        else:
            (f,) = filters
            ks = list(f["values"]) if f["operator"] == "equals" else []
        rows = []
        for k in ks:
            if len(measures) > 1:
                if k in self.together_missing:
                    continue
                row = {dim: k}
                for m in measures:
                    row[m] = self.together_value.get((k, m), None if k in self.alone_missing.get(m, ()) else "1")
            else:
                (m,) = measures
                if k in self.alone_missing.get(m, ()):
                    continue
                row = {dim: k, m: "1"}
            rows.append(row)
        return rows


def paired(conn: WitnessConn, client: WitnessClient):
    """Share one log between the connection and the client; `later` facts apply from the first Cube query on."""
    client.log = conn.log
    conn.cube_started = lambda: bool(client.queries)
    return conn, client


class FakeClock:
    """shape_q's time module in a test: monotonic() is `now`, which WitnessConn advances per statement."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def run_v3(case: unittest.TestCase, conn, client, test_id: str = PAIR, budget: int | None = None,
           witness: bool = True, edit=None):
    """_Verify.v3 of a demo pair test as the verify stage runs it: (the _Verify, {check id: check}, the test).
    edit(test), when given, changes the test before it runs."""
    cfg = demo_cfg()
    cfg.verify.existence_witness = witness
    if budget is not None:
        cfg.verify.pair_key_budget = budget
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    v = stage_verify._Verify(cfg, Path(tmp.name), demo_model(cfg), ["v3", "v4"])
    test = next(t for t in shape_q.tests(cfg, v.model) if t["id"] == test_id)
    if edit is not None:
        edit(test)
    v.v3(conn, client, test, None)
    return v, {c.id: c for c in v.res.checks}, test


def without_elapsed(detail: dict) -> dict:
    return {k: v for k, v in detail.items() if k != "elapsed_ms"}


class WitnessConfigTest(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "crosscheck.toml"
            p.write_text('version = 1\n[source]\nschemas = ["s"]\n' + text, encoding="utf-8")
            return config.load(p)

    def test_off_by_default_and_a_boolean(self):
        self.assertIs(config.Verify().existence_witness, False)
        self.assertIs(demo_cfg().verify.existence_witness, False)
        self.assertIs(self.load("[verify]\nexistence_witness = true\n").verify.existence_witness, True)
        for bad in ("1", '"true"', "0"):
            with self.subTest(value=bad), self.assertRaises(StageError) as e:
                self.load(f"[verify]\nexistence_witness = {bad}\n")
            self.assertEqual(e.exception.exit_code, EXIT_CONFIG)


class WitnessOffTest(unittest.TestCase):
    def data(self):
        every = keys(CAP + 5)
        sample = every[:CAP]
        facts = {A: set(sample) - {"P00007"}, B: set(sample)}
        return every, facts

    def test_off_runs_no_witness_and_adds_no_check(self):
        every, facts = self.data()
        conn = WitnessConn(every, facts)
        v, checks, _t = run_v3(self, *paired(conn, WitnessClient(every, {A: {"P00007"}})), witness=False)
        self.assertEqual(list(checks), [f"v3:{PAIR}"])
        self.assertEqual(conn.witness, [])
        self.assertFalse([s for kind, s in conn.log if kind == "sql" and "SAVEPOINT" in s])
        self.assertFalse([line for line in v.res.not_proven if line.startswith("v3w:")])

    def test_on_keeps_the_v3_check_and_adds_a_separate_one(self):
        every, facts = self.data()
        off_v, off, _t = run_v3(self, *paired(WitnessConn(every, facts), WitnessClient(every, {A: {"P00007"}})),
                                witness=False)
        on_v, on, _t = run_v3(self, *paired(WitnessConn(every, facts), WitnessClient(every, {A: {"P00007"}})))
        self.assertEqual(list(on), [f"v3:{PAIR}", WID])
        self.assertEqual((on[f"v3:{PAIR}"].status, on[f"v3:{PAIR}"].detail),
                         (off[f"v3:{PAIR}"].status, off[f"v3:{PAIR}"].detail))
        self.assertEqual(on[f"v3:{PAIR}"].detail["reason"], "keys_not_answered")
        self.assertEqual(on_v.baseline, off_v.baseline, "V4 sees the same V3 status")
        self.assertEqual([line for line in on_v.res.not_proven if not line.startswith("v3w:")], off_v.res.not_proven)


class WitnessLabelTest(unittest.TestCase):
    """Sampled demo pair tests: CAP + 5 keys over a budget of CAP, so the sample is the first CAP keys."""

    every = keys(CAP + 5)
    sample = every[:CAP]

    def run_case(self, facts, alone_missing, test_id=PAIR, **kwargs):
        conn_kwargs = {k: kwargs.pop(k) for k in ("later", "fail") if k in kwargs}
        conn = WitnessConn(self.every, facts, **conn_kwargs)
        client = WitnessClient(self.every, alone_missing, **kwargs)
        v, checks, test = run_v3(self, *paired(conn, client), test_id=test_id)
        return v, checks, conn

    def full(self, but=()):
        return set(self.sample) - set(but)

    def test_absent_without_facts_and_an_empty_together_value_passes(self):
        v, checks, conn = self.run_case({A: self.full({"P00007"}), B: self.full({"P00011"})},
                                        {A: {"P00007"}, B: {"P00011"}})
        self.assertEqual((checks[f"v3:{PAIR}"].status, checks[f"v3:{PAIR}"].detail["answered_keys"]),
                         ("not_run", CAP - 2), "the V3 check is not upgraded")
        c = checks[WID]
        self.assertEqual((c.status, c.config_ref), ("pass", "measures.firm_open, measures.on_hand"))
        self.assertEqual(c.detail, {"mode": "sampled", **counts(asked_keys=CAP, incomplete_keys=2, absent_no_facts=2)})
        self.assertEqual(conn.witness, [(A, self.sample), (B, self.sample), (A, ["P00007"]), (B, ["P00011"])],
                         "one statement per alone measure over the sample, then the absent keys only")
        self.assertNotRegex(json.dumps(v.res.to_dict()), r"P\d{5}", "no key values in results")
        line = next(line for line in v.res.not_proven if line.startswith(f"{WID}: "))
        for phrase in ("where_sql", "crosscheck.toml", "not that the data is complete", "NULL key group",
                       "bracket", "not shared"):
            self.assertIn(phrase, line)

    def test_absent_with_facts_fails(self):
        v, checks, _conn = self.run_case({A: self.full(), B: self.full()}, {A: {"P00007"}})
        c = checks[WID]
        self.assertEqual((c.status, c.detail["absent_with_facts"], c.detail["absent_no_facts"]), ("fail", 1, 0))
        self.assertEqual(checks[f"v3:{PAIR}"].status, "not_run")
        action = stage_verify._next_action(v.res)
        self.assertIn(WID, action)
        self.assertIn("1 key-measure absence(s), absent_with_facts", action)

    def test_a_non_empty_together_value_without_facts_is_a_mismatch(self):
        for value in ("0", "5"):
            with self.subTest(value=value):
                _v, checks, _conn = self.run_case({A: self.full({"P00007"}), B: self.full()}, {A: {"P00007"}},
                                                  together_value={("P00007", A): value})
                c = checks[WID]
                self.assertEqual((c.status, c.detail["witness_value_mismatch"], c.detail["absent_no_facts"]),
                                 ("fail", 1, 0))

    def test_a_key_missing_from_the_together_answer_stays_undecided(self):
        _v, checks, conn = self.run_case({A: self.full(), B: self.full()}, {}, together_missing={"P00007"})
        c = checks[WID]
        self.assertEqual((c.status, c.detail.get("reason")), ("not_run", "witness_incomplete"))
        self.assertEqual(c.detail, {"mode": "sampled", "reason": "witness_incomplete",
                                    **counts(asked_keys=CAP, incomplete_keys=1, undecided_keys=1, together_absent=1)})
        self.assertEqual(len(conn.witness), 2, "no second read without an alone absence")

    def test_a_failed_cast_is_undecided_and_the_other_measure_is_still_witnessed(self):
        _v, checks, conn = self.run_case({B: self.full({"P00011"})}, {A: {"P00007"}, B: {"P00011"}},
                                         fail={A: "22P02"})
        c = checks[WID]
        self.assertEqual((c.status, c.detail.get("reason")), ("not_run", "witness_incomplete"))
        self.assertEqual((c.detail["witness_type"], c.detail["undecided_keys"], c.detail["absent_no_facts"]), (1, 1, 1))
        self.assertEqual(conn.witness, [(A, self.sample), (B, self.sample), (B, ["P00011"])])
        sql = [s for kind, s in conn.log if kind == "sql"]
        self.assertIn("ROLLBACK TO SAVEPOINT sxc_witness", sql, "the failed statement does not abort the key read")
        self.assertEqual(checks[f"v3:{PAIR}"].status, "not_run")

    def test_another_database_error_is_undecided_too(self):
        _v, checks, _conn = self.run_case({}, {A: {"P00007"}}, fail={A: "57014"})
        c = checks[WID]
        self.assertEqual((c.status, c.detail["witness_error"], c.detail["witness_type"]), ("not_run", 1, 0))

    def test_a_change_between_the_two_reads_is_undecided(self):
        for pre, post in ((self.full({"P00007"}), self.full()), (self.full(), self.full({"P00007"}))):
            with self.subTest(pre_has_facts="P00007" in pre):
                _v, checks, _conn = self.run_case({A: pre, B: self.full()}, {A: {"P00007"}}, later={A: post})
                c = checks[WID]
                self.assertEqual((c.status, c.detail["witness_changed"], c.detail["undecided_keys"]),
                                 ("not_run", 1, 1))

    def test_undecided_keys_count_keys_and_the_labels_count_key_measure_absences(self):
        v, checks, _conn = self.run_case({}, {A: {"P00007"}, B: {"P00007"}}, fail={A: "22P02", B: "22P02"},
                                         together_missing={"P00009"})
        c = checks[WID]
        self.assertEqual(c.detail, {"mode": "sampled", "reason": "witness_incomplete",
                                    **counts(asked_keys=CAP, incomplete_keys=2, undecided_keys=2, together_absent=1,
                                             witness_type=2)},
                         "P00007 is one undecided key with two undecided absences; P00009 is one key")
        action = stage_verify._not_run_action(c)
        for phrase in (f"{WID} left 2 key(s) undecided", "together_absent 1", "witness_type 2",
                       "per key and measure an alone answer left out"):
            self.assertIn(phrase, action)

    def test_a_known_fail_wins_over_undecided_keys(self):
        _v, checks, _conn = self.run_case({A: self.full(), B: self.full()}, {A: {"P00007"}},
                                          together_missing={"P00009"})
        c = checks[WID]
        self.assertEqual((c.status, c.detail["absent_with_facts"], c.detail["together_absent"]), ("fail", 1, 1))
        self.assertNotIn("reason", c.detail)

    def test_the_closure_qty_has_no_first_statement_and_an_absence_there_has_facts(self):
        _v, checks, conn = self.run_case({QTY: self.full(), A: self.full({"P00008"})},
                                         {QTY: {"P00007"}, A: {"P00008"}}, test_id=CLOSURE_PAIR)
        c = checks[CLOSURE_WID]
        self.assertEqual((c.status, c.config_ref), ("fail", "measures.firm_open"))
        self.assertEqual(c.detail, {"mode": "sampled",
                                    **counts(asked_keys=CAP, incomplete_keys=2, absent_with_facts=1, absent_no_facts=1)})
        self.assertEqual(conn.witness, [(A, self.sample), (QTY, ["P00007"]), (A, ["P00008"])],
                         "the key read is the closure qty's first read; its absent key is read again")
        reread = next(s for s in conn.witness_sql() if '_closure" t' in s)
        self.assertIn('FROM "sxc_out"."has_component_closure" t WHERE t."ancestor" IN (', reread)

    def test_a_closure_key_gone_at_the_second_read_is_undecided(self):
        _v, checks, _conn = self.run_case({QTY: self.full(), A: self.full()}, {QTY: {"P00007"}},
                                          test_id=CLOSURE_PAIR, later={QTY: self.full({"P00007"})})
        c = checks[CLOSURE_WID]
        self.assertEqual((c.status, c.detail.get("reason"), c.detail["witness_changed"], c.detail["absent_with_facts"]),
                         ("not_run", "witness_incomplete", 1, 0), "a closure rebuild during verify is not a fail")

    def test_every_key_answered_passes_with_nothing_absent(self):
        _v, checks, conn = self.run_case({A: self.full(), B: self.full()}, {})
        self.assertEqual(checks[f"v3:{PAIR}"].status, "pass")
        c = checks[WID]
        self.assertEqual((c.status, c.detail), ("pass", {"mode": "sampled", **counts(asked_keys=CAP)}))
        self.assertEqual(len(conn.witness), 2)


class WitnessChunkTest(unittest.TestCase):
    def test_the_first_read_shares_the_key_snapshot_and_the_second_follows_the_chunk(self):
        every = keys(CAP + 1)
        last = every[-1]
        facts = {A: set(every) - {last}, B: set(every)}
        conn, client = paired(WitnessConn(every, facts), WitnessClient(every, {A: {last}}))
        v, checks, _t = run_v3(self, conn, client, budget=5000)
        pair = checks[f"v3:{PAIR}"]
        self.assertEqual((pair.status, pair.detail["mode"], pair.detail["reason"]), ("not_run", "chunked",
                                                                                    "keys_not_answered"))
        c = checks[WID]
        self.assertEqual((c.status, c.detail), ("pass", {"mode": "chunked", **counts(asked_keys=CAP + 1,
                                                                                      incomplete_keys=1,
                                                                                      absent_no_facts=1)}))
        self.assertEqual(conn.witness, [(A, every[:CAP]), (A, every[CAP:]), (B, every[:CAP]), (B, every[CAP:]),
                                        (A, [last])], "slices of PAIR_KEY_CAP, then the absent key only")
        log = conn.log
        sql_at = [i for i, (kind, s) in enumerate(log) if kind == "sql"]
        key_read = next(i for i in sql_at if "ORDER BY md5(" in log[i][1])
        key_end = next(i for i in sql_at if i > key_read and log[i][1] == "ROLLBACK")
        first_cube = next(i for i, (kind, _q) in enumerate(log) if kind == "cube")
        witness_at = [i for i in sql_at if log[i][1].startswith("SELECT DISTINCT")]
        self.assertTrue(all(key_read < i < key_end for i in witness_at[:4]), "inside the key-read transaction")
        self.assertLess(key_end, first_cube, "the key-read snapshot never waits on Cube")
        self.assertEqual(log[sql_at[0]][1], RR_BEGIN)
        cube_at = [i for i, (kind, _q) in enumerate(log) if kind == "cube"]
        self.assertEqual(len(cube_at), 9, "two chunks and the NULL key group, three queries each")
        post = witness_at[4]
        self.assertTrue(cube_at[5] < post < cube_at[6], "after the second chunk's queries, before the NULL key group")
        begin = max(i for i in sql_at if i < post and log[i][1].startswith("BEGIN"))
        self.assertEqual(log[begin][1], RR_BEGIN)
        self.assertGreater(begin, cube_at[5], "a new snapshot")
        off = run_v3(self, *paired(WitnessConn(every, facts), WitnessClient(every, {A: {last}})), budget=5000,
                     witness=False)[1][f"v3:{PAIR}"]
        self.assertEqual((pair.status, without_elapsed(pair.detail)), (off.status, without_elapsed(off.detail)))
        self.assertTrue(any(line.startswith(f"{WID}: ") for line in v.res.not_proven))


class WitnessScopeTest(unittest.TestCase):
    def test_the_unfiltered_path_gets_no_witness(self):
        every = keys(10)
        conn, client = paired(WitnessConn(every, {A: set(every), B: set(every)}), WitnessClient(every, {A: {every[3]}}))
        v, checks, _t = run_v3(self, conn, client)
        self.assertEqual(list(checks), [f"v3:{PAIR}"])
        self.assertEqual(conn.witness, [])
        self.assertFalse([line for line in v.res.not_proven if line.startswith("v3w:")])

    def test_v4_replays_run_no_witness(self):
        every = keys(CAP + 5)
        conn, client = paired(WitnessConn(every, {A: set(every) - {"P00007"}, B: set(every)}),
                              WitnessClient(every, {A: {"P00007"}}))
        v, checks, test = run_v3(self, conn, client)
        self.assertIn(WID, checks)
        before, reads = list(conn.witness), len([s for kind, s in conn.log if kind == "sql"])
        self.assertEqual(v._test_fn(conn, test)(client), checks[f"v3:{PAIR}"].status)
        self.assertEqual(conn.witness, before)
        self.assertEqual(len([s for kind, s in conn.log if kind == "sql"]), reads, "no key read either")

    def test_an_unwitnessed_pair_test_that_errors_gets_no_witness_check(self):
        small = keys(10)
        conn, client = paired(WitnessConn(small, {A: set(small), B: set(small)}), WitnessClient(small, fail=True))
        v, checks, _t = run_v3(self, conn, client)
        self.assertEqual(checks[f"v3:{PAIR}"].status, "error")
        self.assertEqual(list(checks), [f"v3:{PAIR}"], "the unfiltered path gets no witness, also when it errors")
        self.assertFalse([line for line in v.res.not_proven if line.startswith("v3w:")])

        def trap(_conn, sql):
            if "count(DISTINCT" in sql:
                return DatabaseError({"S": "ERROR", "C": "57014", "M": "canceling statement"})
            return None
        every = keys(CAP + 5)
        conn, client = paired(WitnessConn(every, {A: set(every), B: set(every)}, trap=trap), WitnessClient(every))
        _v, checks, _t = run_v3(self, conn, client)
        self.assertEqual(checks[f"v3:{PAIR}"].status, "error")
        self.assertEqual(list(checks), [f"v3:{PAIR}"], "a key read that failed armed no witness")
        self.assertEqual(conn.witness, [])

    def test_a_pair_test_that_errors_leaves_the_witness_not_run(self):
        every = keys(CAP + 5)
        conn, client = paired(WitnessConn(every, {A: set(every), B: set(every)}), WitnessClient(every, fail=True))
        _v, checks, _t = run_v3(self, conn, client)
        self.assertEqual(checks[f"v3:{PAIR}"].status, "error")
        self.assertEqual((checks[WID].status, checks[WID].detail), ("not_run", {"reason": "pair_error"}))



def dead_from_first_witness(conn, sql):
    """The connection is lost at the first witness statement: it and every later statement raise InterfaceError."""
    if sql.startswith("SELECT DISTINCT"):
        conn.dead = True
    return InterfaceError("network error") if getattr(conn, "dead", False) else None


class WitnessFailureTest(unittest.TestCase):
    every = keys(CAP + 5)

    def same_as_off(self, trap, budget=None, edit=None):
        """The demo pair test with P00007 missing from A's alone answer, off and on under the same trap and a fake
        clock: asserts that the V3 check (elapsed_ms included), the V4 baseline and the other not_proven lines are
        the option-off run's, and returns (the witness check of the on run, its V3 check)."""
        facts = {A: set(self.every) - {"P00007"}, B: set(self.every)}
        runs = {}
        for witness in (False, True):
            clock = FakeClock()
            conn = WitnessConn(self.every, facts, trap=trap, clock=clock)
            with mock.patch.object(shape_q, "time", clock):
                runs[witness] = run_v3(self, *paired(conn, WitnessClient(self.every, {A: {"P00007"}})),
                                       budget=budget, witness=witness, edit=edit)
        (off_v, off, _t), (on_v, on, _t) = runs[False], runs[True]
        pair = f"v3:{PAIR}"
        self.assertEqual((on[pair].status, on[pair].detail), (off[pair].status, off[pair].detail))
        self.assertEqual(on_v.baseline, off_v.baseline, "V4 sees the same V3 status")
        self.assertEqual([line for line in on_v.res.not_proven if not line.startswith("v3w:")], off_v.res.not_proven)
        self.assertNotRegex(json.dumps(on_v.res.to_dict()), r"P\d{5}", "no key values in results")
        return on.get(WID), on[pair]

    def test_a_lost_connection_during_the_witness_leaves_the_pair_test_as_off(self):
        for mode, budget in (("sampled", None), ("chunked", 5000)):
            with self.subTest(mode=mode):
                w, pair = self.same_as_off(dead_from_first_witness, budget)
                self.assertNotEqual(pair.status, "error")
                self.assertEqual((w.status, w.detail["reason"], w.detail["witness_error"]),
                                 ("not_run", "witness_incomplete", 1))

    def test_a_failed_savepoint_is_a_witness_error(self):
        def trap(_conn, sql):
            if sql.startswith("SAVEPOINT"):
                return DatabaseError({"S": "ERROR", "C": "25P02", "M": "current transaction is aborted"})
            return None
        for mode, budget in (("sampled", None), ("chunked", 5000)):
            with self.subTest(mode=mode):
                w, _pair = self.same_as_off(trap, budget)
                self.assertEqual((w.status, w.detail["witness_error"], w.detail["absent_no_facts"]), ("not_run", 1, 0))

    def test_a_second_read_that_cannot_begin_is_a_witness_error(self):
        def trap(conn, sql):
            if sql.startswith("BEGIN") and conn.cube_started():
                return InterfaceError("network error")
            return None
        for mode, budget in (("sampled", None), ("chunked", 5000)):
            with self.subTest(mode=mode):
                w, _pair = self.same_as_off(trap, budget)
                self.assertEqual((w.status, w.detail["witness_error"], w.detail["witness_changed"]), ("not_run", 1, 0))

    def test_elapsed_ms_leaves_the_witness_reads_out(self):
        w, pair = self.same_as_off(None, 5000)
        self.assertEqual((pair.detail["mode"], w.status), ("chunked", "pass"))
        self.assertGreater(pair.detail["elapsed_ms"], 0, "the fake clock moves with the key read")

    def test_a_key_the_witness_cannot_canonicalise_errors_the_pair_test_as_off(self):
        def edit(test):
            test["kinds"][test["alone"][A]] = "no_such_kind"
        for mode, budget in (("sampled", None), ("chunked", 5000)):
            with self.subTest(mode=mode):
                w, pair = self.same_as_off(None, budget, edit)
                self.assertEqual((pair.status, pair.detail["error"]), ("error", "ValueError"))
                self.assertEqual((w.status, w.detail), ("not_run", {"reason": "pair_error"}))


if __name__ == "__main__":
    unittest.main()
