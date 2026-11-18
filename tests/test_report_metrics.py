"""Measure evidence on the default report, including shared errors in generated SQL."""

import json
import sqlite3

import pgfixture
from semantic_crosscheck import config, evidence, shape_q
from semantic_crosscheck.result import inputs_fingerprint
from test_report_evidence import ORDER, STAGE_CHECKS, at, run_report, write_stage
from test_verify_evidence import SqlConn, TotalsClient, VerifyRun


class MetricReportTest(VerifyRun):
    def publish(self, result=None, *, stale=False, stages=ORDER[:-1]):
        inputs = inputs_fingerprint(self.cfg)
        for i, stage in enumerate(stages):
            write_stage(self.out, stage, STAGE_CHECKS[stage], i, inputs=inputs)
        if result is not None:
            data = result.to_dict()
            data.update(inputs=inputs, started_at=at(2 if stale else 12), finished_at=at(13))
            directory = self.out / "verify"
            directory.mkdir(exist_ok=True)
            (directory / "result.json").write_text(json.dumps(data), encoding="utf-8")
        code, _console = run_report(self.out, config_file=self.cfg.path)
        js = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        md = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertLess(md.index("## Metric evidence"), md.index("| Stage |"))
        self.assertIn("Standalone `measure-oracle` and `expect` runs have separate receipts", md.split("| Stage |")[0])
        rows = {r["facet"]: r for r in js["metric_evidence"]}
        self.assertEqual(set(rows), {f.id for f in evidence.facets(self.cfg) if f.element.startswith("measures.")})
        return code, rows, md

    def test_compared_zero_rows_no_values_and_missing_observation(self):
        self.rows["q03_stock"] = []
        result = self.verify()
        _, rows, _ = self.publish(result)
        self.assertEqual(rows["m_firm_open.value"]["basis"], "authored_comparison")
        self.assertIn("q02_where_used", rows["m_firm_open.value"]["sources"])
        self.assertEqual(rows["m_on_hand.value"]["basis"], "zero_rows")
        self.rows["q03_stock"] = [{c: 0 for c in self.cfg.gold[2].map}]
        result = self.verify()
        _, rows, _ = self.publish(result)
        self.assertEqual(rows["m_on_hand.value"]["basis"], "no_values")
        result.evidence["facets"] = [r for r in result.evidence["facets"] if r["facet"] != "m_on_hand.value"]
        _, rows, _ = self.publish(result)
        self.assertEqual(rows["m_on_hand.value"]["reason"], "facet_not_recorded")

    def test_stale_narrowed_missing_and_old_verify_never_credit_current_facets(self):
        _, rows, _ = self.publish()
        self.assertEqual({r["reason"] for r in rows.values()}, {"verify_missing"})
        result = self.verify()
        _, rows, _ = self.publish(result, stale=True)
        self.assertEqual({r["reason"] for r in rows.values()}, {"verify_stale"})
        _, rows, _ = self.publish(self.verify(only=["v1"]))
        self.assertEqual({r["reason"] for r in rows.values()}, {"verify_narrowed"})
        result.evidence = None
        _, rows, _ = self.publish(result)
        self.assertEqual({r["reason"] for r in rows.values()}, {"observation_missing"})
        self.assertTrue(all(r["basis"] == "not_run" and not r["sources"] for r in rows.values()))

    def test_shared_wrong_filter_can_pass_with_generated_evidence_only(self):
        # Both generated SQL paths omit SUB because the same declaration omitted it.
        path = self.cfg.path
        path.write_text(path.read_text(encoding="utf-8").replace('["MAIN", "SUB"]', '["MAIN"]'), encoding="utf-8")
        self.cfg = config.load(path)
        self.gold_file("q03_stock", "SELECT * FROM sxc_out.v_rule_stock")
        model = self.write_stages()
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("ATTACH DATABASE ':memory:' AS sxc_demo")
        conn.create_function("btrim", 1, str.strip)
        conn.execute("CREATE TABLE sxc_demo.stock (part_id text, location_id text, qty integer)")
        conn.executemany("INSERT INTO sxc_demo.stock VALUES ('P1', ?, ?)", [("MAIN", 7), ("SUB", 3)])
        total = next(t for t in shape_q.tests(self.cfg, model) if t["id"] == "total:m_on_hand.value")
        client = TotalsClient(conn, model)
        self.assertEqual(shape_q._run_total(SqlConn(conn), client, self.cfg, total), ("pass", {"equal": True}))
        self.assertEqual(client.load({"measures": total["members"]}), [{"m_on_hand.value": "7"}])
        self.assertEqual(conn.execute("SELECT sum(qty) FROM sxc_demo.stock WHERE location_id IN ('MAIN','SUB')"
                                      ).fetchone(), (10,))
        code, rows, md = self.publish(self.verify())
        self.assertEqual(code, 0)
        self.assertEqual(rows["m_on_hand.value"]["basis"], "generated_only")
        self.assertIn("v3:total:m_on_hand.value", rows["m_on_hand.value"]["sources"])
        self.assertIn("share a wrong definition", md)

    def test_mutation_only_requires_a_real_authored_kill_on_this_facet(self):
        result = self.verify()
        row = self.facet(result, "m_firm_open_dated.ts")
        mutant = "v4:dated_shift_one_day:firm_open_dated"
        row.update(status="detected", reason="", detected_by=[mutant])
        result.add(mutant, "pass", "", killed_by="v1:q05_firm_window", detection="authored")
        result.evidence["mutation"]["killed_authored"] += 1
        _, rows, _ = self.publish(result)
        self.assertEqual(rows[row["facet"]]["basis"], "mutation_only")
        self.assertIn(mutant, rows[row["facet"]]["sources"])
        # An unrelated facet row cannot claim the same mutant or an unconfigured source.
        self.facet(result, "part+m_firm_open").update(status="detected", reason="", detected_by=[mutant])
        _, rows, _ = self.publish(result)
        self.assertEqual(rows["part+m_firm_open"]["basis"], "not_run")
        self.assertEqual(rows["part+m_firm_open"]["sources"], [])

    def test_config_and_check_details_bound_positive_evidence(self):
        result = self.verify()
        # The high-level facet and per-member counts alone cannot earn authored credit.
        for check in result.checks:
            if check.id.startswith("v1:q03_stock"):
                check.detail.update(empty=True, matched_rows=0)
            elif check.id.startswith("v2:q03_stock:"):
                check.detail.update(empty_bindings=check.detail["bindings"], rows_compared=0)
        _, rows, _ = self.publish(result)
        self.assertEqual(rows["m_on_hand.value"]["basis"], "not_run")
        # Losing the Cube result makes chronology unverifiable for every metric.
        (self.out / "cube" / "result.json").unlink()
        _, rows, _ = self.publish(result, stages=("doctor", "demo", "build", "reason"))
        self.assertEqual({r["reason"] for r in rows.values()}, {"prerequisite_missing"})

    def test_generated_declarations_and_a_standalone_total_do_not_exercise_a_join(self):
        self.gold_file("q03_stock", "SELECT * FROM sxc_out.v_rule_stock")
        self.rows["q03_stock"] = "error"
        result = self.verify()
        for c in result.checks:
            if c.id.startswith(("v3:additive:", "v3:pair:")):
                c.status = "not_run"
        code, rows, _ = self.publish(result)
        self.assertNotEqual(code, 0)
        self.assertEqual(rows["m_on_hand.value"]["basis"], "generated_only")
        join = rows["part+m_on_hand"]
        self.assertEqual((join["basis"], join["reason"], join["sources"]), ("missing", "generated_text", []))

    def test_empty_and_unrecorded_generated_groups_do_not_establish_join_coverage(self):
        self.gold_file("q03_stock", "SELECT * FROM sxc_out.v_rule_stock")
        self.rows["q03_stock"] = "error"
        result = self.verify()
        for check in result.checks:
            if check.id.startswith(("v3:pair:", "v3:additive:")):
                check.status = "not_run"
        additive = next(c for c in result.checks if c.id == "v3:additive:m_on_hand.value:part.part_class")
        pair = next(c for c in result.checks if c.id.startswith("v3:pair:") and "m_on_hand.value" in c.id)
        for check, status, detail, basis in [
            (additive, "pass", {"groups": 0}, "zero_rows"),
            (additive, "pass", {}, "not_run"), (additive, "pass", {"groups": True}, "not_run"),
            (additive, "pass", {"groups": 1}, "generated_only"),
            (pair, "fail", {"keys": 0, "duplicate_keys": 1}, "zero_rows"),
            (pair, "pass", {"sampled_keys": 3, "keys": 2, "answered_keys": 0}, "zero_rows"),
            (pair, "pass", {"mode": "chunked", "answered_keys": 0, "null_key_group": {"keys": 1}}, "generated_only"),
            (pair, "pass", {"mode": "chunked", "answered_keys": 0, "null_key_group": {"keys": 0}}, "zero_rows"),
            (pair, "pass", {}, "not_run"),
        ]:
            with self.subTest(cid=check.id, detail=detail):
                check.status, check.detail = status, detail
                _, rows, _ = self.publish(result)
                self.assertEqual(rows["part+m_on_hand"]["basis"], basis)
                check.status = "not_run"

    def test_empty_generated_gold_is_an_empty_observation(self):
        self.gold_file("q03_stock", "SELECT * FROM sxc_out.v_rule_stock")
        self.rows["q03_stock"] = []
        result = self.verify()
        for c in result.checks:
            if c.id.startswith(("v3:additive:", "v3:pair:")):
                c.status = "not_run"
        _, rows, _ = self.publish(result)
        self.assertEqual(rows["part+m_on_hand"]["basis"], "zero_rows")
        self.assertEqual(rows["part+m_on_hand"]["sources"], ["q03_stock"])

    def test_mutation_requires_a_selected_operator_and_a_recorded_baseline(self):
        result = self.verify()
        row = self.facet(result, "m_firm_open_dated.ts")
        cid = "v4:dated_shift_one_day:firm_open_dated"
        row.update(status="detected", reason="", detected_by=[cid])
        result.add(cid, "pass", "", killed_by="v2:q05_firm_window:fiction[1234]", detection="authored")
        result.evidence["mutation"]["killed_authored"] = 1
        _, rows, _ = self.publish(result)
        self.assertEqual(rows[row["facet"]]["basis"], "not_run")
        binding = next(c for c in result.checks if c.id.startswith("v2:q05_firm_window:") and
                       c.status == "pass" and c.detail.get("bindings", 0) > 0)
        result.checks[-1].detail["killed_by"] = binding.id + "[0]"
        _, rows, _ = self.publish(result)
        self.assertEqual(rows[row["facet"]]["basis"], "mutation_only")
        result.checks[-1].detail["killed_by"] = "v1:q05_firm_window"
        split = result.evidence["mutation"]
        split["operators_selected"] -= 1
        split["operators_not_selected"].append("dated_shift_one_day")
        _, rows, _ = self.publish(result)
        self.assertEqual(rows[row["facet"]]["basis"], "not_run")
        split["operators_not_selected"].remove("dated_shift_one_day")
        split["operators_selected"] += 1
        split["killed_authored"] += 1
        _, rows, _ = self.publish(result)
        self.assertTrue(all(r["reason"] == "observation_missing" for r in rows.values()))
