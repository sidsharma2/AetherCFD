"""Measurements vs simulation, and a first data-assimilation step (no Qt).

    m = load_measurements("piv_line.csv", length_unit="mm")    # columns x, y, z, <field>[, <field>_std]
    s = sample(centres, values, m.points)                       # value of the cell nearest each point
    report = compare(m, {"T": T_cells}, centres)                # bias, RMSE, R^2, worst points per field
    a = assimilate(centres, T_cells, m.points, m.values["T"], sigma_obs, sigma_b=50.0, length=2e-3)

``assimilate`` is optimal interpolation (the analysis step of 3D-Var / a Kalman filter with a static
background covariance): B_ij = sigma_b^2 exp(-d_ij^2 / 2L^2) between cells and observation points,
x_a = x_b + B H^T (H B H^T + R)^-1 (y - H x_b). Only cells within 4L of an observation are touched, so it
scales to large meshes. The posterior standard deviation says where the data actually constrained the field.
The four views the CFDMI idea asks for are all returned: measured, simulated, correction, assimilated.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_UNITS = {"m": 1.0, "mm": 1e-3, "cm": 1e-2, "um": 1e-6, "in": 0.0254}


@dataclass
class Measurements:
    points: np.ndarray  # (m, 3) in metres
    values: dict[str, np.ndarray]  # field -> (m,)
    sigma: dict[str, np.ndarray] = field(default_factory=dict)  # field -> (m,) standard deviation, if given
    source: str = ""
    notes: list[str] = field(default_factory=list)


def load_measurements(path: str | Path, *, length_unit: str = "m", x="x", y="y", z="z") -> Measurements:
    """CSV with a header row: point coordinates (x, y, z; a missing z is 0) and one column per measured
    field; optional uncertainty columns named <field>_std (or <field>_sigma). Comma, semicolon or tab
    separated; header names are matched ignoring case and spaces. Rows without finite coordinates are
    dropped (listed in ``notes``)."""
    p = Path(path)
    s = _UNITS.get(length_unit)
    if s is None:
        raise ValueError(f"length_unit must be one of {sorted(_UNITS)}")
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    first = text.split("\n", 1)[0]
    sep = max((",", ";", "\t"), key=first.count)
    raw = [r for r in csv.reader(text.splitlines(), delimiter=sep) if any(c.strip() for c in r)]
    if len(raw) < 2:
        raise ValueError(f"{p.name} has no data rows")
    header = [c.strip() for c in raw[0]]
    k = {c.lower(): i for i, c in enumerate(header) if c}
    if x.lower() not in k or y.lower() not in k:
        raise ValueError(f"{p.name}: need columns {x!r} and {y!r} (found {header})")
    body = raw[1:]

    def col(i):
        out = np.full(len(body), np.nan)
        for j, r in enumerate(body):
            v = r[i].strip() if i < len(r) else ""
            if v:
                try:
                    out[j] = float(v)
                except ValueError:
                    raise ValueError(f"{p.name} row {j + 2}, column {header[i]!r}: {v!r} is not a number") from None
        return out

    pts = np.column_stack([col(k[x.lower()]), col(k[y.lower()]),
                           col(k[z.lower()]) if z.lower() in k else np.zeros(len(body))]) * s
    skip = {x.lower(), y.lower(), z.lower()}
    vals, sig = {}, {}
    for i, c in enumerate(header):
        cl = c.lower()
        if not c or cl in skip or cl.endswith(("_std", "_sigma")):
            continue
        vals[c] = col(i)
        for suf in ("_std", "_sigma"):
            if (cl + suf) in k:
                sig[c] = col(k[cl + suf])
    notes = []
    good = np.isfinite(pts).all(axis=1)
    if not good.all():
        notes.append(f"{int((~good).sum())} row(s) without finite coordinates skipped (rows "
                     + ", ".join(str(j + 2) for j in np.flatnonzero(~good)[:8]) + ("…" if (~good).sum() > 8 else "") + ")")
        pts = pts[good]
        vals = {n: v[good] for n, v in vals.items()}
        sig = {n: v[good] for n, v in sig.items()}
    if not len(pts):
        raise ValueError(f"{p.name}: no row has finite coordinates")
    if not vals:
        raise ValueError(f"{p.name}: no measured-value column besides the coordinates")
    return Measurements(pts, vals, sig, str(p), notes)


def nearest_cells(centres: np.ndarray, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(cell index, distance) of the nearest cell centre to each point (KD-tree)."""
    from scipy.spatial import cKDTree

    d, i = cKDTree(centres).query(points)
    return i, d


