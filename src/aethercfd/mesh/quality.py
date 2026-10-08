"""Mesh quality per face and per cell, with OpenFOAM's own definitions (primitiveMeshTools).

Face centres/areas use OpenFOAM's triangle decomposition about the face's point average; cell centres
and volumes use its pyramid decomposition about the face-centre average. On top of the scalar maxima
checkMesh prints, this gives *where*: every cell's worst value and connected hotspot clusters (bad cells
that touch), each with location, size and the metric that makes it bad -- what a mesher (or the copilot)
needs to fix the right block or refinement region.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from aethercfd.mesh.polymesh import PolyMesh

VSMALL = 1e-300
ROOTVSMALL = 1e-150


@dataclass
class MeshGeometry:
    face_centres: np.ndarray
    face_areas: np.ndarray  # area vectors
    cell_centres: np.ndarray
    cell_volumes: np.ndarray


@dataclass
class QualityReport:
    n_cells: int
    n_faces: int
    n_internal: int
    face_non_ortho: np.ndarray  # degrees, internal faces
    face_skewness: np.ndarray  # all faces (boundary skewness for boundary faces)
    face_weight: np.ndarray  # internal: min(w, 1-w)
    face_volume_ratio: np.ndarray  # internal: min/max of owner/neighbour volumes
    cell_aspect_ratio: np.ndarray
    cell_volume: np.ndarray
    cell_non_ortho: np.ndarray  # worst face non-orthogonality per cell
    cell_skewness: np.ndarray
    cell_volume_ratio: np.ndarray  # worst neighbour volume ratio per cell (small = size jump)
    geometry: MeshGeometry
    summary: dict = field(default_factory=dict)

    def cell_fields(self) -> dict[str, np.ndarray]:
        return {"nonOrthogonality": self.cell_non_ortho, "skewness": self.cell_skewness,
                "aspectRatio": self.cell_aspect_ratio, "volumeRatio": self.cell_volume_ratio,
                "cellVolume": self.cell_volume}


# ============================================================================ geometry
def geometry(mesh: PolyMesh) -> MeshGeometry:
    pts = mesh.points
    faces = mesh.padded_faces()  # (nF, m)
    sizes = mesh.face_sizes
    nf, m = faces.shape
    fp = pts[faces]  # (nF, m, 3)
    valid = np.arange(m)[None, :] < sizes[:, None]
    pavg = (fp * valid[..., None]).sum(1) / sizes[:, None]
    nxt = np.roll(faces, -1, axis=1)
    # wrap: the point after the last valid one is the first
    last = sizes - 1
    nxt[np.arange(nf), last] = faces[:, 0]
    fp_next = pts[nxt]
    tri_c = fp + fp_next + pavg[:, None, :]  # 3 * centroid
    tri_n = np.cross(fp_next - fp, pavg[:, None, :] - fp)  # 2 * area vector
    tri_n *= valid[..., None]
    sum_n = tri_n.sum(1)
    sum_n_hat = sum_n / (np.linalg.norm(sum_n, axis=1, keepdims=True) + ROOTVSMALL)
    a = np.einsum("fmk,fk->fm", tri_n, sum_n_hat)  # projected areas (OpenFOAM >= v1712)
    sum_a = a.sum(1)
    ctr = np.einsum("fm,fmk->fk", a, tri_c) / (3.0 * sum_a[:, None] + VSMALL)
    tri = sizes == 3
    ctr[tri] = pavg[tri]
    sf = 0.5 * sum_n
    # cells
    nc = mesh.n_cells
    own, nei = mesh.owner, mesh.neighbour
    ni = mesh.n_internal
    nfc = np.bincount(own, minlength=nc) + np.bincount(nei, minlength=nc)
    c_est = np.zeros((nc, 3))
    np.add.at(c_est, own, ctr)
    np.add.at(c_est, nei, ctr[:ni])
    c_est /= nfc[:, None]
    pyr_own = np.einsum("fk,fk->f", sf, ctr - c_est[own])
    pyr_nei = np.einsum("fk,fk->f", sf[:ni], c_est[nei] - ctr[:ni])
    pc_own = 0.75 * ctr + 0.25 * c_est[own]
    pc_nei = 0.75 * ctr[:ni] + 0.25 * c_est[nei]
    vol = np.bincount(own, pyr_own, minlength=nc) + np.bincount(nei, pyr_nei, minlength=nc)
    cc = np.zeros((nc, 3))
    np.add.at(cc, own, pyr_own[:, None] * pc_own)
    np.add.at(cc, nei, pyr_nei[:, None] * pc_nei)
    good = np.abs(vol) > VSMALL
    cc[good] /= vol[good, None]
    cc[~good] = c_est[~good]
    return MeshGeometry(ctr, sf, cc, vol / 3.0)


# ============================================================================ metrics
def solution_directions(mesh: PolyMesh, sf: np.ndarray) -> np.ndarray:
    """Boolean mask of the directions OpenFOAM solves in: an empty/wedge patch removes the axis its
    faces mostly point along (like polyMesh::solutionD)."""
    sol = np.ones(3, bool)
    for p in mesh.patches:
        if p.get("type") in ("empty", "wedge") and p.get("nFaces"):
            s, n = p["startFace"], p["nFaces"]
            share = np.abs(sf[s:s + n]).sum(0)
            sol[int(np.argmax(share))] = False
    return sol


def quality(mesh: PolyMesh, geo: MeshGeometry | None = None) -> QualityReport:
    geo = geo or geometry(mesh)
    own, nei, ni = mesh.owner, mesh.neighbour, mesh.n_internal
    nc = mesh.n_cells
    cf, sf, cc, vol = geo.face_centres, geo.face_areas, geo.cell_centres, geo.cell_volumes
    mag_sf = np.linalg.norm(sf, axis=1)
    # non-orthogonality
    d = cc[nei] - cc[own[:ni]]
    cosang = np.einsum("fk,fk->f", d, sf[:ni]) / (np.linalg.norm(d, axis=1) * mag_sf[:ni] + VSMALL)
    non_ortho = np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0)))
    # skewness (internal + boundary), OpenFOAM primitiveMeshTools::faceSkewness / boundaryFaceSkewness
    faces = mesh.padded_faces()
    cpf = cf - cc[own]
    dd = np.empty_like(cf)
    dd[:ni] = d
    nhat_b = sf[ni:] / (mag_sf[ni:, None] + VSMALL)
    dd[ni:] = nhat_b * np.einsum("fk,fk->f", nhat_b, cpf[ni:])[:, None]
    sv = cpf - (np.einsum("fk,fk->f", sf, cpf) / (np.einsum("fk,fk->f", sf, dd) + ROOTVSMALL))[:, None] * dd
    msv = np.linalg.norm(sv, axis=1)
    svhat = sv / (msv[:, None] + ROOTVSMALL)
    fd = 0.2 * np.linalg.norm(dd, axis=1) + ROOTVSMALL
    proj = np.abs(np.einsum("fmk,fk->fm", mesh.points[faces] - cf[:, None, :], svhat)).max(1)
    skew = msv / np.maximum(fd, proj)
    # weights and volume ratio
    d_own = np.abs(np.einsum("fk,fk->f", sf[:ni], cf[:ni] - cc[own[:ni]]))
    d_nei = np.abs(np.einsum("fk,fk->f", sf[:ni], cc[nei] - cf[:ni]))
    w = d_nei / (d_own + d_nei + VSMALL)
    weight = np.minimum(w, 1.0 - w)
    va, vb = np.abs(vol[own[:ni]]), np.abs(vol[nei])
    vratio = np.minimum(va, vb) / (np.maximum(va, vb) + VSMALL)
    # aspect ratio (primitiveMeshTools::cellClosedness): only over solution directions (2D/wedge aware)
    msum = np.zeros((nc, 3))
    np.add.at(msum, own, np.abs(sf))
    np.add.at(msum, nei, np.abs(sf[:ni]))
    sol = solution_directions(mesh, sf)
    ar = msum[:, sol].max(1) / (msum[:, sol].min(1) + ROOTVSMALL)
    if sol.sum() == 3:
        v = np.maximum(np.abs(vol), ROOTVSMALL)
        ar = np.maximum(ar, msum.sum(1) / 6.0 / v ** (2.0 / 3.0))
    # per-cell worst values
    c_no = np.zeros(nc)
    np.maximum.at(c_no, own[:ni], non_ortho)
    np.maximum.at(c_no, nei, non_ortho)
    c_sk = np.zeros(nc)
    np.maximum.at(c_sk, own, skew)
    np.maximum.at(c_sk, nei, skew[:ni])
    c_vr = np.ones(nc)
    np.minimum.at(c_vr, own[:ni], vratio)
    np.minimum.at(c_vr, nei, vratio)
    rep = QualityReport(nc, mesh.n_faces, ni, non_ortho, skew, weight, vratio, ar, vol, c_no, c_sk, c_vr, geo)
    rep.summary = {
        "cells": nc, "faces": mesh.n_faces, "internal_faces": ni,
        # checkMesh's "average" is acos of the mean cosine, not the mean angle
        "non_ortho_max": float(non_ortho.max(initial=0)),
        "non_ortho_avg": float(np.degrees(np.arccos(np.clip(cosang.mean(), -1, 1)))) if ni else 0.0,
        "faces_non_ortho_gt_65": int((non_ortho > 65).sum()), "faces_non_ortho_gt_70": int((non_ortho > 70).sum()),
        "skewness_max": float(skew.max(initial=0)), "aspect_ratio_max": float(ar.max(initial=0)),
        "min_volume": float(vol.min()) if vol.size else 0.0, "negative_volumes": int((vol <= 0).sum()),
        "min_face_weight": float(weight.min(initial=0.5)), "min_volume_ratio": float(vratio.min(initial=1)),
        "faces_volume_ratio_lt_0p2": int((vratio < 0.2).sum()),
    }
    return rep


# ============================================================================ hotspots
@dataclass
class Hotspot:
    metric: str
    cells: int
    worst: float
    centroid: tuple[float, float, float]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]


DEFAULT_LIMITS = {"nonOrthogonality": (">", 65.0), "skewness": (">", 4.0), "aspectRatio": (">", 40.0),
                  "volumeRatio": ("<", 0.2)}


def _components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Connected-component labels of an undirected graph with n nodes and edges (a, b)."""
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        g = coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n))
        return connected_components(g, directed=False)[1]
    except ImportError:  # label propagation fallback
        lab = np.arange(n)
        while True:
            m = np.minimum(lab[a], lab[b])
            new = lab.copy()
            np.minimum.at(new, a, m)
            np.minimum.at(new, b, m)
            new = new[new]
            if np.array_equal(new, lab):
                return lab
            lab = new


