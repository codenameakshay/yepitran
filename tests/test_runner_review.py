import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yepitran.proc import AppError, Cancelled, InfraError
from yepitran.runner import APP_FAIL, INFRA_FAIL, PASS, Attempt, LegRunner
from yepitran.scenario import parse
from yepitran.ui import Node


class FakeDriver:
    platform = "ios"
    app = {"id": "com.example", "name": "Example"}
    screen = (402.0, 874.0)

    def __init__(self, log, workdir):
        self.log, self.workdir = log, workdir
        self.state = "foreground"
        self.nodes = [
            Node("Example", "", "", "Application", 0, 0, 402, 874, path=(0,)),
            Node("Home", "home", "", "Button", 0, 100, 200, 50, path=(0, 0)),
        ]
        self.prepare_errors = []
        self.workdirs = []
        self.taps = []
        self.preserve_state = False

    def prepare(self, artifact, scenario, level):
        self.workdirs.append(self.workdir)
        if not self.preserve_state:
            self.state = "foreground"
        if self.prepare_errors:
            error = self.prepare_errors.pop(0)
            if error:
                raise error

    def launch(self):
        pass

    def tree(self):
        return self.nodes

    def tap(self, x, y):
        self.taps.append((x, y))
        if getattr(self, "die_on_tap", False):
            self.state = "dead"

    def type_text(self, text):
        pass

    def clear_text(self):
        pass

    def swipe(self, *args):
        pass

    def openurl(self, url):
        pass

    def screenshot(self, path):
        pass

    def start_video(self, path):
        if getattr(self, "video_error", None):
            raise self.video_error
        pass

    def stop_video(self):
        pass

    def app_state(self):
        return self.state

    def crash_log(self):
        return None

    def check_health(self):
        pass

    def teardown(self):
        pass

    def markers(self):
        return []


class RunnerReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.driver = FakeDriver(None, None)
        self.patches = [
            mock.patch("yepitran.lease.HOME", Path(self.tmp.name) / "lease-home"),
            mock.patch("yepitran.runner.load_factor", lambda: 1.0),
            mock.patch("yepitran.proc.load_factor", lambda: 1.0),
            mock.patch("yepitran.runner.make_driver", lambda platform, log, workdir: self.driver),
            mock.patch("yepitran.proc.reap", lambda markers: []),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def runner(self, source):
        runner = LegRunner("ios", Path("app.app"), parse(source), Path(self.tmp.name) / "out", "review", 0.1)
        self.addCleanup(runner.logfile.close)
        return runner

    def test_platform_filtered_empty_scenario_cannot_pass(self):
        runner = self.runner("ios: launch\n")
        runner.platform = "android"
        runner.steps = []
        leg = runner.run()
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertIn("no steps for android", leg.reason)
        self.assertEqual(leg.attempts, [])

    def test_app_failure_without_two_healthy_app_failures_is_infra(self):
        runner = self.runner("launch\n")
        outcomes = [
            Attempt(1, "app", "missing expected element"),
            Attempt(2, "infra", "emulator disconnected"),
            Attempt(3, "infra", "emulator disconnected"),
        ]
        with mock.patch.object(runner, "attempt", side_effect=outcomes):
            runner.attempts()
        self.assertEqual(runner.leg.verdict, INFRA_FAIL)
        self.assertIn("could not confirm", runner.leg.reason)
        self.assertNotEqual(runner.leg.verdict, APP_FAIL)

    def test_unknown_app_state_cannot_turn_stale_tree_miss_into_app_failure(self):
        runner = self.runner("launch\n")
        self.driver.state = "unknown"
        with self.assertRaises(InfraError):
            runner.diagnose(self.driver.nodes, "waited for \"Settings\"")

    def test_foreign_app_selector_hit_is_infrastructure_not_pass(self):
        self.driver.nodes.append(Node("Settings", "", "", "Button", 10, 200, 100, 50, path=(0, 1)))
        self.driver.state = "background (in front: Settings)"
        self.driver.preserve_state = True
        leg = self.runner('assert visible "Settings"\n').run()
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertNotEqual(leg.verdict, PASS)
        self.assertIn("background", leg.attempts[0].error)

    def test_app_crash_on_last_step_is_not_a_pass(self):
        self.driver.die_on_tap = True
        leg = self.runner('tap "Home"\n').run()
        self.assertEqual(leg.verdict, APP_FAIL)
        self.assertIn("crashed or quit", leg.attempts[0].error)

    def test_gone_requires_readable_app_content(self):
        self.driver.nodes = self.driver.nodes[:1]
        leg = self.runner('assert gone "Spinner"\n').run()
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertIn("nothing from the app", leg.attempts[0].error)

    def test_android_system_nodes_cannot_satisfy_app_selector(self):
        runner = self.runner('assert visible "Settings" 0ms\n')
        runner.platform = "android"
        self.driver.nodes = [
            Node("Home", "", "", "Button", 0, 100, 200, 50, pkg="com.example"),
            Node("Settings", "", "", "Button", 0, 200, 200, 50, pkg="com.android.settings"),
        ]
        with mock.patch.object(runner, "budget", return_value=0):
            leg = runner.run()
        self.assertEqual(leg.verdict, APP_FAIL)
        self.assertIn("never appeared", leg.attempts[0].error)

    def test_explicit_zero_timeout_is_not_replaced_by_default(self):
        runner = self.runner('wait "Home" 0ms\n')
        self.assertEqual(runner.budget(runner.steps[0].timeout), 0)

    def test_unknown_exception_in_step_is_classified_as_infrastructure(self):
        runner = self.runner("launch\n")
        with mock.patch.object(runner, "step", side_effect=RuntimeError("broken adapter")):
            leg = runner.run()
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertIn("unexpected RuntimeError", leg.attempts[0].error)

    def test_cancellation_during_step_is_not_swallowed_by_video_cleanup(self):
        runner = self.runner("launch\n")
        with mock.patch.object(runner, "step", side_effect=Cancelled()):
            leg = runner.run()
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertIn("interrupted", leg.reason)

    def test_workdir_is_refreshed_before_every_attempt(self):
        self.driver.prepare_errors = [InfraError("retry"), InfraError("retry"), None]
        leg = self.runner("launch\n").run()
        self.assertEqual(leg.verdict, PASS)
        self.assertEqual(len(set(self.driver.workdirs)), 3)
        self.assertTrue(all(p.name == f"attempt-{i}" for i, p in enumerate(self.driver.workdirs, 1)))

    def test_stale_video_from_reused_output_is_not_attached_after_start_failure(self):
        runner = self.runner("launch\n")
        attempt_dir = runner.root / "attempt-1"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "video.mp4").write_bytes(b"old recording")
        self.driver.video_error = InfraError("recorder unavailable")
        leg = runner.run()
        self.assertEqual(leg.verdict, PASS)
        self.assertEqual(leg.attempts[0].video, "")

    def test_id_only_ios_control_counts_as_app_content(self):
        runner = self.runner("launch\n")
        runner.d.nodes = [
            Node("", "", "", "Application", 0, 0, 402, 874, path=(0,)),
            Node("", "continue", "", "Button", 10, 100, 100, 50, path=(0, 0)),
        ]
        self.assertEqual(runner.app_nodes(runner.d.nodes), [runner.d.nodes[1]])

    def test_teardown_and_logging_errors_cannot_skip_lease_release(self):
        runner = self.runner("launch\n")
        released = []
        lease = mock.Mock()
        lease.acquire.return_value = None
        lease.release.side_effect = lambda: released.append(True)
        runner.d.teardown = mock.Mock(side_effect=RuntimeError("teardown failed"))
        with mock.patch("yepitran.runner.Lease", return_value=lease), \
                mock.patch.object(runner, "log", side_effect=OSError("disk full")):
            leg = runner.run()
        self.assertEqual(released, [True])
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertTrue(runner.logfile.closed)

    def test_verdict_tests_isolate_home_even_if_lease_was_imported_first(self):
        from yepitran.lease import Lease
        from tests.test_verdict import LegVerdictTests

        with mock.patch("yepitran.lease.HOME", Path(self.tmp.name) / "imported-before-test"):
            case = LegVerdictTests()
            case.setUp()
            try:
                self.assertEqual(Lease("ios", "probe").path,
                                 case.outdir / "yepitran-home/locks/ios.lock")
            finally:
                case.tearDown()
                case.doCleanups()


if __name__ == "__main__":
    unittest.main()
