"""Simlock gateway client: lease a fleet device, renew it, run adb on it, give it back.

Simlock (https://github.com/callstackincubator/simlock) shares simulators and emulators between
agents. The `simlock` CLI only reaches this machine's own daemon, so yepitran talks to the
gateway's HTTP API with an agent token of its own (`~/.yepitran/simlock.json` or the
YEPITRAN_SIMLOCK_URL / YEPITRAN_SIMLOCK_TOKEN variables)."""
from __future__ import annotations

import http.client
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

from . import proc
from .proc import InfraError

CONFIG = proc.HOME / "simlock.json"
TTL_MS = 10 * 60 * 1000  # a run killed with -9 frees the device within 10 minutes


def config() -> dict | None:
    url, token = os.environ.get("YEPITRAN_SIMLOCK_URL"), os.environ.get("YEPITRAN_SIMLOCK_TOKEN")
    try:
        if not (url and token) and CONFIG.exists():
            c = json.loads(CONFIG.read_text())
            url, token = url or c["url"], token or c["token"]
        if not isinstance(url, (str, type(None))) or not isinstance(token, (str, type(None))):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise InfraError("simlock configuration is unreadable or invalid; it needs a \"url\" and a \"token\" string") from None
    return {"url": url.rstrip("/"), "token": token} if url and token else None


def interruptible(fn):
    """urllib cannot be interrupted, so the call runs on a thread and Ctrl-C returns at once.
    ponytail: the abandoned call still finishes in the background; a lease it grants late
    expires after TTL_MS, or the next run gives it back first."""
    if proc.cancel.is_set():
        raise proc.Cancelled()
    box: queue.Queue = queue.Queue(1)

    def run() -> None:
        try:
            box.put((True, fn()))
        except BaseException as e:
            box.put((False, e))

    threading.Thread(target=run, daemon=True).start()
    while True:
        try:
            ok, value = box.get(timeout=0.2)
        except queue.Empty:
            if proc.cancel.is_set():
                raise proc.Cancelled() from None
            continue
        if proc.cancel.is_set():
            raise proc.Cancelled()
        if ok:
            return value
        raise value


class Error(InfraError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"simlock {code}: {message}")
        self.status, self.code = status, code


