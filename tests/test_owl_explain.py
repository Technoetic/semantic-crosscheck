"""Bounded same-HermiT explanations; no source assertions are exposed in diagnostics."""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pgfixture
from semantic_crosscheck import config, owl, stage_reason
from semantic_crosscheck.result import EXIT_CONFIG, Result, StageError

try:
    from semantic_crosscheck import owl_explain
except ImportError:
    owl_explain = None

JAVA = owl.java_executable()
HAVE_OWL = bool(JAVA and importlib.util.find_spec("owlready2"))
SECRET = "private-source-key-DO-NOT-PRINT"


def cfg_graph(disjoint=False):
    cfg = config.load(pgfixture.DEMO_CONFIG)
    cfg.ontology.defined = []
    cfg.ontology.classes = {"product": "Product", "raw": "RawMaterial"}
    graph = owl.Graph({SECRET: {"raw", "product"} if disjoint else {"raw"}, "leaf": set()},
                      [] if disjoint else [(SECRET, "leaf")])
    return cfg, graph


def options(**changes):
    opts = dict(explain_inconsistency=True, explain_max_axioms=128, explain_max_checks=64,
                explain_timeout_ms=10000, only=["owl"])
    opts.update(changes)
    return SimpleNamespace(**opts)


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(owl_explain, "the explanation implementation is missing")
        self.cfg, self.graph = cfg_graph()

    def explain(self, checker=None, **budgets):
        patch = mock.patch.object(owl_explain, "_check", side_effect=checker or (lambda *_a: False))
        with patch:
            return owl_explain.explain(self.cfg, self.graph, "unused-java", **budgets)

    def test_check_cap_retains_only_an_actually_checked_sufficient_core(self):
        got = self.explain(max_checks=1)
        self.assertEqual((got["status"], got["reason"], got["checks_completed"]),
                         ("not_run", "check_limit", 1))
        self.assertTrue(got["sufficient"])
        self.assertEqual(got["minimality"], "not_proven")
        self.assertEqual(len(got["core"]), got["axiom_groups"])

    def test_axiom_cap_performs_no_reasoner_check_and_no_core_claim(self):
        checker = mock.Mock()
        got = self.explain(checker, max_axioms=1)
        checker.assert_not_called()
        self.assertEqual((got["status"], got["reason"], got["checks_completed"]),
                         ("not_run", "axiom_limit", 0))
        self.assertFalse(got["sufficient"])
        self.assertEqual(got["core"], [])

    def test_dense_graph_is_rejected_before_sorting_or_materializing_groups(self):
        self.graph.edges = [(SECRET, "leaf")] * 301950
        with mock.patch.object(owl, "axiom_groups") as collect:
            got = self.explain(max_axioms=128, timeout_ms=1)
        self.assertEqual(collect.call_count, 0)
        self.assertEqual((got["reason"], got["checks_completed"]), ("axiom_limit", 0))
        self.assertFalse(got["axiom_groups_count_complete"])
        self.assertGreater(got["axiom_groups"], 128)
        self.assertFalse(got["sufficient"])

    def test_large_configuration_counts_are_bounded_before_collection(self):
        for field in ("classes", "no_children", "defined"):
            cfg, graph = cfg_graph()
            if field == "classes":
                cfg.ontology.classes = {f"v{j}": f"Class{j}" for j in range(1024)}
            elif field == "no_children":
                cfg.ontology.no_children = ["raw"] * 1024
            else:
                cfg.ontology.defined = [SimpleNamespace(name=f"Defined{j}", kind="has_child", cls="")
                                        for j in range(1024)]
            with self.subTest(field=field), mock.patch.object(owl, "axiom_groups") as collect:
                got = owl_explain.explain(cfg, graph, "unused", max_axioms=128)
            collect.assert_not_called()
            self.assertEqual(got["reason"], "axiom_limit")
            self.assertFalse(got["axiom_groups_count_complete"])

    def test_additional_node_types_stop_preflight_at_the_limit(self):
        self.cfg, self.graph = cfg_graph(disjoint=True)
        with mock.patch.object(owl, "axiom_groups") as collect:
            got = self.explain(max_axioms=10)
        collect.assert_not_called()
        self.assertEqual(got["axiom_groups"], 11)
        self.assertFalse(got["axiom_groups_count_complete"])

    def test_preflight_deadline_stops_before_collection(self):
        with mock.patch.object(owl_explain.time, "monotonic", side_effect=[0.0, 0.0, 0.002]), \
             mock.patch.object(owl, "axiom_groups") as collect:
            got = self.explain(timeout_ms=1)
        collect.assert_not_called()
        self.assertEqual(got["reason"], "timeout")
        self.assertFalse(got["axiom_groups_count_complete"])
        self.assertFalse(got["sufficient"])

    def test_completed_preflight_reports_exact_count(self):
        got = self.explain(max_checks=1)
        self.assertTrue(got["axiom_groups_count_complete"])
        self.assertEqual(got["axiom_groups"], len(owl.axiom_groups(self.cfg, self.graph)))

    def test_initial_consistency_does_not_claim_a_core(self):
        got = self.explain(lambda *_a: True)
        self.assertEqual(got["reason"], "initial_consistent")
        self.assertFalse(got["sufficient"])
        self.assertEqual(got["core"], [])

    def test_failure_and_timeout_never_echo_exception_messages(self):
        for exc, reason in [(RuntimeError(SECRET + " C:/private/file"), "reasoner_error"),
                            (TimeoutError(SECRET), "timeout")]:
            with self.subTest(reason=reason):
                def fail(*_a):
                    raise exc
                got = self.explain(fail)
                self.assertEqual((got["status"], got["reason"]), ("not_run", reason))
                self.assertFalse(got["sufficient"])
                self.assertNotIn(SECRET, json.dumps(got))
                self.assertNotIn("C:/private", json.dumps(got))

    def test_error_after_verified_core_preserves_partial_sufficiency(self):
        checks = iter([False, RuntimeError(SECRET)])
        def check(*_a):
            answer = next(checks)
            if isinstance(answer, Exception):
                raise answer
            return answer
        got = self.explain(check)
        self.assertTrue(got["sufficient"])
        self.assertEqual((got["reason"], got["minimality"]), ("reasoner_error", "not_proven"))

    def test_retained_final_deletions_must_all_be_checked(self):
        ids = [group.id for group in owl.axiom_groups(self.cfg, self.graph)]
        required = set(ids[-2:])
        seen = []
        def check(_cfg, _graph, _java, active, _deadline):
            seen.append(frozenset(active))
            return not required.issubset(active)
        got = self.explain(check)
        self.assertEqual((got["status"], got["minimality"], got["sufficient"]),
                         ("pass", "subset_minimal", True))
        self.assertEqual({group["id"] for group in got["core"]}, required)
        self.assertEqual(got["minimality_checks_completed"], 2)
        self.assertEqual(seen[-2:], [frozenset(required - {key}) for key in ids[-2:]])
        self.assertNotIn(SECRET, json.dumps(got))
        self.assertTrue(all(set(group) == {"id", "kind", "config_ref"} for group in got["core"]))

    def test_nonminimal_final_control_cannot_pass(self):
        ids = [group.id for group in owl.axiom_groups(self.cfg, self.graph)]
        calls = iter([False] + [True] * len(ids) + [False])
        got = self.explain(lambda *_a: next(calls))
        self.assertEqual((got["status"], got["minimality"], got["reason"]),
                         ("not_run", "not_proven", "verification_disagreement"))

    def test_worker_payload_removes_source_keys_and_class_values_and_is_stable(self):
        self.cfg.ontology.classes = {SECRET: "RawMaterial"}
        self.cfg.ontology.no_children = [SECRET]
        graph = owl.Graph({f"{SECRET}-{j}": {SECRET} for j in range(12)},
                          [(f"{SECRET}-11", f"{SECRET}-0")])
        ids = {group.id for group in owl.axiom_groups(self.cfg, graph)}
        before = owl_explain._payload(self.cfg, graph, "java", ids)
        reordered = owl.Graph(dict(reversed(list(graph.nodes.items()))), list(reversed(graph.edges)))
        self.assertEqual(before, owl_explain._payload(self.cfg, reordered, "java", ids))
        self.assertNotIn(SECRET, json.dumps(before))
        self.assertNotIn(str(self.cfg.path), json.dumps(before))
        # Ordinal keys must sort in original index order beyond 10 individuals in the worker too.
        self.assertEqual([row[0] for row in before["nodes"]], sorted(row[0] for row in before["nodes"]))

    def test_outside_endpoint_ordinals_preserve_literal_axiom_order(self):
        self.cfg.ontology.classes = {"raw": "RawMaterial"}
        graph = owl.Graph({"z": {"raw"}, "y": set()}, [("a", "y"), ("z", "y")])
        ids = {group.id for group in owl.axiom_groups(self.cfg, graph)}
        payload = owl_explain._payload(self.cfg, graph, "java", ids)
        self.assertEqual(payload["nodes"], [["k000000000001", []], ["k000000000002", ["c0"]]])
        self.assertEqual(payload["edges"], [["k000000000000", "k000000000001"],
                                           ["k000000000002", "k000000000001"]])

    def test_mixed_keys_and_duplicate_edges_preserve_group_correspondence(self):
        graph = owl.Graph({2: set(), "z": {"raw"}, 1: set()},
                          [(0, 2), ("a", "z"), ("z", 2), (0, 2), (1, "z")])
        ordered = owl._ordered_graph(graph)
        all_keys = sorted(set(graph.nodes) | {key for pair in graph.edges for key in pair},
                          key=lambda key: (type(key).__name__, owl.canon.key(key)))
        mapped = {key: f"k{j:012d}" for j, key in enumerate(all_keys)}
        ids = {group.id for group in owl.axiom_groups(self.cfg, graph)}
        payload = owl_explain._payload(self.cfg, graph, "java", ids)
        self.assertEqual([row[0] for row in payload["nodes"]], [mapped[key] for key in ordered.nodes])
        self.assertEqual(payload["edges"], [[mapped[a], mapped[b]] for a, b in ordered.edges])
        worker_graph = owl.Graph({key: set(values) for key, values in payload["nodes"]},
                                 [tuple(pair) for pair in payload["edges"]])
        self.assertEqual(owl._ordered_graph(worker_graph).edges, worker_graph.edges)
        self.assertEqual(len(payload["nodes"]), len(graph.nodes))

    def test_payload_binds_group_ids_to_original_encoded_assertions(self):
        graph = owl.Graph({"z": {"raw"}, "y": set()}, [("a", "y"), ("z", "y")])
        ids = {group.id for group in owl.axiom_groups(self.cfg, graph)}
        payload = owl_explain._payload(self.cfg, graph, "java", ids)
        edges = [group for group in payload["groups"] if group["kind"] == "edge_assertion"]
        self.assertEqual([group["assertion"] for group in edges], payload["edges"])
        self.assertEqual([group["id"] for group in edges], [group.id for group in owl.axiom_groups(self.cfg, graph)
                                                           if group.kind == "edge_assertion"])
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_limits_are_positive_bounded_integers(self):
        for name in ("max_axioms", "max_checks", "timeout_ms"):
            for bad in (0, -1, True, 1.5, "1", 10**12):
                with self.subTest(name=name, bad=bad):
                    with self.assertRaises(StageError) as caught:
                        owl_explain.explain(self.cfg, self.graph, "unused", **{name: bad})
                    self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)

    def test_stage_rejects_invalid_scope_and_limits_before_database(self):
        with self.assertRaises(StageError):
            stage_reason._only(options(only=["shacl"]))
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            Result("build").write(out)
            with mock.patch.object(stage_reason.db, "connect") as connect:
                with self.assertRaises(StageError):
                    stage_reason.run(self.cfg, options(explain_max_checks=0), out)
                connect.assert_not_called()
            self.cfg.ontology.hierarchy = ""
            with self.assertRaises(StageError):
                stage_reason.run(self.cfg, options(), out)

    def test_incomplete_attachment_preserves_original_failure(self):
        result = Result("reason")
        result.add("owl:consistency", "fail", "ontology")
        with mock.patch.object(owl_explain, "_check", return_value=False):
            owl_explain.run(self.cfg, self.graph, "java", options(explain_max_checks=1), result)
        self.assertEqual(result.status, "fail")
        self.assertEqual([(check.id, check.status) for check in result.checks],
                         [("owl:consistency", "fail"), ("owl:explanation", "not_run")])
        self.assertTrue(result.checks[-1].detail["sufficient"])
        self.assertTrue(any("not independent certification" in text for text in result.not_proven))


class WorkerContainmentTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(owl_explain)

    def test_worker_environment_excludes_connection_strings(self):
        with mock.patch.dict(os.environ, {"SXC_SYNTHETIC_DSN": SECRET, "UNRELATED_PRIVATE_VALUE": SECRET}):
            env = owl_explain._worker_env("private-worker-directory")
        self.assertNotIn(SECRET, json.dumps(env))
        self.assertNotIn("SXC_SYNTHETIC_DSN", env)
        self.assertEqual(env["TMP"], "private-worker-directory")

    def test_worker_rejects_mismatched_assertion_metadata(self):
        cfg, graph = cfg_graph()
        ids = {group.id for group in owl.axiom_groups(cfg, graph)}
        payload = owl_explain._payload(cfg, graph, "unused-java", ids)
        payload["groups"][-1]["assertion"] = ["invalid-ordinal", "invalid-ordinal"]
        with self.assertRaises(owl.ReasonerError):
            owl_explain._process(json.dumps(payload).encode(), time.monotonic() + 5)

    def test_timed_out_worker_and_descendant_are_terminated(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            script, marker = tmp / "worker.py", tmp / "pid"
            script.write_text("import sys, subprocess, time\n"
                              "from pathlib import Path\n"
                              "sys.stdin.buffer.read()\n"
                              "child=subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])\n"
                              f"Path({str(marker)!r}).write_text(str(child.pid))\n"
                              "time.sleep(60)\n", encoding="utf-8")
            started = time.monotonic()
            with mock.patch.object(owl_explain, "WORKER", script):
                with self.assertRaises(TimeoutError):
                    owl_explain._process(b"{}", time.monotonic() + 2)
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(marker.is_file(), "the descendant must actually have been started")
            pid = int(marker.read_text())
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x1000, False, pid)
                if handle:
                    try:
                        status = wintypes.DWORD()
                        self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(status)))
                        self.assertNotEqual(status.value, 259, "the descendant must not remain active")
                    finally:
                        kernel.CloseHandle(handle)
            else:
                # A reparented zombie may briefly remain visible, but it cannot execute after SIGKILL.
                stat = Path(f"/proc/{pid}/stat")
                if stat.exists():
                    self.assertEqual(stat.read_text().split()[2], "Z")

    @unittest.skipUnless(os.name == "nt", "Windows job containment")
    def test_failed_containment_cannot_start_worker_oracle(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            marker, script = tmp / "started", tmp / "worker.py"
            script.write_text("import sys\nfrom pathlib import Path\n"
                              "if sys.stdin.buffer.read():\n"
                              f"    Path({str(marker)!r}).write_text('started')\n", encoding="utf-8")
            job = mock.Mock()
            job.assign.side_effect = OSError(SECRET)
            with mock.patch.object(owl_explain, "WORKER", script), \
                 mock.patch.object(owl_explain, "_WindowsJob", return_value=job):
                with self.assertRaises(OSError):
                    owl_explain._process(b"{}", time.monotonic() + 2)
            self.assertFalse(marker.exists())
            job.close.assert_called_once()

    def test_stage_calls_diagnostic_only_after_real_failure_and_when_requested(self):
        cfg, graph = cfg_graph()
        for consistent, requested in ((True, True), (False, False), (False, True)):
            with self.subTest(consistent=consistent, requested=requested):
                result = Result("reason")
                reasoned = owl.Reasoned(consistent, 2, 2, 0, 1, 1)
                with mock.patch.object(stage_reason, "_closure_reason", return_value="closure_not_passed"), \
                     mock.patch.object(owl, "read", return_value=(graph, None, {})), \
                     mock.patch.object(owl, "reason", return_value=reasoned), \
                     mock.patch.object(owl_explain, "run") as diagnostic:
                    stage_reason._owl(None, cfg, {}, "java", result,
                                      options(explain_inconsistency=requested))
                self.assertEqual(diagnostic.call_count, int(requested and not consistent))
                self.assertEqual(result.checks[0].status, "pass" if consistent else "fail")

    def test_unavailable_baseline_never_invokes_explanation(self):
        cfg, graph = cfg_graph()
        for error in (owl.TooManyIndividuals(129, 128), owl.ReasonerError("OwlReadyJavaError")):
            with self.subTest(error=type(error).__name__):
                result = Result("reason")
                read = (mock.patch.object(owl, "read", side_effect=error) if isinstance(error, owl.TooManyIndividuals)
                        else mock.patch.object(owl, "read", return_value=(graph, None, {})))
                with mock.patch.object(stage_reason, "_closure_reason", return_value="closure_not_passed"), \
                     read, mock.patch.object(owl, "reason", side_effect=error), \
                     mock.patch.object(owl_explain, "run") as diagnostic:
                    stage_reason._owl(None, cfg, {}, "java", result, options())
                diagnostic.assert_not_called()
                self.assertIn(result.checks[0].status, ("not_run", "error"))


@unittest.skipUnless(HAVE_OWL, "needs owlready2 and Java")
class RealHermitExplainTest(unittest.TestCase):
    def test_outside_endpoint_core_is_inconsistent_on_original_graph(self):
        cfg, _graph = cfg_graph()
        cfg.ontology.classes = {"raw": "RawMaterial"}
        cases = [("outside", owl.Graph({"z": {"raw"}, "y": set()}, [("a", "y"), ("z", "y")])),
                 ("numeric_alias", owl.Graph({2: {"raw"}, 1: set(), "leaf": set()},
                                             [(2.0, "leaf"), (1, "leaf")]))]
        for label, graph in cases:
            with self.subTest(case=label):
                self.assertFalse(owl.reason(cfg, graph, JAVA).consistent)
                got = owl_explain.explain(cfg, graph, JAVA, timeout_ms=60000)
                self.assertEqual((got["status"], got["minimality"]), ("pass", "subset_minimal"), got)
                core = {group["id"] for group in got["core"]}
                for active, expected_consistent in [(core, False)] + [(core - {group}, True) for group in core]:
                    with self.subTest(retained=len(active)):
                        model = owl.build(cfg, graph, active_axioms=active)
                        try:
                            self.assertEqual(owl.hermit(model.world, JAVA)[0], expected_consistent)
                        finally:
                            model.world.close()

    def test_real_no_children_and_disjoint_cores(self):
        self.assertIsNotNone(owl_explain)
        for disjoint, required_kind in ((False, "no_children"), (True, "disjoint_classes")):
            with self.subTest(disjoint=disjoint):
                cfg, graph = cfg_graph(disjoint)
                original = owl.reason(cfg, graph, JAVA)
                self.assertFalse(original.consistent)
                got = owl_explain.explain(cfg, graph, JAVA, timeout_ms=60000)
                self.assertEqual((got["status"], got["minimality"]), ("pass", "subset_minimal"), got)
                core = {group["id"] for group in got["core"]}
                self.assertIn(required_kind, {group["kind"] for group in got["core"]})
                model = owl.build(cfg, graph, active_axioms=core)
                try:
                    self.assertFalse(owl.hermit(model.world, JAVA)[0])
                finally:
                    model.world.close()
                for removed in core:
                    model = owl.build(cfg, graph, active_axioms=core - {removed})
                    try:
                        self.assertTrue(owl.hermit(model.world, JAVA)[0])
                    finally:
                        model.world.close()
                self.assertNotIn(SECRET, json.dumps(got))

    def test_actual_worker_missing_java_and_hard_timeout(self):
        cfg, graph = cfg_graph()
        error = owl_explain.explain(cfg, graph, "C:/missing-private/java.exe", timeout_ms=10000)
        self.assertEqual((error["status"], error["reason"]), ("not_run", "reasoner_error"))
        self.assertNotIn("missing-private", json.dumps(error))
        started = time.monotonic()
        timeout = owl_explain.explain(cfg, graph, JAVA, timeout_ms=1)
        self.assertEqual((timeout["status"], timeout["reason"]), ("not_run", "timeout"))
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
