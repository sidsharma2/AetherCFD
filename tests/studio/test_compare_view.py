"""Side-by-side compare: linked viewports, one colour range, a diverging range for the difference."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("vtk")

from aethercfd.results.write_fields import field_files  # noqa: E402
from aethercfd.studio.app.automation import AppAutomation  # noqa: E402
from aethercfd.studio.app.services import AppServices  # noqa: E402
from aethercfd.studio.panels import register_builtin_panels  # noqa: E402
from aethercfd.studio.shell.main_window import MainWindow  # noqa: E402

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
    w, sv = win
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
