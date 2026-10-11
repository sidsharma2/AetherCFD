"""Visualization pipeline stages on the real finite-volume mesh (VTK only, no Qt).

Each stage takes the case's internal mesh(es) and returns render-ready vtkPolyData plus how to colour
it. Cell data stays cell data wherever the operation allows (slice, clip, threshold, quality): every
polygon shows its parent cell's value. Stages that need continuous fields (contour, streamlines,
quick schlieren) interpolate cell->point and say so ("interpolated") -- the viewport labels them.

Smoothing is the viewer's choice (``smooth`` in run_stage): smooth colours slices and clips with the values
interpolated to the mesh points and draws contours as smooth iso-lines/surfaces; cells (the default here)
keeps every cell's own value and draws contours as the stair-stepped faces between cells (results.sections).

Stage specs are plain dicts so they can be saved with layouts and sent across threads:
  {"id": "s1", "type": "slice", "axis": "z", "pos": 0.5}                      (pos: 0..1 of the bounds)
  {"type": "slice", "section": True, "stack": 5, "axis": "x", "contours": 8, "arrows": True, "edges": True}
  {"type": "slice", "origin": [x, y, z], "normal": [nx, ny, nz]}  or  {"type": "slice", "through": "<patch>"}
  {"type": "streamlines", "seed": "patches", "patches": ["patch/inlet"], "color_by": "residence_time",
   "tubes": True}                                                     (flow paths from inlets)
  {"type": "clip", "axis": "x", "pos": 0.5, "invert": False, "crinkle": False}
  {"type": "threshold", "field": "T", "lo": 1500, "hi": 2500}
  {"type": "contour", "field": "T", "values": [1500, 1800]}
  {"type": "streamlines", "field": "U", "seed": "line"|"plane"|"sphere", "n": 50, "axis": "x", "pos": 0.1}
  {"type": "glyphs", "field": "U", "max_points": 2000, "scale": 1.0, "on_slice": {"axis": "z", "pos": 0.5}}
  {"type": "schlieren", "source": "rho", "k": 12.0, "axis": "z", "pos": 0.5}
  {"type": "quality", "metric": "nonOrthogonality", "min": 60}                 (cells worse than min only)
View options (applied to every block): {"mirror": "y"} and {"revolve": {"axis": "z", "angle": 5, "copies": 72}}
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import vtk
from vtk.util import numpy_support as nps

CELLS = vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS
POINTS = vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS

STAGE_TYPES = ["slice", "clip", "threshold", "contour", "streamlines", "glyphs", "schlieren", "quality",
               "qcriterion", "isosurface", "pathlines"]
STAGE_FIELDS = {"threshold": "field", "contour": "field", "streamlines": "field", "glyphs": "field",
                "schlieren": "source", "isosurface": "field", "qcriterion": "field", "slice": "field",
                "clip": "field", "pathlines": "field"}
RESIDENCE = "IntegrationTime"  # the stream tracer's time along a path: residence time from the seed


@dataclass
class StageOutput:
    poly: vtk.vtkPolyData
    scalar: str | None = None  # array to colour by
    association: str = "cell"  # cell | point
    range: tuple[float, float] | None = None
    cmap: str | None = None  # stage-specific colour map (e.g. gray for schlieren)
    interpolated: bool = False
    label: str = ""
    notes: list[str] = field(default_factory=list)
    # extra solid-coloured geometry drawn with the stage: "lines" (contours on a section), "arrows"
    overlays: dict[str, vtk.vtkPolyData] = field(default_factory=dict)
    edges: bool = False  # draw the cut cells' edges (the mesh) on this stage
    title: str | None = None  # colour-bar title when it is not the array name (e.g. residence time [s])


# ============================================================================ helpers
def bounds_of(datasets: list) -> list[float]:
    b = [math.inf, -math.inf] * 3
    for ds in datasets:
        db = ds.GetBounds()
        for k in range(3):
            b[2 * k] = min(b[2 * k], db[2 * k])
            b[2 * k + 1] = max(b[2 * k + 1], db[2 * k + 1])
    return b


def plane_from(spec: dict, bounds: list[float]) -> vtk.vtkPlane:
    ax = "xyz".index(str(spec.get("axis", "z")).lower()[0])
    origin = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2]
    if "origin" in spec:
        origin = list(spec["origin"])
    else:
        frac = float(spec.get("pos", 0.5))
        origin[ax] = bounds[2 * ax] + frac * (bounds[2 * ax + 1] - bounds[2 * ax])
    normal = list(spec.get("normal") or [1.0 if i == ax else 0.0 for i in range(3)])
    p = vtk.vtkPlane()
    p.SetOrigin(*origin)
    p.SetNormal(*normal)
    return p


def to_poly(obj) -> vtk.vtkPolyData:
    """Any dataset or composite -> one polydata (outer surface for volumes), cell data preserved."""
    if obj is None:
        return vtk.vtkPolyData()
    if obj.IsA("vtkCompositeDataSet"):
        app = vtk.vtkAppendPolyData()
        it = obj.NewIterator()
        it.InitTraversal()
        n = 0
        while not it.IsDoneWithTraversal():
            d = it.GetCurrentDataObject()
            if d is not None and d.GetNumberOfCells():
                app.AddInputData(to_poly(d))
                n += 1
            it.GoToNextItem()
        if n == 0:
            return vtk.vtkPolyData()
        app.Update()
        out = vtk.vtkPolyData()
        out.ShallowCopy(app.GetOutput())
        return out
    if obj.IsA("vtkPolyData"):
        return obj
    return surface(obj)


def surface(ds) -> vtk.vtkPolyData:
    """Outer surface of a volume dataset, cell data preserved and every face wound outward (see
    orient_outward); a polydata is returned as it is."""
    if ds.IsA("vtkPolyData"):
        return ds
    g = vtk.vtkGeometryFilter()
    g.SetInputData(ds)
    g.PassThroughCellIdsOn()
    g.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(g.GetOutput())
    ids = out.GetCellData().GetArray("vtkOriginalCellIds")
    if ids is not None:
        if ds.IsA("vtkUnstructuredGrid"):
            try:
                orient_outward(out, ds, nps.vtk_to_numpy(ids))
            except Exception:  # presentation only: never fail a stage over it
                import logging

                logging.getLogger(__name__).exception("orienting the surface faces")
        out.GetCellData().RemoveArray("vtkOriginalCellIds")
    return out


def orient_outward(poly: vtk.vtkPolyData, ds, src: np.ndarray) -> int:
    """Wind every boundary face of ``poly`` (the surface of unstructured grid ``ds``; ``src``: each face's cell
    in ``ds``) so it points away from its own cell. VTK's OpenFOAM reader hands prism (wedge) cells over inside
    out, so their wall faces come out wound inward; the GPU then lights them from behind, and a curved wall
    meshed with prism layers shows dark moiré streaks. Returns the number of faces flipped."""
    polys = poly.GetPolys()
    if poly.GetNumberOfCells() != polys.GetNumberOfCells() or not polys.GetNumberOfCells() or not ds.GetNumberOfCells():
        return 0  # (vertices or lines mixed in: leave it)
    off = nps.vtk_to_numpy(polys.GetOffsetsArray()).astype(np.int64)
    conn = nps.vtk_to_numpy(polys.GetConnectivityArray()).astype(np.int64)
    pts = nps.vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    starts, sizes = off[:-1], np.diff(off)
    face = np.repeat(np.arange(len(sizes)), sizes)
    pos = np.arange(len(conn)) - starts[face]
    nxt = conn[starts[face] + (pos + 1) % sizes[face]]
    normal = np.add.reduceat(np.cross(pts[conn], pts[nxt]), starts, axis=0)  # Newell: area-weighted normal
    centre = np.add.reduceat(pts[conn], starts, axis=0) / sizes[:, None]
    cells = ds.GetCells()
    coff = nps.vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
    cconn = nps.vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
    vpts = nps.vtk_to_numpy(ds.GetPoints().GetData()).astype(np.float64)
    nonempty = np.diff(coff) > 0
    cc = np.zeros((len(coff) - 1, 3))
    cc[nonempty] = np.add.reduceat(vpts[cconn], coff[:-1][nonempty], axis=0) / np.diff(coff)[nonempty, None]
    inward = np.einsum("ij,ij->i", normal, centre - cc[src]) < 0
    if not inward.any():
        return 0
    flip = inward[face]
    idx = np.arange(len(conn))
    idx[flip] = (starts[face] + sizes[face] - 1 - pos)[flip]  # reverse the point order of those faces
    new = vtk.vtkCellArray()
    new.SetData(nps.numpy_to_vtk(off, deep=True, array_type=vtk.VTK_ID_TYPE),
                nps.numpy_to_vtk(conn[idx], deep=True, array_type=vtk.VTK_ID_TYPE))
    poly.SetPolys(new)
    return int(inward.sum())


def _append(polys: list) -> vtk.vtkPolyData:
    polys = [p for p in polys if p is not None and p.GetNumberOfCells()]
    if not polys:
        return vtk.vtkPolyData()
    if len(polys) == 1:
        return polys[0]
    app = vtk.vtkAppendPolyData()
    for p in polys:
        app.AddInputData(p)
    app.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(app.GetOutput())
    return out



def scalar_array(ds, name: str, comp: str = "magnitude", association: str = "cell") -> str | None:
    """Ensure a 1-component array for (name, component) exists on ds; return its name."""
    data = ds.GetCellData() if association == "cell" else ds.GetPointData()
    arr = data.GetArray(name)
    if arr is None:
        return None
    if arr.GetNumberOfComponents() == 1:
        return name
    out_name = f"{name}_{'mag' if comp == 'magnitude' else comp}"
    if data.GetArray(out_name) is not None:
        return out_name
    a = nps.vtk_to_numpy(arr)
    v = np.linalg.norm(a, axis=1) if comp == "magnitude" else a[:, "xyz".index(comp)]
    o = nps.numpy_to_vtk(np.ascontiguousarray(v, dtype=np.float64), deep=True)
    o.SetName(out_name)
    data.AddArray(o)
    return out_name


def _range(poly, name: str | None, association: str) -> tuple[float, float] | None:
    if not name:
        return None
    data = poly.GetCellData() if association == "cell" else poly.GetPointData()
    a = data.GetArray(name)
    if a is None or a.GetNumberOfTuples() == 0:
        return None
    lo, hi = a.GetRange()
    return (lo, hi) if lo <= hi else None


def cell_to_point(ds):
    f = vtk.vtkCellDataToPointData()
    f.SetInputData(ds)
    f.PassCellDataOn()
    f.Update()
    out = f.GetOutput().NewInstance()
    out.ShallowCopy(f.GetOutput())
    return out


def has_points(polys, name: str | None) -> bool:
    """Does every non-empty dataset carry ``name`` as point data (the reader's cell-to-point values)?"""
    polys = [p for p in polys if p is not None and p.GetNumberOfCells()]
    return bool(name and polys and all(p.GetPointData().GetArray(name) is not None for p in polys))


def colour_array(poly, name: str | None, comp: str, smooth: bool) -> tuple[str | None, str]:
    """(array, association) to colour a cut surface by: interpolated point values when smoothing (and the
    reader made them), else the cells' own values."""
    if not name:
        return None, "cell"
    if smooth and poly.GetPointData().GetArray(name) is not None:
        return scalar_array(poly, name, comp, "point"), "point"
    return scalar_array(poly, name, comp), "cell"


def split_by_field(polys: list, name: str | None) -> tuple[vtk.vtkPolyData, vtk.vtkPolyData]:
    """(pieces that carry the cell array ``name``, the others), each appended. A multi-region cut through a
    fluid (with U) and solids (without): appended together the U array would be dropped (appending keeps
    only the arrays every piece has), so the solids' part is kept apart and drawn as 'no data'."""
    polys = [p for p in polys if p is not None and p.GetNumberOfCells()]
    if not name:
        return _append(polys), vtk.vtkPolyData()
    has = [p for p in polys if p.GetCellData().GetArray(name) is not None]
    if not has:  # nobody has it: keep the geometry, nothing to colour by
        return _append(polys), vtk.vtkPolyData()
    return _append(has), _append([p for p in polys if p.GetCellData().GetArray(name) is None])


def _cut(internal, plane, name: str | None = None) -> tuple[vtk.vtkPolyData, vtk.vtkPolyData]:
    polys = []
    for ds in internal:
        c = vtk.vtkPlaneCutter()
        c.SetInputData(ds)
        c.SetPlane(plane)
        c.InterpolateAttributesOn()
        c.Update()
        polys.append(to_poly(c.GetOutput()))
    return split_by_field(polys, name)


def section_planes(spec, bounds, patches: dict | None = None) -> tuple[list[vtk.vtkPlane], list[str]]:
    """The plane(s) of a slice spec: one plane (axis/pos, origin/normal, through a patch's centre) or a
    stack of n parallel planes along the axis."""
    from aethercfd.results import sections as sx

    notes = []
    if spec.get("stack"):
        base = {k: v for k, v in spec.items() if k != "origin"}
        return [plane_from({**base, "pos": f}, bounds) for f in sx.stack_positions(int(spec["stack"]))], notes
    spec = dict(spec)
    if spec.get("through"):
        hit = [p for k, p in (patches or {}).items() if k == spec["through"] or k.split("/")[-1] == spec["through"]]
        centre = sx.centre_of(hit)
        if centre is None:
            notes.append(f"patch {spec['through']!r} not found: the plane goes through the domain centre")
        else:
            spec["origin"] = centre
    if spec.get("origin") is None:
        spec.pop("origin", None)
    return [plane_from(spec, bounds)], notes


def section_overlays(spec, poly, name, assoc, internal_vec, normal) -> tuple[dict, list[str]]:
    """Contour lines and in-plane arrows drawn on a section (solid colours, over the field)."""
    from aethercfd.results import sections as sx

    out, notes = {}, []
    nlev = spec.get("contours")
    if nlev and name and poly.GetNumberOfCells():
        levels = list(nlev) if isinstance(nlev, list | tuple) else None
        data = poly.GetPointData() if assoc == "point" else poly.GetCellData()
        lo, hi = data.GetArray(name).GetRange()
        levels = levels or sx.auto_levels(lo, hi, int(nlev) if not isinstance(nlev, bool) else 8)
        if assoc == "point":  # smooth: the iso-lines of the interpolated field
            c = vtk.vtkContourFilter()
            c.SetInputData(poly)
            c.SetInputArrayToProcess(0, 0, 0, POINTS, name)
            for i, v in enumerate(levels):
                c.SetValue(i, float(v))
            c.Update()
            lines = to_poly(c.GetOutput())
        else:  # cells: the edges between cut cells on either side of each level
            lines = sx.staircase(poly, name, levels)
        out["lines"] = lines
    if spec.get("arrows") and poly.GetNumberOfCells():
        vec = spec.get("vector") or internal_vec
        arr = poly.GetCellData().GetArray(vec)
        if arr is None or arr.GetNumberOfComponents() != 3:
            notes.append(f"arrows: {vec!r} is not loaded")
        else:
            cc = vtk.vtkCellCenters()
            cc.SetInputData(poly)
            cc.VertexCellsOn()
            cc.Update()
            m = vtk.vtkMaskPoints()
            m.SetInputConnection(cc.GetOutputPort())
            m.SetMaximumNumberOfPoints(int(spec.get("max_arrows", 900)))
            m.RandomModeOn()
            m.SetRandomModeType(2)  # spatially stratified: evenly spread, not clumped where cells are small
            m.GenerateVerticesOn()
            m.Update()
            pts = to_poly(m.GetOutput())
            a = np.nan_to_num(nps.vtk_to_numpy(pts.GetPointData().GetArray(vec)).astype(np.float64))
            n = np.asarray(normal, dtype=np.float64)
            n = n / (np.linalg.norm(n) or 1.0)
            inplane = a - np.outer(a @ n, n)  # what the section shows: the velocity in its plane
            v = nps.numpy_to_vtk(np.ascontiguousarray(inplane), deep=True)
            v.SetName("_inplane")
            pts.GetPointData().AddArray(v)
            b = poly.GetBounds()
            diag = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5]))
            mag = np.linalg.norm(inplane, axis=1)
            spacing = diag / max(8.0, math.sqrt(max(pts.GetNumberOfPoints(), 1)))
            g = vtk.vtkGlyph3D()
            g.SetInputData(pts)
            arrow = vtk.vtkArrowSource()
            g.SetSourceConnection(arrow.GetOutputPort())
            g.SetInputArrayToProcess(1, 0, 0, POINTS, "_inplane")
            g.SetVectorModeToUseVector()
            g.SetScaleModeToScaleByVector()
            g.SetScaleFactor(float(spec.get("arrow_scale", 1.0)) * 0.9 * spacing / (mag.max() or 1.0))
            g.OrientOn()
            g.Update()
            out["arrows"] = to_poly(g.GetOutput())
            notes.append(f"arrows: in-plane {vec}")
    return out, notes


