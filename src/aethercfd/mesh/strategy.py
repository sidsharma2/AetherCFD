"""Meshing strategy advisor: which mesher and settings fit this geometry and question (no Qt here).

Rules from practice (see docs/research/automeshing_survey.md): structured blocking where the shape allows
it (extruded or axisymmetric shapes, simple ducts), wedge meshes for axisymmetric flow, snappyHexMesh for
complex watertight STLs, cfMesh (cartesianMesh, bundled with ESI v2306) when snappy layer coverage is poor,
a wrapping step for dirty CAD. Each recommendation carries the reasons and the first numbers to use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from aethercfd.mesh.geometry import GeometryReport


@dataclass
class MeshPlan:
    mesher: str
    score: float
    why: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)


def plan(geo: GeometryReport | None, *, flow: str = "external", first_cell: float | None = None,
         target_cells: int = 2_000_000, fork: str = "esi") -> list[MeshPlan]:
    out: list[MeshPlan] = []
    if geo is None:
        out.append(MeshPlan("blockMesh (structured blocks)", 3.0, ["no geometry file: build the domain from blocks"],
                            {"tip": "use the Grading designer to match cell sizes across blocks (ratio <= 1.2)"}))
        return out
    L = geo.char_length or 1.0
    box = [geo.bounds[1] - geo.bounds[0], geo.bounds[3] - geo.bounds[2], geo.bounds[5] - geo.bounds[4]]
    vol = max(1e-30, box[0] * box[1] * box[2]) * (8 if flow == "external" else 1)
    base = (vol / max(1, target_cells / 4)) ** (1 / 3)  # background cells take ~1/4 of the budget
    surface = geo.min_radius / 4 if geo.min_radius else L / 100
    level = max(0, min(8, round(math.log2(max(base / max(surface, 1e-30), 1.0)))))
    if geo.shape == "axisymmetric":
        out.append(MeshPlan("Wedge (axisymmetric 2-D) with blockMesh or extrudeMesh", 5.0,
                            [f"the body is axisymmetric about {geo.axis}: a 5° wedge captures the flow at ~1/100 the "
                             "cost of 3-D, if the flow itself is axisymmetric (no swirl asymmetry, no cross-flow)"],
                            {"wedge_angle_deg": 5, "axis": geo.axis},
                            ["swirl and 3-D instabilities are not represented; check with a 3-D case once"]))
    if geo.shape == "extruded":
        out.append(MeshPlan("2-D profile + extrudeMesh", 4.5,
                            [f"the geometry is an extruded profile along {geo.axis}: mesh the profile (blockMesh or "
                             "gmsh) and extrude 1 cell (2-D) or N cells (3-D, uniform spanwise)"],
                            {"extrude_axis": geo.axis}))
    out.append(MeshPlan("snappyHexMesh (castellated + snap + layers)", 3.5 + (1.0 if geo.watertight else -2.0),
                        ["works on any watertight STL; refinement follows curvature and features",
                         f"suggested feature angle {geo.feature_angle:.0f}° (from the dihedral-angle histogram)"],
                        {"background_cell": round(base, 6), "surface_level": [level, level + 1],
                         "resolveFeatureAngle": round(geo.feature_angle), "nCellsBetweenLevels": 3,
                         "featureEdgeLevel": level + 1,
                         "layers": {"nSurfaceLayers": 5, "expansionRatio": 1.2,
                                    "finalLayerThickness": 0.4, "relativeSizes": True}
                         if first_cell is None else {"firstLayerThickness": first_cell, "expansionRatio": 1.2,
                                                     "relativeSizes": False}},
                        ["layer coverage drops at sharp concave corners; check the 'layer' summary in the log"]
                        + ([] if geo.watertight else geo.notes)))
    if fork == "esi":
        out.append(MeshPlan("cfMesh cartesianMesh (bundled with v2306)", 3.0 + (0.5 if geo.watertight else -2.0),
                            ["hex-dominant with robust boundary layers; often better layer coverage than snappy on "
                             "complex parts", "one dictionary (meshDict): maxCellSize, surface refinement, layers"],
                            {"maxCellSize": round(base, 6), "boundaryCellSize": round(base / 2 ** level, 6)},
                            ["needs a feature-edge FMS/STL with patches named; less control over refinement regions"]))
    if not geo.watertight:
        out.append(MeshPlan("Surface wrapping first (shrink-wrap), then snappy/cfMesh", 4.0,
                            [f"the STL has {geo.open_edges} open and {geo.nonmanifold_edges} non-manifold edges",
                             "wrapping closes gaps smaller than the wrap resolution and gives a clean surface"],
                            {"wrap_resolution": round(surface, 6)}))
    out.sort(key=lambda p: -p.score)
    return out
