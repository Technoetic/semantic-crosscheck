"""Offline business-contract counterexamples, using synthetic declarations only."""
import copy
import json
import sys
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/semantic-crosscheck/scripts'))
from semantic_crosscheck import business_contracts
from semantic_crosscheck.config import Gold, Measure
from semantic_crosscheck.result import StageError


def fixtures():
    cfg = SimpleNamespace(
        measures={'sales': Measure('sales', 'sales_order', 'amount', 'sum', 'part_id')},
        gold=[Gold('sales_gold', 'unread.sql', ['part_id'],
                   {'measures': ['m_sales.value']}, {'amount': 'm_sales.value'})])
    metric = dict(id='sales', label='Net sales', aliases=['Revenue'], definition='Synthetic sales.',
                  review='reviewed', binding=dict(measure='sales', gold='sales_gold', cube_member='m_sales.value'),
                  context=dict(unit='usd', grain='part_id', population='all_orders', aggregation='sum',
                               denominator='not_applicable', time_basis='order_date', timezone='utc'),
                  period_kind='range')
    selection = dict(metric_id='sales', term='Revenue', binding=copy.deepcopy(metric['binding']),
                     context=copy.deepcopy(metric['context']),
                     period=dict(kind='range', start='2024-02-29', end='2024-03-01'))
    return cfg, {'version': 1, 'metrics': [metric]}, {'version': 1, 'selections': [selection]}


