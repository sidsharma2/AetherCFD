"""The viewport's render widget: pyvistaqt's QtInteractor, made to recover a lost GL context.

pyvistaqt 0.13 renders through a QOpenGLWidget. VTK asks for its context with ``WindowMakeCurrentEvent``, and
pyvistaqt answers with ``self._ctx.makeCurrent(self._surface)``, where ``_surface`` is the Python wrapper of the
offscreen surface Qt made for the widget, taken once in ``initializeGL``. That wrapper can go stale while the
surface itself is fine: after a perspective switch closed and reopened the viewport's dock, ordinary Python code
that touches other Qt objects (the Visualization panel and the layout snapshot walk every dock; a splitter's
``sizes()``) made the next ``makeCurrent`` raise ``RuntimeError``. pyvistaqt then forgets the context for good:
``IsCurrent()`` stays False even after ``MakeCurrent()``, VTK skips every render, the viewport shows a blank
frame and ``screenshot()`` raises "Render window is not current" (tests/studio/test_viewport_gl_recovery.py).

``ViewportInteractor`` re-arms the widget instead: when the context it holds is gone but the widget still has a
live one, it takes that context again, with an offscreen surface it owns (a Python-owned wrapper that nothing
else can invalidate). VTK renders into its own framebuffers, so any surface compatible with the context will
do, the same as pyvistaqt's own pre-show fallback. It also puts back the VTK observers when Qt destroyed and
recreated the context (``_cleanup_context`` removes them for good; the next ``initializeGL`` needs them).
Nothing is re-armed once the widget is closing (ADR 0008 rule 4), and only the GUI thread does any of this.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from pyvistaqt import QtInteractor

log = logging.getLogger(__name__)


def _alive(obj) -> bool:
    if obj is None:
        return False
    try:
        import shiboken6

        return shiboken6.isValid(obj)
    except Exception:
        return False


class ViewportInteractor(QtInteractor):
    """QtInteractor that recovers its GL context (see the module docstring)."""

    _aether_closing = False
    _aether_ctx_dying = False
    _aether_surface = None
    recoveries = 0  # how often the context was re-armed (tests, the sweep's log)

    # ------------------------------------------------------------------ context recovery
    def _gui_thread(self) -> bool:
        app = QApplication.instance()
        return app is not None and QThread.currentThread() is app.thread()

    def _own_surface(self, ctx):
        from PySide6.QtGui import QOffscreenSurface

        s = self._aether_surface
        if _alive(s) and s.isValid() and s.format().version() == ctx.format().version():
            return s
        s = QOffscreenSurface()
        s.setFormat(ctx.format())
        s.create()
        if not s.isValid():
            return None
        self._aether_surface = s  # Python-owned: its wrapper cannot go stale under us
        return s

    def recover_context(self) -> bool:
        """Re-arm a widget whose context pyvistaqt has lost track of. Returns True when it re-armed."""
        if self._aether_closing or self._aether_ctx_dying or not self._gui_thread():
            return False
        ctx, surface = getattr(self, "_ctx", None), getattr(self, "_surface", None)
        if _alive(ctx) and _alive(surface):
            return False  # healthy (the normal path: nothing to do)
        try:
            if not self.isValid():  # never shown yet: pyvistaqt's own pre-show fallback serves it
                return False
            live = self.context()
            if not _alive(live) or not live.isValid():
                return False
            own = self._own_surface(live)
            if own is None:
                return False
            from pyvistaqt.rwi import _GLAPI

            self._ctx, self._surface, self._gl = live, own, _GLAPI(live)
            self._fallback = None
            self._fallback_allowed = True
        except Exception:  # private pyvistaqt state changed shape: leave it as it is
            log.exception("could not re-arm the viewport's GL context")
            return False
        self.recoveries += 1
        log.info("viewport GL context re-armed (%d)", self.recoveries)
        return True

    def _cb_make_current(self, obj, evt):  # pyvistaqt: services VTK's WindowMakeCurrentEvent
        self.recover_context()
        return super()._cb_make_current(obj, evt)

    def _cleanup_context(self):  # pyvistaqt: Qt is destroying the widget's context
        self._aether_ctx_dying = True
        try:
            super()._cleanup_context()
        finally:
            self._aether_ctx_dying = False
        if not self._aether_closing:
            self._rearm_observers()  # Qt creates a new context next; initializeGL needs the observers

    def _rearm_observers(self) -> None:
        if getattr(self, "_vtk_observers", None):
            return
        try:
            rw, iren = self._RenderWindow, self._Iren
            if rw is None or iren is None:
                return
            for obj, event, handler in ((rw, "WindowMakeCurrentEvent", self._cb_make_current),
                                        (rw, "WindowIsCurrentEvent", self._cb_is_current),
                                        (rw, "WindowFrameEvent", self._cb_frame),
                                        (rw, "CursorChangedEvent", self.CursorChangedEvent),
                                        (iren, "CreateTimerEvent", self.CreateTimer),
                                        (iren, "DestroyTimerEvent", self.DestroyTimer)):
                self._vtk_observers.append((obj, obj.AddObserver(event, handler)))
            self._fallback_allowed = True
        except Exception:
            log.exception("could not re-arm the viewport's VTK observers")

    # ------------------------------------------------------------------ close: never re-arm again
    def closeEvent(self, evt):
        self._aether_closing = True
        return super().closeEvent(evt)

    def close(self):
        self._aether_closing = True
        return super().close()
