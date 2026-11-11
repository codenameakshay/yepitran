"""iOS Simulator driver: simctl for the device, AXe for the accessibility tree and input."""
from __future__ import annotations

import json
import plistlib
import re
import shutil
import time
from pathlib import Path

from . import proc
from .proc import AppError, InfraError, run, sleep, spawn, stop, wait_until
from .ui import Node

SIM_NAME = "yepitran iPhone"
AXE_BROKEN = ("SimulatorKit", "Library not loaded", "image not found", "dyld")
AXE_TRANSIENT = ("remote automation session", "No translation object", "Connection interrupted",
                 "Unable to", "timed out", "Timed out")


def simctl(*args: str, timeout: float = 60, check: bool = True, what: str | None = None,
           cleanup: bool = False, **kw):
    return run(["xcrun", "simctl", *args], timeout=timeout, check=check, what=what or f"simctl {args[0]}",
               cleanup=cleanup, **kw)


def pick_device_type() -> tuple[str, str]:
    """Newest runtime that offers a plain iPhone Pro. Foldables (iPhone Duo) give AXe an empty tree."""
    rts = json.loads(simctl("list", "runtimes", "-j").stdout)["runtimes"]
    rts = [r for r in rts if r.get("platform") == "iOS" and r.get("isAvailable")]
    for rt in sorted(rts, key=lambda r: [int(x) for x in re.findall(r"\d+", r["version"])], reverse=True):
        pros = [d for d in rt.get("supportedDeviceTypes", []) if re.fullmatch(r"iPhone \d+ Pro", d["name"])]
        if pros:
            best = max(pros, key=lambda d: int(re.search(r"\d+", d["name"]).group()))
            return best["identifier"], rt["identifier"]
    raise InfraError("no iOS runtime offers an iPhone Pro simulator; install one in Xcode > Settings > Components")


def find_sim(cleanup: bool = False) -> dict | None:
    for rt, devs in json.loads(simctl("list", "devices", "-j", cleanup=cleanup).stdout)["devices"].items():
        for d in devs:
            if d["name"] == SIM_NAME and d.get("isAvailable", True):
                return {**d, "runtime": rt}
    return None


