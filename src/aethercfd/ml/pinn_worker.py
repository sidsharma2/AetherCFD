"""PINN worker: physics-informed neural network training, run in the PyTorch environment (not the app's).

    <ml-env python> -I pinn_worker.py <job_dir>

Standalone on purpose (torch + numpy only): the app writes ``job.json`` + ``inputs.npz`` into a folder,
runs this script there, follows ``history.jsonl`` and reads ``outputs.npz`` / ``result.json``.

Physics (steady):
  ns    incompressible Navier-Stokes, 2-D or 3-D: outputs (u, v[, w], p) with p kinematic (p/rho);
        residuals: div u = 0 and (u.grad)u + grad p - nu lap u = 0
  ad    advection-diffusion of one scalar with a given velocity: u.grad c - D lap c - S = 0
  data  no physics (a smooth neural fit of the data, for comparison)

inputs.npz:  x_data (n, d), y_data (n, k) (NaN = not measured), [w_data (n,)], x_colloc (m, d),
             [u_colloc (m, d) for ad], [s_colloc (m,) source for ad], x_eval (q, d),
             [x_val (r, d), y_val (r, k)]: held-out points, never trained on; their error is reported
Coordinates and outputs are non-dimensionalised internally (bounding box, data mean/std) and the PDE
residuals scaled by reference velocity and length, so the loss terms are balanced without hand tuning.

job.json options besides the physics: seed, device (auto | cpu), layers, fourier, fourier_scale, lr,
epochs_adam, lbfgs_steps, lbfgs_points, batch, weights, cpu_fallback (true: a CUDA out-of-memory run
starts again on the CPU), hard_bc ("auto" or face names such as ["x-", "x+"]: Dirichlet data on those
faces of the bounding box hold by construction), parent_pid (the worker stops when the app is gone).

result.json always says ok true or false. A failure carries a plain ``error`` and a ``reason``:
no_torch | cancelled | parent_gone | diverged | out_of_memory | bad_input | crash.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np


def _write_result(jd: Path, res: dict) -> None:
    """Atomic: the app polls for result.json and must never read it half-written."""
    tmp = Path(jd, "result.json.tmp")
    tmp.write_text(json.dumps(res, indent=1), encoding="utf-8")
    tmp.replace(Path(jd, "result.json"))


try:
    import torch
    from torch import nn
except Exception as _exc:  # no torch, or a torch whose DLLs do not load: say so instead of a bare traceback
    if __name__ != "__main__":
        raise
    _write_result(Path(sys.argv[1]), {
        "ok": False, "reason": "no_torch", "python": sys.executable,
        "error": (f"PyTorch is not installed in {sys.executable}" if isinstance(_exc, ModuleNotFoundError)
                  and getattr(_exc, "name", "") == "torch" else
                  f"PyTorch does not load in {sys.executable} ({type(_exc).__name__}: {_exc})")
        + ". Run scripts/setup_ml_env.py to create a working environment (docs/pinn.md)."})
    sys.exit(3)


class Stop(Exception):
    """Training stopped on purpose (cancel, app gone) or by a failure with a plain message."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


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


class HardBC(nn.Module):
    """y = g(x) + phi(x) (net(x) - y_mu) for the enforced outputs: phi is zero on the chosen faces of the
    bounding box (one linear factor per face, 1 at the centre) and g is a small network fitted to the data on
    those faces first and then frozen, so the faces keep g's values whatever the main network does."""

    def __init__(self, net: Net, g: Net, faces: list[tuple[int, int]], enforce: list[bool]):
        super().__init__()
        self.net, self.g, self.faces = net, g, faces
        self.register_buffer("mask", torch.as_tensor(enforce, dtype=torch.float32))
        for q in self.g.parameters():
            q.requires_grad_(False)

    def forward(self, x):
        y = self.net(x)
        z = (x - self.net.c) / self.net.s
        phi = torch.ones_like(z[:, :1])
        for a, side in self.faces:
            phi = phi * ((1 + z[:, a:a + 1]) if side == 0 else (1 - z[:, a:a + 1]))
        hard = self.g(x) + phi * (y - self.net.y_mu)
        return self.mask * hard + (1 - self.mask) * y


