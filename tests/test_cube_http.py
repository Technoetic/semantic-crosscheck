"""Real loopback HTTP regressions for source-byte and elapsed acceptance bounds."""
import contextlib
import http.server
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pgfixture  # noqa: F401
from semantic_crosscheck import compare, cube_client, cube_replay
from semantic_crosscheck.cube_client import CubeClient, CubeError, CubeResponseError
from test_compare_unit import FakeConn, demo_cfg, gold_of
from test_cube_replay import arguments


GOOD = b'{"data": []}'
WAIT = b'{"error": "Continue wait"}'
CAP = 64


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        self.connection.settimeout(2)
        if self.headers.get('Content-Length'):
            self.rfile.read(int(self.headers['Content-Length']))
        scenario = self.server.scenarios[self.path.split('/')[1]]
        scenario['requests'] += 1
        body = scenario.get('body', GOOD)
        if scenario.get('poll') and scenario['requests'] == 1:
            body = WAIT
        try:
            if scenario.get('header_idle'):
                time.sleep(scenario['header_idle'])
            self.send_response(scenario.get('status', 200))
            if 'location' in scenario:
                self.send_header('Location', scenario['location'])
            framing = scenario.get('framing', 'length')
            if framing in ('chunked', 'bad-chunk'):
                self.send_header('Transfer-Encoding', 'chunked')
                # A conflicting length must not bypass the actual body cap.
                self.send_header('Content-Length', '1')
            elif framing != 'absent':
                self.send_header('Content-Length', str(scenario.get('length', len(body))))
            self.send_header('Connection', 'close')
            self.end_headers()
            if scenario.get('idle'):
                time.sleep(scenario['idle'])
            if scenario.get('drip'):
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(scenario['drip'])
            elif framing == 'bad-chunk':
                self.wfile.write(b'not-a-chunk-size\r\nprivate-body\r\n')
            elif framing == 'chunked':
                self.wfile.write(f'{len(body):x}\r\n'.encode() + body + b'\r\n0\r\n\r\n')
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            self.close_connection = True


class CubeHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = False
        cls.server.block_on_close = True
        cls.server.scenarios = {}
        started = threading.Event()

        def serve():
            started.set()
            cls.server.serve_forever(poll_interval=0.01)

        cls.worker = threading.Thread(target=serve)
        cls.worker.start()
        if not started.wait(2):
            raise RuntimeError('local HTTP server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.worker.join(2)
        cls.server.server_close()  # joins finite request handlers
        if cls.worker.is_alive():
            raise RuntimeError('local HTTP server did not stop')

    def client(self, *, timeout_s=3, cap=CAP, **scenario):
        key = str(len(self.server.scenarios))
        scenario['requests'] = 0
        self.server.scenarios[key] = scenario
        kwargs = {'timeout_s': timeout_s}
        if cap is not None:
            kwargs['max_response_bytes'] = cap
        return CubeClient(f'http://127.0.0.1:{self.server.server_port}/{key}',
                          'synthetic-test-secret', **kwargs)

    def invoke(self, client, replay=False):
        return cube_replay._load(client, {}) if replay else client.load({})

    def outcome(self, operation):
        try:
            return ('ok', operation())
        except Exception as error:
            return ('error', compare.error_detail(error))

    def test_redirects_are_bounded_errors_and_never_contact_the_target(self):
        for replay in (False, True):
            for status in (301, 302, 303, 307, 308):
                for size in (CAP, CAP + 1, CAP * 64):
                    with self.subTest(replay=replay, status=status, size=size):
                        target = self.client()
                        client = self.client(status=status, location=target.base,
                                             body=b'private-body' + b'x' * (size - 12))
                        result = self.outcome(lambda: self.invoke(client, replay))
                        detail = ({'error': 'CubeError', 'http_status': status} if size == CAP else
                                  {'error': 'CubeResponseError', 'cube': 'invalid_response'})
                        self.assertEqual(result, ('error', detail))
                        self.assertEqual(self.server.scenarios[target.base.rsplit('/', 1)[1]]['requests'], 0)
                        self.assertNotIn('private-body', repr(result))

    def test_redirect_transport_never_drains_body_with_unbounded_read(self):
        # Instrument public methods around real HTTP, including urllib's handler
        # callbacks; checking only the eventual error misses intermediate drains.
        original_read = http.client.HTTPResponse.read
        original_read1 = http.client.HTTPResponse.read1
        for replay in (False, True):
            for framing in ('length', 'absent', 'chunked'):
                with self.subTest(replay=replay, framing=framing):
                    reads, responses = [], []

                    def read(response, amount=None):
                        data = original_read(response, amount)
                        reads.append((amount, len(data)))
                        responses.append(response)
                        return data

                    def read1(response, amount=-1):
                        data = original_read1(response, amount)
                        reads.append((amount, len(data)))
                        responses.append(response)
                        return data

                    target = self.client()
                    client = self.client(status=302, location=target.base,
                                         body=b'x' * (CAP * 64), framing=framing)
                    with mock.patch.object(http.client.HTTPResponse, 'read', read), \
                            mock.patch.object(http.client.HTTPResponse, 'read1', read1):
                        self.outcome(lambda: self.invoke(client, replay))
                    self.assertTrue(reads)
                    self.assertTrue(all(amount is not None and 0 < amount <= CAP + 1
                                        for amount, _ in reads), reads)
                    self.assertEqual(sum(size for _, size in reads), CAP + 1)
                    self.assertTrue(all(response.isclosed() for response in responses))
                    self.assertEqual(self.server.scenarios[target.base.rsplit('/', 1)[1]]['requests'], 0)

    def test_meta_and_ready_do_not_follow_redirects(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                target = self.client()
                client = self.client(status=status, location=target.base, body=b'private-body')
                self.assertEqual(self.outcome(client.meta),
                                 ('error', {'error': 'CubeError', 'http_status': status}))
                self.assertIs(client.ready(), False)
                self.assertEqual(self.server.scenarios[target.base.rsplit('/', 1)[1]]['requests'], 0)

    def test_exact_cap_and_one_extra_byte_for_all_framings_and_callers(self):
        # Missing the bounded reader allows the padded cap+1 envelope to succeed.
        for replay in (False, True):
            for framing in ('length', 'absent', 'chunked'):
                for size in (CAP, CAP + 1):
                    with self.subTest(replay=replay, framing=framing, size=size):
                        result = self.outcome(lambda: self.invoke(self.client(
                            body=GOOD + b' ' * (size - len(GOOD)), framing=framing), replay))
                        expected = ('ok', []) if size == CAP else (
                            'error', {'error': 'CubeResponseError', 'cube': 'invalid_response'})
                        self.assertEqual(result, expected)

    def test_default_cap_rejects_large_response_without_constructor_override(self):
        body = GOOD + b' ' * (16 * 1024 * 1024 + 1 - len(GOOD))
        result = self.outcome(lambda: self.client(cap=None, body=body).load({}))
        self.assertEqual(result, ('error', {'error': 'CubeResponseError', 'cube': 'invalid_response'}))

    def test_lying_lengths_cannot_make_oversize_or_truncated_json_valid(self):
        for length in (8, CAP * 10):
            for replay in (False, True):
                with self.subTest(length=length, replay=replay):
                    result = self.outcome(lambda: self.invoke(self.client(
                        body=GOOD + b' ' * CAP, length=length), replay))
                    self.assertEqual(result, ('error', {'error': 'CubeResponseError', 'cube': 'invalid_response'}))

    def test_truncated_declared_body_is_rejected_even_when_prefix_is_valid_json(self):
        for replay in (False, True):
            with self.subTest(replay=replay):
                result = self.outcome(lambda: self.invoke(self.client(body=GOOD, length=len(GOOD) + 1), replay))
                self.assertEqual(result, ('error', {'error': 'CubeResponseError', 'cube': 'invalid_response'}))

    def test_bytes_past_declared_body_are_not_part_of_the_http_entity(self):
        # urllib stops at Content-Length; extra wire bytes cannot be counted as JSON.
        for replay in (False, True):
            with self.subTest(replay=replay):
                self.assertEqual(self.invoke(self.client(body=GOOD + b'private-tail' * CAP,
                                                        length=len(GOOD)), replay), [])

    def test_error_bodies_share_the_cap_and_safe_status_reporting(self):
        for replay in (False, True):
            for framing in ('length', 'absent', 'chunked'):
                for size in (CAP, CAP + 1):
                    with self.subTest(replay=replay, framing=framing, size=size):
                        result = self.outcome(lambda: self.invoke(self.client(
                            status=503, body=b'private-body' + b'x' * (size - 12), framing=framing), replay))
                        detail = ({'error': 'CubeError', 'http_status': 503} if size == CAP else
                                  {'error': 'CubeResponseError', 'cube': 'invalid_response'})
                        self.assertEqual(result, ('error', detail))
                        self.assertNotIn('private-body', repr(result))

    def test_slow_success_and_error_bodies_exhaust_one_elapsed_budget(self):
        for replay in (False, True):
            for status in (200, 503):
                with self.subTest(replay=replay, status=status):
                    result = self.outcome(lambda: self.invoke(self.client(
                        timeout_s=0.3, drip=0.08, status=status), replay))
                    self.assertEqual(result, ('error', {'error': 'CubeError', 'cube': 'timeout'}))

    def test_idle_socket_timeouts_are_normalized(self):
        for replay in (False, True):
            for delay in ('idle', 'header_idle'):
                with self.subTest(replay=replay, delay=delay):
                    result = self.outcome(lambda: self.invoke(self.client(timeout_s=0.1, **{delay: 0.4}), replay))
                    self.assertEqual(result, ('error', {'error': 'CubeError', 'cube': 'timeout'}))

    def test_invalid_chunk_framing_is_a_safe_response_error(self):
        for status in (200, 503):
            with self.subTest(status=status):
                result = self.outcome(lambda: self.client(framing='bad-chunk', status=status).load({}))
                self.assertEqual(result, ('error', {'error': 'CubeResponseError', 'cube': 'invalid_response'}))
                self.assertNotIn('private-body', repr(result))

    def test_polls_and_final_body_share_the_initial_deadline(self):
        # Each response fits a fresh 0.55s budget; their combined duration does not.
        for replay in (False, True):
            with self.subTest(replay=replay):
                result = self.outcome(lambda: self.invoke(self.client(
                    timeout_s=0.55, poll=True, idle=0.25), replay))
                self.assertEqual(result, ('error', {'error': 'CubeError', 'cube': 'timeout'}))

    def test_direct_and_meta_requests_establish_their_own_budget(self):
        for method in ('meta', '_req'):
            with self.subTest(method=method):
                client = self.client(timeout_s=0.3, drip=0.08)
                result = self.outcome(lambda: client.meta() if method == 'meta' else client._req('/custom'))
                self.assertEqual(result, ('error', {'error': 'CubeError', 'cube': 'timeout'}))

    def test_client_recovers_after_rejection_and_readiness_stays_boolean(self):
        client = self.client(body=GOOD + b' ' * CAP)
        with self.assertRaises(CubeResponseError):
            client.load({})
        scenario = self.server.scenarios[client.base.rsplit('/', 1)[1]]
        scenario['body'] = GOOD
        self.assertEqual(client.load({}), [])
        self.assertIs(client.ready(), True)
        scenario['status'] = 503
        self.assertIs(client.ready(), False)

    def test_binding_report_never_promotes_size_or_timeout_failure_to_equality(self):
        target = self.client()
        for scenario, category in (({'body': b'{"data": [], "private": "private-body"}' + b' ' * CAP}, 'invalid_response'),
                                   ({'timeout_s': 0.1, 'idle': 0.4}, 'timeout'),
                                   ({'status': 302, 'location': target.base, 'body': b'x' * (CAP + 1)}, 'invalid_response')):
            with self.subTest(category=category):
                cfg = demo_cfg()
                result = compare.run_binding(FakeConn(cols=['max_depth', 'pairs'], data=[]),
                    self.client(**scenario), cfg, gold_of(cfg, 'q04_depth_histogram'), {})
                self.assertEqual((compare.status(result), result.get('side'), result.get('cube')),
                                 ('error', 'cube', category))
                self.assertNotIn('private-body', repr(result))

    def test_replay_report_cannot_call_transport_errors_survivors_or_witnesses(self):
        # Keep actual HTTP, _load and replay.run verdict/artifact handling. Replace
        # the unavailable database and Cube process setup with synthetic fixtures.
        target = self.client()
        for scenario in ({'body': GOOD + b' ' * CAP}, {'timeout_s': 0.1, 'idle': 0.4},
                         {'status': 302, 'location': target.base, 'body': b'private-body'},
                         {'status': 302, 'location': target.base, 'body': b'x' * (CAP + 1)}):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.ExitStack() as stack:
                client = self.client(**scenario)
                client.runtime = {}
                conn = SimpleNamespace(run=lambda *a, **k: [], close=lambda: None)
                model = {'original': 'synthetic'}
                mutant = SimpleNamespace(operator='measure_agg_to_max', model={'mutant': 'synthetic'})

                @contextlib.contextmanager
                def serve(*args, **kwargs):
                    yield client

                replacements = [
                    (cube_replay.db, 'dsn_from_env', lambda *a: 'synthetic-test-connection'),
                    (cube_replay.db, 'connect', lambda *a: conn),
                    (cube_replay.db, 'write_tx', lambda *a: contextlib.nullcontext()),
                    (cube_replay.db, 'scalar', lambda *a, **k: 1),
                    (cube_replay.cube_run, 'installed', lambda *a: True),
                    (cube_replay.procutil, 'which', lambda *a: 'synthetic-runtime'),
                    (cube_replay, '_materialize', lambda *a: None),
                    (cube_replay, '_physical_digest', lambda *a: 'synthetic-digest'),
                    (cube_replay, '_cleanup', lambda *a: None),
                    (cube_replay.cube_gen, 'generate', lambda *a: (model, [])),
                    (cube_replay.cube_gen, 'write_model', lambda *a: None),
                    (cube_replay.mutate, 'operators', lambda *a: [mutant]),
                    (cube_replay.cube_run, 'serve', serve),
                ]
                for target, name, value in replacements:
                    stack.enter_context(mock.patch.object(target, name, value))
                result = cube_replay.run(None, arguments(), tmp)
                self.assertEqual(result.status, 'error')
                runtime = next(c for c in result.checks if c.id == 'cube_replay:runtime')
                self.assertEqual(runtime.detail, {'reason': 'runtime_execution_failed', 'synthetic_only': True})
                self.assertFalse(any(c.id == 'cube_replay:comparison' for c in result.checks))
                self.assertFalse((Path(tmp) / 'cube_replay/fixture.json').exists())
                report = json.dumps(result.to_dict())
                self.assertNotIn('synthetic-test-secret', report)
                self.assertNotIn('synthetic-test-connection', report)


class CubeBudgetTest(unittest.TestCase):
    def test_limit_stops_body_consumption_at_cap_plus_one_and_closes_both_paths(self):
        class Body(io.BytesIO):
            consumed = 0

            def read1(self, amount):
                data = super().read1(amount)
                self.consumed += len(data)
                return data

            def read(self, *args):
                raise AssertionError('body must be read within the source-byte allowance')

        for status in (200, 503):
            with self.subTest(status=status):
                client = CubeClient('http://127.0.0.1:9', 'synthetic', max_response_bytes=CAP)
                body = Body(b'x' * (CAP * 10))
                opener = mock.Mock()
                if status == 200:
                    opener.open.return_value = body
                else:
                    opener.open.side_effect = urllib.error.HTTPError(client.base, status, 'test', {}, body)
                client._opener = opener
                with self.assertRaises(CubeResponseError):
                    client.load({})
                self.assertEqual(body.consumed, CAP + 1)
                self.assertTrue(body.closed)

    def test_invalid_budgets_fail_before_transport(self):
        for value in (0, -1, float('nan'), float('inf'), float('-inf'), True, False,
                      '1', None, 1j, 10 ** 1000):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                CubeClient('http://127.0.0.1:9', 'synthetic', timeout_s=value)
        for value in (0, -1, True, False, 1.5, '64', None):
            with self.subTest(cap=value), self.assertRaises(ValueError):
                CubeClient('http://127.0.0.1:9', 'synthetic', max_response_bytes=value)

    def test_finite_positive_numeric_budgets_are_usable(self):
        for value in (1, 0.5, Decimal('0.5'), Fraction(1, 2)):
            with self.subTest(value=value):
                client = CubeClient('http://127.0.0.1:9', 'synthetic', timeout_s=value)
                with mock.patch.object(client, '_req', return_value={'data': []}):
                    self.assertEqual(client.load({}), [])

    def test_late_custom_request_success_is_rejected_by_both_callers(self):
        for replay in (False, True):
            with self.subTest(replay=replay):
                clock = [100.0]
                client = CubeClient('http://127.0.0.1:9', 'synthetic', timeout_s=1)

                def late(path, payload, *, deadline=None):
                    clock[0] += 2
                    return {'data': []}

                with mock.patch.object(client, '_req', side_effect=late), \
                        mock.patch.object(time, 'monotonic', side_effect=lambda: clock[0]):
                    with self.assertRaises(CubeError) as caught:
                        cube_replay._load(client, {}) if replay else client.load({})
                self.assertEqual(compare.error_detail(caught.exception)['cube'], 'timeout')

    def test_poll_sleep_is_capped_and_does_not_start_a_late_request(self):
        for replay in (False, True):
            with self.subTest(replay=replay):
                clock = [100.0]
                calls = []
                client = CubeClient('http://127.0.0.1:9', 'synthetic', timeout_s=0.05)

                def waiting(path, payload, *, deadline=None):
                    calls.append(deadline)
                    return {'error': 'Continue wait'}

                def sleep(seconds):
                    self.assertLessEqual(seconds, 0.050001)
                    clock[0] += seconds

                with mock.patch.object(client, '_req', side_effect=waiting), \
                        mock.patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
                        mock.patch.object(time, 'sleep', side_effect=sleep):
                    with self.assertRaises(CubeError):
                        cube_replay._load(client, {}) if replay else client.load({})
                self.assertEqual(len(calls), 1)
                self.assertAlmostEqual(calls[0], 100.05)

    def test_decode_time_counts_against_direct_request_budget(self):
        from test_cube_envelope import served
        client = served(GOOD)
        client.timeout_s = 1
        clock = [100.0]
        decode = cube_client.decode_json

        def late_decode(raw):
            clock[0] += 2
            return decode(raw)

        with mock.patch.object(cube_client, 'decode_json', side_effect=late_decode), \
                mock.patch.object(time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(CubeError) as caught:
                client.meta()
        self.assertEqual(compare.error_detail(caught.exception)['cube'], 'timeout')

    def test_custom_request_typeerror_is_not_retried(self):
        client = CubeClient('http://127.0.0.1:9', 'synthetic')
        for replay in (False, True):
            with self.subTest(replay=replay), mock.patch.object(
                    client, '_req', side_effect=TypeError('custom failure')) as request:
                with self.assertRaises(TypeError):
                    cube_replay._load(client, {}) if replay else client.load({})
                self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
