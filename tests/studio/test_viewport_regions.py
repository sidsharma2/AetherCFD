"""The 3D viewport on a multi-region (CHT) case: the Regions control, set_regions, slices and stages on the
shown regions only, the copilot's regions parameter, and outward-wound surfaces.

The fixture (tests/unit/test_regions.py) is the cavity mesh twice: region "a" is the solid (T = 40 at t = 0.3,
p = 40) and region "b" the fluid (U, p = 4), so every value shows which region it came from.
"""

from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from aethercfd.studio.app.automation import AppAutomation
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow
from tests.unit.test_regions import two_region_case


@pytest.fixture
def vp(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = two_region_case(tmp_path)
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
    _drawn(qtbot, v)
    yield v, sv, a
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _drawn(qtbot, v):
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=60000)


def _checked(v) -> set[str]:
    return {v.patches.item(i).text() for i in range(v.patches.count())
            if v.patches.item(i).checkState() == Qt.CheckState.Checked}


def test_regions_control_and_set_regions(vp, qtbot):
    v, _sv, _a = vp
    assert not v.region_btn.isHidden() and v.regions() == ["a", "b"]
    assert v.region_kinds == {"a": "solid", "b": "fluid"}  # from regionProperties
    assert v.region_sel is None and {"a/internalMesh", "b/internalMesh"} <= set(v.actors)
    labels = [act.text() for act in v.region_menu.actions()]
    assert labels[:3] == ["All regions", "Fluid only", "Solid only"] and "a  (solid)" in labels

    msg = v.set_regions("solids")
    assert msg.startswith("regions shown: a") and v.region_sel == ["a"] and "a" in v.region_btn.text()
    _drawn(qtbot, v)
    assert set(v.actors) == {"a/internalMesh"}
    assert "/a/internalMesh" in _checked(v) and "/b/internalMesh" not in _checked(v)  # the expert list follows

    assert v.set_regions("zz").startswith("cannot show regions: no region 'zz'") and v.region_sel == ["a"]
    assert v.set_regions(["B"]).startswith("regions shown: b")  # names in any case
    _drawn(qtbot, v)
    assert set(v.actors) == {"b/internalMesh"}
    assert v.set_regions("all").startswith("regions shown: all") and v.region_sel is None
    _drawn(qtbot, v)
    assert {"a/internalMesh", "b/internalMesh"} <= set(v.actors)

    # unticking a region's mesh in the expert 'Show' list hides that region (and the Regions button says so)
    item = next(v.patches.item(i) for i in range(v.patches.count()) if v.patches.item(i).text() == "/a/internalMesh")
    item.setCheckState(Qt.CheckState.Unchecked)
    assert v.region_sel == ["b"] and "b" in v.region_btn.text()
    _drawn(qtbot, v)
    v.set_regions(None)
    _drawn(qtbot, v)


def test_coincident_regions_get_their_own_depth_offset(vp):
    v, _sv, _a = vp
    fa = v._depth_offset("a/internalMesh")
    fb = v._depth_offset("b/internalMesh")
    assert fb == (0.0, 0.0) and fa[0] == 0.0 and fa[1] < 0  # the solid wins over the fluid at the interface
    assert v._depth_offset("stage:c1") == (0.0, 0.0)
    import vtk

    _actor, mapper = v.actors["a/internalMesh"]
    factor, units = vtk.reference(1.0), vtk.reference(1.0)
    mapper.GetRelativeCoincidentTopologyPolygonOffsetParameters(factor, units)
    assert (float(factor), float(units)) == fa


def test_slices_and_stages_use_the_shown_regions(vp, qtbot):
    v, _sv, a = vp
    assert v.show_field("p", "0.3", "z").startswith("showing p at t = 0.3")
    _drawn(qtbot, v)
    assert {"slice0", "slice1"} <= set(v.actors)  # the slice cuts every shown region, not just the first
    assert {v.block_regions["slice0"], v.block_regions["slice1"]} == {"a", "b"}
    assert v.range == pytest.approx((4.0, 40.0))

    msg = v.show_field("T", "0.3", "z", regions="solids")
    assert msg.startswith("showing T in a at t = 0.3"), msg
    _drawn(qtbot, v)
    assert {k for k in v.actors if k.startswith("slice")} == {"slice0"} and v.block_regions["slice0"] == "a"
    assert v.range == pytest.approx((40.0, 40.0))
    assert "U is not written in a" in v.show_field("U", "0.3", None, regions="a")

    v.show_field("p", "0.3", None, regions="all")
    _drawn(qtbot, v)
    sid = a.add_stage("slice", {"axis": "z", "pos": 0.5}, regions="fluid")
    assert sid.startswith("c") and v.region_sel == ["b"]
    qtbot.waitUntil(lambda: f"stage:{sid}" in v.stage_info, timeout=60000)
    one = v.stage_info[f"stage:{sid}"]
    assert one["range"] == pytest.approx((4.0, 4.0))  # the fluid's p only
    assert v.set_regions("all").startswith("regions shown")
    qtbot.waitUntil(lambda: v.stage_info.get(f"stage:{sid}", {}).get("cells") == 2 * one["cells"], timeout=60000)
    assert v.stage_info[f"stage:{sid}"]["range"] == pytest.approx((4.0, 40.0))
    assert a.add_stage("slice", {}, regions="zz").startswith("cannot")
    a.remove_stage(None)
    _drawn(qtbot, v)