# ============================================================================ stages
def stage_slice(spec, internal, field_name, comp, *, smooth: bool = False, patches: dict | None = None) \
        -> StageOutput:
    fname = spec.get("field") or field_name
    planes, notes = section_planes(spec, bounds_of(internal), patches)
    polys, lines, arrows, nodata = [], [], [], []
    vec = "U"
    for plane in planes:  # contours and arrows per plane: a stack's planes never connect to each other
        poly, rest = _cut(internal, plane, fname)
        name, assoc = colour_array(poly, fname, spec.get("component", comp), smooth)
        ov, ov_notes = section_overlays(spec, poly, name, assoc, vec, plane.GetNormal())
        polys.append(poly)
        nodata.append(rest)
        lines.append(ov.get("lines"))
        arrows.append(ov.get("arrows"))
        notes += [n for n in ov_notes if n not in notes]
    poly = _append(polys)
    name, assoc = colour_array(poly, fname, spec.get("component", comp), smooth)
    overlays = {k: _append(v) for k, v in (("lines", lines), ("arrows", arrows), ("nodata", nodata))
                if any(x is not None and x.GetNumberOfCells() for x in v)}
    if "nodata" in overlays:
        notes.append(f"grey: regions without {fname}")
    n = len(planes)
    label = f"section{'s' if n > 1 else ''}" + (f" ×{n}" if n > 1 else "") if spec.get("section") else "slice"
    if spec.get("contours"):
        notes.append("contours: " + ("smooth iso-lines" if assoc == "point" else "per cell (the mesh's steps)"))
    return StageOutput(poly, name, assoc, _range(poly, name, assoc), interpolated=assoc == "point",
                       label=label + (" (smoothed)" if assoc == "point" else ""), notes=notes, overlays=overlays,
                       edges=bool(spec.get("edges")))


