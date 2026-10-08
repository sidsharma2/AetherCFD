"""AetherMesh tools for the copilot (read-only analysis and design calculators; no Qt here)."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from aethercfd.copilot.tools import Tool, ToolContext, ToolError

_CACHE: dict[tuple[str, float], tuple] = {}


def _mesh_dir(ctx: ToolContext, region: str) -> Path:
    base = ctx.root / "constant"
    d = base / region / "polyMesh" if region else base / "polyMesh"
    if not (d / "points").exists() and not (d / "points.gz").exists():
        cands = sorted(p.parent.relative_to(base).as_posix() for p in base.glob("*/polyMesh/owner"))
        raise ToolError(f"no polyMesh at {ctx.rel(d)}" + (f"; regions with a mesh: {cands}" if cands else ""))
    return d


def _quality(d: Path):
    from aethercfd.mesh.polymesh import read_polymesh
    from aethercfd.mesh.quality import quality

    pts = d / "points" if (d / "points").exists() else d / "points.gz"
    key = (str(d), pts.stat().st_mtime)
    if key not in _CACHE:
        _CACHE.clear()
        t0 = time.perf_counter()
        m = read_polymesh(d)
        r = quality(m)
        _CACHE[key] = (m, r, time.perf_counter() - t0)
    return _CACHE[key]


class HotspotParams(BaseModel):
    region: str = Field("", description="Mesh region for multi-region cases (e.g. 'fluid'); '' = constant/polyMesh")
    non_ortho: float = Field(65.0, description="Flag cells whose worst face non-orthogonality exceeds this [deg]")
    skewness: float = Field(4.0)
    aspect_ratio: float = Field(40.0)
    volume_ratio: float = Field(0.2, description="Flag cells next to a neighbour this much smaller/larger (size jump)")
    length_unit: str = Field("mm", description="Unit for reported coordinates: m or mm")


def mesh_hotspots(ctx: ToolContext, p: HotspotParams) -> dict:
    from aethercfd.mesh.quality import histogram, hotspots

    d = _mesh_dir(ctx, p.region)
    m, r, secs = _quality(d)
    s = 1e3 if p.length_unit == "mm" else 1.0
    limits = {"nonOrthogonality": (">", p.non_ortho), "skewness": (">", p.skewness),
              "aspectRatio": (">", p.aspect_ratio), "volumeRatio": ("<", p.volume_ratio)}
    hs = hotspots(m, r, limits)
    bb = m.points.min(0) * s, m.points.max(0) * s

    def fmt(v):
        return [round(float(x), 4) for x in v]

    out = {"mesh": ctx.rel(d), "seconds": round(secs, 1), "summary": r.summary,
           "bounding_box": {"min": fmt(bb[0]), "max": fmt(bb[1]), "unit": p.length_unit},
           "non_ortho_histogram_deg": histogram(r.face_non_ortho, [0, 30, 45, 55, 60, 65, 70, 75, 80]),
           "aspect_ratio_histogram": histogram(r.cell_aspect_ratio, [1, 5, 10, 20, 40, 80, 160]),
           "hotspots": {}}
    for name, clusters in hs.items():
        out["hotspots"][name] = [
            {"cells": h.cells, "worst": round(h.worst, 4) if not np.isnan(h.worst) else None,
             "centroid": fmt(np.array(h.centroid) * s) if not np.isnan(h.worst) else None,
             "bbox_min": fmt(np.array(h.bbox_min) * s) if not np.isnan(h.worst) else None,
             "bbox_max": fmt(np.array(h.bbox_max) * s) if not np.isnan(h.worst) else None,
             **({"note": "remaining smaller clusters"} if np.isnan(h.worst) else {})}
            for h in clusters]
    out["definitions"] = ("OpenFOAM checkMesh definitions; per-cell worst face value; clusters are face-connected "
                          "flagged cells; volumeRatio = smaller/larger volume across a face (0.111 = 9:1 jump)")
    return out


class GradingParams(BaseModel):
    length: float = Field(gt=0, description="Edge length (any unit; sizes are in the same unit)")
    first: float | None = Field(None, description="Required first cell size (to match the neighbour block)")
    last: float | None = Field(None, description="Required last cell size")
    n: int | None = Field(None, ge=1, description="Fixed cell count (omit to let the design choose)")
    max_size: float | None = Field(None, description="Largest allowed cell (e.g. for an aspect-ratio limit)")
    max_ratio: float = Field(1.2, gt=1.0, le=2.0, description="Largest cell-to-cell size ratio")


def grading_design(ctx: ToolContext, p: GradingParams) -> dict:
    from aethercfd.mesh.grading import design, sizes

    d = design(p.length, first=p.first, last=p.last, n=p.n, max_size=p.max_size, max_ratio=p.max_ratio)
    out = d.summary()
    out["how_to_use"] = ("blockMesh: put blockMesh_grading in simpleGrading/edgeGrading for this direction and "
                         "'cells' in the block's cell count")
    if p.n is None and p.first and p.last:
        out["compare_single_ratio"] = {"cells": d.n, "expansion": p.last / p.first,
                                       "max_cell_to_cell_ratio": round(float(
                                           (lambda c: np.maximum(c[1:] / c[:-1], c[:-1] / c[1:]).max())(
                                               sizes(d.n, p.length, p.last / p.first))), 4) if d.n > 1 else 1.0}
    return out


class WallParams(BaseModel):
    yplus: float = Field(1.0, gt=0, description="Target y+ of the first cell centre")
    U: float = Field(gt=0, description="Reference velocity [m/s]")
    L: float = Field(gt=0, description="Plate length (plate) or hydraulic diameter (pipe) [m]")
    kind: str = Field("plate", description="plate | pipe")
    nu: float | None = Field(None, description="Kinematic viscosity [m2/s]")
    mu: float | None = Field(None, description="Dynamic viscosity [Pa s] (with rho)")
    rho: float | None = Field(None, description="Density [kg/m3]")
    T: float | None = Field(None, description="Temperature [K]: air viscosity by Sutherland")
    p: float | None = Field(None, description="Pressure [Pa]: air density by the ideal gas law (with T)")
    core_cell: float | None = Field(None, description="Core cell size next to the layers [m]: plan the layers too")
    layer_ratio: float = Field(1.2, gt=1.0, le=2.0)


def wall_spacing(ctx: ToolContext, p: WallParams) -> dict:
    from aethercfd.mesh.wall import first_cell_height, ideal_gas_rho, plan_layers, sutherland_mu

    rho, mu, nu = p.rho, p.mu, p.nu
    if nu is None:
        if mu is None and p.T is not None:
            mu = sutherland_mu(p.T)
        if rho is None and p.T is not None and p.p is not None:
            rho = ideal_gas_rho(p.p, p.T)
        if mu is None or rho is None:
            raise ToolError("give nu, or mu and rho, or T (and p) for air")
        nu = mu / rho
    w = first_cell_height(p.yplus, p.U, p.L, nu=nu, rho=rho, kind=p.kind)
    out = {"inputs": {"nu": nu, "rho": rho, "mu": mu}, **w.as_dict(),
           "note": "estimate from a correlation (+-30 %); first_cell = 2 * y_p for cell-centred FV"}
    if p.core_cell:
        out["layers"] = plan_layers(w.first_cell, core=p.core_cell, ratio=p.layer_ratio).as_dict()
    return out


class ChecksParams(BaseModel):
    pass


def case_checks(ctx: ToolContext, p: ChecksParams) -> dict:
    from aethercfd.case.checks import run_checks

    facts, found = run_checks(ctx.session.case())
    return {"facts": facts.__dict__,
            "findings": [{"id": f.id, "severity": f.severity, "title": f.title, "why": f.why, "evidence": f.evidence,
                          "fix": f.fix_label, "edits": [e.__dict__ for e in f.edits]} for f in found],
            "how_to_fix": "apply an edit with dict_set (file, key, value, insert_if_missing) and a rationale"}


class CombustionParams(BaseModel):
    fuel: str = Field("CH4:1", description="Fuel stream mole fractions, e.g. 'CH4:0.25, O2:0.1575, N2:0.5925' "
                                           "(Sandia D)")
    oxidizer: str = Field("O2:0.21, N2:0.79")
    T_fuel: float = 300.0
    T_ox: float = 300.0
    p: float = Field(101325.0, description="Pressure [Pa]")
    phi: float = Field(1.0, description="Equivalence ratio for premixed quantities")
    mechanism: str = Field("gri30.yaml", description="Cantera mechanism (built-in name or YAML path)")
    what: list[str] = Field(default_factory=lambda: ["stoichiometry", "flame_temperature", "transport"],
                            description="any of stoichiometry, flame_temperature, transport, flame_speed (~20 s), "
                                        "ignition, regime, advice")
    u_prime: float | None = Field(None, description="Turbulence intensity [m/s] for the regime")
    l_t: float | None = Field(None, description="Integral length [m] for the regime")
    flame: str = Field("non-premixed", description="premixed | non-premixed | partially-premixed (advice)")
    want: list[str] = Field(default_factory=lambda: ["temperature"], description="advice targets: temperature, "
                                                                                    "CO, NOx, soot, extinction, ignition")
    round_jet: bool = False


def combustion_precalc(ctx: ToolContext, p: CombustionParams) -> dict:
    from aethercfd.combustion import calc

    st = calc.Streams(p.mechanism, p.fuel, p.oxidizer, p.T_fuel, p.T_ox, p.p)
    out: dict = {}
    if "stoichiometry" in p.what:
        out["stoichiometry"] = calc.stoichiometric(st)
    if "flame_temperature" in p.what:
        r = calc.adiabatic_flame(st, [round(x, 2) for x in np.arange(0.6, 1.61, 0.1)])
        out["flame_temperature"] = {"peak": r["peak"], "rows": [{"phi": x["phi"], "T_ad": round(x["T_ad"], 1),
                                                                 "X_CO": x["X_CO"], "X_NO": x["X_NO"]}
                                                                for x in r["rows"]]}
    if "transport" in p.what:
        out["transport_unburnt"] = calc.transport(st, phi=p.phi)
    fl = None
    if "flame_speed" in p.what or ("regime" in p.what and p.u_prime):
        fl = calc.flame_speed(st, p.phi)
        out["flame_speed"] = {"S_L": fl.S_L, "delta_L": fl.delta_L, "T_b": fl.T_ad}
    if "ignition" in p.what:
        out["ignition"] = [calc.ignition_delay(st, p.phi, T0) for T0 in (1100, 1300, 1500, 1800)]
    if "regime" in p.what and p.u_prime and p.l_t and fl is not None:
        out["regime"] = calc.regime(fl.S_L, fl.delta_L, p.u_prime, p.l_t, calc.transport(st, phi=p.phi)["nu"])
    if "advice" in p.what:
        from aethercfd.combustion.advisor import Situation, advise

        fork = "foundation" if "foundation" in str(getattr(ctx.session.case().fork_guess, "family", "")).lower() \
            else "esi"
        reg = out.get("regime", {})
        out["advice"] = [{"model": a.model, "why": a.why, "caveats": a.caveats, "turbulence": a.turbulence,
                          "chemistry": a.chemistry, "files": a.files}
                         for a in advise(Situation(p.flame, reg.get("regime"), reg.get("Da"), reg.get("Ka"),
                                                   tuple(p.want), calc.gas_summary(p.mechanism)["species"],
                                                   int(ctx.session.case().n_cells or 0), True, p.round_jet, fork))]
    return out


class CodeCreateParams(BaseModel):
    template: str = Field(description="kEpsilonPope | codedSource | codedFixedValue | solverCopy")
    name: str = Field(description="C++ identifier for the new model/solver")
    rationale: str
    confidence: float = Field(ge=0, le=1)


class CodeBuildParams(BaseModel):
    name: str = Field(description="Folder name under <case>/aether-code/")
    rationale: str
    confidence: float = Field(ge=0, le=1)


def code_templates(ctx: ToolContext, _p: ChecksParams) -> dict:
    from aethercfd.codegen.templates import TEMPLATES

    return {"templates": [{"id": t.id, "title": t.title, "fork": t.family, "kind": t.kind, "summary": t.summary,
                           "usage": t.usage} for t in TEMPLATES],
            "where": "sources go to <case>/aether-code/<name>/; libraries build into $FOAM_USER_LIBBIN in WSL"}


def code_create(ctx: ToolContext, p: CodeCreateParams) -> dict:
    from aethercfd.copilot.tools import Justified

    dest = ctx.root / "aether-code" / p.name
    return ctx.execute("code.create", {"template": p.template, "name": p.name, "dest": str(dest)},
                       Justified(rationale=p.rationale, confidence=p.confidence))


def code_build(ctx: ToolContext, p: CodeBuildParams) -> dict:
    from aethercfd.copilot.tools import Justified

    out = ctx.execute("code.build", {"dir": str(ctx.root / "aether-code" / p.name)},
                      Justified(rationale=p.rationale, confidence=p.confidence))
    data = out.pop("data", None) or {}
    out["library"] = data.get("library")
    out["diagnostics"] = data.get("diagnostics", [])[:30]
    out["output_tail"] = (data.get("output") or "")[-4000:]
    return out


MESH_TOOLS = [
    Tool("code_templates", "Templates for writing your own OpenFOAM physics (k-epsilon with Pope's round-jet "
         "correction, coded sources, coded BCs, copy-a-solver).", ChecksParams, code_templates),
    Tool("code_create", "Create OpenFOAM source code from a template in <case>/aether-code/<name> (user approves "
         "the new files).", CodeCreateParams, code_create, mutates=True),
    Tool("code_build", "Compile <case>/aether-code/<name> with wmake inside WSL using the case's OpenFOAM; returns "
         "compiler diagnostics (file, line, message) to fix with write_file. Needs approval.", CodeBuildParams,
         code_build, mutates=True),
    Tool("combustion_precalc", "Combustion pre-calculations with Cantera before a reacting run: stoichiometric "
         "mixture fraction, adiabatic flame temperature vs phi (with CO/NO), mixture transport, laminar flame speed "
         "and thickness, ignition delay, Peters regime (Da, Ka, Kolmogorov scale), and a ranked combustion-model "
         "recommendation with the exact OpenFOAM dictionary entries for this case's fork.", CombustionParams,
         combustion_precalc),
    Tool("case_checks", "Numerics lab and pre-run checks: steady/compressible/turbulence facts and findings "
         "(scheme boundedness, non-orthogonal correction vs the mesh, relaxation, turbulence fields, pressure "
         "dimensions, thermo range, BC consistency, disk projection) with the exact dict edits that fix them.",
         ChecksParams, case_checks),
    Tool("mesh_hotspots", "Mesh quality computed directly from constant/polyMesh with checkMesh's definitions, "
         "plus WHERE the bad cells are: face-connected clusters per metric (non-orthogonality, skewness, aspect "
         "ratio, size jumps) with cell counts, worst values, centroids and bounding boxes, and histograms. "
         "Use it to decide which blocks/refinement regions to change.", HotspotParams, mesh_hotspots),
    Tool("grading_design", "Design blockMesh grading for one edge: given its length and the cell sizes required at "
         "its ends (to match neighbouring blocks), the cell count and multi-grading that keep every cell-to-cell "
         "ratio below max_ratio and cells below max_size. Returns the blockMesh grading text.", GradingParams,
         grading_design),
    Tool("wall_spacing", "First cell height for a target y+ (flat-plate or pipe skin-friction correlation; air "
         "properties from T and p via Sutherland/ideal gas) and an optional layer plan up to a core cell size.",
         WallParams, wall_spacing),
]
