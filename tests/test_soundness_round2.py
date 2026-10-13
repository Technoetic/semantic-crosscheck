"""Regression tests for the second verification round.

- The inputs fingerprint is taken before a stage runs: an edit made while the stage runs leaves the result recorded
  with the old inputs, so report calls it stale instead of certifying files the stage never read.
- A failed or interrupted report run leaves no report.md / report.json from an earlier run behind.
- min and max measures keep the comparison kind of their column (text stays text: '007' is not '7').
- Time values are compared at Cube's millisecond resolution.
- measure_filter_drop counts rows worth 0 that add a key (or a key and day for a dated measure) to the result.
- measure_agg_to_max on a dated measure counts keys whose rows differ inside a date window although their totals agree.
Integration tests are skipped unless SXC_TEST_ADMIN_DSN is set.
"""

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture

import sxc  # noqa: E402
from semantic_crosscheck import canon, closure, compare, config, cube_gen, db, mutate, report  # noqa: E402
from semantic_crosscheck.result import Result, inputs_fingerprint  # noqa: E402


def quiet_main(argv) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return sxc.main(argv)


class FingerprintTiming(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_round2_fp_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(pgfixture.DEMO_CONFIG.parent, self.tmp / "demo")
        self.config = self.tmp / "demo" / "crosscheck.toml"
        self.out = self.tmp / "out"

    def fake_stage(self, edit):
        def run(cfg, args, out):
            edit()
            res = Result("verify")
            res.add("v1:q01_explosion", "pass", "")
            return res
        return mock.Mock(run=run)

    def test_an_edit_during_the_run_is_not_recorded_as_the_runs_input(self):
        gold = self.tmp / "demo" / "gold" / "q03_stock.sql"
        before = inputs_fingerprint(config.load(self.config))

        def edit():
            gold.write_text(gold.read_text(encoding="utf-8") + "\n-- edited while verify ran\n", encoding="utf-8")
        real_import = sxc.importlib.import_module
        with mock.patch.object(sxc.importlib, "import_module",
                               side_effect=lambda name: self.fake_stage(edit) if name.endswith("stage_verify") else real_import(name)):
            self.assertEqual(quiet_main(["verify", "--config", str(self.config), "--out", str(self.out)]), 0)
        saved = json.loads((self.out / "verify" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["inputs"], before)
        self.assertNotEqual(saved["inputs"]["gold_sha256"], inputs_fingerprint(config.load(self.config))["gold_sha256"])
        stale = report.staleness(report.read_results(self.out), inputs_fingerprint(config.load(self.config)))
        self.assertEqual(stale["verify"], {"after": "gold SQL", "why": "inputs_changed"})

    def test_a_failed_report_leaves_no_earlier_report_files(self):
        self.out.mkdir()
        for name in ("report.md", "report.json"):
            (self.out / name).write_text("Overall: PASS", encoding="utf-8")
        with mock.patch.object(report, "run", side_effect=KeyboardInterrupt):
            self.assertEqual(quiet_main(["report", "--config", str(self.config), "--out", str(self.out)]), 1)
        self.assertFalse((self.out / "report.md").exists())
        self.assertFalse((self.out / "report.json").exists())

    def test_a_report_that_cannot_find_its_config_removes_the_earlier_report_files(self):
        self.out.mkdir()
        for name in ("report.md", "report.json"):
            (self.out / name).write_text("Overall: PASS", encoding="utf-8")
        missing = self.tmp / "demo" / "crosscheck-typo.toml"
        self.assertEqual(quiet_main(["report", "--config", str(missing), "--out", str(self.out)]), 2)
        self.assertFalse((self.out / "report.md").exists())
        self.assertFalse((self.out / "report.json").exists())


class MeasureKinds(unittest.TestCase):
    def test_min_and_max_keep_their_column_kind(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        m = cfg.measures["on_hand"]
        cols = {"part_id": "text", "qty": "numeric", "code": "text", "seen": "date", "location_id": "text"}
        self.assertEqual(cube_gen.value_kind(m, cols), "number")
        self.assertEqual(cube_gen.value_kind(dataclasses.replace(m, agg="count", column="code"), cols), "number")
        self.assertEqual(cube_gen.value_kind(dataclasses.replace(m, agg="min", column="code"), cols), "string")
        self.assertEqual(cube_gen.value_kind(dataclasses.replace(m, agg="max", column="seen"), cols), "time")
        cube = cube_gen.measure_cube(cfg, dataclasses.replace(m, agg="min", column="code"), cols)
        kinds = compare.member_kinds({"cubes": [cube]})
        self.assertEqual(kinds["m_on_hand.value"], "string")
        self.assertFalse(canon.same("007", "7", kinds["m_on_hand.value"]))
        self.assertTrue(canon.same("7", "7.000", "number"))

    def test_times_compare_at_millisecond_resolution(self):
        self.assertTrue(canon.same("2026-01-05T10:30:00.123456", "2026-01-05T10:30:00.123", "time"))
        self.assertFalse(canon.same("2026-01-05T10:30:00.124", "2026-01-05T10:30:00.123", "time"))
        self.assertEqual(canon.canon("2026-01-05", "time"), "2026-01-05T00:00:00.000")


@unittest.skipUnless(pgfixture.available(), "SXC_TEST_ADMIN_DSN not set")
class WitnessRound2(unittest.TestCase):
    NAME = "sxc_t_round2_witness"

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture(cls.NAME, "clean").setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        owner = db.connect("SXC_PG_OWNER_DSN")
        try:
            with db.write_tx(owner):
                for oid, qty, day in (("T-1", 5, "2026-02-01"), ("T-2", 5, "2026-03-01"), ("T-3", -5, "2026-04-01")):
                    owner.run("INSERT INTO sxc_demo.sales_order VALUES (:o, 'P-000001', 'FIRM', 10, :q, :d)", o=oid, q=qty, d=day)
        finally:
            owner.close()
        cls.conn = db.connect("SXC_PG_DSN")
        with db.write_tx(cls.conn):
            closure.build(cls.conn, cls.cfg, cls.cfg.hierarchy())

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.fx.teardown()

    def model_for(self, cfg):
        with db.read_only(self.conn):
            model, _notes = cube_gen.generate(cfg, self.conn)
        return model

    def mutant(self, cfg, operator, target):
        found = [mt for mt in mutate.operators(cfg, self.model_for(cfg)) if mt.operator == operator and mt.target == target]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_filter_drop_counts_zero_rows_that_add_keys(self):
        wide = dataclasses.replace(self.cfg.measures["firm_open"], filter={"status": ["FIRM", "FORECAST"]})
        cfg = dataclasses.replace(self.cfg, measures={**self.cfg.measures, "firm_open": wide})
        moving = db.scalar(self.conn, "SELECT count(*) FROM sxc_demo.sales_order WHERE status = 'CLOSED' AND qty_open <> 0")
        self.assertEqual(moving, 0, "the demo's closed orders are all worth 0")
        only_closed = db.scalar(self.conn, """SELECT count(*) FROM (SELECT part_id FROM sxc_demo.sales_order
            EXCEPT SELECT part_id FROM sxc_demo.sales_order WHERE status IN ('FIRM', 'FORECAST')) x""")
        n = mutate.witness(self.conn, cfg, self.mutant(cfg, "measure_filter_drop", "firm_open"))
        self.assertEqual(n, only_closed)
        self.assertGreater(n, 0, "keys that only closed orders have appear once the filter is gone")

    def test_agg_to_max_on_a_dated_measure_sees_rows_that_cancel_in_total(self):
        own = dataclasses.replace(self.cfg.measures["firm_open_dated"], filter={"order_id": ["T-1", "T-2", "T-3"]})
        cfg = dataclasses.replace(self.cfg, measures={**self.cfg.measures, "firm_open_dated": own})
        n = mutate.witness(self.conn, cfg, self.mutant(cfg, "measure_agg_to_max", "firm_open_dated"))
        self.assertEqual(n, 1, "5 + 5 - 5 equals max 5 in total, but a window with the two 5s gives 10 against 5")


if __name__ == "__main__":
    unittest.main()
