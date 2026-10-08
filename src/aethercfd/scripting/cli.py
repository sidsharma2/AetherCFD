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
        failures += _doctor_backend(args.probe)

    print()
    print("all green" if failures == 0 else f"{failures} problem(s)")
    return 0 if failures == 0 else 1


def _doctor_backend(probe: bool) -> int:
    """Report OpenFOAM/WSL reachability. Never boots a stopped distro unless ``probe`` is set."""
    import json

    from aethercfd.backend.wsl.distro import list_distros
    from aethercfd.backend.wsl.forks import probe_forks
    from aethercfd.backend.wsl.runner import WslRunner
    from aethercfd.core.settings import Settings, cache_dir

    settings = Settings.load()
    allowed = settings.wsl.allowed_distros
    print(f"\nWSL distros (allowed for AetherCFD: {', '.join(allowed)}):")
    try:
        distros = list_distros()
    except Exception as exc:
        print(f"  [ERR] {exc}")
        return 1
    for d in distros:
        tag = "allowed" if d.name in allowed else "-"
        print(f"  {d.name:<16} {d.state:<10} WSL{d.version}{'  (default)' if d.default else ''}   [{tag}]")
    failures = 0
    for name in allowed:
        info = next((d for d in distros if d.name == name), None)
        cache = cache_dir() / "forks" / f"{name}.json"
        if info is None:
            print(f"  [ERR] allowed distro {name!r} is not installed")
            failures += 1
            continue
        if not info.running and not probe:
            note = f" (last probe cached in {cache})" if cache.exists() else ""
            print(f"  {name}: stopped -- not booting it; `aethercfd doctor --probe` starts and probes it{note}")
            continue
        forks = probe_forks(WslRunner(name, allowed=allowed))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([f.to_json() for f in forks], indent=2), encoding="utf-8")
        if not forks:
            print(f"  [ERR] {name}: no OpenFOAM installation found")
            failures += 1
        for f in forks:
            print(f"  [ok ] {name}: {f.label:<26} {f.bashrc}{'  (runs as root)' if f.mpi_run_as_root else ''}")
    return failures


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aethercfd", description=__doc__)
    p.add_argument("--version", action="version", version=f"aethercfd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check the environment and OpenFOAM reachability")
    d.add_argument("--no-backend", action="store_true", help="skip WSL/OpenFOAM checks")
    d.add_argument("--probe", action="store_true", help="boot allowed distros if needed and probe OpenFOAM forks")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
