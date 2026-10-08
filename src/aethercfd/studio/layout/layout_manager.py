"""Save/restore layouts per perspective and per monitor setup; react to monitors coming and going.

Restore order (QtAds silently skips dock names it does not know):
  1. pre-create every dock named in the saved state (placeholders for unknown panel types)
  2. CDockManager.restoreState
  3. hand each panel its saved JSON state (applied when the panel is first shown)
  4. put the main window and each floating workspace back on *its* monitor (stored relative to that
     monitor's available area, so resolution/arrangement changes are absorbed)
  5. clamp every top-level window onto a visible screen
Autosave is continuous (debounced): by the time Windows reports a monitor gone, it has already moved
the windows, so the last good layout must already be on disk.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import UTC, datetime

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from aethercfd import __version__
from aethercfd.studio.layout import clamp as clampmod
from aethercfd.studio.layout.layout_store import LayoutStore
from aethercfd.studio.layout.recipes import PERSPECTIVES, Perspective, perspective
from aethercfd.studio.layout.screens import (
    Rect,
    ScreenInfo,
    arrangement,
    current_screens,
    fingerprint,
    screen_at,
)
from aethercfd.studio.shell.dock_host import DockHost
from aethercfd.studio.shell.panel_base import Area

log = logging.getLogger(__name__)
_DOCK_NAME = re.compile(rb'<Widget Name="([^"]+)"')


def _rect(q: QRect) -> Rect:
    return Rect(q.x(), q.y(), q.width(), q.height())


def _qrect(r: Rect) -> QRect:
    return QRect(r.x, r.y, r.w, r.h)


class LayoutManager(QObject):
    perspective_changed = Signal(str)
    layout_restored = Signal(str, str)  # perspective, how ("exact" | "similar" | "recipe")
    screens_changed = Signal(list)  # [ScreenInfo]

    def __init__(self, window, host: DockHost, services, store: LayoutStore | None = None):
        super().__init__(window)
        self.window = window
        self.host = host
        self.services = services
        self.store = store or LayoutStore()
        self.current = "setup"
        self.screens: list[ScreenInfo] = current_screens()
        self._suspend_autosave = 0
        self._save_timer = QTimer(self, singleShot=True, interval=800, timeout=self.save_now)
        self._screen_timer = QTimer(self, singleShot=True, interval=1500, timeout=self._on_screens_settled)
        host.layout_changed.connect(self.schedule_save)
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda *_: self._screen_timer.start())
        app.screenRemoved.connect(lambda *_: self._screen_timer.start())
        app.primaryScreenChanged.connect(lambda *_: self._screen_timer.start())

    # ------------------------------------------------------------------ snapshot
    def _screen_of(self, r: Rect) -> ScreenInfo | None:
        return screen_at(self.screens, r)

    def _win_record(self, widget) -> dict:
        geo = widget.normalGeometry() if widget.isMaximized() or widget.isFullScreen() else widget.geometry()
        r = _rect(geo)
        s = self._screen_of(r) or self.screens[0]
        state = "fullscreen" if widget.isFullScreen() else "maximized" if widget.isMaximized() else "normal"
        return {"screen_key": s.key, "screen_index": s.index, "rect_rel": clampmod.to_relative(r, s), "state": state}

    def snapshot(self) -> dict:
        self.screens = current_screens()
        floating = []
        for fw in self.host.floating_containers():
            if not fw.isVisible():
                continue
            names = [d.objectName() for d in fw.dockWidgets()]
            if names:
                floating.append({"panels": names, **self._win_record(fw)})
        panels = {}
        for name in self.host.docks():
            p = self.host.panel(name)
            if p is not None:
                try:
                    st = p.save_state()
                except Exception:
                    log.exception("save_state failed for %s", name)
                    st = {}
                if st:
                    panels[name] = {"v": p.STATE_VERSION, "state": st}
        return {
            "screens": [{"key": s.key, "geom": [s.geometry.x, s.geometry.y, s.geometry.w, s.geometry.h],
                         "dpr": s.dpr} for s in self.screens],
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "app_version": __version__,
            "main_window": self._win_record(self.window),
            "ads_state": base64.b64encode(self.host.save_state()).decode("ascii"),
            "floating": floating, "panels": panels,
        }

    def schedule_save(self) -> None:
        if not self._suspend_autosave:
            self._save_timer.start()

    def save_now(self) -> None:
        if self._suspend_autosave:
            return
        try:
            snap = self.snapshot()
            self.store.save_variant(self.current, fingerprint(self.screens), snap)
        except Exception:
            log.exception("layout autosave failed")

    # ------------------------------------------------------------------ restore
    def restore(self, pid: str) -> str:
        self._suspend_autosave += 1
        try:
            self.screens = current_screens()
            self.current = pid
            hit = self.store.find_variant(pid, fingerprint(self.screens), arrangement(self.screens), len(self.screens))
            how = "recipe"
            if hit is not None:
                how, variant = hit
                if not self._apply_variant(variant):
                    how = "recipe"
            if how == "recipe":
                self.apply_recipe(perspective(pid) or PERSPECTIVES[0])
            self.clamp_all()
        finally:
            self._suspend_autosave -= 1
        self.perspective_changed.emit(pid)
        self.layout_restored.emit(pid, how)
        self.schedule_save()
        return how

    def _apply_variant(self, v: dict) -> bool:
        try:
            blob = base64.b64decode(v["ads_state"])
        except (KeyError, ValueError):
            return False
        names = [m.decode() for m in _DOCK_NAME.findall(blob)]
        ok = self.host.restore_state(blob, names, self.services.panels.get)
        if not ok:
            return False
        for name, rec in v.get("panels", {}).items():
            d = self.host.dock(name)
            host = d.widget() if d else None
            if host is not None and hasattr(host, "set_pending_state"):
                host.set_pending_state(rec.get("state", {}))
        self._place_window(self.window, v.get("main_window"))
        by_panels = {frozenset(f["panels"]): f for f in v.get("floating", [])}
        for fw in self.host.floating_containers():
            key = frozenset(d.objectName() for d in fw.dockWidgets())
            rec = by_panels.get(key)
            if rec is not None:
                self._place_window(fw, rec)
        return True

    def _place_window(self, w, rec: dict | None) -> None:
        if not rec:
            return
        s = next((x for x in self.screens if x.key == rec.get("screen_key")), None)
        if s is None:  # that monitor is gone: same position index if it exists, else primary
            idx = rec.get("screen_index", 0)
            s = self.screens[idx] if idx < len(self.screens) else next((x for x in self.screens if x.primary),
                                                                         self.screens[0])
        r = clampmod.clamp(clampmod.from_relative(tuple(rec["rect_rel"]), s), self.screens)
        if w.isMaximized() or w.isFullScreen():
            w.showNormal()
        w.setGeometry(_qrect(r))
        if rec.get("state") == "maximized":
            w.showMaximized()
        elif rec.get("state") == "fullscreen":
            w.showFullScreen()

    def apply_recipe(self, p: Perspective) -> None:
        for name in list(self.host.docks()):
            d = self.host.dock(name)
            if d is not None and not d.isClosed() and name != "aether.workbench#main":
                d.toggleView(False)
        groups: dict[str, object] = {}
        for place in p.recipe(len(self.screens)):
            spec = self.services.panels.get(place.panel)
            if spec is None:
                continue
            if place.area is Area.FLOATING and place.screen is not None and place.screen < len(self.screens):
                d = self.host.open_panel(spec, area=Area.RIGHT, focus=False)
                name = d.objectName()
                container = groups.get(place.group) if place.group else None
                if container is not None:
                    self.host.add_to_container(name, container)
                else:
                    s = self.screens[place.screen]
                    self.host.float_to(name, _qrect(s.available), maximize=place.maximize)
                    if place.group:
                        groups[place.group] = d.dockContainer()
            else:
                area = place.area if place.area is not Area.FLOATING else spec.default_area
                self.host.open_panel(spec, area=area, focus=False)

    def reset(self) -> None:
        self._suspend_autosave += 1
        try:
            self.apply_recipe(perspective(self.current) or PERSPECTIVES[0])
            self.clamp_all()
        finally:
            self._suspend_autosave -= 1
        self.save_now()

    def switch(self, pid: str) -> None:
        if pid == self.current:
            return
        self.save_now()
        self.restore(pid)

    # ------------------------------------------------------------------ monitors
    def clamp_all(self) -> None:
        self.screens = current_screens()
        tops = [self.window] + [fw for fw in self.host.floating_containers() if fw.isVisible()]
        for w in tops:
            if w.isMaximized() or w.isFullScreen():
                r = _rect(w.geometry())
                if self._screen_of(r) is None or not any(s.geometry.intersect_area(r) for s in self.screens):
                    w.showNormal()
                else:
                    continue
            r = _rect(w.geometry())
            c = clampmod.clamp(r, self.screens)
            if c != r:
                w.move(c.x, c.y)  # move first, then resize (mixed-DPI quirk)
                QGuiApplication.processEvents()
                w.resize(c.w, c.h)

    def _on_screens_settled(self) -> None:
        old_fp = fingerprint(self.screens)
        self.screens = current_screens()
        new_fp = fingerprint(self.screens)
        self.screens_changed.emit(self.screens)
        if new_fp == old_fp:
            self.clamp_all()
            return
        hit = self.store.find_variant(self.current, new_fp, arrangement(self.screens), len(self.screens))
        if hit is not None and hit[0] == "exact":
            self.restore(self.current)
            self.services.notify(f"Monitor setup changed: restored your '{self.current}' layout for these screens.")
            return
        # unknown setup: floating workspaces whose monitor vanished dock back into the main window
        alive_keys = {s.key for s in self.screens}
        for fw in self.host.floating_containers():
            r = _rect(fw.geometry())
            s_old = screen_at(self.screens, r)
            if s_old is None or s_old.key not in alive_keys or not any(
                    s.geometry.intersect_area(r) for s in self.screens):
                for d in list(fw.dockWidgets()):
                    spec = self.services.panels.get(self.host.split_name(d.objectName())[0])
                    self.host.place(d, spec.fallback_area if spec else Area.RIGHT)
                    self.host.ensure_visible(d)
        self.clamp_all()
        n = len(self.screens)
        self.services.notify(f"Monitor setup changed ({n} screen{'s' if n != 1 else ''}). "
                             "View ▸ Perspectives ▸ Reset applies the layout recipe for this setup.")
        self.save_now()

    def move_to_monitor(self, dock_name: str, index: int, *, maximize: bool = False) -> None:
        self.screens = current_screens()
        if index >= len(self.screens):
            return
        s = self.screens[index]
        # join an existing floating workspace on that monitor, else float onto it
        for fw in self.host.floating_containers():
            if fw.isVisible() and screen_at(self.screens, _rect(fw.geometry())) == s:
                self.host.add_to_container(dock_name, fw.dockContainer())
                return
        if s.index == (screen_at(self.screens, _rect(self.window.geometry())) or s).index:
            spec = self.services.panels.get(self.host.split_name(dock_name)[0])
            self.host.place(self.host.dock(dock_name), spec.default_area if spec else Area.RIGHT)
            self.host.ensure_visible(self.host.dock(dock_name))
            return
        a = s.available
        self.host.float_to(dock_name, QRect(a.x + a.w // 8, a.y + a.h // 8, a.w * 3 // 4, a.h * 3 // 4),
                           maximize=maximize)

    def identify(self) -> None:
        """Show a big monitor number on every screen for two seconds."""
        from PySide6.QtWidgets import QLabel

        self._identify = []
        for s in current_screens():
            lbl = QLabel(f"{s.index + 1}")
            lbl.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                               | Qt.WindowType.Tool)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            lbl.setStyleSheet("color: white; background: rgba(30,31,34,210); font-size: 120pt; "
                              "border: 3px solid #4a8fe7; border-radius: 16px; padding: 20px 60px;")
            lbl.adjustSize()
            g = s.geometry
            lbl.move(g.x + (g.w - lbl.width()) // 2, g.y + (g.h - lbl.height()) // 2)
            lbl.show()
            self._identify.append(lbl)
        QTimer.singleShot(2000, lambda: [w.close() for w in self._identify])
