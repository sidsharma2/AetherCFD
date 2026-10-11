"""Sections, per-cell contours, flow-path seeds, line probes and pathlines on the real mesh (VTK only, no Qt).

Two honest ways to look at a cell-centred finite-volume field:
  * smooth: values interpolated to the mesh points (the OpenFOAM reader's cell-to-point, which uses the
    boundary values on the patches); contours are the smooth iso-lines / iso-surfaces of that field;
  * cells: every cell keeps its own value; a contour is the set of cell faces (3D) or cut-polygon edges (on
    a section) between cells on either side of the level, so the mesh resolution stays visible.

Everything here takes and returns plain VTK objects and dicts, so it runs on the viewport's worker thread.
"""

from __future__ import annotations

import math
import re

import numpy as np
import vtk
from vtk.util import numpy_support as nps

CELLS = vtk.vtkDataObject.FIELD_ASSOCIATION_CELLS

# section presets offered in the viewport's Sections menu and to the copilot
PRESETS = ("mid-x", "mid-y", "mid-z", "ortho3", "stack", "plane")
# patch types that never take in flow
_NOT_INLET_TYPES = {"wall", "empty", "symmetry", "symmetryPlane", "wedge", "cyclic", "cyclicAMI", "processor",
                    "mappedWall", "mappedPatch", "overset", "cyclicSlip", "nonConformalCyclic"}
_INLET_NAME = re.compile(r"inlet|inflow|intake|(^|[_\W])in($|[_\W])|feed|jet", re.IGNORECASE)
_OUTLET_NAME = re.compile(r"outlet|outflow|exhaust|exit", re.IGNORECASE)


# ============================================================================ planes
def plane_through(points: list) -> tuple[list[float], list[float]]:
    """(origin, unit normal) of the plane through three points; ValueError if they are (nearly) collinear."""
    p = np.asarray(points, dtype=float).reshape(3, 3)
    n = np.cross(p[1] - p[0], p[2] - p[0])
    size = max(np.linalg.norm(p[1] - p[0]), np.linalg.norm(p[2] - p[0]), 1e-300)
    if np.linalg.norm(n) < 1e-9 * size * size:
        raise ValueError("the three points are on one line: pick points that span a plane")
    n /= np.linalg.norm(n)
    return p.mean(axis=0).tolist(), n.tolist()


def stack_positions(n: int) -> list[float]:
    """n evenly spaced fractions inside (0, 1): the planes of a slice stack (none on the domain's faces)."""
    n = max(1, int(n))
    return [(k + 0.5) / n for k in range(n)]


def longest_axis(bounds: list[float]) -> str:
    ext = [bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]]
    return "xyz"[ext.index(max(ext))]


def thinnest_axis(bounds: list[float]) -> str:
    ext = [bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]]
    return "xyz"[ext.index(min(ext))]


def section_specs(preset: str, *, axis: str | None = None, n: int = 5, origin=None, normal=None,
                  points=None, through: str | None = None) -> list[dict]:
    """Stage specs (without ids) for a section preset: mid-x / mid-y / mid-z, ortho3 (three orthogonal
    mid-planes), stack (n parallel planes along ``axis``), plane (through 3 ``points``, ``origin`` + ``normal``,
    or through the centre of the patch ``through`` with ``normal``/``axis``)."""
    preset = (preset or "").lower().replace("_", "-").replace(" ", "-")
    base = {"type": "slice", "section": True, "visible": True}
    if preset in ("mid-x", "mid-y", "mid-z", "x", "y", "z"):
        return [{**base, "axis": preset[-1], "pos": 0.5}]
    if preset in ("ortho3", "ortho", "three", "orthogonal"):
        return [{**base, "axis": a, "pos": 0.5} for a in "xyz"]
    if preset in ("stack", "slice-stack"):
        return [{**base, "axis": (axis or "x").lower()[0], "stack": max(2, min(int(n), 40))}]
    if preset in ("plane", "points", "normal"):
        spec = dict(base)
        if points:
            spec["origin"], spec["normal"] = plane_through(points)
        else:
            if normal is None:
                ax = "xyz".index((axis or "z").lower()[0])
                normal = [1.0 if i == ax else 0.0 for i in range(3)]
            nv = np.asarray(normal, dtype=float)
            if nv.shape != (3,) or not np.linalg.norm(nv):
                raise ValueError("normal must be three numbers, not all zero")
            spec["normal"] = (nv / np.linalg.norm(nv)).tolist()
            if through:
                spec["through"] = through
            elif origin is not None:
                spec["origin"] = [float(x) for x in origin]
            else:
                spec["axis"] = "xyz"[int(np.argmax(np.abs(nv)))]
                spec["pos"] = 0.5
        return [spec]
    raise ValueError(f"unknown section preset {preset!r}; one of {', '.join(PRESETS)}")


