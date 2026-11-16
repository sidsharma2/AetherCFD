"""Where the app is running from: a source checkout (uv venv) or the packaged build (PyInstaller).

The packaged ``AetherCFD.exe`` has no separate Python interpreter, so "run this module in a child
process" must go through the executable itself (``AetherCFD.exe --run-module <module> ...``, handled
in ``aethercfd.studio.__main__``).
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_MODULE_FLAG = "--run-module"
RUN_SCRIPT_FLAG = "--run-script"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def module_argv(module: str, *args: str) -> list[str]:
    """argv that runs ``python -m <module> <args>`` in a child process, in either mode."""
    if is_frozen():
        return [sys.executable, RUN_MODULE_FLAG, module, *args]
    return [sys.executable, "-m", module, *args]


def script_argv(path: str, *args: str) -> list[str]:
    """argv that runs ``python <path> <args>`` with AetherCFD's own Python (numpy, scipy, cantera...)."""
    if is_frozen():
        return [sys.executable, RUN_SCRIPT_FLAG, path, *args]
    return [sys.executable, path, *args]


def app_executable() -> Path | None:
    """The packaged executable (for GPU preference, shortcuts), or None when running from source."""
    return Path(sys.executable).resolve() if is_frozen() else None


# The Terminal panel's xterm.js files. They are committed (vendored) with a manifest of versions and sha256
# values, so a clone, a git worktree and the packaged app all have them. Same path in source and frozen mode:
# PyInstaller puts the package data next to the package's modules.
XTERM_DIR = Path(__file__).resolve().parents[1] / "studio" / "resources" / "xterm"


def xterm_manifest(xterm_dir: Path = XTERM_DIR) -> dict:
    import json

    return json.loads((xterm_dir / "vendor.json").read_text(encoding="utf-8"))


def terminal_asset_problems(xterm_dir: Path = XTERM_DIR, *, verify_hashes: bool = False) -> list[str]:
    """What is wrong with the vendored xterm.js files ([] when all are present and, if asked, unchanged)."""
    import hashlib

    try:
        files = xterm_manifest(xterm_dir)["files"]
    except (OSError, ValueError, KeyError) as exc:
        return [f"cannot read {xterm_dir / 'vendor.json'}: {exc}"]
    problems = []
    for name, meta in files.items():
        f = xterm_dir / "dist" / name
        if not f.is_file() or f.stat().st_size == 0:
            problems.append(f"missing {f}")
        elif verify_hashes and hashlib.sha256(f.read_bytes()).hexdigest() != meta.get("sha256"):
            problems.append(f"changed {f} (sha256 differs from vendor.json)")
    return problems
