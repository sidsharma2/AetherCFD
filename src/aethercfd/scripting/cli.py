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


class TerminalBroker:
    """Approval in a terminal: shows the full plan and asks; --yes approves non-interactively."""

    def __init__(self, assume_yes: bool):
        self.assume_yes = assume_yes

    def request(self, plan):
        from aethercfd.commands.executor import Approval

        print(plan.describe())
        for fc in plan.file_changes:
            if fc.diff:
                print(fc.diff)
        if self.assume_yes:
            return Approval(True, "cli --yes")
        if not sys.stdin.isatty():
            return Approval(False, "cli", "needs approval; rerun with --yes")
        ans = input("Approve? [y/N] ").strip().lower()
        return Approval(ans in ("y", "yes"), "cli")


def _session(args: argparse.Namespace, *, backend: bool = True):
    from aethercfd.scripting.api import Session

    return Session(args.case, create_project=getattr(args, "create_project", False),
                   broker=TerminalBroker(getattr(args, "yes", False)), with_backend=backend)


def cmd_open(args: argparse.Namespace) -> int:
    from aethercfd.case.model import load_case
    from aethercfd.project.project import Placement, Project
    from aethercfd.runs.placement import resolve_placement

    c = load_case(args.case)
    g = c.fork_guess
    print(f"case      {c.root}")
    print(f"solver    {c.solver_label}")
    print(f"fork      {g.family} {g.version_hint or ''} (confidence {g.confidence:.0%})")
    for e in g.evidence:
        print(f"            - {e}")
    print(f"times     {len(c.times)} ({c.times[0][1]} .. {c.latest_time})" if c.times else "times     none")
    print(f"mesh      {c.n_cells or 'not generated'} cells, regions: {[r.name or '(single)' for r in c.regions]}")
    for r in c.regions:
        kind = "axisymmetric wedge" if r.is_axisymmetric else ("2D (empty)" if r.is_2d else "3D")
        print(f"            {r.name or '(single)'}: {kind}, {len(r.patches)} patches")
    print(f"parallel  {'decomposed, ' + str(c.n_processors) + ' processors' if c.decomposed else 'reconstructed'}")
    place, why = resolve_placement(c, Placement.AUTO)
    print(f"placement {place.value} ({why})")
    if c.generated_by:
        print(f"generated {c.generated_by}")
    proj = Project.find(c.root)
    print(f"project   {'none (read-only browsing)' if proj is None else f'{proj.mode.value} at {proj.meta_dir}'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    s = _session(args)
    if s.project is None:
        print("no AetherCFD project here yet; rerun with --create-project", file=sys.stderr)
        return 2
    params = {"preset": args.preset, "nprocs": args.np, "placement": args.placement,
              "enable_signals": not args.no_signals}
    if args.fork:
        params["fork"] = args.fork
    res = s.do("run.start", case=str(s.root), **params)
    print(res.message)
    if not res.ok:
        return 1
    if args.wait:
        def show(topic, p):
            if topic == "run.state":
                print(f"[state] {p['state']}")
            elif topic == "run.progress" and p["step"].index % max(1, args.every) == 0:
                st = p["step"]
                worst = max(st.residuals.items(), key=lambda kv: kv[1].initial, default=None)
                tail = f" worst residual {worst[0]}={worst[1].initial:.2e}" if worst else ""
                print(f"  t={st.time_text}{tail}")
            elif topic == "run.guard":
                print(f"[guard {p['level']}] {p['message']}")
        s.bus.subscribe("*", show)
        rec = s.runs.wait(res.data)
        print(f"run {rec.id}: {rec.state} (exit {rec.exit_code})")
        return 0 if rec.state == "finished" else 1
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    s = _session(args)
    if s.runs is None:
        print("no project here")
        return 1
    for r in s.runs.runs():
        steps = " -> ".join(st["app"] for st in r.steps)
        print(f"{r.id}  {r.state:<13} {r.placement:<9} exit={r.exit_code}  {steps}  {r.stop_reason or ''}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    s = _session(args)
    res = s.do("run.control", run_id=args.run_id, action=args.action)
    print(res.message)
    return 0 if res.ok else 1


def cmd_set(args: argparse.Namespace) -> int:
    s = _session(args, backend=False)
    if s.project is None:
        print("no AetherCFD project here yet; rerun with --create-project", file=sys.stderr)
        return 2
    res = s.do("dict.set", file=str(s.root / args.file), key=args.key, value=args.value,
               insert_if_missing=args.insert)
    print(res.message or ("ok" if res.ok else "failed"))
    return 0 if res.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aethercfd", description=__doc__)
    p.add_argument("--version", action="version", version=f"aethercfd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check the environment and OpenFOAM reachability")
    d.add_argument("--no-backend", action="store_true", help="skip WSL/OpenFOAM checks")
    d.add_argument("--probe", action="store_true", help="boot allowed distros if needed and probe OpenFOAM forks")
    d.set_defaults(func=cmd_doctor)

    o = sub.add_parser("open", help="describe a case (read-only)")
    o.add_argument("case")
    o.set_defaults(func=cmd_open)

    r = sub.add_parser("run", help="run OpenFOAM on a case (detached; --wait to follow it)")
    r.add_argument("case")
    r.add_argument("--preset", default="solve", choices=["solve", "check", "mesh", "mesh-check-solve"])
    r.add_argument("--np", type=int, default=1)
    r.add_argument("--placement", default="auto", choices=["auto", "in-place", "staged"])
    r.add_argument("--fork", help="fork id, e.g. esi-v2306 or foundation-14")
    r.add_argument("--no-signals", action="store_true")
    r.add_argument("--create-project", action="store_true")
    r.add_argument("--yes", action="store_true", help="approve plans that need approval")
    r.add_argument("--wait", action="store_true")
    r.add_argument("--every", type=int, default=50, help="with --wait: print every N steps")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("runs", help="list runs of a case/project")
    rs.add_argument("case")
    rs.set_defaults(func=cmd_runs)

    c = sub.add_parser("control", help="write_now | stop_write | pause | resume | terminate | kill")
    c.add_argument("case")
    c.add_argument("run_id")
    c.add_argument("action", choices=["write_now", "stop_write", "pause", "resume", "terminate", "kill"])
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=cmd_control)

    st = sub.add_parser("set", help="set a dictionary value (minimal, journalled edit)")
    st.add_argument("case")
    st.add_argument("file", help="relative to the case, e.g. system/controlDict")
    st.add_argument("key", help="e.g. endTime or PIMPLE/nOuterCorrectors")
    st.add_argument("value")
    st.add_argument("--insert", action="store_true", help="add the keyword if missing")
    st.add_argument("--create-project", action="store_true")
    st.set_defaults(func=cmd_set)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