def stage_clip(spec, internal, field_name, comp, *, smooth: bool = False) -> StageOutput:
    plane = plane_from(spec, bounds_of(internal))
    if spec.get("invert"):
        n = plane.GetNormal()
        plane.SetNormal(-n[0], -n[1], -n[2])
    polys = []
    for ds in internal:
        if spec.get("crinkle"):  # whole cells (shows the real cells cut by the plane)
            f = vtk.vtkExtractGeometry()
            f.SetImplicitFunction(plane)
            f.ExtractInsideOn()
            f.ExtractBoundaryCellsOn()
        else:
            f = vtk.vtkTableBasedClipDataSet()
            f.SetClipFunction(plane)
            f.InsideOutOn()
        f.SetInputData(ds)
        f.Update()
        polys.append(to_poly(f.GetOutput()))
    fname = spec.get("field") or field_name
    poly, rest = split_by_field(polys, fname)
    name, assoc = colour_array(poly, fname, comp, smooth and not spec.get("crinkle"))
    label = "cut-away" if spec.get("cutaway") else "clip"
    return StageOutput(poly, name, assoc, _range(poly, name, assoc), interpolated=assoc == "point",
                       label=f"{label} (whole cells)" if spec.get("crinkle") else
                       label + (" (smoothed)" if assoc == "point" else ""), edges=bool(spec.get("edges")),
                       overlays={"nodata": rest} if rest.GetNumberOfCells() else {},
                       notes=[f"grey: regions without {fname}"] if rest.GetNumberOfCells() else [])


