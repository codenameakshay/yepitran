from __future__ import annotations

import subprocess
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yepitran import android, proc


class AndroidReviewTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.home_patch = patch.object(android.proc, "HOME", Path(self.home.name))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        reap_patch = patch.object(android.proc, "reap", return_value=[])
        reap_patch.start()
        self.addCleanup(reap_patch.stop)
        for name in ("run", "spawn", "console"):
            guard = patch.object(android, name, side_effect=AssertionError(f"unmocked Android {name}"))
            guard.start()
            self.addCleanup(guard.stop)

    def driver(self, root: Path | None = None) -> android.Android:
        root = root or Path(tempfile.mkdtemp())
        return android.Android(lambda _: None, root)

    def listener_run(self, command: str | None, out: str = ""):
        """Stand-in for run(): lsof reports a PID on the private port (or none), ps prints that
        PID's command line, and every other call returns `out`."""
        def fake(args, **kwargs):
            if args[0] == "lsof":
                return subprocess.CompletedProcess(args, 0, "123\n" if command else "", "")
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, f"{command}\n", "")
            return subprocess.CompletedProcess(args, 0, out, "")
        return fake

    def test_all_adb_entry_points_pin_the_private_server_and_scan_limit(self):
        d = self.driver()
        d.serial = "emulator-5700"
        fork = f"{android.ADB} -L tcp:localhost:{android.ADB_PORT} fork-server server --reply-fd 4"
        with patch.object(android, "run", side_effect=self.listener_run(fork)) as run, \
             patch.object(android, "free", return_value=True):
            d.adb_raw("devices")
            d.start_adb_server()
        adb = [c for c in run.call_args_list if c.args[0][0] == android.ADB]
        self.assertEqual(["devices", "kill-server", "start-server"], [c.args[0][-1] for c in adb])
        for call in adb:
            self.assertEqual(android.ADB_ENV, call.kwargs["env"])
            self.assertIn("-P", call.args[0])

    def test_port_discovery_reserves_implicit_adb_port(self):
        processes = [
            (11, "qemu-system-aarch64 -avd other -port 5750"),
            (12, "/sdk/emulator/emulator -avd other -ports 5710,5711"),
        ]
        with patch.object(android.proc, "processes", return_value=processes):
            self.assertEqual({5710, 5711, 5750, 5751}, android.used_emulator_ports())

    def test_boot_verifies_console_identity_before_touching_adb(self):
        d = self.driver()
        d.console_port = 5700
        with patch.object(android, "console", return_value=["another_avd"]), \
             patch.object(d, "adb_raw") as adb:
            with self.assertRaisesRegex(proc.InfraError, "another session took it"):
                d.wait_booted()
        adb.assert_not_called()

    def test_shutdown_sends_console_kill_only_to_owned_avd(self):
        d = self.driver()
        d.console_port = 5700
        d.emu = Mock()
        d.emu.poll.return_value = None
        with patch.object(android, "console", return_value=["another_avd"]) as console, \
             patch.object(android, "stop"):
            d.kill_emulator()
        self.assertEqual(["avd name"], [c.args[1] for c in console.call_args_list])

    def test_shutdown_kills_owned_qemu_even_if_launcher_already_exited(self):
        d = self.driver()
        d.console_port = 5700
        d.emu = Mock()
        d.emu.poll.return_value = 1
        launcher = d.emu
        with patch.object(android, "console", side_effect=[[android.AVD], []]) as console, \
             patch.object(android, "stop") as stop:
            d.kill_emulator()
        self.assertEqual(["avd name", "kill"], [c.args[1] for c in console.call_args_list])
        stop.assert_called_once_with(launcher, sig=15, grace=30)

    def test_orphan_marker_matches_owned_avd_before_new_ports_are_picked(self):
        d = self.driver()
        marker, = d.markers()
        self.assertEqual(f"-avd {android.AVD}", marker)
        self.assertTrue(proc._marker_matches(["emulator", "-avd", android.AVD], marker, "emulator"))
        self.assertFalse(proc._marker_matches(["emulator", "-avd", android.AVD + "-other"], marker, "emulator"))

    def test_app_state_requires_exact_package_ownership_for_foreground(self):
        d = self.driver()
        d.app = {"id": "com.example"}
        d.shell = Mock(side_effect=["123", "mCurrentFocus=Window{u0 com.example.other/.Main}"])
        self.assertIn("background", d.app_state())

    def test_app_state_does_not_attribute_another_apps_anr(self):
        d = self.driver()
        d.app = {"id": "com.example"}
        d.shell = Mock(side_effect=["123", "mCurrentFocus=Window{u0 Application Not Responding: com.other}"])
        self.assertIn("background", d.app_state())

    def test_app_state_detects_owned_app_anr(self):
        d = self.driver()
        d.app = {"id": "com.example"}
        d.shell = Mock(side_effect=["123", "mCurrentFocus=Window{u0 Application Not Responding: com.example}"])
        self.assertEqual("anr", d.app_state())

    def test_app_state_detects_owned_package_foreground(self):
        d = self.driver()
        d.app = {"id": "com.example"}
        d.shell = Mock(side_effect=["123", "mCurrentFocus=Window{u0 com.example/.Main}"])
        self.assertEqual("foreground", d.app_state())

    def test_crash_log_only_attributes_an_explicit_crashed_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.driver(Path(tmp))
            d.app = {"id": "com.example"}
            d.shell = Mock(return_value=(
                "E/AndroidRuntime: FATAL EXCEPTION: main\n"
                "E/AndroidRuntime: Process: com.example.other, PID: 8\n"
                "E/AndroidRuntime: unrelated\n"))
            d.adb = Mock(return_value=subprocess.CompletedProcess([], 0, "full logcat", ""))
            self.assertIsNone(d.crash_log())
            d.shell = Mock(return_value=(
                "E/AndroidRuntime: FATAL EXCEPTION: main\n"
                "E/AndroidRuntime: Process: com.other, PID: 7\n"
                "E/AndroidRuntime: other crash\n"
                "E/AndroidRuntime: FATAL EXCEPTION: main\n"
                "E/AndroidRuntime: Process: com.example, PID: 9\n"
                "E/AndroidRuntime: owned crash\n"))
            result = d.crash_log()
            self.assertIn("owned crash", result)
            self.assertNotIn("other crash", result)

    def test_teardown_finishes_process_cleanup_after_cancellation(self):
        d = self.driver()
        fork = f"{android.ADB} -L tcp:localhost:{android.ADB_PORT} fork-server server --reply-fd 4"
        proc.cancel.set()
        try:
            with patch.object(d, "stop_video", side_effect=proc.Cancelled()), \
                 patch.object(d, "kill_emulator") as kill, \
                 patch.object(android, "run", side_effect=self.listener_run(fork)) as run, \
                 patch.object(android, "free", return_value=True):
                with self.assertRaises(proc.Cancelled):
                    d.teardown()
        finally:
            proc.cancel.clear()
        kill.assert_called_once()
        kill_server = [c for c in run.call_args_list if c.args[0][0] == android.ADB]
        self.assertEqual(["kill-server"], [c.args[0][-1] for c in kill_server])
        self.assertTrue(kill_server[0].kwargs["cleanup"])
        self.assertEqual(android.ADB_ENV, kill_server[0].kwargs["env"])

    def test_start_server_refuses_a_non_adb_listener(self):
        d = self.driver()
        with patch.object(android, "run", side_effect=self.listener_run("python3 -m http.server 5137")) as run, \
             patch.object(android, "free", return_value=True):
            with self.assertRaisesRegex(proc.InfraError, "python3"):
                d.start_adb_server()
        self.assertFalse(any(c.args[0][0] == android.ADB for c in run.call_args_list))

    def test_start_server_replaces_any_adb_listener(self):
        d = self.driver()
        fork = f"adb -L tcp:localhost:{android.ADB_PORT} fork-server server --reply-fd 4"
        with patch.object(android, "run", side_effect=self.listener_run(fork)) as run, \
             patch.object(android, "free", return_value=True):
            d.start_adb_server()
        adb = [c for c in run.call_args_list if c.args[0][0] == android.ADB]
        self.assertEqual(["kill-server", "start-server"], [c.args[0][-1] for c in adb])
        for call in adb:
            self.assertEqual(android.ADB_ENV, call.kwargs["env"])

    def test_slow_adb_server_is_killed_by_pid_after_kill_server(self):
        d = self.driver()
        fork = f"adb -L tcp:localhost:{android.ADB_PORT} fork-server server --reply-fd 4"
        with patch.object(android, "run", side_effect=self.listener_run(fork)), \
             patch.object(android, "port_released", side_effect=[False, True]), \
             patch.object(android.os, "kill") as kill:
            d.stop_adb_server()
        kill.assert_called_once_with(123, android.signal.SIGKILL)

    def test_non_adb_listener_is_never_killed(self):
        d = self.driver()
        with patch.object(android, "run", side_effect=self.listener_run("python3 -m http.server 5137")), \
             patch.object(android.os, "kill") as kill:
            with self.assertRaises(android.InfraError):
                d.stop_adb_server()
        kill.assert_not_called()

    def test_port_in_time_wait_counts_as_free_but_a_listener_does_not(self):
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        self.assertFalse(android.free(port))
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = listener.accept()
        accepted.close()
        listener.close()
        time.sleep(0.2)
        client.close()
        # Linux holds the port in FIN_WAIT/LAST_ACK for a moment before TIME_WAIT; port_released() waits the same way.
        deadline = time.monotonic() + 5
        while not android.free(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(android.free(port))

    def test_private_adb_command_accepts_sdk_paths_with_spaces(self):
        with patch.object(android, "ADB", "/opt/Android SDK/platform-tools/adb"):
            self.assertTrue(android.Android.is_adb_server(
                f"{android.ADB} -L tcp:localhost:{android.ADB_PORT} fork-server server --reply-fd 4"))
            self.assertFalse(android.Android.is_adb_server("python3 -m http.server 5137"))

    def test_recovery_restarts_owned_private_server_before_reconnecting(self):
        d = self.driver()
        d.serial = "emulator-5700"
        fork = f"{android.ADB} -L tcp:localhost:{android.ADB_PORT} fork-server server --reply-fd 4"
        with patch.object(d, "check_alive"), \
             patch.object(android, "run", side_effect=self.listener_run(fork, "emulator-5700\tdevice\n")) as run, \
             patch.object(android, "wait_until", side_effect=lambda fn, *a, **k: fn() or self.fail("device not listed")), \
             patch.object(d, "apply_reverse") as reverse, \
             patch.object(android, "free", return_value=True):
            d.recover_adb("server wedged")
        adb = [c for c in run.call_args_list if c.args[0][0] == android.ADB]
        self.assertEqual(["kill-server", "start-server", "devices"], [c.args[0][-1] for c in adb])
        self.assertTrue(adb[0].kwargs["cleanup"])
        for call in adb:
            self.assertEqual(android.ADB_ENV, call.kwargs["env"])
        reverse.assert_called_once()

    def test_recovery_restarts_when_owned_adb_server_has_already_exited(self):
        d = self.driver()
        d.serial = "emulator-5700"
        with patch.object(d, "check_alive"), \
             patch.object(android, "run", side_effect=self.listener_run(None, "emulator-5700\tdevice\n")) as run, \
             patch.object(android, "wait_until", side_effect=lambda fn, *a, **k: fn() or self.fail("device not listed")), \
             patch.object(d, "apply_reverse") as reverse:
            d.recover_adb("server exited")
        adb = [c for c in run.call_args_list if c.args[0][0] == android.ADB]
        self.assertEqual(["start-server", "devices"], [c.args[0][-1] for c in adb])
        for call in adb:
            self.assertEqual(android.ADB_ENV, call.kwargs["env"])
        reverse.assert_called_once()

    def test_prepare_output_cleanup_removes_only_known_attempt_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("emulator.log", "logcat.txt", "segments.txt", "video.mp4", "video.webm",
                         "video-00.webm"):
                (root / name).write_text("stale")
            untouched = root / "notes.txt"
            untouched.write_text("keep")
            self.driver(root).clear_attempt_outputs()
            self.assertFalse(any((root / name).exists() for name in (
                "emulator.log", "logcat.txt", "segments.txt", "video.mp4", "video.webm", "video-00.webm")))
            self.assertEqual("keep", untouched.read_text())

    def test_cold_boot_wipes_userdata_before_saving_clean_snapshot(self):
        d = self.driver()
        events = []
        scenario = type("Scenario", (), {"app": "", "reverse": [], "permissions": "deny"})()
        done = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(android, "apk_info", return_value={"id": "app.test", "activity": "Main"}), \
             patch.object(d, "ensure_avd"), patch.object(d, "kill_emulator"), \
             patch.object(d, "start_adb_server"), patch.object(android, "pick_ports", return_value=(5700, 5701)), \
             patch.object(android, "spawn", return_value=Mock()) as spawn, patch.object(d, "wait_booted"), \
             patch.object(d, "configure"), patch.object(d, "adb", side_effect=lambda *a, **k: events.append(("adb", a)) or done), \
             patch.object(android, "console", side_effect=lambda *a, **k: events.append(("console", a)) or ["yepitran_API36"]), \
             patch.object(d, "install", side_effect=lambda *a: events.append(("install", a))), \
             patch.object(d, "apply_reverse"), patch.object(d, "shell", return_value="9"):
            d.prepare(Path("app.apk"), scenario, level=2)
        commands = [(kind, args[0] if args else None) for kind, args in events]
        uninstall = next(i for i, e in enumerate(commands) if e == ("adb", "uninstall"))
        snapshot = next(i for i, e in enumerate(commands) if e[0] == "console")
        install = next(i for i, e in enumerate(commands) if e[0] == "install")
        self.assertLess(uninstall, snapshot)
        self.assertLess(snapshot, install)
        self.assertIn("-wipe-data", spawn.call_args.args[0])

    def test_uiautomator_extracts_xml_despite_console_diagnostics(self):
        d = self.driver()
        xml = ('<not-xml>\n<hierarchy><node class="android.widget.Button" '
               'bounds="[1,2][11,22]" text="OK" /></hierarchy>\nUI hierchary dumped to: /dev/tty >')
        with patch.object(d, "adb", return_value=subprocess.CompletedProcess([], 0, xml, "")):
            nodes = d.tree()
        self.assertEqual("OK", nodes[0].label)
        self.assertEqual((1, 2, 10, 20), (nodes[0].x, nodes[0].y, nodes[0].w, nodes[0].h))

    def test_input_text_quotes_shell_metacharacters_and_rejects_controls(self):
        d = self.driver()
        with patch.object(d, "shell") as shell:
            d.type_text("hello; reboot 'x' $(id)")
        command = shell.call_args.args[0]
        self.assertTrue(command.startswith("input text '"))
        self.assertEqual(3, len(android.shlex.split(command)))
        self.assertEqual("input", android.shlex.split(command)[0])
        with self.assertRaisesRegex(proc.InfraError, "control characters"):
            d.type_text("hello\nreboot")
        with self.assertRaisesRegex(proc.InfraError, "reserves %s"):
            d.type_text("literal %s")

    def test_ambiguous_mutation_timeout_is_recovered_but_never_replayed(self):
        d = self.driver()
        with patch.object(d, "adb_raw", side_effect=proc.InfraError("timed out")) as raw, \
             patch.object(d, "recover_adb") as recover:
            with self.assertRaisesRegex(proc.InfraError, "completion is unknown"):
                d.tap(10, 20)
        self.assertEqual(1, raw.call_count)
        recover.assert_called_once()

    def test_read_only_adb_operation_retries_after_transport_recovery(self):
        d = self.driver()
        offline = subprocess.CompletedProcess([], 1, "", "device offline")
        ok = subprocess.CompletedProcess([], 0, "1\n", "")
        with patch.object(d, "adb_raw", side_effect=[offline, ok]) as raw, \
             patch.object(d, "recover_adb") as recover:
            self.assertEqual("1\n", d.shell("getprop sys.boot_completed"))
        self.assertEqual(2, raw.call_count)
        recover.assert_called_once()

    def test_reverse_failure_is_not_silently_ignored(self):
        d = self.driver()
        d.reverse = [8080]
        failed = subprocess.CompletedProcess([], 1, "", "device offline")
        with patch.object(d, "adb_raw", return_value=failed):
            with self.assertRaisesRegex(proc.InfraError, "adb reverse tcp:8080 failed"):
                d.apply_reverse()

    def test_video_start_discards_old_output_and_cancel_skips_concat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "video-00.webm").write_bytes(b"old")
            target = root / "video.mp4"
            target.write_bytes(b"old")
            d = self.driver(root)
            with patch.object(android.threading.Thread, "start"):
                d.start_video(target)
            self.assertFalse((root / "video-00.webm").exists())
            self.assertFalse(target.exists())
            d.video_thread = Mock()
            d.video_thread.is_alive.return_value = False
            d.segments = [root / "video-01.webm"]
            d.segments[0].write_bytes(b"fresh")
            with patch.object(android.proc.cancel, "is_set", return_value=True), \
                 patch.object(android, "run") as run:
                d.stop_video()
            run.assert_not_called()

    def test_video_rotation_concats_all_segments_with_escaped_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "user's-attempt"
            root.mkdir()
            d = self.driver(root)
            target = root / "video.mp4"
            starts = []

            def record(port, command, *args, **kwargs):
                if command.startswith("screenrecord start"):
                    path = Path(command.split()[-1])
                    path.write_bytes(b"segment")
                    starts.append(path)
                    if len(starts) == 2:
                        d.video_stop.set()
                return []

            with patch.object(android, "SEGMENT", 0), patch.object(android, "console", side_effect=record), \
                 patch.object(android.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                 patch.object(android.time, "sleep"), \
                 patch.object(android, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                d.start_video(target)
                d.video_thread.join(5)
                d.stop_video()
            self.assertEqual(2, len(starts))
            listing = root / "segments.txt"
            self.assertIn("'\\''", android.ffconcat_quote(starts[0]))
            self.assertIn("-f", run.call_args.args[0])
            self.assertFalse(listing.exists())
            self.assertTrue(all(not segment.exists() for segment in starts))

    def test_video_start_refuses_to_replace_a_live_prior_recorder(self):
        d = self.driver()
        d.video_thread = Mock()
        d.video_thread.is_alive.return_value = True
        with self.assertRaisesRegex(proc.InfraError, "previous Android video recorder"):
            d.start_video(Path("video.mp4"))

    def test_prepare_timeout_keeps_live_recorder_and_stops_before_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.driver(Path(tmp))
            d.console_port = 5700
            d.video_thread = Mock()
            d.video_thread.is_alive.return_value = True
            original_workdir = d.workdir
            with patch.object(d.video_thread, "join") as join, \
                 patch.object(android, "apk_info") as apk_info:
                with self.assertRaisesRegex(proc.InfraError, "video recorder did not stop"):
                    d.prepare(Path("unused.apk"), Mock(), level=1)
            join.assert_called_once_with(30)
            apk_info.assert_not_called()
            self.assertIsNotNone(d.video_thread)
            self.assertEqual(5700, d.console_port)
            self.assertEqual(original_workdir, d.workdir)


if __name__ == "__main__":
    unittest.main()
