"""--smoke: open the studio, cycle every perspective, screenshot every top-level window, quit.

Fails (exit 1) on Python exceptions or on Qt warnings about OpenGL / RHI / WebEngine.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from aethercfd.studio.layout.recipes import PERSPECTIVES

_BAD = ("OpenGL", "QRhi", "WebEngine", "GL_", "QOpenGLWidget")


def _pump(app: QApplication, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.01)


def run_smoke(app: QApplication, win, out: Path, qt_warnings: list[str]) -> int:
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    old_hook = sys.excepthook

    def hook(t, v, tb):
        errors.append("".join(traceback.format_exception(t, v, tb)))
        old_hook(t, v, tb)

    sys.excepthook = hook
    report = {"perspectives": {}}
    try:
        _pump(app, 2.0)
        for p in PERSPECTIVES:
            how = win.layouts.restore(p.id)
            _pump(app, 1.2)
            shots = []
            win.grab().save(str(out / f"{p.id}_main.png"))
            shots.append(f"{p.id}_main.png")
            for i, fw in enumerate(win.host.floating_containers()):
                if fw.isVisible():
                    fw.grab().save(str(out / f"{p.id}_float{i}.png"))
                    shots.append(f"{p.id}_float{i}.png")
            open_docks = [n for n, d in win.host.docks().items() if not d.isClosed()]
            report["perspectives"][p.id] = {"how": how, "open": open_docks, "shots": shots}
    except Exception:
        errors.append(traceback.format_exc())
    bad = [w for w in qt_warnings if any(k in w for k in _BAD)]
    report.update(errors=errors, qt_warnings=qt_warnings, bad_warnings=bad, ok=not errors and not bad)
    (out / "smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("ok", "errors", "bad_warnings")}, indent=1))
    win.close()
    return 0 if report["ok"] else 1
