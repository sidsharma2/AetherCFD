"""PINN worker against exact solutions, and its failure paths, in a real PyTorch environment.

Kovasznay flow: an exact steady Navier-Stokes solution (Re = 20). The PINN sees velocity only on the
domain boundary and one pressure value; the interior comes from the physics. Advection-diffusion: the
1-D exact solution c = (e^{Pe x} - 1)/(e^{Pe} - 1) from boundary data only.

These tests need the PyTorch environment that scripts/setup_ml_env.py creates. Point them at an existing one
with the AETHERCFD_ML_PYTHON environment variable (the python executable or the environment folder). Without
one they are skipped; the plumbing is still tested without torch in test_pinn_plumbing.py.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from aethercfd.ml import envsetup, pinn

ML_PY = pinn.find_ml_python()
needs_torch = pytest.mark.skipif(ML_PY is None, reason="no PyTorch environment: set AETHERCFD_ML_PYTHON or run "
                                                       "scripts/setup_ml_env.py (see docs/pinn.md)")
pytestmark = needs_torch


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
    xv = np.column_stack([rng.uniform(x0, x1, 300), rng.uniform(y0, y1, 300)])  # interior, never trained on
    yv = np.column_stack(_kovasznay(xv[:, 0], xv[:, 1], nu))
    yv[:, 2] = np.nan  # pressure is known only up to a constant
    job = pinn.PinnJob("ns", np.column_stack([bx, by]), yd, xc, xe,
                       {"nu": nu, "dim": 2, "epochs_adam": 4000, "lbfgs_steps": 800, "lr": 2e-3,
                        "layers": [50, 50, 50, 50], "seed": 0,
                        "val_source": "300 interior points of the exact solution"},
                       x_val=xv, y_val=yv, names=["Ux", "Uy", "p"])
    t0 = time.time()
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "kov"), timeout_s=1100)
    assert res["ok"], res
    ue, ve, _ = _kovasznay(xe[:, 0], xe[:, 1], nu)
    y = res["y_eval"]
    err_u = np.linalg.norm(y[:, 0] - ue) / np.linalg.norm(ue)
    err_v = np.linalg.norm(y[:, 1] - ve) / np.linalg.norm(ve)
    print(f"\nKovasznay: rel L2 u {err_u:.4f}, v {err_v:.4f}; held-out RMSE/std {res['heldout']['rmse_over_std']}; "
          f"{res['device']} {res['gpu']}, worker {res['seconds']} s, wall {time.time() - t0:.0f} s")
    assert err_u < 0.03 and err_v < 0.1, (err_u, err_v, res["seconds"], res["device"])
    # honest reporting: the held-out error is there, small, and the provenance is saved
    hu, hv, hp = res["heldout"]["rmse_over_std"]
    assert hp is None and hu < 0.1 and hv < 0.2
    assert res["seed"] == 0 and res["torch"] and res["hyper"]["epochs_adam"] == 4000 and len(res["inputs_sha256"]) == 64
    assert res["history"][-1]["stage"] == "final" and "val" in res["history"][-1]


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


def _ad_job(**params) -> tuple[pinn.PinnJob, np.ndarray]:
    pe = 2.0
    rng = np.random.default_rng(1)
    ys = np.linspace(0, 1, 30)
    xd = np.column_stack([np.r_[np.zeros(30), np.ones(30)], np.r_[ys, ys]])
    yd = np.r_[np.zeros(30), np.ones(30)][:, None]
    xc = rng.uniform(0, 1, (2000, 2))
    xe = np.r_[rng.uniform(0, 1, (500, 2)), np.column_stack([np.zeros(50), rng.uniform(0, 1, 50)]),
               np.column_stack([np.ones(50), rng.uniform(0, 1, 50)])]
    exact = (np.exp(pe * xe[:, 0]) - 1) / (np.exp(pe) - 1)
    job = pinn.PinnJob("ad", xd, yd, xc, xe, {"diffusivity": 1 / pe, "dim": 2, "epochs_adam": 1500,
                                              "lbfgs_steps": 200, **params}, u_colloc=np.tile([1.0, 0.0], (2000, 1)))
    return job, exact


@pytest.mark.timeout(600)
def test_hard_boundary_values(tmp_path):
    """hard_bc auto: the x = 0 and x = 1 faces carry data and are enforced; the corners alone (y faces) are not."""
    job, exact = _ad_job(hard_bc="auto")
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "hard"), timeout_s=550)
    assert res["ok"], res
    assert res["hard_bc"]["faces"] == ["x-", "x+"] and res["hard_bc"]["outputs_enforced"] == [True]
    y = res["y_eval"][:, 0]
    assert np.max(np.abs(y[500:] - exact[500:])) < 5e-3  # on the faces: the boundary values
    assert np.sqrt(np.mean((y[:500] - exact[:500]) ** 2)) < 0.02
    assert "held exactly" in " ".join(pinn.summary(res))


@pytest.mark.timeout(300)
def test_nan_loss_stops_with_a_plain_message(tmp_path):
    job, _ = _ad_job(_inject="nan")
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "nan"), timeout_s=250)
    assert not res["ok"] and res["reason"] == "diverged" and "smaller learning rate" in res["error"]
    assert res["seed"] == 0 and res["torch"]  # provenance even on a failure


@pytest.mark.timeout(300)
def test_learning_rate_far_too_high_does_not_hang(tmp_path):
    job, _ = _ad_job(lr=1e6, epochs_adam=300, lbfgs_steps=50)
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "lr"), timeout_s=250)
    assert res["ok"] or res["reason"] == "diverged", res  # a bad fit or a plain divergence, never a hang


@pytest.mark.timeout(400)
def test_out_of_gpu_memory_falls_back_to_the_cpu(tmp_path):
    st = pinn.ml_status()
    job, _ = _ad_job(_inject="oom" if st.get("cuda") else "oom_any", epochs_adam=500, lbfgs_steps=0)
    res = pinn.wait(pinn.start(job, job_dir=tmp_path / "oom"), timeout_s=350)
    if st.get("cuda"):
        assert res["ok"] and res["device"] == "cpu" and "ran out of memory" in " ".join(res["notes"])
    else:
        assert not res["ok"] and res["reason"] == "out_of_memory" and "smaller network" in res["error"]


@pytest.mark.timeout(300)
def test_cancel_a_real_run(tmp_path):
    job, _ = _ad_job(epochs_adam=200000)
    jd = pinn.start(job, job_dir=tmp_path / "cancel")
    deadline = time.time() + 120
    while not pinn.progress(jd)["history"] and time.time() < deadline:
        time.sleep(0.2)
    t0 = time.time()
    assert pinn.cancel(jd, grace_s=10)
    res = pinn.load(jd)
    assert not res["ok"] and res["reason"] == "cancelled" and time.time() - t0 < 12
    assert res.get("torch")  # the worker wrote it (cooperative stop), it was not killed


@pytest.mark.timeout(300)
def test_worker_stops_when_the_app_is_gone(tmp_path):
    import json
    import subprocess

    job, _ = _ad_job(epochs_adam=200000)
    jd = tmp_path / "orphan"
    sleeper = subprocess.Popen([str(ML_PY), "-c", "import time; time.sleep(4)"])  # stands in for the app
    jd = pinn.start(job, job_dir=jd)
    pinn.cancel(jd)  # rewrite the job with the stand-in as parent, then start that run by hand
    spec = json.loads((jd / "job.json").read_text())
    spec["parent_pid"] = sleeper.pid
    (jd / "job.json").write_text(json.dumps(spec))
    (jd / "result.json").unlink()
    (jd / "cancel").unlink()
    w = subprocess.Popen([str(ML_PY), "-I", str(pinn.WORKER), str(jd)])
    sleeper.wait()
    assert w.wait(120) == 2
    assert json.loads((jd / "result.json").read_text())["reason"] == "parent_gone"


def test_setup_script_finds_and_checks_the_existing_environment():
    """Detection against the real environment: it works, so nothing is installed."""
    said, ran = [], []
    env_dir = ML_PY.parent.parent
    code = envsetup.main(["--dir", str(env_dir), "--no-settings"], run=lambda *a, **k: ran.append(a), out=said.append)
    assert code == 0 and ran == [] and "already exists" in said[0] and "Check passed" in said[1], said
    st = pinn.ml_status(str(ML_PY))
    assert st["available"] and st["torch"] and st["autograd_ok"]
