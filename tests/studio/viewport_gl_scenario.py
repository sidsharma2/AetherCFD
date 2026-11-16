"""Helper for test_viewport_gl_recovery.py (not a test module): switch perspectives the way a user does and
check that every viewport shown afterwards can still render.

Runs in its own process because the app's bootstrap (GL surface format, AA_ShareOpenGLContexts, Qt Quick on
OpenGL) must run before the QApplication exists, and pytest-qt creates its application without it.

Usage: python viewport_gl_scenario.py CASE_DIR  -> prints one JSON line per step, exit 0 when every step drew.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main(case: Path) -> int:
    from aethercfd.studio.app.bootstrap import bootstrap

    bootstrap()
    import numpy as np
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    from aethercfd.studio.app.automation import AppAutomation
    from aethercfd.studio.app.services import AppServices
    from aethercfd.studio.panels import register_builtin_panels
    from aethercfd.studio.shell.main_window import MainWindow

    def pump(sec: float) -> None:
        t = time.monotonic()
        while time.monotonic() - t < sec:
            app.processEvents()
            time.sleep(0.01)

    def wait(fn, sec: float = 120) -> bool:
        t = time.monotonic()
        while time.monotonic() - t < sec:
            app.processEvents()
            if fn():
                return True
            time.sleep(0.01)
        return False

    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    w.resize(1600, 1000)
    w.show()
    sv.open_case(case, create_project=True)
    vp = AppAutomation(sv)._viewport(open_if_missing=True)
    ok = wait(lambda: bool(vp.meta) and vp.field.count() > 0) and wait(lambda: getattr(vp, "drawn_gen", -1) == vp.gen)
    pump(1.0)
    failures = 0 if ok else 1

    def step(tag: str) -> None:
        nonlocal failures
        shown = [v for v in sv.host.panels_of("aether.viewport") if v.plotter.isVisible()]
        pump(0.5)
        # what the app's own Python code does between renders (the Visualization panel's title lookup, the
        # layout snapshot): walk every dock, and the splitter the viewport sits in
        for n in sv.host.docks():
            sv.host.panel(n)
        for v in shown:
            d = next(d for n, d in sv.host.docks().items() if sv.host.panel(n) is v)
            sp = d.dockAreaWidget().parentSplitter()
            _ = (sp.sizes(), [sp.widget(i).isVisible() for i in range(sp.count())])
        for v in shown:
            pl = v.plotter
            pl.render()  # what the app does on any change (a new field, a stage, a camera move)
            try:
                std = float(np.asarray(pl.screenshot(return_img=True)).std())  # the Screenshot button's path
                err = ""
            except Exception as exc:
                std, err = 0.0, f"{type(exc).__name__}: {exc}"
            pl.render_window.MakeCurrent()
            current = bool(pl.render_window.IsCurrent())
            img = pl.grabFramebuffer()  # what the widget shows on screen
            shown_std = float(np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes()).copy().std())
            good = current and std > 2.0 and shown_std > 2.0
            failures += 0 if good else 1
            print(json.dumps({"step": tag, "viewport": v.ctx.instance_id, "current": current, "std": round(std, 1),
                              "on_screen_std": round(shown_std, 1), "recoveries": getattr(pl, "recoveries", None),
                              "error": err, "ok": good}), flush=True)
        if not shown:
            print(json.dumps({"step": tag, "viewport": None}), flush=True)

    step("start")
    for pid in ("meshing", "post", "setup", "post", "setup", "meshing", "post"):
        w.layouts.switch(pid)
        pump(1.0)
        step(pid)
    print(json.dumps({"failures": failures}), flush=True)
    sv.pool.waitForDone(10000)
    w.close()  # the app's own close path (ADR 0008 rule 4), then a normal exit
    pump(1.0)
    sv.bridge.close()
    from aethercfd.studio.panels.viewport.worker import stop_shared_worker

    stop_shared_worker()
    print(json.dumps({"closed": True}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
