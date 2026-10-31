"""A Cube answer outside Cube's load envelope, or a Cube row without a requested member, is an error.

Why: CubeClient.load read every answer without an "error" member as rows through r.get("data", []), so {},
{"results": []}, {"data": {}} and {"data": ""} became zero rows, and compare.cube_rows read a missing member as NULL
(0 under zero_if_null). V1 and V2 then passed vacuously against empty gold rows, a V3 pair test of at most
PAIR_KEY_CAP keys passed over no keys whatever the source data held, a V4 mutant counted as killed against non-empty
gold rows, and the other shapes ({"data": null}, a body that is not an object, a row that is not an object) stopped
the whole verify stage with an unexpected exception. A body that is not JSON was an error named by its decode
exception, whose next action pointed at the gold SQL. Every malformed case here fails without the fix; the valid
answers, the existing errors and the /meta decode error are pinned. No database, Cube or network: the real client
runs with its HTTP call replaced by fixed JSON answers, or with its opener replaced by fixed HTTP 200 bodies.
"""

import contextlib
import io
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import pgfixture  # noqa: F401  (puts the scripts directory on sys.path)
from semantic_crosscheck import compare, cube_client, mutate, shape_q, stage_verify
from semantic_crosscheck.cube_client import CubeClient, CubeError
from test_compare_unit import FakeConn, demo_cfg, demo_model, gold_of

MALFORMED = [{}, {"data": None}, {"results": []}, {"data": {}}, {"data": ""}, [], {"data": "rows"}, None, "text",
             {"data": [1]}, {"data": [{"m.value": "1"}, "row"]}]
INVALID = {"error": "CubeResponseError", "cube": "invalid_response"}
# HTTP 200 bodies that are not JSON: a page, nothing, a byte order mark alone, bytes that do not decode
NOT_JSON = [b"<html>ok</html>", b"", b"\xff\xfe", b'{"data": [\xff]}']


class Answers(CubeClient):
    """The real CubeClient.load; _req returns the given JSON answers in turn, the last one again and again."""

    def __init__(self, *answers, timeout_s: float = 5):
        super().__init__("http://127.0.0.1:9", "s" * 64, timeout_s=timeout_s)
        self.answers, self.requests = list(answers), []

    def _req(self, path, payload=None):
        self.requests.append((path, payload))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Returns:
    """A client whose load returns `data` as it is (FakeClient turns a falsy value into [])."""

    def __init__(self, data):
        self.data = data

    def load(self, query):
        return self.data


class Body:
    """A stand-in for CubeClient._opener: every request gets an HTTP 200 answer with `body`, so the real _req, its
    json.loads included, runs without a network. `paths` records the paths asked for."""

    def __init__(self, body: bytes):
        self.body, self.paths = body, []

    def open(self, req, timeout=None):
        self.paths.append(req.selector)
        return io.BytesIO(self.body)


def served(body: bytes) -> CubeClient:
    client = CubeClient("http://127.0.0.1:9", "s" * 64)
    client._opener = Body(body)
    return client


def category(e: BaseException):
    return compare.error_detail(e).get("cube")


