"""Diagnose the blank viewport after ~89 float/dock cycles: count floating containers / top-level
windows / GL contexts per cycle, and test whether deleting empty floating containers prevents it."""
import os, sys, time, argparse
os.environ.setdefault("QT_API", "pyside6")
ap = argparse.ArgumentParser(); ap.add_argument("--cycles", type=int, default=120)
ap.add_argument("--cleanup", action="store_true", help="delete empty floating containers after re-dock")
args = ap.parse_args()
from PySide6.QtCore import QCoreApplication, Qt
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
import ctypes
def gdi_user():
    h = ctypes.windll.kernel32.GetCurrentProcess()
    return ctypes.windll.user32.GetGuiResources(h, 0), ctypes.windll.user32.GetGuiResources(h, 1)
def pump(ms=60):
    end = time.perf_counter() + ms/1000
    while time.perf_counter() < end: app.processEvents(); time.sleep(0.005)
win = QMainWindow(); win.resize(1400, 850); mgr = ads.CDockManager(win)
c = ads.CDockWidget(mgr, "Workbench"); c.setWidget(QLabel("central")); mgr.setCentralWidget(c)
vp = ads.CDockWidget(mgr, "3D Viewport"); pl = QtInteractor(vp, auto_update=False)
pl.add_mesh(pv.Cone(), color="white"); pl.set_background("#1b1c1f"); vp.setWidget(pl)
mgr.addDockWidget(ads.RightDockWidgetArea, vp); win.show(); pump(400)
def bright():
    pl.render(); pump(20); img = pl.grabFramebuffer(); n = 0
    for y in range(0, img.height(), 6):
        for x in range(0, img.width(), 6):
            q = img.pixelColor(x, y); n += (q.red()+q.green()+q.blue()) > 400
    return n, img.width(), img.height()
first_fail = None
for i in range(args.cycles):
    vp.setFloating(); pump(60)
    fl = bright()
    mgr.addDockWidget(ads.RightDockWidgetArea, vp); pump(60)
    if args.cleanup:
        for f in list(mgr.floatingWidgets()):
            if not f.hasTopLevelDockWidget() and f.dockWidgets() == []:
                f.deleteLater()
        pump(30)
    dk = bright()
    nfloat = len(mgr.floatingWidgets())
    tops = len([w for w in QApplication.topLevelWidgets()])
    wins = len(QGuiApplication.allWindows())
    gdi, user = gdi_user()
    if i % 10 == 0 or (dk[0] < 20 and first_fail is None):
        print(f"cycle {i:3d} float={fl[0]:4d} dock={dk[0]:4d} fb={dk[1]}x{dk[2]} floatingContainers={nfloat} "
              f"topLevelWidgets={tops} QWindows={wins} GDI={gdi} USER={user} ctx={pl.context() is not None}")
    if dk[0] < 20 and first_fail is None:
        first_fail = i
print("first blank cycle:", first_fail)
pl.close(); app.quit()
