"""Android Emulator driver.

Isolation: our own AVD, on console/adb ports outside the default 5554-5585 scan, registered
with a private adb server (port 5137) that scans nothing. Other sessions' `adb devices`
never see it, and their `adb kill-server` cannot drop it."""
from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import proc, simlock
from .proc import AppError, Cancelled, InfraError, run, sleep, spawn, stop, wait_until
from .ui import Node

SDK = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
           or Path.home() / ("Library/Android/sdk" if sys.platform == "darwin" else "Android/Sdk"))
# The local emulator runs the host's own ABI. An x86_64 image translates arm64 code, so arm64 APKs run on both.
HOST_ABI = "arm64-v8a" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
IMAGE_DIR = SDK / "system-images/android-36/google_apis" / HOST_ABI
# Swiftshader ANRs under load on macOS; a headless Linux CI box has no GPU to use.
GPU = os.environ.get("YEPITRAN_EMULATOR_GPU") or ("host" if sys.platform == "darwin" else "swiftshader_indirect")
ADB = str(SDK / "platform-tools/adb")
EMULATOR = str(SDK / "emulator/emulator")
AVD = "yepitran_API36"
IMAGE = f"system-images;android-36;google_apis;{HOST_ABI}"
AVD_DIR = Path(os.environ.get("ANDROID_AVD_HOME") or
               Path(os.environ.get("ANDROID_USER_HOME") or Path.home() / ".android") / "avd") / f"{AVD}.avd"
ADB_PORT = "5137"
SNAPSHOT = "yepitran_clean_v2"
SEGMENT = 170  # console screenrecord stops itself at 180 s; rotate before that
AVD_CONFIG = {"hw.keyboard": "yes", "hw.gpu.enabled": "yes", "hw.gpu.mode": GPU, "hw.ramSize": "3072",
              "disk.dataPartition.size": "6G", "fastboot.forceColdBoot": "no"}
TRANSPORT = ("device offline", "not found", "no devices", "cannot connect to daemon", "daemon not running",
             "closed", "protocol fault", "Connection reset", "device still connecting", "Broken pipe")
# Every adb call carries this: if the private server died, adb auto-starts a new one, and
# without the scan limit that server would attach to every other session's emulator.
ADB_ENV = {"ADB_LOCAL_TRANSPORT_MAX_PORT": "5554", "ANDROID_ADB_SERVER_PORT": ADB_PORT,
           "ANDROID_ADB_SERVER_ADDRESS": "localhost", "ADB_SERVER_SOCKET": f"tcp:localhost:{ADB_PORT}"}
JBR = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"


def console(port: int, command: str, timeout: float = 15) -> list[str]:
    """Emulator console (telnet protocol). Works with adb dead, which is why video and the
    identity check go through it."""
    try:
        token = (Path.home() / ".emulator_console_auth_token").read_text().strip()
        with socket.create_connection(("127.0.0.1", port), timeout=timeout * proc.load_factor()) as s:
            f = s.makefile("rwb")

            def answer() -> list[str]:
                out = []
                while True:
                    line = f.readline()
                    if not line:
                        raise InfraError(f"emulator console closed during {command!r}")
                    line = line.decode(errors="replace").rstrip("\r\n")
                    if line == "OK":
                        return out
                    if line.startswith("KO"):
                        raise InfraError(f"emulator console refused {command!r}: {line}")
                    out.append(line)

            answer()
            for cmd in (f"auth {token}", command):
                f.write(cmd.encode() + b"\n")
                f.flush()
                out = answer()
            return out
    except OSError as e:
        raise InfraError(f"emulator console on port {port} not answering ({e})") from None


def build_tool(name: str) -> str:
    tools = sorted((SDK / "build-tools").glob(f"*/{name}"),
                   key=lambda p: [int(x) for x in re.findall(r"\d+", p.parent.name)])
    if not tools:
        raise InfraError(f"no {name} in {SDK}/build-tools; install build-tools with sdkmanager")
    return str(tools[-1])


def ffconcat_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "'\\''")


def apk_info(apk: Path) -> dict:
    p = run([build_tool("aapt2"), "dump", "badging", str(apk)], check=False, what="aapt2 dump badging")
    pkg = re.search(r"package: name='([^']+)'", p.stdout)
    act = re.search(r"launchable-activity: name='([^']+)'", p.stdout)
    if not pkg:
        raise AppError(f"{apk.name} is not a readable APK: {proc.tail(p.stderr or p.stdout, 2)}")
    abis = re.search(r"native-code: (.*)", p.stdout)
    if abis and not {"arm64-v8a", HOST_ABI} & set(abis.group(1).replace("'", " ").split()):
        raise AppError(f"{apk.name} has no arm64-v8a or {HOST_ABI} code ({abis.group(1).strip()})")
    return {"id": pkg.group(1), "activity": act.group(1) if act else ""}


def walk(el, path=()):
    """Depth-first, keeping each node's position so taps can tell what is drawn on top."""
    for i, child in enumerate(c for c in el if c.tag == "node"):
        yield path + (i,), child
        yield from walk(child, path + (i,))


