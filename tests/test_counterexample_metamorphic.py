"""BOM metamorphic relations: gold-independent consistency checks of one trusted closure SQL.

No database: the trusted SQL is replaced by small Python closures with known semantics (path-product sum, min
over paths, and set-semantics UNION). The relations must accept the sum, reject the two wrong aggregations, stay
vacuous (not_run) where a transform has nothing to change, and never place answer rows in result.json.
"""
import contextlib
import copy
from decimal import Decimal, localcontext
import importlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401 - puts the package on sys.path
import sxc
from semantic_crosscheck import constraints, counterexample as ce
from semantic_crosscheck.result import StageError

try:
    mm = importlib.import_module('semantic_crosscheck.metamorphic')
except ModuleNotFoundError:
    mm = None

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'counterexamples'
COLUMNS = ['ancestor', 'descendant', 'qty']


def closure(fixture, mode='sum'):
    """Reference semantics for the tests: every path a->d with the product of its edge quantities.

    sum is the BOM path-product quantity Q(a, d); min keeps the smallest path (the shipped candidate's bug);
    union collapses equal (ancestor, descendant, product, depth) paths like a recursive UNION would.
    """
    edges = [(row[0], row[1], Decimal(str(row[3]))) for row in fixture['bom_line']]
    paths = []

    def walk(ancestor, node, product, depth):
        for parent, child, quantity in edges:
            if parent == node:
                paths.append((ancestor, child, product * quantity, depth + 1))
                walk(ancestor, child, product * quantity, depth + 1)
    with localcontext() as context:
        context.prec = 400   # exact, like PostgreSQL numeric
        for ancestor in sorted({edge[0] for edge in edges}):
            walk(ancestor, ancestor, Decimal(1), 0)
        if mode == 'union':
            paths = sorted(set(paths))
        grouped = {}
        for ancestor, descendant, product, _depth in paths:
            grouped.setdefault((ancestor, descendant), []).append(product)
        aggregate = min if mode == 'min' else sum
        return COLUMNS, [[a, d, aggregate(values)] for (a, d), values in sorted(grouped.items())]


def qmap(fixture, mode='sum'):
    return mm.closure_map(*closure(fixture, mode))


def empty():
    return {table: [] for table in ce.SCHEMA}


def part(identifier):
    return [identifier, 'synthetic', 'unit', 'make', 'synthetic_' + identifier]


def graph(lines, extra_parts=()):
    fixture = empty()
    ids = sorted({line[0] for line in lines} | {line[1] for line in lines} | set(extra_parts))
    fixture['part'] = [part(identifier) for identifier in ids]
    fixture['bom_line'] = [[parent, child, index + 1, quantity] for index, (parent, child, quantity) in enumerate(lines)]
    return fixture


DIAMOND = [('A', 'B', '2'), ('B', 'D', '3'), ('A', 'C', '5'), ('C', 'D', '7')]


