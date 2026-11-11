import contextlib
import ast
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yepitran import doctor, simlock
from yepitran.proc import Cancelled, InfraError
from yepitran.runner import INFRA_FAIL, LegRunner
from yepitran.scenario import parse


class FleetDoctorIntegrationTests(unittest.TestCase):
    def run_fleet_doctor(self, config, call=None):
        rows = []
        add = lambda status, check, detail, todo="": rows.append((status, check, detail, todo))
        with mock.patch("yepitran.doctor.simlock.config", return_value=config), \
                mock.patch("yepitran.doctor.simlock.Client.call", side_effect=call):
            doctor.fleet_android(add)
        return rows

    def test_missing_gateway_config_reports_failure_instead_of_type_error(self):
        rows = self.run_fleet_doctor(None)
        self.assertEqual(rows[0][0:3], (doctor.FAIL, "simlock gateway", "not configured"))

    def test_gateway_down_and_bad_token_are_reported_as_gateway_failures(self):
        for error in (InfraError("gateway unreachable"), simlock.Error(401, "UNAUTHORIZED", "bad token")):
            with self.subTest(error=error):
                calls = mock.Mock(side_effect=error)
                rows = self.run_fleet_doctor({"url": "https://gateway", "token": "test"}, calls)
                self.assertTrue(any(row[0:2] == (doctor.FAIL, "simlock gateway") for row in rows))

    def test_config_read_infra_error_is_reported_as_gateway_failure(self):
        rows = []
        with mock.patch("yepitran.doctor.simlock.config", side_effect=InfraError("invalid Simlock config")):
            doctor.fleet_android(lambda *row: rows.append(row))
        self.assertTrue(any(row[0:2] == (doctor.FAIL, "simlock gateway") for row in rows))

    def test_backend_config_parse_error_is_reported_without_running_local_checks(self):
        for error in (ValueError("invalid JSON"), AttributeError("config is not an object")):
            with self.subTest(error=error):
                rows = []
                with mock.patch("yepitran.doctor.android.backend", side_effect=error):
                    self.assertIsNone(doctor.android_backend(lambda *row: rows.append(row)))
                self.assertTrue(any(row[0:2] == (doctor.FAIL, "simlock gateway") for row in rows))

    def test_catalog_requires_an_explicit_android_platform_entry(self):
        catalog = {"platforms": [{"platform": "android", "modelRuntimes": {"Pixel 8": ["35"]}}]}
        calls = mock.Mock(side_effect=[{"name": "agent", "role": "runner"}, catalog, {"leases": []}])
        rows = self.run_fleet_doctor({"url": "https://gateway", "token": "test"}, calls)
        self.assertTrue(any(row[0:2] == (doctor.OK, "simlock Android device") for row in rows))

    def test_catalog_does_not_misread_an_unidentified_single_platform_as_android(self):
        catalog = {"platforms": [{"platform": "ios", "modelRuntimes": {"Pixel 8": ["35"]}}]}
        calls = mock.Mock(side_effect=[{"name": "agent", "role": "runner"}, catalog])
        rows = self.run_fleet_doctor({"url": "https://gateway", "token": "test"}, calls)
        self.assertTrue(any(row[0:2] == (doctor.FAIL, "simlock gateway") for row in rows))

    def test_non_object_identity_and_non_list_leases_report_gateway_failure(self):
        cases = (
            ([['not an object']], "identity"),
            ([{"name": "agent", "role": "runner"},
             {"platforms": [{"platform": "android", "modelRuntimes": {}}]}, {"leases": {}}], "leases"),
        )
        for response_sequence, label in cases:
            with self.subTest(label=label):
                calls = mock.Mock(side_effect=response_sequence)
                rows = self.run_fleet_doctor({"url": "https://gateway", "token": "test"}, calls)
                self.assertTrue(any(row[0:2] == (doctor.FAIL, "simlock gateway") for row in rows))

    def test_malformed_catalog_reports_infra_failure_without_index_or_attribute_crash(self):
        for catalog in (
            None,
            {"platforms": []},
            {"platforms": None},
            {"platforms": [{"platform": "ios", "modelRuntimes": {}}]},
            {"platforms": [{"platform": "android", "modelRuntimes": None}]},
            {"platforms": [{"platform": "android", "modelRuntimes": {"Pixel 8": "35"}}]},
            {"platforms": [{"platform": "android", "modelRuntimes": {"Pixel 8": [35]}}]},
        ):
            with self.subTest(catalog=catalog):
                calls = mock.Mock(side_effect=[{"name": "agent", "role": "runner"}, catalog])
                rows = self.run_fleet_doctor({"url": "https://gateway", "token": "test"}, calls)
                self.assertTrue(any(row[0] == doctor.FAIL and row[1] == "simlock gateway" for row in rows))


