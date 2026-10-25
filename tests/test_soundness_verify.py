"""Integration regression tests of the verification-soundness fixes (PostgreSQL 17 and, for most, the Cube adapter).

  WitnessDataTest   (database)         V4 witnesses count what the mutation changes on the data: closure_end_swap
                                       with one class for every part, dated_shift_one_day without a gold window,
                                       measure_agg_to_max on one key and on a negative value next to a zero
  CubeVerifyTest    (database + Cube)  a database that refuses the mutant's connections makes the V4 check an error,
                                       never a kill; a closure that did not pass in build gates the closure mutants
                                       (no false survivor on a stale closure table); V3 pair tests above the key cap
                                       compare a deterministic sample that Cube filters, and within
                                       verify.pair_key_budget every key in chunks (each answered by Cube) plus the
                                       NULL key group (notSet), a real NULL key included
  TypedVerifyTest   (database + Cube)  text keys that look numeric stay distinct, timestamps with a time of day match
                                       Cube's rendering, and text that differs only in its spelling of a number fails

Database names start with sxc_f_sound. Skipped unless SXC_TEST_ADMIN_DSN names a superuser connection and, for the
Cube tests, the adapter is installed (SXC_CUBE_HOME or the per-user default) with node on PATH.
"""

import argparse
import copy
import dataclasses
import json
import os
import re
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import closure, compare, config, cube_gen, cube_run, db, mutate, shape_q, stage_verify
from semantic_crosscheck.config import Entity, Measure
from semantic_crosscheck.result import Result

CUBE_READY = cube_run.installed(cube_run.cube_home()) and bool(shutil.which("node"))
ROW_ID = re.compile(r"P-\d{6}|SO-\d{6}")


def activate(fx):
    os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(fx.check, fx.name)
    os.environ["SXC_PG_OWNER_DSN"] = pgfixture.dsn_for(fx.owner, fx.name)


def owner_run(fx, *statements):
    """Run statements as the loader role in one committed transaction."""
    os.environ["SXC_PG_OWNER_DSN"] = pgfixture.dsn_for(fx.owner, fx.name)
    conn = db.connect("SXC_PG_OWNER_DSN")
    try:
        with db.write_tx(conn):
            for sql in statements:
                conn.run(sql)
    finally:
        conn.close()


def admin_run(sql: str):
    conn = pgfixture._admin()
    try:
        conn.run(sql)
    finally:
        conn.close()


def mutants(cfg, model) -> dict:
    return {mu.check_id: mu for mu in mutate.operators(cfg, model)}


