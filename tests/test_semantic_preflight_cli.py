"""Offline preflight must not mutate inputs or disclose authored business text."""
import contextlib
import copy
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import db


CONTEXT = dict(unit='pieces', grain='part_id', population='stock', aggregation='sum',
               denominator='not_applicable', time_basis='snapshot', timezone='utc')
BINDING = dict(measure='stock', gold='stock', cube_member='m_stock.value')


def contract():
    return {'version': 1, 'metrics': [dict(id='stock', label='private-stock-label', aliases=['stock'],
        definition='private-authored-definition', review='reviewed', binding=BINDING.copy(),
        context=CONTEXT.copy(), period_kind='as_of')]}


def plan():
    return {'version': 1, 'selections': [dict(metric_id='stock', term='stock', binding=BINDING.copy(),
        context=CONTEXT.copy(), period={'kind': 'as_of', 'date': '2026-01-31'})]}


class PreflightCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='private-preflight-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / 'private-config.toml'
        self.contract = self.root / 'private-contract.json'
        self.plan = self.root / 'private-plan.json'
        self.out = self.root / 'out'
        (self.root / 'stock.sql').write_text('private-sql-canary', encoding='utf-8')
        self.cfg.write_text('''version = 1
[source]
schemas = ["sxc_demo"]
[entities.stock]
table = "sxc_demo.stock"
key = ["part_id"]
[measures.stock]
entity = "stock"
column = "qty"
agg = "sum"
by = "part_id"
[[gold]]
id = "stock"
sql = "stock.sql"
key = ["part_id"]
map = {part_id="m_stock.key", qty="m_stock.value"}
cube_query = {dimensions=["m_stock.key"], measures=["m_stock.value"]}
''', encoding='utf-8')
        self.contract.write_text(json.dumps(contract()), encoding='utf-8')
        self.plan.write_text(json.dumps(plan()), encoding='utf-8')

    def args(self):
        return ['semantic-preflight', '--config', str(self.cfg), '--contract', str(self.contract),
                '--plan', str(self.plan), '--out', str(self.out)]

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
        return code, console

    def saved(self):
        return json.loads((self.out / 'semantic_preflight' / 'result.json').read_text(encoding='utf-8'))

    def test_success_binds_only_captured_inputs_without_database_or_sql_reads(self):
        old_open = os.open
        old_read_bytes = Path.read_bytes
        def guarded_open(path, *args, **kwargs):
            self.assertFalse(str(path).endswith('.sql'), 'must not read SQL contents')
            return old_open(path, *args, **kwargs)
        def guarded_bytes(path):
            self.assertFalse(str(path).endswith('.sql'), 'must not fingerprint SQL contents')
            return old_read_bytes(path)
        with patch.object(db, 'connect', side_effect=AssertionError('no database')), \
                patch.object(sxc, 'inputs_fingerprint', side_effect=AssertionError('no generic fingerprint')), \
                patch('socket.create_connection', side_effect=AssertionError('no network')), \
                patch('os.open', side_effect=guarded_open), patch.object(Path, 'read_bytes', guarded_bytes):
            self.assertEqual(self.invoke()[0], 0)
        res = self.saved()
        self.assertEqual(res['stage'], 'semantic_preflight')
        for name, source in [('config', self.cfg), ('contract', self.contract), ('plan', self.plan)]:
            self.assertEqual(res['inputs'][name + '_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(res['status'], 'pass')
        self.assertNotIn('private-', json.dumps(res))
        self.assertNotIn(str(self.root), json.dumps(res))

    def test_incomplete_and_mismatch_are_distinct(self):
        doc = plan()
        del doc['selections'][0]['context']['denominator']
        self.plan.write_text(json.dumps(doc), encoding='utf-8')
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.saved()['status'], 'not_run')
        doc['selections'][0]['context']['denominator'] = 'different'
        self.plan.write_text(json.dumps(doc), encoding='utf-8')
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.saved()['status'], 'fail')

    def test_invalid_argv_is_sanitized_and_never_mutates_output(self):
        for extra in [ ['--unknown', 'private-canary'], ['--con', str(self.contract)],
                       ['--contract', str(self.plan)], ['--out', str(self.root)], ['--quiet', '--quiet'] ]:
            with self.subTest(extra=extra[0]):
                self.assertEqual(self.invoke(self.args() + extra)[0], 2)
                self.assertFalse(self.out.exists())
        self.assertEqual(self.invoke(['--unknown', 'private-canary', *self.args()])[0], 2)
        self.assertFalse(self.out.exists())

    def test_every_input_cannot_alias_output_or_gitignore(self):
        targets = [self.out / 'semantic_preflight' / 'result.json',
                   self.out / 'semantic_preflight' / 'result.json.part', self.out / '.gitignore']
        for attr in ('cfg', 'contract', 'plan'):
            source = getattr(self, attr)
            raw = source.read_bytes()
            for target in targets:
                with self.subTest(input=attr, target=target.name):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    setattr(self, attr, target)
                    self.assertEqual(self.invoke()[0], 2)
                    self.assertEqual(target.read_bytes(), raw)
                    setattr(self, attr, source)

    def test_linked_inputs_and_output_hardlinks_are_rejected_without_cleanup(self):
        target = self.out / 'semantic_preflight' / 'result.json'
        target.parent.mkdir(parents=True)
        os.link(self.contract, target)
        original = self.contract.read_bytes()
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.contract.read_bytes(), original)
        self.assertEqual(target.read_bytes(), original)

    def test_unrelated_hardlinked_output_is_preserved(self):
        original = self.root / 'unrelated.json'
        original.write_bytes(b'preserve this output alias')
        target = self.out / 'semantic_preflight' / 'result.json'
        target.parent.mkdir(parents=True)
        os.link(original, target)
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(target.read_bytes(), original.read_bytes())

    def test_linked_ancestors_are_rejected(self):
        parent = self.root / 'linked-parent'
        try:
            parent.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('symbolic link creation unavailable')
        self.out = parent / 'out'
        self.assertEqual(self.invoke()[0], 2)
        self.assertFalse((self.root / 'out').exists())

    def test_strict_json_errors_replace_stale_pass_without_echo(self):
        self.assertEqual(self.invoke()[0], 0)
        for raw in ['{"version":1,"version":1,"private-canary":0}',
                    '{"version":NaN}', '[[' * 40 + '0' + ']]' * 40,
                    json.dumps({'private-canary': [0] * 10001})]:
            self.contract.write_text(raw, encoding='utf-8')
            self.assertEqual(self.invoke()[0], 2)
            self.assertEqual(self.saved()['status'], 'error')
            self.assertNotIn('private-', json.dumps(self.saved()))

    def test_oversized_missing_or_invalid_utf8_input_never_publishes_pass(self):
        for raw in [b'x' * (256 * 1024 + 1), b'\xff']:
            self.contract.write_bytes(raw)
            self.assertEqual(self.invoke()[0], 2)
            target = self.out / 'semantic_preflight' / 'result.json'
            if target.exists():
                self.assertEqual(self.saved()['status'], 'error')
        self.contract.unlink()
        self.assertEqual(self.invoke()[0], 2)
        if (self.out / 'semantic_preflight' / 'result.json').exists():
            self.assertNotEqual(self.saved()['status'], 'pass')

    def test_windows_stat_clock_difference_is_not_a_file_identity_change(self):
        from semantic_crosscheck import semantic_preflight as module
        first = SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFREG, st_size=3,
                                st_mtime_ns=5, st_ctime_ns=4, st_nlink=1)
        second = copy.copy(first)
        second.st_ctime_ns = 5
        self.assertEqual(module._identity(first) == module._identity(second), os.name == 'nt')
        second.st_mtime_ns = 6
        self.assertNotEqual(module._identity(first), module._identity(second))

    def test_malformed_config_text_is_never_echoed(self):
        self.cfg.write_text('private-canary = { invalid syntax', encoding='utf-8')
        self.assertEqual(self.invoke()[0], 2)
        if (self.out / 'semantic_preflight' / 'result.json').exists():
            self.assertNotIn('private-', json.dumps(self.saved()))

    def test_input_changed_after_capture_cannot_publish_earlier_verdict(self):
        from semantic_crosscheck import semantic_preflight as module
        from semantic_crosscheck import business_contracts
        evaluate = business_contracts.evaluate
        def changing(*args):
            res = evaluate(*args)
            self.plan.write_text(json.dumps({'version': 1, 'selections': []}), encoding='utf-8')
            return res
        with patch.object(business_contracts, 'evaluate', side_effect=changing):
            self.assertEqual(self.invoke()[0], 2)
        self.assertFalse((self.out / 'semantic_preflight' / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
