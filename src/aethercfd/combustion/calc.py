"""Combustion pre-calculations with Cantera: what to know BEFORE a reacting simulation (no Qt here).

Everything is a plain function returning dicts of numbers with units, so the GUI, the copilot and scripts
share it. Definitions:
- mixture fraction: Bilger's element-based Z (fuel stream = 1, oxidizer stream = 0); Z_st where the
  coupling function vanishes.
- flame thickness: thermal thickness delta_L = (T_b - T_u) / max|dT/dx| of a freely propagating flame.
- ignition delay: time of the maximum dT/dt in a constant-pressure, adiabatic reactor.
- regime: Peters/Borghi numbers from u', l_t, S_L, delta_L (Da = (l_t/u')/(delta_L/S_L),
  Ka = (delta_L/eta)^2 with eta from the Kolmogorov scaling, eps ~ u'^3/l_t).
"""

from __future__ import annotations

import functools
import math
import threading
from dataclasses import dataclass, field

import numpy as np

AIR = "O2:0.21, N2:0.79"
# One Cantera calculation at a time. The panel, the copilot and scripts call these functions from pool
# threads, and building Cantera objects on two threads at once crashed the process (an access violation
# in a CI run: a calculation left by a closed panel was still running when the next one started).
_CANTERA = threading.RLock()


def _one_at_a_time(fn):
    @functools.wraps(fn)
    def locked(*args, **kwargs):
        with _CANTERA:
            return fn(*args, **kwargs)

    return locked


@dataclass
class Streams:
    mechanism: str = "gri30.yaml"
    fuel: str = "CH4:1"
    oxidizer: str = AIR
    T_fuel: float = 300.0
    T_ox: float = 300.0
    p: float = 101325.0

    def gas(self):
        import cantera as ct

        return ct.Solution(self.mechanism)


@dataclass
class FlameResult:
    phi: float
    S_L: float  # m/s
    delta_L: float  # m (thermal thickness)
    T_ad: float
    T_u: float
    x: list[float] = field(default_factory=list)
    T: list[float] = field(default_factory=list)
    grid_points: int = 0


def _mix(st: Streams, phi: float):
    gas = st.gas()
    gas.TP = st.T_ox, st.p
    gas.set_equivalence_ratio(phi, st.fuel, st.oxidizer)
    return gas


@_one_at_a_time
def stoichiometric(st: Streams) -> dict:
    """Z_st, the fuel/oxidizer mass ratio at stoichiometry, and the stream temperatures' mixing line."""
    gas = st.gas()
    gas.TP = st.T_ox, st.p
    gas.set_equivalence_ratio(1.0, st.fuel, st.oxidizer)
    z_st = gas.mixture_fraction(st.fuel, st.oxidizer, element="Bilger")
    return {"Z_st": float(z_st), "fuel_oxidizer_mass_ratio": float(z_st / (1 - z_st)),
            "note": "Bilger mixture fraction; fuel stream Z = 1, oxidizer Z = 0"}


@_one_at_a_time
def phi_of_z(st: Streams, z: float) -> float:
    zst = stoichiometric(st)["Z_st"]
    return float(z / (1 - z) * (1 - zst) / zst)


@_one_at_a_time
def z_of_phi(st: Streams, phi: float) -> float:
    gas = _mix(st, phi)
    return float(gas.mixture_fraction(st.fuel, st.oxidizer, element="Bilger"))


@_one_at_a_time
def adiabatic_flame(st: Streams, phis: list[float] | None = None) -> dict:
    """Equilibrium (HP) temperature and major products vs equivalence ratio."""
    phis = phis or [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4]
    rows = []
    for phi in phis:
        gas = _mix(st, phi)
        gas.equilibrate("HP")
        X = gas.mole_fraction_dict(threshold=1e-6)
        rows.append({"phi": phi, "T_ad": float(gas.T),
                     "major": {k: float(v) for k, v in sorted(X.items(), key=lambda kv: -kv[1])[:6]},
                     "X_CO": float(X.get("CO", 0.0)), "X_NO": float(X.get("NO", 0.0))})
    peak = max(rows, key=lambda r: r["T_ad"])
    return {"rows": rows, "peak": {"phi": peak["phi"], "T_ad": peak["T_ad"]}}


