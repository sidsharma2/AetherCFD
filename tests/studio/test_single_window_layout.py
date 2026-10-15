"""Single-window layouts: by default every perspective keeps its panels docked in the main window, even with
several monitors; Settings.use_extra_screens opts in to the multi-screen recipes (panels floated elsewhere)."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.layout.layout_store import LayoutStore
from aethercfd.studio.layout.recipes import perspective
from aethercfd.studio.layout.screens import Rect


def test_single_window_variants_only_match_single_window_layouts(tmp_path):
    store = LayoutStore(tmp_path)
    three = [{"key": f"s{i}"} for i in range(3)]
    store.save_variant("run", "fp-multi", {"screens": three, "saved_at": "1", "floating": [{"panels": ["x"]}]})
    # an older multi-screen layout must not be reused for the single-window mode on the same desk
    assert store.find_variant("run", "fp-multi|single-window", "a", 3, single_window=True) is None
    assert store.find_variant("run", "other", "a", 3, single_window=False)[0] == "similar"
    store.save_variant("run", "fp-multi|single-window", {"screens": three, "saved_at": "2", "single_window": True})
    how, v = store.find_variant("run", "elsewhere|single-window", "a", 3, single_window=True)
    assert how == "similar" and v["single_window"] is True


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    from aethercfd.studio.app.services import AppServices
    from aethercfd.studio.panels import register_builtin_panels
    from aethercfd.studio.shell.main_window import MainWindow

    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.show()
    yield w, sv
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _three_screens(w):
    s0 = w.layouts.screens[0]
    g = s0.geometry
    return [replace(s0, index=i, name=f"S{i}", geometry=Rect(g.x + i * g.w, g.y, g.w, g.h),
                    available=Rect(g.x + i * g.w, g.y, g.w, g.h), primary=i == 0) for i in range(3)]


def _floating_panels(w) -> set[str]:
    return {d.objectName() for fw in w.layouts.host.floating_containers() if fw.isVisible() for d in fw.dockWidgets()}


def test_run_monitor_stays_in_the_main_window_by_default(win):
    w, sv = win
    assert sv.settings.use_extra_screens is False
    w.layouts.screens = _three_screens(w)
    w.layouts.apply_recipe(perspective("run"))
    assert not _floating_panels(w)
    for panel in ("aether.residuals", "aether.run_status", "aether.solver_log"):
        name = next(n for n in w.layouts.open_panel_names() if n.startswith(panel))
        d = sv.host.dock(name)
        assert d is not None and not d.isFloating() and d.dockAreaWidget() is not None, panel
    assert w.layouts._layout_key().endswith("|single-window")


def test_extra_screens_opt_in_floats_the_run_monitor(win, monkeypatch):
    w, sv = win
    sv.settings.use_extra_screens = True
    screens = _three_screens(w)
    w.layouts.screens = screens
    monkeypatch.setattr("aethercfd.studio.layout.layout_manager.current_screens", lambda: screens)
    w.layouts.apply_recipe(perspective("run"))
    assert any(n.startswith("aether.residuals") for n in _floating_panels(w))
    assert not w.layouts._layout_key().endswith("|single-window")
