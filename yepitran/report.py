"""Report folder: verdict.json for agents, report.md for PR comments, report.html for people."""
from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from .runner import APP_FAIL, INFRA_FAIL, PASS, Leg

ICON = {"pass": "✅", "fail": "❌", "not run": "·"}
COLOR = {PASS: "#1a7f37", APP_FAIL: "#cf222e", INFRA_FAIL: "#9a6700"}
MEANING = {
    PASS: "Every step passed on a real device.",
    APP_FAIL: "The app did the wrong thing on a healthy device. Fix the app.",
    INFRA_FAIL: "The device or tooling broke; this says nothing about the app. Rerun, or run `yepitran doctor`.",
}


def overall(legs: list[Leg]) -> str:
    if not legs:
        return INFRA_FAIL
    verdicts = {l.verdict for l in legs}
    return APP_FAIL if APP_FAIL in verdicts else INFRA_FAIL if INFRA_FAIL in verdicts else PASS


def write(outdir: Path, scenario_name: str, legs: list[Leg]) -> str:
    verdict = overall(legs)
    (outdir / "verdict.json").write_text(json.dumps(
        {"verdict": verdict, "scenario": scenario_name, "legs": [asdict(l) for l in legs]}, indent=2))
    (outdir / "report.md").write_text(markdown(verdict, scenario_name, legs))
    (outdir / "report.html").write_text(page(verdict, scenario_name, legs))
    return verdict


def final_attempt(leg: Leg):
    return leg.attempts[-1] if leg.attempts else None


def markdown(verdict: str, name: str, legs: list[Leg], link_root: str = "") -> str:
    out = [f"## yepitran: {verdict}", "", f"Scenario `{name}`. {MEANING[verdict]}", ""]
    for leg in legs:
        a = final_attempt(leg)
        out += [f"### {leg.platform}: {leg.verdict}", "", leg.reason, ""]
        if leg.app_id:
            out.append(f"App `{leg.app_id}`, {len(leg.attempts)} attempt(s), {leg.seconds:.0f}s.")
        if a:
            if a.video:
                out.append(f"Video: [{a.video}]({link_root}{a.video})")
            out += ["", "| | Step | Time | Note |", "|---|---|---|---|"]
            for s in a.steps:
                note = s.message.replace("|", "\\|").replace("\n", " ")
                if s.screenshot:
                    note = (note + " " if note else "") + f"[screenshot]({link_root}{s.screenshot})"
                out.append(f"| {ICON[s.status]} | `{s.raw}` | {s.seconds:.1f}s | {note} |")
        recov = [e for e in leg.events if not e.split(" ", 1)[1].startswith(("  ok", "  FAIL"))]
        if recov:
            out += ["", "<details><summary>What yepitran did</summary>", "", "```"] + recov + ["```", "</details>"]
        out.append("")
    return "\n".join(out)


def page(verdict: str, name: str, legs: list[Leg]) -> str:
    e = html.escape
    parts = []
    for leg in legs:
        attempts = []
        for a in leg.attempts:
            rows = "".join(
                f"<tr class='{s.status.replace(' ', '')}'><td>{ICON[s.status]}</td><td><code>{e(s.raw)}</code></td>"
                f"<td>{s.seconds:.1f}s</td><td>{e(s.message)}"
                + (f"<br><a href='{e(s.screenshot)}'><img src='{e(s.screenshot)}' loading='lazy'></a>" if s.screenshot else "")
                + "</td></tr>" for s in a.steps)
            video = f"<video controls preload='metadata' src='{e(a.video)}'></video>" if a.video else "<p>No video.</p>"
            open_ = " open" if a is leg.attempts[-1] else ""
            attempts.append(
                f"<details{open_}><summary>Attempt {a.n}: {e(a.outcome or 'did not finish')} "
                f"({a.seconds:.0f}s){(' : ' + e(a.error)) if a.error else ''}</summary>"
                f"<div class='att'>{video}<table>{rows}</table></div></details>")
        events = e("\n".join(leg.events))
        parts.append(
            f"<section><h2>{e(leg.platform)} <span class='v' style='background:{COLOR[leg.verdict]}'>{e(leg.verdict)}</span></h2>"
            f"<p>{e(leg.reason)}</p><p class='muted'>{e(leg.app_id)} · {e(leg.artifact)} · {leg.seconds:.0f}s</p>"
            + "".join(attempts)
            + f"<details><summary>Log</summary><pre>{events}</pre></details></section>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>yepitran {e(verdict)}</title><style>
:root{{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--fail:#ffebe9}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--fail:#3c1618}}}}
body{{font:15px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:1100px;margin:0 auto;padding:16px}}
h1 .v,h2 .v{{color:#fff;padding:2px 10px;border-radius:6px;font-size:.8em}}
.muted{{color:var(--muted)}} table{{border-collapse:collapse;width:100%;margin-top:8px}}
td{{border-top:1px solid var(--line);padding:6px;vertical-align:top}} tr.fail td{{background:var(--fail)}}
tr.notrun td{{color:var(--muted)}} img{{max-height:320px;max-width:100%;margin-top:6px;border:1px solid var(--line);border-radius:6px}}
video{{max-height:520px;max-width:100%;border-radius:8px}} pre{{white-space:pre-wrap;font-size:12px}}
summary{{cursor:pointer;margin:8px 0}} section{{border-top:2px solid var(--line);margin-top:16px}}
</style></head><body>
<h1>yepitran <span class="v" style="background:{COLOR[verdict]}">{e(verdict)}</span></h1>
<p>Scenario <code>{e(name)}</code>. {e(MEANING[verdict])}</p>
{''.join(parts)}
</body></html>"""
