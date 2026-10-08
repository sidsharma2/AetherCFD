"""Near-wall resolution: first-cell height from a target y+, and prism/graded layer planning.

Skin friction from standard correlations (flat plate: Schlichting, Cf = (2 log10 Re_x - 0.65)^-2.3;
pipe/duct: Blasius Fanning friction factor, Cf = 0.079 Re_D^-0.25), u_tau = U sqrt(Cf/2),
y_p = y+ nu / u_tau. For a cell-centred finite-volume mesh the first cell is twice the centroid
distance: dy1 = 2 y_p. Estimates only (+-30 %): check y+ from the solution afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def sutherland_mu(T: float, *, As: float = 1.458e-6, Ts: float = 110.4) -> float:
    """Dynamic viscosity of air [Pa s] (OpenFOAM's sutherland transport defaults)."""
    return As * T**1.5 / (T + Ts)


def ideal_gas_rho(p: float, T: float, *, R: float = 287.05) -> float:
    return p / (R * T)


def skin_friction(re: float, kind: str = "plate") -> float:
    if re <= 0:
        raise ValueError("Reynolds number must be positive")
    if kind == "pipe":
        if re < 2300:
            return 16.0 / re  # laminar Fanning
        return 0.079 * re**-0.25
    if re < 5e5:
        return 0.664 / math.sqrt(re)  # laminar (Blasius plate), local
    return (2.0 * math.log10(re) - 0.65) ** -2.3


@dataclass
class WallSpacing:
    reynolds: float
    cf: float
    tau_w: float
    u_tau: float
    y_p: float  # wall distance of the first cell centre for the target y+
    first_cell: float  # first cell height (2 y_p)
    regime: str

    def as_dict(self) -> dict:
        return {k: (float(f"{v:.6g}") if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def first_cell_height(yplus: float, U: float, L: float, *, nu: float | None = None, mu: float | None = None,
                      rho: float | None = None, kind: str = "plate") -> WallSpacing:
    """``L`` is the plate length (kind='plate') or hydraulic diameter (kind='pipe')."""
    if nu is None:
        if mu is None or rho is None:
            raise ValueError("give nu, or mu and rho")
        nu = mu / rho
    rho = rho if rho is not None else 1.0
    re = U * L / nu
    cf = skin_friction(re, kind)
    tau = 0.5 * cf * rho * U * U
    u_tau = U * math.sqrt(cf / 2.0)
    y_p = yplus * nu / u_tau
    regime = ("laminar" if (kind == "pipe" and re < 2300) or (kind == "plate" and re < 5e5)
              else "transitional" if kind == "pipe" and re < 1e4 else "turbulent")
    return WallSpacing(re, cf, tau, u_tau, y_p, 2.0 * y_p, regime)


def yplus_of(first_cell: float, U: float, L: float, *, nu: float, kind: str = "plate") -> float:
    """y+ of the first cell centre for a given first cell height."""
    cf = skin_friction(U * L / nu, kind)
    return (first_cell / 2.0) * U * math.sqrt(cf / 2.0) / nu


@dataclass
class LayerPlan:
    n: int
    ratio: float
    first: float
    last: float
    total: float
    note: str = ""

    def as_dict(self) -> dict:
        return {k: (float(f"{v:.6g}") if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def plan_layers(first: float, *, core: float | None = None, total: float | None = None, ratio: float = 1.2,
                max_layers: int = 60) -> LayerPlan:
    """Layers growing from ``first`` with ``ratio`` until the last layer reaches ~core/ratio (smooth
    hand-over to the core cells), or until ``total`` thickness is reached."""
    if core is None and total is None:
        raise ValueError("give the core cell size or the total layer thickness")
    n, t, d = 0, 0.0, first
    while n < max_layers:
        if total is not None and t + d > total * 1.0001:
            break
        if core is not None and total is None and d > core / ratio * 1.0001:
            break
        t += d
        n += 1
        d *= ratio
    last = first * ratio ** (n - 1) if n else 0.0
    note = ""
    if n == max_layers:
        note = f"capped at {max_layers} layers; raise the ratio or the first cell height"
    elif core is not None and last * ratio < core * 0.7:
        note = f"last layer {last:.3g} is much smaller than the core cells {core:.3g}: size jump at the layer edge"
    return LayerPlan(n, ratio, first, last, t, note)
