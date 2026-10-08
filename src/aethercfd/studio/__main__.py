"""AetherCFD Studio entry point:  python -m aethercfd.studio [case] [--smoke] [--reset-layout]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aethercfd-studio")
    ap.add_argument("case", nargs="?", help="OpenFOAM case folder to open")
    ap.add_argument("--smoke", action="store_true", help="open, cycle perspectives, screenshot, quit")
    ap.add_argument("--smoke-out", default="build/smoke")
    ap.add_argument("--perspective", default=None)
    ap.add_argument("--reset-layout", action="store_true", help="ignore saved layouts (use recipes)")
    args = ap.parse_args(argv)
    _setup_logging()

    from aethercfd.studio.app.bootstrap import bootstrap

    bootstrap()
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication

    qt_warnings: list[str] = []

    def on_qt_message(mode, ctx, msg):
        if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            qt_warnings.append(msg)
            logging.getLogger("qt").warning(msg)

    qInstallMessageHandler(on_qt_message)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AetherCFD Studio")
    app.setOrganizationName("AetherCFD")

    from aethercfd.studio.app.services import AppServices
    from aethercfd.studio.panels import register_builtin_panels
    from aethercfd.studio.shell.main_window import MainWindow
    from aethercfd.studio.theme import style

    style.apply(app)
    services = AppServices()
    register_builtin_panels(services.panels)
    win = MainWindow(services)
    win.show()
    if args.reset_layout:
        win.layouts.store = type(win.layouts.store)(Path(win.layouts.store.root) / "_ignored_reset")
    win.layouts.restore(args.perspective or "setup")
    if args.case:
        services.open_case(args.case)
    if args.smoke:
        from aethercfd.studio.smoke import run_smoke

        code = run_smoke(app, win, Path(args.smoke_out), qt_warnings)
    else:
        code = app.exec()
    _teardown(app, win)
    return code


def _teardown(app, win) -> None:
    """Deterministic shutdown: delete the window tree and its WebEngine pages while QApplication still
    exists, then let PySide tear the application down -- leaving it to interpreter finalization (random
    order) can hang on QtWebEngine."""
    from PySide6.QtCore import QCoreApplication, QEvent

    log = logging.getLogger("aethercfd.studio.teardown")
    log.info("teardown: deleting window tree")
    win.deleteLater()
    for i in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        log.info("teardown: pass %d done", i)


def _setup_logging() -> None:
    """Always log to studio.log in the AetherCFD cache folder (%LOCALAPPDATA%/AetherCFD/Cache); also to the
    console when there is one (pythonw / the Start-menu launcher has no stderr)."""
    from logging.handlers import RotatingFileHandler

    from aethercfd.core.settings import cache_dir

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cache_dir() / "studio.log", maxBytes=5 * 2**20, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _run_module(module: str, args: list[str]) -> int:
    """``AetherCFD.exe --run-module <module> ...``: the packaged app's stand-in for ``python -m``
    (see aethercfd.core.runtime.module_argv). No Qt is imported on this path."""
    import contextlib
    import io
    import runpy
    import traceback

    for name in ("stdout", "stderr"):  # windowed exe launched without pipes
        if getattr(sys, name) is None:
            setattr(sys, name, io.StringIO())
    sys.argv = [module, *args]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    except BaseException:
        traceback.print_exc()
        return 1
    finally:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()
    return 0


def run() -> None:
    """Console/GUI entry point. Ends with os._exit: after the deterministic teardown above, Python's
    interpreter finalization would destroy the remaining Qt/QtWebEngine objects in arbitrary order,
    which hangs the process (observed: the window closes, the process never ends). Everything that
    must persist (layouts, journal, settings) is written synchronously before this point, and runs
    live in WSL independently of this process."""
    import os

    from aethercfd.core.runtime import RUN_MODULE_FLAG

    if len(sys.argv) > 2 and sys.argv[1] == RUN_MODULE_FLAG:
        os._exit(_run_module(sys.argv[2], sys.argv[3:]))
    code = int(main() or 0)
    logging.getLogger("aethercfd.studio.teardown").info("teardown: main() returned %s, exiting", code)
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    if sys.platform == "win32":
        # os._exit -> ExitProcess still runs DLL_PROCESS_DETACH, where QtWebEngine's Chromium can
        # deadlock (observed). TerminateProcess skips DLL detach; all state is already on disk.
        import ctypes

        k32 = ctypes.windll.kernel32
        # declare types: undeclared, the pseudo-handle (-1) is passed as a 32-bit int and the call
        # fails silently on x64
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateProcess(k32.GetCurrentProcess(), code)
    os._exit(code)


if __name__ == "__main__":
    run()