def sample(centres: np.ndarray, values: np.ndarray, points: np.ndarray) -> np.ndarray:
    idx, _ = nearest_cells(centres, points)
    return values[idx]


def _stats(meas: np.ndarray, sim: np.ndarray) -> dict:
    ok = np.isfinite(meas) & np.isfinite(sim)
    m, s = meas[ok], sim[ok]
    if m.size == 0:
        return {"n": 0}
    err = s - m
    ss = float(((m - m.mean()) ** 2).sum())
    worst = np.argsort(-np.abs(err))[:5]
    return {"n": int(m.size), "bias": float(err.mean()), "rmse": float(np.sqrt((err ** 2).mean())),
            "max_abs_error": float(np.abs(err).max()), "r2": float(1 - (err ** 2).sum() / ss) if ss > 0 else None,
            "relative_rmse": float(np.sqrt((err ** 2).mean()) / (np.abs(m).mean() or 1.0)),
            "worst_points": [int(np.flatnonzero(ok)[w]) for w in worst]}


def outside_mesh(centres: np.ndarray, points: np.ndarray, idx: np.ndarray, dist: np.ndarray,
                 factor: float = 3.0) -> np.ndarray:
    """True where a point is farther from its nearest cell centre than ``factor`` times that cell's own
    spacing (distance to the neighbouring centre): the point lies outside the mesh (wrong units or frame)."""
    from scipy.spatial import cKDTree

    if len(centres) < 2 or not len(idx):
        return np.zeros(len(idx), bool)
    spacing = cKDTree(centres).query(centres[idx], k=2)[0][:, 1]
    return dist > factor * np.maximum(spacing, 1e-300)


def compare(m: Measurements, fields: dict[str, np.ndarray], centres: np.ndarray) -> dict:
    """Per measured field that the simulation has: statistics of simulated - measured at the points.
    Points outside the mesh are left out of the statistics (counted in ``outside``)."""
    idx, dist = nearest_cells(centres, m.points)
    out_mask = outside_mesh(centres, m.points, idx, dist)
    out = {"n_points": len(m.points), "max_point_to_cell_distance": float(dist.max()), "fields": {},
           "outside": int(out_mask.sum()), "notes": list(m.notes)}
    if out_mask.any():
        out["notes"].append(f"{int(out_mask.sum())} of {len(out_mask)} point(s) lie outside the mesh (check the "
                            "length unit and coordinate frame); left out")
    for name, meas in m.values.items():
        sim = fields.get(name)
        if sim is None:
            out["fields"][name] = {"note": "not in the simulation fields"}
            continue
        sv = sim[idx] if sim.ndim == 1 else np.linalg.norm(sim[idx], axis=1)
        sv = np.where(out_mask, np.nan, sv)
        out["fields"][name] = _stats(meas, sv) | {"simulated": sv.tolist() if len(sv) <= 200 else None}
    return out


@dataclass
class Assimilation:
    analysis: np.ndarray  # assimilated field (all cells)
    correction: np.ndarray  # analysis - background
    posterior_std: np.ndarray  # per cell (sigma_b where no data reaches)
    innovation: np.ndarray  # y - H x_b at the observation points
    residual: np.ndarray  # y - H x_a
    touched_cells: int
    notes: list[str] = field(default_factory=list)