@_one_at_a_time
def flame_speed(st: Streams, phi: float = 1.0, *, width: float = 0.03, refine: tuple = (3, 0.1, 0.2),
                loglevel: int = 0) -> FlameResult:
    """Freely propagating premixed flame (mixture-averaged transport)."""
    import cantera as ct

    gas = _mix(st, phi)
    T_u = gas.T
    f = ct.FreeFlame(gas, width=width)
    f.set_refine_criteria(ratio=refine[0], slope=refine[1], curve=refine[2])
    f.transport_model = "mixture-averaged"
    f.solve(loglevel=loglevel, auto=True)
    x, T = f.grid, f.T
    grad = np.abs(np.gradient(T, x)).max()
    return FlameResult(phi, float(f.velocity[0]), float((T[-1] - T[0]) / grad), float(T[-1]), float(T_u),
                       list(map(float, x)), list(map(float, T)), len(x))


@_one_at_a_time
def ignition_delay(st: Streams, phi: float, T0: float, p: float | None = None, t_end: float = 2.0) -> dict:
    """Constant-pressure adiabatic reactor; delay = time of max dT/dt."""
    import cantera as ct

    gas = st.gas()
    gas.TP = T0, p or st.p
    gas.set_equivalence_ratio(phi, st.fuel, st.oxidizer)
    r = ct.IdealGasConstPressureReactor(gas, clone=False)
    net = ct.ReactorNet([r])
    t, T = [0.0], [r.T]
    while net.time < t_end:
        net.step()
        t.append(net.time)
        T.append(r.T)
        if r.T > T0 + 400 and len(T) > 3 and T[-1] - T[-2] < 1e-3:
            break
    t, T = np.array(t), np.array(T)
    dTdt = np.gradient(T, t)
    i = int(np.argmax(dTdt))
    ignited = T[-1] > T0 + 300
    return {"T0": T0, "phi": phi, "p": p or st.p, "tau_ign": float(t[i]) if ignited else None,
            "T_final": float(T[-1]), "ignited": bool(ignited)}


@_one_at_a_time
def counterflow_flamelet(st: Streams, strain: float = 100.0, width: float = 0.02, loglevel: int = 0) -> dict:
    """Counterflow diffusion flame at a nominal strain rate a = (|u_f| + |u_o|)/width [1/s]."""
    import cantera as ct

    gas = st.gas()
    gas.TP = st.T_ox, st.p
    f = ct.CounterflowDiffusionFlame(gas, width=width)
    gas.TPX = st.T_fuel, st.p, st.fuel
    rho_f = gas.density
    gas.TPX = st.T_ox, st.p, st.oxidizer
    rho_o = gas.density
    u = strain * width / 2.0
    f.fuel_inlet.mdot = rho_f * u
    f.fuel_inlet.X = st.fuel
    f.fuel_inlet.T = st.T_fuel
    f.oxidizer_inlet.mdot = rho_o * u
    f.oxidizer_inlet.X = st.oxidizer
    f.oxidizer_inlet.T = st.T_ox
    f.set_refine_criteria(ratio=4, slope=0.2, curve=0.3, prune=0.04)
    f.solve(loglevel=loglevel, auto=True)
    Zs = []
    for j in range(len(f.grid)):
        f.gas.TPY = f.T[j], st.p, f.Y[:, j]
        Zs.append(f.gas.mixture_fraction(st.fuel, st.oxidizer, element="Bilger"))
    return {"strain_nominal": strain, "T_max": float(f.T.max()), "burning": bool(f.T.max() > max(st.T_fuel,
                                                                                                  st.T_ox) + 300),
            "Z": list(map(float, Zs)), "T": list(map(float, f.T)), "grid_points": len(f.grid)}


