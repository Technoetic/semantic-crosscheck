"""Real PG17 counterexample/search/replay checks on the throw-away pgfixture only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import counterexample as ce, db


EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'counterexamples'


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class CounterexamplePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_counterexample_pg').setup()
        owner = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(owner):
                owner.run('CREATE SEQUENCE sxc_demo.ce_readonly_probe')
                owner.run('GRANT USAGE ON SEQUENCE sxc_demo.ce_readonly_probe TO xc_check')
        finally:
            owner.close()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sxc_counterexample_pg_')
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)

    def args(self, scenario='nulls', **changes):
        values = dict(reference_sql=str(EXAMPLES / (scenario + '-reference.sql')),
                      candidate_sql=str(EXAMPLES / (scenario + '-candidate.sql')),
                      scenario=scenario, seed=7, max_examples=2, max_shrink_steps=300,
                      statement_timeout_ms=1000, dsn_env='SXC_PG_DSN', replay=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def assert_no_answer_values(self, result):
        document = result.to_dict()
        encoded = json.dumps(document)
        self.assertNotIn('"tables":', encoded)
        self.assertNotIn('"rows":', encoded)
        self.assertNotIn('postgresql://', encoded)

    def test_all_four_example_pairs_shrink_and_replay_actual_sql(self):
        for scenario in ce.SCENARIOS:
            with self.subTest(scenario=scenario):
                directory = self.out / scenario
                args = self.args(scenario)
                result = ce.run(None, args, directory)
                self.assertEqual(result.status, 'fail')
                detail = result.checks[0].detail
                self.assertTrue(detail['found'])
                self.assertLessEqual(detail['minimized_rows'], detail['original_rows'])
                artifact = directory / 'counterexample' / 'fixture.json'
                payload = json.loads(artifact.read_text(encoding='utf-8'))
                self.assertTrue(payload['synthetic'])
                connection = self.fx.check_conn()
                try:
                    answer = ce.evaluate(connection, payload['tables'],
                                         Path(args.reference_sql).read_text(encoding='utf-8'),
                                         Path(args.candidate_sql).read_text(encoding='utf-8'), 1000)
                finally:
                    connection.close()
                self.assertFalse(answer['equal'])
                replay = copy.copy(args)
                replay.replay = str(artifact)
                replay_result = ce.run(None, replay, directory)
                self.assertEqual(replay_result.status, 'fail')
                self.assertEqual(replay_result.checks[0].detail['minimized_rows'], detail['minimized_rows'])
                self.assert_no_answer_values(result)
                self.assert_no_answer_values(replay_result)

    def test_same_sql_is_bounded_not_run_not_equivalence_pass(self):
        for scenario in ce.SCENARIOS:
            with self.subTest(scenario=scenario):
                args = self.args(scenario)
                args.candidate_sql = args.reference_sql
                directory = self.out / scenario
                result = ce.run(None, args, directory)
                self.assertEqual(result.status, 'not_run')
                self.assertEqual(result.checks[0].detail['examples'], 2)
                self.assertTrue(result.not_proven)
                self.assertFalse((directory / 'counterexample' / 'fixture.json').exists())

    def test_write_attempt_is_blocked_by_actual_readonly_transaction(self):
        candidate = self.out / 'write-attempt.sql'
        candidate.write_text("SELECT nextval('sxc_demo.ce_readonly_probe') AS n", encoding='utf-8')
        reference = self.out / 'reference.sql'
        reference.write_text('SELECT 1::bigint AS n', encoding='utf-8')
        args = self.args(reference_sql=str(reference), candidate_sql=str(candidate))
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['sqlstate'], '25006')
        self.assertFalse((self.out / 'counterexample' / 'fixture.json').exists())
        owner = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.read_only(owner):
                called = owner.run('SELECT is_called FROM sxc_demo.ce_readonly_probe')[0][0]
        finally:
            owner.close()
        self.assertFalse(called, 'Rejected SQL must leave the synthetic write probe unchanged')
        self.assert_no_answer_values(result)

    def test_dml_candidate_errors_never_become_saved_counterexamples(self):
        candidate = self.out / 'dml.sql'
        candidate.write_text('UPDATE sxc_demo.stock SET qty = qty + 1 RETURNING qty', encoding='utf-8')
        result = ce.run(None, self.args(candidate_sql=str(candidate)), self.out)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.out / 'counterexample' / 'fixture.json').exists())
        self.assert_no_answer_values(result)

    def test_changed_sql_replay_is_rejected_before_new_connection(self):
        args = self.args()
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'fail')
        artifact = self.out / 'counterexample' / 'fixture.json'
        changed = self.out / 'changed.sql'
        changed.write_text(Path(args.candidate_sql).read_text(encoding='utf-8') + '\n-- changed input\n', encoding='utf-8')
        replay = self.args(candidate_sql=str(changed), replay=str(artifact))
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError):
                ce.run(None, replay, self.out / 'replay')
            connect.assert_not_called()

    def test_non_generated_replay_row_is_rejected_before_connection(self):
        args = self.args()
        self.assertEqual(ce.run(None, args, self.out).status, 'fail')
        artifact = self.out / 'counterexample' / 'fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        payload['tables']['stock'].append(['not-generated', 'not-generated', '1'])
        tampered = self.out / 'tampered.json'
        tampered.write_text(json.dumps(payload), encoding='utf-8')
        args.replay = str(tampered)
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(ce.StageError):
                ce.run(None, args, self.out / 'tampered-replay')
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
