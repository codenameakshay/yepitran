#!/usr/bin/env python3
"""Run one scenario N times per platform and report the pass rate.

    scripts/soak.py <ios .app> <android .apk> <scenario> [-n 10] [--platform ios|android|both]

Platforms run side by side (one device each); runs on a platform go one after another."""
import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("app")
ap.add_argument("apk")
ap.add_argument("scenario")
ap.add_argument("-n", type=int, default=10)
ap.add_argument("--platform", choices=["ios", "android", "both"], default="both")
ap.add_argument("--out", type=Path, default=Path("yepitran-out") / time.strftime("soak-%Y%m%d-%H%M%S"))
a = ap.parse_args()
a.out.mkdir(parents=True, exist_ok=True)
results = {}


def soak(platform: str, artifact: str) -> None:
    rows = results.setdefault(platform, [])
    for i in range(1, a.n + 1):
        out = a.out / f"{platform}-{i:02d}"
        t = time.monotonic()
        subprocess.run([str(ROOT / "bin/yepitran"), artifact, a.scenario, "--out", str(out)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        v = json.loads((out / "verdict.json").read_text())
        leg = v["legs"][0]
        rows.append({"run": i, "verdict": v["verdict"], "attempts": len(leg["attempts"]),
                     "seconds": round(time.monotonic() - t), "reason": leg["reason"][:160], "report": str(out)})
        print(f"{platform} {i:2d}/{a.n}: {v['verdict']:10s} attempts={len(leg['attempts'])} "
              f"{time.monotonic() - t:4.0f}s  {leg['reason'][:100]}", flush=True)


jobs = {"ios": a.app, "android": a.apk}
platforms = ["ios", "android"] if a.platform == "both" else [a.platform]
threads = [threading.Thread(target=soak, args=(p, jobs[p])) for p in platforms]
for t in threads:
    t.start()
for t in threads:
    t.join()

print()
for p in platforms:
    rows = results[p]
    passes = sum(r["verdict"] == "PASS" for r in rows)
    infra = sum(r["verdict"] == "INFRA FAIL" for r in rows)
    retried = sum(r["attempts"] > 1 for r in rows)
    print(f"{p}: {passes}/{len(rows)} PASS, {infra} INFRA FAIL, {retried} needed a retry, "
          f"median {sorted(r['seconds'] for r in rows)[len(rows) // 2]}s")
(a.out / "soak.json").write_text(json.dumps(results, indent=2))
print(f"details: {a.out / 'soak.json'}")
sys.exit(0 if all(len(rows) == a.n and all(r["verdict"] == "PASS" for r in rows) for rows in results.values()) else 1)
