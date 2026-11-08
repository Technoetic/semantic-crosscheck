"""Actual PostgreSQL AST comparisons; optional parser is never faked by regex."""
import importlib.util
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import sql_structure


SQL = 'SELECT part_id, sum(qty) FROM sxc_demo.sales_order WHERE ordered_at >= $1 AND ordered_at < $2 GROUP BY part_id'


def expectation():
    return {'version': 1, 'relation': {'schema': 'sxc_demo', 'name': 'sales_order'},
            'select': [{'column': 'part_id'}, {'aggregate': 'sum', 'column': 'qty'}],
            'group_by': ['part_id'], 'where': [
                {'column': 'ordered_at', 'op': '>=', 'value': {'parameter': 1}},
                {'column': 'ordered_at', 'op': '<', 'value': {'parameter': 2}}]}


class EnvironmentTest(unittest.TestCase):
    def test_missing_parser_never_passes(self):
        with patch.object(sql_structure, '_parser', return_value=None):
            result = sql_structure.evaluate(SQL, expectation())
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'parser_unavailable')

    def test_actual_missing_import_and_incompatible_parser_are_unavailable(self):
        with patch.dict(sys.modules, {'pglast': None}):
            self.assertIsNone(sql_structure._parser())
        for version, pg_version in [('v8.7', (18, 3)), ('v7.21', (17, 7)), ('v7.22', (18, 3))]:
            fake = SimpleNamespace(__version__=version, parser=SimpleNamespace(
                get_postgresql_version=lambda: pg_version))
            with self.subTest(version=version, postgres=pg_version), patch.dict(sys.modules, {'pglast': fake}):
                self.assertIsNone(sql_structure._parser())