FACE_NAMES = {(a, s): f"{'xyz'[a]}{'-+'[s]}" for a in range(3) for s in range(2)}


def bc_faces(spec, xd, yd, lo, hi, tol: float = 1e-6) -> tuple[list[tuple[int, int]], np.ndarray]:
    """Faces of the bounding box to enforce and which data points lie on them. ``auto``: every face that
    holds at least 3 data points and at least 5 % of them (a corner point alone does not make a face)."""
    d = xd.shape[1]
    span = np.maximum(hi - lo, 1e-30)
    faces, on = [], np.zeros(len(xd), bool)
    names = {v: k for k, v in FACE_NAMES.items()}
    want = None if spec in (True, "auto") else {names[s] for s in spec}
    for a in range(d):
        for side, v in ((0, lo[a]), (1, hi[a])):
            m = np.abs(xd[:, a] - v) <= tol * span[a]
            if (want is None and m.sum() >= max(3, 0.05 * len(xd))) or (want is not None and (a, side) in want):
                if not m.any():
                    raise Stop("bad_input", f"hard_bc: no data point lies on face {FACE_NAMES[(a, side)]}")
                faces.append((a, side))
                on |= m
    return faces, on


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
        # u.grad c = (1/rho) div(alpha grad c) = D lap c + G.grad c, with D = alpha/rho, G = grad(alpha)/rho per point
        # (variable D from the CFD's turbulence fields), or a constant D
        D = extra["d"] if "d" in extra else float(job.get("diffusivity", 1e-3))
        c = out[:, 0:1]
        dc = grad(c, x)
        lap = sum(grad(dc[:, j:j + 1], x)[:, j:j + 1] for j in range(d))
        uvel = extra["u"]
        adv = (uvel * dc).sum(1, keepdim=True)
        src = extra.get("s")
        r = adv - D * lap - (src if src is not None else 0.0)
        if "g" in extra:
            r = r - (extra["g"] * dc).sum(1, keepdim=True)
        return [r * (L / (U * job["_C"]))]
    return []


def _process_alive(pid: int) -> bool:
    """The app process still runs (a worker must not train on after the app crashed or was killed)."""
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() != 87  # ERROR_INVALID_PARAMETER: no such process; other errors: assume alive
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return os.getppid() == pid


def hyper(job: dict) -> dict:
    """The training settings with their defaults filled in (saved with the result)."""
    return {"layers": list(job.get("layers", [64, 64, 64, 64])), "fourier": int(job.get("fourier", 0)),
            "fourier_scale": float(job.get("fourier_scale", 1.0)), "lr": float(job.get("lr", 1e-3)),
            "epochs_adam": int(job.get("epochs_adam", 3000)), "lbfgs_steps": int(job.get("lbfgs_steps", 300)),
            "lbfgs_points": int(job.get("lbfgs_points", 8192)), "batch": int(job.get("batch", 4096)),
            "weights": {"data": 1.0, "pde": 1.0} | dict(job.get("weights", {})),
            "hard_bc": job.get("hard_bc") or None, "cpu_fallback": bool(job.get("cpu_fallback", True)),
            **({"nu": float(job["nu"])} if "nu" in job else {}),
            **({"diffusivity": job["diffusivity"]} if "diffusivity" in job else {})}


