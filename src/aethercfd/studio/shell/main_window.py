"""Main window: slim chrome (menu + status bar); everything else is a dockable panel."""

from __future__ import annotations

import logging
import shutil

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QActionGroup, QKeySequence
from PySide6.QtWidgets import QFileDialog, QLabel, QMainWindow, QMenu, QMessageBox

from aethercfd import __version__
from aethercfd.studio.layout.layout_manager import LayoutManager
from aethercfd.studio.layout.recipes import PERSPECTIVES
from aethercfd.studio.layout.screens import current_screens, fingerprint
from aethercfd.studio.shell.dock_host import DockHost
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)


class StatusSegment(QLabel):
    def __init__(self, text: str = "", tooltip: str = ""):
        super().__init__(text)
        self.setToolTip(tooltip)
        self.setStyleSheet(f"color: {T.text_dim}; padding: 0 8px;")

    def set(self, text: str, color: str | None = None, tooltip: str | None = None) -> None:
        self.setText(text)
        self.setStyleSheet(f"color: {color or T.text_dim}; padding: 0 8px;")
        if tooltip is not None:
            self.setToolTip(tooltip)


class MainWindow(QMainWindow):
    def __init__(self, services):
        super().__init__()
        self.services = services
        services.window = self
        self.setWindowTitle("AetherCFD Studio")
        from pathlib import Path

        from PySide6.QtGui import QIcon

        app_icon = Path(__file__).resolve().parents[1] / "resources" / "aethercfd.png"
        self.setWindowIcon(QIcon(str(app_icon)) if app_icon.exists() else icon("mdi6.weather-windy", T.accent))
        self.resize(1600, 950)
        self.host = DockHost(self, services)
        services.host = self.host
        self.layouts = LayoutManager(self, self.host, services)
        services.layouts = self.layouts
        self._build_menus()
        self._build_status()
        from aethercfd.studio.palette.command_palette import install

        self.palette = install(self)
        from aethercfd.studio.shell.command_bar import CommandBar

        self.command_bar = CommandBar(self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.command_bar)
        services.notification.connect(self._on_notify)
        services.case_changed.connect(self._on_case)
        services.backend_ready.connect(self._on_backend)

    # ------------------------------------------------------------------ menus
    def _build_menus(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("&File")
        a = f.addAction(icon("msc.folder-opened"), "Open Case…", self.open_case_dialog)
        a.setShortcut(QKeySequence("Ctrl+O"))
        n = f.addAction(icon("msc.new-folder"), "New Case from Template…", self.new_case_dialog)
        n.setShortcut(QKeySequence("Ctrl+N"))
        f.addSeparator()
        f.addAction("Exit", self.close)

        v = mb.addMenu("&View")
        self.add_panel_menu = v.addMenu(icon("msc.add"), "Add Panel")
        self.add_panel_menu.aboutToShow.connect(self._fill_add_panel_menu)
        persp = v.addMenu("Perspectives")
        group = QActionGroup(self)
        self._persp_actions = {}
        for p in PERSPECTIVES:
            act = persp.addAction(p.title, lambda pid=p.id: self.layouts.switch(pid))
            act.setCheckable(True)
            act.setShortcut(QKeySequence(p.shortcut))
            group.addAction(act)
            self._persp_actions[p.id] = act
        persp.addSeparator()
        persp.addAction("Reset Current Perspective", self.layouts.reset)
        self.layouts.perspective_changed.connect(
            lambda pid: self._persp_actions.get(pid) and self._persp_actions[pid].setChecked(True))
        lock = v.addAction("Lock Layout")
        lock.setCheckable(True)
        lock.toggled.connect(self._lock_layout)
        v.addSeparator()
        full = v.addAction("Full Screen", self._toggle_fullscreen)
        full.setShortcut(QKeySequence("F11"))

        w = mb.addMenu("&Window")
        w.addAction(icon("mdi6.monitor-multiple"), "Identify Monitors", self.layouts.identify)
        self.move_menu = w.addMenu("Move Focused Panel to")
        self.move_menu.aboutToShow.connect(self._fill_move_menu)
        w.addAction("Layout Diagnostics…", self.show_layout_diagnostics)

        h = mb.addMenu("&Help")
        h.addAction(icon("msc.settings-gear"), "Setup Wizard…", self.show_setup_wizard)
        h.addSeparator()
        h.addAction("About AetherCFD", lambda: QMessageBox.about(
            self, "AetherCFD Studio", f"AetherCFD Studio {__version__}\nDrives OpenFOAM without hiding it."))

    def _fill_add_panel_menu(self) -> None:
        m = self.add_panel_menu
        m.clear()
        for cat, specs in self.services.panels.categories().items():
            sub = m.addMenu(cat)
            for s in specs:
                act = sub.addAction(icon(s.icon), s.title, lambda sp=s: self.host.open_panel(sp))
                if s.shortcut:
                    act.setShortcut(QKeySequence(s.shortcut))
                act.setToolTip(s.description)
        bundles = self.services.panels.bundles()
        if bundles:
            m.addSeparator()
            for b in bundles:
                m.addAction(b.title, lambda bb=b: self.open_bundle(bb))

    def open_bundle(self, bundle) -> None:
        for tid in bundle.members:
            spec = self.services.panels.get(tid)
            if spec is not None:
                self.host.open_panel(spec)

    def _fill_move_menu(self) -> None:
        m: QMenu = self.move_menu
        m.clear()
        focused = self.host.manager.focusedDockWidget()
        for s in current_screens():
            act = m.addAction(f"{s.label}  ({s.geometry.w}×{s.geometry.h})",
                              lambda i=s.index: focused and self.layouts.move_to_monitor(focused.objectName(), i))
            act.setEnabled(focused is not None)
        if focused is None:
            m.addAction("(click a panel first)").setEnabled(False)

    def _lock_layout(self, on: bool) -> None:
        import PySide6QtAds as ads

        feats = (ads.CDockWidget.DockWidgetFeature.DockWidgetMovable
                 | ads.CDockWidget.DockWidgetFeature.DockWidgetFloatable
                 | ads.CDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.host.manager.lockDockWidgetFeaturesGlobally(feats if on else ads.CDockWidget.DockWidgetFeature(0))

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ------------------------------------------------------------------ status bar
    def _build_status(self) -> None:
        sb = self.statusBar()
        self.seg_msg = StatusSegment("Ready")
        self.seg_case = StatusSegment("no case", "Open case and its OpenFOAM fork")
        self.seg_backend = StatusSegment("WSL: –", "OpenFOAM backend")
        self.seg_gpu = StatusSegment("GPU: –", "OpenGL renderer used by the 3D viewport")
        self.seg_disk = StatusSegment("", "Free space")
        sb.addWidget(self.seg_msg, 1)
        for seg in (self.seg_case, self.seg_backend, self.seg_gpu, self.seg_disk):
            sb.addPermanentWidget(seg)
        self._disk_timer = QTimer(self, interval=30000, timeout=self._update_disk)
        self._disk_timer.start()
        self._update_disk()

    def _update_disk(self) -> None:
        try:
            d = shutil.disk_usage("D:\\")
            txt = f"D: {d.free / 2**30:,.0f} GB free"
            col = T.error if d.free < 50 * 2**30 else T.text_dim
        except OSError:
            txt, col = "", None
        try:
            import psutil

            vm = psutil.virtual_memory()
            txt += f"  ·  RAM {vm.available / 2**30:.1f}/{vm.total / 2**30:.0f} GB"
        except Exception:
            pass
        self.seg_disk.set(txt, col)

    def _on_notify(self, msg: str, level: str) -> None:
        col = {"error": T.error, "warn": T.warn}.get(level, T.text_dim)
        self.seg_msg.set(msg, col)

    def _on_case(self, session) -> None:
        if session is None:
            self.seg_case.set("no case")
            return
        c = session.case()
        g = c.fork_guess
        self.seg_case.set(f"{c.name}  ·  {c.solver_label}  ·  {g.family or '?'} {g.version_hint or ''}".strip(),
                          T.text, str(c.root))
        self.setWindowTitle(f"{c.name} — AetherCFD Studio")
        self.seg_backend.set("WSL: starting…", T.warn)

    def _on_backend(self, session) -> None:
        forks = ", ".join(f.short_tag for f in session.forks) or "no OpenFOAM found"
        self.seg_backend.set(f"{session.runner.distro}: {forks}", T.ok if session.forks else T.error)

    def set_gpu(self, renderer: str) -> None:
        intel = "intel" in renderer.lower()
        self.seg_gpu.set(f"GPU: {renderer}", T.warn if intel else T.text_dim,
                         "The 3D viewport is rendering on the integrated GPU. View ▸ GPU… explains how to use "
                         "the NVIDIA GPU." if intel else "OpenGL renderer")

    # ------------------------------------------------------------------ actions
    def new_case_dialog(self) -> None:
        from aethercfd.studio.dialogs.new_case import NewCaseWizard

        NewCaseWizard(self.services, self).exec()

    def open_case_dialog(self) -> None:
        roots = [r for r in self.services.settings.case_roots if r]
        d = QFileDialog.getExistingDirectory(self, "Open OpenFOAM case (folder with system/controlDict)",
                                             roots[0] if roots else "")
        if d:
            self.services.open_case(d)

    def show_setup_wizard(self) -> None:
        """First-run setup (WSL, OpenFOAM, case folders, optional tools, the copilot's AI sign-in); re-openable."""
        from aethercfd.studio.dialogs.setup_wizard import SetupWizard

        if SetupWizard(self.services, self).exec():
            for p in self.host.panels_of("aether.copilot"):  # the provider choice may have changed
                if hasattr(p, "reload_provider"):
                    p.reload_provider()

    def show_layout_diagnostics(self) -> None:
        scr = current_screens()
        lines = [f"fingerprint {fingerprint(scr)}   perspective '{self.layouts.current}'", ""]
        for s in scr:
            lines.append(f"{s.label}: {s.name}  id={s.pnp_id}  {s.geometry.w}x{s.geometry.h} @ ({s.geometry.x},"
                         f"{s.geometry.y})  DPR {s.dpr}  {s.refresh} Hz  key={s.key}")
        lines.append("")
        g = self.geometry()
        lines.append(f"main window: {g.x()},{g.y()} {g.width()}x{g.height()} max={self.isMaximized()}")
        for fw in self.host.floating_containers():
            if fw.isVisible():
                g = fw.geometry()
                names = ", ".join(d.windowTitle() for d in fw.dockWidgets())
                lines.append(f"floating: {g.x()},{g.y()} {g.width()}x{g.height()}  [{names}]")
        QMessageBox.information(self, "Layout diagnostics", "\n".join(lines))

    def closeEvent(self, e) -> None:
        for name in list(self.host.docks()):
            p = self.host.panel(name)
            if p is not None and not p.can_close():
                e.ignore()
                return
        self.layouts.save_now()
        for name in list(self.host.docks()):
            p = self.host.panel(name)
            if p is not None:
                try:
                    p.dispose()
                except Exception:
                    log.exception("dispose failed for %s", name)
        s = self.services.session
        if s is not None and s.runs is not None:
            s.runs.shutdown()  # stops monitoring only; runs keep going in WSL
        self.services.bridge.close()
        from aethercfd.studio.panels.viewport.worker import stop_shared_worker

        stop_shared_worker()
        super().closeEvent(e)
