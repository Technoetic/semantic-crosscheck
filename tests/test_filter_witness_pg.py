"""Filter-witness regressions on a synthetic, explicitly configured PostgreSQL fixture."""
import dataclasses
import unittest

import pgfixture
from semantic_crosscheck import config, cube_gen, db, mutate


@unittest.skipUnless(pgfixture.available(), 'requires disposable PostgreSQL')
class FilterWitnessPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_t_filter_witness')
        cls.fx.setup()
        cls.addClassCleanup(cls.fx.teardown)
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        cls.owner = db.connect('SXC_PG_OWNER_DSN')
        cls.addClassCleanup(cls.owner.close)
        cls.conn = cls.fx.check_conn()
        cls.addClassCleanup(cls.conn.close)
        with db.write_tx(cls.owner):
            cls.owner.run('DROP TABLE sxc_demo.sales_order CASCADE')
            cls.owner.run('CREATE TABLE sxc_demo.sales_order (order_id text primary key, part_id text, status text, qty_open numeric, qty_total numeric, due_date date)')
        cls.cols = dict(cube_gen.columns(cls.conn, cls.cfg.entities['sales_order'].table))

    def test_exposed_aggregate_differences(self):
        # Expected values are independently derived from the listed inputs.
        cases = [
            ('sum', [('FIRM', None), ('OTHER', 0)], None, 0, True),
            ('sum', [('FIRM', 10), ('OTHER', 5), ('OTHER', -5)], 10, 10, False),
            ('min', [('FIRM', 10), ('OTHER', 15)], 10, 10, False),
            ('max', [('FIRM', 10), ('OTHER', 5)], 10, 10, False),
            ('sum', [('OTHER', 0)], None, 0, True),
            ('count', [('FIRM', None), ('OTHER', None)], 0, 0, False),
            ('count', [('FIRM', None), ('OTHER', 0)], 0, 1, True),
        ]
        for agg, rows, before, after, differs in cases:
            with self.subTest(agg=agg, rows=rows):
                with db.write_tx(self.owner):
                    self.owner.run('DELETE FROM sxc_demo.sales_order')
                    for i, (status, qty) in enumerate(rows):
                        self.owner.run('INSERT INTO sxc_demo.sales_order (order_id, part_id, status, qty_open) VALUES (:id, :key, :status, :qty)', id=str(i), key='k', status=status, qty=qty)
                m = dataclasses.replace(self.cfg.measures['firm_open'], agg=agg)
                model = {'cubes': [cube_gen.measure_cube(self.cfg, m, self.cols)]}
                mu = mutate._filter_drop(self.cfg, model, m)
                with db.read_only(self.conn):
                    orig = self.conn.run(model['cubes'][0]['sql'])
                    changed = self.conn.run(mu.model['cubes'][0]['sql'])
                self.assertEqual(orig[0][1] if orig else None, before)
                self.assertEqual(changed[0][1], after)
                self.assertEqual(mutate.witness(self.conn, self.cfg, mu) > 0, differs)

    def test_normalization_drop_uses_aggregate_observability(self):
        with db.write_tx(self.owner):
            self.owner.run('DROP TABLE sxc_demo.stock CASCADE')
            self.owner.run('CREATE TABLE sxc_demo.stock (part_id text, location_id text, qty numeric)')
        cols = dict(cube_gen.columns(self.conn, self.cfg.entities['stock'].table))
        m = self.cfg.measures['on_hand']
        model = {'cubes': [cube_gen.measure_cube(self.cfg, m, cols)]}
        mu = mutate._normalization_drop(self.cfg, model, m)
        for extra, expected in [(0, True), (None, False)]:
            with self.subTest(extra=extra), db.write_tx(self.owner):
                self.owner.run('DELETE FROM sxc_demo.stock')
                self.owner.run("INSERT INTO sxc_demo.stock VALUES ('k', 'MAIN', NULL), ('k', 'main', :v)", v=extra)
            self.assertEqual(mutate.witness(self.conn, self.cfg, mu) > 0, expected)

    def test_cube_and_v4_observe_null_and_cancellation(self):
        import copy
        import shutil
        import tempfile
        from pathlib import Path
        from semantic_crosscheck import cube_run
        if not shutil.which('node') or not cube_run.installed(cube_run.cube_home()):
            self.skipTest('requires pinned Cube adapter')
        cfg = copy.deepcopy(self.cfg)
        cfg.measures = {'firm_open': cfg.measures['firm_open']}
        cfg.verify.mutation_operators = ['measure_filter_drop']
        cases = [
            ('sum', [('FIRM', None), ('OTHER', 0)], None, '0', 'killed'),
            ('sum', [('FIRM', 10), ('OTHER', 5), ('OTHER', -5)], '10', '10', 'equivalent'),
            ('min', [('FIRM', 10), ('OTHER', 15)], '10', '10', 'equivalent'),
            ('max', [('FIRM', 10), ('OTHER', 5)], '10', '10', 'equivalent'),
        ]
        for agg, rows, expected_original, expected_mutant, outcome in cases:
            with self.subTest(agg=agg, rows=rows), tempfile.TemporaryDirectory() as tmp:
                cfg.measures['firm_open'] = dataclasses.replace(cfg.measures['firm_open'], agg=agg)
                with db.write_tx(self.owner):
                    self.owner.run('DELETE FROM sxc_demo.sales_order')
                    for i, (status, qty) in enumerate(rows):
                        self.owner.run('INSERT INTO sxc_demo.sales_order (order_id, part_id, status, qty_open) VALUES (:id, :key, :status, :qty)', id=str(i), key='k', status=status, qty=qty)
                model = {'cubes': [cube_gen.measure_cube(cfg, cfg.measures['firm_open'], self.cols)]}
                query = {'dimensions': ['m_firm_open.key'], 'measures': ['m_firm_open.value']}
                out = Path(tmp)
                cube_gen.write_model(model, out / 'original' / 'model')
                with cube_run.serve(cfg, out / 'original', out / 'original' / 'server.log') as client:
                    original = client.load(query)[0]['m_firm_open.value']
                self.assertEqual(original, expected_original)
                mutant = mutate._filter_drop(cfg, model, cfg.measures['firm_open'])
                cube_gen.write_model(mutant.model, out / 'mutant' / 'model')
                with cube_run.serve(cfg, out / 'mutant', out / 'mutant' / 'server.log') as client:
                    self.assertEqual(client.load(query)[0]['m_firm_open.value'], expected_mutant)
                coverage = []
                def suite(client):
                    value = client.load(query)[0]['m_firm_open.value']
                    self.assertEqual(value, expected_mutant)
                    return ('pass', '') if value == original else ('fail', 'v3:fixture')
                checks, _ = mutate.run_mutants(cfg, out, self.conn, model, suite, closure={}, coverage=coverage)
                self.assertEqual(coverage[0]['outcome'], outcome)
                if outcome == 'killed':
                    self.assertEqual(checks[0][1], 'pass')
                    self.assertEqual(checks[0][3]['killed_by'], 'v3:fixture')
                    survivor = []
                    checks, _ = mutate.run_mutants(cfg, out, self.conn, model, lambda client: ('pass', ''), closure={}, coverage=survivor)
                    self.assertEqual(survivor[0]['outcome'], 'survived')
                    self.assertEqual(checks[0][1], 'fail')
                else:
                    self.assertEqual(coverage[0]['reason'], 'data_equivalent')
                    self.assertEqual(checks, [])
