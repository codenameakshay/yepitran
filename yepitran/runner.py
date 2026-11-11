"""Runs one scenario on one platform: lease, attempts, steps, verdict.

Verdict rules:
  PASS        some attempt ran every step.
  APP FAIL    the app did the wrong thing on a healthy device. A first app failure is
              re-run on a cleanly booted device to confirm it is not a misread.
  INFRA FAIL  every attempt broke on the device or tooling, or the run was interrupted.
An app-looking failure on a device that then fails its health check counts as infra."""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import proc, ui
from .lease import Lease
from .proc import AppError, Cancelled, InfraError, load_factor, sleep
from .scenario import Scenario, Selector, Step

PASS, APP_FAIL, INFRA_FAIL = "PASS", "APP FAIL", "INFRA FAIL"
MAX_ATTEMPTS = 3
TEXT_FIELDS = ("TextField", "SecureTextField", "EditText", "TextView", "SearchField")
_print_lock = threading.Lock()

# System UI that can cover the app. Each rule: words that identify it, optional package,
# and the button to press per `permissions:` policy.
DIALOGS = {
    "ios": [
        {"when": ("Would Like to", "Would Like Access"), "allow": ("Allow", "Allow While Using App", "Allow Full Access", "OK"),
         "deny": ("Don’t Allow", "Don't Allow")},
        {"when": ("Allow Paste",), "allow": ("Allow Paste",), "deny": ("Don’t Allow Paste", "Don't Allow Paste")},
        {"when": ("Open in “",), "allow": ("Open",), "deny": ("Open",)},
    ],
    "android": [
        {"pkg": "permissioncontroller", "allow": ("While using the app", "Allow", "Only this time"),
         "deny": ("Don’t allow", "Don't allow")},
        {"when": ("isn't responding", "isn’t responding"), "allow": ("Wait",), "deny": ("Wait",)},
        {"when": ("keeps stopping", "has stopped"), "allow": ("Close app", "OK"), "deny": ("Close app", "OK")},
    ],
}


@dataclass
class StepResult:
    line: int
    raw: str
    status: str  # pass | fail | not run
    seconds: float = 0.0
    message: str = ""
    screenshot: str = ""


@dataclass
class Attempt:
    n: int
    outcome: str = ""  # pass | app | infra | cancelled
    error: str = ""
    steps: list[StepResult] = field(default_factory=list)
    video: str = ""
    seconds: float = 0.0


@dataclass
class Leg:
    platform: str
    artifact: str
    verdict: str = INFRA_FAIL
    reason: str = ""
    app_id: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    seconds: float = 0.0


def make_driver(platform: str, log, workdir: Path):
    if platform == "ios":
        from .ios import IOS
        return IOS(log, workdir)
    from .android import Android, SimlockAndroid, backend
    return SimlockAndroid(log, workdir) if backend() == "simlock" else Android(log, workdir)


