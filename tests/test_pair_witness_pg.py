"""Existence witness against real PostgreSQL 17 and, for most tests, Cube (verify.existence_witness, M5).

  WitnessPgTest    (database)         the first witness read runs inside the key-read REPEATABLE READ transaction,
                                      casts the key texts the way PostgreSQL casts Cube's equals filter values ('01'
                                      finds the integer 1), and a statement whose cast fails is undecided without
                                      aborting the key read or the other measure's statement
  WitnessCubeTest  (database + Cube)  the decisive fact: through real Cube, the together value of a key without
                                      qualifying rows is NULL (sum and count measures alike) while its alone answer
                                      has no row; the demo's sparse keys are absent_no_facts in sampled and chunked
                                      tests with the V3 checks unchanged; a measure cube with an extra filter (a
                                      deliberate defect) gives absent_with_facts; the dense fixture has no absent key

Database names start with sxc_f_wit. Skipped unless SXC_TEST_ADMIN_DSN names a superuser connection and, for the
Cube tests, the adapter is installed (SXC_CUBE_HOME or the per-user default) with node on PATH.
"""

import argparse
import copy
import dataclasses
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, cube_gen, cube_run, db, shape_q, stage_verify
from semantic_crosscheck.result import Result
from test_snapshot_sample import RR_BEGIN, Recording
from test_soundness_verify import CUBE_READY, activate, owner_run, prepare

ROW_ID = re.compile(r"P-\d{6}|SO-\d{6}")
NODE_PAIR = "pair:m_firm_open.value+m_on_hand.value"
WITNESS_CONFIG = """version = 1

[source]
schemas = ["sxc_demo"]
output_schema = "sxc_out"

[entities.wnode]
table = "sxc_demo.wit_node"
key = ["k"]

[entities.wfact]
table = "sxc_demo.wit_fact"
key = ["id"]

[entities.wdate]
table = "sxc_demo.wit_date"
key = ["id"]

[measures.w_ok]
entity = "wfact"
column = "v"
agg = "sum"
by = "k"
filter = { status = ["OK"] }
node = "wnode"

[measures.w_date]
entity = "wdate"
column = "v"
agg = "sum"
by = "k"
node = "wnode"

[verify]
mutation_operators = []
"""
WITNESS_TABLES = (
    "CREATE TABLE sxc_demo.wit_node (k text PRIMARY KEY)",
    "INSERT INTO sxc_demo.wit_node VALUES ('01'), ('2'), ('3'), ('4')",
    "CREATE TABLE sxc_demo.wit_fact (id int PRIMARY KEY, k int, v numeric, status text)",
    "INSERT INTO sxc_demo.wit_fact VALUES (1, 1, 5, 'OK'), (2, 2, 7, 'NO'), (3, NULL, 1, 'OK')",
    "CREATE TABLE sxc_demo.wit_date (id int PRIMARY KEY, k date, v numeric)",
    "INSERT INTO sxc_demo.wit_date VALUES (1, '2026-01-01', 1)",
)


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run integration tests")
class WitnessPgTest(unittest.TestCase):
    NAME = "sxc_f_wit_pg"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        owner_run(cls.fx, *WITNESS_TABLES)
        tmp = tempfile.TemporaryDirectory(prefix="sxc_f_wit_", ignore_cleanup_errors=True)
        cls.addClassCleanup(tmp.cleanup)
        path = Path(tmp.name) / "crosscheck.toml"
        path.write_text(WITNESS_CONFIG, encoding="utf-8")
        cls.cfg = config.load(path)

    def setUp(self):
        activate(self.fx)

    def test_the_first_read_shares_the_key_snapshot_and_casts_like_cube(self):
        conn = self.fx.check_conn()
        self.addCleanup(conn.close)
        with db.read_only(conn):
            model = cube_gen.generate(self.cfg, conn)[0]
        test = next(t for t in shape_q.tests(self.cfg, model) if t["kind"] == "pair")
        ok, dated = "m_w_ok.value", "m_w_date.value"
        self.assertEqual(test["measures"], [ok, dated])
        rec = Recording(conn)
        with mock.patch.object(shape_q, "PAIR_KEY_CAP", 2):
            w = shape_q.PairWitness(rec, self.cfg, test)
            total, keys = shape_q.pair_keys(rec, self.cfg, test, 10, witness=w)
        self.assertEqual((total, sorted(keys)), (4, ["01", "2", "3", "4"]))
        self.assertEqual(w.pre[ok], {"1": True, "2": False, "3": False, "4": False},
                         "'01' finds the integer 1 as Cube's equals filter does; status NO is filtered out")
        self.assertEqual(set(w.pre[dated].values()), {"witness_type"}, "a key text that is no date cannot be cast")
        kinds = rec.kinds()
        self.assertEqual(kinds[:4], ["rr_begin", "timeout", "count", "sample"])
        self.assertEqual(kinds[-1], "rollback")
        witness = [k for k in kinds if k.startswith("SELECT DISTINCT")]
        self.assertEqual(len(witness), 4, "two slices of PAIR_KEY_CAP per measure")
        self.assertIn("ROLLBACK TO SAVEPOINT sxc_witness", kinds)
        self.assertEqual(rec.sql[0], RR_BEGIN)
        self.assertEqual(db.scalar(conn, "SHOW transaction_isolation"), "read committed", "the snapshot is closed")


