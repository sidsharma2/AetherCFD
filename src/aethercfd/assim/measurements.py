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


def load_measurements(path: str | Path, *, length_unit: str = "m", x="x", y="y", z="z") -> Measurements:
    """CSV with a header row: point coordinates (x, y, z; a missing z is 0) and one column per measured
    field; optional uncertainty columns named <field>_std (or <field>_sigma)."""
    p = Path(path)
    with p.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{p.name} has no data rows")
    cols = [c.strip() for c in rows[0]]
    k = {c.lower(): c for c in rows[0]}
    if x.lower() not in k or y.lower() not in k:
        raise ValueError(f"{p.name}: need columns {x!r} and {y!r} (found {cols})")
    s = _UNITS.get(length_unit)
    if s is None:
        raise ValueError(f"length_unit must be one of {sorted(_UNITS)}")

    def col(name):
        return np.array([float(r[name]) if r[name] not in ("", None) else np.nan for r in rows])

    pts = np.column_stack([col(k[x.lower()]), col(k[y.lower()]),
                           col(k[z.lower()]) if z.lower() in k else np.zeros(len(rows))]) * s
    skip = {x.lower(), y.lower(), z.lower()}
    vals, sig = {}, {}
    for c in rows[0]:
        cl = c.lower()
        if cl in skip or cl.endswith(("_std", "_sigma")):
            continue
        vals[c.strip()] = col(c)
        for suf in ("_std", "_sigma"):
            if (c + suf).lower() in k:
                sig[c.strip()] = col(k[(c + suf).lower()])
    return Measurements(pts, vals, sig, str(p))


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


def compare(m: Measurements, fields: dict[str, np.ndarray], centres: np.ndarray) -> dict:
    """Per measured field that the simulation has: statistics of simulated - measured at the points."""
    idx, dist = nearest_cells(centres, m.points)
    out = {"n_points": len(m.points), "max_point_to_cell_distance": float(dist.max()), "fields": {}}
    for name, meas in m.values.items():
        sim = fields.get(name)
        if sim is None:
            out["fields"][name] = {"note": "not in the simulation fields"}
            continue
        sv = sim[idx] if sim.ndim == 1 else np.linalg.norm(sim[idx], axis=1)
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

    xb = np.asarray(background, dtype=np.float64)
    y = np.asarray(obs, dtype=np.float64)
    good = np.isfinite(y)
    pts, y = np.asarray(points)[good], y[good]
    so = np.broadcast_to(np.asarray(sigma_obs, dtype=np.float64), good.shape)[good]
    idx, _ = nearest_cells(centres, pts)
    hx = xb[idx]
    innov = y - hx
    # observation-space covariance H B H^T + R, between the observation points (their cells)
    dpp = np.linalg.norm(centres[idx][:, None, :] - centres[idx][None, :, :], axis=2)
    S = sigma_b ** 2 * np.exp(-0.5 * (dpp / length) ** 2) + np.diag(so ** 2)
    w = np.linalg.solve(S, innov)  # (m,)
    tree = cKDTree(centres)
    near = sorted(set().union(*[set(tree.query_ball_point(p, 4 * length)) for p in centres[idx]])) if len(idx) else []
    near = np.asarray(near, dtype=int)
    analysis = xb.copy()
    post = np.full(xb.shape, float(sigma_b))
    if near.size:
        dcp = np.linalg.norm(centres[near][:, None, :] - centres[idx][None, :, :], axis=2)
        BHt = sigma_b ** 2 * np.exp(-0.5 * (dcp / length) ** 2)  # (n_near, m)
        analysis[near] = xb[near] + BHt @ w
        Sinv_HB = np.linalg.solve(S, BHt.T)  # (m, n_near)
        var = sigma_b ** 2 - np.einsum("ij,ji->i", BHt, Sinv_HB)
        post[near] = np.sqrt(np.clip(var, 0, None))
    res = Assimilation(analysis, analysis - xb, post, innov, y - analysis[idx], int(near.size))
    if not good.all():
        res.notes.append(f"{int((~good).sum())} observation(s) without a value were skipped")
    if length <= 0 or sigma_b <= 0:
        res.notes.append("sigma_b and length must be positive")
    return res