def prepare(fx, cfg, out: Path):
    """build (closure) and cube (generated model) results, as the real stages leave them."""
    conn = fx.check_conn()
    try:
        build = Result("build")
        with db.write_tx(conn):
            checks, _log = closure.build(conn, cfg, cfg.hierarchy())
        for cid, status, ref, detail in checks:
            build.add(cid, status, ref, **detail)
        build.write(out)
        with db.read_only(conn):
            model, _notes = cube_gen.generate(cfg, conn)
    finally:
        conn.close()
    cube_gen.write_model(model, out / "cube" / "model")
    cube = Result("cube")
    cube.add("cube:model", "pass", "", cubes=len(model["cubes"]))
    cube.write(out)
    return model


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run integration tests")
class WitnessDataTest(unittest.TestCase):
    NAME = "sxc_f_sound_witness"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.conn = cls.fx.check_conn()
        cls.addClassCleanup(cls.conn.close)
        with db.write_tx(cls.conn):
            checks, _log = closure.build(cls.conn, cls.cfg, cls.cfg.hierarchy())
        if [c[1] for c in checks] != ["pass", "pass"]:
            raise AssertionError(f"closure build failed: {[c[:2] for c in checks]}")
        cls.model = cls.generate(cls.cfg)

    @classmethod
    def generate(cls, cfg) -> dict:
        with db.read_only(cls.conn):
            return cube_gen.generate(cfg, cls.conn)[0]

    def setUp(self):
        activate(self.fx)

    def scalar(self, sql: str, **params):
        with db.read_only(self.conn):
            return db.scalar(self.conn, sql, **params)

    def test_end_swap_counts_pairs_of_different_rows_even_with_one_class(self):
        owner_run(self.fx, "CREATE TABLE sxc_demo.sound_saved_class AS SELECT part_id, part_class FROM sxc_demo.part",
                  "UPDATE sxc_demo.part SET part_class = 'assembly'")
        try:
            by_class = self.scalar(
                'SELECT count(*) FROM "sxc_out"."has_component_closure" p LEFT JOIN "sxc_demo"."part" a ON a.part_id = '
                'p.ancestor LEFT JOIN "sxc_demo"."part" d ON d.part_id = p.descendant WHERE a.part_class IS DISTINCT FROM d.part_class')
            self.assertEqual(by_class, 0, "the class-based witness would call the mutant equivalent")
            n = mutate.witness(self.conn, self.cfg, mutants(self.cfg, self.model)["v4:closure_end_swap:has_component"])
            self.assertEqual(n, self.fx.data["expected"]["closure_pairs"])
        finally:
            owner_run(self.fx, "UPDATE sxc_demo.part p SET part_class = s.part_class FROM sxc_demo.sound_saved_class s "
                               "WHERE s.part_id = p.part_id", "DROP TABLE sxc_demo.sound_saved_class")

    def test_dated_shift_counts_every_dated_row_without_a_gold_window(self):
        dated = sum(1 for o in self.fx.data["orders"] if o["status"] == "FIRM" and o["due_date"])
        cfg = copy.deepcopy(self.cfg)
        cfg.gold = [g for g in cfg.gold if g.id != "q05_firm_window"]
        for c in (cfg, self.cfg):
            n = mutate.witness(self.conn, c, mutants(c, self.model)["v4:dated_shift_one_day:firm_open_dated"])
            self.assertEqual(n, dated, [g.id for g in c.gold])

    def test_agg_to_max_sees_one_key_with_several_rows(self):
        firm = {}
        for o in self.fx.data["orders"]:
            if o["status"] == "FIRM":
                firm[o["part_id"]] = firm.get(o["part_id"], 0) + (1 if Decimal(o["qty_open"]) else 0)
        part = min(p for p, n in firm.items() if n >= 2)
        cfg = copy.deepcopy(self.cfg)
        m = cfg.measures["firm_open_dated"]
        m.filter = {"status": ["FIRM"], "part_id": [part]}
        old = self.scalar(f"SELECT CASE WHEN count(*) >= 2 THEN count(*) ELSE 0 END FROM ({mutate._per_key(cfg, m)}) k "
                          "WHERE k.v <> 0")
        self.assertEqual(old, 0, "the per-key witness would call the mutant equivalent")
        model = self.generate(cfg)
        cfg.verify.mutation_operators = ["measure_agg_to_max"]
        self.assertGreater(mutate.witness(self.conn, cfg, mutants(cfg, model)["v4:measure_agg_to_max:firm_open_dated"]), 0)

    def test_agg_to_max_sees_a_negative_value_next_to_a_zero(self):
        owner_run(self.fx, "CREATE TABLE sxc_demo.sound_values (k text PRIMARY KEY, v numeric)",
                  "INSERT INTO sxc_demo.sound_values VALUES ('a', -5), ('b', 0)")
        try:
            cfg = copy.deepcopy(self.cfg)
            cfg.entities["sound"] = Entity("sound", "sxc_demo.sound_values", ["k"])
            cfg.measures = {"sound_sum": Measure(name="sound_sum", entity="sound", column="v", agg="sum", by="k")}
            cfg.verify.mutation_operators = ["measure_agg_to_max"]
            model = self.generate(cfg)
            n = mutate.witness(self.conn, cfg, mutants(cfg, model)["v4:measure_agg_to_max:sound_sum"])
            self.assertEqual(n, 2, "the total (-5 against max 0) and the pair of keys")
        finally:
            owner_run(self.fx, "DROP TABLE sxc_demo.sound_values")


