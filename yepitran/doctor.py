"""`yepitran doctor`: every check names the exact fix. `--fix` applies the safe ones."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import android, ios, proc, simlock
from .lease import Lease

OK, WARN, FAIL = "ok", "warn", "FAIL"
MAC = sys.platform == "darwin"


def sh(*cmd: str, timeout: float = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x or 0) for x in m.groups()) if m else (0,)


def local_android(add, fix: bool) -> None:
    manager = android.SDK / "cmdline-tools/latest/bin/avdmanager"
    add(OK if manager.is_file() else FAIL, "avdmanager", str(manager) if manager.is_file() else "missing",
        f"install Android SDK command-line tools under {android.SDK}/cmdline-tools/latest")
    lsof = shutil.which("lsof")
    add(OK if lsof else FAIL, "lsof", lsof or "missing; cannot inspect the private adb server",
        "install lsof with your system package manager")
    rc, emu_v = sh(android.EMULATOR, "-version", timeout=30)
    add(OK if rc == 0 else FAIL, "emulator", emu_v.splitlines()[0] if rc == 0 else f"missing at {android.EMULATOR}",
        "sdkmanager emulator, or use a simlock fleet: see README \"Android on simlock\"")
    image = android.IMAGE_DIR
    add(OK if image.exists() else FAIL, "system image", android.IMAGE if image.exists() else "missing",
        f"sdkmanager '{android.IMAGE}'")
    java = android.JBR if Path(android.JBR).exists() else ("java" if sh("java", "-version")[0] == 0 else None)
    add(OK if java else WARN, "Java (for avdmanager)", java or "no JDK (/usr/bin/java is a stub)",
        "install Android Studio or a JDK")
    token = Path.home() / ".emulator_console_auth_token"
    add(OK if token.exists() else WARN, "emulator console token", str(token) if token.exists() else "missing",
        "boot any emulator once; it writes the token")
    if (android.AVD_DIR / "config.ini").exists():
        snap = (android.AVD_DIR / "snapshots" / android.SNAPSHOT).exists()
        add(OK, "yepitran AVD", f"{android.AVD}" + (" with clean snapshot" if snap else " (snapshot made on first run)"))
    elif fix and image.exists() and manager.is_file():
        lease = Lease("android", "doctor-fix")
        lease.acquire(5, print)
        try:
            android.Android(print, Path("/tmp")).ensure_avd()
        finally:
            lease.release()
        add(OK, "yepitran AVD", f"created {android.AVD}")
    else:
        add(WARN, "yepitran AVD", "not created yet (the first run creates it)", "yepitran doctor --fix")


def fleet_android(add) -> None:
    try:
        cfg = simlock.config()
        if not cfg:
            add(FAIL, "simlock gateway", "not configured",
                f"set YEPITRAN_SIMLOCK_URL and YEPITRAN_SIMLOCK_TOKEN, or configure {simlock.CONFIG}")
            return
        add(OK, "Android devices", f"simlock fleet at {cfg['url']} (local emulator not needed; "
            "YEPITRAN_ANDROID=local to use one)")
        c = simlock.Client(cfg)
        who = c.call("GET", "/v1/identity", timeout=20)
        if not isinstance(who, dict):
            raise proc.InfraError("simlock identity response is not an object")
        add(OK, "simlock token", f"{who.get('name')} ({who.get('role')})")
        catalog = c.call("GET", "/v1/catalog?platform=android", timeout=30)
        runtimes = android_runtimes(catalog)
        have = runtimes.get(android.FLEET_DEVICE)
        add(OK if have else FAIL, "simlock Android device",
            f"{android.FLEET_DEVICE} on Android {', '.join(have)}" if have else f"{android.FLEET_DEVICE} has no system image",
            "set YEPITRAN_ANDROID_DEVICE to a model `simlock catalog` lists with a runtime")
        leases = c.my_leases()
        if not isinstance(leases, list):
            raise proc.InfraError("simlock leases response is not a list")
        add(OK if not leases else WARN, "simlock leases", f"{len(leases)} held by yepitran's token"
            + ("" if not leases else " (a killed run's; the next run gives them back)"), "yepitran gives them back itself")
    except proc.InfraError as e:
        add(FAIL, "simlock gateway", str(e), f"check the gateway is up and the token in {simlock.CONFIG} is valid")


def android_runtimes(catalog: object) -> dict[str, list[str]]:
    if not isinstance(catalog, dict):
        raise proc.InfraError("simlock catalog response is not an object")
    platforms = catalog.get("platforms")
    entry = None
    if isinstance(platforms, list):
        entry = next((p for p in platforms if isinstance(p, dict) and
                      (p.get("platform") == "android" or p.get("name") == "android")), None)
    if not isinstance(entry, dict):
        raise proc.InfraError("simlock catalog has no Android platform entry")
    runtimes = entry.get("modelRuntimes", {})
    if not isinstance(runtimes, dict) or any(
            not isinstance(versions, list) or not all(isinstance(version, str) for version in versions)
            for versions in runtimes.values()):
        raise proc.InfraError("simlock catalog has an invalid Android modelRuntimes map")
    return runtimes


def android_backend(add) -> str | None:
    """Resolve the Android backend without letting a broken config crash doctor."""
    try:
        return android.backend()
    except (proc.InfraError, OSError, ValueError, TypeError, AttributeError) as e:
        add(FAIL, "simlock gateway", str(e),
            f"check the Simlock config in {simlock.CONFIG} or YEPITRAN_SIMLOCK_URL/TOKEN")
        return None


def ios_checks(add, fix: bool) -> None:
    rc, xcode = sh("xcodebuild", "-version")
    xver = version(xcode)
    add(OK if rc == 0 else FAIL, "Xcode", xcode.replace("\n", ", ") if rc == 0 else "not found",
        "install Xcode, then `sudo xcode-select -s /Applications/Xcode.app`")
    try:
        dtype, rt = ios.pick_device_type()
        add(OK, "iOS runtime", f"{dtype.split('.')[-1]} on {rt.split('.')[-1]}")
    except proc.InfraError as e:
        add(FAIL, "iOS runtime", str(e), "Xcode > Settings > Components: install an iOS simulator runtime")
    rc, axe_v = sh("axe", "--version")
    av = version(axe_v)
    if rc != 0:
        add(FAIL, "AXe", "not installed", "brew install cameroncooke/axe/axe")
    elif xver >= (27, 1) and av < (1, 8):
        todo = "brew upgrade axe"
        if fix:
            sh("brew", "upgrade", "axe", timeout=600)
            av = version(sh("axe", "--version")[1])
        add(OK if av >= (1, 8) else FAIL, "AXe", f"{'.'.join(map(str, av))} (Xcode 27.1 moved SimulatorKit; "
            "AXe before 1.8.0 cannot read any tree)", todo)
    else:
        add(OK, "AXe", axe_v)
    sim = ios.find_sim()
    if sim:
        add(OK, "yepitran simulator", f"{sim['name']} {sim['udid']} ({sim['state']})")
    elif fix:
        lease = Lease("ios", "doctor-fix")
        lease.acquire(5, print)
        try:
            udid = ios.IOS(print, Path("/tmp")).ensure_sim()
        finally:
            lease.release()
        add(OK, "yepitran simulator", f"created {udid}")
    else:
        add(WARN, "yepitran simulator", "not created yet (the first run creates it)", "yepitran doctor --fix")


def doctor(fix: bool) -> int:
    rows: list[tuple[str, str, str, str]] = []  # status, check, detail, fix

    def add(status, check, detail, todo=""):
        rows.append((status, check, detail, todo))
        mark = {OK: "  ok ", WARN: " warn", FAIL: " FAIL"}[status]
        print(f"{mark}  {check}: {detail}" + (f"\n         -> {todo}" if todo and status != OK else ""))

    if MAC:
        ios_checks(add, fix)
    else:
        add(OK, "iOS", "skipped: iOS needs a Mac (Android runs here)")

    # Android ---------------------------------------------------------------
    rc, adb_v = sh(android.ADB, "version")
    add(OK if rc == 0 else FAIL, "adb", adb_v.splitlines()[0] if rc == 0 else f"missing at {android.ADB}",
        "install Android SDK platform-tools, or set ANDROID_HOME")
    try:
        add(OK, "aapt2", android.build_tool("aapt2"))
    except proc.InfraError as e:
        add(FAIL, "aapt2", str(e), "sdkmanager 'build-tools;36.1.0'")
    backend = android_backend(add)
    if backend == "simlock":
        fleet_android(add)
    elif backend == "local":
        local_android(add, fix)

    # Host ------------------------------------------------------------------
    add(OK if shutil.which("ffmpeg") else WARN, "ffmpeg", shutil.which("ffmpeg") or "missing; Android video stays .webm",
        "brew install ffmpeg")
    add(OK if shutil.which("flutter") or shutil.which("fvm") else WARN, "flutter",
        shutil.which("flutter") or shutil.which("fvm") or "missing; only prebuilt .app/.apk work", "install Flutter")
    free_gb = shutil.disk_usage(Path.home()).free / 1e9
    add(OK if free_gb > 15 else FAIL, "disk", f"{free_gb:.0f} GB free", "free at least 15 GB (emulator data + videos)")
    load = os.getloadavg()[0]
    lf = proc.load_factor()
    add(OK if lf < 2 else WARN, "load", f"{load:.0f} on {os.cpu_count()} cores; timeouts stretch {lf:.1f}x",
        "runs still work, just slower; other sessions are busy")
    booted = sh("xcrun", "simctl", "list", "devices", "booted")[1].count("(Booted)") if MAC else 0
    emus = sum(1 for _, c in proc.processes() if "qemu-system" in c)
    add(OK if booted + emus <= 2 else WARN, "devices on this machine",
        f"{booted} booted simulator(s), {emus} emulator(s)",
        "yepitran itself uses at most one of each; the rest belong to other sessions")
    rc, owner = sh("lsof", "-nP", f"-iTCP:{android.ADB_PORT}", "-sTCP:LISTEN", "-Fc")
    if rc == 0 and owner and "adb" not in owner:
        add(FAIL, "private adb port", f"{android.ADB_PORT} is taken by {owner}", "stop that process")

    # Leases and orphans ------------------------------------------------------
    markers = {"android": android.Android(print, Path("/tmp")).markers()}
    if MAC:
        markers["ios"] = ios.IOS(print, Path("/tmp")).markers()
    for platform, ms in markers.items():
        lease = Lease(platform, "doctor")
        if not lease.is_free():
            h = lease.holder()
            add(OK, f"{platform} lease", f"held by run {h.get('run')} (pid {h.get('pid')}) since {h.get('since')}")
            continue
        orphans = [(pid, cmd) for pid, cmd in proc.processes()
                   if (argv := cmd.split()) and proc.is_reapable(os.path.basename(argv[0]))
                   and any(proc._marker_matches(argv, m, os.path.basename(argv[0])) for m in ms)]
        if orphans and fix:
            lease.acquire(5, print)
            try:
                killed = proc.reap(ms)
            finally:
                lease.release()
            add(OK, f"{platform} orphans", f"killed {len(killed)}")
        elif orphans:
            add(WARN, f"{platform} orphans", f"{len(orphans)} leftover process(es): "
                + ", ".join(str(p) for p, _ in orphans[:5]), "yepitran doctor --fix")
        else:
            add(OK, f"{platform} lease", "free, no orphans")

    fails = sum(1 for r in rows if r[0] == FAIL)
    print(f"\n{'all good' if not fails else f'{fails} problem(s) to fix'}")
    return 1 if fails else 0
