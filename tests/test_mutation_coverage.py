"""Bounded per-mutant execution receipts and replay beyond the first pair chunk."""

import json
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck import config, mutate, shape_q, stage_verify
from test_pair_chunks import CAP, KeyedClient, KeysConn, baseline, keys
from test_mutate import demo_model

try:
    from semantic_crosscheck import mutation_coverage
except ImportError:
    mutation_coverage = None


class FullReplayTest(unittest.TestCase):
    def test_null_replay_requires_consistent_presence_but_all_absent_is_valid(self):
        every = keys(CAP + 1)
        _verify, _check, test = baseline(self, 5000, KeysConn(every), KeyedClient(every, null="same"))
        suffix, null = shape_q.replay_parts(test)[-1]
        self.assertEqual(suffix, ":null")
        self.assertEqual(null(KeyedClient(every, null="same", missing=[None]))[0], "not_run")
        self.assertEqual(null(KeyedClient(every))[0], "pass")

    def test_replay_catches_a_mismatch_in_the_last_chunk_without_rereading_keys(self):
        every = keys(2 * CAP + 1)
        conn = KeysConn(every)
        verify, check, test = baseline(self, 5000, conn, KeyedClient(every))
        self.assertEqual(check.status, "pass")
        reads = len(conn.queries)
        replay = KeyedClient(every, bad=[every[-1]])
        self.assertEqual(verify._test_fn(conn, test)(replay), "fail")
        self.assertEqual(len(conn.queries), reads)

    def test_replay_catches_the_null_group(self):
        every = keys(CAP + 1)
        verify, _check, test = baseline(self, 5000, KeysConn(every), KeyedClient(every, null="same"))
        self.assertEqual(verify._test_fn(KeysConn(every), test)(KeyedClient(every, null="bad")), "fail")

    def test_v4_names_the_later_chunk_and_records_remaining_null_as_unrun(self):
        every = keys(CAP + 1)
        conn = KeysConn(every)
        verify, check, test = baseline(self, 5000, conn, KeyedClient(every))
        cid = check.id
        verify.items, verify.suite_tests = [(cid, cid, verify._test_fn(conn, test))], [test]
        seen = {}

        def run_mutants(cfg, out, conn, model, suite, **kwargs):
            status, item = suite(KeyedClient(every, bad=[every[-1]]))
            seen.update(status=status, item=item, receipt=suite.receipt())
            st, detail = mutate.verdict(status, item)
            return [("v4:measure_agg_to_max:on_hand", st, "measures.on_hand", detail)], []

        with mock.patch.object(stage_verify.mutate, "run_mutants", side_effect=run_mutants):
            verify.v4(conn)
        self.assertEqual((seen["status"], seen["item"]), ("fail", cid + ":chunk[1]"))
        self.assertEqual([r["status"] for r in seen["receipt"]["items"]], ["pass", "fail", "not_run"])
        self.assertEqual(seen["receipt"]["items"][-1]["id"], cid + ":null")
        self.assertNotIn(every[-1], json.dumps(seen["receipt"]))


