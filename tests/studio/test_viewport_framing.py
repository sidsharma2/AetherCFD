"""The camera stays framed on what the viewport shows: after a stage is added, the base mesh is hidden or the
viewport is resized (a panel opened beside it), until the user moves the camera themselves. The README's
schlieren screenshot needed a manual Fit: hiding the base mesh while the Visualization panel opened left the
camera zoomed past the ends of the domain."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from aethercfd.studio.app.automation import AppAutomation
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


@pytest.fixture
def vp(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    d = case / "0.1"
    shutil.copytree(case / "0", d)
    (d / "p").write_text(re.sub(r"internalField\s+uniform\s+[^;]+;", "internalField uniform 2;",
                                (d / "p").read_text()))
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
    assert v.show_field("p", "0.1").startswith("showing")
    _drawn(qtbot, v)
    yield v, w, a
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _drawn(qtbot, v):
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=60000)


def _corners_on_screen(v) -> tuple[bool, float]:
    """(every corner of what is shown projects inside the view, the fraction of the view's width or height
    it fills, whichever is larger)."""
    b = v._shown_box() if hasattr(v, "_shown_box") else v.plotter.renderer.ComputeVisiblePropBounds()
    ren = v.plotter.renderer
    w, h = ren.GetSize()
    xs, ys = [], []
    for x in b[0:2]:
        for y in b[2:4]:
            for z in b[4:6]:
                ren.SetWorldPoint(x, y, z, 1.0)
                ren.WorldToDisplay()
                dx, dy, _ = ren.GetDisplayPoint()
                xs.append(dx)
                ys.append(dy)
    inside = min(xs) >= -1 and max(xs) <= w + 1 and min(ys) >= -1 and max(ys) <= h + 1
    return inside, max((max(xs) - min(xs)) / w, (max(ys) - min(ys)) / h)


def _resize(qtbot, v, w, width: int) -> None:
    w.resize(width, 900)
    qtbot.waitUntil(lambda: abs(v.plotter.width() - (v.width())) < 40 and v.width() <= width, timeout=10000)
    qtbot.wait(400)  # the resize settles and any re-framing runs


def test_camera_stays_framed_on_what_is_shown(vp, qtbot):
    v, w, a = vp
    assert v._is2d and v.look == "studio"
    sid = a.add_stage("slice", {"axis": "z", "pos": 0.5})
    qtbot.waitUntil(lambda: f"stage:{sid}" in v.stage_info, timeout=60000)
    _drawn(qtbot, v)
    v.set_view_options({"base": "hidden"})  # the stage alone, as for the schlieren screenshot
    _drawn(qtbot, v)
    base = next(act for n, (act, _m) in v.actors.items() if n.endswith("internalMesh"))
    assert not base.GetVisibility()
    inside, fill = _corners_on_screen(v)
    assert inside and fill > 0.5, (inside, fill)
    # a panel opened beside the viewport (here: a narrower window) leaves it tall and narrow
    _resize(qtbot, v, w, 520)
    inside, fill = _corners_on_screen(v)
    assert inside, "the domain's ends are outside the view after the resize"
    assert fill > 0.5, fill  # and still framed, not left zoomed out
    _resize(qtbot, v, w, 1400)
    inside, fill = _corners_on_screen(v)
    assert inside and fill > 0.5, (inside, fill)

    # the base mesh back: still framed
    v.set_view_options({})
    _drawn(qtbot, v)
    assert _corners_on_screen(v)[0]


def test_a_camera_the_user_moved_stays_put(vp, qtbot):
    v, w, _a = vp
    pl = v.plotter
    centre = QPointF(pl.width() / 2, pl.height() / 2)
    ev = QWheelEvent(centre, QPointF(pl.mapToGlobal(centre.toPoint())), QPoint(0, 0), QPoint(0, 120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    scale0 = pl.renderer.GetActiveCamera().GetParallelScale()
    QApplication.sendEvent(pl, ev)  # the user zooms in
    cam = pl.renderer.GetActiveCamera()
    assert cam.GetParallelScale() < scale0 and v._framing is None
    before = (cam.GetPosition(), cam.GetFocalPoint(), cam.GetParallelScale())
    _resize(qtbot, v, w, 700)
    v.set_view_options({"base": "outline"})
    _drawn(qtbot, v)
    qtbot.wait(200)
    after = (cam.GetPosition(), cam.GetFocalPoint(), cam.GetParallelScale())
    assert all(x == pytest.approx(y) for x, y in zip(after, before, strict=True)), (before, after)
    # Fit frames it again, and from then on it is kept framed
    v._fit()
    _resize(qtbot, v, w, 1200)
    assert _corners_on_screen(v)[0]
