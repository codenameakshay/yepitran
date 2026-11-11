"""Process plumbing shared by every driver: the two failure kinds, subprocess calls
scaled to machine load, long-lived children, and cancellation."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

HOME = Path(os.environ.get("YEPITRAN_HOME", Path.home() / ".yepitran"))
cancel = threading.Event()


class InfraError(Exception):
    """The device or the tooling broke. Says nothing about the app."""


class AppError(Exception):
    """The app under test did the wrong thing."""


class Cancelled(Exception):
    pass


def load_factor() -> float:
    """Timeouts stretch with load: a Mac at load 90 on 18 cores is 5x slower at everything."""
    return min(5.0, max(1.0, os.getloadavg()[0] / (os.cpu_count() or 8)))


def sleep(seconds: float) -> None:
    if cancel.wait(seconds):
        raise Cancelled()


def tail(text: str, n: int = 6) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def run(cmd: list[str], timeout: float = 60, check: bool = True, env: dict | None = None,
        input=None, what: str | None = None, binary: bool = False, cwd=None,
        cleanup: bool = False) -> subprocess.CompletedProcess:
    """Run a command, interrupting it promptly when the run is cancelled.

    Teardown commands may pass ``cleanup=True`` so cancellation does not prevent
    them from shutting down resources acquired by this run.
    """
    if cancel.is_set() and not cleanup:
        raise Cancelled()
    what = what or " ".join(cmd[:3])
    limit = timeout * load_factor()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                             cwd=cwd, env={**os.environ, **env} if env else None, text=not binary,
                             start_new_session=True)
        deadline = time.monotonic() + limit
        pending_input = input
        while True:
            if cancel.is_set() and not cleanup:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = p.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = p.communicate()
                # communicate can finish when only the session leader exits;
                # descendants may have detached their stdio but remain in-group.
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, limit)
            try:
                stdout, stderr = p.communicate(input=pending_input, timeout=min(.25, remaining))
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        p = subprocess.CompletedProcess(cmd, p.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if 'p' in locals():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise InfraError(f"{what}: no answer after {limit:.0f}s") from None
    except FileNotFoundError:
        raise InfraError(f"{cmd[0]} is not installed; run `yepitran doctor`") from None
    if cancel.is_set() and not cleanup:
        raise Cancelled()
    if check and p.returncode != 0:
        out = p.stderr or p.stdout
        if binary:
            out = out.decode(errors="replace")
        raise InfraError(f"{what}: exit {p.returncode}: {tail(out)}")
    return p


def spawn(cmd: list[str], log: Path, env: dict | None = None) -> subprocess.Popen:
    """A long-lived child in its own session, so Ctrl-C reaches us first and we stop it in order."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        return subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, **env} if env else None, start_new_session=True)


def stop(p: subprocess.Popen | None, sig: int = signal.SIGINT, grace: float = 15) -> None:
    """Signal the child's whole session: the emulator launcher forks qemu, which outlives a
    signal sent to the launcher alone."""
    if p is None:
        return
    if getattr(p, "_yepitran_stopped", False):
        return
    try:
        os.killpg(p.pid, sig)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(p.pid, 0)
        except ProcessLookupError:
            break
        if p.poll() is None:
            try:
                p.wait(min(.1, max(0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(.05)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    p._yepitran_stopped = True


def processes() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-ww", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), cmd.strip()))
    return rows


REAPABLE = ("axe", "simctl", "xcrun", "qemu-system", "emulator", "adb", "ffmpeg", "crashpad_handler")


def reap(markers: list[str]) -> list[str]:
    """Kill leftovers of a dead run. Only called while holding the device lease, so any
    tool process that names our device is ours by construction."""
    me = os.getpid()
    killed = []
    for pid, cmd in processes():
        argv = cmd.split()
        argv0 = os.path.basename(argv[0]) if argv else ""
        if pid == me or not is_reapable(argv0):
            continue
        # Marker substrings in shell wrappers, logs, URLs, and unrelated options
        # are not process ownership. Match device selectors/AVD args as tokens.
        if any(_marker_matches(argv, marker, argv0) for marker in markers):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(f"{pid} {cmd[:120]}")
            except ProcessLookupError:
                pass
    return killed


def _marker_matches(argv: list[str], marker: str, argv0: str = "") -> bool:
    if marker.startswith("-avd "):
        return (argv0 == "emulator" or argv0.startswith("qemu-system-")) and any(
            argv[i] == "-avd" and argv[i + 1] == marker[5:] for i in range(len(argv) - 1))
    if marker.startswith("-ports "):
        return (argv0 == "emulator" or argv0.startswith("qemu-system-")) and any(
            argv[i] == "-ports" and argv[i + 1] == marker[7:] for i in range(len(argv) - 1))
    if marker.startswith("tcp:"):
        # Deliberately do not match ADB from command lines alone: even a private
        # port can be owned by another tool/session, so a persisted owner PID is
        # required before cleanup is safe.
        return False
    if argv0 == "axe":
        return any(argv[i] in ("--udid", "-u") and argv[i + 1] == marker
                   for i in range(len(argv) - 1))
    if argv0 == "simctl":
        return len(argv) > 2 and argv[2] == marker
    if argv0 == "xcrun":
        return len(argv) > 3 and argv[1] == "simctl" and argv[3] == marker
    return False


def is_reapable(argv0: str) -> bool:
    return argv0 in REAPABLE or argv0.startswith("qemu-system-")


def valid_png(data: bytes) -> bool:
    """Screencaps over a flaky transport arrive truncated: right header, no IEND."""
    return data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in data[-16:]


def wait_until(predicate, timeout: float, what: str, every: float = 1.0):
    deadline = time.monotonic() + timeout * load_factor()
    last = None
    while True:
        try:
            value = predicate()
            if value:
                return value
        except InfraError as e:
            last = e
        if time.monotonic() > deadline:
            raise InfraError(f"{what} did not happen in {timeout * load_factor():.0f}s"
                             + (f" (last error: {last})" if last else ""))
        sleep(every)
