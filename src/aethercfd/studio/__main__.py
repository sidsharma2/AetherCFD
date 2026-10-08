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

        return run_smoke(app, win, Path(args.smoke_out), qt_warnings)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
