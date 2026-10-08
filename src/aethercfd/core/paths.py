"""Windows <-> WSL path translation and OpenFOAM path-safety checks.

This is the only module that knows how `D:\\x` maps to `/mnt/d/x` and how ext4 paths are reached
from Windows (`\\\\wsl.localhost\\<distro>\\...`). Everything else asks it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

_UNC_WSL = re.compile(r"^[\\/]{2}(?:wsl\.localhost|wsl\$)[\\/]([^\\/]+)(?:[\\/](.*))?$", re.IGNORECASE)
_MNT = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


class PathTranslationError(ValueError):
    pass


def win_to_wsl(path: str | PureWindowsPath, *, distro: str | None = None) -> str:
    """`D:\\a\\b` -> `/mnt/d/a/b`; `\\\\wsl.localhost\\<distro>\\root\\x` -> `/root/x`.

    For a UNC path, ``distro`` (if given) must match the distro named in the path, because a UNC
    path into one distro is meaningless inside another.
    """
    s = str(path)
    m = _UNC_WSL.match(s)
    if m:
        unc_distro, rest = m.group(1), (m.group(2) or "")
        if distro is not None and unc_distro.lower() != distro.lower():
            raise PathTranslationError(f"{s!r} lives in distro {unc_distro!r}, not {distro!r}")
        rest = rest.replace("\\", "/").strip("/")
        return "/" + rest if rest else "/"
    w = PureWindowsPath(s)
    if not w.drive or not w.drive.endswith(":"):
        raise PathTranslationError(f"{s!r} is not an absolute drive path or a \\\\wsl.localhost path")
    drive = w.drive[0].lower()
    rel = "/".join(w.parts[1:])
    return f"/mnt/{drive}/{rel}" if rel else f"/mnt/{drive}"


def wsl_to_win(path: str, *, distro: str) -> str:
    """`/mnt/d/a/b` -> `D:\\a\\b`; any other absolute path -> `\\\\wsl.localhost\\<distro>\\...`."""
    if not path.startswith("/"):
        raise PathTranslationError(f"{path!r} is not an absolute WSL path")
    m = _MNT.match(path)
    if m:
        drive, rest = m.group(1).upper(), (m.group(2) or "")
        return f"{drive}:\\" + rest.replace("/", "\\")
    rest = path.strip("/").replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}" + (f"\\{rest}" if rest else "")


def is_wsl_unc(path: str) -> bool:
    return bool(_UNC_WSL.match(str(path)))


@dataclass(frozen=True)
class PathSafety:
    safe: bool
    problems: tuple[str, ...]


# OpenFOAM's fileName::valid() strips whitespace and quote characters, so a case path containing
# them silently becomes a *different* path inside the solver (hard failure, see ADR 0003).
_UNSAFE_CHARS = {" ": "space", "\t": "tab", '"': "double quote", "'": "single quote"}


def openfoam_path_safety(wsl_path: str) -> PathSafety:
    """Can OpenFOAM run a case at this (WSL) path in place?"""
    problems: list[str] = []
    for ch, name in _UNSAFE_CHARS.items():
        if ch in wsl_path:
            problems.append(f"contains a {name}")
    if any(ord(c) > 127 for c in wsl_path):
        problems.append("contains non-ASCII characters")
    if "$" in wsl_path:
        problems.append("contains '$' (expanded by OpenFOAM dictionary macros)")
    return PathSafety(safe=not problems, problems=tuple(problems))


def onedrive_hint(win_path: str) -> bool:
    """Paths synced by OneDrive lock files mid-run and create sync conflicts."""
    return "onedrive" in str(win_path).lower()
