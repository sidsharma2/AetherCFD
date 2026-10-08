"""PINN worker: physics-informed neural network training, run in the PyTorch environment (not the app's).

    <ml-env python> pinn_worker.py <job_dir>

Standalone on purpose (torch + numpy only): the app writes ``job.json`` + ``inputs.npz`` into a folder,
runs this script there, follows ``history.jsonl`` and reads ``outputs.npz`` / ``result.json``.

Physics (steady):
  ns    incompressible Navier-Stokes, 2-D or 3-D: outputs (u, v[, w], p) with p kinematic (p/rho);
        residuals: div u = 0 and (u.grad)u + grad p - nu lap u = 0
  ad    advection-diffusion of one scalar with a given velocity: u.grad c - D lap c - S = 0
  data  no physics (a smooth neural fit of the data, for comparison)

inputs.npz:  x_data (n, d), y_data (n, k) (NaN = not measured), [w_data (n,)], x_colloc (m, d),
             [u_colloc (m, d) for ad], [s_colloc (m,) source for ad], x_eval (q, d)
Coordinates and outputs are non-dimensionalised internally (bounding box, data mean/std) and the PDE
residuals scaled by reference velocity and length, so the loss terms are balanced without hand tuning.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn


class Fourier(nn.Module):
    def __init__(self, d: int, n: int, scale: float):
        super().__init__()
        self.register_buffer("B", torch.randn(d, n) * scale)

    def forward(self, z):
        zb = 2 * math.pi * z @ self.B
        return torch.cat([z, torch.sin(zb), torch.cos(zb)], dim=1)


class Net(nn.Module):
    def __init__(self, d: int, k: int, layers: list[int], fourier: int, fscale: float, lo, hi, y_mu, y_sd):
        super().__init__()
        self.register_buffer("c", torch.as_tensor((lo + hi) / 2, dtype=torch.float32))
        self.register_buffer("s", torch.as_tensor(np.maximum((hi - lo) / 2, 1e-12), dtype=torch.float32))
        self.register_buffer("y_mu", torch.as_tensor(y_mu, dtype=torch.float32))
        self.register_buffer("y_sd", torch.as_tensor(y_sd, dtype=torch.float32))
        self.ff = Fourier(d, fourier, fscale) if fourier else None
        width = d + 2 * fourier if fourier else d
        mods = []
        for h in layers:
            mods += [nn.Linear(width, h), nn.Tanh()]
            width = h
        mods.append(nn.Linear(width, k))
        self.mlp = nn.Sequential(*mods)

    def forward(self, x):
        z = (x - self.c) / self.s  # inside the graph: autograd derivatives are w.r.t. physical x
        if self.ff is not None:
            z = self.ff(z)
        return self.y_mu + self.y_sd * self.mlp(z)


def grad(y, x):
    return torch.autograd.grad(y, x, torch.ones_like(y), create_graph=True)[0]


def residuals(job, net, x, extra):
    """Dimensionless PDE residuals at collocation points x (requires_grad)."""
    d = x.shape[1]
    out = net(x)
    U, L = job["_U"], job["_L"]
    if job["physics"] == "ns":
        nu = float(job.get("nu", 1e-3))
        u = [out[:, i:i + 1] for i in range(d)]
        p = out[:, d:d + 1]
        du = [grad(ui, x) for ui in u]
        dp = grad(p, x)
        cont = sum(du[i][:, i:i + 1] for i in range(d)) * (L / U)
        mom = []
        for i in range(d):
            lap = sum(grad(du[i][:, j:j + 1], x)[:, j:j + 1] for j in range(d))
            adv = sum(u[j] * du[i][:, j:j + 1] for j in range(d))
            mom.append((adv + dp[:, i:i + 1] - nu * lap) * (L / U ** 2))
        return [cont, *mom]
    if job["physics"] == "ad":
        D = float(job.get("diffusivity", 1e-3))
        c = out[:, 0:1]
        dc = grad(c, x)
        lap = sum(grad(dc[:, j:j + 1], x)[:, j:j + 1] for j in range(d))
        uvel = extra["u"]
        adv = (uvel * dc).sum(1, keepdim=True)
        src = extra.get("s")
        r = adv - D * lap - (src if src is not None else 0.0)
        return [r * (L / (U * job["_C"]))]
    return []


def main(job_dir: str) -> int:
    jd = Path(job_dir)
    job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    data = np.load(jd / "inputs.npz")
    dev = torch.device("cuda" if torch.cuda.is_available() and job.get("device", "auto") != "cpu" else "cpu")
    torch.manual_seed(int(job.get("seed", 0)))
    xd, yd = data["x_data"].astype(np.float64), data["y_data"].astype(np.float64)
    xc, xe = data["x_colloc"].astype(np.float64), data["x_eval"].astype(np.float64)
    wd = data["w_data"].astype(np.float64) if "w_data" in data else np.ones(len(xd))
    d, k = xd.shape[1], yd.shape[1]
    allx = np.vstack([xd, xc, xe])
    lo, hi = allx.min(0), allx.max(0)
    y_mu = np.nanmean(yd, axis=0)
    y_sd = np.nanstd(yd, axis=0)
    y_sd = np.where(np.isfinite(y_sd) & (y_sd > 0), y_sd, 1.0)
    y_mu = np.where(np.isfinite(y_mu), y_mu, 0.0)
    job["_L"] = float(np.max(hi - lo)) or 1.0
    if job["physics"] == "ns":
        speed = np.linalg.norm(np.nan_to_num(yd[:, :d]), axis=1)
        job["_U"] = float(np.percentile(speed, 95)) or 1.0
        y_sd[:d] = max(float(np.max(y_sd[:d])), 1e-12)  # one scale for all velocity components
    elif job["physics"] == "ad":
        job["_U"] = float(np.percentile(np.linalg.norm(data["u_colloc"], axis=1), 95)) or 1.0
        job["_C"] = float(y_sd[0]) or 1.0
    net = Net(d, k, job.get("layers", [64, 64, 64, 64]), int(job.get("fourier", 0)), float(job.get("fourier_scale", 1.0)),
              lo, hi, y_mu, y_sd).to(dev)
    f32 = lambda a: torch.as_tensor(a, dtype=torch.float32, device=dev)  # noqa: E731
    Xd, Yd, Wd = f32(xd), f32(np.nan_to_num(yd)), f32(wd)
    Md = f32(np.isfinite(yd).astype(np.float32))
    Ysd = f32(y_sd)
    Xc = f32(xc)
    extra = {}
    if job["physics"] == "ad":
        extra["u"] = f32(data["u_colloc"])
        if "s_colloc" in data:
            extra["s"] = f32(data["s_colloc"]).reshape(-1, 1)
    w = {"data": 1.0, "pde": 1.0} | job.get("weights", {})
    batch = int(job.get("batch", 4096))
    hist = (jd / "history.jsonl").open("w", encoding="utf-8")
    t0 = time.time()

    def loss_terms(idx=None):
        e = ((net(Xd) - Yd) / Ysd) * Md
        ld = (Wd[:, None] * e ** 2).sum() / Md.sum().clamp(min=1)
        lp = torch.zeros((), device=dev)
        if job["physics"] in ("ns", "ad") and w["pde"] > 0:
            sel = idx if idx is not None else slice(None)
            x = Xc[sel].clone().requires_grad_(True)
            ex = {kk: v[sel] for kk, v in extra.items()}
            rs = residuals(job, net, x, ex)
            lp = sum((r ** 2).mean() for r in rs) / len(rs)
        return ld, lp

    def log(stage, step, ld, lp):
        hist.write(json.dumps({"stage": stage, "step": step, "data": float(ld), "pde": float(lp),
                               "t": round(time.time() - t0, 2)}) + "\n")
        hist.flush()

    n_adam = int(job.get("epochs_adam", 3000))
    opt = torch.optim.Adam(net.parameters(), lr=float(job.get("lr", 1e-3)))
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, max(n_adam, 1), eta_min=float(job.get("lr", 1e-3)) / 50)
    m = len(xc)
    for it in range(n_adam):
        idx = torch.randint(0, m, (min(batch, m),), device=dev) if m > batch else None
        ld, lp = loss_terms(idx)
        loss = w["data"] * ld + w["pde"] * lp
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if it % 100 == 0 or it == n_adam - 1:
            log("adam", it, ld.item(), lp.item())
    n_lb = int(job.get("lbfgs_steps", 300))
    if n_lb:
        lb = torch.optim.LBFGS(net.parameters(), lr=1.0, max_iter=n_lb, history_size=50,
                               line_search_fn="strong_wolfe", tolerance_grad=1e-9, tolerance_change=1e-12)
        sub = torch.randperm(m, device=dev)[:min(m, int(job.get("lbfgs_points", 8192)))] if m > batch else None
        state = {"n": 0}

        def closure():
            lb.zero_grad()
            ld_, lp_ = loss_terms(sub)
            loss_ = w["data"] * ld_ + w["pde"] * lp_
            loss_.backward()
            state["n"] += 1
            if state["n"] % 25 == 0:
                log("lbfgs", state["n"], ld_.item(), lp_.item())
            return loss_

        lb.step(closure)
    ld, lp = loss_terms(None if m <= 20000 else torch.randperm(m, device=dev)[:20000])
    log("final", 0, ld.item(), lp.item())
    with torch.no_grad():
        ye = torch.cat([net(f32(xe[i:i + 65536])) for i in range(0, len(xe), 65536)]).cpu().numpy()
        yfit = net(Xd).cpu().numpy()
    np.savez_compressed(jd / "outputs.npz", y_eval=ye, y_fit=yfit)
    ok = np.isfinite(yd)
    rel = [float(np.sqrt(np.mean((yfit[ok[:, j], j] - yd[ok[:, j], j]) ** 2)) / (y_sd[j] or 1.0)) if ok[:, j].any()
           else None for j in range(k)]
    (jd / "result.json").write_text(json.dumps({
        "ok": True, "device": str(dev), "gpu": torch.cuda.get_device_name(0) if dev.type == "cuda" else None,
        "seconds": round(time.time() - t0, 1), "final_data_loss": float(ld), "final_pde_loss": float(lp),
        "fit_rmse_over_std": rel, "n_data": len(xd), "n_colloc": int(m), "n_eval": len(xe),
        "torch": torch.__version__}, indent=1), encoding="utf-8")
    hist.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:  # report to the app instead of dying silently
        import traceback

        Path(sys.argv[1], "result.json").write_text(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                                                "traceback": traceback.format_exc()[-4000:]}),
                                                    encoding="utf-8")
        sys.exit(1)