@unittest.skipIf(mm is None, 'checked by MetamorphicModuleTest')
class TransformTest(unittest.TestCase):
    def plans(self, fixture, seed=7, case_index=0, **options):
        return mm.transforms(fixture, seed, case_index, **options)

    def holds(self, fixture, name, mode='sum', **options):
        plan = self.plans(fixture, **options)[name]
        self.assertIsInstance(plan, mm.Plan, plan)
        return plan.compare(qmap(fixture, mode), qmap(plan.tables, mode))

    def test_unfolding_copies_shared_subassemblies_into_a_forest(self):
        fixture = graph(DIAMOND + [('D', 'E', '11')])
        plan = self.plans(fixture)['mr1_unfold']
        children = [line[1] for line in plan.tables['bom_line']]
        self.assertEqual(len(children), len(set(children)), 'every instance has exactly one parent')
        copies = dict(plan.spec['copies'])
        self.assertEqual(sorted(copies.values()), ['D', 'E'])
        part_ids = [row[0] for row in plan.tables['part']]
        self.assertEqual(len(part_ids), len(set(part_ids)))
        self.assertLessEqual(set(copies), set(part_ids))
        self.assertTrue(all(identifier not in {'A', 'B', 'C', 'D', 'E'} for identifier in copies))
        self.assertEqual(plan.tables['stock'], fixture['stock'])
        self.assertEqual(fixture, graph(DIAMOND + [('D', 'E', '11')]), 'the original fixture is not modified')

    def test_unfolding_holds_for_path_sums_and_catches_min_and_set_union(self):
        fixture = graph(DIAMOND)
        compared, mismatched = self.holds(fixture, 'mr1_unfold')
        self.assertGreater(compared, 0)
        self.assertEqual(mismatched, 0)
        self.assertGreater(self.holds(fixture, 'mr1_unfold', 'min')[1], 0)
        equal_products = graph([('A', 'B', '2'), ('B', 'D', '3'), ('A', 'C', '3'), ('C', 'D', '2')])
        self.assertEqual(self.holds(equal_products, 'mr1_unfold', 'sum')[1], 0)
        self.assertGreater(self.holds(equal_products, 'mr1_unfold', 'union')[1], 0)
        # Distinct products: set semantics loses nothing, so the relation is (correctly) silent.
        self.assertEqual(self.holds(fixture, 'mr1_unfold', 'union')[1], 0)

    def test_unfolding_expected_value_is_the_sum_over_copies(self):
        fixture = graph(DIAMOND)
        plan = self.plans(fixture)['mr1_unfold']
        original = {('A', 'B'): Decimal(2), ('A', 'C'): Decimal(5), ('A', 'D'): Decimal(41),
                    ('B', 'D'): Decimal(3), ('C', 'D'): Decimal(7)}
        copy_of_d = next(fresh for fresh, source in plan.spec['copies'] if source == 'D')
        good = {('A', 'B'): Decimal(2), ('A', 'C'): Decimal(5), ('B', 'D'): Decimal(3),
                ('C', copy_of_d): Decimal(7), ('A', 'D'): Decimal(6), ('A', copy_of_d): Decimal(35)}
        self.assertEqual(plan.compare(original, good)[1], 0)
        wrong = {**good, ('A', copy_of_d): Decimal(34)}
        self.assertEqual(plan.compare(original, wrong)[1], 1)
        stray = {**good, ('B', copy_of_d): Decimal(1)}
        self.assertEqual(plan.compare(original, stray)[1], 1, 'a path between copies that do not connect is an error')

    def test_scaling_expected_value_and_exact_decimals(self):
        fixture = graph([('A', 'B', '123456789012.345678'), ('B', 'D', '456789012345.678901'), ('A', 'C', '0.333333'), ('C', 'D', '3')])
        plan = self.plans(fixture)['mr2_scale']
        row = plan.spec['bom_line_row']
        self.assertEqual(plan.spec['k'], mm.K)
        u, v, _line, q = fixture['bom_line'][row]
        self.assertEqual(Decimal(plan.tables['bom_line'][row][3]), Decimal(q) * mm.K)
        original = qmap(fixture)
        compared, mismatched = plan.compare(original, qmap(plan.tables))
        self.assertGreater(compared, 0)
        self.assertEqual(mismatched, 0)
        # A float-rounded answer differs in the last digits and must be a mismatch, not tolerated.
        transformed = qmap(plan.tables)
        key = ('A', 'D')
        transformed[key] = Decimal(repr(float(transformed[key])))
        self.assertNotEqual(transformed[key], qmap(plan.tables)[key])
        self.assertEqual(plan.compare(original, transformed)[1], 1)
        # Trailing zeros are a representation, not a value difference (canon number form).
        padded = {pair: Decimal(str(value) + ('0' if '.' in str(value) else '.000')) for pair, value in qmap(plan.tables).items()}
        self.assertEqual(plan.compare(original, padded)[1], 0)

    def test_scaling_formula_on_a_known_edge(self):
        fixture = graph(DIAMOND)
        plan = mm.scale(fixture, 1, k=3)
        u, v, _line, q = fixture['bom_line'][1]   # B -> D, quantity 3
        self.assertEqual((u, v, q), ('B', 'D', '3'))
        expected = qmap(fixture)
        expected[('A', 'D')] += 2 * 3 * 2          # (k - 1) * q * Q(A, B) * Q(D, D)
        expected[('B', 'D')] += 2 * 3              # (k - 1) * q * Q(B, B) * Q(D, D)
        self.assertEqual(qmap(plan.tables), expected)
        self.assertEqual(plan.compare(qmap(fixture), expected)[1], 0)
        self.assertGreater(plan.compare(qmap(fixture), qmap(fixture))[1], 0, 'an unchanged answer violates the scaling')

    def test_scaling_catches_min_aggregation(self):
        found = False
        for seed in range(40):
            fixture = ce.generate('bom_diamond', seed, 0)
            plan = self.plans(fixture, seed=seed)['mr2_scale']
            self.assertEqual(plan.compare(qmap(fixture), qmap(plan.tables))[1], 0)
            found = found or plan.compare(qmap(fixture, 'min'), qmap(plan.tables, 'min'))[1] > 0
        self.assertTrue(found)

    def test_disjoint_component_leaves_original_pairs_unchanged(self):
        fixture = graph(DIAMOND, extra_parts=['sxc_mr3_0'])
        plan = self.plans(fixture)['mr3_disjoint']
        fresh = set(plan.spec['parts'])
        self.assertNotIn('sxc_mr3_0', fresh, 'fresh ids never collide with fixture ids')
        self.assertTrue(fresh.isdisjoint({row[0] for row in fixture['part']}))
        self.assertEqual(len(plan.tables['bom_line']), len(fixture['bom_line']) + 4)
        compared, mismatched = plan.compare(qmap(fixture), qmap(plan.tables))
        self.assertEqual(compared, len(qmap(fixture)))
        self.assertEqual(mismatched, 0)
        leaky = {**qmap(plan.tables), ('A', sorted(fresh)[0]): Decimal(1)}
        self.assertEqual(plan.compare(qmap(fixture), leaky)[1], 1, 'a pair across components must not appear')
        changed = {**qmap(plan.tables), ('A', 'D'): Decimal(1)}
        self.assertEqual(plan.compare(qmap(fixture), changed)[1], 1)

    def test_unit_intermediate_node_expected_values(self):
        fixture = graph(DIAMOND + [('D', 'E', '11')])
        plan = self.plans(fixture)['mr4_unit_node']
        row, w = plan.spec['bom_line_row'], plan.spec['node']
        u, v, line, q = fixture['bom_line'][row]
        self.assertNotEqual(Decimal(q), 1)
        self.assertIn([u, w, line, q], plan.tables['bom_line'])
        self.assertIn([w, v, 1, '1'], plan.tables['bom_line'])
        self.assertNotIn([u, v, line, q], plan.tables['bom_line'])
        self.assertIn(w, {row[0] for row in plan.tables['part']})
        compared, mismatched = plan.compare(qmap(fixture), qmap(plan.tables))
        self.assertEqual(mismatched, 0)
        self.assertGreater(compared, len(qmap(fixture)))
        transformed = qmap(plan.tables)
        transformed[(u, w)] = Decimal(1)  # the quantity carried on the wrong half of the split edge
        self.assertGreater(plan.compare(qmap(fixture), transformed)[1], 0)
        missing = qmap(plan.tables)
        del missing[(w, v)]
        self.assertEqual(plan.compare(qmap(fixture), missing)[1], 1)

    def test_min_passes_the_relations_it_is_invariant_under(self):
        fixture = graph(DIAMOND)
        self.assertEqual(self.holds(fixture, 'mr3_disjoint', 'min')[1], 0)
        self.assertEqual(self.holds(fixture, 'mr4_unit_node', 'min')[1], 0)

    def test_vacuous_fixtures_are_not_applicable_with_reasons(self):
        chain = self.plans(graph([('A', 'B', '2'), ('B', 'C', '3')]))
        self.assertEqual(chain['mr1_unfold'], 'no_diamond')
        self.assertEqual(chain['mr2_scale'], 'no_multi_path_edge')
        self.assertIsInstance(chain['mr3_disjoint'], mm.Plan)
        self.assertIsInstance(chain['mr4_unit_node'], mm.Plan)
        units = self.plans(graph([(a, b, '1') for a, b, _ in DIAMOND]))
        self.assertEqual(units['mr4_unit_node'], 'no_non_unit_quantity')
        self.assertIsInstance(units['mr1_unfold'], mm.Plan)
        for fixture, reason in ((empty(), 'no_bom_lines'), (graph([('A', 'B', '1'), ('B', 'A', '1')]), 'cycle'),
                                (graph([('A', 'A', '2')]), 'cycle')):
            self.assertEqual(set(self.plans(fixture).values()), {reason})
        nulls = graph(DIAMOND)
        nulls['bom_line'][0][3] = None
        self.assertEqual(set(self.plans(nulls).values()), {'null_bom_value'})

    def test_size_bound_makes_unfolding_not_applicable(self):
        lines = []
        for level in range(6):   # six diamonds in series: 64 root paths to the last node
            top, bottom = f'N{level}', f'N{level + 1}'
            lines += [(top, f'L{level}', '1'), (f'L{level}', bottom, '1'), (top, f'R{level}', '1'), (f'R{level}', bottom, '1')]
        fixture = graph(lines)
        self.assertIsInstance(self.plans(fixture)['mr1_unfold'], mm.Plan)
        self.assertEqual(self.plans(fixture, limit=100)['mr1_unfold'], 'size_bound')

    def test_specs_are_deterministic_json_and_hash_bound(self):
        fixture = ce.generate('bom_diamond', 7, 3)
        first, second = self.plans(fixture, seed=7, case_index=3), self.plans(copy.deepcopy(fixture), seed=7, case_index=3)
        for name in mm.RELATIONS:
            self.assertEqual(first[name].spec, second[name].spec)
            self.assertEqual(json.loads(json.dumps(first[name].spec)), first[name].spec)
            self.assertRegex(first[name].sha256, '^[0-9a-f]{64}$')
        self.assertEqual(len({first[name].sha256 for name in mm.RELATIONS}), 4)

    def test_closure_map_shape_errors(self):
        good = [['A', 'B', Decimal(2)]]
        self.assertEqual(mm.closure_map(['depth', 'ancestor', 'descendant', 'qty'], [[1, 'A', 'B', 2]]),
                         {('A', 'B'): Decimal(2)})
        self.assertEqual(mm.closure_map(COLUMNS, good + [['A', 'A', Decimal(1)]]), {('A', 'B'): Decimal(2)},
                         'reflexive rows are not compared')
        for columns, rows, reason in ((['ancestor', 'descendant'], [['A', 'B']], 'missing_columns'),
                                      (['ancestor', 'descendant', 'qty', 'qty'], [['A', 'B', 1, 1]], 'duplicate_columns'),
                                      (COLUMNS, good + [['A', 'B', Decimal(3)]], 'duplicate_pair'),
                                      (COLUMNS, [['A', None, Decimal(1)]], 'null_value'),
                                      (COLUMNS, [['A', 'B', None]], 'null_value'),
                                      (COLUMNS, [['A', 'B', 1.5]], 'non_numeric_qty'),
                                      (COLUMNS, [['A', 'B', '2']], 'non_numeric_qty'),
                                      (COLUMNS, [['A', 'B', True]], 'non_numeric_qty'),
                                      (COLUMNS, [['A', 'B', Decimal('NaN')]], 'non_numeric_qty'),
                                      (COLUMNS, [[1, 'B', Decimal(1)]], 'non_text_id')):
            with self.subTest(reason=reason, rows=rows):
                with self.assertRaises(mm.ShapeError) as caught:
                    mm.closure_map(columns, rows)
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn('A', str(caught.exception))


