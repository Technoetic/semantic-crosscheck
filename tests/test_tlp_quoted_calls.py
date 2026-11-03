"""Quoted volatile invocations cannot bypass either SQL builder's gate."""
import unittest
import json
from pathlib import Path
import tempfile

from semantic_crosscheck import tlp


class QuotedCallTest(unittest.TestCase):
    def spec(self, relation='SELECT qty FROM stock', predicate='qty > 0'):
        return {'relation_sql': relation, 'predicate_sql': predicate,
                'aggregate': {'function': 'count', 'column': '*'}}

    def load(self, spec):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'spec.json'
            path.write_text(json.dumps(dict(spec, schema_version=1, scenario='nulls')),
                            encoding='utf-8')
            return tlp.load_spec(path)

    def test_expression_commas_do_not_create_relation_aliases(self):
        cases = [self.spec(relation='SELECT 1 AS qty, NOT "random"() > 0.5 AS flag FROM stock'),
                 self.spec(predicate='COALESCE(NULL::boolean, NOT "random"() > 0.5)')]
        for spec in cases:
            loaded = self.load(spec)
            for builder in (tlp._query, tlp._norec_query):
                with self.subTest(builder=builder.__name__, spec=spec), \
                        self.assertRaisesRegex(ValueError, '^Known unstable SQL input$'):
                    builder(loaded)

    def test_only_and_adjacent_as_quoted_aliases_remain_supported(self):
        for relation in ('SELECT * FROM ONLY stock AS "random"(part_id)',
                         'SELECT * FROM stock AS"random"(part_id)'):
            loaded = self.load(self.spec(relation=relation, predicate='TRUE'))
            for builder in (tlp._query, tlp._norec_query):
                with self.subTest(builder=builder.__name__, relation=relation):
                    self.assertIsInstance(builder(loaded), str)

    def test_relation_alias_context_matrix_after_spec_loading(self):
        relations = [
            'SELECT * FROM ONLY public.stock "random"(part_id)',
            'SELECT * FROM ONLY (public.stock) AS"random"(part_id)',
            'SELECT * FROM stock * AS"random"(part_id)',
            'SELECT * FROM "FROM"."JOIN" AS"random"(part_id)',
            'SELECT * FROM stock s, ONLY stock AS"random"(part_id)',
            'SELECT * FROM stock s JOIN ONLY stock AS"random"(part_id) ON TRUE',
            'SELECT * FROM stock s JOIN LATERAL generate_series(1, 2) AS"random"(qty) ON TRUE',
            'SELECT * FROM LATERAL (SELECT 1 AS qty) "random"(qty)',
            'SELECT * FROM (SELECT * FROM stock AS"random"(part_id)) AS"clock_timestamp"(qty)',
            'SELECT * FROM (stock AS"random"(part_id) JOIN stock s ON TRUE)',
            'SELECT * FROM ROWS FROM (generate_series(1, 2)) AS"random"(qty)',
            'SELECT * FROM (VALUES (1), (2)) AS"random"(qty)',
            'SELECT * FROM stock AS"random"(part_id) WHERE COALESCE(NULL, TRUE)',
        ]
        for relation in relations:
            loaded = self.load(self.spec(relation=relation, predicate='TRUE'))
            for builder in (tlp._query, tlp._norec_query):
                with self.subTest(builder=builder.__name__, relation=relation):
                    self.assertIsInstance(builder(loaded), str)

    def test_expression_context_matrix_never_exempts_volatile_calls(self):
        calls = ['"random"()', 'pg_catalog . "random" ( )',
                 '"pg_catalog"."clock_timestamp"()']
        relation_templates = [
            'SELECT 1 AS qty, {call} IS NOT NULL FROM stock',
            'SELECT 1 AS qty, NOT {call} IS NULL FROM stock',
            'SELECT 1 IS DISTINCT FROM 2, NOT {call} IS NULL FROM stock',
            'SELECT * FROM stock WHERE COALESCE(NULL::boolean, NOT {call} IS NULL)',
            'SELECT * FROM stock s JOIN stock t ON COALESCE(NULL::boolean, NOT {call} IS NULL)',
            'SELECT * FROM stock s JOIN stock t ON (ARRAY[TRUE, NOT {call} IS NULL])[1]',
            'SELECT * FROM stock ORDER BY COALESCE(NULL::boolean, NOT {call} IS NULL)',
            'SELECT * FROM (SELECT 1 AS qty, NOT {call} IS NULL) s',
            'SELECT * FROM (VALUES (TRUE), (NOT {call} IS NULL)) s(flag)',
            'SELECT * FROM {call}',
            'SELECT * FROM ROWS FROM ({call}) s',
            'SELECT * FROM stock s, LATERAL {call}',
            'SELECT * FROM stock s JOIN LATERAL {call} ON TRUE',
            'SELECT * FROM generate_series(1, 2 OPERATOR(pg_catalog.+) {call}) s',
        ]
        predicate_templates = [
            'COALESCE(NULL::boolean, NOT {call} IS NULL)',
            'COALESCE(NULL::boolean, (NOT {call} IS NULL))',
            'COALESCE(NULL, OPERATOR(pg_catalog.-) {call}) IS NOT NULL',
            'qty OPERATOR(pg_catalog.+) (OPERATOR(pg_catalog.-) {call}) IS NOT NULL',
            '"FROM"."JOIN" IS NULL OR {call} IS NOT NULL',
            "'FROM stock AS' IS NULL OR {call} IS NOT NULL",
        ]
        for call in calls:
            cases = [self.spec(relation=value.format(call=call), predicate='TRUE')
                     for value in relation_templates]
            cases += [self.spec(predicate=value.format(call=call)) for value in predicate_templates]
            for spec in cases:
                loaded = self.load(spec)
                for builder in (tlp._query, tlp._norec_query):
                    with self.subTest(builder=builder.__name__, spec=spec), \
                            self.assertRaisesRegex(ValueError, '^Known unstable SQL input$'):
                        builder(loaded)

    def test_quoted_known_volatile_calls_are_rejected_in_both_inputs(self):
        expressions = ['"random"()', 'pg_catalog . "random" ( )',
                       '"pg_catalog" . "clock_timestamp" ( )',
                       '"nextval"(\'x\')', '"gen_random_uuid"()']
        for builder in (tlp._query, tlp._norec_query):
            for expression in expressions:
                for spec in (self.spec(relation='SELECT ' + expression + ' FROM stock'),
                             self.spec(predicate=expression + ' IS NOT NULL')):
                    with self.subTest(builder=builder.__name__, expression=expression, spec=spec), \
                            self.assertRaisesRegex(ValueError, '^Known unstable SQL input$'):
                        builder(spec)

            with self.assertRaisesRegex(ValueError, '^Known unstable SQL input$'):
                builder(self.spec(predicate='COALESCE(qty, OPERATOR(pg_catalog.-) "random"()) > 0'))

    def test_identifiers_and_literals_are_not_treated_as_quoted_calls(self):
        cases = [self.spec(relation='SELECT "random" AS "SELECT" FROM "clock_timestamp"'),
                 self.spec(relation='SELECT qty AS "ran""dom" FROM "SELECT"'),
                 self.spec(predicate="'random' = 'random'"),
                 self.spec(predicate='"random" IS NOT NULL'),
                 self.spec(predicate='"ran""dom"() IS NOT NULL'),
                 self.spec(predicate='"SELECT"."qty" > 0')]
        for builder in (tlp._query, tlp._norec_query):
            for spec in cases:
                with self.subTest(builder=builder.__name__, spec=spec):
                    self.assertIsInstance(builder(spec), str)

    def test_volatile_named_table_alias_with_column_list_is_not_a_call(self):
        relations = ['SELECT qty FROM stock AS "random"(qty)',
                     'SELECT qty FROM stock "random" (qty)',
                     'SELECT qty FROM "stock" AS "clock_timestamp"(qty)',
                     'SELECT qty FROM public.stock AS "random"(qty)',
                     'SELECT a.qty FROM stock a, stock AS "random"(qty)',
                     'SELECT a.qty FROM stock a, stock "random"(qty)',
                     'SELECT qty FROM generate_series(1, 2) AS "random"(qty)',
                     'SELECT qty FROM generate_series(1, 2) "random"(qty)',
                     'SELECT qty FROM (SELECT qty FROM stock) AS "random"(qty)',
                     'SELECT qty FROM (SELECT qty FROM stock) "random"(qty)']
        for builder in (tlp._query, tlp._norec_query):
            for relation in relations:
                with self.subTest(builder=builder.__name__, relation=relation):
                    self.assertIsInstance(builder(self.spec(relation=relation)), str)

    def test_quoted_table_function_is_still_rejected(self):
        for builder in (tlp._query, tlp._norec_query):
            for relation in ('SELECT * FROM "random"()',
                             'SELECT * FROM pg_catalog . "random" ( )',
                             'SELECT * FROM "pg_catalog" . "clock_timestamp" ( )',
                             'SELECT * FROM LATERAL "random"()',
                             'SELECT * FROM stock JOIN "random"() ON true',
                             'SELECT * FROM stock, "random"()'):
                with self.subTest(builder=builder.__name__, relation=relation), \
                        self.assertRaisesRegex(ValueError, '^Known unstable SQL input$'):
                    builder(self.spec(relation=relation))

    def test_volatile_calls_after_qualified_operator_are_rejected(self):
        expressions = ['OPERATOR(pg_catalog.+) "random"()',
                       'OPERATOR(pg_catalog.+) pg_catalog . "random" ( )',
                       'OPERATOR(pg_catalog.+) "pg_catalog" . "clock_timestamp" ( )']
        for builder in (tlp._query, tlp._norec_query):
            for expression in expressions:
                for spec in (self.spec(relation='SELECT 1 ' + expression + ' FROM stock'),
                             self.spec(predicate='qty ' + expression + ' > 0')):
                    with self.subTest(builder=builder.__name__, expression=expression, spec=spec), \
                            self.assertRaisesRegex(ValueError, '^Known unstable SQL input$'):
                        builder(spec)

    def test_malformed_and_unsupported_lexical_forms_remain_rejected(self):
        for builder in (tlp._query, tlp._norec_query):
            for predicate in ('"random', "'random", "'a\\b' = 'a'", 'qty -- x',
                              'qty /* x */', 'qty; SELECT 1', '$tag$x$tag$'):
                with self.subTest(builder=builder.__name__, predicate=predicate), self.assertRaises(ValueError):
                    builder(self.spec(predicate=predicate))

    def test_spec_loading_preserves_quoted_keyword_aliases(self):
        spec = dict(self.spec(relation='SELECT "random" AS "SELECT" FROM "SELECT"',
                              predicate='"SELECT"."random" IS NOT NULL'),
                    schema_version=1, scenario='nulls')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'spec.json'
            path.write_text(json.dumps(spec), encoding='utf-8')
            loaded = tlp.load_spec(path)
            for builder in (tlp._query, tlp._norec_query):
                self.assertIsInstance(builder(loaded), str)


if __name__ == '__main__':
    unittest.main()