def test_showcase_frames_the_chosen_region(vp, qtbot):
    v, _sv, a = vp
    v.show_field("p", "0.3")
    _drawn(qtbot, v)
    msg = a.showcase("solids")
    assert msg.startswith("showcase in a"), msg
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen and not v._showcase_pending, timeout=60000)
    assert v.region_sel == ["a"] and v.look == "studio" and v.bounds is not None
    assert all(v.block_regions.get(k.split("~")[0]) == "a" for k in v.actors)


def test_copilot_tools_take_regions(vp, qtbot):
    from aethercfd.copilot.tools import ToolContext, ToolSet
    from aethercfd.studio.panels.copilot.panel import _UiHooks

    v, sv, _a = vp
    ts = ToolSet(ToolContext(sv.session, ui=_UiHooks(sv)))
    schemas = {t["name"]: t["inputSchema"] for t in ts.describe()}
    for name in ("show_field", "add_view_stage", "viewport_showcase", "viewport_regions"):
        assert "regions" in schemas[name]["properties"], name

    def call(name, args):  # the tools run on the copilot's thread; the GUI answers on its own
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("r", ts.call(name, args)))
        th.start()
        qtbot.waitUntil(lambda: "r" in out, timeout=60000)
        th.join(5)
        return out["r"]

    ok, text = call("viewport_regions", {})
    d = json.loads(text)
    assert ok and d["regions"] == ["a", "b"] and d["kinds"] == {"a": "solid", "b": "fluid"}
    ok, text = call("show_field", {"field": "T", "time": "0.3", "slice_axis": "z", "regions": "solids"})
    assert ok and "showing T in a" in json.loads(text)["result"]
    _drawn(qtbot, v)
    assert v.region_sel == ["a"] and v.scalar
    ok, text = call("viewport_regions", {"regions": ["nope"]})
    assert not ok and "no region 'nope'" in text
    ok, text = call("add_view_stage", {"type": "slice", "params": {"axis": "z"}, "regions": "fluid", "wait_s": 30})
    d = json.loads(text)
    assert ok and d["computed"] and v.region_sel == ["b"], text
    ok, text = call("viewport_showcase", {"regions": "all"})
    assert ok and v.region_sel is None


def test_prism_faces_are_wound_outward():
    """VTK's OpenFOAM reader hands prisms over inside out: their wall faces must be flipped to face out."""
    import numpy as np
    import vtk
    from vtk.util import numpy_support as nps

    from aethercfd.studio.panels.viewport.worker import _geometry

    pts = vtk.vtkPoints()
    for p in ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1)):
        pts.InsertNextPoint(*p)
    for order in ([0, 1, 2, 3, 4, 5], [0, 2, 1, 3, 5, 4]):  # one of them is inside out, like the reader's prisms
        ug = vtk.vtkUnstructuredGrid()
        ug.SetPoints(pts)
        ug.InsertNextCell(vtk.VTK_WEDGE, 6, order)
        surf = _geometry(ug)
        n = vtk.vtkPolyDataNormals()
        n.SetInputData(surf)
        n.ComputeCellNormalsOn()
        n.ComputePointNormalsOff()
        n.ConsistencyOff()
        n.AutoOrientNormalsOff()
        n.SplittingOff()
        n.Update()
        normals = nps.vtk_to_numpy(n.GetOutput().GetCellData().GetNormals())
        cc = vtk.vtkCellCenters()
        cc.SetInputData(surf)
        cc.Update()
        centres = nps.vtk_to_numpy(cc.GetOutput().GetPoints().GetData())
        out = np.einsum("ij,ij->i", normals, centres - np.array([1 / 3, 1 / 3, 0.5]))
        assert surf.GetNumberOfPolys() == 5 and (out > 0).all(), order
        assert surf.GetCellData().GetArray("vtkOriginalCellIds") is None
