import json
import unittest
from decimal import Decimal
from types import SimpleNamespace

import pgfixture  # noqa: F401
from semantic_crosscheck import compare
from semantic_crosscheck.cube_client import CubeResponseError
from test_cube_envelope import served


class StrictCubeJsonTest(unittest.TestCase):
    def test_duplicate_keys_at_every_depth_are_invalid_responses(self):
        bodies = [
            b'{"data": [], "data": [{"m.value": 1}]}',
            b'{"data": [{"m.value": 1, "m.value": 2}]}',
            b'{"data": [{"m.value": {"nested": 1, "nested": 2}}]}',
        ]
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(CubeResponseError) as caught:
                    served(body).load({})
                self.assertEqual(compare.error_detail(caught.exception), {"error": "CubeResponseError", "cube": "invalid_response"})
                self.assertNotIn("m.value", str(caught.exception))

    def test_nonfinite_constants_at_any_depth_are_invalid_responses(self):
        for constant in (b'NaN', b'Infinity', b'-Infinity'):
            for prefix, suffix in ((b'{"data": [{"m.value": ', b'}]}'),
                                   (b'{"data": [], "extra": [', b']}')):
                with self.subTest(constant=constant, prefix=prefix):
                    with self.assertRaises(CubeResponseError) as caught:
                        served(prefix + constant + suffix).load({})
                    self.assertEqual(compare.error_detail(caught.exception)["cube"], "invalid_response")

    def test_invalid_json_and_encoding_are_sanitized(self):
        for body in (b'{"secret":', b'{"data": [\xff]}'):
            with self.subTest(body=body), self.assertRaises(CubeResponseError) as caught:
                served(body).load({})
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(compare.error_detail(caught.exception)["cube"], "invalid_response")

    def test_meta_decode_errors_remain_value_errors(self):
        for body in (b'{', b'\xff', b'{"x": 1, "x": 2}', b'{"x": NaN}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                served(body).meta()
        with self.assertRaises(json.JSONDecodeError):
            served(b'{').meta()
        with self.assertRaises(UnicodeDecodeError):
            served(b'\xff').meta()

    def test_json_primitives_and_fractional_precision_are_preserved(self):
        row, = served(b'{"data": [{"int": 42, "frac": 1.2500, "exp": 1e-30, "text": "1.2500", "bool": true, "null": null}]}').load({})
        self.assertEqual(row, {"int": 42, "frac": Decimal('1.2500'), "exp": Decimal('1e-30'),
                               "text": "1.2500", "bool": True, "null": None})
        self.assertIs(type(row['int']), int)
        self.assertIs(type(row['frac']), Decimal)
        self.assertIs(type(row['exp']), Decimal)
        self.assertIs(type(row['text']), str)
        self.assertIs(type(row['bool']), bool)

    def test_huge_decimal_difference_reaches_comparison(self):
        row, = served(b'{"data": [{"m.key": 1, "m.value": 123456789012345678901234567890.00000000000000000001}]}').load({})
        gold = SimpleNamespace(map={"key": "m.key", "value": "m.value"}, key=["key"], zero_if_null=[])
        result = compare.compare_rows(gold, [{"key": 1, "value": Decimal('123456789012345680000000000000')}],
                                      [{"key": row['m.key'], "value": row['m.value']}], {"value": "number"})
        self.assertFalse(result['pass'])
        self.assertEqual(result['mismatched_columns'], {"value": 1})


if __name__ == '__main__':
    unittest.main()
