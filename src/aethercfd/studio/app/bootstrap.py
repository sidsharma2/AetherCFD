"""Process bootstrap. Everything here must run BEFORE QApplication exists (ADR 0004):

- the process-wide OpenGL surface format (exactly pyvistaqt's), so every GL widget is compatible
  with every top-level window it may be floated into;
- AA_ShareOpenGLContexts, so a viewport keeps its GL context when docked/floated between windows,
  and QtWebEngine (the terminal) works;
- Qt Quick/WebEngine forced onto OpenGL, the same graphics API as the VTK viewport.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import sys
from pathlib import Path

_BOOTSTRAPPED = False
log = logging.getLogger("aethercfd.studio")


def crash_dir() -> Path:
    from aethercfd.core.settings import cache_dir

    d = cache_dir() / "crash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def select_gpu(choice: str | None = None) -> str:
    """Pick the GPU for OpenGL before any GL context exists. On NVIDIA Optimus laptops the integrated GPU
    drives the screen and renders by default; SHIM_MCCOMPAT asks the NVIDIA driver to render THIS process
    on the discrete GPU (no system setting is changed; frames are copied to the display as usual)."""
    if choice is None:
        try:
            from aethercfd.core.settings import Settings

            choice = Settings.load().gpu
        except Exception:
            choice = "high-performance"
    if sys.platform != "win32" or "SHIM_MCCOMPAT" in os.environ:
        return os.environ.get("SHIM_MCCOMPAT", "")
    if choice == "high-performance":
        os.environ["SHIM_MCCOMPAT"] = "0x800000001"
    elif choice == "integrated":
        os.environ["SHIM_MCCOMPAT"] = "0x800000000"
    return os.environ.get("SHIM_MCCOMPAT", "")


def bootstrap() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    os.environ.setdefault("QT_API", "pyside6")
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    select_gpu()
    # crash forensics: native faults in GL/VTK code land here with all thread stacks
    fh = open(crash_dir() / "faulthandler.log", "a", encoding="utf-8")  # noqa: SIM115 (kept open on purpose)
    faulthandler.enable(fh, all_threads=True)
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AetherCFD.Studio")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
    from pyvistaqt.rwi import _default_format

    QSurfaceFormat.setDefaultFormat(_default_format())
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)
    _BOOTSTRAPPED = True
