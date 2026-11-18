"""Sections, views, flow paths and the Smooth / Cells toggle of the 3D viewport.

The fixture cavity checks the controls; two local cases (a multi-region CHT case with a probe-tip patch and
a dilution-air inlet, and a decomposed 3-D jet case) check them on real 3D results and write Smooth vs Cells
screenshots of the same section and contours (into tmp_path, and into $AETHERCFD_REPORT_DIR when it is set).
The local cases live under $AETHERCFD_TEST_CASES (folders cht_multiregion and decomposed_3d, or the paths in
$AETHERCFD_TEST_CHT_CASE / $AETHERCFD_TEST_3D_CASE); the tests skip when they are not set or not there.
"""

from __future__ import annotations

import json
import os
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


def _local_case(env: str, default: str) -> Path | None:
    """A case under $AETHERCFD_TEST_CASES (or the absolute path in ``env``); None when nothing is set."""
    root = os.environ.get("AETHERCFD_TEST_CASES")
    rel = os.environ.get(env)
    if rel and Path(rel).is_absolute():
        return Path(rel)
    return Path(root) / (rel or default) if root else None


CHT = _local_case("AETHERCFD_TEST_CHT_CASE", "cht_multiregion")
JET3D = _local_case("AETHERCFD_TEST_3D_CASE", "decomposed_3d")


def _app(qtbot, case: Path, *, create_project: bool = True):
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    sv.open_case(case, create_project=create_project)
    a = AppAutomation(sv)
    v = a._viewport(open_if_missing=True)
    qtbot.waitUntil(lambda: bool(v.meta) and v.field.count() > 0, timeout=600000)
    return v, sv, a


def _drawn(qtbot, v, timeout: int = 900000):
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=timeout)


def _close(sv):
    sv.pool.waitForDone(10000)
    sv.bridge.close()


