import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yepitran import build, report
from yepitran.proc import AppError, InfraError
from yepitran.runner import INFRA_FAIL


class BuildReportReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "pubspec.yaml").write_text("name: fixture\n")

    @staticmethod
    def result(code=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def test_unknown_flutter_build_failure_is_infrastructure_not_app_blame(self):
        with mock.patch("yepitran.build.run", side_effect=[self.result(), self.result(
                1, stderr="Xcode build service unavailable\ntry again later")]):
            with self.assertRaises(InfraError):
                build.build(self.root, "ios", self.root / "build.log")

    def test_source_compiler_diagnostic_is_attributed_to_the_app(self):
        with mock.patch("yepitran.build.run", side_effect=[self.result(), self.result(
                1, stderr="lib/main.dart:12:3: Error: Undefined name 'missing'.")]):
            with self.assertRaises(AppError):
                build.build(self.root, "ios", self.root / "build.log")

    def test_pubspec_resolution_failure_is_attributed_to_the_app(self):
        with mock.patch("yepitran.build.run", return_value=self.result(
                1, stderr="Because every version of fixture depends on missing_pkg, version solving failed.")):
            with self.assertRaises(AppError):
                build.build(self.root, "ios", self.root / "build.log")

    def test_success_without_expected_artifact_is_infrastructure_failure(self):
        with mock.patch("yepitran.build.run", side_effect=[self.result(), self.result()]), \
                self.assertRaises(InfraError):
            build.build(self.root, "ios", self.root / "build.log")

    def test_empty_report_is_not_a_pass(self):
        outdir = self.root / "report"
        outdir.mkdir()

        verdict = report.write(outdir, "empty-scenario", [])

        self.assertEqual(verdict, INFRA_FAIL)
        self.assertEqual(json.loads((outdir / "verdict.json").read_text())["verdict"], INFRA_FAIL)


if __name__ == "__main__":
    unittest.main()
