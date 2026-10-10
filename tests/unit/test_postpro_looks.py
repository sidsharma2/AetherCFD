"""Units, colour-bar labels, per-field colour maps and run presets (no Qt)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aethercfd.results.units import bar_title, dimensions_of, si_scale, unit_text

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def test_units_from_field_headers():
    assert unit_text(dimensions_of(FIX, "U")) == "m/s"
    assert unit_text(dimensions_of(FIX, "p")) == "m²/s²"  # kinematic pressure in an incompressible case
    assert unit_text((1, -1, -2, 0, 0, 0, 0)) == "Pa"
    assert unit_text((0, 0, 0, 1, 0, 0, 0)) == "K"
    assert unit_text((0, 0, 0, 0, 0, 0, 0)) == ""
    assert unit_text((2, 1, 0, 0, 0, 0, 0)) == "kg² m"
    assert unit_text(None) is None and dimensions_of(FIX, "nope") is None


def test_si_scaling_for_colour_bars():
    assert si_scale(3.6e5, 1.17e7, "Pa") == (1e6, "MPa")
    assert si_scale(9e4, 1.2e5, "Pa") == (1e3, "kPa")
    assert si_scale(299, 538, "K") == (1.0, "K")
    f, lab = si_scale(0, 3e-5, "m²/s")
    assert f == 1e-6 and lab == "×10⁻⁶ m²/s"
    assert si_scale(0, 0, "Pa") == (1.0, "Pa")
    assert bar_title("p", "Pa", "MPa") == "p [MPa]" and bar_title("alpha", "", "") == "alpha"


def test_ticks_and_label_digits():
    pytest.importorskip("PySide6")
    from aethercfd.studio.panels.viewport import look as lk

    assert lk.nice_ticks(2.53e-05, 49.97) == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert all(299.4 <= t <= 538.3 for t in lk.nice_ticks(299.4, 538.3))
    assert lk.label_format(299.95, 300.05) == "%.6g"  # narrow range: the labels must differ
    assert lk.label_format(0, 50) == "%.3g"
    for lo, hi in ((300, 2200), (0, 12345)):  # ordinary ranges: never scientific notation
        fmt = lk.label_format(lo, hi)
        assert all("e" not in fmt % t for t in lk.nice_ticks(lo, hi))
    assert lk.auto_cmap("T") == "plasma" and lk.auto_cmap("U") == "turbo" and lk.auto_cmap("TPinnErr") == "coolwarm"
    assert lk.auto_cmap("UAssim") != "coolwarm" and lk.auto_cmap("TAssimCorr") == "coolwarm"
    for f in ("T", "U", "p", "k", "alpha.water", "TPinn", "whatever"):
        assert lk.auto_cmap(f) not in ("inferno", "magma")  # no black low end on the dark Studio background


def test_run_preset_suggestion(tmp_path):
    from aethercfd.case.model import load_case
    from aethercfd.commands.builtin.run_cmds import PRESETS, StartParams, _steps, suggest_preset

    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
    assert suggest_preset(load_case(case))[0] == "mesh-check-solve"  # blockMeshDict, no mesh
    (case / "Allrun").write_text("#!/bin/sh\n. $WM_PROJECT_DIR/bin/tools/RunFunctions\nrunApplication blockMesh\n")
    assert suggest_preset(load_case(case))[0] == "allrun"
    steps = _steps(load_case(case), StartParams(case=str(case), preset="auto"))
    assert [s.label for s in steps] == ["bash ./Allrun"] and steps[0].log == "log.Allrun"
    (case / "constant" / "polyMesh").mkdir(parents=True)
    (case / "constant" / "polyMesh" / "owner").write_text("")
    assert suggest_preset(load_case(case))[0] == "solve"
    assert set(PRESETS) >= {"auto", "solve", "allrun", "mesh-check-solve"}
    from aethercfd.commands.base import CommandError

    (case / "Allrun").unlink()
    with pytest.raises(CommandError, match="no Allrun"):
        _steps(load_case(case), StartParams(case=str(case), preset="allrun"))


def test_catalog_pitzdaily_is_steady_in_both_forks():
    from aethercfd.templates.catalog import TEMPLATES

    t = next(x for x in TEMPLATES if x.id == "pitzDaily")
    assert t.sources["foundation"].endswith("pitzDailySteady")  # v14's plain pitzDaily is transient


def test_copilot_has_the_postprocessing_tools():
    from aethercfd.copilot.tools import BASE_TOOLS as TOOLS

    names = {t.name for t in TOOLS}
    assert {"viewport_look", "viewport_showcase", "viewport_animation", "open_in_paraview", "render_image"} <= names
    run = next(t for t in TOOLS if t.name == "run_openfoam")
    assert "preset" in run.params.model_fields


def test_end_time_is_always_written():
    from aethercfd.templates.create import write_interval_for_end

    cd = ("FoamFile { class dictionary; object controlDict; }\nendTime 120;\ndeltaT 1;\nwriteControl timeStep;\n"
          "writeInterval 100;\n")
    assert write_interval_for_end(cd)[0] == "60"  # 120 steps, write every 60: step 120 is written
    assert write_interval_for_end(cd.replace("endTime 120", "endTime 200")) is None
    rt = cd.replace("timeStep", "adjustableRunTime").replace("endTime 120", "endTime 0.25").replace(
        "writeInterval 100", "writeInterval 0.1")
    v = float(write_interval_for_end(rt)[0])
    assert abs(0.25 / v - round(0.25 / v)) < 1e-12  # the end time is exactly a write time (no %.6g truncation)
    assert write_interval_for_end(cd.replace("timeStep", "clockTime")) is None
    # a prime step count: no "fix" that writes every step (thousands of folders); a warning instead
    value, why = write_interval_for_end(cd.replace("endTime 120", "endTime 2003"))
    assert value is None and "will not be written" in why
    assert write_interval_for_end(cd.replace("endTime 120", "endTime 50"))[0] == "50"  # end before first write
