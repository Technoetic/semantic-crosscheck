"""Authored expectations exercise real pySHACL and fail-closed fixture boundaries."""
import argparse
import copy
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import pgfixture  # noqa: F401
from semantic_crosscheck.result import StageError

SH = 'http://www.w3.org/ns/shacl#'
XSD = 'http://www.w3.org/2001/XMLSchema#'
HEAD = '@prefix sh: <' + SH + '> . @prefix x: <urn:private:> . '
BEFORE = HEAD + 'x:row a x:Thing; x:quantity -1; x:serial "private-secret" .'
AFTER = BEFORE.replace('quantity -1', 'quantity 2')
SHAPES = HEAD + 'x:Shape a sh:NodeShape; sh:targetClass x:Thing; sh:property [sh:path x:quantity; sh:minInclusive 0] .'


def fact(component='MinInclusiveConstraintComponent', path='urn:private:quantity', value=None):
    return {'focus': 'urn:private:row', 'path': path, 'component': component,
            'severity': 'Violation', 'value': value or {'kind': 'number', 'value': '-1'}}


def manifest():
    return {'schema_version': 1, 'synthetic': True, 'cases': [{
        'id': 'numeric', 'before': 'before.ttl', 'after': 'after.ttl', 'shapes': 'shapes.ttl',
        'retained_targets': [{'shape': 'urn:private:Shape', 'node': 'urn:private:row'}],
        'immutable_predicates': ['urn:private:serial'], 'immutable_triples': [],
        'edit_costs': {'add': 2, 'remove': 3},
        'expected': {'before': {'conforms': False, 'results': [fact()]},
                     'after': {'conforms': True, 'results': []}}}]}


@unittest.skipUnless(importlib.util.find_spec('rdflib') and importlib.util.find_spec('pyshacl'),
                     'optional SHACL packages require requirements/reason.txt')
class RepairTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('semantic_crosscheck.shacl_repair'),
                             'bounded authored SHACL repair stage must be implemented')
        self.m = importlib.import_module('semantic_crosscheck.shacl_repair')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = self.root / 'spec.json'
        self.out = self.root / 'output'
        (self.root / 'before.ttl').write_text(BEFORE, encoding='utf-8')
        (self.root / 'after.ttl').write_text(AFTER, encoding='utf-8')
        (self.root / 'shapes.ttl').write_text(SHAPES, encoding='utf-8')

    def run_fixture(self, document=None, timeout_ms=30000):
        self.spec.write_text(json.dumps(document or manifest()), encoding='utf-8')
        return self.m.run(None, argparse.Namespace(spec=self.spec, timeout_ms=timeout_ms), self.out)

    def check(self, result, suffix):
        return next(c for c in result.checks if c.id.endswith(':' + suffix))

    def test_real_numeric_repair_matches_literal_reports_and_measures_soft_cost(self):
        result = self.run_fixture()
        self.assertEqual(result.stage, 'shacl_repair')
        self.assertEqual(result.status, 'pass')
        self.assertEqual(self.check(result, 'edits').detail,
                         {'added_triples': 1, 'removed_triples': 1, 'edit_cost': 5})
        self.assertEqual(self.check(result, 'before').detail['actual_violations'], 1)
        self.assertEqual(self.check(result, 'after').detail['actual_violations'], 0)
        self.assertEqual(result.inputs['fixture_cases'], 1)

    def test_real_class_repair_is_a_type_addition_and_not_a_simulated_report(self):
        (self.root / 'shapes.ttl').write_text(HEAD + 'x:Shape a sh:NodeShape; sh:targetClass x:Thing; '
            'sh:property [sh:path x:child; sh:class x:Part] .', encoding='utf-8')
        before = HEAD + 'x:row a x:Thing; x:child x:part; x:serial "private-secret" .'
        (self.root / 'before.ttl').write_text(before, encoding='utf-8')
        (self.root / 'after.ttl').write_text(before + ' x:part a x:Part .', encoding='utf-8')
        doc = manifest()
        doc['cases'][0]['expected']['before']['results'] = [fact('ClassConstraintComponent',
            'urn:private:child', {'kind': 'iri', 'value': 'urn:private:part'})]
        self.assertEqual(self.run_fixture(doc).status, 'pass')

    def test_real_property_pair_repair_and_exact_decimal_normalization(self):
        (self.root / 'shapes.ttl').write_text(HEAD + 'x:Shape a sh:NodeShape; sh:targetClass x:Thing; '
            'sh:property [sh:path x:quantity; sh:lessThan x:limit] .', encoding='utf-8')
        before = BEFORE.replace('quantity -1', 'quantity 3.00; x:limit 2')
        (self.root / 'before.ttl').write_text(before, encoding='utf-8')
        (self.root / 'after.ttl').write_text(before.replace('3.00', '1.00'), encoding='utf-8')
        doc = manifest()
        doc['cases'][0]['expected']['before']['results'] = [fact('LessThanConstraintComponent',
            value={'kind': 'number', 'value': '3'})]
        self.assertEqual(self.run_fixture(doc).status, 'pass')

    def test_target_class_deletion_cannot_pass_even_when_actual_after_report_is_empty(self):
        (self.root / 'after.ttl').write_text(AFTER.replace('a x:Thing;', ''), encoding='utf-8')
        result = self.run_fixture()
        self.assertEqual(self.check(result, 'after').status, 'pass')
        self.assertEqual(self.check(result, 'retention').status, 'fail')
        self.assertEqual(self.check(result, 'retention').detail['retained_after'], 0)
        self.assertEqual(result.status, 'fail')

    def test_explicit_target_node_deletion_requires_surviving_data_subject(self):
        (self.root / 'shapes.ttl').write_text(SHAPES.replace('sh:targetClass x:Thing', 'sh:targetNode x:row'), encoding='utf-8')
        (self.root / 'after.ttl').write_text(HEAD, encoding='utf-8')
        result = self.run_fixture()
        self.assertEqual(self.check(result, 'retention').status, 'fail')

    def test_hard_immutable_predicate_rejects_deletion_addition_and_changed_value(self):
        for after in [AFTER.replace('; x:serial "private-secret"', ''),
                      AFTER + ' x:row x:serial "other-secret" .',
                      AFTER.replace('private-secret', 'other-secret')]:
            with self.subTest(after=after[-15:]):
                (self.root / 'after.ttl').write_text(after, encoding='utf-8')
                result = self.run_fixture()
                self.assertEqual(self.check(result, 'immutable').status, 'fail')
                self.assertEqual(result.status, 'fail')

    def test_immutable_triple_must_exist_before_and_after_even_with_zero_edit_cost(self):
        doc = manifest()
        case = doc['cases'][0]
        case['edit_costs'] = {'add': 0, 'remove': 0}
        case['immutable_triples'] = [[{'kind': 'iri', 'value': 'urn:private:row'},
            {'kind': 'iri', 'value': 'urn:private:serial'},
            {'kind': 'literal', 'value': 'private-secret', 'datatype': XSD + 'string', 'language': ''}]]
        (self.root / 'after.ttl').write_text(AFTER.replace('private-secret', 'other-secret'), encoding='utf-8')
        result = self.run_fixture(doc)
        self.assertEqual(self.check(result, 'immutable').status, 'fail')
        case['immutable_triples'][0][2]['value'] = 'absent-secret'
        self.assertEqual(self.check(self.run_fixture(doc), 'immutable').status, 'fail')

    def test_equal_violation_count_with_wrong_independent_expected_value_fails(self):
        doc = manifest()
        doc['cases'][0]['expected']['before']['results'][0]['value']['value'] = '-2'
        result = self.run_fixture(doc)
        self.assertEqual(self.check(result, 'before').status, 'fail')
        self.assertEqual(self.check(result, 'before').detail['missing_constraints'], 1)
        self.assertEqual(self.check(result, 'before').detail['unexpected_constraints'], 1)

    def test_before_must_be_nonconforming_and_after_expected_must_conform(self):
        for phase in ['before', 'after']:
            doc = manifest()
            doc['cases'][0]['expected'][phase] = {'conforms': phase == 'before',
                'results': [] if phase == 'before' else [fact()]}
            with self.subTest(phase=phase), self.assertRaises(StageError) as caught:
                self.run_fixture(doc)
            self.assertEqual(caught.exception.exit_code, 2)

    def test_unsupported_sparql_complex_path_and_datatype_never_pass(self):
        for constraint in ['sh:datatype <' + XSD + 'integer>', 'sh:path (x:a x:b)',
                           'sh:sparql [sh:select "SELECT ?this WHERE {}"]']:
            with self.subTest(constraint=constraint):
                text = HEAD + 'x:Shape a sh:NodeShape; sh:targetNode x:row; ' + constraint + ' .'
                (self.root / 'shapes.ttl').write_text(text, encoding='utf-8')
                result = self.run_fixture()
                self.assertEqual(result.status, 'not_run')

    def test_imports_in_data_or_shapes_are_rejected_without_dereferencing(self):
        for name in ['before.ttl', 'after.ttl', 'shapes.ttl']:
            original = (self.root / name).read_text(encoding='utf-8')
            (self.root / name).write_text(original + ' <urn:doc> <http://www.w3.org/2002/07/owl#imports> '
                '<http://127.0.0.1:1/private-secret> .', encoding='utf-8')
            self.assertEqual(self.run_fixture().status, 'not_run')
            (self.root / name).write_text(original, encoding='utf-8')

    def test_malformed_turtle_and_ill_typed_literals_have_no_output_leaks(self):
        for text in ['private-secret is not turtle', HEAD + 'x:row x:value "private-secret"^^<' + XSD + 'integer> .']:
            (self.root / 'after.ttl').write_text(text, encoding='utf-8')
            result = self.run_fixture()
            self.assertNotEqual(result.status, 'pass')
            output = json.dumps(result.to_dict())
            for secret in ['private-secret', 'urn:private:', str(self.root)]:
                self.assertNotIn(secret, output)

    def test_strict_schema_unknown_keys_empty_targets_duplicates_and_nonfinite_numbers(self):
        docs = []
        for field, value in [('schema_version', True), ('synthetic', False), ('unknown', 1), ('cases', [])]:
            doc = manifest()
            doc[field] = value
            docs.append(json.dumps(doc))
        for field, value in [('retained_targets', []), ('immutable_predicates', ['urn:x', 'urn:x']),
                             ('unknown', True), ('edit_costs', {'add': -1, 'remove': 1})]:
            doc = manifest()
            doc['cases'][0][field] = value
            docs.append(json.dumps(doc))
        docs += ['{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}',
                 '{"schema_version":Infinity}', '{"schema_version":1.0}']
        for raw in docs:
            with self.subTest(raw=raw[:90]):
                self.spec.write_text(raw, encoding='utf-8')
                with self.assertRaises(StageError) as caught:
                    self.m.run(None, argparse.Namespace(spec=self.spec, timeout_ms=30000), self.out)
                self.assertEqual(caught.exception.exit_code, 2)
                self.assertNotIn(str(self.root), str(caught.exception))

    def test_local_only_paths_size_case_and_triple_limits(self):
        for path in ['../before.ttl', str(self.root / 'before.ttl'), 'https://example.org/private.ttl', 'before.json']:
            doc = manifest()
            doc['cases'][0]['before'] = path
            with self.subTest(path=path), self.assertRaises(StageError):
                self.run_fixture(doc)
        doc = manifest()
        doc['cases'] = [copy.deepcopy(doc['cases'][0]) for _ in range(17)]
        with self.assertRaises(StageError):
            self.run_fixture(doc)
        (self.root / 'before.ttl').write_bytes(b'#' * (1024 * 1024 + 1))
        with self.assertRaises(StageError):
            self.run_fixture()
        (self.root / 'before.ttl').write_text(HEAD + '\n'.join('x:n%d x:p %d .' % (i, i) for i in range(10001)), encoding='utf-8')
        self.assertEqual(self.run_fixture().status, 'not_run')

    def test_node_level_shape_report_has_an_absent_path(self):
        (self.root / 'shapes.ttl').write_text(HEAD + 'x:Shape a sh:NodeShape; sh:targetNode x:row; '
            'sh:class x:Part .', encoding='utf-8')
        (self.root / 'after.ttl').write_text(AFTER + ' x:row a x:Part .', encoding='utf-8')
        doc = manifest()
        doc['cases'][0]['expected']['before']['results'] = [fact('ClassConstraintComponent', path=None,
            value={'kind': 'iri', 'value': 'urn:private:row'})]
        self.assertEqual(self.run_fixture(doc).status, 'pass')

    def test_target_class_subclass_closure_matches_real_pyshacl_target_membership(self):
        extra = ' x:Child <http://www.w3.org/2000/01/rdf-schema#subClassOf> x:Thing .'
        for name, text in [('before.ttl', BEFORE), ('after.ttl', AFTER)]:
            (self.root / name).write_text(text.replace('a x:Thing', 'a x:Child') + extra, encoding='utf-8')
        self.assertEqual(self.run_fixture().status, 'pass')

    def test_empty_shape_scope_and_relative_or_blank_terms_cannot_pass(self):
        for name, text in [('shapes.ttl', HEAD), ('after.ttl', HEAD),
                           ('after.ttl', AFTER.replace('x:row', '<relative-private>'))]:
            with self.subTest(name=name, text=text[-15:]):
                original = (self.root / name).read_bytes()
                (self.root / name).write_text(text, encoding='utf-8')
                self.assertNotEqual(self.run_fixture().status, 'pass')
                (self.root / name).write_bytes(original)

    def test_duplicate_independent_expected_facts_are_compared_as_a_bag(self):
        doc = manifest()
        doc['cases'][0]['expected']['before']['results'].append(fact())
        result = self.run_fixture(doc)
        self.assertEqual(self.check(result, 'before').status, 'fail')
        self.assertEqual(self.check(result, 'before').detail['missing_constraints'], 1)

    def test_hard_immutable_literals_preserve_lexical_identity(self):
        predicate = ' x:row x:fixed "01"^^<' + XSD + 'integer> .'
        (self.root / 'before.ttl').write_text(BEFORE + predicate, encoding='utf-8')
        (self.root / 'after.ttl').write_text(AFTER + predicate.replace('"01"', '"1"'), encoding='utf-8')
        doc = manifest()
        doc['cases'][0]['immutable_predicates'].append('urn:private:fixed')
        self.assertEqual(self.check(self.run_fixture(doc), 'immutable').status, 'fail')

    def test_missing_retention_identity_and_blank_data_nodes_cannot_pass(self):
        for change in ['x:row', '_:private']:
            (self.root / 'before.ttl').write_text(BEFORE.replace('x:row', change), encoding='utf-8')
            doc = manifest()
            if change == 'x:row':
                doc['cases'][0]['retained_targets'][0]['node'] = 'urn:private:absent'
            self.assertNotEqual(self.run_fixture(doc).status, 'pass')

    def test_timeout_bounds_real_subprocess_and_cannot_leave_stale_success(self):
        stale = self.out / 'shacl_repair' / 'result.json'
        stale.parent.mkdir(parents=True)
        stale.write_text('{"status":"pass"}', encoding='utf-8')
        result = self.run_fixture(timeout_ms=1)
        self.assertEqual(result.status, 'error')
        self.assertFalse(stale.exists())
        self.assertEqual(self.check(result, 'execution').detail['reason'], 'timeout')

    def test_malformed_spec_clears_own_stale_result_and_part(self):
        directory = self.out / 'shacl_repair'
        directory.mkdir(parents=True)
        for name in ['result.json', 'result.json.part']:
            (directory / name).write_text('{"status":"pass"}', encoding='utf-8')
        with self.assertRaises(StageError):
            self.run_fixture({'wrong': True})
        self.assertEqual(list(directory.iterdir()), [])

    def test_spec_output_alias_is_preserved_instead_of_deleted(self):
        alias = self.out / 'shacl_repair' / 'result.json'
        alias.parent.mkdir(parents=True)
        alias.write_text(json.dumps(manifest()), encoding='utf-8')
        before = alias.read_bytes()
        with self.assertRaises(StageError):
            self.m.run(None, argparse.Namespace(spec=alias, timeout_ms=30000), self.out)
        self.assertEqual(alias.read_bytes(), before)

    def test_data_output_alias_is_preserved_even_with_invalid_extension(self):
        alias = self.out / 'shacl_repair' / 'result.json'
        alias.parent.mkdir(parents=True)
        alias.write_text(AFTER, encoding='utf-8')
        doc = manifest()
        doc['cases'][0]['after'] = 'output/shacl_repair/result.json'
        before = alias.read_bytes()
        with self.assertRaises(StageError):
            self.run_fixture(doc)
        self.assertEqual(alias.read_bytes(), before)

    def test_duplicate_json_fields_cannot_hide_a_declared_input_alias(self):
        alias = self.out / 'shacl_repair' / 'result.json'
        alias.parent.mkdir(parents=True)
        alias.write_text(AFTER, encoding='utf-8')
        raw = json.dumps(manifest()).replace('"after": "after.ttl"',
            '"after": "output/shacl_repair/result.json", "after": "after.ttl"')
        self.spec.write_text(raw, encoding='utf-8')
        before = alias.read_bytes()
        with self.assertRaises(StageError):
            self.m.run(None, argparse.Namespace(spec=self.spec, timeout_ms=30000), self.out)
        self.assertEqual(alias.read_bytes(), before)

    def test_hardlinked_data_output_alias_is_preserved(self):
        alias = self.out / 'shacl_repair' / 'result.json'
        alias.parent.mkdir(parents=True)
        os.link(self.root / 'after.ttl', alias)
        before = alias.read_bytes()
        with self.assertRaises(StageError):
            self.run_fixture()
        self.assertEqual(alias.read_bytes(), before)

    def test_oversized_malformed_and_invalid_utf8_manifests_preserve_declared_output_inputs(self):
        import sxc
        for execution in ('module', 'cli'):
            for field in ('before', 'after', 'shapes'):
                for filename in ('result.json', 'result.json.part'):
                    for invalid in ('oversized', 'malformed', 'unicode'):
                        with self.subTest(execution=execution, field=field, filename=filename, invalid=invalid):
                            alias = self.out / 'shacl_repair' / filename
                            alias.parent.mkdir(parents=True, exist_ok=True)
                            alias.write_bytes(b'protected-private-input')
                            doc = manifest()
                            doc['cases'][0][field] = 'output/shacl_repair/' + filename
                            if invalid == 'oversized':
                                doc['cases'][0]['id'] = 'x' * (256 * 1024)
                            raw = json.dumps(doc).encode('utf-8')
                            if invalid == 'malformed':
                                raw = raw[:-1]
                            if invalid == 'unicode':
                                raw += b'\xff'
                            self.spec.write_bytes(raw)
                            if execution == 'module':
                                with self.assertRaises(StageError):
                                    self.m.run(None, argparse.Namespace(spec=self.spec, timeout_ms=30000), self.out)
                            else:
                                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                                    code = sxc.main(['shacl-repair', '--spec', str(self.spec), '--out', str(self.out)])
                                self.assertEqual(code, 2)
                            self.assertTrue(alias.exists(), 'uninspectable manifest must prevent cleanup')
                            self.assertEqual(alias.read_bytes(), b'protected-private-input')

    def test_uninspectable_manifest_cannot_overwrite_a_hardlinked_declared_input(self):
        import sxc
        for invalid in ('oversized', 'malformed'):
            with self.subTest(invalid=invalid):
                alias = self.out / 'shacl_repair' / 'result.json'
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.unlink(missing_ok=True)
                os.link(self.root / 'after.ttl', alias)
                original = alias.read_bytes()
                doc = manifest()
                if invalid == 'oversized':
                    doc['cases'][0]['id'] = 'x' * (256 * 1024)
                raw = json.dumps(doc).encode('utf-8')
                self.spec.write_bytes(raw if invalid == 'oversized' else raw[:-1])
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(sxc.main(['shacl-repair', '--spec', str(self.spec), '--out', str(self.out)]), 2)
                self.assertEqual(alias.read_bytes(), original)
                self.assertEqual((self.root / 'after.ttl').read_bytes(), original)

    def test_actual_validation_never_changes_authored_files(self):
        originals = {path.name: path.read_bytes() for path in self.root.glob('*.ttl')}
        self.assertEqual(self.run_fixture().status, 'pass')
        self.assertEqual({path.name: path.read_bytes() for path in self.root.glob('*.ttl')}, originals)

    def test_operational_config_invalid_timeout_and_missing_manifest_fail_safely(self):
        for cfg, timeout in [(object(), 30000), (None, 0), (None, True), (None, 600001)]:
            with self.subTest(timeout=timeout), self.assertRaises(StageError) as caught:
                self.m.run(cfg, argparse.Namespace(spec=self.spec, timeout_ms=timeout), self.out)
            self.assertEqual(caught.exception.exit_code, 2)
        with self.assertRaises(StageError) as caught:
            self.m.run(None, argparse.Namespace(spec=self.spec, timeout_ms=30000), self.out)
        self.assertEqual(caught.exception.exit_code, 2)

    def test_result_contains_only_counts_fixed_identifiers_and_digests(self):
        result = self.run_fixture()
        output = json.dumps(result.to_dict())
        for secret in ['urn:private:', 'private-secret', 'numeric', 'before.ttl', str(self.root)]:
            self.assertNotIn(secret, output)
        self.assertEqual(len(result.inputs['fixture_sha256']), 64)
        self.assertTrue(result.not_proven)


if __name__ == '__main__':
    unittest.main()
