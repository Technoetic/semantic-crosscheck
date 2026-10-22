"""Actual physical-fixture/compiler/mutator/Cube replay, never caller SQL."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import cube_run, db, mutate
from test_cube_replay import arguments, replay
from semantic_crosscheck.result import StageError

ENABLED = pgfixture.available() and shutil.which('node') and cube_run.installed(cube_run.cube_home())


@unittest.skipUnless(ENABLED, 'needs dedicated PostgreSQL, node and installed Cube')
class CubeReplayPgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = 'sxc_t_cube_replay_' + str(os.getpid())
        cls.fx = pgfixture.PgFixture(cls.database, owner='sxc_replay_owner', check='sxc_replay_check').setup()
        cls.addClassCleanup(cls.fx.teardown)
        # Test admin connection to this throw-away database only, no role/database writes by replay.
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(os.environ[pgfixture.ADMIN_ENV])
        cls.env = patch.dict(os.environ, {'SXC_CUBE_REPLAY_DSN': urlunparse(parsed._replace(path='/' + cls.database))})
        cls.env.start(); cls.addClassCleanup(cls.env.stop)

    def schemas(self):
        conn = db.connect('SXC_CUBE_REPLAY_DSN')
        try:
            # PostgreSQL retains empty backend temp namespaces after sessions;
            # these are engine-managed, unlike the random private user schemas.
            return conn.run("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_(toast_)?temp_[0-9]+$' ORDER BY nspname")
        finally:
            conn.close()

    def test_actual_cube_detects_each_compiled_named_mutation(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        before = self.schemas()
        for operator in mutate.OPERATORS:
            with self.subTest(operator=operator), tempfile.TemporaryDirectory() as tmp:
                result = replay.run(None, arguments(mutation_operator=operator), tmp)
                self.assertEqual(result.status, 'pass', result.to_dict())
                detail = next(c.detail for c in result.checks if c.id == 'cube_replay:comparison')
                self.assertTrue(detail['original_gold_agree'])
                self.assertFalse(detail['mutant_gold_agree'])
                self.assertTrue(detail['model_mutation_bound'])
                fixture = Path(tmp) / 'cube_replay/fixture.json'
                self.assertTrue(fixture.is_file())
                self.assertEqual(replay.load_replay(fixture.read_text(encoding='utf-8'), operator, 7)['synthetic'], True)
                self.assertNotIn('synthetic_A', json.dumps(result.to_dict()))
                self.assertEqual(self.schemas(), before)

    def test_startup_failure_cleans_owned_schema_and_preserves_existing_schema(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        before = self.schemas()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(replay.cube_run, 'serve', side_effect=StageError('private DSN secret path', 3)):
                result = replay.run(None, arguments(), tmp)
            self.assertNotEqual(result.status, 'pass')
            self.assertFalse((Path(tmp) / 'cube_replay/fixture.json').exists())
            self.assertNotIn('private DSN secret path', json.dumps(result.to_dict()))
        self.assertEqual(self.schemas(), before)

    def test_malformed_mutant_response_is_error_and_cleanup_still_runs(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        before = self.schemas()
        original_load = replay._load
        calls = []
        def malformed_second(client, query):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('malformed mutant data')
            return original_load(client, query)
        with tempfile.TemporaryDirectory() as tmp, patch.object(replay, '_load', side_effect=malformed_second):
            result = replay.run(None, arguments(), tmp)
            self.assertEqual(result.status, 'error')
            self.assertFalse((Path(tmp) / 'cube_replay/fixture.json').exists())
        self.assertEqual(self.schemas(), before)

    def test_existing_random_name_collision_cannot_be_cleaned_up(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        name = 'sxc_replay_' + '0' * 24 + '_out'
        conn = db.connect('SXC_CUBE_REPLAY_DSN')
        try:
            conn.run(f'CREATE SCHEMA "{name}"')
            before = self.schemas()
            with tempfile.TemporaryDirectory() as tmp, patch.object(replay.secrets, 'token_hex', return_value='0' * 24):
                result = replay.run(None, arguments(), tmp)
                self.assertNotEqual(result.status, 'pass')
            self.assertEqual(self.schemas(), before)
        finally:
            conn.run(f'DROP SCHEMA "{name}"')
            conn.close()

    def test_input_change_probe_and_original_gold_disagreement_never_pass(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        before = self.schemas()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(replay, '_physical_digest', side_effect=['before', 'after']):
                result = replay.run(None, arguments(), tmp)
            self.assertEqual(result.status, 'error')
            self.assertFalse((Path(tmp) / 'cube_replay/fixture.json').exists())
        original_spec = replay._spec
        def wrong_gold(operator, source):
            query, projection, sql = original_spec(operator, source)
            return query, projection, sql.replace('sum(qty) AS value', 'sum(qty)+1 AS value')
        with tempfile.TemporaryDirectory() as tmp, patch.object(replay, '_spec', side_effect=wrong_gold):
            result = replay.run(None, arguments(), tmp)
            self.assertEqual(result.status, 'fail', result.to_dict())
            self.assertFalse((Path(tmp) / 'cube_replay/fixture.json').exists())
        self.assertEqual(self.schemas(), before)

    def test_own_fixture_can_replay_in_place_after_owned_schema_was_removed(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        before = self.schemas()
        with tempfile.TemporaryDirectory() as tmp:
            first = replay.run(None, arguments(), tmp)
            self.assertEqual(first.status, 'pass', first.to_dict())
            fixture = Path(tmp) / 'cube_replay/fixture.json'
            second = replay.run(None, arguments(replay=str(fixture)), tmp)
            self.assertEqual(second.status, 'pass', second.to_dict())
            self.assertEqual(first.checks[0].detail['fixture_sha256'], second.checks[0].detail['fixture_sha256'])
        self.assertEqual(self.schemas(), before)

    def test_replay_file_change_during_cube_execution_is_error_and_cleans_up(self):
        self.assertIsNotNone(replay, 'actual Cube replay missing')
        before = self.schemas()
        for in_place in (False, True):
            with self.subTest(in_place=in_place), tempfile.TemporaryDirectory() as tmp:
                fixture = Path(tmp) / ('cube_replay/fixture.json' if in_place else 'input-fixture.json')
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text(json.dumps(replay.fixture_payload('measure_agg_to_max', 7)), encoding='utf-8')
                original_load = replay._load
                def changed_input(client, query):
                    rows = original_load(client, query)
                    fixture.write_text('changed', encoding='utf-8')
                    return rows
                with patch.object(replay, '_load', side_effect=changed_input):
                    result = replay.run(None, arguments(replay=str(fixture)), tmp)
                self.assertEqual(result.status, 'error', result.to_dict())
                self.assertFalse((Path(tmp) / 'cube_replay/fixture.json').exists())
        self.assertEqual(self.schemas(), before)


if __name__ == '__main__':
    unittest.main()
