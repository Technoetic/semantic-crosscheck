"""Data revision binding is observed, fail-closed, and never emits raw probe data."""

import argparse
import dataclasses
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture
from semantic_crosscheck import config, db, report
from semantic_crosscheck.result import Result, StageError, inputs_fingerprint

if importlib.util.find_spec('semantic_crosscheck.provenance'):
    from semantic_crosscheck import provenance
else:
    provenance = None


class Prepared:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.columns = [{'name': 'revision'}]

    def run(self):
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, rows):
        self.prepared = Prepared(rows)
        self.statements = []
        self.closed = False

    def run(self, sql):
        self.statements.append(sql)
        return []

    def prepare(self, sql):
        self.probe_sql = sql
        return self.prepared

    def close(self):
        self.closed = True


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(provenance, 'Data revision binding has not been implemented')
        self.temp = tempfile.TemporaryDirectory(prefix='sxc_provenance_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'crosscheck.toml'
        self.path.write_text('version = 1\n[source]\nschemas = ["src"]\n', encoding='utf-8')
        self.cfg = config.load(self.path)
        self.query = self.root / 'revision.sql'
        self.query.write_text('SELECT revision::text FROM src.data_revision', encoding='utf-8')

    def bind(self):
        self.cfg.source.data_revision_sql_file = 'revision.sql'

    def connections(self, *values):
        conns = [Connection([[value]]) for value in values]
        return conns, mock.patch.object(db, 'connect', side_effect=conns)

    def passed(self, stage='verify'):
        result = Result(stage)
        result.add('fixture', 'pass')
        return result

    def bound_result(self, stage='verify', value='synthetic-revision-one'):
        conns, patch = self.connections(value, value, value, value)
        with patch:
            start = provenance.begin(self.cfg, stage)
            return provenance.finish(self.cfg, self.passed(stage), start)

    def pipeline(self):
        for i, stage in enumerate(('build', 'cube', 'verify')):
            result = self.bound_result(stage)
            result.inputs = inputs_fingerprint(self.cfg)
            saved = result.to_dict()
            saved['started_at'] = f'2026-10-16T10:{i * 2:02d}:00+0000'
            saved['finished_at'] = f'2026-10-16T10:{i * 2 + 1:02d}:00+0000'
            target = self.root / 'out' / stage
            target.mkdir(parents=True)
            (target / 'result.json').write_text(json.dumps(saved), encoding='utf-8')

    def test_optional_config_is_explicit_and_type_checked(self):
        self.assertEqual(self.cfg.source.data_revision_sql_file, '')
        self.path.write_text('version = 1\n[source]\nschemas=["src"]\ndata_revision_sql_file="revision.sql"\n', encoding='utf-8')
        self.assertEqual(config.load(self.path).source.data_revision_sql_file, 'revision.sql')
        self.path.write_text('version = 1\n[source]\nschemas=["src"]\ndata_revision_sql_file=7\n', encoding='utf-8')
        with self.assertRaises(StageError):
            config.load(self.path)

    def test_unconfigured_keeps_legacy_pass_but_explicitly_unbound(self):
        result = self.passed()
        with mock.patch.object(db, 'connect') as connect:
            start = provenance.begin(self.cfg, 'verify')
            self.assertIs(provenance.finish(self.cfg, result, start), result)
        connect.assert_not_called()
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.provenance['binding'], 'unbound')
        self.assertTrue(any('unbound' in note for note in result.not_proven))
        self.assertEqual(result.to_dict()['provenance']['binding'], 'unbound')

    def test_bound_probes_both_roles_in_fresh_read_only_transactions(self):
        self.bind()
        conns, patch = self.connections(*(['synthetic-revision-one'] * 4))
        self.cfg.cube.dsn_env = 'SXC_FIXTURE_CUBE_DSN'
        with patch as connect:
            result = provenance.finish(self.cfg, self.passed(), provenance.begin(self.cfg, 'verify'))
        self.assertEqual(connect.call_args_list, [mock.call(self.cfg.source.dsn_env), mock.call('SXC_FIXTURE_CUBE_DSN')] * 2)
        self.assertEqual(result.status, 'pass')
        self.assertTrue(result.provenance['stable'])
        self.assertEqual(result.provenance['binding'], 'bound')
        for conn in conns:
            self.assertEqual(conn.statements[0], 'BEGIN TRANSACTION READ ONLY')
            self.assertEqual(conn.statements[-1], 'ROLLBACK')
            self.assertTrue(conn.closed and conn.prepared.closed)
        encoded = json.dumps(result.to_dict())
        for raw in ('synthetic-revision-one', self.query.read_text(), str(self.query)):
            self.assertNotIn(raw, encoded)

    def test_change_after_cached_comparisons_cannot_pass(self):
        self.bind()
        conns, patch = self.connections('before', 'before', 'after', 'after')
        with patch:
            result = provenance.finish(self.cfg, self.passed(), provenance.begin(self.cfg, 'verify'))
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.provenance['binding'], 'changed')
        self.assertFalse(result.provenance['stable'])
        self.assertIn('rerun', result.next_safe_action)
        self.assertNotIn('before', json.dumps(result.to_dict()))

    def test_different_source_and_cube_revisions_fail_before_stage(self):
        self.bind()
        conns, patch = self.connections('source-fixture-token', 'cube-fixture-token')
        with patch, self.assertRaises(StageError) as raised:
            provenance.begin(self.cfg, 'verify')
        self.assertNotIn('fixture-token', str(raised.exception))
        self.assertTrue(all(conn.closed for conn in conns))

    def test_revision_is_exactly_one_nonempty_text_scalar(self):
        self.bind()
        for rows in ([], [[None]], [[1]], [[True]], [['']], [['  ']], [['one', 'two']], [['one'], ['two']]):
            with self.subTest(rows=rows):
                conn = Connection(rows)
                with mock.patch.object(db, 'connect', return_value=conn), self.assertRaises(StageError):
                    provenance.begin(self.cfg, 'verify')
                self.assertTrue(conn.closed and conn.prepared.closed)

    def test_exact_text_revision_preserves_whitespace_and_unicode(self):
        self.bind()
        first = self.bound_result(value='r1')
        second = self.bound_result(value=' r1')
        third = self.bound_result(value='r1\u00e9')
        self.assertNotEqual(first.provenance['end'], second.provenance['end'])
        self.assertNotEqual(first.provenance['end'], third.provenance['end'])

    def test_probe_failure_sanitizes_original_exception_and_fails_closed(self):
        self.bind()
        sensitive = 'fixture-row-value SELECT private-data /fixture/local/path'
        conns, patch = self.connections('r1', 'r1')
        with patch:
            start = provenance.begin(self.cfg, 'verify')
        with mock.patch.object(db, 'connect', side_effect=RuntimeError(sensitive)):
            result = provenance.finish(self.cfg, self.passed(), start)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.provenance['binding'], 'error')
        self.assertNotIn(sensitive, json.dumps(result.to_dict()))
        with mock.patch.object(db, 'connect', side_effect=RuntimeError(sensitive)), self.assertRaises(StageError) as raised:
            provenance.begin(self.cfg, 'verify')
        self.assertNotIn(sensitive, str(raised.exception))

    def test_prepared_cleanup_failure_still_closes_connection(self):
        self.bind()
        conn = Connection([['fixture-revision']])
        conn.prepared.close = mock.Mock(side_effect=RuntimeError('fixture raw query cleanup error'))
        with mock.patch.object(db, 'connect', return_value=conn), self.assertRaises(StageError) as raised:
            provenance.begin(self.cfg, 'verify')
        self.assertTrue(conn.closed, 'Prepared statement cleanup must not skip connection cleanup')
        self.assertEqual(conn.statements[-1], 'ROLLBACK')
        self.assertNotIn('fixture raw query', str(raised.exception))

    def test_query_file_missing_and_sql_error_do_not_leak_contents(self):
        self.bind()
        self.query.unlink()
        with self.assertRaises(StageError) as raised:
            provenance.begin(self.cfg, 'verify')
        self.assertNotIn(str(self.query), str(raised.exception))
        self.query.write_text('SELECT synthetic_sensitive_literal', encoding='utf-8')
        conn = Connection(RuntimeError('SQL with synthetic_sensitive_literal'))
        with mock.patch.object(db, 'connect', return_value=conn), self.assertRaises(StageError) as raised:
            provenance.begin(self.cfg, 'verify')
        self.assertNotIn('synthetic_sensitive_literal', str(raised.exception))

    def test_engine_and_revision_query_are_input_fingerprints(self):
        first = inputs_fingerprint(self.cfg)
        self.assertRegex(first['engine_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(first['revision_query_sha256'], '')
        self.bind()
        before = inputs_fingerprint(self.cfg)
        self.query.write_text(self.query.read_text() + '\n-- changed query\n', encoding='utf-8')
        after = inputs_fingerprint(self.cfg)
        self.assertNotEqual(before['revision_query_sha256'], after['revision_query_sha256'])
        self.assertNotIn(str(self.query), json.dumps(after))

    def test_engine_fingerprint_tracks_code_and_dependency_metadata(self):
        engine = self.root / 'engine.py'
        engine.write_text('fixture = 1\n', encoding='utf-8')
        with mock.patch.object(provenance, '_engine_files', return_value=[('engine.py', engine)]), \
             mock.patch.object(provenance, '_dependency_versions', return_value={'fixture': '1'}):
            first = provenance.engine_fingerprint()
            engine.write_text('fixture = 2\n', encoding='utf-8')
            self.assertNotEqual(first, provenance.engine_fingerprint())
            first = provenance.engine_fingerprint()
            with mock.patch.object(provenance, '_dependency_versions', return_value={'fixture': '2'}):
                self.assertNotEqual(first, provenance.engine_fingerprint())

    def test_report_rejects_old_engine_even_without_revision_configuration(self):
        result = self.passed('build').to_dict()
        result['inputs'] = inputs_fingerprint(self.cfg)
        result['inputs']['engine_sha256'] = '0' * 64
        stale = report.staleness({'build': result}, inputs_fingerprint(self.cfg))
        self.assertEqual(stale['build'], {'after': 'verification engine', 'why': 'inputs_changed'})

    def test_report_revision_change_invalidates_all_previously_passing_data_stages(self):
        self.bind()
        self.pipeline()
        conns, patch = self.connections('synthetic-revision-two', 'synthetic-revision-two')
        with patch:
            result = report.run(self.cfg, argparse.Namespace(), self.root / 'out')
        self.assertNotEqual(result.status, 'pass')
        summary = json.loads((self.root / 'out/report.json').read_text())
        self.assertEqual({item['stage'] for item in summary['stale']}, {'build', 'cube', 'verify'})
        self.assertNotIn('synthetic-revision', json.dumps(summary))

    def test_report_bound_pipeline_passes_only_at_the_observed_revision(self):
        self.bind()
        self.pipeline()
        conns, patch = self.connections('synthetic-revision-one', 'synthetic-revision-one')
        with patch:
            result = report.run(self.cfg, argparse.Namespace(), self.root / 'out')
        self.assertEqual(result.status, 'pass')
        self.assertEqual(result.provenance['binding'], 'bound')

    def test_report_missing_revision_evidence_cannot_reuse_old_pass(self):
        self.bind()
        self.pipeline()
        path = self.root / 'out/cube/result.json'
        saved = json.loads(path.read_text())
        saved.pop('provenance')
        path.write_text(json.dumps(saved), encoding='utf-8')
        conns, patch = self.connections('synthetic-revision-one', 'synthetic-revision-one')
        with patch:
            result = report.run(self.cfg, argparse.Namespace(), self.root / 'out')
        self.assertNotEqual(result.status, 'pass')
        self.assertEqual(next(c for c in result.checks if c.id == 'stage:cube').status, 'not_run')

    def test_report_probe_error_is_error_not_a_reused_pass(self):
        self.bind()
        self.pipeline()
        with mock.patch.object(db, 'connect', side_effect=RuntimeError('fixture raw SQL and token')):
            result = report.run(self.cfg, argparse.Namespace(), self.root / 'out')
        self.assertEqual(result.status, 'error')
        self.assertNotIn('fixture raw SQL and token', json.dumps(result.to_dict()))


@unittest.skipUnless(pgfixture.available(), 'SXC_TEST_ADMIN_DSN is not set')
class ProvenancePostgresTests(unittest.TestCase):
    """Synthetic PG fixture proves real read-only probes, scalar typing and boundary changes."""

    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture('sxc_provenance_guard')
        cls.addClassCleanup(cls.fx.teardown)
        cls.fx.setup()

    def setUp(self):
        os.environ['SXC_PG_DSN'] = pgfixture.dsn_for(self.fx.check, self.fx.name)
        self.temp = tempfile.TemporaryDirectory(prefix='sxc_provenance_pg_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = config.load(pgfixture.DEMO_CONFIG)
        self.cfg.path = self.root / 'crosscheck.toml'
        self.query = self.root / 'revision.sql'
        self.cfg.source.data_revision_sql_file = 'revision.sql'
        conn = self.fx.check_conn()
        try:
            with db.write_tx(conn):
                conn.run('DROP TABLE IF EXISTS sxc_out.fixture_data_revision')
                conn.run('CREATE TABLE sxc_out.fixture_data_revision (revision text NOT NULL)')
                conn.run("INSERT INTO sxc_out.fixture_data_revision VALUES ('fixture-revision-1')")
        finally:
            conn.close()
        self.query.write_text('SELECT revision FROM sxc_out.fixture_data_revision', encoding='utf-8')

    def read_rows(self):
        conn = self.fx.check_conn()
        try:
            with db.read_only(conn):
                return conn.run('SELECT revision FROM sxc_out.fixture_data_revision')
        finally:
            conn.close()

    def test_actual_text_revision_and_stable_boundary_pass_without_row_values(self):
        result = Result('verify')
        result.add('fixture', 'pass')
        start = provenance.begin(self.cfg, 'verify')
        provenance.finish(self.cfg, result, start)
        self.assertEqual(result.status, 'pass')
        self.assertTrue(result.provenance['stable'])
        self.assertNotIn('fixture-revision-1', json.dumps(result.to_dict()))

    def test_actual_postgres_rejects_probe_write_even_with_table_write_privilege(self):
        self.query.write_text("UPDATE sxc_out.fixture_data_revision SET revision='fixture-write-attempt' RETURNING revision", encoding='utf-8')
        with self.assertRaises(StageError) as raised:
            provenance.begin(self.cfg, 'verify')
        self.assertNotIn('fixture-write-attempt', str(raised.exception))
        self.assertEqual(self.read_rows(), [['fixture-revision-1']])

    def test_actual_postgres_rejects_integer_and_multiple_statements(self):
        for sql in ('SELECT 42', "SELECT 'a'::text; SELECT 'b'::text"):
            self.query.write_text(sql, encoding='utf-8')
            with self.subTest(sql=sql), self.assertRaises(StageError):
                provenance.begin(self.cfg, 'verify')

    def test_actual_revision_update_invalidates_otherwise_passing_comparisons(self):
        start = provenance.begin(self.cfg, 'verify')
        conn = self.fx.check_conn()
        try:
            with db.write_tx(conn):
                conn.run("UPDATE sxc_out.fixture_data_revision SET revision='fixture-revision-2'")
        finally:
            conn.close()
        result = Result('verify')
        result.add('fixture_cached_gold_comparison', 'pass')
        provenance.finish(self.cfg, result, start)
        self.assertEqual(result.status, 'fail')
        self.assertEqual(result.provenance['binding'], 'changed')
        self.assertNotIn('fixture-revision-2', json.dumps(result.to_dict()))


if __name__ == '__main__':
    unittest.main()
