import os
from pathlib import Path
import subprocess
import textwrap
import unittest


class ActionReviewTests(unittest.TestCase):
    def cleanup_step(self, environment, free="4", df_code="0"):
        action = (Path(__file__).resolve().parents[1] / "action.yml").read_text()
        # Execute only the cleanup fragment, with every external command replaced
        # by a shell function. No package, disk, Docker or device command can run.
        fragment = action.split("        # The emulator, its image", 1)[1].split(
            "        # Hosted Linux runners have KVM", 1)[0]
        fragment = textwrap.dedent("        # The emulator, its image" + fragment)
        mocks = r'''
df() { echo DF >&2; echo "$FREE_GB"; return "$DF_CODE"; }
tail() { local value; IFS= read -r value; echo "$value"; }
tr() { local value; IFS= read -r value; echo "${value//[^0-9]/}"; }
sudo() { sudo_calls=$((sudo_calls + 1)); }
sudo_calls=0
'''
        return subprocess.run(
            ["/bin/bash", "-euo", "pipefail", "-c", mocks + fragment + '\necho "done $sudo_calls"'],
            env={**os.environ, "PATH": "", "RUNNER_ENVIRONMENT": environment,
                 "FREE_GB": free, "DF_CODE": df_code},
            capture_output=True, text=True, timeout=5)

    def test_self_hosted_does_not_need_gnu_df(self):
        result = self.cleanup_step("self-hosted", df_code="1")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done 0\n", ""))

    def test_hosted_cleanup_only_runs_when_disk_is_low(self):
        low = self.cleanup_step("github-hosted")
        plenty = self.cleanup_step("github-hosted", free="20")
        self.assertEqual(low.returncode, 0)
        self.assertEqual(low.stdout.splitlines()[-1], "done 2")
        self.assertEqual(plenty.returncode, 0)
        self.assertEqual(plenty.stdout, "done 0\n")
