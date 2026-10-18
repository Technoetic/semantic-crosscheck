"""Synthetic OWL witnesses: hand-derived memberships, with the configured axioms intact."""

import copy
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pgfixture
from semantic_crosscheck import config, owl, stage_reason
from semantic_crosscheck.config import Defined
from semantic_crosscheck.result import EXIT_CONFIG, Result, StageError

try:
    from semantic_crosscheck import owl_witness
except ImportError:
    owl_witness = None

JAVA = owl.java_executable()
HAVE_OWL = bool(JAVA and importlib.util.find_spec("owlready2"))


def witness_config():
    cfg = config.load(pgfixture.DEMO_CONFIG)
    cfg.ontology.defined = [Defined("HasChild", "has_child"), Defined("HasParent", "has_parent"),
                            Defined("ContainsRaw", "contains_class", "raw"),
                            Defined("TypedParent", "class_with_child", "product"),
                            Defined("UsedRaw", "class_used_as_part", "raw")]
    return cfg


class WitnessScopeTest(unittest.TestCase):
    def test_no_defined_classes_are_explicitly_not_run_and_not_proven(self):
        # Empty requested witness coverage must not become an implicit pass.
        self.assertIsNotNone(owl_witness)
        cfg = witness_config()
        cfg.ontology.defined = []
        result = Result("reason")
        owl_witness.run(None, cfg, JAVA, result)
        self.assertEqual([(check.id, check.status) for check in result.checks], [("v5:witness:scope", "not_run")])
        self.assertTrue(any("no configured defined classes" in text for text in result.not_proven))

    def test_explicit_witness_request_cannot_be_excluded_by_only(self):
        # Removing the scope guard would silently discard the requested OWL checks.
        with self.assertRaises(StageError) as caught:
            stage_reason._only(SimpleNamespace(only=["shacl"], synthetic_witnesses=True))
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)

    def test_explicit_witness_request_needs_a_configured_ontology(self):
        # A missing ontology must not let unrelated passing shapes claim the requested witness work passed.
        cfg = witness_config()
        cfg.ontology.hierarchy = ""
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            Result("build").write(out)
            with self.assertRaises(StageError) as caught:
                stage_reason.run(cfg, SimpleNamespace(only=["owl"], synthetic_witnesses=True), out)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)


@unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java")
class WitnessOntologyTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(owl_witness, "the synthetic witness implementation is missing")
        self.cfg = witness_config()

    def test_all_five_kinds_have_real_positive_and_nonmember_controls(self):
        # Wrong direction, direct-only contains, and a missing class intersection must change these literal sets.
        expected = [{"root", "middle"}, {"middle", "leaf"}, {"root", "middle"},
                    {"typed_parent"}, {"typed_child"}]
        for defined, want in zip(self.cfg.ontology.defined, expected):
            with self.subTest(kind=defined.kind):
                case = owl_witness.plan(self.cfg, defined)
                answer = owl.reason(self.cfg, case.graph, JAVA)
                self.assertTrue(answer.consistent)
                self.assertEqual(answer.members[defined.name], want)
                self.assertEqual(case.expected, want)
                self.assertTrue(set(case.graph.nodes) - want)
                self.assertTrue(all(len(values) <= 1 for values in case.graph.nodes.values()),
                                "disjoint classes are placed on separate individuals")

    def test_no_children_class_is_not_given_an_impossible_positive_parent(self):
        # Ignoring no_children would turn an unsatisfiable class into a fabricated passing witness.
        case = owl_witness.plan(self.cfg, Defined("Impossible", "class_with_child", "raw"))
        self.assertIsNone(case.graph)
        self.assertEqual(case.reason, "configured_class_unsatisfiable")

    def test_class_definition_without_its_class_column_is_a_configuration_gap(self):
        self.cfg.ontology.class_column = ""
        case = owl_witness.plan(self.cfg, self.cfg.ontology.defined[2])
        self.assertIsNone(case.graph)
        self.assertEqual(case.reason, "configuration_gap")

    def test_nonclass_definitions_work_without_a_class_column(self):
        self.cfg.ontology.classes = {}
        self.cfg.ontology.class_column = ""
        self.cfg.ontology.no_children = []
        self.cfg.ontology.defined = self.cfg.ontology.defined[:2]
        for defined, want in zip(self.cfg.ontology.defined, ({"root", "middle"}, {"middle", "leaf"})):
            case = owl_witness.plan(self.cfg, defined)
            self.assertEqual(owl.reason(self.cfg, case.graph, JAVA).members[defined.name], want)

    def test_controls_are_absence_of_entailment_and_not_negative_owl_facts(self):
        # Treating absent types as closed-world negatives would reject this consistent extension.
        defined = self.cfg.ontology.defined[-1]
        case = owl_witness.plan(self.cfg, defined)
        before = owl.reason(self.cfg, case.graph, JAVA)
        self.assertNotIn("plain_child", before.members[defined.name])
        extension = copy.deepcopy(case.graph)
        extension.nodes["plain_child"] = {"raw"}
        after = owl.reason(self.cfg, extension, JAVA)
        self.assertTrue(after.consistent)
        self.assertEqual(after.members[defined.name], {"typed_child", "plain_child"})


if __name__ == "__main__":
    unittest.main()
