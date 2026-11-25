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
        for token in (
            "relrowsecurity",
            "relforcerowsecurity",
            "rolbypassrls",
            "indnullsnotdistinct",
            "indisvalid",
            "indisready",
            "indpred IS NULL",
            "indexprs IS NULL",
            "relispopulated",
            "relpersistence",
            "relhasrules",
            "view_security_invoker",
            "view_security_barrier",
            "convalidated",
            "pg_inherits",
            "child_partition_row_security",
            "child_foreign_partitions",
            "child_unlogged_partitions",
            "child_unvalidated_constraints",
            "mismatched_column_collations",
            "pg_collation_actual_version",
        ):
            self.assertIn(token, pg17_sql)
        self.assertIn("pg_database_collation_actual_version", doctor._DB_COLLATION_VERSION_SQL)

        pg14_sql = doctor._privileges_sql(140009)
        self.assertIn("relrowsecurity", pg14_sql)
        self.assertIn("rolbypassrls", pg14_sql)
        self.assertIn("relispopulated", pg14_sql)
        self.assertIn("convalidated", pg14_sql)
        self.assertNotIn("indnullsnotdistinct", pg14_sql)

    def test_v035_catalog_warnings_views_partitions_unlogged_and_collation(self):
        self.assertEqual(doctor.collation_version_warnings({"db_collation_version_mismatch": False}), [])
        coll_warns = doctor.collation_version_warnings({"db_collation_version_mismatch": True})
        self.assertEqual(len(coll_warns), 1)
        self.assertIn("datcollversion", coll_warns[0])

        # Default view (security_invoker=false) warns; security_invoker=true clears it.
        definer_warns = doctor.catalog_warnings(
            "v_def", {"table_exists": True, "relkind": "v", "view_security_invoker": False, "has_rules": True}
        )
        self.assertEqual(len(definer_warns), 1)
        self.assertIn("view_security_invoker=false", definer_warns[0])
        self.assertEqual(
            doctor.catalog_warnings(
                "v_inv",
                {"table_exists": True, "relkind": "v", "view_security_invoker": True,
                 "view_security_barrier": True, "has_rules": True},
            ),
            [],
        )

        # UNLOGGED table, child UNLOGGED partition, child foreign partition, child NOT VALID constraints, column collation mismatch.
        unlogged_warns = doctor.catalog_warnings(
            "tmp_fact", {"table_exists": True, "relkind": "r", "relpersistence": "u"}
        )
        self.assertEqual(len(unlogged_warns), 1)
        self.assertIn("relpersistence='u'", unlogged_warns[0])

        child_warns = doctor.catalog_warnings(
            "part_fact",
            {
                "table_exists": True,
                "relkind": "p",
                "relpersistence": "p",
                "child_foreign_partitions": True,
                "child_unlogged_partitions": True,
                "child_unvalidated_constraints": 3,
                "mismatched_column_collations": 2,
            },
        )
        self.assertEqual(len(child_warns), 4)
        self.assertTrue(any("child_foreign_partitions=true" in w for w in child_warns))
        self.assertTrue(any("child_unlogged_partitions=true" in w for w in child_warns))
        self.assertTrue(any("3 local unvalidated CHECK/FOREIGN KEY constraint(s)" in w for w in child_warns))
        self.assertTrue(any("2 column collation(s) with a version mismatch" in w for w in child_warns))

    def test_grain_evaluate_fails_closed_on_foreign_child_partition(self):
        actual = {
            "source": {"entity": "fact", "table": "sxc_demo.part_parent", "key": ["fact_id"]},
            "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
            "group_by": ["node_id"],
            "aggregate": {"function": "sum", "column": "qty"},
            "filters": {"exact": {}, "normalized": {}},
        }
        foreign_child_meta = [{
            "relkind": "p",
            "relpersistence": "p",
            "relrowsecurity": False,
            "rolbypassrls": False,
            "unique_nulls_not_distinct": False,
            "unvalidated_constraints": 0,
            "child_partition_row_security": False,
            "child_foreign_partitions": True,
            "child_unlogged_partitions": False,
            "child_unvalidated_constraints": 0,
        }]
        with mock.patch.object(grain.db, "rows", return_value=foreign_child_meta), \
                mock.patch.object(grain.db, "scalar", side_effect=AssertionError("must not read rows with foreign child")):
            outcome = grain.evaluate(object(), actual, 100)
        self.assertEqual(outcome["complete"], False)
        self.assertEqual(outcome["reason"], "unsupported_relation")
        self.assertTrue(outcome["source_child_foreign_partitions"])

    def test_catalog_warnings_and_unpopulated_mv_hint(self):
        self.assertEqual(doctor.catalog_warnings("part", {"table_exists": False, "relkind": "f"}), [])
        self.assertEqual(
            doctor.catalog_warnings(
                "part",
                {
                    "table_exists": True,
                    "relkind": "r",
                    "row_security": False,
                    "child_partition_row_security": False,
                    "unvalidated_constraints": 0,
                    "has_rules": False,
                },
            ),
            [],
        )
        # Views and materialized views naturally set relhasrules=true (_RETURN rule) and must not warn.
        for view_kind in ("v", "m"):
            self.assertEqual(
                doctor.catalog_warnings("mv", {"table_exists": True, "relkind": view_kind, "has_rules": True}),
                [],
            )

        child_rls_warns = doctor.catalog_warnings(
            "fact",
            {"table_exists": True, "relkind": "p", "row_security": False, "child_partition_row_security": True},
        )
        self.assertEqual(len(child_rls_warns), 1)
        self.assertIn("child_partition_row_security=true", child_rls_warns[0])

        foreign_warns = doctor.catalog_warnings(
            "ext",
            {"table_exists": True, "relkind": "f", "unvalidated_constraints": 2, "has_rules": True},
        )
        self.assertEqual(len(foreign_warns), 2)
        self.assertTrue(any("foreign table (relkind='f')" in w for w in foreign_warns))
        self.assertTrue(any("2 unvalidated CHECK/FOREIGN KEY constraint(s)" in w for w in foreign_warns))

        table_rule_warns = doctor.catalog_warnings(
            "fact",
            {"table_exists": True, "relkind": "r", "has_rules": True},
        )
        self.assertEqual(len(table_rule_warns), 1)
        self.assertIn("relhasrules=true", table_rule_warns[0])

        cfg = config.load(pgfixture.DEMO_CONFIG)
        mv_hint = doctor.select_hint(
            cfg,
            "stock",
            {"table_exists": True, "can_use": True, "can_select": True, "relkind": "m", "is_populated": False},
        )
        self.assertIn("REFRESH MATERIALIZED VIEW sxc_demo.stock", mv_hint)
        no_sel_hint = doctor.select_hint(
            cfg,
            "stock",
            {"table_exists": True, "can_use": True, "can_select": False, "relkind": "m", "is_populated": False},
        )
        self.assertIn("grant the check role USAGE", no_sel_hint)

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

    def test_partial_and_expression_nulls_not_distinct_indexes_do_not_false_positive(self):
        self._owner_exec(
            "DROP TABLE IF EXISTS sxc_demo.idx_guard_tbl;"
            "CREATE TABLE sxc_demo.idx_guard_tbl (id integer PRIMARY KEY, code text, active boolean);"
            "CREATE UNIQUE INDEX idx_guard_partial ON sxc_demo.idx_guard_tbl (code) NULLS NOT DISTINCT WHERE active;"
            "CREATE UNIQUE INDEX idx_guard_expr ON sxc_demo.idx_guard_tbl (lower(code)) NULLS NOT DISTINCT;"
            "INSERT INTO sxc_demo.idx_guard_tbl VALUES (1, 'A', true), (2, NULL, false);"
        )
        self.addCleanup(self._owner_exec, "DROP TABLE IF EXISTS sxc_demo.idx_guard_tbl;")

        cfg_path = self.tmp / "idx_guard.toml"
        cfg_path.write_text(
            'version = 1\n'
            '[source]\nschemas = ["sxc_demo"]\noutput_schema = "sxc_out"\nstatement_timeout_ms = 5000\n'
            '[entities.guard]\ntable = "sxc_demo.idx_guard_tbl"\nkey = ["id"]\n'
            '[entities.node]\ntable = "sxc_demo.rls_node"\nkey = ["node_id"]\n'
            '[measures.total]\nentity = "guard"\ncolumn = "id"\nagg = "count"\nby = "code"\nnode = "node"\n',
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(["doctor", "--config", str(cfg_path), "--out", str(self.tmp)])
        self.assertEqual(code, 0)
        doc_res = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        checks = {c["id"]: c for c in doc_res["checks"]}
        self.assertFalse(checks["privileges:select:guard"]["detail"]["unique_nulls_not_distinct"])
        self.assertTrue(checks["privileges:select:node"]["detail"]["unique_nulls_not_distinct"])

        spec_path = self.tmp / "idx_guard_grain.json"
        spec_path.write_text(
            json.dumps({
                "schema_version": 1,
                "contracts": [{
                    "measure": "total",
                    "source": {"entity": "guard", "table": "sxc_demo.idx_guard_tbl", "key": ["id"]},
                    "group_by": ["code"],
                    "aggregate": {"function": "count", "column": "id"},
                    "filters": {"exact": {}, "normalized": {}},
                    "output": {"cube": "m_total", "key": ["key"], "value": "value", "reaggregate": "sum"},
                    "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
                    "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
                    "null_policy": {"operation": "exclude", "allow_excluded_rows": True},
                }],
            }),
            encoding="utf-8",
        )
        g_res = grain.run(
            config.load(cfg_path),
            SimpleNamespace(spec=str(spec_path), max_rows=10000, config=str(cfg_path),
                            _diagnostic_configuration_sha=None),
            self.tmp,
        )
        ev = next(c for c in g_res.checks if c.id == "grain:c0001:evidence").detail
        self.assertFalse(ev["source_unique_nulls_not_distinct"])
        self.assertTrue(ev["node_unique_nulls_not_distinct"])

    def test_unpopulated_mv_unvalidated_constraint_table_rule_and_child_partition_rls(self):
        self._owner_exec(
            "DROP MATERIALIZED VIEW IF EXISTS sxc_demo.mv_unpop;"
            "CREATE MATERIALIZED VIEW sxc_demo.mv_unpop AS SELECT node_id FROM sxc_demo.rls_node WITH NO DATA;"
            f'GRANT SELECT ON sxc_demo.mv_unpop TO "{self.fx.check}";'
            "DROP TABLE IF EXISTS sxc_demo.part_parent CASCADE;"
            "CREATE TABLE sxc_demo.part_parent (fact_id integer, node_id text, qty numeric) PARTITION BY RANGE (fact_id);"
            "CREATE TABLE sxc_demo.part_child PARTITION OF sxc_demo.part_parent FOR VALUES FROM (1) TO (100);"
            "INSERT INTO sxc_demo.part_parent VALUES (1, 'a', 10);"
            "ALTER TABLE sxc_demo.part_child ENABLE ROW LEVEL SECURITY;"
            "CREATE POLICY child_only ON sxc_demo.part_child FOR SELECT USING (qty > 0);"
            "ALTER TABLE sxc_demo.rls_fact ADD CONSTRAINT rls_fact_qty_pos CHECK (qty > 0) NOT VALID;"
            "CREATE RULE rls_fact_noop AS ON INSERT TO sxc_demo.rls_fact DO INSTEAD NOTHING;"
        )
        self.addCleanup(
            self._owner_exec,
            "DROP MATERIALIZED VIEW IF EXISTS sxc_demo.mv_unpop;"
            "DROP TABLE IF EXISTS sxc_demo.part_parent CASCADE;"
            "ALTER TABLE sxc_demo.rls_fact DROP CONSTRAINT IF EXISTS rls_fact_qty_pos;"
            "DROP RULE IF EXISTS rls_fact_noop ON sxc_demo.rls_fact;",
        )

        cfg_path = self.tmp / "hygiene.toml"
        cfg_path.write_text(
            'version = 1\n'
            '[source]\nschemas = ["sxc_demo"]\noutput_schema = "sxc_out"\nstatement_timeout_ms = 5000\n'
            '[entities.mv]\ntable = "sxc_demo.mv_unpop"\nkey = ["node_id"]\n'
            '[entities.part_fact]\ntable = "sxc_demo.part_parent"\nkey = ["fact_id"]\n'
            '[entities.fact]\ntable = "sxc_demo.rls_fact"\nkey = ["fact_id"]\n'
            '[entities.node]\ntable = "sxc_demo.rls_node"\nkey = ["node_id"]\n'
            '[measures.ptotal]\nentity = "part_fact"\ncolumn = "qty"\nagg = "sum"\nby = "node_id"\nnode = "node"\n',
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(["doctor", "--config", str(cfg_path), "--out", str(self.tmp)])
        self.assertEqual(code, 1)
        doc_res = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        checks = {c["id"]: c for c in doc_res["checks"]}
        self.assertEqual(checks["privileges:select:mv"]["status"], "fail")
        self.assertEqual(checks["privileges:select:mv"]["detail"]["relkind"], "m")
        self.assertFalse(checks["privileges:select:mv"]["detail"]["is_populated"])
        self.assertIn("REFRESH MATERIALIZED VIEW sxc_demo.mv_unpop", doc_res["next_safe_action"])

        self.assertEqual(checks["privileges:select:part_fact"]["status"], "pass")
        self.assertFalse(checks["privileges:select:part_fact"]["detail"]["row_security"])
        self.assertTrue(checks["privileges:select:part_fact"]["detail"]["child_partition_row_security"])
        self.assertTrue(any("entities.part_fact: a child partition" in n for n in doc_res["not_proven"]))

        self.assertEqual(checks["privileges:select:fact"]["detail"]["unvalidated_constraints"], 1)
        self.assertTrue(checks["privileges:select:fact"]["detail"]["has_rules"])
        self.assertTrue(any("1 unvalidated CHECK/FOREIGN KEY constraint(s)" in n for n in doc_res["not_proven"]))
        self.assertTrue(any("entities.fact: source table has PostgreSQL rewrite rules" in n for n in doc_res["not_proven"]))
        self.assertFalse(any("entities.mv: source table has PostgreSQL rewrite rules" in n for n in doc_res["not_proven"]))

        # Refreshing the materialized view and validating the constraint clears both issues cleanly.
        self._owner_exec(
            "REFRESH MATERIALIZED VIEW sxc_demo.mv_unpop;"
            "ALTER TABLE sxc_demo.rls_fact VALIDATE CONSTRAINT rls_fact_qty_pos;"
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code_after = sxc.main(["doctor", "--config", str(cfg_path), "--out", str(self.tmp)])
        self.assertEqual(code_after, 0)
        doc_after = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        checks_after = {c["id"]: c for c in doc_after["checks"]}
        self.assertEqual(checks_after["privileges:select:mv"]["status"], "pass")
        self.assertTrue(checks_after["privileges:select:mv"]["detail"]["is_populated"])
        self.assertEqual(checks_after["privileges:select:fact"]["detail"]["unvalidated_constraints"], 0)
        self.assertFalse(any("unvalidated CHECK/FOREIGN KEY" in n for n in doc_after["not_proven"]))

        # Grain discloses child-partition RLS asymmetry without leaking relation identifiers.
        spec_path = self.tmp / "part_grain.json"
        spec_path.write_text(
            json.dumps({
                "schema_version": 1,
                "contracts": [{
                    "measure": "ptotal",
                    "source": {"entity": "part_fact", "table": "sxc_demo.part_parent", "key": ["fact_id"]},
                    "group_by": ["node_id"],
                    "aggregate": {"function": "sum", "column": "qty"},
                    "filters": {"exact": {}, "normalized": {}},
                    "output": {"cube": "m_ptotal", "key": ["key"], "value": "value", "reaggregate": "sum"},
                    "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
                    "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
                    "null_policy": {"operation": "exclude", "allow_excluded_rows": False},
                }],
            }),
            encoding="utf-8",
        )
        g_res = grain.run(
            config.load(cfg_path),
            SimpleNamespace(spec=str(spec_path), max_rows=10000, config=str(cfg_path),
                            _diagnostic_configuration_sha=None),
            self.tmp,
        )
        self.assertEqual(g_res.status, "pass")
        ev = next(c for c in g_res.checks if c.id == "grain:c0001:evidence").detail
        self.assertFalse(ev["source_row_security"])
        self.assertTrue(ev["source_child_partition_row_security"])
        self.assertIn(grain._CHILD_RLS_NOT_PROVEN, g_res.not_proven)
        payload = json.dumps(g_res.to_dict())
        for secret in ("part_parent", "part_child", "rls_node"):
            self.assertNotIn(secret, payload)

    def test_view_security_options_unlogged_child_constraints_and_foreign_partition(self):
        self._owner_exec(
            "DROP VIEW IF EXISTS sxc_demo.v_definer, sxc_demo.v_invoker;"
            "CREATE VIEW sxc_demo.v_definer AS SELECT node_id FROM sxc_demo.rls_node;"
            "CREATE VIEW sxc_demo.v_invoker WITH (security_invoker = true, security_barrier = true) "
            "AS SELECT node_id FROM sxc_demo.rls_node;"
            f'GRANT SELECT ON sxc_demo.v_definer, sxc_demo.v_invoker TO "{self.fx.check}";'
            "DROP TABLE IF EXISTS sxc_demo.unlogged_fact, sxc_demo.part_hyg, sxc_demo.part_fparent CASCADE;"
            "CREATE UNLOGGED TABLE sxc_demo.unlogged_fact (fact_id integer PRIMARY KEY, node_id text, qty numeric);"
            "INSERT INTO sxc_demo.unlogged_fact VALUES (1, 'a', 10);"
            "CREATE TABLE sxc_demo.part_hyg (fact_id integer, node_id text, qty numeric) PARTITION BY RANGE (fact_id);"
            "CREATE UNLOGGED TABLE sxc_demo.part_hyg_u PARTITION OF sxc_demo.part_hyg FOR VALUES FROM (1) TO (100);"
            "INSERT INTO sxc_demo.part_hyg VALUES (1, 'a', 10);"
            "ALTER TABLE sxc_demo.part_hyg_u ADD CONSTRAINT part_hyg_u_qty_pos CHECK (qty > 0) NOT VALID;"
            "CREATE TABLE sxc_demo.part_fparent (fact_id integer, node_id text, qty numeric) PARTITION BY RANGE (fact_id);"
        )
        admin = pgfixture._admin(self.fx.name)
        try:
            admin.run("DROP FOREIGN DATA WRAPPER IF EXISTS sxc_dummy_fdw CASCADE")
            admin.run("CREATE FOREIGN DATA WRAPPER sxc_dummy_fdw")
            admin.run("CREATE SERVER sxc_dummy_srv FOREIGN DATA WRAPPER sxc_dummy_fdw")
            admin.run(f'GRANT USAGE ON FOREIGN SERVER sxc_dummy_srv TO "{self.fx.owner}"')
        finally:
            admin.close()
        self._owner_exec(
            "CREATE FOREIGN TABLE sxc_demo.part_fchild PARTITION OF sxc_demo.part_fparent "
            "FOR VALUES FROM (1) TO (100) SERVER sxc_dummy_srv;"
        )

        def cleanup():
            self._owner_exec(
                "DROP VIEW IF EXISTS sxc_demo.v_definer, sxc_demo.v_invoker;"
                "DROP TABLE IF EXISTS sxc_demo.unlogged_fact, sxc_demo.part_hyg, sxc_demo.part_fparent CASCADE;"
            )
            adm = pgfixture._admin(self.fx.name)
            try:
                adm.run("DROP FOREIGN DATA WRAPPER IF EXISTS sxc_dummy_fdw CASCADE")
            finally:
                adm.close()

        self.addCleanup(cleanup)

        cfg_path = self.tmp / "v035_hygiene.toml"
        cfg_path.write_text(
            'version = 1\n'
            '[source]\nschemas = ["sxc_demo"]\noutput_schema = "sxc_out"\nstatement_timeout_ms = 5000\n'
            '[entities.v_def]\ntable = "sxc_demo.v_definer"\nkey = ["node_id"]\n'
            '[entities.v_inv]\ntable = "sxc_demo.v_invoker"\nkey = ["node_id"]\n'
            '[entities.ufact]\ntable = "sxc_demo.unlogged_fact"\nkey = ["fact_id"]\n'
            '[entities.phyg]\ntable = "sxc_demo.part_hyg"\nkey = ["fact_id"]\n'
            '[entities.pfpar]\ntable = "sxc_demo.part_fparent"\nkey = ["fact_id"]\n'
            '[entities.node]\ntable = "sxc_demo.rls_node"\nkey = ["node_id"]\n'
            '[measures.utotal]\nentity = "ufact"\ncolumn = "qty"\nagg = "sum"\nby = "node_id"\nnode = "node"\n'
            '[measures.htotal]\nentity = "phyg"\ncolumn = "qty"\nagg = "sum"\nby = "node_id"\nnode = "node"\n'
            '[measures.ftotal]\nentity = "pfpar"\ncolumn = "qty"\nagg = "sum"\nby = "node_id"\nnode = "node"\n',
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = sxc.main(["doctor", "--config", str(cfg_path), "--out", str(self.tmp)])
        self.assertEqual(code, 0)
        doc_res = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        checks = {c["id"]: c for c in doc_res["checks"]}
        self.assertFalse(checks["pg_version"]["detail"]["db_collation_version_mismatch"])

        self.assertFalse(checks["privileges:select:v_def"]["detail"]["view_security_invoker"])
        self.assertFalse(checks["privileges:select:v_def"]["detail"]["view_security_barrier"])
        self.assertTrue(checks["privileges:select:v_inv"]["detail"]["view_security_invoker"])
        self.assertTrue(checks["privileges:select:v_inv"]["detail"]["view_security_barrier"])
        self.assertTrue(any("entities.v_def: source view executes with view-owner privileges" in n
                            for n in doc_res["not_proven"]))
        self.assertFalse(any("entities.v_inv: source view" in n for n in doc_res["not_proven"]))

        self.assertEqual(checks["privileges:select:ufact"]["detail"]["relpersistence"], "u")
        self.assertTrue(any("entities.ufact: source relation is UNLOGGED" in n for n in doc_res["not_proven"]))

        phyg_detail = checks["privileges:select:phyg"]["detail"]
        self.assertEqual(phyg_detail["relpersistence"], "p")
        self.assertTrue(phyg_detail["child_unlogged_partitions"])
        self.assertEqual(phyg_detail["unvalidated_constraints"], 0)
        self.assertEqual(phyg_detail["child_unvalidated_constraints"], 1)
        self.assertEqual(phyg_detail["mismatched_column_collations"], 0)
        self.assertTrue(any("entities.phyg: a child partition or inherited relation is UNLOGGED" in n
                            for n in doc_res["not_proven"]))
        self.assertTrue(any("entities.phyg: child partition(s) or inherited relation(s) have 1 local unvalidated" in n
                            for n in doc_res["not_proven"]))

        pfpar_detail = checks["privileges:select:pfpar"]["detail"]
        self.assertTrue(pfpar_detail["child_foreign_partitions"])
        self.assertTrue(any("entities.pfpar: a child partition or inherited relation is a foreign table" in n
                            for n in doc_res["not_proven"]))

        # Grain discloses unlogged + child NOT VALID constraints (opaque) and fails closed on foreign child partition.
        spec_hyg = self.tmp / "hyg_grain.json"
        spec_hyg.write_text(
            json.dumps({
                "schema_version": 1,
                "contracts": [
                    {
                        "measure": "utotal",
                        "source": {"entity": "ufact", "table": "sxc_demo.unlogged_fact", "key": ["fact_id"]},
                        "group_by": ["node_id"],
                        "aggregate": {"function": "sum", "column": "qty"},
                        "filters": {"exact": {}, "normalized": {}},
                        "output": {"cube": "m_utotal", "key": ["key"], "value": "value", "reaggregate": "sum"},
                        "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
                        "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
                        "null_policy": {"operation": "exclude", "allow_excluded_rows": False},
                    },
                    {
                        "measure": "htotal",
                        "source": {"entity": "phyg", "table": "sxc_demo.part_hyg", "key": ["fact_id"]},
                        "group_by": ["node_id"],
                        "aggregate": {"function": "sum", "column": "qty"},
                        "filters": {"exact": {}, "normalized": {}},
                        "output": {"cube": "m_htotal", "key": ["key"], "value": "value", "reaggregate": "sum"},
                        "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
                        "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
                        "null_policy": {"operation": "exclude", "allow_excluded_rows": False},
                    },
                    {
                        "measure": "ftotal",
                        "source": {"entity": "pfpar", "table": "sxc_demo.part_fparent", "key": ["fact_id"]},
                        "group_by": ["node_id"],
                        "aggregate": {"function": "sum", "column": "qty"},
                        "filters": {"exact": {}, "normalized": {}},
                        "output": {"cube": "m_ftotal", "key": ["key"], "value": "value", "reaggregate": "sum"},
                        "node": {"entity": "node", "table": "sxc_demo.rls_node", "key": ["node_id"]},
                        "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
                        "null_policy": {"operation": "exclude", "allow_excluded_rows": False},
                    },
                ],
            }),
            encoding="utf-8",
        )
        g_res = grain.run(
            config.load(cfg_path),
            SimpleNamespace(spec=str(spec_hyg), max_rows=10000, config=str(cfg_path),
                            _diagnostic_configuration_sha=None),
            self.tmp,
        )
        self.assertEqual(g_res.status, "not_run")
        ev_by_id = {c.id: c for c in g_res.checks}
        self.assertEqual(ev_by_id["grain:c0001:evidence"].status, "pass")
        self.assertTrue(ev_by_id["grain:c0001:evidence"].detail["source_unlogged"])
        self.assertEqual(ev_by_id["grain:c0002:evidence"].status, "pass")
        self.assertTrue(ev_by_id["grain:c0002:evidence"].detail["source_child_unlogged_partitions"])
        self.assertEqual(ev_by_id["grain:c0002:evidence"].detail["source_child_unvalidated_constraints"], 1)
        self.assertEqual(ev_by_id["grain:c0003:evidence"].status, "not_run")
        self.assertEqual(ev_by_id["grain:c0003:evidence"].detail["reason"], "unsupported_relation")
        self.assertTrue(ev_by_id["grain:c0003:evidence"].detail["source_child_foreign_partitions"])
        self.assertIn(grain._UNLOGGED_NOT_PROVEN, g_res.not_proven)
        self.assertIn(grain._UNVALIDATED_CONSTRAINTS_NOT_PROVEN, g_res.not_proven)
        self.assertIn(grain._CHILD_FOREIGN_NOT_PROVEN, g_res.not_proven)
        payload = json.dumps(g_res.to_dict())
        for secret in ("unlogged_fact", "part_hyg", "part_fparent", "part_fchild", "sxc_dummy"):
            self.assertNotIn(secret, payload)

        # Validating the child constraint clears child_unvalidated_constraints cleanly.
        self._owner_exec("ALTER TABLE sxc_demo.part_hyg_u VALIDATE CONSTRAINT part_hyg_u_qty_pos;")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code_after = sxc.main(["doctor", "--config", str(cfg_path), "--out", str(self.tmp)])
        self.assertEqual(code_after, 0)
        doc_after = json.loads((self.tmp / "doctor" / "result.json").read_text(encoding="utf-8"))
        checks_after = {c["id"]: c for c in doc_after["checks"]}
        self.assertEqual(checks_after["privileges:select:phyg"]["detail"]["child_unvalidated_constraints"], 0)
        self.assertFalse(any("local unvalidated CHECK/FOREIGN KEY" in n for n in doc_after["not_proven"]))


if __name__ == "__main__":
    unittest.main()
