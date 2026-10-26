"""Closing the window saves the layout; that must never render a viewport (its GL window is being destroyed)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("vtk")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow


def test_saving_and_closing_never_renders_a_viewport_that_drew_nothing(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.show()
    sv.host.open_panel(sv.panels.get("aether.viewport"), instance="nc1")
    vp = sv.host.build_panel("aether.viewport#nc1")
    qtbot.wait(100)
    assert not vp.plotter.renderer.camera.is_set  # no case: nothing drawn, the camera never set
    renders = []
    monkeypatch.setattr(vp.plotter, "render", lambda *a, **k: renders.append(1))
    st = vp.save_state()
    assert set(st["camera"]) == {"position", "focal_point", "view_up"}
    assert renders == [] and not vp.plotter.renderer.camera.is_set  # pyvista's plotter.camera would render
    w.close()  # closeEvent -> layouts.save_now -> every panel's save_state, then dispose
    assert renders == []
    assert "camera" not in vp.save_state()  # a closed viewport: no camera read at all
    sv.pool.waitForDone(10000)
