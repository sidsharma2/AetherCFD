"""User settings (roaming, %APPDATA%\\AetherCFD\\settings.toml) and machine caches
(%LOCALAPPDATA%\\AetherCFD\\...). Plain TOML so the user can edit it by hand."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from platformdirs import user_cache_dir, user_config_dir
from pydantic import BaseModel, Field

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


class WslSettings(BaseModel):
    allowed_distros: list[str] = Field(default_factory=lambda: ["LMAE-OpenFOAM"])
    default_distro: str = "LMAE-OpenFOAM"
    use_distro_anchor: bool = False  # ADR 0006: not needed on WSL 2.7; opt-in fallback


class GuardSettings(BaseModel):
    log_warn_mb_per_s: float = 2.0
    log_stop_mb_per_s: float = 20.0
    log_hard_cap_gb: float = 10.0
    disk_reserve_gb: float = 20.0
    disk_reserve_fraction: float = 0.10


class Settings(BaseModel):
    wsl: WslSettings = Field(default_factory=WslSettings)
    guards: GuardSettings = Field(default_factory=GuardSettings)
    plot_style: str = r"D:\OpenFOAM\plot_style.py"
    gpu: str = "high-performance"  # high-performance (discrete NVIDIA on Optimus laptops) | integrated | system
    gpu_compute: str = "auto"  # auto (CUDA via CuPy when available) | cuda | cpu
    claude_exe: str | None = None  # Claude Code CLI for the copilot (default: found automatically)
    ml_python: str | None = None  # python.exe of the PyTorch environment for PINNs (default: found automatically)
    freecad_cmd: str | None = None  # FreeCADCmd.exe for CAD import (default: found automatically)
    viewport_look: str = "studio"  # studio (AetherCFD's own look) | classic (ParaView-style, true cell values)
    # auto (Smooth in Studio, Cells in Classic) | smooth (colours and contours interpolated to the mesh points)
    # | cells (every cell's own value, stair-stepped contours: the mesh resolution stays visible)
    viewport_smoothing: str = "auto"
    recent_cases: list[str] = Field(default_factory=list)
    case_roots: list[str] = Field(default_factory=lambda: [r"D:\OpenFOAM"])

    paraview_exe: str = r"C:\Program Files\ParaView 6.1.0\bin\paraview.exe"

    def add_recent(self, path: str, keep: int = 12) -> None:
        self.recent_cases = [path, *[p for p in self.recent_cases if p.lower() != path.lower()][:keep - 1]]

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.toml"

    @classmethod
    def load(cls) -> Settings:
        p = cls.path()
        if p.exists():
            with open(p, "rb") as f:
                return cls.model_validate(tomllib.load(f))
        return cls()

    def save(self) -> None:
        # TOML has no null: unset optional settings (e.g. no claude_exe) are left out
        atomic_write_bytes(self.path(), tomli_w.dumps(self.model_dump(exclude_none=True)).encode("utf-8"))
