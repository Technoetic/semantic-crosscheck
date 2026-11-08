"""Guarded opt-in SQL capture never leaks SQL, values, input paths or errors."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import db, sql_structure
from test_sql_structure import SQL, expectation


class SqlStructureCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='private-structure-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sql = self.root / 'private-sql.sql'
        self.expect = self.root / 'private-expectation.json'
        self.out = self.root / 'out'
        self.sql.write_text(SQL, encoding='utf-8')
        self.expect.write_text(json.dumps(expectation()), encoding='utf-8')

    def args(self):
        return ['sql-structure', '--sql', str(self.sql), '--expect', str(self.expect), '--out', str(self.out)]

    def invoke(self, args=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = sxc.main(self.args() if args is None else args)
            except SystemExit as exc:
                code = exc.code
        console = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn('private-', console)
        self.assertNotIn(str(self.root), console)
        self.assertNotIn('sales_order', console)
        return code, console

    def saved(self):
        return json.loads((self.out / 'sql_structure' / 'result.json').read_text(encoding='utf-8'))

    def test_missing_parser_is_environment_not_run(self):
        with patch.object(sql_structure, '_parser', return_value=None):
            self.assertEqual(self.invoke()[0], 3)
        self.assertEqual(self.saved()['status'], 'not_run')

    def test_strict_argument_errors_never_mutate_or_echo(self):
        for extra in [['--config', 'private-canary'], ['--sq', str(self.sql)],
                      ['--unknown', 'private-canary'], ['--sql', str(self.expect)],
                      ['--out', str(self.root)], ['--quiet', '--quiet']]:
            with self.subTest(extra=extra[0]):
                self.assertEqual(self.invoke(self.args() + extra)[0], 2)
                self.assertFalse(self.out.exists())
        self.assertEqual(self.invoke(['--private-canary', *self.args()])[0], 2)

    def test_unsafe_captures_and_outputs_do_not_mutate_inputs(self):
        target = self.out / 'sql_structure' / 'result.json'
        target.parent.mkdir(parents=True)
        original = self.sql.read_bytes()
        os.link(self.sql, target)
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.sql.read_bytes(), original)
        self.assertEqual(target.read_bytes(), original)

    def test_each_input_cannot_alias_output_or_gitignore(self):
        for attr in ('sql', 'expect'):
            source = getattr(self, attr)
            raw = source.read_bytes()
            for target in [self.out / 'sql_structure' / 'result.json',
                           self.out / 'sql_structure' / 'result.json.part', self.out / '.gitignore']:
                with self.subTest(input=attr, target=target.name):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    setattr(self, attr, target)
                    self.assertEqual(self.invoke()[0], 2)
                    self.assertEqual(target.read_bytes(), raw)
                    setattr(self, attr, source)

    def test_oversized_sql_and_invalid_json_do_not_save_a_pass(self):
        for raw in [b'x' * (64 * 1024 + 1), b'\xff']:
            self.sql.write_bytes(raw)
            self.assertEqual(self.invoke()[0], 2)
            self.assertEqual(self.saved()['status'], 'error')
        self.sql.write_text(SQL, encoding='utf-8')
        for raw in ['{"version":1,"version":1,"private-canary":0}', '{"version":NaN}',
                    '{"private-canary":' + '[' * 30 + '0' + ']' * 30 + '}']:
            self.expect.write_text(raw, encoding='utf-8')
            self.assertEqual(self.invoke()[0], 2)
            self.assertEqual(self.saved()['status'], 'error')

    @unittest.skipUnless(importlib.util.find_spec('pglast'), 'optional PostgreSQL AST parser not installed')
    def test_success_is_offline_and_binds_only_captured_inputs(self):
        with patch.object(db, 'connect', side_effect=AssertionError('no database')), \
                patch.object(sxc, 'inputs_fingerprint', side_effect=AssertionError('no operational fingerprint')), \
                patch('socket.create_connection', side_effect=AssertionError('no network')):
            self.assertEqual(self.invoke()[0], 0)
        result = self.saved()
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['inputs']['sql_sha256'], hashlib.sha256(self.sql.read_bytes()).hexdigest())
        self.assertEqual(result['inputs']['expectation_sha256'], hashlib.sha256(self.expect.read_bytes()).hexdigest())
        self.assertEqual(result['provenance']['binding'], 'not_applicable')
        self.assertEqual(result['runtime_evidence']['binding'], 'not_applicable')
        self.assertNotIn('private-', json.dumps(result))
        self.assertNotIn('sales_order', json.dumps(result))

    @unittest.skipUnless(importlib.util.find_spec('pglast'), 'optional PostgreSQL AST parser not installed')
    def test_mutation_cannot_publish_captured_verdict(self):
        evaluate = sql_structure.evaluate
        def changed(*args):
            result = evaluate(*args)
            self.sql.write_text('SELECT private-canary', encoding='utf-8')
            return result
        with patch.object(sql_structure, 'evaluate', side_effect=changed):
            self.assertEqual(self.invoke()[0], 2)
        self.assertFalse((self.out / 'sql_structure' / 'result.json').exists())

    @unittest.skipUnless(importlib.util.find_spec('pglast'), 'optional PostgreSQL AST parser not installed')
    def test_input_change_during_result_write_removes_own_passing_receipt(self):
        original_write = sql_structure.Result.write
        for source in (self.sql, self.expect):
            with self.subTest(input=source.name):
                self.sql.write_text(SQL, encoding='utf-8')
                self.expect.write_text(json.dumps(expectation()), encoding='utf-8')
                def changed(result, out):
                    target = original_write(result, out)
                    source.write_text('private-canary changed', encoding='utf-8')
                    return target
                with patch.object(sql_structure.Result, 'write', changed):
                    self.assertEqual(self.invoke()[0], 2)
                self.assertFalse((self.out / 'sql_structure' / 'result.json').exists())

    @unittest.skipUnless(importlib.util.find_spec('pglast'), 'optional PostgreSQL AST parser not installed')
    def test_late_change_does_not_delete_a_replaced_receipt(self):
        original_write = sql_structure.Result.write
        replacement = b'private-canary another writer\n'
        def changed(result, out):
            target = original_write(result, out)
            self.sql.write_text('SELECT qty FROM stock LIMIT 1', encoding='utf-8')
            target.write_bytes(replacement)
            return target
        with patch.object(sql_structure.Result, 'write', changed):
            self.assertEqual(self.invoke()[0], 2)
        self.assertEqual((self.out / 'sql_structure' / 'result.json').read_bytes(), replacement)

    @unittest.skipUnless(importlib.util.find_spec('pglast'), 'optional PostgreSQL AST parser not installed')
    def test_stale_pass_replaced_by_mismatch_and_unsupported_results(self):
        self.assertEqual(self.invoke()[0], 0)
        self.sql.write_text(SQL.replace('sum(qty)', 'count(qty)'), encoding='utf-8')
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.saved()['status'], 'fail')
        self.sql.write_text('SELECT private-canary FROM', encoding='utf-8')
        with patch.dict(os.environ, {'SXC_DEBUG': '1'}):
            self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.saved()['status'], 'not_run')
        self.assertNotIn('private-', json.dumps(self.saved()))