def stage_threshold(spec, internal, field_name, comp) -> StageOutput:
    fname = spec.get("field") or field_name
    polys, used = [], None
    for ds in internal:
        name = scalar_array(ds, fname, spec.get("component", comp))
        if name is None:
            continue
        used = name
        t = vtk.vtkThreshold()
        t.SetInputData(ds)
        t.SetInputArrayToProcess(0, 0, 0, CELLS, name)
        lo = spec.get("lo")
        hi = spec.get("hi")
        a = ds.GetCellData().GetArray(name).GetRange()
        t.SetLowerThreshold(float(a[0] if lo is None else lo))
        t.SetUpperThreshold(float(a[1] if hi is None else hi))
        t.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
        t.Update()
        polys.append(to_poly(t.GetOutput()))
    poly = _append(polys)
    out = StageOutput(poly, used, "cell", _range(poly, used, "cell"), label=f"threshold {fname}")
    if used is None:
        out.notes.append(f"field {fname!r} is not loaded")
    return out


def _levels(spec, internal, name) -> list[float]:
    vals = spec.get("values") or []
    if vals:
        return [float(v) for v in vals]
    rng = [ds.GetCellData().GetArray(name).GetRange() for ds in internal if ds.GetCellData().GetArray(name)]
    if not rng:
        return []
    lo, hi = min(r[0] for r in rng), max(r[1] for r in rng)
    return list(np.linspace(lo, hi, int(spec.get("n", 5)) + 2)[1:-1])


def _pointed(ds, name: str):
    """``ds`` with ``name`` as point data: the reader's (smooth mode reads it) or averaged from the cells."""
    if ds.GetPointData().GetArray(name) is not None:
        return ds
    return cell_to_point(ds)


def stage_contour(spec, internal, field_name, comp, *, smooth: bool = True) -> StageOutput:
    """Smooth: iso-surfaces of the field interpolated to the mesh points. Cells: the faces between cells on
    either side of each value -- a stair-stepped surface that shows the mesh resolution, nothing interpolated."""
    from aethercfd.results import sections as sx

    fname = spec.get("field") or field_name
    names = [scalar_array(ds, fname, spec.get("component", comp)) if fname else None for ds in internal]
    used = next((n for n in names if n), None)
    vals = _levels(spec, internal, used) if used else []
    polys = []
    for ds, name in zip(internal, names, strict=True):
        if name is None:
            continue
        if not smooth:
            polys.append(sx.staircase(ds, name, vals))
            continue
        pd = _pointed(ds, fname)
        if pd.GetPointData().GetArray(name) is None:  # a vector component: point values of that component
            scalar_array(pd, fname, spec.get("component", comp), "point")
        c = vtk.vtkContourFilter()
        c.SetInputData(pd)
        c.SetInputArrayToProcess(0, 0, 0, POINTS, name)
        for i, v in enumerate(vals):
            c.SetValue(i, float(v))
        c.ComputeScalarsOn()
        c.Update()
        polys.append(to_poly(c.GetOutput()))
    poly = _append(polys)
    color = (spec.get("color_by") or used) if smooth else used  # a stepped surface carries its level only
    assoc = "point"
    if color and poly.GetPointData().GetArray(color) is None and poly.GetCellData().GetArray(color) is not None:
        assoc = "cell"
    notes = [] if smooth else ["per cell: the faces between cells on either side of each value (the mesh's steps)"]
    sm, sub = int(spec.get("smooth", 0) or 0), int(spec.get("subdivide", 0) or 0)
    if (sm or sub) and smooth:
        poly = present_smooth(poly, sm, sub)
        notes.append(f"smoothed for presentation ({sm} iterations, {sub} subdivisions): not the raw data")
    out = StageOutput(poly, color, assoc, _range(poly, color, assoc), interpolated=smooth,
                      label=f"contour {fname}" + ("" if smooth else " (per cell)"), notes=notes)
    if used is None:
        out.notes.append(f"field {fname!r} is not loaded")
    return out


def _seeds(spec, bounds) -> vtk.vtkAlgorithm:
    n = int(spec.get("n", 50))
    c = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2]
    ext = [bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]]
    kind = spec.get("seed", "line")
    ax = "xyz".index(str(spec.get("axis", "x")).lower()[0])
    pos = bounds[2 * ax] + float(spec.get("pos", 0.05)) * ext[ax]
    if kind == "line":  # across the longest of the two other directions, at 'pos' along 'axis'
        others = [i for i in range(3) if i != ax]
        o = max(others, key=lambda i: ext[i])
        p1, p2 = list(c), list(c)
        p1[ax] = p2[ax] = pos
        p1[o], p2[o] = bounds[2 * o] + 0.02 * ext[o], bounds[2 * o + 1] - 0.02 * ext[o]
        if "p1" in spec:
            p1, p2 = list(spec["p1"]), list(spec["p2"])
        s = vtk.vtkLineSource()
        s.SetPoint1(*p1)
        s.SetPoint2(*p2)
        s.SetResolution(max(1, n - 1))
        return s
    if kind == "plane":  # a grid of seeds on the plane normal to 'axis' at 'pos'
        u, w = [i for i in range(3) if i != ax]
        o0 = list(c)
        o0[ax] = pos
        o0[u] = bounds[2 * u] + 0.02 * ext[u]
        o0[w] = bounds[2 * w] + 0.02 * ext[w]
        p1, p2 = list(o0), list(o0)
        p1[u] = bounds[2 * u + 1] - 0.02 * ext[u]
        p2[w] = bounds[2 * w + 1] - 0.02 * ext[w]
        s = vtk.vtkPlaneSource()
        s.SetOrigin(*o0)
        s.SetPoint1(*p1)
        s.SetPoint2(*p2)
        r = max(1, round(math.sqrt(n)))
        s.SetResolution(r, r)
        return s
    s = vtk.vtkPointSource()
    s.SetCenter(*(spec.get("center") or c))
    s.SetRadius(float(spec.get("radius", (0.03 if kind == "point" else 0.1) * max(ext))))
    s.SetNumberOfPoints(n)
    return s


