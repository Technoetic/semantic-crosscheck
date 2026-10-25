"""Generated-shape syntax preflight, without logging invalid shape values."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import pgfixture
from semantic_crosscheck import config, shacl_check, shacl_corpus, shapes, stage_reason

HAVE_SHACL = all(importlib.util.find_spec(x) for x in ('rdflib', 'pyshacl'))
PREFIX = '@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <urn:example:> . '


@unittest.skipUnless(HAVE_SHACL, 'needs pinned pySHACL and rdflib')
class MetaShaclTest(unittest.TestCase):
    def test_all_existing_generated_corpus_shapes_pass(self):
        for case in shacl_corpus.corpus():
            with self.subTest(case=case.id):
                self.assertEqual(shacl_check.validate_generated_shapes(case.shapes), (True, 0))

    def test_missing_or_literal_path_fails_without_emitting_private_marker(self):
        for definition in ('ex:S a sh:NodeShape; sh:property ex:P . ex:P a sh:PropertyShape .',
                           'ex:S a sh:PropertyShape; sh:path "private-marker" .'):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                conforms, count = shacl_check.validate_generated_shapes(PREFIX + definition)
            self.assertFalse(conforms)
            self.assertGreater(count, 0)
            self.assertEqual(buf.getvalue(), '')

    def test_parse_and_engine_errors_have_only_a_fixed_code(self):
        with self.assertRaises(shacl_check.ValidationError) as error:
            shacl_check.validate_generated_shapes('private-marker malformed turtle')
        self.assertNotIn('private-marker', str(error.exception))
        from pyshacl import entrypoints
        with mock.patch.object(entrypoints, 'meta_validate', side_effect=RuntimeError('private-marker')):
            with self.assertRaises(shacl_check.ValidationError) as error:
                shacl_check.validate_generated_shapes(PREFIX + 'ex:S a sh:NodeShape .')
        self.assertNotIn('private-marker', str(error.exception))

    def test_malformed_engine_report_never_passes(self):
        import rdflib
        from pyshacl import entrypoints
        for returned in ((True, 'private-marker', ''), (True, rdflib.Graph(), ''),
                         ('true', rdflib.Graph(), ''), (False, rdflib.Graph(), '')):
            with self.subTest(result_type=type(returned[1]).__name__), \
                    mock.patch.object(entrypoints, 'meta_validate', return_value=returned), \
                    self.assertRaises(shacl_check.ValidationError):
                shacl_check.validate_generated_shapes(PREFIX + 'ex:S a sh:NodeShape .')


@unittest.skipUnless(HAVE_SHACL, 'needs pinned pySHACL and rdflib')
class MetaShaclStageTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load(pgfixture.DEMO_CONFIG)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_stage(self, **patches):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(stage_reason, 'require_stage', return_value={}))
        self.stack.enter_context(mock.patch.object(stage_reason, 'shapes_ttl', return_value='generated'))
        self.stack.enter_context(mock.patch.object(stage_reason, 'components', return_value=None))
        for name, value in patches.items():
            self.stack.enter_context(mock.patch.object(shacl_check, name, value))

    def test_preflight_failure_blocks_source_export_and_is_actionable(self):
        self.run_stage(validate_generated_shapes=lambda ttl: (False, 2))
        with mock.patch.object(stage_reason.db, 'connect') as connect:
            res = stage_reason.run(self.cfg, SimpleNamespace(only=['shacl']), self.out)
        connect.assert_not_called()
        self.assertEqual(res.status, 'fail')
        self.assertEqual(next(c for c in res.checks if c.id == 'shacl:meta:generated').detail['violations'], 2)
        blocked = [c for c in res.checks if c.id.startswith('shacl:') and c.id != 'shacl:meta:generated']
        self.assertTrue(blocked)
        self.assertTrue(all(c.status == 'not_run' for c in blocked))
        self.assertIn('generated SHACL', res.next_safe_action)

    def test_preflight_engine_error_is_sanitized_and_never_passes(self):
        def broken(ttl):
            raise RuntimeError('private-marker')
        self.run_stage(validate_generated_shapes=broken)
        with mock.patch.object(stage_reason.db, 'connect') as connect:
            res = stage_reason.run(self.cfg, SimpleNamespace(only=['shacl']), self.out)
        connect.assert_not_called()
        self.assertEqual(res.status, 'error')
        self.assertNotIn('private-marker', json.dumps(res.to_dict()))

    def test_one_preflight_even_with_multiple_groups_and_owl_can_proceed(self):
        self.run_stage()
        with mock.patch.object(shacl_check, 'validate_generated_shapes', return_value=(False, 1)) as preflight, \
                mock.patch.object(stage_reason.db, 'connect') as connect, \
                mock.patch.object(stage_reason, 'check_owl_sources'), \
                mock.patch.object(stage_reason, '_owl') as owl, mock.patch.object(stage_reason, '_shacl') as shacl:
            stage_reason.run(self.cfg, SimpleNamespace(only=['shacl', 'owl']), self.out)
        preflight.assert_called_once()
        owl.assert_called_once()
        shacl.assert_not_called()
        connect.return_value.close.assert_called_once()

    def test_owl_only_does_not_invoke_preflight(self):
        self.run_stage()
        with mock.patch.object(shacl_check, 'validate_generated_shapes') as preflight, \
                mock.patch.object(stage_reason.db, 'connect'), mock.patch.object(stage_reason, 'check_owl_sources'), \
                mock.patch.object(stage_reason, '_owl'):
            stage_reason.run(self.cfg, SimpleNamespace(only=['owl']), self.out)
        preflight.assert_not_called()


if __name__ == '__main__':
    unittest.main()
