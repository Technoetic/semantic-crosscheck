"""Bounded-exhaustive BOM fixtures (scenario bom_exhaustive) for the metamorphic mode, judged by the exact oracle.

No database: the enumeration is checked against an independent brute-force enumeration (a different search space and
canonical form), and the stage runs with the trusted SQL replaced by small Python closures with known semantics:
path-product sum (correct), min over paths, set-semantics UNION and a label-dependent filter (wrong). The oracle must
accept the sum over the whole enumeration, reject the wrong ones at a deterministic case, refuse to pass an incomplete
or narrowed enumeration, and never place answer rows in result.json.
"""
from collections import Counter
import copy
from decimal import Decimal
from fractions import Fraction
import hashlib
import importlib
import itertools
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - puts the package on sys.path
import sxc
from semantic_crosscheck import closure_oracle, constraints, counterexample as ce, metamorphic as mm
from semantic_crosscheck.result import StageError

from test_counterexample_metamorphic import COLUMNS, closure

try:
    bx = importlib.import_module('semantic_crosscheck.bom_exhaustive')
except ModuleNotFoundError:
    bx = None

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'counterexamples'
HOST_COUNTS = {2: 5, 3: 153, 4: 1585}   # the host's independent count: 1,743 structures


# ---- an independent enumeration ---------------------------------------------------------------------------------

def _acyclic(nodes, edges):
    indegree = Counter(child for _parent, child in edges)
    ready, seen = [node for node in nodes if indegree[node] == 0], 0
    while ready:
        node = ready.pop()
        seen += 1
        for parent, child in edges:
            if parent == node:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
    return seen == len(nodes)


def _weakly_connected(nodes, edges):
    nodes = set(nodes)
    seen, stack = set(), [next(iter(nodes))]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack += [b for a, b in edges if a == node] + [a for a, b in edges if b == node]
    return seen == nodes


def _brute_canon(parts, lines):
    """Smallest sorted relabelling over ALL permutations (not only topological numberings)."""
    return min(tuple(sorted((p[a], p[b], q) for a, b, q in lines)) for p in itertools.permutations(range(parts)))


def independent_classes():
    """{parts: {canonical lines}} from multisets over every ordered pair (both directions), filtered afterwards."""
    classes = {}
    for parts in (2, 3, 4):
        alphabet = [(a, b, q) for a in range(parts) for b in range(parts) if a != b for q in (1, 2)]
        found = set()
        for size in range(1, 6):
            for lines in itertools.combinations_with_replacement(alphabet, size):
                if max(Counter((a, b) for a, b, _q in lines).values()) > 2:
                    continue
                edges = {(a, b) for a, b, _q in lines}
                if _weakly_connected(range(parts), edges) and _acyclic(range(parts), edges):
                    found.add(_brute_canon(parts, lines))
        classes[parts] = found
    return classes


def fixture_lines(fixture):
    return [(row[0], row[1], row[3]) for row in fixture['bom_line']]


def relabel_to_numbers(fixture):
    """The fixture's bom lines over node numbers 0..n-1 (ids in sorted order)."""
    ids = sorted({row[0] for row in fixture['bom_line']} | {row[1] for row in fixture['bom_line']})
    number = {identifier: position for position, identifier in enumerate(ids)}
    return len(ids), [(number[a], number[b], int(q)) for a, b, q in fixture_lines(fixture)]


def label_filtered_closure(fixture):
    """A wrong closure that follows only lines whose parent id sorts below the child id."""
    kept = copy.deepcopy(fixture)
    kept['bom_line'] = [row for row in kept['bom_line'] if row[0] < row[1]]
    return closure(kept)


