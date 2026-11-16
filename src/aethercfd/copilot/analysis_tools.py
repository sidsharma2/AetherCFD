"""Copilot tools for analysis: diffs, modal analysis, measurements + assimilation, run surrogates, CAD import."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field, model_validator

from aethercfd.copilot.tools import Justified, Tool, ToolContext, ToolError, _clip


def _region_field():
    return Field("", description="region of a multi-region (CHT) case, e.g. fluid: required there, empty otherwise")


def _case_data(ctx: ToolContext, region: str):
    """CaseData of the case (of ``region``); a multi-region case without a region is a clear error."""
    from aethercfd.results.casedata import CaseData

    data = CaseData(ctx.root)
    regs = data.regions()
    if regs and not region:
        raise ToolError(f"multi-region case: pass region, one of {', '.join(regs)} (the fluid region is "
                        f"{data.default_region()!r}); fields and cells differ per region")
    if region and region not in regs:
        raise ToolError(f"no region {region!r}: " + (f"the case's regions are {', '.join(regs)}" if regs else
                                                     "the case has a single region (leave region empty)"))
    return data.for_region(region) if region else data


class DiffParams(BaseModel):
    a: str = Field(description="File relative to the case")
    b: str = Field(description="File relative to the case")
    context: int = Field(3, ge=0, le=50)


def diff_files(ctx: ToolContext, p: DiffParams) -> dict:
    from aethercfd.core.textio import decode_bytes
    from aethercfd.documents.diff import side_by_side, unified

    fa, fb = ctx.path(p.a), ctx.path(p.b)
    for f in (fa, fb):
        if not f.is_file():
            raise ToolError(f"{f} is not a file")
    ta, tb = decode_bytes(fa.read_bytes())[0], decode_bytes(fb.read_bytes())[0]
    n = max(ta.count("\n"), tb.count("\n"))
    if n > 60_000:  # difflib is quadratic in the worst case: field data would hang the tool
        raise ToolError(f"{n:,} lines: too large to diff line by line (field data?); compare the arrays with "
                        "app_python / numpy instead")
    return {"summary": side_by_side(ta, tb).summary(), "diff": _clip(unified(ta, tb, p.a, p.b, p.context), 12000)}


class ModesParams(BaseModel):
    field: str = Field(description="Cell field saved at several times, e.g. p, T, U")
    method: str = Field("pod", description="pod | dmd")
    component: str = Field("magnitude", description="for vectors: magnitude | x | y | z")
    n_modes: int = Field(6, ge=1, le=50)
    delays: int = Field(1, ge=1, le=50, description="DMD time delays (> 1 for structures oscillating in place)")
    first_time: float | None = Field(None, description="ignore times before this (e.g. a start-up transient)")
    region: str = _region_field()


def modal_analysis(ctx: ToolContext, p: ModesParams) -> dict:
    from aethercfd.results.modes import dmd, pod

    data = _case_data(ctx, p.region)
    times = [t for t in data.times() if t > 0 and (p.first_time is None or t >= p.first_time)]
    if len(times) < 3:
        raise ToolError(f"only {len(times)} saved time(s) after 0: modal analysis needs at least 3 (write more often)")
    try:
        ts, X = data.snapshots(p.field, times)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if X.ndim == 3:
        X = np.linalg.norm(X, axis=2) if p.component == "magnitude" else X[:, :, "xyz".index(p.component)]
    if p.method == "pod":
        try:
            vol = data.quality().cell_volume
        except Exception:
            vol = None
        r = pod(X, weights=vol, n_modes=p.n_modes)
        return {"method": "POD", "times": ts, "energy_percent": [round(100 * e, 3) for e in r.energy],
                "modes_for_90pct": r.modes_for(0.9), "modes_for_99pct": r.modes_for(0.99), "backend": r.backend,
                "notes": r.notes}
    dt = float(np.median(np.diff(ts)))
    r = dmd(X, dt=dt, rank=p.n_modes, delays=p.delays)
    o = np.argsort(-r.growth_rate)
    return {"method": "DMD", "times": ts, "dt": dt, "backend": r.backend, "notes": r.notes,
            "modes": [{"growth_rate": float(r.growth_rate[k]), "frequency": float(abs(r.frequency[k])),
                       "amplitude": float(r.amplitudes[k])} for k in o],
            "reading": "raw snapshots: the mode with growth ~0 and frequency 0 is the mean field; growth_rate > 0 grows per unit of the time axis (iterations for a steady solver)"}


class MeasParams(BaseModel):
    csv: str = Field(description="Measurements CSV in the case: x, y, z, <field>[, <field>_std]")
    field: str
    time: float | None = Field(None, description="result time (default latest)")
    length_unit: str = Field("m", description="unit of x, y, z in the CSV: m | mm | cm")
    region: str = _region_field()


def _meas_sim(ctx: ToolContext, p: MeasParams):
    from aethercfd.assim.measurements import load_measurements

    try:
        m = load_measurements(ctx.path(p.csv), length_unit=p.length_unit)
    except (OSError, ValueError) as exc:
        raise ToolError(str(exc)) from None
    if p.field not in m.values:
        raise ToolError(f"{p.csv} has no column {p.field!r} (has {sorted(m.values)})")
    data = _case_data(ctx, p.region)
    times = data.times()
    if p.time is None and not times:
        raise ToolError("the case has no saved results")
    t = p.time if p.time is not None else times[-1]
    f = data.fields(t, [p.field])
    if p.field not in f:
        raise ToolError(f"the simulation has no field {p.field!r} at t = {t:g}"
                        + (f" in region {p.region} (it has {', '.join(data.field_names())})" if p.region else ""))
    v = f[p.field]
    return m, data, t, data.centres(t), (v if v.ndim == 1 else np.linalg.norm(v, axis=1))


def compare_measurements(ctx: ToolContext, p: MeasParams) -> dict:
    from aethercfd.assim.measurements import compare

    m, _data, t, c, v = _meas_sim(ctx, p)
    rep = compare(m, {p.field: v}, c)
    rep["time"] = t
    if p.region:
        rep["region"] = p.region
    return rep


class AssimParams(MeasParams, Justified):
    sigma_b: float = Field(gt=0, description="background std: how wrong the simulation may be, field units")
    length: float = Field(gt=0, description="correlation length of the model error [m]")
    sigma_obs: float | None = Field(None, description="measurement std when the CSV has no <field>_std column")
    write: bool = Field(False, description="write <field>Assim/AssimCorr/AssimStd fields (needs approval)")


def assimilate_measurements(ctx: ToolContext, p: AssimParams) -> dict:
    from aethercfd.assim.measurements import assimilate
    from aethercfd.results.write_fields import field_files

    m, _data, t, c, v = _meas_sim(ctx, p)
    obs = m.values[p.field]
    so = m.sigma.get(p.field)
    if so is None:
        so = p.sigma_obs if p.sigma_obs is not None else np.maximum(0.01 * np.abs(obs), 1e-12)
    try:
        a = assimilate(c, v, m.points, obs, so, sigma_b=p.sigma_b, length=p.length)
    except (ValueError, np.linalg.LinAlgError) as exc:
        raise ToolError(str(exc)) from None
    out = {"time": t, "innovation_rms": float(np.sqrt(np.mean(a.innovation ** 2))),
           "residual_rms": float(np.sqrt(np.mean(a.residual ** 2))), "cells_corrected": a.touched_cells,
           "max_abs_correction": float(np.abs(a.correction).max()),
           "posterior_std_range": [float(a.posterior_std.min()), float(a.posterior_std.max())], "notes": a.notes}
    if p.region:
        out["region"] = p.region
    if p.write:
        tname = next((d for d in (f"{t:g}", str(t)) if (ctx.root / d).is_dir() or
                      (ctx.root / "processor0" / d).is_dir()), f"{t:g}")
        files = field_files(ctx.root, tname, {f"{p.field}Assim": a.analysis, f"{p.field}AssimCorr": a.correction,
                                               f"{p.field}AssimStd": a.posterior_std}, like=p.field,
                            region=p.region)
        out["write"] = ctx.execute("files.write", {"root": str(ctx.root), "files": files}, p)
    return out


class SurrogateParams(BaseModel):
    inputs: list[list[float]] = Field(description="one row of input values per run")
    outputs: list[float] = Field(description="the result of each run")
    predict_at: list[list[float]] = Field(default_factory=list, description="inputs to predict")
    names: list[str] = Field(default_factory=list, description="input names, for the report")


def surrogate_runs(ctx: ToolContext, p: SurrogateParams) -> dict:
    from aethercfd.ml.surrogate import GPSurrogate

    X = np.asarray(p.inputs, dtype=float)
    gp = GPSurrogate().fit(X, p.outputs)
    out = {"length_scales": dict(zip(p.names or [f"x{i + 1}" for i in range(X.shape[1])],
                                     map(float, gp.length_scales()), strict=False)),
           "suggest_next": [float(v) for v in gp.suggest(list(zip(X.min(0), X.max(0), strict=True)))],
           "notes": gp.notes}
    if p.predict_at:
        m, s = gp.predict(p.predict_at)
        out["predictions"] = [{"at": a, "mean": float(mi), "std": float(si), "extrapolated": bool(e)}
                              for a, mi, si, e in zip(p.predict_at, m, s, gp.last_extrapolated, strict=True)]
    return out


class CadParams(Justified):
    source: str = Field(description="CAD file in the case (STEP/IGES/BREP/FCStd)")
    name: str = Field("", description="STL name (default: the CAD file's name)")
    regions: str = Field("solids", description="solids (one patch per named part) | faces (one per CAD face)")
    scale: float = Field(0.001, description="CAD units to metres (FreeCAD works in mm: 0.001)")


def cad_import(ctx: ToolContext, p: CadParams) -> dict:
    import tempfile

    from aethercfd.mesh.cad import cad_to_stl

    src = ctx.path(p.source)
    fam = "foundation" if "foundation" in str(getattr(ctx.session.case().fork_guess, "family", "")).lower() else "esi"
    rel = f"{'constant/triSurface' if fam == 'esi' else 'constant/geometry'}/{p.name or src.stem}.stl"
    with tempfile.TemporaryDirectory(prefix="aether-cad-") as td:
        r = cad_to_stl(src, Path(td) / "out.stl", regions=p.regions, scale=p.scale,
                       freecad=getattr(ctx.session.settings, "freecad_cmd", None))
        if not r.ok:
            return {"ok": False, "notes": r.notes, "log": r.log[-3000:]}
        out = ctx.execute("files.write", {"root": str(ctx.root), "copies": {rel: str(r.stl)}}, p)
    out.update({"stl": rel, "regions": r.regions, "bbox_m": r.bbox_m, "notes": r.notes,
                "next": "snappy_setup with this STL (named regions become patches)"})
    return out


ANALYSIS_TOOLS = [
    Tool("diff_files", "Unified diff of two case files (e.g. fvSolution of two runs, a dict before/after).", DiffParams,
         diff_files),
    Tool("modal_analysis", "POD energy spectrum or DMD growth rates/frequencies of a field over the saved times "
         "(diagnose oscillations or a mode that makes a steady run diverge).", ModesParams, modal_analysis),
    Tool("compare_measurements", "Experiment vs simulation: sample a field at the measurement points of a CSV "
         "(nearest cell) and report bias, RMSE, R^2 and the worst points.", MeasParams, compare_measurements),
    Tool("assimilate_measurements", "Optimal-interpolation data assimilation of a CSV of measurements into a "
         "field: corrected field, correction and posterior std; write=true saves them as fields (approval).",
         AssimParams, assimilate_measurements, mutates=True),
    Tool("surrogate_runs", "Gaussian-process surrogate over runs (inputs -> one output): predictions with "
         "uncertainty, extrapolation flags, and the next run that reduces the uncertainty most.", SurrogateParams,
         surrogate_runs),
    Tool("cad_import", "Convert a CAD file in the case (STEP/IGES/BREP/FCStd) with FreeCAD into a meshing STL in "
         "constant/triSurface (named parts become patches). Needs approval.", CadParams, cad_import, mutates=True),
]


class PinnParams(Justified):
    physics: str = Field("ad", description="ad (scalar carried by the CFD velocity) | ns (incompressible U, p) | data")
    field: str = Field("T", description="scalar to reconstruct for ad/data (ns uses U and p)")
    coefficient: float = Field(1e-5, gt=0, description="ad: diffusivity D [m2/s]; ns: kinematic viscosity [m2/s]")
    n_data: int = Field(40, ge=3, description="random CFD cells used as data (ignored with csv)")
    csv: str = Field("", description="measurements CSV in the case to use as data instead")
    csv_unit: str = Field("m", description="length unit of the CSV coordinates: m | mm | cm | in")
    box: list[float] = Field(default_factory=list, description="xmin ymin zmin xmax ymax zmax [m] (empty: all)")
    region: str = _region_field()
    epochs: int = Field(3000, ge=100, le=100000)
    lbfgs: int = Field(400, ge=0, le=20000)
    wait_min: float = Field(20, gt=0, le=240, description="minutes to wait; the run is stopped after that")
    seed: int = Field(0, ge=0, description="random seed (data, held-out and collocation points, network); saved")
    write: bool = Field(False, description="write <field>Pinn and <field>PinnErr fields (approval)")

    @model_validator(mode="before")
    @classmethod
    def _old_box(cls, v):
        if isinstance(v, dict) and isinstance(v.get("region"), list | tuple):  # region used to be the box
            v = {**v, "box": v.get("box") or v["region"], "region": ""}
        return v


def pinn_train(ctx: ToolContext, p: PinnParams) -> dict:
    from aethercfd.ml.pinn import (
        SETUP_HINT,
        build_job,
        find_ml_python,
        parse_region,
        record_file,
        start,
        summary,
        wait,
    )

    data = _case_data(ctx, p.region)
    ml_py = getattr(ctx.session.settings, "ml_python", None)
    if find_ml_python(ml_py) is None:  # say how to get one before the case's fields are read
        raise ToolError("No PyTorch environment found, so a PINN cannot be trained on this computer. " + SETUP_HINT)
    pts = vals = None
    if p.csv:
        from aethercfd.assim.measurements import load_measurements

        try:
            m = load_measurements(ctx.path(p.csv), length_unit=p.csv_unit)
        except (OSError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        if p.field not in m.values:
            raise ToolError(f"{p.csv} has no column {p.field!r}")
        pts, vals = m.points, m.values[p.field]
    try:
        box = parse_region(p.box)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    params = {"epochs_adam": p.epochs, "lbfgs_steps": p.lbfgs, ("diffusivity" if p.physics == "ad" else "nu"):
              p.coefficient, "seed": p.seed}
    try:
        job = build_job(ctx.root, physics=p.physics, field=p.field, box=box, region=p.region, n_data=p.n_data,
                        data_points=pts, data_values=vals, **params)
        jd = start(job, ml_python=ml_py, job_dir=ctx.root / ".aethercfd" / "local" / "pinn" / "copilot")
    except (ValueError, RuntimeError, OSError, KeyError, IndexError) as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}" if isinstance(exc, (KeyError, IndexError)) else str(exc)) from None
    try:
        res = wait(jd, timeout_s=p.wait_min * 60)  # cancels the run on timeout: no orphan worker
    except TimeoutError as exc:
        return {"ok": False, "error": f"{exc}. Raise wait_min, or use fewer epochs or data points.", "job_dir": str(jd)}
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason"), "error": res.get("error"),
                **({"log": res.get("log", "")[-1500:]} if res.get("reason") == "crash" else {})}
    out = {k: res.get(k) for k in ("kind", "rmse_over_std_vs_cfd", "n_vs_cfd", "heldout", "fit_rmse_over_std",
                                   "device", "gpu", "seconds", "seed", "torch", "hyper", "final_data_loss",
                                   "final_pde_loss", "n_data", "n_colloc", "n_eval", "notes")} | {"names": res["names"]}
    out["summary"] = " ".join(summary(res))
    out["record"] = str(jd / "record.json")
    if p.region:
        out["region"] = p.region
    if p.write:
        from aethercfd.results.write_fields import field_files

        t = job.params["time"]
        name = job.names[0] if job.physics != "ns" else "U"
        if job.physics == "ns":
            base = data.fields(t, ["U"])["U"]
            pred = base.copy()
            for j, a in enumerate(job.params["axes"]):
                pred[res["eval_cells"], a] = res["y_eval"][:, j]
        else:
            v = data.fields(t, [name])[name]
            base = v if v.ndim == 1 else np.linalg.norm(v, axis=1)
            pred = base.copy()
            pred[res["eval_cells"]] = res["y_eval"][:, 0]
        tname = next((d for d in (f"{t:g}", str(t)) if (ctx.root / d).is_dir()
                      or (ctx.root / "processor0" / d).is_dir()), f"{t:g}")
        files = field_files(ctx.root, tname, {f"{name}Pinn": pred, f"{name}PinnErr": pred - base}, like=name,
                            region=p.region)
        files |= record_file(res, name, tname, p.region)
        out["write"] = ctx.execute("files.write", {"root": str(ctx.root), "files": files}, p)
    return out


ANALYSIS_TOOLS.append(Tool(
    "pinn_train", "Train a physics-informed neural network on the GPU (PyTorch, separate environment): reconstruct a "
    "scalar from sparse data with advection-diffusion physics using the CFD velocity, an incompressible flow (ns), or a "
    "data-only fit. The result is a reconstruction, not a CFD solution: report its held-out error "
    "(rmse_over_std_vs_cfd on cells it did not train on, and heldout) to the user, not only the loss. "
    "write=true saves <field>Pinn fields (approval).",
    PinnParams, pinn_train, mutates=True))