class LoadEnvelopeTest(unittest.TestCase):
    def test_answers_outside_the_load_envelope_are_invalid_response_errors(self):
        for answer in MALFORMED:
            with self.subTest(answer=answer):
                with self.assertRaises(CubeError) as caught:
                    Answers(answer).load({"measures": ["m.value"]})
                self.assertEqual(compare.error_detail(caught.exception), INVALID)

    def test_a_load_body_that_is_not_json_is_an_invalid_response_error(self):
        for body in NOT_JSON:
            with self.subTest(body=body):
                client = served(body)
                try:
                    with self.assertRaises(CubeError) as caught:
                        client.load({"measures": ["m.value"]})
                except ValueError as e:   # without the fix: JSONDecodeError or UnicodeDecodeError, no cube category
                    self.fail(f"escaped as {type(e).__name__}")
                self.assertEqual(compare.error_detail(caught.exception), INVALID)
                self.assertEqual(client._opener.paths, ["/cubejs-api/v1/load"])
                # /meta keeps the decode error, which the cube stage reports as the reason of cube:meta
                with self.assertRaises((json.JSONDecodeError, UnicodeDecodeError)):
                    client.meta()

    def test_valid_answers_and_the_existing_errors_are_unchanged(self):
        rows = [{"m.key": "a", "m.value": None}]
        self.assertEqual(Answers({"data": []}).load({"measures": ["m.value"]}), [])
        client = Answers({"data": rows, "query": {}, "lastRefreshTime": "x"})
        self.assertEqual(client.load({"measures": ["m.value"], "renewQuery": True}), rows)
        self.assertEqual(client.requests[0][1]["query"], {"measures": ["m.value"], "limit": cube_client.DEFAULT_LIMIT})
        for answer, cube in [({"error": "Error: x"}, "query_error"), ({"data": rows * 2}, "row_limit")]:
            with self.subTest(answer=answer):
                with self.assertRaises(CubeError) as caught:
                    Answers(answer).load({"measures": ["m.value"], "limit": 2})
                self.assertEqual(category(caught.exception), cube)

    def test_continue_wait_still_polls_and_times_out_and_the_answer_after_it_is_checked(self):
        # the client's own clock: the real time.time for the deadline and a mock sleep, so the process-wide
        # time.sleep stays untouched
        clock = SimpleNamespace(time=time.time, sleep=mock.Mock())
        with mock.patch.object(cube_client, "time", clock):
            rows = [{"m.value": "1"}]
            self.assertEqual(Answers({"error": "Continue wait"}, {"data": rows}).load({"measures": ["m.value"]}), rows)
            self.assertEqual(clock.sleep.call_count, 1)
            with self.assertRaises(CubeError) as caught:
                Answers({"error": "Continue wait"}, {}).load({"measures": ["m.value"]})
            self.assertEqual(category(caught.exception), "invalid_response")
            with self.assertRaises(CubeError) as caught:
                Answers({"error": "Continue wait"}, timeout_s=-1).load({"measures": ["m.value"]})
            self.assertEqual(category(caught.exception), "timeout")


class CubeRowsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = demo_cfg()
        self.gold = gold_of(self.cfg, "q03_stock")      # zero_if_null = ["on_hand", "consigned"]
        self.params = {"part_class": "assembly"}
        self.row = {"part.part_id": "p1", "part.part_class": None, "m_on_hand.value": None, "m_consigned.value": "0"}

    def v1(self, cube_rows: list) -> dict:
        conn = FakeConn(cols=["part_id", "part_class", "on_hand", "consigned"], data=[("p1", None, 0, 0)])
        return compare.run_binding(conn, Answers({"data": cube_rows}), self.cfg, self.gold, self.params)

    def test_a_row_without_a_mapped_member_is_an_error_never_null_or_zero(self):
        r = self.v1([self.row])
        self.assertTrue(r["pass"], "a NULL Cube returns keeps zero_if_null")
        self.assertEqual(r["declared_null_as_zero"], 1)
        for member in self.gold.map.values():
            with self.subTest(member=member):
                r = self.v1([{k: v for k, v in self.row.items() if k != member}])
                self.assertEqual((compare.status(r), r.get("side"), r.get("error"), r.get("cube")),
                                 ("error", "cube", "MissingMember", "invalid_response"))

    def test_rows_that_are_not_a_list_of_objects_are_errors(self):
        for data in (["row"], [None], "rows", {"data": []}, None):
            with self.subTest(data=data):
                try:
                    with self.assertRaises(CubeError) as caught:
                        compare.cube_rows(Returns(data), self.gold, self.params)
                except (AttributeError, TypeError) as e:   # without the fix these escaped as a stage crash
                    self.fail(f"escaped as {type(e).__name__}")
                self.assertEqual(compare.error_detail(caught.exception), {"error": "MissingMember", "cube": "invalid_response"})