class SuiteCoverageTest(unittest.TestCase):
    def suite(self, items, **kwargs):
        self.assertIsNotNone(mutation_coverage, "per-mutant coverage implementation is missing")
        return mutation_coverage.Suite(items, **kwargs)

    def test_early_kill_records_only_actual_calls_and_leaves_remaining_unrun(self):
        calls = []
        def fn(name, status):
            def run(_client):
                calls.append(name)
                return status
            return name, run
        suite = self.suite([fn("v3:a:chunk[0]", "pass"), fn("v3:a:chunk[1]", "fail"), fn("v3:a:null", "pass")])
        self.assertEqual(suite(None), ("fail", "v3:a:chunk[1]"))
        self.assertEqual(calls, ["v3:a:chunk[0]", "v3:a:chunk[1]"])
        receipt = suite.receipt()
        self.assertEqual((receipt["executed_items"], receipt["complete"]), (2, False))
        self.assertEqual(receipt["items"][-1]["reason"], "early_stop")

    def test_budget_does_not_misclassify_survival_and_resets_for_each_mutant(self):
        suite = self.suite([("v1:a", lambda _: "pass"), ("v1:b", lambda _: "fail")], max_items=1)
        for _ in range(2):
            self.assertEqual(suite(None), ("budget_exceeded", "v1:b"))
            self.assertEqual(suite.receipt()["executed_items"], 1)
            self.assertEqual(suite.receipt()["items"][1]["reason"], "item_budget")
        self.assertEqual(mutate.verdict("budget_exceeded", "v1:b"),
                         ("not_run", {"reason": "suite_budget", "item": "v1:b"}))

    def test_retry_counts_attempts_and_persistent_error_is_unjudged(self):
        answers = iter(["error", "pass"])
        suite = self.suite([("v1:a", lambda _: next(answers)), ("v1:b", lambda _: "pass")])
        self.assertEqual(suite(None), ("pass", ""))
        receipt = suite.receipt()
        self.assertEqual(receipt["items"][0]["attempts"], 2)
        self.assertTrue(receipt["complete"])
        suite = self.suite([("v1:a", lambda _: "error"), ("v1:b", lambda _: "fail")])
        self.assertEqual(suite(None), ("error", "v1:a"))
        self.assertEqual(suite.receipt()["items"][1]["status"], "not_run")

    def test_receipts_are_detached_and_skip_is_not_execution(self):
        suite = self.suite([("v1:a", lambda _: "pass")])
        suite(None)
        old = suite.receipt()
        skipped = suite.skipped("data_equivalent")
        self.assertEqual(skipped["executed_items"], 0)
        self.assertEqual(skipped["items"][0]["reason"], "data_equivalent")
        suite(None)
        self.assertEqual(old["items"][0]["status"], "pass")
        old["items"][0]["status"] = "error"
        self.assertEqual(suite.receipt()["items"][0]["status"], "pass")

    def test_invalid_limits_rejected(self):
        for bad in (0, -1, True, 1.5, "2"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.suite([], max_items=bad)

    def test_error_details_cannot_introduce_row_values(self):
        secret = "private_source_row"
        suite = self.suite([("v1:a", lambda _: ("error", {"cube": secret, "sqlstate": secret, "exception": secret}))])
        suite(None)
        self.assertNotIn(secret, json.dumps(suite.receipt()))

    def test_equivalent_and_not_applicable_instances_have_no_executed_items(self):
        cfg, model = demo_model()
        cfg.verify.mutation_operators = ["measure_normalization_drop"]
        suite = self.suite([("v1:a", lambda _: self.fail("equivalent mutant must not start Cube"))])
        ledger = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mutate, "witness", return_value=0):
            checks, _notes = mutate.run_mutants(cfg, Path(tmp), None, model, suite, coverage=ledger)
        self.assertEqual(checks, [])
        self.assertEqual(len(ledger), 2)
        self.assertEqual({row["outcome"] for row in ledger}, {"equivalent"})
        self.assertTrue(all(row["coverage"]["executed_items"] == 0 for row in ledger))
        cfg, model = demo_model(closure_exists=False)
        cfg.verify.mutation_operators = ["closure_depth_shift"]
        ledger = []
        with tempfile.TemporaryDirectory() as tmp:
            mutate.run_mutants(cfg, Path(tmp), None, model, suite,
                               closure={"has_component": "pass"}, coverage=ledger)
        self.assertEqual(ledger[0]["outcome"], "not_applicable")
        self.assertEqual(ledger[0]["coverage"]["items"][0]["attempts"], 0)

    def test_artifact_replaces_a_symlink_without_touching_its_target(self):
        self.assertIsNotNone(mutation_coverage)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            target = out / "keep.txt"
            target.write_text("untouched", encoding="utf-8")
            (out / "verify").mkdir()
            artifact = out / "verify" / "mutation-coverage.json"
            try:
                artifact.symlink_to(target)
            except OSError:
                self.skipTest("symlinks unavailable")
            mutation_coverage.write(out, [], max_items=1)
            self.assertEqual(target.read_text(encoding="utf-8"), "untouched")
            self.assertFalse(artifact.is_symlink())
            self.assertEqual(json.loads(artifact.read_text(encoding="utf-8"))["mutants"], [])


class CoverageConfigTest(unittest.TestCase):
    def test_default_and_validated_override(self):
        self.assertEqual(getattr(config.Verify(), "mutant_max_items", None), 10000)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "crosscheck.toml"
            for value, valid in (("1", True), ("10000", True), ("0", False), ("true", False), ("1.5", False)):
                path.write_text('version = 1\n[source]\nschemas = ["s"]\n[verify]\nmutant_max_items = ' + value,
                                encoding="utf-8")
                if valid:
                    self.assertEqual(config.load(path).verify.mutant_max_items, int(value))
                else:
                    with self.assertRaises(Exception):
                        config.load(path)


class StageLedgerTest(unittest.TestCase):
    def verify(self, out, baseline_status):
        cfg, model = demo_model()
        cfg.verify.mutation_operators = ["measure_normalization_drop"]
        v = stage_verify._Verify(cfg, out, model, ["v4"])
        v.items = [("v1:a", "v1:a", lambda _: "pass"), ("v1:b", "v1:b", lambda _: "pass")]
        v.baseline = {"v1:a": baseline_status, "v1:b": "fail"}
        return v

    def test_every_baseline_failed_still_lists_all_selected_mutants(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            v = self.verify(out, "fail")
            with mock.patch.object(mutate, "witness") as witness, mock.patch.object(mutate.cube_run, "serve") as serve:
                v.v4(None)
            witness.assert_not_called()
            serve.assert_not_called()
            ledger = json.loads((out / "verify" / "mutation-coverage.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["mutants"]), 2)
            self.assertTrue(all(row["outcome"] == "not_run" for row in ledger["mutants"]))
            self.assertEqual(len(ledger["excluded_items"]), 2)

    def test_excluded_baseline_survival_tallies_match_final_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = self.verify(Path(tmp), "pass")
            with mock.patch.object(mutate, "witness", return_value=3), \
                 mock.patch.object(mutate.cube_run, "serve", side_effect=lambda *_a, **_kw: contextlib.nullcontext(None)):
                v.v4(None)
            self.assertEqual(len(v.res.checks), 2)
            for check in v.res.checks:
                self.assertEqual(check.status, "not_run")
                self.assertEqual((check.detail["summary"]["survived"], check.detail["summary"]["not_run"]), (0, 2))


if __name__ == "__main__":
    unittest.main()
