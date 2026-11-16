"""Build, check and zip the packaged app (PyInstaller, onedir).

    uv run python packaging/build.py                 # build (or reuse a finished build), check, zip
    uv run python packaging/build.py --rebuild       # always run PyInstaller again
    uv run python packaging/build.py --skip-build    # only check and zip the existing dist/AetherCFD
    uv run python packaging/build.py --zip-only      # only zip the existing dist/AetherCFD
    uv run python packaging/build.py --cpu-only      # experimental: no CUDA libraries (dist/cpu/AetherCFD)
    uv run python packaging/install.py

Steps, in order. A step that fails stops the build with a message; run the script again after the fix.
1. Preflight: free commit memory (PyInstaller crashes at random when Windows runs short), the vendored
   xterm.js files, the files the spec bundles, and (GPU build) CuPy.
2. PyInstaller. It is skipped when build/pyinstaller/build-stamp.json shows a finished build of the same
   inputs (sources, spec, uv.lock, Python and PyInstaller versions), so a rerun after a failed check does
   not rebuild. PyInstaller's warn-*.txt and xref-*.html are copied to build/reports/.
3. Required components: the exes, the terminal assets, the PINN worker, winpty, Cantera data, and the CUDA
   libraries (GPU build) or their absence (CPU build).
4. Size report of the largest components: printed and saved to build/reports/size-report.{txt,json}.
5. Smoke checks on the built app: the CLI runs `doctor --no-backend`, the GUI completes a --smoke run on
   the fixture case (perspectives, editor, viewport, terminal), `--run-module` and `--run-script` work.
6. The release zip build/AetherCFD-<version>-win64[-cpu].zip: the app folder, both installers (install.py
   and "Install AetherCFD.cmd", which needs no Python) and INSTALL.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "AetherCFD"
NO_WINDOW = 0x08000000
GiB = 2**30
GITHUB_ASSET_LIMIT = 2 * GiB  # GitHub release assets must be under 2 GiB each
DEFAULT_MIN_COMMIT_GB = 12.0  # measured peak of a full build is several GB; keep a margin for the rest of Windows
RELEASE_EXTRAS = {"install.py": ROOT / "packaging" / "install.py",
                  "Install AetherCFD.cmd": ROOT / "packaging" / "Install AetherCFD.cmd",
                  "INSTALL.md": ROOT / "INSTALL.md"}
# files the spec bundles by path: a missing one would only show up as a broken feature in the built app
SPEC_INPUTS = ["packaging/aethercfd.spec", "packaging/entry_gui.py", "packaging/entry_cli.py",
               "src/aethercfd/studio/resources/aethercfd.ico", "src/aethercfd/ml/pinn_worker.py",
               "src/aethercfd/services/cfd_agents/_runner.py", "src/aethercfd/studio/resources/xterm/terminal.html"]
XTERM_IN_DIST = "_internal/aethercfd/studio/resources/xterm"
REQUIRED = ["AetherCFD.exe", "aethercfd-cli.exe", "_internal/aethercfd/ml/pinn_worker.py",
            "_internal/aethercfd/services/cfd_agents/_runner.py", "_internal/winpty/winpty-agent.exe",
            "_internal/winpty/winpty.dll", "_internal/cantera/data/gri30.yaml",
            "_internal/PySide6/QtWebEngineProcess.exe", f"{XTERM_IN_DIST}/terminal.html"]
REQUIRED_GPU = ["_internal/cupy", "_internal/nvidia/cuda_nvrtc/bin", "_internal/nvidia/cuda_runtime/bin",
                "_internal/nvidia/cublas/bin", "_internal/nvidia/cusolver/bin"]
ABSENT_CPU = ["_internal/cupy", "_internal/nvidia"]


class BuildError(RuntimeError):
    """A step failed; the message says what to do."""


# ------------------------------------------------------------------------------------------- version
def read_version(root: Path = ROOT) -> str:
    """__version__ from src/aethercfd/__init__.py: the one place the version is written."""
    text = (root / "src" / "aethercfd" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise BuildError("no __version__ in src/aethercfd/__init__.py")
    return m.group(1)


def version() -> str:
    return read_version()


def win_version_tuple(ver: str) -> tuple[int, int, int, int]:
    """Four numbers for the exe's file version: 0.1.0.dev0 -> (0, 1, 0, 0), 1.2.3rc1 -> (1, 2, 3, 0)."""
    m = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", ver)
    if not m:
        raise BuildError(f"version {ver!r} does not start with numbers")
    return (*(int(g or 0) for g in m.groups()), 0)