def centre_of(polys: list) -> list[float] | None:
    """Area-weighted centre of some surfaces (a patch: the 'probe tip', an inlet)."""
    tot, acc = 0.0, np.zeros(3)
    for p in polys:
        if p is None or not p.GetNumberOfCells():
            continue
        cs = vtk.vtkCellSizeFilter()
        cs.SetInputData(p)
        cs.ComputeAreaOn()
        cs.ComputeVolumeOff()
        cs.ComputeLengthOff()
        cs.ComputeVertexCountOff()
        cs.Update()
        area = nps.vtk_to_numpy(cs.GetOutput().GetCellData().GetArray("Area"))
        cc = vtk.vtkCellCenters()
        cc.SetInputData(p)
        cc.Update()
        xyz = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData())
        w = area if area.sum() > 0 else np.ones(len(xyz))
        acc += (xyz * w[:, None]).sum(axis=0)
        tot += float(w.sum())
    return (acc / tot).tolist() if tot > 0 else None


# ============================================================================ per-cell (staircase) contours
def _face_keys(poly: vtk.vtkPolyData, lines: bool, scale: float) -> np.ndarray:
    """One hashable key per polygon (or line) of ``poly``: its rounded centroid as a void scalar."""
    cells = poly.GetLines() if lines else poly.GetPolys()
    n = cells.GetNumberOfCells()
    if n == 0:
        return np.empty(0, dtype="V24")
    offs = nps.vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
    conn = nps.vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
    xyz = nps.vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    counts = np.diff(offs)
    sums = np.add.reduceat(xyz[conn], offs[:-1], axis=0) if len(conn) else np.zeros((n, 3))
    cen = sums / np.maximum(counts, 1)[:, None]
    q = np.ascontiguousarray(np.round(cen / scale).astype(np.int64))
    return q.view("V24").ravel()


def _boundary(ds, lines: bool) -> vtk.vtkPolyData:
    """The surface faces (3D) or boundary edges (a 2D section) of a dataset."""
    g = vtk.vtkGeometryFilter()
    g.SetInputData(ds)
    g.Update()
    if not lines:
        return g.GetOutput()
    fe = vtk.vtkFeatureEdges()
    fe.SetInputConnection(g.GetOutputPort())
    fe.BoundaryEdgesOn()
    fe.FeatureEdgesOff()
    fe.ManifoldEdgesOff()
    fe.NonManifoldEdgesOff()
    fe.ColoringOff()
    fe.Update()
    return fe.GetOutput()


def _subset(poly: vtk.vtkPolyData, keep: np.ndarray, lines: bool) -> vtk.vtkPolyData:
    ids = vtk.vtkIdTypeArray()
    ids.SetNumberOfComponents(1)
    for i in np.flatnonzero(keep):
        ids.InsertNextValue(int(i))
    sel_node = vtk.vtkSelectionNode()
    sel_node.SetFieldType(vtk.vtkSelectionNode.CELL)
    sel_node.SetContentType(vtk.vtkSelectionNode.INDICES)
    sel_node.SetSelectionList(ids)
    sel = vtk.vtkSelection()
    sel.AddNode(sel_node)
    ex = vtk.vtkExtractSelection()
    ex.SetInputData(0, poly)
    ex.SetInputData(1, sel)
    ex.Update()
    g = vtk.vtkGeometryFilter()
    g.SetInputData(ex.GetOutput())
    g.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(g.GetOutput())
    return out


