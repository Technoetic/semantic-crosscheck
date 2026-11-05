"""Offline scalar boundaries must stay exact, bounded and witness-compatible."""
import datetime as dt
from decimal import Decimal, Inexact, Rounded, localcontext
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - package path setup only; no fixture is started
from semantic_crosscheck import db, witness
from semantic_crosscheck.result import StageError

try:
    boundary = importlib.import_module('semantic_crosscheck.boundary_schema')
except ModuleNotFoundError:
    boundary = None


def declaration(**changes):
    value = dict(version=1, table='items', column='qty', type='integer',
                 rows=[0, 2], predicate_sql='qty >= 10')
    value.update(changes)
    return value


class BoundarySchemaTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(boundary, 'The offline boundary-schema generator must exist')
        self.tmp = tempfile.TemporaryDirectory(prefix='sxc_boundary_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'boundary-input.json'

    def load(self, document=None, raw=None):
        self.path.write_bytes(raw if raw is not None else json.dumps(
            declaration() if document is None else document).encode('utf-8'))
        return boundary.load_spec(self.path)

    def domain(self, document):
        derived = boundary.derive(self.load(document))
        return derived['tables'][document['table']]['columns'][0]['values']

    def run_stage(self, document=None):
        self.load(document)
        return boundary.run(None, SimpleNamespace(spec=str(self.path)), self.root / 'out')

    def assert_rejected(self, document):
        with self.assertRaises(StageError) as caught:
            self.load(document)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertNotIn(document.get('predicate_sql', 'PRIVATE-INPUT'), str(caught.exception))

    def test_integer_neighbors_are_literal_minus_one_literal_plus_one_and_null(self):
        for op in ('=', '<>', '!=', '<', '<=', '>', '>='):
            with self.subTest(op=op):
                self.assertEqual(self.domain(declaration(predicate_sql=' qty ' + op + ' +10\n')),
                                 [None, 9, 10, 11])
        self.assertEqual(self.domain(declaration(predicate_sql='qty = -0')), [None, -1, 0, 1])

    def test_integer_extrema_omit_only_the_unrepresentable_neighbor(self):
        self.assertEqual(self.domain(declaration(predicate_sql='qty = -2147483648')),
                         [None, -2147483648, -2147483647])
        self.assertEqual(self.domain(declaration(predicate_sql='qty = 2147483647')),
                         [None, 2147483646, 2147483647])

    def test_integer_literals_must_be_exact_integer_syntax_and_int32(self):
        for token in ('2147483648', '-2147483649', '1.0', '1e2', '+ 1', '0x10', '9' * 256):
            with self.subTest(token=token):
                self.assert_rejected(declaration(predicate_sql='qty = ' + token))

    def test_numeric_quantum_and_exponent_use_fixed_exact_strings(self):
        cases = [
            ('1.20', [None, '1.19', '1.20', '1.21']),
            ('1.2000', [None, '1.19', '1.20', '1.21']),
            ('1.2e1', [None, '11.99', '12.00', '12.01']),
            ('-.50', [None, '-0.51', '-0.50', '-0.49']),
            ('-0e2', [None, '-0.01', '0.00', '0.01']),
        ]
        for token, expected in cases:
            with self.subTest(token=token):
                self.assertEqual(self.domain(declaration(type='numeric(6,2)', predicate_sql='qty >= ' + token)),
                                 expected)

    def test_numeric_precision_38_and_scale_extrema_do_not_round(self):
        cases = [
            ('numeric(38,0)', '9' * 38, [None, '9' * 37 + '8', '9' * 38]),
            ('numeric(3,2)', '-9.99', [None, '-9.99', '-9.98']),
            ('numeric(38,38)', '0.' + '9' * 38,
             [None, '0.' + '9' * 37 + '8', '0.' + '9' * 38]),
        ]
        for precision in (2, 28, 80):
            for kind, token, expected in cases:
                with self.subTest(precision=precision, kind=kind), localcontext() as context:
                    context.prec = precision
                    context.traps[Inexact] = True
                    context.traps[Rounded] = True
                    self.assertEqual(self.domain(declaration(type=kind, predicate_sql='qty = ' + token)), expected)

    def test_numeric_nonrepresentable_literals_are_rejected_without_rounding(self):
        for token in ('1.001', '10000.00', '-10000.00', '1e1000000000', '1e-1000000000',
                      '0e1000000000', '1e' + '9' * 200, 'NaN', 'Infinity'):
            with self.subTest(token=token):
                self.assert_rejected(declaration(type='numeric(6,2)', predicate_sql='qty = ' + token))

    def test_numeric_types_require_the_existing_canonical_precision_scale_subset(self):
        for kind in ('numeric', 'NUMERIC(6,2)', 'numeric(0,0)', 'numeric(39,0)',
                     'numeric(2,3)', 'numeric(06,2)', 'numeric(6,02)', 'numeric(6, 2)',
                     'text', 'bigint', 'timestamp', 'boolean'):
            with self.subTest(kind=kind):
                self.assert_rejected(declaration(type=kind))

    def test_date_neighbors_cover_leap_day_and_witness_supported_extrema(self):
        cases = [
            ("DATE '2024-02-29'", [None, '2024-02-28', '2024-02-29', '2024-03-01']),
            ("date '0001-01-01'", [None, '0001-01-01', '0001-01-02']),
            ("Date '9999-12-31'", [None, '9999-12-30', '9999-12-31']),
        ]
        for literal, expected in cases:
            with self.subTest(literal=literal):
                self.assertEqual(self.domain(declaration(column='day', type='date', predicate_sql='day < ' + literal)),
                                 expected)

    def test_date_literals_reject_invalid_dates_noniso_and_timestamp_casts(self):
        for literal in ("DATE '2023-02-29'", "DATE '0000-01-01'", "DATE '10000-01-01'",
                        "DATE '2026-1-1'", "DATE 'infinity'", "'2026-01-01'::date",
                        "TIMESTAMP '2026-01-01 00:00:00'"):
            with self.subTest(literal=literal):
                self.assert_rejected(declaration(column='day', type='date', predicate_sql='day = ' + literal))

    def test_full_consumption_rejects_every_unsupported_sql_form(self):
        invalid = [
            'qty = 10 AND qty < 20', 'qty = 10 OR TRUE', 'qty = 10 -- comment',
            'qty = 10 /* comment */', 'qty = 10;', '(qty = 10)', '10 = qty',
            'items.qty = 10', 'other = 10', 'QTY = 10', '"qty" = 10',
            'qty = abs(10)', 'qty = (SELECT 10)', 'qty = 10::integer',
            'qty BETWEEN 1 AND 10', 'qty IN (1, 10)', 'qty IS NULL',
            'qty == 10', 'qty => 10', 'qty = 10\x00', 'qty = 10\u200b',
            'qty = 10 JOIN private_relation ON TRUE', 'qty = 10 UNION SELECT 1',
            'qty = :param', 'qty = $1', 'qty = TRUE', 'qty = NULL',
        ]
        for predicate in invalid:
            with self.subTest(predicate=predicate):
                self.assert_rejected(declaration(predicate_sql=predicate))

    def test_declaration_has_exact_fields_and_typed_bounded_rows(self):
        invalid = [declaration(version=True), declaration(version=2), declaration(extra=1),
                   declaration(rows=[0, 5]), declaration(rows=[2, 1]), declaration(rows=[False, 1]),
                   declaration(rows=[0.0, 1]), declaration(rows=[0]), declaration(rows='0,2'),
                   declaration(table='sxc_candidate'), declaration(table='private.table'),
                   declaration(column='Qty'), declaration(column='q' * 64), declaration(predicate_sql=1)]
        for key in declaration():
            missing = declaration()
            del missing[key]
            invalid.append(missing)
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(StageError):
                    self.load(document)

    def test_strict_json_rejects_duplicate_keys_nonfinite_invalid_utf8_and_oversize(self):
        for raw in (b'{"version":1,"version":1}', b'{"rows":[NaN,1]}', b'\xff',
                    b' ' * (65536 + 1), b''):
            with self.subTest(size=len(raw)), self.assertRaises(StageError):
                self.load(raw=raw)

    def test_pure_derivation_preserves_exact_declared_rows_and_witness_shape(self):
        spec = self.load(declaration(rows=[1, 4]))
        self.assertEqual(boundary.derive(spec), {'version': 1, 'tables': {'items': {
            'rows': [1, 4], 'columns': [{'name': 'qty', 'type': 'integer', 'values': [None, 9, 10, 11]}]}}})
        self.assertEqual(boundary.derive(spec), boundary.derive(spec))
        self.path.unlink()
        self.assertEqual(boundary.derive(spec)['tables']['items']['rows'], [1, 4])

    def test_stage_roundtrips_through_witness_without_database_or_solver(self):
        with patch.object(db, 'connect', side_effect=AssertionError('No database allowed')), \
                patch.object(witness, 'generate', side_effect=AssertionError('No solver allowed')):
            result = self.run_stage(declaration(predicate_sql='qty >= 123456789'))
        target = self.root / 'out' / 'boundary_schema' / 'witness-schema.json'
        spec = witness.load(target)
        self.assertEqual(spec.domains['items'][0], (None, 123456788, 123456789, 123456790))
        self.assertEqual(result.stage, 'boundary_schema')
        self.assertEqual(result.status, 'pass')
        detail = result.checks[0].detail
        self.assertEqual(detail['domain_values'], 4)
        self.assertEqual(detail['non_null_values'], 3)
        self.assertEqual(detail['source_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(detail['witness_schema_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(detail['generator_sha256'], hashlib.sha256(Path(boundary.__file__).read_bytes()).hexdigest())
        encoded = json.dumps(result.to_dict())
        for secret in ('123456789', 'qty >=', str(self.path), 'items'):
            self.assertNotIn(secret, encoded)

    def test_generated_numeric_and_date_documents_roundtrip_without_value_change(self):
        cases = [
            (declaration(type='numeric(38,38)', predicate_sql='qty = 0.' + '9' * 38),
             (None, Decimal('0.' + '9' * 37 + '8'), Decimal('0.' + '9' * 38))),
            (declaration(column='day', type='date', predicate_sql="day = DATE '2024-02-29'"),
             (None, dt.date(2024, 2, 28), dt.date(2024, 2, 29), dt.date(2024, 3, 1))),
        ]
        for document, expected in cases:
            with self.subTest(kind=document['type']):
                self.assertEqual(self.run_stage(document).status, 'pass')
                loaded = witness.load(self.root / 'out' / 'boundary_schema' / 'witness-schema.json')
                self.assertEqual(loaded.domains['items'][0], expected)

    def test_invalid_spec_clears_previous_generated_artifact(self):
        self.run_stage()
        target = self.root / 'out' / 'boundary_schema' / 'witness-schema.json'
        self.path.write_text(json.dumps(declaration(predicate_sql='qty = PRIVATE-INPUT')), encoding='utf-8')
        with self.assertRaises(StageError) as caught:
            boundary.run(None, SimpleNamespace(spec=str(self.path)), self.root / 'out')
        self.assertNotIn('PRIVATE-INPUT', str(caught.exception))
        self.assertFalse(target.exists())

    def test_input_artifact_alias_is_rejected_without_erasing_input(self):
        target = self.root / 'out' / 'boundary_schema' / 'witness-schema.json'
        target.parent.mkdir(parents=True)
        raw = json.dumps(declaration()).encode('utf-8')
        target.write_bytes(raw)
        with self.assertRaises(StageError):
            boundary.run(None, SimpleNamespace(spec=str(target)), self.root / 'out')
        self.assertEqual(target.read_bytes(), raw)

    def test_source_change_during_derivation_cannot_emit_bound_evidence(self):
        self.load()
        real = boundary.derive
        def derive_and_change(spec):
            document = real(spec)
            self.path.write_bytes(self.path.read_bytes() + b'\n')
            return document
        with patch.object(boundary, 'derive', side_effect=derive_and_change):
            result = boundary.run(None, SimpleNamespace(spec=str(self.path)), self.root / 'out')
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.root / 'out' / 'boundary_schema' / 'witness-schema.json').exists())

    def test_generator_change_during_derivation_cannot_claim_the_new_generator_hash(self):
        self.load()
        generator = self.root / 'generator.py'
        generator.write_bytes(b'original generator bytes')
        real = boundary.derive
        def derive_and_change(spec):
            document = real(spec)
            generator.write_bytes(b'changed generator bytes')
            return document
        with patch.object(boundary, '__file__', str(generator)), \
                patch.object(boundary, 'derive', side_effect=derive_and_change):
            result = boundary.run(None, SimpleNamespace(spec=str(self.path)), self.root / 'out')
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.root / 'out' / 'boundary_schema' / 'witness-schema.json').exists())

    def test_artifact_failure_is_sanitized_and_leaves_no_partial_witness(self):
        self.load()
        with patch.object(boundary.os, 'replace', side_effect=OSError('PRIVATE-OUTPUT')):
            result = boundary.run(None, SimpleNamespace(spec=str(self.path)), self.root / 'out')
        self.assertEqual(result.status, 'error')
        self.assertNotIn('PRIVATE-OUTPUT', json.dumps(result.to_dict()))
        self.assertEqual(list((self.root / 'out' / 'boundary_schema').iterdir()), [])

    def test_published_output_drift_cannot_retain_expected_bytes_hash_or_pass(self):
        self.load()
        replace = boundary.os.replace
        def replace_and_change(source, target):
            replace(source, target)
            Path(target).write_bytes(b'{}')
        with patch.object(boundary.os, 'replace', side_effect=replace_and_change):
            result = boundary.run(None, SimpleNamespace(spec=str(self.path)), self.root / 'out')
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.root / 'out' / 'boundary_schema' / 'witness-schema.json').exists())


if __name__ == '__main__':
    unittest.main()