def exe_version_info(ver: str, name: str, description: str):
    """PyInstaller version resource: shown in the exe's Properties > Details."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    nums = win_version_tuple(ver)
    strings = [("CompanyName", "AetherCFD Studio authors"), ("FileDescription", description),
               ("FileVersion", ver), ("InternalName", name), ("OriginalFilename", f"{name}.exe"),
               ("ProductName", "AetherCFD Studio"), ("ProductVersion", ver)]
    return VSVersionInfo(ffi=FixedFileInfo(filevers=nums, prodvers=nums),
                         kids=[StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings])]),
                               VarFileInfo([VarStruct("Translation", [1033, 1200])])])


# ------------------------------------------------------------------------------------------- layout
def dist_dir(cpu_only: bool = False) -> Path:
    return ROOT / "dist" / "cpu" / "AetherCFD" if cpu_only else DIST


def work_dir(cpu_only: bool = False) -> Path:
    return ROOT / "build" / ("pyinstaller-cpu" if cpu_only else "pyinstaller")


REPORTS = ROOT / "build" / "reports"


# ------------------------------------------------------------------------------------------- 1. preflight
def commit_memory() -> tuple[int, int]:
    """(free, limit) commit memory in bytes: what Windows can still hand out (RAM + page file)."""
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MemoryStatusEx()
    st.dwLength = ctypes.sizeof(st)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
        raise OSError("GlobalMemoryStatusEx failed")
    return st.ullAvailPageFile, st.ullTotalPageFile


def memory_problem(min_gb: float, mem_fn=commit_memory) -> str | None:
    if min_gb <= 0:
        return None
    try:
        free, limit = mem_fn()
    except (OSError, AttributeError) as exc:  # not Windows, or the call failed: do not block the build
        print(f"preflight: could not read commit memory ({exc}); continuing")
        return None
    print(f"preflight: commit memory free {free / GiB:.1f} GB of {limit / GiB:.1f} GB")
    if free / GiB >= min_gb:
        return None
    return (f"Windows has only {free / GiB:.1f} GB of commit memory free (limit {limit / GiB:.1f} GB); a "
            f"PyInstaller build needs about {min_gb:g} GB and crashes at random without it. Close large "
            "programs (other builds, browsers, a running simulation or local AI model), or let Windows "
            "grow the page file, then run build.py again. --min-commit-gb 0 skips this check.")


def preflight_problems(cpu_only: bool, *, root: Path = ROOT) -> list[str]:
    """Everything the PyInstaller step needs that can be checked before it starts (except memory)."""
    sys.path.insert(0, str(root / "src"))
    from aethercfd.core.runtime import terminal_asset_problems

    problems = [f"terminal (xterm.js) asset: {p}. The files are committed under "
                "src/aethercfd/studio/resources/xterm/dist; restore them with `git checkout -- "
                "src/aethercfd/studio/resources/xterm`." for p in
                terminal_asset_problems(root / "src" / "aethercfd" / "studio" / "resources" / "xterm",
                                        verify_hashes=True)]
    problems += [f"missing {rel} (the spec bundles it)" for rel in SPEC_INPUTS if not (root / rel).is_file()]
    import importlib.util

    needed = ["PyInstaller", "winpty"] + ([] if cpu_only else ["cupy"])
    problems += [f"Python package {m!r} is not installed in this environment: run `uv sync`"
                 for m in needed if importlib.util.find_spec(m) is None]
    return problems


# ------------------------------------------------------------------------------------------- 2. PyInstaller
def _src_files(root: Path):
    for f in sorted((root / "src" / "aethercfd").rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts and "node_modules" not in f.parts:
            yield f


def fingerprint(cpu_only: bool, root: Path = ROOT) -> str:
    """Changes when anything that goes into the build changes (file sizes and times, versions, the flag)."""
    h = hashlib.sha256()
    h.update(f"{sys.version}|{platform.machine()}|cpu_only={cpu_only}".encode())
    try:
        from importlib.metadata import version as dist_version

        h.update(dist_version("pyinstaller").encode())
    except Exception:
        pass
    for rel in ("uv.lock", "pyproject.toml", "packaging/aethercfd.spec", "packaging/entry_gui.py",
                "packaging/entry_cli.py"):
        f = root / rel
        h.update(rel.encode() + (f.read_bytes() if f.is_file() else b"<missing>"))
    for f in _src_files(root):
        st = f.stat()
        h.update(f"{f.relative_to(root).as_posix()}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()


def stamp_path(cpu_only: bool) -> Path:
    return work_dir(cpu_only) / "build-stamp.json"


def finished_build(dist: Path, stamp: Path, fp: str) -> bool:
    """True when ``dist`` holds a finished PyInstaller build of exactly these inputs."""
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("fingerprint") == fp and (dist / "AetherCFD.exe").is_file() and (
        dist / "aethercfd-cli.exe").is_file()


def run_pyinstaller(cpu_only: bool) -> None:
    env = {**os.environ, "AETHERCFD_BUILD_CPU_ONLY": "1" if cpu_only else ""}
    distpath = dist_dir(cpu_only).parent
    stamp = stamp_path(cpu_only)
    stamp.unlink(missing_ok=True)  # an interrupted build must not look finished
    r = subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "aethercfd.spec"),
                        "--distpath", str(distpath), "--workpath", str(work_dir(cpu_only)),
                        "--noconfirm", "--log-level", "WARN"], cwd=ROOT, env=env)
    if r.returncode != 0:
        try:
            free = f"{commit_memory()[0] / GiB:.1f} GB"
        except (OSError, AttributeError):
            free = "unknown"
        raise BuildError(f"PyInstaller failed (exit {r.returncode}); commit memory free now {free}. "
                         "Read the messages above. If it died without a Python error, Windows probably ran "
                         "short of memory: close programs and run build.py again.")


def keep_pyinstaller_reports(cpu_only: bool, reports: Path = REPORTS) -> list[Path]:
    """Copy PyInstaller's warn-*.txt (modules it could not find) and xref-*.html next to the size report."""
    reports.mkdir(parents=True, exist_ok=True)
    kept = []
    for f in sorted(work_dir(cpu_only).glob("*/warn-*.txt")) + sorted(work_dir(cpu_only).glob("*/xref-*.html")):
        dst = reports / (f"{f.stem}-cpu{f.suffix}" if cpu_only else f.name)
        shutil.copy2(f, dst)
        kept.append(dst)
    return kept


