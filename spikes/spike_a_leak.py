import os, sys, time, collections
os.environ.setdefault("QT_API", "pyside6")
from PySide6.QtCore import QCoreApplication, Qt, QEvent
DEFER = "--defer" in sys.argv
from PySide6.QtGui import QSurfaceFormat, QGuiApplication
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from pyvistaqt.rwi import _default_format
QSurfaceFormat.setDefaultFormat(_default_format())
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
app = QApplication(sys.argv)
import PySide6QtAds as ads, pyvista as pv
from pyvistaqt import QtInteractor
def pump(ms=60):
    end = time.perf_counter() + ms/1000
    while time.perf_counter() < end:
        app.processEvents(); time.sleep(0.005)
        if DEFER: QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
win = QMainWindow(); win.resize(1400, 850); mgr = ads.CDockManager(win)
c = ads.CDockWidget(mgr, "Workbench"); c.setWidget(QLabel("central")); mgr.setCentralWidget(c)
vp = ads.CDockWidget(mgr, "3D Viewport")
use_gl = "--label" not in sys.argv
if use_gl:
    pl = QtInteractor(vp, auto_update=False); pl.add_mesh(pv.Cone()); vp.setWidget(pl)
else:
    vp.setWidget(QLabel("plain"))
mgr.addDockWidget(ads.RightDockWidgetArea, vp); win.show(); pump(300)
base = collections.Counter(type(w).__name__ + ("(hidden)" if not w.isVisible() else "") for w in QApplication.topLevelWidgets())
nw0 = len(QGuiApplication.allWindows())
for i in range(5):
    vp.setFloating(); pump(60); mgr.addDockWidget(ads.RightDockWidgetArea, vp); pump(60)
after = collections.Counter(type(w).__name__ + ("(hidden)" if not w.isVisible() else "") for w in QApplication.topLevelWidgets())
print("use_gl:", use_gl, "QWindows", nw0, "->", len(QGuiApplication.allWindows()))
print("new top-level widgets after 5 cycles:", dict(after - base))
for w in QGuiApplication.allWindows():
    pass
print("window classes:", collections.Counter(type(w).__name__ + ":" + (w.objectName() or "") for w in QGuiApplication.allWindows()))
app.quit()
