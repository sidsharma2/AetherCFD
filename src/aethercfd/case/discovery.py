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


def case_folder_problem(path: str | Path) -> str | None:
    """None when ``path`` is an OpenFOAM case folder; else a sentence the user can act on (a moved Recent entry,
    a file, a parent folder of cases). Looks at most two levels down, so a whole drive answers quickly."""
    p = Path(path)
    try:
        if not p.exists():
            return f"{p} does not exist (moved, renamed or on a drive that is not connected?)"
        if not p.is_dir():
            return f"{p} is a file: open the case folder that holds system/controlDict"
        if (p / "system" / "controlDict").is_file():
            return None
        inside = discover_cases(p, max_depth=2)
    except OSError as exc:
        return f"cannot read {p}: {exc.strerror or exc}"
    if inside:
        names = ", ".join(c.relative_to(p).as_posix() for c in inside[:3]) + (" …" if len(inside) > 3 else "")
        return f"{p.name or p} is not a case but holds {len(inside)}: open one of them ({names})"
    return f"{p} is not an OpenFOAM case (no system/controlDict): open the folder that holds system/ and constant/"
