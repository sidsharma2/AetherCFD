"""PINN worker against exact solutions (runs in the PyTorch environment; skipped without one).

Kovasznay flow: an exact steady Navier-Stokes solution (Re = 20). The PINN sees velocity only on the
domain boundary and one pressure value; the interior comes from the physics. Advection-diffusion: the
1-D exact solution c = (e^{Pe x} - 1)/(e^{Pe} - 1) from boundary data only.
"""

from __future__ import annotations

import numpy as np
import pytest

from aethercfd.ml import pinn

pytestmark = pytest.mark.skipif(pinn.find_ml_python() is None, reason="no PyTorch environment")


def _kovasznay(x, y, nu):
    lam = 1 / (2 * nu) - np.sqrt(1 / (4 * nu ** 2) + 4 * np.pi ** 2)
    u = 1 - np.exp(lam * x) * np.cos(2 * np.pi * y)
    v = lam / (2 * np.pi) * np.exp(lam * x) * np.sin(2 * np.pi * y)
    p = 0.5 * (1 - np.exp(2 * lam * x))
    return u, v, p


@pytest.mark.timeout(1200)
def test_kovasznay_from_boundary_data(tmp_path):
    nu = 1 / 20
    rng = np.random.default_rng(0)
    s = np.linspace(0, 1, 50)
    x0, x1, y0, y1 = -0.5, 1.0, -0.5, 1.5
    bx = np.r_[x0 + 0 * s, x1 + 0 * s, x0 + (x1 - x0) * s, x0 + (x1 - x0) * s]
    by = np.r_[y0 + (y1 - y0) * s, y0 + (y1 - y0) * s, y0 + 0 * s, y1 + 0 * s]
    u, v, p = _kovasznay(bx, by, nu)
    yd = np.column_stack([u, v, np.full_like(u, np.nan)])
    yd[0, 2] = p[0]  # one pressure value fixes the gauge
    xc = np.column_stack([rng.uniform(x0, x1, 3000), rng.uniform(y0, y1, 3000)])
    xe = np.column_stack([rng.uniform(x0, x1, 2000), rng.uniform(y0, y1, 2000)])
    job = pinn.PinnJob("ns", np.column_stack([bx, by]), yd, xc, xe,
                       {"nu": nu, "dim": 2, "epochs_adam": 4000, "lbfgs_steps": 800, "lr": 2e-3,
                        "layers": [50, 50, 50, 50]})
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "kov"), timeout_s=1100)
    assert res["ok"], res
    ue, ve, _ = _kovasznay(xe[:, 0], xe[:, 1], nu)
    y = res["y_eval"]
    err_u = np.linalg.norm(y[:, 0] - ue) / np.linalg.norm(ue)
    err_v = np.linalg.norm(y[:, 1] - ve) / np.linalg.norm(ve)
    assert err_u < 0.03 and err_v < 0.1, (err_u, err_v, res["seconds"], res["device"])


@pytest.mark.timeout(900)
def test_advection_diffusion_from_boundaries(tmp_path):
    pe = 2.0  # u = 1, D = 0.5, unit length
    rng = np.random.default_rng(1)
    ys = np.linspace(0, 1, 30)
    xd = np.column_stack([np.r_[np.zeros(30), np.ones(30)], np.r_[ys, ys]])
    yd = np.r_[np.zeros(30), np.ones(30)][:, None]
    xc = rng.uniform(0, 1, (2000, 2))
    xe = rng.uniform(0, 1, (1000, 2))
    job = pinn.PinnJob("ad", xd, yd, xc, xe, {"diffusivity": 1 / pe, "dim": 2, "epochs_adam": 2500,
                                              "lbfgs_steps": 400}, u_colloc=np.tile([1.0, 0.0], (2000, 1)))
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "ad"), timeout_s=800)
    assert res["ok"], res
    exact = (np.exp(pe * xe[:, 0]) - 1) / (np.exp(pe) - 1)
    assert np.sqrt(np.mean((res["y_eval"][:, 0] - exact) ** 2)) < 0.02