class MetamorphicModuleTest(unittest.TestCase):
    def test_metamorphic_module_exists(self):
        self.assertIsNotNone(mm, 'semantic_crosscheck.metamorphic must exist')


@unittest.skipIf(mm is None, 'checked by MetamorphicModuleTest')
class MetamorphicRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.sql = self.path / 'closure.sql'
        self.sql.write_text((EXAMPLES / 'bom_diamond-reference.sql').read_text(encoding='utf-8'), encoding='utf-8')

    def args(self, **changes):
        values = dict(metamorphic_sql=str(self.sql), reference_sql=None, candidate_sql=None, scenario='bom_diamond',
                      seed=7, max_examples=3, max_shrink_steps=256, dsn_env='SXC_PG_DSN', replay=None,
                      statement_timeout_ms=1000, constraints=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def run_with(self, mode, args=None, out=None):
        calls = []

        def answer(_conn, fixture, _sql, _timeout, _profile):
            calls.append(fixture)
            return closure(fixture, mode)
        with patch.object(mm.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(mm, 'answer', side_effect=answer):
            result = ce.run(None, args or self.args(), out or self.path)
        return result, calls

    def checks(self, result):
        return {check.id: check for check in result.checks}

    def test_reference_semantics_passes_all_four_relations_with_not_proven(self):
        result, calls = self.run_with('sum')
        self.assertEqual(result.status, 'pass')
        checks = self.checks(result)
        self.assertEqual(set(checks), {mm.CHECK_IDS[name] for name in mm.RELATIONS})
        for name in mm.RELATIONS:
            detail = checks[mm.CHECK_IDS[name]].detail
            self.assertEqual(detail['cases_run'], 3)
            self.assertGreater(detail['pairs_compared'], 0)
            self.assertEqual(detail['mismatched_pairs'], 0)
            self.assertEqual(detail['seed'], 7)
            self.assertRegex(detail['transform_sha256'], '^[0-9a-f]{64}$')
        self.assertEqual(len(calls), 3 * 5, 'one original and four transformed fixtures per case')
        text = ' '.join(result.not_proven)
        self.assertIn('edge direction', text)
        self.assertIn('V1/V3/V5', text)
        self.assertFalse((self.path / 'counterexample/fixture.json').exists())
        encoded = json.dumps(result.to_dict())
        self.assertNotIn('"tables":', encoded)
        self.assertNotIn('synthetic_A', encoded)

    def test_min_aggregation_fails_and_writes_a_replayable_fixture(self):
        result, _ = self.run_with('min')
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.exit_code(), 1)
        checks = self.checks(result)
        self.assertEqual(checks[mm.CHECK_IDS['mr1_unfold']].status, 'fail')
        detail = checks[mm.CHECK_IDS['mr1_unfold']].detail
        self.assertGreater(detail['mismatched_pairs'], 0)
        self.assertEqual(detail['case_index'], 0)
        self.assertEqual(checks[mm.CHECK_IDS['mr4_unit_node']].status, 'pass')
        artifact = self.path / 'counterexample/fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        self.assertEqual(payload['format'], mm.FORMAT)
        self.assertTrue(payload['synthetic'])
        self.assertIn('mr1_unfold', payload['failed'])
        self.assertEqual(set(payload['transforms']), set(mm.RELATIONS))
        self.assertEqual(payload['tables'], ce.generate('bom_diamond', 7, 0))
        self.assertEqual(detail['fixture_sha256'], __import__('hashlib').sha256(artifact.read_bytes()).hexdigest())
        self.assertNotIn('"tables":', json.dumps(result.to_dict()))
        replay, calls = self.run_with('min', self.args(replay=str(artifact)), self.path / 'replay')
        self.assertEqual(replay.status, 'fail')
        self.assertEqual(len(calls), 5)
        self.assertEqual(self.checks(replay)[mm.CHECK_IDS['mr1_unfold']].detail['mismatched_pairs'], detail['mismatched_pairs'])
        fixed, _ = self.run_with('sum', self.args(replay=str(artifact)), self.path / 'fixed')
        self.assertEqual(fixed.status, 'pass', 'replay reruns the relations; it does not replay a recorded verdict')

    def test_union_semantics_fails_on_equal_product_paths(self):
        seed = next(s for s in range(500) if (lambda n: n[0] * n[1] == n[2] * n[3])(
            [int(row[3]) for row in ce.generate('bom_diamond', s, 0)['bom_line']]))
        result, _ = self.run_with('union', self.args(seed=seed, max_examples=1))
        self.assertEqual(self.checks(result)[mm.CHECK_IDS['mr1_unfold']].status, 'fail')
        distinct, _ = self.run_with('union', self.args(seed=7, max_examples=1))
        self.assertEqual(distinct.status, 'pass', 'set semantics only changes answers when equal paths collide')

    def test_replay_rejects_tampered_transforms_and_changed_sql_before_connecting(self):
        self.run_with('min')
        artifact = self.path / 'counterexample/fixture.json'
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        tampered = copy.deepcopy(payload)
        tampered['transforms']['mr2_scale']['k'] = 5
        for name, document in (('transform', tampered),
                               ('rows', dict(payload, tables=dict(payload['tables'], stock=[['X', 'L', '1']]))),
                               ('format', dict(payload, format=ce.FORMAT)),
                               ('failed', dict(payload, failed=['unknown']))):
            with self.subTest(name=name):
                path = self.path / f'{name}.json'
                path.write_text(json.dumps(document), encoding='utf-8')
                with patch.object(mm.db, 'connect') as connect:
                    with self.assertRaises(StageError) as caught:
                        ce.run(None, self.args(replay=str(path)), self.path / name)
                    connect.assert_not_called()
                self.assertEqual(caught.exception.exit_code, 2)
        changed = self.path / 'changed.sql'
        changed.write_text(self.sql.read_text(encoding='utf-8') + '\n-- changed\n', encoding='utf-8')
        with patch.object(mm.db, 'connect') as connect:
            with self.assertRaises(StageError):
                ce.run(None, self.args(metamorphic_sql=str(changed), replay=str(artifact)), self.path / 'changed')
            connect.assert_not_called()
        comparison = self.path / 'comparison'
        with patch.object(ce.db, 'connect') as connect:
            with self.assertRaises(StageError):
                ce.run(None, SimpleNamespace(reference_sql=str(self.sql), candidate_sql=str(self.sql), scenario='bom_diamond',
                                             seed=7, max_examples=1, max_shrink_steps=1, dsn_env='SXC_PG_DSN',
                                             replay=str(artifact), statement_timeout_ms=1000), comparison)
            connect.assert_not_called()

    def test_vacuous_cases_are_not_run_never_pass(self):
        chain = graph([('A', 'B', '2'), ('B', 'C', '3')])
        with patch.object(mm.ce, 'generate', return_value=chain):
            result, _ = self.run_with('sum')
        checks = self.checks(result)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(checks[mm.CHECK_IDS['mr1_unfold']].status, 'not_run')
        self.assertEqual(checks[mm.CHECK_IDS['mr1_unfold']].detail['not_run_reasons'], {'no_diamond': 3})
        self.assertEqual(checks[mm.CHECK_IDS['mr2_scale']].status, 'not_run')
        self.assertEqual(checks[mm.CHECK_IDS['mr3_disjoint']].status, 'pass')
        with patch.object(mm.ce, 'generate', return_value=empty()):
            result, calls = self.run_with('sum', out=self.path / 'empty')
        self.assertEqual({check.status for check in result.checks}, {'not_run'})
        self.assertEqual(calls, [], 'nothing to transform: no SQL runs')

    def test_answer_rows_that_compare_nothing_are_not_run(self):
        def nothing(_conn, _fixture, _sql, _timeout, _profile):
            return COLUMNS, []
        with patch.object(mm.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(mm, 'answer', side_effect=nothing):
            result = ce.run(None, self.args(), self.path)
        checks = self.checks(result)
        self.assertEqual(checks[mm.CHECK_IDS['mr3_disjoint']].status, 'not_run')
        self.assertEqual(checks[mm.CHECK_IDS['mr2_scale']].status, 'fail', 'the scaled edge must appear in the answer')
        self.assertEqual(checks[mm.CHECK_IDS['mr2_scale']].detail['case_index'], 0, 'the search stops at the first violation')
        self.assertEqual(checks[mm.CHECK_IDS['mr3_disjoint']].detail['not_run_reasons'], {'no_pairs': 1})

    def test_integrity_profile_violated_by_a_transform_is_not_run(self):
        profile = self.path / 'profile.json'
        document = json.loads((EXAMPLES / 'constraints.json').read_text(encoding='utf-8'))
        document['tables']['part']['unique'] = [['name']]
        profile.write_text(json.dumps(document), encoding='utf-8')
        result, _ = self.run_with('sum', self.args(constraints=str(profile)))
        checks = self.checks(result)
        self.assertEqual(checks[mm.CHECK_IDS['mr1_unfold']].status, 'not_run')
        self.assertEqual(checks[mm.CHECK_IDS['mr1_unfold']].detail['not_run_reasons'], {'integrity_profile': 3})
        self.assertEqual(checks[mm.CHECK_IDS['mr2_scale']].status, 'pass')
        self.assertEqual(checks[mm.CHECK_IDS['mr2_scale']].detail['constraints_mode'], 'constrained')
        plain, _ = self.run_with('sum', self.args(constraints=str(EXAMPLES / 'constraints.json')), self.path / 'plain')
        self.assertEqual(plain.status, 'pass')

    def test_shape_errors_and_query_errors_are_errors_without_values(self):
        def duplicate(_conn, fixture, _sql, _timeout, _profile):
            return COLUMNS, [['A', 'B', Decimal(1)], ['A', 'B', Decimal(2)]]
        def broken(*_args):
            raise RuntimeError('ROW-VALUE-MUST-NOT-LEAK')
        for name, side_effect, check in (('shape', duplicate, mm.SHAPE_ID), ('query', broken, 'counterexample:query')):
            with self.subTest(name=name):
                with patch.object(mm.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
                     patch.object(mm, 'answer', side_effect=side_effect):
                    result = ce.run(None, self.args(), self.path / name)
                self.assertEqual(result.status, 'error')
                self.assertIn(check, {c.id for c in result.checks})
                self.assertNotIn('ROW-VALUE', json.dumps(result.to_dict()))
                self.assertFalse(any(c.status == 'pass' for c in result.checks))
                self.assertFalse((self.path / name / 'counterexample/fixture.json').exists())

    def test_sql_edit_during_the_run_never_passes(self):
        def answer(_conn, fixture, _sql, _timeout, _profile):
            self.sql.write_text('SELECT 1', encoding='utf-8')
            return closure(fixture)
        with patch.object(mm.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(mm, 'answer', side_effect=answer):
            result = ce.run(None, self.args(), self.path)
        self.assertEqual(result.status, 'error')
        self.assertEqual([c.id for c in result.checks], ['counterexample:inputs'])

    def test_invalid_mode_arguments_are_configuration_errors_before_connecting(self):
        for changes in ({'reference_sql': str(self.sql)}, {'candidate_sql': str(self.sql)}, {'scenario': 'nulls'},
                        {'max_examples': 0}, {'metamorphic_sql': str(self.path / 'missing.sql')}):
            with self.subTest(changes=changes), patch.object(mm.db, 'connect') as connect:
                with self.assertRaises(StageError) as caught:
                    ce.run(None, self.args(**changes), self.path)
                self.assertEqual(caught.exception.exit_code, 2)
                connect.assert_not_called()

    def test_comparison_mode_still_requires_both_sql_files(self):
        for missing in ('reference_sql', 'candidate_sql'):
            args = SimpleNamespace(reference_sql=str(self.sql), candidate_sql=str(self.sql), scenario='nulls', seed=7,
                                   max_examples=1, max_shrink_steps=1, dsn_env='SXC_PG_DSN', replay=None,
                                   statement_timeout_ms=1000)
            setattr(args, missing, None)
            with self.subTest(missing=missing), patch.object(ce.db, 'connect') as connect:
                with self.assertRaises(StageError) as caught:
                    ce.run(None, args, self.path)
                self.assertEqual(caught.exception.exit_code, 2)
                connect.assert_not_called()


class MetamorphicCliTest(unittest.TestCase):
    def test_parser_exposes_metamorphic_mode_without_comparison_files(self):
        args = sxc.parser().parse_args(['counterexample', '--metamorphic-sql', 'closure.sql', '--seed', '3'])
        self.assertEqual((args.metamorphic_sql, args.reference_sql, args.candidate_sql), ('closure.sql', None, None))

    def test_scenario_default_depends_on_the_mode(self):
        seen = []

        def run(cfg, args, out):
            seen.append(args.scenario)
            from semantic_crosscheck.result import Result
            res = Result('counterexample')
            res.add('counterexample:search', 'not_run', reason='test')
            return res
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sxc.importlib, 'import_module', return_value=SimpleNamespace(run=run)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                sxc.main(['counterexample', '--metamorphic-sql', 'closure.sql', '--out', tmp])
                sxc.main(['counterexample', '--reference-sql', 'r.sql', '--candidate-sql', 'c.sql', '--out', tmp])
                sxc.main(['counterexample', '--metamorphic-sql', 'closure.sql', '--scenario', 'nulls', '--out', tmp])
        self.assertEqual(seen, ['bom_diamond', 'nulls', 'nulls'])

    def test_missing_comparison_files_exit_with_configuration_error(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sxc.main(['counterexample', '--reference-sql', 'r.sql', '--out', tmp]), 2)


if __name__ == '__main__':
    unittest.main()
