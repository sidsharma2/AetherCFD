"""``aethercfd doctor``: one command that says what works on this PC and how to fix what does not.

Every line is ok / warn / FAIL with a one-line fix. FAIL means a required part is broken (the app, its
Python packages, the terminal files, WSL with OpenFOAM, writable folders); warn means an optional part is
missing (GPU, ParaView, FreeCAD, the PyTorch environment, the copilot CLI). The exit code is 1 when any
line is FAIL. Nothing is installed or changed; a stopped WSL distro is only started with ``--probe``.

In the packaged app, ``aethercfd-cli.exe doctor`` checks CUDA through ``AetherCFD.exe --run-module
aethercfd.scripting.doctor --gpu-json FILE``, because only the GUI executable carries CuPy.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from aethercfd import __version__

OK, WARN, FAIL = "ok", "warn", "fail"
NO_WINDOW = 0x08000000

# (import name, distribution name) pairs the studio needs at runtime
RUNTIME_DEPS: list[tuple[str, str]] = [
    ("pydantic", "pydantic"),
    ("watchdog", "watchdog"),
    ("platformdirs", "platformdirs"),
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("matplotlib", "matplotlib"),
    ("cantera", "cantera"),
    ("PySide6", "PySide6"),
    ("PySide6QtAds", "PySide6-QtAds"),
    ("vtk", "vtk"),
    ("pyvista", "pyvista"),
    ("pyvistaqt", "pyvistaqt"),
    ("qtpy", "QtPy"),
    ("pyqtgraph", "pyqtgraph"),
    ("qtawesome", "qtawesome"),
    ("winpty", "pywinpty"),
    ("rapidfuzz", "rapidfuzz"),
    ("psutil", "psutil"),
]
UV_SYNC_FIX = "in the AetherCFD source folder run `uv sync`"


@dataclass
class Check:
    section: str
    name: str
    status: str  # ok | warn | fail
    detail: str = ""
    fix: str = ""


# ---------------------------------------------------------------------------------------------- helpers
def is_frozen() -> bool:
    from aethercfd.core.runtime import is_frozen as _frozen

    return _frozen()


def _first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


def load_settings():
    """(Settings, None), or (usable Settings, error text) when settings.toml cannot be read or held a value
    that had to be dropped (Settings.load recovers from both and says so in ``load_problem``)."""
    from aethercfd.core.settings import Settings

    try:
        s = Settings.load()
        return s, s.load_problem
    except Exception as exc:
        return Settings(), f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------------------------- app
def check_app(gui_exe: Path | None = None) -> list[Check]:
    s = "App"
    out = [Check(s, "AetherCFD", OK, f"{__version__} ({'installed app' if is_frozen() else 'source checkout'})")]
    if is_frozen():
        gui = gui_exe or Path(sys.executable).with_name("AetherCFD.exe")
        out.append(Check(s, "GUI executable", OK, str(gui)) if gui.is_file() else
                   Check(s, "GUI executable", FAIL, f"{gui} is missing",
                         "reinstall the app (Install AetherCFD.cmd from the release zip)"))
        return out
    ver = sys.version_info
    if (ver.major, ver.minor) != (3, 12):
        out.append(Check(s, "Python", FAIL, f"{platform.python_version()}; AetherCFD needs 3.12",
                         "run it with `uv run`, which uses the Python that uv installs for this project"))
    else:
        out.append(Check(s, "Python", OK, f"{platform.python_version()} ({sys.executable})"))
    return out


def check_packages(deps: list[tuple[str, str]] = RUNTIME_DEPS) -> list[Check]:
    """Each runtime dependency is installed and imports (source checkout only)."""
    os.environ.setdefault("QT_API", "pyside6")
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    s = "Python packages"
    out = []
    for mod, dist in deps:
        try:
            version = md.version(dist)
        except md.PackageNotFoundError:
            out.append(Check(s, dist, FAIL, "not installed", UV_SYNC_FIX))
            continue
        try:
            importlib.import_module(mod)
        except Exception as exc:  # report, never crash the doctor
            out.append(Check(s, dist, FAIL, f"{version}: {type(exc).__name__}: {_first_line(str(exc))}", UV_SYNC_FIX))
        else:
            out.append(Check(s, dist, OK, version))
    # PySide6 and PySide6-QtAds must be an exact pair; a mismatch crashes at import or later
    try:
        req = [r for r in (md.requires("PySide6-QtAds") or []) if r.startswith("PySide6-Essentials")]
        pyside = md.version("PySide6-Essentials")
        if req and pyside not in req[0]:
            out.append(Check(s, "PySide6 / QtAds pair", FAIL, f"QtAds wants {req[0]}, PySide6-Essentials is {pyside}",
                             UV_SYNC_FIX))
    except md.PackageNotFoundError:
        pass
    return out


def check_qt_vtk() -> list[Check]:
    """The pieces the window needs: Qt widgets, the web view of the terminal, VTK's OpenGL renderer."""
    s = "Qt and VTK"
    out = []
    for name, mod in (("Qt widgets", "PySide6.QtWidgets"), ("Qt web view (terminal)", "PySide6.QtWebEngineWidgets"),
                      ("VTK OpenGL renderer", "vtkmodules.vtkRenderingOpenGL2")):
        try:
            importlib.import_module(mod)
            out.append(Check(s, name, OK, mod))
        except Exception as exc:
            out.append(Check(s, name, FAIL, f"{type(exc).__name__}: {_first_line(str(exc))}", UV_SYNC_FIX))
    return out


