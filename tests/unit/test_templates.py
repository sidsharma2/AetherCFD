"""Case templates: ranking by physics and value-text edits that keep the OpenFOAM form."""

from __future__ import annotations

from aethercfd.templates.catalog import (
    TEMPLATES,
    rank,
    read_scalar,
    read_vector,
    set_scalar_text,
    set_vector_text,
)
from aethercfd.templates.create import display_value, new_value_text


def test_every_template_has_a_source_and_unique_id():
    ids = [t.id for t in TEMPLATES]
    assert len(ids) == len(set(ids))
    assert all(t.sources and set(t.sources) <= {"esi", "foundation"} for t in TEMPLATES)
    for t in TEMPLATES:
        for p in t.params:
            assert set(t.sources) <= set(p.where), (t.id, p.id)  # located in every fork the template has


def test_ranking_matches_physics():
    top = rank({"regime": "supersonic", "time": "transient", "physics": "none", "geometry": "internal"})[0][0]
    assert top.id in ("forwardStep", "obliqueShock", "shockTube")
    assert rank({"regime": "incompressible", "time": "steady", "physics": "none", "geometry": "internal"})[0][0].id \
        == "pitzDaily"
    assert rank({"regime": "compressible", "time": "steady", "physics": "cht", "geometry": "internal"})[0][0].id \
        == "heatExchanger"
    stl = [t.id for t, _s, _w in rank({"regime": "incompressible", "time": "steady", "physics": "none",
                                        "geometry": "stl"})[:2]]
    assert "motorBike" in stl
    assert all("foundation" in t.sources for t, _s, _w in rank({}, family="foundation"))


def test_value_text_edits_keep_form():
    assert set_scalar_text("[0 2 -1 0 0 0 0] 1e-05", 1.5e-5) == "[0 2 -1 0 0 0 0] 1.5e-05"
    assert set_scalar_text("2000", 500) == "500"
    assert set_vector_text("uniform (10 0 0)", [25.0, 0, 0], only_x=True) == "uniform (25.0 0 0)"
    assert set_vector_text("uniform (25.75 3.62 0)", [30, 5, 0]) == "uniform (30 5 0)"
    assert read_vector("uniform (25.75 3.62 0)") == [25.75, 3.62, 0.0] and read_scalar("nu [0 2 -1 0 0 0 0] 2e-5") \
        == 2e-5
    assert new_value_text("vector_x", "uniform (10 0 0)", 12) == "uniform (12.0 0 0)"
    assert display_value("vector_x", "uniform (10 0 0)") == 10.0
