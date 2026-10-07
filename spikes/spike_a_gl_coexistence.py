"""Spike A — can QtAds + a pyvistaqt (QOpenGLWidget) viewport + an xterm.js QWebEngineView
coexist in one window, survive float/dock cycles, and render correctly?

Run:  uv run python spikes/spike_a_gl_coexistence.py [--cycles 100] [--eager-webengine]
Writes spikes/out/spike_a_<tag>.json and a few PNGs. Exit code 0 only if every check passed.

This is throwaway exploration code; the decisions it supports go into docs/adr/0004-docking.md.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")

ap = argparse.ArgumentParser()
ap.add_argument("--cycles", type=int, default=100)
ap.add_argument("--eager-webengine", action="store_true", help="import QtWebEngine before QApplication")
ap.add_argument("--tag", default="default")
ap.add_argument("--keep-open", action="store_true")
ap.add_argument("--no-reap", action="store_true")
args = ap.parse_args()

# ---------------------------------------------------------------- bootstrap (before QApplication)
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QSurfaceFormat  # noqa: E402
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface  # noqa: E402
from pyvistaqt.rwi import _default_format  # noqa: E402

QSurfaceFormat.setDefaultFormat(_default_format())
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
if args.eager_webengine:
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401,E402

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow  # noqa: E402

qt_messages: list[str] = []


def _qt_msg(mode, ctx, msg):  # collect warnings; GL/RHI/WebEngine ones fail the spike
    qt_messages.append(f"{mode.name}: {msg}")


from PySide6.QtCore import qInstallMessageHandler  # noqa: E402

qInstallMessageHandler(_qt_msg)
app = QApplication(sys.argv)

import PySide6QtAds as ads  # noqa: E402
import pyvista as pv  # noqa: E402
from pyvistaqt import QtInteractor  # noqa: E402
from pyvistaqt.rwi import QVTKRenderWindowInteractor as _RWI  # noqa: E402

GLCOUNT = {"init": 0, "cleanup": 0, "resize": 0}
_oi, _oc, _orz = _RWI.initializeGL, _RWI._cleanup_context, _RWI.resizeGL


def _ii(self):
    GLCOUNT["init"] += 1
    return _oi(self)


def _cc(self):
    GLCOUNT["cleanup"] += 1
    return _oc(self)


def _rz(self, w, h):
    GLCOUNT["resize"] += 1
    GLCOUNT["last_resize"] = (w, h)
    return _orz(self, w, h)


_RWI.initializeGL, _RWI._cleanup_context, _RWI.resizeGL = _ii, _cc, _rz

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
XTERM = Path(__file__).resolve().parents[1] / "src/aethercfd/studio/resources/xterm/dist"

REAP = False  # disproved: QtAds deletes emptied containers itself (see ADR 0004)
report: dict = {"tag": args.tag, "qt_scale_factor": os.environ.get("QT_SCALE_FACTOR"),
                "eager_webengine": args.eager_webengine, "checks": {}}


def check(name: str, ok: bool, detail=None):
    report["checks"][name] = {"ok": bool(ok), "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail is not None else ""))


def pump(ms: int = 60):
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        app.processEvents()
        # no outer exec() loop here, so run deleteLater() explicitly like a real event loop would
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.005)


# ---------------------------------------------------------------- process image (GPU preference target)
buf = ctypes.create_unicode_buffer(32768)
ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
report["process_image"] = buf.value
report["sys_executable"] = sys.executable
print("process image:", buf.value)

# ---------------------------------------------------------------- window + dock manager
for flag, on in [
    (ads.CDockManager.eConfigFlag.DragPreviewIsDynamic, True),
    (ads.CDockManager.eConfigFlag.DragPreviewShowsContentPixmap, False),
    (ads.CDockManager.eConfigFlag.DragPreviewHasWindowFrame, False),
    (ads.CDockManager.eConfigFlag.OpaqueSplitterResize, True),
    (ads.CDockManager.eConfigFlag.FocusHighlighting, True),
    (ads.CDockManager.eConfigFlag.EqualSplitOnInsertion, True),
    (ads.CDockManager.eConfigFlag.FloatingContainerHasWidgetTitle, True),
]:
    ads.CDockManager.setConfigFlag(flag, on)
ads.CDockManager.setAutoHideConfigFlags(ads.CDockManager.eAutoHideFlag.AutoHideFeatureEnabled)

win = QMainWindow()
win.setWindowTitle("AetherCFD — Spike A (GL coexistence)")
win.resize(1500, 900)
mgr = ads.CDockManager(win)

central = ads.CDockWidget(mgr, "Workbench")
central.setWidget(QLabel("Workbench (central)"))
central.setFeature(ads.CDockWidget.DockWidgetFeature.NoTab, True)
central_area = mgr.setCentralWidget(central)

# viewport
vp_dock = ads.CDockWidget(mgr, "3D Viewport")
plotter = QtInteractor(vp_dock, auto_update=False)
plotter.set_background("#1b1c1f")
plotter.add_mesh(pv.Cone(resolution=48), color="white", smooth_shading=False)
plotter.reset_camera()
vp_dock.setWidget(plotter.interactor if hasattr(plotter, "interactor") else plotter)
vp_area = mgr.addDockWidget(ads.RightDockWidgetArea, vp_dock)

# dummy panels
for title, where in [("Case Files", ads.LeftDockWidgetArea), ("Solver Log", ads.BottomDockWidgetArea)]:
    d = ads.CDockWidget(mgr, title)
    d.setWidget(QLabel(title))
    mgr.addDockWidget(where, d)

win.show()
pump(500)

# terminal (lazy WebEngine import unless --eager-webengine)
t0 = time.perf_counter()
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

report["webengine_import_s"] = round(time.perf_counter() - t0, 3)
term_dock = ads.CDockWidget(mgr, "Terminal")
view = QWebEngineView()
html = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="xterm.css"><style>html,body{{margin:0;height:100%;background:#1e1f22}}
#t{{height:100%}}</style></head><body><div id="t"></div>
<script src="xterm.js"></script><script src="addon-fit.js"></script>
<script>
  window.term = new Terminal({{fontFamily:'Cascadia Mono, Consolas, monospace', fontSize:13,
    theme:{{background:'#1e1f22', foreground:'#d6d6d6'}}}});
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(document.getElementById('t'));
  fit.fit(); term.write('hello from xterm.js inside QtWebEngine\\r\\n$ ');
  window.addEventListener('resize', () => fit.fit());
</script></body></html>"""
view.setHtml(html, QUrl.fromLocalFile(str(XTERM) + "/"))
term_dock.setWidget(view)
mgr.addDockWidget(ads.BottomDockWidgetArea, term_dock)
pump(2500)

