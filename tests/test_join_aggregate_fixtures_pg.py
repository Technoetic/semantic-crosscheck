"""Actual PG17 queries and incorrect query pairs share independently authored literals."""

import os
import unittest

import pgfixture
from semantic_crosscheck import db
from test_join_aggregate_fixtures import bag, cases, evaluate, expected_bag, jf


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class JoinAggregateFixturesPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_join_aggregate_' + str(os.getpid())).setup()
        conn = cls.fx.check_conn()
        try:
            with db.read_only(conn, 1000):
                version = int(conn.run('SHOW server_version_num')[0][0])
            if not 170000 <= version < 180000:
                raise AssertionError('The join/aggregate bridge requires PostgreSQL 17')
        except BaseException:
            cls.fx.teardown()
            raise
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def test_postgres_and_offline_arithmetic_each_match_the_literal_cases(self):
        self.assertIsNotNone(jf, 'The bounded join/aggregate arithmetic must exist')
        conn = self.fx.check_conn()
        try:
            for case in cases():
                with self.subTest(case=case['id']):
                    with db.read_only(conn, 1000):
                        actual = bag(conn.run(case['sql']))
                    self.assertEqual(actual, expected_bag(case))
                    self.assertEqual(bag(evaluate(case)), expected_bag(case))
        finally:
            conn.close()

    def test_postgres_wrong_query_pairs_distinguish_all_authored_mutants(self):
        conn = self.fx.check_conn()
        try:
            for case in cases():
                for mutant in case.get('mutants', []):
                    with self.subTest(case=case['id'], mutant=mutant['operation']):
                        with db.read_only(conn, 1000):
                            correct = bag(conn.run(case['sql']))
                            wrong = bag(conn.run(mutant['sql']))
                        self.assertEqual(correct, expected_bag(case))
                        self.assertEqual(wrong, expected_bag(mutant))
                        self.assertNotEqual(wrong, correct)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
