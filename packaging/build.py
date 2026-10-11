"""Build and smoke-test the packaged app, then zip it for sharing.

    uv run python packaging/build.py [--skip-build] [--no-zip]
    uv run python packaging/build.py --zip-only        # just zip the existing dist/AetherCFD
    uv run python packaging/install.py

Checks on the built dist/AetherCFD: the CLI runs `doctor`, the GUI completes a --smoke run on the
fixture case (perspectives, editor, viewport, terminal) and `--run-module` works (plot export, with
matplotlib in a child process of the windowed exe). When the checks pass, the release zip
build/AetherCFD-<version>-win64.zip is written: the app folder, both installers (install.py and
"Install AetherCFD.cmd", which needs no Python) and INSTALL.md for the person you give it to.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "AetherCFD"
NO_WINDOW = 0x08000000
RELEASE_EXTRAS = {"install.py": ROOT / "packaging" / "install.py",
                  "Install AetherCFD.cmd": ROOT / "packaging" / "Install AetherCFD.cmd",
                  "INSTALL.md": ROOT / "INSTALL.md"}


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from aethercfd import __version__

    return __version__


def make_zip(dist: Path = DIST, out_dir: Path = ROOT / "build", ver: str | None = None,
             extras: dict[str, Path] | None = None) -> Path:
    """AetherCFD-<version>-win64.zip: AetherCFD/ (the app), the installers and INSTALL.md at the top level."""
    if not (dist / "AetherCFD.exe").is_file():
        raise FileNotFoundError(f"no packaged app in {dist}; run packaging/build.py first")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"AetherCFD-{ver or version()}-win64.zip"
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


def build() -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "aethercfd.spec"),
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
                    "--noconfirm", "--log-level", "WARN"], check=True, cwd=ROOT)


def check() -> int:
    failures = []
    gui, cli = DIST / "AetherCFD.exe", DIST / "aethercfd-cli.exe"
    r = subprocess.run([str(cli), "doctor"], capture_output=True, text=True, timeout=180)
    print("cli doctor:", r.returncode, r.stdout.strip().splitlines()[:3])
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
    out = ROOT / "build" / "smoke_frozen"
    import os

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
        if not any("(cuda)" in x for x in labels):
            failures.append(f"Q-criterion did not run on CUDA in the packaged app: {labels}")
        if not (rep.get("combustion") or {}).get("Z_st"):
            failures.append("Cantera did not work in the packaged app")
    except (OSError, ValueError) as exc:
        failures.append(f"no smoke report: {exc}")
    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"dist size {size / 2**20:.0f} MB")
    worker = DIST / "_internal" / "aethercfd" / "ml" / "pinn_worker.py"
    print("pinn worker shipped:", worker.is_file())
    if not worker.is_file():
        failures.append("pinn_worker.py is missing from the bundle (PINN training would not start)")
    for f in failures:
        print("FAIL:", f, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    if "--zip-only" in sys.argv:
        print(f"release zip {make_zip()}")
        sys.exit(0)
    if "--skip-build" not in sys.argv:
        build()
    code = check()
    if code == 0 and "--no-zip" not in sys.argv:
        z = make_zip()
        print(f"release zip {z} ({z.stat().st_size / 2**20:.0f} MB): give it with INSTALL.md (inside)")
    sys.exit(code)
