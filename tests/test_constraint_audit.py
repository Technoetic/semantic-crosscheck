"""Default-off audit contract: declarations, bindings, bounds and privacy."""
import contextlib
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pgfixture  # noqa: F401
import sxc
from semantic_crosscheck import constraints, counterexample as ce, witness


class AuditInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write('reference.sql', 'SELECT count(id) AS n FROM items')
        self.write('candidate.sql', 'SELECT count(qty) AS n FROM items')
        self.write_json('schema.json', {'version': 1, 'tables': {'items': {'rows': [0, 2], 'columns': [
            {'name': 'id', 'type': 'integer', 'values': [1, 2]},
            {'name': 'qty', 'type': 'integer', 'values': [None, 0]}]}}})
        self.profile({'version': 1, 'tables': {'items': {'primary_key': ['id'], 'not_null': ['qty']}}},
                     ['/tables/items/primary_key', '/tables/items/not_null/0'], ['/tables/items/not_null/0'])

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def write_json(self, name, value):
        return self.write(name, json.dumps(value))

    def profile(self, document, selectors, relax):
        base = self.write_json('constraints.json', document)
        self.manifest = {'version': 1, 'constraints_sha256': hashlib.sha256(base.read_bytes()).hexdigest(),
                         'provenance': [{'rule': rule, 'source': 'observed'} for rule in selectors], 'relax': relax}
        self.write_json('audit.json', self.manifest)

    def args(self, **changes):
        values = dict(reference_sql=str(self.root / 'reference.sql'), candidate_sql=str(self.root / 'candidate.sql'),
                      constraints=str(self.root / 'constraints.json'), witness_schema=str(self.root / 'schema.json'),
                      constraint_audit=str(self.root / 'audit.json'), scenario=None, seed=7, max_examples=24,
                      max_shrink_steps=128, solver_timeout_ms=3000, statement_timeout_ms=1000,
                      shrink_strategy='rows', dsn_env='SXC_TEST_UNUSED', replay=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def run_fake(self, args=None, answer=None):
        if answer is None:
            answer = lambda _c, f, *_a, **_kw: {'equal': not any(r[1] is None for r in f['items'])}
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
                patch.object(ce, 'evaluate', side_effect=answer):
            return ce.run(None, args or self.args(), self.root)

    def check(self, result, arm):
        return next(c for c in result.checks if c.id == 'counterexample:audit_' + arm)

    def artifact(self, arm='relaxed'):
        return self.root / ('counterexample/audit/' + arm + '-fixture.json')


class ConstraintAuditTest(AuditInputs):
    def test_cli_is_explicit_and_absence_preserves_legacy_default(self):
        self.assertIsNone(getattr(sxc.parser().parse_args(['counterexample']), 'constraint_audit', 'missing'))
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = sxc.parser().parse_args(['counterexample', '--constraint-audit', 'audit.json'])
            except SystemExit:
                self.fail('The explicit audit flag must be supported')
        self.assertEqual(args.constraint_audit, 'audit.json')
        legacy = self.args(constraint_audit=None)
        self.assertEqual(self.run_fake(legacy).status, 'not_run')
        self.assertFalse(self.artifact().exists())

    def test_not_null_masks_mismatch_and_outside_only_is_diagnostic(self):
        result = self.run_fake()
        self.assertEqual(result.status, 'not_run')
        self.assertEqual(result.exit_code(), 1)
        self.assertFalse(self.check(result, 'declared').detail['found'])
        self.assertEqual(self.check(result, 'relaxed').detail['classification'], 'outside_declared_domain')
        payload = json.loads(self.artifact().read_text())
        self.assertEqual(payload['format'], 'sxc-synthetic-constraint-audit')
        self.assertTrue(any(row[1] is None for row in payload['tables']['items']))
        self.assertFalse(self.artifact('declared').exists())

    def test_declared_disagreement_is_fail_and_identical_sql_is_never_pass(self):
        self.assertEqual(self.run_fake(answer=lambda *_a, **_kw: {'equal': False}).status, 'fail')
        result = self.run_fake(answer=lambda *_a, **_kw: {'equal': True})
        self.assertEqual(result.status, 'not_run')
        self.assertTrue(all(not c.detail.get('found') for c in result.checks))
        self.assertFalse(self.artifact().exists())
        self.assertFalse(self.artifact('declared').exists())

    def test_manifest_rejections_happen_before_connection(self):
        variants = []
        for field, value in [('version', True), ('constraints_sha256', '0' * 64), ('provenance', []),
                             ('relax', []), ('relax', ['/checks/99']),
                             ('relax', ['/tables/items/not_null/00']),
                             ('relax', ['/tables/items/not_null/0'] * 2), ('extra', 'SECRET')]:
            variants.append(dict(self.manifest, **{field: value}))
        variants += [dict(self.manifest, provenance=self.manifest['provenance'] * 2),
                     dict(self.manifest, provenance=[{'rule': '/tables/items/primary_key', 'source': 'truth'},
                                                    self.manifest['provenance'][1]]),
                     dict(self.manifest, provenance=[dict(self.manifest['provenance'][0], note='SECRET'),
                                                    self.manifest['provenance'][1]])]
        for document in variants:
            self.write_json('audit.json', document)
            with self.subTest(document=document), patch.object(ce.db, 'connect') as connect:
                with self.assertRaises(ce.StageError) as error:
                    ce.run(None, self.args(), self.root)
                self.assertNotIn('SECRET', str(error.exception))
                connect.assert_not_called()
        for raw in ['{"version":1,"version":1}', '{"version":NaN}']:
            self.write('audit.json', raw)
            with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
                ce.run(None, self.args(), self.root)
            connect.assert_not_called()

    def test_modes_and_missing_inputs_rejected_before_connection(self):
        for change in [dict(scenario='nulls'), dict(metamorphic_sql='x'), dict(mutation_operator='x'),
                       dict(gold_sql='x'), dict(constraints=None), dict(witness_schema=None),
                       dict(candidate_sql=None), dict(shrink_strategy='structured')]:
            with self.subTest(change=change), patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
                ce.run(None, self.args(**change), self.root)
            connect.assert_not_called()

    def test_primary_key_group_keeps_explicit_not_null_and_rejects_redundant_relaxation(self):
        document = {'version': 1, 'tables': {'items': {'primary_key': ['id'], 'not_null': ['id']}}}
        selectors = ['/tables/items/primary_key', '/tables/items/not_null/0']
        self.profile(document, selectors, ['/tables/items/primary_key'])
        self.run_fake(answer=lambda *_a, **_kw: {'equal': True})
        projected = constraints.load(self.root / 'counterexample/audit/relaxed-constraints.json', witness.load(self.args().witness_schema).schema)
        self.assertEqual(projected.not_null, (('items', 0),))
        self.assertEqual(projected.unique, ())
        self.profile(document, selectors, ['/tables/items/not_null/0'])
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, self.args(), self.root)
        connect.assert_not_called()

    def test_fk_dependency_requires_explicit_relaxation_and_overlapping_unique_retains_key(self):
        self.write_json('schema.json', {'version': 1, 'tables': {name: {'rows': [0, 1], 'columns': [
            {'name': 'id', 'type': 'integer', 'values': [None, 1]}]} for name in ('items', 'other')}})
        document = {'version': 1, 'tables': {'items': {'primary_key': ['id']}}, 'foreign_keys': [
            {'table': 'other', 'columns': ['id'], 'references': {'table': 'items', 'columns': ['id']}}]}
        selectors = ['/tables/items/primary_key', '/foreign_keys/0']
        self.profile(document, selectors, ['/tables/items/primary_key'])
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, self.args(), self.root)
        connect.assert_not_called()
        self.profile(document, selectors, selectors)
        self.assertEqual(self.run_fake(answer=lambda *_a, **_kw: {'equal': True}).status, 'not_run')
        # Existing v1 disallows the same PK and UNIQUE key; it must stay rejected.
        document['tables']['items']['unique'] = [['id']]
        self.profile(document, selectors + ['/tables/items/unique/0'], ['/tables/items/primary_key'])
        with self.assertRaises(ce.StageError):
            self.run_fake()

    def test_all_provenance_categories_are_claims_and_summary_has_only_opaque_rule_ids(self):
        for source in ('catalog', 'human', 'observed', 'inferred'):
            for entry in self.manifest['provenance']:
                entry['source'] = source
            self.write_json('audit.json', self.manifest)
            result = self.run_fake()
            summary = json.dumps(result.to_dict()) + sxc._summary(result, self.root / 'SECRET-PATH', False)
            for forbidden in ('/tables/', 'SECRET-PATH', str(self.root), 'SELECT', 'qty', 'items'):
                self.assertNotIn(forbidden, summary)
            self.assertIn('r0001', summary)

    def test_two_non_none_profiles_equal_limits_and_outside_shrink_invariant(self):
        from semantic_crosscheck import constraint_audit
        actual = witness.generate
        calls = []
        def generate(spec, profile, **kw):
            calls.append((spec, profile, kw))
            return actual(spec, profile, **kw)
        with patch.object(witness, 'generate', side_effect=generate):
            result = self.run_fake()
        self.assertIs(calls[0][0], calls[1][0])
        self.assertTrue(all(call[1] is not None for call in calls))
        self.assertEqual(calls[0][2], calls[1][2])
        detail = self.check(result, 'relaxed').detail
        self.assertLessEqual(detail['attempted_examples'], 24)
        self.assertLessEqual(detail['shrink_steps'], 128)
        payload = json.loads(self.artifact().read_text())
        self.assertTrue(any(row[1] is None for row in payload['tables']['items']))

    def test_solver_incomplete_and_query_errors_never_become_witnesses(self):
        for reason in ('solver_timeout', 'solver_unavailable', 'solver_unknown'):
            with patch.object(witness, 'generate', return_value=witness.Generation((), reason, 1)):
                result = self.run_fake()
            self.assertEqual(result.status, 'not_run')
            self.assertTrue(all(c.detail['reason'] == reason for c in result.checks))
        with patch.object(witness, 'generate', return_value=witness.Generation(({'items': [[1, None]]},), 'solver_timeout', 1)):
            result = self.run_fake(answer=lambda *_a, **_kw: {'equal': False})
        self.assertEqual(result.status, 'not_run')
        self.assertFalse(self.artifact().exists())
        def error(*_a, **_kw):
            raise ValueError('SECRET-SQL-path-cell')
        result = self.run_fake(answer=error)
        self.assertEqual(result.status, 'error')
        self.assertNotIn('SECRET', json.dumps(result.to_dict()))
        self.assertFalse(self.artifact().exists())

    def test_every_input_revalidated_even_after_no_witness_and_stale_fixtures_removed(self):
        for filename in ('reference.sql', 'candidate.sql', 'schema.json', 'constraints.json', 'audit.json'):
            self.setUp()
            self.run_fake()
            modified = False
            def equal(*_a, **_kw):
                nonlocal modified
                if not modified:
                    path = self.root / filename
                    path.write_bytes(path.read_bytes() + b'\n')
                    modified = True
                return {'equal': True}
            result = self.run_fake(answer=equal)
            self.assertEqual(result.status, 'error', filename)
            self.assertFalse(self.artifact().exists())

    def test_replay_twice_and_all_binding_mismatches_fail_before_connection(self):
        self.run_fake()
        original = self.artifact().read_bytes()
        args = self.args(replay=str(self.artifact()))
        for _ in range(2):
            self.assertEqual(self.run_fake(args).status, 'not_run')
        payload = json.loads(original)
        mutations = [dict(payload, format=ce.FORMAT), dict(payload, arm='declared'),
                     dict(payload, classification='declared_domain_disagreement'),
                     dict(payload, seed=True), dict(payload, case_index=True),
                     dict(payload, sql_sha256={'reference': '0' * 64, 'candidate': '0' * 64}),
                     dict(payload, generator=dict(payload['generator'], schema_sha256='0' * 64)),
                     dict(payload, audit=dict(payload['audit'], manifest_sha256='0' * 64)),
                     dict(payload, audit=dict(payload['audit'], projected_constraints_sha256='0' * 64)),
                     dict(payload, audit=dict(payload['audit'], original_constraints_sha256='0' * 64)),
                     dict(payload, original_tables={'items': []}),
                     dict(payload, tables={'items': [[1, 0]]})]
        for changed in mutations:
            self.artifact().write_text(json.dumps(changed))
            with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
                ce.run(None, args, self.root)
            connect.assert_not_called()
        self.artifact().write_bytes(original)
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, self.args(constraint_audit=None, replay=str(self.artifact())), self.root)
        connect.assert_not_called()

    def test_original_row_bounds_and_finite_domain_revalidated_on_replay(self):
        spec = json.loads((self.root / 'schema.json').read_text())
        spec['tables']['items']['rows'] = [2, 2]
        self.write_json('schema.json', spec)
        self.run_fake()
        payload = json.loads(self.artifact().read_text())
        self.assertEqual(len(payload['tables']['items']), 2)
        payload['original_tables']['items'].pop()
        self.artifact().write_text(json.dumps(payload))
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, self.args(replay=str(self.artifact())), self.root)
        connect.assert_not_called()

    def test_cli_preserves_binding_and_suppresses_paths_on_success_and_error(self):
        argv = ['counterexample', '--constraint-audit', str(self.root / 'audit.json'), '--constraints', str(self.root / 'constraints.json'),
                '--witness-schema', str(self.root / 'schema.json'), '--reference-sql', str(self.root / 'reference.sql'),
                '--candidate-sql', str(self.root / 'candidate.sql'), '--out', str(self.root), '--max-examples', '24']
        for bad in (False, True):
            if bad:
                self.write('audit.json', '{}')
            output = io.StringIO()
            with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
                    patch.object(ce, 'evaluate', return_value={'equal': True}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(sxc.main(argv), 2 if bad else 1)
            self.assertNotIn(str(self.root), output.getvalue())
            result = json.loads((self.root / 'counterexample/result.json').read_text())
            self.assertIn('constraint_audit', result['inputs'])

    def test_empty_projected_profile_still_uses_c_collation_and_retains_schema(self):
        self.profile({'version': 1, 'tables': {'items': {'not_null': ['qty']}}},
                     ['/tables/items/not_null/0'], ['/tables/items/not_null/0'])
        profiles = []
        def answer(_c, f, _r, _q, _t, profile, *, schema):
            profiles.append(profile)
            sql, _ = ce.wrap_sql('SELECT * FROM items', f, profile=profile, schema=schema)
            self.assertIn('integer', sql)
            return {'equal': not any(r[1] is None for r in f['items'])}
        self.run_fake(answer=answer)
        self.assertTrue(all(p is not None for p in profiles))
        self.assertEqual(profiles[-1].not_null, ())

    def test_pending_witness_is_confirmed_after_both_arms(self):
        seen_declared = 0
        started_relaxed = False
        def answer(_c, _f, _r, _q, _t, profile, **_kw):
            nonlocal started_relaxed, seen_declared
            if ('items', 1) not in profile.not_null:
                started_relaxed = True
            elif started_relaxed:
                seen_declared += 1
                return {'equal': True}  # previously found witness is no longer stable
            return {'equal': False}
        result = self.run_fake(answer=answer)
        self.assertGreater(seen_declared, 0)
        self.assertEqual(result.status, 'error')
        self.assertFalse(self.artifact('declared').exists())

    def test_actual_changed_inputs_reject_replay_before_connection(self):
        self.run_fake()
        original = self.artifact().read_bytes()
        for filename in ('reference.sql', 'candidate.sql', 'schema.json', 'constraints.json', 'audit.json'):
            path = self.root / filename
            before = path.read_bytes()
            path.write_bytes(before + b'\n')
            self.artifact().write_bytes(original)
            with self.subTest(filename=filename), patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
                ce.run(None, self.args(replay=str(self.artifact())), self.root)
            connect.assert_not_called()
            path.write_bytes(before)

    def test_output_aliases_never_delete_or_overwrite_inputs(self):
        target = self.artifact('declared')
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = b'SELECT 42 AS n'
        target.write_bytes(raw)
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, self.args(reference_sql=str(target)), self.root)
        connect.assert_not_called()
        self.assertEqual(target.read_bytes(), raw)

    def test_no_witness_and_incomplete_have_distinct_classifications(self):
        result = self.run_fake(answer=lambda *_a, **_kw: {'equal': True})
        self.assertTrue(all(c.detail['classification'] == 'bounded_no_witness' for c in result.checks))
        with patch.object(witness, 'generate', return_value=witness.Generation((), 'solver_timeout', 1)):
            result = self.run_fake()
        self.assertTrue(all(c.detail['classification'] == 'incomplete' for c in result.checks))

    def test_multiple_list_removals_keep_original_selector_indices(self):
        self.profile({'version': 1, 'tables': {'items': {'not_null': ['id', 'qty']}}, 'checks': [
            {'table': 'items', 'column': 'qty', 'op': 'ge', 'value': 0},
            {'table': 'items', 'column': 'qty', 'op': 'le', 'value': 1},
            {'table': 'items', 'column': 'qty', 'op': 'ne', 'value': 2}]},
            ['/tables/items/not_null/0', '/tables/items/not_null/1', '/checks/0', '/checks/1', '/checks/2'],
            ['/tables/items/not_null/0', '/checks/0', '/checks/2'])
        self.run_fake(answer=lambda *_a, **_kw: {'equal': True})
        projected = json.loads((self.root / 'counterexample/audit/relaxed-constraints.json').read_text())
        self.assertEqual(projected['tables']['items']['not_null'], ['qty'])
        self.assertEqual(projected['checks'], [{'table': 'items', 'column': 'qty', 'op': 'le', 'value': 1}])

    def test_domain_change_during_no_witness_invalidates_audit(self):
        def answer(*_a, **_kw):
            path = self.root / 'counterexample/audit/relaxed-constraints.json'
            path.write_bytes(path.read_bytes() + b'\n')
            return {'equal': True}
        result = self.run_fake(answer=answer)
        self.assertEqual(result.status, 'error')
        self.assertFalse(self.artifact().exists())

    def test_numeric_check_projection_preserves_exact_decimal_literal(self):
        schema = json.loads((self.root / 'schema.json').read_text())
        schema['tables']['items']['columns'][1] = {'name': 'qty', 'type': 'numeric(38,4)',
            'values': [None, '12345678901234567890123456789.0001']}
        self.write_json('schema.json', schema)
        raw = ('{"version":1,"tables":{"items":{"primary_key":["id"],"not_null":["qty"]}},'
               '"checks":[{"table":"items","column":"qty","op":"ge",'
               '"value":12345678901234567890123456789.0001}]}')
        self.write('constraints.json', raw)
        self.manifest['constraints_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
        self.manifest['provenance'].append({'rule': '/checks/0', 'source': 'human'})
        self.write_json('audit.json', self.manifest)
        self.assertEqual(self.run_fake().status, 'not_run')
        profile = constraints.load(self.root / 'counterexample/audit/relaxed-constraints.json', witness.load(self.args().witness_schema).schema)
        self.assertEqual(profile.checks[0][3][0], Decimal('12345678901234567890123456789.0001'))

    def test_replay_rejects_structured_metadata_and_changed_budgets(self):
        self.run_fake()
        original = self.artifact().read_bytes()
        for change in (dict(max_examples=23), dict(max_shrink_steps=127), dict(solver_timeout_ms=2999),
                       dict(statement_timeout_ms=1001), dict(seed=8)):
            self.artifact().write_bytes(original)
            with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
                ce.run(None, self.args(replay=str(self.artifact()), **change), self.root)
            connect.assert_not_called()
        payload = json.loads(original)
        payload['shrink']['trace'] = []
        self.artifact().write_text(json.dumps(payload))
        with patch.object(ce.db, 'connect') as connect, self.assertRaises(ce.StageError):
            ce.run(None, self.args(replay=str(self.artifact())), self.root)
        connect.assert_not_called()

    def test_shrink_query_errors_never_publish_a_witness(self):
        def answer(_c, fixture, *_a, **_kw):
            count = len(fixture['items'])
            if count == 1:
                raise ValueError('SECRET query failure')
            return {'equal': count == 0}
        result = self.run_fake(answer=answer)
        self.assertEqual(result.status, 'error')
        self.assertFalse(self.artifact().exists())
        self.assertFalse(self.artifact('declared').exists())

    def test_cli_rejects_result_file_alias_without_overwriting_source(self):
        target = self.root / 'counterexample/result.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'SELECT 42 AS n')
        argv = ['counterexample', '--constraint-audit', str(self.root / 'audit.json'),
                '--reference-sql', str(target), '--out', str(self.root)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sxc.main(argv), 2)
        self.assertEqual(target.read_bytes(), b'SELECT 42 AS n')

    def test_audit_parser_errors_are_sanitized_for_every_accepted_audit_spelling(self):
        sentinel = 'D:/PRIVATE-PARSER-VALUE'
        for flag in ('--constraint-audit', '--constraint-aud'):
            for audit_option in ([flag, 'audit.json'], [flag + '=audit.json']):
                for invalid in (['--seed', sentinel], ['--shrink-strategy', sentinel],
                                ['--UNKNOWN-' + sentinel], ['--seed']):
                    output = io.StringIO()
                    with self.subTest(option=audit_option, invalid=invalid), \
                            contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        try:
                            code = sxc.main(['counterexample', *audit_option, '--out', str(self.root), *invalid])
                        except SystemExit as error:
                            code = error.code
                    self.assertEqual(code, 2)
                    self.assertNotIn(sentinel, output.getvalue())
                    self.assertNotIn(str(self.root), output.getvalue())

    def test_ordinary_parser_errors_keep_their_existing_output_and_system_exit(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as caught:
            sxc.main(['counterexample', '--seed', 'ORDINARY-PARSER-VALUE'])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('ORDINARY-PARSER-VALUE', output.getvalue())

    def test_cli_preflight_and_parser_failure_clear_all_prior_witness_outputs(self):
        manifest = str(self.root / 'audit.json')
        for audit_option in (['--constraint-audit', manifest], ['--constraint-audit=' + manifest],
                             ['--constraint-aud', manifest], ['--constraint-aud=' + manifest]):
            for invalid in (['--config', 'unused.toml'], ['--seed', 'PRIVATE'],
                            ['--shrink-strategy', 'PRIVATE'], ['--UNKNOWN-PRIVATE']):
                previous = self.run_fake(answer=lambda *_a, **_kw: {'equal': False})
                previous.write(self.root)
                self.assertTrue(any(c.detail.get('found') for c in previous.checks))
                ordinary = self.root / 'counterexample/fixture.json'
                ordinary.write_bytes(b'{"old":true}')
                self.assertTrue(self.artifact('declared').exists())
                self.assertTrue(self.artifact('relaxed').exists())
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    try:
                        code = sxc.main(['counterexample', *audit_option, '--ou', str(self.root), *invalid])
                    except SystemExit as error:
                        code = error.code
                self.assertEqual(code, 2)
                self.assertFalse(ordinary.exists())
                self.assertFalse(self.artifact('declared').exists())
                self.assertFalse(self.artifact('relaxed').exists())
                self.assertNotIn('PRIVATE', output.getvalue())
                result = json.loads((self.root / 'counterexample/result.json').read_text())
                self.assertEqual(result['status'], 'error')
                self.assertFalse(any(c['detail'].get('found') for c in result['checks']))

    def test_parse_error_cleanup_preserves_aliased_source_and_recognizes_abbreviated_out(self):
        ordinary = self.root / 'counterexample/fixture.json'
        ordinary.parent.mkdir(parents=True, exist_ok=True)
        source = b'SELECT 42 AS n'
        ordinary.write_bytes(source)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = sxc.main(['counterexample', '--constraint-aud=audit.json', '--ou', str(self.root),
                                 '--ref', str(ordinary), '--seed', 'PRIVATE'])
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, 2)
        self.assertEqual(ordinary.read_bytes(), source)
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_cli_same_output_replay_survives_early_cleanup(self):
        self.run_fake()
        argv = ['counterexample', '--constraint-audit', str(self.root / 'audit.json'), '--constraints', str(self.root / 'constraints.json'),
                '--witness-schema', str(self.root / 'schema.json'), '--reference-sql', str(self.root / 'reference.sql'),
                '--candidate-sql', str(self.root / 'candidate.sql'), '--out', str(self.root), '--max-examples', '24',
                '--max-shrink-steps', '128', '--solver-timeout-ms', '3000', '--statement-timeout-ms', '1000',
                '--replay', str(self.artifact())]
        with patch.object(ce.db, 'connect', return_value=SimpleNamespace(close=lambda: None)), \
                patch.object(ce, 'evaluate', return_value={'equal': False}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sxc.main(argv), 1)
        payload = json.loads(self.artifact().read_text())
        self.assertEqual(payload['classification'], 'outside_declared_domain')


if __name__ == '__main__':
    unittest.main()
