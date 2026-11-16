"""PINNs from the app (no torch here): build a job from the case, run the worker in the ML environment.

The network trains out of process in a separate PyTorch environment, so the app stays small and a CUDA or
torch problem can never take the GUI down. ``find_ml_python`` looks, in this order, at the ``ml_python``
setting, the ``AETHERCFD_ML_PYTHON`` environment variable, ``<repo>/.ml-env``, the default place
``scripts/setup_ml_env.py`` creates (``%LOCALAPPDATA%/AetherCFD/ml-env`` on Windows) and, in a packaged
app, ``ml-env`` next to the exe. Typical uses:

- reconstruct a field from sparse probes with the flow physics as a prior (``ad``: a temperature or
  mixture fraction carried by the CFD velocity; ``ns``: an incompressible flow from a few velocity probes);
- a smooth, differentiable surrogate of a CFD field over a box (``data``), e.g. for sensitivities.

A result is a PINN reconstruction, never a CFD solution. Every result built from a case reports its error on
CFD cells the network did not train on, and on held-out data points.

    job = build_job(case, physics="ad", field="T", box=bbox, n_data=40, diffusivity=2e-5)
    job = build_job(cht_case, physics="data", field="T", region="fluid")   # one region of a CHT case
    jd = start(job)             # returns the job folder; training runs in the background
    progress(jd)                # last loss values, done?
    cancel(jd)                  # stop it (the worker exits; killed after a grace period)
    res = load(jd)              # predictions on the box's cells, held-out errors, device, seconds, seed
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

from aethercfd.ml.envsetup import check_env, default_env_dir, env_python

WORKER = Path(__file__).with_name("pinn_worker.py")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW; POSIX refuses any creationflags
KIND = "PINN reconstruction (a neural-network fit to sparse data with the physics as a penalty; not a CFD solution)"
SETUP_HINT = ("PINN training needs a separate Python environment with PyTorch. Create it once with "
              "'python scripts/setup_ml_env.py' from the AetherCFD source folder (it installs the CUDA build of "
              "PyTorch when an NVIDIA GPU is present, the CPU build otherwise, checks it and saves its path in the "
              "settings). Or set the ml_python setting, or the AETHERCFD_ML_PYTHON environment variable, to the "
              "python of an environment that has torch and numpy. See docs/pinn.md.")
_PROCS: dict[str, subprocess.Popen] = {}  # job folder -> worker started by this process (exact exit status)


def _as_python(c: str | Path | None) -> Path | None:
    """A python file as is; an environment folder -> its python."""
    if not c:
        return None
    p = Path(str(c).strip().strip('"'))
    if p.is_dir():
        p = env_python(p)
    return p if p.is_file() else None


def find_ml_python(setting: str | None = None) -> Path | None:
    repo = Path(__file__).resolve().parents[3]
    cands = [setting, os.environ.get("AETHERCFD_ML_PYTHON"), repo / ".ml-env"]
    cands.append(default_env_dir())
    if getattr(sys, "frozen", False):  # packaged app: next to the installed exe
        cands.append(Path(sys.executable).parent / "ml-env")
    for c in cands:
        p = _as_python(c)
        if p is not None:
            return p
    return None


def ml_status(setting: str | None = None) -> dict:
    """available True with torch version, GPU and python; or False with a ``note`` and the ``hint`` how to fix
    it (shown as is in the panel and the copilot)."""
    py = find_ml_python(setting)
    if py is None:
        bad = f" The ml_python setting {setting!r} is not a python file or environment folder." if setting else ""
        return {"available": False, "note": "no PyTorch environment found." + bad, "hint": SETUP_HINT}
    st = check_env(py)
    if not st["available"]:
        st["hint"] = SETUP_HINT
    return st


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
    x_val: np.ndarray | None = None  # held-out points: never trained on, their error is reported
    y_val: np.ndarray | None = None
    train_cells: np.ndarray | None = None  # cell ids used as training data (left out of the error vs the CFD)


def build_job(case: str | Path, *, physics: str, field: str, time: float | None = None,
              box: tuple[list[float], list[float]] | None = None, region: str = "", n_data: int = 50,
              n_colloc: int = 8000,
              data_points: np.ndarray | None = None, data_values: np.ndarray | None = None,
              max_eval: int = 200_000, seed: int = 0, diffusivity_field: str | None = None,
              prandtl: float = 0.71, n_holdout: int = 500, holdout_fraction: float = 0.2, **params) -> PinnJob:
    """From the open case (``region`` of a multi-region case): the cells inside ``box`` (min, max corners;
    default all) are evaluated; data are ``n_data`` random cells of the CFD ``field`` (or given measurement
    points/values); collocation points are cell centres. ns uses field "U" (+ "p"), ad uses ``field`` with
    the CFD velocity.

    Held-out points: ``n_holdout`` other random cells (CFD data), or ``holdout_fraction`` of the measurement
    points (at least 5 kept for training). The network never trains on them; their error is in the result."""
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
    pick = None
    xv = yv = None
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
        n_hold = round(holdout_fraction * len(xd))
        n_hold = n_hold if len(xd) - n_hold >= 5 else 0  # keep at least 5 points to train on
        if n_hold:
            perm = rng.permutation(len(xd))
            hold, keep = perm[:n_hold], np.sort(perm[n_hold:])
            xv, yv, xd, yd = xd[hold], yd[hold], xd[keep], yd[keep]
            params = dict(params, val_source=f"{n_hold} of the measurement points, held out of training")
    else:
        pick = rng.choice(sel, min(n_data, len(sel)), replace=False)
        xd, yd = c[pick][:, axes], Y[pick]
        rest = np.setdiff1d(sel, pick)
        if n_holdout and rest.size:
            hold = rng.choice(rest, min(n_holdout, rest.size), replace=False)
            xv, yv = c[hold][:, axes], Y[hold]
            params = dict(params, val_source=f"{len(hold)} random CFD cells, held out of training")
    col = rng.choice(sel, min(n_colloc, len(sel)), replace=False)
    job = PinnJob(physics, xd, yd, c[col][:, axes], c[ev][:, axes], dict(params, dim=len(axes), seed=seed),
                  eval_cells=ev, names=out_names, x_val=xv, y_val=yv, train_cells=pick)
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
    job.params["case"] = str(Path(case))
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
        raise RuntimeError("No PyTorch environment found. " + SETUP_HINT)
    # absolute: the worker runs with the job folder as its working directory
    jd = (Path(job_dir) if job_dir else Path(tempfile.mkdtemp(prefix="aether-pinn-"))).resolve()
    jd.mkdir(parents=True, exist_ok=True)
    if _worker_alive(jd):
        raise RuntimeError(f"a PINN is still training in {jd} (pid {_pid(jd)}): wait for it or stop it first")
    arrays = {"x_data": job.x_data, "y_data": job.y_data, "x_colloc": job.x_colloc, "x_eval": job.x_eval}
    for k in ("u_colloc", "s_colloc", "w_data", "d_colloc", "g_colloc", "x_val", "y_val"):
        if getattr(job, k) is not None:
            arrays[k] = getattr(job, k)
    if (job.x_val is None) != (job.y_val is None):
        raise ValueError("held-out points need both x_val and y_val")
    for f in ("result.json", "result.json.tmp", "outputs.npz", "history.jsonl", "cancel", "truth_eval.npy",
              "eval_cells.npy", "train_cells.npy", "record.json"):
        (jd / f).unlink(missing_ok=True)
    np.savez(jd / "inputs.npz", **arrays)
    if getattr(job, "_truth", None) is not None:
        np.save(jd / "truth_eval.npy", job._truth)
    if job.eval_cells is not None:
        np.save(jd / "eval_cells.npy", job.eval_cells)
    if job.train_cells is not None:
        np.save(jd / "train_cells.npy", job.train_cells)
    spec = {"physics": job.physics, "names": job.names, "seed": 0, **job.params, "parent_pid": os.getpid()}
    (jd / "job.json").write_text(json.dumps(spec, default=_jsonable, indent=1), encoding="utf-8")
    with open(jd / "worker.log", "w", encoding="utf-8") as log:  # the child keeps its own inherited handle
        proc = subprocess.Popen([str(py), "-I", str(WORKER), str(jd)], cwd=str(jd), stdout=log,
                                stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    _PROCS[str(jd)] = proc
    (jd / "worker.pid").write_text(f"{proc.pid} {_create_time(proc.pid)}")
    return jd


def _jsonable(v):
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, np.integer):
        return int(v)
    return float(v)


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
    proc = _PROCS.get(str(Path(jd).resolve()))
    if proc is not None:  # started here: poll() is exact and reaps the process
        return proc.poll() is None
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


def _kill(jd: Path) -> None:
    """Kill the job's worker: the Popen started here, or the process in worker.pid when its start time matches
    (never a process that merely reuses the pid)."""
    proc = _PROCS.get(str(Path(jd).resolve()))
    if proc is not None:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)
        return
    if not _worker_alive(jd):
        return
    with contextlib.suppress(Exception):
        import psutil

        p = psutil.Process(_pid(jd))
        p.kill()
        p.wait(10)


def _write_result(jd: Path, res: dict) -> None:
    tmp = Path(jd, "result.json.tmp")
    tmp.write_text(json.dumps(res, indent=1), encoding="utf-8")
    tmp.replace(Path(jd, "result.json"))


def cancel(jd: str | Path, grace_s: float = 5.0) -> bool:
    """Stop a training run: ask the worker (it checks every few steps and writes a cancelled result), then
    kill it if it has not exited after ``grace_s``. True if a worker was running."""
    jd = Path(jd).resolve()
    if not _worker_alive(jd):
        return False
    with contextlib.suppress(OSError):
        (jd / "cancel").write_text("cancel", encoding="utf-8")
    t0 = time.time()
    while _worker_alive(jd) and time.time() - t0 < grace_s:
        time.sleep(0.1)
    if _worker_alive(jd):
        _kill(jd)
    if not (jd / "result.json").is_file():
        _write_result(jd, {"ok": False, "reason": "cancelled", "error": "stopped by the user before it finished"})
    return True


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
    done = not _worker_alive(jd)  # it writes result.json just before it exits
    return {"done": done, "last": rows[-1] if rows else None, "history": rows}


def wait(jd: str | Path, timeout_s: float = 3600, *, cancel_on_timeout: bool = True) -> dict:
    """Block until the run ends and return ``load``; after ``timeout_s`` the run is cancelled (no orphan
    process) and TimeoutError raised."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if progress(jd)["done"]:
            return load(jd)
        time.sleep(0.5)
    if cancel_on_timeout:
        cancel(jd)
    raise TimeoutError(f"the PINN was still training after {timeout_s:.0f} s" +
                       (" and was stopped" if cancel_on_timeout else "") + f" ({jd})")


