"""Hole-in-a-box blocking design on an illustrative set of concentric tubes (holes through the tube walls)."""

from __future__ import annotations

import pytest

from aethercfd.copilot.mesh_tools import HoleBoxParams, hole_box_design
from aethercfd.mesh.blocking import corner_misalignment, design_hole_box, tube_levels

RADII = {"cap": 0.65, "R0": 1.0, "R1": 1.5, "R2": 2.5, "R3": 3.0}


def test_multi_level_hole_design():
    lv = tube_levels(RADII, 45.0, physical={"R2": False, "R3": False})
    d = design_hole_box(0.25, 0.75, lv, n_a=6, n_end=4)
    a = {r["level"]: r["box_half_length"] for r in d["levels"]}
    assert a["cap"] == pytest.approx(0.36) and a["R1"] == pytest.approx(0.45) and a["R2"] == pytest.approx(0.45)
    assert a["R0"] == pytest.approx(0.3827, abs=0.001)  # a square box fits between the gap and slab limits
    rows = {r["level"]: r for r in d["levels"]}
    # a box over the whole band is ~27 deg off at the cap; the H-O box more than halves that
    assert rows["cap"]["misalignment_full_band_deg"] == pytest.approx(26.65, abs=0.3)
    assert rows["cap"]["misalignment_deg"] < 12
    assert rows["R2"]["layout"].startswith("ellipse") and rows["R2"]["misalignment_deg"] < 1e-9
    assert rows["cap"]["end_slab"]["band_plane_cell"] == pytest.approx(0.126, abs=0.002)
    assert all(r.get("slab_cone_deg_from_previous", 0) < 15 for r in d["levels"])


def test_misalignment_and_short_band_note():
    assert corner_misalignment(1.0, 1.0) == pytest.approx(0.0)
    assert corner_misalignment(0.75, 0.25) == pytest.approx(26.6, abs=0.2)
    d = design_hole_box(0.25, 0.3, tube_levels({"w": 1.0}, 45.0))
    assert any("too short" in n for n in d["notes"])


def test_copilot_tool():
    out = hole_box_design(None, HoleBoxParams(hole_r=0.25, half_length=0.75, levels=[
        {"name": "cap", "radius": 0.65}, {"name": "R2", "radius": 2.5, "physical": False}]))
    assert [r["level"] for r in out["levels"]] == ["cap", "R2"]