def app_info(app: Path) -> dict:
    plist = app / "Info.plist"
    if not plist.exists():
        raise AppError(f"{app} is not an iOS .app bundle (no Info.plist)")
    info = plistlib.loads(plist.read_bytes())
    if "iPhoneSimulator" not in info.get("CFBundleSupportedPlatforms", ["iPhoneSimulator"]):
        raise AppError(f"{app.name} is built for {info.get('CFBundleSupportedPlatforms')}, not the simulator "
                       "(build with `--simulator`)")
    return {"id": info["CFBundleIdentifier"], "exe": info.get("CFBundleExecutable", ""),
            "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or ""}


class IOS:
    platform = "ios"

    def __init__(self, log, workdir: Path):
        self.log, self.workdir = log, workdir
        self.udid = ""
        self.app: dict = {}
        self.video = None
        self.screen = (402.0, 874.0)
        self.launched_at = 0.0

    # device lifecycle -------------------------------------------------

    def markers(self) -> list[str]:
        sim = find_sim()
        return [sim["udid"]] if sim else []

    def ensure_sim(self) -> str:
        sim = find_sim()
        if sim:
            return sim["udid"]
        dtype, rt = pick_device_type()
        self.log(f"creating simulator {SIM_NAME!r} ({dtype.split('.')[-1]}, {rt.split('.')[-1]})")
        return simctl("create", SIM_NAME, dtype, rt, timeout=120).stdout.strip()

    def state(self, cleanup: bool = False) -> str:
        sim = find_sim(cleanup=cleanup)
        return sim["state"] if sim and sim["udid"] == self.udid else "Missing"

    def prepare(self, artifact: Path, scenario, level: int) -> None:
        self.app = app_info(artifact)
        if scenario.app and scenario.app != self.app["id"]:
            raise AppError(f"scenario says app {scenario.app} but {artifact.name} is {self.app['id']}")
        self.udid = self.ensure_sim()
        if self.state() != "Shutdown":
            simctl("shutdown", self.udid, timeout=120, check=False)
            wait_until(lambda: self.state() == "Shutdown", 120, "simulator shutdown")
        if level >= 3:
            self.log("erasing the simulator (last-resort clean slate)")
            simctl("erase", self.udid, timeout=180)
        self.configure()
        t = time.monotonic()
        simctl("boot", self.udid, timeout=180)
        simctl("bootstatus", self.udid, "-b", timeout=420, what="simulator boot")
        self.log(f"simulator booted in {time.monotonic() - t:.0f}s")
        roots = wait_until(self._tree_once, 180, "accessibility reader (AXe) ready", every=3)
        self.screen = (roots[0].w, roots[0].h)
        simctl("uninstall", self.udid, self.app["id"], check=False, timeout=120)
        simctl("keychain", self.udid, "reset", check=False)
        for i in range(3):
            try:
                simctl("install", self.udid, str(artifact), timeout=300, what="app install")
                break
            except InfraError as e:
                if self._is_app_install_error(str(e)):
                    raise AppError(f"the simulator rejected {artifact.name}: {e}") from None
                if i == 2:
                    raise
                self.log(f"install failed, retrying: {e}")
                sleep(3)
        action = "grant" if scenario.permissions == "allow" else "revoke"
        simctl("privacy", self.udid, action, "all", self.app["id"], check=False)

    @staticmethod
    def _is_app_install_error(message: str) -> bool:
        text = message.casefold()
        return any(marker in text for marker in (
            "invalid application bundle", "invalid app bundle", "not a valid application",
            "failed to verify code signature", "code signature is invalid", "unsupported architecture",
            "built for ios device", "does not contain an executable",
        ))

    KEYBOARD_OFF = ("KeyboardAutocorrection", "KeyboardPrediction", "KeyboardCheckSpelling",
                    "KeyboardShowPredictionBar", "KeyboardAutocapitalization", "KeyboardPeriodShortcut",
                    "KeyboardSmartPunctuation")

    def configure(self) -> None:
        """Autocorrect rewrites typed text ("yepitran" -> "proof run", "Cab" -> "Can"). The keyboard
        daemon reads these once at boot, so they are written into the shut-down device's plist."""
        plist = Path.home() / f"Library/Developer/CoreSimulator/Devices/{self.udid}/data/Library/Preferences/com.apple.Preferences.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        prefs = plistlib.loads(plist.read_bytes()) if plist.exists() else {}
        if all(prefs.get(k) is False for k in self.KEYBOARD_OFF):
            return
        prefs.update({k: False for k in self.KEYBOARD_OFF})
        plist.write_bytes(plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY))

    def teardown(self) -> None:
        try:
            self.stop_video()
        finally:
            if self.udid and self.state(cleanup=True) in ("Booted", "Booting"):
                simctl("shutdown", self.udid, timeout=120, check=False, cleanup=True)

    def check_health(self) -> None:
        st = self.state()
        if st != "Booted":
            raise InfraError(f"the simulator is {st.lower()}, not booted (killed or shut down from outside)")

    # app ----------------------------------------------------------------

    def launch(self) -> None:
        simctl("terminate", self.udid, self.app["id"], check=False)
        self.launched_at = time.time()
        simctl("launch", self.udid, self.app["id"], timeout=120, what="app launch")

    def app_state(self) -> str:
        result = simctl("spawn", self.udid, "launchctl", "list", check=False)
        if result.returncode != 0:
            return "unknown"
        out = result.stdout
        alive = any(re.match(r"^\d+\s", l) and f"UIKitApplication:{self.app['id']}[" in l for l in out.splitlines())
        if not alive:
            return "dead"
        try:
            roots = self._tree_once()
        except InfraError:
            return "unknown"
        front = roots[0].label if roots else ""
        if not self.app["name"] or front == self.app["name"]:
            return "foreground"
        return f"background (in front: {front or 'system UI'})"

    def crash_log(self) -> str | None:
        reports = Path.home() / "Library/Logs/DiagnosticReports"
        exe = self.app.get("exe") or "Runner"
        bundle_id = self.app.get("id")
        if not bundle_id:
            return None
        fresh = []
        bundle_marker = re.compile(r'"bundleID"\s*:\s*' + re.escape(json.dumps(bundle_id)))
        for report in reports.glob("*.ips"):
            if not report.name.startswith(exe) or report.stat().st_mtime < self.launched_at - 1:
                continue
            try:
                text = report.read_text(errors="replace")
            except OSError:
                continue
            # Crash logs can be shared across concurrent simulator sessions.
            if bundle_marker.search(text) and f"/Devices/{self.udid}/" in text:
                fresh.append((report, text))
        if not fresh:
            return None
        newest, text = max(fresh, key=lambda item: item[0].stat().st_mtime)
        dest = self.workdir / newest.name
        shutil.copy(newest, dest)
        parts = [m.group(0) for m in (re.search(r'"exception"\s*:\s*\{[^}]*\}', text),
                                      re.search(r'"termination"\s*:\s*\{[^}]*\}', text)) if m]
        return f"crash report {dest.name}: " + " ".join(parts)[:600]

    # ui -----------------------------------------------------------------

    def _tree_once(self) -> list[Node]:
        p = run(["axe", "describe-ui", "--udid", self.udid], timeout=45, check=False, what="axe describe-ui")
        if p.returncode != 0:
            err = proc.tail(p.stderr or p.stdout, 3)
            if any(s in err for s in AXE_BROKEN):
                raise InfraError(f"AXe cannot load the simulator frameworks ({err}). Upgrade AXe "
                                 "(`brew upgrade axe`; Xcode 27.1 needs 1.8.0+). `yepitran doctor` checks this")
            raise InfraError(f"axe describe-ui failed: {err}")
        try:
            data = json.loads(p.stdout)
        except ValueError:
            raise InfraError("axe describe-ui returned unreadable output") from None
        nodes: list[Node] = []
        for i, root in enumerate(data):
            self._flatten(root, nodes, (i,))
        if not nodes:
            raise InfraError("axe describe-ui returned an empty tree")
        return nodes

    def _flatten(self, n: dict, out: list[Node], path: tuple) -> None:
        f = n.get("frame") or {}
        v = n.get("AXValue")
        out.append(Node(label=n.get("AXLabel") or "", id=n.get("AXUniqueId") or "",
                        value="" if v is None else str(v), kind=n.get("type") or "",
                        x=f.get("x", 0), y=f.get("y", 0), w=f.get("width", 0), h=f.get("height", 0), path=path))
        for i, c in enumerate(n.get("children") or []):
            self._flatten(c, out, path + (i,))

    def tree(self) -> list[Node]:
        last = None
        for i in range(4):
            try:
                return self._tree_once()
            except InfraError as e:
                if "cannot load" in str(e):
                    raise
                last = e
                self.check_health()
                sleep(1.5 * (i + 1))
        raise last

    def axe(self, *args: str, input: str | None = None) -> None:
        for i in range(3):
            p = run(["axe", *args, "--udid", self.udid], timeout=45, check=False,
                    input=input, what=f"axe {args[0]}")
            if p.returncode == 0:
                return
            err = proc.tail(p.stderr or p.stdout, 3)
            if not any(s in err for s in AXE_TRANSIENT) or i == 2:
                raise InfraError(f"axe {args[0]} failed: {err}")
            self.check_health()
            sleep(1.5)

    def tap(self, x: float, y: float) -> None:
        self.axe("tap", "-x", f"{x:.1f}", "-y", f"{y:.1f}")

    def type_text(self, text: str) -> None:
        """Paste, not `axe type`: typed keys go through autocorrect even with it switched off
        ("yepitran" -> "proof run"); a paste lands verbatim, and any Unicode works."""
        simctl("pbcopy", self.udid, input=text)
        self.axe("key-combo", "--modifiers", "227", "--key", "25")

    def clear_text(self) -> None:
        self.axe("key-combo", "--modifiers", "227", "--key", "4")
        self.axe("key", "42")

    def swipe(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.axe("swipe", "--start-x", f"{x1:.0f}", "--start-y", f"{y1:.0f}", "--end-x", f"{x2:.0f}",
                 "--end-y", f"{y2:.0f}", "--duration", "0.5")

    def openurl(self, url: str) -> None:
        simctl("openurl", self.udid, url, timeout=60, what="open deep link")

    def screenshot(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        for _ in range(3):
            result = simctl("io", self.udid, "screenshot", "--type=png", str(path), timeout=60, check=False)
            if result.returncode == 0 and path.exists() and proc.valid_png(path.read_bytes()):
                return
            self.check_health()
            sleep(1)
        raise InfraError("simulator screenshot came back empty or truncated 3 times")

    def start_video(self, path: Path) -> None:
        log = self.workdir / "video.log"
        self.video = spawn(["xcrun", "simctl", "io", self.udid, "recordVideo", "--codec=h264", "--force", str(path)], log)
        deadline = time.monotonic() + 20 * proc.load_factor()
        while time.monotonic() < deadline and self.video.poll() is None:
            if "Recording started" in log.read_text(errors="replace"):
                return
            sleep(0.3)
        self.log("video recorder did not confirm it started; the run goes on")

    def stop_video(self) -> None:
        stop(self.video, grace=20)
        self.video = None
