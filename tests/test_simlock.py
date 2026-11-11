from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from yepitran import android, proc, simlock
from yepitran.proc import InfraError


def stream(*lines: bytes) -> MagicMock:
    """Stand-in for the context manager urlopen returns: iterating it yields the raw lines."""
    cm = MagicMock()
    cm.__enter__.return_value = cm
    cm.__exit__.return_value = False
    cm.__iter__.return_value = iter(lines)
    return cm


class SimlockClientTests(unittest.TestCase):
    def setUp(self):
        self.client = simlock.Client({"url": "https://g", "token": "t"})

    def test_exec_parses_sse_stream_into_exit_code_and_output(self):
        lines = (
            b"event: output\n", b'data: {"stream":"stdout","chunk":"hel"}\n', b"\n",
            b"event: output\n", b'data: {"stream":"stderr","chunk":"warn"}\n', b"\n",
            b"event: output\n", b'data: {"stream":"stdout","chunk":"lo"}\n', b"\n",
            b"event: exit\n", b'data: {"exitCode":3}\n',
        )
        with patch.object(self.client, "request", return_value=stream(*lines)):
            result = self.client.exec("L", "adb", ["devices"])
        self.assertEqual((3, "hello", "warn"), result)

    def test_exec_raises_infra_error_when_stream_ends_without_exit(self):
        lines = (b"event: output\n", b'data: {"stream":"stdout","chunk":"hi"}\n', b"\n")
        with patch.object(self.client, "request", return_value=stream(*lines)):
            with self.assertRaisesRegex(InfraError, "without an exit code"):
                self.client.exec("L", "adb", ["devices"])

    def test_exec_raises_infra_error_on_error_event(self):
        lines = (b"event: error\n", b'data: {"code":"EXEC_TIMEOUT","message":"x"}\n')
        with patch.object(self.client, "request", return_value=stream(*lines)):
            with self.assertRaisesRegex(InfraError, "EXEC_TIMEOUT"):
                self.client.exec("L", "adb", ["devices"])