@unittest.skipUnless(importlib.util.find_spec('pglast'), 'optional PostgreSQL AST parser not installed')
class SqlStructureTest(unittest.TestCase):
    def evaluate(self, sql=SQL, expected=None):
        return sql_structure.evaluate(sql, expectation() if expected is None else expected)

    def test_real_parser_matches_explicit_structure_and_redacts(self):
        result = self.evaluate()
        self.assertEqual(result.status, 'pass')
        self.assertEqual([c.id for c in result.checks], ['relation', 'selection', 'grouping', 'predicates'])
        raw = json.dumps(result.to_dict())
        for private in ('sales_order', 'ordered_at', 'part_id', 'qty', SQL):
            self.assertNotIn(private, raw)
        self.assertTrue(result.not_proven)

    def test_wrong_aggregate_grain_population_and_predicate_fail(self):
        for sql in (SQL.replace('sum(qty)', 'count(qty)'),
                    SQL.replace('GROUP BY part_id', 'GROUP BY ordered_at'),
                    SQL.replace('sales_order', 'stock'),
                    SQL.replace('ordered_at < $2', 'ordered_at <= $2'),
                    SQL.replace('ordered_at < $2', 'ordered_at < $3')):
            with self.subTest(sql=sql):
                self.assertEqual(self.evaluate(sql).status, 'fail')

    def test_between_cannot_match_half_open_range(self):
        sql = SQL.replace('ordered_at >= $1 AND ordered_at < $2', 'ordered_at BETWEEN $1 AND $2')
        self.assertEqual(self.evaluate(sql).status, 'fail')
        doc = expectation()
        doc['where'] = [{'column': 'ordered_at', 'op': 'between',
                         'lower': {'parameter': 1}, 'upper': {'parameter': 2}}]
        self.assertEqual(self.evaluate(sql, doc).status, 'pass')

    def test_predicate_conjunction_order_is_irrelevant(self):
        sql = SQL.replace('ordered_at >= $1 AND ordered_at < $2', 'ordered_at < $2 AND ordered_at >= $1')
        self.assertEqual(self.evaluate(sql).status, 'pass')

    def test_literals_are_typed_and_never_returned(self):
        doc = {'version': 1, 'relation': {'name': 'stock'}, 'select': [{'column': 'qty'}],
               'group_by': [], 'where': [{'column': 'qty', 'op': '=', 'value': {'literal': 'private-canary'}}]}
        result = self.evaluate("SELECT qty FROM stock WHERE qty = 'private-canary'", doc)
        self.assertEqual(result.status, 'pass')
        self.assertNotIn('private-canary', json.dumps(result.to_dict()))
        doc['where'][0]['value'] = {'literal': True}
        self.assertEqual(self.evaluate('SELECT qty FROM stock WHERE qty = 1', doc).status, 'fail')

    def test_supported_zero_negative_and_count_star(self):
        doc = {'version': 1, 'relation': {'name': 'stock'}, 'select': [{'aggregate': 'count', 'column': '*'}],
               'group_by': [], 'where': [{'column': 'qty', 'op': '>=', 'value': {'literal': -1}},
                                      {'column': 'qty', 'op': '<>', 'value': {'literal': 0}}]}
        self.assertEqual(self.evaluate('SELECT count(*) FROM stock WHERE qty >= -1 AND qty <> 0', doc).status, 'pass')

    def test_unrecognized_sql_is_not_run_never_partial_pass(self):
        statements = [
            'WITH s AS (SELECT * FROM stock) SELECT qty FROM s',
            'SELECT qty FROM stock s', 'SELECT s.qty FROM stock s',
            'SELECT qty AS amount FROM stock', 'SELECT qty FROM stock JOIN part USING (part_id)',
            'SELECT qty::int FROM stock', 'SELECT qty FROM stock WHERE qty = 1 OR qty = 2',
            'SELECT qty FROM stock ORDER BY qty', 'SELECT qty FROM stock LIMIT 1',
            'SELECT DISTINCT qty FROM stock', 'SELECT sum(DISTINCT qty) FROM stock',
            'SELECT sum(qty) FILTER (WHERE qty > 0) FROM stock',
            'SELECT sum(qty) OVER () FROM stock', 'SELECT qty FROM ONLY stock',
            'SELECT qty FROM stock UNION SELECT qty FROM stock',
            'SELECT qty FROM stock; DELETE FROM stock', 'SELECT qty FROM stock WHERE qty IS NULL',
            "SELECT qty FROM stock WHERE qty = 1.25", 'SELECT qty FROM stock GROUP BY 1',
            "SELECT qty FROM stock WHERE qty = '2026-01-01'::date",
        ]
        for sql in statements:
            with self.subTest(sql=sql):
                result = self.evaluate(sql)
                self.assertEqual(result.status, 'not_run')
                self.assertFalse(any(c.status == 'pass' for c in result.checks))

    def test_invalid_syntax_is_sanitized_not_run(self):
        result = self.evaluate('SELECT private-canary FROM')
        self.assertEqual(result.status, 'not_run')
        self.assertNotIn('private-canary', json.dumps(result.to_dict()))

    def test_incomplete_expectation_requires_clarification(self):
        doc = expectation()
        del doc['where']
        result = self.evaluate(expected=doc)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'needs_clarification')

    def test_unknown_or_ill_typed_expectation_is_invalid(self):
        cases = []
        for field, value in [('version', True), ('group_by', 'part_id'), ('where', None),
                             ('select', []), ('unexpected', 'private-canary')]:
            doc = expectation()
            doc[field] = value
            cases.append(doc)
        doc = expectation()
        doc['where'][0]['value']['parameter'] = True
        cases.append(doc)
        for doc in cases:
            with self.subTest(doc=doc):
                self.assertEqual(self.evaluate(expected=doc).status, 'error')

    def test_bounded_sql_and_expectation(self):
        self.assertEqual(self.evaluate(SQL + ' ' * (64 * 1024)).status, 'error')
        doc = expectation()
        doc['where'] = doc['where'] * 10001
        self.assertEqual(self.evaluate(expected=doc).status, 'error')
