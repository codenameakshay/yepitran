from __future__ import annotations

import http.client
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch

from yepitran import simlock
from yepitran import proc
from yepitran.proc import Cancelled, InfraError


class BlockingResponse:
    def __init__(self):
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed.set()

    def read(self):
        self.closed.wait(2)
        return b"{}"

    def __iter__(self):
        yield b"event: output\n"
        self.closed.wait(2)
        raise OSError("closed during SSE read")


class SimlockClientReviewTests(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "socket.create_connection",
                       "socket.socket.connect", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError(f"external operation forbidden: {target}"))
            guard.start()
            self.addCleanup(guard.stop)
        self.cancel = threading.Event()
        cancellation = patch.object(proc, "cancel", self.cancel)
        cancellation.start()
        self.addCleanup(cancellation.stop)
        self.client = simlock.Client({"url": "https://gateway", "token": "secret"})

    def test_structured_simlock_error_remains_retry_safe(self):
        structured = urllib_error(502, b'{"error":{"code":"WORKER_UNREACHABLE","message":"down"}}')
        with patch.object(simlock.urllib.request, "urlopen", side_effect=structured):
            with self.assertRaises(simlock.Error) as caught:
                self.client.request("POST", "/v1/leases/L/exec", {})
        self.assertEqual("WORKER_UNREACHABLE", caught.exception.code)

    def test_proxy_or_malformed_http_error_is_unknown_outcome_without_replay(self):
        for body in (b"<html>bad gateway</html>", b"null", b"[]", b'{"error":null}',
                     b'{"error":{"code":123,"message":"down"}}'):
            with self.subTest(body=body), patch.object(
                    simlock.urllib.request, "urlopen", side_effect=urllib_error(502, body)) as request:
                with self.assertRaisesRegex(InfraError, "outcome may be unknown") as caught:
                    self.client.exec("L", "adb", ["shell", "tap", "1", "2"])
                self.assertNotIsInstance(caught.exception, simlock.Error)
                self.assertEqual(1, request.call_count)

    def test_truncated_exec_stream_is_unknown_outcome_without_replay(self):
        class TruncatedResponse(BlockingResponse):
            def __iter__(self):
                yield b"event: output\n"
                raise http.client.IncompleteRead(b"partial", 20)

        with patch.object(simlock.urllib.request, "urlopen", return_value=TruncatedResponse()) as request:
            with self.assertRaisesRegex(InfraError, "stream broke") as caught:
                self.client.exec("L", "adb", ["shell", "tap", "1", "2"])
        self.assertNotIsInstance(caught.exception, simlock.Error)
        self.assertEqual(1, request.call_count)

    def test_invalid_config_is_infra_error_without_exposing_secrets(self):
        for source in ("broken secret-token", "[]", '{"url":42,"token":"secret-token"}',
                       '{"url":"https://gateway","token":42}'):
            with self.subTest(source=source), patch.dict(simlock.os.environ, {}, clear=True), \
                 patch.object(simlock, "CONFIG") as config:
                config.exists.return_value = True
                config.read_text.return_value = source
                with self.assertRaisesRegex(InfraError, "configuration") as caught:
                    simlock.config()
                self.assertNotIn("secret-token", str(caught.exception))
        with patch.dict(simlock.os.environ, {}, clear=True), patch.object(simlock, "CONFIG") as config:
            config.exists.side_effect = OSError("private path with secret-token")
            with self.assertRaisesRegex(InfraError, "configuration") as caught:
                simlock.config()
            self.assertNotIn("secret-token", str(caught.exception))

    def test_cancel_interrupts_blocked_response_body(self):
        cancellation = self.cancel
        response = BlockingResponse()
        self.addCleanup(response.close)
        with patch.object(self.client, "request", return_value=response):
            outcome = []

            def call():
                try:
                    self.client.call("GET", "/v1/leases")
                except BaseException as e:
                    outcome.append(e)

            thread = threading.Thread(target=call)
            thread.start()
            time.sleep(.05)
            started = time.monotonic()
            cancellation.set()
            thread.join(.5)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, .5)
        self.assertIsInstance(outcome[0], Cancelled)

    def test_exec_cancellation_during_sse_does_not_report_exit_or_replay(self):
        cancellation = self.cancel
        response = BlockingResponse()
        self.addCleanup(response.close)
        requests = Mock(return_value=response)
        with patch.object(self.client, "request", requests):
            outcome = []

            def execute():
                try:
                    self.client.exec("L", "adb", ["shell", "tap"])
                except BaseException as e:
                    outcome.append(e)

            thread = threading.Thread(target=execute)
            thread.start()
            time.sleep(.05)
            cancellation.set()
            thread.join(.5)
            self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], Cancelled)
        requests.assert_called_once()

def urllib_error(status: int, body: bytes):
    import urllib.error

    return urllib.error.HTTPError("https://gateway", status, "error", {}, io.BytesIO(body))


if __name__ == "__main__":
    unittest.main()
