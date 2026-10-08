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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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


def run() -> None:
    """Console/GUI entry point. Ends with os._exit: after the deterministic teardown above, Python's
    interpreter finalization would destroy the remaining Qt/QtWebEngine objects in arbitrary order,
    which hangs the process (observed: the window closes, the process never ends). Everything that
    must persist (layouts, journal, settings) is written synchronously before this point, and runs
    live in WSL independently of this process."""
    import os

    code = int(main() or 0)
    logging.getLogger("aethercfd.studio.teardown").info("teardown: main() returned %s, exiting", code)
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
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
