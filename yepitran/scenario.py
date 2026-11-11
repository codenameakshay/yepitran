"""Scenario file format: one step per line, read top to bottom.

    # comment
    app: com.example.app          header lines are `key: value`
    permissions: allow            allow | deny  (system permission prompts)
    reverse: 3000 8080            Android: keep `adb reverse tcp:N tcp:N` alive
    timeout: 20s                  default wait for `wait` / `assert visible`

    launch
    wait "Welcome" 30s
    tap "Sign in"                 exact text (or one line of a multi-line label)
    tap id=login_button           accessibility id / resource-id
    tap has="Sign"                substring
    tap "Add task" #2             the 2nd match, counting top to bottom
    type "hello" into "Name"
    scroll down until "Item 40"
    deeplink myapp://settings
    assert visible "Saved"
    assert gone "Spinner"
    screenshot after-save
    ios: tap "Done"               platform-only step
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field, replace

KINDS = ("launch", "tap", "type", "scroll", "deeplink", "wait", "assert", "screenshot")
HEADERS = ("app", "permissions", "reverse", "timeout")
PLATFORMS = ("ios", "android")


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Selector:
    by: str  # text | id | has
    value: str
    nth: int = 1
    explicit_nth: bool = False

    def __str__(self) -> str:
        base = f'"{self.value}"' if self.by == "text" else f'{self.by}="{self.value}"'
        return base + (f" #{self.nth}" if self.nth > 1 else "")


@dataclass(frozen=True)
class Step:
    line: int
    raw: str
    kind: str
    platform: str | None = None
    target: Selector | None = None
    text: str | None = None  # type text, deeplink url, screenshot name, scroll direction
    timeout: float | None = None
    negate: bool = False  # assert gone


@dataclass
class Scenario:
    name: str
    steps: list[Step]
    app: str | None = None
    permissions: str = "allow"
    reverse: list[int] = field(default_factory=list)
    timeout: float = 20.0

    def for_platform(self, platform: str) -> list[Step]:
        return [s for s in self.steps if s.platform in (None, platform)]


def parse_duration(tok: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", tok)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    return n / 1000 if unit == "ms" else n * 60 if unit == "m" else n


def parse_selector(tok: str) -> Selector:
    for by in ("id", "has", "text"):
        if tok.startswith(by + "="):
            value = tok[len(by) + 1:]
            if not value:
                raise ScenarioError(f"empty selector {tok!r}")
            return Selector(by, value)
    if not tok:
        raise ScenarioError("empty selector")
    return Selector("text", tok)


def parse(source: str, name: str = "scenario") -> Scenario:
    sc = Scenario(name=name, steps=[])
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            _parse_line(sc, lineno, line)
        except ScenarioError as e:
            raise ScenarioError(f"line {lineno}: {e}\n    {line}") from None
        except ValueError as e:  # shlex: unbalanced quotes
            raise ScenarioError(f"line {lineno}: {e}\n    {line}") from None
    if not sc.steps:
        raise ScenarioError("scenario has no steps")
    return sc


def _parse_line(sc: Scenario, lineno: int, line: str) -> None:
    head, _, rest = line.partition(":")
    if head in HEADERS and not sc.steps:
        _header(sc, head, rest.strip())
        return
    platform = None
    if head in PLATFORMS:
        platform, line = head, rest.strip()
    nths = []
    command = line
    while m := re.search(r"\s+(#\d+)$", command):
        prefix = command[:m.start()]
        if (len(prefix) - len(prefix.rstrip("\\"))) % 2:
            break
        nths.append(m.group(1))
        command = prefix.rstrip()
    toks = shlex.split(command)
    if not toks:
        raise ScenarioError("empty step")
    kind, args = toks[0], toks[1:]
    if len(nths) > 1 or (nths and int(nths[0][1:]) == 0):
        raise ScenarioError("use one #N (N >= 1) to pick the Nth match")
    if kind not in KINDS:
        hint = " (headers must come before the first step)" if kind.rstrip(":") in HEADERS else ""
        raise ScenarioError(f"unknown step {kind!r}; expected one of {', '.join(KINDS)}{hint}")
    step = _step(lineno, line, kind, platform, args)
    if nths:
        if step.target is None:
            raise ScenarioError(f"#N needs a selector; {kind} has none")
        step = replace(step, target=replace(step.target, nth=int(nths[0][1:]), explicit_nth=True))
    sc.steps.append(step)


def _header(sc: Scenario, key: str, value: str) -> None:
    if key == "app":
        if not value:
            raise ScenarioError("app needs a bundle id or package name")
        sc.app = value
    elif key == "permissions":
        if value not in ("allow", "deny"):
            raise ScenarioError("permissions must be allow or deny")
        sc.permissions = value
    elif key == "reverse":
        try:
            ports = [int(p) for p in value.split()]
        except ValueError:
            raise ScenarioError("reverse takes port numbers") from None
        if not ports or any(p < 1 or p > 65535 for p in ports):
            raise ScenarioError("reverse ports must be between 1 and 65535")
        sc.reverse = ports
    elif key == "timeout":
        t = parse_duration(value)
        if t is None:
            raise ScenarioError("timeout looks like 20s, 2m or 500ms")
        sc.timeout = t


def _step(lineno: int, raw: str, kind: str, platform: str | None, args: list[str]) -> Step:
    s = lambda **kw: Step(line=lineno, raw=raw, kind=kind, platform=platform, **kw)  # noqa: E731
    if kind == "launch":
        _arity(args, 0, 0)
        return s()
    if kind == "tap":
        _arity(args, 1, 1)
        return s(target=parse_selector(args[0]))
    if kind == "type":
        if len(args) == 1:
            return s(text=args[0])
        if len(args) == 3 and args[1] == "into":
            return s(text=args[0], target=parse_selector(args[2]))
        raise ScenarioError('type "text" [into <selector>]')
    if kind == "scroll":
        if not args or args[0] not in ("up", "down"):
            raise ScenarioError("scroll up|down [until <selector>]")
        if len(args) == 1:
            return s(text=args[0])
        if len(args) == 3 and args[1] == "until":
            return s(text=args[0], target=parse_selector(args[2]))
        raise ScenarioError("scroll up|down [until <selector>]")
    if kind == "deeplink":
        _arity(args, 1, 1)
        scheme, sep, remainder = args[0].partition("://")
        if not sep or not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme) or not remainder:
            raise ScenarioError("deeplink needs a URL with a scheme, e.g. myapp://home")
        return s(text=args[0])
    if kind == "wait":
        _arity(args, 1, 2)
        return s(target=parse_selector(args[0]), timeout=_timeout(args[1:]))
    if kind == "assert":
        if not args or args[0] not in ("visible", "gone"):
            raise ScenarioError("assert visible|gone <selector> [timeout]")
        _arity(args, 2, 3)
        return s(target=parse_selector(args[1]), timeout=_timeout(args[2:]), negate=args[0] == "gone")
    if kind == "screenshot":
        _arity(args, 1, 1)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", args[0]):
            raise ScenarioError("screenshot name: letters, digits, . _ - only")
        return s(text=args[0])
    raise AssertionError(kind)


def _arity(args: list[str], lo: int, hi: int) -> None:
    if not lo <= len(args) <= hi:
        want = str(lo) if lo == hi else f"{lo}-{hi}"
        raise ScenarioError(f"expected {want} argument(s), got {len(args)}")


def _timeout(rest: list[str]) -> float | None:
    if not rest:
        return None
    t = parse_duration(rest[0])
    if t is None:
        raise ScenarioError(f"bad timeout {rest[0]!r}; use 30s, 2m or 500ms")
    return t
