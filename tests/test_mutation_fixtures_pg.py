"""Real PostgreSQL distinguishes hand-checked pairs for every supported operator."""
import copy
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import constraints, counterexample as ce, db

try:
    mf = importlib.import_module('semantic_crosscheck.mutation_fixtures')
except ModuleNotFoundError:
    mf = None


WALK = '''WITH RECURSIVE walk(ancestor, descendant, depth, qty) AS (
 SELECT parent_id, child_id, 1, qty_per::numeric FROM bom_line
 UNION ALL
 SELECT w.ancestor, b.child_id, w.depth + 1, w.qty * b.qty_per
 FROM walk w JOIN bom_line b ON b.parent_id=w.descendant WHERE w.depth < 8
) '''
PAIRS = {
    'closure_depth_shift': (WALK + "SELECT min(depth) AS value FROM walk WHERE ancestor='A' AND descendant='D'",
                            WALK + "SELECT min(depth)+1 AS value FROM walk WHERE ancestor='A' AND descendant='D'", 2, 3),
    'closure_path_count_one': (WALK + "SELECT count(*) AS value FROM walk WHERE ancestor='A' AND descendant='D' GROUP BY ancestor,descendant",
                               WALK + "SELECT 1::bigint AS value FROM walk WHERE ancestor='A' AND descendant='D' GROUP BY ancestor,descendant", 2, 1),
    'closure_qty_single_path': (WALK + "SELECT sum(qty) AS value FROM walk WHERE ancestor='A' AND descendant='D'",
                                WALK + "SELECT sum(qty)/count(*) AS value FROM walk WHERE ancestor='A' AND descendant='D'", 41, 20.5),
    'measure_filter_drop': ("SELECT sum(qty_open) AS value FROM sales_order WHERE status='FIRM'",
                            'SELECT sum(qty_open) AS value FROM sales_order', 2, 5),
    'measure_normalization_drop': ("SELECT sum(qty_open) AS value FROM sales_order WHERE upper(btrim(status))='FIRM'",
                                   "SELECT sum(qty_open) AS value FROM sales_order WHERE status='FIRM'", 10, 2),
    'measure_agg_to_max': ('SELECT sum(qty) AS value FROM stock', 'SELECT max(qty) AS value FROM stock', 5, 3),
    'dated_shift_one_day': ("SELECT sum(qty_open) AS value FROM sales_order WHERE due_date=DATE '2026-01-01'",
                            "SELECT sum(qty_open) AS value FROM sales_order WHERE due_date + INTERVAL '1 day'=DATE '2026-01-01'", 2, 7),
    'closure_join_swap': ("SELECT s.qty AS value FROM bom_line b JOIN stock s ON s.part_id=b.child_id",
                           "SELECT s.qty AS value FROM bom_line b JOIN stock s ON s.part_id=b.parent_id", 9, 2),
    'closure_end_swap': ("SELECT p.name AS value FROM bom_line b JOIN part p ON p.part_id=b.child_id",
                          "SELECT p.name AS value FROM bom_line b JOIN part p ON p.part_id=b.parent_id", 'synthetic_B', 'synthetic_A'),
}


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class MutationFixturePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_mutation_fixtures_' + str(os.getpid())).setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.assertIsNotNone(mf, 'Operator-directed SQL counterexamples must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def args(self, operator='measure_agg_to_max', gold=True, **changes):
        original, mutant, _, _ = PAIRS[operator]
        for name, sql in [('original', original), ('mutant', mutant)]:
            (self.out / (name + '.sql')).write_text(sql, encoding='utf-8')
        # Authored separately: explicit addition of each stock value, rather than max/sum aggregate rewrite.
        gold_path = self.out / 'gold.sql'
        gold_path.write_text("WITH RECURSIVE totals(n,v) AS (SELECT 0,0::numeric UNION ALL "
                             "SELECT n+1,v+COALESCE((SELECT qty FROM stock ORDER BY part_id,location_id LIMIT 1 OFFSET n),0) "
                             "FROM totals WHERE n < (SELECT count(*) FROM stock)) "
                             "SELECT v AS value FROM totals ORDER BY n DESC LIMIT 1", encoding='utf-8')
        values = dict(mutation_operator=operator, gold_sql=str(gold_path) if gold else None,
                      reference_sql=str(self.out / 'original.sql'), candidate_sql=str(self.out / 'mutant.sql'),
                      metamorphic_sql=None, scenario=None, seed=7, max_examples=2, max_shrink_steps=300,
                      statement_timeout_ms=1000, dsn_env='SXC_PG_DSN', replay=None, constraints=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_each_real_pair_distinguishes_with_hand_checked_literal_answers(self):
        conn = self.fx.check_conn()
        try:
            for operator, (original, mutant, expected_original, expected_mutant) in PAIRS.items():
                with self.subTest(operator=operator):
                    fixture = mf.generate(operator, 7, 0)
                    for sql, expected in [(original, expected_original), (mutant, expected_mutant)]:
                        with db.read_only(conn):
                            wrapped, params = ce.wrap_sql(sql, fixture)
                            self.assertEqual(conn.run(wrapped, **params), [[expected]])
                    answer = mf.evaluate(conn, fixture, original, mutant, None, 1000)
                    self.assertFalse(answer['original_mutant']['equal'])
        finally:
            conn.close()

    def test_every_operator_search_shrinks_rechecks_and_replays_without_gold(self):
        for operator in PAIRS:
            with self.subTest(operator=operator):
                args = self.args(operator, gold=False)
                directory = self.out / operator
                result = ce.run(None, args, directory)
                detail = result.checks[0].detail
                self.assertEqual(result.status, 'fail')
                self.assertTrue(detail['synthetic_distinguished'])
                self.assertFalse(detail['gold_observes'])
                self.assertEqual(detail['gold_observation'], 'not_proven')
                self.assertTrue(detail['synthetic_only'])
                artifact = directory / 'counterexample/fixture.json'
                payload = json.loads(artifact.read_text(encoding='utf-8'))
                self.assertEqual(payload['mutation_operator'], operator)
                self.assertTrue(payload['shrink']['deletion_1_minimal'])
                replay_args = copy.copy(args); replay_args.replay = str(artifact)
                replayed = ce.run(None, replay_args, directory / 'replay')
                self.assertEqual(replayed.status, 'fail')
                replay_payload = json.loads((directory / 'replay/counterexample/fixture.json').read_text(encoding='utf-8'))
                self.assertEqual(replay_payload['tables'], payload['tables'])
                self.assertEqual(replay_payload['sql_sha256'], payload['sql_sha256'])
                encoded = json.dumps(result.to_dict())
                self.assertNotIn('synthetic_A', encoded)
                self.assertNotIn('"tables"', encoded)
                self.assertNotIn('postgresql://', encoded)

    def test_gold_observation_requires_agreement_and_is_bound_to_replay(self):
        args = self.args(constraints=str(pgfixture.ROOT / 'examples/counterexamples/constraints.json'))
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'fail')
        self.assertTrue(result.checks[0].detail['gold_observes'])
        payload = json.loads((self.out / 'counterexample/fixture.json').read_text(encoding='utf-8'))
        self.assertIn('gold', payload['sql_sha256'])
        self.assertEqual(payload['constraints']['mode'], 'constrained')
        profile = constraints.load(args.constraints, ce.SCHEMA)
        conn = self.fx.check_conn()
        try:
            for table, rows in payload['tables'].items():
                for index in range(len(rows)):
                    trial = copy.deepcopy(payload['tables']); del trial[table][index]
                    try:
                        constraints.validate_fixture(trial, ce.SCHEMA, profile)
                    except constraints.ConstraintViolation:
                        continue
                    answer = mf.evaluate(conn, trial, Path(args.reference_sql).read_text(encoding='utf-8'),
                                         Path(args.candidate_sql).read_text(encoding='utf-8'),
                                         Path(args.gold_sql).read_text(encoding='utf-8'), 1000, profile)
                    self.assertTrue(answer['original_mutant']['equal'] or not answer['original_gold']['equal'])
        finally:
            conn.close()

    def test_replay_in_the_same_output_reads_the_input_before_replacing_it(self):
        args = self.args(constraints=str(pgfixture.ROOT / 'examples/counterexamples/constraints.json'))
        first = ce.run(None, args, self.out)
        self.assertTrue(first.checks[0].detail['gold_observes'])
        artifact = self.out / 'counterexample/fixture.json'
        original_tables = json.loads(artifact.read_text(encoding='utf-8'))['tables']
        args.replay = str(artifact)
        try:
            repeated = ce.run(None, args, self.out)
        except ce.StageError:
            self.fail('Replaying into the same output must not delete its replay input before reading it')
        self.assertTrue(repeated.checks[0].detail['synthetic_distinguished'])
        self.assertTrue(repeated.checks[0].detail['gold_observes'])
        self.assertEqual(json.loads(artifact.read_text(encoding='utf-8'))['tables'], original_tables)

    def test_public_cli_operator_mode_does_not_inject_a_legacy_scenario(self):
        args = self.args()
        completed = subprocess.run(
            [sys.executable, str(pgfixture.SCRIPTS / 'sxc.py'), 'counterexample',
             '--mutation-operator', args.mutation_operator, '--reference-sql', args.reference_sql,
             '--candidate-sql', args.candidate_sql, '--gold-sql', args.gold_sql,
             '--out', str(self.out), '--max-examples', '1', '--quiet'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 1, 'A synthetic distinction must be reachable through the public CLI')
        saved = json.loads((self.out / 'counterexample/result.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['checks'][0]['detail']['synthetic_distinguished'])
        self.assertTrue(saved['checks'][0]['detail']['gold_observes'])
        args.replay = str(self.out / 'counterexample/fixture.json')
        Path(args.gold_sql).write_text('SELECT 5::numeric AS value', encoding='utf-8')
        with patch.object(ce.db, 'connect', side_effect=AssertionError('must not connect')):
            with self.assertRaises(ce.StageError):
                ce.run(None, args, self.out / 'changed-gold')

    def test_gold_disagreement_is_explicit_and_never_an_observation(self):
        args = self.args()
        Path(args.gold_sql).write_text('SELECT -123::numeric AS value', encoding='utf-8')
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'fail')
        self.assertFalse(result.checks[0].detail['gold_observes'])
        self.assertEqual(result.checks[0].detail['gold_observation'], 'original_gold_disagree')
        self.assertEqual(result.checks[1].id, 'counterexample:mutation:gold_agreement')
        self.assertEqual(result.checks[1].status, 'fail')

    def test_identical_sql_and_invalid_domains_never_pass_or_emit_witness(self):
        args = self.args(gold=False)
        args.candidate_sql = args.reference_sql
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'not_run')
        self.assertFalse((self.out / 'counterexample/fixture.json').exists())
        args = self.args(gold=False)
        path = self.out / 'invalid.json'
        path.write_text(json.dumps({'version': 1, 'tables': {}, 'checks': [{'table': 'stock', 'column': 'qty', 'op': 'lt', 'value': '0'}]}), encoding='utf-8')
        args.constraints = str(path)
        with patch.object(ce.db, 'connect', side_effect=AssertionError('invalid fixtures must not execute')):
            result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['invalid_domain_examples'], 2)
        self.assertFalse((self.out / 'counterexample/fixture.json').exists())

    def test_query_error_never_becomes_detection_and_output_has_no_sql_values(self):
        args = self.args()
        Path(args.candidate_sql).write_text("SELECT 1/0 AS value, 'ROW-MUST-NOT-LEAK' AS secret", encoding='utf-8')
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'error')
        self.assertFalse(result.checks[0].detail['synthetic_distinguished'])
        self.assertFalse(result.checks[0].detail['gold_observes'])
        self.assertFalse((self.out / 'counterexample/fixture.json').exists())
        self.assertNotIn('ROW-MUST-NOT-LEAK', json.dumps(result.to_dict()))

    def test_unsupported_sql_schema_is_explicit_not_run(self):
        for sql in ['SELECT not_a_fixture_column FROM stock', 'SELECT * FROM not_a_fixture_table']:
            with self.subTest(sql=sql):
                args = self.args(gold=False)
                Path(args.candidate_sql).write_text(sql, encoding='utf-8')
                result = ce.run(None, args, self.out)
                self.assertEqual(result.status, 'not_run')
                self.assertEqual(result.checks[0].detail['reason'], 'unsupported_sql_schema')
                self.assertFalse(result.checks[0].detail['synthetic_distinguished'])
                self.assertFalse(result.checks[0].detail['gold_observes'])
                self.assertFalse((self.out / 'counterexample/fixture.json').exists())

    def test_gold_query_errors_never_publish_a_distinguishing_fixture(self):
        args = self.args()
        Path(args.gold_sql).write_text('SELECT 1/0 AS value', encoding='utf-8')
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.checks[0].detail['distinguished_examples'], 0)
        self.assertEqual(result.checks[0].detail['gold_observed_examples'], 0)
        self.assertFalse((self.out / 'counterexample/fixture.json').exists())

    def test_final_recheck_error_clears_unconfirmed_detection_counters(self):
        args = self.args(max_shrink_steps=0)
        real_evaluate = mf.evaluate
        calls = 0

        def first_answer_then_error(*values, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError('recheck failed')
            return real_evaluate(*values, **kwargs)

        with patch.object(mf, 'evaluate', side_effect=first_answer_then_error):
            result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'error')
        self.assertFalse(result.checks[0].detail['synthetic_distinguished'])
        self.assertEqual(result.checks[0].detail['distinguished_examples'], 0)
        self.assertEqual(result.checks[0].detail['gold_observed_examples'], 0)
        self.assertFalse((self.out / 'counterexample/fixture.json').exists())

    def test_committed_example_triplet_proves_gold_observation_on_real_postgres(self):
        args = self.args()
        examples = pgfixture.ROOT / 'examples/counterexamples'
        args.reference_sql = str(examples / 'mutation_agg-original.sql')
        args.candidate_sql = str(examples / 'mutation_agg-mutant.sql')
        args.gold_sql = str(examples / 'mutation_agg-gold.sql')
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'fail')
        self.assertTrue(result.checks[0].detail['gold_observes'])
        self.assertEqual(result.checks[1].status, 'pass')

    def test_mutation_query_cannot_advance_even_disposable_database_sequence(self):
        owner = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(owner):
                owner.run('CREATE SEQUENCE sxc_demo.mutation_readonly_probe')
                owner.run('GRANT USAGE ON SEQUENCE sxc_demo.mutation_readonly_probe TO xc_check')
            args = self.args(gold=False)
            Path(args.candidate_sql).write_text("SELECT nextval('sxc_demo.mutation_readonly_probe') AS value", encoding='utf-8')
            result = ce.run(None, args, self.out)
            self.assertEqual(result.status, 'error')
            self.assertEqual(result.checks[0].detail['sqlstate'], '25006')
            self.assertFalse(result.checks[0].detail['synthetic_distinguished'])
            self.assertFalse((self.out / 'counterexample/fixture.json').exists())
            with db.read_only(owner):
                self.assertFalse(owner.run('SELECT is_called FROM sxc_demo.mutation_readonly_probe')[0][0])
        finally:
            owner.close()


if __name__ == '__main__':
    unittest.main()
