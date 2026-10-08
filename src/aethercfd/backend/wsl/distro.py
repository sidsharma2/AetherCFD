"""Read WSL distro state without booting anything (``wsl.exe -l -v`` never starts a distro)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

from aethercfd.backend.wsl.runner import CREATE_NO_WINDOW, decode_output


@dataclass(frozen=True)
class DistroInfo:
    name: str
    state: str  # Running | Stopped | Installing | ...
    version: int
    default: bool

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"


def parse_list_verbose(text: str) -> list[DistroInfo]:
    """Parse ``wsl -l -v`` output (header line, then rows; '*' marks the default distro)."""
    out = []
    for line in text.replace("\x00", "").splitlines():
        s = line.strip()
        if not s or s.upper().startswith("NAME"):
            continue
        default = s.startswith("*")
        parts = s.lstrip("*").split()
        if len(parts) < 3:
            continue
        try:
            version = int(parts[-1])
        except ValueError:
            continue
        out.append(DistroInfo(name=" ".join(parts[:-2]), state=parts[-2], version=version, default=default))
    return out


def list_distros(wsl_exe: str = "wsl.exe") -> list[DistroInfo]:
    proc = subprocess.run([wsl_exe, "-l", "-v"], capture_output=True, timeout=30,
                          env=dict(os.environ, WSL_UTF8="1"),
                          creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return parse_list_verbose(decode_output(proc.stdout))