class _Static:
    """A ready seed polydata posing as an algorithm (patch seeds are computed, not a VTK source)."""

    def __init__(self, poly):
        self.poly = poly

    def Update(self):
        pass

    def GetOutput(self):
        return self.poly


def flow_seeds(spec, bounds, patches: dict | None = None):
    """The seeds of a streamline / pathline stage: on patches (inlets), a line, a plane or a ball."""
    from aethercfd.results import sections as sx

    if spec.get("seed") == "patches":
        want = spec.get("patches") or []
        polys = [p for k, p in (patches or {}).items() if k in want or k.split("/")[-1] in want]
        return _Static(sx.patch_seeds(polys, int(spec.get("n", 150))))
    return _seeds(spec, bounds)


def stage_streamlines(spec, internal, field_name, comp, *, patches: dict | None = None) -> StageOutput:
    """Streamlines of the velocity interpolated to the mesh points (always: a path needs a continuous field).
    Seeded on the inlet patches they run downstream only; 'IntegrationTime' along them is the residence time
    since the seed (an estimate: no turbulent dispersion)."""
    from aethercfd.results import sections as sx

    vec = spec.get("field") or "U"
    b = bounds_of(internal)
    diag = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5]))
    polys = []
    seeds = flow_seeds(spec, b, patches)  # keep the source alive while the tracer runs (a temporary crashes VTK)
    seeds.Update()
    from_patches = spec.get("seed") == "patches"
    for ds in internal:
        if ds.GetCellData().GetArray(vec) is None:
            continue
        pd = _pointed(ds, vec)
        st = vtk.vtkStreamTracer()
        st.SetInputData(pd)
        st.SetSourceData(seeds.GetOutput())
        st.SetInputArrayToProcess(0, 0, 0, POINTS, vec)
        st.SetMaximumPropagation(float(spec.get("max_length", 3.0)) * diag)
        if from_patches:  # from an inlet, downstream only
            st.SetIntegrationDirectionToForward()
        else:
            st.SetIntegrationDirectionToBoth()
        st.SetIntegratorTypeToRungeKutta45()
        st.SetInitialIntegrationStep(0.2)
        st.SetMaximumNumberOfSteps(int(spec.get("max_steps", 4000)))
        st.Update()
        polys.append(to_poly(st.GetOutput()))
    poly = _append(polys)
    notes = []
    color = spec.get("color_by") or vec
    title = None
    if color in ("residence_time", "time", RESIDENCE):
        name = RESIDENCE if poly.GetPointData().GetArray(RESIDENCE) is not None else None
        title = "residence time [s]"
        if name:
            notes.append("residence time: time along each path from its seed (integration time; an estimate, "
                         "no turbulent dispersion)")
    else:
        name = scalar_array(poly, color, "magnitude", "point") if poly.GetNumberOfCells() else None
    if name is None and poly.GetNumberOfCells():
        notes.append(f"{color!r} is not on the paths: coloured by |{vec}|")
        name = scalar_array(poly, vec, "magnitude", "point")
    rng = _range(poly, name, "point")
    if spec.get("tubes") and poly.GetNumberOfLines():
        poly = sx.tubes(poly, float(spec.get("tube_radius", 0.0025)) * diag)
    out = StageOutput(poly, name, "point", rng, interpolated=True, title=title, notes=notes,
                      label=("flow paths" if from_patches else "streamlines") + f" {vec}")
    if from_patches and not seeds.GetOutput().GetNumberOfPoints():
        out.notes.append("no inlet patch geometry to seed from")
    elif not poly.GetNumberOfCells():
        out.notes.append(f"no streamlines: is {vec!r} loaded and are the seeds inside the domain?")
    return out


def stage_glyphs(spec, internal, field_name, comp) -> StageOutput:
    vec = spec.get("field") or "U"
    src_sets = internal
    if spec.get("on_slice"):
        src_sets = [stage_slice(spec["on_slice"], internal, None, comp).poly]
    pts_polys = []
    for ds in src_sets:
        if ds.GetCellData().GetArray(vec) is None:
            continue
        cc = vtk.vtkCellCenters()
        cc.SetInputData(ds)
        cc.VertexCellsOn()
        cc.Update()
        m = vtk.vtkMaskPoints()
        m.SetInputConnection(cc.GetOutputPort())
        m.SetMaximumNumberOfPoints(int(spec.get("max_points", 2000)))
        m.RandomModeOn()
        m.SetRandomModeType(1)
        m.GenerateVerticesOn()
        m.Update()
        pts_polys.append(to_poly(m.GetOutput()))
    pts = _append(pts_polys)
    if not pts.GetNumberOfPoints():
        return StageOutput(vtk.vtkPolyData(), notes=[f"field {vec!r} is not loaded"], label="glyphs")
    a = nps.vtk_to_numpy(pts.GetPointData().GetArray(vec))
    mag = np.linalg.norm(a, axis=1)
    b = bounds_of(internal)
    diag = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5]))
    scale = float(spec.get("scale", 1.0)) * 0.05 * diag / (mag.max() or 1.0)
    arrow = vtk.vtkArrowSource()
    g = vtk.vtkGlyph3D()
    g.SetInputData(pts)
    g.SetSourceConnection(arrow.GetOutputPort())
    g.SetInputArrayToProcess(1, 0, 0, POINTS, vec)
    g.SetVectorModeToUseVector()
    g.SetScaleModeToScaleByVector()
    g.SetScaleFactor(scale)
    g.OrientOn()
    g.Update()
    poly = to_poly(g.GetOutput())
    name = scalar_array(poly, vec, "magnitude", "point")
    return StageOutput(poly, name, "point", _range(poly, name, "point"), label=f"glyphs {vec}")


