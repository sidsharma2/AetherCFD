"""PINNs from the app (no torch here): build a job from the case, run the worker in the ML environment.

The network trains out of process in a separate PyTorch environment (``ml_python`` setting, or
``<repo>/.ml-env``, or ``%LOCALAPPDATA%/AetherCFD/ml-env``), so the app stays small and a CUDA/torch
problem can never take the GUI down. Typical uses:

- reconstruct a field from sparse probes with the flow physics as a prior (``ad``: a temperature or
  mixture fraction carried by the CFD velocity; ``ns``: an incompressible flow from a few velocity probes);
- a smooth, differentiable surrogate of a CFD field over a box (``data``), e.g. for sensitivities.

    job = build_job(case, physics="ad", field="T", box=bbox, n_data=40, diffusivity=2e-5)
    job = build_job(cht_case, physics="data", field="T", region="fluid")   # one region of a CHT case
    jd = start(job)             # returns the job folder; training runs in the background
    progress(jd)                # last loss values, done?
    res = load(jd)              # predictions on the box's cells, fit errors, device, seconds
"""

from __future__ import annotations

import contextlib
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
              box: tuple[list[float], list[float]] | None = None, region: str = "", n_data: int = 50,
              n_colloc: int = 8000,
              data_points: np.ndarray | None = None, data_values: np.ndarray | None = None,
              max_eval: int = 200_000, seed: int = 0, diffusivity_field: str | None = None,
              prandtl: float = 0.71, **params) -> PinnJob:
    """From the open case (``region`` of a multi-region case): the cells inside ``box`` (min, max corners;
    default all) are evaluated; data are ``n_data`` random cells of the CFD ``field`` (or given measurement
    points/values); collocation points are cell centres. ns uses field "U" (+ "p"), ad uses ``field`` with
    the CFD velocity."""
    from aethercfd.results.casedata import CaseData

    if region is not None and not isinstance(region, str):  # the old keyword: region=(lo, hi) was the box
        box, region = region, ""
    rng = np.random.default_rng(seed)
    data = CaseData(case, region or "")
    if time is None:
        saved = [x for x in data.times() if x > 0]
        if not saved:  # only the initial conditions: nothing a network could learn the flow from
            raise ValueError("the case has no saved results (only the initial conditions at t = 0): run it first")
    t = time if time is not None else saved[-1]
    names = ["U", "p"] if physics == "ns" else [field] + (["U"] if physics == "ad" else [])
    f = data.fields(t, names)
    c = data.centres(t)
    sel = np.arange(len(c))
    if box is not None:
        lo, hi = np.asarray(box[0], float), np.asarray(box[1], float)
        sel = np.flatnonzero(np.all((c >= lo) & (c <= hi), axis=1))
        if sel.size == 0:
            raise ValueError("no cells inside the box (region)")
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
        from aethercfd.assim.measurements import nearest_cells, outside_mesh

        P = np.asarray(data_points, float)
        idx, dist = nearest_cells(c, P)
        out = outside_mesh(c, P, idx, dist)
        if out.all():
            raise ValueError("none of the measurement points lies inside the mesh: check the CSV length unit")
        xd = P[~out][:, axes]
        yd = np.asarray(data_values, float).reshape(len(P), -1)[~out]
        if out.any():
            params = dict(params, data_points_outside_mesh=int(out.sum()))
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
    job.params["region"] = data.region
    job._truth = Y[ev]  # CFD values at the evaluation cells (for the error report)
    return job


def parse_region(v) -> tuple[list[float], list[float]] | None:
    """"xmin ymin zmin xmax ymax zmax" (text or numbers) -> (lo, hi); empty -> None."""
    if isinstance(v, str):
        v = v.replace(",", " ").split()
    if not v:
        return None
    try:
        nums = [float(x) for x in v]
    except (TypeError, ValueError):
        raise ValueError("region: six numbers, xmin ymin zmin xmax ymax zmax") from None
    if len(nums) != 6:
        raise ValueError(f"region: six numbers, xmin ymin zmin xmax ymax zmax (got {len(nums)})")
    lo, hi = nums[:3], nums[3:]
    if not all(a < b for a, b in zip(lo, hi, strict=True)):
        raise ValueError("region: each min must be smaller than its max")
    return lo, hi


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
    if _worker_alive(jd):
        raise RuntimeError(f"a PINN is still training in {jd} (pid {_pid(jd)}): wait for it or stop that process")
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
    with open(jd / "worker.log", "w", encoding="utf-8") as log:  # the child keeps its own inherited handle
        proc = subprocess.Popen([str(py), str(WORKER), str(jd)], cwd=str(jd), stdout=log, stderr=subprocess.STDOUT,
                                creationflags=0x08000000)
    (jd / "worker.pid").write_text(f"{proc.pid} {_create_time(proc.pid)}")
    return jd


def _pid(jd: Path) -> int | None:
    try:
        return int((Path(jd) / "worker.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _create_time(pid: int) -> float:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return 0.0


def _worker_alive(jd: Path) -> bool:
    """The worker recorded in worker.pid still runs (same pid AND start time: pids are reused on Windows)."""
    try:
        parts = (Path(jd) / "worker.pid").read_text().split()
        pid, started = int(parts[0]), float(parts[1]) if len(parts) > 1 else 0.0
    except (OSError, ValueError, IndexError):
        return False
    try:
        import psutil
    except ImportError:
        return not (Path(jd) / "result.json").is_file()  # cannot tell: assume it runs until it reports
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE and (
            not started or abs(p.create_time() - started) < 1.0)
    except Exception:
        return False


def _log_tail(jd: Path, n: int = 2000) -> str:
    f = Path(jd) / "worker.log"
    return f.read_text(encoding="utf-8", errors="replace")[-n:] if f.is_file() else ""


def progress(jd: str | Path) -> dict:
    """``done`` also when the worker died without a result (killed, out of memory, no torch): ``load`` then
    returns ok False with the end of worker.log, instead of the caller polling forever."""
    jd = Path(jd)
    h = jd / "history.jsonl"
    rows = []
    if h.is_file():
        for x in h.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):  # the line being written right now
                rows.append(json.loads(x))
    done = (jd / "result.json").is_file() or not _worker_alive(jd)
    return {"done": done, "last": rows[-1] if rows else None, "history": rows}


def wait(jd: str | Path, timeout_s: float = 3600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if progress(jd)["done"]:
            return load(jd)
        time.sleep(1.0)
    raise TimeoutError(f"PINN still training after {timeout_s:.0f} s ({jd})")


def load(jd: str | Path) -> dict:
    jd = Path(jd)
    try:
        res = json.loads((jd / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tail = _log_tail(jd)
        last = next((ln for ln in reversed(tail.splitlines()) if ln.strip()), "")
        return {"ok": False, "error": "the PINN worker stopped without a result" + (f": {last}" if last else
                                                                                   " (killed or out of memory?)"),
                "traceback": tail, "log": tail}
    if not res.get("ok"):
        return res | {"log": _log_tail(jd)}
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