# ------------------------------------------------------------------------------------------- 3. components
def missing_components(dist: Path, cpu_only: bool) -> list[str]:
    sys.path.insert(0, str(ROOT / "src"))
    from aethercfd.core.runtime import terminal_asset_problems

    problems = [f"missing {rel}" for rel in REQUIRED if not (dist / rel).exists()]
    problems += [f"terminal asset in the build: {p}" for p in
                 terminal_asset_problems(dist / XTERM_IN_DIST, verify_hashes=True)]
    if cpu_only:
        problems += [f"CPU-only build contains {rel}" for rel in ABSENT_CPU if (dist / rel).exists()]
    else:
        problems += [f"missing {rel} (CUDA)" for rel in REQUIRED_GPU
                     if not (dist / rel).is_dir() or not any((dist / rel).iterdir())]
    return problems


# ------------------------------------------------------------------------------------------- 4. size report
def component_of(rel: Path) -> str:
    """Group key for the size report: nvidia/<library>, a top-level package folder, or a single file."""
    parts = rel.parts
    if parts[0] != "_internal":
        return parts[0]
    inner = parts[1:]
    if not inner:
        return "_internal"
    if inner[0] == "nvidia" and len(inner) > 2:
        return f"nvidia/{inner[1]}"
    if inner[0] == "PySide6" and len(inner) > 1 and inner[1].startswith("Qt6WebEngine"):
        return "PySide6 (QtWebEngine)"
    return inner[0] if len(inner) > 1 else f"{inner[0]} (file)"


