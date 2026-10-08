"""snappyHexMesh setup from the geometry analysis: a runnable mesh case for either OpenFOAM fork.

``snappy_setup`` turns an STL plus its ``GeometryReport`` (and optional overrides) into the files
snappyHexMesh needs and the step list to run:

- ``system/blockMeshDict``: the background box. External flow gets a wind-tunnel domain around the body
  (upstream/downstream/lateral multiples of the body size) with inlet/outlet/sides patches. Internal flow
  gets the surface's bounding box plus one cell; snappy keeps the inside.
- feature edges: ``surfaceFeatureExtract`` (ESI, ``surfaceFeatureExtractDict``) or ``surfaceFeatures``
  (Foundation, ``surfaceFeaturesDict``), included angle = 180 - the suggested feature angle.
- ``system/snappyHexMeshDict``: refinement levels from the strategy (curvature/feature driven), feature
  refinement, layers (relative or absolute first-layer thickness from the y+ calculator) and inline mesh
  quality controls. Named STL solids become patches (inlet/outlet/... -> patch, others -> wall).
- the point kept by snappy (``locationInMesh`` / ``insidePoint``): far upstream for external flow; for
  internal flow a sample inside the closed surface, as far from it as possible.

Fork differences handled: surface keyword (``triSurfaceMesh``+name / ``triSurface``+file), surface folder
(``constant/triSurface`` / ``constant/geometry``), the feature utility, and ``locationInMesh``/``insidePoint``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.mesh.geometry import GeometryReport

_HEADER = """/*--------------------------------*- C++ -*----------------------------------*\\
  Written by AetherCFD Studio (snappyHexMesh setup from the geometry analysis)
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    location    "{loc}";
    object      {obj};
}}

"""
_PATCH_WORDS = re.compile(r"(inlet|outlet|inflow|outflow|^in$|^out$|^in_|^out_|farfield|atmosphere|symmetry)",
                          re.IGNORECASE)
AXES = {"x": 0, "y": 1, "z": 2}


@dataclass
class SnappySetup:
    files: dict[str, str]  # path relative to the case -> text
    copy_surface: tuple[str, str]  # (source STL, destination relative to the case)
    steps: list[tuple[str, list[str]]]
    summary: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def stl_solids(path: str | Path) -> list[str]:
    """Names of the solids of an ASCII STL (regions -> patches); [] for binary or single unnamed solids."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            head = fh.read(512)
            if not head.lstrip().lower().startswith(b"solid") or b"\0" in head:
                return []
            fh.seek(0)
            names = [ln.split(None, 1)[1].strip().decode("utf-8", "replace") if len(ln.split()) > 1 else ""
                     for ln in fh if ln.lstrip().lower().startswith(b"solid")]
    except OSError:
        return []
    names = [n for n in names if n]
    return names if len(names) > 1 else []


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _vec(v) -> str:
    return "(" + " ".join(_fmt(x) for x in v) + ")"


def _inside_point(stl: str | Path, bounds, h: float):
    """A point inside the closed surface, as far from it as a coarse sample grid finds (VTK)."""
    import vtk

    from aethercfd.mesh.geometry import read_surface

    surf = read_surface(stl)
    enc = vtk.vtkSelectEnclosedPoints()
    enc.Initialize(surf)
    dist = vtk.vtkImplicitPolyDataDistance()
    dist.SetInput(surf)
    best, best_d = None, -1.0
    n = 9
    for i in range(1, n):
        for j in range(1, n):
            for k in range(1, n):
                p = [bounds[2 * a] + (bounds[2 * a + 1] - bounds[2 * a]) * f / n
                     for a, f in zip(range(3), (i + 0.137, j + 0.211, k + 0.173), strict=True)]
                if enc.IsInsideSurface(p):
                    d = abs(dist.EvaluateFunction(p))
                    if d > best_d:
                        best, best_d = p, d
    enc.Complete()
    if best is None:
        return None, 0.0
    return best, best_d


def snappy_setup(stl: str | Path, geo: GeometryReport, *, fork_family: str = "esi", flow: str = "external",
                 flow_dir: str = "+x", background_cell: float | None = None,
                 surface_levels: tuple[int, int] | None = None, feature_level: int | None = None,
                 layers: dict | None = None, first_layer: float | None = None, n_layers: int = 5,
                 domain: tuple[float, float, float] = (3.0, 6.0, 3.0), name: str | None = None,
                 n_cells_between_levels: int = 3, max_global_cells: int = 20_000_000, target_cells: int = 2_000_000,
                 strategy_settings: dict | None = None) -> SnappySetup:
    """Files + steps for a snappyHexMesh case. ``domain`` = (upstream, downstream, lateral) multiples of the
    body length for external flow. The background cell follows from the domain volume and ``target_cells``
    (a quarter of the budget); the refinement levels then keep the strategy's finest surface cell (or the
    geometry's curvature radius / 4)."""
    if fork_family not in ("esi", "foundation"):
        raise ValueError("fork_family: esi | foundation")
    stl = Path(stl)
    name = name or re.sub(r"\W", "_", stl.stem) or "body"
    ext = stl.suffix.lower() if stl.suffix.lower() in (".stl", ".obj") else ".stl"
    fname = f"{name}{ext}"
    st = strategy_settings or {}
    b = list(geo.bounds)
    size = [b[1] - b[0], b[3] - b[2], b[5] - b[4]]
    L = max(size) or 1.0
    notes: list[str] = []
    # the finest surface cell the strategy asked for (its background cell / 2^level), kept when the background
    # cell is re-derived for this domain
    if st.get("background_cell") and st.get("surface_level"):
        s_target = st["background_cell"] / 2 ** st["surface_level"][0]
    else:
        s_target = (geo.min_radius / 4) if geo.min_radius else L / 100
    if background_cell:
        h = background_cell
    else:  # background cells take ~1/4 of the budget, over the real domain volume
        if flow == "external":
            up, down, lat = domain
            vol = size[AXES[flow_dir[-1]]] + (up + down) * L
            for a in range(3):
                if a != AXES[flow_dir[-1]]:
                    vol *= size[a] + 2 * lat * L
        else:
            vol = max(geo.volume or 0.0, 1e-30) if geo.volume else size[0] * size[1] * size[2]
        h = (vol / max(1.0, 0.25 * target_cells)) ** (1 / 3)
    if surface_levels:
        lv = tuple(surface_levels)
    else:
        l0 = max(0, min(8, round(math.log2(max(h / s_target, 1.0)))))
        lv = (l0, l0 + 1)
    fl = feature_level if feature_level is not None else lv[1]
    feat_angle = float(st.get("resolveFeatureAngle") or geo.feature_angle or 30.0)
    esi = fork_family == "esi"
    surf_dir = "constant/triSurface" if esi else "constant/geometry"

    # background box
    if flow == "external":
        sign = -1 if flow_dir.startswith("-") else 1
        ax = AXES[flow_dir[-1]]
        up, down, lat = domain
        lo, hi = [], []
        for a in range(3):
            if a == ax:
                lo.append(b[2 * a] - (up if sign > 0 else down) * L)
                hi.append(b[2 * a + 1] + (down if sign > 0 else up) * L)
            else:
                lo.append(b[2 * a] - lat * L)
                hi.append(b[2 * a + 1] + lat * L)
    else:
        lo = [b[2 * a] - h for a in range(3)]
        hi = [b[2 * a + 1] + h for a in range(3)]
    n = [max(1, math.ceil((hi[a] - lo[a]) / h)) for a in range(3)]
    hi = [lo[a] + n[a] * h for a in range(3)]  # cubic cells exactly
    bg_cells = n[0] * n[1] * n[2]
    if bg_cells > max_global_cells / 4:
        notes.append(f"background mesh alone has {bg_cells:,} cells; increase the background cell size")

    faces = {"x-": "(0 4 7 3)", "x+": "(1 2 6 5)", "y-": "(0 1 5 4)", "y+": "(3 7 6 2)", "z-": "(0 3 2 1)",
             "z+": "(4 5 6 7)"}
    if flow == "external":
        axn = flow_dir[-1]
        inlet = f"{axn}{'-' if not flow_dir.startswith('-') else '+'}"
        outlet = f"{axn}{'+' if not flow_dir.startswith('-') else '-'}"
        sides = [k for k in faces if k not in (inlet, outlet)]
        bpatches = [("inlet", "patch", [faces[inlet]]), ("outlet", "patch", [faces[outlet]]),
                    ("sides", "patch", [faces[k] for k in sides])]
    else:
        bpatches = [("background", "patch", list(faces.values()))]
    v = [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]), (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
         (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]), (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2])]
    bm = _HEADER.format(loc="system", obj="blockMeshDict") + "scale 1;\n\nvertices\n(\n" + "".join(
        f"    {_vec(p)}\n" for p in v) + ");\n\nblocks\n(\n" + (
        f"    hex (0 1 2 3 4 5 6 7) ({n[0]} {n[1]} {n[2]}) simpleGrading (1 1 1)\n);\n\nedges\n(\n);\n\n"
        "boundary\n(\n") + "".join(
        f"    {pn}\n    {{\n        type {pt};\n        faces\n        (\n" + "".join(
            f"            {f}\n" for f in fs) + "        );\n    }\n" for pn, pt, fs in bpatches) + ");\n"

    # point kept by snappy
    if flow == "external":
        sign = -1 if flow_dir.startswith("-") else 1
        ax = AXES[flow_dir[-1]]
        keep = [lo[a] + 1.37 * h if a != ax else (lo[a] + 1.37 * h if sign > 0 else hi[a] - 1.37 * h)
                for a in range(3)]
        keep[(ax + 1) % 3] = 0.5 * (lo[(ax + 1) % 3] + hi[(ax + 1) % 3]) + 0.137 * h
        keep[(ax + 2) % 3] = 0.5 * (lo[(ax + 2) % 3] + hi[(ax + 2) % 3]) + 0.211 * h
    else:
        keep, clearance = _inside_point(stl, b, h)
        if keep is None:
            raise ValueError("no point inside the surface was found: is it closed (watertight)? For external "
                             "flow use flow='external'")
        if clearance < h:
            notes.append(f"the kept point is only {clearance:.3g} from the surface (< one background cell); "
                         "check locationInMesh")

    # feature extraction
    included = round(180.0 - feat_angle)
    if esi:
        feat_file, feat_util = "system/surfaceFeatureExtractDict", "surfaceFeatureExtract"
        feat_txt = _HEADER.format(loc="system", obj="surfaceFeatureExtractDict") + (
            f"{fname}\n{{\n    extractionMethod    extractFromSurface;\n    includedAngle       {included};\n"
            "    subsetFeatures\n    {\n        nonManifoldEdges yes;\n        openEdges yes;\n    }\n"
            "    writeObj            no;\n}\n")
    else:
        feat_file, feat_util = "system/surfaceFeaturesDict", "surfaceFeatures"
        feat_txt = _HEADER.format(loc="system", obj="surfaceFeaturesDict") + (
            f"surfaces (\"{fname}\");\n\nincludedAngle   {included};\n")

    # surface regions -> patches
    solids = stl_solids(stl)
    regions = ""
    if solids:
        regions = "            regions\n            {\n" + "".join(
            f"                {s}\n                {{\n                    level ({lv[0]} {lv[1]});\n"
            f"                    patchInfo {{ type {'patch' if _PATCH_WORDS.search(s) else 'wall'}; }}\n"
            "                }\n" for s in solids) + "            }\n"
    surf_kw = (f"        type triSurfaceMesh;\n        name {name};\n" if esi
               else f"        type triSurface;\n        file \"{fname}\";\n")

    # layers
    lay = dict(layers or st.get("layers") or {})
    if first_layer:
        lay = {"firstLayerThickness": first_layer, "expansionRatio": lay.get("expansionRatio", 1.2),
               "relativeSizes": False}
    add_layers = bool(lay) and n_layers > 0
    rel = lay.get("relativeSizes", True)
    layer_patch = f'"{name}.*"' if not solids else '"' + "|".join(
        f"{name}_{s}" for s in solids if not _PATCH_WORDS.search(s)) + '"'
    thick = (f"    firstLayerThickness {_fmt(lay['firstLayerThickness'])};\n    minThickness {_fmt(0.3 * lay['firstLayerThickness'])};\n"
             if "firstLayerThickness" in lay else
             f"    finalLayerThickness {_fmt(lay.get('finalLayerThickness', 0.4))};\n    minThickness 0.1;\n")
    keep_kw = "locationInMesh" if esi else "insidePoint"
    snappy = _HEADER.format(loc="system", obj="snappyHexMeshDict") + f"""castellatedMesh true;
snap            true;
addLayers       {'true' if add_layers else 'false'};

geometry
{{
    {fname if esi else name}
    {{
{surf_kw}    }}
}};

castellatedMeshControls
{{
    maxLocalCells       {min(max_global_cells, 2_000_000)};
    maxGlobalCells      {max_global_cells};
    minRefinementCells  10;
    maxLoadUnbalance    0.10;
    nCellsBetweenLevels {n_cells_between_levels};

    features
    (
        {{
            file "{name}.eMesh";
            level {fl};
        }}
    );

    refinementSurfaces
    {{
        {name}
        {{
            level ({lv[0]} {lv[1]});
            patchInfo {{ type wall; }}
{regions}        }}
    }}

    resolveFeatureAngle {round(feat_angle)};

    refinementRegions
    {{
    }}

    {keep_kw} {_vec(keep)};

    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch    3;
    tolerance       2.0;
    nSolveIter      100;
    nRelaxIter      5;
    nFeatureSnapIter 10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes {'true' if rel and 'firstLayerThickness' not in lay else 'false'};
    layers
    {{
        {layer_patch}
        {{
            nSurfaceLayers {n_layers};
        }}
    }}
    expansionRatio {_fmt(lay.get('expansionRatio', 1.2))};
{thick}    nGrow 0;
    featureAngle 120;
    slipFeatureAngle 30;
    nRelaxIter 5;
    nSmoothSurfaceNormals 1;
    nSmoothNormals 3;
    nSmoothThickness 10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedialAxisAngle 90;
    nBufferCellsNoExtrude 0;
    nLayerIter 50;
}}

meshQualityControls
{{
    maxNonOrtho 65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave 80;
    minVol {'1e-13' if esi else '-1e30'};
    minTetQuality {'1e-15' if esi else '-1e30'};
    minArea -1;
    minTwist 0.02;
    minDeterminant 0.001;
    minFaceWeight 0.05;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
    relaxed
    {{
        maxNonOrtho 75;
    }}
}}

mergeTolerance 1e-6;
"""
    files = {"system/blockMeshDict": bm, feat_file: feat_txt, "system/snappyHexMeshDict": snappy}
    steps = [("blockMesh", []), (feat_util, []), ("snappyHexMesh", ["-overwrite"]), ("checkMesh", [])]
    # surface cells ~ area / (finest cell)^2, times the buffer layers of each refinement level
    h_s = h / 2 ** lv[1]
    est = bg_cells + geo.area / h_s ** 2 * (n_cells_between_levels + 1) + (
        geo.area / h_s ** 2 * n_layers if add_layers else 0)
    summary = {"fork_family": fork_family, "flow": flow, "background_cell": h, "background_cells": bg_cells,
               "domain_min": lo, "domain_max": hi, "surface_levels": list(lv), "feature_level": fl,
               "included_angle": included, keep_kw: keep, "layers": add_layers and {"n": n_layers, **lay},
               "patches_from_stl_solids": solids, "rough_cell_estimate": int(est),
               "surface_file": f"{surf_dir}/{fname}"}
    return SnappySetup(files, (str(stl), f"{surf_dir}/{fname}"), steps, summary, notes)


MINIMAL_SYSTEM = {
    "system/controlDict": "application snappyHexMesh;\nstartFrom startTime;\nstartTime 0;\nstopAt endTime;\n"
                          "endTime 0;\ndeltaT 1;\nwriteControl timeStep;\nwriteInterval 1;\nwriteFormat binary;\n"
                          "writePrecision 12;\nwriteCompression off;\ntimeFormat general;\ntimePrecision 6;\n"
                          "runTimeModifiable false;\n",
    "system/fvSchemes": "ddtSchemes { default steadyState; }\ngradSchemes { default Gauss linear; }\n"
                        "divSchemes { default none; }\nlaplacianSchemes { default Gauss linear corrected; }\n"
                        "interpolationSchemes { default linear; }\nsnGradSchemes { default corrected; }\n",
    "system/fvSolution": "solvers {}\n",
}


def minimal_system(fork_family: str) -> dict[str, str]:
    """controlDict/fvSchemes/fvSolution for a mesh-only case (only written where missing)."""
    banner = ("/*  OpenFOAM: The Open Source CFD Toolbox | Version:  2306 | Website:  www.openfoam.com  */\n"
              if fork_family == "esi" else
              "/*  OpenFOAM: The Open Source CFD Toolbox | Version:  14 | Website:  https://openfoam.org  */\n")
    return {k: banner + _HEADER.format(loc="system", obj=k.split("/")[-1]) + v for k, v in MINIMAL_SYSTEM.items()}