class Client:
    def __init__(self, cfg: dict):
        self.url, self.token = cfg["url"], cfg["token"]

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 90):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", **({"Content-Type": "application/json"} if data else {})})
        try:
            return urllib.request.urlopen(req, timeout=timeout * proc.load_factor())
        except urllib.error.HTTPError as e:
            # Only a Simlock error body proves Simlock answered before running anything.
            # A proxy's 502 page may come after the command ran, so it is an unknown outcome.
            try:
                err = json.loads(e.read() or b"{}")["error"]
                code, message = err["code"], str(err.get("message", ""))
                if not isinstance(code, str) or not code:
                    raise ValueError
            except (ValueError, KeyError, TypeError, OSError, http.client.HTTPException):
                raise InfraError(f"simlock gateway {self.url} answered HTTP {e.code} with no Simlock error; "
                                 "the outcome may be unknown") from None
            raise Error(e.code, code, message) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise InfraError(f"simlock gateway {self.url} not reachable ({getattr(e, 'reason', e)})") from None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 90,
             cleanup: bool = False) -> dict:
        """cleanup=True runs even after Ctrl-C (releases, cancels)."""
        def go() -> dict:
            try:
                with self.request(method, path, body, timeout) as r:
                    raw = r.read()
                return json.loads(raw) if raw else {}
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise InfraError(f"simlock gateway {self.url} answer broke ({e})") from None
        return go() if cleanup else interruptible(go)

    # leases ---------------------------------------------------------------------

    def my_leases(self) -> list[dict]:
        return self.call("GET", "/v1/leases").get("leases", [])

    def lease(self, platform: str, device: str, log, wait: float = 1800) -> dict:
        req = self.call("POST", "/v1/lease-requests", {"platform": platform, "device": device, "ttlMs": TTL_MS,
                                                       "timeoutMs": int(wait * 1000)})["request"]
        last, deadline = "", time.monotonic() + wait + 60
        try:
            while req["state"] not in ("granted", "failed", "cancelled"):
                if proc.cancel.is_set():
                    raise proc.Cancelled()
                note = req["state"] + (f" (queue position {req['queuePosition']})" if req.get("queuePosition") else "")
                if note != last:
                    log(f"simlock: {note}")
                    last = note
                if time.monotonic() > deadline:
                    raise InfraError(f"simlock gave no {device} in {wait:.0f}s (last state: {req['state']})")
                req = self.call("GET", f"/v1/lease-requests/{req['id']}?wait=10", timeout=40)["request"]
        except BaseException:
            self.cancel_request(req)
            raise
        if req["state"] != "granted":
            err = req.get("error") or {}
            raise InfraError(f"simlock could not lease a {device}: {err.get('code', req['state'])} {err.get('message', '')}".strip())
        return req["lease"]

    def cancel_request(self, req: dict) -> None:
        try:
            if req.get("lease"):
                self.release(req["lease"]["id"])
                return
            self.call("DELETE", f"/v1/lease-requests/{req['id']}", timeout=10, cleanup=True)
        except Error as e:
            if e.code != "REQUEST_NOT_CANCELLABLE":
                return
            # Too late to cancel: the device is booting or already granted. Wait briefly for the
            # grant and give it back, so it is not held until its TTL runs out.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    cur = self.call("GET", f"/v1/lease-requests/{req['id']}?wait=5", timeout=15, cleanup=True)["request"]
                    if cur.get("lease"):
                        self.release(cur["lease"]["id"])
                        return
                except (InfraError, KeyError):
                    return
                if cur.get("state") in ("failed", "cancelled"):
                    return
        except InfraError:
            pass  # unreachable: the lease expires on its TTL, or the next run gives it back

    def renew(self, lease_id: str) -> dict:
        return self.call("POST", f"/v1/leases/{lease_id}/renew", timeout=30, cleanup=True)

    def release(self, lease_id: str) -> None:
        """Retries through a worker blip, so Ctrl-C does not leave the device held until its TTL."""
        for i in range(4):
            try:
                self.call("DELETE", f"/v1/leases/{lease_id}", timeout=30, cleanup=True)
                return
            except Error as e:
                if e.code == "UNKNOWN_LEASE":
                    return
                if e.code != "WORKER_UNREACHABLE" or i == 3:
                    raise
            except InfraError:
                if i == 3:
                    raise
            time.sleep(3)  # not proc.sleep: this runs during teardown, after Ctrl-C

    # exec -----------------------------------------------------------------------

    def exec(self, lease_id: str, tool: str, args: list[str], stdin: str | None = None,
             timeout: float = 60, cleanup: bool = False) -> tuple[int, str, str]:
        """Run one adb/simctl command on the worker that holds the device. (exit, stdout, stderr).
        Raises Error before the command ran (safe to resend) and InfraError when the stream
        broke after it started (completion unknown)."""
        body = {"tool": tool, "args": args, **({"stdin": stdin} if stdin is not None else {})}
        go = lambda: self._exec(lease_id, body, tool, args, timeout)  # noqa: E731
        return go() if cleanup else interruptible(go)

    def _exec(self, lease_id: str, body: dict, tool: str, args: list[str], timeout: float) -> tuple[int, str, str]:
        out, err, event = [], [], ""
        with self.request("POST", f"/v1/leases/{lease_id}/exec", body, timeout=timeout) as r:
            try:
                for raw in r:
                    line = raw.decode().rstrip("\n").rstrip("\r")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        if event == "output":
                            (out if data.get("stream") == "stdout" else err).append(data.get("chunk", ""))
                        elif event == "exit":
                            return data.get("exitCode", 1), "".join(out), "".join(err)
                        elif event == "error":
                            raise InfraError(f"simlock exec {args[:2]}: {data.get('code')} {data.get('message', '')}")
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise InfraError(f"simlock exec stream broke during {tool} {' '.join(args)[:60]} ({e})") from None
        raise InfraError(f"simlock exec stream ended without an exit code ({tool} {' '.join(args)[:60]})")


class Renewer:
    """Renews a lease every TTL/4. `lost` is set when simlock says the lease is gone."""

    def __init__(self, client: Client, lease_id: str, log):
        self.client, self.lease_id, self.log = client, lease_id, log
        self.stop_event, self.lost = threading.Event(), ""
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self) -> None:
        while not self.stop_event.wait(TTL_MS / 4000):
            try:
                for n in self.client.renew(self.lease_id).get("notices", []):
                    self.log(f"simlock notice: {n}")
            except Error as e:
                if e.code == "UNKNOWN_LEASE":
                    self.lost = "simlock ended the lease (expired or taken back)"
                    return
                self.log(f"lease renew failed ({e}); retrying")
            except InfraError as e:
                self.log(f"lease renew failed ({e}); retrying")

    def stop(self) -> None:
        self.stop_event.set()
