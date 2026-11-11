"""`yepitran pr <url>`: check out the PR head in a scratch clone, build, prove, and
print the PR comment. Posts it only with --post."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import build, report
from .cli import EXIT, USAGE, run_scenario, usage_error
from .proc import HOME, InfraError, run
from .runner import Leg

BLOCK = re.compile(r"```yepitran\s*\n(.*?)```", re.S)


def scenario_from(body: str, checkout: Path, given: Path | None) -> tuple[Path, str]:
    if not given:
        m = BLOCK.search(body or "")
        if m:
            return Path("pr-body.proof"), m.group(1)
        files = sorted((checkout / ".yepitran").glob("*.proof"))
        if not files:
            usage_error("yepitran pr: no scenario. Pass --scenario, add a ```yepitran block to the PR body, "
                        "or commit .yepitran/<name>.proof")
        given = files[0]
    try:
        return given, given.read_text()
    except (OSError, UnicodeError) as e:
        usage_error(f"yepitran pr: scenario {given}: {e}")


def prove_pr(url: str, scenario: Path | None, platform: str, post: bool) -> int:
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        print("yepitran pr: expected https://github.com/<owner>/<repo>/pull/<n>", file=sys.stderr)
        return USAGE
    owner, repo, num = m.groups()
    info = json.loads(run(["gh", "pr", "view", url, "--json", "headRefOid,body,title"], what="gh pr view").stdout)
    sha = info["headRefOid"]
    checkout = HOME / "pr" / f"{owner}-{repo}-{num}-{sha[:8]}"
    if not (checkout / ".git").exists():
        print(f"yepitran pr: cloning {owner}/{repo} at {sha[:8]} into {checkout}", file=sys.stderr)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", f"{owner}/{repo}", str(checkout), "--", "--filter=blob:none", "--no-checkout"],
            timeout=900, what="gh repo clone")
        run(["git", "-C", str(checkout), "fetch", "origin", f"pull/{num}/head"], timeout=600, what="git fetch PR")
        run(["git", "-C", str(checkout), "checkout", "--detach", sha], timeout=600, what="git checkout PR head")
    try:
        head = run(["git", "-C", str(checkout), "rev-parse", "HEAD"], what="verify PR checkout").stdout.strip()
        dirty = run(["git", "-C", str(checkout), "status", "--porcelain"], what="verify clean PR checkout").stdout
        if head != sha or dirty.strip():
            raise InfraError(f"cached checkout {checkout} is not a clean copy of PR head {sha}; "
                             "use a fresh checkout before proving this PR")
    except InfraError as e:
        print(f"yepitran pr: {e}", file=sys.stderr)
        return EXIT["INFRA FAIL"]
    if not build.is_flutter_project(checkout):
        print("yepitran pr: the repo root has no pubspec.yaml; only Flutter repos build automatically. "
              "Build it yourself and run `yepitran <artifact> <scenario>`.", file=sys.stderr)
        return USAGE
    sc_path, text = scenario_from(info.get("body", ""), checkout, scenario)
    try:
        verdict, outdir = run_scenario([str(checkout)], sc_path, platform, None, 1800, scenario_text=text)
    except InfraError as e:
        print(f"yepitran pr: {e}", file=sys.stderr)
        return EXIT["INFRA FAIL"]
    data = json.loads((outdir / "verdict.json").read_text())
    legs = [Leg(**{k: v for k, v in l.items() if k != "attempts"}) for l in data["legs"]]
    legs = [_with_attempts(l, d) for l, d in zip(legs, data["legs"])]
    body = report.markdown(verdict, data["scenario"], legs, link_root=f"{outdir}/")
    body += f"\n\nPR head `{sha[:8]}`. Video and screenshots are in `{outdir}` on the machine that ran yepitran.\n"
    comment = outdir / "pr-comment.md"
    comment.write_text(body)
    if post:
        run(["gh", "pr", "comment", url, "--body-file", str(comment)], what="gh pr comment")
        print(f"yepitran pr: posted the report on {url}", file=sys.stderr)
    else:
        print(f"\n--- dry run: this comment was NOT posted ({comment}); pass --post to post it ---\n{body}")
    return EXIT[verdict]


def _with_attempts(leg: Leg, raw: dict) -> Leg:
    from .runner import Attempt, StepResult
    leg.attempts = [Attempt(**{**a, "steps": [StepResult(**s) for s in a["steps"]]}) for a in raw["attempts"]]
    return leg
