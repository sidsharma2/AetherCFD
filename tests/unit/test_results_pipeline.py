"""Visualization pipeline stages on a real OpenFOAM mesh (offscreen: filters only, no rendering)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

vtk = pytest.importorskip("vtk")
from vtk.util import numpy_support as nps  # noqa: E402

from aethercfd.results import pipeline as pl  # noqa: E402
from aethercfd.results.foam_reader import FoamResults, iter_leaves  # noqa: E402

CASE = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


@pytest.fixture(scope="module")
def internal():
    r = FoamResults(CASE)
    mb = r.read(0.0, fields=[])
    ds = next(d for p, d in iter_leaves(mb) if p.endswith("internalMesh"))
    cc = vtk.vtkCellCenters()
    cc.SetInputData(ds)
    cc.Update()
    c = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData())
    x, y = c[:, 0] - 0.05, c[:, 1] - 0.05
    U = np.stack([-y, x, np.zeros_like(x)], 1)  # solid-body vortex around the cavity centre
    rho = 1.0 + 0.5 * np.tanh((c[:, 0] - 0.05) / 0.004)  # a sharp "shock" at x = 0.05
    T = 300 + 1000 * c[:, 0]
    for name, a in (("U", U), ("rho", rho), ("T", T)):
        arr = nps.numpy_to_vtk(np.ascontiguousarray(a, dtype=np.float64), deep=True)
        arr.SetName(name)
        ds.GetCellData().AddArray(arr)
    return [ds]


def test_slice_keeps_cell_values(internal):
    out = pl.run_stage({"type": "slice", "axis": "z", "pos": 0.5}, internal, "T")
    assert out.poly.GetNumberOfCells() == 400 and out.scalar == "T"
    assert 300 < out.range[0] < out.range[1] < 400


def test_clip_and_crinkle(internal):
    half = pl.run_stage({"type": "clip", "axis": "x", "pos": 0.5}, internal, "T")
    whole = pl.run_stage({"type": "clip", "axis": "x", "pos": 0.52, "crinkle": True}, internal, "T")
    assert half.poly.GetNumberOfCells() > 0 and whole.poly.GetNumberOfCells() > 0
    assert half.range[1] <= 350.0 + 1e-6  # only the x < 0.05 half remains (T = 300 + 1000 x)


def test_threshold_and_contour(internal):
    t = pl.run_stage({"type": "threshold", "field": "T", "lo": 370, "hi": 400}, internal, None)
    assert t.poly.GetNumberOfCells() > 0 and t.range[0] >= 370 - 1e-9
    c = pl.run_stage({"type": "contour", "field": "T", "values": [350]}, internal, None)
    assert c.poly.GetNumberOfCells() > 0 and c.interpolated


def test_streamlines_follow_the_vortex(internal):
    s = pl.run_stage({"type": "streamlines", "field": "U", "seed": "line", "axis": "y", "pos": 0.5, "n": 8,
                      "max_length": 2.0}, internal, None)
    assert s.poly.GetNumberOfCells() > 0 and s.scalar == "U_mag" and s.association == "point"
    pts = nps.vtk_to_numpy(s.poly.GetPoints().GetData())
    r = np.hypot(pts[:, 0] - 0.05, pts[:, 1] - 0.05)
    assert r.max() < 0.071  # circles stay inside the cavity


def test_glyphs(internal):
    g = pl.run_stage({"type": "glyphs", "field": "U", "max_points": 50}, internal, None)
    assert g.poly.GetNumberOfCells() > 0


def test_schlieren_marks_the_shock(internal):
    s = pl.run_stage({"type": "schlieren", "source": "rho", "k": 10, "axis": "z", "pos": 0.5}, internal, None)
    assert s.cmap == "gray" and s.interpolated and s.poly.GetNumberOfCells() == 400
    vals = nps.vtk_to_numpy(s.poly.GetCellData().GetArray("schlieren"))
    cells = vtk.vtkCellCenters()
    cells.SetInputData(s.poly)
    cells.Update()
    x = nps.vtk_to_numpy(cells.GetOutput().GetPoints().GetData())[:, 0]
    assert vals[np.abs(x - 0.05) < 0.003].mean() < 0.3 < vals[np.abs(x - 0.05) > 0.03].mean()


def test_quality_stage_attaches_metrics(internal):
    q = pl.run_stage({"type": "quality", "metric": "aspectRatio"}, internal, None,
                     mesh_dirs=[CASE / "constant" / "polyMesh"])
    assert not q.notes and q.poly.GetNumberOfCells() > 0 and q.range == pytest.approx((1.0, 1.0))
    none = pl.run_stage({"type": "quality", "metric": "nonOrthogonality", "min": 10}, internal, None,
                        mesh_dirs=[CASE / "constant" / "polyMesh"])
    assert none.poly.GetNumberOfCells() == 0


def test_mirror_revolve_and_fields_needed(internal):
    surf = pl.to_poly(internal[0])
    m = pl.mirror(surf, "x", 0.0)
    assert m.GetBounds()[1] <= 1e-12 and m.GetBounds()[0] == pytest.approx(-0.1)
    assert len(pl.revolve(surf, "z", 90.0, 4)) == 3
    assert pl.fields_needed([{"type": "schlieren", "source": "rho"}, {"type": "streamlines"}]) == \
        {"rho", "grad(rho)", "U"}
    assert pl.wedge_angle([np.array([0, 1, 0.0436]), np.array([0, -1, 0.0436])]) == pytest.approx(5.0, abs=0.01)


def _prism(order) -> vtk.vtkUnstructuredGrid:
    pts = vtk.vtkPoints()
    for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1)):
        pts.InsertNextPoint(*p)
    ug = vtk.vtkUnstructuredGrid()
    ug.SetPoints(pts)
    ug.InsertNextCell(vtk.VTK_WEDGE, 6, order)
    t = nps.numpy_to_vtk(np.array([7.0]), deep=True)
    t.SetName("T")
    ug.GetCellData().AddArray(t)
    return ug


def _all_outward(poly) -> bool:
    """Every face of a convex closed surface points away from its centre."""
    n = vtk.vtkPolyDataNormals()
    n.SetInputData(poly)
    n.ComputeCellNormalsOn()
    n.ComputePointNormalsOff()
    n.ConsistencyOff()
    n.AutoOrientNormalsOff()
    n.SplittingOff()
    n.Update()
    normals = nps.vtk_to_numpy(n.GetOutput().GetCellData().GetNormals())
    cc = vtk.vtkCellCenters()
    cc.SetInputData(poly)
    cc.Update()
    centres = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData())
    mid = centres.mean(axis=0)
    return bool(len(normals)) and bool((np.einsum("ij,ij->i", normals, centres - mid) > 0).all())


@pytest.mark.parametrize("order", [[0, 1, 2, 3, 4, 5], [0, 2, 1, 3, 5, 4]])  # one is inside out, like the reader's
def test_stage_surfaces_are_wound_outward(order):
    """VTK's OpenFOAM reader hands prisms over inside out: clip / threshold surfaces must still face outward
    (an inward face is lit from behind and shows as a dark streak)."""
    ug = _prism(order)
    surf = pl.to_poly(ug)
    assert surf.GetNumberOfPolys() == 5 and _all_outward(surf)
    assert surf.GetCellData().GetArray("vtkOriginalCellIds") is None
    for spec in ({"type": "clip", "axis": "z", "pos": 0.5}, {"type": "clip", "axis": "z", "pos": 0.5, "crinkle": True},
                 {"type": "threshold", "field": "T", "lo": 0}):
        out = pl.run_stage(spec, [ug], "T")
        assert out.poly.GetNumberOfPolys() >= 5 and _all_outward(out.poly), spec
        assert out.range == pytest.approx((7.0, 7.0))  # cell data survives the reorientation
