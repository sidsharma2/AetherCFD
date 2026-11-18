"""User settings (roaming, %APPDATA%\\AetherCFD\\settings.toml) and machine caches
(%LOCALAPPDATA%\\AetherCFD\\...). Plain TOML so the user can edit it by hand."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from platformdirs import user_cache_dir, user_config_dir
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

from aethercfd.core.clusters import ClusterProfile
from aethercfd.core.textio import atomic_write_bytes

APP = "AetherCFD"


def config_dir() -> Path:
    p = Path(os.environ.get("AETHERCFD_CONFIG_DIR") or user_config_dir(APP, appauthor=False, roaming=True))
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = Path(os.environ.get("AETHERCFD_CACHE_DIR") or user_cache_dir(APP, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_case_root() -> Path:
    """Where new users keep cases unless they choose otherwise: ~/OpenFOAM."""
    return Path.home() / "OpenFOAM"


class WslSettings(BaseModel):
    # empty on a new PC: the first-run setup (Help > Setup Wizard) picks the distro that has OpenFOAM
    allowed_distros: list[str] = Field(default_factory=list)
    default_distro: str = ""
    use_distro_anchor: bool = False  # ADR 0006: not needed on WSL 2.7; opt-in fallback


class GuardSettings(BaseModel):
    log_warn_mb_per_s: float = 2.0
    log_stop_mb_per_s: float = 20.0
    log_hard_cap_gb: float = 10.0
    disk_reserve_gb: float = 20.0
    disk_reserve_fraction: float = 0.10
    # divergence watchdog (roadmap Q6): "stop" stops a run on unambiguous signs (NaN, blow-up) and offers a
    # restart from the last good time; "warn" only reports them; "off" turns the watchdog off
    divergence: Literal["stop", "warn", "off"] = "stop"


class Settings(BaseModel):
    wsl: WslSettings = Field(default_factory=WslSettings)
    guards: GuardSettings = Field(default_factory=GuardSettings)
    clusters: list[ClusterProfile] = Field(default_factory=list)  # remote HPC targets (ADR 0007); no secrets
    plot_style: str = ""  # your matplotlib style module for publication plots (default: a bundled journal style)
    gpu: str = "high-performance"  # high-performance (discrete NVIDIA on Optimus laptops) | integrated | system
    gpu_compute: str = "auto"  # auto (CUDA via CuPy when available) | cuda | cpu
    copilot_provider: str = "claude"  # claude (Claude Code) | codex (OpenAI Codex CLI)
    copilot_model: str | None = None  # model for new copilot conversations (default: the provider's own default)
    # the Claude copilot only runs on a Claude subscription sign-in (never an API key / Console account billed
    # per token); see docs/copilot-accounts.md
    copilot_subscription_only: bool = True
    claude_exe: str | None = None  # Claude Code CLI for the copilot (default: found automatically)
    codex_exe: str | None = None  # OpenAI Codex CLI for the copilot (default: found automatically)
    ml_python: str | None = None  # python.exe of the PyTorch environment for PINNs (default: found automatically)
    freecad_cmd: str | None = None  # FreeCADCmd.exe for CAD import (default: found automatically)
    cfd_agents_python: str | None = None  # python.exe of the cfd_agents environment (aethercfd.services.cfd_agents)
    cfd_agents_root: str | None = None  # folder that contains the cfd_agents package
    theme: str = "system"  # system (follows Windows' app mode) | light | dark
    # perspectives float panels onto extra monitors (e.g. Run Monitor on a 2nd/3rd screen); off keeps every
    # panel docked in the main window
    use_extra_screens: bool = False
    viewport_look: str = "studio"  # studio (AetherCFD's own look) | classic (ParaView-style, true cell values)
    # auto (Smooth in Studio, Cells in Classic) | smooth (colours and contours interpolated to the mesh points)
    # | cells (every cell's own value, stair-stepped contours: the mesh resolution stays visible)
    viewport_smoothing: str = "auto"
    recent_cases: list[str] = Field(default_factory=list)
    case_roots: list[str] = Field(default_factory=lambda: [str(default_case_root())])
    mechanism_roots: list[str] = Field(default_factory=list)  # folders with kinetic mechanisms (YAML/CHEMKIN)
    setup_done: bool = False  # the first-run setup wizard was finished (it is not shown again by itself)

    paraview_exe: str | None = None  # paraview.exe (default: the newest ParaView found in Program Files)

    _load_problem: str | None = PrivateAttr(default=None)

    @property
    def load_problem(self) -> str | None:
        """Why settings.toml could not be used as written (None when it was fine); shown once at start."""
        return self._load_problem

    @load_problem.setter
    def load_problem(self, value: str | None) -> None:
        self._load_problem = value

    def add_recent(self, path: str, keep: int = 12) -> None:
        self.recent_cases = [path, *[p for p in self.recent_cases if p.lower() != path.lower()][:keep - 1]]

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.toml"

    @classmethod
    def load(cls) -> Settings:
        """The user's settings. A hand-edit that broke the file never stops the app: a TOML syntax error gives
        the defaults, a bad value is dropped (its default is used); either way the file is first copied to
        ``settings.toml.broken-<time>`` (the next save would overwrite it) and :attr:`load_problem` says so."""
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            data = tomllib.loads(p.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            out = cls()
            out.load_problem = _set_aside(p, f"could not read {p.name} ({exc}): using the default settings")
            return out
        dropped: list[str] = []
        for _ in range(50):
            try:
                out = cls.model_validate(data)
                break
            except ValidationError as exc:
                err = exc.errors()[0]
                dropped.append(".".join(str(x) for x in err["loc"]) + f" ({err['msg']})")
                if not _drop(data, list(err["loc"]), missing=err["type"] == "missing"):
                    out = cls()
                    dropped.append("everything else")
                    break
        else:
            out = cls()
        if dropped:
            out.load_problem = _set_aside(p, f"{p.name} had values AetherCFD cannot use; their defaults are used "
                                             f"instead: {'; '.join(dropped[:6])}")
        return out

    def save(self) -> None:
        # TOML has no null: unset optional settings (e.g. no claude_exe) are left out
        atomic_write_bytes(self.path(), tomli_w.dumps(self.model_dump(exclude_none=True)).encode("utf-8"))


def _drop(data, loc: list, *, missing: bool) -> bool:
    """Remove the value at ``loc`` (or, for a missing required key, the object that lacks it). False when there
    is nothing left to remove."""
    if missing:
        loc = loc[:-1]
    if not loc:
        return False
    parent = data
    for k in loc[:-1]:
        try:
            parent = parent[k]
        except (KeyError, IndexError, TypeError):
            return False
    try:
        del parent[loc[-1]]
    except (KeyError, IndexError, TypeError):
        return False
    return True


def _set_aside(p: Path, message: str) -> str:
    import logging
    import shutil
    from datetime import datetime

    copy = p.with_name(f"{p.name}.broken-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        shutil.copy2(p, copy)
        message += f". Your file was copied to {copy}"
    except OSError:
        pass
    logging.getLogger(__name__).warning("%s", message)
    return message
