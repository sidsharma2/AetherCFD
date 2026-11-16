"""The 3D viewport's toolbar at narrow widths: no control is squeezed below its size hint, no text is cut,
nothing overlaps or runs off the panel; the controls wrap onto more rows instead (studio/panels/viewport/
toolbar.py). Checked from 600 px to 1600 px on the multi-region heater, with the longest labels the toolbar
shows there ("Regions: solids", "Surface + edges", "magnitude", "Lock range", "Flow paths")."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QLabel,
    QStyle,
    QStyleOptionComboBox,
    QToolButton,
    QWidget,
)

from aethercfd.studio.app.automation import AppAutomation
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow

HEATER = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_multiRegionHeater"
WIDTHS = (600, 700, 800, 1000, 1200, 1400, 1600)
CONTROLS = ("field", "comp", "region_btn", "prev_t", "time", "next_t", "last_t", "follow", "look_box", "btn_smooth",
            "btn_cells", "btn_edges", "rep", "cmap", "lock", "btn_show", "btn_sections", "btn_flow", "btn_views",
            "btn_fit", "btn_ao", "btn_shot", "btn_render", "btn_rec", "btn_pv", "btn_side")


@pytest.fixture
def vp(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "heater"
    shutil.copytree(HEATER, case)
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    w.resize(1600, 900)
    w.show()
    sv.open_case(case, create_project=True)
    v = AppAutomation(sv)._viewport(open_if_missing=True)
    qtbot.waitUntil(lambda: bool(v.meta) and v.field.count() > 0, timeout=60000)
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=60000)
    yield v, w
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _toolbar_widgets(v) -> list[tuple[str, QWidget]]:
    """The shown toolbar controls (named attributes, plus the 'Field' and 't' labels above the 3D view)."""
    out = [(n, getattr(v, n)) for n in CONTROLS if getattr(v, n).isVisible()]
    top = v.plotter.mapTo(v, v.plotter.rect().topLeft()).y()
    for lab in v.findChildren(QLabel):
        if lab.isVisible() and lab.text().strip() and lab.mapTo(v, lab.rect().topLeft()).y() < top \
                and not v.side.isAncestorOf(lab):
            out.append((f"label {lab.text().strip()!r}", lab))
    return out


def _problems(v) -> list[str]:
    bad = []
    rects = []
    for name, wd in _toolbar_widgets(v):
        r = wd.rect().translated(wd.mapTo(v, wd.rect().topLeft()))
        rects.append((name, r))
        if wd.width() + 1 < wd.sizeHint().width():
            bad.append(f"{name} squeezed: {wd.width()} < {wd.sizeHint().width()} px")
        if r.left() < 0 or r.right() >= v.width():
            bad.append(f"{name} runs off the panel: x {r.left()}..{r.right()} of {v.width()}")
        fm = wd.fontMetrics()
        if isinstance(wd, QComboBox):
            opt = QStyleOptionComboBox()
            wd.initStyleOption(opt)
            field = wd.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                              QStyle.SubControl.SC_ComboBoxEditField, wd)
            for i in range(wd.count()):  # every item, not just the shown one, fits without eliding
                need = fm.horizontalAdvance(wd.itemText(i))
                if need > field.width():
                    bad.append(f"{name} item {wd.itemText(i)!r} cut: needs {need} px, has {field.width()}")
        elif isinstance(wd, QToolButton):
            if wd.toolButtonStyle() == wd.toolButtonStyle().ToolButtonIconOnly or not wd.text():
                if not wd.toolTip():
                    bad.append(f"{name} is icon-only without a tooltip")
            elif fm.horizontalAdvance(wd.text().strip()) > wd.width():
                bad.append(f"{name} text {wd.text()!r} cut")
        elif isinstance(wd, (QCheckBox, QLabel, QAbstractButton)) and fm.horizontalAdvance(wd.text()) > wd.width():
            bad.append(f"{name} text {wd.text()!r} cut")
    for i, (a, ra) in enumerate(rects):
        for b, rb in rects[i + 1:]:
            if ra.intersects(rb):
                bad.append(f"{a} overlaps {b}")
    return bad


def _at_width(qtbot, v, w, width: int) -> None:
    w.resize(width, 900)
    qtbot.waitUntil(lambda: abs(v.width() - width) <= 2, timeout=10000)
    qtbot.wait(150)  # the layout request after the resize


def test_toolbar_never_cuts_a_label(vp, qtbot):
    v, w = vp
    assert v.set_regions("solids").startswith("regions shown")
    v.rep.setCurrentText("Surface + edges")
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen, timeout=60000)
    assert "solids" in v.region_btn.text() and v.region_btn.isVisible()
    rows = {}
    for width in WIDTHS:
        _at_width(qtbot, v, w, width)
        assert _problems(v) == [], f"at {width} px"
        rows[width] = v.toolbar.row_count()
    assert rows[1600] == 1 and rows[600] <= 3, rows  # wraps instead of squeezing, and only as far as needed
    assert v.toolbar.height() < v.height() / 4

    # a vector field: the component box ("magnitude") is shown too
    v.set_regions(None)
    assert v.show_field("U").startswith("showing")
    qtbot.waitUntil(lambda: getattr(v, "drawn_gen", -1) == v.gen and v.comp.isVisible(), timeout=60000)
    for width in (600, 1000):
        _at_width(qtbot, v, w, width)
        assert _problems(v) == [], f"at {width} px with U"


def test_toolbar_keeps_keyboard_access(vp, qtbot):
    v, w = vp
    _at_width(qtbot, v, w, 700)
    v.field.setFocus()
    seen = []
    for _ in range(40):  # Tab walks the toolbar's controls in their visual order
        fw = v.focusWidget() if v.isAncestorOf(v.focusWidget() or v) else None
        if fw is not None and fw not in seen:
            seen.append(fw)
        v.focusNextChild()
    named = {id(getattr(v, n)): n for n in CONTROLS}
    order = [named[id(x)] for x in seen if id(x) in named]
    assert order[:2] == ["field", "region_btn"] and "time" in order and "cmap" in order, order
