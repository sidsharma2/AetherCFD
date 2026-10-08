"""Combustion pre-calculations against textbook values (Cantera, GRI-Mech 3.0)."""

from __future__ import annotations

import pytest

ct = pytest.importorskip("cantera")

from aethercfd.combustion.calc import (  # noqa: E402
    Streams,
    adiabatic_flame,
    flame_speed,
    ignition_delay,
    phi_of_z,
    regime,
    resolution_advice,
    stoichiometric,
    transport,
    z_of_phi,
)

CH4_AIR = Streams(fuel="CH4:1", oxidizer="O2:0.21, N2:0.79", T_fuel=300, T_ox=300)
SANDIA_D = Streams(fuel="CH4:0.25, O2:0.1575, N2:0.5925", oxidizer="O2:0.21, N2:0.79", T_fuel=294, T_ox=291)


def test_stoichiometric_mixture_fraction():
    assert stoichiometric(CH4_AIR)["Z_st"] == pytest.approx(0.055, abs=0.002)
    assert stoichiometric(SANDIA_D)["Z_st"] == pytest.approx(0.351, abs=0.005)  # Barlow & Frank
    z = z_of_phi(CH4_AIR, 1.0)
    assert phi_of_z(CH4_AIR, z) == pytest.approx(1.0, rel=1e-6)


def test_adiabatic_flame_temperature():
    r = adiabatic_flame(CH4_AIR, [1.0, 1.05])
    assert r["rows"][0]["T_ad"] == pytest.approx(2225, abs=15)
    assert 1.0 <= r["peak"]["phi"] <= 1.05


@pytest.mark.slow
def test_laminar_flame_speed_and_thickness():
    f = flame_speed(CH4_AIR, 1.0)
    assert 0.35 < f.S_L < 0.42 and 3e-4 < f.delta_L < 7e-4 and f.T_ad == pytest.approx(2220, abs=40)


def test_ignition_delay_order_of_magnitude():
    r = ignition_delay(CH4_AIR, 1.0, 1500.0)
    assert r["ignited"] and 1e-4 < r["tau_ign"] < 5e-3


def test_regime_and_resolution():
    tp = transport(CH4_AIR, phi=1.0)
    assert tp["nu"] == pytest.approx(1.6e-5, rel=0.15)
    g = regime(S_L=0.38, delta_L=4.5e-4, u_prime=2.0, l_t=0.01, nu=tp["nu"])
    assert g["regime"] == "thin reaction zones" and 1 < g["Ka"] < 100
    assert resolution_advice(4.5e-4, 1e-3)["cells_per_flame_thickness"] < 1
