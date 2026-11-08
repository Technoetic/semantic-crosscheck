"""Bridge the bounded offline algebra to actual PostgreSQL on synthetic queries."""

import os
import unittest

import pgfixture
from semantic_crosscheck import db
from test_provenance_fixtures import cases, evaluate, expected_bag, pf


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class ProvenanceFixturesPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_provenance_fixtures_' + str(os.getpid())).setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def test_postgres_and_reference_each_match_the_literal_expected_bags(self):
        self.assertIsNotNone(pf, 'The bounded offline bag reference operations must exist')
        conn = self.fx.check_conn()
        try:
            for case in cases():
                with self.subTest(case=case['id']):
                    with db.read_only(conn, 1000):
                        actual = conn.run(case['sql'])
                    expected = expected_bag(case)
                    self.assertEqual(pf.bag(actual), expected)
                    self.assertEqual(evaluate(case), expected)
        finally:
            conn.close()

    def test_upstream_annotation_support_is_not_a_postgres_difference_bug(self):
        self.assertIsNotNone(pf, 'The bounded offline bag reference operations must exist')
        conn = self.fx.check_conn()
        try:
            prefix = ("WITH r1(part_id) AS (VALUES ('P-A'::text), ('P-A')), "
                      "r2(part_id) AS (VALUES ('P-A'::text)) ")
            for difference in ('EXCEPT', 'EXCEPT ALL'):
                with self.subTest(difference=difference):
                    with db.read_only(conn, 1000):
                        actual = conn.run(prefix + 'SELECT DISTINCT part_id FROM r1 ' +
                                          difference + ' SELECT part_id FROM r2')
                    self.assertEqual(actual, [])
            self.assertEqual(pf.dedup_diff_annotations([['P-A'], ['P-A']], [['P-A']]),
                             {('P-A',): 1})
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
