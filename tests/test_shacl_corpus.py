"""Independent, hand-derived report expectations and fail-closed runtime boundaries."""
import argparse
import contextlib
import importlib
import importlib.util
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck.result import StageError

SH = 'http://www.w3.org/ns/shacl#'
XSD = 'http://www.w3.org/2001/XMLSchema#'
PREFIX = '@prefix sh: <' + SH + '> . @prefix xsd: <' + XSD + '> . '
EMPTY = PREFIX + '[] a sh:ValidationReport; sh:conforms true .'
BAD = PREFIX + '''[] a sh:ValidationReport; sh:conforms false; sh:result [
 a sh:ValidationResult; sh:focusNode <urn:row:1>; sh:resultPath <urn:qty>;
 sh:sourceConstraintComponent sh:MinExclusiveConstraintComponent;
 sh:resultSeverity sh:Violation; sh:value "1.100"^^xsd:decimal ] .'''


@unittest.skipUnless(importlib.util.find_spec('rdflib') and importlib.util.find_spec('pyshacl'),
                     'optional SHACL packages require requirements/reason.txt')
class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('semantic_crosscheck.shacl_corpus'),
                             'independent SHACL corpus module must be implemented')
        self.m = importlib.import_module('semantic_crosscheck.shacl_corpus')

    def test_report_normalizes_decimal_and_ignores_result_blank_node_names_and_messages(self):
        one = self.m.parse_report(BAD)
        two = self.m.parse_report(BAD.replace('[] a sh:ValidationReport', '_:different a sh:ValidationReport')
                                  .replace('"1.100"', '"1.1"').replace('sh:value',
                                  'sh:resultMessage "different wording"; sh:value'))
        self.assertEqual(one, two)
        self.assertEqual(one.violations, ((
            'urn:row:1', 'urn:qty', SH + 'MinExclusiveConstraintComponent', SH + 'Violation',
            ('number', '1.1')),))

    def test_constraint_path_severity_and_value_changes_fail_even_with_same_focus(self):
        base = self.m.parse_report(BAD)
        for old, new in [('MinExclusiveConstraintComponent', 'MaxInclusiveConstraintComponent'),
                         ('urn:qty', 'urn:other'), ('sh:Violation;', 'sh:Warning;'),
                         ('"1.100"', '"1.101"')]:
            with self.subTest(new=new):
                wrong = self.m.parse_report(BAD.replace(old, new))
                self.assertFalse(self.m.compare_reports(base, wrong)['matches'])
        self.assertTrue(self.m.compare_reports(base, base)['matches'])

    def test_valid_empty_report_is_distinct_from_missing_or_malformed_report(self):
        self.assertEqual(self.m.parse_report(EMPTY).violations, ())
        malformed = ['', '@prefix sh: <' + SH + '> .', 'not turtle',
                     EMPTY.replace('sh:conforms true', 'sh:conforms "true"'),
                     EMPTY.replace('sh:conforms true', 'sh:conforms false'),
                     BAD.replace('sh:conforms false', 'sh:conforms true'),
                     BAD.replace('sh:resultPath <urn:qty>;', ''),
                     BAD.replace('sh:resultSeverity sh:Violation;', ''),
                     BAD.replace('sh:value "1.100"^^xsd:decimal', 'sh:value _:unknown')]
        for text in malformed:
            with self.subTest(text=text[:45]):
                with self.assertRaises(self.m.CorpusError):
                    self.m.parse_report(text)

    def test_invalid_boolean_lexical_form_cannot_become_false_by_rdflib_coercion(self):
        text = BAD.replace('sh:conforms false', 'sh:conforms "private-invalid-boolean"^^xsd:boolean')
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            with self.assertRaises(self.m.CorpusError):
                self.m.parse_report(text)
        self.assertEqual(output.getvalue(), '')

    def test_duplicate_semantic_violations_are_compared_as_a_multiset(self):
        # Construct two distinct validation results, not two edges to the same result.
        body = BAD.split('sh:result [', 1)[1].rsplit('] .', 1)[0]
        extra = PREFIX + '[] a sh:ValidationReport; sh:conforms false; sh:result [' + body + '], [' + body + '] .'
        one, two = self.m.parse_report(BAD), self.m.parse_report(extra)
        self.assertEqual(len(two.violations), 2)
        self.assertFalse(self.m.compare_reports(one, two)['matches'])

    def test_real_pyshacl_matches_every_independently_expected_corpus_case(self):
        cases = self.m.corpus()
        self.assertGreaterEqual(len(cases), 7)
        self.assertTrue(any(not c.expected.violations for c in cases))
        for case in cases:
            with self.subTest(case=case.id):
                actual = self.m.validate_pyshacl(case.data, case.shapes, 30000)
                self.assertEqual(actual, case.expected)

    def test_combined_case_keeps_both_constraints_for_one_focus(self):
        case = next(c for c in self.m.corpus() if c.id == 'combined')
        components = [v[2] for v in case.expected.violations if v[0].endswith('%5B%221%22%5D')]
        self.assertEqual(sorted(components), sorted([SH + 'ClassConstraintComponent', SH + 'MaxInclusiveConstraintComponent']))
        # Removing the literal value predicate must lose a constraint, although focus 1 remains in sh:class.
        import rdflib
        graph = rdflib.Graph().parse(data=case.data, format='turtle')
        pred = rdflib.URIRef('https://example.org/sxc-corpus#bom_line.child_id/value')
        graph.remove((None, pred, None))
        actual = self.m.validate_pyshacl(graph.serialize(format='turtle'), case.shapes, 30000)
        self.assertFalse(self.m.compare_reports(case.expected, actual)['matches'])

    def test_missing_jena_and_invalid_timeout_raise_sanitized_stage_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            for args, code in [(argparse.Namespace(jena_home=temp, timeout_ms=30000), 3),
                               (argparse.Namespace(jena_home=temp, timeout_ms=0), 2)]:
                with self.subTest(code=code), self.assertRaises(StageError) as caught:
                    self.m.run(None, args, Path(temp))
                self.assertEqual(caught.exception.exit_code, code)
                self.assertNotIn(temp, str(caught.exception))

    def test_missing_java_and_operational_configuration_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            (p/'lib').mkdir()
            (p/'lib'/'jena-shacl-test.jar').touch()
            args = argparse.Namespace(jena_home=temp, timeout_ms=30000)
            with mock.patch.object(self.m.owl, 'java_executable', return_value=None):
                with self.assertRaises(StageError) as caught:
                    self.m.run(None, args, p)
                self.assertEqual(caught.exception.exit_code, 3)
            with self.assertRaises(StageError) as caught:
                self.m.run(object(), args, p)
            self.assertEqual(caught.exception.exit_code, 2)

    def test_timeout_terminates_owned_descendant_with_inherited_output_handles(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)/'child.pid'
            child = 'import time; time.sleep(60)'
            code = ('import subprocess,sys,time; from pathlib import Path; '
                    'p=subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); '
                    'Path(' + repr(str(p)) + ').write_text(str(p.pid)); time.sleep(60)')
            started = time.monotonic()
            with self.assertRaises(self.m.CorpusError) as caught:
                self.m._capture([sys.executable, '-c', code], 1200)
            self.assertEqual(caught.exception.reason, 'timeout')
            self.assertLess(time.monotonic() - started, 4)
            self.assertTrue(p.exists(), 'fixture must actually start a descendant')
            pid = int(p.read_text())
            if os.name == 'nt':
                import ctypes
                api = ctypes.WinDLL('kernel32', use_last_error=True)
                api.OpenProcess.restype = ctypes.c_void_p
                handle = api.OpenProcess(0x1000, False, pid)
                if handle:
                    status = ctypes.c_ulong()
                    try:
                        self.assertTrue(api.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(status)))
                        self.assertNotEqual(status.value, 259, 'owned child must not remain active')
                    finally:
                        api.CloseHandle(ctypes.c_void_p(handle))
            else:
                # A terminated orphan can briefly be a zombie until init reaps it.
                proc = Path('/proc')/str(pid)/'stat'
                if proc.exists():
                    self.assertEqual(proc.read_text().split()[2], 'Z')

    def test_jena_parser_accepts_only_successful_complete_report(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            for value in ('good', 'missing', 'malformed', 'nonzero', 'timeout', 'flood', 'stderr_flood'):
                body = {
                    'good': 'print(' + repr(EMPTY) + ')',
                    'missing': 'pass',
                    'malformed': 'print("bad turtle private-marker")',
                    'nonzero': 'print(' + repr(EMPTY) + '); raise SystemExit(1)',
                    'timeout': 'import time; time.sleep(5)',
                    'flood': 'import sys; sys.stdout.write("x"*1000000)',
                    'stderr_flood': 'import sys; sys.stderr.write("x"*1000000)',
                }[value]
                with self.subTest(value=value), mock.patch.object(self.m, '_jena_command',
                        return_value=[sys.executable, '-c', body]):
                    if value == 'good':
                        got = self.m.validate_jena(p, sys.executable, p/'d.ttl', p/'s.ttl', 2000)
                        self.assertTrue(got.conforms)
                    else:
                        with self.assertRaises(self.m.CorpusError) as caught:
                            self.m.validate_jena(p, sys.executable, p/'d.ttl', p/'s.ttl', 150)
                        self.assertNotIn('private-marker', str(caught.exception))
                        self.assertNotIn(temp, str(caught.exception))

    def test_jena_loads_file_uris_with_spaces_and_passes_literal_argv(self):
        with tempfile.TemporaryDirectory(prefix='corpus spaces ') as temp:
            p = Path(temp)
            command = self.m._jena_command(p, sys.executable, p/'data.ttl', p/'shapes.ttl')
            self.assertEqual(command[-4:], ['--shapes', (p/'shapes.ttl').as_uri(),
                                            '--data', (p/'data.ttl').as_uri()])

    def test_run_checks_both_engines_against_expectations_and_sanitizes_mismatches(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(self.m, '_components',
                return_value=(Path(temp), sys.executable)), mock.patch.object(self.m, 'validate_pyshacl',
                return_value=self.m.parse_report(EMPTY)), mock.patch.object(self.m, 'validate_jena',
                return_value=self.m.parse_report(EMPTY)):
            result = self.m.run(None, argparse.Namespace(jena_home=temp, timeout_ms=30000), Path(temp))
        self.assertEqual(result.status, 'fail')
        self.assertTrue(any(c.status == 'fail' and c.id.startswith('pyshacl:') for c in result.checks))
        self.assertTrue(any(c.status == 'fail' and c.id.startswith('jena:') for c in result.checks))
        text = json.dumps(result.to_dict())
        self.assertNotIn(temp, text)
        self.assertNotIn('https://example.org', text)
        self.assertNotIn('urn:row', text)
        self.assertTrue(result.not_proven)

    @unittest.skipUnless(os.environ.get('SXC_JENA_HOME') and os.environ.get('SXC_JAVA'),
                         'actual Jena and Java require explicit environment')
    def test_actual_jena_and_pyshacl_match_all_expected_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.m.run(None, argparse.Namespace(jena_home=os.environ['SXC_JENA_HOME'], timeout_ms=30000), Path(temp))
        self.assertEqual(result.status, 'pass', result.to_dict())
        self.assertGreaterEqual(len(result.checks), 14)


if __name__ == '__main__':
    unittest.main()
