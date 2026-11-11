"""yepitran: prove a mobile app works on a real iOS Simulator and Android Emulator.

  yepitran <app.app|app.apk|flutter project>... <scenario> [--platform ios|android|both]
  yepitran doctor [--fix]
  yepitran pr <github PR url> [--scenario FILE] [--post]

Exit code: 0 PASS, 1 APP FAIL, 2 INFRA FAIL, 64 bad usage."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

from . import build, proc, report
from .proc import AppError
from .runner import APP_FAIL, INFRA_FAIL, PASS, Leg, LegRunner
from .scenario import ScenarioError, parse

EXIT = {PASS: 0, APP_FAIL: 1, INFRA_FAIL: 2}
USAGE = 64


def usage_error(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(USAGE)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(USAGE, f"{self.prog}: error: {message}\n")


def classify(targets: list[str]) -> tuple[dict[str, Path], Path | None]:
    artifacts, project = {}, None
    for t in targets:
        p = Path(t).expanduser().resolve()
        if not p.exists():
            usage_error(f"yepitran: {t} does not exist")
        if p.suffix == ".app":
            artifacts["ios"] = p
        elif p.suffix == ".apk":
            artifacts["android"] = p
        elif p.suffix == ".ipa":
            usage_error("yepitran: an .ipa is a device build; give the simulator .app "
                        "(flutter build ios --simulator)")
        elif build.is_flutter_project(p):
            project = p
        else:
            usage_error(f"yepitran: {t} is not an .app, an .apk or a Flutter project")
    return artifacts, project


def run_scenario(targets: list[str], scenario_path: Path, platform: str | None, out: Path | None,
                 lease_wait: float, scenario_text: str | None = None) -> tuple[str, Path]:
    try:
        text = scenario_text if scenario_text is not None else scenario_path.read_text()
        sc = parse(text, scenario_path.stem)
    except (OSError, ScenarioError) as e:
        print(f"yepitran: scenario {scenario_path}: {e}", file=sys.stderr)
        sys.exit(USAGE)
    artifacts, project = classify(targets)
    if platform in (None, "both"):
        both = ["ios", "android"] if sys.platform == "darwin" else ["android"]  # iOS needs a Mac
        platforms = both if project or platform == "both" else sorted(artifacts)
    else:
        platforms = [platform]
    missing = [p for p in platforms if p not in artifacts and not project]
    if missing:
        usage_error(f"yepitran: no {' or '.join(missing)} artifact given (pass a .app / .apk or a Flutter project)")

    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{sc.name}-{uuid.uuid4().hex[:8]}"
    outdir = (out or Path.cwd() / "yepitran-out" / run_id).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "scenario.txt").write_text(text)
    print(f"yepitran: {sc.name} on {', '.join(platforms)} -> {outdir}", file=sys.stderr)

    legs: dict[str, Leg] = {}
    for p in platforms:  # builds run one after another: flutter holds a global lock anyway
        if p in artifacts:
            continue
        print(f"[{p}] building {build.app_dir(project)}", file=sys.stderr)
        try:
            artifacts[p] = build.build(project, p, outdir / f"build-{p}.log")
        except proc.Cancelled as e:
            legs[p] = Leg(platform=p, artifact=str(project), verdict=INFRA_FAIL,
                          reason=f"build cancelled: {e or 'interrupted'}")
        except AppError as e:
            legs[p] = Leg(platform=p, artifact=str(project), verdict=APP_FAIL, reason=f"build failed: {e}")
        except proc.InfraError as e:
            legs[p] = Leg(platform=p, artifact=str(project), verdict=INFRA_FAIL, reason=f"build tooling: {e}")

    runners = [LegRunner(p, artifacts[p], sc, outdir, run_id, lease_wait) for p in platforms if p not in legs]
    if proc.cancel.is_set():
        for r in runners:
            legs[r.platform] = Leg(platform=r.platform, artifact=str(r.artifact), verdict=INFRA_FAIL,
                                   reason="interrupted before the device run started")
    else:
        threads = [threading.Thread(target=lambda r=r: legs.__setitem__(r.platform, r.run()), daemon=True)
                   for r in runners]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    for r in runners:  # a thread killed mid-flight still owes a row
        legs.setdefault(r.platform, Leg(platform=r.platform, artifact=str(r.artifact),
                                        reason="interrupted before a verdict"))

    ordered = [legs[p] for p in platforms]
    verdict = report.write(outdir, sc.name, ordered)
    print(f"\n{verdict}", file=sys.stderr)
    for leg in ordered:
        print(f"  {leg.platform}: {leg.verdict}. {leg.reason}", file=sys.stderr)
    print(f"  report: {outdir / 'report.html'}", file=sys.stderr)
    return verdict, outdir


def _terminate(signum, frame):
    if proc.cancel.is_set():
        return
    proc.cancel.set()
    print("\nyepitran: cancellation requested; stopping owned processes and releasing leases",
          file=sys.stderr, flush=True)


def version() -> str:
    try:
        from importlib.metadata import version as installed
        return installed("yepitran")
    except Exception:  # running from a checkout
        return "dev"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("run", "doctor", "pr", "-h", "--help", "--version"):
        argv.insert(0, "run")
    ap = ArgumentParser(prog="yepitran", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--version", action="version", version=f"yepitran {version()}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a scenario (default command)")
    r.add_argument("paths", nargs="+", metavar="target... scenario",
                   help="one or more of: Runner.app, app.apk, Flutter project dir; then the scenario file")
    r.add_argument("--platform", choices=["ios", "android", "both"])
    r.add_argument("--out", type=Path, help="report folder (default ./yepitran-out/<time>-<scenario>)")
    r.add_argument("--lease-wait", type=float, default=1800, help="seconds to queue for a busy device (default 1800)")
    d = sub.add_parser("doctor", help="check Xcode, AXe, adb, emulator, devices, leases")
    d.add_argument("--fix", action="store_true", help="create devices, reap orphans, upgrade AXe")
    p = sub.add_parser("pr", help="build a GitHub PR head and prove it (dry run unless --post)")
    p.add_argument("url")
    p.add_argument("--scenario", type=Path, help="default: the ```yepitran block in the PR body, else .yepitran/*.proof")
    p.add_argument("--platform", choices=["ios", "android", "both"], default="both")
    p.add_argument("--post", action="store_true", help="post the report as a PR comment")
    args = ap.parse_args(argv)

    proc.cancel.clear()
    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    try:
        if args.cmd == "doctor":
            from .doctor import doctor
            return doctor(args.fix)
        if args.cmd == "pr":
            from .pr import prove_pr
            return prove_pr(args.url, args.scenario, args.platform, args.post)
        if len(args.paths) < 2:
            ap.error("give at least one target and a scenario file")
        *targets, scenario = args.paths
        verdict, _ = run_scenario(targets, Path(scenario), args.platform, args.out, args.lease_wait)
        return EXIT[verdict]
    except proc.Cancelled:
        return EXIT[INFRA_FAIL]
    except (proc.InfraError, OSError) as e:
        print(f"yepitran: INFRA FAIL: {e}", file=sys.stderr)
        return EXIT[INFRA_FAIL]


if __name__ == "__main__":
    sys.exit(main())
