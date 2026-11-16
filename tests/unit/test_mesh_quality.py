"""AetherMesh polyMesh reader and quality metrics (OpenFOAM definitions)."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from aethercfd.mesh.polymesh import PolyMesh, read_polymesh
from aethercfd.mesh.quality import geometry, hotspots, quality

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity" / "constant" / "polyMesh"
HDR = ("FoamFile\n{{\n    version 2.0;\n    format {fmt};\n    arch \"LSB;label=32;scalar=64\";\n"
       "    class {cls};\n    object {obj};\n}}\n// * * * //\n\n")


def test_cavity_is_perfect():
    m = read_polymesh(FIX)
    assert m.n_cells == 400 and m.n_faces == 1640 and m.n_internal == 760
    r = quality(m)
    s = r.summary
    assert s["non_ortho_max"] < 1e-6 and s["skewness_max"] < 1e-6
    assert abs(r.cell_volume.sum() - 0.1 * 0.1 * 0.01) < 1e-12  # cavity: 0.1 x 0.1 x 0.01 m
    assert np.allclose(s["min_face_weight"], 0.5) and np.allclose(s["min_volume_ratio"], 1.0)
    assert abs(s["aspect_ratio_max"] - 1.0) < 1e-9  # 2D (empty in z): only x/y count, like checkMesh
    assert all(not v for v in hotspots(m, r).values())


def _box_mesh(nx=4, ny=3, nz=2, warp=0.0) -> PolyMesh:
    """Structured hex box with OpenFOAM face ordering (internal faces first, upper-triangular)."""
    xs, ys, zs = np.linspace(0, 1, nx + 1), np.linspace(0, 1, ny + 1), np.linspace(0, 1, nz + 1)
    P = np.array([[x, y, z] for z in zs for y in ys for x in xs])
    if warp:
        P[:, 0] += warp * np.sin(np.pi * P[:, 1]) * P[:, 2]
    pid = lambda i, j, k: i + j * (nx + 1) + k * (nx + 1) * (ny + 1)  # noqa: E731
    cid = lambda i, j, k: i + j * nx + k * nx * ny  # noqa: E731
    internal, boundary = [], []
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                c = cid(i, j, k)
                quads = {  # outward from cell c
                    "x+": [pid(i + 1, j, k), pid(i + 1, j + 1, k), pid(i + 1, j + 1, k + 1), pid(i + 1, j, k + 1)],
                    "y+": [pid(i, j + 1, k), pid(i, j + 1, k + 1), pid(i + 1, j + 1, k + 1), pid(i + 1, j + 1, k)],
                    "z+": [pid(i, j, k + 1), pid(i + 1, j, k + 1), pid(i + 1, j + 1, k + 1), pid(i, j + 1, k + 1)],
                    "x-": [pid(i, j, k), pid(i, j, k + 1), pid(i, j + 1, k + 1), pid(i, j + 1, k)],
                    "y-": [pid(i, j, k), pid(i + 1, j, k), pid(i + 1, j, k + 1), pid(i, j, k + 1)],
                    "z-": [pid(i, j, k), pid(i, j + 1, k), pid(i + 1, j + 1, k), pid(i + 1, j, k)],
                }
                for d, (nb, ok) in {"x+": (cid(i + 1, j, k), i + 1 < nx), "y+": (cid(i, j + 1, k), j + 1 < ny),
                                    "z+": (cid(i, j, k + 1), k + 1 < nz)}.items():
                    (internal if ok else boundary).append((c, nb if ok else -1, quads[d]))
                for d, ok in (("x-", i == 0), ("y-", j == 0), ("z-", k == 0)):
                    if ok:
                        boundary.append((c, -1, quads[d]))
    internal.sort(key=lambda t: (t[0], t[1]))
    faces = [f for _, _, f in internal] + [f for _, _, f in boundary]
    owner = np.array([o for o, _, _ in internal] + [o for o, _, _ in boundary])
    nei = np.array([n for _, n, _ in internal])
    flat = np.array([p for f in faces for p in f])
    return PolyMesh(P, np.arange(len(faces) + 1) * 4, flat, owner, nei)


def _write(mesh: PolyMesh, d: Path, *, binary: bool) -> None:
    d.mkdir(parents=True, exist_ok=True)
    fmt = "binary" if binary else "ascii"

    def put(name, cls, n, payload: bytes):
        (d / name).write_bytes(HDR.format(fmt=fmt, cls=cls, obj=name).encode() + f"{n}\n(".encode() + payload
                               + b")\n")

    if binary:
        put("points", "vectorField", len(mesh.points), mesh.points.astype("<f8").tobytes())
        put("owner", "labelList", len(mesh.owner), mesh.owner.astype("<i4").tobytes())
        put("neighbour", "labelList", len(mesh.neighbour), mesh.neighbour.astype("<i4").tobytes())
        off = mesh.face_offsets.astype("<i4")
        lab = mesh.face_points.astype("<i4")
        (d / "faces").write_bytes(HDR.format(fmt=fmt, cls="faceCompactList", obj="faces").encode()
                                  + f"{len(off)}\n(".encode() + off.tobytes() + b")\n"
                                  + f"{len(lab)}\n(".encode() + lab.tobytes() + b")\n")
    else:
        put("points", "vectorField", len(mesh.points),
            "\n".join(f"({x:.17g} {y:.17g} {z:.17g})" for x, y, z in mesh.points).encode())
        put("owner", "labelList", len(mesh.owner), "\n".join(map(str, mesh.owner)).encode())
        put("neighbour", "labelList", len(mesh.neighbour), "\n".join(map(str, mesh.neighbour)).encode())
        sizes = np.diff(mesh.face_offsets)
        txt = "\n".join(f"{s}({' '.join(map(str, mesh.face_points[o:o + s]))})"
                        for o, s in zip(mesh.face_offsets[:-1], sizes, strict=True))
        put("faces", "faceList", len(sizes), txt.encode())


def test_ascii_and_binary_roundtrip_and_warped_metrics(tmp_path):
    m = _box_mesh(warp=0.35)
    for binary in (False, True):
        d = tmp_path / ("bin" if binary else "asc")
        _write(m, d, binary=binary)
        r = read_polymesh(d)
        assert np.allclose(r.points, m.points) and np.array_equal(r.owner, m.owner)
        assert np.array_equal(r.face_points, m.face_points) and np.array_equal(r.neighbour, m.neighbour)
    g = geometry(m)
    assert abs(g.cell_volumes.sum() - 1.0) < 1e-9  # shear warp keeps the volume of the unit box
    q = quality(m, g)
    assert q.summary["non_ortho_max"] > 5 and q.summary["negative_volumes"] == 0


def test_hotspots_cluster_connected_cells(tmp_path):
    m = _box_mesh(nx=6, ny=1, nz=1)
    r = quality(m)
    r.cell_aspect_ratio[:] = 1.0
    r.cell_aspect_ratio[[1, 2, 4]] = 50.0  # two clusters: {1,2} and {4}
    hs = hotspots(m, r, {"aspectRatio": (">", 40.0)})["aspectRatio"]
    assert sorted(h.cells for h in hs) == [1, 2]


def test_fixture_copy_with_gz(tmp_path):
    import gzip

    d = tmp_path / "polyMesh"
    shutil.copytree(FIX, d)
    pts = d / "points"
    (d / "points.gz").write_bytes(gzip.compress(pts.read_bytes()))
    pts.unlink()
    assert read_polymesh(d).points.shape == (882, 3)


def test_a_case_without_a_mesh_says_to_run_the_mesher(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError, match=r"no mesh yet.*run blockMesh"):
        read_polymesh(tmp_path / "constant" / "polyMesh")
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="has no points file"):
        read_polymesh(tmp_path / "constant" / "polyMesh")
