"""Geometry intake for meshing: what an STL is made of before choosing how to mesh it (VTK, no Qt).

- integrity: watertight?, open (boundary) edges, non-manifold edges, degenerate triangles
- size: bounding box, surface area, volume (for closed surfaces), characteristic length
- features: dihedral-angle histogram and a suggested feature angle (the gap between smooth-surface angles
  and true edges), feature-edge length
- curvature: per-vertex mean curvature -> the smallest radius worth resolving
- shape recognition: extruded prism (all normals perpendicular to one axis except the caps) and
  axisymmetric body (radius about an axis is a function of the axial coordinate only)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import vtk
from vtk.util import numpy_support as nps


@dataclass
class GeometryReport:
    path: str
    triangles: int
    bounds: tuple[float, ...]
    area: float
    volume: float | None
    watertight: bool
    open_edges: int
    nonmanifold_edges: int
    degenerate: int
    feature_angle: float
    feature_edges: int
    feature_length: float
    min_radius: float | None
    shape: str  # extruded | axisymmetric | general
    axis: str | None = None
    char_length: float = 0.0
    dihedral_histogram: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_surface(path: str | Path) -> vtk.vtkPolyData:
    p = Path(path)
    r = vtk.vtkSTLReader() if p.suffix.lower() == ".stl" else vtk.vtkOBJReader() if p.suffix.lower() == ".obj" \
        else vtk.vtkSTLReader()
    r.SetFileName(str(p))
    r.Update()
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(r.GetOutput())
    clean.Update()
    tri = vtk.vtkTriangleFilter()
    tri.SetInputData(clean.GetOutput())
    tri.Update()
    return tri.GetOutput()


def _edges(poly: vtk.vtkPolyData, **kw) -> int:
    f = vtk.vtkFeatureEdges()
    f.SetInputData(poly)
    f.SetBoundaryEdges(kw.get("boundary", False))
    f.SetNonManifoldEdges(kw.get("nonmanifold", False))
    f.SetFeatureEdges(kw.get("feature", False))
    f.SetManifoldEdges(False)
    if "angle" in kw:
        f.SetFeatureAngle(kw["angle"])
    f.Update()
    return f.GetOutput().GetNumberOfCells()


def dihedral_angles(poly: vtk.vtkPolyData) -> np.ndarray:
    """Angle between the normals of the two triangles sharing each interior edge [deg]."""
    pts = nps.vtk_to_numpy(poly.GetPoints().GetData())
    conn = nps.vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    n = np.cross(pts[conn[:, 1]] - pts[conn[:, 0]], pts[conn[:, 2]] - pts[conn[:, 0]])
    n /= np.linalg.norm(n, axis=1, keepdims=True).clip(min=1e-300)
    e = np.concatenate([conn[:, [0, 1]], conn[:, [1, 2]], conn[:, [2, 0]]])
    tri = np.tile(np.arange(len(conn)), 3)
    key = np.sort(e, axis=1)
    k = key[:, 0].astype(np.int64) * (len(pts) + 1) + key[:, 1]
    order = np.argsort(k, kind="stable")
    ks, ts = k[order], tri[order]
    same = ks[1:] == ks[:-1]
    a, b = ts[:-1][same], ts[1:][same]
    cosang = np.clip(np.einsum("ij,ij->i", n[a], n[b]), -1, 1)
    return np.degrees(np.arccos(cosang))


def suggest_feature_angle(angles: np.ndarray) -> float:
    """Largest gap in the dihedral-angle distribution between 10 and 80 deg: smooth facets sit below it,
    true edges above it. Falls back to 30 deg (snappyHexMesh's usual value)."""
    a = np.sort(angles[(angles > 1) & (angles < 179)])
    if a.size < 10:
        return 30.0
    hist, edges = np.histogram(a, bins=np.arange(0, 181, 5))
    best, at = 0, 30.0
    for i in range(2, 16):  # 10..80 deg
        if hist[i] == 0:
            run = 1
            j = i + 1
            while j < 36 and hist[j] == 0:
                run += 1
                j += 1
            if run > best:
                best, at = run, float(edges[i] + 2.5 * run)
    return max(10.0, min(80.0, at))


def _sample(pts: np.ndarray, conn: np.ndarray, areas: np.ndarray, n: int = 30000) -> np.ndarray:
    """Points spread over the surface by area (vertices alone are too sparse on long triangles)."""
    rng = np.random.default_rng(0)
    idx = rng.choice(len(conn), n, p=areas / areas.sum())
    u, v = rng.random(n), rng.random(n)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    a, b, c = pts[conn[idx, 0]], pts[conn[idx, 1]], pts[conn[idx, 2]]
    return a + u[:, None] * (b - a) + v[:, None] * (c - a)


def _recognise(pts: np.ndarray, conn: np.ndarray, normals: np.ndarray, areas: np.ndarray) -> tuple[str, str | None]:
    w = areas / areas.sum()
    cent = pts[conn].mean(1)
    best = None
    for ax, name in enumerate("xyz"):  # extrusion: sides parallel to the axis, two equal caps at its ends
        side = np.abs(normals[:, ax]) < 0.02
        cap = np.abs(np.abs(normals[:, ax]) - 1) < 0.02
        if w[side | cap].sum() < 0.999 or w[side].sum() < 0.3 or not cap.any():
            continue
        lo, hi = pts[:, ax].min(), pts[:, ax].max()
        tol = 1e-6 * max(hi - lo, 1e-30)
        at_lo = cap & (np.abs(cent[:, ax] - lo) < tol)
        at_hi = cap & (np.abs(cent[:, ax] - hi) < tol)
        if (at_lo | at_hi).sum() != cap.sum():
            continue  # a face normal to the axis in the middle: a step, not an extrusion
        a_lo, a_hi = areas[at_lo].sum(), areas[at_hi].sum()
        if abs(a_lo - a_hi) > 0.01 * max(a_lo, a_hi):
            continue
        cap_frac = w[cap].sum()
        if best is None or cap_frac < best[0]:
            best = (cap_frac, name)
    if best is not None:
        return "extruded", best[1]
    s = _sample(pts, conn, areas)
    spreads = []
    for ax, name in enumerate("xyz"):  # axisymmetry: radius about the axis depends on the axial coordinate only
        others = [i for i in range(3) if i != ax]
        z = s[:, ax]
        bins = np.linspace(z.min(), z.max(), 41)
        idx = np.clip(np.digitize(z, bins) - 1, 0, 39)
        cx = (s[:, others[0]].min() + s[:, others[0]].max()) / 2
        cy = (s[:, others[1]].min() + s[:, others[1]].max()) / 2
        r = np.linalg.norm(s[:, others] - np.array([cx, cy]), axis=1)
        vals = []
        for b in range(40):
            rb = r[idx == b]
            if rb.size < 30 or rb.min() < 0.5 * np.median(rb):
                continue  # too few samples, or a flat cap (a disk spans small radii too)
            vals.append(float(rb.std() / (rb.mean() + 1e-300)))
        if len(vals) >= 15:
            spreads.append((float(np.median(vals)), name))
    if spreads:
        sp, name = min(spreads)
        if sp < 0.03:
            return "axisymmetric", name
    return "general", None


def analyse(path: str | Path) -> GeometryReport:
    poly = read_surface(path)
    n = poly.GetNumberOfCells()
    b = poly.GetBounds()
    mp = vtk.vtkMassProperties()
    mp.SetInputData(poly)
    mp.Update()
    open_e = _edges(poly, boundary=True)
    nonman = _edges(poly, nonmanifold=True)
    watertight = open_e == 0 and nonman == 0
    q = vtk.vtkMeshQuality()
    q.SetInputData(poly)
    q.SetTriangleQualityMeasureToArea()
    q.Update()
    areas = nps.vtk_to_numpy(q.GetOutput().GetCellData().GetArray("Quality"))
    angles = dihedral_angles(poly)
    fa = suggest_feature_angle(angles)
    fe = vtk.vtkFeatureEdges()
    fe.SetInputData(poly)
    fe.BoundaryEdgesOff()
    fe.NonManifoldEdgesOff()
    fe.ManifoldEdgesOff()
    fe.FeatureEdgesOn()
    fe.SetFeatureAngle(fa)
    fe.Update()
    flen = 0.0
    fpts = fe.GetOutput()
    for i in range(fpts.GetNumberOfCells()):
        c = fpts.GetCell(i)
        p0, p1 = c.GetPoints().GetPoint(0), c.GetPoints().GetPoint(1)
        flen += math.dist(p0, p1)
    curv = vtk.vtkCurvatures()
    curv.SetInputData(poly)
    curv.SetCurvatureTypeToMean()
    curv.Update()
    H = nps.vtk_to_numpy(curv.GetOutput().GetPointData().GetArray("Mean_Curvature"))
    Hs = np.abs(H[np.isfinite(H)])
    min_r = float(1.0 / np.percentile(Hs, 98)) if Hs.size and np.percentile(Hs, 98) > 0 else None
    pts = nps.vtk_to_numpy(poly.GetPoints().GetData())
    conn = nps.vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    nrm = np.cross(pts[conn[:, 1]] - pts[conn[:, 0]], pts[conn[:, 2]] - pts[conn[:, 0]])
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True).clip(min=1e-300)
    shape, axis = _recognise(pts, conn, nrm, np.abs(areas)) if watertight else ("general", None)
    char = (mp.GetVolume() ** (1 / 3)) if watertight else max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
    hist, edges = np.histogram(angles, bins=[0, 5, 10, 20, 30, 45, 60, 90, 120, 180])
    rep = GeometryReport(str(path), n, tuple(round(x, 9) for x in b), float(mp.GetSurfaceArea()),
                         float(mp.GetVolume()) if watertight else None, watertight, open_e, nonman,
                         int((np.abs(areas) < 1e-18).sum()), fa, fpts.GetNumberOfCells(), flen, min_r, shape, axis,
                         float(char),
                         [{"from": float(edges[i]), "to": float(edges[i + 1]), "edges": int(hist[i])}
                          for i in range(len(hist))])
    if not watertight:
        rep.notes.append(f"not watertight ({open_e} open, {nonman} non-manifold edges): snappyHexMesh may leak; "
                         "repair or use a wrapping (shrink-wrap) step first")
    if rep.degenerate:
        rep.notes.append(f"{rep.degenerate} degenerate (zero-area) triangles")
    return rep
