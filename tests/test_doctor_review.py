import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yepitran import android, doctor
from yepitran.lease import Lease


class DoctorLeaseTests(unittest.TestCase):
    def test_local_android_reports_missing_avdmanager_and_lsof(self):
        rows = []
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(android, "SDK", Path(temp) / "sdk"), \
                mock.patch.object(android, "IMAGE_DIR", Path(temp) / "image"), \
                mock.patch.object(android, "AVD_DIR", Path(temp) / "avd"), \
                mock.patch.object(Path, "home", return_value=Path(temp)), \
                mock.patch.object(doctor, "sh", return_value=(0, "version")), \
                mock.patch.object(doctor.shutil, "which", return_value=None):
            doctor.local_android(lambda *row: rows.append(row), False)
        failures = {row[1]: row for row in rows if row[0] == doctor.FAIL}
        self.assertIn("avdmanager", failures)
        self.assertIn("lsof", failures)
        self.assertIn("cmdline-tools/latest", failures["avdmanager"][3])
        self.assertIn("lsof", failures["lsof"][3])

    def test_linux_doctor_never_inspects_ios_devices(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(doctor, "MAC", False), \
                mock.patch.object(doctor, "ios_checks", side_effect=AssertionError("iOS check on Linux")), \
                mock.patch.object(doctor.ios.IOS, "markers", side_effect=AssertionError("simctl on Linux")), \
                mock.patch.object(doctor, "sh", side_effect=lambda *a, **kw: (1, "") if a[0] == "lsof" else (0, "tool version")) as sh, \
                mock.patch.object(doctor.shutil, "disk_usage", return_value=mock.Mock(free=20e9)), \
                mock.patch.object(doctor, "local_android"), \
                mock.patch.object(doctor, "android_backend", return_value="local"), \
                mock.patch.object(android, "build_tool", return_value="aapt2"), \
                mock.patch("yepitran.proc.processes", return_value=[]), \
                mock.patch("yepitran.lease.HOME", Path(temp)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.doctor(False), 0)
        self.assertFalse(any(c.args[0] in {"xcrun", "xcodebuild", "axe"} for c in sh.call_args_list))

    def test_device_creation_holds_platform_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sdk/system-images/android-36/google_apis/arm64-v8a").mkdir(parents=True)
            manager = root / "sdk/cmdline-tools/latest/bin/avdmanager"
            manager.parent.mkdir(parents=True)
            manager.touch()
            held = []

            def create_ios(driver):
                held.append(("ios", not Lease("ios", "observer").is_free()))
                return "fake-udid"

            def create_android(driver):
                held.append(("android", not Lease("android", "observer").is_free()))

            with mock.patch.object(doctor, "MAC", True), \
                    mock.patch("yepitran.lease.HOME", root / "home"), \
                    mock.patch("yepitran.doctor.sh", return_value=(0, "1.8.0")), \
                    mock.patch("yepitran.ios.pick_device_type", return_value=("iPhone", "runtime")), \
                    mock.patch("yepitran.ios.find_sim", return_value=None), \
                    mock.patch("yepitran.ios.IOS.ensure_sim", create_ios), \
                    mock.patch.object(android, "SDK", root / "sdk"), \
                    mock.patch.object(android, "IMAGE_DIR", root / "sdk/system-images/android-36/google_apis/arm64-v8a"), \
                    mock.patch.object(android, "AVD_DIR", root / "avd"), \
                    mock.patch.object(android, "build_tool", return_value="aapt2"), \
                    mock.patch.object(android.Android, "ensure_avd", create_android), \
                    mock.patch("yepitran.proc.processes", return_value=[]), \
                    contextlib.redirect_stdout(io.StringIO()):
                doctor.doctor(True)
                self.assertTrue(Lease("ios", "observer").is_free())
                self.assertTrue(Lease("android", "observer").is_free())

            self.assertEqual(held, [("ios", True), ("android", True)])
