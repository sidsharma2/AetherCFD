"""Fast reader for OpenFOAM polyMesh (points, faces, owner, neighbour, boundary) into numpy.

ASCII and binary (``format binary``; labels 32/64 bit, scalars 64 bit per the ``arch`` header), faceList
and faceCompactList. Read-only; never writes. Used for mesh-quality maps and geometry measurements that
must match OpenFOAM's own definitions (checkMesh) without running OpenFOAM.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_HEADER_END = re.compile(rb"^\s*//\s*\*", re.MULTILINE)


@dataclass
class PolyMesh:
    points: np.ndarray  # (nPoints, 3) float64
    face_offsets: np.ndarray  # (nFaces+1,) int64; face i = face_points[off[i]:off[i+1]]
    face_points: np.ndarray  # (sum of face sizes,) int64
    owner: np.ndarray  # (nFaces,) int64
    neighbour: np.ndarray  # (nInternalFaces,) int64
    patches: list[dict] = field(default_factory=list)  # name, type, startFace, nFaces
    path: Path | None = None

    @property
    def n_faces(self) -> int:
        return len(self.owner)

    @property
    def n_internal(self) -> int:
        return len(self.neighbour)

    @property
    def n_cells(self) -> int:
        return int(max(self.owner.max(initial=-1), self.neighbour.max(initial=-1)) + 1)

    @property
    def face_sizes(self) -> np.ndarray:
        return np.diff(self.face_offsets)

    def padded_faces(self) -> np.ndarray:
        """(nFaces, maxSize) point labels, padded by repeating each face's last point."""
        sizes = self.face_sizes
        m = int(sizes.max())
        if np.all(sizes == m):
            return self.face_points.reshape(-1, m)
        idx = self.face_offsets[:-1, None] + np.minimum(np.arange(m)[None, :], sizes[:, None] - 1)
        return self.face_points[idx]


def _read_bytes(p: Path) -> bytes:
    if p.exists():
        return p.read_bytes()
    gz = p.with_name(p.name + ".gz")
    if gz.exists():
        return gzip.decompress(gz.read_bytes())
    if not p.parent.is_dir():
        raise FileNotFoundError(f"no mesh yet ({p.parent} does not exist): run blockMesh or snappyHexMesh first")
    raise FileNotFoundError(f"the mesh in {p.parent} has no {p.name} file: run the mesher again (blockMesh or "
                            "snappyHexMesh), or check that the files were copied completely")


def _header(data: bytes) -> tuple[dict[str, str], int]:
    m = re.search(rb"FoamFile\s*\{(.*?)\}", data[:4096], re.DOTALL)
    hdr: dict[str, str] = {}
    if m:
        for k, v in re.findall(rb"(\w+)\s+([^;]+);", m.group(1)):
            hdr[k.decode()] = v.decode().strip().strip('"')
        body = m.end()
    else:
        body = 0
    return hdr, body


def _sizes(hdr: dict[str, str]) -> tuple[type, type]:
    arch = hdr.get("arch", "")
    lab = np.int64 if "label=64" in arch else np.int32
    sca = np.float32 if "scalar=32" in arch else np.float64
    return lab, sca


def _count_and_open(data: bytes, start: int) -> tuple[int, int]:
    """Position after the '(' of the first top-level 'N (' list, and N."""
    m = re.compile(rb"(\d+)\s*\(", re.DOTALL).search(data, start)
    if m is None:
        raise ValueError("no list found")
    return int(m.group(1)), m.end()


_PARENS = bytes.maketrans(b"()", b"  ")


def _ascii_numbers(chunk: bytes, dtype, *, block: int = 32 * 2**20) -> np.ndarray:
    """Whitespace-separated numbers ("4(1 8 57 50)" -> 4 1 8 57 50). split()+array is ~30x faster than
    np.fromstring(sep=' ') here; done in blocks to bound the temporary list of tokens."""
    parts, i, n = [], 0, len(chunk)
    while i < n:
        j = min(n, i + block)
        while j < n and chunk[j:j + 1] not in b" \n\t\r()":
            j += 1  # never split a number
        parts.append(np.array(chunk[i:j].translate(_PARENS).split(), dtype=dtype))
        i = j
    return np.concatenate(parts) if parts else np.empty(0, dtype)


