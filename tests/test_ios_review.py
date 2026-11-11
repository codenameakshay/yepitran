import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yepitran import proc
from yepitran.ios import IOS
from yepitran.proc import AppError, InfraError
from yepitran.ui import Node


class IOSReviewTests(unittest.TestCase):
    def test_clear_text_accepts_the_shared_readback_length(self):
        text = ["old text"]
        selected = [False]

        def axe(*args):
            if args == ("key-combo", "--modifiers", "227", "--key", "4"):
                selected[0] = True
            elif args == ("key", "42") and selected[0]:
                text[0] = ""

        with mock.patch.object(self.driver, "axe", side_effect=axe):
            self.driver.clear_text(150)
        self.assertEqual(text[0], "")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.driver = IOS(lambda _: None, self.root)
        self.driver.udid = "device-1"
        self.driver.app = {"id": "com.example.target", "exe": "Runner", "name": "Target"}

    def test_crash_log_ignores_newer_same_executable_report_for_other_app(self):
        report_dir = self.root / "Library/Logs/DiagnosticReports"
        report_dir.mkdir(parents=True)
        target = report_dir / "Runner-target.ips"
        other = report_dir / "Runner-other.ips"
        other_sim = report_dir / "Runner-other-simulator.ips"
        target.write_text('{"bundleID":"com.example.target","procPath":"/CoreSimulator/Devices/device-1/data/Runner.app/Runner",'
                          '"exception":{"type":"target"}}')
        other.write_text('{"bundleID":"com.example.other","exception":{"type":"other"}}')
        other_sim.write_text('{"bundleID":"com.example.target","procPath":"/CoreSimulator/Devices/device-2/data/Runner.app/Runner",'
                             '"exception":{"type":"other simulator"}}')
        self.driver.launched_at = time.time()
        os.utime(target, (self.driver.launched_at + 1, self.driver.launched_at + 1))
        os.utime(other, (self.driver.launched_at + 2, self.driver.launched_at + 2))
        os.utime(other_sim, (self.driver.launched_at + 3, self.driver.launched_at + 3))

        with mock.patch("yepitran.ios.Path.home", return_value=self.root):
            result = self.driver.crash_log()

        self.assertIn("Runner-target.ips", result)
        self.assertIn('"type":"target"', result)
        self.assertFalse((self.root / "Runner-other.ips").exists())
        self.assertFalse((self.root / "Runner-other-simulator.ips").exists())

    def test_crash_log_rejects_only_recent_reports_from_other_bundle(self):
        report_dir = self.root / "Library/Logs/DiagnosticReports"
        report_dir.mkdir(parents=True)
        (report_dir / "Runner-other.ips").write_text(
            '{"bundleID":"com.example.target","procPath":"/CoreSimulator/Devices/device-2/data/Runner.app/Runner"}')
        with mock.patch("yepitran.ios.Path.home", return_value=self.root):
            self.assertIsNone(self.driver.crash_log())

    def test_teardown_attempts_simulator_shutdown_when_video_stop_fails(self):
        self.driver.stop_video = mock.Mock(side_effect=InfraError("recorder stuck"))
        with mock.patch.object(self.driver, "state", return_value="Booted"), \
                mock.patch("yepitran.ios.simctl") as simctl:
            with self.assertRaisesRegex(InfraError, "recorder stuck"):
                self.driver.teardown()

        simctl.assert_called_once_with("shutdown", "device-1", timeout=120, check=False, cleanup=True)

    def test_teardown_shuts_down_simulator_still_booting_after_cancelled_setup(self):
        self.driver.stop_video = mock.Mock()
        with mock.patch.object(self.driver, "state", return_value="Booting"), \
                mock.patch("yepitran.ios.simctl") as simctl:
            self.driver.teardown()

        simctl.assert_called_once_with("shutdown", "device-1", timeout=120, check=False, cleanup=True)

    def test_teardown_propagates_cleanup_mode_through_cancelled_state_lookup(self):
        self.driver.stop_video = mock.Mock()
        devices = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-1": [
            {"name": "yepitran iPhone", "udid": "device-1", "state": "Booting"}
        ]}}
        commands = []

        class FakePopen:
            pid = 54321
            returncode = 0

            def __init__(self, cmd, **kwargs):
                commands.append((cmd, kwargs))

            def communicate(self, input=None, timeout=None):
                return (json.dumps(devices) if "list" in commands[-1][0] else "", "")

        was_cancelled = proc.cancel.is_set()
        proc.cancel.set()
        try:
            with mock.patch("yepitran.proc.subprocess.Popen", FakePopen):
                self.driver.teardown()
        finally:
            proc.cancel.clear()
            if was_cancelled:
                proc.cancel.set()

        self.assertEqual(len(commands), 2)
        self.assertIn("list", commands[0][0])
        self.assertIn("shutdown", commands[1][0])

    def test_nonzero_launchctl_result_does_not_prove_app_is_dead(self):
        failed = SimpleNamespace(returncode=1, stdout="launchctl unavailable", stderr="")
        with mock.patch("yepitran.ios.simctl", return_value=failed):
            self.assertEqual(self.driver.app_state(), "unknown")

    def test_installer_error_classifier_does_not_call_infra_invalidity_an_app_failure(self):
        self.assertFalse(IOS._is_app_install_error("MIInstallerErrorDomain: invalid device state"))
        self.assertFalse(IOS._is_app_install_error("simctl: invalid argument"))
        self.assertTrue(IOS._is_app_install_error("invalid application bundle"))

    def test_install_rejection_for_invalid_bundle_is_attributed_to_app(self):
        artifact = self.root / "Runner.app"
        self.driver.app = {"id": "com.example.target", "exe": "Runner", "name": "Target"}
        scenario = SimpleNamespace(app=None, permissions="allow")

        def fake_simctl(*args, **kwargs):
            if args[0] == "install":
                raise InfraError("app install: exit 1: invalid application bundle")
            return SimpleNamespace(stdout="")

        with mock.patch("yepitran.ios.app_info", return_value=self.driver.app), \
                mock.patch.object(self.driver, "ensure_sim", return_value="device-1"), \
                mock.patch.object(self.driver, "state", return_value="Shutdown"), \
                mock.patch.object(self.driver, "configure"), \
                mock.patch("yepitran.ios.wait_until", return_value=[Node("", "", "", "", 0, 0, 402, 874)]), \
                mock.patch("yepitran.ios.simctl", side_effect=fake_simctl), \
                mock.patch("yepitran.ios.sleep"):
            with self.assertRaises(AppError):
                self.driver.prepare(artifact, scenario, level=1)

    def test_invalid_device_state_during_install_remains_infrastructure_failure(self):
        artifact = self.root / "Runner.app"
        self.driver.app = {"id": "com.example.target", "exe": "Runner", "name": "Target"}
        scenario = SimpleNamespace(app=None, permissions="allow")

        def fake_simctl(*args, **kwargs):
            if args[0] == "install":
                raise InfraError("app install: exit 1: invalid device state")
            return SimpleNamespace(stdout="")

        with mock.patch("yepitran.ios.app_info", return_value=self.driver.app), \
                mock.patch.object(self.driver, "ensure_sim", return_value="device-1"), \
                mock.patch.object(self.driver, "state", return_value="Shutdown"), \
                mock.patch.object(self.driver, "configure"), \
                mock.patch("yepitran.ios.wait_until", return_value=[Node("", "", "", "", 0, 0, 402, 874)]), \
                mock.patch("yepitran.ios.simctl", side_effect=fake_simctl), \
                mock.patch("yepitran.ios.sleep"):
            with self.assertRaises(InfraError):
                self.driver.prepare(artifact, scenario, level=1)

    def test_screenshot_does_not_accept_stale_valid_file_after_simctl_failure(self):
        screenshot = self.root / "screen.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20 + b"IEND\xaeB`\x82")
        failed = SimpleNamespace(returncode=1)
        with mock.patch("yepitran.ios.simctl", return_value=failed), \
                mock.patch.object(self.driver, "check_health"):
            with self.assertRaisesRegex(InfraError, "came back empty or truncated"):
                self.driver.screenshot(screenshot)

        self.assertFalse(screenshot.exists())


if __name__ == "__main__":
    unittest.main()