def size_report(dist: Path, top: int = 25) -> dict:
    groups: dict[str, int] = {}
    total = files = 0
    for f in dist.rglob("*"):
        if f.is_file():
            n = f.stat().st_size
            total += n
            files += 1
            key = component_of(f.relative_to(dist))
            groups[key] = groups.get(key, 0) + n
    ranked = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)
    nvidia = sum(n for k, n in groups.items() if k.startswith("nvidia/"))
    return {"dist": str(dist), "total_bytes": total, "files": files, "nvidia_bytes": nvidia,
            "largest": [{"component": k, "bytes": n} for k, n in ranked[:top]]}


def format_size_report(rep: dict) -> str:
    mb = 2**20
    lines = [f"{rep['dist']}: {rep['total_bytes'] / mb:,.0f} MB in {rep['files']:,} files "
             f"(NVIDIA CUDA libraries {rep['nvidia_bytes'] / mb:,.0f} MB)", "largest components:"]
    lines += [f"  {c['bytes'] / mb:9,.1f} MB  {c['component']}" for c in rep["largest"]]
    return "\n".join(lines)


def write_size_report(dist: Path, reports: Path = REPORTS, suffix: str = "") -> dict:
    rep = size_report(dist)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"size-report{suffix}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (reports / f"size-report{suffix}.txt").write_text(format_size_report(rep) + "\n", encoding="utf-8")
    print(format_size_report(rep))
    return rep


# ------------------------------------------------------------------------------------------- 5. smoke checks
def _expect_cuda(cpu_only: bool) -> bool:
    """The GPU build must run its Q-criterion on CUDA when this PC can (same check, from source)."""
    if cpu_only:
        return False
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from aethercfd.core.gpu import info

        return info() is not None
    except Exception:
        return False


