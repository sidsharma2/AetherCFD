"""snappyHexMesh setup generator: valid dictionaries for both forks, external and internal flow."""

from __future__ import annotations

import pytest
import vtk

from aethercfd.foamdict.parser import parse
from aethercfd.foamdict.semantic import DictView
from aethercfd.mesh.geometry import analyse
from aethercfd.mesh.snappy import minimal_system, snappy_setup, stl_solids
from aethercfd.mesh.strategy import plan


def _sphere(path, r=0.05):
    s = vtk.vtkSphereSource()
    s.SetRadius(r)
    s.SetThetaResolution(48)
    s.SetPhiResolution(24)
    w = vtk.vtkSTLWriter()
    w.SetInputConnection(s.GetOutputPort())
    w.SetFileName(str(path))
    w.Write()
    return path


def _pipe_with_regions(path):
    """Closed cylinder as an ASCII STL with three solids: inlet cap, outlet cap, wall."""
    import math

    n, r, L = 32, 0.01, 0.1
    ring = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]

    def facet(a, b, c):
        return (f"  facet normal 0 0 0\n    outer loop\n      vertex {a[0]} {a[1]} {a[2]}\n"
                f"      vertex {b[0]} {b[1]} {b[2]}\n      vertex {c[0]} {c[1]} {c[2]}\n    endloop\n  endfacet\n")

    wall = "".join(facet((*ring[i], 0), (*ring[(i + 1) % n], 0), (*ring[(i + 1) % n], L))
                   + facet((*ring[i], 0), (*ring[(i + 1) % n], L), (*ring[i], L)) for i in range(n))
    inlet = "".join(facet((0, 0, 0), (*ring[(i + 1) % n], 0), (*ring[i], 0)) for i in range(n))
    outlet = "".join(facet((0, 0, L), (*ring[i], L), (*ring[(i + 1) % n], L)) for i in range(n))
    path.write_text(f"solid inlet\n{inlet}endsolid inlet\nsolid outlet\n{outlet}endsolid outlet\n"
                    f"solid wall\n{wall}endsolid wall\n")
    return path


@pytest.mark.parametrize("fork", ["esi", "foundation"])
def test_external_sphere(tmp_path, fork):
    stl = _sphere(tmp_path / "ball.stl")
    geo = analyse(stl)
    best = next(p for p in plan(geo) if p.mesher.startswith("snappyHexMesh"))
    s = snappy_setup(stl, geo, fork_family=fork, strategy_settings=best.settings)
    for rel, text in s.files.items():
        doc = parse(text)
        assert doc.ok, (rel, doc.diagnostics[:3])
    v = DictView(parse(s.files["system/snappyHexMeshDict"]))
    keep = "locationInMesh" if fork == "esi" else "insidePoint"
    assert v.get_text(f"castellatedMeshControls/{keep}") is not None
    geo_key = "ball.stl" if fork == "esi" else "ball"
    assert v.get_text(f"geometry/{geo_key}/type").strip() == ("triSurfaceMesh" if fork == "esi" else "triSurface")
    bm = DictView(parse(s.files["system/blockMeshDict"]))
    assert bm.get_text("inlet/type") is None  # patches are in the boundary list, not top-level
    assert [st[0] for st in s.steps] == ["blockMesh", "surfaceFeatureExtract" if fork == "esi" else
                                         "surfaceFeatures", "snappyHexMesh", "checkMesh"]
    assert s.copy_surface[1] == ("constant/triSurface/ball.stl" if fork == "esi" else "constant/geometry/ball.stl")
    # the kept point is in the domain and outside the sphere
    p = s.summary[keep]
    assert all(lo < x < hi for x, lo, hi in zip(p, s.summary["domain_min"], s.summary["domain_max"], strict=True))
    assert sum(x * x for x in p) ** 0.5 > 0.05
    assert all(parse(t).ok for t in minimal_system(fork).values())


def test_internal_pipe_regions(tmp_path):
    stl = _pipe_with_regions(tmp_path / "pipe.stl")
    assert stl_solids(stl) == ["inlet", "outlet", "wall"]
    geo = analyse(stl)
    s = snappy_setup(stl, geo, flow="internal", background_cell=0.002, surface_levels=(1, 2), first_layer=1e-4)
    txt = s.files["system/snappyHexMeshDict"]
    assert parse(txt).ok
    v = DictView(parse(txt))
    regions = "castellatedMeshControls/refinementSurfaces/pipe/regions"
    assert v.get_text(f"{regions}/inlet/patchInfo/type").strip() == "patch"
    assert v.get_text(f"{regions}/wall/patchInfo/type").strip() == "wall"
    assert v.get_text("addLayersControls/firstLayerThickness").strip() == "0.0001"
    assert '"pipe_wall"' in txt  # layers only on the wall region
    x, y, z = s.summary["locationInMesh"]
    assert (x * x + y * y) ** 0.5 < 0.01 and 0 < z < 0.1