@_one_at_a_time
def transport(st: Streams, phi: float | None = None, T: float | None = None, X: str | None = None) -> dict:
    gas = st.gas()
    if X:
        gas.TPX = T or st.T_ox, st.p, X
    else:
        gas.TP = T or st.T_ox, st.p
        gas.set_equivalence_ratio(phi if phi is not None else 1.0, st.fuel, st.oxidizer)
    mu = gas.viscosity
    return {"T": gas.T, "rho": gas.density, "mu": mu, "nu": mu / gas.density, "cp": gas.cp_mass,
            "k": gas.thermal_conductivity, "Pr": mu * gas.cp_mass / gas.thermal_conductivity,
            "alpha": gas.thermal_conductivity / (gas.density * gas.cp_mass), "M": gas.mean_molecular_weight}


def regime(S_L: float, delta_L: float, u_prime: float, l_t: float, nu: float) -> dict:
    """Peters' premixed regime numbers and the resolution they imply."""
    tau_c = delta_L / S_L
    tau_t = l_t / u_prime
    eps = u_prime**3 / l_t
    eta = (nu**3 / eps) ** 0.25
    Da = tau_t / tau_c
    Ka = (delta_L / eta) ** 2
    Re_t = u_prime * l_t / nu
    if u_prime < S_L:
        name = "wrinkled flamelets" if Ka < 1 else "?"
    elif Ka < 1:
        name = "corrugated flamelets"
    elif Ka < 100:
        name = "thin reaction zones"
    else:
        name = "broken reaction zones"
    if Re_t < 1:
        name = "laminar"
    return {"Da": Da, "Ka": Ka, "Re_t": Re_t, "eta": eta, "tau_chem": tau_c, "tau_turb": tau_t,
            "u_prime_over_S_L": u_prime / S_L, "l_t_over_delta_L": l_t / delta_L, "regime": name,
            "cells_to_resolve_flame_10_per_delta": delta_L / 10.0, "kolmogorov_cell": eta,
            "note": "Peters (2000) premixed regimes; for non-premixed flames use the flamelet strain/dissipation "
                    "instead of S_L"}


def resolution_advice(delta_L: float, cell: float) -> dict:
    n = delta_L / cell if cell > 0 else 0.0
    if n >= 10:
        verdict = "flame front resolved (laminar/DNS-grade)"
    elif n >= 3:
        verdict = "marginal: OK for thickened/LES models, under-resolved for finite-rate laminar chemistry"
    else:
        verdict = "under-resolved: use a turbulent combustion model (EDC/PaSR/flamelets) or refine"
    return {"cells_per_flame_thickness": n, "verdict": verdict}


@_one_at_a_time
def gas_summary(mechanism: str) -> dict:
    import cantera as ct

    g = ct.Solution(mechanism)
    return {"mechanism": mechanism, "species": g.n_species, "reactions": g.n_reactions,
            "elements": list(g.element_names), "transport": g.transport_model,
            "has_NOx": any(s in g.species_names for s in ("NO", "NO2", "N2O")),
            "has_soot_precursors": any(s in g.species_names for s in ("C2H2", "A1", "C6H6", "BIN1A")),
            "stiffness_hint": "large" if g.n_species > 100 else "moderate" if g.n_species > 40 else "small"}


def is_cantera_available() -> bool:
    try:
        import cantera  # noqa: F401

        return True
    except ImportError:
        return False


def fmt_si(v: float, unit: str) -> str:
    if v == 0 or not math.isfinite(v):
        return f"{v} {unit}"
    for f, p in ((1e-6, "µ"), (1e-3, "m"), (1.0, ""), (1e3, "k"), (1e6, "M")):
        if abs(v) < f * 1000:
            return f"{v / f:.4g} {p}{unit}"
    return f"{v:.4g} {unit}"
