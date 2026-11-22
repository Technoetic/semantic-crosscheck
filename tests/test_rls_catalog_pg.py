"""PostgreSQL 17 RLS visibility and UNIQUE NULLS NOT DISTINCT catalog guards."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pgfixture
import sxc
from semantic_crosscheck import config, db, doctor, grain


class CatalogGuardUnitTests(unittest.TestCase):
    def test_privileges_sql_gates_nulls_not_distinct_by_server_version(self):
        pg17_sql = doctor._privileges_sql(170000)
        self.assertIn("relrowsecurity", pg17_sql)
        self.assertIn("relforcerowsecurity", pg17_sql)
        self.assertIn("rolbypassrls", pg17_sql)
        self.assertIn("indnullsnotdistinct", pg17_sql)

        pg14_sql = doctor._privileges_sql(140009)
        self.assertIn("relrowsecurity", pg14_sql)
        self.assertIn("rolbypassrls", pg14_sql)
        self.assertNotIn("indnullsnotdistinct", pg14_sql)

    def test_rls_visibility_warning_discloses_only_active_rls_tables(self):
        self.assertIsNone(doctor.rls_visibility_warning("part", {"table_exists": False, "row_security": True}))
        self.assertIsNone(doctor.rls_visibility_warning("part", {"table_exists": True, "row_security": False}))

        warn_filtered = doctor.rls_visibility_warning(
            "part", {"table_exists": True, "row_security": True, "force_row_security": False, "bypass_rls": False}
        )
        self.assertIsNotNone(warn_filtered)
        assert warn_filtered is not None
        self.assertIn("entities.part", warn_filtered)
        self.assertIn("relrowsecurity=true", warn_filtered)
        self.assertIn("rolbypassrls=false", warn_filtered)

        warn_bypass = doctor.rls_visibility_warning(
            "stock", {"table_exists": True, "row_security": True, "force_row_security": True, "bypass_rls": True}
        )
        self.assertIsNotNone(warn_bypass)
        assert warn_bypass is not None
        self.assertIn("relforcerowsecurity=true", warn_bypass)
        self.assertIn("rolbypassrls=true", warn_bypass)

    def test_grain_summarizability_failure_taxonomy(self):
        clean = {
            "orphan_groups": 0, "fanout_groups": 0, "source_duplicate_groups": 0,
            "node_duplicate_groups": 0, "source_null_key_rows": 0, "node_null_key_rows": 0,
            "excluded_null_rows": 0,
        }
        self.assertEqual(grain.summarizability_failures(clean, False), [])
        self.assertEqual(grain.summarizability_failures({**clean, "orphan_groups": 2}, False), ["incompleteness"])
        self.assertEqual(grain.summarizability_failures({**clean, "fanout_groups": 1}, False), ["non_strictness"])
        self.assertEqual(
            grain.summarizability_failures({**clean, "source_duplicate_groups": 1, "node_null_key_rows": 1}, False),
            ["non_strictness", "null_key"],
        )
        self.assertEqual(grain.summarizability_failures({**clean, "excluded_null_rows": 3}, False), ["null_exclusion"])
        self.assertEqual(grain.summarizability_failures({**clean, "excluded_null_rows": 3}, True), [])

    def test_grain_evaluate_fails_closed_when_rls_filters_check_role(self):
        actual = {
            "source": {"entity": "fact", "table": "sxc_demo.rls_fact", "key": ["fact_id"]},
            "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
            "group_by": ["node_id"],
            "aggregate": {"function": "sum", "column": "qty"},
            "filters": {"exact": {}, "normalized": {}},
        }
        rls_meta = [{
            "relkind": "r",
            "relrowsecurity": True,
            "relforcerowsecurity": False,
            "rolbypassrls": False,
            "unique_nulls_not_distinct": False,
        }]
        with mock.patch.object(grain.db, "rows", return_value=rls_meta), \
                mock.patch.object(grain.db, "scalar", side_effect=AssertionError("must not read rows under RLS")):
            outcome = grain.evaluate(object(), actual, 100)
        self.assertEqual(outcome["complete"], False)
        self.assertEqual(outcome["reason"], "row_level_security")
        self.assertTrue(outcome["source_row_security"])


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class RlsAndNullsNotDistinctPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture("sxc_t_rls_cat").setup()
        cls._owner_exec(
            "CREATE TABLE sxc_demo.rls_node (node_id text PRIMARY KEY, alt_code text, "
            "CONSTRAINT rls_node_alt_nnd UNIQUE NULLS NOT DISTINCT (alt_code));"
            "CREATE TABLE sxc_demo.rls_fact (fact_id integer PRIMARY KEY, node_id text, qty numeric, tenant text);"
            "INSERT INTO sxc_demo.rls_node VALUES ('a', 'c1'), ('b', NULL);"
            "INSERT INTO sxc_demo.rls_fact VALUES (1, 'a', 10, 't1'), (2, 'b', 20, 't2');"
        )

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    @classmethod
    def _owner_exec(cls, sql: str):
        conn = db.connect("SXC_PG_OWNER_DSN")
        try:
            with db.write_tx(conn):
                for stmt in sql.split(";"):
                    if stmt.strip():
                        conn.run(stmt)
        finally:
            conn.close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sxc_rls_pg_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg_path = self.tmp / "crosscheck.toml"
        self.cfg_path.write_text(
            'version = 1\n'
            '[source]\nschemas = ["sxc_demo"]\noutput_schema = "sxc_out"\nstatement_timeout_ms = 5000\n'
            '[entities.fact]\ntable = "sxc_demo.rls_fact"\nkey = ["fact_id"]\n'
            '[entities.node]\ntable = "sxc_demo.rls_node"\nkey = ["node_id"]\n'
            '[measures.total]\nentity = "fact"\ncolumn = "qty"\nagg = "sum"\nby = "node_id"\nnode = "node"\n',
            encoding="utf-8",
        )
        self.cfg = config.load(self.cfg_path)
        self.spec_path = self.tmp / "grain.json"
        self.spec_path.write_text(
            json.dumps({
                "schema_version": 1,
                "contracts": [{
                    "measure": "total",
                    "source": {"entity": "fact", "table": "sxc_demo.rls_fact", "key": ["fact_id"]},
                    "group_by": ["node_id"],
                    "aggregate": {"function": "sum", "column": "qty"},
                    "filters": {"exact": {}, "normalized": {}},
                    "output": {"cube": "m_total", "key": ["key"], "value": "value", "reaggregate": "sum"},
                    "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
                    "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
                    "null_policy": {"operation": "exclude", "allow_excluded_rows": False},
                }],
            }),
            encoding="utf-8",
        )

    def _run_doctor(self) -> tuple[int, dict]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(["doctor", "--config", str(self.cfg_path), "--out", str(self.tmp)])
        return code, json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))

    def test_unique_nulls_not_distinct_and_clean_grain_catalog_flags(self):
        code, doc_res = self._run_doctor()
        self.assertEqual(code, 0, doc_res)
        checks = {c["id"]: c for c in doc_res["checks"]}
        self.assertFalse(checks["privileges:select:fact"]["detail"]["row_security"])
        self.assertFalse(checks["privileges:select:fact"]["detail"]["unique_nulls_not_distinct"])
        self.assertFalse(checks["privileges:select:node"]["detail"]["row_security"])
        self.assertTrue(checks["privileges:select:node"]["detail"]["unique_nulls_not_distinct"])
        self.assertFalse(any("row-level security" in n for n in doc_res["not_proven"]))

        grain_args = SimpleNamespace(
            spec=str(self.spec_path),
            max_rows=10000,
            config=str(self.cfg_path),
            _diagnostic_configuration_sha=None,
        )
        g_res = grain.run(self.cfg, grain_args, self.tmp)
        self.assertEqual(g_res.status, "pass")
        ev = next(c for c in g_res.checks if c.id == "grain:c0001:evidence").detail
        self.assertFalse(ev["source_row_security"])
        self.assertFalse(ev["node_row_security"])
        self.assertFalse(ev["source_unique_nulls_not_distinct"])
        self.assertTrue(ev["node_unique_nulls_not_distinct"])
        self.assertEqual(ev["summarizability_failures"], [])

    def test_enabled_row_level_security_warns_in_doctor_and_fails_closed_in_grain(self):
        self._owner_exec(
            "ALTER TABLE sxc_demo.rls_fact ENABLE ROW LEVEL SECURITY;"
            "DROP POLICY IF EXISTS t1_only ON sxc_demo.rls_fact;"
            "CREATE POLICY t1_only ON sxc_demo.rls_fact FOR SELECT USING (tenant = 't1');"
        )
        self.addCleanup(
            self._owner_exec,
            "DROP POLICY IF EXISTS t1_only ON sxc_demo.rls_fact;"
            "ALTER TABLE sxc_demo.rls_fact DISABLE ROW LEVEL SECURITY;",
        )

        code, doc_res = self._run_doctor()
        self.assertEqual(code, 0, doc_res)
        checks = {c["id"]: c for c in doc_res["checks"]}
        fact_detail = checks["privileges:select:fact"]["detail"]
        self.assertTrue(fact_detail["row_security"])
        self.assertFalse(fact_detail["bypass_rls"])
        self.assertTrue(any("entities.fact: PostgreSQL row-level security is enabled" in n
                            for n in doc_res["not_proven"]))

        grain_args = SimpleNamespace(
            spec=str(self.spec_path),
            max_rows=10000,
            config=str(self.cfg_path),
            _diagnostic_configuration_sha=None,
        )
        g_res = grain.run(self.cfg, grain_args, self.tmp)
        self.assertEqual(g_res.status, "not_run")
        ev = next(c for c in g_res.checks if c.id == "grain:c0001:evidence")
        self.assertEqual(ev.status, "not_run")
        self.assertEqual(ev.detail["reason"], "row_level_security")
        self.assertTrue(ev.detail["source_row_security"])
        self.assertIn(grain._RLS_NOT_PROVEN, g_res.not_proven)
        payload = json.dumps(g_res.to_dict())
        for secret in ("rls_fact", "rls_node", "t1", "t2"):
            self.assertNotIn(secret, payload)


if __name__ == "__main__":
    unittest.main()
