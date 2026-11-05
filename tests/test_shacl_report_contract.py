"""Live exported-row reports reject malformed evidence before projecting keys."""
import copy
from decimal import Decimal
import importlib.util
from types import SimpleNamespace
import unittest
from unittest import mock

import pgfixture
from semantic_crosscheck import config, naming, shacl_check, shapes

HAVE_SHACL = all(importlib.util.find_spec(name) for name in ('rdflib', 'pyshacl'))
PRIVATE = 'private-engine-value'


@unittest.skipUnless(HAVE_SHACL, 'needs pinned pySHACL and rdflib')
class LiveReportContractTest(unittest.TestCase):
    def setUp(self):
        import pyshacl
        import rdflib
        self.rdf, self.engine = rdflib, pyshacl
        self.sh = rdflib.Namespace(shapes.SH)
        self.cfg = config.load(pgfixture.DEMO_CONFIG)
        self.cfg.entities['item'] = config.Entity('item', 'sxc_synthetic.item', ['id'])
        self.shape = config.Shape(id='qty_nonnegative', target='item', path='qty', min_inclusive=Decimal(0))
        self.cfg.shapes = [self.shape]
        self.data = shacl_check.build_graph(self.cfg, [self.shape], {'item': [['one', Decimal(-1)]]}, {})
        self.graph = shacl_check.shapes_graph(shapes.shapes_ttl(self.cfg)[0])
        self.api, self.report, _ = pyshacl.validate(self.data, shacl_graph=self.graph, inference='none', allow_warnings=True)
        self.root = next(self.report.subjects(rdflib.RDF.type, self.sh.ValidationReport))
        self.result = next(self.report.objects(self.root, self.sh.result))

    def rejected(self, graph, api=False):
        with mock.patch.object(self.engine, 'validate', return_value=(api, graph, PRIVATE)):
            self.rejected_action(lambda: shacl_check.validate(self.data, self.graph))

    def rejected_action(self, action):
        with self.assertRaises(Exception) as raised:
            action()
        self.assertIsInstance(raised.exception, shacl_check.ValidationError)
        self.assertNotIn(PRIVATE, str(raised.exception))

    def test_engine_return_shape_and_boolean_are_checked(self):
        for returned in ((False, self.report), (False, self.report, '', PRIVATE),
                         ('false', self.report, ''), (0, self.report, ''),
                         (False, PRIVATE, '')):
            with self.subTest(value_type=type(returned[0]).__name__), \
                    mock.patch.object(self.engine, 'validate', return_value=returned):
                self.rejected_action(lambda: shacl_check.validate(self.data, self.graph))

    def test_missing_multiple_or_literal_report_root_is_rejected(self):
        empty = self.rdf.Graph()
        multiple = copy.deepcopy(self.report)
        multiple.add((self.rdf.BNode(), self.rdf.RDF.type, self.sh.ValidationReport))
        literal_root = self.rdf.Graph()
        literal_root.add((self.rdf.Literal(PRIVATE), self.rdf.RDF.type, self.sh.ValidationReport))
        literal_root.add((self.rdf.Literal(PRIVATE), self.sh.conforms, self.rdf.Literal(True)))
        for graph in (empty, multiple, literal_root):
            with self.subTest(triples=len(graph)):
                self.rejected(graph)

    def test_conformance_requires_one_typed_boolean(self):
        for values in ([], [self.rdf.Literal('false')], [self.rdf.Literal(0)],
                       [self.rdf.Literal(True), self.rdf.Literal(False)]):
            graph = copy.deepcopy(self.report)
            graph.remove((self.root, self.sh.conforms, None))
            for value in values:
                graph.add((self.root, self.sh.conforms, value))
            with self.subTest(values=values):
                self.rejected(graph)

    def test_api_and_rdf_verdict_cannot_hide_violation_results(self):
        graph = copy.deepcopy(self.report)
        graph.set((self.root, self.sh.conforms, self.rdf.Literal(True)))
        self.rejected(graph, api=True)
        self.rejected(self.report, api=True)

    def test_empty_report_requires_conformance(self):
        graph = self.rdf.Graph()
        graph.add((self.root, self.rdf.RDF.type, self.sh.ValidationReport))
        graph.add((self.root, self.sh.conforms, self.rdf.Literal(False)))
        self.rejected(graph)

    def test_results_must_be_typed_and_linked_to_the_report(self):
        for triple in ((self.root, self.sh.result, self.result),
                       (self.result, self.rdf.RDF.type, self.sh.ValidationResult)):
            graph = copy.deepcopy(self.report)
            graph.remove(triple)
            with self.subTest(predicate=triple[1]):
                self.rejected(graph)
        graph = copy.deepcopy(self.report)
        graph.add((self.rdf.BNode(), self.rdf.RDF.type, self.sh.ValidationResult))
        self.rejected(graph)

    def test_required_result_fields_have_one_usable_term(self):
        fields = (self.sh.focusNode, self.sh.sourceShape, self.sh.sourceConstraintComponent, self.sh.resultSeverity)
        for field in fields:
            for operation in ('missing', 'literal', 'duplicate'):
                graph = copy.deepcopy(self.report)
                if operation != 'duplicate':
                    graph.remove((self.result, field, None))
                if operation != 'missing':
                    graph.add((self.result, field, self.rdf.Literal(PRIVATE)))
                with self.subTest(field=field, operation=operation):
                    self.rejected(graph)

    def test_source_focus_severity_and_property_path_must_belong_to_the_input(self):
        changes = ((self.sh.sourceShape, self.rdf.URIRef('urn:' + PRIVATE)),
                   (self.sh.focusNode, self.rdf.URIRef('urn:' + PRIVATE)),
                   (self.sh.resultSeverity, self.rdf.URIRef('urn:' + PRIVATE)),
                   (self.sh.resultPath, self.rdf.URIRef('urn:' + PRIVATE)))
        for field, value in changes:
            graph = copy.deepcopy(self.report)
            graph.set((self.result, field, value))
            with self.subTest(field=field):
                self.rejected(graph)
        graph = copy.deepcopy(self.report)
        graph.remove((self.result, self.sh.resultPath, None))
        self.rejected(graph)
        graph = copy.deepcopy(self.report)
        graph.add((self.result, self.sh.resultPath, self.rdf.URIRef('urn:' + PRIVATE)))
        self.rejected(graph)

    def test_node_shape_cannot_replace_the_projectable_property_source(self):
        graph = copy.deepcopy(self.report)
        graph.set((self.result, self.sh.sourceShape,
                   self.rdf.URIRef(naming.rdf_node_shape(self.cfg, self.shape.id))))
        self.rejected(graph)

    def test_pair_node_shape_cannot_replace_the_projectable_property_source(self):
        pair = config.Shape(id='pair_gt', target='item', sql_violation='t.a > t.b')
        data = shacl_check.build_pair_graph(self.cfg, [pair], {'item': [['one', 2, 1]]})
        shape_graph = shacl_check.shapes_graph(shapes.pair_shapes_ttl(self.cfg, [pair]))
        api, graph, _ = self.engine.validate(data, shacl_graph=shape_graph, inference='none', allow_warnings=True)
        result = next(graph.subjects(self.rdf.RDF.type, self.sh.ValidationResult))
        graph.set((result, self.sh.sourceShape, self.rdf.URIRef(naming.rdf_node_shape(self.cfg, pair.id))))
        with mock.patch.object(self.engine, 'validate', return_value=(api, graph, PRIVATE)):
            self.rejected_action(lambda: shacl_check.validate(data, shape_graph))

    def test_projection_rejects_invalid_decoded_row_keys_without_leaking_values(self):
        source = self.rdf.URIRef(naming.rdf_property_shape(self.cfg, self.shape.id))
        for suffix in ('not-json-' + PRIVATE, '%7B%22' + PRIVATE + '%22%3A1%7D',
                       '%5B%5B%22' + PRIVATE + '%22%5D%5D', '%5B%5D'):
            graph = copy.deepcopy(self.report)
            graph.set((self.result, self.sh.sourceShape, source))
            prefix = naming.rdf_node(self.cfg, 'item', []).rsplit('/', 1)[0] + '/'
            graph.set((self.result, self.sh.focusNode, self.rdf.URIRef(prefix + suffix)))
            with self.subTest(suffix=suffix):
                self.rejected_action(lambda: shacl_check.focus_keys(self.cfg, [self.shape], graph))

    def test_real_conforming_and_violating_reports_keep_exact_focus_keys(self):
        for qty, expected in ((Decimal(0), set()), (Decimal(-1), {('one',)})):
            data = shacl_check.build_graph(self.cfg, [self.shape], {'item': [['one', qty]]}, {})
            got = shacl_check.validate(data, self.graph)
            self.assertEqual(shacl_check.focus_keys(self.cfg, [self.shape], got), {self.shape.id: expected})

    def test_real_warning_and_info_results_survive_allowed_conformance(self):
        source = self.rdf.URIRef(naming.rdf_property_shape(self.cfg, self.shape.id))
        for severity in (self.sh.Warning, self.sh.Info):
            graph = copy.deepcopy(self.graph)
            graph.set((source, self.sh.severity, severity))
            got = shacl_check.validate(self.data, graph)
            self.assertEqual(shacl_check.focus_keys(self.cfg, [self.shape], got), {self.shape.id: {('one',)}})
            self.assertTrue(list(got.objects(None, self.sh.result)))

    def test_allowed_warning_api_can_coexist_with_a_strict_rdf_nonconformance_flag(self):
        graph = copy.deepcopy(self.report)
        graph.set((self.result, self.sh.resultSeverity, self.sh.Warning))
        with mock.patch.object(self.engine, 'validate', return_value=(True, graph, '')):
            got = shacl_check.validate(self.data, self.graph)
        self.assertEqual(shacl_check.focus_keys(self.cfg, [self.shape], got), {self.shape.id: {('one',)}})

    def test_real_results_of_other_planned_groups_are_ignored_after_validation(self):
        other = config.Shape(id='qty_is_part', target='item', path='qty', class_='part')
        self.cfg.shapes.append(other)
        graph = shacl_check.shapes_graph(shapes.shapes_ttl(self.cfg)[0])
        got = shacl_check.validate(self.data, graph)
        sources = set(got.objects(None, self.sh.sourceShape))
        self.assertIn(self.rdf.URIRef(naming.rdf_property_shape(self.cfg, other.id)), sources)
        self.assertEqual(shacl_check.focus_keys(self.cfg, [self.shape], got), {self.shape.id: {('one',)}})

    def test_malformed_engine_report_is_isolated_to_its_value_group(self):
        other = config.Shape(id='qty_is_part', target='item', path='qty', class_='part')
        self.cfg.shapes.append(other)
        conn = SimpleNamespace(run=lambda *_a, **_k: None)
        fetched = {'main': ({'item': [['one', Decimal(-1)]]}, {}),
                   'extra:1': ({'item': [['one', 'P1']]}, {'part': ['P1']})}
        original = self.engine.validate
        calls = []
        def engine(data, **kwargs):
            calls.append(None)
            return original(data, **kwargs) if len(calls) == 1 else (False, self.rdf.Graph(), PRIVATE)
        with mock.patch.object(shacl_check, '_limited_fetch', return_value=(fetched, {'main': 1, 'extra:1': 2})), \
                mock.patch.object(shacl_check, 'sql_keys', return_value=set()), \
                mock.patch.object(self.engine, 'validate', side_effect=engine):
            got = shacl_check.run(conn, self.cfg, shapes.shapes_ttl(self.cfg)[0])
        self.assertEqual(got[self.shape.id]['shacl_keys'], {('one',)})
        self.assertNotIn('validation_error', got[self.shape.id])
        self.assertIsNone(got[other.id]['shacl_keys'])
        self.assertIn('validation_error', got[other.id])
        self.assertNotIn(PRIVATE, repr(got))

    def test_real_property_pair_report_keeps_warning_focus_rows(self):
        pair = config.Shape(id='pair_gt', target='item', sql_violation='t.a > t.b', severity='warning')
        graph = shacl_check.build_pair_graph(self.cfg, [pair], {'item': [['one', 2, 1]]})
        report = shacl_check.validate(graph, shapes.pair_shapes_ttl(self.cfg, [pair]))
        self.assertEqual(shacl_check.pair_focus_keys(self.cfg, [pair], report), {pair.id: {('one',)}})

    def test_malformed_property_pair_report_is_a_sanitized_validation_error(self):
        pair = config.Shape(id='pair_gt', target='item', sql_violation='t.a > t.b')
        conn = SimpleNamespace(run=lambda *_a, **_k: [['one', 2, 1]])
        with mock.patch.object(shacl_check, 'sql_keys', return_value=set()), \
                mock.patch.object(shacl_check, '_column_type', return_value=('integer', True, False)), \
                mock.patch.object(shacl_check, '_duplicate_key_rows', return_value=0), \
                mock.patch.object(shacl_check.db, 'scalar', return_value=1), \
                mock.patch.object(self.engine, 'validate', return_value=(False, self.rdf.Graph(), PRIVATE)):
            got = shacl_check.run_pairs(conn, self.cfg, [pair], shapes.pair_shapes_ttl(self.cfg, [pair]))
        self.assertIn('validation_error', got[pair.id])
        self.assertNotIn('shacl_keys', got[pair.id])
        self.assertNotIn(PRIVATE, repr(got))
