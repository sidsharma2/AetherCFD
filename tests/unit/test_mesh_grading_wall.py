"""AetherMesh grading design and near-wall spacing."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aethercfd.mesh.grading import design, n_for, sizes
from aethercfd.mesh.wall import first_cell_height, plan_layers, skin_friction, sutherland_mu, yplus_of


def test_geometric_sizes_match_blockmesh_convention():
    c = sizes(10, 1.0, 8.0)
    assert math.isclose(c.sum(), 1.0) and math.isclose(c[-1] / c[0], 8.0)
    assert np.allclose(c[1:] / c[:-1], 8.0 ** (1 / 9))
    assert np.allclose(sizes(4, 2.0, 1.0), 0.5)
    assert math.isclose(n_for(1.0, 0.1, 1.0), 10.0)


def test_rev_b1_segment_e_redesign():
    """REV B.1 downstream bore: 47.5 mm, 100 cells graded 8 today (0.125 mm neighbours on both sides,
    1.125 mm last cell -> 9:1 jump). Matching both neighbours with r <= 1.1 and cells <= 0.3 mm:"""
    old = sizes(100, 47.5, 8.0)
    assert old[-1] / 0.125 > 8.9  # the 9:1 jump the investigation found
    d = design(47.5, first=0.125, last=0.125, max_size=0.3, max_ratio=1.1)
    c = d.cells()
    assert math.isclose(c.sum(), 47.5, rel_tol=1e-9)
    assert d.max_cell_ratio <= 1.1001
    assert c[0] == pytest.approx(0.125, rel=0.02) and c[-1] == pytest.approx(0.125, rel=0.02)
    assert c.max() <= 0.3 * 1.0001
    assert 150 <= d.n <= 200
    g = d.blockmesh()
    assert g.startswith("((") and g.count("(") == 4  # three segments: grow, uniform, shrink
    frac_l = sum(s.length for s in d.segments) / 47.5
    assert math.isclose(frac_l, 1.0)


def test_one_sided_and_uniform():
    d = design(10.0, first=0.1, max_ratio=1.2, max_size=1.0)
    c = d.cells()
    assert c[0] == pytest.approx(0.1, rel=0.02) and d.max_cell_ratio <= 1.2001 and c.max() <= 1.0001
    d2 = design(10.0, last=0.1, max_ratio=1.2, max_size=1.0)
    assert d2.cells()[-1] == pytest.approx(0.1, rel=0.02) and d2.cells()[0] > 0.5
    assert design(3.0, n=6).blockmesh() == "1"


def test_fixed_n_reports_violations():
    d = design(1.0, first=0.01, last=0.2, n=10, max_ratio=1.2)
    assert d.notes and "cell-to-cell ratio" in d.notes[0]


def test_wall_spacing_flat_plate_reference():
    # air at 20 C, U = 10 m/s, L = 1 m: Re ~ 6.6e5; y+ = 1 puts the first cell CENTRE ~0.03 mm from the
    # wall (the usual calculator value), so the first cell is twice that on a cell-centred mesh
    nu = sutherland_mu(293.15) / 1.204
    w = first_cell_height(1.0, 10.0, 1.0, nu=nu)
    assert 6e5 < w.reynolds < 7e5 and w.regime == "turbulent"
    assert 2.5e-5 < w.y_p < 4e-5 and w.first_cell == pytest.approx(2 * w.y_p)
    assert yplus_of(w.first_cell, 10.0, 1.0, nu=nu) == pytest.approx(1.0, rel=1e-9)
    assert skin_friction(1000, "pipe") == pytest.approx(0.016)


def test_layer_plan_hands_over_to_core():
    p = plan_layers(1e-5, core=2e-4, ratio=1.2)
    assert p.last <= 2e-4 / 1.2 * 1.001 and p.last * 1.2 > 2e-4 * 0.7 and not p.note
    q = plan_layers(1e-5, total=5e-5, ratio=1.2)
    assert q.total <= 5e-5 * 1.0001 and q.n == 3  # a 4th layer would make it 5.37e-5
