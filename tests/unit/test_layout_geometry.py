from __future__ import annotations

from aethercfd.studio.layout.clamp import (
    clamp,
    from_relative,
    title_visible,
    to_relative,
    visible_fraction,
)
from aethercfd.studio.layout.screens import Rect, ScreenInfo, arrangement, fingerprint, screen_at


def scr(i, x, y, w=1920, h=1080, key="CMN1521", dpr=1.0, primary=False, taskbar=48):
    return ScreenInfo(i, f"\\\\.\\DISPLAY{i + 1}", key, "", Rect(x, y, w, h), Rect(x, y, w, h - taskbar), dpr,
                      (344, 193), 60, primary)


LAPTOP = scr(0, 0, 0, primary=True)
LEFT_EXT = scr(0, -1920, 0, key="DEL4321")
RIGHT_EXT = scr(2, 1920, 0, key="DEL4322")
THREE = [LEFT_EXT, scr(1, 0, 0, primary=True), RIGHT_EXT]


def test_fingerprint_ignores_arrangement_but_arrangement_does_not():
    a = [scr(0, 0, 0, key="A"), scr(1, 1920, 0, key="B")]
    b = [scr(0, -1920, 0, key="B"), scr(1, 0, 0, key="A")]
    assert fingerprint(a) == fingerprint(b)
    assert arrangement(a) != arrangement(b)
    assert fingerprint([LAPTOP]) != fingerprint(THREE)


def test_relative_roundtrip_survives_resolution_change():
    r = Rect(100, 50, 800, 600)
    rel = to_relative(r, LAPTOP)
    big = scr(0, 0, 0, w=3840, h=2160, taskbar=96)
    back = from_relative(rel, big)
    assert back.w == 1600 and back.h == round(600 * (2160 - 96) / (1080 - 48))


def test_window_on_removed_monitor_is_moved_back():
    on_right = Rect(2200, 100, 1200, 800)  # was on the right external monitor
    out = clamp(on_right, [LAPTOP])
    assert LAPTOP.available.intersect_area(out) == out.area  # fully on the laptop now
    assert out.w == 1200 and out.h == 800


def test_window_bigger_than_screen_is_shrunk():
    huge = Rect(-100, -100, 4000, 3000)
    out = clamp(huge, [LAPTOP])
    assert out == Rect(0, 0, 1920, 1032)


def test_reachable_windows_are_untouched():
    for r in (Rect(100, 100, 800, 600), Rect(-1800, 200, 1000, 700), Rect(1000, 100, 1500, 700)):
        assert clamp(r, THREE) == r


def test_title_bar_off_screen_is_fixed():
    r = Rect(300, -500, 800, 900)  # most of it visible, but the title bar is above the screen
    assert not title_visible(r, [LAPTOP])
    out = clamp(r, [LAPTOP])
    assert out.y >= 0


def test_negative_coordinates_portrait_and_gaps():
    portrait = scr(0, -1080, -400, w=1080, h=1920, key="P")
    screens = [portrait, LAPTOP]
    r = Rect(-1000, 100, 700, 900)
    assert clamp(r, screens) == r
    gap = Rect(5000, 5000, 400, 300)
    out = clamp(gap, screens)
    assert visible_fraction(out, screens) == 1.0


def test_screen_at_prefers_centre_then_overlap_then_nearest():
    assert screen_at(THREE, Rect(100, 100, 50, 50)) is THREE[1]
    assert screen_at(THREE, Rect(-50, 100, 60, 50)) is THREE[0]  # centre at x=-20 -> left monitor
    assert screen_at([LAPTOP], Rect(9000, 9000, 10, 10)) is LAPTOP


def test_mixed_dpi_key_uses_native_pixels():
    a = scr(0, 0, 0, w=1280, h=720, dpr=1.5, key="X")
    assert "1920x1080" in a.key  # logical 1280x720 at 150% == native 1920x1080
