import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# proc reads YEPITRAN_HOME at import; set it first so no test can take the real device lease.
os.environ["YEPITRAN_HOME"] = tempfile.mkdtemp()

from yepitran.proc import InfraError  # noqa: E402
from yepitran.runner import APP_FAIL, INFRA_FAIL, PASS, LegRunner  # noqa: E402
from yepitran.scenario import parse  # noqa: E402
from yepitran.ui import Node  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20 + b"IEND\xaeB`\x82"
HEADER = "timeout: 300ms\n"


class FakeDriver:
    platform = "ios"
    app = {"id": "com.x", "name": "X"}
    screen = (402.0, 874.0)

    def __init__(self, log, workdir):
        self.log = log
        self.workdir = workdir
        self.prepare_errors = []
        self.prepares = 0
        self.done_from = None
        self.state = "foreground"
        self.crash = None
        self.health_error = None
        self.taps = []

    def prepare(self, artifact, scenario, level):
        self.prepares += 1
        if self.prepare_errors:
            err = self.prepare_errors.pop(0)
            if err is not None:
                raise err

    def launch(self):
        pass

    def tree(self):
        nodes = [
            Node("X", "", "", "Application", 0, 0, 402, 874, "", (0,)),
            Node("Home", "", "", "Button", 0, 100, 402, 50, "", (0, 0)),
        ]
        if self.done_from is not None and self.prepares >= self.done_from:
            nodes.append(Node("Done", "", "", "Button", 0, 200, 402, 50, "", (0, 1)))
        return nodes

    def tap(self, x, y):
        self.taps.append((x, y))

    def type_text(self, text):
        pass

    def clear_text(self):
        pass

    def swipe(self, *args):
        pass

    def openurl(self, url):
        pass

    def screenshot(self, path):
        Path(path).write_bytes(PNG)

    def start_video(self, path):
        pass

    def stop_video(self):
        pass

    def app_state(self):
        return self.state

    def crash_log(self):
        return self.crash

    def check_health(self):
        if self.health_error is not None:
            raise self.health_error

    def teardown(self):
        pass

    def markers(self):
        return []


class LegVerdictTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver(log=None, workdir=None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.outdir = Path(tmp.name)
        self.patches = [
            mock.patch("yepitran.lease.HOME", self.outdir / "yepitran-home"),
            mock.patch("yepitran.runner.load_factor", lambda: 1.0),
            mock.patch("yepitran.proc.load_factor", lambda: 1.0),
            mock.patch("yepitran.runner.make_driver", lambda platform, log, workdir: self.driver),
            mock.patch("yepitran.proc.reap", lambda markers: []),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def run_scenario(self, text, run_id="t"):
        runner = LegRunner(platform="ios", artifact=Path("/tmp/x.app"), scenario=parse(HEADER + text),
                           outdir=self.outdir, run_id=run_id, lease_wait=5)
        return runner.run()

    def test_clean_run_passes_on_first_attempt_with_screenshot(self):
        leg = self.run_scenario('launch\ntap "Home"\nscreenshot s1\n')
        self.assertEqual(leg.verdict, PASS)
        self.assertEqual(len(leg.attempts), 1)
        self.assertTrue(leg.attempts[0].steps[2].screenshot)
        self.assertEqual(self.driver.taps, [(201.0, 125.0)])

    def test_missing_element_on_healthy_device_is_app_fail_after_rerun(self):
        leg = self.run_scenario('launch\nwait "Done"\n')
        self.assertEqual(leg.verdict, APP_FAIL)
        self.assertEqual(len(leg.attempts), 2)
        self.assertIn("reproduced", leg.reason)
        self.assertIn("On screen", leg.attempts[0].error)

    def test_setup_failures_are_retried_until_one_succeeds(self):
        self.driver.prepare_errors = [InfraError("boom"), InfraError("boom"), None]
        leg = self.run_scenario('launch\ntap "Home"\n')
        self.assertEqual(leg.verdict, PASS)
        self.assertEqual(len(leg.attempts), 3)

    def test_setup_failing_every_attempt_is_infra_fail(self):
        self.driver.prepare_errors = [InfraError("boom") for _ in range(3)]
        leg = self.run_scenario('launch\ntap "Home"\n')
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertEqual(len(leg.attempts), 3)
        self.assertIn("every attempt", leg.reason)

    def test_missing_element_on_unhealthy_device_is_infra(self):
        self.driver.health_error = InfraError("sim died")
        leg = self.run_scenario('wait "Done"\n')
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertEqual(leg.attempts[0].outcome, "infra")
        self.assertIn("sim died", leg.attempts[0].error)

    def test_dead_app_is_reported_as_crash_with_report(self):
        self.driver.state = "dead"
        self.driver.crash = "crash report r.ips"
        leg = self.run_scenario('launch\nwait "Done"\n')
        self.assertEqual(leg.verdict, APP_FAIL)
        self.assertIn("crashed", leg.attempts[0].error)
        self.assertIn("crash report r.ips", leg.attempts[0].error)

    def test_app_failure_that_does_not_reproduce_passes_with_flaky_note(self):
        self.driver.done_from = 2
        leg = self.run_scenario('launch\nwait "Done"\n')
        self.assertEqual(leg.verdict, PASS)
        self.assertEqual(len(leg.attempts), 2)
        self.assertIn("did not reproduce", leg.reason)

    def test_device_lease_is_released_after_a_run(self):
        first = self.run_scenario('launch\ntap "Home"\n', run_id="first")
        second = self.run_scenario('launch\ntap "Home"\n', run_id="second")
        self.assertEqual(first.verdict, PASS)
        self.assertEqual(second.verdict, PASS)


if __name__ == "__main__":
    unittest.main()
