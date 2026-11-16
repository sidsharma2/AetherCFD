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
# build.py --cpu-only (experimental): no CuPy, no NVIDIA CUDA libraries; GPU features fall back to numpy
CPU_ONLY = os.environ.get("AETHERCFD_BUILD_CPU_ONLY") == "1"

# version resource (Properties > Details) from src/aethercfd/__init__.py, via packaging/build.py
import importlib.util

_spec = importlib.util.spec_from_file_location("aether_build", ROOT / "packaging" / "build.py")
_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_build)
VERSION = _build.read_version(ROOT)

import winpty as _winpty

WINPTY = Path(_winpty.__file__).parent

hidden = (collect_submodules("aethercfd", filter=lambda m: ".tests" not in m)
          + collect_submodules("vtkmodules", filter=lambda m: not m.startswith(("vtkmodules.test", "vtkmodules.web")))
          + ["PySide6QtAds", "winpty", "pyvistaqt", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
             "PySide6.QtOpenGLWidgets", "PySide6.QtSvg"]
          # savefig() picks its backend by name at runtime: PDF/PNG export of plots
          + [f"matplotlib.backends.backend_{b}" for b in ("agg", "pdf", "svg", "ps", "pgf")])
datas = (collect_data_files("aethercfd", excludes=["**/node_modules/**", "**/__pycache__/**"])
         + collect_data_files("qtawesome") + collect_data_files("pyvista") + collect_data_files("pyqtgraph")
         + collect_data_files("cantera"))  # mechanisms (gri30.yaml, ...)
# the PINN worker runs as a script in the separate PyTorch environment: ship it as a file, not bytecode
datas.append((str(ROOT / "src" / "aethercfd" / "ml" / "pinn_worker.py"), "aethercfd/ml"))
# run by the cfd_agents' own interpreter (aethercfd.services.cfd_agents): must exist as a .py file
datas.append((str(ROOT / "src" / "aethercfd" / "services" / "cfd_agents" / "_runner.py"), "aethercfd/services/cfd_agents"))
binaries = collect_dynamic_libs("winpty") + [(str(p), "winpty") for p in WINPTY.glob("*.exe")]

# CUDA compute (left out with CPU_ONLY): CuPy + the CUDA runtime / NVRTC DLLs from the nvidia-* wheels, kept at
# the same relative paths (nvidia/<lib>/bin) so CuPy's library finder locates them inside the bundle
import site

hidden += collect_submodules("cantera")
binaries += collect_dynamic_libs("cantera")
for sp in ([] if CPU_ONLY else site.getsitepackages()):
    nv = Path(sp) / "nvidia"
    if nv.is_dir():
        for dll in nv.glob("*/bin/*.dll"):
            binaries.append((str(dll), str(dll.parent.relative_to(Path(sp)))))
hidden += [] if CPU_ONLY else (
             collect_submodules("cupy", filter=lambda m: ".testing" not in m and "tests" not in m)
           + collect_submodules("cupy_backends") + collect_submodules("cupyx", filter=lambda m: "tests" not in m)
           + collect_submodules("cuda.pathfinder")
           # stdlib imported only from CuPy's compiled Cython modules (invisible to the analysis):
           # without it "import cupy" fails in the bundle and every GPU feature silently uses the CPU
           + ["graphlib"]
           # "import cupy" registers cupy.testing as a lazy module (find_spec must succeed; never executed)
           + ["cupy.testing"])
# CUDA headers for NVRTC kernel compilation (found by cuda.pathfinder under nvidia/<lib>/include)
if not CPU_ONLY:
    for sp in site.getsitepackages():
        for inc in (Path(sp) / "nvidia").glob("*/include"):
            datas.append((str(inc), str(inc.relative_to(Path(sp)))))
    datas += collect_data_files("cuda.pathfinder")
    datas += collect_data_files("cupy") + collect_data_files("cupyx")
    binaries += collect_dynamic_libs("cupy") + collect_dynamic_libs("cupy_backends")
excludes = ["tkinter", "PyQt5", "PyQt6", "IPython", "jupyter_client", "notebook", "pytest", "hypothesis",
            "foamlib", "import_linter", "importlinter", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
            "PySide6.QtQuick3D", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
            "PySide6.QtGraphs", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
            "PySide6.QtSerialPort", "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtPdf",
            "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects"]
if CPU_ONLY:
    excludes += ["cupy", "cupyx", "cupy_backends", "cuda", "fastrlock", "nvidia"]

gui_a = Analysis([str(ROOT / "packaging" / "entry_gui.py")], pathex=[str(SRC)], binaries=binaries, datas=datas,
                 hiddenimports=hidden, excludes=excludes, noarchive=False)
cli_a = Analysis([str(ROOT / "packaging" / "entry_cli.py")], pathex=[str(SRC)],
                 hiddenimports=collect_submodules("aethercfd.scripting") + collect_submodules("aethercfd.commands")
                 + ["aethercfd.ml.envsetup"],  # `aethercfd-cli ml-setup`
                 excludes=excludes + ["PySide6", "vtkmodules", "pyvista", "pyvistaqt", "pyqtgraph", "matplotlib",
                                     # `doctor` checks CUDA through AetherCFD.exe, which has the full CuPy
                                     "cupy", "cupyx", "cupy_backends"])

gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True, name="AetherCFD", icon=ICON,
              console=False, upx=False,
              version=_build.exe_version_info(VERSION, "AetherCFD", "AetherCFD Studio"))
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True, name="aethercfd-cli", icon=ICON,
              console=True, upx=False,
              version=_build.exe_version_info(VERSION, "aethercfd-cli", "AetherCFD Studio command line"))
coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas, cli_exe, cli_a.binaries, cli_a.datas,
               name="AetherCFD", upx=False)
