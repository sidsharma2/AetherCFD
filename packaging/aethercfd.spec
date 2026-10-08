# PyInstaller spec -- onedir build of AetherCFD Studio (see packaging/build.py).
#
#   dist/AetherCFD/AetherCFD.exe       windowed GUI (also serves --run-module for child processes)
#   dist/AetherCFD/aethercfd-cli.exe   console CLI (doctor / open / run / runs / control / set)
#
# Panels are imported by name at runtime, so every aethercfd submodule is a hidden import.
# ruff: noqa
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
ICON = str(SRC / "aethercfd" / "studio" / "resources" / "aethercfd.ico")

import winpty as _winpty

WINPTY = Path(_winpty.__file__).parent

hidden = (collect_submodules("aethercfd", filter=lambda m: ".tests" not in m)
          + collect_submodules("vtkmodules", filter=lambda m: not m.startswith(("vtkmodules.test", "vtkmodules.web")))
          + ["PySide6QtAds", "winpty", "pyvistaqt", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
             "PySide6.QtOpenGLWidgets", "PySide6.QtSvg"]
          # savefig() picks its backend by name at runtime: PDF/PNG export of plots
          + [f"matplotlib.backends.backend_{b}" for b in ("agg", "pdf", "svg", "ps", "pgf")])
datas = (collect_data_files("aethercfd", excludes=["**/node_modules/**", "**/__pycache__/**"])
         + collect_data_files("qtawesome") + collect_data_files("pyvista") + collect_data_files("pyqtgraph"))
binaries = collect_dynamic_libs("winpty") + [(str(p), "winpty") for p in WINPTY.glob("*.exe")]
excludes = ["tkinter", "PyQt5", "PyQt6", "IPython", "jupyter_client", "notebook", "pytest", "hypothesis",
            "foamlib", "import_linter", "importlinter", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
            "PySide6.QtQuick3D", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
            "PySide6.QtGraphs", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
            "PySide6.QtSerialPort", "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtPdf",
            "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects"]

gui_a = Analysis([str(ROOT / "packaging" / "entry_gui.py")], pathex=[str(SRC)], binaries=binaries, datas=datas,
                 hiddenimports=hidden, excludes=excludes, noarchive=False)
cli_a = Analysis([str(ROOT / "packaging" / "entry_cli.py")], pathex=[str(SRC)],
                 hiddenimports=collect_submodules("aethercfd.scripting") + collect_submodules("aethercfd.commands"),
                 excludes=excludes + ["PySide6", "vtkmodules", "pyvista", "pyvistaqt", "pyqtgraph", "matplotlib"])

gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True, name="AetherCFD", icon=ICON,
              console=False, upx=False)
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True, name="aethercfd-cli", icon=ICON,
              console=True, upx=False)
coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas, cli_exe, cli_a.binaries, cli_a.datas,
               name="AetherCFD", upx=False)
