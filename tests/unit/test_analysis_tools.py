"""Diff engine, CaseData, Python console engine, POD/DMD, measurements + assimilation, GP surrogate."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from aethercfd.assim.measurements import assimilate, compare, load_measurements
from aethercfd.documents.diff import side_by_side, unified
from aethercfd.ml.surrogate import GPSurrogate
from aethercfd.results.casedata import CaseData
from aethercfd.results.modes import dmd, pod
from aethercfd.scripting.console import Console

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def _case_with_times(tmp_path, values=(1.0, 2.0, 4.0)) -> Path:
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    for i, v in enumerate(values, start=1):
        d = case / f"0.{i}"
        shutil.copytree(case / "0", d)
        t = (d / "p").read_text()
        (d / "p").write_text(re.sub(r"internalField\s+uniform\s+[^;]+;", f"internalField uniform {v};", t))
    return case


# ------------------------------------------------------------------ diff
def test_side_by_side_alignment_and_stats():
    a = "a\nb\nc\nd\n"
    b = "a\nB\nc\nd\ne\n"
    d = side_by_side(a, b)
    assert d.summary() == "2 change(s): +2 / -1 lines"
    kinds = [r.kind for r in d.rows]
    assert kinds == ["equal", "replace", "equal", "equal", "insert"]
    assert d.rows[-1].left is None and d.rows[-1].right == "e"
    assert side_by_side(a, a).identical
    assert "+e" in unified(a, b)
    ctx = side_by_side("\n".join(map(str, range(50))), "\n".join(map(str, range(49))) + "\nX", context=2)
    assert len(ctx.rows) <= 5


# ------------------------------------------------------------------ case data
def test_casedata_times_fields_centres(tmp_path):
    data = CaseData(_case_with_times(tmp_path))
    assert data.times() == [0.0, 0.1, 0.2, 0.3]
    f = data.fields(names=["p"])
    assert set(f) == {"p"} and f["p"].shape == (400,) and np.allclose(f["p"], 4.0)
    assert data.centres().shape == (400, 3)
    ts, snap = data.snapshots("p")
    assert ts == [0.1, 0.2, 0.3] and snap.shape == (3, 400) and np.allclose(snap[:, 0], [1, 2, 4])
    assert data.mesh().n_cells == 400


# ------------------------------------------------------------------ console
def test_console_engine(tmp_path):
    from aethercfd.scripting.api import Session

    s = Session(_case_with_times(tmp_path), create_project=True, with_backend=False)
    c = Console(s, figures_dir=tmp_path / "figs")
    assert c.run("1 + 1").output.strip() == "2"
    assert c.run("for i in range(2):").more
    out = c.run("for i in range(2):\n    print('i', i)", force=True).output
    assert "i 0" in out and "i 1" in out
    assert "ZeroDivisionError" in c.run("1/0").output
    assert c.run("print(float(data.fields(names=['p'])['p'].max()))").output.strip() == "4.0"
    r = c.run("plt.plot([1, 2, 3]); show()")
    assert len(r.images) == 1 and Path(r.images[0]).is_file()


# ------------------------------------------------------------------ modes
def test_pod_and_dmd_recover_planted_modes():
    x = np.linspace(0, 1, 3000)
    t = np.arange(40) * 0.05
    f1, f2 = np.sin(2 * np.pi * x), np.sin(4 * np.pi * x)
    X = 1.0 + np.array([3 * np.exp(-0.5 * ti) * np.cos(4 * np.pi * ti) * f1
                        + np.exp(0.3 * ti) * np.cos(np.pi * ti) * f2 for ti in t])
    p = pod(X, n_modes=5, backend="cpu")
    assert p.energy[:2].sum() > 0.999 and p.modes_for(0.999) <= 2
    d = dmd(X - X.mean(0), dt=0.05, rank=4, delays=4, backend="cpu")
    g = sorted(np.round(d.growth_rate, 2))
    assert g[-1] == pytest.approx(0.30, abs=0.02) and g[0] == pytest.approx(-0.5, abs=0.03)
    assert sorted(set(np.round(np.abs(d.frequency), 1))) == [0.5, 2.0]
    assert d.modes.shape == (4, 3000)


# ------------------------------------------------------------------ measurements
def test_measurements_compare_and_assimilate(tmp_path):
    rng = np.random.default_rng(0)
    c = np.column_stack([np.linspace(0, 1, 5000), np.zeros(5000), np.zeros(5000)])
    truth = 300 + 40 * np.exp(-0.5 * ((c[:, 0] - 0.5) / 0.08) ** 2)
    xb = np.full(5000, 300.0)
    ox = np.linspace(0.2, 0.8, 15)
    y = 300 + 40 * np.exp(-0.5 * ((ox - 0.5) / 0.08) ** 2) + rng.normal(0, 0.5, 15)
    f = tmp_path / "m.csv"
    with f.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["x", "y", "T", "T_std"])
        w.writerows([[xx * 1000, 0, v, 0.5] for xx, v in zip(ox, y, strict=True)])
    m = load_measurements(f, length_unit="mm")
    assert m.points[0, 0] == pytest.approx(0.2) and np.allclose(m.sigma["T"], 0.5)
    rep = compare(m, {"T": xb}, c)["fields"]["T"]
    assert rep["n"] == 15 and rep["bias"] < -10
    a = assimilate(c, xb, m.points, m.values["T"], m.sigma["T"], sigma_b=40.0, length=0.06)
    band = (c[:, 0] > 0.3) & (c[:, 0] < 0.7)
    assert np.sqrt(((a.analysis - truth) ** 2)[band].mean()) < 1.0 < np.sqrt(((xb - truth) ** 2)[band].mean())
    assert a.posterior_std[(c[:, 0] > 0.45) & (c[:, 0] < 0.55)].mean() < 1.0
    with pytest.raises(ValueError):
        load_measurements(f, length_unit="furlong")


# ------------------------------------------------------------------ surrogate
def test_gp_surrogate_interpolates_and_flags_extrapolation():
    f = lambda p: 6.8 + 0.004 * (p - 310) - 0.0002 * (p - 310) ** 2  # noqa: E731
    X = np.array([280, 300, 320, 340, 360.0])
    gp = GPSurrogate().fit(X[:, None], f(X))
    mu, sd = gp.predict([[330.0], [400.0]])
    assert mu[0] == pytest.approx(f(330.0), abs=0.05) and sd[0] < 0.1
    assert list(gp.last_extrapolated) == [False, True]
    assert 260 <= gp.suggest([(260, 400)])[0] <= 400
