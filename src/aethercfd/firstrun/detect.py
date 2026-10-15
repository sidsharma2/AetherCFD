"""What a new PC has and lacks for AetherCFD, and exactly how to get what is missing (no Qt here).

Everything here only *looks*: ``wsl.exe -l -v`` (never boots a distro), file lookups, and the AI CLIs'
own ``--version`` / sign-in status. Two things start processes the user asked for: probing a distro for
OpenFOAM (boots that distro and sources each OpenFOAM bashrc in a subshell) and the provider sign-in
(the CLI's own login in a visible console). Nothing is ever installed: missing software is explained
with the commands to run.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

NOT_FOR_OPENFOAM = {"agora", "docker-desktop", "docker-desktop-data", "rancher-desktop", "rancher-desktop-data",
                    "podman-machine-default"}

INSTALL_WSL = """\
WSL (Windows Subsystem for Linux) is not installed or not enabled.
In a PowerShell opened with "Run as administrator":
    wsl --install --no-distribution
then restart Windows, and install Ubuntu (next step)."""

INSTALL_DISTRO = """\
WSL works but has no Linux distribution yet. In PowerShell (no admin needed):
    wsl --install -d Ubuntu-22.04
Create the Linux user name and password it asks for, then press "Look again"."""

INSTALL_OPENFOAM = """\
No OpenFOAM was found in {distro}. Install one of the two versions AetherCFD supports, inside Ubuntu
(open it from the Start menu, or run: wsl -d {distro}):

OpenFOAM ESI v2306 (openfoam.com), Ubuntu 20.04/22.04:
    curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
    sudo apt-get install -y openfoam2306-default
  (installs to /usr/lib/openfoam/openfoam2306; details: develop.openfoam.com > wiki > precompiled > debian)

OpenFOAM Foundation 14 (openfoam.org), Ubuntu 22.04/24.04:
    sudo sh -c "wget -O - https://dl.openfoam.org/gpg.key > /etc/apt/trusted.gpg.d/openfoam.asc"
    sudo add-apt-repository http://dl.openfoam.org/ubuntu
    sudo apt-get update && sudo apt-get install -y openfoam14
  (installs to /opt/openfoam14; details: openfoam.org/download/14-ubuntu)