@pytest.fixture
def vp(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    d = case / "0.1"
    shutil.copytree(case / "0", d)
    (d / "p").write_text(re.sub(r"internalField\s+uniform\s+[^;]+;", "internalField uniform 2;",
                                (d / "p").read_text()))
    v, sv, a = _app(qtbot, case)
    assert v.show_field("p", "0.1").startswith("showing")
    _drawn(qtbot, v)
    yield v, sv, a
    _close(sv)


def test_smooth_cells_toggle_is_a_remembered_choice(vp, qtbot):
    v, sv, a = vp
    assert v.look == "studio" and v.smoothing == "smooth" and v.btn_smooth.isChecked()
    assert not v.btn_edges.isEnabled()  # mesh edges belong to Cells mode
    msg = a.viewport_smoothing("cells", True)
    assert msg.startswith("cells") and "mesh edges" in msg
    _drawn(qtbot, v)
    assert not v.smoothed and "cells: each cell's own value" in v.info.text()
    assert "smoothed" not in v.scalar_bar.GetTitle()
    assert sv.settings.viewport_smoothing == "cells" and v.btn_edges.isEnabled() and v.btn_edges.isChecked()
    base = next(act for n, (act, _m) in v.actors.items() if n.endswith("internalMesh"))
    assert base.GetProperty().GetEdgeVisibility()  # the mesh shows
    a.viewport_look("classic")
    a.viewport_look("studio")
    _drawn(qtbot, v)
    assert v.smoothing == "cells"  # an explicit choice survives look changes
    assert v.set_smoothing("auto").startswith("smooth") and sv.settings.viewport_smoothing == "auto"
    a.viewport_look("classic")
    assert v.smoothing == "cells"  # auto again: the look decides
    assert "unknown smoothing" in v.set_smoothing("blurry")


def test_sections_menu_presets_and_display(vp, qtbot):
    v, _sv, a = vp
    assert "three orthogonal" in a.section("ortho3")
    _drawn(qtbot, v)
    secs = v._sections()
    assert len(secs) == 3 and all(f"stage:{s['id']}" in v.stage_info for s in secs)
    assert any(n.startswith("shell:") for n in v.actors)  # Studio: in a glass shell
    assert "stack of 4 slices along x" in a.section("stack", axis="x", n=4)
    _drawn(qtbot, v)
    info = v.stage_info[f"stage:{v._sections()[0]['id']}"]
    assert len(v._sections()) == 1 and "×4" in info["label"]
    msg = a.section("plane", points=[[0.02, 0.0, 0.0], [0.02, 0.1, 0.0], [0.02, 0.0, 0.01]],
                    show={"edges": True, "contours": True, "arrows": True})
    assert "edges, contours, arrows" in msg
    _drawn(qtbot, v)
    s = v._sections()[0]
    assert s["edges"] and s["contours"] and s["arrows"] and abs(abs(s["normal"][0]) - 1) < 1e-9
    assert "on one line" in v.add_sections("plane", points=[[0, 0, 0], [1, 1, 1], [2, 2, 2]])
    assert "no patch like" in a.section("plane", through="noSuchPatch")
    assert "through the centre of movingWall" in a.section("plane", through="moving wall", normal=[0, 1, 0])
    assert v._sections()[0]["through"].endswith("movingWall")
    assert "cut-away normal to x" in a.cutaway("x")
    _drawn(qtbot, v)
    assert any(s.get("cutaway") for s in v.stages)
    assert "removed" in a.section("mid-z", clear=True)
    assert not v._sections() and not any(s.get("cutaway") for s in v.stages)


def test_drag_pick_2d_view_and_line_probe(vp, qtbot):
    v, sv, a = vp
    a.section("mid-z")
    _drawn(qtbot, v)
    assert v.drag_section(True).startswith("drag")
    w, rep, _sid = v._plane_widget
    rep.SetOrigin(0.03, 0.05, 0.005)
    rep.SetNormal(1.0, 0.0, 0.0)
    w.InvokeEvent("InteractionEvent")  # what a drag does
    qtbot.waitUntil(lambda: v._sections()[0].get("normal") == [1.0, 0.0, 0.0], timeout=5000)
    _drawn(qtbot, v)
    assert "pos" not in v._sections()[0] and v._sections()[0]["origin"][0] == pytest.approx(0.03)
    assert v.drag_section(False) == "section handles off" and v._plane_widget is None
    # picking: three picks make a plane (the clicks go to the pick, not the camera)
    got = []
    v.start_pick(3, "pick", got.append)
    for p in ([0.01, 0.0, 0.0], [0.01, 0.1, 0.0], [0.01, 0.0, 0.01]):
        v.add_pick(p)
    assert got and len(got[0]) == 3 and v._pick is None
    # the line probe answers on the worker
    assert a.line_probe([0.001, 0.05, 0.005], [0.099, 0.05, 0.005], "p", 50).startswith("probing p")
    qtbot.waitUntil(lambda: v.last_line is not None, timeout=60000)
    assert v.last_line["inside"] == 50 and set(v.last_line["values"]) == {2.0}
    assert v.line_csv(v.last_line).startswith("s,x,y,z,value")
    # open the section in 2D: a second viewport looking straight at it
    assert v.open_section_2d().startswith("opened the section")
    other = sv.host.build_panel("aether.viewport#sec2d")
    qtbot.waitUntil(lambda: bool(other.stages) and "stage:sec2d" in other.stage_info, timeout=120000)
    cam = other.plotter.renderer.GetActiveCamera()
    qtbot.waitUntil(lambda: bool(cam.GetParallelProjection()), timeout=20000)
    assert other.view_opts.get("base") == "hidden" and other.field.currentText() == "p"


def test_views_bookmarks_and_quad(vp, qtbot, tmp_path):
    v, _sv, a = vp
    for name in ("front", "top", "right", "iso", "x", "back"):
        assert a.camera(name).startswith("camera")
    a.camera("top")
    pos = v.plotter.renderer.GetActiveCamera().GetPosition()
    assert a.bookmark("save", "lid view").startswith("saved the view 'lid view'")
    a.camera("front")
    assert a.camera("lid view") == "view 'lid view'"
    assert v.plotter.renderer.GetActiveCamera().GetPosition() == pytest.approx(pos)
    assert a.bookmark("list") == ["lid view"]
    store = json.loads((v._out_dir("views") / "bookmarks.json").read_text())
    assert str(v.case_root) in store
    imgs = a.bookmark("render", "")
    assert isinstance(imgs, list) and len(imgs) == 1 and Path(imgs[0]).is_file()
    assert a.bookmark("delete", "lid view").startswith("deleted") and a.bookmark("list") == []
    assert "unknown view" in a.camera("sideways")
    assert a.quad(True).startswith("quad view")
    assert len(v._quad) == 3 and v.plotter.ren_win.GetRenderers().GetNumberOfItems() >= 4
    ren = v._quad[0][0]
    assert ren.GetActiveCamera().GetParallelProjection() and ren.GetActors().GetNumberOfItems() >= 1
    v.request_load()
    _drawn(qtbot, v)  # a reload keeps all four views filled
    assert v._quad[1][0].GetActors().GetNumberOfItems() >= len(v.actors)
    assert a.quad(False) == "quad view off" and v._quad is None
    assert tuple(v.plotter.renderer.GetViewport()) == (0.0, 0.0, 1.0, 1.0)


def test_flow_paths_controls(vp, qtbot):
    v, _sv, a = vp
    assert v.inlet_paths() == []  # the lid-driven cavity has no inlet
    assert "no inlet patch found" in a.flow_paths("inlets")
    assert a.flow_paths("point", point=[0.05, 0.05, 0.005], color_by="residence_time").startswith("flow paths")
    _drawn(qtbot, v)
    st = next(s for s in v.stages if s.get("flow"))
    assert st["seed"] == "point" and st["color_by"] == "residence_time" and st["tubes"]  # tubes in Studio
    assert a.flow_paths("line", p1=[0.01, 0.05, 0.005], p2=[0.09, 0.05, 0.005]).startswith("flow paths from a line")
    assert len([s for s in v.stages if s.get("flow")]) == 1  # replaces the earlier paths
    assert v.set_pathlines(True).startswith("pathlines")
    _drawn(qtbot, v)
    info = next(i for k, i in v.stage_info.items() if k.startswith("stage:flow"))
    assert info["label"].startswith("pathlines U (2 saved times"), info  # icoFoam: transient, 2 saved times
    v._steady = True  # a steady run: its saved times are iterations, so streamlines, said so
    v.request_load()
    _drawn(qtbot, v)
    info = next(i for k, i in v.stage_info.items() if k.startswith("stage:flow"))
    assert "not pathlines" in info["label"] and "steady run" in info["notes"][0], info
    assert a.flow_paths(clear=True) == "flow paths removed" and not any(s.get("flow") for s in v.stages)


# ---------------------------------------------------------------------------------------- real 3D cases
def _report(tmp_path: Path) -> list[Path]:
    out = [tmp_path / "shots"]
    if os.environ.get("AETHERCFD_REPORT_DIR"):
        out.append(Path(os.environ["AETHERCFD_REPORT_DIR"]))
    for d in out:
        d.mkdir(parents=True, exist_ok=True)
    return out


def _shot(v, dirs: list[Path], name: str) -> Path:
    f = dirs[0] / f"{name}.png"
    v.render_image(str(f), 1600, 900, background="look", base="as shown")
    for d in dirs[1:]:
        shutil.copy2(f, d / f.name)
    return f


def _frame_start(v, frac: float = 0.3) -> None:
    """Zoom onto the first part of the domain along its long axis (where the inlets and the probe tip are)."""
    b = list(v.bounds)
    ext = [b[1] - b[0], b[3] - b[2], b[5] - b[4]]
    k = ext.index(max(ext))
    b[2 * k + 1] = b[2 * k] + frac * ext[k]
    v.frame_bounds(b, padding=0.05)


def _real(qtbot, case: Path | None, tmp_path, monkeypatch):
    if case is None:
        pytest.skip("set AETHERCFD_TEST_CASES to a folder of local test cases")
    if not (case / "system" / "controlDict").is_file():
        pytest.skip(f"{case} is not here")
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    return _app(qtbot, case, create_project=False)


@pytest.mark.slow
@pytest.mark.timeout(1800)  # two big real cases: minutes when the machine is busy
def test_cht_probe_tip_section_and_dilution_paths(qtbot, tmp_path, monkeypatch):
    """'show me a section through the probe tip and the flow paths from the dilution inlet, with the cells
    visible' on a multi-region CHT case with a probe-tip patch."""
    v, sv, a = _real(qtbot, CHT, tmp_path, monkeypatch)
    dirs = _report(tmp_path)
    try:
        assert v.show_field("U", smoothing="smooth").startswith("showing")
        _drawn(qtbot, v)
        assert "DILUTION_AIR_INLET" in [p.split("/")[-1] for p in v.inlet_paths()]
        msg = a.section("plane", through="probe tip", axis="z", show={"contours": True})
        assert "through the centre of PROBE_TIP_REGION_FRAGMENT" in msg, msg
        _drawn(qtbot, v)
        info = v.stage_info[f"stage:{v._sections()[0]['id']}"]
        assert info["cells"] > 100 and info["association"] == "point" and "smoothed" in info["label"]
        lines = v.stage_info.get(f"stage:{v._sections()[0]['id']}@lines")
        assert lines is not None and lines["cells"] > 0
        v.set_view("front")
        v.frame_stage(v._sections()[0]["id"])
        _shot(v, dirs, "cht_probe_tip_section_U_smooth")
        assert "smoothed" in v.scalar_bar.GetTitle()
        a.viewport_smoothing("cells", True)
        _drawn(qtbot, v)
        info = v.stage_info[f"stage:{v._sections()[0]['id']}"]
        assert info["association"] == "cell" and "smoothed" not in v.scalar_bar.GetTitle()
        _shot(v, dirs, "cht_probe_tip_section_U_cells")
        msg = a.flow_paths("inlets", inlets=["dilution inlet"], color_by="residence_time")
        assert msg.startswith("flow paths from DILUTION_AIR_INLET"), msg
        _drawn(qtbot, v)
        fi = next(i for k, i in v.stage_info.items() if k.startswith("stage:flow"))
        assert fi["cells"] > 0 and fi["title"] == "residence time [s]", fi
        v.set_view("iso")
        _shot(v, dirs, "cht_dilution_flow_paths_residence_time")
        # the section keeps its bar; the flow paths get their own, with the residence time
        assert v.flow_bar.GetVisibility() and v.flow_bar.GetTitle().startswith("residence time")
        assert "|U|" in v.scalar_bar.GetTitle()
    finally:
        _close(sv)


@pytest.mark.slow
@pytest.mark.timeout(1800)  # two big real cases: minutes when the machine is busy
def test_decomposed_3d_ortho_sections_smooth_vs_cells(qtbot, tmp_path, monkeypatch):
    v, sv, a = _real(qtbot, JET3D, tmp_path, monkeypatch)
    dirs = _report(tmp_path)
    try:
        assert v.show_field("T").startswith("showing")
        _drawn(qtbot, v)
        a.add_stage("contour", {"field": "T", "n": 4}, smoothing="smooth")
        msg = a.section("ortho3", show={"contours": True})
        assert "three orthogonal" in msg
        _drawn(qtbot, v)
        assert all(f"stage:{s['id']}@lines" in v.stage_info for s in v._sections())
        v.set_view("iso")
        _frame_start(v)
        _shot(v, dirs, "jet3d_ortho_sections_T_smooth")
        assert v.scalar_bar.GetTitle().endswith("smoothed")
        smooth_iso = next(i for k, i in v.stage_info.items() if i.get("type") == "contour")
        a.viewport_smoothing("cells", True)
        _drawn(qtbot, v)
        cells_iso = next(i for k, i in v.stage_info.items() if i.get("type") == "contour")
        assert "per cell" in cells_iso["label"] and "per cell" not in smooth_iso["label"]
        _shot(v, dirs, "jet3d_ortho_sections_T_cells")
        a.quad(True)
        _shot_quad = dirs[0] / "jet3d_quad_view.png"
        v.plotter.screenshot(str(_shot_quad))
        for d in dirs[1:]:
            shutil.copy2(_shot_quad, d / _shot_quad.name)
        a.quad(False)
        msg = a.flow_paths("inlets")
        assert msg.startswith("flow paths from"), msg
        _drawn(qtbot, v)
        fi = next(i for k, i in v.stage_info.items() if k.startswith("stage:flow"))
        assert fi["cells"] > 0
        _shot(v, dirs, "jet3d_flow_paths_from_inlets")
    finally:
        _close(sv)
