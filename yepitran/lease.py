"""One lease per platform. flock is released by the kernel when the holder dies,
even on SIGKILL, so a crashed run can never strand the device."""
from __future__ import annotations

import fcntl
import json
import os
import time

from .proc import HOME, InfraError, sleep


class Lease:
    def __init__(self, platform: str, run_id: str):
        self.path = HOME / "locks" / f"{platform}.lock"
        self.platform, self.run_id = platform, run_id
        self.fh = None

    def holder(self) -> dict:
        try:
            return json.loads(self.path.read_text() or "{}")
        except (OSError, ValueError):
            return {}

    def acquire(self, wait: float, log) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.monotonic() + wait
        told = False
        locked = False
        try:
            while True:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    h = self.holder()
                    if time.monotonic() > deadline:
                        raise InfraError(f"{self.platform} device is busy: run {h.get('run')} (pid {h.get('pid')}) "
                                         f"has held it since {h.get('since')}; waited {wait:.0f}s") from None
                    if not told and h:  # the holder writes its name just after it locks
                        log(f"waiting for the {self.platform} device: run {h.get('run')} (pid {h.get('pid')}) holds it")
                        told = True
                    sleep(2)
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.write(json.dumps({"run": self.run_id, "pid": os.getpid(),
                                      "since": time.strftime("%Y-%m-%d %H:%M:%S")}))
            self.fh.flush()
        except BaseException:
            if locked:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_UN)
                except OSError:
                    pass
            self.fh.close()
            self.fh = None
            raise

    def release(self) -> None:
        if self.fh:
            fh, self.fh = self.fh, None
            try:
                fh.seek(0)
                fh.truncate()
            except OSError:
                # Unlocking the descriptor is more important than clearing stale
                # diagnostics from a file whose filesystem may have failed.
                pass
            finally:
                try:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    fh.close()
                except OSError:
                    pass

    def is_free(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
                return True
            except BlockingIOError:
                return False
