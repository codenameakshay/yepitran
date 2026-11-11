import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yepitran import cli, pr, proc


class CLIReleaseTests(unittest.TestCase):
    def test_early_infrastructure_errors_do_not_exit_as_app_failures(self):
        for command, target in ((["doctor"], "yepitran.doctor.doctor"),
                                (["pr", "https://github.com/example/app/pull/1"], "yepitran.pr.prove_pr"),
                                (["run", "app.apk", "smoke.proof"], "yepitran.cli.run_scenario")):
            for error in (proc.InfraError("tool missing"), OSError("cannot write report")):
                with self.subTest(command=command, error=error), \
                        patch(target, side_effect=error), patch.object(cli.signal, "signal"), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(command), 2)

    def test_missing_explicit_pr_scenario_is_usage_error(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as exc:
            pr.scenario_from("", Path(temp), Path(temp) / "missing.proof")
        self.assertEqual(exc.exception.code, cli.USAGE)

    def test_parser_errors_use_documented_usage_exit(self):
        for args in ([], ["run"], ["run", "only-one-path"],
                     ["run", "app.apk", "smoke.proof", "--platform", "wrong"],
                     ["--not-a-flag"]):
            with self.subTest(args=args), patch.object(cli.signal, "signal"), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                cli.main(args)
            self.assertEqual(exc.exception.code, cli.USAGE)

    def test_bad_targets_use_documented_usage_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unknown = root / "app.bin"
            unknown.touch()
            ipa = root / "app.ipa"
            ipa.touch()
            apk = root / "app.apk"
            apk.touch()
            for target, platform in ((root / "missing", None), (unknown, None),
                                     (ipa, None), (apk, "ios")):
                with self.subTest(target=target, platform=platform), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    cli.run_scenario([str(target)], root / "smoke.proof", platform, root / "out", 0,
                                     scenario_text="launch\n")
                self.assertEqual(exc.exception.code, cli.USAGE)

    def test_pr_without_scenario_uses_usage_exit(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as exc:
            pr.scenario_from("", Path(temp), None)
        self.assertEqual(exc.exception.code, cli.USAGE)

    def test_version_prints_installed_package_version_without_device_calls(self):
        out = io.StringIO()
        with patch("importlib.metadata.version", return_value="0.1.0") as version, \
                patch.object(cli, "run_scenario", side_effect=AssertionError("device run")), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            cli.main(["--version"])
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(out.getvalue(), "yepitran 0.1.0\n")
        version.assert_called_once_with("yepitran")
