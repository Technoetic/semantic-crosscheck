"""CLI entry points for independently scoped synthetic coverage checks."""
import contextlib
import io
import unittest

import pgfixture  # noqa: F401 - make the installed CLI importable
import sxc
from semantic_crosscheck import mutate


class NextCliTests(unittest.TestCase):
    def parse(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            try:
                return sxc.parser().parse_args(argv)
            except SystemExit:
                self.fail('CLI rejected a supported synthetic coverage request')

    def test_reason_accepts_explicit_synthetic_witness_request(self):
        args = self.parse(['reason', '--synthetic-witnesses'])
        self.assertTrue(args.synthetic_witnesses)

    def test_synthetic_witnesses_are_opt_in(self):
        self.assertFalse(getattr(self.parse(['reason']), 'synthetic_witnesses', False))

    def test_each_real_mutation_operator_has_a_fixture_entry_point(self):
        for operator in mutate.OPERATORS:
            with self.subTest(operator=operator):
                args = self.parse(['counterexample', '--mutation-operator', operator,
                                   '--reference-sql', 'original.sql', '--candidate-sql', 'mutant.sql',
                                   '--gold-sql', 'gold.sql'])
                self.assertEqual(args.mutation_operator, operator)
                self.assertEqual(args.gold_sql, 'gold.sql')

    def test_unknown_mutation_operator_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            sxc.parser().parse_args(['counterexample', '--mutation-operator', 'nonexistent_operator'])
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
