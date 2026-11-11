from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from yepitran import cli, proc


class LifecycleReviewTests(unittest.TestCase):
    def test_default_report_paths_do_not_collide_in_the_same_second(self):
        from yepitran.runner import Leg

        class Runner:
            def __init__(self, platform, artifact, *args):
                self.platform, self.artifact = platform, artifact

            def run(self):
                return Leg(self.platform, str(self.artifact), verdict="PASS")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cli, "classify", return_value=({"ios": root / "app.app"}, None)), \
                    patch.object(cli, "LegRunner", Runner), patch.object(cli.Path, "cwd", return_value=root), \
                    patch.object(cli.time, "strftime", return_value="same-second"), \
                    patch.object(cli.sys, "stderr", StringIO()):
                first = cli.run_scenario([], Path("smoke.proof"), "ios", None, 0, scenario_text="launch")
                second = cli.run_scenario([], Path("smoke.proof"), "ios", None, 0, scenario_text="launch")
            self.assertEqual((first[0], second[0]), ("PASS", "PASS"))
            self.assertNotEqual(first[1], second[1])

    def test_cancelled_build_produces_an_infrastructure_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(cli, "classify", return_value=({}, root)), \
                    patch.object(cli.build, "app_dir", return_value=root), \
                    patch.object(cli.build, "build", side_effect=proc.Cancelled), \
                    patch.object(cli, "LegRunner") as runner, \
                    patch.object(cli.sys, "stderr", StringIO()):
                verdict, out = cli.run_scenario([], Path("smoke.proof"), "ios", root / "out", 0,
                                               scenario_text="launch")
            self.assertEqual(verdict, "INFRA FAIL")
            self.assertIn("build cancelled", json.loads((out / "verdict.json").read_text())["legs"][0]["reason"])
            runner.assert_not_called()

    def test_reap_requires_simctl_to_be_the_actual_xcrun_tool(self):
        inventory = [(101, "xcrun simctl spawn owned-udid launchctl list"),
                     (102, "xcrun python report.py simctl spawn owned-udid"),
                     (103, "axe-wrapper describe-ui --udid owned-udid"),
                     (104, "axe describe-ui --udid owned-udid-copy"),
                     (105, "axe describe-ui --udid owned-udid")]
        with patch.object(proc, "processes", return_value=inventory), \
                patch.object(proc.os, "getpid", return_value=999), \
                patch.object(proc.os, "kill") as kill:
            killed = proc.reap(["owned-udid"])
        self.assertEqual([call.args[0] for call in kill.call_args_list], [101, 105])
        self.assertEqual(len(killed), 2)

    def test_cancelled_short_command_terminates_its_process_group(self):
        child = Mock()
        child.pid = 3030
        child.returncode = -signal.SIGTERM
        proc.cancel.clear()
        calls = 0

        def request_cancel(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                return "", ""
            proc.cancel.set()
            raise subprocess.TimeoutExpired(["tool"], 0.25)

        child.communicate.side_effect = request_cancel
        try:
            with patch.object(proc, "load_factor", return_value=1), \
                    patch.object(proc.subprocess, "Popen", return_value=child), \
                    patch.object(proc.os, "killpg") as killpg:
                with self.assertRaises(proc.Cancelled):
                    proc.run(["tool"], timeout=1)
            self.assertEqual(
                [(3030, signal.SIGTERM), (3030, signal.SIGKILL)],
                [call.args for call in killpg.call_args_list],
            )
        finally:
            proc.cancel.clear()

    def test_android_port_marker_matches_only_emulator_device_args(self):
        marker = "-ports 5700,5701"
        self.assertTrue(proc._marker_matches(
            ["/sdk/emulator/emulator", "-avd", "yepitran", "-ports", "5700,5701"],
            marker,
            "emulator",
        ))
        self.assertTrue(proc._marker_matches(
            ["qemu-system-aarch64", "-ports", "5700,5701"], marker, "qemu-system-aarch64"))
        self.assertFalse(proc._marker_matches(
            ["/sdk/emulator/emulator", "-avd", "other", "-ports", "5702,5703"],
            marker,
            "emulator",
        ))
        self.assertFalse(proc._marker_matches(
            ["adb", "-P", "5700", "shell", "echo", "5700,5701"], marker, "adb"))

    def test_named_avd_marker_is_an_exact_argument_not_a_substring(self):
        marker = "-avd yepitran_API36"
        self.assertTrue(proc._marker_matches(
            ["/sdk/emulator/emulator", "-avd", "yepitran_API36", "-port", "5700"],
            marker,
            "emulator",
        ))
        self.assertFalse(proc._marker_matches(
            ["/sdk/emulator/emulator", "-avd", "yepitran_API36-copy", "-port", "5700"],
            marker,
            "emulator",
        ))

    def test_reap_selects_only_processes_with_exact_owned_device_selector(self):
        inventory = [
            (101, "/sdk/emulator/emulator -avd yepitran -ports 5700,5701"),
            (102, "/sdk/emulator/emulator -avd other -ports 5702,5703"),
            (103, "adb -P 5700 shell echo 5700,5701"),
        ]
        with patch.object(proc, "processes", return_value=inventory), \
                patch.object(proc.os, "getpid", return_value=999), \
                patch.object(proc.os, "kill") as kill:
            killed = proc.reap(["-ports 5700,5701"])

        self.assertEqual([101], [call.args[0] for call in kill.call_args_list])
        self.assertEqual(["101 /sdk/emulator/emulator -avd yepitran -ports 5700,5701"], killed)

    def test_stop_signals_emulator_process_group_even_if_launcher_exits(self):
        child = Mock()
        child.pid = 4242
        child._yepitran_stopped = False
        child.poll.return_value = 0
        child.wait.return_value = 0
        with patch.object(proc.os, "killpg") as killpg:
            proc.stop(child, sig=signal.SIGINT, grace=0.01)

        self.assertEqual((4242, signal.SIGINT), killpg.call_args_list[0].args)
        self.assertIn((4242, 0), [call.args for call in killpg.call_args_list])
        self.assertEqual((4242, signal.SIGKILL), killpg.call_args_list[-1].args)
        child.wait.assert_called_once_with(5)

    def test_second_termination_request_is_idempotent_and_does_not_reap(self):
        cancel = proc.cancel
        cancel.clear()
        with patch.object(proc, "reap") as reap, patch.object(cli.sys, "stderr", StringIO()):
            cli._terminate(signal.SIGINT, None)
            cli._terminate(signal.SIGINT, None)
        self.assertTrue(cancel.is_set())
        reap.assert_not_called()
        cancel.clear()

    def test_cancelled_lease_wait_closes_its_descriptor_and_releases_after_owner(self):
        from yepitran import lease as lease_module

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lease_module, "HOME", Path(tmp)):
                owner = lease_module.Lease("android", "owner")
                owner.acquire(0, lambda _: None)
                waiter = lease_module.Lease("android", "waiter")
                with patch.object(lease_module, "sleep", side_effect=proc.Cancelled):
                    with self.assertRaises(proc.Cancelled):
                        waiter.acquire(30, lambda _: None)
                self.assertIsNone(waiter.fh)
                waiter.release()
                self.assertEqual("owner", owner.holder()["run"])
                owner.release()

                replacement = lease_module.Lease("android", "replacement")
                replacement.acquire(0, lambda _: None)
                replacement.release()


if __name__ == "__main__":
    unittest.main()
