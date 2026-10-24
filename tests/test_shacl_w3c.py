"""The opt-in W3C SHACL core subset: third-party expected reports, a fixed profile and a pinned suite.

Every manifest and test file below is original synthetic Turtle in the W3C suite's format. No file of the
W3C data-shapes repository is copied here; the actual suite is read only by the gated integration test.
"""
import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck.result import StageError

SH = 'http://www.w3.org/ns/shacl#'
EX = 'https://example.org/sxc-w3c#'
PREFIXES = '''@prefix ex: <https://example.org/sxc-w3c#> .
@prefix mf: <http://www.w3.org/2001/sw/DataAccess/tests/test-manifest#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix sht: <http://www.w3.org/ns/shacl-test#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
'''
CORE = ('data-shapes-test-suite', 'tests', 'core')
# An in-profile synthetic case: sxc's exporter shape (targetClass, sh:property, simple path, numeric bound).
QTY = '''
ex:bom_line_1 rdf:type ex:BomLine ; ex:qty_per 0 .
ex:bom_line_2 rdf:type ex:BomLine ; ex:qty_per 2 .
ex:bom_line_3 rdf:type ex:BomLine .
ex:BomLineShape rdf:type sh:NodeShape ; sh:targetClass ex:BomLine ; sh:property ex:BomLineShape-qty .
ex:BomLineShape-qty sh:path ex:qty_per ; sh:minExclusive 0 .
'''
QTY_RESULT = ('sh:focusNode ex:bom_line_1 ; sh:resultPath ex:qty_per ; sh:resultSeverity sh:Violation ; '
              'sh:sourceConstraintComponent sh:MinExclusiveConstraintComponent ; sh:sourceShape ex:BomLineShape-qty ; '
              'sh:value 0')
# A node-level class case: node shape results carry no sh:resultPath.
PART = '''
ex:part_1 rdf:type ex:Part .
ex:PartShape rdf:type sh:NodeShape ; sh:class ex:Part ; sh:targetNode ex:part_1 , ex:part_9 .
'''
PART_RESULT = ('rdf:type sh:ValidationResult ; sh:focusNode ex:part_9 ; sh:resultSeverity sh:Violation ; '
               'sh:sourceConstraintComponent sh:ClassConstraintComponent ; sh:value ex:part_9')
# Explicit severities, as shapes_ttl() writes them.
WARNING_QTY = QTY.replace('sh:minExclusive 0 .', 'sh:minExclusive 0 ; sh:severity sh:Warning .')
WARNING_QTY_RESULT = QTY_RESULT.replace('sh:Violation', 'sh:Warning')
VIOLATION_PART = PART.replace('sh:class ex:Part ;', 'sh:class ex:Part ; sh:severity sh:Violation ;')


def test_file(name, body, results=(), conforms=None, status='sht:approved', kind='sht:Validate',
              action='sht:dataGraph <> ; sht:shapesGraph <>'):
    conforms = (not results) if conforms is None else conforms
    report = 'rdf:type sh:ValidationReport ; sh:conforms "%s"^^xsd:boolean' % str(conforms).lower()
    report += ''.join(' ; sh:result [ %s ]' % r for r in results)
    status_line = ('  mf:status %s ;\n' % status) if status else ''
    return (PREFIXES + body + '\n<> rdf:type mf:Manifest ; mf:entries ( <%s> ) .\n<%s> rdf:type %s ;\n'
            '  mf:action [ %s ] ;\n  mf:result [ %s ] ;\n%s.\n' % (name, name, kind, action, report, status_line))


def manifest(*includes):
    return (PREFIXES + '<> rdf:type mf:Manifest ;\n'
            + ''.join('  mf:include <%s> ;\n' % i for i in includes) + '.\n')


def write_suite(root, files, commit=None):
    """files: {path relative to tests/core: text}. Returns the suite root."""
    base = Path(root).joinpath(*CORE)
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    if commit is not None:
        (Path(root) / 'PINNED_COMMIT').write_text(commit + '\n', encoding='utf-8')
    return Path(root)


@unittest.skipUnless(importlib.util.find_spec('rdflib') and importlib.util.find_spec('pyshacl'),
                     'optional SHACL packages require requirements/reason.txt')
