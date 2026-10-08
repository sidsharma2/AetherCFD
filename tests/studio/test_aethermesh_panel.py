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