def staircase(ds, name: str, levels: list[float]) -> vtk.vtkPolyData:
    """Cells-mode contour of the cell array ``name``: for each level, the faces between cells above and below
    it (a volume: a stair-stepped iso-surface) or the edges between cut polygons (a section: a stair-stepped
    iso-line). Nothing is interpolated: the steps are the mesh. Each output cell carries ``name`` = its level."""
    if ds is None or not ds.GetNumberOfCells() or ds.GetCellData().GetArray(name) is None:
        return vtk.vtkPolyData()
    lines = ds.IsA("vtkPolyData")  # a section or other surface: edges between polygons; a volume: faces
    if lines:  # neighbouring cut polygons must share their points, or every edge looks like a boundary
        clean = vtk.vtkCleanPolyData()
        clean.SetInputData(ds)
        clean.PointMergingOn()
        clean.SetTolerance(0.0)
        clean.ConvertPolysToLinesOff()
        clean.ConvertLinesToPointsOff()
        clean.Update()
        ds = clean.GetOutput()
    b = ds.GetBounds()
    scale = max(math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5])), 1e-30) * 1e-9
    whole = _face_keys(_boundary(ds, lines), lines, scale)
    rng = ds.GetCellData().GetArray(name).GetRange()
    app = vtk.vtkAppendPolyData()
    n = 0
    for lev in levels:
        lev = float(lev)
        if not (rng[0] < lev <= rng[1]):
            continue
        t = vtk.vtkThreshold()
        t.SetInputData(ds)
        t.SetInputArrayToProcess(0, 0, 0, CELLS, name)
        t.SetUpperThreshold(lev)
        t.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_UPPER)
        t.Update()
        if not t.GetOutput().GetNumberOfCells():
            continue
        surf = _boundary(t.GetOutput(), lines)
        keys = _face_keys(surf, lines, scale)
        if not len(keys):
            continue
        keep = ~np.isin(keys, whole)
        if not keep.any():
            continue
        part = _subset(surf, keep, lines)
        arr = nps.numpy_to_vtk(np.full(part.GetNumberOfCells(), lev, dtype=np.float64), deep=True)
        arr.SetName(name)
        cd = part.GetCellData()
        for i in reversed(range(cd.GetNumberOfArrays())):
            cd.RemoveArray(i)
        cd.AddArray(arr)
        part.GetPointData().Initialize()
        app.AddInputData(part)
        n += 1
    if not n:
        return vtk.vtkPolyData()
    app.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(app.GetOutput())
    return out


def auto_levels(lo: float, hi: float, n: int = 8) -> list[float]:
    """n contour levels strictly inside (lo, hi), evenly spaced."""
    if not (hi > lo):
        return []
    return [float(v) for v in np.linspace(lo, hi, int(n) + 2)[1:-1]]


# ============================================================================ flow-path seeds
def find_inlets(patches: list[tuple[str, str, str]]) -> list[str]:
    """Reader patch paths that look like inlets. ``patches`` is (reader path, patch name, patch type): a name
    with inlet/inflow/intake/feed/jet (inlet*, *INLET*, *_in) that is not a wall, symmetry, empty, cyclic or
    an outlet."""
    out = []
    for path, name, ptype in patches:
        if ptype in _NOT_INLET_TYPES or name.startswith("procBoundary"):
            continue
        if _INLET_NAME.search(name) and not _OUTLET_NAME.search(name):
            out.append(path)
    return out


def patch_seeds(polys: list, n: int = 150, inward: bool = True) -> vtk.vtkPolyData:
    """Up to ``n`` seed points on some patches (face centres, every patch gets its share), moved a quarter of
    a face size into the domain so the tracer starts inside a cell (OpenFOAM boundary normals point out)."""
    polys = [p for p in polys if p is not None and p.GetNumberOfCells()]
    if not polys:
        return vtk.vtkPolyData()
    per = max(12, int(n) // len(polys))
    pts_all = []
    for p in polys:
        nrm = vtk.vtkPolyDataNormals()
        nrm.SetInputData(p)
        nrm.ComputeCellNormalsOn()
        nrm.ComputePointNormalsOff()
        nrm.ConsistencyOff()
        nrm.AutoOrientNormalsOff()
        nrm.SplittingOff()
        nrm.Update()
        cc = vtk.vtkCellCenters()
        cc.SetInputConnection(nrm.GetOutputPort())
        cc.Update()
        cs = vtk.vtkCellSizeFilter()
        cs.SetInputData(p)
        cs.ComputeAreaOn()
        cs.Update()
        xyz = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData()).astype(np.float64)
        normals = nps.vtk_to_numpy(nrm.GetOutput().GetCellData().GetNormals()).astype(np.float64)
        area = nps.vtk_to_numpy(cs.GetOutput().GetCellData().GetArray("Area")).astype(np.float64)
        if len(xyz) > per:  # evenly through the face list (faces are ordered; random would clump)
            idx = np.linspace(0, len(xyz) - 1, per).round().astype(int)
            xyz, normals, area = xyz[idx], normals[idx], area[idx]
        if inward:
            xyz = xyz - normals * (0.25 * np.sqrt(np.maximum(area, 0.0)))[:, None]
        pts_all.append(xyz)
    xyz = np.concatenate(pts_all)
    pts = vtk.vtkPoints()
    pts.SetData(nps.numpy_to_vtk(np.ascontiguousarray(xyz), deep=True))
    out = vtk.vtkPolyData()
    out.SetPoints(pts)
    verts = vtk.vtkCellArray()
    for i in range(len(xyz)):
        verts.InsertNextCell(1)
        verts.InsertCellPoint(i)
    out.SetVerts(verts)
    return out


