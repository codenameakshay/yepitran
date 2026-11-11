#!/usr/bin/env python3
"""Break things on purpose and check yepitran recovers or says INFRA FAIL plainly.

    scripts/chaos.py <ios .app> <android .apk> <scenario> [case ...]

Cases: kill-adb, kill-emulator, kill-sim, parallel, sigint, sigkill, lose-lease. Default: all that fit
the Android backend (kill-adb and kill-emulator need a local emulator; lose-lease needs simlock).
After every case: no leftover device processes, both leases free, no simlock lease held."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yepitran import android, ios, proc, simlock  # noqa: E402
from yepitran.lease import Lease  # noqa: E402

APP, APK, SCENARIO = sys.argv[1:4]
FLEET = android.backend() == "simlock"
CASES = sys.argv[4:] or (["kill-sim", "parallel", "sigint", "sigkill", "lose-lease"] if FLEET else
                         ["kill-adb", "kill-emulator", "kill-sim", "parallel", "sigint", "sigkill"])


def unsupported_cases(cases: list[str], fleet: bool) -> list[str]:
    known = {"kill-adb", "kill-emulator", "kill-sim", "parallel", "sigint", "sigkill", "lose-lease"}
    local_only = {"kill-adb", "kill-emulator"}
    fleet_only = {"lose-lease"}
    return [name for name in cases if name not in known or (fleet and name in local_only) or
            (not fleet and name in fleet_only)]


unsupported = unsupported_cases(CASES, FLEET)
if unsupported:
    backend_name = "simlock fleet" if FLEET else "local emulator"
    raise SystemExit(f"unsupported chaos case(s) for {backend_name}: {', '.join(unsupported)}")

OUT = ROOT / "yepitran-out" / time.strftime("chaos-%Y%m%d-%H%M%S")
ADB = [android.ADB, "-P", android.ADB_PORT]


class Run:
    def __init__(self, name: str, *targets: str):
        self.out = OUT / name
        self.lines: list[str] = []
        self.p = subprocess.Popen([str(ROOT / "bin/yepitran"), *targets, SCENARIO, "--out", str(self.out)],
                                  stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stderr:
            self.lines.append(line.rstrip())
            print(f"    {line.rstrip()}", flush=True)

    def wait_for(self, text: str, timeout: float = 900) -> bool:
        """False when the run ended (or stalled) before the fault could be injected."""
        deadline = time.time() + timeout
        while not any(text in l for l in self.lines):
            if time.time() > deadline or self.p.poll() is not None:
                print(f"    (never saw {text!r}; no fault injected)", flush=True)
                return False
            time.sleep(0.5)
        return True

    def finish(self) -> tuple[int, dict]:
        code = self.p.wait()
        time.sleep(1)
        f = self.out / "verdict.json"
        return code, json.loads(f.read_text()) if f.exists() else {}


def leftovers() -> list[str]:
    markers = android.Android(print, Path("/tmp")).markers() + ios.IOS(print, Path("/tmp")).markers()
    return [c[:100] for _, c in proc.processes()
            if any(m in c for m in markers) and any(os.path.basename(c.split(" ", 1)[0]).startswith(r)
                                                    for r in proc.REAPABLE)]


def clean_after(name: str) -> list[str]:
    problems = []
    if leftovers():
        problems.append(f"leftover processes: {leftovers()}")
    for p in ("ios", "android"):
        if not Lease(p, "chaos").is_free():
            problems.append(f"{p} lease still held")
    if FLEET and simlock.Client(simlock.config()).my_leases():
        problems.append("simlock lease still held")
    sim = ios.find_sim()
    if sim and sim["state"] != "Shutdown":
        problems.append(f"simulator left {sim['state']}")
    return problems


def summary(v: dict) -> str:
    return "; ".join(f"{l['platform']} {l['verdict']} in {len(l['attempts'])} attempt(s): {l['reason'][:140]}"
                     for l in v.get("legs", []))


def not_injected(r: Run) -> tuple[bool, str]:
    code, v = r.finish()
    return False, f"fault never injected (run ended first): {summary(v)}"


def case_kill_adb():
    r = Run("kill-adb", APK)
    if not r.wait_for("ok   launch"):
        return not_injected(r)
    subprocess.run([*ADB, "kill-server"])
    code, v = r.finish()
    return v.get("verdict") == "PASS", summary(v)


def case_kill_emulator():
    r = Run("kill-emulator", APK)
    if not r.wait_for("ok   wait"):
        return not_injected(r)
    for pid, cmd in proc.processes():
        if "qemu-system" in cmd and f"-avd {android.AVD}" in cmd:
            os.kill(pid, signal.SIGKILL)
    code, v = r.finish()
    leg = v["legs"][0]
    ok = leg["verdict"] == "PASS" and len(leg["attempts"]) >= 2 or leg["verdict"] == "INFRA FAIL"
    return ok, summary(v)


def case_kill_sim():
    r = Run("kill-sim", APP)
    if not r.wait_for("ok   wait"):
        return not_injected(r)
    subprocess.run(["xcrun", "simctl", "shutdown", ios.find_sim()["udid"]])
    code, v = r.finish()
    leg = v["legs"][0]
    ok = leg["verdict"] == "PASS" and len(leg["attempts"]) >= 2 or leg["verdict"] == "INFRA FAIL"
    return ok, summary(v)


def case_parallel():
    a, b = Run("parallel-a", APP), Run("parallel-b", APP)
    (ca, va), (cb, vb) = a.finish(), b.finish()
    queued = any("waiting for the ios device" in l for l in a.lines + b.lines)
    return va.get("verdict") == vb.get("verdict") == "PASS" and queued, \
        f"queued={queued}; A: {summary(va)} | B: {summary(vb)}"


def case_sigint():
    r = Run("sigint", APP, APK)
    if not r.wait_for("ok   launch"):
        return not_injected(r)
    r.p.send_signal(signal.SIGINT)
    code, v = r.finish()
    return code == 2 and v.get("verdict") == "INFRA FAIL", f"exit {code}; {summary(v)}"


def case_sigkill():
    r = Run("sigkill", APK)
    if not r.wait_for("ok   launch"):
        return not_injected(r)
    r.p.kill()
    r.p.wait()
    stranded = leftovers()
    r2 = Run("sigkill-next", APK)
    code, v = r2.finish()
    reaped = any("orphaned" in l or "left by an earlier run" in l for l in r2.lines)
    return v.get("verdict") == "PASS" and reaped, \
        f"{len(stranded)} process(es) stranded by kill -9; next run reaped={reaped}; {summary(v)}"


def case_lose_lease():
    """Explicitly release Simlock's lease mid-run to exercise lease-loss recovery."""
    r = Run("lose-lease", APK)
    if not r.wait_for("ok   wait"):
        return not_injected(r)
    c = simlock.Client(simlock.config())
    for lease in c.my_leases():
        c.release(lease["id"])
    code, v = r.finish()
    leg = v["legs"][0]
    ok = leg["verdict"] == "PASS" and len(leg["attempts"]) >= 2 or leg["verdict"] == "INFRA FAIL"
    return ok, summary(v)


results = []
for name in CASES:
    print(f"\n== {name}", flush=True)
    t = time.time()
    ok, detail = globals()[f"case_{name.replace('-', '_')}"]()
    problems = clean_after(name)
    ok = ok and not problems
    results.append((name, ok, detail, problems, round(time.time() - t)))
    print(f"== {name}: {'OK' if ok else 'NOT OK'} ({time.time() - t:.0f}s) {detail} {problems or ''}", flush=True)

print("\nchaos summary")
for name, ok, detail, problems, secs in results:
    print(f"  {'OK    ' if ok else 'NOT OK'} {name:14s} {secs:4d}s  {detail}  {'; '.join(problems)}")
(OUT / "chaos.json").write_text(json.dumps(results, indent=2))
sys.exit(0 if all(r[1] for r in results) else 1)
