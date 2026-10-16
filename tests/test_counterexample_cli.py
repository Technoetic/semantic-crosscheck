"""The synthetic SQL tool must not silently load an operational configuration."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pgfixture  # adds the plugin scripts to sys.path
import sxc
from semantic_crosscheck.result import Result, StageError


class CounterexampleCLI(unittest.TestCase):
    def test_parser_exposes_the_bounded_sql_comparison(self):
        args = sxc.parser().parse_args([
            "counterexample", "--reference-sql", "reference.sql", "--candidate-sql", "candidate.sql",
            "--scenario", "bom_diamond", "--seed", "31", "--max-examples", "24",
            "--max-shrink-steps", "99", "--dsn-env", "SXC_SYNTHETIC_DSN",
            "--statement-timeout-ms", "5000",
        ])
        self.assertEqual((args.scenario, args.seed, args.max_examples, args.max_shrink_steps),
                         ("bom_diamond", 31, 24, 99))
        self.assertEqual((args.dsn_env, args.statement_timeout_ms), ("SXC_SYNTHETIC_DSN", 5000))

    def test_replay_has_explicit_sql_inputs(self):
        args = sxc.parser().parse_args(["counterexample", "--reference-sql", "reference.sql",
                                       "--candidate-sql", "candidate.sql", "--replay", "fixture.json"])
        self.assertEqual(args.replay, "fixture.json")

    def test_config_free_even_with_a_default_config_in_the_working_directory(self):
        with patch.object(Path, "is_file", return_value=True):
            self.assertIsNone(sxc.config_path("counterexample", None))

    def test_explicit_operational_configuration_is_rejected(self):
        with self.assertRaises(StageError) as caught:
            sxc.config_path("counterexample", "crosscheck.toml")
        self.assertEqual(caught.exception.exit_code, 2)

    def test_dispatch_does_not_load_config_or_fingerprint_operational_data(self):
        seen = []

        def run(cfg, args, out):
            seen.append((cfg, args.scenario))
            res = Result("counterexample")
            res.add("search", "not_run", reason="bounded_search_only")
            return res

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sxc.config, "load", side_effect=AssertionError("must not load")), \
                 patch.object(sxc.importlib, "import_module", return_value=SimpleNamespace(run=run)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = sxc.main(["counterexample", "--reference-sql", "reference.sql",
                               "--candidate-sql", "candidate.sql", "--out", tmp])
            self.assertEqual(rc, 1)
            self.assertEqual(seen, [(None, "nulls")])
            self.assertTrue((Path(tmp) / "counterexample" / "result.json").is_file())


if __name__ == "__main__":
    unittest.main()
