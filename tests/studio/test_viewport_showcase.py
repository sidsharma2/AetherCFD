"""The showcase shows every selected region: its cut passes through all of them when one plane can, else it
shows their surfaces. The multi-region heater's solids (tests/fixtures/cases/esi_multiRegionHeater) are the
case that went wrong: a mid-plane of their combined box cut only the heater's post, so the two thin plates
(leftSolid, rightSolid: y 0..0.01) were not shown at all."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.app.automation import AppAutomation
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.panels.viewport.panel import showcase_cut
from aethercfd.studio.shell.main_window import MainWindow

HEATER = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_multiRegionHeater"
SOLIDS = {"heater", "leftSolid", "rightSolid"}


def test_showcase_cut_rule():
    post = [-0.01, 0.01, -0.04, 0.01, -0.05, 0.05]  # the heater: a post below a plate
    left = [-0.1, -0.01, 0.0, 0.01, -0.05, 0.05]
    right = [0.01, 0.1, 0.0, 0.01, -0.05, 0.05]
    axis, pos = showcase_cut([post, left, right])
    y = -0.04 + pos / 1000 * 0.05
    assert axis == 1 and 0.0 < y < 0.01  # normal to y, inside the plates (not the old y = -0.015)
    assert showcase_cut([[0, 1, 0, 1, 0, 0.2]]) == (2, 500)  # one region: the middle, normal to the thin axis
    # two boxes that overlap on no axis: no single plane passes through both
    assert showcase_cut([[0, 1, 0, 1, 0, 1], [2, 3, 2, 3, 2, 3]]) is None
    # overlapping only along x: the cut is normal to x even though x is the longest extent
    assert showcase_cut([[0, 2, 0, 1, 0, 1], [1, 3, 2, 3, 2, 3]])[0] == 0
    assert showcase_cut([]) is None


@pytest.fixture
def vp(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "heater"
    shutil.copytree(HEATER, case)
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    sv.open_case(case, create_project=True)
    a = AppAutomation(sv)
    v = a._viewport(open_if_missing=True)
    qtbot.waitUntil(lambda: bool(v.meta) and v.field.count() > 0, timeout=60000)
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=60000)
    yield v, a
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _settled(qtbot, v):
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen and not v._showcase_pending, timeout=60000)


def _in_view(v, b) -> bool:
    """The box's eight corners project inside the viewport."""
    ren = v.plotter.renderer
    w, h = ren.GetSize()
    for x in b[0:2]:
        for y in b[2:4]:
            for z in b[4:6]:
                ren.SetWorldPoint(x, y, z, 1.0)
                ren.WorldToDisplay()
                dx, dy, _dz = ren.GetDisplayPoint()
                if not (-1 <= dx <= w + 1 and -1 <= dy <= h + 1):
                    return False
    return True


def test_showcase_of_the_solids_shows_every_solid(vp, qtbot):
    v, a = vp
    msg = a.showcase("solids")
    assert msg.startswith("showcase in solids"), msg
    _settled(qtbot, v)
    assert set(v.region_sel) == SOLIDS and v.slice_on.isChecked()
    assert set(v.region_bounds) == SOLIDS
    cells = {}
    for name, (_actor, mapper) in v.actors.items():
        if name.startswith("slice"):
            cells[v.block_regions[name]] = mapper.GetInput().GetNumberOfCells()
    assert set(cells) == SOLIDS and all(n > 0 for n in cells.values()), cells  # the cut passes through all three
    assert v.slice_axis.currentText() == "Y"
    for r in SOLIDS:
        assert _in_view(v, v.region_bounds[r]), r  # the camera frames every solid
    again = v.showcase()  # now with the solids' extents known: says where it cut
    assert again.startswith("showcase in solids: slice normal to y through"), again
    _settled(qtbot, v)


def test_showcase_without_a_common_plane_shows_the_surfaces(vp, qtbot, monkeypatch):
    v, a = vp
    a.showcase("solids")
    _settled(qtbot, v)
    # make the solids' extents overlap on no axis (as for regions side by side in all three directions)
    fake = {"heater": [0, 1, 0, 1, 0, 1], "leftSolid": [2, 3, 2, 3, 2, 3], "rightSolid": [4, 5, 4, 5, 4, 5]}
    monkeypatch.setattr(v, "region_bounds", fake)
    msg = v.showcase()
    assert "no single plane passes through all of" in msg and not v.slice_on.isChecked(), msg
    _settled(qtbot, v)
    assert not any(k.startswith("slice") for k in v.actors)
    shown = {v.block_regions.get(k) for k in v.actors if k.endswith("internalMesh")}
    assert shown == SOLIDS  # every selected region's surface is drawn
    for r in SOLIDS:
        assert _in_view(v, v.region_bounds[r]), r
