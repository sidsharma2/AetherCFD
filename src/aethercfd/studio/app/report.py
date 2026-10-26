"""Help > Report a problem: one local zip with what a bug report needs, and nothing from a case folder.

Holds the tail of studio.log, the native-crash log (faulthandler.log), the newest hang dumps (watchdog.py) and the
versions of the app and its libraries. Nothing is uploaded; the user decides whether to send the file. Qt-free, so
it is unit-tested directly.
"""

from __future__ import annotations

import platform
import sys
import time
import zipfile
from importlib import metadata
from pathlib import Path

LOG_TAIL_BYTES = 1_000_000
CRASH_TAIL_BYTES = 1_000_000
MAX_HANG_DUMPS = 10
LIBS = ("PySide6", "vtk", "pyvista", "pyvistaqt", "numpy", "scipy", "cupy-cuda12x", "cantera", "pydantic")


def _tail(path: Path, nbytes: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    return (b"[... earlier lines left out ...]\n" + data) if size > nbytes else data


def versions_text(settings=None) -> str:
    from aethercfd import __version__

    lines = [f"AetherCFD {__version__}", f"Python {sys.version.split()[0]} ({sys.executable})",
             f"Platform {platform.platform()}", f"Frozen {bool(getattr(sys, 'frozen', False))}", ""]
    for lib in LIBS:
        try:
            lines.append(f"{lib} {metadata.version(lib)}")
        except metadata.PackageNotFoundError:
            lines.append(f"{lib} (not installed)")
    if settings is not None:  # choices that explain behaviour; no paths, no account names
        lines += ["", *(f"setting {k} = {getattr(settings, k, None)!r}" for k in
                        ("theme", "gpu", "gpu_compute", "copilot_provider", "viewport_look"))]
    return "\n".join(lines) + "\n"


def make_report(cache: Path, dest_dir: Path, *, settings=None, note: str = "") -> Path:
    """Write AetherCFD-report-<time>.zip into dest_dir and return its path."""
    cache, dest_dir = Path(cache), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"AetherCFD-report-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    crash = cache / "crash"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("versions.txt", versions_text(settings))
        if note:
            z.writestr("note.txt", note)
        if (cache / "studio.log").is_file():
            z.writestr("studio.log", _tail(cache / "studio.log", LOG_TAIL_BYTES))
        if (crash / "faulthandler.log").is_file():
            z.writestr("faulthandler.log", _tail(crash / "faulthandler.log", CRASH_TAIL_BYTES))
        if crash.is_dir():
            hangs = sorted(crash.glob("hang-*.txt"), key=lambda p: p.stat().st_mtime)[-MAX_HANG_DUMPS:]
            for h in hangs:
                z.write(h, f"hangs/{h.name}")
    return out
