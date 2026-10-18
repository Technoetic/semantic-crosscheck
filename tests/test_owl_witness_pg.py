"""Actual HermiT and PostgreSQL checks; no source tables are changed by witnesses."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import pgfixture
from semantic_crosscheck import db, owl, stage_build, stage_reason
from semantic_crosscheck.config import Defined
from semantic_crosscheck.result import Result
from test_owl_witness import HAVE_OWL, JAVA, owl_witness, witness_config


@unittest.skipUnless(pgfixture.available() and HAVE_OWL, "needs test PostgreSQL, owlready2 and Java")
class WitnessPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture("sxc_t_owl_witness").setup()
        cls.addClassCleanup(cls.fx.teardown)

    def setUp(self):
        self.assertIsNotNone(owl_witness, "the synthetic witness implementation is missing")
        self.cfg = witness_config()

    def run_witnesses(self, cfg=None, java=JAVA):
        result, statements = Result("reason"), []
        conn = self.fx.check_conn()
        real = conn.run

        def record(sql, **params):
            statements.append((sql, params))
            return real(sql, **params)

        conn.run = record
        try:
            owl_witness.run(conn, cfg or self.cfg, java, result)
        finally:
            conn.close()
        return result, statements

    def test_actual_engines_pass_all_five_independent_expected_memberships(self):
        # Skipping either engine or comparing the two translations only must not satisfy this test.
        result, statements = self.run_witnesses()
        self.assertEqual([c.id for c in result.checks], ["v5:witness:" + d.name for d in self.cfg.ontology.defined])
        self.assertTrue(all(c.status == "pass" for c in result.checks), result.to_dict())
        self.assertEqual([c.detail["expected_members"] for c in result.checks], [2, 2, 2, 1, 1])
        self.assertTrue(all(c.detail["nonmember_controls"] >= 2 for c in result.checks))
        self.assertEqual(sum(sql.startswith("WITH ") for sql, _ in statements), 5)
        self.assertTrue(all(params for sql, params in statements if sql.startswith("WITH ")))
        self.assertEqual(sum("BEGIN TRANSACTION" in sql and "READ ONLY" in sql for sql, _ in statements), 1)
        self.assertEqual(statements[-1][0], "ROLLBACK")
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "CREATE", "UPDATE", "DELETE", "DROP"))
                             for sql, _ in statements))
        encoded = json.dumps(result.to_dict())
        for value in ("typed_child", "plain_child", "isolated", "sxc_demo", "VALUES", "postgresql://"):
            self.assertNotIn(value, encoded)

    def test_wrong_direction_is_detected_by_expected_memberships(self):
        real = owl.definition

        def wrong(kind, node_cls, cls, edge, closure):
            return real("has_parent" if kind == "has_child" else kind, node_cls, cls, edge, closure)

        with mock.patch.object(owl, "definition", wrong):
            result, _ = self.run_witnesses()
        check = result.checks[0]
        self.assertEqual(check.status, "fail")
        self.assertGreater(check.detail["owl_missing"], 0)
        self.assertEqual(check.detail["sql_missing"], 0)

    def test_direct_edge_substitution_for_transitive_contains_is_detected(self):
        real = owl.definition

        def direct(kind, node_cls, cls, edge, closure):
            return node_cls & edge.some(cls) if kind == "contains_class" else real(kind, node_cls, cls, edge, closure)

        with mock.patch.object(owl, "definition", direct):
            result, _ = self.run_witnesses()
        self.assertEqual((result.checks[2].status, result.checks[2].detail["owl_missing"]), ("fail", 1))

    def test_removed_intersection_is_detected_in_both_engines_even_if_they_agree(self):
        # Independent expectations are essential when the SQL and OWL translations share the same defect.
        definition, sql_definition = owl.definition, owl.defined_sql

        def no_intersection(kind, node_cls, cls, edge, closure):
            if kind == "class_with_child":
                return definition("has_child", node_cls, cls, edge, closure)
            return definition(kind, node_cls, cls, edge, closure)

        def no_sql_intersection(cfg, defined):
            if defined.kind == "class_with_child":
                defined = Defined(defined.name, "has_child")
            return sql_definition(cfg, defined)

        with mock.patch.object(owl, "definition", no_intersection), mock.patch.object(owl, "defined_sql", no_sql_intersection):
            result, _ = self.run_witnesses()
        check = result.checks[3]
        self.assertEqual((check.status, check.detail["owl_unexpected"], check.detail["sql_unexpected"]), ("fail", 1, 1))
        self.assertEqual((check.detail["owl_only"], check.detail["sql_only"]), (0, 0))

    def test_configured_no_children_conflict_is_not_run(self):
        self.cfg.ontology.defined = [Defined("Impossible", "class_with_child", "raw")]
        result, statements = self.run_witnesses()
        self.assertEqual((result.checks[0].status, result.checks[0].detail["reason"]),
                         ("not_run", "configured_class_unsatisfiable"))
        self.assertFalse(statements)

    def test_size_limit_is_not_run(self):
        self.cfg.verify.owl_max_individuals = 1
        result, statements = self.run_witnesses()
        self.assertTrue(all((c.status, c.detail["reason"]) == ("not_run", "resource_limit") for c in result.checks))
        self.assertFalse(statements)

    def test_actual_sql_without_class_column_supports_nonclass_definitions(self):
        self.cfg.ontology.defined = self.cfg.ontology.defined[:2]
        self.cfg.ontology.classes = {}
        self.cfg.ontology.class_column = ""
        self.cfg.ontology.no_children = []
        result, _ = self.run_witnesses()
        self.assertEqual([check.status for check in result.checks], ["pass", "pass"])

    def test_sql_failure_is_an_error_with_only_sqlstate(self):
        # A SQL failure must not be mistaken for zero members or leak the query/parameters.
        self.cfg.ontology.defined = self.cfg.ontology.defined[:1]
        with mock.patch.object(owl, "defined_sql", return_value=("SELECT 1 / 0", {})):
            result, _ = self.run_witnesses()
        self.assertEqual((result.checks[0].status, result.checks[0].detail["sqlstate"]), ("error", "22012"))
        self.assertNotIn("1 / 0", json.dumps(result.to_dict()))
        self.assertTrue(any("database_error" in text for text in result.not_proven))

    def test_real_java_failure_is_an_error_without_its_path(self):
        self.cfg.ontology.defined = self.cfg.ontology.defined[:1]
        result, _ = self.run_witnesses(java="D:/nonexistent_witness_java/private_marker.exe")
        self.assertEqual((result.checks[0].status, result.checks[0].detail["reason"]), ("error", "reasoner_error"))
        self.assertNotIn("private_marker", json.dumps(result.to_dict()))
        self.assertTrue(any("reasoner_error" in text for text in result.not_proven))

    def test_inconsistent_synthetic_graph_does_not_entail_a_passing_witness(self):
        self.cfg.ontology.defined = self.cfg.ontology.defined[:1]
        case = owl_witness.plan(self.cfg, self.cfg.ontology.defined[0])
        case.graph.nodes["root"] = {"raw"}  # A no_children individual with an asserted child.
        with mock.patch.object(owl_witness, "plan", return_value=case):
            result, _ = self.run_witnesses()
        self.assertEqual((result.checks[0].status, result.checks[0].detail["reason"]),
                         ("not_run", "synthetic_inconsistent"))

    def test_real_vacuity_disclaimer_survives_passing_synthetic_witness(self):
        # Synthetic coverage cannot prove membership exists in the real source graph.
        import tempfile
        from pathlib import Path
        cfg = witness_config()
        cfg.ontology.defined = [Defined("ProductUsedAsPart", "class_used_as_part", "product")]
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            stage_build.run(cfg, SimpleNamespace(hierarchy=""), out).write(out)
            result = stage_reason.run(cfg, SimpleNamespace(only=["owl"], synthetic_witnesses=True), out)
        checks = {c.id: c for c in result.checks}
        self.assertEqual(checks["v5:defined:ProductUsedAsPart"].detail["owl_members"], 0)
        self.assertEqual(checks["v5:witness:ProductUsedAsPart"].status, "pass")
        self.assertTrue(any("v5:defined:ProductUsedAsPart" in text and "vacuously" in text for text in result.not_proven))


if __name__ == "__main__":
    unittest.main()
