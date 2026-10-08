"""Where the app is running from: a source checkout (uv venv) or the packaged build (PyInstaller).

The packaged ``AetherCFD.exe`` has no separate Python interpreter, so "run this module in a child
process" must go through the executable itself (``AetherCFD.exe --run-module <module> ...``, handled
in ``aethercfd.studio.__main__``).
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_MODULE_FLAG = "--run-module"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def module_argv(module: str, *args: str) -> list[str]:
    """argv that runs ``python -m <module> <args>`` in a child process, in either mode."""
    if is_frozen():
        return [sys.executable, RUN_MODULE_FLAG, module, *args]
    return [sys.executable, "-m", module, *args]


def app_executable() -> Path | None:
    """The packaged executable (for GPU preference, shortcuts), or None when running from source."""
    return Path(sys.executable).resolve() if is_frozen() else None
