import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yepitran import cli, pr, report
from yepitran.runner import Leg, PASS


class PRPostingTests(unittest.TestCase):
    def test_posting_requires_explicit_flag_at_cli_boundary(self):
        url = "https://github.com/example/mobile/pull/12"
        with mock.patch("yepitran.pr.prove_pr", return_value=0) as prove, \
                mock.patch("yepitran.cli.signal.signal"):
            self.assertEqual(cli.main(["pr", url]), 0)
            prove.assert_called_once_with(url, None, "both", False)
            prove.reset_mock()
            self.assertEqual(cli.main(["pr", url, "--post"]), 0)
            prove.assert_called_once_with(url, None, "both", True)

    def test_prove_pr_writes_draft_but_only_posts_when_requested(self):
        url = "https://github.com/example/mobile/pull/12"
        sha = "1234567890abcdef"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkout = root / "pr" / "example-mobile-12-12345678"
            (checkout / ".git").mkdir(parents=True)
            (checkout / "pubspec.yaml").write_text("name: mobile\n")
            out = root / "out"
            out.mkdir()
            report.write(out, "smoke", [Leg("ios", "app.app", verdict=PASS, reason="all steps passed")])
            info = SimpleNamespace(stdout=json.dumps({"headRefOid": sha, "body": "```yepitran\nlaunch\n```"}))
            commands = []

            def command(args, **kwargs):
                commands.append(args)
                if args[0] == "git":
                    return SimpleNamespace(stdout=sha if "rev-parse" in args else "")
                return info

            with mock.patch("yepitran.pr.HOME", root), mock.patch("yepitran.pr.run", side_effect=command), \
                    mock.patch("yepitran.pr.run_scenario", return_value=(PASS, out)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pr.prove_pr(url, None, "ios", False), 0)
                self.assertEqual(commands[0], ["gh", "pr", "view", url, "--json", "headRefOid,body,title"])
                self.assertEqual(len(commands), 3)
                self.assertEqual(sum(cmd[:3] == ["gh", "pr", "comment"] for cmd in commands), 0)
                draft = out / "pr-comment.md"
                self.assertIn("PR head `12345678`", draft.read_text())
                self.assertEqual(pr.prove_pr(url, None, "ios", True), 0)
                self.assertEqual(commands[-1], ["gh", "pr", "comment", url, "--body-file", str(draft)])
                self.assertEqual(sum(cmd[:3] == ["gh", "pr", "comment"] for cmd in commands), 1)

    def test_wrong_or_dirty_cached_checkout_is_not_proved_or_posted(self):
        url = "https://github.com/example/mobile/pull/12"
        sha = "1234567890abcdef"
        for head, status in (("12345678wronghead", ""), (sha, " M lib/main.dart")):
            with self.subTest(head=head, status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                checkout = root / "pr/example-mobile-12-12345678"
                (checkout / ".git").mkdir(parents=True)
                (checkout / "pubspec.yaml").write_text("name: mobile\n")
                out = root / "out"
                out.mkdir()
                report.write(out, "smoke", [Leg("ios", "app.app", verdict=PASS)])
                responses = [SimpleNamespace(stdout=json.dumps({"headRefOid": sha,
                             "body": "```yepitran\nlaunch\n```"})),
                             SimpleNamespace(stdout=head), SimpleNamespace(stdout=status)]
                with mock.patch("yepitran.pr.HOME", root), \
                        mock.patch("yepitran.pr.run", side_effect=responses) as command, \
                        mock.patch("yepitran.pr.run_scenario", return_value=(PASS, out)) as prove:
                    self.assertEqual(pr.prove_pr(url, None, "ios", True), 2)
                prove.assert_not_called()
                self.assertEqual([args.args[0][0] for args in command.call_args_list], ["gh", "git", "git"])