def used_emulator_ports() -> set[int]:
    used = set()
    for _, cmd in proc.processes():
        if "qemu-system" in cmd or "/emulator" in cmd:
            for m in re.finditer(r"(?:^|\s)-ports?\s+(\d+)(?:,(\d+))?(?=\s|$)", cmd):
                ports = [int(x) for x in m.groups() if x]
                if len(ports) == 1:
                    ports.append(ports[0] + 1)
                used.update(ports)
    return used


def free(port: int) -> bool:
    """Nothing listens on the port. SO_REUSEADDR so leftover TIME_WAIT connections (the emulator's,
    after adb stops) do not read as busy; a live listener still does."""
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def port_released(port: int, wait: float = 5) -> bool:
    """`adb kill-server` returns before the daemon has closed its socket."""
    deadline = time.monotonic() + wait
    while not free(port):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def pick_ports() -> tuple[int, int]:
    used = used_emulator_ports()
    for c in range(5700, 5800, 2):
        if not {c, c + 1} & used and free(c) and free(c + 1):
            return c, c + 1
    raise InfraError("no free emulator port pair in 5700-5799")


class Android:
    platform = "android"

    def __init__(self, log, workdir: Path):
        self.log, self.workdir = log, workdir
        self.serial = ""
        self.console_port = 0
        self.emu = None
        self.app: dict = {}
        self.reverse: list[int] = []
        self.system_server = ""
        self.screen = (1080.0, 2400.0)
        self.video_stop = threading.Event()
        self.video_thread = None
        self.segments: list[Path] = []

    def markers(self) -> list[str]:
        # The runner reaps abandoned processes before prepare() chooses a new port pair.
        # The lease gives this named AVD a single owner, and proc.reap matches this as
        # an exact -avd argument (unlike the dynamically allocated ports).
        return [f"-avd {AVD}"]

    # adb ------------------------------------------------------------------

    def adb_raw(self, *args: str, timeout: float = 30, binary: bool = False, input=None):
        return run([ADB, "-P", ADB_PORT, "-s", self.serial, *args], timeout=timeout, check=False,
                   binary=binary, input=input, env=ADB_ENV, what=f"adb {args[0]}")

    def adb(self, *args: str, timeout: float = 60, check: bool = True, binary: bool = False, input=None):
        retryable = self.adb_retryable(args)
        for attempt in range(3):
            try:
                p = self.adb_raw(*args, timeout=timeout, binary=binary, input=input)
            except InfraError as e:  # timed out: often a wedged adb server
                self.recover_adb(str(e))
                if not retryable or attempt == 2:
                    raise InfraError(f"adb {' '.join(args)[:80]} timed out; completion is unknown, so it was not replayed") from e
                continue
            err = p.stderr.decode(errors="replace") if binary else p.stderr
            transport = p.returncode != 0 and any(t in err for t in TRANSPORT)
            if transport:
                self.recover_adb(proc.tail(err, 1))
                if retryable and attempt < 2:
                    continue
                raise InfraError(f"adb {' '.join(args)[:80]} lost transport; completion is unknown, so it was not replayed")
            if check and p.returncode != 0:
                out = p.stdout.decode(errors="replace") if binary else p.stdout
                raise InfraError(f"adb {' '.join(args)[:80]}: exit {p.returncode}: {proc.tail(err or out, 3)}")
            return p
        raise AssertionError("unreachable")

    @staticmethod
    def adb_retryable(args: tuple[str, ...]) -> bool:
        if not args:
            return False
        if args[0] == "devices":
            return True
        if args[0] == "reverse":
            return len(args) > 1 and args[1] == "--list"
        if args[0] == "logcat":
            return "-d" in args
        if args[0] == "exec-out":
            return len(args) > 1 and args[1] in {"screencap", "uiautomator"}
        if args[0] == "shell" and len(args) > 1:
            command = args[1].lstrip()
            return bool(re.match(r"(?:getprop|pm path|pidof|dumpsys|settings get|screencap|md5sum)\b", command))
        return False

    def shell(self, command: str, timeout: float = 60, check: bool = True) -> str:
        return self.adb("shell", command, timeout=timeout, check=check).stdout

    def adb_listener(self) -> tuple[int, str] | None:
        """(pid, command line) of whatever listens on the private adb port, or None."""
        pids = run(["lsof", "-nP", "-t", f"-iTCP:{ADB_PORT}", "-sTCP:LISTEN"], check=False, timeout=10,
                   cleanup=True, what="inspect private adb port").stdout.split()
        if not pids:
            return None
        command = run(["ps", "-ww", "-p", pids[0], "-o", "command="], check=False, timeout=10,
                      cleanup=True, what="inspect private adb port").stdout.strip()
        return int(pids[0]), command or "an unknown process"

    @staticmethod
    def is_adb_server(command: str) -> bool:
        program = ADB if command.startswith(ADB + " ") else command.split(" ", 1)[0]
        return os.path.basename(program) == "adb" and " fork-server " in f" {command} "

    def stop_adb_server(self) -> None:
        """Port 5137 is yepitran's. Whatever adb server holds it (ours, a dead run's, or one adb
        auto-started after someone killed ours) is stopped, so the next one starts with ADB_ENV
        and can never scan other sessions' emulators. A non-adb listener is never touched."""
        listener = self.adb_listener()
        if listener is None:
            return
        pid, command = listener
        if not self.is_adb_server(command):
            raise InfraError(f"port {ADB_PORT} (yepitran's private adb port) is taken by `{command[:100]}`; "
                             "stop it or free the port")
        run([ADB, "-P", ADB_PORT, "kill-server"], check=False, timeout=15, env=ADB_ENV,
            cleanup=True, what="stop private adb server")
        if port_released(int(ADB_PORT), wait=15 * proc.load_factor()):
            return
        # kill-server was accepted but the daemon is slow to let go (it drains emulator transports).
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not port_released(int(ADB_PORT)):
            raise InfraError(f"the adb server on port {ADB_PORT} did not stop")

    def start_adb_server(self) -> None:
        self.stop_adb_server()
        run([ADB, "-P", ADB_PORT, "start-server"], env=ADB_ENV, timeout=30, what="private adb server")

    def recover_adb(self, why: str) -> None:
        self.log(f"adb lost the device ({why}); restarting the private adb server and reconnecting")
        self.check_alive()
        self.start_adb_server()

        def back() -> bool:
            out = run([ADB, "-P", ADB_PORT, "devices"], timeout=15, check=False, env=ADB_ENV).stdout
            return f"{self.serial}\tdevice" in out

        wait_until(back, 90, "emulator to re-register with adb", every=2)
        self.apply_reverse()
        self.log("adb reconnected")

    def clear_attempt_outputs(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        for name in ("emulator.log", "logcat.txt", "segments.txt", "video.mp4", "video.webm"):
            path = self.workdir / name
            if path.is_file() or path.is_symlink():
                path.unlink()
        for path in [*self.workdir.glob("video-*.webm"), *self.workdir.glob("video-*.mp4")]:
            if path.is_file() or path.is_symlink():
                path.unlink()

    # device lifecycle -------------------------------------------------------

    def ensure_avd(self) -> None:
        config = AVD_DIR / "config.ini"
        if not config.exists():
            if not IMAGE_DIR.exists():
                raise InfraError(f"system image missing; run: sdkmanager '{IMAGE}'")
            avdmanager = SDK / "cmdline-tools/latest/bin/avdmanager"
            # Respect a caller's JDK; only replace macOS's Java stub when none was chosen.
            env = {"JAVA_HOME": JBR} if not os.environ.get("JAVA_HOME") and Path(JBR).exists() else {}
            # Newer SDK tools on Linux default to ~/.config/.android/avd; pin the AVD (and its .ini)
            # to AVD_DIR so avdmanager, the emulator and yepitran all look in the same place.
            env["ANDROID_AVD_HOME"] = str(AVD_DIR.parent)
            AVD_DIR.parent.mkdir(parents=True, exist_ok=True)
            self.log(f"creating AVD {AVD}")
            run([str(avdmanager), "create", "avd", "-n", AVD, "-k", IMAGE, "-d", "pixel_7", "--force",
                 "-p", str(AVD_DIR)],
                input="no\n", env=env, timeout=180, what="avdmanager create")
        lines = [l for l in config.read_text().splitlines() if not l.startswith("disk.dataPartition.path")]
        keys = {l.split("=", 1)[0] for l in lines}
        lines = [f"{l.split('=', 1)[0]}={AVD_CONFIG[l.split('=', 1)[0]]}" if l.split("=", 1)[0] in AVD_CONFIG else l
                 for l in lines]
        lines += [f"{k}={v}" for k, v in AVD_CONFIG.items() if k not in keys]
        config.write_text("\n".join(lines) + "\n")

    def check_alive(self) -> None:
        if self.emu is None or self.emu.poll() is not None:
            code = None if self.emu is None else self.emu.returncode
            log = proc.tail((self.workdir / "emulator.log").read_text(errors="replace"), 3) \
                if (self.workdir / "emulator.log").exists() else ""
            raise InfraError(f"the emulator process is gone (exit {code}). {log}".strip())

    def load_app(self, artifact: Path, scenario) -> None:
        if self.video_thread:
            self.stop_video()
        self.clear_attempt_outputs()
        self.app = apk_info(artifact)
        if scenario.app and scenario.app != self.app["id"]:
            raise AppError(f"scenario says app {scenario.app} but {artifact.name} is {self.app['id']}")
        if not self.app["activity"]:
            raise AppError(f"{artifact.name} has no launchable activity")

    def prepare(self, artifact: Path, scenario, level: int) -> None:
        self.load_app(artifact, scenario)
        self.reverse = scenario.reverse
        self.ensure_avd()
        self.kill_emulator()
        self.start_adb_server()
        snap = AVD_DIR / "snapshots" / SNAPSHOT
        if level >= 3 and snap.exists():
            shutil.rmtree(snap, ignore_errors=True)
        c, a = pick_ports()
        self.console_port, self.serial = c, f"emulator-{c}"
        cmd = [EMULATOR, "-avd", AVD, "-ports", f"{c},{a}", "-no-window", "-no-audio", "-no-boot-anim",
               "-gpu", GPU, "-no-snapshot-save"]
        use_snap = snap.exists() and level == 1
        cmd += ["-snapshot", SNAPSHOT] if use_snap else ["-no-snapshot-load"]
        if level >= 3 or not use_snap:
            cmd.append("-wipe-data")
            self.log("wiping emulator data for a clean boot")
        t = time.monotonic()
        self.emu = spawn(cmd, self.workdir / "emulator.log", env={**ADB_ENV, "ANDROID_AVD_HOME": str(AVD_DIR.parent)})
        self.wait_booted()
        how = "from snapshot" if use_snap else "cold"
        self.log(f"emulator {self.serial} booted {how} in {time.monotonic() - t:.0f}s")
        self.configure()
        self.adb("uninstall", self.app["id"], check=False, timeout=120)
        if not use_snap:
            console(c, f"avd snapshot save {SNAPSHOT}", timeout=180)
            self.log("saved a clean snapshot for fast boots")
        self.install(artifact, scenario.permissions)
        self.apply_reverse()
        self.system_server = self.shell("pidof system_server").strip()

    def wait_booted(self) -> None:
        name = wait_until(lambda: console(self.console_port, "avd name"), 60,
                          "emulator console to answer", every=1)
        if not name or name[0] != AVD:
            raise InfraError(f"port {self.console_port} answers as AVD {name}, not {AVD}; another session took it")
        self.wait_runtime()

    def wait_runtime(self) -> None:
        def booted() -> bool:
            self.check_alive()
            p = self.adb_raw("shell", "getprop sys.boot_completed; pm path android", timeout=20)
            return p.returncode == 0 and p.stdout.startswith("1") and "package:" in p.stdout

        # A runtime restart right after boot (zygote) would kill the app mid-run; wait it out.
        def stable() -> bool:
            a = self.adb_raw("shell", "pidof system_server", timeout=20).stdout.strip()
            sleep(3)
            b = self.adb_raw("shell", "pidof system_server", timeout=20).stdout.strip()
            return bool(a) and a == b

        for predicate, timeout, what, every in ((booted, 420, "emulator boot", 2),
                                                (stable, 120, "Android runtime to settle", 1)):
            deadline, last = time.monotonic() + timeout * proc.load_factor(), None
            while True:
                self.check_alive()
                try:
                    if predicate():
                        break
                except InfraError as e:
                    last = e
                    self.check_alive()  # a transport blip can recover; a dead device cannot
                if time.monotonic() > deadline:
                    raise InfraError(f"{what} did not happen in {timeout * proc.load_factor():.0f}s"
                                     + (f" (last error: {last})" if last else ""))
                sleep(every)

    def configure(self) -> None:
        self.shell("; ".join([
            "settings put global heads_up_notifications_enabled 0",
            "settings put secure immersive_mode_confirmations confirmed",
            "settings put system screen_off_timeout 2147483647",
            "settings put global verifier_verify_adb_installs 0",
            "settings put global package_verifier_user_consent -1",
            "settings delete secure enabled_accessibility_services",
            "settings put secure show_ime_with_hard_keyboard 0",
            "svc power stayon true",
            "cmd bluetooth_manager disable",
            "input keyevent KEYCODE_WAKEUP",
            "wm dismiss-keyguard",
            "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS",
        ]), check=False, timeout=90)

    def install(self, apk: Path, permissions: str) -> None:
        self.adb("uninstall", self.app["id"], check=False, timeout=120)
        flags = ["-r", "-t"] + (["-g"] if permissions == "allow" else [])
        for i in range(3):
            p = self.install_once(flags, apk)
            out = p.stdout + p.stderr
            if p.returncode == 0 and "Success" in out:
                return
            bad = re.search(r"INSTALL_(PARSE_FAILED\w*|FAILED_(NO_MATCHING_ABIS|OLDER_SDK|INVALID_APK|"
                            r"DUPLICATE_PERMISSION|TEST_ONLY|MISSING_SHARED_LIBRARY)\w*)", out)
            if bad:
                raise AppError(f"Android rejected {apk.name}: {bad.group(0)}")
            if i == 2:
                raise InfraError(f"adb install failed 3 times: {proc.tail(out, 2)}")
            self.log(f"install failed ({proc.tail(out, 1)}); retrying")
            sleep(3)

    def install_once(self, flags: list[str], apk: Path):
        return self.adb("install", *flags, str(apk), timeout=300, check=False)

    def apply_reverse(self) -> None:
        for port in self.reverse:
            p = self.adb_raw("reverse", f"tcp:{port}", f"tcp:{port}")
            if p.returncode != 0:
                raise InfraError(f"adb reverse tcp:{port} failed: {proc.tail(p.stderr or p.stdout, 2)}")

    def kill_emulator(self) -> None:
        if self.emu:
            # The emulator launcher can exit while its qemu child remains alive.
            # Identify the console before sending kill; never trust the port alone.
            try:
                name = console(self.console_port, "avd name", timeout=5)
                if name and name[0] == AVD:
                    console(self.console_port, "kill", timeout=5)
            except InfraError:
                pass
            stop(self.emu, sig=15, grace=30)
        self.emu = None

    def teardown(self) -> None:
        try:
            self.stop_video()
        finally:
            try:
                self.kill_emulator()
            finally:
                try:
                    self.stop_adb_server()
                except InfraError as e:
                    self.log(f"private adb server shutdown: {e}")

    def check_identity(self) -> None:
        name = console(self.console_port, "avd name")
        if not name or name[0] != AVD:
            raise InfraError(f"emulator port {self.console_port} now answers as {name}, not {AVD}")

    def check_health(self) -> None:
        self.check_alive()
        self.check_identity()
        out = self.shell("pidof system_server; echo :; settings get secure enabled_accessibility_services",
                         timeout=30).split(":")
        pid, services = out[0].strip(), (out[1].strip() if len(out) > 1 else "")
        if pid != self.system_server:
            raise InfraError(f"Android restarted its runtime mid-run (system_server {self.system_server} -> "
                             f"{pid or 'none'}; zygote restart)")
        if "talkback" in services.lower():
            self.shell("settings delete secure enabled_accessibility_services", check=False)
            self.log("TalkBack was on (it turns taps into focus moves); turned it off")
        if self.reverse:
            listed = self.adb("reverse", "--list", check=False).stdout
            if any(f"tcp:{p}" not in listed for p in self.reverse):
                self.apply_reverse()
                self.log(f"adb reverse for {self.reverse} had dropped; re-applied")

    # app --------------------------------------------------------------------

    def launch(self) -> None:
        pkg = self.app["id"]
        self.shell(f"am force-stop {pkg}; logcat -b crash -c", check=False)
        self.launched_at = time.time()
        for i in range(3):
            out = self.shell(f"am start -W -n {pkg}/{self.app['activity']}", timeout=180, check=False)
            if "Error:" in out and "does not exist" in out:
                raise AppError(f"launch failed: {proc.tail(out, 2)}")
            if "Status: ok" in out or "Complete" in out:
                return
            self.log(f"launch did not confirm ({proc.tail(out, 1)}); retrying")
            sleep(2)
        raise InfraError("am start did not confirm a launch 3 times (system too slow to start the activity)")

    def app_state(self) -> str:
        pid = self.shell(f"pidof {self.app['id']}", check=False).strip()
        if not pid:
            return "dead"
        focus = re.search(r"mCurrentFocus=(.*)", self.shell("dumpsys window | grep mCurrentFocus", check=False))
        focus = focus.group(1) if focus else ""
        package = re.escape(self.app["id"])
        owns_focus = re.search(rf"(?<![\w.]){package}(?::[\w.]+)?(?=$|[\s/}}])", focus) is not None
        if owns_focus and "Not Responding" in focus:
            return "anr"
        if owns_focus:
            return "foreground"
        return f"background (in front: {focus.strip() or 'nothing'})"

    def crash_log(self) -> str | None:
        crash = self.shell("logcat -d -b crash -v brief", check=False)
        full = self.adb("logcat", "-d", "-v", "threadtime", "-t", "3000", check=False).stdout
        (self.workdir / "logcat.txt").write_text(full)
        package = re.escape(self.app["id"])
        owned = re.search(rf"(?:^|\s)Process:\s*{package}(?::[\w.]+)?(?:,|\s|$)", crash)
        if not owned:
            return None
        # Crash buffer is device-global. Only attribute it when Android names this
        # package as the crashed process; include nearby exception context, not other apps.
        starts = list(re.finditer(r"(?m)^.*FATAL EXCEPTION.*$", crash))
        start = max((m.start() for m in starts if m.start() <= owned.start()), default=owned.start())
        end = next((m.start() for m in starts if m.start() > owned.start()), len(crash))
        block = [line for line in crash[start:end].splitlines() if line.strip()]
        return "crash buffer: " + " | ".join(block[:12])[:800]

    # ui ---------------------------------------------------------------------

    def tree(self) -> list[Node]:
        last = ""
        for i in range(4):
            p = self.adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=60, check=False)
            out = p.stdout
            start = out.find("<hierarchy")
            end = out.find("</hierarchy>", start)
            if start >= 0 and end >= 0:
                return self.parse(out[start:end + len("</hierarchy>")])
            last = proc.tail(out + p.stderr, 1) or "no output"
            self.check_alive()
            sleep(1 + i)
        raise InfraError(f"uiautomator could not read the screen 4 times ({last})")

    def parse(self, xml: str) -> list[Node]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise InfraError(f"uiautomator returned broken XML ({e})") from None
        nodes = []
        for path, n in walk(root):
            m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", n.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            text, desc, hint = n.get("text", ""), n.get("content-desc", ""), n.get("hint", "")
            cls = n.get("class", "").rsplit(".", 1)[-1]
            if cls == "EditText":
                label, value = desc or hint, text
            else:
                label, value = desc or text, text if desc and text != desc else ""
            nodes.append(Node(label=label, id=n.get("resource-id", "").rsplit("/", 1)[-1], value=value,
                              kind=cls, x=x1, y=y1, w=x2 - x1, h=y2 - y1, pkg=n.get("package", ""), path=path))
        if not nodes:
            raise InfraError("uiautomator returned a tree with no nodes")
        self.screen = (max(n.x + n.w for n in nodes), max(n.y + n.h for n in nodes))
        return nodes

    def tap(self, x: float, y: float) -> None:
        self.shell(f"input tap {x:.0f} {y:.0f}")

    def type_text(self, text: str) -> None:
        if not text.isascii():
            raise InfraError("adb `input text` only types ASCII; use ASCII text in the scenario")
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise InfraError("adb `input text` cannot type control characters")
        if "%s" in text:
            raise InfraError("adb `input text` reserves %s for spaces; remove that sequence from the scenario")
        self.shell(f"input text {shlex.quote(text.replace(' ', '%s'))}")

    def clear_text(self) -> None:
        self.shell("input keycombination 113 29; input keyevent 67")

    def swipe(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.shell(f"input swipe {x1:.0f} {y1:.0f} {x2:.0f} {y2:.0f} 500")

    def openurl(self, url: str) -> None:
        out = self.shell(f"am start -W -a android.intent.action.VIEW -d {shlex.quote(url)} {self.app['id']}",
                         timeout=120, check=False)
        if "Error" in out:
            raise AppError(f"no activity in {self.app['id']} handles {url}: {proc.tail(out, 2)}")

    def screenshot(self, path: Path) -> None:
        for _ in range(3):
            p = self.adb("exec-out", "screencap", "-p", binary=True, check=False, timeout=60)
            if p.returncode == 0 and proc.valid_png(p.stdout):
                path.write_bytes(p.stdout)
                return
            sleep(1)
        # adb keeps truncating; the emulator can still grab its own framebuffer.
        self.log("adb screencap was truncated 3 times; using the emulator's own framebuffer")
        with tempfile.TemporaryDirectory() as d:
            console(self.console_port, f"screenrecord screenshot {d}", timeout=60)
            shots = sorted(Path(d).glob("*.png"))
            if not shots or not proc.valid_png(shots[-1].read_bytes()):
                raise InfraError("no valid screenshot from adb or the emulator console")
            shutil.copy(shots[-1], path)

    def start_video(self, path: Path) -> None:
        if self.video_thread and self.video_thread.is_alive():
            raise InfraError("the previous Android video recorder is still stopping")
        workdir, stop_event, segments = self.workdir, threading.Event(), []
        self.video_path, self.video_stop, self.segments = path, stop_event, segments
        for old in self.workdir.glob("video-*.webm"):
            if old.is_file() or old.is_symlink():
                old.unlink()
        for old in (path, path.with_suffix(".webm")):
            if old.is_file() or old.is_symlink():
                old.unlink()
        def record() -> None:
            i = 0
            while not stop_event.is_set():
                seg = workdir / f"video-{i:02d}.webm"
                try:
                    console(self.console_port, f"screenrecord start --fps 15 {seg}")
                except InfraError as e:
                    self.log(f"video segment {i} did not start: {e}")
                    return
                segments.append(seg)
                stop_event.wait(SEGMENT)
                try:
                    console(self.console_port, "screenrecord stop")
                except InfraError:
                    return
                i += 1

        self.video_thread = threading.Thread(target=record, daemon=True)
        self.video_thread.start()

    def stop_video(self) -> None:
        if not self.video_thread:
            return
        thread, stop_event = self.video_thread, self.video_stop
        stop_event.set()
        thread.join(30)
        if thread.is_alive():
            raise InfraError("Android video recorder did not stop")
        self.video_thread = None
        if not proc.cancel.is_set():
            time.sleep(1)
            self.join_segments([s for s in self.segments if s.exists() and s.stat().st_size > 0])

    def join_segments(self, segs: list[Path]) -> None:
        if not segs or proc.cancel.is_set():
            return
        if not shutil.which("ffmpeg"):
            shutil.copy(segs[0], self.video_path.with_suffix(segs[0].suffix))
            return
        listing = self.workdir / "segments.txt"
        listing.write_text("".join(f"file '{ffconcat_quote(s)}'\n" for s in segs))
        p = run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-vf",
                 "scale=trunc(iw/4)*2:trunc(ih/4)*2", str(self.video_path)], timeout=300, check=False)
        if p.returncode == 0:
            for s in segs:
                s.unlink()
            listing.unlink()
        else:
            self.log(f"ffmpeg could not convert the video; kept the .webm segments ({proc.tail(p.stderr, 1)})")


