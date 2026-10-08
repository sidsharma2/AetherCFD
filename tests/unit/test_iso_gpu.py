"""GPU/CPU isosurfaces, gradients and Q-criterion against exact answers on a box mesh."""

from __future__ import annotations

import math

import numpy as np
import pytest
from test_mesh_quality import _box_mesh

from aethercfd.core.gpu import array_module, to_numpy
from aethercfd.results.iso import (
    build_tetmesh,
    isosurface,
    q_criterion,
    velocity_gradient,
    vorticity,
)

BACKENDS = ["cpu"] + (["cuda"] if array_module("auto")[1] == "cuda" else [])


@pytest.fixture(scope="module")
def tm():
    return build_tetmesh(_box_mesh(24, 24, 24))


def _area(s) -> float:
    p, t = s.points.astype(np.float64), s.triangles
    return float(0.5 * np.linalg.norm(np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]]), axis=1).sum())


def test_tet_decomposition_volume(tm):
    # 12 tets per hex (6 faces x 2 fan triangles); their volumes add up to the box
    assert tm.tets.shape[0] == 12 * 24**3
    x = tm.xyz[tm.tets.astype(np.int64)]
    vol = np.abs(np.einsum("ij,ij->i", x[:, 1] - x[:, 0], np.cross(x[:, 2] - x[:, 0], x[:, 3] - x[:, 0]))) / 6
    assert vol.sum() == pytest.approx(1.0, rel=1e-9)


@pytest.mark.parametrize("backend", BACKENDS)
def test_sphere_isosurface(tm, backend):
    c = tm.xyz[tm.n_points:]
    dist = np.linalg.norm(c - 0.5, axis=1)
    s = isosurface(tm, dist, 0.3, color_cells=c[:, 0], backend=backend)
    assert s.backend == backend and len(s.triangles) > 1000
    assert _area(s) == pytest.approx(4 * math.pi * 0.3**2, rel=0.04)
    r = np.linalg.norm(s.points - 0.5, axis=1)
    assert np.abs(r - 0.3).max() < 0.03
    outward = np.einsum("ij,ij->i", s.normals, s.points - 0.5) > 0  # oriented towards larger distance
    assert outward.mean() > 0.99
    assert s.scalars is not None and s.scalars.min() >= 0.15 and s.scalars.max() <= 0.85
    # shared vertices: a closed surface has about twice as many triangles as vertices
    assert 1.8 < len(s.triangles) / len(s.points) < 2.2


@pytest.mark.parametrize("backend", BACKENDS)
def test_solid_body_rotation_q_and_vorticity(tm, backend):
    xp, _ = array_module(backend)
    c = tm.xyz[tm.n_points:]
    om = 3.0
    U = np.stack([-om * (c[:, 1] - 0.5), om * (c[:, 0] - 0.5), np.zeros(len(c))], 1)
    L = velocity_gradient(tm, xp.asarray(U), xp)
    q = to_numpy(q_criterion(L, xp))
    w = to_numpy(vorticity(L, xp))
    interior = np.all((c > 1.5 / 24) & (c < 1 - 1.5 / 24), axis=1)  # Green-Gauss is exact away from walls
    assert np.allclose(q[interior], om**2, rtol=1e-9)
    assert np.allclose(w[interior], [0, 0, 2 * om], atol=1e-9)


def test_cpu_and_gpu_agree(tm):
    if "cuda" not in BACKENDS:
        pytest.skip("no CUDA device")
    c = tm.xyz[tm.n_points:]
    f = np.sin(6 * c[:, 0]) * np.cos(5 * c[:, 1]) + c[:, 2]
    a = isosurface(tm, f, 0.4, backend="cpu")
    b = isosurface(tm, f, 0.4, backend="cuda")
    assert len(a.triangles) == len(b.triangles) and len(a.points) == len(b.points)
    assert _area(a) == pytest.approx(_area(b), rel=1e-6)