js_result: dict = {}


def _js_cb(v):
    js_result["line0"] = v


view.page().runJavaScript(
    "window.term ? term.buffer.active.getLine(0).translateToString(true) : null", 0, _js_cb
)
pump(800)
check("xterm.js renders inside QWebEngineView", js_result.get("line0", "") .startswith("hello from xterm"),
      js_result.get("line0"))

# ---------------------------------------------------------------- GL renderer
caps = plotter.ren_win.ReportCapabilities()
renderer = next((ln.split(":", 1)[1].strip() for ln in caps.splitlines() if "renderer string" in ln), "?")
vendor = next((ln.split(":", 1)[1].strip() for ln in caps.splitlines() if "vendor string" in ln), "?")
version = next((ln.split(":", 1)[1].strip() for ln in caps.splitlines() if "version string" in ln), "?")
report["gl"] = {"renderer": renderer, "vendor": vendor, "version": version}
print("GL:", vendor, "|", renderer, "|", version)




def reap_floating() -> int:
    """QtAds leaves the emptied CFloatingDockContainer (+ its dock area and tab menu) alive but hidden
    when a dock widget is moved programmatically. The DockHost must reap them after every move."""
    from PySide6.QtWidgets import QMenu
    n = 0
    for w in QApplication.topLevelWidgets():
        if isinstance(w, ads.CFloatingDockContainer) and not w.isVisible():
            if not w.dockContainer().dockWidgets():
                w.deleteLater(); n += 1
        elif isinstance(w, ads.CDockAreaWidget) and not w.isVisible() and w.dockWidgetsCount() == 0:
            w.deleteLater(); n += 1
        elif isinstance(w, QMenu) and not w.isVisible() and w.parent() is None and not w.actions():
            w.deleteLater(); n += 1
    return n

# ---------------------------------------------------------------- pixel check helper
def cone_pixels(tag: str | None = None) -> tuple[int, tuple[int, int]]:
    """Render through Qt's own framebuffer grab (what the user actually sees)."""
    plotter.render()
    pump(30)
    img = plotter.grabFramebuffer()
    if tag:
        img.save(str(OUT / f"spike_a_{args.tag}_{tag}.png"))
    w, h = img.width(), img.height()
    bright = 0
    step = 4
    for y in range(0, h, step):
        for x in range(0, w, step):
            c = img.pixelColor(x, y)
            if c.red() + c.green() + c.blue() > 400:
                bright += 1
    return bright, (w, h)


n0, size0 = cone_pixels("initial")
dpr = plotter.devicePixelRatioF()
report["initial"] = {"bright_samples": n0, "fb_size": size0, "widget_size": [plotter.width(), plotter.height()],
                     "dpr": dpr}
check("viewport renders cone in docked state", n0 > 50, f"{n0} bright samples, fb={size0}, dpr={dpr}")
check("framebuffer size matches widget*dpr",
      abs(size0[0] - round(plotter.width() * dpr)) <= 2 and abs(size0[1] - round(plotter.height() * dpr)) <= 2,
      f"fb={size0} widget={plotter.width()}x{plotter.height()} dpr={dpr}")

