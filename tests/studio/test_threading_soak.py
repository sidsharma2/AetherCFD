"""ADR 0008 checks: renders stay on the GUI thread, and a multi-region viewport survives re-reads and closes
while its worker is still loading (the 2d8564e deadlock: the GUI drew the reader's own objects while the
worker read again).

The fixture tests/fixtures/cases/esi_multiRegionHeater is ESI's multiRegionHeater tutorial at t = 0.4
(two fluid and three solid regions).
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("vtk")

from PySide6.QtWidgets import QApplication

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.panels.viewport import render_thread
from aethercfd.studio.shell.main_window import MainWindow

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_multiRegionHeater"


@pytest.fixture
def heater(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "heater"
    shutil.copytree(FIX, case)
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.resize(1100, 750)
    w.show()
    sv.open_case(case, create_project=True)
    render_thread.VIOLATIONS.clear()
    yield w, sv
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _viewport(sv, qtbot, instance: str):
    sv.host.open_panel(sv.panels.get("aether.viewport"), instance=instance)
    vp = sv.host.build_panel(f"aether.viewport#{instance}")
    qtbot.waitUntil(lambda: bool(vp.meta) and vp.field.count() > 0, timeout=60000)
    return vp


def _drawn(vp) -> bool:
    return getattr(vp, "drawn_gen", -1) == vp.gen


def test_the_guard_records_a_render_off_the_gui_thread(heater, qtbot):
    _w, sv = heater
    vp = _viewport(sv, qtbot, "g1")
    qtbot.waitUntil(lambda: _drawn(vp), timeout=60000)
    assert render_thread.VIOLATIONS == []  # every render so far ran on the GUI thread
    # fire the render window's StartEvent from a worker thread: the observer runs exactly as it would for a
    # real off-thread Render(), without touching OpenGL from that thread
    t = threading.Thread(target=lambda: vp.plotter.render_window.InvokeEvent("StartEvent"), name="soak-worker")
    t.start()
    t.join(10)
    assert len(render_thread.VIOLATIONS) == 1
    assert "soak-worker" in render_thread.VIOLATIONS[0] and "viewport#g1" in render_thread.VIOLATIONS[0]
    render_thread.VIOLATIONS.clear()


def test_soak_regions_rereads_and_closes_while_the_worker_loads(heater, qtbot):
    """200 region switches without waiting for the previous load; every tenth cycle closes the viewport while
    a load is in flight and opens a new one. Ends with no hang, no slot exception and no off-thread render."""
    _w, sv = heater
    errors: list[str] = []
    import sys
    import traceback

    prev = sys.excepthook
    sys.excepthook = lambda t, v, tb: errors.append("".join(traceback.format_exception(t, v, tb)))
    try:
        vp = _viewport(sv, qtbot, "s0")
        assert vp.regions() == ["bottomWater", "heater", "leftSolid", "rightSolid", "topAir"]
        # the 2d8564e hang needed the showcase path: a mid-plane slice in a glass shell with smooth (cell-to-point)
        # data, then "the solids only" while the GUI kept painting
        actions = [lambda v: v.showcase(), lambda v: v.showcase("solids"), lambda v: v.set_regions("all"),
                   lambda v: v.set_regions("fluid"), lambda v: v.showcase("heater"), lambda v: v.set_regions("all")]
        t0 = time.monotonic()
        opened = 0
        for i in range(200):
            msg = actions[i % len(actions)](vp)  # re-read; the previous load may still be running
            assert not str(msg).startswith("cannot"), msg
            for _ in range(3):  # paint while the worker reads
                QApplication.processEvents()
                time.sleep(0.005)
            if i % 10 == 9:
                assert sv.host.close_panel(f"aether.viewport#s{opened}")  # closed with a load in flight
                QApplication.processEvents()
                opened += 1
                vp = _viewport(sv, qtbot, f"s{opened}")
        vp.set_regions("all")
        qtbot.waitUntil(lambda: _drawn(vp) and vp.region_sel is None, timeout=120000)  # the last load lands
        elapsed = time.monotonic() - t0
    finally:
        sys.excepthook = prev
    assert errors == []
    assert render_thread.VIOLATIONS == []
    assert {f"{r}/internalMesh" for r in ("bottomWater", "heater", "leftSolid", "rightSolid", "topAir")} <= set(
        vp.actors)
    assert elapsed < 240  # a deadlock would stop here instead