class W3CSuiteTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('semantic_crosscheck.shacl_corpus')
        self.assertTrue(hasattr(self.m, 'load_w3c_suite'), 'the W3C suite mode must be implemented')
        self.temp = tempfile.TemporaryDirectory(prefix='sxc w3c ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def suite(self, files, commit=None):
        return self.m.load_w3c_suite(write_suite(self.root, files, commit))

    def reasons(self, suite):
        return {case.id: case.reason for case in suite.cases}

    # ---- manifests -----------------------------------------------------------------------------------
    def test_manifest_includes_recurse_once_and_entry_type_and_status_decide_what_runs(self):
        files = {
            'manifest.ttl': manifest('a/manifest.ttl', 'b/manifest.ttl', 'a/manifest.ttl'),
            'a/manifest.ttl': manifest('qty-001.ttl', 'manifest.ttl', 'proposed-001.ttl'),
            'a/qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT]),
            'a/proposed-001.ttl': test_file('proposed-001', QTY, [QTY_RESULT], status='sht:proposed'),
            'b/manifest.ttl': manifest('nostatus-001.ttl', 'other-001.ttl', 'nested/manifest.ttl'),
            'b/nostatus-001.ttl': test_file('nostatus-001', QTY, [QTY_RESULT], status=None),
            'b/other-001.ttl': test_file('other-001', QTY, [QTY_RESULT], kind='sht:ValidateWithInference'),
            'b/nested/manifest.ttl': manifest('part-001.ttl'),
            'b/nested/part-001.ttl': test_file('part-001', PART, [PART_RESULT]),
        }
        suite = self.suite(files)
        self.assertEqual(self.reasons(suite), {
            'core/a/qty-001': '', 'core/a/proposed-001': 'status_not_approved',
            'core/b/nostatus-001': 'status_not_approved', 'core/b/other-001': 'unsupported_entry_type',
            'core/b/nested/part-001': ''})
        # Every manifest and test file read is part of the digest, sorted by its suite-relative path.
        self.assertEqual(sorted(suite.files), sorted('data-shapes-test-suite/tests/core/' + r for r in files))

    def test_unreadable_or_escaping_manifests_are_configuration_errors_without_paths(self):
        cases = {
            'missing include': {'manifest.ttl': manifest('absent/manifest.ttl')},
            'outside include': {'manifest.ttl': manifest('../../../outside.ttl')},
            'remote include': {'manifest.ttl': manifest('http://example.org/manifest.ttl')},
            'not turtle': {'manifest.ttl': PREFIXES + '<> rdf:type mf:Manifest ; mf:include <x> ; private-marker'},
            'not a manifest': {'manifest.ttl': manifest('a.ttl'), 'a.ttl': PREFIXES + 'ex:a ex:b ex:c .\n'},
        }
        for label, files in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory(prefix='sxc w3c bad ') as other:
                with self.assertRaises(StageError) as caught:
                    self.m.load_w3c_suite(write_suite(other, files))
                self.assertEqual(caught.exception.exit_code, 2)
                self.assertNotIn(other, str(caught.exception))
                self.assertNotIn('private-marker', str(caught.exception))
        with self.assertRaises(StageError) as caught:
            self.m.load_w3c_suite(self.root)
        self.assertEqual(caught.exception.exit_code, 3)

    def test_an_entry_listed_twice_is_not_run_instead_of_silently_dropped(self):
        # b/manifest.ttl lists a's entry again without its description; neither listing may win silently.
        files = {'manifest.ttl': manifest('a/manifest.ttl', 'b/manifest.ttl'),
                 'a/manifest.ttl': manifest('qty-001.ttl'),
                 'a/qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT]),
                 'b/manifest.ttl': PREFIXES + '<> rdf:type mf:Manifest ; mf:entries ( <../a/qty-001> ) .\n'}
        suite = self.suite(files)
        self.assertEqual(self.reasons(suite), {'core/a/qty-001': 'duplicate_entry'})
        self.assertIsNone(suite.cases[0].expected)

    # ---- profile -------------------------------------------------------------------------------------
    def test_every_feature_outside_the_profile_is_not_run_with_its_reason(self):
        cases = {
            'datatype': ('ex:S rdf:type sh:NodeShape ; sh:targetNode ex:part_1 ; sh:datatype xsd:integer .',
                         'unsupported_component'),
            'mincount': ('ex:S sh:targetNode ex:part_1 ; sh:property [ sh:path ex:qty_per ; sh:minCount 1 ] .',
                         'unsupported_component'),
            'maxexclusive': ('ex:S sh:targetNode ex:part_1 ; sh:maxExclusive 4 .', 'unsupported_component'),
            'deactivated': ('ex:S sh:targetNode ex:part_1 ; sh:class ex:Part ; sh:deactivated true .',
                            'unsupported_component'),
            'nested': ('ex:S sh:targetClass ex:Part ; sh:property [ sh:path ex:child ; '
                       'sh:property [ sh:path ex:qty_per ; sh:minExclusive 0 ] ] .', 'unsupported_component'),
            'oneormore': ('ex:S sh:targetNode ex:part_1 ; sh:property [ sh:path [ sh:oneOrMorePath ex:child ] ; '
                          'sh:class ex:Part ] .', 'complex_path'),
            'inverse': ('ex:S sh:targetNode ex:part_1 ; sh:property [ sh:path [ sh:inversePath ex:child ] ; '
                        'sh:class ex:Part ] .', 'complex_path'),
            # A path node no shape uses is still recursive SHACL vocabulary the profile does not cover.
            'unusedpath': ('ex:S sh:targetNode ex:part_1 ; sh:class ex:Part .\n_:p sh:zeroOrMorePath ( _:p ) .',
                           'complex_path'),
            'imports': ('<> <http://www.w3.org/2002/07/owl#imports> <https://example.org/remote.ttl> .\n'
                        'ex:S sh:targetNode ex:part_1 ; sh:class ex:Part .', 'unsupported_component'),
            'sparql': ('ex:S sh:targetNode ex:part_1 ; sh:sparql [ sh:select "SELECT $this WHERE { }" ] .', 'sparql'),
            'closed': ('ex:S sh:targetNode ex:part_1 ; sh:closed true ; sh:property [ sh:path ex:qty_per ] .',
                       'closed_shape'),
            'subjectsof': ('ex:S sh:targetSubjectsOf ex:qty_per ; sh:class ex:Part .', 'unsupported_target'),
            'owlclass': ('ex:S rdf:type <http://www.w3.org/2002/07/owl#Class> , sh:NodeShape ; sh:class ex:Part .',
                         'unsupported_target'),
            'literalfocus': ('ex:S sh:targetNode 7 ; sh:minInclusive 8 .', 'non_iri_focus'),
            'datebound': ('ex:S sh:targetNode ex:part_1 ; sh:property [ sh:path ex:due ; '
                          'sh:minInclusive "2026-01-01"^^xsd:date ] .', 'unsupported_value'),
            'severity': ('ex:S sh:targetNode ex:part_1 ; sh:class ex:Part ; sh:severity sh:Info .', 'unsupported_value'),
            'classliteral': ('ex:S sh:targetNode ex:part_1 ; sh:class "Part" .', 'unsupported_value'),
            # SHACL leaves several severities on one shape undefined; sxc emits exactly one.
            'severities': ('ex:S sh:targetNode ex:part_1 ; sh:class ex:Part ; sh:severity sh:Violation , sh:Warning .',
                           'unsupported_value'),
            # Guards no path keyword or other check would catch: each case is otherwise inside the profile.
            'sequence': ('ex:S sh:targetNode ex:part_1 ; sh:property [ sh:path ( ex:child ex:qty_per ) ; '
                         'sh:class ex:Part ] .', 'complex_path'),
            'twopaths': ('ex:S sh:targetNode ex:part_1 ; sh:property [ sh:path ex:child , ex:qty_per ; '
                         'sh:class ex:Part ] .', 'unsupported_value'),
            'pathless': ('ex:S sh:targetNode ex:part_1 ; sh:property ex:P .\nex:P sh:class ex:Part .',
                         'unsupported_value'),
            'literalshape': ('ex:S sh:targetNode ex:part_1 ; sh:property "x" .', 'unsupported_value'),
            'blankfocus': ('ex:S sh:targetNode _:n ; sh:class ex:Part .', 'non_iri_focus'),
            'blankclass': ('ex:S sh:targetClass _:c ; sh:class ex:Part .', 'unsupported_value'),
        }
        files = {'manifest.ttl': manifest(*[name + '.ttl' for name in cases])}
        for name, (body, _reason) in cases.items():
            files[name + '.ttl'] = test_file(name, body)
        suite = self.suite(files)
        reasons = self.reasons(suite)
        self.assertEqual(set(reasons), {'core/' + name for name in cases})
        for name, (_body, reason) in cases.items():
            with self.subTest(reason=name):
                self.assertEqual(reasons['core/' + name], reason)
        features = {case.id: case.features for case in suite.cases}
        self.assertIn('datatype', features['core/datatype'])
        self.assertIn('property', features['core/nested'])
        self.assertEqual(features['core/imports'], ('owl_imports',))
        for name, expected in {'severities': ('severity',), 'sequence': ('path',), 'twopaths': ('path',),
                               'pathless': ('property',), 'literalshape': ('property', 'shape'),
                               'blankfocus': ('targetNode',), 'blankclass': ('targetClass',)}.items():
            with self.subTest(features=name):
                self.assertEqual(features['core/' + name], expected)
        for case in suite.cases:
            with self.subTest(case=case.id):
                self.assertIsNone(case.expected)

    def test_one_violation_or_warning_severity_stays_inside_the_profile(self):
        # shapes_ttl() writes one sh:severity on every shape, so an explicit Violation or Warning is in the profile.
        files = {'manifest.ttl': manifest('warning-001.ttl', 'violation-001.ttl'),
                 'warning-001.ttl': test_file('warning-001', WARNING_QTY, [WARNING_QTY_RESULT]),
                 'violation-001.ttl': test_file('violation-001', VIOLATION_PART, [PART_RESULT])}
        self.assertEqual(self.reasons(self.suite(files)), {'core/warning-001': '', 'core/violation-001': ''})

    def test_graph_files_and_expected_terms_the_comparison_cannot_identify_are_not_run(self):
        blank_focus = ('rdf:type sh:ValidationResult ; sh:focusNode _:b1 ; sh:resultSeverity sh:Violation ; '
                       'sh:sourceConstraintComponent sh:ClassConstraintComponent ; sh:value _:b1')
        literal_focus = ('sh:focusNode "text" ; sh:resultSeverity sh:Violation ; '
                         'sh:sourceConstraintComponent sh:ClassConstraintComponent ; sh:value "text"')
        relative = ('sh:focusNode <local-node> ; sh:resultSeverity sh:Violation ; '
                    'sh:sourceConstraintComponent sh:ClassConstraintComponent ; sh:value <local-node>')
        missing_focus = 'sh:resultSeverity sh:Violation ; sh:sourceConstraintComponent sh:ClassConstraintComponent'
        # Document-relative references that resolve outside the base directory, or sit in a datatype or component.
        root_relative = ('sh:focusNode </part_9> ; sh:resultSeverity sh:Violation ; '
                         'sh:sourceConstraintComponent sh:ClassConstraintComponent ; sh:value ex:part_9')
        relative_type = ('sh:focusNode ex:bom_line_1 ; sh:resultPath ex:qty_per ; sh:resultSeverity sh:Violation ; '
                         'sh:sourceConstraintComponent sh:MinExclusiveConstraintComponent ; sh:value "0"^^<local-type>')
        relative_component = ('sh:focusNode ex:part_9 ; sh:resultSeverity sh:Violation ; '
                              'sh:sourceConstraintComponent <LocalComponent> ; sh:value ex:part_9')
        cases = {
            'jsonld': ({}, test_file('jsonld', PART, [PART_RESULT],
                                     action='sht:dataGraph <jsonld-data.jsonld> ; sht:shapesGraph <>'), 'non_turtle'),
            'outside': ({}, test_file('outside', PART, [PART_RESULT],
                                      action='sht:dataGraph <../../../outside.ttl> ; sht:shapesGraph <>'),
                        'outside_suite'),
            'remote': ({}, test_file('remote', PART, [PART_RESULT],
                                     action='sht:dataGraph <http://example.org/data.ttl> ; sht:shapesGraph <>'),
                       'outside_suite'),
            'absent': ({}, test_file('absent', PART, [PART_RESULT],
                                     action='sht:dataGraph <absent-data.ttl> ; sht:shapesGraph <>'), 'missing_graph'),
            'noshapes': ({}, test_file('noshapes', PART, [PART_RESULT], action='sht:dataGraph <>'), 'missing_graph'),
            'blank': ({}, test_file('blank', PART + '_:b1 rdf:type ex:Other .\n'
                                    'ex:PartShape sh:targetClass ex:Other .\n', [blank_focus]), 'blank_node_term'),
            'literal': ({}, test_file('literal', PART, [literal_focus]), 'non_iri_focus'),
            'relative': ({}, test_file('relative', PART, [relative]), 'relative_iri'),
            'rootrelative': ({}, test_file('rootrelative', PART, [root_relative]), 'relative_iri'),
            'relativetype': ({}, test_file('relativetype', QTY, [relative_type]), 'relative_iri'),
            'relativecomponent': ({}, test_file('relativecomponent', PART, [relative_component]), 'relative_iri'),
            'malformed': ({}, test_file('malformed', PART, [missing_focus]), 'malformed_expected'),
            'inconsistent': ({}, test_file('inconsistent', PART, [PART_RESULT], conforms=True), 'malformed_expected'),
        }
        files = {'manifest.ttl': manifest(*[name + '.ttl' for name in cases]),
                 'jsonld-data.jsonld': '{}'}
        for name, (_extra, text, _reason) in cases.items():
            files[name + '.ttl'] = text
        suite = self.suite(files)
        self.assertEqual(self.reasons(suite), {'core/' + name: reason for name, (_x, _t, reason) in cases.items()})
        self.assertNotIn('data-shapes-test-suite/tests/core/jsonld-data.jsonld', suite.files)

    def test_a_separate_data_graph_must_parse_and_must_not_import(self):
        separate = 'sht:dataGraph <%s> ; sht:shapesGraph <>'
        files = {'manifest.ttl': manifest('imports-001.ttl', 'broken-001.ttl', 'separate-001.ttl'),
                 'imports-001.ttl': test_file('imports-001', PART, [PART_RESULT], action=separate % 'imports-data.ttl'),
                 'imports-data.ttl': PREFIXES + '<> <http://www.w3.org/2002/07/owl#imports> '
                                               '<https://example.org/remote.ttl> .\nex:part_1 rdf:type ex:Part .\n',
                 'broken-001.ttl': test_file('broken-001', PART, [PART_RESULT], action=separate % 'broken-data.ttl'),
                 'broken-data.ttl': PREFIXES + 'ex:part_1 rdf:type ex:Part ; private-marker\n',
                 'separate-001.ttl': test_file('separate-001', PART, [PART_RESULT], action=separate % 'separate-data.ttl'),
                 'separate-data.ttl': PREFIXES + 'ex:part_1 rdf:type ex:Part .\n'}
        suite = self.suite(files)
        self.assertEqual(self.reasons(suite), {'core/imports-001': 'unsupported_component',
                                               'core/broken-001': 'malformed_graph', 'core/separate-001': ''})
        self.assertEqual({case.id: case.features for case in suite.cases}['core/imports-001'], ('owl_imports',))
        self.assertIn('data-shapes-test-suite/tests/core/separate-data.ttl', suite.files)

    # ---- expected reports ----------------------------------------------------------------------------
    def test_expected_report_keeps_duplicates_absent_paths_and_ignores_source_shape(self):
        part = ('sh:focusNode ex:part_9 ; sh:resultSeverity sh:Violation ; '
                'sh:sourceConstraintComponent sh:ClassConstraintComponent ; sh:value ex:part_9')
        files = {'manifest.ttl': manifest('part-001.ttl', 'qty-001.ttl', 'twice-001.ttl'),
                 'part-001.ttl': test_file('part-001', PART, [PART_RESULT]),
                 'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT]),
                 'twice-001.ttl': test_file('twice-001', PART.replace('sh:class ex:Part', 'sh:class ex:Part , ex:Item'),
                                            [part + ' ; sh:sourceShape ex:PartShape', part])}
        cases = {case.id: case for case in self.suite(files).cases}
        absent = ('absent',)
        self.assertEqual(cases['core/part-001'].expected, self.m.Report(False, (
            (('iri', EX + 'part_9'), absent, SH + 'ClassConstraintComponent', SH + 'Violation', ('iri', EX + 'part_9')),)))
        self.assertEqual(cases['core/qty-001'].expected.violations, (
            (('iri', EX + 'bom_line_1'), ('iri', EX + 'qty_per'), SH + 'MinExclusiveConstraintComponent',
             SH + 'Violation', ('number', '0')),))
        self.assertEqual(len(cases['core/twice-001'].expected.violations), 2)
        self.assertEqual(len(set(cases['core/twice-001'].expected.violations)), 1)

    def test_engine_report_comparison_is_strict_about_paths_values_and_blank_nodes(self):
        expected = self.suite({'manifest.ttl': manifest('qty-001.ttl'),
                               'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT])}).cases[0].expected
        good = ('@prefix sh: <%s> . @prefix ex: <%s> .\n[] a sh:ValidationReport ; sh:conforms false ; sh:result '
                '[ a sh:ValidationResult ; sh:focusNode ex:bom_line_1 ; sh:resultPath ex:qty_per ; '
                'sh:resultSeverity sh:Violation ; sh:sourceConstraintComponent sh:MinExclusiveConstraintComponent ; '
                'sh:sourceShape [] ; sh:resultMessage "any wording" ; sh:value 0.0 ] .' % (SH, EX))
        self.assertTrue(self.m.compare_reports(expected, self.m.parse_w3c_report(good))['matches'])
        for old, new in [('sh:resultPath ex:qty_per ; ', ''), ('ex:bom_line_1', '_:b7'),
                         ('sh:value 0.0', 'sh:value 1'), ('sh:value 0.0', 'sh:value "0"'),
                         ('sh:Violation', 'sh:Warning'), ('sh:value 0.0 ]', 'sh:value 0.0 ], [ a sh:ValidationResult ; '
                          'sh:focusNode ex:bom_line_1 ; sh:resultPath ex:qty_per ; sh:resultSeverity sh:Violation ; '
                          'sh:sourceConstraintComponent sh:MinExclusiveConstraintComponent ; sh:value 0 ]')]:
            with self.subTest(new=new[:40]):
                detail = self.m.compare_reports(expected, self.m.parse_w3c_report(good.replace(old, new)))
                self.assertFalse(detail['matches'])
        for bad in ['', good.replace('sh:conforms false', 'sh:conforms true'),
                    good.replace('sh:focusNode ex:bom_line_1 ;', ''), good.replace(' a sh:ValidationResult ;', ''),
                    good.replace('sh:value 0.0', 'sh:value 0.0, 1')]:
            with self.subTest(bad=bad[-50:]), self.assertRaises(self.m.CorpusError):
                self.m.parse_w3c_report(bad)

    # ---- pin -----------------------------------------------------------------------------------------
    def test_digest_covers_sorted_relative_paths_and_bytes(self):
        files = {'b/x.ttl': b'two', 'a/y.ttl': b'one'}
        expected = hashlib.sha256()
        for rel in ('a/y.ttl', 'b/x.ttl'):
            expected.update(rel.encode() + b'\0' + str(len(files[rel])).encode() + b'\0' + files[rel])
        self.assertEqual(self.m.suite_digest(files), expected.hexdigest())
        self.assertNotEqual(self.m.suite_digest({'b/x.ttl': b'twO', 'a/y.ttl': b'one'}), expected.hexdigest())
        self.assertNotEqual(self.m.suite_digest({'b/z.ttl': b'two', 'a/y.ttl': b'one'}), expected.hexdigest())
        self.assertNotEqual(self.m.suite_digest({'a/y.ttl': b'onetwo'}), self.m.suite_digest({'a/y.tt': b'lonetwo'}))
        self.assertRegex(self.m.W3C_SUITE_COMMIT, r'^[0-9a-f]{40}$')
        self.assertRegex(self.m.W3C_SUITE_SHA256, r'^[0-9a-f]{64}$')

    def run_mode(self, files, *, pinned, allow=False, commit=None, engine_report=None):
        root = write_suite(self.root, files, commit)
        digest = self.m.load_w3c_suite(root).sha256
        calls = []

        def engine(*args, **kwargs):
            calls.append(args)
            return engine_report

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.m, '_components', return_value=(self.root, sys.executable)))
            stack.enter_context(mock.patch.object(self.m, 'validate_pyshacl_files', side_effect=engine))
            stack.enter_context(mock.patch.object(self.m, 'validate_jena', side_effect=engine))
            if pinned:
                stack.enter_context(mock.patch.object(self.m, 'W3C_SUITE_SHA256', digest))
            args = argparse.Namespace(jena_home='', timeout_ms=30000, w3c_suite=str(root), allow_unpinned_suite=allow)
            result = self.m.run(None, args, self.root)
        return result, calls, digest

    def test_unpinned_suite_refuses_to_run_engines_unless_explicitly_allowed(self):
        files = {'manifest.ttl': manifest('qty-001.ttl', 'datatype-001.ttl'),
                 'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT]),
                 'datatype-001.ttl': test_file('datatype-001', 'ex:S sh:targetNode ex:a ; sh:datatype xsd:integer .')}
        # A commit label in the checkout is a claim, not a pin: only the digest names the pinned commit.
        result, calls, digest = self.run_mode(files, pinned=False, commit=self.m.W3C_SUITE_COMMIT)
        self.assertEqual(calls, [])
        self.assertEqual({c.status for c in result.checks}, {'not_run'})
        self.assertEqual({c.detail['reason'] for c in result.checks}, {'suite_not_pinned'})
        self.assertEqual(len(result.checks), 4)
        self.assertEqual(result.inputs['w3c_suite_sha256'], digest)
        self.assertFalse(result.inputs['w3c_suite_pinned'])
        self.assertEqual(result.inputs['w3c_suite_commit'], '')
        expected = next(c for c in self.m.load_w3c_suite(self.root).cases if c.id == 'core/qty-001').expected
        result, calls, _digest = self.run_mode(files, pinned=False, allow=True, engine_report=expected)
        self.assertEqual(len(calls), 2)
        self.assertEqual({c.id: c.status for c in result.checks}, {
            'w3c:pyshacl:core/qty-001': 'pass', 'w3c:jena:core/qty-001': 'pass',
            'w3c:pyshacl:core/datatype-001': 'not_run', 'w3c:jena:core/datatype-001': 'not_run',
            'w3c:suite': 'not_run'})
        self.assertEqual(result.status, 'not_run')
        self.assertTrue(any('not verified' in line for line in result.not_proven))

    def test_an_allowed_unpinned_suite_cannot_pass_even_when_every_entry_is_inside_the_profile(self):
        files = {'manifest.ttl': manifest('qty-001.ttl'), 'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT])}
        expected = self.suite(files).cases[0].expected
        result, calls, _digest = self.run_mode(files, pinned=False, allow=True, engine_report=expected)
        self.assertEqual(len(calls), 2)
        self.assertEqual({c.id: c.status for c in result.checks}, {
            'w3c:pyshacl:core/qty-001': 'pass', 'w3c:jena:core/qty-001': 'pass', 'w3c:suite': 'not_run'})
        self.assertEqual(next(c for c in result.checks if c.id == 'w3c:suite').detail, {'reason': 'suite_not_pinned'})
        self.assertEqual((result.status, result.exit_code()), ('not_run', 1))
        # The same suite under the pin has no suite check, so its passes are the result.
        result, _calls, _digest = self.run_mode(files, pinned=True, engine_report=expected)
        self.assertEqual((result.status, [c.id for c in result.checks if c.id == 'w3c:suite']), ('pass', []))

    def test_pinned_suite_runs_only_profile_cases_and_records_ids_and_hashes_only(self):
        files = {'manifest.ttl': manifest('qty-001.ttl', 'part-001.ttl'),
                 'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT]),
                 'part-001.ttl': test_file('part-001', PART, [PART_RESULT])}
        empty = self.m.Report(True, ())
        result, calls, digest = self.run_mode(files, pinned=True, commit='a' * 40, engine_report=empty)
        self.assertEqual(len(calls), 4)
        for args in calls:
            # Engines read private copies of the digested bytes, one file for data and shapes as the manifest says.
            self.assertEqual(Path(args[-3]), Path(args[-2]))
            self.assertNotIn(self.temp.name, str(args[-2]))
        self.assertEqual({c.status for c in result.checks}, {'fail'})
        # The digest decides the commit; the checkout's own label ('a' * 40 here) is not recorded.
        self.assertEqual(result.inputs, {'w3c_suite_commit': self.m.W3C_SUITE_COMMIT, 'w3c_suite_sha256': digest,
                                         'w3c_suite_files': 3, 'w3c_suite_pinned': True})
        text = json.dumps(result.to_dict())
        for private in (self.temp.name, 'example.org', 'bom_line_1', 'part_9'):
            self.assertNotIn(private, text)
        self.assertEqual(result.checks[0].detail['missing_constraints'], 1)
        # The mode validates the suite's own graphs: the exporter is untested and a pass is not full compliance.
        scope = ' '.join(result.not_proven)
        for words in ("sxc's exporter", 'fixed corpus', 'sh:sourceShape', 'full compliance'):
            self.assertIn(words, scope)

    def test_actual_pyshacl_matches_synthetic_expected_reports(self):
        files = {'manifest.ttl': manifest('qty-001.ttl', 'part-001.ttl', 'warning-001.ttl'),
                 'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT]),
                 'part-001.ttl': test_file('part-001', PART, [PART_RESULT]),
                 'warning-001.ttl': test_file('warning-001', WARNING_QTY, [WARNING_QTY_RESULT])}
        suite = self.suite(files)
        self.assertEqual({case.reason for case in suite.cases}, {''})
        with tempfile.TemporaryDirectory() as temp:
            for case in suite.cases:
                with self.subTest(case=case.id):
                    path = Path(temp) / (case.id.replace('/', '_') + '.ttl')
                    path.write_bytes(suite.files[case.shapes])
                    actual = self.m.validate_pyshacl_files(path, path, 30000, self.m.parse_w3c_report)
                    self.assertTrue(self.m.compare_reports(case.expected, actual)['matches'])

    # ---- the fixed corpus stays the default ------------------------------------------------------------
    def test_fixed_corpus_is_unchanged_without_the_option_even_when_the_variable_is_set(self):
        report = self.m.parse_report('@prefix sh: <%s> . [] a sh:ValidationReport; sh:conforms true .' % SH)
        with mock.patch.dict(os.environ, {'SXC_W3C_SHACL_SUITE': str(self.root)}), \
                mock.patch.object(self.m, '_components', return_value=(self.root, sys.executable)), \
                mock.patch.object(self.m, 'validate_pyshacl', return_value=report), \
                mock.patch.object(self.m, 'validate_jena', return_value=report), \
                mock.patch.object(self.m, 'load_w3c_suite', side_effect=AssertionError('W3C mode must be opt-in')):
            result = self.m.run(None, argparse.Namespace(jena_home='', timeout_ms=30000), self.root)
            again = self.m.run(None, argparse.Namespace(jena_home='', timeout_ms=30000, w3c_suite=None,
                                                         allow_unpinned_suite=False), self.root)
        for res in (result, again):
            self.assertEqual(len(res.checks), 16)
            self.assertTrue(all(c.id.startswith(('pyshacl:', 'jena:')) for c in res.checks))
            self.assertEqual(res.inputs, {})

    def test_option_errors_and_the_variable_default_are_sanitized_stage_errors(self):
        import sxc
        args = sxc.parser().parse_args(['shacl-corpus', '--w3c-suite'])
        self.assertEqual((args.w3c_suite, args.allow_unpinned_suite), ('', False))
        self.assertIsNone(sxc.parser().parse_args(['shacl-corpus']).w3c_suite)
        self.assertEqual(sxc.parser().parse_args(['shacl-corpus', '--w3c-suite', 'dir',
                                                  '--allow-unpinned-suite']).w3c_suite, 'dir')
        with mock.patch.object(self.m, '_components', return_value=(self.root, sys.executable)):
            with self.assertRaises(StageError) as caught:
                self.m.run(None, argparse.Namespace(jena_home='', timeout_ms=30000, w3c_suite=None,
                                                    allow_unpinned_suite=True), self.root)
            self.assertEqual(caught.exception.exit_code, 2)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('SXC_W3C_SHACL_SUITE', None)
                with self.assertRaises(StageError) as caught:
                    self.m.run(None, argparse.Namespace(jena_home='', timeout_ms=30000, w3c_suite='',
                                                        allow_unpinned_suite=False), self.root)
                self.assertEqual(caught.exception.exit_code, 3)
            files = {'manifest.ttl': manifest('qty-001.ttl'), 'qty-001.ttl': test_file('qty-001', QTY, [QTY_RESULT])}
            write_suite(self.root, files)
            with mock.patch.dict(os.environ, {'SXC_W3C_SHACL_SUITE': str(self.root)}):
                result = self.m.run(None, argparse.Namespace(jena_home='', timeout_ms=30000, w3c_suite='',
                                                             allow_unpinned_suite=False), self.root)
            self.assertEqual([c.detail['reason'] for c in result.checks], ['suite_not_pinned'] * 2)


# Observed on 2026-10-24 with pySHACL 0.40.1, RDFLib 7.6.0 and Apache Jena 6.2.0 (Java 21) on the pinned suite:
# both engines match every in-profile expected report. A change in either direction (a new failure, a case
# entering or leaving the profile, a different number of expected results) fails this test.
OBSERVED = {
    'core/node/class-001': ('pass', 'pass'),
    'core/node/class-003': ('pass', 'pass'),
    'core/property/class-001': ('pass', 'pass'),
    'core/property/maxInclusive-001': ('pass', 'pass'),
    'core/property/minExclusive-001': ('pass', 'pass'),
    'core/property/minExclusive-002': ('pass', 'pass'),
}
OBSERVED_RESULTS = {'core/node/class-001': 2, 'core/node/class-003': 5, 'core/property/class-001': 2,
                    'core/property/maxInclusive-001': 2, 'core/property/minExclusive-001': 2,
                    'core/property/minExclusive-002': 2}
# Not-run checks by reason (two engines per entry): 92 of the 98 approved core entries are outside the profile.
OBSERVED_NOT_RUN = {'unsupported_component': 106, 'non_iri_focus': 36, 'complex_path': 28, 'unsupported_target': 8,
                    'closed_shape': 4, 'sparql': 2}


@unittest.skipUnless(importlib.util.find_spec('rdflib') and importlib.util.find_spec('pyshacl'),
                     'optional SHACL packages require requirements/reason.txt')
@unittest.skipUnless(os.environ.get('SXC_W3C_SHACL_SUITE') and os.environ.get('SXC_JENA_HOME')
                     and os.environ.get('SXC_JAVA'), 'the pinned W3C suite, Jena and Java require explicit environment')
class PinnedW3CSuiteTests(unittest.TestCase):
    def test_actual_engines_on_the_pinned_core_suite_match_the_observed_outcomes(self):
        m = importlib.import_module('semantic_crosscheck.shacl_corpus')
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(jena_home=os.environ['SXC_JENA_HOME'], timeout_ms=60000, w3c_suite='',
                                      allow_unpinned_suite=False)
            result = m.run(None, args, Path(temp))
        self.assertTrue(result.inputs['w3c_suite_pinned'], 'SXC_W3C_SHACL_SUITE must hold the pinned commit')
        self.assertEqual(result.inputs['w3c_suite_commit'], m.W3C_SUITE_COMMIT)
        ran = {}
        reasons = {}
        for check in result.checks:
            _w3c, engine, case = check.id.split(':', 2)
            if check.status == 'not_run':
                reasons[check.detail['reason']] = reasons.get(check.detail['reason'], 0) + 1
            else:
                ran.setdefault(case, {})[engine] = check.status
                with self.subTest(check=check.id):
                    # A pass must compare real results, never two empty reports.
                    self.assertEqual(check.detail['expected_violations'], OBSERVED_RESULTS[case])
        observed = {case: (engines.get('pyshacl'), engines.get('jena')) for case, engines in ran.items()}
        self.assertEqual(observed, OBSERVED, result.to_dict())
        self.assertEqual(reasons, OBSERVED_NOT_RUN)
        self.assertEqual(len(result.checks), 2 * 98)
        self.assertEqual(result.status, 'not_run')


if __name__ == '__main__':
    unittest.main()