def tubes(poly: vtk.vtkPolyData, radius: float, sides: int = 10) -> vtk.vtkPolyData:
    """Streamlines as shaded tubes (Studio look); the point arrays ride along."""
    if not poly.GetNumberOfLines():
        return poly
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(poly)
    t = vtk.vtkTubeFilter()
    t.SetInputConnection(clean.GetOutputPort())
    t.SetRadius(radius)
    t.SetNumberOfSides(sides)
    t.CappingOn()
    t.Update()
    out = vtk.vtkPolyData()
    out.ShallowCopy(t.GetOutput())
    return out


# ============================================================================ probes (line probes, pathlines)
def _only(ds, association: str):
    """A shallow copy of ``ds`` carrying only its point (or cell) arrays: a probe then never mixes the
    interpolated and the per-cell value of a field with the same name."""
    out = ds.NewInstance()
    out.CopyStructure(ds)
    if association == "point":
        out.GetPointData().ShallowCopy(ds.GetPointData())
    else:
        out.GetCellData().ShallowCopy(ds.GetCellData())
    return out


class Prober:
    """Values of one array at arbitrary points inside some volume meshes (several regions), with the cell
    locators built once. ``association`` point = interpolated (smooth), cell = the containing cell's value."""

    def __init__(self, datasets: list, association: str = "point"):
        self.sources = []
        for ds in datasets:
            src = _only(ds, association)
            loc = vtk.vtkStaticCellLocator()
            loc.SetDataSet(src)
            loc.BuildLocator()
            strat = vtk.vtkCellLocatorStrategy()
            strat.SetCellLocator(loc)
            self.sources.append((src, loc, strat))

    def sample(self, xyz: np.ndarray, names: list[str]) -> tuple[dict[str, np.ndarray], np.ndarray]:
        """({name: values [n, ncomp] with NaN outside}, inside mask) at the points ``xyz``."""
        xyz = np.ascontiguousarray(np.asarray(xyz, dtype=np.float64).reshape(-1, 3))
        pts = vtk.vtkPoints()
        pts.SetData(nps.numpy_to_vtk(xyz, deep=True))
        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        out: dict[str, np.ndarray] = {}
        inside = np.zeros(len(xyz), dtype=bool)
        for src, _loc, strat in self.sources:
            p = vtk.vtkProbeFilter()
            p.SetInputData(poly)
            p.SetSourceData(src)
            p.SetFindCellStrategy(strat)
            p.Update()
            res = p.GetOutput().GetPointData()
            mask = res.GetArray(p.GetValidPointMaskArrayName())
            if mask is None:
                continue
            ok = nps.vtk_to_numpy(mask).astype(bool) & ~inside
            hit = False
            for name in names:
                arr = res.GetArray(name)
                if arr is None:
                    continue
                vals = nps.vtk_to_numpy(arr).astype(np.float64).reshape(len(xyz), -1)
                if name not in out:
                    out[name] = np.full(vals.shape, np.nan)
                out[name][ok] = vals[ok]
                hit = True
            if hit:
                inside |= ok
        return out, inside

    def values(self, xyz: np.ndarray, name: str) -> tuple[np.ndarray | None, np.ndarray]:
        """(values [n, ncomp] with NaN outside, inside mask) of one array."""
        got, inside = self.sample(xyz, [name])
        return got.get(name), inside


