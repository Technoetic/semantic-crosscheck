"""Raw-number expansion must be rejected before comparison or evidence promotion."""
import contextlib
import json
import tempfile
import unittest
from decimal import Decimal, Inexact, Rounded, localcontext
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck import canon, compare, cube_replay, mutate, stage_verify
from semantic_crosscheck.cube_client import CubeResponseError
from test_cube_envelope import served
from test_compare_unit import FakeConn, demo_cfg, demo_model, gold_of


def bounded(body, cap=128):
    client = served(body)
    client.max_response_bytes = cap
    return client


class CubeNumericBudgetTest(unittest.TestCase):
    def test_signed_exponent_exact_boundary_and_one_character_over(self):
        # All four admitted values have exactly 128 fixed-point characters.
        for accepted, rejected in ((b'1e127', b'1e128'), (b'-1e126', b'-1e127'),
                                   (b'1e-126', b'1e-127'), (b'-1e-125', b'-1e-126')):
            for operation in (lambda c: c.load({}), lambda c: cube_replay._load(c, {}), lambda c: c.meta()):
                with self.subTest(accepted=accepted, operation=operation):
                    body = b'{"data": [{"n": ' + accepted + b'}]}'
                    result = operation(bounded(body))
                    row = result['data'][0] if isinstance(result, dict) else result[0]
                    self.assertEqual(row['n'].as_tuple(), Decimal(accepted.decode()).as_tuple())
                    with self.assertRaises(CubeResponseError) as caught:
                        operation(bounded(b'{"data": [{"n": ' + rejected + b'}]}'))
                    self.assertEqual(compare.error_detail(caught.exception),
                                     {'error': 'CubeResponseError', 'cube': 'invalid_response'})

    def test_aggregate_includes_nested_arrays_and_unmapped_envelope_values(self):
        # Each 1e63 is 64 characters. Two fit; a third exceeds the shared 128 budget.
        allowed = b'{"data": [], "extra": [1e63, {"nested": [1e63]}]}'
        self.assertEqual(bounded(allowed).load({}), [])
        rejected = b'{"data": [], "extra": [1e63, {"nested": [1e63, 1e63]}]}'
        with self.assertRaises(CubeResponseError):
            bounded(rejected).load({})

    def test_zero_extreme_exponents_contribute_one_each_without_formatting(self):
        with mock.patch.object(canon, 'format', side_effect=AssertionError('unexpected formatting'), create=True):
            row, = bounded(b'{"data":[{"n":1e124,"z":[0e999999999,-0e-999999999,0.000]}]}').load({})
            self.assertEqual([canon.canon(value) for value in row['z']], ['0', '0', '0'])
            self.assertEqual([value.as_tuple() for value in row['z']],
                             [Decimal('0e999999999').as_tuple(), Decimal('-0e-999999999').as_tuple(),
                              Decimal('0.000').as_tuple()])
        # 125 characters plus four zeros exceeds 128; zero is not a free value.
        with self.assertRaises(CubeResponseError):
            bounded(b'{"data":[{"n":1e124,"z":[0e999999999,-0e-999999999,0.0,0.0]}]}').load({})

    def test_fraction_digits_sign_and_trailing_zeros_count_without_rounding(self):
        # 120 + 8 = 128; the negative sign makes the second response 129.
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = True
            row, = bounded(b'{"data":[{"a":1e119,"b":123.4500}]}').load({})
            self.assertEqual(row['b'].as_tuple(), Decimal('123.4500').as_tuple())
            with self.assertRaises(CubeResponseError):
                bounded(b'{"data":[{"a":1e119,"b":-123.4500}]}').load({})

    def test_ordinary_precision_integer_and_numeric_string_are_preserved(self):
        body = b'{"data":[{"n":12345678901234567890.123456789012345678900,"i":42,"s":"1e999999999","b":true,"z":null}]}'
        row, = bounded(body).load({})
        self.assertEqual(row['n'].as_tuple(), Decimal('12345678901234567890.123456789012345678900').as_tuple())
        self.assertIs(type(row['i']), int)
        self.assertEqual(row['i'], 42)
        self.assertEqual(row['s'], '1e999999999')
        self.assertIs(row['b'], True)
        self.assertIsNone(row['z'])

    def test_binding_blocks_raw_number_before_fixed_point_formatting(self):
        cfg = demo_cfg()
        gold = gold_of(cfg, 'q04_depth_histogram')
        body = b'{"data":[{"has_component_closure.max_depth":1,"has_component_closure.pairs":1e256}],"private":"private-body"}'
        # Only the formatter is intercepted; actual decoding, load and binding run.
        # The bounded exponent is harmless even if this interceptor is removed.
        def reject_format(value, spec):
            if value == Decimal('1e256'):
                self.fail('oversized raw number reached fixed-point formatting')
            return format(value, spec)

        with mock.patch.object(canon, 'format', reject_format, create=True):
            result = compare.run_binding(FakeConn(cols=['max_depth', 'pairs'], data=[(1, 3)]),
                                         bounded(body, 256), cfg, gold, {})
        self.assertEqual((compare.status(result), result.get('side'), result.get('error'), result.get('cube')),
                         ('error', 'cube', 'CubeResponseError', 'invalid_response'))
        self.assertNotIn('private-body', json.dumps(result))
        self.assertNotIn('1e256', json.dumps(result))

    def test_numeric_rejection_leaves_mutant_unjudged(self):
        cfg = demo_cfg()
        model = demo_model(cfg)
        cfg.verify.mutation_operators = ['closure_depth_shift']
        gold = gold_of(cfg, 'q04_depth_histogram')
        body = b'{"data":[],"private":"private-body","extra":1e256}'
        conn = FakeConn(cols=['max_depth', 'pairs'], data=[(1, 3)])
        suite = stage_verify.mutation_suite([('v1:q04_depth_histogram', lambda client:
            compare.status(compare.run_binding(conn, client, cfg, gold, {})))])

        @contextlib.contextmanager
        def serve(*args, **kwargs):
            yield bounded(body, 256)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mutate, 'witness', return_value=3), \
                mock.patch.object(mutate.cube_run, 'serve', serve):
            checks, _ = mutate.run_mutants(cfg, tmp, None, model, suite, closure={'has_component': 'pass'})
        (_, status, _, detail), = checks
        self.assertEqual((status, detail.get('reason')), ('error', 'suite_error'))
        self.assertNotIn('killed_by', detail)
        self.assertNotIn('private-body', repr(checks))


if __name__ == '__main__':
    unittest.main()
