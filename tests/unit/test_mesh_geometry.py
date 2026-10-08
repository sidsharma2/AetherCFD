"""AetherMesh geometry intake and meshing strategy on generated STLs."""

from __future__ import annotations

import pytest

vtk = pytest.importorskip("vtk")

from aethercfd.mesh.geometry import analyse  # noqa: E402
from aethercfd.mesh.strategy import plan  # noqa: E402


def _write(src, path):
    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(src.GetOutputPort())
    w = vtk.vtkSTLWriter()
    w.SetInputConnection(tri.GetOutputPort())
    w.SetFileName(str(path))
    w.Write()
    return path


def test_cube_is_watertight_with_sharp_features(tmp_path):
    c = vtk.vtkCubeSource()
    c.SetXLength(2)
    c.SetYLength(1)
    c.SetZLength(1)
    r = analyse(_write(c, tmp_path / "cube.stl"))
    assert r.watertight and r.volume == pytest.approx(2.0, rel=1e-6) and r.area == pytest.approx(10.0, rel=1e-6)
    assert r.feature_edges == 12 and r.feature_length == pytest.approx(16.0, rel=1e-6)
    assert 10 <= r.feature_angle <= 80


def test_sphere_and_cone_are_axisymmetric(tmp_path):
    s = vtk.vtkSphereSource()
    s.SetThetaResolution(64)
    s.SetPhiResolution(64)
    s.SetRadius(0.5)
    r = analyse(_write(s, tmp_path / "sphere.stl"))
    assert r.shape == "axisymmetric" and r.watertight and r.min_radius == pytest.approx(0.5, rel=0.25)
    cone = vtk.vtkConeSource()
    cone.SetResolution(96)
    cone.SetDirection(0, 0, 1)
    cone.CappingOn()
    rc = analyse(_write(cone, tmp_path / "cone.stl"))
    assert rc.shape == "axisymmetric" and rc.axis == "z"
    assert plan(rc)[0].mesher.startswith("Wedge")


def test_extruded_profile(tmp_path):
    pts = [(0, 0), (2, 0), (2, 0.5), (0.5, 0.5), (0.5, 2), (0, 2)]  # L-shape, extruded along z
    poly = vtk.vtkPolyData()
    p = vtk.vtkPoints()
    for x, y in pts:
        p.InsertNextPoint(x, y, 0)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(len(pts), list(range(len(pts))))
    poly.SetPoints(p)
    poly.SetPolys(cells)
    ex = vtk.vtkLinearExtrusionFilter()
    ex.SetInputData(poly)
    ex.SetExtrusionTypeToVectorExtrusion()
    ex.SetVector(0, 0, 3)
    ex.CappingOn()
    r = analyse(_write(ex, tmp_path / "L.stl"))
    assert r.shape == "extruded" and r.axis == "z" and r.watertight


def test_open_surface_is_flagged(tmp_path):
    pl = vtk.vtkPlaneSource()
    pl.SetResolution(4, 4)
    r = analyse(_write(pl, tmp_path / "plane.stl"))
    assert not r.watertight and r.open_edges > 0 and r.volume is None
    steps = [m.mesher for m in plan(r)]
    assert steps[0].startswith("Surface wrapping")