class SimlockConfigTests(unittest.TestCase):
    def test_config_reads_env_and_strips_trailing_slash(self):
        env = {"YEPITRAN_SIMLOCK_URL": "https://g/", "YEPITRAN_SIMLOCK_TOKEN": "t"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual({"url": "https://g", "token": "t"}, simlock.config())

    def test_config_is_none_without_env_or_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(simlock, "CONFIG", Path(tmp) / "nonexistent.json"):
            self.assertIsNone(simlock.config())


class BackendTests(unittest.TestCase):
    def test_forced_local_wins_over_configured_gateway(self):
        with patch.dict(os.environ, {"YEPITRAN_ANDROID": "local"}, clear=True), \
             patch.object(android.simlock, "config", return_value={"url": "u", "token": "t"}):
            self.assertEqual("local", android.backend())

    def test_gateway_configured_selects_simlock(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(android.simlock, "config", return_value={"url": "u", "token": "t"}):
            self.assertEqual("simlock", android.backend())

    def test_no_gateway_selects_local(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(android.simlock, "config", return_value=None):
            self.assertEqual("local", android.backend())


class SimlockAndroidTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        for name in ("run", "spawn", "console"):
            guard = patch.object(android, name, side_effect=AssertionError(f"unmocked Android {name}"))
            guard.start()
            self.addCleanup(guard.stop)

    def driver(self) -> android.SimlockAndroid:
        return android.SimlockAndroid(lambda _: None, Path(self.home.name),
                                      cfg={"url": "u", "token": "t"})

    def test_adb_raw_resends_after_503(self):
        d = self.driver()
        d.lease_id = "L"
        d.client.exec = Mock(side_effect=[simlock.Error(503, "WORKER_UNREACHABLE", "x"), (0, "ok", "")])
        with patch.object(android, "sleep"):
            p = d.adb_raw("get-state")
        self.assertEqual(0, p.returncode)
        self.assertEqual("ok", p.stdout)
        self.assertEqual(2, d.client.exec.call_count)

    def test_adb_raw_does_not_resend_after_422(self):
        d = self.driver()
        d.lease_id = "L"
        d.client.exec = Mock(side_effect=simlock.Error(422, "BAD_REQUEST", "x"))
        with patch.object(android, "sleep"):
            with self.assertRaises(simlock.Error):
                d.adb_raw("get-state")
        self.assertEqual(1, d.client.exec.call_count)

    def test_adb_raw_reports_unknown_lease_as_ended(self):
        d = self.driver()
        d.lease_id = "L"
        d.client.exec = Mock(side_effect=simlock.Error(404, "UNKNOWN_LEASE", "x"))
        with patch.object(android, "sleep"):
            with self.assertRaisesRegex(InfraError, "ended the lease"):
                d.adb_raw("get-state")
        self.assertEqual(1, d.client.exec.call_count)

    def test_adb_raw_refuses_when_renewer_lost_the_lease(self):
        d = self.driver()
        d.lease_id = "L"
        d.renewer = Mock(lost="gone")
        d.client.exec = Mock()
        with self.assertRaisesRegex(InfraError, "gone"):
            d.adb_raw("get-state")
        d.client.exec.assert_not_called()

    def test_push_splits_file_into_base64_parts_and_joins_them(self):
        d = self.driver()
        d.lease_id = "L"
        data = os.urandom(13 * 1024 * 1024)
        local = Path(self.home.name) / "app.apk"
        local.write_bytes(data)
        d.client.exec = Mock(return_value=(0, "", ""))
        d.shell = Mock(return_value=f"{hashlib.md5(data).hexdigest()}  /data/local/tmp/yepitran.apk")
        with patch.object(android, "CHUNK", 6 * 1024 * 1024):
            d.push(local, "/data/local/tmp/yepitran.apk")

        self.assertEqual(3, d.client.exec.call_count)
        by_part = {}
        for call in d.client.exec.call_args_list:
            args = call.args[2]
            self.assertEqual("shell", args[0])
            part = args[1].split(".")[-2]  # <remote>.<part>.<try>
            by_part[part] = base64.b64decode(call.kwargs["stdin"])
        self.assertEqual(
            {"000", "001", "002"},
            {k for k in by_part},
        )
        chunk = 6 * 1024 * 1024
        self.assertEqual(data[:chunk], by_part["000"])
        self.assertEqual(data[chunk:2 * chunk], by_part["001"])
        self.assertEqual(data[2 * chunk:], by_part["002"])

    def test_push_retry_writes_a_fresh_file_and_joins_only_the_good_one(self):
        d = self.driver()
        d.lease_id = "L"
        data = b"apk data"
        local = Path(self.home.name) / "app.apk"
        local.write_bytes(data)
        d.client.exec = Mock(side_effect=[InfraError("stream broke"), (0, "", "")])
        d.shell = Mock(return_value=hashlib.md5(data).hexdigest())
        with patch.object(android, "sleep"):
            d.push(local, "/r/app.apk")
        first, second = (c.args[2][1].split("> ", 1)[1] for c in d.client.exec.call_args_list)
        self.assertNotEqual(first, second)
        joined = d.shell.call_args.args[0].split(" && ", 1)[0]
        self.assertIn(second, joined)
        self.assertNotIn(first, joined)

    def test_push_refuses_an_empty_file_before_any_upload(self):
        d = self.driver()
        d.lease_id = "L"
        local = Path(self.home.name) / "empty.apk"
        local.write_bytes(b"")
        d.client.exec = Mock()
        with self.assertRaisesRegex(android.AppError, "empty"):
            d.push(local, "/r/app.apk")
        d.client.exec.assert_not_called()

    def test_stop_video_stops_screenrecord_even_after_its_stream_died(self):
        d = self.driver()
        d.lease_id = "L"
        d.video_thread = Mock(is_alive=Mock(return_value=False))
        d.video_stop = Mock()
        d.remote_segments = []
        d.shell = Mock(return_value="")
        d.join_segments = Mock()
        d.stop_video()
        self.assertIn("pkill -INT screenrecord", d.shell.call_args.args[0])

    def test_push_refuses_damaged_upload(self):
        d = self.driver()
        d.lease_id = "L"
        data = os.urandom(1024)
        local = Path(self.home.name) / "app.apk"
        local.write_bytes(data)
        d.client.exec = Mock(return_value=(0, "", ""))
        d.shell = Mock(return_value="0" * 32 + "  /data/local/tmp/yepitran.apk")
        with self.assertRaisesRegex(InfraError, "damaged"):
            d.push(local, "/data/local/tmp/yepitran.apk")

    def test_prepare_refuses_reverse_before_leasing(self):
        d = self.driver()
        d.client = Mock()
        scenario = Mock(reverse=[3000])
        with patch.object(d, "load_app"):
            with self.assertRaisesRegex(InfraError, "reverse"):
                d.prepare(Path("app.apk"), scenario, 0)
        d.client.lease.assert_not_called()

    def test_prepare_gives_back_leftover_leases_before_leasing(self):
        d = self.driver()
        scenario = Mock(reverse=[], permissions="allow")
        d.client = Mock()
        d.client.my_leases.return_value = [{"id": "old1"}]
        events = []
        d.client.release.side_effect = lambda lease_id: events.append(("release", lease_id))

        def lease(*args, **kwargs):
            events.append(("lease",))
            return {"id": "new", "address": "emulator-5586", "device": "Pixel 8", "os": "36",
                    "worker": {"label": "w"}}

        d.client.lease.side_effect = lease
        with patch.object(d, "load_app"), \
             patch.object(android.simlock, "Renewer"), \
             patch.object(d, "wait_runtime"), \
             patch.object(d, "configure"), \
             patch.object(d, "install"), \
             patch.object(d, "shell", return_value="123"):
            d.prepare(Path("app.apk"), scenario, 0)

        self.assertEqual([("release", "old1"), ("lease",)], events)
        self.assertEqual("new", d.lease_id)
        self.assertEqual("emulator-5586", d.serial)

    def test_teardown_releases_lease_even_when_stop_video_raises(self):
        d = self.driver()
        d.lease_id = "L"
        d.client = Mock()
        d.renewer = Mock()
        renewer = d.renewer
        with patch.object(d, "stop_video", side_effect=InfraError("x")):
            with self.assertRaises(InfraError):
                d.teardown()
        d.client.release.assert_called_once_with("L")
        renewer.stop.assert_called_once()
        self.assertEqual("", d.lease_id)

    def test_markers_is_empty(self):
        self.assertEqual([], self.driver().markers())


if __name__ == "__main__":
    unittest.main()


class ClientCancelTests(unittest.TestCase):
    def setUp(self):
        self.client = simlock.Client({"url": "https://g", "token": "t"})

    def tearDown(self):
        proc.cancel.clear()

    def test_ctrl_c_returns_while_a_gateway_call_still_blocks(self):
        import threading
        release = threading.Event()
        def blocked(*a, **k):
            release.wait(10)
            raise AssertionError("should have been abandoned")
        with patch.object(self.client, "request", side_effect=blocked):
            threading.Timer(0.3, proc.cancel.set).start()
            t = time.monotonic()
            with self.assertRaises(proc.Cancelled):
                self.client.exec("L", "adb", ["shell", "input tap 1 2"])
            self.assertLess(time.monotonic() - t, 3)
        release.set()

    def test_cleanup_calls_still_run_after_ctrl_c(self):
        proc.cancel.set()
        with patch.object(self.client, "request") as req:
            req.return_value.__enter__.return_value.read.return_value = b"{}"
            self.client.release("L")
        req.assert_called_once()

    def test_too_late_to_cancel_waits_for_the_grant_and_gives_it_back(self):
        answers = iter([{"request": {"id": "r", "state": "booting"}},
                        {"request": {"id": "r", "state": "granted", "lease": {"id": "L"}}}])
        def call(method, path, *a, **k):
            if method == "DELETE" and path == "/v1/lease-requests/r":
                raise simlock.Error(409, "REQUEST_NOT_CANCELLABLE", "started")
            if method == "DELETE":
                return {}
            return next(answers)
        with patch.object(self.client, "call", side_effect=call) as mocked:
            self.client.cancel_request({"id": "r"})
        self.assertIn(("DELETE", "/v1/leases/L"), [c.args[:2] for c in mocked.call_args_list])


class LeaseLossTests(unittest.TestCase):
    def test_release_retries_through_a_worker_blip(self):
        c = simlock.Client({"url": "https://g", "token": "t"})
        with patch.object(c, "call", side_effect=[simlock.Error(503, "WORKER_UNREACHABLE", "x"), {}]) as call, \
             patch.object(simlock.time, "sleep"):
            c.release("L")
        self.assertEqual(2, call.call_count)

    def test_a_lost_lease_fails_recovery_at_once_without_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            drv = android.SimlockAndroid(lambda _: None, Path(d), cfg={"url": "u", "token": "t"})
            drv.lease_id = "L"
            drv.client.exec = Mock(side_effect=simlock.Error(404, "UNKNOWN_LEASE", "gone"))
            with self.assertRaisesRegex(InfraError, "ended the lease"):
                drv.adb("shell", "input tap 1 2")
            self.assertEqual(1, drv.client.exec.call_count)  # no 2-minute wait, no resend


class HostTests(unittest.TestCase):
    def test_apk_with_only_x86_code_is_refused_on_an_arm_host(self):
        out = "package: name='a.b'\nlaunchable-activity: name='a.b.Main'\nnative-code: 'x86_64'\n"
        with patch.object(android, "HOST_ABI", "arm64-v8a"), patch.object(android, "build_tool", return_value="aapt2"), \
             patch.object(android, "run", return_value=Mock(stdout=out, stderr="")):
            with self.assertRaises(android.AppError):
                android.apk_info(Path("app.apk"))

    def test_arm64_apk_is_accepted_on_an_x86_host(self):
        out = "package: name='a.b'\nlaunchable-activity: name='a.b.Main'\nnative-code: 'arm64-v8a'\n"
        with patch.object(android, "HOST_ABI", "x86_64"), patch.object(android, "build_tool", return_value="aapt2"), \
             patch.object(android, "run", return_value=Mock(stdout=out, stderr="")):
            self.assertEqual("a.b", android.apk_info(Path("app.apk"))["id"])
