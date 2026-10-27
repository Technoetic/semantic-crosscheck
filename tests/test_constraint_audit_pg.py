"""PostgreSQL-confirmed diagnostics, errors, shrink and replay; synthetic DB only."""
import json
import os
import unittest
from urllib.parse import urlparse, urlunparse

import pgfixture
from semantic_crosscheck import counterexample as ce
from test_constraint_audit import AuditInputs


@unittest.skipUnless(pgfixture.available(), 'requires throw-away PostgreSQL')
class ConstraintAuditPgTest(AuditInputs):
    @classmethod
    def setUpClass(cls):
        cls.database = 'sxc_test_eighth_constraint_audit'
        conn = pgfixture._admin()
        try:
            conn.run('DROP DATABASE IF EXISTS "' + cls.database + '" WITH (FORCE)')
            conn.run('CREATE DATABASE "' + cls.database + '"')
        finally:
            conn.close()
        cls.env = 'SXC_TEST_AUDIT_DSN'
        os.environ[cls.env] = urlunparse(urlparse(os.environ[pgfixture.ADMIN_ENV])._replace(path='/' + cls.database))

    @classmethod
    def tearDownClass(cls):
        os.environ.pop(cls.env, None)
        conn = pgfixture._admin()
        try:
            conn.run('DROP DATABASE "' + cls.database + '" WITH (FORCE)')
        finally:
            conn.close()

    def run_pg(self, **changes):
        return ce.run(None, self.args(dsn_env=self.env, **changes), self.root)

    def test_count_nullable_masked_by_not_null_and_relaxed_replays_twice(self):
        result = self.run_pg()
        self.assertEqual(result.status, 'not_run')
        self.assertFalse(self.check(result, 'declared').detail['found'])
        self.assertTrue(self.check(result, 'relaxed').detail['found'])
        payload = json.loads(self.artifact().read_text())
        self.assertEqual(payload['classification'], 'outside_declared_domain')
        self.assertEqual(len(payload['tables']['items']), 1)
        for _ in range(2):
            replay = self.run_pg(replay=str(self.artifact()))
            self.assertEqual(replay.status, 'not_run')
            self.assertEqual(self.check(replay, 'declared').detail['reason'], 'not_replayed')
            self.assertTrue(self.check(replay, 'relaxed').detail['found'])

    def test_declared_disagreement_and_replay_are_fail(self):
        self.write('candidate.sql', 'SELECT count(qty) + 1 AS n FROM items')
        self.assertEqual(self.run_pg().status, 'fail')
        for _ in range(2):
            self.assertEqual(self.run_pg(replay=str(self.artifact('declared'))).status, 'fail')

    def test_identical_sql_is_bounded_no_witness(self):
        self.write('candidate.sql', (self.root / 'reference.sql').read_text())
        result = self.run_pg()
        self.assertEqual(result.status, 'not_run')
        self.assertTrue(all(not c.detail['found'] for c in result.checks))
        self.assertFalse(self.artifact().exists())

    def test_query_exceptions_are_sanitized_errors(self):
        self.write('candidate.sql', "SELECT 1/0 AS n FROM items")
        result = self.run_pg()
        self.assertEqual(result.status, 'error')
        self.assertFalse(self.artifact().exists())
        self.assertNotIn('division', json.dumps(result.to_dict()))

    def test_minimum_row_bound_and_outside_status_survive_shrinking(self):
        spec = json.loads((self.root / 'schema.json').read_text())
        spec['tables']['items']['rows'] = [2, 2]
        self.write_json('schema.json', spec)
        result = self.run_pg()
        payload = json.loads(self.artifact().read_text())
        self.assertEqual(len(payload['tables']['items']), 2)
        self.assertTrue(any(row[1] is None for row in payload['tables']['items']))
        self.assertGreater(self.check(result, 'relaxed').detail['shrink_invalid_domain_trials'], 0)

    def test_shipped_example_hashes_and_queries_work_together(self):
        example = pgfixture.ROOT / 'examples/constraint-audit'
        for source, target in (('reference.sql', 'reference.sql'), ('candidate.sql', 'candidate.sql'),
                               ('schema.json', 'schema.json'), ('constraints.json', 'constraints.json'),
                               ('manifest.json', 'audit.json')):
            (self.root / target).write_bytes((example / source).read_bytes())
        result = self.run_pg()
        self.assertEqual(result.status, 'not_run')
        self.assertFalse(self.check(result, 'declared').detail['found'])
        self.assertEqual(self.check(result, 'relaxed').detail['classification'], 'outside_declared_domain')


if __name__ == '__main__':
    unittest.main()