@unittest.skipIf(bx is None, 'checked by ExhaustiveModuleTest')
class EnumerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.independent = independent_classes()

    def test_counts_match_an_independent_enumeration_and_the_host_count(self):
        # Fail loudly: a scope or canonical-form change must not silently shrink the "exhaustive" set.
        self.assertEqual({parts: len(found) for parts, found in self.independent.items()}, HOST_COUNTS)
        mine = Counter(parts for parts, _lines in bx.structures())
        self.assertEqual(dict(mine), HOST_COUNTS)
        self.assertEqual(len(bx.structures()), 1743)
        self.assertEqual(bx.cases_total(), 1743 * 2)
        for parts, found in self.independent.items():
            self.assertEqual({_brute_canon(n, lines) for n, lines in bx.structures() if n == parts}, found)

    def test_no_two_structures_are_isomorphic(self):
        owner = {}
        for index, (parts, lines) in enumerate(bx.structures()):
            for permutation in itertools.permutations(range(parts)):
                image = (parts, tuple(sorted((permutation[a], permutation[b], q) for a, b, q in lines)))
                self.assertEqual(owner.setdefault(image, index), index, 'two structures are relabellings of each other')

    def test_every_case_is_a_connected_acyclic_bom_within_the_scope(self):
        profile = constraints.load(EXAMPLES / 'constraints.json', ce.SCHEMA)
        for index in range(bx.cases_total()):
            fixture, info = bx.case(index)
            lines = fixture_lines(fixture)
            nodes = {a for a, _b, _q in lines} | {b for _a, b, _q in lines}
            edges = {(a, b) for a, b, _q in lines}
            with self.subTest(index=index):
                self.assertTrue(2 <= len(nodes) <= 4 and 1 <= len(lines) <= 5)
                self.assertEqual((info['parts'], info['bom_lines']), (len(nodes), len(lines)))
                self.assertLessEqual(max(Counter((a, b) for a, b, _q in lines).values()), 2)
                self.assertTrue(all(q in ('1', '2') for _a, _b, q in lines))
                self.assertTrue(all(isinstance(x, str) for x in nodes))
                self.assertTrue(_acyclic(nodes, edges) and _weakly_connected(nodes, edges))
                self.assertEqual([row[0] for row in fixture['part']], sorted(nodes) + ['noise'])
                self.assertEqual(fixture['part'][-1], ['noise', 'synthetic', 'unit', 'make', 'synthetic_noise'])
                self.assertEqual(fixture['stock'], [['noise', 'L0', '1']])
                self.assertEqual(fixture['sales_order'], [])
                self.assertEqual([row[2] for row in fixture['bom_line']], list(range(1, len(lines) + 1)))
                constraints.validate_fixture(fixture, ce.SCHEMA, None)
                constraints.validate_fixture(fixture, ce.SCHEMA, profile)

    def test_both_placements_label_the_same_structure_in_opposite_orders(self):
        for structure in range(len(bx.structures())):
            topological, info_a = bx.case(2 * structure)
            reverse, info_b = bx.case(2 * structure + 1)
            with self.subTest(structure=structure):
                self.assertEqual((info_a['placement'], info_b['placement']), ('topological', 'reverse'))
                self.assertEqual(info_a['structure_index'], info_b['structure_index'])
                self.assertTrue(all(a < b for a, b, _q in fixture_lines(topological)), 'parents get the smaller labels')
                self.assertTrue(all(a > b for a, b, _q in fixture_lines(reverse)), 'children get the smaller labels')
                self.assertNotEqual(topological, reverse)
                canonical = _brute_canon(*bx.structures()[structure])
                self.assertEqual(_brute_canon(*relabel_to_numbers(topological)), canonical)
                self.assertEqual(_brute_canon(*relabel_to_numbers(reverse)), canonical)

    def test_enumeration_is_deterministic(self):
        self.assertEqual(bx._enumerate(), bx._enumerate())
        self.assertEqual(bx._enumerate(), bx.structures())
        first = [bx.case(index) for index in range(bx.cases_total())]
        fixture = bx.case(5)[0]
        fixture['bom_line'].append(['X', 'Y', 9, '1'])   # a caller's change never leaks into the next case
        self.assertEqual([bx.case(index) for index in range(bx.cases_total())], first)
        digest = bx.enumeration_sha256()
        self.assertRegex(digest, '^[0-9a-f]{64}$')
        recomputed = hashlib.sha256()
        for fixture, _info in first:
            recomputed.update(json.dumps(fixture, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n')
        self.assertEqual(digest, recomputed.hexdigest())
        for bad in (-1, bx.cases_total(), True, '0'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bx.case(bad)


class ExhaustiveModuleTest(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(bx, 'semantic_crosscheck.bom_exhaustive must exist')

    def test_expected_quantities_helper_exists(self):
        self.assertTrue(hasattr(closure_oracle, 'expected_quantities'))


@unittest.skipUnless(hasattr(closure_oracle, 'expected_quantities'), 'checked by ExhaustiveModuleTest')
class ExpectedQuantitiesTest(unittest.TestCase):
    def test_diamond_sums_path_products(self):
        edges = [('A', 'B', Decimal(2)), ('B', 'D', Decimal(3)), ('A', 'C', Decimal(5)), ('C', 'D', Decimal(7))]
        self.assertEqual(closure_oracle.expected_quantities(edges),
                         {('A', 'B'): 2, ('B', 'D'): 3, ('A', 'C'): 5, ('C', 'D'): 7, ('A', 'D'): 41})

    def test_duplicate_lines_are_summed_and_equal_products_both_count(self):
        edges = [('A', 'B', 1), ('A', 'B', 1), ('B', 'C', Decimal('2'))]
        expected = closure_oracle.expected_quantities(edges)
        self.assertEqual(expected, {('A', 'B'): 2, ('B', 'C'): 2, ('A', 'C'): 4})
        self.assertTrue(all(isinstance(value, Fraction) for value in expected.values()))

    def test_chain_and_exact_decimals(self):
        edges = [('A', 'B', Decimal('0.5')), ('B', 'C', Decimal('0.333333')), ('C', 'D', 3)]
        self.assertEqual(closure_oracle.expected_quantities(edges)[('A', 'D')], Fraction('0.4999995'))
        self.assertEqual(set(closure_oracle.expected_quantities(edges)),
                         {('A', 'B'), ('B', 'C'), ('C', 'D'), ('A', 'C'), ('B', 'D'), ('A', 'D')})

    def test_unsupported_inputs_raise_a_fixed_reason(self):
        for edges, reason in (([('A', 'B', 1), ('B', 'A', 1)], 'cycle'), ([('A', 'A', 1)], 'cycle'),
                              ([('A', 'B', 1.5)], 'unsupported_quantity'),
                              ([('A', 'B', Decimal('NaN'))], 'unsupported_quantity')):
            with self.subTest(reason=reason), self.assertRaises(closure_oracle.OracleUnavailable) as caught:
                closure_oracle.expected_quantities(edges)
            self.assertEqual(caught.exception.reason, reason)
        limits = closure_oracle.Limits(max_pairs=2)
        with self.assertRaises(closure_oracle.OracleUnavailable) as caught:
            closure_oracle.expected_quantities([('A', 'B', 1), ('B', 'C', 1)], limits=limits)
        self.assertEqual(caught.exception.reason, 'budget_exceeded')


@unittest.skipIf(bx is None, 'checked by ExhaustiveModuleTest')
class OracleCompareTest(unittest.TestCase):
    def test_sum_agrees_and_wrong_aggregations_disagree(self):
        fixture = bx.case(0)[0]
        compared, mismatched = mm.oracle_compare(fixture, mm.closure_map(*closure(fixture)))
        self.assertGreater(compared, 0)
        self.assertEqual(mismatched, 0)
        duplicate = next(bx.case(i)[0] for i in range(bx.cases_total())
                         if fixture_lines(bx.case(i)[0]) == [('A', 'B', '1'), ('A', 'B', '1')])
        self.assertEqual(mm.oracle_compare(duplicate, mm.closure_map(*closure(duplicate, 'union'))), (1, 1))
        self.assertEqual(mm.oracle_compare(duplicate, mm.closure_map(*closure(duplicate, 'min'))), (1, 1))

    def test_absent_extra_and_reflexive_pairs(self):
        fixture = bx.case(0)[0]   # A -> B, qty 1
        self.assertEqual(fixture_lines(fixture), [('A', 'B', '1')])
        self.assertEqual(mm.oracle_compare(fixture, {}), (1, 1), 'an absent pair is 0')
        self.assertEqual(mm.oracle_compare(fixture, {('A', 'B'): Decimal('1.000')}), (1, 0), 'exact, not textual')
        self.assertEqual(mm.oracle_compare(fixture, {('A', 'B'): Decimal(1), ('B', 'A'): Decimal(0)}), (2, 0))
        self.assertEqual(mm.oracle_compare(fixture, {('A', 'B'): Decimal(1), ('B', 'A'): Decimal(1)}), (2, 1))
        self.assertEqual(mm.oracle_compare(fixture, mm.closure_map(COLUMNS, [['A', 'B', Decimal(1)], ['A', 'A', Decimal(7)]])),
                         (1, 0), 'reflexive rows are not compared')
        self.assertEqual(mm.oracle_compare(fixture, {('A', 'B'): Decimal('1.0000000000000000000001')}), (1, 1))

    def test_vacuous_fixture_is_not_applicable(self):
        empty = {table: [] for table in ce.SCHEMA}
        with self.assertRaises(mm.NotApplicable) as caught:
            mm.oracle_compare(empty, {})
        self.assertEqual(caught.exception.reason, 'no_bom_lines')


@unittest.skipIf(bx is None, 'checked by ExhaustiveModuleTest')
class ExhaustiveRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.sql = self.path / 'closure.sql'
        self.sql.write_text((EXAMPLES / 'bom_diamond-reference.sql').read_text(encoding='utf-8'), encoding='utf-8')

    def args(self, **changes):
        values = dict(metamorphic_sql=str(self.sql), reference_sql=None, candidate_sql=None, scenario='bom_exhaustive',
                      seed=7, max_examples=None, max_shrink_steps=256, dsn_env='SXC_PG_DSN', replay=None,
                      statement_timeout_ms=1000, constraints=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def run_with(self, semantics, args=None, out=None):
        calls = []

        def answer(_conn, fixture, _sql, _timeout, _profile):
            calls.append(fixture)
            return semantics(fixture)
        with patch.object(mm.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(mm, 'answer', side_effect=answer):
            result = ce.run(None, args or self.args(), out or self.path)
        return result, calls

    def checks(self, result):
        return {check.id: check for check in result.checks}

    def test_reference_semantics_passes_the_whole_enumeration(self):
        result, calls = self.run_with(closure)
        self.assertEqual(result.status, 'pass', [(c.id, c.status, c.detail.get('reason')) for c in result.checks])
        checks = self.checks(result)
        self.assertEqual(set(checks), {mm.ORACLE_ID} | set(mm.CHECK_IDS.values()))
        oracle = checks[mm.ORACLE_ID].detail
        self.assertEqual((oracle['cases_total'], oracle['structures'], oracle['placements']),
                         (3486, 1743, ['topological', 'reverse']))
        self.assertEqual((oracle['cases_run'], oracle['cases_not_run'], oracle['examples']), (3486, 0, 3486))
        self.assertEqual(oracle['mismatched_pairs'], 0)
        self.assertGreater(oracle['pairs_compared'], 3486)
        self.assertEqual(oracle['scenario'], 'bom_exhaustive')
        self.assertEqual(oracle['enumeration_sha256'], bx.enumeration_sha256())
        self.assertEqual(oracle['scope']['max_parts'], 4)
        for name in mm.RELATIONS:
            detail = checks[mm.CHECK_IDS[name]].detail
            self.assertGreater(detail['cases_run'], 0)
            self.assertEqual(detail['mismatched_pairs'], 0)
            self.assertEqual(detail['cases_run'] + detail['cases_not_run'], 3486)
        self.assertEqual(checks[mm.CHECK_IDS['mr3_disjoint']].detail['cases_run'], 3486)
        self.assertEqual((calls.count(bx.case(0)[0]), calls.count(bx.case(1)[0])), (1, 1), 'each case answered once')
        text = ' '.join(result.not_proven)
        for phrase in ('4 parts', '5 bom_line rows', 'qty_per in {1, 2}', 'label placements', 'qty only'):
            self.assertIn(phrase, text)
        self.assertFalse((self.path / 'counterexample/fixture.json').exists())
        encoded = json.dumps(result.to_dict())
        self.assertNotIn('"tables":', encoded)
        self.assertNotIn('synthetic_', encoded)

    def test_union_fails_at_the_first_equal_product_case_and_replays(self):
        result, _ = self.run_with(lambda fixture: closure(fixture, 'union'))
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.exit_code(), 1)
        oracle = self.checks(result)[mm.ORACLE_ID]
        self.assertEqual(oracle.status, 'fail')
        index = oracle.detail['case_index']
        fixture, info = bx.case(index)
        self.assertEqual(fixture_lines(fixture), [('A', 'B', '1'), ('A', 'B', '1')], 'two parallel paths, equal product')
        self.assertEqual((oracle.detail['placement'], oracle.detail['structure_index']), (info['placement'], info['structure_index']))
        self.assertGreater(oracle.detail['case_mismatched_pairs'], 0)
        for index_before in range(index):   # every earlier case agreed
            earlier = bx.case(index_before)[0]
            self.assertEqual(mm.oracle_compare(earlier, mm.closure_map(*closure(earlier, 'union')))[1], 0)
        artifact = self.path / 'counterexample/fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual((payload['format'], payload['scenario'], payload['case_index']), (mm.FORMAT, 'bom_exhaustive', index))
        self.assertEqual((payload['placement'], payload['structure_index']), (info['placement'], info['structure_index']))
        self.assertEqual(payload['enumeration_sha256'], bx.enumeration_sha256())
        self.assertIn('oracle', payload['failed'])
        self.assertEqual(payload['tables'], fixture)
        self.assertEqual(oracle.detail['fixture_sha256'], hashlib.sha256(artifact.read_bytes()).hexdigest())
        replay, calls = self.run_with(lambda f: closure(f, 'union'), self.args(replay=str(artifact)), self.path / 'replay')
        self.assertEqual(replay.status, 'fail')
        self.assertEqual(self.checks(replay)[mm.ORACLE_ID].detail['case_index'], index)
        self.assertEqual(calls[0], fixture)
        fixed, _ = self.run_with(closure, self.args(replay=str(artifact)), self.path / 'fixed')
        self.assertEqual(fixed.status, 'not_run', 'one replayed case never passes an exhaustive check')
        self.assertEqual({c.detail['reason'] for c in fixed.checks}, {'replay_covers_one_case'})

    def test_label_dependent_filter_passes_topological_cases_and_fails_a_reverse_one(self):
        for index in range(0, bx.cases_total(), 2):
            fixture = bx.case(index)[0]
            self.assertEqual(mm.oracle_compare(fixture, mm.closure_map(*label_filtered_closure(fixture)))[1], 0)
        result, _ = self.run_with(label_filtered_closure)
        oracle = self.checks(result)[mm.ORACLE_ID]
        self.assertEqual((result.status, oracle.status), ('fail', 'fail'))
        self.assertEqual(oracle.detail['placement'], 'reverse')

    def test_default_bom_diamond_run_rarely_sees_union(self):
        """The K11 gap the exhaustive scenario closes: the seeded diamonds detect UNION in only a few of 64 cases."""
        detected = 0
        for index in range(64):
            fixture = ce.generate('bom_diamond', 7, index)
            with patch.object(mm, 'answer', side_effect=lambda _c, f, *_a: closure(f, 'union')):
                outcome = mm.check_case(None, fixture, 'SQL', 1000, None, 7, index)
            detected += any(entry['status'] == 'fail' for entry in outcome.values())
        self.assertGreaterEqual(detected, 1)
        self.assertLessEqual(detected, 8)   # seed 7: 2 equal-product originals (unfolding), 4 more via scaling

    def test_incomplete_enumeration_is_not_run_never_a_partial_pass(self):
        target = bx.case(10)[0]

        def bounded(fixture):
            if fixture == target:
                raise mm.SizeBound('SQL result row limit exceeded')
            return closure(fixture)
        result, _ = self.run_with(bounded)
        checks = self.checks(result)
        self.assertEqual(result.status, 'not_run')
        oracle = checks[mm.ORACLE_ID]
        self.assertEqual((oracle.status, oracle.detail['reason']), ('not_run', 'enumeration_incomplete'))
        self.assertEqual(oracle.detail['not_run_reasons'], {'size_bound': 1})
        self.assertEqual(oracle.detail['cases_run'], 3485)
        for name in mm.RELATIONS:
            self.assertNotEqual(checks[mm.CHECK_IDS[name]].status, 'pass', name)

    def test_inadmissible_cases_are_counted_and_profiles_bind(self):
        plain, _ = self.run_with(closure, self.args(constraints=str(EXAMPLES / 'constraints.json')), self.path / 'plain')
        self.assertEqual(plain.status, 'pass')
        detail = self.checks(plain)[mm.ORACLE_ID].detail
        self.assertEqual((detail['invalid_domain_examples'], detail['constraints_mode']), (0, 'constrained'))
        profile = self.path / 'units.json'
        document = json.loads((EXAMPLES / 'constraints.json').read_text(encoding='utf-8'))
        document['checks'].append({'table': 'bom_line', 'column': 'qty_per', 'op': 'eq', 'value': '1'})
        profile.write_text(json.dumps(document), encoding='utf-8')
        units, _ = self.run_with(closure, self.args(constraints=str(profile)), self.path / 'units')
        oracle = self.checks(units)[mm.ORACLE_ID].detail
        admissible = sum(all(q == '1' for _a, _b, q in fixture_lines(bx.case(i)[0])) for i in range(bx.cases_total()))
        self.assertEqual((oracle['cases_run'], oracle['invalid_domain_examples'], oracle['attempted_examples']),
                         (admissible, 3486 - admissible, 3486))
        self.assertEqual(self.checks(units)[mm.ORACLE_ID].status, 'pass')
        self.assertTrue(any('outside the integrity profile' in text for text in units.not_proven))

    def test_max_examples_must_be_omitted_or_cover_every_case(self):
        for value in (1, 64, 1000, 3485):
            with self.subTest(value=value), patch.object(mm.db, 'connect') as connect:
                with self.assertRaises(StageError) as caught:
                    ce.run(None, self.args(max_examples=value), self.path)
                self.assertEqual(caught.exception.exit_code, 2)
                self.assertIn('bom_exhaustive', str(caught.exception))
                connect.assert_not_called()
        result, _ = self.run_with(closure, self.args(max_examples=3486), self.path / 'covering')
        self.assertEqual(self.checks(result)[mm.ORACLE_ID].detail['cases_run'], 3486)

    def test_scenario_is_rejected_outside_the_metamorphic_mode(self):
        comparison = SimpleNamespace(reference_sql=str(self.sql), candidate_sql=str(self.sql), scenario='bom_exhaustive',
                                     seed=7, max_examples=1, max_shrink_steps=1, dsn_env='SXC_PG_DSN', replay=None,
                                     statement_timeout_ms=1000, constraints=None, metamorphic_sql=None)
        mutation = SimpleNamespace(**vars(comparison), mutation_operator='measure_agg_to_max', gold_sql=None)
        for name, args in (('comparison', comparison), ('mutation', mutation)):
            with self.subTest(name=name), patch.object(ce.db, 'connect') as connect, \
                    patch.object(mm.db, 'connect') as metamorphic_connect:
                with self.assertRaises(StageError) as caught:
                    ce.run(None, args, self.path / name)
                self.assertEqual(caught.exception.exit_code, 2)
                connect.assert_not_called()
                metamorphic_connect.assert_not_called()
        with self.assertRaises(SystemExit):
            sxc.parser().parse_args(['counterexample', '--metamorphic-sql', 'x.sql', '--scenario', 'nope'])
        args = sxc.parser().parse_args(['counterexample', '--metamorphic-sql', 'x.sql', '--scenario', 'bom_exhaustive'])
        self.assertEqual((args.scenario, args.max_examples), ('bom_exhaustive', None))

    def test_cli_defaults_keep_64_examples_outside_the_exhaustive_scenario(self):
        seen = []

        def run(cfg, args, out):
            seen.append((args.scenario, args.max_examples))
            from semantic_crosscheck.result import Result
            res = Result('counterexample')
            res.add('counterexample:search', 'not_run', reason='test')
            return res
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sxc.importlib, 'import_module', return_value=SimpleNamespace(run=run)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                sxc.main(['counterexample', '--metamorphic-sql', 'c.sql', '--out', tmp])
                sxc.main(['counterexample', '--metamorphic-sql', 'c.sql', '--scenario', 'bom_exhaustive', '--out', tmp])
                sxc.main(['counterexample', '--reference-sql', 'r.sql', '--candidate-sql', 'c.sql',
                          '--scenario', 'bom_exhaustive', '--out', tmp])
                sxc.main(['counterexample', '--reference-sql', 'r.sql', '--candidate-sql', 'c.sql', '--out', tmp])
                sxc.main(['counterexample', '--mutation-operator', 'measure_agg_to_max', '--reference-sql', 'r.sql',
                          '--candidate-sql', 'c.sql', '--out', tmp])
        self.assertEqual(seen, [('bom_diamond', 64), ('bom_exhaustive', None), ('bom_exhaustive', 64), ('nulls', 64),
                                (None, 64)])

    def test_replay_rejects_a_changed_case_before_connecting(self):
        self.run_with(lambda fixture: closure(fixture, 'union'))
        artifact = self.path / 'counterexample/fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        other = bx.case(payload['case_index'] + 2)[0]
        tampered_rows = copy.deepcopy(payload)
        tampered_rows['bom_line_extra'] = 1
        for name, document in (('rows', dict(payload, tables=other)),
                               ('subset', dict(payload, tables=dict(payload['tables'], bom_line=payload['tables']['bom_line'][:1]))),
                               ('placement', dict(payload, placement='topological' if payload['placement'] == 'reverse' else 'reverse')),
                               ('structure', dict(payload, structure_index=payload['structure_index'] + 1)),
                               ('enumeration', dict(payload, enumeration_sha256='0' * 64)),
                               ('index', dict(payload, case_index=bx.cases_total())),
                               ('scenario', dict(payload, scenario='bom_diamond')),
                               ('failed', dict(payload, failed=['unknown'])),
                               ('extra', tampered_rows)):
            with self.subTest(name=name):
                path = self.path / f'{name}.json'
                path.write_text(json.dumps(document), encoding='utf-8')
                with patch.object(mm.db, 'connect') as connect:
                    with self.assertRaises(StageError) as caught:
                        ce.run(None, self.args(replay=str(path)), self.path / name)
                    connect.assert_not_called()
                self.assertEqual(caught.exception.exit_code, 2)
        with patch.object(mm.db, 'connect') as connect:   # a bom_exhaustive fixture is not a bom_diamond replay
            with self.assertRaises(StageError):
                ce.run(None, self.args(scenario='bom_diamond', max_examples=64, replay=str(artifact)), self.path / 'diamond')
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
