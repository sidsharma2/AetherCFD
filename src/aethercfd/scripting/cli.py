"""Headless command-line entry point: ``aethercfd doctor|...``.

Everything the GUI can do is meant to be reachable from here too; the GUI is one client of
the command layer, this CLI is another (and the future MCP server is a third).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as md
import platform
import sys

from aethercfd import __version__

# (import name, distribution name) pairs the studio needs at runtime.
_RUNTIME_DEPS: list[tuple[str, str]] = [
    ("pydantic", "pydantic"),
    ("watchdog", "watchdog"),
    ("platformdirs", "platformdirs"),
    ("numpy", "numpy"),
    ("PySide6", "PySide6"),
    ("PySide6QtAds", "PySide6-QtAds"),
    ("vtk", "vtk"),
    ("pyvista", "pyvista"),
    ("pyvistaqt", "pyvistaqt"),
    ("qtpy", "QtPy"),
    ("pyqtgraph", "pyqtgraph"),
    ("qtawesome", "qtawesome"),
    ("winpty", "pywinpty"),
    ("rapidfuzz", "rapidfuzz"),
    ("psutil", "psutil"),
]


def _check_imports() -> list[tuple[str, str, str | None]]:
    """Return (dist, version, error) for every runtime dependency."""
    import os

    os.environ.setdefault("QT_API", "pyside6")
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    rows: list[tuple[str, str, str | None]] = []
    for mod, dist in _RUNTIME_DEPS:
        try:
            version = md.version(dist)
        except md.PackageNotFoundError:
            rows.append((dist, "-", "not installed"))
            continue
        try:
            importlib.import_module(mod)
        except Exception as exc:  # report, never crash the doctor
            rows.append((dist, version, f"{type(exc).__name__}: {exc}"))
        else:
            rows.append((dist, version, None))
    return rows


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"AetherCFD {__version__}")
    print(f"Python    {sys.version.split()[0]}  ({sys.executable})")
    print(f"Platform  {platform.platform()}")
    print()
    rows = _check_imports()
    width = max(len(r[0]) for r in rows)
    failures = 0
    for dist, version, err in rows:
        mark = "ok " if err is None else "ERR"
        failures += err is not None
        line = f"  [{mark}] {dist:<{width}}  {version}"
        if err:
            line += f"   -> {err}"
        print(line)

    # PySide6 and PySide6-QtAds must be an exact pair; a mismatch crashes at import or later.
    try:
        qtads_req = [r for r in (md.requires("PySide6-QtAds") or []) if r.startswith("PySide6-Essentials")]
        pyside = md.version("PySide6-Essentials")
        if qtads_req and pyside not in qtads_req[0]:
            print(f"\n  [ERR] PySide6-QtAds wants {qtads_req[0]} but PySide6-Essentials is {pyside}")
            failures += 1
    except md.PackageNotFoundError:
        pass

    if not args.no_backend:
        failures += _doctor_backend()

    print()
    print("all green" if failures == 0 else f"{failures} problem(s)")
    return 0 if failures == 0 else 1


def _doctor_backend() -> int:
    """Report OpenFOAM/WSL reachability. Never boots a stopped distro unless asked."""
    try:
        from aethercfd.backend.wsl.distro import list_distros
    except ImportError:
        return 0  # backend not built yet
    print("\nWSL distros:")
    try:
        for d in list_distros():
            print(f"  {d.name:<16} {d.state:<10} v{d.version}{'  (default)' if d.default else ''}")
    except Exception as exc:
        print(f"  [ERR] {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aethercfd", description=__doc__)
    p.add_argument("--version", action="version", version=f"aethercfd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check the environment and OpenFOAM reachability")
    d.add_argument("--no-backend", action="store_true", help="skip WSL/OpenFOAM checks")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
