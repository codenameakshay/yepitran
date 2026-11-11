"""Mock-only regressions for the small Simlock review fixes."""
import io
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yepitran import android, proc, simlock


class SimlockFollowupTests(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "socket.create_connection",
                       "socket.socket.connect", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError(f"external operation forbidden: {target}"))
            guard.start()
            self.addCleanup(guard.stop)
        cancellation = patch.object(proc, "cancel", threading.Event())
        cancellation.start()
        self.addCleanup(cancellation.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.driver = android.SimlockAndroid(lambda _: None, Path(self.tmp.name),
                                            cfg={"url": "https://fleet", "token": "test"})
        self.driver.lease_id = "L"

    def test_cancelled_exec_never_starts_a_new_command(self):
        proc.cancel.set()
        request = Mock(return_value=io.BytesIO(b'event: exit\ndata: {"exitCode":0}\n'))
        with patch.object(self.driver.client, "request", request):
            with self.assertRaises(proc.Cancelled):
                self.driver.client.exec("L", "adb", ["shell", "input tap 1 2"])
        request.assert_not_called()

    def test_cancel_wins_over_a_result_that_arrives_together(self):
        def finished():
            proc.cancel.set()
            return "completed after cancel"

        def thread(*, target, daemon):
            return Mock(start=Mock(side_effect=target))

        with patch.object(simlock.threading, "Thread", side_effect=thread):
            with self.assertRaises(proc.Cancelled):
                simlock.interruptible(finished)

    def test_cleanup_exec_and_call_still_return_after_cancel(self):
        proc.cancel.set()
        answers = [io.BytesIO(b'event: exit\ndata: {"exitCode":0}\n'), io.BytesIO(b'{"released":true}')]
        with patch.object(self.driver.client, "request", side_effect=answers) as request:
            self.assertEqual((0, "", ""), self.driver.client.exec("L", "adb", ["shell", "pkill screenrecord"],
                                                               cleanup=True))
            self.assertEqual({"released": True}, self.driver.client.call("DELETE", "/v1/leases/L", cleanup=True))
        self.assertEqual(["POST", "DELETE"], [c.args[0] for c in request.call_args_list])

    def test_late_grant_release_failure_does_not_mask_cancellation(self):
        c = self.driver.client
        released = []

        def call(method, path, *args, **kwargs):
            if method == "POST":
                return {"request": {"id": "R", "state": "queued"}}
            if method == "DELETE" and path == "/v1/lease-requests/R":
                raise simlock.Error(409, "REQUEST_NOT_CANCELLABLE", "booting")
            if method == "GET":
                if not kwargs.get("cleanup"):
                    raise proc.Cancelled()
                return {"request": {"id": "R", "state": "granted", "lease": {"id": "late"}}}
            released.append(path)
            raise proc.InfraError("release unavailable")

        with patch.object(c, "call", side_effect=call), patch.object(simlock.time, "sleep"):
            with self.assertRaises(proc.Cancelled):
                c.lease("android", "Pixel 8", lambda _: None)
        self.assertEqual(["/v1/leases/late"] * 4, released)

    def test_release_retries_unknown_outcome_then_accepts_already_released(self):
        proc.cancel.set()
        c = self.driver.client
        with patch.object(c, "call", side_effect=[proc.InfraError("reply lost"),
                          simlock.Error(404, "UNKNOWN_LEASE", "gone")]) as call, \
                patch.object(simlock.time, "sleep"):
            self.assertIsNone(c.release("L"))
        self.assertEqual([("DELETE", "/v1/leases/L")] * 2, [c.args for c in call.call_args_list])
        self.assertTrue(all(c.kwargs["cleanup"] for c in call.call_args_list))

    def test_video_wait_timeout_refuses_to_download_unfinished_segment(self):
        d = self.driver
        d.video_thread = Mock(is_alive=Mock(return_value=False))
        d.remote_segments = ["/data/local/tmp/yepitran-video-00.mp4"]
        d.adb_raw = Mock(return_value=subprocess.CompletedProcess([], 1, "", "recorder still running"))
        d.join_segments = Mock()

        with self.assertRaisesRegex(proc.InfraError, "recorder still running"):
            d.stop_video()

        self.assertEqual(1, d.adb_raw.call_count)
        self.assertIn("pkill -INT screenrecord", d.adb_raw.call_args.args[1])
        d.join_segments.assert_not_called()

    def test_runtime_wait_stops_on_lease_loss_in_either_phase(self):
        for during_stability in (False, True):
            with self.subTest(during_stability=during_stability):
                d = self.driver
                d.lost = ""
                answers = [(0, "1\npackage:android", "")] if during_stability else []
                d.client.exec = Mock(side_effect=answers + [simlock.Error(404, "UNKNOWN_LEASE", "gone")])
                with patch.object(android, "sleep", side_effect=AssertionError("waited after lease loss")), \
                        patch.object(proc, "sleep", side_effect=AssertionError("waited after lease loss")):
                    with self.assertRaisesRegex(proc.InfraError, "ended the lease"):
                        d.wait_runtime()
                self.assertEqual(1 + int(during_stability), d.client.exec.call_count)

    def test_runtime_wait_still_recovers_transient_errors_and_unstable_pids(self):
        d = self.driver
        d.client.exec = Mock(side_effect=[proc.InfraError("stream broke"), (0, "0", ""),
                            (0, "1\npackage:android", ""), (0, "100", ""), (0, "200", ""),
                            (0, "300", ""), (0, "300", "")])
        with patch.object(android, "sleep"):
            self.assertIsNone(d.wait_runtime())
        self.assertEqual(7, d.client.exec.call_count)

    def test_failed_release_does_not_forget_renewer_lease_loss(self):
        d = self.driver
        d.renewer = Mock(lost="lease revoked")
        d.client.release = Mock(side_effect=proc.InfraError("gateway offline"))
        with self.assertRaisesRegex(proc.InfraError, "gateway offline"):
            d.release_lease()
        self.assertEqual("L", d.lease_id)
        with self.assertRaisesRegex(proc.InfraError, "lease revoked"):
            d.check_alive()
        d.client.release = Mock(return_value=None)
        d.release_lease()
        self.assertEqual(("", ""), (d.lease_id, d.lost))

    def test_adb_retry_checks_for_lease_loss_during_backoff(self):
        d = self.driver
        d.renewer = Mock(lost="")
        d.client.exec = Mock(side_effect=[simlock.Error(503, "WORKER_UNREACHABLE", "offline"), (0, "ok", "")])
        with patch.object(android, "sleep", side_effect=lambda _: setattr(d.renewer, "lost", "lease revoked")):
            with self.assertRaisesRegex(proc.InfraError, "lease revoked"):
                d.adb_raw("shell", "input text hello")
        self.assertEqual(1, d.client.exec.call_count)

    def test_recovery_does_not_probe_again_after_lease_loss_during_backoff(self):
        d = self.driver
        d.renewer = Mock(lost="")
        d.client.exec = Mock(side_effect=[proc.InfraError("stream broke"), (0, "device\n", "")])
        with patch.object(android, "sleep", side_effect=lambda _: setattr(d.renewer, "lost", "lease revoked")):
            with self.assertRaisesRegex(proc.InfraError, "lease revoked"):
                d.recover_adb("stream broke")
        self.assertEqual(1, d.client.exec.call_count)

    def test_recovery_does_not_accept_success_after_renewal_loses_the_lease(self):
        d = self.driver
        d.renewer = Mock(lost="")

        def execute(*args, **kwargs):
            d.renewer.lost = "lease revoked"
            return 0, "device\n", ""

        d.client.exec = Mock(side_effect=execute)
        with self.assertRaisesRegex(proc.InfraError, "lease revoked"):
            d.recover_adb("stream broke")
        self.assertEqual(1, d.client.exec.call_count)

    def apk(self):
        local = Path(self.tmp.name) / "app.apk"
        local.write_bytes(b"apk data")
        return local

    def test_upload_remembers_lease_loss_for_later_operations(self):
        d = self.driver
        d.client.exec = Mock(side_effect=simlock.Error(404, "UNKNOWN_LEASE", "gone"))
        with self.assertRaisesRegex(proc.InfraError, "ended the lease"):
            d.push(self.apk(), "/r/app.apk")
        with self.assertRaisesRegex(proc.InfraError, "ended the lease"):
            d.recover_adb("upload failed")
        self.assertEqual(1, d.client.exec.call_count)

    def test_upload_stops_if_renewal_loses_the_lease_during_backoff(self):
        d = self.driver
        d.renewer = Mock(lost="")
        d.client.exec = Mock(side_effect=[proc.InfraError("stream broke"), (0, "", "")])
        with patch.object(android, "sleep", side_effect=lambda _: setattr(d.renewer, "lost", "lease revoked")):
            with self.assertRaisesRegex(proc.InfraError, "lease revoked"):
                d.push(self.apk(), "/r/app.apk")
        self.assertEqual(1, d.client.exec.call_count)

    def test_upload_has_five_distinct_tries_without_a_terminal_backoff(self):
        d = self.driver
        d.client.exec = Mock(side_effect=proc.InfraError("stream broke"))
        with patch.object(android, "sleep") as sleep:
            with self.assertRaisesRegex(proc.InfraError, "failed 5 times"):
                d.push(self.apk(), "/r/app.apk")
        self.assertEqual([3, 6, 12, 24], [c.args[0] for c in sleep.call_args_list])
        self.assertEqual([f"base64 -d > /r/app.apk.000.{i}" for i in range(5)],
                         [c.args[2][1] for c in d.client.exec.call_args_list])

    def test_video_stop_skips_device_calls_after_loss_or_cancel(self):
        d = self.driver
        for reason in ("direct", "renewal", "cancel", "released"):
            with self.subTest(reason=reason):
                proc.cancel.clear()
                d.lease_id, d.lost, d.renewer = "L", "", None
                if reason == "direct":
                    d.lost = "lease revoked"
                elif reason == "renewal":
                    d.renewer = Mock(lost="lease revoked")
                elif reason == "cancel":
                    proc.cancel.set()
                else:
                    d.lease_id = ""
                thread = d.video_thread = Mock(is_alive=Mock(return_value=True))
                d.client.exec = Mock(side_effect=AssertionError("sent after loss or cancel"))
                self.assertIsNone(d.stop_video())
                self.assertTrue(d.video_stop.is_set())
                self.assertIs(thread, d.video_thread)
                d.client.exec.assert_not_called()

    def test_video_stop_remembers_a_lease_lost_during_polling(self):
        d = self.driver
        d.video_thread = Mock(is_alive=Mock(return_value=True))
        d.client.exec = Mock(side_effect=simlock.Error(404, "UNKNOWN_LEASE", "gone"))
        with patch.object(android.time, "monotonic", side_effect=[0, 0, 61]):
            self.assertIsNone(d.stop_video())
        with self.assertRaisesRegex(proc.InfraError, "ended the lease"):
            d.check_alive()
        self.assertEqual(1, d.client.exec.call_count)

    def test_mutation_callers_never_replay_an_unknown_exec_outcome(self):
        d = self.driver
        d.app = {"id": "example.app"}
        d.push = Mock()
        for operation, expected in ((lambda: d.tap(10, 20), "input tap 10 20"),
                                    (lambda: d.type_text("hello"), "input text hello"),
                                    (lambda: d.install(self.apk(), "allow"),
                                     "pm install -r -t -g /data/local/tmp/yepitran.apk")):
            with self.subTest(command=expected):
                mutations = []

                def execute(lease_id, tool, args, **kwargs):
                    if args == ["get-state"]:
                        return 0, "device\n", ""
                    if args == ["uninstall", "example.app"]:
                        return 0, "Success", ""
                    mutations.append(args)
                    raise proc.InfraError("exec stream ended without an exit code")

                d.client.exec = Mock(side_effect=execute)
                with self.assertRaisesRegex(proc.InfraError, "completion is unknown"):
                    operation()
                self.assertEqual([["shell", expected]], mutations)

    def test_mutation_retries_only_when_worker_refuses_before_execution(self):
        d = self.driver
        d.client.exec = Mock(side_effect=[simlock.Error(503, "WORKER_UNREACHABLE", "offline"), (0, "", "")])
        with patch.object(android, "sleep"):
            self.assertIsNone(d.tap(10, 20))
        self.assertEqual([("L", "adb", ["shell", "input tap 10 20"])] * 2,
                         [c.args for c in d.client.exec.call_args_list])

    def test_cancel_returns_before_blocked_exec_and_cleanup_runs_then_threads_drain(self):
        c = self.driver.client
        entered, finish = threading.Event(), threading.Event()
        thread_type, workers, outcome, requests = threading.Thread, [], [], []

        def request(method, path, *args, **kwargs):
            requests.append((method, path))
            if method == "POST":
                entered.set()
                finish.wait(2)
                return io.BytesIO(b'event: exit\ndata: {"exitCode":0}\n')
            return io.BytesIO(b'{"released":true}')

        def worker(**kwargs):
            thread = thread_type(**kwargs)
            workers.append(thread)
            return thread

        def call():
            try:
                c.exec("L", "adb", ["shell", "input tap 1 2"])
            except BaseException as e:
                outcome.append(type(e))

        caller = thread_type(target=call)
        with patch.object(c, "request", side_effect=request), \
                patch.object(simlock.threading, "Thread", side_effect=worker):
            try:
                caller.start()
                self.assertTrue(entered.wait(1))
                proc.cancel.set()
                caller.join(1)
                self.assertFalse(caller.is_alive())
                self.assertEqual([proc.Cancelled], outcome)
                c.release("L")
                self.assertFalse(finish.is_set())
                self.assertEqual([("POST", "/v1/leases/L/exec"), ("DELETE", "/v1/leases/L")], requests)
            finally:
                finish.set()
                caller.join(1)
                for thread in workers:
                    thread.join(1)
        self.assertTrue(workers)
        self.assertTrue(all(not thread.is_alive() for thread in workers))
