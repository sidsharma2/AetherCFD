"""Terminal: a real shell in WSL (Windows ConPTY + xterm.js), started in the case folder with exactly
one OpenFOAM fork sourced. Work exactly as in your usual terminal; the GUI never hides the shell."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QToolButton, QVBoxLayout

from aethercfd.core.paths import openfoam_path_safety, win_to_wsl
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)
RES = Path(__file__).resolve().parents[2] / "resources"
# ANSI colours: xterm.js' own (Tango) on dark; on light, darker hues that stay readable on a white page
_ANSI_DARK = ("#2e3436", "#cc0000", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
              "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec")
_ANSI_LIGHT = ("#000000", "#c4302b", "#18794e", "#8a6a00", "#0451a5", "#a3289e", "#0b7f99", "#5c5f66",
               "#6e7178", "#d13a34", "#1f8a3c", "#9a7b00", "#2a6fd1", "#bc05bc", "#0598bc", "#8a8d93")
_ANSI_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white", "brightBlack", "brightRed",
               "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan", "brightWhite")


def xterm_theme() -> dict:
    """xterm.js ITheme of the active app theme."""
    theme = {"background": T.base, "foreground": T.text, "cursor": T.text, "cursorAccent": T.base,
             "selectionBackground": T.accent_soft}
    theme.update(zip(_ANSI_NAMES, _ANSI_DARK if T.dark else _ANSI_LIGHT, strict=True))
    return theme


class _PtyReader(QThread):
    data = Signal(str)
    exited = Signal()

    def __init__(self, pty):
        super().__init__()
        self.pty = pty
        self._stop = False

    def run(self) -> None:
        # Non-blocking reads: pywinpty's blocking read waits for a full buffer, so short output
        # (a prompt) would never arrive. ~100 Hz polling costs nothing measurable.
        idle = 0
        while not self._stop:
            try:
                s = self.pty.read(blocking=False)
            except Exception:
                break
            if s:
                idle = 0
                self.data.emit(s)
                continue
            idle += 1
            if idle % 50 == 0 and not self.pty.isalive():
                break
            self.msleep(10)
        self.exited.emit()

    def stop(self) -> None:
        self._stop = True
        with contextlib.suppress(Exception):
            self.pty.cancel_io()


class Bridge(QObject):
    """Exposed to the page over QWebChannel."""

    output = Signal(str)

    def __init__(self, panel: TerminalPanel):
        super().__init__()
        self.panel = panel

    @Slot(str)
    def input(self, data: str) -> None:
        self.panel.write(data)

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self.panel.resize_pty(cols, rows)

    @Slot(int, int)
    def ready(self, cols: int, rows: int) -> None:
        self.panel.start_shell(cols, rows)

    @Slot(str)
    def copy(self, text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)

    @Slot()
    def paste(self) -> None:
        self.panel.write(QGuiApplication.clipboard().text())


class TerminalPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView  # lazy: only when a terminal opens

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 2, 6, 2)
        self.fork = QComboBox(toolTip="OpenFOAM version sourced in this shell")
        self.fork.currentIndexChanged.connect(lambda *_: self.restart())
        self.where = QLabel()
        self.where.setStyleSheet(f"color: {T.text_dim};")
        restart = QToolButton(icon=icon("msc.debug-restart"), toolTip="Restart shell")
        restart.clicked.connect(self.restart)
        ext = QToolButton(icon=icon("msc.terminal"), toolTip="Open the same shell in Windows Terminal")
        ext.clicked.connect(self.open_external)
        bar.addWidget(QLabel("OpenFOAM"))
        bar.addWidget(self.fork)
        bar.addWidget(self.where, 1)
        bar.addWidget(restart)
        bar.addWidget(ext)
        lay.addLayout(bar)
        from PySide6.QtWebEngineCore import QWebEnginePage

        class _Page(QWebEnginePage):
            def javaScriptConsoleMessage(page, level, msg, line, src):
                log.info("terminal page [%s] %s:%s %s", level, src.rsplit("/", 1)[-1], line, msg)

        self.view = QWebEngineView()
        self.view.setPage(_Page(self.view))
        self.bridge = Bridge(self)
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.page().setBackgroundColor(QColor(T.base))
        self.view.loadFinished.connect(lambda _ok: self.on_theme_changed())
        self.view.setUrl(QUrl.fromLocalFile(str(RES / "xterm" / "terminal.html")))
        lay.addWidget(self.view, 1)
        self.pty = None
        self.reader: _PtyReader | None = None
        self._buf: list[str] = []
        self._size = (120, 30)
        self._flush = QTimer(self, interval=16, timeout=self._flush_out)  # ~60 fps batching
        self._flush.start()
        sv = ctx.services
        sv.backend_ready.connect(self._on_backend)  # bound slots: disconnected when this terminal is closed
        sv.case_changed.connect(self._on_case)
        self._fill_forks()

    def on_theme_changed(self) -> None:
        self.view.page().setBackgroundColor(QColor(T.base))
        self.view.page().runJavaScript(
            f"window.setTheme && window.setTheme({json.dumps(xterm_theme())}, {1 if T.dark else 4.5})")

    # ------------------------------------------------------------------ forks / launch
    def _fill_forks(self) -> None:
        s = self.ctx.services.session
        self.fork.blockSignals(True)
        cur = self.fork.currentData()
        self.fork.clear()
        self.fork.addItem("none (plain Linux shell)", None)
        if s is not None:
            for f in s.forks:
                self.fork.addItem(f.label, f)
            guess = s.case().fork_guess.pick(s.forks) if s.forks else None
            target = cur or guess
            for i in range(self.fork.count()):
                if self.fork.itemData(i) is not None and target is not None and self.fork.itemData(i).id == target.id:
                    self.fork.setCurrentIndex(i)
        self.fork.blockSignals(False)

    def _command(self) -> tuple[str, str, dict]:
        s = self.ctx.services.session
        distro = (s.runner.distro if s and s.runner else self.ctx.services.settings.wsl.default_distro)
        rc = win_to_wsl(str(RES / "wsl" / "aether_rc.sh"))
        cwd = win_to_wsl(str(s.root)) if s is not None else "~"
        args = ["-d", distro, "--cd", cwd, "--exec", "bash", "--noprofile", "--rcfile", rc, "-i"]
        env = dict(os.environ)
        f = self.fork.currentData()
        env["AETHER_FOAM_BASHRC"] = f.bashrc if f else ""
        env["AETHER_FORK"] = f.short_tag if f else "no OpenFOAM"
        env["AETHER_FORK_LABEL"] = f.label if f else "no OpenFOAM"
        env["WSLENV"] = ":".join(x for x in (env.get("WSLENV", ""), "AETHER_FOAM_BASHRC/u", "AETHER_FORK/u",
                                             "AETHER_FORK_LABEL/u") if x)
        env["WSL_UTF8"] = "1"
        self.where.setText(f"  {distro}:{cwd}" + ("   (path has spaces: OpenFOAM tools cannot run in place here)"
                                                  if s is not None and not openfoam_path_safety(cwd).safe else ""))
        return "wsl.exe", subprocess.list2cmdline(args), env

    def start_shell(self, cols: int, rows: int) -> None:
        if self.pty is not None:
            return
        import winpty

        self._size = (max(20, cols), max(5, rows))
        app, cmdline, env = self._command()
        # pywinpty prepends `app` to cmdline itself, and an explicit environment block makes wsl.exe
        # fail (Wsl/Service/E_UNEXPECTED): inherit our environment, temporarily extended with the
        # AETHER_* variables for the duration of the (synchronous) spawn.
        extra = {k: v for k, v in env.items() if k.startswith(("AETHER_", "WSLENV", "WSL_UTF8"))}
        saved = {k: os.environ.get(k) for k in extra}
        try:
            os.environ.update(extra)
            self.pty = winpty.PTY(*self._size, backend=winpty.Backend.ConPTY)
            self.pty.spawn(app, cmdline=cmdline)
        except Exception as exc:
            self._buf.append(f"\r\nAetherCFD: could not start the shell: {exc}\r\n")
            self.pty = None
            return
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.reader = _PtyReader(self.pty)
        # connect to a METHOD, not to self._buf.append: _flush_out swaps in a new list, and a bound
        # list.append would keep feeding the old, discarded one (only the first chunk would show)
        self.reader.data.connect(self._on_pty_data)
        self.reader.exited.connect(
            lambda: self._on_pty_data("\r\n\x1b[2m[shell exited — click Restart]\x1b[0m\r\n"))
        self.reader.start()

    def _on_pty_data(self, s: str) -> None:
        self._buf.append(s)

    def _on_backend(self, _s=None) -> None:
        self._fill_forks()

    def _on_case(self, _s=None) -> None:
        self.restart()

    def restart(self) -> None:
        self._stop_shell()
        self._buf.append("\x1bc")  # reset the terminal
        self.start_shell(*self._size)

    def _stop_shell(self) -> None:
        """Ask the shell to exit and keep DRAINING its output until it is gone (max 3 s) before
        releasing the PTY: ClosePseudoConsole blocks while undrained output is pending, which would
        hang the app on exit."""
        pty, reader = self.pty, self.reader
        self.pty, self.reader = None, None
        if pty is not None:
            with contextlib.suppress(Exception):
                if pty.isalive():
                    pty.write("exit\r")
        if reader is not None:
            reader.data.disconnect()
            deadline = time.monotonic() + 3.0
            while reader.isRunning() and time.monotonic() < deadline:
                reader.wait(100)  # the reader keeps reading (draining) until the process ends
            reader.stop()
            reader.wait(1000)
        if pty is not None:
            with contextlib.suppress(Exception):
                while pty.read(blocking=False):
                    pass

    # ------------------------------------------------------------------ io
    def write(self, data: str) -> None:
        if self.pty is not None:
            try:
                self.pty.write(data)
            except Exception:
                log.exception("pty write failed")

    def resize_pty(self, cols: int, rows: int) -> None:
        self._size = (max(20, cols), max(5, rows))
        if self.pty is not None:
            with contextlib.suppress(Exception):
                self.pty.set_size(*self._size)

    def _on_pty_data(self, s: str) -> None:
        self._buf.append(s)

    def _flush_out(self) -> None:
        if self._buf:
            data, self._buf = "".join(self._buf), []
            self.bridge.output.emit(data)

    def paste_path(self, win_path: str) -> None:
        self.write(shlex.quote(win_to_wsl(win_path)) + " ")

    def open_external(self) -> None:
        app, cmdline, env = self._command()
        try:
            subprocess.Popen(["wt.exe", "new-tab", app, *cmdline.split(" ")], env=env)
        except OSError:
            subprocess.Popen(f'start "" {app} {cmdline}', shell=True, env=env)

    def dispose(self) -> None:
        self._flush.stop()
        self._stop_shell()
        # delete the page before its view (and both before QApplication goes away): a WebEngine page
        # that outlives its profile at process exit makes Qt hang on shutdown
        page = self.view.page()
        self.view.setPage(None) if hasattr(self.view, "setPage") else None
        page.deleteLater()
        self.view.deleteLater()


PANELS = [PanelSpec("aether.terminal", "Terminal", "msc.terminal", "Tools", TerminalPanel, multi_instance=True,
                    default_area=Area.BOTTOM, close_policy="destroy", requires=frozenset({"webengine"}),
                    shortcut="Ctrl+`", description="WSL shell in the case folder with one OpenFOAM fork sourced")]
