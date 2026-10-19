"""Real PostgreSQL coverage of the independent oracle's read snapshot and verify integration."""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pgfixture
from semantic_crosscheck import closure, closure_oracle, config, db, stage_verify
from semantic_crosscheck.config import Entity, Hierarchy
from semantic_crosscheck.result import Result


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run integration tests")
class ClosureOraclePgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture("sxc_next_oracle", "clean", owner="xc_oracle_owner", check="xc_oracle_check")
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()
        cls.cfg = copy.deepcopy(config.load(pgfixture.DEMO_CONFIG))
        cls.h = Hierarchy("oracle", "oracle_edge", "part", "parent", "child", "qty")
        cls.cfg.entities["oracle_edge"] = Entity("oracle_edge", "sxc_demo.oracle_edge", ["parent", "child"])
        cls.cfg.hierarchies = {"oracle": cls.h}
        cls.cfg.gold, cls.cfg.measures = [], {}
        cls.conn = cls.fx.check_conn()
        cls.addClassCleanup(cls.conn.close)
        cls.admin = pgfixture._admin(cls.fx.name)
        cls.addClassCleanup(cls.admin.close)
        cls.admin.run("CREATE TABLE sxc_demo.oracle_edge (parent text, child text, qty numeric)")
        cls.admin.run('GRANT SELECT ON sxc_demo.oracle_edge TO "xc_oracle_check"')

    def setUp(self):
        with db.write_tx(self.admin):
            self.admin.run("TRUNCATE sxc_demo.oracle_edge")
            self.admin.run("INSERT INTO sxc_demo.oracle_edge VALUES ('SECRET_PARENT', 'SECRET_CHILD', 2)")
        with db.write_tx(self.conn):
            checks, _ = closure.build(self.conn, self.cfg, self.h)
        self.assertEqual([c[1] for c in checks], ["pass", "pass"])

    def verify(self, out, build_status="pass"):
        build = Result("build")
        build.add("closure:oracle", build_status, "hierarchies.oracle")
        build.write(out)
        verifier = stage_verify._Verify(self.cfg, Path(out), {"cubes": []}, ["v3"])
        verifier.run(self.conn)
        return verifier.res

    def test_v3_runs_the_raw_oracle_without_cube_and_detects_each_corrupted_metric(self):
        # Break caught: failing to register the oracle, or checking only aggregate reachability metrics.
        with TemporaryDirectory() as tmp:
            res = self.verify(tmp)
            by_id = {c.id: c for c in res.checks}
            self.assertIn("v3:closure_oracle:oracle", by_id)
            self.assertEqual(by_id["v3:closure_oracle:oracle"].status, "pass")
            for column in ("qty", "path_count", "min_depth", "max_depth"):
                with self.subTest(column=column):
                    with db.write_tx(self.conn):
                        self.conn.run(f"UPDATE sxc_out.oracle_closure SET {column} = {column} + 1")
                    res = self.verify(tmp)
                    check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
                    self.assertEqual((check.status, check.detail["mismatched_columns"]), ("fail", {column: 1}))
                    self.assertNotIn("SECRET", json.dumps(res.to_dict()))
                    with db.write_tx(self.conn):
                        self.conn.run(f"UPDATE sxc_out.oracle_closure SET {column} = {column} - 1")

    def test_v3_does_not_certify_a_stale_closure_after_a_failed_build(self):
        # Break caught: treating a table left by an earlier build as an eligible oracle target.
        with TemporaryDirectory() as tmp:
            res = self.verify(tmp, build_status="fail")
        check = next((c for c in res.checks if c.id == "v3:closure_oracle:oracle"), None)
        self.assertIsNotNone(check, "the raw oracle must leave an explicit gated result")
        self.assertEqual((check.status, check.detail["reason"], check.detail["build_check"]),
                         ("not_run", "closure_not_passed", "fail"))
        self.assertTrue(any("v3:closure_oracle:oracle" in line for line in res.not_proven))

    def test_raw_edges_and_closure_share_one_readonly_repeatable_read_snapshot(self):
        # Break caught: READ COMMITTED would read the old raw quantity (2) and the new closure quantity (3).
        admin, connection = self.admin, self.conn
        observed = []

        class InterleavingConnection:
            def run(self, sql, **params):
                result = connection.run(sql, **params)
                if sql.startswith('SELECT "parent", "child", "qty" FROM'):
                    observed.append((connection.run("SHOW transaction_isolation")[0][0],
                                     connection.run("SHOW transaction_read_only")[0][0]))
                    with db.write_tx(admin):
                        admin.run("UPDATE sxc_demo.oracle_edge SET qty = 3")
                        admin.run("UPDATE sxc_out.oracle_closure SET qty = 3")
                return result

        status, detail = closure_oracle.run(InterleavingConnection(), self.cfg, self.h)
        self.assertEqual((status, detail["matched_pairs"]), ("pass", 1))
        self.assertEqual(observed, [("repeatable read", "on")])
        # The wrapper changed the real tables; the observer passed using the prior consistent snapshot.
        with db.read_only(self.conn):
            self.assertEqual(self.conn.run("SELECT qty FROM sxc_out.oracle_closure")[0][0], 3)

    def test_v3_empty_graph_pass_discloses_vacuous_coverage(self):
        # Break caught: silently skipping bounded or vacuous verification in not_proven.
        with db.write_tx(self.admin):
            self.admin.run("TRUNCATE sxc_demo.oracle_edge")
            self.admin.run("TRUNCATE sxc_out.oracle_closure")
        with TemporaryDirectory() as tmp:
            res = self.verify(tmp)
            check = next((c for c in res.checks if c.id == "v3:closure_oracle:oracle"), None)
            self.assertIsNotNone(check)
            self.assertEqual((check.status, check.detail["empty_graph"]), ("pass", True))
            self.assertTrue(any("v3:closure_oracle:oracle" in line and "empty" in line for line in res.not_proven))

    def test_v3_row_budget_does_not_silently_certify_a_matching_prefix(self):
        # Break caught: LIMIT without the extra row would hide an incorrect complete quantity.
        limit = closure_oracle.Limits().max_raw_rows
        with db.write_tx(self.admin):
            self.admin.run("INSERT INTO sxc_demo.oracle_edge SELECT 'SECRET_PARENT', 'SECRET_CHILD', 2 "
                           "FROM generate_series(1, :n)", n=limit)
            self.admin.run("UPDATE sxc_out.oracle_closure SET qty = :prefix_qty", prefix_qty=limit * 2)
        with TemporaryDirectory() as tmp:
            res = self.verify(tmp)
        check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
        self.assertEqual((check.status, check.detail["reason"], check.detail["limit"]),
                         ("not_run", "budget_exceeded", "max_raw_rows"))
        self.assertTrue(any("budget_exceeded" in line for line in res.not_proven))

    def build_cyclic(self, on_cycle):
        """SECRET_PARENT <-> SECRET_CHILD, a self-loop on SECRET_C2 and a tail with a NULL quantity below them."""
        with db.write_tx(self.admin):
            self.admin.run("INSERT INTO sxc_demo.oracle_edge VALUES ('SECRET_CHILD', 'SECRET_PARENT', 3), "
                           "('SECRET_CHILD', 'SECRET_C2', 5), ('SECRET_CHILD', 'SECRET_C2', 7), "
                           "('SECRET_C2', 'SECRET_C2', 11), ('SECRET_C2', 'SECRET_C3', NULL), ('SECRET_C3', 'SECRET_C4', 13)")
        cfg = copy.deepcopy(self.cfg)
        cfg.hierarchies["oracle"].on_cycle = on_cycle
        with db.write_tx(self.conn):
            checks, _ = closure.build(self.conn, cfg, cfg.hierarchies["oracle"])
        return cfg, checks

    def verify_cfg(self, cfg, out, build_status="pass"):
        build = Result("build")
        build.add("closure:oracle", build_status, "hierarchies.oracle")
        build.write(out)
        verifier = stage_verify._Verify(cfg, Path(out), {"cubes": []}, ["v3"])
        verifier.run(self.conn)
        return verifier.res

    def test_v3_independently_verifies_report_mode_cycle_edge_removal(self):
        # Break caught: borrowing build's SCC-edge deletion instead of recomputing it, or certifying a closure that
        # differs from the raw edges once the edges inside cycles are removed.
        cfg, checks = self.build_cyclic("report")
        self.assertEqual([c[1] for c in checks], ["pass", "pass"])
        build_stats = checks[0][3]
        self.assertEqual((build_stats["dropped_cycle_edges"], build_stats["cycle_nodes"]), (3, 3))
        with TemporaryDirectory() as tmp:
            res = self.verify_cfg(cfg, tmp)
            check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
            self.assertEqual((check.status, check.detail["cycle_handling"], check.detail["dropped_cycle_edges"],
                              check.detail["cycle_nodes"], check.detail["matched_pairs"]),
                             ("pass", "scc_edge_removal", 3, 3, 6))
            self.assertTrue(any("v3:closure_oracle:oracle" in line and "inside cycles" in line
                                for line in res.not_proven))
            self.assertNotIn("SECRET", json.dumps(res.to_dict()))
            for column in ("qty", "path_count"):
                with self.subTest(column=column):
                    with db.write_tx(self.conn):
                        self.conn.run(f"UPDATE sxc_out.oracle_closure SET {column} = {column} + 1 "
                                      "WHERE ancestor = 'SECRET_CHILD' AND descendant = 'SECRET_C2'")
                    res = self.verify_cfg(cfg, tmp)
                    check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
                    self.assertEqual((check.status, check.detail["mismatched_columns"]), ("fail", {column: 1}))
                    self.assertNotIn("SECRET", json.dumps(res.to_dict()))
                    with db.write_tx(self.conn):
                        self.conn.run(f"UPDATE sxc_out.oracle_closure SET {column} = {column} - 1 "
                                      "WHERE ancestor = 'SECRET_CHILD' AND descendant = 'SECRET_C2'")
            # A closure row reached only through an edge inside the cycle must not be accepted.
            with db.write_tx(self.conn):
                self.conn.run("INSERT INTO sxc_out.oracle_closure (ancestor, descendant, min_depth, max_depth, path_count, qty) "
                              "VALUES ('SECRET_PARENT', 'SECRET_C2', 2, 2, 1, 24)")
            res = self.verify_cfg(cfg, tmp)
        check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
        self.assertEqual((check.status, check.detail["extra_pairs"]), ("fail", 1))

    def test_error_policy_cycle_still_fails_at_the_build_gate(self):
        # Break caught: letting the new report-mode handling relax error mode, at build or in verify.
        cfg, checks = self.build_cyclic("error")
        self.assertEqual([(c[1], c[3]["reason"]) for c in checks], [("fail", "cycle")])
        with TemporaryDirectory() as tmp:
            res = self.verify_cfg(cfg, tmp, build_status="fail")
        check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
        self.assertEqual((check.status, check.detail["reason"]), ("not_run", "closure_not_passed"))
        status, detail = closure_oracle.run(self.conn, cfg, cfg.hierarchies["oracle"])
        self.assertEqual((status, detail["reason"]), ("fail", "cycle"))

    def test_new_raw_cycle_fails_error_policy_even_if_the_build_record_previously_passed(self):
        # Break caught: allowing a stale build record to conceal a newly cyclic error-policy graph.
        with db.write_tx(self.admin):
            self.admin.run("INSERT INTO sxc_demo.oracle_edge VALUES ('SECRET_CHILD', 'SECRET_PARENT', 3)")
        with TemporaryDirectory() as tmp:
            res = self.verify(tmp)
        check = next(c for c in res.checks if c.id == "v3:closure_oracle:oracle")
        self.assertEqual((check.status, check.detail["reason"]), ("fail", "cycle"))

    def test_key_types_with_nonpython_equality_are_explicitly_not_proven(self):
        # Break caught: bpchar ignores trailing spaces while Python str equality preserves them.
        self.admin.run("CREATE TABLE sxc_demo.oracle_char (parent char(4), child char(4), qty numeric)")
        try:
            self.admin.run('GRANT SELECT ON sxc_demo.oracle_char TO "xc_oracle_check"')
            cfg = copy.deepcopy(self.cfg)
            cfg.entities["oracle_edge"].table = "sxc_demo.oracle_char"
            status, detail = closure_oracle.run(self.conn, cfg, self.h)
            self.assertEqual((status, detail["reason"]), ("not_run", "unsupported_key_type_or_collation"))
        finally:
            self.admin.run("DROP TABLE sxc_demo.oracle_char")


if __name__ == "__main__":
    unittest.main()
