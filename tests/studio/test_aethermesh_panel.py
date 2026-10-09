"""AetherMesh panel: quality, grading, wall spacing and geometry tabs on the fixture case."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
vtk = pytest.importorskip("vtk")

from aethercfd.studio.app.services import AppServices  # noqa: E402
from aethercfd.studio.panels.aethermesh.panel import AetherMeshPanel  # noqa: E402
from aethercfd.studio.shell.panel_base import PanelContext  # noqa: E402

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    sv = AppServices()
    sv.open_case(case)
    p = AetherMeshPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    yield p
    sv.pool.waitForDone(30000)
    sv.bridge.close()


def test_quality_tab(panel, qtbot):
    panel.analyse_quality()
    qtbot.waitUntil(lambda: panel.q_tree.topLevelItemCount() == 4, timeout=30000)
    assert "400 cells" in panel.q_summary.toPlainText()


def test_grading_and_wall_tabs(panel):
    panel.design()
    assert "166 cells" in panel.g_out.toPlainText() and "((" in panel.g_out.toPlainText()
    panel.wall()
    assert "First cell height" in panel.w_out.toPlainText()


def test_geometry_tab(panel, qtbot, tmp_path):
    cube = vtk.vtkCubeSource()
    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(cube.GetOutputPort())
    w = vtk.vtkSTLWriter()
    w.SetInputConnection(tri.GetOutputPort())
    w.SetFileName(str(tmp_path / "cube.stl"))
    w.Write()
    panel.stl.setText(str(tmp_path / "cube.stl"))
    panel.analyse_geometry()
    qtbot.waitUntil(lambda: "snappyHexMesh" in panel.geo_out.toPlainText(), timeout=30000)
    assert "watertight" in panel.geo_out.toPlainText()


def test_holes_tab(panel):
    panel.holes()
    txt = panel.h_out.toPlainText()
    assert "ellipse" in txt and "0.36" in txt and "27.2°" in txt
    panel.h_levels.setPlainText("cap x")
    panel.holes()
    assert "cannot read" in panel.h_out.toPlainText()


def test_write_snappy_setup_button(panel, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from tests.unit.test_mesh_snappy import _sphere

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    case = Path(panel.sv.session.root)
    stl = _sphere(tmp_path / "ball.stl")
    panel.stl.setText(str(stl))
    panel.sn_cells.setValue(100_000)
    panel.write_snappy()
    assert (case / "system" / "snappyHexMeshDict").is_file()
    assert (case / "constant" / "triSurface" / "ball.stl").is_file()


def test_write_snappy_asks_before_replacing(panel, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from tests.unit.test_mesh_snappy import _sphere

    case = Path(panel.sv.session.root)
    bm = case / "system" / "blockMeshDict"
    before = bm.read_bytes()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Cancel)
    panel.stl.setText(str(_sphere(tmp_path / "ball.stl")))
    panel.sn_cells.setValue(100_000)
    panel.write_snappy()
    assert asked and "blockMeshDict" in asked[0]
    assert bm.read_bytes() == before and not (case / "system" / "snappyHexMeshDict").exists()


def test_bad_inputs_give_messages(panel):
    panel.g_first.setValue(0)
    panel.g_last.setValue(0)
    panel.g_max.setValue(0)
    panel.design()  # used to raise ValueError out of the slot
    assert "give n" in panel.g_out.toPlainText()
    panel.h_levels.setPlainText("cap 0.6295\nR0 1.0795")
    panel.h_r.setValue(5.0)
    panel.h_half.setValue(1.0)
    panel.holes()
    assert "<" in panel.h_out.toPlainText()  # the message keeps its "<" (HTML-escaped)
