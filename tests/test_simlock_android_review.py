from __future__ import annotations

import base64
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from yepitran import android, proc, simlock


class SimlockAndroidReviewTests(unittest.TestCase):
    def test_configure_stops_when_the_lease_ends(self):
        self.driver.lease_id = "L"
        logs = []
        self.driver.log = logs.append
        self.driver.client.exec.side_effect = simlock.Error(404, "UNKNOWN_LEASE", "ended")
        with self.assertRaisesRegex(proc.InfraError, "ended the lease"):
            self.driver.configure()
        self.assertEqual(self.driver.lost, "simlock ended the lease (expired or taken back)")

    def test_configure_runs_setup_through_the_gateway(self):
        self.driver.lease_id = "L"
        logs = []
        self.driver.log = logs.append
        self.driver.client.exec.return_value = (1, "", "unsupported setting")
        # The adb transport still answers, even if an optional setting is unsupported.
        self.driver.client.exec.side_effect = lambda lease, tool, args, **kw: (
            (0, "device\n", "") if args == ["get-state"] else (1, "", "unsupported setting"))
        self.driver.configure()
        self.assertTrue(any("unsupported setting" in line for line in logs), logs)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.driver = android.SimlockAndroid(lambda _: None, self.root, cfg={"url": "https://fleet", "token": "t"})
        self.driver.client.my_leases = Mock(return_value=[])
        self.driver.client.request = Mock(side_effect=AssertionError("unmocked Simlock request"))
        self.driver.client.call = Mock(side_effect=AssertionError("unmocked Simlock call"))
        self.driver.client.exec = Mock(side_effect=AssertionError("unmocked Simlock exec"))
        proc.cancel.clear()
        self.addCleanup(proc.cancel.clear)

    def test_missing_configuration_is_an_infrastructure_error(self):
        with patch.object(android.simlock, "config", return_value=None):
            with self.assertRaisesRegex(proc.InfraError, "no Simlock URL/token"):
                android.SimlockAndroid(lambda _: None, self.root)

    def test_failed_release_keeps_lease_id_for_a_later_cleanup_attempt(self):
        self.driver.lease_id = "L"
        self.driver.client.release = Mock(side_effect=proc.InfraError("gateway offline"))

        with self.assertRaisesRegex(proc.InfraError, "gateway offline"):
            self.driver.release_lease()

        self.assertEqual("L", self.driver.lease_id)

    def test_prepare_releases_prior_lease_even_when_app_loading_is_cancelled(self):
        self.driver.lease_id = "prior"
        self.driver.client.release = Mock()
        self.driver.load_app = Mock(side_effect=proc.Cancelled())
        scenario = Mock(reverse=[])

        with self.assertRaises(proc.Cancelled):
            self.driver.prepare(self.root / "app.apk", scenario, 0)

        self.driver.client.release.assert_called_once_with("prior")
        self.assertEqual("", self.driver.lease_id)

    def test_prepare_releases_grant_when_renewer_creation_fails(self):
        self.driver.client.lease = Mock(return_value={"id": "new", "address": "emulator-5586"})
        self.driver.client.release = Mock()
        scenario = Mock(reverse=[], permissions="allow")
        with patch.object(self.driver, "load_app"), \
             patch.object(android.simlock, "Renewer", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                self.driver.prepare(self.root / "app.apk", scenario, 0)

        self.driver.client.release.assert_called_once_with("new")
        self.assertEqual("", self.driver.lease_id)

    def test_prepare_releases_grant_when_configuration_fails(self):
        self.driver.client.lease = Mock(return_value={"id": "new", "address": "emulator-5586"})
        self.driver.client.release = Mock()
        scenario = Mock(reverse=[], permissions="allow")
        with patch.object(self.driver, "load_app"), \
             patch.object(android.simlock, "Renewer"), \
             patch.object(self.driver, "wait_runtime"), \
             patch.object(self.driver, "configure", side_effect=proc.InfraError("setup failed")):
            with self.assertRaisesRegex(proc.InfraError, "setup failed"):
                self.driver.prepare(self.root / "app.apk", scenario, 0)

        self.driver.client.release.assert_called_once_with("new")
        self.assertEqual("", self.driver.lease_id)

    def test_upload_cancellation_exits_without_waiting_for_retry_backoff(self):
        local = self.root / "app.apk"
        local.write_bytes(b"apk data")
        self.driver.lease_id = "L"

        def cancel_during_exec(*args, **kwargs):
            proc.cancel.set()
            raise proc.Cancelled()

        self.driver.client.exec = Mock(side_effect=cancel_during_exec)
        started = time.monotonic()
        with self.assertRaises(proc.Cancelled):
            self.driver.push(local, "/data/local/tmp/app.apk")

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(1, self.driver.client.exec.call_count)

    def test_local_video_keeps_webm_segment_when_ffmpeg_is_unavailable(self):
        local = android.Android(lambda _: None, self.root)
        local.video_path = self.root / "video.mp4"
        segment = self.root / "video-00.webm"
        segment.write_bytes(b"webm segment")

        with patch.object(android.shutil, "which", return_value=None):
            local.join_segments([segment])

        self.assertEqual(b"webm segment", (self.root / "video.webm").read_bytes())
        self.assertTrue(segment.exists())


if __name__ == "__main__":
    unittest.main()
