"""The one shape both platforms' accessibility trees are flattened into."""
from __future__ import annotations

from dataclasses import dataclass

from .scenario import Selector


@dataclass(frozen=True)
class Node:
    label: str
    id: str
    value: str
    kind: str
    x: float
    y: float
    w: float
    h: float
    pkg: str = ""
    path: tuple = ()  # position in the tree; later in depth-first order is drawn on top

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def texts(self) -> list[str]:
        """Flutter merges a tile's title and subtitle into one label joined by newlines."""
        out = []
        for t in (self.label, self.value):
            if t:
                out.append(t.strip())
                out.extend(l.strip() for l in t.split("\n") if l.strip())
        return out

    def matches(self, sel: Selector) -> bool:
        if sel.by == "id":
            return self.id == sel.value
        if sel.by == "text":
            return sel.value in self.texts()
        return any(sel.value in t for t in self.texts())

    def on_screen(self, screen: tuple[float, float]) -> bool:
        """Off-screen rows come back with a 0x0 frame (iOS) or clipped bounds; neither is tappable."""
        cx, cy = self.center
        return self.w > 1 and self.h > 1 and 0 <= cx <= screen[0] and 0 <= cy <= screen[1]

    def describe(self) -> str:
        name = self.label.replace("\n", " / ") or self.value or self.id or self.kind
        return f"{self.kind} {name!r}"


def find(nodes: list[Node], sel: Selector, screen: tuple[float, float], tappable: bool = False) -> Node | None:
    """Top-most match. For taps, a button beats a same-named title ("Add task" sheet title vs CTA)."""
    hits = [n for n in nodes if n.matches(sel) and n.on_screen(screen)]
    rank = ((lambda n: ("Button" not in n.kind, n.y, n.x))
            if tappable and not sel.explicit_nth else (lambda n: (n.y, n.x)))
    hits.sort(key=rank)
    return hits[sel.nth - 1] if len(hits) >= sel.nth else None


def count(nodes: list[Node], sel: Selector, screen: tuple[float, float]) -> int:
    return sum(1 for n in nodes if n.matches(sel) and n.on_screen(screen))


def contains(n: Node, x: float, y: float) -> bool:
    return n.x <= x <= n.x + n.w and n.y <= y <= n.y + n.h


def related(a: Node, b: Node) -> bool:
    return a.path[:len(b.path)] == b.path or b.path[:len(a.path)] == a.path


def tap_point(target: Node, nodes: list[Node], screen: tuple[float, float] | None = None) -> tuple[float, float]:
    """The centre, unless another labelled element sits on it: a full-screen scrim's centre is under
    the sheet, a tall row's centre under a floating tab bar. Tree order is not draw order (Flutter
    lists a modal barrier after the sheet), so any overlapping labelled element counts.
    Then the free point nearest the centre."""
    others = [n for n in nodes if (n.label or n.value) and n.w > 1 and n.h > 1 and not related(n, target)
              and not (contains(n, target.x, target.y) and contains(n, target.x + target.w, target.y + target.h))]
    cx, cy = target.center
    steps = [k / 10 for k in range(1, 10)]
    points = [(target.x + fx * target.w, target.y + fy * target.h) for fx in steps for fy in steps]
    candidates = [(cx, cy)] + points
    candidates = [p for p in candidates if p[0] >= 0 and p[1] >= 0
                  and (screen is None or (p[0] <= screen[0] and p[1] <= screen[1]))]
    for x, y in sorted(candidates, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2):
        if not any(contains(n, x, y) for n in others):
            return x, y
    if screen is not None:
        return min(screen[0], max(0.0, cx)), min(screen[1], max(0.0, cy))
    return max(0.0, cx), max(0.0, cy)


def signature(nodes: list[Node]) -> tuple:
    return tuple((n.label, n.id, n.value, n.kind, n.pkg, n.path, round(n.x), round(n.y), round(n.w), round(n.h))
                 for n in nodes)


def labelled(nodes: list[Node]) -> list[Node]:
    return [n for n in nodes if n.label or n.value]


def visible_summary(nodes: list[Node], screen: tuple[float, float], limit: int = 25) -> str:
    seen = []
    for n in labelled(nodes):
        if n.on_screen(screen):
            d = n.describe()
            if d not in seen:
                seen.append(d)
    more = f" (+{len(seen) - limit} more)" if len(seen) > limit else ""
    return "; ".join(seen[:limit]) + more if seen else "(nothing labelled on screen)"


def text_landed(typed: str, shown: str) -> bool:
    """Fields may change case and add separators ("5551234" -> "(555) 123-4"), so the typed
    characters must appear in order. What they may not do is add letters, or split a word
    with a space: that is autocorrect ("yepitran" -> "proof run"), not formatting."""
    t, s = typed.lower(), shown.lower()
    if t in s:
        return True
    i = 0
    for j, c in enumerate(s):
        if i < len(t) and c == t[i]:
            i += 1
        elif i and i < len(t) and (c.isalpha() or (c.isspace() and t[i - 1].isalpha() and t[i].isalpha())):
            return False
    return i == len(t)