class VerdictTest(unittest.TestCase):
    """What V1, V4 and a V3 pair test make of a malformed answer."""

    maxDiff = None

    def setUp(self):
        self.cfg = demo_cfg()
        self.gold = gold_of(self.cfg, "q04_depth_histogram")

    def binding(self, conn):
        return lambda client: compare.status(compare.run_binding(conn, client, self.cfg, self.gold, {}))

    def test_v1_is_an_error_not_a_vacuous_pass_or_a_failure(self):
        for answer in MALFORMED:
            for gold_rows in ([], [(1, 3)]):
                with self.subTest(answer=answer, gold_rows=len(gold_rows)):
                    conn = FakeConn(cols=["max_depth", "pairs"], data=gold_rows)
                    try:
                        r = compare.run_binding(conn, Answers(answer), self.cfg, self.gold, {})
                    except Exception as e:  # noqa: BLE001 - without the fix some answers escape as a stage crash
                        r = {"pass": False, "error": type(e).__name__, "escaped": True}
                    self.assertEqual((compare.status(r), r.get("side"), r.get("cube")), ("error", "cube", "invalid_response"))
        conn = FakeConn(cols=["max_depth", "pairs"], data=[])
        r = compare.run_binding(conn, Answers({"data": []}), self.cfg, self.gold, {})
        self.assertEqual((compare.status(r), r["empty"]), ("pass", True), "a real empty answer stays a flagged pass")

    def test_v4_retries_then_leaves_the_mutant_unjudged_never_killed(self):
        cfg, model = self.cfg, demo_model(self.cfg)
        cfg.verify.mutation_operators = ["closure_depth_shift"]
        conn = FakeConn(cols=["max_depth", "pairs"], data=[(1, 3)])
        calls = []

        def item(client):
            calls.append(1)
            return self.binding(conn)(client)
        suite = stage_verify.mutation_suite([("v1:q04_depth_histogram", item)])
        for answer in MALFORMED + [{"data": [{"has_component_closure.max_depth": 1}]}]:
            calls.clear()

            @contextlib.contextmanager
            def serve(cfg_, run_dir, log_path=None, answer=answer):
                yield Answers(answer)
            with self.subTest(answer=answer), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
                    mock.patch.object(mutate, "witness", return_value=3), \
                    mock.patch.object(mutate.cube_run, "serve", serve):
                checks, _not_proven = mutate.run_mutants(cfg, tmp, None, model, suite, closure={"has_component": "pass"})
                (cid, status, _ref, detail), = checks
                self.assertEqual((cid, status, detail.get("reason"), detail.get("item")),
                                 ("v4:closure_depth_shift:has_component", "error", "suite_error", "v1:q04_depth_histogram"))
                self.assertNotIn("killed_by", detail)
                self.assertEqual(len(calls), 2, "an item that errors is retried once")
        calls.clear()
        self.assertEqual(suite(Answers({"data": []})), ("fail", "v1:q04_depth_histogram"),
                         "a real empty answer against gold rows still kills")

    def test_v3_pair_test_is_an_error_not_a_pass_over_no_keys(self):
        model = demo_model(self.cfg)
        for answer in MALFORMED:
            test = next(t for t in shape_q.tests(self.cfg, model) if t["id"] == "pair:m_firm_open.value+m_on_hand.value")
            with self.subTest(answer=answer):
                try:
                    st, detail = shape_q.run_test(FakeConn(scalar=5), Answers(answer), self.cfg, model, test,
                                                  budget=self.cfg.verify.pair_key_budget)
                except Exception as e:  # noqa: BLE001 - without the fix some answers escape as a stage crash
                    st, detail = "escaped", {"error": type(e).__name__}
                self.assertEqual((st, detail), ("error", INVALID))

    def test_a_load_body_that_is_not_json_is_a_v1_and_v3_error_with_the_adapter_action(self):
        model = demo_model(self.cfg)
        for body in NOT_JSON:
            with self.subTest(body=body):
                conn = FakeConn(cols=["max_depth", "pairs"], data=[(1, 3)])
                r = compare.run_binding(conn, served(body), self.cfg, self.gold, {})
                res = stage_verify.Result("verify")
                res.add("v1:q04_depth_histogram", compare.status(r), "gold[3] q04_depth_histogram", **stage_verify._detail(r))
                action = stage_verify._next_action(res)
                test = next(t for t in shape_q.tests(self.cfg, model) if t["id"] == "pair:m_firm_open.value+m_on_hand.value")
                v3 = shape_q.run_test(FakeConn(scalar=5), served(body), self.cfg, model, test,
                                      budget=self.cfg.verify.pair_key_budget)
                self.assertEqual({"v1": (compare.status(r), r.get("side"), r.get("error"), r.get("cube")),
                                  "adapter_action": "doctor --require cube" in action and "gold SQL" not in action,
                                  "v3": v3},
                                 {"v1": ("error", "cube", "CubeResponseError", "invalid_response"),
                                  "adapter_action": True, "v3": ("error", INVALID)})

    def test_next_action_names_the_adapter_not_the_gold_sql(self):
        res = stage_verify.Result("verify")
        res.add("v1:q04_depth_histogram", "error", "gold[3] q04_depth_histogram", side="cube", **INVALID)
        action = stage_verify._next_action(res)
        self.assertIn("doctor --require cube", action)
        self.assertNotIn("server.log", action)
        self.assertTrue(action.endswith("rerun verify"), action)


if __name__ == "__main__":
    unittest.main()