@unittest.skipUnless(pgfixture.available() and CUBE_READY,
                     "needs SXC_TEST_ADMIN_DSN and the Cube adapter (SXC_CUBE_HOME) with node on PATH")
class CubeVerifyTest(unittest.TestCase):
    NAME = "sxc_f_sound_cube"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        tmp = tempfile.TemporaryDirectory(prefix="sxc_f_sound_", ignore_cleanup_errors=True)
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.model = prepare(cls.fx, cls.cfg, cls.tmp / "base")

    def setUp(self):
        activate(self.fx)

    def out(self, name: str, closure_status: str = "") -> Path:
        out = self.tmp / name
        shutil.copytree(self.tmp / "base" / "cube", out / "cube")
        shutil.copytree(self.tmp / "base" / "build", out / "build")
        if closure_status:
            build = Result("build")
            build.add("closure:has_component", closure_status, "hierarchies.has_component", reason="cycle")
            build.write(out)
        return out

    def verify(self, cfg, out: Path, only: list[str]) -> Result:
        res = stage_verify.run(cfg, argparse.Namespace(only=only, gold=[]), out)
        self.assertIsNone(ROW_ID.search(json.dumps(res.to_dict())), "results must not contain row values")
        return res

    def test_refused_connections_during_v4_are_an_error_never_a_kill(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.mutation_operators = ["closure_depth_shift"]
        cfg.cube = dataclasses.replace(cfg.cube, query_timeout_s=30)
        real = mutate.run_mutants

        def refusing(*args, **kwargs):
            admin_run(f'ALTER DATABASE "{self.NAME}" CONNECTION LIMIT 1')
            try:
                return real(*args, **kwargs)
            finally:
                admin_run(f'ALTER DATABASE "{self.NAME}" CONNECTION LIMIT -1')

        with mock.patch.object(mutate, "run_mutants", refusing):
            res = self.verify(cfg, self.out("refused"), ["v4"])
        c = next(c for c in res.checks if c.id == "v4:closure_depth_shift:has_component")
        self.assertEqual((c.status, c.detail["reason"]), ("error", "suite_error"), c.detail)
        self.assertRegex(c.detail["item"], r"^v[123]:")
        self.assertNotIn("killed_by", c.detail)
        self.assertEqual((res.status, res.exit_code()), ("error", 1))
        self.assertNotIn("server.log", res.next_safe_action)

    def test_closure_that_did_not_pass_gates_the_closure_mutants(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.mutation_operators = ["closure_depth_shift", "closure_end_swap", "measure_filter_drop"]
        out = self.out("gated", closure_status="fail")      # the closure table of the clean build is still there
        res = self.verify(cfg, out, ["v4"])
        by_id = {c.id: c for c in res.checks}
        for cid in ("v4:closure_depth_shift:has_component", "v4:closure_end_swap:has_component"):
            self.assertEqual((by_id[cid].status, by_id[cid].detail["reason"], by_id[cid].detail["build_check"]),
                             ("not_run", "closure_not_passed", "fail"), cid)
        self.assertFalse(list((out / "verify" / "mutants").glob("closure_*")), "no closure mutant was served")
        self.assertEqual([c.id for c in res.checks if c.status == "fail"], [], "no false survivor")
        for target in ("firm_open", "firm_open_dated"):
            self.assertIn(by_id[f"v4:measure_filter_drop:{target}"].status, ("pass", "not_run"))
        self.assertEqual(res.status, "not_run")

    def test_pair_tests_above_the_cap_compare_a_sample(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.pair_key_budget = 40           # the budget at the cap, like the default, so nothing is chunked
        with mock.patch.object(shape_q, "PAIR_KEY_CAP", 40):
            res = self.verify(cfg, self.out("sampled"), ["v3"])
        pairs = [c for c in res.checks if c.id.startswith("v3:pair:")]
        self.assertEqual(len(pairs), 4)
        incomplete = []
        for c in pairs:
            self.assertEqual((c.detail["sampled_keys"], c.detail["duplicate_keys"]), (40, 0), c.id)
            self.assertGreater(c.detail["total_keys"], 40, c.id)
            self.assertLessEqual(c.detail["keys"], 40, c.id)
            if c.detail["answered_keys"] < 40:
                incomplete.append(c.id)
                self.assertEqual((c.status, c.detail.get("reason")), ("not_run", "keys_not_answered"), c.id)
                self.assertFalse({"d95_keys", "d95_rate"} & set(c.detail), c.id)
                prefix = f"{c.id}: requested 40 of {c.detail['total_keys']} keys, but only "
            else:
                self.assertEqual(c.status, "pass", c.id)
                note = (f"{c.id}: compared 40 of {c.detail['total_keys']} keys (a deterministic sample with verify.seed); "
                        "the other keys were not compared")
                self.assertEqual(c.detail["d95_keys"], shape_q.d95_keys(40, c.detail["total_keys"]), c.id)
                prefix = f"{note}; a mismatch affecting at least {c.detail['d95_keys']} keys ("
            line = next((line for line in res.not_proven if line.startswith(prefix)), "")
            self.assertTrue(line, c.id)
            if c.detail["answered_keys"] < 40:
                self.assertNotIn("95%", line)
            self.assertIn("exceeds verify.pair_key_budget (40)", line)
        self.assertTrue(incomplete)
        self.assertEqual([c.id for c in res.checks if c.status != "pass" and not c.id.startswith("scope:")], incomplete)

    def test_pair_tests_within_the_budget_compare_every_key_in_chunks(self):
        """Through real Cube: together queries answer every key, but sparse standalone facts stay unjudged. The
        fully answered intersection is distinct from together-row count. In addition,
        the NULL key group is queried (a notSet filter on the grouped member and on each alone dimension) and answered
        empty, which also shows Cube honours notSet: ignoring it would return every key there. The demo has no NULL
        key (primary keys, closure ends), and in a generated model a NULL key joins no measure row while measure cubes
        drop NULL keys (cube_gen.where_sql), so a disagreement only in the NULL key group cannot come from Cube here;
        test_pair_chunks covers that it fails."""
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.pair_key_budget = 10_000       # above every key count of the demo
        with mock.patch.object(shape_q, "PAIR_KEY_CAP", 500), \
                mock.patch.object(shape_q, "_null_keys", wraps=shape_q._null_keys) as null_keys:
            res = self.verify(cfg, self.out("chunked"), ["v3"])
        pairs = [c for c in res.checks if c.id.startswith("v3:pair:")]
        self.assertEqual(len(pairs), 4)
        self.assertEqual(null_keys.call_count, 3 * len(pairs), "the NULL key group: together and both alone queries")
        incomplete = []
        for c in pairs:
            d = c.detail
            self.assertEqual((d.get("mode"), d["duplicate_keys"], d["mismatched"]), ("chunked", 0, {}), c.id)
            self.assertGreater(d["total_keys"], 500, c.id)
            self.assertEqual((d["compared_keys"], d["chunks"]), (d["total_keys"], -(-d["total_keys"] // 500)), c.id)
            self.assertLessEqual(d["answered_keys"], d["total_keys"])
            self.assertEqual(d["keys"] - d["null_key_group"]["keys"], d["total_keys"], c.id)
            self.assertFalse({"sampled_keys", "d95_keys", "d95_rate"} & set(d), c.id)
            alone = {m: 0 for m in d["alone_only_keys"]}
            self.assertEqual(d["null_key_group"], {"keys": 0, "mismatched": {}, "duplicate_keys": 0,
                                                   "alone_only_keys": alone}, c.id)
            if d["answered_keys"] < d["total_keys"]:
                incomplete.append(c.id)
                self.assertEqual((c.status, d.get("reason")), ("not_run", "keys_not_answered"), c.id)
                prefix = f"{c.id}: Cube's together and alone answers held {d['answered_keys']} of the {d['total_keys']} keys"
            else:
                self.assertEqual(c.status, "pass", c.id)
                prefix = f"{c.id}: compared every key ({d['total_keys']}) in {d['chunks']} chunks of at most 500 plus the NULL"
            self.assertTrue(any(line.startswith(prefix) for line in res.not_proven), c.id)
        self.assertTrue(incomplete, "the synthetic fixture really has sparse standalone fact keys")
        self.assertEqual([c.id for c in res.checks if c.status != "pass" and not c.id.startswith("scope:")], incomplete)

    def test_dense_measure_keys_pass_all_actual_chunked_pair_queries(self):
        """Positive control: independent per-node COUNT sources answer every requested key in every query."""
        cfg = copy.deepcopy(self.cfg)
        cfg.measures = {name: dataclasses.replace(m, entity="part", column="part_id", agg="count", by="part_id",
                                                 filter={}, filter_normalized={}, date_column="", node="part")
                        for name, m in cfg.measures.items()}
        for mode, cap, budget in (("chunked", 500, 10_000), ("sampled", 40, 40)):
            cfg.verify.pair_key_budget = budget
            out = self.tmp / ("dense_pairs_" + mode)
            prepare(self.fx, cfg, out)
            with mock.patch.object(shape_q, "PAIR_KEY_CAP", cap):
                result = self.verify(cfg, out, ["v3"])
            pairs = [c for c in result.checks if c.id.startswith("v3:pair:")]
            self.assertTrue(any("closure.qty" in c.id for c in pairs), "closure pairs were exercised")
            self.assertTrue(any(c.id.startswith("v3:pair:m_") for c in pairs), "node pairs were exercised")
            for c in pairs:
                self.assertEqual(c.status, "pass", c.id)
                if mode == "chunked":
                    self.assertEqual(c.detail.get("mode"), mode, c.id)
                    self.assertEqual(c.detail["answered_keys"], c.detail["total_keys"], c.id)
                else:
                    self.assertEqual(c.detail["answered_keys"], cap, c.id)
                    self.assertIn("d95_keys", c.detail, c.id)

    def test_a_real_null_key_is_answered_in_the_null_key_group(self):
        """The NULL group is compared separately even when sparse non-NULL facts leave the parent check unjudged."""
        owner_run(self.fx, "CREATE TABLE sxc_demo.sound_null_part AS SELECT part_id FROM sxc_demo.part",
                  "INSERT INTO sxc_demo.sound_null_part VALUES (NULL)")
        try:
            cfg = copy.deepcopy(self.cfg)
            cfg.entities["sound_node"] = Entity("sound_node", "sxc_demo.sound_null_part", ["part_id"])
            cfg.measures["sound_firm"] = dataclasses.replace(cfg.measures["firm_open"], name="sound_firm",
                                                             closure_join="", node="sound_node")
            cfg.measures["sound_hand"] = dataclasses.replace(cfg.measures["on_hand"], name="sound_hand", node="sound_node")
            cfg.verify.pair_key_budget = 10_000
            out = self.tmp / "null_key"
            shutil.copytree(self.tmp / "base" / "build", out / "build")
            conn = self.fx.check_conn()
            try:
                with db.read_only(conn):
                    model, _notes = cube_gen.generate(cfg, conn)
            finally:
                conn.close()
            cube_gen.write_model(model, out / "cube" / "model")
            cube = Result("cube")
            cube.add("cube:model", "pass", "", cubes=len(model["cubes"]))
            cube.write(out)
            with mock.patch.object(shape_q, "PAIR_KEY_CAP", 500):
                res = self.verify(cfg, out, ["v3"])
        finally:
            owner_run(self.fx, "DROP TABLE sxc_demo.sound_null_part")
        a, b = "m_sound_firm.value", "m_sound_hand.value"
        c = next(c for c in res.checks if c.id == f"v3:pair:{a}+{b}")
        d = c.detail
        self.assertEqual((c.status, d.get("mode"), d.get("reason")), ("not_run", "chunked", "keys_not_answered"), d)
        self.assertGreater(d["total_keys"], 500)
        self.assertLess(d["answered_keys"], d["total_keys"], "sparse standalone fact rows are not invented")
        self.assertEqual((d["compared_keys"], d["keys"]),
                         (d["total_keys"], d["total_keys"] + 1), "the NULL key counts once, apart")
        self.assertEqual(d["null_key_group"], {"keys": 1, "mismatched": {}, "duplicate_keys": 0,
                                               "alone_only_keys": {a: 0, b: 0}})


TYPED_CONFIG = """version = 1

[source]
schemas = ["sxc_demo"]
output_schema = "sxc_out"

[entities.item]
table = "sxc_demo.sound_item"
key = ["code"]

[entities.event]
table = "sxc_demo.sound_event"
key = ["event_id"]

[measures.amount]
entity = "event"
column = "amount"
agg = "sum"
by = "code"
node = "item"

[measures.amount_dated]
entity = "event"
column = "amount"
agg = "sum"
by = "code"
date_column = "at"

[verify]
mutation_operators = []

[[gold]]
id = "g_codes"
sql = "gold/g_codes.sql"
key = "code"
zero_if_null = ["amount"]
cube_query = { dimensions = ["item.code", "item.label"], measures = ["m_amount.value"] }
map = { code = "item.code", label = "item.label", amount = "m_amount.value" }

[[gold]]
id = "g_times"
sql = "gold/g_times.sql"
key = ["code", "at"]
cube_query = { dimensions = ["m_amount_dated.key", "m_amount_dated.ts"], measures = ["m_amount_dated.value"] }
map = { code = "m_amount_dated.key", at = "m_amount_dated.ts", amount = "m_amount_dated.value" }

[[gold]]
id = "g_spelling"
sql = "gold/g_spelling.sql"
key = "code"
cube_query = { dimensions = ["item.code", "item.label"], filters = [{ member = "item.code", operator = "equals", values = ["a", "b"] }] }
map = { code = "item.code", label = "item.label" }
"""
TYPED_GOLD = {
    "g_codes": "SELECT i.code, i.label, coalesce(sum(e.amount), 0) AS amount\n"
               "FROM sxc_demo.sound_item i LEFT JOIN sxc_demo.sound_event e ON e.code = i.code GROUP BY i.code, i.label\n",
    "g_times": "SELECT code, at, sum(amount) AS amount FROM sxc_demo.sound_event WHERE code IS NOT NULL GROUP BY code, at\n",
    # the gold SQL swaps two labels that only differ in how they spell the same number
    "g_spelling": "SELECT code, CASE label WHEN '2.10' THEN '2.1' WHEN '2.1' THEN '2.10' ELSE label END AS label\n"
                  "FROM sxc_demo.sound_item WHERE code IN ('a', 'b')\n",
}
TYPED_TABLES = (
    "CREATE TABLE sxc_demo.sound_item (code text PRIMARY KEY, label text)",
    "CREATE TABLE sxc_demo.sound_event (event_id text PRIMARY KEY, code text, at timestamp, amount numeric)",
    "INSERT INTO sxc_demo.sound_item VALUES ('1.1', 'p'), ('1.10', 'q'), ('1e3', 'r'), ('1000', 's'), ('true', 't'), "
    "('a', '2.10'), ('b', '2.1')",
    "INSERT INTO sxc_demo.sound_event VALUES ('e1', '1.1', '2026-01-05 10:30:00', 1.5), ('e2', '1.10', '2026-01-05 00:00:00', 2), "
    "('e3', '1e3', '2026-01-06 23:59:59.5', 3), ('e4', '1000', '2026-01-07 08:00:00', 4), ('e5', 'true', '2026-01-07 08:00:00', 5), "
    "('e6', '1.1', '2026-01-05 10:30:00', 0.5), ('e7', 'a', '2026-02-01 12:00:00.25', 1)",
)


@unittest.skipUnless(pgfixture.available() and CUBE_READY,
                     "needs SXC_TEST_ADMIN_DSN and the Cube adapter (SXC_CUBE_HOME) with node on PATH")
class TypedVerifyTest(unittest.TestCase):
    NAME = "sxc_f_sound_types"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        owner_run(cls.fx, *TYPED_TABLES)
        tmp = tempfile.TemporaryDirectory(prefix="sxc_f_sound_", ignore_cleanup_errors=True)
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        (cls.tmp / "gold").mkdir()
        for gid, sql in TYPED_GOLD.items():
            (cls.tmp / "gold" / f"{gid}.sql").write_text(sql, encoding="utf-8", newline="\n")
        (cls.tmp / "crosscheck.toml").write_text(TYPED_CONFIG, encoding="utf-8", newline="\n")
        cls.cfg = config.load(cls.tmp / "crosscheck.toml")
        cls.out = cls.tmp / "out"
        activate(cls.fx)
        conn = cls.fx.check_conn()
        try:
            with db.read_only(conn):
                cls.model, _notes = cube_gen.generate(cls.cfg, conn)
        finally:
            conn.close()
        cube_gen.write_model(cls.model, cls.out / "cube" / "model")
        Result("cube").write(cls.out)

    def setUp(self):
        activate(self.fx)

    def test_verify_compares_in_the_member_types(self):
        res = stage_verify.run(self.cfg, argparse.Namespace(only=["v1"], gold=[]), self.out)
        v1 = {c.id: c for c in res.checks if c.id.startswith("v1:")}
        self.assertEqual((v1["v1:g_codes"].status, v1["v1:g_codes"].detail["matched_rows"],
                          v1["v1:g_codes"].detail["duplicate_keys_cube"]), ("pass", 7, 0), v1["v1:g_codes"].detail)
        self.assertEqual((v1["v1:g_times"].status, v1["v1:g_times"].detail["matched_rows"]), ("pass", 6), v1["v1:g_times"].detail)
        self.assertEqual((v1["v1:g_spelling"].status, v1["v1:g_spelling"].detail["mismatched_columns"]), ("fail", {"label": 2}))

    def test_untyped_comparison_was_wrong_on_the_same_cube_rows(self):
        gold = {g.id: g for g in self.cfg.gold}
        kinds = compare.member_kinds(self.model)
        conn = self.fx.check_conn()
        try:
            with cube_run.serve(self.cfg, self.out / "cube", self.tmp / "contrast.log") as client:
                got = {(gid, typed): compare.run_binding(conn, client, self.cfg, g, {},
                                                         types=compare.gold_kinds(g, kinds) if typed else None)
                       for gid, g in gold.items() for typed in (True, False)}
        finally:
            conn.close()
        self.assertEqual({k: r["pass"] for k, r in got.items()},
                         {("g_codes", True): True, ("g_codes", False): False, ("g_times", True): True,
                          ("g_times", False): False, ("g_spelling", True): False, ("g_spelling", False): True})
        self.assertEqual(got[("g_codes", False)]["duplicate_keys_cube"], 2, "'1.1'/'1.10' and '1e3'/'1000' collapsed")
        self.assertEqual(got[("g_times", False)]["matched_rows"], 0, "no timestamp key matched Cube's rendering")
        self.assertEqual(got[("g_times", True)]["matched_rows"], 6)


if __name__ == "__main__":
    unittest.main()
