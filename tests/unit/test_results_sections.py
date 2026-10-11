"""Sections, Smooth vs Cells contours, flow-path seeds, line probes and pathlines (VTK only, no rendering)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

vtk = pytest.importorskip("vtk")
from vtk.util import numpy_support as nps  # noqa: E402

from aethercfd.results import pipeline as pl  # noqa: E402
from aethercfd.results import sections as sx  # noqa: E402
from aethercfd.results.foam_reader import FoamResults, iter_leaves  # noqa: E402

CASE = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def _with_fields(ds):
    cc = vtk.vtkCellCenters()
    cc.SetInputData(ds)
    cc.Update()
    c = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData())
    x, y = c[:, 0] - 0.05, c[:, 1] - 0.05
    fields = {"U": np.stack([-y, x, np.zeros_like(x)], 1), "T": 300 + 1000 * c[:, 0]}
    for name, a in fields.items():
        arr = nps.numpy_to_vtk(np.ascontiguousarray(a, dtype=np.float64), deep=True)
        arr.SetName(name)
        ds.GetCellData().AddArray(arr)
    return ds


@pytest.fixture(scope="module")
def cells():
    """The 20 x 20 x 1 cavity with cell data only (what the reader gives in Cells mode)."""
    r = FoamResults(CASE)
    mb = r.read(0.0, fields=[], patches=["internalMesh", "patch/movingWall"])
    leaves = dict(iter_leaves(mb))
    return [_with_fields(leaves["internalMesh"])], leaves["boundary/movingWall"]


@pytest.fixture(scope="module")
def smooth(cells):
    """The same with the values also interpolated to the points (what the reader gives in Smooth mode)."""
    return [pl.cell_to_point(cells[0][0])]


def _xs(poly):
    cc = vtk.vtkCellCenters()
    cc.SetInputData(poly)
    cc.Update()
    return nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData())[:, 0]


def test_per_cell_isosurface_follows_the_cell_faces(cells, smooth):
    # T = 300 + 1000 x: the level 355 lies inside the cell column at 0.050 < x < 0.055
    stair = pl.run_stage({"type": "contour", "field": "T", "values": [355.0]}, cells[0], None, smooth=False)
    xs = _xs(stair.poly)
    assert stair.poly.GetNumberOfCells() == 20  # one face per cell row: the mesh's own faces
    assert np.allclose(xs, 0.055, atol=1e-9)  # on the face between the cells, not at x = 0.055 by luck
    assert "per cell" in stair.label and not stair.interpolated
    iso = pl.run_stage({"type": "contour", "field": "T", "values": [352.0]}, smooth, None, smooth=True)
    assert iso.interpolated and np.allclose(_xs(iso.poly), 0.052, atol=2e-4)  # between the faces: interpolated
    stair2 = pl.run_stage({"type": "contour", "field": "T", "values": [352.0]}, cells[0], None, smooth=False)
    assert np.allclose(_xs(stair2.poly), 0.050, atol=1e-9)  # the face between the cells either side
    per_cell = pl.run_stage({"type": "isosurface", "field": "T", "value": 352.0}, cells[0], None, smooth=False)
    assert per_cell.poly.GetNumberOfCells() == 20 and "per cell" in per_cell.label


def test_section_smooth_vs_cells_and_overlays(cells, smooth):
    spec = {"type": "slice", "section": True, "axis": "z", "pos": 0.5, "contours": 6, "arrows": True,
            "edges": True}
    c = pl.run_stage(spec, cells[0], "T", smooth=False)
    s = pl.run_stage(spec, smooth, "T", smooth=True)
    assert c.association == "cell" and s.association == "point" and "smoothed" in s.label
    assert c.edges and c.poly.GetNumberOfCells() == 400
    lines_c, lines_s = c.overlays["lines"], s.overlays["lines"]
    assert lines_c.GetNumberOfLines() > 0 and lines_s.GetNumberOfLines() > 0
    # per-cell contour lines run along cell edges (x on the 0.005 grid); smooth ones cut through the cells
    xc = nps.vtk_to_numpy(lines_c.GetPoints().GetData())[:, 0]
    assert np.allclose(np.round(xc / 0.005) * 0.005, xc, atol=1e-9)
    assert c.overlays["arrows"].GetNumberOfCells() > 0 and any("in-plane" in n for n in c.notes)


def test_stacks_points_and_patch_planes(cells):
    stack = pl.run_stage({"type": "slice", "section": True, "axis": "x", "stack": 4}, cells[0], "T", smooth=False)
    assert stack.poly.GetNumberOfCells() == 4 * 20 and "×4" in stack.label
    xs = sorted(set(np.round(_xs(stack.poly), 6)))
    assert len(xs) == 4 and xs[0] > 0 and xs[-1] < 0.1
    _origin, normal = sx.plane_through([[0.01, 0, 0.0], [0.01, 0.1, 0.0], [0.01, 0.0, 0.01]])
    assert np.allclose(np.abs(normal), [1, 0, 0])
    with pytest.raises(ValueError):
        sx.plane_through([[0, 0, 0], [1, 1, 1], [2, 2, 2]])
    spec = sx.section_specs("plane", points=[[0.01, 0, 0.0], [0.01, 0.1, 0.0], [0.01, 0.0, 0.01]])[0]
    assert np.allclose(_xs(pl.run_stage(spec, cells[0], "T").poly), 0.01)
    through = pl.run_stage({"type": "slice", "through": "movingWall", "normal": [0, 1, 0]}, cells[0], "T",
                           patches={"patch/movingWall": cells[1]})
    yc = nps.vtk_to_numpy(through.poly.GetPoints().GetData())[:, 1]
    assert np.allclose(yc, 0.1)  # the plane through the lid's centre
    assert len(sx.section_specs("ortho3")) == 3 and sx.section_specs("stack", axis="y", n=3)[0]["stack"] == 3


def test_inlets_seeds_and_residence_time(cells, smooth):
    rows = [("patch/inlet", "inlet", "patch"), ("fluid/patch/DILUTION_AIR_INLET", "DILUTION_AIR_INLET", "patch"),
            ("patch/outlet", "outlet", "patch"), ("patch/movingWall", "movingWall", "wall"),
            ("fluid/patch/SAMPLE_INLET", "SAMPLE_INLET", "patch"), ("patch/inletWall", "inletWall", "wall"),
            ("patch/front", "front", "empty")]
    assert sx.find_inlets(rows) == ["patch/inlet", "fluid/patch/DILUTION_AIR_INLET", "fluid/patch/SAMPLE_INLET"]
    seeds = sx.patch_seeds([cells[1]], n=10)
    y = nps.vtk_to_numpy(seeds.GetPoints().GetData())[:, 1]
    assert seeds.GetNumberOfPoints() == 12 and np.all(y < 0.1)  # moved inside from the lid
    out = pl.run_stage({"type": "streamlines", "field": "U", "seed": "patches", "patches": ["movingWall"], "n": 12,
                        "color_by": "residence_time", "tubes": True}, smooth, None,
                       patches={"patch/movingWall": cells[1]})
    assert out.scalar == pl.RESIDENCE and out.title == "residence time [s]" and out.range[1] > 0
    assert out.poly.GetNumberOfStrips() + out.poly.GetNumberOfPolys() > 0 and not out.poly.GetNumberOfLines()  # tubes
    assert any("residence time" in n for n in out.notes)


def test_line_probe_steps_in_cells_mode(cells, smooth):
    a, b = [0.0005, 0.0525, 0.005], [0.0995, 0.0525, 0.005]
    c = sx.probe_line(cells[0], a, b, "T", n=400, smooth=False)
    s = sx.probe_line(smooth, a, b, "T", n=400, smooth=True)
    assert c["association"] == "cell" and s["association"] == "point"
    assert len(set(np.round(c["values"], 9))) == 20  # one value per cell crossed: the steps are the mesh
    assert len(set(np.round(s["values"], 9))) > 100
    assert c["inside"] == 400 and abs(c["s"][-1] - 0.099) < 1e-12


def test_pathlines_follow_the_vortex(smooth):
    seeds = np.array([[0.07, 0.05, 0.005], [0.05, 0.08, 0.005]])
    frames = [(t, smooth) for t in (0.0, 0.5, 1.0, 1.5)]  # a steady vortex saved at 4 times
    poly = sx.pathlines(seeds, frames, "U", substeps=20)
    assert poly.GetNumberOfLines() == 2
    xyz = nps.vtk_to_numpy(poly.GetPoints().GetData())
    r = np.linalg.norm(xyz[:, :2] - 0.05, axis=1)
    assert r.max() - r.min() > 0.005  # the two particles start on different circles ...
    t = nps.vtk_to_numpy(poly.GetPointData().GetArray("time"))
    assert t.max() == pytest.approx(1.5)
    first = xyz[: len(xyz) // 2]
    assert np.ptp(np.linalg.norm(first[:, :2] - 0.05, axis=1)) < 0.002  # ... and each stays on its circle


def test_multi_region_append_keeps_fields_the_solids_lack():
    """A section through a fluid (with U) and a solid (without): U survives, NaN (no data) on the solid."""
    def square(x0, with_u):
        s = vtk.vtkPlaneSource()
        s.SetOrigin(x0, 0, 0)
        s.SetPoint1(x0 + 1, 0, 0)
        s.SetPoint2(x0, 1, 0)
        s.Update()
        p = s.GetOutput()
        if with_u:
            for data, n in ((p.GetCellData(), p.GetNumberOfCells()), (p.GetPointData(), p.GetNumberOfPoints())):
                a = nps.numpy_to_vtk(np.ones((n, 3), dtype=np.float32), deep=True)
                a.SetName("U")
                data.AddArray(a)
        return p

    out = pl._append([square(0, True), square(2, False)])
    u = nps.vtk_to_numpy(out.GetCellData().GetArray("U"))
    assert out.GetPointData().GetArray("U") is not None and u.shape == (2, 3)
    assert np.isfinite(u[0]).all() and np.isnan(u[1]).all()
    assert out.GetCellData().GetArray("U").GetRange(-1) == (pytest.approx(3 ** 0.5), pytest.approx(3 ** 0.5))