def hotspots(mesh: PolyMesh, rep: QualityReport, limits: dict | None = None, *, top: int = 8) -> dict[str, list]:
    """Clusters of face-connected bad cells per metric, largest/worst first."""
    limits = limits or DEFAULT_LIMITS
    fields = rep.cell_fields()
    cc = rep.geometry.cell_centres
    own, nei, ni = mesh.owner[: mesh.n_internal], mesh.neighbour, mesh.n_internal
    out: dict[str, list] = {}
    for name, (op, lim) in limits.items():
        vals = fields[name]
        bad = vals > lim if op == ">" else vals < lim
        idx = np.flatnonzero(bad)
        if idx.size == 0:
            out[name] = []
            continue
        local = -np.ones(rep.n_cells, np.int64)
        local[idx] = np.arange(idx.size)
        both = bad[own[:ni]] & bad[nei]
        lab = _components(idx.size, local[own[:ni]][both], local[nei][both])
        clusters = []
        for c in np.unique(lab):
            members = idx[lab == c]
            v = vals[members]
            p = cc[members]
            clusters.append(Hotspot(name, int(members.size), float(v.max() if op == ">" else v.min()),
                                    tuple(np.round(p.mean(0), 9)), tuple(np.round(p.min(0), 9)),
                                    tuple(np.round(p.max(0), 9))))
        clusters.sort(key=lambda h: (-h.cells, -abs(h.worst)))
        out[name] = clusters[:top] + ([] if len(clusters) <= top else
                                      [Hotspot(name, sum(h.cells for h in clusters[top:]), float("nan"),
                                               (np.nan,) * 3, (np.nan,) * 3, (np.nan,) * 3)])
    return out


def histogram(values: np.ndarray, edges: list[float]) -> list[dict]:
    counts, _ = np.histogram(values, bins=[*edges, np.inf])
    return [{"from": e, "count": int(c)} for e, c in zip(edges, counts, strict=True)]
