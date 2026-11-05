"""Real PostgreSQL reduction/replay; transforms are deliberately fault-injected."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import db, tlp


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class TlpReductionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_tlp_reduction_pg').setup()
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                conn.run('CREATE FUNCTION sxc_demo.reduction_volatile_value() RETURNS numeric LANGUAGE sql VOLATILE AS $$ SELECT EXTRACT(epoch FROM clock_timestamp())::numeric $$')
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sxc_tlp_red_pg_')
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.path = self.out / 'spec.json'

    def spec(self, oracle='tlp', **changes):
        value = dict(schema_version=1, scenario='nulls',
                     relation_sql='SELECT 0::numeric AS qty FROM stock UNION ALL SELECT 0::numeric FROM stock',
                     predicate_sql='qty > 0', aggregate={'function': 'sum', 'column': 'qty'})
        if oracle == 'norec':
            value.update(relation_sql='SELECT qty FROM stock', aggregate={'function': 'count', 'column': '*'})
        value.update(changes)
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def args(self, **changes):
        value = dict(spec=str(self.path), oracle='tlp', seed=7, max_examples=1,
                     dsn_env='SXC_PG_DSN', statement_timeout_ms=1000, replay=None,
                     shrink=True, max_shrink_steps=200)
        return SimpleNamespace(**dict(value, **changes))

    @staticmethod
    def broken_partition():
        return ('SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p '
                'UNION SELECT * FROM sxc_tlp_evaluated WHERE NOT __sxc_tlp_p '
                'UNION SELECT * FROM sxc_tlp_evaluated WHERE __sxc_tlp_p IS NULL')

    def test_actual_duplicate_disagreement_shrinks_and_v2_round_trips(self):
        self.spec()
        with patch.object(tlp, '_partition_sql', side_effect=self.broken_partition):
            result = tlp.run(None, self.args(), self.out)
            artifact = self.out / 'tlp' / 'fixture.json'
            saved = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(result.status, 'fail')
            self.assertGreater(saved['search']['accepted'], 0)
            self.assertEqual(len(saved['tables']['stock']), 1)
            self.assertEqual(sum(map(len, saved['tables'].values())), 1)
            replayed = tlp.run(None, self.args(replay=str(artifact)), self.out / 'replay')
            self.assertEqual(replayed.status, 'fail')
            self.assertFalse(replayed.checks[0].detail['reduction']['deletion_1_minimal'])
            in_place = tlp.run(None, self.args(replay=str(artifact)), self.out)
            self.assertEqual(in_place.status, 'fail')
        for forbidden in ('SELECT ', 'qty >', str(self.out), '"tables":', '"trace":'):
            self.assertNotIn(forbidden, json.dumps(result.to_dict()))

    def test_actual_norec_direction_shrinks_and_replays(self):
        self.spec('norec')
        with patch.object(tlp, '_norec_reference_sql', return_value='COUNT(*)'):
            result = tlp.run(None, self.args(oracle='norec'), self.out)
            artifact = self.out / 'tlp' / 'fixture.json'
            saved = json.loads(artifact.read_text(encoding='utf-8'))
            self.assertEqual(result.status, 'fail')
            self.assertEqual(saved['signature']['count_relation'], 'less')
            self.assertEqual(sum(map(len, saved['tables'].values())), 1)
            self.assertEqual(tlp.run(None, self.args(oracle='norec', replay=str(artifact)),
                                     self.out / 'replay').status, 'fail')

    def test_actual_final_independent_recheck_cannot_turn_a_lost_failure_into_evidence(self):
        self.spec()
        normal = tlp._partition_sql
        calls = []
        def changed_partition():
            calls.append(1)
            return self.broken_partition() if len(calls) <= 2 else normal()
        with patch.object(tlp, '_partition_sql', side_effect=changed_partition):
            result = tlp.run(None, self.args(max_shrink_steps=0), self.out)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result.status, 'error')
        self.assertFalse((self.out / 'tlp' / 'fixture.json').exists())

    def test_actual_unstable_inputs_errors_and_edited_replay_cannot_establish_witness(self):
        for relation in ('SELECT sxc_demo.reduction_volatile_value() AS qty', 'SELECT 1 / 0 AS qty'):
            self.spec(relation_sql=relation)
            result = tlp.run(None, self.args(), self.out)
            self.assertEqual(result.status, 'error')
            self.assertFalse((self.out / 'tlp' / 'fixture.json').exists())
        self.spec()
        with patch.object(tlp, '_partition_sql', side_effect=self.broken_partition):
            self.assertEqual(tlp.run(None, self.args(), self.out).status, 'fail')
        artifact = self.out / 'tlp' / 'fixture.json'
        saved = json.loads(artifact.read_text(encoding='utf-8'))
        wrong = copy.deepcopy(saved)
        wrong['original']['tables']['stock'][0][2] = '999'
        artifact.write_text(json.dumps(wrong), encoding='utf-8')
        with patch.object(tlp.db, 'connect') as connect, self.assertRaises(tlp.StageError):
            tlp.run(None, self.args(replay=str(artifact)), self.out / 'edited')
        connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
