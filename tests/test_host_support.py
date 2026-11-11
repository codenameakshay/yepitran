import contextlib
import io
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yepitran import android, build, cli, proc
from yepitran.runner import Leg, PASS


def host_config(system="linux", machine="x86_64", env=None):
    with patch.dict(os.environ, env or {}, clear=True), \
            patch("sys.platform", system), \
            patch("platform.machine", return_value=machine), \
            patch.object(Path, "home", return_value=Path("/test-home")):
        return runpy.run_path(str(Path(android.__file__)), run_name="yepitran._host_test")


class HostConfigTests(unittest.TestCase):
    def test_apk_abis_match_local_host_or_supported_translation(self):
        for host, apk_abi, accepted in (
                ("arm64-v8a", "arm64-v8a", True), ("arm64-v8a", "x86_64", False),
                ("arm64-v8a", "armeabi-v7a", False), ("arm64-v8a", None, True),
                ("x86_64", "x86_64", True), ("x86_64", "arm64-v8a", True),
                ("x86_64", "armeabi-v7a", False), ("x86_64", None, True)):
            badging = "package: name='example.app'\nlaunchable-activity: name='example.app.Main'\n"
            if apk_abi:
                badging += f"native-code: '{apk_abi}'\n"
            with self.subTest(host=host, apk=apk_abi), \
                    patch.object(android, "HOST_ABI", host), \
                    patch.object(android, "build_tool", return_value="aapt2"), \
                    patch.object(android, "run", return_value=SimpleNamespace(stdout=badging, stderr="")):
                if accepted:
                    self.assertEqual(android.apk_info(Path("app.apk"))["id"], "example.app")
                else:
                    with self.assertRaises(android.AppError):
                        android.apk_info(Path("app.apk"))

    def test_avd_creation_preserves_explicit_java_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            avd = root / "avd"
            image = root / "image"
            image.mkdir()
            bundled_java = root / "bundled-java"
            bundled_java.mkdir()

            def create(args, **kwargs):
                avd.mkdir()
                (avd / "config.ini").write_text("hw.keyboard=no\n")

            with patch.dict(os.environ, {"JAVA_HOME": "/chosen-jdk"}), \
                    patch.object(android, "JBR", str(bundled_java)), \
                    patch.object(android, "IMAGE_DIR", image), \
                    patch.object(android, "AVD_DIR", avd), \
                    patch.object(android, "SDK", root / "sdk"), \
                    patch.object(android, "run", side_effect=create) as run:
                android.Android(lambda _: None, root).ensure_avd()
            self.assertNotIn("JAVA_HOME", run.call_args.kwargs["env"])
            # The AVD must land in AVD_DIR on every host, not the tool's own default.
            self.assertEqual(run.call_args.kwargs["env"]["ANDROID_AVD_HOME"], str(avd.parent))
            self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("-p") + 1], str(avd))
            self.assertEqual(run.call_args.args[0][0], str(root / "sdk/cmdline-tools/latest/bin/avdmanager"))
            self.assertIn(android.IMAGE, run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["input"], "no\n")
            self.assertIn("hw.keyboard=yes", (avd / "config.ini").read_text())

    def test_process_inventory_keeps_device_markers_after_long_paths(self):
        command = "/sdk/" + "long-path/" * 12 + "/qemu-system-x86_64 -avd " + android.AVD

        def ps(args, **kwargs):
            return SimpleNamespace(stdout="123 " + (command if "-ww" in args else command[:80]) + "\n")

        with patch.object(proc.subprocess, "run", side_effect=ps):
            rows = proc.processes()
        self.assertEqual(rows, [(123, command)])
        argv = rows[0][1].split()
        self.assertTrue(proc._marker_matches(argv, f"-avd {android.AVD}", "qemu-system-x86_64"))

    def test_adb_listener_identity_survives_long_sdk_paths(self):
        adb = "/sdk/" + "long-path/" * 12 + "platform-tools/adb"
        command = adb + " -L tcp:localhost:5137 fork-server server --reply-fd 4"

        def run(args, **kwargs):
            return SimpleNamespace(stdout="123\n" if args[0] == "lsof" else
                                   command if "-ww" in args else command[:80])

        with patch.object(android, "ADB", adb), patch.object(android, "run", side_effect=run):
            driver = android.Android(lambda _: None, Path("unused"))
            pid, identity = driver.adb_listener()
            self.assertEqual(pid, 123)
            self.assertTrue(driver.is_adb_server(identity))

    def test_sdk_image_and_gpu_defaults_follow_host(self):
        for system, machine, sdk, abi, gpu in (
                ("linux", "x86_64", "Android/Sdk", "x86_64", "swiftshader_indirect"),
                ("linux", "aarch64", "Android/Sdk", "arm64-v8a", "swiftshader_indirect"),
                ("darwin", "arm64", "Library/Android/sdk", "arm64-v8a", "host"),
                ("darwin", "x86_64", "Library/Android/sdk", "x86_64", "host")):
            with self.subTest(system=system, machine=machine):
                cfg = host_config(system, machine)
                self.assertEqual(cfg["SDK"], Path("/test-home") / sdk)
                self.assertEqual(cfg["IMAGE"], f"system-images;android-36;google_apis;{abi}")
                self.assertEqual(cfg["IMAGE_DIR"], cfg["SDK"] / "system-images/android-36/google_apis" / abi)
                self.assertEqual(cfg["GPU"], gpu)

    def test_sdk_and_gpu_overrides(self):
        cfg = host_config(env={"ANDROID_HOME": "/sdk", "ANDROID_SDK_ROOT": "/old-sdk",
                               "YEPITRAN_EMULATOR_GPU": "software"})
        self.assertEqual(cfg["SDK"], Path("/sdk"))
        self.assertEqual(cfg["GPU"], "software")
        self.assertEqual(host_config(env={"ANDROID_SDK_ROOT": "/fallback"})["SDK"], Path("/fallback"))

    def test_avd_location_follows_android_environment(self):
        for env, root in (({}, "/test-home/.android/avd"),
                          ({"ANDROID_USER_HOME": "/android-user"}, "/android-user/avd"),
                          ({"ANDROID_USER_HOME": "/android-user", "ANDROID_AVD_HOME": "/avds"}, "/avds")):
            with self.subTest(env=env):
                cfg = host_config(env=env)
                self.assertEqual(cfg["AVD_DIR"], Path(root) / f"{cfg['AVD']}.avd")

    def test_flutter_android_build_includes_both_host_abis(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "pubspec.yaml").write_text("name: fixture\n")
            artifact = project / build.OUTPUTS["android"]
            artifact.parent.mkdir(parents=True)
            artifact.touch()
            with patch.object(build, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run, \
                    patch.object(build, "flutter_cmd", return_value=["flutter"]):
                self.assertEqual(build.build(project, "android", project / "build.log"), artifact)
            self.assertEqual(run.call_args_list[1].args[0],
                             ["flutter", "build", "apk", "--profile", "--target-platform", "android-arm64,android-x64"])

    def test_linux_flutter_default_only_builds_and_runs_android(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "pubspec.yaml").write_text("name: fixture\n")
            runner = Mock()
            runner.platform = "android"
            runner.run.return_value = Leg("android", "app.apk", verdict=PASS)
            with patch.object(cli.sys, "platform", "linux"), \
                    patch.object(cli.build, "build", return_value=root / "app.apk") as build_app, \
                    patch.object(cli, "LegRunner", return_value=runner) as make_runner, \
                    patch.object(proc.cancel, "is_set", return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()):
                verdict, _ = cli.run_scenario([str(project)], Path("smoke.proof"), None,
                                              root / "out", 0, scenario_text="launch\n")
            self.assertEqual(verdict, PASS)
            self.assertEqual(build_app.call_args.args[1], "android")
            self.assertEqual(make_runner.call_count, 1)
            self.assertEqual(make_runner.call_args.args[0], "android")


class ConfigureTests(unittest.TestCase):
    def test_failed_setup_is_logged_but_a_healthy_local_device_continues(self):
        logs = []
        d = android.Android(logs.append, Path("/tmp"))
        d.serial = "emulator-5700"
        d.emu = Mock()
        d.emu.poll.return_value = None

        def adb_run(args, **kwargs):
            if "get-state" in args:
                return subprocess.CompletedProcess(args, 0, "device\n", "")
            return subprocess.CompletedProcess(args, 1, "", "unsupported setting")

        with patch.object(android, "run", side_effect=adb_run):
            d.configure()
        self.assertTrue(any("unsupported setting" in line for line in logs), logs)

    def test_failed_setup_cannot_hide_a_dead_local_transport(self):
        d = android.Android(lambda _: None, Path("/tmp"))
        d.emu = Mock()
        d.emu.poll.return_value = None
        with patch.object(android, "run", return_value=subprocess.CompletedProcess([], 1, "", "device offline")):
            with self.assertRaisesRegex(proc.InfraError, "device setup.*device offline"):
                d.configure()

    def test_a_hanging_setup_command_is_skipped_and_the_rest_still_run(self):
        logs, sent = [], []

        def adb_raw(*args, **kwargs):
            if args[0] == "get-state":
                return subprocess.CompletedProcess(args, 0, "device\n", "")
            sent.append(args[1])
            if args[1].startswith("cmd bluetooth_manager"):
                raise android.InfraError("adb shell: no answer after 30s")
            return subprocess.CompletedProcess(args, 0, "", "")

        d = android.Android(logs.append, Path("/tmp"))
        with patch.object(d, "adb_raw", side_effect=adb_raw), patch.object(d, "check_alive"):
            d.configure()
        self.assertEqual(list(android.Android.SETUP), sent)
        self.assertTrue(any("cmd bluetooth_manager disable" in line for line in logs))