# ---------------------------------------------------------------------------------------------- terminal
def check_terminal_assets(xterm_dir: Path | None = None) -> list[Check]:
    from aethercfd.core.runtime import XTERM_DIR, terminal_asset_problems, xterm_manifest

    d = xterm_dir or XTERM_DIR
    problems = terminal_asset_problems(d, verify_hashes=True)
    if problems:
        fix = ("reinstall the app" if is_frozen() else
               "restore the committed files: `git checkout -- src/aethercfd/studio/resources/xterm`")
        return [Check("Terminal", "xterm.js files", FAIL, f"{problems[0]}" +
                      (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""), fix)]
    ver = xterm_manifest(d)["packages"].get("@xterm/xterm", "?")
    return [Check("Terminal", "xterm.js files", OK, f"xterm.js {ver}, {d / 'dist'}")]


# ---------------------------------------------------------------------------------------------- GPU
def gpu_probe() -> dict:
    """CuPy on device 0, as core.gpu does it, but keeping the error text: {"ok", "info" | "error"}."""
    if os.environ.get("AETHERCFD_NO_CUDA"):
        return {"ok": False, "error": "turned off by the AETHERCFD_NO_CUDA environment variable"}
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import cupy as cp

            if cp.cuda.runtime.getDeviceCount() < 1:
                return {"ok": False, "error": "CUDA sees no GPU"}
            cp.zeros(1).sum()
            p = cp.cuda.runtime.getDeviceProperties(0)
            _free, total = cp.cuda.Device(0).mem_info
            return {"ok": True, "info": {"name": p["name"].decode(), "memory_gb": round(total / 2**30, 1),
                                         "runtime": cp.cuda.runtime.runtimeGetVersion(),
                                         "driver": cp.cuda.runtime.driverGetVersion()}}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {_first_line(str(exc))}"}