# Simlock fleet ----------------------------------------------------------------------

FLEET_DEVICE = os.environ.get("YEPITRAN_ANDROID_DEVICE", "Pixel 8")
REMOTE_APK = "/data/local/tmp/yepitran.apk"
CHUNK = 6 * 1024 * 1024  # raw bytes per upload call; 4 run at once
TRIES = 5  # per upload part


def backend() -> str:
    """`simlock` when a gateway is configured (this Mac runs no emulators), else `local`.
    YEPITRAN_ANDROID=local|simlock forces one."""
    forced = os.environ.get("YEPITRAN_ANDROID", "").strip().lower()
    if forced in ("local", "simlock"):
        return forced
    return "simlock" if simlock.config() else "local"


class SimlockAndroid(Android):
    """Android on a Simlock fleet emulator, which runs on another machine. adb runs there
    through the gateway's exec API; files cross as base64 over stdin and stdout. Every
    attempt leases a fresh device: simlock wipes a device when its lease ends."""

    def __init__(self, log, workdir: Path, cfg: dict | None = None):
        super().__init__(log, workdir)
        cfg = cfg or simlock.config()
        if not cfg:
            raise InfraError("Android is set to simlock but there is no Simlock URL/token; "
                             f"write {simlock.CONFIG} or set YEPITRAN_SIMLOCK_URL and YEPITRAN_SIMLOCK_TOKEN")
        self.client = simlock.Client(cfg)
        self.lease_id = ""
        self.lost = ""  # why simlock took the lease back, once it has
        self.renewer: simlock.Renewer | None = None
        self.remote_segments: list[str] = []

    def markers(self) -> list[str]:
        return []  # nothing runs on this Mac

    # adb over the gateway ---------------------------------------------------------

    def adb_raw(self, *args: str, timeout: float = 30, binary: bool = False, input=None):
        if binary or input is not None:
            raise InfraError("internal: binary adb I/O is not available on a simlock device")
        for i in range(3):
            self.check_alive()
            if proc.cancel.is_set():
                raise Cancelled()
            try:
                code, out, err = self.client.exec(self.lease_id, "adb", list(args), timeout=timeout)
                return subprocess.CompletedProcess(args, code, out, err)
            except simlock.Error as e:  # answered before the command ran: safe to resend
                if e.code == "UNKNOWN_LEASE":
                    self.lost = "simlock ended the lease (expired or taken back)"
                    raise InfraError(self.lost) from None
                if e.code != "WORKER_UNREACHABLE" or i == 2:
                    raise
                self.log(f"{e}; resending")
                sleep(3 * (i + 1))
        raise AssertionError("unreachable")

    def recover_adb(self, why: str) -> None:
        self.check_alive()  # a lease simlock ended never comes back: fail now, not after a wait
        self.log(f"lost the fleet device ({why}); waiting for it to answer again")
        deadline = time.monotonic() + 120 * proc.load_factor()
        while True:
            self.check_alive()
            try:
                code, out, _ = self.client.exec(self.lease_id, "adb", ["get-state"], timeout=20)
                self.check_alive()
                if code == 0 and "device" in out:
                    self.log("fleet device answers again")
                    return
            except simlock.Error as e:
                if e.code == "UNKNOWN_LEASE":
                    self.lost = "simlock ended the lease (expired or taken back)"
            except InfraError:
                pass
            self.check_alive()
            if time.monotonic() > deadline:
                raise InfraError(f"the fleet emulator did not answer for {120 * proc.load_factor():.0f}s")
            sleep(3)

    def check_alive(self) -> None:
        if not self.lease_id:
            raise InfraError("no simlock device is leased")
        if self.lost:
            raise InfraError(self.lost)
        if self.renewer and self.renewer.lost:
            raise InfraError(self.renewer.lost)

    def check_identity(self) -> None:
        pass  # the gateway fills in the leased device and refuses any other

    # lifecycle --------------------------------------------------------------------

    def prepare(self, artifact: Path, scenario, level: int) -> None:
        try:
            self.load_app(artifact, scenario)
        finally:
            self.release_lease()  # the last attempt's device; every attempt gets a fresh one
        if scenario.reverse:
            raise InfraError("`reverse:` needs a local emulator: a simlock device runs on another machine, "
                             "so adb reverse would reach that machine, not this one. Run with YEPITRAN_ANDROID=local")
        for old in self.client.my_leases():  # a killed run's; our own flock makes them ours
            self.client.release(old["id"])
            self.log(f"gave back simlock lease {old['id']} left by an earlier run")
        t = time.monotonic()
        lease = self.client.lease("android", FLEET_DEVICE, self.log)
        self.lease_id, self.serial = lease["id"], lease.get("address", "")
        try:
            self.renewer = simlock.Renewer(self.client, self.lease_id, self.log)
            worker = (lease.get("worker") or {}).get("label", "?")
            self.log(f"simlock leased a fresh {lease.get('device')} (Android {lease.get('os')}) on {worker} "
                     f"in {time.monotonic() - t:.0f}s")
            self.wait_runtime()
            self.configure()
            self.install(artifact, scenario.permissions)
            self.system_server = self.shell("pidof system_server").strip()
        except BaseException:
            try:
                self.release_lease()
            except InfraError as e:
                self.log(f"could not give the simlock device back after setup failed: {e}")
            raise

    def install(self, apk: Path, permissions: str) -> None:
        self.push(apk, REMOTE_APK)
        super().install(apk, permissions)
        self.shell(f"rm -f {REMOTE_APK}", check=False)

    def install_once(self, flags: list[str], apk: Path):
        p = self.adb("shell", f"pm install {' '.join(flags)} {REMOTE_APK}", timeout=300, check=False)
        return subprocess.CompletedProcess(p.args, p.returncode, p.stdout, p.stderr)

    def push(self, local: Path, remote: str) -> None:
        data = local.read_bytes()
        if not data:
            raise AppError(f"{local.name} is empty")
        parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
        t = time.monotonic()

        def send(n: int) -> str:
            body = base64.b64encode(parts[n]).decode()
            for i in range(TRIES):
                self.check_alive()
                # A broken stream does not stop the command on the worker, so every try writes
                # its own file: an earlier try still writing can never mix into the one we keep.
                part = f"{remote}.{n:03d}.{i}"
                try:
                    code, _, err = self.client.exec(self.lease_id, "adb", ["shell", f"base64 -d > {part}"],
                                                    stdin=body, timeout=300)
                    if code == 0:
                        return part
                    last = proc.tail(err, 1)
                except simlock.Error as e:
                    if e.code == "UNKNOWN_LEASE":
                        self.lost = "simlock ended the lease during the upload"
                        raise InfraError(self.lost) from None
                    last = str(e)
                except InfraError as e:
                    last = str(e)
                if proc.cancel.is_set():
                    raise Cancelled()
                # The worker's uplink drops for a few seconds under a big upload; give it time.
                if i < TRIES - 1:
                    sleep(min(30, 3 * 2 ** i))
            raise InfraError(f"upload of {local.name} part {n} failed {TRIES} times ({last})")

        with ThreadPoolExecutor(4) as pool:
            names = " ".join(pool.map(send, range(len(parts))))
        out = self.shell(f"cat {names} > {remote} && rm -f {remote}.[0-9]* && md5sum {remote}", timeout=120)
        if hashlib.md5(data).hexdigest() not in out:
            raise InfraError(f"upload of {local.name} arrived damaged (md5 mismatch)")
        self.log(f"uploaded {local.name} ({len(data) / 1e6:.0f} MB) in {time.monotonic() - t:.0f}s")

    def release_lease(self) -> None:
        if self.renewer:
            self.lost = self.lost or self.renewer.lost
            self.renewer.stop()
            self.renewer = None
        if self.lease_id:
            self.client.release(self.lease_id)
            self.lease_id, self.lost = "", ""  # only once it is back: a failed release is tried again at teardown
            self.log("gave the simlock device back")

    def kill_emulator(self) -> None:
        self.release_lease()

    def teardown(self) -> None:
        try:
            self.stop_video()
        finally:
            self.release_lease()

    # screen -------------------------------------------------------------------------

    def screenshot(self, path: Path) -> None:
        for _ in range(3):
            p = self.adb("shell", "screencap -p | base64", timeout=90, check=False)
            png = base64.b64decode(p.stdout) if p.returncode == 0 else b""
            if proc.valid_png(png):
                path.write_bytes(png)
                return
            sleep(1)
        raise InfraError("the fleet emulator gave no valid screenshot 3 times")

    def start_video(self, path: Path) -> None:
        if self.video_thread and self.video_thread.is_alive():
            raise InfraError("the previous Android video recorder is still stopping")
        stop_event, remote = threading.Event(), []
        self.video_path, self.video_stop, self.remote_segments = path, stop_event, remote
        for old in (path, path.with_suffix(".webm")):
            if old.is_file() or old.is_symlink():
                old.unlink()
        lease_id = self.lease_id

        def record() -> None:
            i = 0
            while not stop_event.is_set():
                seg = f"/data/local/tmp/yepitran-video-{i:02d}.mp4"
                remote.append(seg)
                try:
                    self.client.exec(lease_id, "adb", ["shell", f"screenrecord --time-limit {SEGMENT} "
                                                       f"--bit-rate 2000000 --size 720x1600 {seg}"],
                                     timeout=SEGMENT + 60)
                except Cancelled:
                    return
                except InfraError as e:
                    self.log(f"video segment {i} stopped: {e}")
                    return
                i += 1

        self.video_thread = threading.Thread(target=record, daemon=True)
        self.video_thread.start()

    def stop_video(self) -> None:
        if not self.video_thread:
            return
        thread, stop_event = self.video_thread, self.video_stop
        stop_event.set()
        if proc.cancel.is_set() or not self.lease_id or self.lost or (self.renewer and self.renewer.lost):
            return  # release wipes the device; keep the thread reference until it actually exits
        deadline = time.monotonic() + 60
        while thread.is_alive() and time.monotonic() < deadline and self.lease_id:
            self.check_alive()
            try:  # repeat: the stop can land before screenrecord has started
                self.client.exec(self.lease_id, "adb", ["shell", "pkill -INT screenrecord"], timeout=20)
            except simlock.Error as e:
                if e.code == "UNKNOWN_LEASE":
                    self.lost = "simlock ended the lease (expired or taken back)"
                    return
            except InfraError:
                pass
            thread.join(2)
        if thread.is_alive():
            raise InfraError("Android video recorder did not stop")
        self.video_thread = None
        if proc.cancel.is_set() or not self.lease_id or self.lost:
            return
        # A broken exec stream ends the thread but not screenrecord on the device; stop it and
        # wait, or the last segment is pulled half-written. The device is ours alone.
        self.shell("pkill -INT screenrecord; for i in $(seq 40); do pidof screenrecord >/dev/null || exit 0; "
                   "sleep 0.5; done; exit 1", timeout=60)
        segs = []
        for n, remote in enumerate(self.remote_segments):
            p = self.adb("shell", f"[ -s {remote} ] && base64 {remote}", timeout=300, check=False)
            data = base64.b64decode(p.stdout) if p.returncode == 0 else b""
            if data:
                local = self.workdir / f"video-{n:02d}.mp4"
                local.write_bytes(data)
                segs.append(local)
        self.join_segments(segs)
