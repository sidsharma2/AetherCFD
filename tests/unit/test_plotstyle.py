"""Plot subprocesses (run report, residual export) survive a stale matplotlib font cache."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _env(cfg: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MPLCONFIGDIR"] = str(cfg)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _broken_font_cache(cfg: Path) -> None:
    """A fontlist of this matplotlib version whose bundled fonts are only STIXGeneral, as another install of the
    same version left in ~/.matplotlib on the developer PC: every report figure failed (mathtext needs the other
    STIX fonts, falls back to DejaVu Sans, and that is missing too)."""
    cfg.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-c", "import matplotlib.font_manager"], env=_env(cfg), check=True,
                   capture_output=True, timeout=300)
    (cache,) = cfg.glob("fontlist-v*.json")
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["ttflist"] = [e for e in data["ttflist"] if not e["fname"].startswith("fonts")
                       or e["name"] in ("STIXGeneral", "Last Resort High-Efficiency")]
    cache.write_text(json.dumps(data), encoding="utf-8")


def test_report_plots_draw_with_a_font_cache_that_lacks_dejavu(tmp_path):
    cfg = tmp_path / "mplcfg"
    _broken_font_cache(cfg)
    out = tmp_path / "out"
    out.mkdir()
    np.savez(out / "series.npz", s0_t=np.arange(20.0), s0_v=np.linspace(1.0, 2.0, 20))
    spec = {"npz": str(out / "series.npz"), "out_dir": str(out), "style": "", "figures": [
        {"name": "qoi_flows", "x_label": "Iteration", "y_label": "Flow rate (m³/s)", "logy": False, "window": None,
         "series": [{"key": "s0", "label": "outlet"}]}]}
    (out / "plots.json").write_text(json.dumps(spec), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "aethercfd.results.report_plots", str(out / "plots.json")],
                       env=_env(cfg), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-800:]
    assert (out / "qoi_flows.png").is_file()
    # the user's cache file is not rewritten (another matplotlib install may own it)
    (cache,) = cfg.glob("fontlist-v*.json")
    ttf = json.loads(cache.read_text(encoding="utf-8"))["ttflist"]
    assert not any("DejaVu" in e["name"] for e in ttf)


def test_plot_failure_note_is_the_error_not_a_traceback(tmp_path, monkeypatch):
    from aethercfd.results import report

    class R:
        returncode = 1
        stdout = ""
        stderr = 'Traceback (most recent call last):\n  File "x.py", line 1\nValueError: no font\n'

    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: R())
    figs = [{"name": "f", "series": [{"key": "s0", "label": "a"}]}]
    made, notes = report.draw(figs, {"s0_t": [0.0], "s0_v": [1.0]}, tmp_path, None)
    assert made == []
    assert notes == ["plots failed: ValueError: no font"]
