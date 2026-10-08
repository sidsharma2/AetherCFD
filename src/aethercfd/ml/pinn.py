"""PINNs from the app (no torch here): build a job from the case, run the worker in the ML environment.

The network trains out of process in a separate PyTorch environment (``ml_python`` setting, or
``<repo>/.ml-env``, or ``%LOCALAPPDATA%/AetherCFD/ml-env``), so the app stays small and a CUDA/torch
problem can never take the GUI down. Typical uses:

- reconstruct a field from sparse probes with the flow physics as a prior (``ad``: a temperature or
  mixture fraction carried by the CFD velocity; ``ns``: an incompressible flow from a few velocity probes);
- a smooth, differentiable surrogate of a CFD field over a region (``data``), e.g. for sensitivities.

    job = build_job(case, physics="ad", field="T", region=bbox, n_data=40, diffusivity=2e-5)
    jd = start(job)             # returns the job folder; training runs in the background
    progress(jd)                # last loss values, done?
    res = load(jd)              # predictions on the region's cells, fit errors, device, seconds
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

WORKER = Path(__file__).with_name("pinn_worker.py")


def find_ml_python(setting: str | None = None) -> Path | None:
    cands = [setting, os.environ.get("AETHERCFD_ML_PYTHON")]
    repo = Path(__file__).resolve().parents[3]
    cands += [str(repo / ".ml-env" / "Scripts" / "python.exe"),
              str(Path(os.environ.get("LOCALAPPDATA", "")) / "AetherCFD" / "ml-env" / "Scripts" / "python.exe")]
    if getattr(sys, "frozen", False):  # packaged app: next to the installed exe
        cands.append(str(Path(sys.executable).parent / "ml-env" / "Scripts" / "python.exe"))
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


def ml_status(setting: str | None = None) -> dict:
    py = find_ml_python(setting)
    if py is None:
        return {"available": False, "note": "no PyTorch environment found (ml_python setting / .ml-env)"}
    r = subprocess.run([str(py), "-c", "import json, torch; print(json.dumps({'torch': torch.__version__, "
                        "'cuda': torch.cuda.is_available(), 'gpu': torch.cuda.get_device_name(0) "
                        "if torch.cuda.is_available() else None}))"], capture_output=True, text=True, timeout=120,
                       creationflags=0x08000000)
    try:
        return {"available": True, "python": str(py)} | json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"available": False, "python": str(py), "note": (r.stderr or r.stdout)[-500:]}


@dataclass
class PinnJob:
    physics: str  # ns | ad | data
    x_data: np.ndarray
    y_data: np.ndarray
    x_colloc: np.ndarray
    x_eval: np.ndarray
    params: dict = field(default_factory=dict)
    u_colloc: np.ndarray | None = None
    d_colloc: np.ndarray | None = None  # per-point diffusivity (ad)
    g_colloc: np.ndarray | None = None  # grad(alpha)/rho at the points (ad, variable diffusivity)
    s_colloc: np.ndarray | None = None
    w_data: np.ndarray | None = None
    eval_cells: np.ndarray | None = None  # cell ids of x_eval (to write fields back)
    names: list[str] = field(default_factory=list)


def build_job(case: str | Path, *, physics: str, field: str, time: float | None = None,
              region: tuple[list[float], list[float]] | None = None, n_data: int = 50, n_colloc: int = 8000,
              data_points: np.ndarray | None = None, data_values: np.ndarray | None = None,
              max_eval: int = 200_000, seed: int = 0, diffusivity_field: str | None = None,
              prandtl: float = 0.71, **params) -> PinnJob:
    """From the open case: the cells inside ``region`` (min, max corners; default all) are evaluated; data
    are ``n_data`` random cells of the CFD ``field`` (or given measurement points/values); collocation
    points are cell centres. ns uses field "U" (+ "p"), ad uses ``field`` with the CFD velocity."""
    from aethercfd.results.casedata import CaseData

    rng = np.random.default_rng(seed)
    data = CaseData(case)
    t = time if time is not None else data.times()[-1]
    names = ["U", "p"] if physics == "ns" else [field] + (["U"] if physics == "ad" else [])
    f = data.fields(t, names)
    c = data.centres(t)
    sel = np.arange(len(c))
    if region is not None:
        lo, hi = np.asarray(region[0], float), np.asarray(region[1], float)
        sel = np.flatnonzero(np.all((c >= lo) & (c <= hi), axis=1))
        if sel.size == 0:
            raise ValueError("no cells inside the region")
    dim = 3
    if np.ptp(c[sel], axis=0).min() < 1e-12 * max(np.ptp(c[sel], axis=0).max(), 1e-30):
        dim = 2  # a one-cell-thick (2-D) case: drop the empty direction
    axes = np.argsort(-np.ptp(c[sel], axis=0))[:dim] if dim == 2 else np.arange(3)
    axes = np.sort(axes)
    ev = sel if len(sel) <= max_eval else np.sort(rng.choice(sel, max_eval, replace=False))
    if physics == "ns":
        if "U" not in f:
            raise ValueError("ns needs the velocity field U")
        Y = np.column_stack([f["U"][:, axes], f["p"] if "p" in f else np.full(len(c), np.nan)])
        out_names = [f"U{'xyz'[a]}" for a in axes] + ["p"]
    else:
        if field not in f:
            raise ValueError(f"the case has no field {field!r} at t = {t:g}")
        v = f[field]
        Y = (v if v.ndim == 1 else np.linalg.norm(v, axis=1))[:, None]
        out_names = [field]
    if data_points is not None:
        xd = np.asarray(data_points, float)[:, axes]
        yd = np.asarray(data_values, float).reshape(len(xd), -1)
    else:
        pick = rng.choice(sel, min(n_data, len(sel)), replace=False)
        xd, yd = c[pick][:, axes], Y[pick]
    col = rng.choice(sel, min(n_colloc, len(sel)), replace=False)
    job = PinnJob(physics, xd, yd, c[col][:, axes], c[ev][:, axes], dict(params, dim=len(axes)),
                  eval_cells=ev, names=out_names)
    if physics == "ad":
        if "U" not in f:
            raise ValueError("ad needs the CFD velocity U")
        job.u_colloc = f["U"][col][:, axes]
        if diffusivity_field == "turbulent":
            D, G = effective_diffusivity(data, t, prandtl=prandtl)
            job.d_colloc, job.g_colloc = D[col], G[col][:, axes]
            job.params["diffusivity"] = "turbulent (alphat + mu/Pr)/rho per cell"
            job.params["d_range"] = [float(D[sel].min()), float(D[sel].max())]
    job.params["axes"] = [int(a) for a in axes]
    job.params["time"] = t
    job._truth = Y[ev]  # CFD values at the evaluation cells (for the error report)
    return job


def _sutherland(case: Path) -> tuple[float, float]:
    """(As, Ts) from constant/thermophysicalProperties (air values when absent)."""
    import re

    f = Path(case) / "constant" / "thermophysicalProperties"
    t = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
    a = re.search(r"(?<![A-Za-z])As\s+([0-9.eE+-]+)", t)
    b = re.search(r"(?<![A-Za-z])Ts\s+([0-9.eE+-]+)", t)
    return (float(a.group(1)) if a else 1.458e-6), (float(b.group(1)) if b else 110.4)


def effective_diffusivity(data, t: float, *, prandtl: float = 0.71) -> tuple[np.ndarray, np.ndarray]:
    """Per cell D = alpha_eff / rho and G = grad(alpha_eff) / rho, with alpha_eff = alphat + mu(T)/Pr [kg/m/s]
    (Sutherland mu): the energy equation u.grad T = (1/rho) div(alpha_eff grad T) of a compressible RANS run."""
    from aethercfd.core.gpu import array_module, to_numpy
    from aethercfd.results.iso import tetmesh_for, velocity_gradient

    f = data.fields(t, ["alphat", "rho", "T", "p"])
    if "alphat" not in f or "T" not in f:
        raise ValueError("a turbulent diffusivity needs the alphat and T fields")
    T = f["T"]
    rho = f["rho"] if "rho" in f else f["p"] / (287.05 * T)
    As, Ts = _sutherland(data.root)
    mu = As * T ** 1.5 / (T + Ts)
    alpha = f["alphat"] + mu / prandtl
    sub = Path("constant") / data.region / "polyMesh" if data.region else Path("constant") / "polyMesh"
    tm = tetmesh_for(data.root / sub)
    xp, _name = array_module("auto")
    A = np.zeros((len(alpha), 3))
    A[:, 0] = alpha
    L = to_numpy(velocity_gradient(tm, xp.asarray(A), xp))
    return alpha / rho, L[:, 0, :] / rho[:, None]


def start(job: PinnJob, *, ml_python: str | None = None, job_dir: str | Path | None = None) -> Path:
    py = find_ml_python(ml_python)
    if py is None:
        raise RuntimeError("no PyTorch environment: set ml_python or create .ml-env (see README)")
    # absolute: the worker runs with the job folder as its working directory
    jd = (Path(job_dir) if job_dir else Path(tempfile.mkdtemp(prefix="aether-pinn-"))).resolve()
    jd.mkdir(parents=True, exist_ok=True)
    arrays = {"x_data": job.x_data, "y_data": job.y_data, "x_colloc": job.x_colloc, "x_eval": job.x_eval}
    for k in ("u_colloc", "s_colloc", "w_data", "d_colloc", "g_colloc"):
        if getattr(job, k) is not None:
            arrays[k] = getattr(job, k)
    np.savez(jd / "inputs.npz", **arrays)
    if getattr(job, "_truth", None) is not None:
        np.save(jd / "truth_eval.npy", job._truth)
    if job.eval_cells is not None:
        np.save(jd / "eval_cells.npy", job.eval_cells)
    (jd / "job.json").write_text(json.dumps({"physics": job.physics, "names": job.names, **job.params},
                                            default=float, indent=1), encoding="utf-8")
    for f in ("result.json", "outputs.npz", "history.jsonl"):
        (jd / f).unlink(missing_ok=True)
    log = open(jd / "worker.log", "w", encoding="utf-8")  # noqa: SIM115 -- the child owns it
    proc = subprocess.Popen([str(py), str(WORKER), str(jd)], cwd=str(jd), stdout=log, stderr=subprocess.STDOUT,
                            creationflags=0x08000000)
    (jd / "worker.pid").write_text(str(proc.pid))
    return jd


def progress(jd: str | Path) -> dict:
    jd = Path(jd)
    h = jd / "history.jsonl"
    rows = [json.loads(x) for x in h.read_text(encoding="utf-8").splitlines() if x.strip()] if h.is_file() else []
    done = (jd / "result.json").is_file()
    return {"done": done, "last": rows[-1] if rows else None, "history": rows}


def wait(jd: str | Path, timeout_s: float = 3600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if (Path(jd) / "result.json").is_file():
            return load(jd)
        time.sleep(1.0)
    raise TimeoutError(f"PINN still training after {timeout_s:.0f} s ({jd})")


def load(jd: str | Path) -> dict:
    jd = Path(jd)
    res = json.loads((jd / "result.json").read_text(encoding="utf-8"))
    if not res.get("ok"):
        log = (jd / "worker.log").read_text(encoding="utf-8", errors="replace")[-2000:] if (jd / "worker.log").is_file() else ""
        return res | {"log": log}
    out = np.load(jd / "outputs.npz")
    res["y_eval"] = out["y_eval"]
    res["eval_cells"] = np.load(jd / "eval_cells.npy") if (jd / "eval_cells.npy").is_file() else None
    job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    res["names"] = job.get("names", [])
    if (jd / "truth_eval.npy").is_file():
        truth = np.load(jd / "truth_eval.npy")
        errs = []
        for j in range(truth.shape[1]):
            ok = np.isfinite(truth[:, j])
            if ok.any():
                e = res["y_eval"][ok, j] - truth[ok, j]
                errs.append(float(np.sqrt(np.mean(e ** 2)) / (np.std(truth[ok, j]) or 1.0)))
            else:
                errs.append(None)
        res["rmse_over_std_vs_cfd"] = errs
    res["history"] = progress(jd)["history"]
    return res
