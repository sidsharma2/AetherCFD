# Third-party notices

AetherCFD Studio is licensed under the Apache License 2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)). It is
built on the work of many other projects. This file lists the third-party software that the packaged app
(`AetherCFD.exe` and the release zip) contains, and the external programs it runs. Versions are the ones the
release is built with (`uv.lock`). The release zip holds the license text of each component in its `licenses/`
folder.

## Bundled in the packaged app

| Component | Version | License | Project |
|---|---|---|---|
| Qt for Python: PySide6, shiboken6 (Qt 6.11, including Qt WebEngine) | 6.11.2 | LGPL-3.0-only (used under the LGPL) | https://pyside.org |
| PySide6-QtAds (Qt Advanced Docking System) | 5.1.1 | LGPL-2.1 | https://github.com/mborgerson/pyside6_qtads |
| VTK | 9.7.1 | BSD-3-Clause | https://vtk.org |
| PyVista | 0.49.0 | MIT | https://docs.pyvista.org |
| pyvistaqt | 0.13.1 | MIT | https://github.com/pyvista/pyvistaqt |
| pyqtgraph | 0.14.0 | MIT | https://www.pyqtgraph.org |
| QtPy | 2.4.3 | MIT | https://github.com/spyder-ide/qtpy |
| QtAwesome | 1.4.2 | MIT | https://github.com/spyder-ide/qtawesome |
| Codicons (icon font, through QtAwesome) | 0.0.36 | CC-BY-4.0, © Microsoft Corporation | https://github.com/microsoft/vscode-codicons |
| Material Design Icons (icon font, through QtAwesome) | 6.9.96 | Apache-2.0 | https://pictogrammers.com/library/mdi/ |
| xterm.js with the fit, search and unicode11 add-ons | 6.0.0 | MIT | https://xtermjs.org |
| NumPy | 2.5.3 | BSD-3-Clause (and 0BSD, MIT, Zlib, CC0-1.0 parts) | https://numpy.org |
| SciPy | 1.18.1 | BSD-3-Clause | https://scipy.org |
| Matplotlib (with the DejaVu and STIX fonts) | 3.11.2 | Matplotlib License (PSF-based) | https://matplotlib.org |
| Cantera (with the GRI-Mech 3.0 mechanism file) | 3.2.0 | BSD-3-Clause | https://cantera.org |
| CuPy | 14.2.0 | MIT | https://cupy.dev |
| NVIDIA CUDA runtime, NVRTC, cuBLAS, cuSOLVER, cuSPARSE, cuRAND, nvJitLink | 12.9 | NVIDIA Software License Agreement (redistributable components) | https://developer.nvidia.com/cuda-zone |
| nvidia-ml-py | 13.615.71 | BSD-3-Clause | https://pypi.org/project/nvidia-ml-py/ |
| pydantic, pydantic-core | 2.13.5, 2.46.5 | MIT | https://github.com/pydantic/pydantic |
| psutil | 7.2.2 | BSD-3-Clause | https://github.com/giampaolo/psutil |
| platformdirs | 4.12.3 | MIT | https://github.com/tox-dev/platformdirs |
| tomli-w | 1.2.0 | MIT | https://github.com/hukkin/tomli-w |
| pywinpty (with winpty) | 3.0.5 | MIT | https://github.com/andfoy/pywinpty |
| RapidFuzz | 3.14.6 | MIT | https://github.com/rapidfuzz/RapidFuzz |
| watchdog | 6.0.0 | Apache-2.0 | https://github.com/gorakhargosh/watchdog |
| requests | 2.34.2 | Apache-2.0 | https://requests.readthedocs.io |
| certifi | 2026.7.22 | MPL-2.0 (unmodified) | https://github.com/certifi/python-certifi |
| Pillow | 12.3.0 | MIT-CMU | https://python-pillow.org |
| pooch | 1.9.0 | BSD-3-Clause | https://www.fatiando.org/pooch |
| Python | 3.12 | PSF License 2.0 | https://www.python.org |
| OpenSSL (part of Python) | 3 | Apache-2.0 | https://www.openssl.org |
| PyInstaller bootloader and run-time hooks | 6.22.3 | GPL-2.0-or-later with the bootloader exception; hooks Apache-2.0 | https://pyinstaller.org |
| Microsoft Visual C++ and Universal C runtime | | Microsoft redistributable terms | https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files |

Other Python packages that these depend on are bundled too. Each one's license is in the release zip's
`licenses/` folder.

### Qt, PySide6 and QtAds (LGPL)

The packaged app uses Qt, PySide6 and the Qt Advanced Docking System under the GNU Lesser General Public
License (LGPL-3.0 for Qt and PySide6, LGPL-2.1 for QtAds). These libraries are not modified. They ship as
separate DLL and `.pyd` files in `AetherCFD\_internal`, and you may replace them with your own builds of the
same versions. Their source code is available from the Qt Project (https://download.qt.io), from Qt for Python
(https://code.qt.io/cgit/pyside/pyside-setup.git) and from https://github.com/githubuser0xFFFF/Qt-Advanced-Docking-System
and https://github.com/mborgerson/pyside6_qtads. The LGPL and GPL texts are in the release zip's `licenses/` folder.
The Apache-2.0 license of AetherCFD allows the modification and reverse engineering that the LGPL requires for
this purpose.

## External programs that AetherCFD runs (not bundled)

| Program | License | Project |
|---|---|---|
| OpenFOAM (ESI-OpenCFD, www.openfoam.com, or The OpenFOAM Foundation, openfoam.org) | GPL-3.0-or-later | https://www.openfoam.com, https://openfoam.org |
| Windows Subsystem for Linux and a Linux distribution | Microsoft and distribution terms | https://learn.microsoft.com/windows/wsl/ |
| ParaView (optional, for ray-traced renders) | BSD-3-Clause | https://www.paraview.org |
| FreeCAD (optional, for CAD import) | LGPL-2.1-or-later | https://www.freecad.org |
| PyTorch (optional, in a separate environment for PINN training) | BSD-3-Clause | https://pytorch.org |
| Claude Code or the OpenAI Codex CLI (optional copilot, your own account) | their providers' terms | https://claude.com/claude-code, https://github.com/openai/codex |

OPENFOAM® is a registered trade mark of OpenCFD Limited, producer and distributor of the OpenFOAM software via
www.openfoam.com. AetherCFD Studio is not approved or endorsed by OpenCFD Limited or by The OpenFOAM Foundation.

## In the source repository only

The test fixtures in `tests/fixtures/cases/` are copies of OpenFOAM tutorials under GPL-3.0-or-later (see
[tests/fixtures/cases/README.md](tests/fixtures/cases/README.md)). The development tools (pytest, pytest-qt,
Hypothesis, ruff, import-linter, pyright, PyInstaller) are not part of the packaged app.