Both can live side by side: AetherCFD never sources two versions into one shell. Then press "Look again"."""

TOOL_HINTS = {
    "paraview": "Optional: the expert post-processor and ray-traced renders. Download the Windows installer from "
                "paraview.org/download (any 5.11+; 6.x recommended).",
    "freecad": "Optional: CAD import (STEP/IGES/BREP). Download FreeCAD from freecad.org/downloads.",
    "ml_python": "Optional: PINN training needs a separate Python with PyTorch. With uv: "
                 "uv venv %LOCALAPPDATA%\\AetherCFD\\ml-env --python 3.12, then "
                 "uv pip install --python %LOCALAPPDATA%\\AetherCFD\\ml-env\\Scripts\\python.exe torch numpy "
                 "--index-url https://download.pytorch.org/whl/cu126",
}


@dataclass
class WslReport:
    ok: bool  # wsl.exe answered
    distros: list = field(default_factory=list)  # DistroInfo usable for OpenFOAM (WSL 2, not Docker/Agora)
    other: list = field(default_factory=list)  # DistroInfo AetherCFD will not use
    problem: str = ""  # "" | no-wsl | no-distro | error
    message: str = ""

    @property
    def names(self) -> list[str]:
        return [d.name for d in self.distros]


@dataclass
class OpenFoamReport:
    distro: str
    forks: list = field(default_factory=list)  # ForkProfile
    message: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.forks)


@dataclass
class ToolReport:
    key: str  # paraview | freecad | ml_python
    label: str
    path: Path | None
    hint: str


@dataclass
class AiReport:
    provider: str  # id
    label: str
    found: bool
    version: str = ""
    signed_in: bool = False
    detail: str = ""
    hint: str = ""
    cli: object = None  # CliInfo, for the sign-in button


# ---------------------------------------------------------------------------------------------------- WSL
def check_wsl(list_fn: Callable[[], list] | None = None) -> WslReport:
    """Installed distros, split into usable / not used. ``list_fn`` replaces ``wsl.exe -l -v`` in tests."""
    if list_fn is None:
        from aethercfd.backend.wsl.distro import list_distros as list_fn
    try:
        rows = list_fn()
    except FileNotFoundError:
        return WslReport(False, problem="no-wsl", message=INSTALL_WSL)
    except Exception as exc:  # wsl.exe present but broken/disabled
        return WslReport(False, problem="no-wsl", message=f"wsl.exe did not answer ({exc}).\n\n{INSTALL_WSL}")
    usable = [d for d in rows if d.name.lower() not in NOT_FOR_OPENFOAM and getattr(d, "version", 2) == 2]
    other = [d for d in rows if d not in usable]
    if not usable:
        extra = (f"\n(Found only: {', '.join(d.name for d in other)}, which AetherCFD does not use.)" if other else "")
        return WslReport(True, [], other, "no-distro", INSTALL_DISTRO + extra)
    return WslReport(True, usable, other)


def probe_openfoam(distro: str, probe_fn: Callable[[str], list] | None = None) -> OpenFoamReport:
    """OpenFOAM installs in ``distro`` (boots it). Refreshes the fork cache the app uses."""
    try:
        if probe_fn is not None:
            forks = probe_fn(distro)
        else:
            from aethercfd.backend.wsl.runner import WslRunner
            from aethercfd.scripting.api import load_forks

            forks = load_forks(WslRunner(distro, allowed=[distro]), refresh=True)
    except Exception as exc:
        return OpenFoamReport(distro, [], f"Could not look inside {distro}: {exc}")
    if not forks:
        return OpenFoamReport(distro, [], INSTALL_OPENFOAM.format(distro=distro))
    return OpenFoamReport(distro, forks, ", ".join(f.label for f in forks))


def best_distro(report: WslReport, current: str = "") -> str:
    """The configured distro if it is installed, else the WSL default, else the first usable one."""
    names = report.names
    if current in names:
        return current
    default = next((d.name for d in report.distros if getattr(d, "default", False)), None)
    return default or (names[0] if names else "")


def use_distro(settings, distro: str) -> None:
    """Make ``distro`` the one AetherCFD runs OpenFOAM in (and allow it)."""
    settings.wsl.default_distro = distro
    if distro not in settings.wsl.allowed_distros:
        settings.wsl.allowed_distros = [*settings.wsl.allowed_distros, distro]


# ---------------------------------------------------------------------------------------------------- tools
def _program_dirs() -> list[Path]:
    dirs = [os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("PROGRAMFILES(X86)", ""),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs") if os.environ.get("LOCALAPPDATA") else ""]
    return [Path(d) for d in dirs if d and Path(d).is_dir()]


def find_paraview(setting: str | None = None) -> Path | None:
    """paraview.exe: the setting, PARAVIEW_EXE, or the newest ParaView* install in Program Files."""
    for c in (setting, os.environ.get("PARAVIEW_EXE")):
        if c and Path(c).is_file():
            return Path(c)
    found = []
    for root in _program_dirs():
        found += [p / "bin" / "paraview.exe" for p in root.glob("ParaView*") if (p / "bin" / "paraview.exe").is_file()]
    from aethercfd.copilot.provider import version_key

    return max(found, key=lambda p: version_key(p.parent.parent.name)) if found else None


def find_tools(settings) -> list[ToolReport]:
    from aethercfd.mesh.cad import find_freecad
    from aethercfd.ml.pinn import find_ml_python

    return [ToolReport("paraview", "ParaView", find_paraview(getattr(settings, "paraview_exe", None)),
                       TOOL_HINTS["paraview"]),
            ToolReport("freecad", "FreeCAD", find_freecad(getattr(settings, "freecad_cmd", None)), TOOL_HINTS["freecad"]),
            ToolReport("ml_python", "Python with PyTorch (PINNs)", find_ml_python(getattr(settings, "ml_python", None)),
                       TOOL_HINTS["ml_python"])]


# ---------------------------------------------------------------------------------------------------- AI
def check_ai(settings, providers: dict | None = None) -> list[AiReport]:
    """Each copilot provider: CLI found? signed in? (costs no usage)."""
    if providers is None:
        from aethercfd.copilot.providers import PROVIDERS as providers
    out = []
    for p in providers.values():
        try:
            cli = p.find(p.configured_exe(settings))
        except Exception as exc:
            out.append(AiReport(p.id, p.label, False, detail=str(exc), hint=p.install_hint))
            continue
        if cli is None:
            out.append(AiReport(p.id, p.label, False, hint=p.install_hint))
            continue
        try:
            a = p.auth_status(cli, timeout=20)
        except Exception as exc:
            out.append(AiReport(p.id, p.label, True, cli.version, False, str(exc), cli=cli))
            continue
        # e.g. Claude signed in with an API (Console) account
        problem = p.billing_problem(a, settings) if hasattr(p, "billing_problem") else None
        hint = problem or ("" if a.logged_in else (a.detail if a.method == "config-error" else
                                                   f"Not signed in: press Sign in (or run `{p.login_hint}`)."))
        out.append(AiReport(p.id, p.label, True, cli.version, a.logged_in and not problem, a.detail or a.method, hint,
                            cli))
    return out


def needs_setup(settings, *, settings_existed: bool) -> bool:
    """Show the wizard by itself only on a fresh install (no settings yet, or no distro chosen) until finished."""
    if getattr(settings, "setup_done", False):
        return False
    return not settings_existed or not settings.wsl.default_distro
