"""Find OpenFOAM cases under a folder (any directory containing system/controlDict)."""

from __future__ import annotations

import os
from pathlib import Path

from aethercfd.case.model import is_time_dir

_SKIP = {"postProcessing", "VTK", ".git", ".aethercfd", "__pycache__", "node_modules", "dynamicCode",
         "0.orig", "constant", "system", ".conda-env", "figures"}


def discover_cases(root: str | Path, *, max_depth: int = 6) -> list[Path]:
    """Case roots under ``root`` (including ``root`` itself). A found case is not descended into."""
    root = Path(root)
    found: list[Path] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, _files in os.walk(root):
        p = Path(dirpath)
        if (p / "system" / "controlDict").is_file():
            found.append(p)
            dirnames[:] = []
            continue
        if len(p.parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP and not d.startswith("processor")
                             and not d.startswith(".") and not is_time_dir(d))
    return found