def _read_list(p: Path, kind: str) -> tuple[np.ndarray, dict]:
    data = _read_bytes(p)
    hdr, body = _header(data)
    lab, sca = _sizes(hdr)
    binary = hdr.get("format", "ascii") == "binary"
    cls = hdr.get("class", "")
    if kind == "faces":
        if cls == "faceCompactList":
            n_off, pos = _count_and_open(data, body)
            if binary:
                offsets = np.frombuffer(data, lab, n_off, pos).astype(np.int64)
                pos += n_off * np.dtype(lab).itemsize
                n_lab, pos = _count_and_open(data, pos + 1)
                labels = np.frombuffer(data, lab, n_lab, pos).astype(np.int64)
            else:
                end = data.index(b")", pos)
                offsets = _ascii_numbers(data[pos:end], np.int64)
                n_lab, pos = _count_and_open(data, end + 1)
                end = data.index(b")", pos)
                labels = _ascii_numbers(data[pos:end], np.int64)
            return np.stack([offsets[:-1], offsets[1:]], 1), {"offsets": offsets, "labels": labels}
        n, pos = _count_and_open(data, body)  # ASCII faceList: N( k(a b c ...) ... )
        end = data.rindex(b")")
        flat = _ascii_numbers(data[pos:end], np.int64)
        k = int(flat[0]) if flat.size else 0
        if flat.size == n * (k + 1) and np.all(flat[:: k + 1] == k):  # all faces the same size (hex/tet)
            labels = flat.reshape(n, k + 1)[:, 1:].reshape(-1)
            offsets = np.arange(n + 1, dtype=np.int64) * k
        else:
            sizes = np.empty(n, np.int64)
            pos_i = 0
            for i in range(n):
                s = int(flat[pos_i])
                sizes[i] = s
                pos_i += s + 1
            offsets = np.concatenate([[0], np.cumsum(sizes)])
            mask = np.ones(flat.size, bool)
            mask[np.concatenate([[0], np.cumsum(sizes + 1)[:-1]])] = False
            labels = flat[mask]
        return np.empty(0), {"offsets": offsets, "labels": labels}
    n, pos = _count_and_open(data, body)
    if kind == "points":
        if binary:
            return np.frombuffer(data, sca, 3 * n, pos).astype(np.float64).reshape(n, 3), hdr
        end = data.rindex(b")")
        return _ascii_numbers(data[pos:end], np.float64).reshape(n, 3), hdr
    if binary:  # labelList
        return np.frombuffer(data, lab, n, pos).astype(np.int64), hdr
    end = data.index(b")", pos)
    return _ascii_numbers(data[pos:end], np.int64), hdr


def read_boundary(p: Path) -> list[dict]:
    from aethercfd.case.model import read_patches

    out, start = [], 0
    text = _read_bytes(p).decode("utf-8", "replace")
    for pt in read_patches(p):
        m = re.search(rf"\b{re.escape(pt.name)}\s*\{{[^}}]*?startFace\s+(\d+)", text)
        start = int(m.group(1)) if m else start
        out.append({"name": pt.name, "type": pt.type, "startFace": start, "nFaces": pt.n_faces or 0})
        start += pt.n_faces or 0
    return out


def read_polymesh(mesh_dir: str | Path) -> PolyMesh:
    d = Path(mesh_dir)
    points, _ = _read_list(d / "points", "points")
    _, f = _read_list(d / "faces", "faces")
    owner, _ = _read_list(d / "owner", "labels")
    neighbour, _ = _read_list(d / "neighbour", "labels")
    patches = read_boundary(d / "boundary") if (d / "boundary").exists() else []
    return PolyMesh(points, f["offsets"], f["labels"], owner, neighbour, patches, d)