def _crash_message(tail: str) -> str:
    last = next((ln for ln in reversed(tail.splitlines()) if ln.strip()), "")
    if "out of memory" in tail.lower() or "MemoryError" in tail:
        return "the PINN worker ran out of memory: use fewer collocation points or a smaller box"
    return "the PINN worker stopped without a result" + (f": {last}" if last else
                                                         " (it was killed, or the computer ran out of memory)")


def load(jd: str | Path) -> dict:
    """The result: ok, kind, the predictions ``y_eval`` on ``eval_cells``, ``heldout`` (worker: held-out points)
    and ``rmse_over_std_vs_cfd`` (CFD cells not used as training data), provenance (seed, torch, device,
    hyper-parameters). A failure: ok False with a plain ``error`` (and the worker log)."""
    jd = Path(jd)
    try:
        res = json.loads((jd / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tail = _log_tail(jd)
        return {"ok": False, "reason": "crash", "error": _crash_message(tail), "traceback": tail, "log": tail,
                "history": progress(jd)["history"]}
    res.setdefault("reason", None)
    res["history"] = progress(jd)["history"]
    if not res.get("ok"):
        return res | {"log": _log_tail(jd)}
    out = np.load(jd / "outputs.npz")
    res["y_eval"] = out["y_eval"]
    res["eval_cells"] = np.load(jd / "eval_cells.npy") if (jd / "eval_cells.npy").is_file() else None
    job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    res["names"] = job.get("names", [])
    res["kind"] = KIND
    res["job"] = {k: v for k, v in job.items() if not k.startswith("_") and k != "parent_pid"}
    if (jd / "truth_eval.npy").is_file():
        truth = np.load(jd / "truth_eval.npy")
        keep = np.ones(len(truth), bool)
        if res["eval_cells"] is not None and (jd / "train_cells.npy").is_file():
            keep = ~np.isin(res["eval_cells"], np.load(jd / "train_cells.npy"))
        errs = []
        for j in range(truth.shape[1]):
            ok = np.isfinite(truth[:, j]) & keep
            if ok.any():
                e = res["y_eval"][ok, j] - truth[ok, j]
                errs.append(float(np.sqrt(np.mean(e ** 2)) / (np.std(truth[ok, j]) or 1.0)))
            else:
                errs.append(None)
        res["rmse_over_std_vs_cfd"] = errs
        res["n_vs_cfd"] = int(keep.sum())
    record = {k: v for k, v in res.items() if k not in ("y_eval", "eval_cells", "history", "log", "traceback")}
    with contextlib.suppress(OSError):
        (jd / "record.json").write_text(json.dumps(record, indent=1, default=_jsonable), encoding="utf-8")
    res["record"] = record
    return res


def _fmt(names: list[str], errs: list | None) -> str:
    return ", ".join(f"{n} {e:.3g}" for n, e in zip(names, errs or [], strict=False) if e is not None)


def summary(res: dict) -> list[str]:
    """Plain sentences for the panel and the copilot: what the result is and how far to trust it."""
    if not res.get("ok"):
        return [str(res.get("error") or "the PINN failed")]
    names = res.get("names") or [f"output {j}" for j in range(len(res.get("fit_rmse_over_std") or []))]
    lines = [f"{KIND}."]
    if res.get("rmse_over_std_vs_cfd") is not None:
        lines.append(f"Error against the CFD field on {res.get('n_vs_cfd', 0)} cells the network did not train on "
                     f"(RMSE / std): {_fmt(names, res['rmse_over_std_vs_cfd'])}.")
    if res.get("heldout"):
        h = res["heldout"]
        lines.append(f"Error on held-out points ({h['what']}): {_fmt(names, h['rmse_over_std'])}.")
    if res.get("rmse_over_std_vs_cfd") is None and not res.get("heldout"):
        lines.append("No held-out points: only the training fit is known, so the accuracy away from the data is "
                     "unknown.")
    lines.append(f"Fit on the training points: {_fmt(names, res.get('fit_rmse_over_std'))}.")
    if res.get("hard_bc"):
        lines.append(f"Boundary values held exactly on faces {', '.join(res['hard_bc']['faces'])}.")
    lines.append(f"Trained: done on {res.get('device', '?')} ({res.get('gpu') or 'CPU'}) in "
                 f"{res.get('seconds', 0):.0f} s, seed {res.get('seed')}, torch {res.get('torch')}.")
    lines += [f"Note: {n}." for n in res.get("notes") or []]
    return lines


def record_file(res: dict, name: str, time_name: str, region: str = "") -> dict[str, str]:
    """{relative path: text} of the provenance record saved next to written PINN fields."""
    tag = f"{region}_" if region else ""
    rec = res.get("record") or {}
    return {f".aethercfd/pinn/{tag}{name}Pinn_{time_name}.json": json.dumps(rec, indent=1, default=_jsonable)}
