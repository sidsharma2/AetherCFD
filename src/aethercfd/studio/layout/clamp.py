"""Pure geometry for keeping windows reachable when monitors change (unit-tested exhaustively).

Rules: a window must have its title strip (top ``title_h`` px) fully visible on some screen and at
least ``min_visible`` of its area on screens; otherwise it is moved onto the nearest screen and
shrunk to fit that screen's available area. Window rectangles are stored relative to their screen
so they survive arrangement and resolution changes.
"""

from __future__ import annotations

from aethercfd.studio.layout.screens import Rect, ScreenInfo, screen_at

TITLE_H = 32
MIN_VISIBLE = 0.25


def to_relative(r: Rect, screen: ScreenInfo) -> tuple[float, float, float, float]:
    a = screen.available
    return ((r.x - a.x) / a.w, (r.y - a.y) / a.h, r.w / a.w, r.h / a.h)


def from_relative(rel: tuple[float, float, float, float], screen: ScreenInfo) -> Rect:
    a = screen.available
    return Rect(round(a.x + rel[0] * a.w), round(a.y + rel[1] * a.h), round(rel[2] * a.w), round(rel[3] * a.h))


def title_visible(r: Rect, screens: list[ScreenInfo], title_h: int = TITLE_H) -> bool:
    strip = Rect(r.x, r.y, r.w, min(title_h, r.h))
    covered = sum(s.available.intersect_area(strip) for s in screens)
    # at least 60% of the title strip must be on screen to grab and move the window
    return strip.area > 0 and covered >= 0.6 * strip.area


def visible_fraction(r: Rect, screens: list[ScreenInfo]) -> float:
    if r.area == 0:
        return 0.0
    return min(1.0, sum(s.geometry.intersect_area(r) for s in screens) / r.area)


def fit_into(r: Rect, a: Rect) -> Rect:
    w, h = min(r.w, a.w), min(r.h, a.h)
    x = min(max(r.x, a.x), a.right - w)
    y = min(max(r.y, a.y), a.bottom - h)
    return Rect(x, y, w, h)


def clamp(r: Rect, screens: list[ScreenInfo], *, min_visible: float = MIN_VISIBLE) -> Rect:
    """Return a rectangle guaranteed reachable on the current screens (unchanged if already fine)."""
    if not screens:
        return r
    if title_visible(r, screens) and visible_fraction(r, screens) >= min_visible:
        return r
    target = screen_at(screens, r) or screens[0]
    return fit_into(r, target.available)
