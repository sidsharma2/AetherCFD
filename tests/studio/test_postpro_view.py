"""The viewport's Studio / Classic looks, Showcase, animation export and Open in ParaView."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

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
    for i, v in enumerate((1.0, 4.0), start=1):
        d = case / f"0.{i}"
        shutil.copytree(case / "0", d)
        (d / "p").write_text(re.sub(r"internalField\s+uniform\s+[^;]+;", f"internalField uniform {v};",
                                    (d / "p").read_text()))
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.resize(1200, 800)
    w.show()
    sv.open_case(case, create_project=True)
    a = AppAutomation(sv)
    v = a._viewport(open_if_missing=True)
    qtbot.waitUntil(lambda: bool(v.meta) and v.field.count() > 0, timeout=60000)
    assert v.show_field("U", "0.2").startswith("showing")
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen and v.scalar is not None, timeout=60000)
    yield v, sv, a
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _wait_drawn(qtbot, v):
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=60000)


def test_studio_and_classic_looks(vp, qtbot):
    v, sv, a = vp
    assert a.viewport_look("studio") == "look: studio"
    _wait_drawn(qtbot, v)
    assert v.plotter.renderer.GetGradientBackground()
    assert v.smoothed and "smoothed" in v.info.text()  # honest about the interpolation
    assert v.scalar_bar.GetTitle() == "|U| [m/s]" and v.scalar_bar.GetUseCustomLabels()
    assert v.hud.GetVisibility() and "U" in v.hud.GetInput()
    assert v.cmap.currentText() == "turbo"  # per-field map until the user picks one
    assert a.viewport_look("classic") == "look: classic"
    _wait_drawn(qtbot, v)
    assert not v.plotter.renderer.GetGradientBackground() and not v.smoothed
    assert not v.hud.GetVisibility() and not v.edge_actor.GetVisibility()
    assert sv.settings.viewport_look == "classic"  # remembered
    assert "unknown look" in v.set_look("neon")


def test_showcase_animation_and_paraview(vp, qtbot, monkeypatch, tmp_path):
    v, sv, a = vp
    assert a.showcase().startswith("showcase")  # the cavity is 2D: framed flat, Studio look
    _wait_drawn(qtbot, v)
    assert v.look == "studio" and not v.slice_on.isChecked()
    gif = tmp_path / "turn.gif"
    out = v.record_animation("turntable", str(gif), frames=12, fps=12)
    from PIL import Image

    assert out == str(gif) and Image.open(gif).n_frames == 12
    done = []
    msg = v.record_animation("time", str(tmp_path / "sweep.webp"), on_done=done.append)
    assert msg.startswith("recording a time sweep")
    qtbot.waitUntil(lambda: bool(done), timeout=60000)
    assert Image.open(done[0]).n_frames == v.time.count()
    launched = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: launched.append(argv))
    pv = Path(sv.settings.paraview_exe)
    if not pv.is_file():
        sv.settings.paraview_exe = str(shutil.which("cmd") or "C:/Windows/System32/cmd.exe")
    assert v.open_in_paraview().startswith("opening")
    script = Path(launched[0][1].split("=", 1)[1])
    text = script.read_text()
    assert "OpenFOAMReader" in text and "controlDict" in text
    assert ".aethercfd" in script.parts  # the script lives in the project's local folder, not in the case files


def test_show_field_waits_for_a_case_that_is_loading(vp, qtbot, tmp_path):
    v, sv, _a = vp
    other = tmp_path / "other"
    shutil.copytree(v.case_root, other, ignore=shutil.ignore_patterns(".aethercfd"))
    sv.open_case(other)
    assert v.field.count() == 0 or not v.meta  # no stale fields from the previous case
    msg = v.show_field("p", "0.1")
    assert "still loading" in msg or msg.startswith("showing")
    qtbot.waitUntil(lambda: v.field.currentText() == "p" and v.time.currentText() == "0.1", timeout=60000)


def test_time_sweep_is_aborted_cleanly(vp, qtbot, tmp_path):
    v, _sv, _a = vp
    lock = v.lock.isChecked()
    assert v.record_animation("time", str(tmp_path / "a.gif")).startswith("recording")
    assert "already being recorded" in v.record_animation("time", str(tmp_path / "b.gif"))
    v._on_fail(v.vid, v.gen, "boom")  # a time step could not be read
    assert v._sweep is None and v.lock.isChecked() == lock
    receivers = v.receivers("2stages_updated(PyObject)") if hasattr(v, "receivers") else 0
    assert receivers <= 1  # only the Visualization panel's own listener, if any
    assert v.record_animation("turntable", str(tmp_path / "t.gif"), frames=8).endswith("t.gif")


def test_paraview_script_path_literal(vp, monkeypatch, tmp_path):
    v, sv, _a = vp
    launched = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: launched.append(argv))
    sv.settings.paraview_exe = str(Path(__import__("sys").executable))
    v.open_in_paraview()
    text = Path(launched[0][1].split("=", 1)[1]).read_text()
    ns = {}
    line = next(ln for ln in text.splitlines() if "FileName=" in ln)
    lit = line.split("FileName=", 1)[1].rstrip(")")
    exec(f"x = {lit}", ns)
    assert Path(ns["x"]) == v.case_root / "system" / "controlDict"