def stage_schlieren(spec, internal, field_name, comp) -> StageOutput:
    """Numerical schlieren S = exp(-k |grad rho| / max |grad rho|) on a slice. Exact when the case has
    OpenFOAM's own grad(rho) field (postProcess -func 'grad(rho)'); otherwise VTK's cell gradient
    (labelled interpolated)."""
    src = spec.get("source", "rho")
    k = float(spec.get("k", 12.0))
    exact_name = f"grad({src})"
    sl_polys, interpolated = [], False
    for ds in internal:
        cd = ds.GetCellData()
        if cd.GetArray(exact_name) is not None:
            g = nps.vtk_to_numpy(cd.GetArray(exact_name))
        elif cd.GetArray(src) is not None:
            gf = vtk.vtkGradientFilter()
            gf.SetInputData(ds)
            gf.SetInputArrayToProcess(0, 0, 0, CELLS, src)
            gf.SetResultArrayName("_grad")
            gf.Update()
            g = nps.vtk_to_numpy(gf.GetOutput().GetCellData().GetArray("_grad"))
            interpolated = True
        else:
            continue
        mag = np.linalg.norm(g, axis=1)
        work = ds.NewInstance()
        work.ShallowCopy(ds)
        arr = nps.numpy_to_vtk(np.ascontiguousarray(mag, dtype=np.float64), deep=True)
        arr.SetName("gradMag")
        work.GetCellData().AddArray(arr)
        sl_polys.append(work)
    if not sl_polys:
        return StageOutput(vtk.vtkPolyData(), notes=[f"neither {exact_name} nor {src} is loaded"], label="schlieren")
    gmax = max(float(np.nanmax(nps.vtk_to_numpy(w.GetCellData().GetArray("gradMag"))) or 1.0) for w in sl_polys)
    for w in sl_polys:
        m = nps.vtk_to_numpy(w.GetCellData().GetArray("gradMag"))
        s = nps.numpy_to_vtk(np.ascontiguousarray(np.exp(-k * m / (gmax or 1.0)), dtype=np.float64), deep=True)
        s.SetName("schlieren")
        w.GetCellData().AddArray(s)
    poly = stage_slice(spec, sl_polys, None, "magnitude").poly if spec.get("axis") else _append(
        [to_poly(w) for w in sl_polys])
    return StageOutput(poly, "schlieren", "cell", (0.0, 1.0), cmap="gray", interpolated=interpolated,
                       label=f"schlieren ({'exact grad' if not interpolated else 'VTK gradient'} of {src})")


_QUALITY_CACHE: dict[tuple[str, float], dict[str, np.ndarray]] = {}


def quality_fields(mesh_dir: Path) -> dict[str, np.ndarray]:
    from aethercfd.mesh.polymesh import read_polymesh
    from aethercfd.mesh.quality import quality

    pts = mesh_dir / "points" if (mesh_dir / "points").exists() else mesh_dir / "points.gz"
    key = (str(mesh_dir), pts.stat().st_mtime)
    if key not in _QUALITY_CACHE:
        _QUALITY_CACHE.clear()
        _QUALITY_CACHE[key] = quality(read_polymesh(mesh_dir)).cell_fields()
    return _QUALITY_CACHE[key]


def stage_quality(spec, internal, field_name, comp, *, mesh_dirs: list[Path]) -> StageOutput:
    metric = spec.get("metric", "nonOrthogonality")
    polys, notes = [], []
    for ds, md in zip(internal, mesh_dirs, strict=False):
        try:
            q = quality_fields(md)
        except Exception as exc:
            notes.append(f"quality of {md}: {exc}")
            continue
        vals = q.get(metric)
        if vals is None or len(vals) != ds.GetNumberOfCells():
            notes.append(f"{metric}: {0 if vals is None else len(vals)} values for {ds.GetNumberOfCells()} cells "
                         "(decomposed polyhedra?)")
            continue
        work = ds.NewInstance()
        work.ShallowCopy(ds)
        arr = nps.numpy_to_vtk(np.ascontiguousarray(vals, dtype=np.float64), deep=True)
        arr.SetName(metric)
        work.GetCellData().AddArray(arr)
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None or hi is not None:
            t = vtk.vtkThreshold()
            t.SetInputData(work)
            t.SetInputArrayToProcess(0, 0, 0, CELLS, metric)
            t.SetLowerThreshold(float(lo if lo is not None else -1e300))
            t.SetUpperThreshold(float(hi if hi is not None else 1e300))
            t.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_BETWEEN)
            t.Update()
            polys.append(to_poly(t.GetOutput()))
        elif spec.get("axis"):
            polys.append(stage_slice(spec, [work], metric, "magnitude").poly)
        else:
            polys.append(to_poly(work))
    poly = _append(polys)
    out = StageOutput(poly, metric, "cell", _range(poly, metric, "cell"), cmap=spec.get("cmap", "inferno"),
                      label=f"mesh quality: {metric}", notes=notes)
    if not poly.GetNumberOfCells() and not notes:
        out.notes.append("no cells in that range")
    return out


def present_smooth(poly: vtk.vtkPolyData, iterations: int = 0, subdivide: int = 0) -> vtk.vtkPolyData:
    """PRESENTATION ONLY: Taubin (windowed-sinc) smoothing that does not shrink the surface, optional Loop
    subdivision, then smooth normals. Changes geometry for looks; callers label the result as smoothed."""
    out = poly
    if iterations > 0 and out.GetNumberOfPolys():
        f = vtk.vtkWindowedSincPolyDataFilter()
        f.SetInputData(out)
        f.SetNumberOfIterations(int(iterations))
        f.SetPassBand(0.05)
        f.BoundarySmoothingOff()
        f.FeatureEdgeSmoothingOff()
        f.NonManifoldSmoothingOn()
        f.NormalizeCoordinatesOn()
        f.Update()
        out = f.GetOutput()
    if subdivide > 0 and out.GetNumberOfPolys():
        clean = vtk.vtkCleanPolyData()
        clean.SetInputData(out)
        clean.Update()
        sd = vtk.vtkLoopSubdivisionFilter()
        sd.SetInputConnection(clean.GetOutputPort())
        sd.SetNumberOfSubdivisions(min(int(subdivide), 2))
        sd.Update()
        out = sd.GetOutput()
    if out is not poly:
        n = vtk.vtkPolyDataNormals()
        n.SetInputData(out)
        n.SplittingOff()
        n.ConsistencyOn()
        n.ComputePointNormalsOn()
        n.Update()
        res = vtk.vtkPolyData()
        res.ShallowCopy(n.GetOutput())
        return res
    return out


def surface_to_poly(s) -> vtk.vtkPolyData:
    """aethercfd.results.iso.Surface -> vtkPolyData with point normals (smooth shading) and scalars."""
    poly = vtk.vtkPolyData()
    pts = vtk.vtkPoints()
    pts.SetData(nps.numpy_to_vtk(np.ascontiguousarray(s.points), deep=True))
    poly.SetPoints(pts)
    n = len(s.triangles)
    conn = nps.numpy_to_vtk(np.ascontiguousarray(s.triangles.astype(np.int64).ravel()), deep=True,
                            array_type=vtk.VTK_ID_TYPE)
    offs = nps.numpy_to_vtk(np.arange(0, 3 * n + 1, 3, dtype=np.int64), deep=True, array_type=vtk.VTK_ID_TYPE)
    cells = vtk.vtkCellArray()
    cells.SetData(offs, conn)
    poly.SetPolys(cells)
    nrm = nps.numpy_to_vtk(np.ascontiguousarray(s.normals), deep=True)
    nrm.SetName("Normals")
    poly.GetPointData().SetNormals(nrm)
    if s.scalars is not None:
        sc = nps.numpy_to_vtk(np.ascontiguousarray(s.scalars), deep=True)
        sc.SetName("colour")
        poly.GetPointData().AddArray(sc)
    return poly


