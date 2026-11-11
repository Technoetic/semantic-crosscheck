"""Saved evidence cannot pass by discarding malformed checks or metadata."""

import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path

import pgfixture  # noqa: F401 - supplies the checkout's import path
from semantic_crosscheck import config, report
from semantic_crosscheck.result import Result, check_status, inputs_fingerprint, load_result


class SavedResultContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sxc_result_contract_')
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.path = self.out / 'build' / 'result.json'
        self.path.parent.mkdir()
        self.valid = {
            'stage': 'build', 'status': 'pass',
            'checks': [{'id': 'closure:synthetic', 'status': 'pass', 'detail': {}, 'config_ref': ''}],
            'not_proven': [], 'next_safe_action': '', 'inputs': {},
            'started_at': '2000-01-01T00:00:00+0000', 'finished_at': '2000-01-01T00:00:01+0000',
        }

    def save(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')

    def assert_invalid(self, value):
        self.save(value)
        loaded = report.read_results(self.out)['build']
        self.assertEqual(report.stage_status(loaded), 'error')
        self.assertEqual(report.counts(loaded), {'pass': 0, 'fail': 0, 'error': 1, 'not_run': 0})
        self.assertEqual([(c['id'], c['status']) for c in loaded['checks']], [('result_file', 'error')])
        self.assertNotIn('untrusted-marker', json.dumps(loaded))

    def test_malformed_check_is_never_a_passing_filtered_subset(self):
        for entry in (None, 'untrusted-marker', 1, [], True):
            with self.subTest(entry=entry):
                value = copy.deepcopy(self.valid)
                value['checks'].append(entry)
                self.assert_invalid(value)

    def test_missing_wrong_or_empty_checks_cannot_establish_pass(self):
        for checks in (None, {}, 'untrusted-marker', 1):
            with self.subTest(checks=checks):
                self.assert_invalid(dict(self.valid, checks=checks))
        value = dict(self.valid)
        del value['checks']
        self.assert_invalid(value)
        self.save(dict(self.valid, checks=[]))
        self.assertEqual(report.stage_status(report.read_results(self.out)['build']), 'not_run')

    def test_check_ids_statuses_and_known_field_types_are_validated(self):
        malformed = [
            {}, {'id': '', 'status': 'pass'}, {'id': '  ', 'status': 'pass'},
            {'id': 1, 'status': 'pass'}, {'id': 'x'}, {'id': 'x', 'status': None},
            {'id': 'x', 'status': ['pass']}, {'id': 'x', 'status': 'unknown'},
            {'id': 'x', 'status': 'pass', 'detail': []},
            {'id': 'x', 'status': 'pass', 'config_ref': None},
        ]
        for entry in malformed:
            with self.subTest(entry=entry):
                self.assert_invalid(dict(self.valid, checks=self.valid['checks'] + [entry]))
        self.assert_invalid(dict(self.valid, checks=self.valid['checks'] * 2))

    def test_envelope_versions_stage_and_field_types_are_validated(self):
        fields = {
            'schema_version': (0, 2, True, '1', None),
            'stage': ('', 1, 'cube'), 'status': (None, True, [], 'unknown'),
            'not_proven': (None, {}, [None]), 'next_safe_action': (None, []),
            'started_at': (None, 1), 'finished_at': (None, 1),
            'inputs': (None, []), 'provenance': (None, []), 'runtime_evidence': (None, []),
        }
        for field, values in fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_invalid(dict(self.valid, **{field: value}))

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        raw_cases = [
            '{"checks":[{"id":"x","status":"fail","status":"pass"}]}',
            '{"checks":[{"id":"x","status":"pass","detail":{"n":NaN}}]}',
            '{"checks":[{"id":"x","status":"pass","detail":{"n":Infinity}}]}',
            '{"checks":[{"id":"x","status":"pass","detail":{"n":1e9999}}]}',
        ]
        for raw in raw_cases:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding='utf-8')
                self.assertEqual(report.stage_status(report.read_results(self.out)['build']), 'error')
                with self.assertRaises(ValueError) as caught:
                    load_result(self.out, 'build')
                self.assertNotIn(raw, str(caught.exception))

    def test_invalid_utf8_is_a_sanitized_result_error(self):
        self.path.write_bytes(b'{"untrusted-marker":"\xff"}')
        with self.assertRaises(ValueError) as caught:
            load_result(self.out, 'build')
        self.assertEqual(str(caught.exception), 'invalid saved result')
        self.assertEqual(report.stage_status(report.read_results(self.out)['build']), 'error')

    def test_direct_aggregation_fails_closed_instead_of_ignoring_bad_records(self):
        invalid = [None, [], {'status': 'pass'}, {'checks': None},
                   {'checks': [{'id': 'x', 'status': 'pass'}, None]},
                   {'checks': [{'id': 'x', 'status': 'pass'}] * 2},
                   {'checks': [{'id': 'x', 'status': 'pass'}], 'status': []}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(report.stage_status(value), 'error')
                self.assertEqual(report.counts(value), {'pass': 0, 'fail': 0, 'error': 1, 'not_run': 0})

    def test_legacy_and_v1_results_keep_status_counts_and_prerequisite_lookup(self):
        for version in (None, 1):
            value = copy.deepcopy(self.valid)
            if version is not None:
                value['schema_version'] = version
            self.save(value)
            self.assertEqual(load_result(self.out, 'build'), value)
            self.assertEqual(check_status(self.out, 'build', 'closure:synthetic'), 'pass')
            self.assertEqual(report.stage_status(value), 'pass')
            self.assertEqual(report.counts(value), {'pass': 1, 'fail': 0, 'error': 0, 'not_run': 0})
        # Existing in-memory synthetic receipts omit optional envelope/check fields.
        self.assertEqual(report.stage_status({'checks': [{'id': 'x', 'status': 'pass'}]}), 'pass')
        # Missing legacy timestamps remain a freshness question rather than structural evidence.
        del value['started_at']
        self.save(value)
        self.assertEqual(report.stage_status(report.read_results(self.out)['build']), 'pass')

    def test_invalid_optional_result_counts_as_error_and_redacts_untrusted_text(self):
        self.save(dict(self.valid, checks=self.valid['checks'] + ['untrusted-marker']))
        self.path.parent.rename(self.out / 'doctor')
        result = report.run(None, argparse.Namespace(), self.out)
        self.assertEqual(result.status, 'error')
        summary = json.loads((self.out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['stages']['doctor']['status'], 'error')
        self.assertNotIn('untrusted-marker', json.dumps(summary))
        self.assertNotIn('untrusted-marker', (self.out / 'report.md').read_text(encoding='utf-8'))

    def test_new_writer_declares_supported_version(self):
        result = Result('build')
        result.add('x', 'pass')
        path = result.write(self.out)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['schema_version'], 1)
        self.assertEqual(load_result(self.out, 'build'), saved)

    def test_writer_without_checks_cannot_establish_pass(self):
        result = Result('build')
        self.assertEqual((result.status, result.exit_code()), ('not_run', 1))
        saved = json.loads(result.write(self.out).read_text(encoding='utf-8'))
        self.assertEqual((saved['status'], saved['checks']), ('not_run', []))
        # One passing check is enough to pass: the verdict above comes from the empty check list alone.
        result.add('x', 'pass')
        self.assertEqual((result.status, result.exit_code()), ('pass', 0))

    def test_malformed_optional_reason_cannot_disappear_as_stale(self):
        cfg_path = self.out / 'plain.toml'
        cfg_path.write_text('version = 1\n[source]\nschemas = ["src"]\n'
                            '[entities.item]\ntable = "src.item"\nkey = ["id"]\n', encoding='utf-8')
        cfg = config.load(cfg_path)
        fingerprints = inputs_fingerprint(cfg)
        for index, stage in enumerate(('build', 'cube', 'verify')):
            result = Result(stage, started_at=f'2000-01-01T00:00:0{index}+0000',
                            finished_at=f'2000-01-01T00:00:0{index}+0000')
            result.add('synthetic', 'pass')
            saved = result.to_dict()
            saved['inputs'] = fingerprints
            target = self.out / stage / 'result.json'
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(saved), encoding='utf-8')
        reason = self.out / 'reason' / 'result.json'
        reason.parent.mkdir()
        reason.write_text(json.dumps({'stage': 'reason', 'checks': [{'id': 'x', 'status': 'pass'}, None]}),
                          encoding='utf-8')
        result = report.run(cfg, argparse.Namespace(), self.out)
        self.assertEqual(result.status, 'error')
        self.assertEqual(next(check.status for check in result.checks if check.id == 'stage:reason'), 'error')


if __name__ == '__main__':
    unittest.main()