def assimilate(centres: np.ndarray, background: np.ndarray, points: np.ndarray, obs: np.ndarray,
               sigma_obs: float | np.ndarray, *, sigma_b: float, length: float) -> Assimilation:
    from scipy.spatial import cKDTree

    if not (length > 0 and sigma_b > 0):
        raise ValueError("sigma_b and the correlation length must be positive")
    xb = np.asarray(background, dtype=np.float64)
    y = np.asarray(obs, dtype=np.float64)
    so_all = np.broadcast_to(np.asarray(sigma_obs, dtype=np.float64), y.shape)
    pts_all = np.asarray(points, dtype=np.float64)
    good = np.isfinite(y) & np.isfinite(pts_all).all(axis=1) & np.isfinite(so_all)
    pts, y, so = pts_all[good], y[good], so_all[good]
    notes = []
    if not good.all():
        notes.append(f"{int((~good).sum())} observation(s) without a finite value were skipped")
    idx, dist = nearest_cells(centres, pts) if len(pts) else (np.zeros(0, int), np.zeros(0))
    out = outside_mesh(centres, pts, idx, dist)
    if out.any():
        notes.append(f"{int(out.sum())} observation(s) outside the mesh were skipped (check the length unit)")
        pts, y, so, idx = pts[~out], y[~out], so[~out], idx[~out]
    if not len(idx):
        raise ValueError("no usable observation inside the mesh")
    hx = xb[idx]
    innov = y - hx
    # observation-space covariance H B H^T + R, between the observation points (their cells)
    dpp = np.linalg.norm(centres[idx][:, None, :] - centres[idx][None, :, :], axis=2)
    S = sigma_b ** 2 * np.exp(-0.5 * (dpp / length) ** 2) + np.diag(so ** 2)
    try:
        cho = np.linalg.cholesky(S)
    except np.linalg.LinAlgError:  # zero uncertainty + two points in one cell: S is singular
        S = S + np.eye(len(S)) * 1e-6 * sigma_b ** 2
        cho = np.linalg.cholesky(S)
        notes.append("observation covariance was singular (zero uncertainty at coincident points): regularised")

    def solve(b):
        return np.linalg.solve(cho.T, np.linalg.solve(cho, b))

    w = solve(innov)  # (m,)
    tree = cKDTree(centres)
    counts = tree.query_ball_point(centres[idx], 4 * length, return_length=True)  # cheap: counts only
    if int(np.max(counts)) * len(idx) > 4e8 or int(np.sum(counts)) > 5e7:
        raise ValueError(f"up to {int(np.max(counts)):,} cells lie within 4 correlation lengths of each of the "
                         f"{len(idx)} points: reduce the correlation length (or the number of points)")
    lists = tree.query_ball_point(centres[idx], 4 * length)
    near = np.unique(np.concatenate([np.asarray(li, dtype=np.int64) for li in lists])) if len(lists) else \
        np.zeros(0, np.int64)
    analysis = xb.copy()
    post = np.full(xb.shape, float(sigma_b))
    cidx = centres[idx]
    step = max(1, int(2e7 // max(len(idx), 1)))  # rows per chunk: ~160 MB of float64 at most
    for a in range(0, near.size, step):
        nb = near[a:a + step]
        dcp = np.linalg.norm(centres[nb][:, None, :] - cidx[None, :, :], axis=2)
        BHt = sigma_b ** 2 * np.exp(-0.5 * (dcp / length) ** 2)  # (chunk, m)
        analysis[nb] = xb[nb] + BHt @ w
        var = sigma_b ** 2 - np.einsum("ij,ji->i", BHt, solve(BHt.T))
        post[nb] = np.sqrt(np.clip(var, 0, None))
    return Assimilation(analysis, analysis - xb, post, innov, y - analysis[idx], int(near.size), notes)
