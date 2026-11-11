"""Turn a Flutter project into the artifacts yepitran installs."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .proc import AppError, InfraError, run, tail

OUTPUTS = {"ios": "build/ios/iphonesimulator/Runner.app",
           "android": "build/app/outputs/flutter-apk/app-profile.apk"}
# Debug APKs ANR under `uiautomator dump` (JIT + CheckJNI on the main thread); profile does not.
COMMANDS = {"ios": ["build", "ios", "--simulator", "--debug"],
            "android": ["build", "apk", "--profile", "--target-platform", "android-arm64,android-x64"]}  # arm hosts, x86 CI and fleets

APP_FAILURE_MARKERS = ("version solving failed", "pubspec.yaml has an error",
                       "because every version of", "because no versions of")
SOURCE_DIAGNOSTIC = re.compile(
    r"(?im)^.*\.(?:dart|kt|kts|java|swift|m|mm|h):\d+(?::\d+)?:\s*(?:error\b|Error:)"
)


def is_flutter_project(p: Path) -> bool:
    return p.is_dir() and (p / "pubspec.yaml").exists()


def app_dir(project: Path) -> Path:
    """A plugin repo is proven through its example app."""
    pubspec = (project / "pubspec.yaml").read_text()
    if "\n  plugin:" in pubspec and (project / "example/pubspec.yaml").exists():
        return project / "example"
    return project


def flutter_cmd(project: Path) -> list[str]:
    for d in [project, *project.parents]:
        if (d / ".fvmrc").exists() and shutil.which("fvm"):
            return ["fvm", "flutter"]
    return ["flutter"]


def build(project: Path, platform: str, log: Path) -> Path:
    app = app_dir(project)
    flutter = flutter_cmd(app)
    with open(log, "a") as fh:
        for args in (["pub", "get"], COMMANDS[platform]):
            p = run([*flutter, *args], timeout=1800, check=False, cwd=app, what=f"flutter {' '.join(args[:2])}")
            fh.write(f"$ {' '.join([*flutter, *args])}  (in {app})\n{p.stdout}\n{p.stderr}\n")
            if p.returncode != 0:
                detail = tail(p.stderr or p.stdout, 4)
                output = (p.stderr + p.stdout).lower()
                # Flutter can fail for compilation/configuration or for host/tooling
                # reasons. Only recognizable project diagnostics blame the app;
                # unknown failures remain infrastructure failures.
                error = AppError if (any(marker in output for marker in APP_FAILURE_MARKERS)
                                     or SOURCE_DIAGNOSTIC.search(p.stderr + p.stdout)) else InfraError
                raise error(f"`flutter {' '.join(args)}` failed in {app}: {detail}")
    out = app / OUTPUTS[platform]
    if not out.exists():
        raise InfraError(f"Flutter build reported success but expected artifact {out} is missing")
    return out
