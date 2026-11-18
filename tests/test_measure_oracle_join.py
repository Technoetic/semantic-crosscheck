"""An invoice amount belongs to one fact, even when several lines refer to it."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest

import pgfixture
from semantic_crosscheck import measure_oracle as oracle
from semantic_crosscheck import measure_oracle_join
from semantic_crosscheck.result import StageError


EXAMPLE = pgfixture.ROOT / 'examples' / 'measure-oracle-joins'
JOIN = {'entity': 'lines', 'fact_key': 'id', 'foreign_key': 'invoice_id'}


def spec(**changes):
    return dict({'schema_version': 2, 'entity': 'invoices', 'column': 'amount',
                 'aggregation': 'sum', 'where': [], 'related_exists': JOIN, 'max_rows': 20,
                 'cube_query': {'measures': ['m_eligible.value']}}, **changes)


class MeasureOracleJoinTest(unittest.TestCase):
    def calculate(self, facts, related, *, fact_kind='integer', foreign_kind='integer', **changes):
        cfg = SimpleNamespace(entities={name: SimpleNamespace(table='demo.' + name)
                                        for name in ('invoices', 'lines')})
        connection = object()
        def read_table(conn, table, columns, max_rows):
            self.assertIs(conn, connection)
            if table == 'demo.invoices':
                types = {'id': fact_kind, 'amount': 'numeric', 'state': 'text'}
                rows = facts
            else:
                self.assertEqual(table, 'demo.lines')
                self.assertEqual(set(columns), {'invoice_id'})
                types = {'invoice_id': foreign_kind}
                rows = [{'invoice_id': value} for value in related]
            if len(rows) > max_rows:
                raise oracle.NotRun('row_limit')
            return {name: types[name] for name in columns}, rows
        return measure_oracle_join.source(cfg, spec(**changes), connection, read_table)

    def test_fanout_does_not_multiply_amounts_but_distinct_equal_facts_both_count(self):
        facts = [{'id': key, 'amount': amount, 'state': state} for key, amount, state in (
            (1, Decimal('15.25'), 'posted'), (2, Decimal('15.25'), 'posted'),
            (3, Decimal('-4.50'), 'posted'), (4, None, 'posted'),
            (5, 100, 'posted'), (6, 11, 'void'))]
        related = [1, 1, 2, 3, 4, 6, 6, None, 999]
        condition = [{'column': 'state', 'type': 'text', 'operator': 'eq', 'value': 'posted'}]
        answer = self.calculate(facts, related, where=condition)
        self.assertEqual(answer['value'], Decimal('26.00'))
        self.assertEqual((answer['source_rows'], answer['qualifying_rows'], answer['non_null_values']),
                         (6, 4, 3))
        self.assertEqual((answer['related_rows'], answer['matched_facts'], answer['multi_match_facts'],
                          answer['null_foreign_keys'], answer['orphan_related_rows']), (9, 5, 2, 1, 1))
        self.assertEqual(answer['qualifying_multi_match_facts'], 1)
        self.assertEqual(self.calculate(facts, related, where=condition, aggregation='count')['value'], 3)

    def test_only_void_order_has_multiple_lines(self):
        facts = [{'id': 1, 'amount': 10, 'state': 'posted'}, {'id': 2, 'amount': 20, 'state': 'void'}]
        where = [{'column': 'state', 'type': 'text', 'operator': 'eq', 'value': 'posted'}]
        result = self.calculate(facts, [1, 2, 2], where=where)
        self.assertEqual(result['value'], 10)
        self.assertEqual((result['multi_match_facts'], result['qualifying_multi_match_facts']), (1, 0))

    def test_invalid_identity_is_checked_before_matching_or_filters(self):
        for ids in ([1, 1], [1, None]):
            facts = [{'id': key, 'amount': 1, 'state': 'void'} for key in ids]
            with self.subTest(ids=ids), self.assertRaises(oracle.NotRun) as error:
                self.calculate(facts, [], where=[{'column': 'state', 'type': 'text',
                                                  'operator': 'eq', 'value': 'posted'}])
            self.assertEqual(error.exception.reason, 'invalid_fact_identity')

    def test_empty_null_and_orphan_relationships_keep_sql_sum_and_count_semantics(self):
        for facts, related in (([], []), ([{'id': 1, 'amount': 4}], [None, 99]),
                               ([{'id': 1, 'amount': None}], [1, 1])):
            with self.subTest(facts=facts, related=related):
                self.assertIsNone(self.calculate(facts, related)['value'])
                self.assertEqual(self.calculate(facts, related, aggregation='count')['value'], 0)

    def test_integer_or_deterministically_collated_text_keys_only_no_coercion(self):
        facts = [{'id': '01', 'amount': 2}, {'id': '1', 'amount': 9}]
        self.assertEqual(self.calculate(facts, ['01', '01'], fact_kind='text', foreign_kind='text')['value'], 2)
        for fact_kind, foreign_kind in [('text', 'integer'), ('integer', 'numeric'), ('boolean', 'boolean')]:
            with self.subTest(fact_kind=fact_kind, foreign_kind=foreign_kind), self.assertRaises(oracle.NotRun):
                self.calculate([], [], fact_kind=fact_kind, foreign_kind=foreign_kind)
        for facts, related in (([{'id': True, 'amount': 2}], [1]), ([{'id': 1, 'amount': 2}], [True])):
            with self.subTest(facts=facts, related=related), self.assertRaises(oracle.NotRun):
                self.calculate(facts, related)

    def test_complete_both_tables_before_any_comparison(self):
        for facts, related in (([{'id': 1, 'amount': 2}, {'id': 2, 'amount': 8}], []),
                               ([], [1, 1])):
            with self.subTest(facts=facts, related=related), self.assertRaises(oracle.NotRun) as error:
                self.calculate(facts, related, max_rows=1)
            self.assertEqual(error.exception.reason, 'row_limit')

    def test_v2_closed_global_only_and_v1_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'spec.json'
            path.write_text(json.dumps(spec()), encoding='utf-8')
            self.assertEqual(oracle.load_spec(path)['related_exists'], JOIN)
            for item in (spec(schema_version=1), spec(related_exists={**JOIN, 'weight': 2}),
                         spec(related_exists={**JOIN, 'entity': 'invoices'}),
                         spec(related_exists={**JOIN, 'foreign_key': 'id + 1'}),
                         spec(related_exists=None), spec(group_by='id'),
                         spec(time_window={'column': 'stamp', 'start': '2024-01-01', 'end': '2024-03-01'}),
                         spec(cube_query={'measures': ['m_eligible.value'], 'dimensions': ['m_eligible.key']})):
                path.write_text(json.dumps(item), encoding='utf-8')
                with self.subTest(item=item), self.assertRaises(StageError):
                    oracle.load_spec(path)

    def test_shipped_fixture_has_the_predicted_agreement_and_disagreement(self):
        # The two example views use portable SQL. The integration test uses
        # those same views with actual PostgreSQL, generated models and Cube.
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ':memory:' AS sxc_demo")
        conn.executescript((EXAMPLE / 'fixture.sql').read_text(encoding='utf-8'))
        facts = [dict(row) for row in conn.execute('SELECT * FROM sxc_demo.oracle_sales_order')]
        for row in facts:
            if row['amount'] is not None:
                row['amount'] = Decimal(str(row['amount']))
        related = [row[0] for row in conn.execute('SELECT order_id FROM sxc_demo.oracle_sales_line')]
        for name, view, scalar, expected_match in (
                ('eligible', 'oracle_eligible_orders', Decimal('26'), True),
                ('multiplied', 'oracle_orders_per_line', Decimal('41.25'), False)):
            with self.subTest(name=name):
                document = json.loads((EXAMPLE / (name + '.json')).read_text(encoding='utf-8'))
                expected = self.calculate(facts, related, where=document['where'])
                actual = conn.execute(f'SELECT sum(amount) FROM sxc_demo.{view} WHERE state = ?', ('posted',)).fetchone()[0]
                self.assertEqual(Decimal(str(actual)), scalar)
                member = document['cube_query']['measures'][0]
                self.assertIs(oracle.compare_value(expected['value'], [{member: str(actual)}], member), expected_match)
