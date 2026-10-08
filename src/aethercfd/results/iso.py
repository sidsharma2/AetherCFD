"""Isosurfaces, velocity gradients, Q-criterion and vorticity on the real OpenFOAM mesh -- on the GPU.

Every cell (hex, prism, tet or snappy polyhedron) is split into tetrahedra around its centre: each face
is fanned into triangles and joined to the cell centre (OpenFOAM's own tet decomposition idea). Marching
tetrahedra on that decomposition contours *any* polyhedral mesh exactly, with values at mesh points
(averaged from the surrounding cells) and at cell centres (the cell value itself). Shared vertices are
merged by edge key, triangles are oriented along the field gradient, and smooth vertex normals are
accumulated, giving clean, lit surfaces.

Gradients use Green-Gauss on the finite-volume faces (linear face interpolation; boundary faces take the
owner value), so Q = 1/2 (|Omega|^2 - |S|^2) and vorticity come from the same discretisation as the mesh.

All of it is written against an array module ``xp``: CuPy on the NVIDIA GPU, numpy otherwise
(aethercfd.core.gpu). Results are interpolated (labelled so in the viewport).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from aethercfd.core.gpu import array_module, segment_sum, to_numpy
from aethercfd.mesh.polymesh import PolyMesh, read_polymesh
from aethercfd.mesh.quality import MeshGeometry, geometry

# tet edges and the marching-tetrahedra table (vertex i inside <=> bit i of the case)
EDGES = np.array([[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]], dtype=np.int64)
_TRI = {1: [(0, 1, 2)], 2: [(0, 3, 4)], 4: [(1, 3, 5)], 8: [(2, 4, 5)],
        3: [(1, 3, 4), (1, 4, 2)], 5: [(0, 3, 5), (0, 5, 2)], 6: [(0, 1, 5), (0, 5, 4)]}
TABLE = -np.ones((16, 2, 3), dtype=np.int64)
for _c, _tris in list(_TRI.items()):
    for _k, _t in enumerate(_tris):
        TABLE[_c, _k] = _t
        TABLE[15 - _c, _k] = _t  # the complement has the same crossing edges
NTRI = (TABLE[:, :, 0] >= 0).sum(1)


@dataclass
class TetMesh:
    """Cell-centred tet decomposition of a polyMesh (numpy on the host; ``device`` copies cached)."""

    n_points: int
    n_cells: int
    xyz: np.ndarray  # (nP + nC, 3): mesh points then cell centres
    tets: np.ndarray  # (nT, 4) int32: [cell-centre id, a, b, c]
    inc_point: np.ndarray  # point-cell incidence pairs (unique) for point averaging
    inc_cell: np.ndarray
    owner: np.ndarray
    neighbour: np.ndarray
    face_w: np.ndarray  # linear interpolation weight of the owner value at internal faces
    sf: np.ndarray  # face area vectors
    volumes: np.ndarray
    build_seconds: float = 0.0
    device: dict = field(default_factory=dict)  # xp name -> {array name: device array}

    def on(self, xp, name: str):
        """The arrays on the chosen backend (copied to the GPU once, then reused)."""
        if xp is np:
            return {k: getattr(self, k) for k in ("xyz", "tets", "inc_point", "inc_cell", "owner", "neighbour",
                                                  "face_w", "sf", "volumes")}
        if name not in self.device:
            self.device[name] = {k: xp.asarray(getattr(self, k)) for k in
                                 ("xyz", "tets", "inc_point", "inc_cell", "owner", "neighbour", "face_w", "sf",
                                  "volumes")}
        return self.device[name]


def build_tetmesh(mesh: PolyMesh, geo: MeshGeometry | None = None) -> TetMesh:
    t0 = time.perf_counter()
    geo = geo or geometry(mesh)
    nP, nC, ni = len(mesh.points), mesh.n_cells, mesh.n_internal
    faces = mesh.padded_faces()
    sizes = mesh.face_sizes
    own, nei = mesh.owner, mesh.neighbour
    parts = []
    for k in range(1, int(sizes.max()) - 1):  # fan triangle (p0, pk, pk+1) for faces with > k+1 points
        f = np.flatnonzero(sizes > k + 1)
        a, b, c = faces[f, 0], faces[f, k], faces[f, k + 1]
        parts.append(np.stack([nP + own[f], a, b, c], 1))
        internal = f[f < ni]
        parts.append(np.stack([nP + nei[internal], faces[internal, 0], faces[internal, k + 1],
                               faces[internal, k]], 1))
    tets = np.concatenate(parts).astype(np.int32)
    # point-cell incidence (each pair once) for averaging cell values to points
    fp = mesh.face_points
    fid = np.repeat(np.arange(mesh.n_faces), sizes)
    keys = [fp.astype(np.int64) * nC + own[fid]]
    m = fid < ni
    keys.append(fp[m].astype(np.int64) * nC + nei[fid[m]])
    uk = np.unique(np.concatenate(keys))
    d_own = np.abs(np.einsum("fk,fk->f", geo.face_areas[:ni], geo.face_centres[:ni] - geo.cell_centres[own[:ni]]))
    d_nei = np.abs(np.einsum("fk,fk->f", geo.face_areas[:ni], geo.cell_centres[nei] - geo.face_centres[:ni]))
    w = d_nei / (d_own + d_nei + 1e-300)
    return TetMesh(nP, nC, np.concatenate([mesh.points, geo.cell_centres]).astype(np.float64), tets,
                   (uk // nC).astype(np.int32), (uk % nC).astype(np.int32), own.astype(np.int32),
                   nei.astype(np.int32), w, geo.face_areas, geo.cell_volumes, time.perf_counter() - t0)


_CACHE: dict[tuple[str, float], TetMesh] = {}


def tetmesh_for(mesh_dir: Path) -> TetMesh:
    """Cached per mesh (keyed by the points file's mtime)."""
    pts = mesh_dir / "points" if (mesh_dir / "points").exists() else mesh_dir / "points.gz"
    key = (str(mesh_dir), pts.stat().st_mtime)
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = build_tetmesh(read_polymesh(mesh_dir))
    return _CACHE[key]


# ============================================================================ fields
def point_values(tm: TetMesh, cell_vals, xp):
    """Average of the surrounding cells' values at every mesh point (scalar or vector)."""
    d = tm.on(xp, "dev")
    cnt = xp.bincount(d["inc_point"], minlength=tm.n_points).astype(cell_vals.dtype)
    s = segment_sum(xp, d["inc_point"], cell_vals[d["inc_cell"]], tm.n_points)
    return s / (cnt[:, None] if s.ndim == 2 else cnt).clip(min=1)


def velocity_gradient(tm: TetMesh, U, xp):
    """L[c, i, j] = dU_i/dx_j per cell (Green-Gauss, linear face interpolation, zero-gradient boundaries)."""
    d = tm.on(xp, "dev")
    own, nei = d["owner"], d["neighbour"]
    ni = nei.shape[0]
    Uf = U[own].copy()
    w = d["face_w"][:, None]
    Uf[:ni] = w * U[own[:ni]] + (1.0 - w) * U[nei]
    flux = (d["sf"][:, :, None] * Uf[:, None, :]).reshape(-1, 9)  # G[j, i] = Sf_j U_i
    g = segment_sum(xp, own, flux, tm.n_cells) - segment_sum(xp, nei, flux[:ni], tm.n_cells)
    g = g.reshape(-1, 3, 3) / d["volumes"][:, None, None]
    return xp.transpose(g, (0, 2, 1))  # L_ij = dU_i/dx_j


def q_criterion(L, xp):
    S = 0.5 * (L + xp.transpose(L, (0, 2, 1)))
    W = 0.5 * (L - xp.transpose(L, (0, 2, 1)))
    return 0.5 * ((W * W).sum((1, 2)) - (S * S).sum((1, 2)))


def vorticity(L, xp):
    return xp.stack([L[:, 2, 1] - L[:, 1, 2], L[:, 0, 2] - L[:, 2, 0], L[:, 1, 0] - L[:, 0, 1]], 1)


# ============================================================================ marching tetrahedra
@dataclass
class Surface:
    points: np.ndarray  # (nV, 3) float32
    triangles: np.ndarray  # (nT, 3) int32
    normals: np.ndarray  # (nV, 3) float32
    scalars: np.ndarray | None  # (nV,) colour values
    backend: str
    seconds: float
    tets_active: int = 0


def isosurface(tm: TetMesh, cell_scalar, level: float, *, color_cells=None, backend: str = "auto",
               chunk: int = 4_000_000) -> Surface:
    """Contour ``cell_scalar`` (numpy or device array, length nCells) at ``level``; optional ``color_cells``
    (cell values) are interpolated onto the surface for colouring."""
    xp, name = array_module(backend)
    t0 = time.perf_counter()
    d = tm.on(xp, "dev")
    cs = xp.asarray(cell_scalar, dtype=xp.float64)
    vals = xp.concatenate([point_values(tm, cs, xp), cs])
    col = None
    if color_cells is not None:
        cc = xp.asarray(color_cells, dtype=xp.float64)
        col = xp.concatenate([point_values(tm, cc, xp), cc])
    table = xp.asarray(TABLE)
    ntri = xp.asarray(NTRI)
    edges = xp.asarray(EDGES)
    weights = xp.asarray(np.array([1, 2, 4, 8], dtype=np.int64))
    tri_keys, tri_t, tri_lo, tri_hi, tri_dir = [], [], [], [], []
    n_active = 0
    for s in range(0, d["tets"].shape[0], chunk):
        tet = d["tets"][s:s + chunk]
        v = vals[tet]
        case = ((v > level).astype(xp.int64) * weights).sum(1)
        act = xp.flatnonzero(ntri[case] > 0)
        if act.size == 0:
            continue
        n_active += int(act.size)
        tet, v, case = tet[act], v[act], case[act]
        # gradient direction inside each tet (towards larger values) for consistent orientation
        x = tm_xyz = d["xyz"][tet]
        inside = (v > level)
        cin = (x * inside[..., None]).sum(1) / inside.sum(1, keepdims=True)
        cout = (x * (~inside)[..., None]).sum(1) / (~inside).sum(1, keepdims=True)
        direction = cin - cout
        del tm_xyz
        for k in range(2):
            sel = xp.flatnonzero(table[case, k, 0] >= 0)
            if sel.size == 0:
                continue
            e = table[case[sel], k]  # (n, 3) local edge ids
            a = xp.take_along_axis(tet[sel], edges[e][:, :, 0], 1)
            b = xp.take_along_axis(tet[sel], edges[e][:, :, 1], 1)
            va = xp.take_along_axis(v[sel], edges[e][:, :, 0], 1)
            vb = xp.take_along_axis(v[sel], edges[e][:, :, 1], 1)
            lo, hi = xp.minimum(a, b).astype(xp.int64), xp.maximum(a, b).astype(xp.int64)
            t = xp.where(a < b, (level - va) / (vb - va + 1e-300), (level - vb) / (va - vb + 1e-300))
            tri_keys.append(lo * (tm.n_points + tm.n_cells) + hi)
            tri_t.append(xp.clip(t, 0.0, 1.0))
            tri_lo.append(lo)
            tri_hi.append(hi)
            tri_dir.append(direction[sel])
    if not tri_keys:
        return Surface(np.zeros((0, 3), np.float32), np.zeros((0, 3), np.int32), np.zeros((0, 3), np.float32),
                       None, name, time.perf_counter() - t0, n_active)
    keys = xp.concatenate(tri_keys)  # (nTri, 3)
    tt = xp.concatenate(tri_t)
    lo = xp.concatenate(tri_lo)
    hi = xp.concatenate(tri_hi)
    dirs = xp.concatenate(tri_dir)
    uniq, first, inv = xp.unique(keys.ravel(), return_index=True, return_inverse=True)
    tris = inv.reshape(-1, 3)
    lf, hf, tf = lo.ravel()[first], hi.ravel()[first], tt.ravel()[first]
    pts = d["xyz"][lf] + tf[:, None] * (d["xyz"][hf] - d["xyz"][lf])
    scal = None
    if col is not None:
        scal = col[lf] + tf * (col[hf] - col[lf])
    # orient every triangle along the field gradient, then smooth vertex normals (area-weighted)
    p0, p1, p2 = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    n = xp.cross(p1 - p0, p2 - p0)
    flip = (n * dirs).sum(1) < 0
    tris = xp.where(flip[:, None], tris[:, [0, 2, 1]], tris)
    n = xp.where(flip[:, None], -n, n)
    nv = uniq.shape[0]
    vn = segment_sum(xp, tris.ravel(), xp.repeat(n, 3, axis=0), nv)
    vn /= xp.linalg.norm(vn, axis=1, keepdims=True).clip(min=1e-300)
    out = Surface(to_numpy(pts).astype(np.float32), to_numpy(tris).astype(np.int32),
                  to_numpy(vn).astype(np.float32), None if scal is None else to_numpy(scal).astype(np.float32),
                  name, 0.0, n_active)
    out.seconds = time.perf_counter() - t0
    return out


def auto_q_level(q, xp, percentile: float = 99.0) -> float:
    """A Q level that shows coherent vortices: a high percentile of the positive Q values."""
    pos = q[q > 0]
    if pos.size == 0:
        return 0.0
    return float(to_numpy(xp.percentile(pos, percentile)))
