"""Side-by-side compare: linked viewports, one colour range, a diverging range for the difference."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("vtk")

from aethercfd.results.write_fields import field_files
from aethercfd.studio.app.automation import AppAutomation
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    d = case / "0.1"
    shutil.copytree(case / "0", d)
    (d / "p").write_text(re.sub(r"internalField\s+uniform\s+[^;]+;", "internalField uniform 2;", (d / "p").read_text()))
    for rel, text in field_files(case, "0.1", {"pB": np.linspace(1, 3, 400), "pD": np.linspace(-0.5, 0.2, 400)},
                                 like="p").items():
        (case / rel).write_text(text)
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.show()
    sv.open_case(case)
    yield w, sv
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def test_compare_fields(win, qtbot):
    _w, sv = win
    msg = AppAutomation(sv).compare_fields("p", "pB", "pD", "0.1")
    assert msg.startswith("opened 3 linked viewports")
    a, b, c = (sv.host.build_panel(f"aether.viewport#cmp{x}") for x in "ABC")
    qtbot.waitUntil(lambda: a.lock.isChecked() and b.lock.isChecked() and c.lock.isChecked(), timeout=60000)
    assert a.range == b.range and a.range[0] <= 1.0 and a.range[1] >= 3.0
    assert c.cmap.currentText() == "coolwarm" and c.range[0] == pytest.approx(-c.range[1])
    # rotating the first moves the others
    cam = a.plotter.camera
    cam.Azimuth(30)
    a._cam_moved()
    assert np.allclose(b.plotter.camera.GetPosition(), cam.GetPosition())
    assert np.allclose(c.plotter.camera.GetPosition(), cam.GetPosition())


def test_compare_reports_missing_fields(win):
    _w, sv = win
    assert AppAutomation(sv).compare_fields("p", "nope", "", "0.1").startswith("not written at t = 0.1: nope")


def test_reader_lists_fields_written_at_one_time_only(win):
    from aethercfd.results.foam_reader import FoamResults

    _w, sv = win
    r = FoamResults(sv.session.root)
    assert {"pB", "pD"} <= set(r.refresh().cell_arrays)  # only in 0.1, not in 0


def test_empty_saved_layout_falls_back_to_the_recipe(win):
    import base64

    from aethercfd.studio.layout.layout_manager import _has_panels

    w, _sv = win
    assert not _has_panels(b'<Widget Name="aether.workbench#main" Closed="0"/>')
    assert _has_panels(b'<Widget Name="aether.case_tree#main" Closed="0"/>')
    snap = w.layouts.snapshot()
    empty = dict(snap, ads_state=base64.b64encode(b'<QtAdvancedDockingSystem><Widget Name="aether.workbench#main" '
                                                  b'Closed="0"/></QtAdvancedDockingSystem>').decode())
    assert not w.layouts._apply_variant(empty)  # the restore then applies the perspective's recipe
