"""Panels opened on demand become tabs on their side instead of one more column each; recipes keep their splits."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow
from aethercfd.studio.shell.panel_base import Area


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    qtbot.wait(300)
    yield w, sv
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _areas(host) -> set[int]:
    return {id(d.dockAreaWidget()) for d in host.docks().values()
            if not d.isClosed() and d.dockAreaWidget() is not None and not d.isFloating()}


def test_panels_opened_on_demand_become_tabs_on_their_side(win, qtbot):
    _w, sv = win
    host = sv.host
    right = [s for s in sv.panels.specs() if s.default_area is Area.RIGHT and not s.multi_instance][:4]
    assert len(right) == 4
    first = host.open_panel(right[0])
    qtbot.wait(100)
    before = len(_areas(host))
    docks = [host.open_panel(s) for s in right[1:]]
    qtbot.wait(100)
    area = first.dockAreaWidget()
    assert all(d.dockAreaWidget() is area for d in docks), "each on-demand panel opened a new column"
    assert len(_areas(host)) == before
    assert area.currentDockWidget() is docks[-1]  # the one just opened is the tab in front
    bottom = [s for s in sv.panels.specs() if s.default_area is Area.BOTTOM and not s.multi_instance
              and not s.requires][:2]
    if len(bottom) == 2:
        b1, b2 = host.open_panel(bottom[0]), host.open_panel(bottom[1])
        assert b1.dockAreaWidget() is b2.dockAreaWidget()
        assert b1.dockAreaWidget() is not area


def test_an_explicit_area_still_splits(win, qtbot):
    _w, sv = win
    host = sv.host
    # (panels of one tab_group always tab together: not those)
    right = [s for s in sv.panels.specs() if s.default_area is Area.RIGHT and not s.multi_instance
             and not s.tab_group][:2]
    a = host.open_panel(right[0])
    b = host.open_panel(right[1], area=Area.RIGHT)  # a layout recipe: its own column, as designed
    qtbot.wait(100)
    assert a.dockAreaWidget() is not b.dockAreaWidget()
