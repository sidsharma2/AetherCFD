"""Build and smoke-test the packaged app.

    uv run python packaging/build.py [--skip-build]
    uv run python packaging/install.py

Checks on the built dist/AetherCFD: the CLI runs `doctor`, the GUI completes a --smoke run on the
fixture case (perspectives, editor, viewport, terminal) and `--run-module` works (plot export, with
matplotlib in a child process of the windowed exe).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "AetherCFD"
NO_WINDOW = 0x08000000


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

    env = {**os.environ, "AETHER_SMOKE_WATCHDOG": "500", "AETHER_SMOKE_COMBUSTION": "1",
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
    for f in failures:
        print("FAIL:", f, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    if "--skip-build" not in sys.argv:
        build()
    sys.exit(check())