def _rel_rmse(pred: np.ndarray, ref: np.ndarray, scale: np.ndarray) -> list:
    """RMSE / std per output over the finite reference values (std of the reference, or ``scale`` if flat)."""
    out = []
    for j in range(ref.shape[1]):
        ok = np.isfinite(ref[:, j])
        if not ok.any():
            out.append(None)
            continue
        sd = float(np.std(ref[ok, j]))
        sd = sd if sd > 1e-12 * max(abs(float(np.mean(ref[ok, j]))), 1e-30) else float(scale[j]) or 1.0
        out.append(float(np.sqrt(np.mean((pred[ok, j] - ref[ok, j]) ** 2))) / sd)
    return out


def train(jd: Path, job: dict, dev: torch.device, t0: float) -> dict:
    data = np.load(jd / "inputs.npz")
    seed = int(job.get("seed", 0))
    torch.manual_seed(seed)
    np.random.seed(seed)
    if dev.type == "cuda":
        torch.cuda.manual_seed_all(seed)
    hp = hyper(job)
    xd, yd = data["x_data"].astype(np.float64), data["y_data"].astype(np.float64)
    xc, xe = data["x_colloc"].astype(np.float64), data["x_eval"].astype(np.float64)
    xv = data["x_val"].astype(np.float64) if "x_val" in data else None
    yv = data["y_val"].astype(np.float64) if "y_val" in data else None
    for name, a in (("x_data", xd), ("x_colloc", xc), ("x_eval", xe)) + ((("x_val", xv),) if xv is not None else ()):
        if not np.isfinite(a).all():
            raise Stop("bad_input", f"the input coordinates {name} contain NaN or infinite values")
    if not np.isfinite(yd).any():
        raise Stop("bad_input", "no finite data value to train on")
    wd = data["w_data"].astype(np.float64) if "w_data" in data else np.ones(len(xd))
    d, k = xd.shape[1], yd.shape[1]
    allx = np.vstack([xd, xc, xe] + ([xv] if xv is not None else []))
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
    f32 = lambda a: torch.as_tensor(a, dtype=torch.float32, device=dev)  # noqa: E731
    inject = job.get("_inject")  # test hook: oom | nan
    if (inject == "oom" and dev.type == "cuda") or inject == "oom_any":
        raise torch.cuda.OutOfMemoryError("CUDA out of memory (injected by the test)")
    net = Net(d, k, hp["layers"], hp["fourier"], hp["fourier_scale"], lo, hi, y_mu, y_sd).to(dev)
    model = net
    hard_info = None
    if hp["hard_bc"]:
        faces, on = bc_faces(hp["hard_bc"], xd, yd, lo, hi)
        if not faces:
            raise Stop("bad_input", "hard_bc auto: no face of the bounding box holds enough data points")
        enforce = [bool(np.isfinite(yd[on, j]).all()) for j in range(k)]
        g = Net(d, k, [32, 32], 0, 1.0, lo, hi, y_mu, y_sd).to(dev)
        xg, yg = f32(xd[on]), f32(np.nan_to_num(yd[on]))
        mg, sg = f32(np.isfinite(yd[on]).astype(np.float32)), f32(y_sd)
        og = torch.optim.Adam(g.parameters(), lr=3e-3)
        for _ in range(int(job.get("hard_bc_steps", 2000))):
            lg = ((((g(xg) - yg) / sg) * mg) ** 2).mean()
            og.zero_grad()
            lg.backward()
            og.step()
        gfit = _rel_rmse(g(xg).detach().cpu().numpy(), yd[on], y_sd)
        model = HardBC(net, g, faces, enforce).to(dev)
        hard_info = {"faces": [FACE_NAMES[f] for f in faces], "outputs_enforced": enforce,
                     "n_boundary_points": int(on.sum()), "boundary_fit_rmse_over_std": gfit}
    Xd, Yd, Wd = f32(xd), f32(np.nan_to_num(yd)), f32(wd)
    Md = f32(np.isfinite(yd).astype(np.float32))
    Ysd = f32(y_sd)
    Xc = f32(xc)
    extra = {}
    if job["physics"] == "ad":
        extra["u"] = f32(data["u_colloc"])
        if "s_colloc" in data:
            extra["s"] = f32(data["s_colloc"]).reshape(-1, 1)
        if "d_colloc" in data:
            extra["d"] = f32(data["d_colloc"]).reshape(-1, 1)
        if "g_colloc" in data:
            extra["g"] = f32(data["g_colloc"])
    w = hp["weights"]
    batch = hp["batch"]
    parent = int(job.get("parent_pid") or 0)
    hist = (jd / "history.jsonl").open("w", encoding="utf-8")

    def predict(x: np.ndarray) -> np.ndarray:
        with torch.no_grad():
            return torch.cat([model(f32(x[i:i + 65536])) for i in range(0, len(x), 65536)]).cpu().numpy()

    def check(stage: str, step: int, loss) -> None:
        """Every few steps: stop when asked, when the app is gone, or when the loss is no longer finite."""
        if (jd / "cancel").exists():
            raise Stop("cancelled", "stopped by the user before it finished")
        if parent and not _process_alive(parent):
            raise Stop("parent_gone", "AetherCFD closed while the PINN was training")
        if not math.isfinite(float(loss)):
            raise Stop("diverged", f"training diverged: the loss became NaN or infinite at {stage} step {step}. "
                                   "Try a smaller learning rate (lr), fewer Adam steps between checks, or check "
                                   "the inputs (diffusivity or viscosity, units, data values)")

    def loss_terms(idx=None):
        e = ((model(Xd) - Yd) / Ysd) * Md
        ld = (Wd[:, None] * e ** 2).sum() / Md.sum().clamp(min=1)
        lp = torch.zeros((), device=dev)
        if job["physics"] in ("ns", "ad") and w["pde"] > 0:
            sel = idx if idx is not None else slice(None)
            x = Xc[sel].clone().requires_grad_(True)
            ex = {kk: v[sel] for kk, v in extra.items()}
            rs = residuals(job, model, x, ex)
            lp = sum((r ** 2).mean() for r in rs) / len(rs)
        if inject == "nan":
            ld = ld * float("nan")
        return ld, lp

    def log(stage, step, ld, lp):
        row = {"stage": stage, "step": step, "data": float(ld), "pde": float(lp), "t": round(time.time() - t0, 2)}
        if xv is not None:
            row["val"] = _rel_rmse(predict(xv), yv, y_sd)
        hist.write(json.dumps(row) + "\n")
        hist.flush()

    notes = []
    try:
        n_adam = hp["epochs_adam"]
        opt = torch.optim.Adam(model.parameters(), lr=hp["lr"])
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, max(n_adam, 1), eta_min=hp["lr"] / 50)
        m = len(xc)
        for it in range(n_adam):
            idx = torch.randint(0, m, (min(batch, m),), device=dev) if m > batch else None
            ld, lp = loss_terms(idx)
            loss = w["data"] * ld + w["pde"] * lp
            if it % 20 == 0:
                check("adam", it, loss.item())
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            if it % 100 == 0 or it == n_adam - 1:
                log("adam", it, ld.item(), lp.item())
        if hp["lbfgs_steps"]:
            before = {kk: v.detach().clone() for kk, v in model.state_dict().items()}
            lb = torch.optim.LBFGS([q for q in model.parameters() if q.requires_grad], lr=1.0,
                                   max_iter=hp["lbfgs_steps"], history_size=50, line_search_fn="strong_wolfe",
                                   tolerance_grad=1e-9, tolerance_change=1e-12)
            sub = torch.randperm(m, device=dev)[:min(m, hp["lbfgs_points"])] if m > batch else None
            state = {"n": 0}

            def closure():
                lb.zero_grad()
                ld_, lp_ = loss_terms(sub)
                loss_ = w["data"] * ld_ + w["pde"] * lp_
                state["n"] += 1
                if (jd / "cancel").exists() or state["n"] % 25 == 0:
                    check("lbfgs", state["n"], 0.0)
                loss_.backward()
                if state["n"] % 25 == 0:
                    log("lbfgs", state["n"], ld_.item(), lp_.item())
                return loss_

            lb.step(closure)
            ld, lp = loss_terms(sub)
            if not math.isfinite(float(w["data"] * ld + w["pde"] * lp)):  # keep the Adam result instead
                model.load_state_dict(before)
                notes.append("L-BFGS diverged; the result is the network after the Adam stage")
        ld, lp = loss_terms(None if m <= 20000 else torch.randperm(m, device=dev)[:20000])
        check("final", 0, (w["data"] * ld + w["pde"] * lp).item())
        log("final", 0, ld.item(), lp.item())
        ye = predict(xe)
        yfit = predict(xd)
        if not np.isfinite(ye).all():
            raise Stop("diverged", "the trained network gives NaN or infinite values: try a smaller learning rate")
    finally:
        hist.close()
    np.savez_compressed(jd / "outputs.npz", y_eval=ye, y_fit=yfit)
    res = {"ok": True, "final_data_loss": float(ld), "final_pde_loss": float(lp),
           "fit_rmse_over_std": _rel_rmse(yfit, yd, y_sd), "n_data": len(xd), "n_colloc": int(m), "n_eval": len(xe),
           "hyper": hp, "notes": notes}
    if xv is not None:
        res["heldout"] = {"rmse_over_std": _rel_rmse(predict(xv), yv, y_sd), "n": len(xv),
                          "what": job.get("val_source") or "held-out points (never used in training)"}
    if hard_info:
        res["hard_bc"] = hard_info
    return res


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (isinstance(exc, torch.cuda.OutOfMemoryError | MemoryError) or "out of memory" in text
            or "can't allocate memory" in text)


