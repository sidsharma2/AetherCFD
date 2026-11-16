"""AppServices: the shared services panels reach through PanelContext (no panel imports another)."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from aethercfd.case.discovery import case_folder_problem
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
        with contextlib.suppress(RuntimeError):  # receiver gone (app shutting down)
            self.done.emit((res, err))


class AppServices(QObject):
    case_changed = Signal(object)  # Session | None
    backend_ready = Signal(object)  # Session (runs/forks available)
    notification = Signal(str, str)  # message, level
    files_changed = Signal(list)  # [str] paths changed on disk (GUI thread)
    current_log_changed = Signal(str)  # the solver log the Residuals/Log panels follow
    active_viewport_changed = Signal(object)  # the viewport the Visualization panel acts on
    selection_changed = Signal(object)  # dict describing what the Properties panel shows
    _files_changed_bg = Signal(list)  # emitted from the watcher thread

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = Settings.load()
        self.watchdog = None  # the GUI-thread HangWatchdog, set by the app entry point (studio/__main__.py)
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
        from aethercfd.documents.store import DocumentStore

        self.documents = DocumentStore()  # one buffer per file, shared by every view
        self._watcher = None
        self._files_changed_bg.connect(self._on_files_changed)
        self.current_log: str = ""
        from aethercfd.studio.bridge.log_follow import LogFollower

        self.follower = LogFollower(self, self)
        self.active_viewport = None
        self.copilot_handoff: dict | None = None  # set when the copilot opens a case: its chat continues there
        self.copilot_last_request = ""
        self.selection: dict | None = None
        from aethercfd.studio.theme.manager import THEME

        self.theme = THEME  # theme.changed(name) fires after a live Light/Dark switch

    def set_theme(self, mode: str) -> str:
        """'system' | 'light' | 'dark': switch live and remember it. Returns the theme now shown."""
        name = self.theme.set_mode(mode)
        if self.settings.theme != self.theme.mode:
            self.settings.theme = self.theme.mode
            try:
                self.settings.save()
            except Exception:
                log.warning("could not save the theme setting", exc_info=True)
        return name

    def set_active_viewport(self, vp) -> None:
        if vp is not self.active_viewport:
            self.active_viewport = vp
            self.active_viewport_changed.emit(vp)

    def select(self, what: dict | None) -> None:
        """Tell the Properties panel what the user is looking at: {"kind": "file"|"cell"|"keyword"|...}."""
        self.selection = what
        self.selection_changed.emit(what)

    def set_current_log(self, path: str | Path | None) -> None:
        p = str(path) if path else ""
        if p == self.current_log:
            return
        self.current_log = p
        self.follower.follow(p or None)
        self.current_log_changed.emit(p)

    @staticmethod
    def newest_log(root: Path) -> Path | None:
        logs = [p for p in Path(root).glob("log.*") if p.is_file()]
        logs += [p for p in (Path(root) / "logs").glob("log.*") if p.is_file()] if (Path(root) / "logs").is_dir() else []
        solver_logs = [p for p in logs if not p.name.startswith(("log.blockMesh", "log.checkMesh", "log.decompose",
                                                                  "log.reconstruct", "log.foamDictionary"))]
        pool = solver_logs or logs
        return max(pool, key=lambda p: p.stat().st_mtime) if pool else None

    def _on_files_changed(self, paths: list) -> None:
        open_paths = [p for p in paths if self.documents.get(p) is not None]
        if open_paths:
            self.documents.check(open_paths)
        self.files_changed.emit(paths)

    # ------------------------------------------------------------------ notifications
    def report_startup_problems(self) -> None:
        """Once the window listens: what went wrong before it existed (a settings.toml that could not be used)."""
        if self.settings.load_problem:
            self.notify(self.settings.load_problem, "error")

    def notify(self, message: str, level: str = "info") -> None:
        log.info("notify[%s]: %s", level, message)
        self.notification.emit(message, level)

    # ------------------------------------------------------------------ background work
    def run_bg(self, fn, on_done=None) -> None:
        """Run fn() on the thread pool; on_done((result, error)) on the GUI thread."""
        sig = _Relay(self)

        def finish(res) -> None:
            try:
                if on_done is not None:
                    on_done(res)
            except RuntimeError as exc:  # the panel that asked was closed/deleted meanwhile
                if "deleted" not in str(exc):
                    raise
                log.debug("background result dropped: %s", exc)
            finally:
                sig.deleteLater()  # one relay per job; do not accumulate them

        sig.done.connect(finish)
        self.pool.start(_Job(fn, sig.done))

    # ------------------------------------------------------------------ case
    def open_case(self, path: str | Path, *, create_project: bool = False) -> None:
        from aethercfd.scripting.api import Session

        problem = case_folder_problem(path)
        if problem is not None:  # a moved Recent entry, a file, a parent folder: say so, open nothing
            self.notify(problem, "error")
            return
        try:
            s = Session(path, create_project=create_project, with_backend=False)
        except Exception as exc:
            self.notify(f"Could not open {path}: {exc}", "error")
            return
        self.session = s
        try:  # every way of opening (command line, Start page, File menu, copilot) lands in Recent
            self.settings.add_recent(str(s.root))
            self.settings.save()
        except Exception:
            log.warning("could not record %s in recent cases", s.root, exc_info=True)
        if self._watcher is not None:
            self._watcher.stop()
        from aethercfd.documents.watcher import CaseWatcher

        self._watcher = CaseWatcher(s.root, lambda ps: self._files_changed_bg.emit([str(x) for x in ps])).start()
        self.case_changed.emit(s)
        self.set_current_log(self.newest_log(s.root))
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