class RunnerConstructionIntegrationTests(unittest.TestCase):
    def test_driver_construction_failure_closes_open_logfile(self):
        handle = mock.mock_open()()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("builtins.open", return_value=handle), \
                mock.patch("yepitran.runner.make_driver", side_effect=RuntimeError("driver setup failed")):
            with self.assertRaisesRegex(RuntimeError, "driver setup failed"):
                LegRunner("ios", Path("app.app"), parse("launch\n"), Path(temp) / "out", "review")
        handle.close.assert_called_once_with()

    def test_driver_infrastructure_error_becomes_infra_leg(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("yepitran.runner.make_driver", side_effect=InfraError("simlock is not configured")):
            runner = LegRunner("android", Path("app.apk"), parse("launch\n"), Path(temp) / "out", "review")
            leg = runner.run()
        self.assertEqual(leg.verdict, INFRA_FAIL)
        self.assertIn("simlock is not configured", leg.reason)
        self.assertTrue(runner.logfile.closed)

    def test_cancel_during_prepare_tears_down_releases_lease_and_closes_log(self):
        with tempfile.TemporaryDirectory() as temp:
            driver = mock.Mock()
            driver.markers.return_value = []
            driver.prepare.side_effect = Cancelled()
            lease = mock.Mock()
            with mock.patch("yepitran.runner.make_driver", return_value=driver), \
                    mock.patch("yepitran.runner.Lease", return_value=lease), \
                    mock.patch("yepitran.proc.reap", return_value=[]):
                runner = LegRunner("ios", Path("app.app"), parse("launch\n"), Path(temp) / "out", "review")
                with contextlib.redirect_stderr(io.StringIO()):
                    leg = runner.run()
            self.assertEqual(leg.verdict, INFRA_FAIL)
            self.assertIn("interrupted", leg.reason)
            driver.teardown.assert_called_once_with()
            lease.release.assert_called_once_with()
            self.assertTrue(runner.logfile.closed)


class ChaosCaseGuardTests(unittest.TestCase):
    def test_backend_case_guard_rejects_dangerous_or_unknown_cases(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "chaos.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and
                        node.name == "unsupported_cases")
        isolated = ast.Module(body=[function], type_ignores=[])
        namespace = {}
        exec(compile(isolated, str(source), "exec"), namespace)
        unsupported = namespace["unsupported_cases"]
        self.assertEqual(unsupported(["kill-adb", "lose-lease"], True), ["kill-adb"])
        self.assertEqual(unsupported(["lose-lease"], False), ["lose-lease"])
        self.assertEqual(unsupported(["typo"], False), ["typo"])
        self.assertEqual(unsupported(["kill-sim", "parallel"], True), [])

    def test_guard_call_and_exit_precede_run_class_definition(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "chaos.py"
        tree = ast.parse(source.read_text())
        check_index = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign) and
                           any(isinstance(target, ast.Name) and target.id == "unsupported" for target in node.targets))
        check = tree.body[check_index]
        self.assertIsInstance(check.value, ast.Call)
        self.assertIsInstance(check.value.func, ast.Name)
        self.assertEqual(check.value.func.id, "unsupported_cases")
        self.assertEqual([arg.id for arg in check.value.args if isinstance(arg, ast.Name)], ["CASES", "FLEET"])
        guard = tree.body[check_index + 1]
        self.assertIsInstance(guard, ast.If)
        self.assertIsInstance(guard.test, ast.Name)
        self.assertEqual(guard.test.id, "unsupported")
        self.assertTrue(any(isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and
                            isinstance(node.exc.func, ast.Name) and node.exc.func.id == "SystemExit"
                            for node in guard.body))
        run_index = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.ClassDef) and node.name == "Run")
        self.assertLess(check_index, run_index)
        self.assertLess(check_index + 1, run_index)


if __name__ == "__main__":
    unittest.main()