@unittest.skipUnless(pgfixture.available() and CUBE_READY,
                     "needs SXC_TEST_ADMIN_DSN and the Cube adapter (SXC_CUBE_HOME) with node on PATH")
class WitnessCubeTest(unittest.TestCase):
    NAME = "sxc_f_wit_cube"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        tmp = tempfile.TemporaryDirectory(prefix="sxc_f_wit_", ignore_cleanup_errors=True)
        cls.addClassCleanup(tmp.cleanup)
        cls.tmp = Path(tmp.name)
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.model = prepare(cls.fx, cls.cfg, cls.tmp / "base")

    def setUp(self):
        activate(self.fx)

    def out(self, name: str, model: dict | None = None) -> Path:
        out = self.tmp / name
        shutil.copytree(self.tmp / "base" / "build", out / "build")
        if model is None:
            shutil.copytree(self.tmp / "base" / "cube", out / "cube")
        else:
            cube_gen.write_model(model, out / "cube" / "model")
            cube = Result("cube")
            cube.add("cube:model", "pass", "", cubes=len(model["cubes"]))
            cube.write(out)
        return out

    def verify(self, cfg, out: Path) -> Result:
        res = stage_verify.run(cfg, argparse.Namespace(only=["v3"], gold=[]), out)
        self.assertIsNone(ROW_ID.search(json.dumps(res.to_dict())), "results must not contain row values")
        return res

    def sql(self, sql: str):
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn):
                return conn.run(sql)
        finally:
            conn.close()

    def sparse_key(self, measure: str, keys_sql: str) -> str:
        """The first key of keys_sql without a row that satisfies the measure's where_sql."""
        m = self.cfg.measures[measure]
        table = self.cfg.entity(m.entity, measure).table
        rows = self.sql(f"SELECT s.k FROM ({keys_sql}) s(k) WHERE NOT EXISTS (SELECT 1 FROM {table} t "
                        f"WHERE {cube_gen.where_sql(m)} AND t.{m.by} = s.k) ORDER BY 1 LIMIT 1")
        self.assertTrue(rows, f"the demo has a key without {measure} facts")
        return rows[0][0]

    def test_the_together_value_of_a_key_without_facts_is_null(self):
        """The decisive fact the witness's empty-value rule rests on, for a sum measure on a node pair and on the
        closure pair, and for a count measure (re-aggregated as a sum of per-key counts)."""
        cfg = copy.deepcopy(self.cfg)
        cfg.measures["firm_count"] = dataclasses.replace(cfg.measures["firm_open"], name="firm_count", agg="count",
                                                         closure_join="")
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn):
                model = cube_gen.generate(cfg, conn)[0]
        finally:
            conn.close()
        out = self.out("together_null", model)
        node_key = self.sparse_key("firm_open", "SELECT part_id FROM sxc_demo.part")
        end_key = self.sparse_key("firm_open", "SELECT DISTINCT ancestor FROM sxc_out.has_component_closure")
        cases = (("part.part_id", ["m_firm_open.value", "m_on_hand.value"], "m_firm_open.value", node_key),
                 ("part.part_id", ["m_firm_count.value", "m_on_hand.value"], "m_firm_count.value", node_key),
                 ("has_component_closure.ancestor", ["has_component_closure.qty", "m_firm_open.value"],
                  "m_firm_open.value", end_key))

        def equals(member: str, key: str) -> list:
            return [{"member": member, "operator": "equals", "values": [key]}]
        with cube_run.serve(cfg, out / "cube", out / "server.log") as client:
            for group, measures, member, key in cases:
                with self.subTest(measure=member, group=group):
                    together = client.load({"dimensions": [group], "measures": measures,
                                            "filters": equals(group, key)})
                    self.assertEqual(len(together), 1, "the together answer has the key")
                    self.assertIn(member, together[0])
                    self.assertIsNone(together[0][member],
                                      f"the empty together value is NULL, observed {together[0][member]!r}")
                    dim = member.split(".")[0] + ".key"
                    alone = client.load({"dimensions": [dim], "measures": [member], "filters": equals(dim, key)})
                    self.assertEqual(alone, [], "the alone answer has no row for the key")

    def test_sparse_keys_are_absent_without_facts_and_the_v3_checks_do_not_change(self):
        seen = 0
        for mode, cap, budget in (("chunked", 500, 10_000), ("sampled", 40, 40)):
            with self.subTest(mode=mode):
                runs = {}
                for witness in (False, True):
                    cfg = copy.deepcopy(self.cfg)
                    cfg.verify.pair_key_budget, cfg.verify.existence_witness = budget, witness
                    with mock.patch.object(shape_q, "PAIR_KEY_CAP", cap):
                        runs[witness] = self.verify(cfg, self.out(f"sparse_{mode}_{witness}"))
                off = {c.id: c for c in runs[False].checks}
                on = {c.id: c for c in runs[True].checks}
                pairs = [cid for cid in off if cid.startswith("v3:pair:")]
                self.assertEqual(len(pairs), 4)
                self.assertFalse([cid for cid in off if cid.startswith("v3w:")])
                for cid in pairs:
                    a, b = off[cid], on[cid]
                    self.assertEqual((a.status, without_elapsed(a.detail)), (b.status, without_elapsed(b.detail)), cid)
                    w = on["v3w:pair_witness:" + cid.removeprefix("v3:pair:")]
                    d = w.detail
                    asked = b.detail.get("compared_keys", b.detail.get("sampled_keys"))
                    self.assertEqual((w.status, d["mode"], d["asked_keys"]), ("pass", mode, asked), (w.id, d))
                    self.assertEqual(d["incomplete_keys"], asked - b.detail["answered_keys"], w.id)
                    self.assertEqual((d["absent_with_facts"], d["witness_value_mismatch"], d["undecided"]), (0, 0, 0))
                    self.assertGreaterEqual(d["absent_no_facts"], d["incomplete_keys"], w.id)
                    seen += d["absent_no_facts"]
                    self.assertTrue(any(line.startswith(f"{w.id}: ") for line in runs[True].not_proven), w.id)
                self.assertEqual([line for line in runs[True].not_proven if not line.startswith("v3w:")],
                                 runs[False].not_proven)
        self.assertGreater(seen, 0, "the demo really has sparse keys the witness classified")

    def test_a_measure_cube_with_an_extra_filter_gives_absent_with_facts(self):
        model = copy.deepcopy(self.model)
        cube = next(c for c in model["cubes"] if c["name"] == "m_on_hand")
        self.assertIn(" GROUP BY ", cube["sql"])
        cube["sql"] = cube["sql"].replace(" GROUP BY ", " AND md5(t.\"part_id\") < '8' GROUP BY ", 1)
        cfg = copy.deepcopy(self.cfg)
        cfg.verify.pair_key_budget, cfg.verify.existence_witness = 10_000, True
        with mock.patch.object(shape_q, "PAIR_KEY_CAP", 500):
            res = self.verify(cfg, self.out("extra_filter", model))
        by_id = {c.id: c for c in res.checks}
        hit = [cid for cid in by_id if cid.startswith("v3w:") and "m_on_hand.value" in cid]
        self.assertEqual(len(hit), 2)
        for cid in hit:
            self.assertEqual(by_id[cid].status, "fail", by_id[cid].detail)
            self.assertGreater(by_id[cid].detail["absent_with_facts"], 0, cid)
            self.assertNotEqual(by_id["v3:pair:" + cid.removeprefix("v3w:pair_witness:")].status, "pass", cid)
        for cid in [cid for cid in by_id if cid.startswith("v3w:") and cid not in hit]:
            self.assertEqual(by_id[cid].status, "pass", by_id[cid].detail)
        self.assertEqual(res.status, "fail")

    def test_the_dense_fixture_has_no_absent_key(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.measures = {name: dataclasses.replace(m, entity="part", column="part_id", agg="count", by="part_id",
                                                 filter={}, filter_normalized={}, date_column="", node="part")
                        for name, m in cfg.measures.items()}
        cfg.verify.existence_witness = True
        for mode, cap, budget in (("chunked", 500, 10_000), ("sampled", 40, 40)):
            with self.subTest(mode=mode):
                cfg.verify.pair_key_budget = budget
                out = self.tmp / ("dense_" + mode)
                prepare(self.fx, cfg, out)
                with mock.patch.object(shape_q, "PAIR_KEY_CAP", cap):
                    res = self.verify(cfg, out)
                witnesses = [c for c in res.checks if c.id.startswith("v3w:")]
                self.assertEqual(len(witnesses), len([c for c in res.checks if c.id.startswith("v3:pair:")]))
                self.assertTrue(witnesses)
                for c in witnesses:
                    self.assertEqual(c.status, "pass", c.id)
                    self.assertEqual((c.detail["mode"], c.detail["incomplete_keys"], c.detail["absent_no_facts"],
                                      c.detail["absent_with_facts"], c.detail["undecided"]), (mode, 0, 0, 0, 0), c.id)


def without_elapsed(detail: dict) -> dict:
    return {k: v for k, v in detail.items() if k != "elapsed_ms"}


if __name__ == "__main__":
    unittest.main()
