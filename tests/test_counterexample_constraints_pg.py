"""Constraint semantics against real PostgreSQL in a private throw-away DB."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import constraints, counterexample as ce, db

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'counterexamples'


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class ConstraintPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_counterexample_constraints_pg_' + str(os.getpid())).setup()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def profile(self, document):
        path = self.out / 'profile.json'
        path.write_text(json.dumps(document), encoding='utf-8')
        return constraints.load(path, ce.SCHEMA)

    def valid(self, fixture, profile):
        try:
            constraints.validate_fixture(fixture, ce.SCHEMA, profile)
            return True
        except constraints.ConstraintViolation:
            return False

    def accepts(self, ddl, statements):
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                for sql in ddl:
                    conn.run(sql)
                for sql, params in statements:
                    conn.run(sql, **params)
            return True
        except Exception as error:
            argument = error.args[0] if error.args else None
            code = argument.get('C', '') if isinstance(argument, dict) else ''
            if code not in ('23502', '23503', '23505', '23514', '22003'):
                raise
            return False
        finally:
            conn.close()  # Session-local test tables disappear on close.

    def test_unique_null_distinct_and_pk_not_null_match_postgres(self):
        for rule, qtys, expected in [
            ('UNIQUE', [None, None], True), ('UNIQUE', ['1.00', '1'], False),
            ('PRIMARY KEY', [None], False), ('PRIMARY KEY', ['1', '2'], True),
        ]:
            with self.subTest(rule=rule, qtys=qtys):
                declarations = {'unique': [['qty']]} if rule == 'UNIQUE' else {'primary_key': ['qty']}
                profile = self.profile({'version': 1, 'tables': {'stock': declarations}})
                fixture = {table: [] for table in ce.SCHEMA}
                fixture['stock'] = [['A', 'L' + str(i), qty] for i, qty in enumerate(qtys)]
                observed = self.accepts([f'CREATE TEMP TABLE constraint_qty(qty numeric(18,3) {rule})'],
                                        [('INSERT INTO constraint_qty VALUES (CAST(:qty AS numeric(18,3)))', {'qty': qty})
                                         for qty in qtys])
                self.assertEqual(observed, expected)
                self.assertEqual(self.valid(fixture, profile), expected)

    def test_fk_match_simple_orphans_and_multiple_columns_match_postgres(self):
        profile = self.profile({'version': 1, 'tables': {'part': {'unique': [['part_id', 'unit']]}},
                                'foreign_keys': [{'table': 'stock', 'columns': ['part_id', 'location_id'],
                                                  'references': {'table': 'part', 'columns': ['part_id', 'unit']}}]})
        for parent, child, expected in [
            ([], ['A', None], True), ([], ['A', 'L0'], False),
            ([['A', 'L0']], ['A', 'L0'], True), ([['A', None]], ['A', 'L0'], False),
        ]:
            with self.subTest(parent=parent, child=child):
                fixture = {table: [] for table in ce.SCHEMA}
                fixture['part'] = [[identifier, None, unit, None, None] for identifier, unit in parent]
                fixture['stock'] = [[*child, '1']]
                statements = [('INSERT INTO constraint_parent VALUES (:id, :unit)', {'id': identifier, 'unit': unit})
                              for identifier, unit in parent]
                statements.append(('INSERT INTO constraint_child VALUES (:id, :location)',
                                   {'id': child[0], 'location': child[1]}))
                observed = self.accepts([
                    'CREATE TEMP TABLE constraint_parent(id text, unit text, UNIQUE(id, unit))',
                    'CREATE TEMP TABLE constraint_child(id text, location text, FOREIGN KEY(id, location) REFERENCES constraint_parent(id, unit) MATCH SIMPLE)',
                ], statements)
                self.assertEqual(observed, expected)
                self.assertEqual(self.valid(fixture, profile), expected)

    def test_check_rounding_null_and_overflow_match_postgres(self):
        for not_null, qty, expected in [
            (False, None, True), (True, None, False), (False, '0.00049', False),
            (False, '0.0005', True), (False, '-0.0005', False),
            (False, '999999999999999.9994', True), (False, '999999999999999.9995', False),
        ]:
            with self.subTest(not_null=not_null, qty=qty):
                profile = self.profile({'version': 1, 'tables': {'stock': {'not_null': ['qty'] if not_null else []}},
                                        'checks': [{'table': 'stock', 'column': 'qty', 'op': 'gt', 'value': '0.0001'}]})
                fixture = {table: [] for table in ce.SCHEMA}; fixture['stock'] = [['A', 'L0', qty]]
                observed = self.accepts([
                    'CREATE TEMP TABLE constraint_checked(qty numeric(18,3) ' + ('NOT NULL ' if not_null else '') +
                    'CHECK(qty > 0.0001))'],
                    [('INSERT INTO constraint_checked VALUES (CAST(:qty AS numeric(18,3)))', {'qty': qty})])
                self.assertEqual(observed, expected)
                self.assertEqual(self.valid(fixture, profile), expected)

    def test_constrained_actual_sql_search_replays_and_checks_each_admissible_deletion(self):
        profile = self.profile({'version': 1, 'tables': {'part': {'primary_key': ['part_id']}},
                                'foreign_keys': [{'table': 'stock', 'columns': ['part_id'],
                                                  'references': {'table': 'part', 'columns': ['part_id']}}]})
        args = SimpleNamespace(reference_sql=str(EXAMPLES / 'nulls-reference.sql'),
                               candidate_sql=str(EXAMPLES / 'nulls-candidate.sql'), scenario='nulls', seed=7,
                               max_examples=2, max_shrink_steps=300, statement_timeout_ms=1000,
                               dsn_env='SXC_PG_DSN', replay=None, constraints=str(profile.source))
        result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'fail')
        payload = json.loads((self.out / 'counterexample/fixture.json').read_text(encoding='utf-8'))
        self.assertTrue(self.valid(payload['tables'], profile))
        self.assertEqual(payload['shrink']['minimality_scope'], 'admissible_single_row_deletions')
        self.assertTrue(payload['shrink']['deletion_1_minimal'])
        self.assertGreater(payload['shrink']['invalid_domain_trials'], 0)
        self.assertEqual(len(payload['tables']['part']), 1)
        self.assertEqual(len(payload['tables']['stock']), 1)
        connection = self.fx.check_conn()
        try:
            for table in ce.SCHEMA:
                for index in range(len(payload['tables'][table])):
                    trial = {name: [list(row) for row in rows] for name, rows in payload['tables'].items()}
                    del trial[table][index]
                    if self.valid(trial, profile):
                        answer = ce.evaluate(connection, trial, Path(args.reference_sql).read_text(encoding='utf-8'),
                                             Path(args.candidate_sql).read_text(encoding='utf-8'), 1000, profile=profile)
                        self.assertTrue(answer['equal'])
        finally:
            connection.close()
        args.replay = str(self.out / 'counterexample/fixture.json')
        replay_result = ce.run(None, args, self.out / 'replay')
        self.assertEqual(replay_result.status, 'fail')
        self.assertEqual(replay_result.checks[0].detail['minimized_rows'], 2)

    def test_all_invalid_generated_cases_never_execute_sql(self):
        profile = self.profile({'version': 1, 'tables': {'stock': {'not_null': ['qty']}}})
        args = SimpleNamespace(reference_sql=str(EXAMPLES / 'nulls-reference.sql'),
                               candidate_sql=str(EXAMPLES / 'nulls-candidate.sql'), scenario='nulls', seed=7,
                               max_examples=2, max_shrink_steps=300, statement_timeout_ms=1000,
                               dsn_env='SXC_PG_DSN', replay=None, constraints=str(profile.source))
        with patch.object(ce, 'evaluate', side_effect=AssertionError('Invalid domain must not execute SQL')):
            result = ce.run(None, args, self.out)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.checks[0].detail['reason'], 'no_admissible_generated_examples')
        self.assertEqual(result.checks[0].detail['invalid_domain_examples'], 2)
        self.assertFalse((self.out / 'counterexample/fixture.json').exists())

    def test_constrained_populated_and_empty_text_ctes_use_actual_c_collation(self):
        profile = self.profile({'version': 1, 'tables': {}})
        fixture = {table: [] for table in ce.SCHEMA}
        fixture['part'] = [['A', None, None, None, None]]
        connection = self.fx.check_conn()
        try:
            for candidate in [
                'SELECT pg_collation_for(part_id)::text AS n FROM part',
                'SELECT pg_collation_for((SELECT location_id FROM stock LIMIT 1))::text AS n',
            ]:
                with self.subTest(candidate=candidate):
                    answer = ce.evaluate(connection, fixture, '''SELECT '"C"'::text AS n''', candidate, 1000,
                                         profile=profile)
                    self.assertTrue(answer['equal'])
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