def main(job_dir: str) -> int:
    jd = Path(job_dir)
    t0 = time.time()
    job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    want_cpu = job.get("device", "auto") == "cpu"
    dev = torch.device("cuda" if torch.cuda.is_available() and not want_cpu else "cpu")
    notes = []
    try:
        res = None
        try:
            res = train(jd, dict(job), dev, t0)
        except Exception as exc:
            if not _is_oom(exc):
                raise
            if dev.type != "cuda" or not job.get("cpu_fallback", True):
                where = "the GPU" if dev.type == "cuda" else "the computer"
                raise Stop("out_of_memory", f"out of memory on {where}: use fewer collocation points, a smaller "
                                            "batch or a smaller network" + (", or allow the CPU fallback"
                                                                             if dev.type == "cuda" else "")) from None
        if res is None:  # out of GPU memory: the failed attempt's tensors are released by now
            torch.cuda.empty_cache()
            notes.append("the GPU ran out of memory (it is shared with other programs); trained on the CPU instead")
            dev = torch.device("cpu")
            res = train(jd, dict(job, _inject=None if job.get("_inject") == "oom" else job.get("_inject")), dev, t0)
    except Stop as exc:
        res = {"ok": False, "reason": exc.reason, "error": str(exc)}
    res.update({"device": str(dev), "gpu": torch.cuda.get_device_name(0) if dev.type == "cuda" else None,
                "seconds": round(time.time() - t0, 1), "seed": int(job.get("seed", 0)), "torch": torch.__version__,
                "cuda": torch.version.cuda, "numpy": np.__version__, "python": platform.python_version(),
                "inputs_sha256": hashlib.sha256((jd / "inputs.npz").read_bytes()).hexdigest(),
                "notes": notes + res.get("notes", [])})
    _write_result(jd, res)
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:  # report to the app instead of dying silently
        import traceback

        _write_result(Path(sys.argv[1]), {"ok": False, "reason": "crash", "error": f"{type(exc).__name__}: {exc}",
                                          "traceback": traceback.format_exc()[-4000:]})
        sys.exit(1)