def frozen_gpu_probe(gui: Path, timeout: float = 180) -> dict:
    """Run gpu_probe() inside AetherCFD.exe (the CLI executable has no CuPy)."""
    with tempfile.TemporaryDirectory(prefix="aethercfd-doctor-") as tmp:
        out = Path(tmp) / "gpu.json"
        try:
            subprocess.run([str(gui), "--run-module", "aethercfd.scripting.doctor", "--gpu-json", str(out)],
                           capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
            return json.loads(out.read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the GPU check did not finish in {timeout:.0f} s"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"the GPU check in {gui.name} failed: {exc}"}


def check_gpu(settings, probe: Callable[[], dict] | None = None) -> list[Check]:
    s = "GPU"
    if probe is None:
        probe = (lambda: frozen_gpu_probe(Path(sys.executable).with_name("AetherCFD.exe"))) if is_frozen() else gpu_probe
    r = probe()
    pref = getattr(settings, "gpu_compute", "auto")
    if r.get("ok"):
        i = r["info"]
        rt = i.get("runtime", 0)
        note = " (settings: gpu_compute = cpu, so it is not used)" if pref == "cpu" else ""
        return [Check(s, "CUDA compute (CuPy)", OK,
                      f"{i['name']}, {i.get('memory_gb', '?')} GB, CUDA runtime {rt // 1000}.{rt % 1000 // 10}{note}")]
    driver = shutil.which("nvidia-smi") is not None
    if "AETHERCFD_NO_CUDA" in r.get("error", ""):
        fix = "remove the AETHERCFD_NO_CUDA environment variable to use the GPU"
    elif driver:
        fix = "update the NVIDIA driver (nvidia.com/drivers), then run doctor again"
    else:
        fix = "optional: without an NVIDIA GPU the GPU features run on the CPU (numpy)"
    return [Check(s, "CUDA compute (CuPy)", WARN, f"not available, GPU features use the CPU: {r.get('error', '')}",
                  fix)]


# ---------------------------------------------------------------------------------------------- WSL / OpenFOAM
def _cached_forks(name: str) -> tuple[list[str], float | None]:
    from aethercfd.core.settings import cache_dir

    f = cache_dir() / "forks" / f"{name}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return [d.get("label") or d.get("id") or "?" for d in data], f.stat().st_mtime
    except (OSError, ValueError, AttributeError):
        return [], None


def check_wsl(settings, *, probe: bool = False, list_fn=None, probe_fn=None) -> list[Check]:
    """WSL, its distros, the distro chosen for AetherCFD and the OpenFOAM versions in it."""
    from aethercfd.firstrun import detect

    s = "WSL and OpenFOAM"
    rep = detect.check_wsl(list_fn)
    if rep.problem == "no-wsl":
        return [Check(s, "WSL", FAIL, _first_line(rep.message),
                      "in an admin PowerShell run `wsl --install --no-distribution`, restart Windows (INSTALL.md, step 2)")]
    rows = rep.distros + rep.other
    listed = ", ".join(f"{d.name} ({d.state}{', default' if d.default else ''})" for d in rows) or "none"
    out = [Check(s, "WSL", OK, f"distros: {listed}")]
    if rep.problem == "no-distro":
        out.append(Check(s, "Linux distro", FAIL, "WSL has no distro AetherCFD can use",
                         "run `wsl --install -d Ubuntu-22.04` (INSTALL.md, step 2)"))
        return out
    allowed = list(settings.wsl.allowed_distros)
    if not allowed:
        out.append(Check(s, "distro for AetherCFD", FAIL, "none chosen yet",
                         "start the app and finish Help > Setup Wizard (it picks the distro with OpenFOAM)"))
        return out
    for name in allowed:
        info = next((d for d in rows if d.name == name), None)
        if info is None:
            out.append(Check(s, name, FAIL, "chosen in the settings but not installed",
                             "run Help > Setup Wizard and pick an installed distro"))
            continue
        if not info.running and not probe:
            forks, when = _cached_forks(name)
            if forks:
                stamp = time.strftime("%Y-%m-%d", time.localtime(when)) if when else "?"
                out.append(Check(s, name, OK, f"stopped; OpenFOAM found at the last probe ({stamp}): {', '.join(forks)}"))
            else:
                out.append(Check(s, name, WARN, "stopped and never probed for OpenFOAM",
                                 "run `aethercfd doctor --probe` (starts the distro and looks for OpenFOAM)"))
            continue
        of = detect.probe_openfoam(name, probe_fn)
        if of.ok:
            out.append(Check(s, name, OK, f"OpenFOAM: {', '.join(f.label for f in of.forks)}"))
        else:
            out.append(Check(s, name, FAIL, _first_line(of.message) or "no OpenFOAM found",
                             f"install OpenFOAM inside {name} (INSTALL.md, step 3), then run doctor --probe"))
    return out


# ---------------------------------------------------------------------------------------------- optional tools
def check_tools(settings, *, quick: bool = False, ml_status_fn=None) -> list[Check]:
    from aethercfd.firstrun.detect import find_paraview
    from aethercfd.mesh.cad import find_freecad
    from aethercfd.ml.pinn import find_ml_python, ml_status

    s = "Optional tools"
    out = []
    pv = find_paraview(getattr(settings, "paraview_exe", None))
    out.append(Check(s, "ParaView", OK, str(pv)) if pv else
               Check(s, "ParaView", WARN, "not found (Open in ParaView, ray-traced renders)",
                     "install it from paraview.org/download, or set paraview_exe in settings.toml"))
    fc = find_freecad(getattr(settings, "freecad_cmd", None))
    out.append(Check(s, "FreeCAD", OK, str(fc)) if fc else
               Check(s, "FreeCAD", WARN, "not found (CAD import: STEP, IGES, BREP)",
                     "install it from freecad.org/downloads, or set freecad_cmd in settings.toml"))
    ml_fix = "run `uv run python scripts/setup_ml_env.py` in the source folder (INSTALL.md, PINNs)"
    py = find_ml_python(getattr(settings, "ml_python", None))
    if py is None:
        out.append(Check(s, "PyTorch environment (PINNs)", WARN, "not found", ml_fix))
    elif quick:
        out.append(Check(s, "PyTorch environment (PINNs)", OK, f"{py} (torch not checked: --quick)"))
    else:
        try:
            st = (ml_status_fn or ml_status)(getattr(settings, "ml_python", None))
        except Exception as exc:
            st = {"available": False, "note": f"{type(exc).__name__}: {exc}"}
        if st.get("available"):
            gpu = f", CUDA on {st['gpu']}" if st.get("cuda") else ", CPU only"
            out.append(Check(s, "PyTorch environment (PINNs)", OK, f"torch {st.get('torch')}{gpu} ({py})"))
        else:
            out.append(Check(s, "PyTorch environment (PINNs)", WARN,
                             f"{py} does not import torch: {_first_line(str(st.get('note', '')))}", ml_fix))
    return out


def check_copilot(settings, *, quick: bool = False, providers=None) -> list[Check]:
    """Claude Code / Codex: installed and signed in (asks the CLIs; costs no usage)."""
    from aethercfd.firstrun import detect

    s = "Copilot"
    if quick:
        out = []
        if providers is None:
            from aethercfd.copilot.providers import PROVIDERS as providers
        for p in providers.values():
            cli = p.find(p.configured_exe(settings))
            out.append(Check(s, p.label, OK, f"{cli.version} (sign-in not checked: --quick)") if cli else
                       Check(s, p.label, WARN, "CLI not found", _first_line(p.install_hint)))
        return out
    out = []
    for r in detect.check_ai(settings, providers):
        if not r.found:
            out.append(Check(s, r.label, WARN, "CLI not found", _first_line(r.hint)))
        elif not r.signed_in:
            out.append(Check(s, r.label, WARN, f"{r.version}, not signed in" + (f" ({r.detail})" if r.detail else ""),
                             _first_line(r.hint) or "sign in from the Copilot panel"))
        else:  # no account e-mail in the output: people paste it into reports
            how = "" if not r.detail or "@" in r.detail else f" ({r.detail})"
            out.append(Check(s, r.label, OK, f"{r.version}, signed in{how}"))
    chosen = getattr(settings, "copilot_provider", "claude")
    if out and not any(c.status == OK for c in out):
        out.append(Check(s, "copilot", WARN, f"no copilot CLI is ready (the copilot uses {chosen})",
                         "optional: install Claude Code or the Codex CLI and sign in (INSTALL.md, step 6)"))
    return out


# ---------------------------------------------------------------------------------------------- folders
def _writable(d: Path) -> str | None:
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".doctor-", dir=d)
        os.close(fd)
        os.unlink(name)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def check_folders(settings, settings_error: str | None, *, config: Path | None = None,
                  cache: Path | None = None) -> list[Check]:
    from aethercfd.core import settings as st

    s = "Folders"
    out = []
    try:
        config = config or st.config_dir()
        cache = cache or st.cache_dir()
    except OSError as exc:
        return [Check(s, "settings and cache folders", FAIL, str(exc),
                      "check that %APPDATA% and %LOCALAPPDATA% exist and belong to you")]
    for label, d, env in (("settings folder", config, "AETHERCFD_CONFIG_DIR"),
                          ("cache folder", cache, "AETHERCFD_CACHE_DIR")):
        err = _writable(d)
        out.append(Check(s, label, OK, str(d)) if err is None else
                   Check(s, label, FAIL, f"{d} is not writable: {err}",
                         f"fix the folder's permissions, or set {env} to a writable folder"))
    if settings_error:
        out.append(Check(s, "settings.toml", FAIL, f"cannot read {config / 'settings.toml'}: {_first_line(settings_error)}",
                         "correct the file by hand, or rename it and run Help > Setup Wizard"))
    elif not (config / "settings.toml").is_file():
        out.append(Check(s, "settings.toml", WARN, "not written yet (first start)",
                         "start the app; the setup wizard writes it"))
    else:
        out.append(Check(s, "settings.toml", OK, str(config / "settings.toml")))
    # before the first start the case folder is only a default: the wizard offers to create it
    missing = [r for r in getattr(settings, "case_roots", []) if r and not Path(r).is_dir()]
    if missing and (config / "settings.toml").is_file():
        out.append(Check(s, "case folders", WARN, f"missing: {', '.join(missing)}",
                         "create the folder, or choose another in Help > Setup Wizard"))
    return out


# ---------------------------------------------------------------------------------------------- run
def run_checks(*, backend: bool = True, probe: bool = False, quick: bool = False) -> list[Check]:
    settings, settings_error = load_settings()
    steps: list[tuple[str, Callable[[], list[Check]]]] = [("App", check_app)]
    if not is_frozen():
        steps += [("Python packages", check_packages), ("Qt and VTK", check_qt_vtk)]
    steps += [("Terminal", check_terminal_assets), ("GPU", lambda: check_gpu(settings))]
    if backend:
        steps.append(("WSL and OpenFOAM", lambda: check_wsl(settings, probe=probe)))
    steps += [("Optional tools", lambda: check_tools(settings, quick=quick)),
              ("Copilot", lambda: check_copilot(settings, quick=quick)),
              ("Folders", lambda: check_folders(settings, settings_error))]
    out: list[Check] = []
    for section, fn in steps:
        try:
            out += fn()
        except Exception as exc:  # a broken check is a finding, never a crash
            out.append(Check(section, "doctor check", FAIL, f"{type(exc).__name__}: {_first_line(str(exc))}",
                             "report this (Help > Report a Problem) with the doctor output"))
    return out


TAGS = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}