def probe_line(datasets: list, p1, p2, name: str, *, n: int = 200, comp: str = "magnitude",
               smooth: bool = True) -> dict:
    """The field along the straight line p1 -> p2: distance s, the points and the values (NaN outside the
    mesh). Smooth: interpolated values; cells: each sample takes its cell's value, so the curve shows steps."""
    p1, p2 = np.asarray(p1, dtype=float), np.asarray(p2, dtype=float)
    n = max(2, min(int(n), 20000))
    t = np.linspace(0.0, 1.0, n)
    xyz = p1[None, :] + t[:, None] * (p2 - p1)[None, :]
    assoc = "point" if smooth and any(ds.GetPointData().GetArray(name) is not None for ds in datasets) else "cell"
    vals, inside = Prober(datasets, assoc).values(xyz, name)
    if vals is None:
        raise ValueError(f"field {name!r} is not loaded")
    if vals.shape[1] > 1:
        v = np.linalg.norm(vals, axis=1) if comp == "magnitude" else vals[:, "xyz".index(comp)]
    else:
        v = vals[:, 0]
    return {"s": (t * float(np.linalg.norm(p2 - p1))).tolist(), "values": [None if not np.isfinite(x) else float(x)
                                                                           for x in v],
            "points": xyz.tolist(), "inside": int(inside.sum()), "n": n, "association": assoc,
            "field": name, "component": comp if vals.shape[1] > 1 else None}


def pathlines(seeds: np.ndarray, frames: list[tuple[float, list]], vec: str = "U", substeps: int = 8) \
        -> vtk.vtkPolyData:
    """Particle paths through saved times (a transient run): RK2 (midpoint) in time with the velocity
    interpolated in space (mesh points) and linearly in time between saved times. A particle that leaves the
    mesh stops. Point arrays: 'time' [s] since release and |vec|."""
    seeds = np.asarray(seeds, dtype=np.float64).reshape(-1, 3)
    if len(frames) < 2 or not len(seeds):
        return vtk.vtkPolyData()
    # one dataset per region carrying every saved time's velocity (f0, f1, ...): one cell locator, built once
    merged = []
    for i, base in enumerate(frames[0][1]):
        ds = base.NewInstance()
        ds.CopyStructure(base)
        for k, (_t, dss) in enumerate(frames):
            src = dss[i].GetPointData().GetArray(vec) if i < len(dss) else None
            if src is None:
                continue
            a = src.NewInstance()
            a.ShallowCopy(src)
            a.SetName(f"f{k}")
            ds.GetPointData().AddArray(a)
        merged.append(ds)
    prober = Prober(merged, "point")

    def vel(k: int, x: np.ndarray, a: float) -> tuple[np.ndarray, np.ndarray]:
        got, ok = prober.sample(x, [f"f{k}", f"f{k + 1}"])
        ua, ub = got.get(f"f{k}"), got.get(f"f{k + 1}")
        if ua is None or ub is None:
            return np.zeros_like(x), np.zeros(len(x), dtype=bool)
        return np.nan_to_num((1 - a) * ua[:, :3] + a * ub[:, :3]), ok

    x = seeds.copy()
    alive = np.ones(len(x), dtype=bool)
    paths = [[x[i].copy()] for i in range(len(x))]
    times = [[0.0] for _ in range(len(x))]
    speeds = [[0.0] for _ in range(len(x))]
    t0 = frames[0][0]
    for k in range(len(frames) - 1):
        ta, tb = frames[k][0], frames[k + 1][0]
        h = (tb - ta) / max(1, substeps)
        for j in range(max(1, substeps)):
            if not alive.any():
                break
            a0 = j / max(1, substeps)
            am = (j + 0.5) / max(1, substeps)
            k1, ok1 = vel(k, x, a0)
            xm = x + 0.5 * h * k1
            k2, ok2 = vel(k, xm, am)
            step_ok = alive & ok1 & ok2
            x = np.where(step_ok[:, None], x + h * k2, x)
            alive &= step_ok
            for i in np.flatnonzero(step_ok):
                paths[i].append(x[i].copy())
                times[i].append(ta + (j + 1) * h - t0)
                speeds[i].append(float(np.linalg.norm(k2[i])))
    pts = vtk.vtkPoints()
    lines = vtk.vtkCellArray()
    tarr, sarr = [], []
    for path, ts, sp in zip(paths, times, speeds, strict=True):
        if len(path) < 2:
            continue
        ids = []
        for p in path:
            ids.append(pts.InsertNextPoint(*p))
        sp = [sp[1] if len(sp) > 1 else 0.0, *sp[1:]]
        lines.InsertNextCell(len(ids))
        for i in ids:
            lines.InsertCellPoint(i)
        tarr += ts
        sarr += sp
    out = vtk.vtkPolyData()
    out.SetPoints(pts)
    out.SetLines(lines)
    for nm, vals in (("time", tarr), (f"{vec}_mag", sarr)):
        a = nps.numpy_to_vtk(np.asarray(vals, dtype=np.float64), deep=True)
        a.SetName(nm)
        out.GetPointData().AddArray(a)
    return out
