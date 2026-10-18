"""BOM metamorphic relations against real PG17 on the throw-away pgfixture only.

The shipped reference closure (sum over paths) must satisfy all four relations; the shipped candidate (min over
paths) and a set-semantics UNION closure must violate unfolding; vacuous fixtures stay not_run; a saved violation
replays. Results hold counts, ids and hashes only.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import pgfixture
from semantic_crosscheck import counterexample as ce, metamorphic as mm

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'counterexamples'
REFERENCE = EXAMPLES / 'bom_diamond-reference.sql'
CANDIDATE = EXAMPLES / 'bom_diamond-candidate.sql'


def equal_product_seed():
    """A seed whose first bom_diamond case has two paths A->D with the same product."""
    for seed in range(1000):
        n = [int(row[3]) for row in ce.generate('bom_diamond', seed, 0)['bom_line']]
        if n[0] * n[1] == n[2] * n[3]:
            return seed
    raise AssertionError('no equal-product seed')


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class MetamorphicPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_metamorphic_pg_' + str(os.getpid())).setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sxc_metamorphic_pg_')
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)

    def args(self, sql=REFERENCE, **changes):
        values = dict(metamorphic_sql=str(sql), reference_sql=None, candidate_sql=None, scenario='bom_diamond', seed=7,
                      max_examples=4, max_shrink_steps=256, statement_timeout_ms=5000, dsn_env='SXC_PG_DSN',
                      replay=None, constraints=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def statuses(self, result):
        return {check.id: check.status for check in result.checks}

    def assert_no_answer_values(self, result):
        encoded = json.dumps(result.to_dict())
        self.assertNotIn('"tables":', encoded)
        self.assertNotIn('"rows":', encoded)
        self.assertNotIn('synthetic_', encoded)
        self.assertNotIn('postgresql://', encoded)

    def union_sql(self):
        path = self.out / 'union.sql'
        path.write_text(REFERENCE.read_text(encoding='utf-8').replace('UNION ALL', 'UNION'), encoding='utf-8')
        return path

    def test_reference_sum_closure_passes_all_four_relations(self):
        for profile in (None, str(EXAMPLES / 'constraints.json')):
            with self.subTest(profile=profile):
                directory = self.out / ('constrained' if profile else 'plain')
                result = ce.run(None, self.args(constraints=profile), directory)
                self.assertEqual(result.status, 'pass', [(c.id, c.status, c.detail.get('not_run_reasons')) for c in result.checks])
                self.assertEqual(set(self.statuses(result)), set(mm.CHECK_IDS.values()))
                for check in result.checks:
                    self.assertEqual(check.detail['cases_run'], 4)
                    self.assertGreater(check.detail['pairs_compared'], 0)
                    self.assertEqual(check.detail['mismatched_pairs'], 0)
                self.assertTrue(result.not_proven)
                self.assertFalse((directory / 'counterexample' / 'fixture.json').exists())
                self.assert_no_answer_values(result)

    def test_min_candidate_fails_unfolding_and_replays(self):
        result = ce.run(None, self.args(CANDIDATE), self.out)
        statuses = self.statuses(result)
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.exit_code(), 1)
        self.assertEqual(statuses[mm.CHECK_IDS['mr1_unfold']], 'fail')
        # Scaling may hit an edge of the minimal path, where min and the expected sum agree: either verdict is real.
        self.assertIn(statuses[mm.CHECK_IDS['mr2_scale']], ('pass', 'fail'))
        self.assertEqual(statuses[mm.CHECK_IDS['mr3_disjoint']], 'pass', 'min is invariant under a disjoint component')
        self.assertEqual(statuses[mm.CHECK_IDS['mr4_unit_node']], 'pass', 'min is invariant under a unit intermediate node')
        self.assert_no_answer_values(result)
        artifact = self.out / 'counterexample' / 'fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual(payload['format'], mm.FORMAT)
        self.assertIn('mr1_unfold', payload['failed'])
        self.assertEqual(sorted(payload['failed']), sorted(name for name in mm.RELATIONS if statuses[mm.CHECK_IDS[name]] == 'fail'))
        replay = ce.run(None, self.args(CANDIDATE, replay=str(artifact)), self.out / 'replay')
        self.assertEqual(self.statuses(replay), statuses)
        for name in payload['failed']:
            first = next(c for c in result.checks if c.id == mm.CHECK_IDS[name]).detail
            again = next(c for c in replay.checks if c.id == mm.CHECK_IDS[name]).detail
            self.assertEqual(again['case_mismatched_pairs'], first['case_mismatched_pairs'])
            self.assertEqual(again['transform_sha256'], first['transform_sha256'])
        self.assert_no_answer_values(replay)

    def test_min_candidate_fails_scaling_off_the_minimal_path(self):
        failed = []
        for seed in range(7, 12):
            result = ce.run(None, self.args(CANDIDATE, seed=seed, max_examples=1), self.out / str(seed))
            failed.append(self.statuses(result)[mm.CHECK_IDS['mr2_scale']] == 'fail')
        self.assertTrue(any(failed))

    def test_union_set_semantics_fails_unfolding_on_equal_products(self):
        union = self.union_sql()
        result = ce.run(None, self.args(union, seed=equal_product_seed(), max_examples=1), self.out)
        self.assertEqual(self.statuses(result)[mm.CHECK_IDS['mr1_unfold']], 'fail')
        self.assertEqual(result.status, 'fail')
        distinct = ce.run(None, self.args(union, seed=7, max_examples=1), self.out / 'distinct')
        self.assertEqual(distinct.status, 'pass', 'seed 7 case 0 has distinct path products: UNION loses nothing')

    def test_fixtures_without_a_diamond_are_not_run(self):
        fixture = ce.generate('bom_diamond', 7, 0)
        chain = copy.deepcopy(fixture)
        chain['bom_line'] = [row for row in chain['bom_line'] if row[0] != 'C' and row[1] != 'C']
        connection = self.fx.check_conn()
        try:
            outcome = mm.check_case(connection, chain, CANDIDATE.read_text(encoding='utf-8'), 5000, None, 7, 0)
        finally:
            connection.close()
        self.assertEqual((outcome['mr1_unfold']['status'], outcome['mr1_unfold']['reason']), ('not_run', 'no_diamond'))
        self.assertEqual((outcome['mr2_scale']['status'], outcome['mr2_scale']['reason']), ('not_run', 'no_multi_path_edge'))
        self.assertEqual(outcome['mr3_disjoint']['status'], 'pass')
        self.assertEqual(outcome['mr4_unit_node']['status'], 'pass')

    def test_every_transformed_fixture_is_admissible_under_the_example_profile(self):
        from semantic_crosscheck import constraints
        profile = constraints.load(EXAMPLES / 'constraints.json', ce.SCHEMA)
        for index in range(16):
            plans = mm.transforms(ce.generate('bom_diamond', 7, index), 7, index)
            for name, plan in plans.items():
                if isinstance(plan, mm.Plan):
                    constraints.validate_fixture(plan.tables, ce.SCHEMA, profile)


if __name__ == '__main__':
    unittest.main()