_PERM_CACHE: dict[tuple, np.ndarray] = {}


def global_cell_order(case: Path, n_cells: int, region: str = "") -> np.ndarray | None:
    """For a case read DECOMPOSED (processor meshes appended in processor order): the global OpenFOAM cell id
    of every loaded cell, from processorN/constant/polyMesh/cellProcAddressing."""
    from aethercfd.mesh.polymesh import _read_list

    procs = sorted((p for p in case.glob("processor*") if p.name[9:].isdigit()), key=lambda p: int(p.name[9:]))
    files = [p / "constant" / region / "polyMesh" / "cellProcAddressing" if region else
             p / "constant" / "polyMesh" / "cellProcAddressing" for p in procs]
    if not files or not all(f.exists() for f in files):
        return None
    key = (str(case), tuple(f.stat().st_mtime for f in files))
    if key not in _PERM_CACHE:
        _PERM_CACHE.clear()
        _PERM_CACHE[key] = np.concatenate([_read_list(f, "labels")[0] for f in files])
    perm = _PERM_CACHE[key]
    return perm if len(perm) == n_cells else None


def _to_global(values: np.ndarray, perm: np.ndarray | None) -> np.ndarray:
    if perm is None:
        return values
    out = np.empty_like(values)
    out[perm] = values
    return out


def _check_order(ds, centres: np.ndarray, perm: np.ndarray | None, sample: int = 200) -> bool:
    """Loaded cell i must sit where the mesh's cell (perm[i] or i) is: compare cell centres on a sample."""
    n = ds.GetNumberOfCells()
    idx = np.linspace(0, n - 1, min(sample, n)).astype(int)
    b = [ds.GetCell(int(i)).GetBounds() for i in idx]
    mid = np.array([[(x[0] + x[1]) / 2, (x[2] + x[3]) / 2, (x[4] + x[5]) / 2] for x in b])
    target = centres[perm[idx] if perm is not None else idx]
    span = np.array([x[1] - x[0] + x[3] - x[2] + x[5] - x[4] for x in b])
    return bool(np.all(np.linalg.norm(mid - target, axis=1) <= np.maximum(span, 1e-12)))


def _cell_numpy(ds, name: str, comp: str = "magnitude") -> np.ndarray | None:
    arr = ds.GetCellData().GetArray(name)
    if arr is None:
        return None
    a = nps.vtk_to_numpy(arr).astype(np.float64)
    if a.ndim == 2 and a.shape[1] > 1:
        return np.linalg.norm(a, axis=1) if comp == "magnitude" else a[:, "xyz".index(comp)]
    return a


def stage_gpu_iso(spec, internal, field_name, comp, *, mesh_dirs: list[Path], decomposed: bool = False) \
        -> StageOutput:
    """Q-criterion or any cell field contoured by the tet-decomposition engine (CUDA when available)."""
    from aethercfd.core.gpu import array_module
    from aethercfd.results import iso

    kind = spec.get("type")
    if not internal or not mesh_dirs:
        return StageOutput(vtk.vtkPolyData(), notes=["no internal mesh"], label=kind)
    ds, md = internal[0], mesh_dirs[0]
    try:
        tm = iso.tetmesh_for(md)
    except Exception as exc:
        return StageOutput(vtk.vtkPolyData(), notes=[f"mesh: {exc}"], label=kind)
    if tm.n_cells != ds.GetNumberOfCells():
        return StageOutput(vtk.vtkPolyData(), notes=[f"{ds.GetNumberOfCells()} loaded cells vs {tm.n_cells} in the "
                                                     "mesh"], label=kind)
    perm = global_cell_order(md.parent.parent, tm.n_cells) if decomposed else None
    if decomposed and perm is None:
        return StageOutput(vtk.vtkPolyData(), notes=["decomposed case without cellProcAddressing"], label=kind)
    if not _check_order(ds, tm.xyz[tm.n_points:], perm):
        return StageOutput(vtk.vtkPolyData(), notes=["loaded cells do not match the mesh cell order; not drawing "
                                                     "a wrong surface"], label=kind)
    xp, backend = array_module(spec.get("backend", "auto"))
    t_gpu = time.perf_counter()
    notes = []
    color_name = spec.get("color_by") or ("U" if kind == "qcriterion" else None)
    color = _cell_numpy(ds, color_name) if color_name else None
    color = _to_global(color, perm) if color is not None else None
    if kind == "qcriterion":
        Ucell = ds.GetCellData().GetArray(spec.get("field") or "U")
        if Ucell is None:
            return StageOutput(vtk.vtkPolyData(), notes=["U is not loaded"], label="Q-criterion")
        U = xp.asarray(_to_global(nps.vtk_to_numpy(Ucell).astype(np.float64), perm))
        L = iso.velocity_gradient(tm, U, xp)
        q = iso.q_criterion(L, xp)
        level = spec.get("level")
        if level is None:
            level = iso.auto_q_level(q, xp, float(spec.get("percentile", 99.0)))
            notes.append(f"Q level {level:.4g} 1/s² (auto, {spec.get('percentile', 99.0):g}th percentile of Q > 0)")
        scalar, label = q, "Q-criterion"
    else:
        name = spec.get("field") or field_name
        vals = _cell_numpy(ds, name, spec.get("component", comp)) if name else None
        if vals is None:
            return StageOutput(vtk.vtkPolyData(), notes=[f"field {name!r} is not loaded"], label="isosurface")
        level = spec.get("value")
        if level is None:
            level = float(np.median(vals))
            notes.append(f"value {level:.4g} (median)")
        scalar, label = xp.asarray(_to_global(vals, perm)), f"isosurface {name}"
    s = iso.isosurface(tm, scalar, float(level), color_cells=color, backend=backend)
    notes.append(f"{len(s.triangles):,} triangles in {s.seconds * 1000:.0f} ms on the {s.backend.upper()}")
    if backend == "cuda" and time.perf_counter() - t_gpu > 8.0:
        notes.append(f"{time.perf_counter() - t_gpu:.0f} s in total, mostly compiling the CUDA kernels once; they are "
                     "cached, so the next GPU stage starts in milliseconds")
    poly = surface_to_poly(s)
    sm, sub = int(spec.get("smooth", 0) or 0), int(spec.get("subdivide", 0) or 0)
    if sm or sub:
        poly = present_smooth(poly, sm, sub)
        notes.append(f"smoothed for presentation ({sm} iterations, {sub} subdivisions): not the raw data")
    out = StageOutput(poly, "colour" if s.scalars is not None else None, "point",
                      None if s.scalars is None or not len(s.scalars) else (float(s.scalars.min()),
                                                                             float(s.scalars.max())),
                      cmap=spec.get("cmap"), interpolated=True, label=f"{label} ({s.backend})", notes=notes)
    return out


