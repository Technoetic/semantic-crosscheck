"""Declared numeric precision must not depend on the caller's Decimal context."""
from decimal import Decimal, localcontext
import json
from pathlib import Path
import tempfile
import unittest

import pgfixture  # noqa: F401
from semantic_crosscheck import constraints, witness


class NumericDomainBoundaryTest(unittest.TestCase):
    def test_valid_numeric_boundaries_load_exactly_in_every_ambient_context(self):
        cases = [('numeric(29,0)', '9' * 29), ('numeric(38,0)', '9' * 38),
                 ('numeric(38,38)', '0.' + '9' * 38)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'schema.json'
            for precision in (9, 28, 80):
                for kind, value in cases:
                    with self.subTest(precision=precision, kind=kind), localcontext() as ctx:
                        ctx.prec = precision
                        path.write_text(json.dumps({'version': 1, 'tables': {'items': {'rows': [1, 1],
                            'columns': [{'name': 'qty', 'type': kind, 'values': [value]}]}}}), encoding='utf-8')
                        try:
                            spec = witness.load(path)
                        except Exception as error:
                            self.fail('Valid declared numeric boundary rejected: ' + type(error).__name__)
                        self.assertEqual(spec.domains['items'][0][0], Decimal(value))
                        constraints.validate_fixture({'items': [[value]]}, spec.schema)


if __name__ == '__main__':
    unittest.main()
