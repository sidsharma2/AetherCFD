"""Case templates: vetted OpenFOAM tutorials as starting points, chosen by physics (no Qt here).

A template is a tutorial that exists in the installed fork (ESI v2306 and/or Foundation v14), the
physics it represents (tags used to rank templates against the user's answers), and the few
parameters people change first, located per fork. Values are edited in place with lossless edits,
keeping the existing text's form (dimension sets, ``uniform``), so the case stays the tutorial plus
a reviewable diff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    id: str
    label: str
    unit: str
    kind: str  # scalar | vector_x | vector | word
    where: dict[str, tuple[str, str]]  # fork family -> (file relative to case, keyword path)
    help: str = ""


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    summary: str
    tags: frozenset[str]
    sources: dict[str, str]  # family ("esi" | "foundation") -> path under $FOAM_TUTORIALS
    params: tuple[Param, ...] = ()
    cost: str = "minutes"  # seconds | minutes | hours (on a laptop)
    allrun: bool = False  # meshing/setup needs the tutorial's Allrun (geometry files, snappy, ...)
    notes: str = ""


def _p(id_, label, unit, kind, esi=None, of=None, help_=""):
    where = {}
    if esi:
        where["esi"] = esi
    if of:
        where["foundation"] = of
    return Param(id_, label, unit, kind, where, help_)


END = lambda d: _p("endTime", "End time / iterations", "s", "scalar", ("system/controlDict", "endTime"),  # noqa: E731
                   ("system/controlDict", "endTime"), d)

TEMPLATES: list[Template] = [
    Template("pitzDaily", "Backward-facing step (2D, steady RANS)",
             "Incompressible turbulent separated flow over a step; k-epsilon; blockMesh. The classic first case.",
             frozenset({"incompressible", "steady", "turbulent", "internal", "2d", "separation"}),
             {"esi": "incompressible/simpleFoam/pitzDaily", "foundation": "incompressibleFluid/pitzDaily"},
             (_p("Uin", "Inlet velocity", "m/s", "vector_x", ("0/U", "boundaryField/inlet/value"),
                 ("0/U", "boundaryField/inlet/value")),
              _p("nu", "Kinematic viscosity", "m²/s", "scalar", ("constant/transportProperties", "nu"),
                 ("constant/physicalProperties", "nu")),
              END("SIMPLE iterations")), "seconds"),
    Template("TJunction", "T-junction (3D, transient RANS)",
             "Incompressible transient flow splitting in a T-junction with time-varying inlet pressure; pimpleFoam.",
             frozenset({"incompressible", "transient", "turbulent", "internal", "3d"}),
             {"esi": "incompressible/pimpleFoam/RAS/TJunction", "foundation": "incompressibleFluid/TJunction"},
             (END("physical end time"),), "minutes"),
    Template("airFoil2D", "Airfoil (2D, external, Spalart-Allmaras)",
             "Incompressible external aerodynamics around an airfoil on a C-mesh; steady.",
             frozenset({"incompressible", "steady", "turbulent", "external", "2d", "aero"}),
             {"esi": "incompressible/simpleFoam/airFoil2D", "foundation": "incompressibleFluid/airFoil2D"},
             (_p("Uinf", "Free-stream velocity", "m/s", "vector", ("0/U", "internalField"), ("0/U", "internalField"),
                 "uniform (Ux Uy 0): sets the angle of attack too"), END("SIMPLE iterations")), "seconds"),
    Template("motorBike", "Motorbike (3D external, snappyHexMesh)",
             "External aerodynamics of a complex STL body: blockMesh background + snappyHexMesh with layers.",
             frozenset({"incompressible", "steady", "turbulent", "external", "3d", "snappy", "aero", "stl"}),
             {"esi": "incompressible/simpleFoam/motorBike", "foundation": "incompressibleFluid/motorBike"},
             (END("SIMPLE iterations"),), "hours", allrun=True,
             notes="Geometry comes from the tutorial resources; run Allrun first (meshes in parallel)."),
    Template("squareBend", "Square bend (3D, compressible, steady)",
             "Subsonic compressible turbulent flow in a 90° square duct bend; rhoSimpleFoam / fluid.",
             frozenset({"compressible", "steady", "turbulent", "internal", "3d", "heat"}),
             {"esi": "compressible/rhoSimpleFoam/squareBend", "foundation": "fluid/squareBend"},
             (END("SIMPLE iterations"),), "minutes"),
    Template("forwardStep", "Supersonic forward step (2D)",
             "Mach 3 inviscid flow over a forward-facing step: shocks and expansion; rhoCentralFoam / shockFluid.",
             frozenset({"compressible", "supersonic", "transient", "inviscid", "internal", "2d", "shock"}),
             {"esi": "compressible/rhoCentralFoam/forwardStep", "foundation": "fluid/forwardStep"},
             (END("physical end time"),), "seconds"),
    Template("shockTube", "Shock tube (1D, Sod problem)",
             "Riemann problem: shock, contact and expansion; a standard verification case for compressible solvers.",
             frozenset({"compressible", "supersonic", "transient", "inviscid", "1d", "shock", "verification"}),
             {"esi": "compressible/rhoCentralFoam/shockTube", "foundation": "fluid/shockTube"},
             (END("physical end time"),), "seconds", allrun=True,
             notes="setFields initialises the two states (Allrun)."),
    Template("obliqueShock", "Oblique shock (2D, supersonic)",
             "Supersonic flow deflected by a wedge: oblique shock reflection; rhoCentralFoam.",
             frozenset({"compressible", "supersonic", "transient", "inviscid", "2d", "shock"}),
             {"esi": "compressible/rhoCentralFoam/obliqueShock"}, (END("physical end time"),), "seconds"),
    Template("heatExchanger", "Heat exchanger (3D conjugate heat transfer)",
             "Air flow through a heat exchanger with solid and fluid regions coupled thermally; steady CHT.",
             frozenset({"cht", "heat", "steady", "turbulent", "internal", "3d", "multiregion"}),
             {"esi": "heatTransfer/chtMultiRegionSimpleFoam/heatExchanger",
              "foundation": "multiRegion/CHT/heatExchanger"}, (), "hours", allrun=True,
             notes="Mesh generation and region splitting run from Allrun."),
    Template("multiRegionHeater", "Multi-region heater (transient CHT)",
             "Heater blocks in air and water, transient conjugate heat transfer; chtMultiRegionFoam.",
             frozenset({"cht", "heat", "transient", "laminar", "3d", "multiregion", "buoyancy"}),
             {"esi": "heatTransfer/chtMultiRegionFoam/multiRegionHeater"}, (), "minutes", allrun=True),
    Template("buoyantCavity", "Buoyant cavity (natural convection)",
             "Differentially heated tall cavity: buoyancy-driven turbulent flow; buoyantSimpleFoam.",
             frozenset({"compressible", "steady", "turbulent", "internal", "3d", "heat", "buoyancy"}),
             {"esi": "heatTransfer/buoyantSimpleFoam/buoyantCavity", "foundation": "fluid/buoyantCavity"},
             (END("SIMPLE iterations"),), "minutes"),
    Template("counterFlowFlame2D", "Counter-flow flame (2D laminar combustion)",
             "Methane/air laminar counter-flow diffusion flame with one-step chemistry; reactingFoam.",
             frozenset({"reacting", "combustion", "transient", "laminar", "2d", "heat", "compressible"}),
             {"esi": "combustion/reactingFoam/laminar/counterFlowFlame2D",
              "foundation": "multicomponentFluid/counterFlowFlame2D"}, (END("physical end time"),), "minutes"),
    Template("damBreak", "Dam break (2D laminar VoF)",
             "Free-surface water column collapsing around an obstacle; interFoam / incompressibleVoF.",
             frozenset({"multiphase", "vof", "transient", "laminar", "2d", "incompressible", "free-surface"}),
             {"esi": "multiphase/interFoam/laminar/damBreak/damBreak",
              "foundation": "incompressibleVoF/damBreakLaminar"}, (END("physical end time"),), "minutes",
             allrun=True, notes="setFields initialises the water column (Allrun)."),
    Template("flange", "Flange (snappyHexMesh meshing only)",
             "A mechanical part meshed with snappyHexMesh from an STL with feature edges; meshing tutorial.",
             frozenset({"meshing", "snappy", "stl", "3d"}),
             {"esi": "mesh/snappyHexMesh/flange", "foundation": "mesh/snappyHexMesh/flange"}, (), "minutes",
             allrun=True),
]

QUESTIONS = [
    ("regime", "Flow regime", [("incompressible", "Incompressible (Mach < 0.3)"),
                               ("compressible", "Compressible, subsonic"),
                               ("supersonic", "Supersonic / shocks")]),
    ("time", "Time", [("steady", "Steady state"), ("transient", "Transient")]),
    ("physics", "Extra physics", [("none", "None"), ("heat", "Heat transfer"), ("buoyancy", "Buoyancy"),
                                  ("cht", "Conjugate heat transfer (solid + fluid)"), ("multiphase", "Free surface"),
                                  ("reacting", "Combustion / chemistry")]),
    ("geometry", "Geometry", [("internal", "Internal flow (duct, pipe, channel)"),
                              ("external", "External flow around a body"),
                              ("stl", "My own STL/CAD geometry"), ("any", "Not sure")]),
]


def rank(answers: dict[str, str], family: str | None = None) -> list[tuple[Template, float, list[str]]]:
    """Templates ordered by fit to the answers (score, reasons). Templates that do not exist for the
    requested fork family are left out."""
    out = []
    for t in TEMPLATES:
        if family and family not in t.sources:
            continue
        score, why = 0.0, []
        reg = answers.get("regime")
        if reg and reg in t.tags:
            score += 3
            why.append(reg)
        elif reg == "compressible" and "supersonic" in t.tags:
            score += 1
        elif reg and ({"incompressible", "compressible", "supersonic"} & t.tags) - {reg}:
            score -= 2
        tm = answers.get("time")
        if tm and tm in t.tags:
            score += 2
            why.append(tm)
        ph = answers.get("physics")
        if ph and ph != "none":
            if ph in t.tags:
                score += 4
                why.append(ph)
            else:
                score -= 3
        elif ph == "none" and ({"cht", "multiphase", "reacting", "buoyancy"} & t.tags):
            score -= 2
        geo = answers.get("geometry")
        if geo and geo != "any":
            if geo in t.tags:
                score += 2
                why.append(geo)
            if geo == "stl" and "snappy" in t.tags:
                score += 2
                why.append("snappyHexMesh")
        if "meshing" in t.tags and geo != "stl":
            score -= 4
        out.append((t, score, why))
    out.sort(key=lambda x: -x[1])
    return out


# ============================================================================ value text editing
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def set_scalar_text(old: str, value: float | str) -> str:
    """Replace the LAST number in a value text, keeping dimension sets / 'uniform' / units."""
    nums = list(re.finditer(_NUM, old))
    if not nums:
        return str(value)
    m = nums[-1]
    return old[:m.start()] + f"{value}" + old[m.end():]


def set_vector_text(old: str, vec: list[float] | tuple[float, ...], *, only_x: bool = False) -> str:
    m = re.search(r"\(\s*(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")\s*\)", old)
    if m is None:
        return f"uniform ({' '.join(str(v) for v in vec)})"
    if only_x:
        return old[:m.start(1)] + f"{vec[0]}" + old[m.end(1):]
    return old[:m.start()] + "(" + " ".join(f"{v}" for v in vec) + ")" + old[m.end():]


def read_vector(text: str) -> list[float] | None:
    m = re.search(r"\(\s*(" + _NUM + r")\s+(" + _NUM + r")\s+(" + _NUM + r")\s*\)", text)
    return [float(m.group(i)) for i in (1, 2, 3)] if m else None


def read_scalar(text: str) -> float | None:
    nums = re.findall(_NUM, text)
    return float(nums[-1]) if nums else None


def template(tid: str) -> Template | None:
    return next((t for t in TEMPLATES if t.id == tid), None)


@dataclass
class ParamValue:
    param: Param
    file: str
    key: str
    current: str
    new: str = ""
    errors: list[str] = field(default_factory=list)