def stage_cell_iso(spec, internal, field_name, comp) -> StageOutput:
    """Cells mode isosurface: the faces between cells on either side of the value (no interpolation)."""
    from aethercfd.results import sections as sx

    name = spec.get("field") or field_name
    used, polys, notes = None, [], []
    level = spec.get("value")
    for ds in internal:
        n = scalar_array(ds, name, spec.get("component", comp)) if name else None
        if n is None:
            continue
        used = n
        if level is None:
            vals = nps.vtk_to_numpy(ds.GetCellData().GetArray(n))
            level = float(np.median(vals))
            notes.append(f"value {level:.4g} (median)")
        polys.append(sx.staircase(ds, n, [float(level)]))
    if used is None:
        return StageOutput(vtk.vtkPolyData(), notes=[f"field {name!r} is not loaded"], label="isosurface")
    poly = _append(polys)
    notes.append("per cell: the faces between cells on either side of the value (the mesh's steps)")
    return StageOutput(poly, used, "cell", _range(poly, used, "cell"), cmap=spec.get("cmap"),
                       label=f"isosurface {name} (per cell)", notes=notes)


STAGES = {"slice": stage_slice, "clip": stage_clip, "threshold": stage_threshold, "contour": stage_contour,
          "streamlines": stage_streamlines, "glyphs": stage_glyphs, "schlieren": stage_schlieren}


def run_stage(spec: dict, internal: list, field_name: str | None, comp: str = "magnitude", *,
              mesh_dirs: list[Path] | None = None, decomposed: bool = False, smooth: bool = True,
              patches: dict | None = None) -> StageOutput:
    """One stage. ``smooth`` is the viewer's Smooth / Cells choice (slices, clips, contours, isosurfaces);
    ``patches`` the patch surfaces read for it (inlet seeds, a plane through a patch)."""
    t = spec.get("type")
    if t == "quality":
        return stage_quality(spec, internal, field_name, comp, mesh_dirs=mesh_dirs or [])
    if t == "isosurface" and not smooth:
        return stage_cell_iso(spec, internal, field_name, comp)
    if t in ("qcriterion", "isosurface"):
        return stage_gpu_iso(spec, internal, field_name, comp, mesh_dirs=mesh_dirs or [], decomposed=decomposed)
    if t == "slice":
        return stage_slice(spec, internal, field_name, comp, smooth=smooth, patches=patches)
    if t == "clip":
        return stage_clip(spec, internal, field_name, comp, smooth=smooth)
    if t == "contour":
        return stage_contour(spec, internal, field_name, comp, smooth=smooth)
    if t == "streamlines":
        return stage_streamlines(spec, internal, field_name, comp, patches=patches)
    fn = STAGES.get(t)
    if fn is None:
        return StageOutput(vtk.vtkPolyData(), notes=[f"unknown stage {t!r}"])
    return fn(spec, internal, field_name, comp)


def fields_needed(stages: list[dict]) -> set[str]:
    out = set()
    for s in stages:
        key = STAGE_FIELDS.get(s.get("type", ""))
        if key and s.get(key):
            out.add(s[key])
            if s["type"] == "schlieren":
                out.add(f"grad({s[key]})")
        elif s.get("type") in ("streamlines", "glyphs", "qcriterion", "pathlines"):
            out.add("U")
        if s.get("type") == "slice" and s.get("arrows"):
            out.add(s.get("vector") or "U")
        if s.get("color_by") and s["color_by"] not in ("residence_time", "time", RESIDENCE):
            out.add(s["color_by"])
    return out


def patches_needed(stages: list[dict]) -> set[str]:
    """Patch surfaces the stages need besides the volume: inlet seeds, a plane through a patch."""
    out = set()
    for s in stages:
        if s.get("seed") == "patches":
            out.update(s.get("patches") or [])
        if s.get("through"):
            out.add(s["through"])
    return out


# ============================================================================ view transforms
def _transform(poly: vtk.vtkPolyData, t: vtk.vtkTransform) -> vtk.vtkPolyData:
    f = vtk.vtkTransformFilter()  # vtkTransformPolyDataFilter is deprecated in VTK 9.7
    f.SetInputData(poly)
    f.SetTransform(t)
    f.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(f.GetOutput())
    return out


def mirror(poly: vtk.vtkPolyData, axis: str, origin: float = 0.0) -> vtk.vtkPolyData:
    """The mirror image about the plane axis = origin (the original is kept by the caller)."""
    i = "xyz".index(axis.lower()[0])
    t = vtk.vtkTransform()
    s = [1.0, 1.0, 1.0]
    s[i] = -1.0
    tr = [0.0, 0.0, 0.0]
    tr[i] = origin
    t.Translate(*tr)
    t.Scale(*s)
    t.Translate(*[-x for x in tr])
    return _transform(poly, t)


def revolve(poly: vtk.vtkPolyData, axis: str, angle: float, copies: int,
            origin: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> list[vtk.vtkPolyData]:
    """Rotated copies of a wedge (copies x angle degrees around the axis through origin)."""
    out = []
    vec = [0.0, 0.0, 0.0]
    vec["xyz".index(axis.lower()[0])] = 1.0
    for k in range(1, copies):
        t = vtk.vtkTransform()
        t.Translate(*origin)
        t.RotateWXYZ(k * angle, *vec)
        t.Translate(*[-x for x in origin])
        out.append(_transform(poly, t))
    return out


def wedge_angle(patch_normals: list[np.ndarray]) -> float | None:
    """Wedge opening angle [deg] from the mean normals of the two wedge patches."""
    if len(patch_normals) != 2:
        return None
    a, b = (n / (np.linalg.norm(n) or 1.0) for n in patch_normals)
    return float(180.0 - np.degrees(np.arccos(np.clip(np.dot(a, b), -1, 1))))