class LegRunner:
    def __init__(self, platform: str, artifact: Path, scenario: Scenario, outdir: Path, run_id: str,
                 lease_wait: float = 1800):
        self.platform, self.artifact, self.scenario = platform, artifact, scenario
        self.root = outdir / platform
        self.root.mkdir(parents=True, exist_ok=True)
        self.leg = Leg(platform=platform, artifact=str(artifact))
        self.run_id, self.lease_wait = run_id, lease_wait
        self.logfile = open(self.root / "yepitran.log", "a")
        self.steps = scenario.for_platform(platform)
        try:
            self.d = make_driver(platform, self.log, self.root)
            self.driver_error = None
        except InfraError as e:
            self.d = None
            self.driver_error = e
        except BaseException:
            self.logfile.close()
            raise

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.leg.events.append(f"{stamp} {msg}")
        self.logfile.write(f"{stamp} {msg}\n")
        self.logfile.flush()
        with _print_lock:
            print(f"[{self.platform}] {msg}", file=sys.stderr, flush=True)

    # leg ------------------------------------------------------------------

    def run(self) -> Leg:
        t0 = time.monotonic()
        if self.driver_error is not None:
            self.leg.verdict = INFRA_FAIL
            self.leg.reason = f"device setup: {self.driver_error}"
            self.leg.seconds = time.monotonic() - t0
            self.logfile.close()
            return self.leg
        lease = Lease(self.platform, self.run_id)
        try:
            lease.acquire(self.lease_wait, self.log)
        except Exception as e:
            self.leg.verdict = INFRA_FAIL
            self.leg.reason = ((str(e) or "interrupted while waiting for the device")
                               if isinstance(e, (InfraError, Cancelled))
                               else f"device lease error ({type(e).__name__}): {e}")
            self.leg.seconds = time.monotonic() - t0
            self.logfile.close()
            return self.leg
        try:
            killed = proc.reap(self.d.markers())
            if killed:
                self.log(f"killed {len(killed)} orphaned process(es) from an earlier run: "
                         + "; ".join(k.split(' ', 2)[1].rsplit('/', 1)[-1] for k in killed))
            self.attempts()
        except Cancelled:
            self.leg.verdict, self.leg.reason = INFRA_FAIL, "interrupted (Ctrl-C or SIGTERM) before a verdict"
        except Exception as e:
            self.leg.verdict, self.leg.reason = INFRA_FAIL, f"runner error ({type(e).__name__}): {e}"
        finally:
            try:
                self.d.teardown()
            except Exception as e:  # cleanup must never mask the verdict
                try:
                    self.log(f"teardown problem: {e}")
                except Exception:
                    pass
            finally:
                try:
                    lease.release()
                except Exception as e:
                    self.leg.verdict = INFRA_FAIL
                    self.leg.reason = f"could not release device lease: {e}"
                finally:
                    self.leg.seconds = time.monotonic() - t0
                    try:
                        self.log(f"verdict {self.leg.verdict}: {self.leg.reason}")
                    except Exception:
                        pass
                    self.logfile.close()
        return self.leg

    def attempts(self) -> None:
        if not self.steps:
            self.leg.verdict = INFRA_FAIL
            self.leg.reason = f"scenario has no steps for {self.platform}"
            return
        app_fails = []
        for n in range(1, MAX_ATTEMPTS + 1):
            try:
                a = self.attempt(n)
            except Cancelled:
                raise
            except Exception as e:
                a = Attempt(n=n, outcome="infra", error=f"unexpected {type(e).__name__}: {e}")
                a.steps = [StepResult(s.line, s.raw, "not run") for s in self.steps]
            self.leg.attempts.append(a)
            if a.outcome == "pass":
                self.leg.verdict = PASS
                self.leg.reason = f"all {len(self.steps)} steps passed"
                if app_fails:
                    self.leg.reason += (f"; attempt {app_fails[0].n} failed first ({app_fails[0].error}) "
                                        "and did not reproduce: possible flaky app behavior")
                return
            if a.outcome == "app":
                app_fails.append(a)
                if len(app_fails) == 2:
                    self.leg.verdict, self.leg.reason = APP_FAIL, f"{a.error} (reproduced on a clean device)"
                    return
                if n < MAX_ATTEMPTS:
                    self.log("app failure; re-running on a cleanly booted device to confirm it")
            elif n < MAX_ATTEMPTS:
                self.log(f"infra failure on attempt {n}: {a.error}; recovering and retrying")
        if app_fails:
            self.leg.verdict = INFRA_FAIL
            trail = " -> ".join(f"attempt {a.n}: {a.error}" for a in self.leg.attempts)
            self.leg.reason = (f"could not confirm the app-looking failure on a healthy retry; "
                               f"not blaming the app. {trail}")
        else:
            tried = " -> ".join(f"attempt {a.n}: {a.error}" for a in self.leg.attempts)
            self.leg.verdict, self.leg.reason = INFRA_FAIL, f"the device or tooling broke on every attempt. {tried}"

    def attempt(self, n: int) -> Attempt:
        a = Attempt(n=n)
        adir = self.root / f"attempt-{n}"
        adir.mkdir(parents=True, exist_ok=True)
        self.d.workdir = adir
        t0 = time.monotonic()
        how = {1: "clean boot", 2: "cold boot", 3: "wiped device"}[n]
        self.log(f"attempt {n}/{MAX_ATTEMPTS} ({how}, load factor {load_factor():.1f}x)")
        try:
            self.d.prepare(self.artifact, self.scenario, level=n)
            self.leg.app_id = self.d.app["id"]
        except (InfraError, AppError) as e:
            a.outcome = "app" if isinstance(e, AppError) else "infra"
            a.error = f"setup: {e}"
            a.steps = [StepResult(s.line, s.raw, "not run") for s in self.steps]
            a.seconds = time.monotonic() - t0
            return a
        video = adir / "video.mp4"
        previous_video = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in
                          (video, video.with_suffix(".webm")) if p.exists()}
        video_started = False
        try:
            self.d.start_video(video)
            video_started = True
        except InfraError as e:
            self.log(f"video did not start ({e}); steps run without it")
        try:
            self.run_steps(a, adir)
        finally:
            try:
                self.d.stop_video()
            except Cancelled:
                raise
            except Exception as e:
                self.log(f"video stop problem: {e}")
            for v in (video, video.with_suffix(".webm")) if video_started else ():
                current = (v.stat().st_mtime_ns, v.stat().st_size) if v.exists() else None
                if current and current[1] > 0 and current != previous_video.get(v):
                    a.video = str(v.relative_to(self.root.parent))
            a.seconds = time.monotonic() - t0
        return a

    def run_steps(self, a: Attempt, adir: Path) -> None:
        for i, step in enumerate(self.steps):
            t = time.monotonic()
            r = StepResult(step.line, step.raw, "pass")
            a.steps.append(r)
            try:
                shot = self.step(step, i, adir)
                self.d.check_health()
                self.check_app_foreground(f"after {step.raw}")
                r.screenshot = str(shot.relative_to(self.root.parent)) if shot else ""
                r.seconds = time.monotonic() - t
                self.log(f"  ok   {step.raw}  ({r.seconds:.1f}s)")
                continue
            except AppError as e:
                try:
                    self.d.check_health()
                    a.outcome, r.message = "app", str(e)
                    try:
                        self.check_app_foreground(f"after {step.raw} failed")
                    except AppError as app_error:
                        r.message = str(app_error)
                except InfraError as ie:
                    a.outcome, r.message = "infra", f"{ie} (the step had reported: {e})"
            except InfraError as e:
                a.outcome, r.message = "infra", str(e)
            except Cancelled:
                raise
            except Exception as e:
                a.outcome, r.message = "infra", f"unexpected {type(e).__name__}: {e}"
            r.status, r.seconds, a.error = "fail", time.monotonic() - t, f"line {step.line} `{step.raw}`: {r.message}"
            self.log(f"  FAIL {step.raw}: {r.message}")
            shot = adir / f"{i + 1:02d}-failure.png"
            try:
                self.d.screenshot(shot)
                r.screenshot = str(shot.relative_to(self.root.parent))
            except (InfraError, AppError):
                pass
            try:
                nodes = self.d.tree()
                (adir / f"{i + 1:02d}-failure.tree.txt").write_text("\n".join(
                    f"{'  ' * (len(n.path) - 1)}{n.kind} label={n.label!r} id={n.id!r} value={n.value!r} "
                    f"frame=({n.x:.0f},{n.y:.0f},{n.w:.0f},{n.h:.0f})" for n in nodes))
            except (InfraError, AppError):
                pass
            if a.outcome == "app":
                try:
                    self.d.crash_log()  # saves logcat on Android for the report folder
                except InfraError:
                    pass
            a.steps += [StepResult(s.line, s.raw, "not run") for s in self.steps[i + 1:]]
            return
        a.outcome = "pass"

    # steps ------------------------------------------------------------------

    def step(self, s: Step, i: int, adir: Path) -> Path | None:
        if s.kind == "launch":
            self.d.launch()
            self.wait_for_app()
        elif s.kind == "tap":
            n, nodes = self.find(s.target, s.timeout, tappable=True, with_tree=True)
            many = ui.count(nodes, s.target, self.d.screen)
            if many > 1 and s.target.nth == 1:
                self.log(f"  note: {many} elements match {s.target}; tapped {n.describe()} at y={n.y:.0f}. "
                         f"Add #2..#{many} to pick another")
            self.d.tap(*ui.tap_point(n, nodes, self.d.screen))
        elif s.kind == "type":
            self.type(s)
        elif s.kind == "scroll":
            self.scroll(s)
        elif s.kind == "deeplink":
            self.deeplink(s.text)
        elif s.kind == "wait":
            self.find(s.target, s.timeout)
        elif s.kind == "assert" and s.negate:
            self.gone(s.target, s.timeout)
        elif s.kind == "assert":
            self.find(s.target, s.timeout, verb="expected to see")
        elif s.kind == "screenshot":
            self.settle()
            shot = adir / f"{i + 1:02d}-{s.text}.png"
            self.d.screenshot(shot)
            return shot
        return None

    def budget(self, timeout: float | None) -> float:
        return (self.scenario.timeout if timeout is None else timeout) * load_factor()

    def read(self) -> list[ui.Node]:
        """Tree with any known system dialog answered first."""
        for _ in range(5):
            nodes = self.d.tree()
            if not self.dismiss_dialog(nodes):
                return nodes
            sleep(1)
        return nodes

    def dismiss_dialog(self, nodes: list[ui.Node]) -> bool:
        policy = self.scenario.permissions
        for rule in DIALOGS[self.platform]:
            if "pkg" in rule:
                present = any(rule["pkg"] in n.pkg for n in nodes)
            else:
                present = any(w in t for n in nodes for t in n.texts() for w in rule["when"])
            if not present:
                continue
            for want in rule[policy]:
                for n in nodes:
                    if want in n.texts() and n.on_screen(self.d.screen) and (not rule.get("pkg") or rule["pkg"] in n.pkg):
                        context = next((t for m in nodes for t in m.texts()
                                        for w in rule.get("when", ()) if w in t), rule.get("pkg", ""))
                        self.log(f"system dialog ({context[:80]}): pressed {want!r}")
                        self.d.tap(*n.center)
                        return True
        return False

    def find(self, sel: Selector, timeout: float | None, verb: str = "waited for", tappable: bool = False,
             with_tree: bool = False):
        budget = self.budget(timeout)
        deadline = time.monotonic() + budget
        while True:
            nodes = self.read()
            hit = ui.find(self.app_tree_nodes(nodes), sel, self.d.screen, tappable)
            if hit:
                return (hit, nodes) if with_tree else hit
            if time.monotonic() > deadline:
                self.diagnose(nodes, f"{verb} {sel} for {budget:.0f}s; it never appeared")
            sleep(0.5)

    def gone(self, sel: Selector, timeout: float | None) -> None:
        budget = self.budget(timeout)
        deadline = time.monotonic() + budget
        while True:
            nodes = self.read()
            self.check_app_foreground(f"while checking whether {sel} is gone")
            if not self.app_nodes(nodes):
                self.diagnose(nodes, f"checking that {sel} is gone")
            hit = ui.find(self.app_tree_nodes(nodes), sel, self.d.screen)
            if not hit:
                return
            if time.monotonic() > deadline:
                raise AppError(f"{sel} is still on screen after {budget:.0f}s ({hit.describe()})")
            sleep(0.5)

    def app_nodes(self, nodes: list[ui.Node]) -> list[ui.Node]:
        labelled = [n for n in nodes if n.label or n.value or n.id]
        if self.platform == "android":
            return [n for n in labelled if n.pkg == self.d.app["id"]]
        return [n for n in labelled if n.kind.lower() != "application"]

    def app_tree_nodes(self, nodes: list[ui.Node]) -> list[ui.Node]:
        if self.platform == "android":
            app_id = self.d.app.get("id", "")
            return [n for n in nodes if n.pkg == app_id]
        return nodes

    def check_app_foreground(self, context: str) -> None:
        state = self.d.app_state()
        for _ in range(3):  # activity transitions leave window focus empty for a moment
            if state in ("foreground", "dead", "anr"):
                break
            sleep(1)
            state = self.d.app_state()
        if state == "dead":
            try:
                crash = self.d.crash_log()
            except InfraError:
                crash = None
            raise AppError(f"the app is not running (crashed or quit) {context}. "
                           f"{crash or 'No crash report found.'}")
        if state == "anr":
            raise AppError(f"the app is not responding (ANR) {context}")
        if state != "foreground":
            raise InfraError(f"cannot verify the app is in front {context}: {state}")

    def diagnose(self, nodes: list[ui.Node], msg: str) -> None:
        """Decide whose fault a miss is, then raise."""
        state = self.d.app_state()
        if state == "dead":
            try:
                crash = self.d.crash_log()
            except InfraError:
                crash = None
            raise AppError(f"{msg}: the app is not running (crashed or quit). {crash or 'No crash report found.'}")
        if state == "anr":
            raise AppError(f"{msg}: the app is not responding (ANR) even after pressing Wait")
        if state.startswith("background"):
            raise InfraError(f"{msg}: the app is not in front, {state}")
        if state != "foreground":
            raise InfraError(f"{msg}: app state is unknown ({state}); cannot safely classify this as an app failure")
        if not self.app_nodes(nodes):
            raise InfraError(f"{msg}: the accessibility tree shows nothing from the app (reader problem, "
                             "or the app exposes no accessibility labels)")
        raise AppError(f"{msg}. On screen: {ui.visible_summary(nodes, self.d.screen)}")

    def wait_for_app(self) -> None:
        budget = self.budget(60)
        deadline = time.monotonic() + budget
        while True:
            nodes = self.read()
            if self.app_nodes(nodes):
                self.check_app_foreground("after launch")
                return
            if time.monotonic() > deadline:
                self.diagnose(nodes, f"launched the app and waited {budget:.0f}s for its first screen")
            sleep(1)

    def settle(self) -> None:
        """Wait for two identical trees in a row, so screenshots are not mid-animation."""
        last = None
        for _ in range(6):
            sig = ui.signature(self.read())
            if sig == last:
                return
            last = sig
            sleep(0.4)

    def type(self, s: Step) -> None:
        point = None
        if s.target:
            n, nodes = self.find(s.target, s.timeout, tappable=True, with_tree=True)
            point = ui.tap_point(n, nodes, self.d.screen)
            self.d.tap(*point)
            sleep(0.6)
        shown = ""
        for attempt in range(3):
            self.d.type_text(s.text)
            if point is None:
                return
            deadline = time.monotonic() + self.budget(5)
            while True:  # a paste can wait behind an "Allow Paste" prompt that read() answers
                field_ = self.field_at(self.read(), point)
                # Flutter on iOS reports a text field's text as its label, Android as its value.
                shown = (field_.value or field_.label) if field_ else ""
                if field_ is None or set(shown) <= set("•●*"):
                    return  # nothing readable to check against (secure or unexposed field)
                if ui.text_landed(s.text, shown) or time.monotonic() > deadline:
                    break
                sleep(0.5)
            if ui.text_landed(s.text, shown):
                return
            self.log(f'typed "{s.text}" but the field shows "{shown}"; clearing and retyping')
            self.d.tap(*point)
            self.d.clear_text()
        raise InfraError(f'typed "{s.text}" 3 times but the field shows "{shown}" '
                         "(the keyboard dropped or rewrote input)")

    def field_at(self, nodes: list[ui.Node], point: tuple[float, float]) -> ui.Node | None:
        x, y = point
        hits = [n for n in nodes if n.kind in TEXT_FIELDS and n.x <= x <= n.x + n.w and n.y <= y <= n.y + n.h]
        return min(hits, key=lambda n: n.w * n.h) if hits else None

    def scroll(self, s: Step) -> None:
        w, h = self.d.screen
        y1, y2 = (h * 0.70, h * 0.35) if s.text == "down" else (h * 0.35, h * 0.70)
        if not s.target:
            self.d.swipe(w / 2, y1, w / 2, y2)
            return
        last, still = None, 0
        for _ in range(25):
            nodes = self.read()
            hit = ui.find(self.app_tree_nodes(nodes), s.target, self.d.screen)
            if hit and hit.y + hit.h <= h * 0.9 and hit.y >= h * 0.08:
                return
            sig = ui.signature(nodes)
            still = still + 1 if sig == last else 0
            if still >= 2:
                self.diagnose(nodes, f"scrolled {s.text} to the end; {s.target} is not there")
            last = sig
            self.d.swipe(w / 2, y1, w / 2, y2)
            sleep(0.4)
        self.diagnose(self.read(), f"scrolled {s.text} 25 times; {s.target} never came on screen")

    def deeplink(self, url: str) -> None:
        for attempt in range(3):
            self.d.openurl(url)
            deadline = time.monotonic() + self.budget(15)
            state = ""
            while time.monotonic() < deadline:
                self.read()
                state = self.d.app_state()
                if state == "foreground":
                    return
                sleep(1)
            self.log(f"deep link did not bring the app to the front ({state}); launching warm and resending")
            self.d.launch()
            self.wait_for_app()
        raise InfraError(f"deep link {url} did not reach the app in 3 tries (last: {state})")
