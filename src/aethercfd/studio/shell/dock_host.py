"""DockHost: the only code that talks to Qt Advanced Docking (QtAds). Keeps the rest of the studio
independent of QtAds (ADR 0004: a native-QDockWidget fallback stays possible).

Responsibilities: QtAds configuration, stable dock names ("<type_id>#<instance>"), lazy panel
creation on first show, close policies, placeholder panels, programmatic moves that re-assert
visibility (Spike A lesson), floating to a given screen rectangle.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import PySide6QtAds as ads
from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtWidgets import QMainWindow, QStackedLayout, QWidget

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec, PlaceholderPanel
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)
CF = ads.CDockManager.eConfigFlag
AH = ads.CDockManager.eAutoHideFlag
_NAME = re.compile(r"^(?P<type>[^#]+)#(?P<inst>.+)$")

_AREAS = {Area.LEFT: ads.LeftDockWidgetArea, Area.RIGHT: ads.RightDockWidgetArea,
          Area.BOTTOM: ads.BottomDockWidgetArea, Area.TOP: ads.TopDockWidgetArea,
          Area.CENTER: ads.CenterDockWidgetArea}
_SIDEBARS = {Area.AUTOHIDE_LEFT: ads.SideBarLeft, Area.AUTOHIDE_RIGHT: ads.SideBarRight,
             Area.AUTOHIDE_BOTTOM: ads.SideBarBottom}


def configure_qtads() -> None:
    """Static QtAds flags; must be set before the CDockManager is constructed."""
    for flag, on in [
        (CF.DragPreviewIsDynamic, True), (CF.DragPreviewShowsContentPixmap, False),
        (CF.DragPreviewHasWindowFrame, False), (CF.OpaqueSplitterResize, True),
        (CF.FocusHighlighting, True), (CF.EqualSplitOnInsertion, True),
        (CF.ActiveTabHasCloseButton, True), (CF.AllTabsHaveCloseButton, False),
        (CF.DockAreaDynamicTabsMenuButtonVisibility, True), (CF.DockAreaHideDisabledButtons, True),
        (CF.MiddleMouseButtonClosesTab, True), (CF.FloatingContainerHasWidgetTitle, True),
        (CF.FloatingContainerHasWidgetIcon, True), (CF.XmlCompressionEnabled, False),
        (CF.DockAreaHasUndockButton, True), (CF.DockAreaHasTabsMenuButton, True),
    ]:
        ads.CDockManager.setConfigFlag(flag, on)
    ads.CDockManager.setAutoHideConfigFlags(AH.AutoHideFeatureEnabled)
    ads.CDockManager.setAutoHideConfigFlag(AH.DockAreaHasAutoHideButton, True)
    ads.CDockManager.setAutoHideConfigFlag(AH.AutoHideCloseOnOutsideMouseClick, True)
    ads.CDockManager.setAutoHideConfigFlag(AH.AutoHideShowOnMouseOver, False)


class LazyPanelHost(QWidget):
    """Placeholder content of a dock; builds the real panel the first time it is shown."""

    def __init__(self, build: Callable[[], Panel]):
        super().__init__()
        self._build = build
        self.panel: Panel | None = None
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._pending_state: dict | None = None

    def ensure(self) -> Panel:
        if self.panel is None:
            self.panel = self._build()
            self._stack.addWidget(self.panel)
            self._stack.setCurrentWidget(self.panel)
            if self._pending_state is not None:
                try:
                    self.panel.restore_state(self._pending_state)
                except Exception:
                    log.exception("restoring state of %s failed", type(self.panel).__name__)
                self._pending_state = None
        return self.panel

    def set_pending_state(self, state: dict) -> None:
        if self.panel is not None:
            self.panel.restore_state(state)
        else:
            self._pending_state = state

    def showEvent(self, e) -> None:
        self.ensure().on_shown()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        if self.panel is not None:
            self.panel.on_hidden()
        super().hideEvent(e)


class DockHost(QObject):
    panel_opened = Signal(str)  # dock name
    panel_closed = Signal(str)
    layout_changed = Signal()

    def __init__(self, window: QMainWindow, services):
        super().__init__(window)
        configure_qtads()
        self.window = window
        self.services = services
        self.manager = ads.CDockManager(window)
        self.manager.setStyleSheet(self.manager.styleSheet())  # keep QtAds' own sheet; app QSS overrides
        self._docks: dict[str, ads.CDockWidget] = {}
        self._sides: dict[str, Area] = {}  # dock -> the area it was last placed in (on-demand tabbing)
        self._central = self._make_central()
        for sig in (self.manager.dockAreasAdded, self.manager.dockAreasRemoved,
                    self.manager.dockWidgetAdded, self.manager.dockWidgetRemoved,
                    self.manager.floatingWidgetCreated):
            sig.connect(lambda *a: self.layout_changed.emit())

    # ------------------------------------------------------------------ central workbench
    def _make_central(self) -> ads.CDockWidget:
        from PySide6.QtWidgets import QLabel

        d = ads.CDockWidget(self.manager, "Workbench")
        d.setObjectName("aether.workbench#main")
        lbl = QLabel("Open a case  ·  Ctrl+K to search  ·  View ▸ Add panel")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {T.text_faint}; font-size: 11pt;")
        d.setWidget(lbl)
        d.setFeature(ads.CDockWidget.DockWidgetFeature.NoTab, True)
        d.setFeature(ads.CDockWidget.DockWidgetFeature.DockWidgetClosable, False)
        d.setFeature(ads.CDockWidget.DockWidgetFeature.DockWidgetMovable, False)
        d.setFeature(ads.CDockWidget.DockWidgetFeature.DockWidgetFloatable, False)
        self.manager.setCentralWidget(d)
        return d

    # ------------------------------------------------------------------ names
    @staticmethod
    def split_name(name: str) -> tuple[str, str]:
        m = _NAME.match(name)
        return (m.group("type"), m.group("inst")) if m else (name, "main")

    def next_instance(self, type_id: str) -> str:
        used = {self.split_name(n)[1] for n in self._docks if self.split_name(n)[0] == type_id}
        i = 1
        while str(i) in used:
            i += 1
        return str(i)

    def docks(self) -> dict[str, ads.CDockWidget]:
        return dict(self._docks)

    def dock(self, name: str) -> ads.CDockWidget | None:
        return self._docks.get(name)

    def panel(self, name: str) -> Panel | None:
        d = self._docks.get(name)
        host = d.widget() if d else None
        return host.panel if isinstance(host, LazyPanelHost) else None

    def balance(self, side: float = 0.21, right: float = 0.29) -> None:
        """Give the left/centre/right columns sensible widths after a recipe: QtAds splits evenly, which
        squeezes the centre on a 1080p screen."""
        area = self._central.dockAreaWidget()
        if area is None:
            return
        sp = area.parentSplitter()
        if sp is None or sp.orientation() != Qt.Orientation.Horizontal:
            return
        n = sp.count()
        total = sum(sp.sizes()) or self.manager.width()
        idx = sp.indexOf(area) if sp.indexOf(area) >= 0 else next(
            (i for i in range(n) if sp.widget(i).isAncestorOf(area)), -1)
        if n < 2 or idx < 0:
            return
        sizes = [0] * n
        left = [i for i in range(n) if i < idx]
        rights = [i for i in range(n) if i > idx]
        for i in left:
            sizes[i] = int(total * side / len(left))
        for i in rights:
            sizes[i] = int(total * right / len(rights))
        sizes[idx] = max(100, total - sum(sizes))
        sp.setSizes(sizes)

    def build_panel(self, name: str) -> Panel | None:
        """The panel behind a dock, built now if it has not been shown yet."""
        d = self._docks.get(name)
        host = d.widget() if d else None
        return host.ensure() if isinstance(host, LazyPanelHost) else None

    def panels_of(self, type_id: str) -> list[Panel]:
        """Live (built, not closed) panels of one type; the focused/most recent one first."""
        out = []
        focused = self.manager.focusedDockWidget()
        for name, d in self._docks.items():
            if self.split_name(name)[0] == type_id and not d.isClosed():
                p = self.panel(name)
                if p is not None:
                    out.insert(0, p) if d is focused else out.append(p)
        return out

    # ------------------------------------------------------------------ creation
    def create_dock(self, spec: PanelSpec | None, name: str, *, args: dict | None = None) -> ads.CDockWidget:
        """Create (not yet place) a dock for a panel type; unknown types get a placeholder."""
        if name in self._docks:
            return self._docks[name]
        type_id, inst = self.split_name(name)
        title = spec.title if spec else type_id
        if spec and spec.multi_instance and inst not in ("main",):
            title = f"{spec.title} {inst}" if inst.isdigit() and inst != "1" else spec.title
        d = ads.CDockWidget(self.manager, title)
        d.setObjectName(name)
        ctx = PanelContext(self.services, f"#{inst}", args or {})

        def build() -> Panel:
            if spec is None:
                return PlaceholderPanel(ctx, type_id)
            p = spec.factory(ctx)
            p.title_changed.connect(d.setWindowTitle)
            return p

        host = LazyPanelHost(build)
        d.setWidget(host, ads.CDockWidget.eInsertMode.ForceNoScrollArea)
        if spec:
            d.setIcon(icon(spec.icon))
            d.setToolTip(spec.description or spec.title)
        if spec and spec.close_policy == "destroy":
            d.setFeature(ads.CDockWidget.DockWidgetFeature.CustomCloseHandling, True)
            d.closeRequested.connect(lambda n=name: self.close_panel(n))
        d.closed.connect(lambda n=name: self.panel_closed.emit(n))
        d.closed.connect(self.fix_central_current)
        self._docks[name] = d
        return d

    def open_panel(self, spec: PanelSpec, *, instance: str | None = None, area: Area | None = None,
                   args: dict | None = None, focus: bool = True) -> ads.CDockWidget:
        """Show a panel (creating it if needed) in its default (or the given) area. Opened on demand (no
        ``area``: a menu, the palette, the copilot), it becomes a tab beside the panels already open on its
        side, so the window keeps a few tab strips to click through instead of a new column per panel; a
        layout recipe passes ``area`` and keeps its deliberate splits."""
        if instance is None:
            instance = self.next_instance(spec.type_id) if spec.multi_instance else "main"
        name = f"{spec.type_id}#{instance}"
        existing = self._docks.get(name)
        if existing is not None and not existing.isClosed():
            existing.toggleView(True)
            existing.setAsCurrentTab()
            if focus:
                existing.raise_()
            return existing
        d = existing or self.create_dock(spec, name, args=args)
        self.place(d, area or spec.default_area, tab_group=spec.tab_group, join_side=area is None)
        d.toggleView(True)
        d.setAsCurrentTab()
        self.panel_opened.emit(name)
        return d

    def open_beside(self, spec: PanelSpec, beside: ads.CDockWidget, *, instance: str | None = None) -> ads.CDockWidget:
        """Open a panel split to the right of another one (side-by-side comparisons)."""
        if instance is None:
            instance = self.next_instance(spec.type_id) if spec.multi_instance else "main"
        name = f"{spec.type_id}#{instance}"
        d = self._docks.get(name)
        if d is not None and not d.isClosed():
            return d
        d = d or self.create_dock(spec, name)
        area = beside.dockAreaWidget()
        if area is not None:
            self.manager.addDockWidget(ads.RightDockWidgetArea, d, area)
        else:
            self.place(d, spec.default_area)
        d.toggleView(True)
        self.panel_opened.emit(name)
        return d

    def _side_area(self, area: Area, exclude: ads.CDockWidget):
        """The dock area already holding the most open panels whose home is ``area`` (docked, not floating
        or auto-hidden), or None."""
        counts: dict[int, tuple[int, object]] = {}
        for other_name, other in self._docks.items():
            if other is exclude or other.isClosed() or other.isAutoHide() or other.isFloating() \
                    or other_name == "aether.workbench#main":
                continue
            spec = self.services.panels.get(self.split_name(other_name)[0])
            side = self._sides.get(other_name, spec.default_area if spec else None)  # where it was put, else home
            target = other.dockAreaWidget()
            if side is not area or target is None:
                continue
            n, _ = counts.get(id(target), (0, target))
            counts[id(target)] = (n + 1, target)
        return max(counts.values(), key=lambda c: c[0])[1] if counts else None

    def place(self, d: ads.CDockWidget, area: Area, *, tab_group: str | None = None,
              join_side: bool = False) -> None:
        self._sides[d.objectName()] = area
        if area is Area.FLOATING:
            self.manager.addDockWidgetFloating(d)
            return
        if area in _SIDEBARS:
            self.manager.addAutoHideDockWidget(_SIDEBARS[area], d)
            return
        if area is Area.CENTER:
            # look the area up each time: QtAds recreates dock areas (a cached one may be deleted)
            self.manager.addDockWidgetTabToArea(d, self._central.dockAreaWidget())
            return
        # tab together with an open panel of the same tab group, else dock at the side
        if tab_group:
            for other_name, other in self._docks.items():
                spec = self.services.panels.get(self.split_name(other_name)[0])
                if other is not d and not other.isClosed() and spec and spec.tab_group == tab_group \
                        and other.dockAreaWidget() is not None and not other.isAutoHide():
                    self.manager.addDockWidgetTabToArea(d, other.dockAreaWidget())
                    return
        if join_side:  # on demand: a tab on its side, not one more column
            target = self._side_area(area, d)
            if target is not None:
                self.manager.addDockWidgetTabToArea(d, target)
                return
        self.manager.addDockWidget(_AREAS[area], d)

    def close_panel(self, name: str) -> bool:
        d = self._docks.get(name)
        if d is None:
            return True
        p = self.panel(name)
        if p is not None and not p.can_close():
            return False
        if p is not None:
            p.dispose()
        spec = self.services.panels.get(self.split_name(name)[0])
        if spec is not None and spec.close_policy == "destroy":
            self._docks.pop(name, None)
            self.manager.removeDockWidget(d)
            d.deleteLater()
        else:
            d.toggleView(False)
        self.panel_closed.emit(name)
        return True

    # ------------------------------------------------------------------ moving
    def float_to(self, name: str, rect: QRect, *, maximize: bool = False) -> None:
        d = self._docks[name]
        if not d.isFloating():
            d.setFloating()
        fw = d.floatingDockContainer()
        if fw is not None:
            fw.setGeometry(rect)
            if maximize:
                fw.showMaximized()
        self.ensure_visible(d)

    def add_to_container(self, name: str, container) -> None:
        d = self._docks[name]
        self.manager.addDockWidgetToContainer(ads.CenterDockWidgetArea, d, container)
        self.ensure_visible(d)

    @staticmethod
    def ensure_visible(d: ads.CDockWidget) -> None:
        """After any programmatic move: open, current, visible (Spike A: a dock could be left hidden)."""
        if d.isClosed():
            d.toggleView(True)
        d.setAsCurrentTab()
        if not d.isVisible():
            d.toggleView(True)

    # ------------------------------------------------------------------ state
    def save_state(self) -> bytes:
        return bytes(self.manager.saveState())

    def restore_state(self, blob: bytes, names: list[str], resolve_spec) -> bool:
        """Pre-create every named dock (QtAds silently skips unknown names), then restore."""
        from PySide6.QtCore import QByteArray

        for n in names:
            if n == "aether.workbench#main" or n in self._docks:
                continue
            d = self.create_dock(resolve_spec(self.split_name(n)[0]), n)
            # QtAds places only docks the manager knows: add each one (closed) so restoreState can move it
            # into its saved area. A dock that is merely constructed stays "open" but in no area: an empty window.
            self.manager.addDockWidget(ads.RightDockWidgetArea, d)
            d.toggleView(False)
        ok = bool(self.manager.restoreState(QByteArray(blob)))
        self.fix_central_current()
        return ok and all(self._docks[n].dockAreaWidget() is not None or self._docks[n].isClosed()
                          for n in names if n in self._docks)

    def fix_central_current(self) -> None:
        """The workbench placeholder (no tab) must never cover a real panel in the central area."""
        area = self._central.dockAreaWidget()
        if area is None or area.currentDockWidget() is not self._central:
            return
        for i in range(area.dockWidgetsCount()):
            d = area.dockWidget(i)
            if d is not self._central and not d.isClosed():
                d.setAsCurrentTab()
                return

    def floating_containers(self) -> list:
        return list(self.manager.floatingWidgets())
