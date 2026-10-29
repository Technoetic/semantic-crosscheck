"""Actual read-only PostgreSQL evidence for authored preaggregate grain claims."""
import json
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import config, db
from test_grain import GrainInputs, document, grain


@unittest.skipUnless(pgfixture.available(), 'requires throw-away PostgreSQL')
class GrainPostgresTest(GrainInputs):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_ninth_grain').setup()
        cls.write('CREATE TABLE sxc_demo.grain_fact (fact_id integer, node_id text, qty numeric, status text); CREATE TABLE sxc_demo.grain_node (node_id text)')

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    @classmethod
    def write(cls, sql):
        conn = db.connect('SXC_PG_OWNER_DSN')
        try:
            with db.write_tx(conn):
                for statement in sql.split(';'):
                    if statement.strip():
                        conn.run(statement)
        finally:
            conn.close()

    def setUp(self):
        super().setUp()
        self.write("TRUNCATE sxc_demo.grain_fact, sxc_demo.grain_node; INSERT INTO sxc_demo.grain_node VALUES ('a'), ('b'); INSERT INTO sxc_demo.grain_fact VALUES (1,'a',2,'FIRM'),(2,'a',3,'FIRM'),(3,'b',4,'DRAFT')")

    def run_pg(self, **changes):
        return grain.run(self.cfg, self.args(**changes), self.root)

    def test_real_supported_preaggregate_provenance_and_join_counts(self):
        result = self.run_pg()
        self.assertEqual(result.status, 'pass')
        evidence = self.check(result, 'evidence').detail
        for field, value in {'source_rows': 3, 'node_rows': 2, 'selected_rows': 3,
                             'output_groups': 2, 'joined_rows': 2, 'orphan_groups': 0,
                             'fanout_groups': 0, 'excluded_null_rows': 0}.items():
            self.assertEqual(evidence[field], value)
        payload = json.dumps(result.to_dict())
        for secret in ['grain_fact', 'node_id', 'FIRM', str(self.root), 'SELECT ', 'postgresql://']:
            self.assertNotIn(secret, payload)

    def test_source_and_node_duplicate_and_null_keys_fail_with_counts(self):
        for sql, field in [("INSERT INTO sxc_demo.grain_fact VALUES (1,'b',8,'FIRM')", 'source_duplicate_groups'),
                           ("INSERT INTO sxc_demo.grain_fact VALUES (NULL,'b',8,'FIRM')", 'source_null_key_rows'),
                           ("INSERT INTO sxc_demo.grain_node VALUES ('a')", 'node_duplicate_groups'),
                           ("INSERT INTO sxc_demo.grain_node VALUES (NULL)", 'node_null_key_rows')]:
            with self.subTest(field=field):
                self.setUp()
                self.write(sql)
                result = self.run_pg()
                self.assertEqual(result.status, 'fail')
                self.assertEqual(self.check(result, 'evidence').detail[field], 1)

    def test_missing_reference_fanout_and_null_exclusion_are_observed(self):
        self.write("INSERT INTO sxc_demo.grain_fact VALUES (4,'missing-private',1,'FIRM'),(5,NULL,9,'FIRM'); INSERT INTO sxc_demo.grain_node VALUES ('a')")
        result = self.run_pg()
        self.assertEqual(result.status, 'fail')
        evidence = self.check(result, 'evidence').detail
        self.assertEqual(evidence['orphan_groups'], 1)
        self.assertEqual(evidence['fanout_groups'], 1)
        self.assertEqual(evidence['excluded_null_rows'], 1)
        self.assertNotIn('missing-private', json.dumps(result.to_dict()))

    def test_explicit_null_exclusion_policy_allows_excluded_rows_but_never_erased_scope(self):
        doc = document()
        doc['contracts'][0]['null_policy']['allow_excluded_rows'] = True
        self.spec(doc)
        self.write("INSERT INTO sxc_demo.grain_fact VALUES (4,NULL,9,'FIRM')")
        self.assertEqual(self.run_pg().status, 'pass')
        self.write('UPDATE sxc_demo.grain_fact SET node_id = NULL')
        self.assertEqual(self.run_pg().status, 'not_run')

    def test_exact_normalized_filters_and_count_operation_use_configured_rows(self):
        self.cfg.measures['total'].filter = {'status': ['FIRM']}
        self.cfg.measures['total'].filter_normalized = {'node_id': ['A']}
        self.cfg.measures['total'].agg = 'count'
        doc = document()
        doc['contracts'][0]['filters'] = {'exact': {'status': ['FIRM']}, 'normalized': {'node_id': ['A']}}
        doc['contracts'][0]['aggregate']['function'] = 'count'
        self.spec(doc)
        result = self.run_pg()
        self.assertEqual(result.status, 'pass')
        self.assertEqual(self.check(result, 'evidence').detail['selected_rows'], 2)
        self.assertEqual(self.check(result, 'evidence').detail['output_groups'], 1)
        doc['contracts'][0]['filters']['exact']['status'] = ['DRAFT']
        self.spec(doc)
        self.assertEqual(self.run_pg().status, 'fail')

    def test_row_budget_and_empty_source_cannot_pass_a_partial_snapshot(self):
        result = self.run_pg(max_rows=2)
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(self.check(result, 'evidence').detail['reason'], 'row_budget')
        self.write('TRUNCATE sxc_demo.grain_fact')
        self.assertEqual(self.run_pg().status, 'not_run')

    def test_query_error_and_changed_input_do_not_leave_success(self):
        self.write('ALTER TABLE sxc_demo.grain_fact RENAME COLUMN qty TO private_column')
        try:
            result = self.run_pg()
            self.assertEqual(result.status, 'error')
            self.assertNotIn('private_column', json.dumps(result.to_dict()))
        finally:
            self.write('ALTER TABLE sxc_demo.grain_fact RENAME COLUMN private_column TO qty')
        original = grain.evaluate
        def change(*args, **kwargs):
            answer = original(*args, **kwargs)
            self.spec_path.write_bytes(self.spec_path.read_bytes() + b'\n')
            return answer
        with patch.object(grain, 'evaluate', side_effect=change):
            self.assertEqual(self.run_pg().status, 'error')

    def test_actual_queries_share_readonly_repeatable_snapshot(self):
        original = grain.evaluate
        observed = []
        def inspect(conn, *args, **kwargs):
            observed.append(conn.run("SELECT current_setting('transaction_read_only'), current_setting('transaction_isolation')")[0])
            return original(conn, *args, **kwargs)
        with patch.object(grain, 'evaluate', side_effect=inspect):
            self.assertEqual(self.run_pg().status, 'pass')
        self.assertEqual(list(observed[0]), ['on', 'repeatable read'])

    def test_views_are_explicitly_unsupported_and_filter_text_is_never_executed(self):
        self.write('CREATE OR REPLACE VIEW sxc_demo.grain_view AS SELECT * FROM sxc_demo.grain_fact')
        self.cfg.entities['fact'].table = 'sxc_demo.grain_view'
        doc = document()
        doc['contracts'][0]['source']['table'] = 'sxc_demo.grain_view'
        self.spec(doc)
        self.assertEqual(self.run_pg().status, 'not_run')
        self.cfg.entities['fact'].table = 'sxc_demo.grain_fact'
        literal = "FIRM'); DROP TABLE sxc_demo.grain_fact; --"
        self.cfg.measures['total'].filter = {'status': [literal]}
        doc = document()
        doc['contracts'][0]['filters']['exact'] = {'status': [literal]}
        self.spec(doc)
        result = self.run_pg()
        self.assertEqual(result.status, 'not_run')
        self.assertNotIn(literal, json.dumps(result.to_dict()))
        self.cfg.measures['total'].filter = {}
        self.spec()
        self.assertEqual(self.run_pg().status, 'pass')

    def test_shipped_contracts_execute_against_the_hand_authored_fixture(self):
        example = pgfixture.ROOT / 'examples' / 'grain'
        self.cfg = config.load(example / 'crosscheck.toml')
        self.spec_path.write_bytes((example / 'contract.json').read_bytes())
        self.assertEqual(self.run_pg().status, 'pass')
        self.spec_path.write_bytes((example / 'wrong-group.json').read_bytes())
        self.assertEqual(self.run_pg().status, 'fail')
        self.spec_path.write_bytes((example / 'allow-null-exclusion.json').read_bytes())
        self.write("INSERT INTO sxc_demo.grain_fact VALUES (4,NULL,9,'FIRM')")
        self.assertEqual(self.run_pg().status, 'pass')


if __name__ == '__main__':
    unittest.main()
