"""Bounded-exhaustive BOM fixtures (bom_exhaustive) against real PG17 on the throw-away pgfixture only.

The shipped reference closure (sum over paths) must agree with the exact oracle and satisfy the relations over the
whole enumeration; the shipped candidate (min), a set-semantics UNION closure and a label-dependent filter must fail
at a deterministic case that replays. Results hold counts, ids and hashes only.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

import pgfixture
import sxc
from semantic_crosscheck import bom_exhaustive as bx, counterexample as ce, metamorphic as mm
from semantic_crosscheck.result import StageError

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'counterexamples'
REFERENCE = EXAMPLES / 'bom_diamond-reference.sql'
CANDIDATE = EXAMPLES / 'bom_diamond-candidate.sql'
LABEL_FILTER = """WITH RECURSIVE paths(ancestor, descendant, qty, depth) AS (
  SELECT parent_id, child_id, qty_per::numeric, 1 FROM bom_line WHERE parent_id < child_id
  UNION ALL
  SELECT p.ancestor, b.child_id, p.qty * b.qty_per, p.depth + 1
  FROM paths p JOIN bom_line b ON b.parent_id = p.descendant AND b.parent_id < b.child_id
  WHERE p.depth < 16
)
SELECT ancestor, descendant, sum(qty) AS qty
FROM paths GROUP BY ancestor, descendant;
"""


def lines(fixture):
    return [(row[0], row[1], row[3]) for row in fixture['bom_line']]


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class ExhaustivePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_exhaustive_pg_' + str(os.getpid())).setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sxc_exhaustive_pg_')
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)

    def args(self, sql=REFERENCE, **changes):
        values = dict(metamorphic_sql=str(sql), reference_sql=None, candidate_sql=None, scenario='bom_exhaustive', seed=7,
                      max_examples=None, max_shrink_steps=256, statement_timeout_ms=5000, dsn_env='SXC_PG_DSN',
                      replay=None, constraints=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def sql_file(self, name, text):
        path = self.out / name
        path.write_text(text, encoding='utf-8')
        return path

    def checks(self, result):
        return {check.id: check for check in result.checks}

    def assert_no_answer_values(self, result):
        encoded = json.dumps(result.to_dict())
        self.assertNotIn('"tables":', encoded)
        self.assertNotIn('"rows":', encoded)
        self.assertNotIn('synthetic_', encoded)
        self.assertNotIn('postgresql://', encoded)

    def oracle_mismatches(self, connection, sql, fixture):
        return mm.oracle_compare(fixture, mm.closure_map(*mm.answer(connection, fixture, sql, 5000)))[1]

    def test_reference_passes_the_oracle_and_relations_over_the_whole_enumeration(self):
        for profile in (None, str(EXAMPLES / 'constraints.json')):
            with self.subTest(profile=profile):
                directory = self.out / ('constrained' if profile else 'plain')
                started = time.monotonic()
                result = ce.run(None, self.args(constraints=profile), directory)
                elapsed = time.monotonic() - started
                sys.stderr.write(f'\n[bom_exhaustive] reference full run ({"constrained" if profile else "plain"}): '
                                 f'{elapsed:.1f} s\n')
                self.assertEqual(result.status, 'pass', [(c.id, c.status, c.detail.get('reason')) for c in result.checks])
                checks = self.checks(result)
                self.assertEqual(set(checks), {mm.ORACLE_ID} | set(mm.CHECK_IDS.values()))
                oracle = checks[mm.ORACLE_ID].detail
                self.assertEqual((oracle['cases_run'], oracle['cases_total'], oracle['invalid_domain_examples']), (3486, 3486, 0))
                self.assertEqual(oracle['mismatched_pairs'], 0)
                for name in mm.RELATIONS:
                    self.assertGreater(checks[mm.CHECK_IDS[name]].detail['cases_run'], 0)
                    self.assertEqual(checks[mm.CHECK_IDS[name]].detail['mismatched_pairs'], 0)
                self.assertFalse((directory / 'counterexample' / 'fixture.json').exists())
                self.assert_no_answer_values(result)

    def test_min_candidate_fails_the_oracle_and_replays(self):
        result = ce.run(None, self.args(CANDIDATE), self.out)
        oracle = self.checks(result)[mm.ORACLE_ID]
        self.assertEqual((result.status, oracle.status), ('fail', 'fail'))
        fixture = bx.case(oracle.detail['case_index'])[0]
        self.assertEqual(lines(fixture), [('A', 'B', '1'), ('A', 'B', '1')], 'two parallel lines: min keeps one')
        artifact = self.out / 'counterexample' / 'fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual((payload['scenario'], payload['case_index']), ('bom_exhaustive', oracle.detail['case_index']))
        replay = ce.run(None, self.args(CANDIDATE, replay=str(artifact)), self.out / 'replay')
        again = self.checks(replay)[mm.ORACLE_ID]
        self.assertEqual(again.status, 'fail')
        self.assertEqual((again.detail['case_index'], again.detail['case_mismatched_pairs'], again.detail['placement']),
                         (oracle.detail['case_index'], oracle.detail['case_mismatched_pairs'], oracle.detail['placement']))
        self.assertEqual({c.id: c.status for c in replay.checks}, {c.id: c.status for c in result.checks})
        with self.assertRaises(StageError):   # a replay binds the SQL hash: another SQL file is refused
            ce.run(None, self.args(REFERENCE, replay=str(artifact)), self.out / 'fixed')
        self.assert_no_answer_values(result)
        self.assert_no_answer_values(replay)

    def test_union_fails_deterministically_at_an_equal_product_case(self):
        union = self.sql_file('union.sql', REFERENCE.read_text(encoding='utf-8').replace('UNION ALL', 'UNION'))
        first = ce.run(None, self.args(union), self.out / 'first')
        second = ce.run(None, self.args(union), self.out / 'second')
        oracle = self.checks(first)[mm.ORACLE_ID]
        self.assertEqual((first.status, oracle.status), ('fail', 'fail'))
        self.assertEqual(oracle.detail['case_index'], self.checks(second)[mm.ORACLE_ID].detail['case_index'])
        self.assertEqual((self.out / 'first/counterexample/fixture.json').read_bytes(),
                         (self.out / 'second/counterexample/fixture.json').read_bytes())
        fixture = bx.case(oracle.detail['case_index'])[0]
        self.assertEqual(lines(fixture), [('A', 'B', '1'), ('A', 'B', '1')])
        # The textbook diamond with equal products is also caught by the oracle (not only the parallel lines).
        diamond = next(bx.case(i)[0] for i in range(bx.cases_total())
                       if sorted(lines(bx.case(i)[0])) == [('A', 'B', '1'), ('A', 'C', '1'), ('B', 'D', '1'), ('C', 'D', '1')])
        text = union.read_text(encoding='utf-8')
        connection = self.fx.check_conn()
        try:
            self.assertGreater(self.oracle_mismatches(connection, text, diamond), 0)
            self.assertEqual(self.oracle_mismatches(connection, REFERENCE.read_text(encoding='utf-8'), diamond), 0)
            # The K11 gap this scenario closes: the default seeded bom_diamond run sees UNION in only a few of 64 cases.
            detected = []
            for index in range(64):
                outcome = mm.check_case(connection, ce.generate('bom_diamond', 7, index), text, 5000, None, 7, index)
                if any(entry['status'] == 'fail' for entry in outcome.values()):
                    detected.append(index)
        finally:
            connection.close()
        sys.stderr.write(f'\n[bom_diamond] UNION detected in {len(detected)} of 64 seed-7 cases: {detected}\n')
        self.assertGreaterEqual(len(detected), 1)
        self.assertLessEqual(len(detected), 8, detected)
        replay = ce.run(None, self.args(union, replay=str(self.out / 'first/counterexample/fixture.json')), self.out / 'replay')
        self.assertEqual(self.checks(replay)[mm.ORACLE_ID].status, 'fail')

    def test_label_dependent_filter_passes_topological_cases_and_fails_a_reverse_case(self):
        connection = self.fx.check_conn()
        try:
            for index in range(0, bx.cases_total(), 2):
                self.assertEqual(self.oracle_mismatches(connection, LABEL_FILTER, bx.case(index)[0]), 0, index)
        finally:
            connection.close()
        path = self.sql_file('label.sql', LABEL_FILTER)
        result = ce.run(None, self.args(path), self.out)
        oracle = self.checks(result)[mm.ORACLE_ID]
        self.assertEqual((result.status, oracle.status, oracle.detail['placement']), ('fail', 'fail', 'reverse'))
        replay = ce.run(None, self.args(path, replay=str(self.out / 'counterexample' / 'fixture.json')), self.out / 'replay')
        self.assertEqual((self.checks(replay)[mm.ORACLE_ID].status, self.checks(replay)[mm.ORACLE_ID].detail['case_index']),
                         ('fail', oracle.detail['case_index']))

    def test_scenario_is_rejected_outside_the_metamorphic_mode(self):
        for extra in (['--reference-sql', str(REFERENCE), '--candidate-sql', str(CANDIDATE)],
                      ['--mutation-operator', 'measure_agg_to_max', '--reference-sql', str(REFERENCE),
                       '--candidate-sql', str(CANDIDATE)],
                      ['--metamorphic-sql', str(REFERENCE), '--max-examples', '64']):
            with self.subTest(extra=extra), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = sxc.main(['counterexample', *extra, '--scenario', 'bom_exhaustive', '--out', str(self.out / 'cli')])
            self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
