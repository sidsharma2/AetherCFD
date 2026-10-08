"""AppServices: the shared services panels reach through PanelContext (no panel imports another)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from aethercfd.core.events import BUS
from aethercfd.core.settings import Settings
from aethercfd.studio.bridge.qt_bridge import CoreEventBridge
from aethercfd.studio.shell.panel_registry import PanelRegistry

log = logging.getLogger(__name__)


class _Job(QRunnable):
    def __init__(self, fn, done):
        super().__init__()
        self.fn, self.done = fn, done

    def run(self):
        try:
            res, err = self.fn(), None
        except Exception as exc:  # reported on the GUI thread
            log.exception("background job failed")
            res, err = None, exc
        self.done.emit((res, err))


class AppServices(QObject):
    case_changed = Signal(object)  # Session | None
    backend_ready = Signal(object)  # Session (runs/forks available)
    notification = Signal(str, str)  # message, level
    _job_done = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = Settings.load()
        self.bus = BUS
        self.bridge = CoreEventBridge(self.bus, self)
        self.panels = PanelRegistry()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self.session = None  # aethercfd.scripting.api.Session for the open case
        self.window = None
        self.host = None
        self.layouts = None
        self.gl_renderer: str | None = None

    # ------------------------------------------------------------------ notifications
    def notify(self, message: str, level: str = "info") -> None:
        log.info("notify[%s]: %s", level, message)
        self.notification.emit(message, level)

    # ------------------------------------------------------------------ background work
    def run_bg(self, fn, on_done=None) -> None:
        """Run fn() on the thread pool; on_done((result, error)) on the GUI thread."""
        sig = _Relay(self)
        if on_done is not None:
            sig.done.connect(on_done)
        self.pool.start(_Job(fn, sig.done))

    # ------------------------------------------------------------------ case
    def open_case(self, path: str | Path, *, create_project: bool = False) -> None:
        from aethercfd.scripting.api import Session

        try:
            s = Session(path, create_project=create_project, with_backend=False)
        except Exception as exc:
            self.notify(f"Could not open {path}: {exc}", "error")
            return
        self.session = s
        self.case_changed.emit(s)
        self.notify(f"Opened {s.root}")

        def init_backend():
            from aethercfd.backend.wsl.runner import WslRunner
            from aethercfd.runs.manager import RunManager
            from aethercfd.scripting.api import load_forks

            w = s.settings.wsl
            runner = WslRunner(w.default_distro, allowed=w.allowed_distros)
            forks = load_forks(runner)
            runs = RunManager(s.project, runner, settings=s.settings, bus=s.bus) if s.project else None
            return runner, forks, runs

        def done(res):
            value, err = res
            if err is not None or self.session is not s:
                if err is not None:
                    self.notify(f"OpenFOAM backend unavailable: {err}", "warn")
                return
            s.runner, s.forks, s.runs = value
            s.context.backend, s.context.forks, s.context.run_manager = s.runner, s.forks, s.runs
            if s.runs is not None:
                resumed = s.runs.reattach()
                if resumed:
                    self.notify(f"Re-attached to {len(resumed)} running job(s)")
            self.backend_ready.emit(s)

        self.run_bg(init_backend, done)

    def ensure_project(self) -> bool:
        """Edits/runs need a project (journal). Create the sidecar on first need."""
        s = self.session
        if s is None:
            return False
        if s.project is None:
            from aethercfd.project.project import Project

            s.project = Project.create(s.root)
            s.context.project = s.project
            if s.runner is not None:
                from aethercfd.runs.manager import RunManager

                s.runs = RunManager(s.project, s.runner, settings=s.settings, bus=s.bus)
                s.context.run_manager = s.runs
            self.notify(f"Created AetherCFD project metadata in {s.project.meta_dir}")
        return True


class _Relay(QObject):
    done = Signal(object)