def check(dist: Path = DIST, cpu_only: bool = False) -> int:
    failures = []
    gui, cli = dist / "AetherCFD.exe", dist / "aethercfd-cli.exe"
    r = subprocess.run([str(cli), "doctor", "--no-backend"], capture_output=True, text=True, timeout=600)
    print(f"cli doctor (exit {r.returncode}):")
    print("\n".join("  " + line for line in r.stdout.strip().splitlines()))
    if r.returncode != 0:
        failures.append(f"cli doctor exit {r.returncode}: {r.stderr[-800:]}")

    import numpy as np

    tmp = Path(tempfile.mkdtemp(prefix="aethercfd-build-"))
    np.savez(tmp / "data.npz", x=np.arange(50.0), Ux=np.logspace(0, -4, 50), p=np.logspace(0, -3, 50))
    spec = {"npz": str(tmp / "data.npz"), "out": str(tmp / "residuals"), "style": "", "x_label": "Iteration",
            "title": None, "decimate": 4000}
    (tmp / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    r = subprocess.run([str(gui), "--run-module", "aethercfd.studio.export_plot", str(tmp / "spec.json")],
                       capture_output=True, text=True, timeout=180, creationflags=NO_WINDOW)
    ok = r.returncode == 0 and (tmp / "residuals.pdf").is_file() and (tmp / "residuals.png").is_file()
    print("run-module export:", r.returncode, "ok" if ok else r.stderr[-800:])
    if not ok:
        failures.append("--run-module export_plot failed")

    # --run-script (script.run / the copilot's run_python): a case script with a sibling import, numpy, argv
    (tmp / "helper.py").write_text("VALUE = 7\n", encoding="utf-8")
    (tmp / "gen.py").write_text("import sys, pathlib, numpy, helper\n"
                                "pathlib.Path('gen_out.txt').write_text(f'{helper.VALUE} {sys.argv[1]}')\n",
                                encoding="utf-8")
    r = subprocess.run([str(gui), "--run-script", str(tmp / "gen.py"), "x1"], cwd=tmp, capture_output=True,
                       text=True, timeout=180, creationflags=NO_WINDOW)
    got = (tmp / "gen_out.txt").read_text() if (tmp / "gen_out.txt").is_file() else None
    print("run-script:", r.returncode, got)
    if got != "7 x1":
        failures.append(f"--run-script failed: {r.returncode} {r.stderr[-800:]}")

    case = ROOT / "tests" / "fixtures" / "cases" / "esi_cavity"
    out = ROOT / "build" / ("smoke_frozen_cpu" if cpu_only else "smoke_frozen")
    # STAGE_WAIT: the first CUDA stage of a fresh build compiles its kernels (a minute or more on a busy laptop)
    env = {**os.environ, "AETHER_SMOKE_WATCHDOG": "700", "AETHER_SMOKE_STAGE_WAIT": "300", "AETHER_SMOKE_COMBUSTION": "1",
           "AETHER_SMOKE_STAGES": '[{"type": "qcriterion", "percentile": 90}, {"type": "slice", "axis": "z"}]'}
    r = subprocess.run([str(gui), str(case), "--smoke", "--smoke-out", str(out)], capture_output=True, text=True,
                       timeout=600, env=env)
    print("gui smoke:", r.returncode, f"screenshots in {out}")
    if r.returncode != 0:
        failures.append(f"gui smoke exit {r.returncode} (see {out} and studio.log)")
    try:
        rep = json.loads((out / "smoke.json").read_text(encoding="utf-8"))
        labels = [v.get("label", "") for v in (rep.get("vis") or {}).get("info", {}).values()]
        print("frozen viewport GPU:", (rep.get("viewport") or {}).get("gpu"), "| stages:", labels,
              "| Cantera Z_st:", (rep.get("combustion") or {}).get("Z_st"))
        cuda = any("(cuda)" in x for x in labels)
        if _expect_cuda(cpu_only) and not cuda:
            failures.append(f"Q-criterion did not run on CUDA in the packaged app: {labels}")
        if cpu_only and cuda:
            failures.append(f"the CPU-only build ran a stage on CUDA: {labels}")
        if not (rep.get("combustion") or {}).get("Z_st"):
            failures.append("Cantera did not work in the packaged app")
    except (OSError, ValueError) as exc:
        failures.append(f"no smoke report: {exc}")
    for f in failures:
        print("FAIL:", f, file=sys.stderr)
    return 1 if failures else 0


# ------------------------------------------------------------------------------------------- 6. zip
def make_zip(dist: Path = DIST, out_dir: Path = ROOT / "build", ver: str | None = None,
             extras: dict[str, Path] | None = None, suffix: str = "") -> Path:
    """AetherCFD-<version>-win64.zip: AetherCFD/ (the app), the installers and INSTALL.md at the top level."""
    if not (dist / "AetherCFD.exe").is_file():
        raise FileNotFoundError(f"no packaged app in {dist}; run packaging/build.py first")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"AetherCFD-{ver or version()}-win64{suffix}.zip"
    tmp = out.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(dist.rglob("*")):
            if f.is_file():
                z.write(f, f"AetherCFD/{f.relative_to(dist).as_posix()}")
        for name, src in (RELEASE_EXTRAS if extras is None else extras).items():
            data = src.read_bytes()
            if name.lower().endswith(".cmd"):  # cmd.exe wants CRLF; git may have stored it with LF
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            z.writestr(name, data)
    tmp.replace(out)
    return out


def zip_note(size: int) -> str:
    note = f"{size / 2**20:,.0f} MB"
    if size >= GITHUB_ASSET_LIMIT:
        note += (" -- too large for one GitHub release asset (limit 2 GiB per file): share it another way, "
                 "or try the experimental --cpu-only build")
    elif size >= 0.9 * GITHUB_ASSET_LIMIT:
        note += " -- close to GitHub's 2 GiB limit per release asset"
    return note


# ------------------------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--rebuild", action="store_true", help="run PyInstaller even if a finished build matches")
    g.add_argument("--skip-build", action="store_true", help="never run PyInstaller; check the existing build")
    g.add_argument("--zip-only", action="store_true", help="only zip the existing build")
    p.add_argument("--no-zip", action="store_true", help="stop after the checks")
    p.add_argument("--no-smoke", action="store_true", help="skip the smoke checks (step 5); no zip is written")
    p.add_argument("--cpu-only", action="store_true",
                   help="experimental: leave out CuPy and the NVIDIA CUDA libraries (about 2 GB); GPU features "
                        "use numpy. Output in dist/cpu/AetherCFD and a -cpu zip. The default build is unchanged.")
    p.add_argument("--min-commit-gb", type=float, default=DEFAULT_MIN_COMMIT_GB,
                   help=f"free commit memory needed before PyInstaller starts (default {DEFAULT_MIN_COMMIT_GB:g}; "
                        "0 skips the check)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, *, mem_fn=commit_memory) -> int:
    a = parse_args(argv)
    dist = dist_dir(a.cpu_only)
    suffix = "-cpu" if a.cpu_only else ""
    try:
        if a.zip_only:
            print(f"release zip {make_zip(dist, suffix=suffix)}")
            return 0
        print(f"AetherCFD {version()} ({'CPU-only' if a.cpu_only else 'GPU'} build) -> {dist}")
        problems = preflight_problems(a.cpu_only)
        if problems:
            raise BuildError("preflight failed:\n  " + "\n  ".join(problems))
        fp = fingerprint(a.cpu_only)
        if a.skip_build:
            print("PyInstaller: skipped (--skip-build)")
        elif not a.rebuild and finished_build(dist, stamp_path(a.cpu_only), fp):
            print(f"PyInstaller: skipped, {dist} is a finished build of these inputs (--rebuild forces it)")
        else:
            mem = memory_problem(a.min_commit_gb, mem_fn)
            if mem:
                raise BuildError(mem)
            print("PyInstaller: building (15 to 30 minutes)")
            run_pyinstaller(a.cpu_only)
            stamp_path(a.cpu_only).write_text(json.dumps({"fingerprint": fp, "version": version(),
                                                          "cpu_only": a.cpu_only}, indent=2), encoding="utf-8")
        for f in keep_pyinstaller_reports(a.cpu_only):
            print(f"kept {f}")
        if not (dist / "AetherCFD.exe").is_file():
            raise BuildError(f"no build in {dist}: run without --skip-build")
        missing = missing_components(dist, a.cpu_only)
        if missing:
            raise BuildError("the build is incomplete:\n  " + "\n  ".join(missing))
        print("required components: ok")
        write_size_report(dist, suffix=suffix)
        if a.no_smoke:
            print("smoke checks: skipped (--no-smoke); no zip written")
            return 0
        code = check(dist, a.cpu_only)
        if code == 0 and not a.no_zip:
            z = make_zip(dist, suffix=suffix)
            print(f"release zip {z} ({zip_note(z.stat().st_size)}): give it with INSTALL.md (inside)")
        return code
    except BuildError as exc:
        print(f"\nBUILD STOPPED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