def format_report(checks: list[Check]) -> str:
    lines = [f"AetherCFD {__version__}", f"Python    {platform.python_version()}  ({sys.executable})",
             f"Platform  {platform.platform()}"]
    width = max((len(c.name) for c in checks), default=10)
    section = None
    for c in checks:
        if c.section != section:
            section = c.section
            lines += ["", section]
        lines.append(f"  [{TAGS[c.status]}] {c.name:<{width}}  {c.detail}".rstrip())
        if c.fix and c.status != OK:
            lines.append(f"  {'':6} {'':<{width}}  fix: {c.fix}")
    n = {k: sum(c.status == k for c in checks) for k in (OK, WARN, FAIL)}
    lines += ["", f"{n[OK]} ok, {n[WARN]} warning(s), {n[FAIL]} problem(s)" +
              (": all required parts work" if n[FAIL] == 0 else "")]
    return "\n".join(lines)


def doctor(*, backend: bool = True, probe: bool = False, quick: bool = False, as_json: bool = False) -> int:
    checks = run_checks(backend=backend, probe=probe, quick=quick)
    if as_json:
        print(json.dumps({"version": __version__, "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def _gpu_json_main(argv: list[str]) -> int:
    """``--gpu-json FILE``: write gpu_probe() to FILE (run inside AetherCFD.exe by the packaged CLI)."""
    Path(argv[argv.index("--gpu-json") + 1]).write_text(json.dumps(gpu_probe()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_gpu_json_main(sys.argv[1:]))