# ---------------------------------------------------------------- float/dock soak
failures = []
t_soak = time.perf_counter()
for i in range(args.cycles):
    vp_dock.setFloating()
    pump(80)
    nf, _ = cone_pixels("floating" if i == 0 else None)
    if nf <= 50:
        failures.append((i, "floating", nf))
    mgr.addDockWidget(ads.RightDockWidgetArea, vp_dock)
    if REAP:
        reap_floating()
    pump(80)
    nd, _ = cone_pixels("redocked" if i == 0 else None)
    if nd <= 50:
        failures.append((i, "docked", nd))
    if (nf <= 50 or nd <= 50) and len(failures) <= 2:
        cam = plotter.camera
        print(f"  FAIL@{i}: gl={GLCOUNT} obs={len(plotter._vtk_observers)} ctx={plotter._ctx is not None} "
              f"vis={plotter.isVisible()} size={plotter.width()}x{plotter.height()} "
              f"ready={plotter.ren_win.GetReadyForRendering()} rwsize={plotter.ren_win.GetSize()} "
              f"cam_pos={tuple(round(v, 2) for v in cam.position)} actors={len(plotter.renderer.actors)}")
        a = vp_dock.dockAreaWidget()
        print(f"     dock: closed={vp_dock.isClosed()} autohide={vp_dock.isAutoHide()} floating={vp_dock.isFloating()} "
              f"inFloatingCont={vp_dock.isInFloatingContainer()} tabbed={vp_dock.isTabbed()} current={vp_dock.isCurrentTab()} "
              f"area={'none' if a is None else (a.isVisible(), a.dockWidgetsCount(), a.width(), a.height())} "
              f"dock_vis={vp_dock.isVisible()} dock_size={vp_dock.width()}x{vp_dock.height()} "
              f"areas_in_main={mgr.dockAreaCount()} openAreas={len(mgr.openedDockAreas())} "
              f"rootSplitterSizes={mgr.rootSplitter().sizes() if mgr.rootSplitter() else None}")
        cone_pixels(f"fail{i}")
    if (i + 1) % 20 == 0:
        print(f"  cycle {i + 1}/{args.cycles}: float={nf} dock={nd}")
report["soak_s"] = round(time.perf_counter() - t_soak, 1)
from PySide6.QtGui import QGuiApplication  # noqa: E402
report["qwindows_after_soak"] = len(QGuiApplication.allWindows())
check("no native-window leak after soak", len(QGuiApplication.allWindows()) <= 8, report["qwindows_after_soak"])
check(f"{args.cycles} float/dock cycles keep rendering", not failures, failures[:5] or "no blank frames")

# terminal still alive after the soak
js_result.clear()
view.page().runJavaScript("term.buffer.active.getLine(0).translateToString(true)", 0, _js_cb)
pump(500)
check("xterm.js still alive after soak", str(js_result.get("line0", "")).startswith("hello"), js_result.get("line0"))

# ---------------------------------------------------------------- float the terminal next to the viewport
term_dock.setFloating()
pump(1500)
js_result.clear()
view.page().runJavaScript("term.buffer.active.getLine(0).translateToString(true)", 0, _js_cb)
pump(500)
check("xterm.js survives floating", str(js_result.get("line0", "")).startswith("hello"), js_result.get("line0"))
mgr.addDockWidget(ads.BottomDockWidgetArea, term_dock)
pump(500)

# ---------------------------------------------------------------- tabs in the central area?
extra = ads.CDockWidget(mgr, "Editor: controlDict")
extra.setWidget(QLabel("editor"))
try:
    mgr.addDockWidgetTabToArea(extra, central_area)
    pump(200)
    same = extra.dockAreaWidget() is not None and extra.dockAreaWidget() == central.dockAreaWidget()
    check("panels can be tabbed into the central area", same,
          f"area dock count={central.dockAreaWidget().dockWidgetsCount() if central.dockAreaWidget() else '?'}")
except Exception as exc:
    check("panels can be tabbed into the central area", False, repr(exc))

# ---------------------------------------------------------------- save/restore round trip
state = mgr.saveState()
report["state_bytes"] = len(bytes(state))
ok_restore = mgr.restoreState(state)
pump(300)
n_after, _ = cone_pixels("after_restore")
check("saveState/restoreState round trip keeps viewport rendering", bool(ok_restore) and n_after > 50,
      f"restore={ok_restore} bright={n_after}")

# ---------------------------------------------------------------- Qt warnings
bad = [m for m in qt_messages if any(k in m for k in ("OpenGL", "QRhi", "WebEngine", "GL_", "context"))]
report["qt_messages"] = qt_messages
check("no GL/RHI/WebEngine warnings from Qt", not bad, bad[:5] or None)

report["all_ok"] = all(c["ok"] for c in report["checks"].values())
(OUT / f"spike_a_{args.tag}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("\nALL OK" if report["all_ok"] else "\nSOME CHECKS FAILED", "->", OUT / f"spike_a_{args.tag}.json")

if args.keep_open:
    app.exec()
else:
    plotter.close()
    QTimer.singleShot(0, app.quit)
    app.exec()
sys.exit(0 if report["all_ok"] else 1)
