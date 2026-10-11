"""Light / Dark / System themes: palettes, and a live switch that re-tints what is already on screen."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QTextEdit

from aethercfd.core.settings import Settings
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.panels.run.panels import ResidualsPanel, SolverLogPanel
from aethercfd.studio.panels.welcome.panel import WelcomePanel
from aethercfd.studio.shell.main_window import MainWindow
from aethercfd.studio.shell.panel_base import PanelContext
from aethercfd.studio.theme import manager, style
from aethercfd.studio.theme.tokens import DARK, LIGHT, UI_COLORS, T, color_map
from aethercfd.studio.widgets.code_editor import CodeEditor


def test_palettes_are_complete_and_invertible():
    for t in (DARK, LIGHT):
        cols = [getattr(t, k).lower() for k in UI_COLORS]
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in cols), t.name
        assert len(set(cols)) == len(cols), f"{t.name}: interface colours must be unique to be re-tinted"
        assert len(t.series) == 16
    there, back = color_map(DARK, LIGHT), color_map(LIGHT, DARK)
    assert {v.lower(): k for k, v in there.items()} == back
    assert manager.remap_text(f"color: {DARK.text}; background: #123456;", there) == \
        f"color: {LIGHT.text}; background: #123456;"


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    app = QApplication.instance()
    style.apply(app)
    sv = AppServices()
    sv.theme.set_mode("dark")
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    yield w
    sv.theme.set_mode("dark")  # the theme is process-wide: leave it as the other tests expect
    sv.pool.waitForDone(10000)
    sv.bridge.close()


def _icon_color(ic) -> str:
    img = ic.pixmap(32, 32).toImage()
    px = max((img.pixelColor(x, y) for x in range(32) for y in range(32)), key=lambda c: c.alpha())
    return px.name()


def _syntax_color(ed: CodeEditor, word: str) -> str:
    block = ed.document().firstBlock()
    start = block.text().index(word)
    for r in block.layout().formats():
        if r.start <= start < r.start + r.length:
            return r.format.foreground().color().name()
    return ""


def test_live_switch_retints_existing_widgets(window, qtbot):
    sv = window.services
    app = QApplication.instance()
    ctx = PanelContext(sv, "#main")
    welcome, residuals, log_panel = WelcomePanel(ctx), ResidualsPanel(ctx), SolverLogPanel(ctx)
    for p in (welcome, residuals, log_panel):
        qtbot.addWidget(p)
    label = QLabel("dim")
    label.setStyleSheet(f"color: {T.text_dim}; padding: 2px;")
    rich = QLabel(f"<span style='color:{T.warn}'>careful</span>")
    out = QTextEdit(readOnly=True)
    out.setHtml(f"<pre style='color:{T.text}'>printed in dark</pre>")
    ed = CodeEditor()
    ed.setPlainText("default none;")
    for w in (label, rich, out, ed):
        qtbot.addWidget(w)
    from aethercfd.studio.theme.style import icon

    ic = icon("msc.add")
    status_ic = icon("msc.warning", T.warn)
    assert app.palette().color(QPalette.ColorRole.Window).name() == DARK.panel
    assert _syntax_color(ed, "default") == DARK.syn_keyword
    assert _icon_color(ic) == DARK.text_dim
    changed = []
    sv.theme.changed.connect(changed.append)

    assert sv.set_theme("light") == "light"
    assert changed == ["light"]
    assert app.palette().color(QPalette.ColorRole.Window).name() == LIGHT.panel
    assert LIGHT.panel in app.styleSheet() and DARK.panel not in app.styleSheet()
    # built before the switch, re-tinted in place
    assert label.styleSheet() == f"color: {LIGHT.text_dim}; padding: 2px;"
    assert LIGHT.warn in rich.text()
    frag = next(iter(out.document().firstBlock())).fragment()
    assert frag.charFormat().foreground().color().name() == LIGHT.text
    assert residuals.plot.backgroundBrush().color().name() == LIGHT.base
    assert residuals.plot.getPlotItem().getAxis("left").pen().color().name() == LIGHT.text_dim
    assert _syntax_color(ed, "default") == LIGHT.syn_keyword  # highlighter re-ran with light colours
    assert _icon_color(ic) == LIGHT.text_dim and _icon_color(status_ic) == LIGHT.warn
    assert log_panel._fmt["fatal"].foreground().color().name() == LIGHT.error  # lines still to come
    assert window._theme_actions["light"].isChecked()
    assert Settings.load().theme == "light"  # remembered

    # and back: nothing is left in the light palette
    sv.set_theme("dark")
    assert label.styleSheet() == f"color: {DARK.text_dim}; padding: 2px;"
    assert residuals.plot.backgroundBrush().color().name() == DARK.base
    assert _syntax_color(ed, "default") == DARK.syn_keyword
    assert changed == ["light", "dark"]


def test_system_follows_windows_and_palette_offers_themes(window):
    sv = window.services
    name = sv.set_theme("system")
    assert sv.theme.mode == "system" and name == sv.theme.system_theme()
    assert window._theme_actions["system"].isChecked()
    with pytest.raises(ValueError):
        sv.set_theme("purple")
    labels = {it.label for it in window.palette._collect()}
    assert {"Theme: Light", "Theme: Dark", "Theme: System (follow Windows)"} <= labels
    item = next(it for it in window.palette._collect() if it.label == "Theme: Light")
    item.run()
    assert sv.theme.name == "light" and sv.theme.mode == "light"
