import os, sys, time
os.environ.setdefault("QT_API", "pyside6")
from PySide6.QtCore import QCoreApplication, Qt, QEvent
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from pyvistaqt.rwi import _default_format, QVTKRenderWindowInteractor as RWI
QSurfaceFormat.setDefaultFormat(_default_format())
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
app = QApplication(sys.argv)
import PySide6QtAds as ads, pyvista as pv
from pyvistaqt import QtInteractor
counts = {"init": 0, "cleanup": 0}
_oi, _oc = RWI.initializeGL, RWI._cleanup_context
def ii(self): counts["init"] += 1; return _oi(self)
def cc(self): counts["cleanup"] += 1; return _oc(self)
RWI.initializeGL, RWI._cleanup_context = ii, cc
def pump(ms=60):
    end = time.perf_counter() + ms/1000
    while time.perf_counter() < end:
        app.processEvents(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete); time.sleep(0.005)
win = QMainWindow(); win.resize(1400, 850); mgr = ads.CDockManager(win)
c = ads.CDockWidget(mgr, "W"); c.setWidget(QLabel("c")); mgr.setCentralWidget(c)
vp = ads.CDockWidget(mgr, "VP"); pl = QtInteractor(vp, auto_update=False); pl.add_mesh(pv.Cone(), color="white")
pl.set_background("#1b1c1f"); vp.setWidget(pl); mgr.addDockWidget(ads.RightDockWidgetArea, vp); win.show(); pump(400)
if "--webengine" in sys.argv:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    t = ads.CDockWidget(mgr, "T"); v = QWebEngineView(); v.setHtml("<body style='background:#222'>x</body>"); t.setWidget(v)
    mgr.addDockWidget(ads.BottomDockWidgetArea, t); pump(1500)
def bright():
    pl.render(); pump(20); img = pl.grabFramebuffer(); n = 0
    for y in range(0, img.height(), 6):
        for x in range(0, img.width(), 6):
            q = img.pixelColor(x, y); n += (q.red()+q.green()+q.blue()) > 400
    return n
last = None
for i in range(60):
    vp.setFloating(); pump(60); f = bright()
    mgr.addDockWidget(ads.RightDockWidgetArea, vp); pump(60); d = bright()
    state = (counts["init"], counts["cleanup"], len(pl._vtk_observers), pl._ctx is not None, f > 20, d > 20)
    if state != last:
        print(f"cycle {i:2d}: initializeGL={state[0]} cleanup={state[1]} vtk_observers={state[2]} ctx={state[3]} float_ok={state[4]} dock_ok={state[5]}")
        last = state
pl.close(); app.quit()
