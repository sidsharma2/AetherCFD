"""Screens as plain immutable records + a fingerprint of the monitor setup.

Never keep QScreen objects or call QWidget.screen() (pyvistaqt documents a use-after-free crash);
copy what we need into ScreenInfo immediately. Qt's model/serial fields are empty on this laptop's
panel, so the monitor's PnP id (e.g. CMN1521) is read from Windows (EnumDisplayDevicesW) instead.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    def intersect_area(self, o: Rect) -> int:
        w = min(self.right, o.right) - max(self.x, o.x)
        h = min(self.bottom, o.bottom) - max(self.y, o.y)
        return w * h if w > 0 and h > 0 else 0

    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def contains_point(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom


@dataclass(frozen=True)
class ScreenInfo:
    index: int  # left-to-right order (Monitor 1, 2, 3 ...)
    name: str  # Qt name, e.g. \\.\DISPLAY1
    pnp_id: str  # e.g. CMN1521 (from Windows), or the Qt manufacturer as fallback
    serial: str
    geometry: Rect
    available: Rect
    dpr: float
    physical_mm: tuple[int, int]
    refresh: int
    primary: bool

    @property
    def key(self) -> str:
        """Identity of the physical monitor (stable across arrangement changes)."""
        mm = f"{self.physical_mm[0]}x{self.physical_mm[1]}mm"
        res = f"{round(self.geometry.w * self.dpr)}x{round(self.geometry.h * self.dpr)}@{self.refresh}"
        return f"{self.pnp_id or '?'}|{self.serial or '-'}|{mm}|{res}"

    @property
    def label(self) -> str:
        return f"Monitor {self.index + 1}" + (" (primary)" if self.primary else "")


def fingerprint(screens: list[ScreenInfo]) -> str:
    """Same set of physical monitors -> same fingerprint (arrangement-independent)."""
    return hashlib.sha1("\n".join(sorted(s.key for s in screens)).encode()).hexdigest()[:12]


def arrangement(screens: list[ScreenInfo]) -> str:
    return hashlib.sha1("\n".join(sorted(f"{s.key}@{s.geometry.x},{s.geometry.y}" for s in screens))
                        .encode()).hexdigest()[:12]


def screen_at(screens: list[ScreenInfo], r: Rect) -> ScreenInfo | None:
    """Screen containing the rectangle's centre, else the one it overlaps most, else the nearest."""
    cx, cy = r.center()
    for s in screens:
        if s.geometry.contains_point(cx, cy):
            return s
    best = max(screens, key=lambda s: s.geometry.intersect_area(r), default=None)
    if best is not None and best.geometry.intersect_area(r) > 0:
        return best

    def dist(s: ScreenInfo) -> float:
        sx, sy = s.geometry.center()
        return (sx - cx) ** 2 + (sy - cy) ** 2

    return min(screens, key=dist, default=None)


# ------------------------------------------------------------------ Windows monitor ids
def _windows_pnp_ids() -> dict[str, str]:
    """{'\\\\.\\DISPLAY1': 'CMN1521', ...} via EnumDisplayDevicesW (fast, no WMI)."""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    enum = ctypes.windll.user32.EnumDisplayDevicesW
    out: dict[str, str] = {}
    i = 0
    while True:
        adapter = DISPLAY_DEVICEW()
        adapter.cb = ctypes.sizeof(adapter)
        if not enum(None, i, ctypes.byref(adapter), 0):
            break
        mon = DISPLAY_DEVICEW()
        mon.cb = ctypes.sizeof(mon)
        if enum(adapter.DeviceName, 0, ctypes.byref(mon), 0):
            parts = mon.DeviceID.split("\\")  # MONITOR\CMN1521\{...}\0001
            if len(parts) >= 2 and parts[1]:
                out[adapter.DeviceName] = parts[1]
        i += 1
    return out


def current_screens() -> list[ScreenInfo]:
    """Snapshot of the connected screens (requires a QGuiApplication)."""
    from PySide6.QtGui import QGuiApplication

    pnp = _windows_pnp_ids()
    primary = QGuiApplication.primaryScreen()
    primary_name = primary.name() if primary is not None else ""
    raw = []
    for s in QGuiApplication.screens():
        g, a = s.geometry(), s.availableGeometry()
        ps = s.physicalSize()
        raw.append(dict(
            name=s.name(), pnp_id=pnp.get(s.name()) or s.manufacturer() or "", serial=s.serialNumber() or "",
            geometry=Rect(g.x(), g.y(), g.width(), g.height()),
            available=Rect(a.x(), a.y(), a.width(), a.height()),
            dpr=float(s.devicePixelRatio()), physical_mm=(round(ps.width()), round(ps.height())),
            refresh=round(s.refreshRate()), primary=s.name() == primary_name))
    raw.sort(key=lambda d: (d["geometry"].x, d["geometry"].y))
    return [ScreenInfo(index=i, **d) for i, d in enumerate(raw)]