class BusinessContractsTests(unittest.TestCase):
    def run_case(self, mutate=None):
        cfg, contract, plan = fixtures()
        if mutate:
            mutate(cfg, contract, plan)
        return business_contracts.evaluate(cfg, contract, plan)

    def test_reviewed_complete_declarations_pass_without_io(self):
        with patch('builtins.open', side_effect=AssertionError('unexpected IO')), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('unexpected IO')):
            result = self.run_case()
        self.assertEqual((result.stage, result.status, result.exit_code()), ('semantic_preflight', 'pass', 0))
        self.assertTrue(result.not_proven)

    def test_incomplete_selection_requires_clarification(self):
        for key in ('metric_id', 'term', 'binding', 'context', 'period'):
            # Either ID or term alone is enough; removing both is incomplete.
            def mutate(cfg, c, p):
                p['selections'][0].pop(key)
                if key in ('metric_id', 'term'):
                    p['selections'][0].pop('term' if key == 'metric_id' else 'metric_id')
            with self.subTest(key=key):
                result = self.run_case(mutate)
                self.assertEqual(result.status, 'not_run')
                self.assertEqual(result.checks[-1].detail['reason'], 'needs_clarification')

    def test_each_partial_binding_and_context_requires_clarification(self):
        for group, fields in [('binding', ('measure', 'gold', 'cube_member')),
                              ('context', ('unit', 'grain', 'population', 'aggregation', 'denominator', 'time_basis', 'timezone'))]:
            for field in fields:
                with self.subTest(group=group, field=field):
                    self.assertEqual(self.run_case(lambda cfg,c,p: p['selections'][0][group].pop(field)).status, 'not_run')

    def test_every_complete_binding_and_context_disagreement_fails(self):
        _, c, _ = fixtures()
        for group in ('binding', 'context'):
            for field in c['metrics'][0][group]:
                with self.subTest(group=group, field=field):
                    value = 'other.value' if field == 'cube_member' else 'other'
                    self.assertEqual(self.run_case(lambda cfg,c,p: p['selections'][0][group].__setitem__(field, value)).status, 'fail')

    def test_contract_configuration_disagreements_fail_even_unselected(self):
        for mutation in (
            lambda cfg,c,p: cfg.measures.clear(),
            lambda cfg,c,p: cfg.gold.clear(),
            lambda cfg,c,p: c['metrics'][0]['binding'].__setitem__('cube_member', 'm_other.value'),
            lambda cfg,c,p: cfg.gold[0].cube_query['measures'].clear(),
            lambda cfg,c,p: cfg.gold[0].map.clear(),
            lambda cfg,c,p: c['metrics'][0]['context'].__setitem__('aggregation', 'count'),
            lambda cfg,c,p: c['metrics'][0]['context'].__setitem__('grain', 'order_id'),
        ):
            with self.subTest(mutation=mutation):
                self.assertEqual(self.run_case(mutation).status, 'fail')

    def test_candidate_unknown_and_ambiguous_selection_need_clarification(self):
        for mutation in (
            lambda cfg,c,p: c['metrics'][0].__setitem__('review', 'candidate'),
            lambda cfg,c,p: p['selections'][0].__setitem__('metric_id', 'missing'),
            lambda cfg,c,p: p['selections'].__setitem__(0, {'term': 'Unknown'}),
            lambda cfg,c,p: p['selections'].__setitem__(0, {}),
        ):
            with self.subTest(mutation=mutation):
                self.assertEqual(self.run_case(mutation).status, 'not_run')
        cfg,c,p = fixtures()
        second = copy.deepcopy(c['metrics'][0]); second['id'] = 'other'
        c['metrics'].append(second)
        p['selections'][0].pop('metric_id')
        self.assertEqual(business_contracts.evaluate(cfg,c,p).status, 'not_run')
        p['selections'][0]['metric_id'] = 'sales'
        self.assertEqual(business_contracts.evaluate(cfg,c,p).status, 'pass')

    def test_explicit_id_with_misleading_alias_fails(self):
        self.assertEqual(self.run_case(lambda cfg,c,p: p['selections'][0].__setitem__('term', 'Gross sales')).status, 'fail')

    def test_nfc_trim_casefold_and_no_inferred_synonyms(self):
        def normalize(cfg,c,p):
            c['metrics'][0]['label'] = 'Café STRASSE'
            p['selections'][0]['term'] = '  Cafe\u0301 Straße  '
        self.assertEqual(self.run_case(normalize).status, 'pass')
        self.assertEqual(self.run_case(lambda cfg,c,p: p['selections'].__setitem__(0, {'term': 'income'})).status, 'not_run')

    def test_valid_period_modes_and_kind_conflict(self):
        for period in ({'kind':'none'}, {'kind':'as_of','date':'2000-02-29'},
                       {'kind':'range','start':'0001-01-01','end':'9999-12-31'}):
            def mutate(cfg,c,p):
                c['metrics'][0]['period_kind'] = period['kind']
                p['selections'][0]['period'] = period
            self.assertEqual(self.run_case(mutate).status, 'pass')
        self.assertEqual(self.run_case(lambda cfg,c,p: p['selections'][0].__setitem__('period', {'kind':'none'})).status, 'fail')

    def assert_invalid(self, cfg, c, p):
        with self.assertRaises(StageError) as raised:
            business_contracts.evaluate(cfg,c,p)
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertNotIn('SECRET', str(raised.exception))

    def test_malformed_documents_are_safe_errors(self):
        for mutation in (
            lambda c,p: c.__setitem__('version', True),
            lambda c,p: p.__setitem__('version', 1.0),
            lambda c,p: c.__setitem__('SECRET', 1),
            lambda c,p: c.__setitem__('metrics', []),
            lambda c,p: p.__setitem__('selections', []),
            lambda c,p: c.__setitem__('metrics', c['metrics'] * 65),
            lambda c,p: p.__setitem__('selections', p['selections'] * 65),
            lambda c,p: c['metrics'].append(copy.deepcopy(c['metrics'][0])),
            lambda c,p: c['metrics'][0].__setitem__('id', 'SECRET!'),
            lambda c,p: c['metrics'][0].__setitem__('label', ' '),
            lambda c,p: c['metrics'][0].__setitem__('definition', 'x'*2049),
            lambda c,p: c['metrics'][0].__setitem__('aliases', ['a']*17),
            lambda c,p: c['metrics'][0].__setitem__('review', 'SECRET'),
            lambda c,p: c['metrics'][0]['context'].pop('unit'),
            lambda c,p: p['selections'][0].__setitem__('SECRET', 'SECRET'),
            lambda c,p: p['selections'][0].__setitem__('metric_id', None),
            lambda c,p: p['selections'][0].__setitem__('term', 'x'*161),
            lambda c,p: p['selections'][0]['context'].__setitem__('unit', 5),
            lambda c,p: p['selections'][0]['binding'].__setitem__('SECRET', 'SECRET'),
        ):
            cfg,c,p = fixtures(); mutation(c,p)
            with self.subTest(mutation=mutation): self.assert_invalid(cfg,c,p)
        cfg,c,p = fixtures()
        for invalid in (None, [], 'SECRET', 1):
            self.assert_invalid(cfg, invalid, p)
            self.assert_invalid(cfg, c, invalid)

    def test_invalid_calendar_dates_order_and_period_fields_are_errors(self):
        for period in ({}, {'kind':'range','start':'2024-02-30','end':'2024-03-01'},
                       {'kind':'range','start':'2024-03-01','end':'2024-03-01'},
                       {'kind':'range','start':'2024-03-02','end':'2024-03-01'},
                       {'kind':'as_of','date':'2024-2-01'}, {'kind':'as_of','date':True},
                       {'kind':'none','date':'2024-01-01'}, {'kind':'SECRET'}):
            cfg,c,p = fixtures(); p['selections'][0]['period'] = period
            with self.subTest(period=period): self.assert_invalid(cfg,c,p)

    def test_calendar_parser_exception_details_are_suppressed(self):
        cfg,c,p = fixtures()
        p['selections'][0]['period'] = {'kind': 'as_of', 'date': '0000-01-01'}
        try:
            business_contracts.evaluate(cfg,c,p)
        except StageError as error:
            self.assertNotIn('ValueError:', ''.join(traceback.format_exception(error)))
        else:
            self.fail('invalid date accepted')

    def test_result_redacts_all_authored_values(self):
        cfg,c,p = fixtures()
        c['metrics'][0]['label'] = 'SECRET_LABEL'
        c['metrics'][0]['definition'] = 'SECRET_DEFINITION'
        p['selections'][0]['term'] = 'SECRET_TERM'
        result = business_contracts.evaluate(cfg,c,p)
        output = json.dumps(result.to_dict())
        for value in ('SECRET', 'sales_gold', 'm_sales.value', 'all_orders', 'order_date', 'unread.sql'):
            self.assertNotIn(value, output)
        self.assertEqual(result.status, 'fail')

    def test_evaluation_does_not_modify_documents(self):
        cfg,c,p = fixtures(); before = copy.deepcopy((c,p))
        business_contracts.evaluate(cfg,c,p)
        self.assertEqual((c,p), before)

    def test_unselected_invalid_contract_prevents_success(self):
        cfg,c,p = fixtures()
        invalid = copy.deepcopy(c['metrics'][0])
        invalid['id'] = 'unused'
        invalid['context']['aggregation'] = 'count'
        c['metrics'].append(invalid)
        self.assertEqual(business_contracts.evaluate(cfg,c,p).status, 'fail')

    def test_maximum_valid_collections_and_text_are_accepted(self):
        cfg,c,p = fixtures()
        metric = c['metrics'][0]
        metric['label'] = 'L' * 160
        metric['definition'] = 'D' * 2048
        metric['aliases'] = ['A' * 160] * 16
        p['selections'][0]['term'] = metric['label']
        c['metrics'] = [dict(copy.deepcopy(metric), id='metric_' + str(i)) for i in range(64)]
        p['selections'] = [dict(copy.deepcopy(p['selections'][0]), metric_id='metric_' + str(i)) for i in range(64)]
        result = business_contracts.evaluate(cfg,c,p)
        self.assertEqual(result.status, 'pass')
        self.assertEqual(len(result.checks), 128)


if __name__ == '__main__':
    unittest.main()
