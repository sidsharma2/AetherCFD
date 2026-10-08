"""The command palette finds things even when they are misspelt (real main window, real items)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels import register_builtin_panels
from aethercfd.studio.shell.main_window import MainWindow


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    yield w
    sv.pool.waitForDone(10000)
    sv.bridge.close()


@pytest.mark.parametrize(("typed", "expect"), [
    ("resdiuals", "Open Residuals"),
    ("chek mesh", "Open Mesh Quality"),
    ("vortx", "Q-criterion"),
    ("strmlines", "Streamlines"),
    ("copilt", "Open Copilot"),
    ("nwe case", "New Case"),
    ("propertys", "Open Properties"),
    ("shlieren", "Schlieren"),
    ("visualisation", "Visualization"),
    ("terminl", "Open Terminal"),
    ("convergance", "Open Residuals"),
    ("render 4k", "Render image"),
])
def test_typos_find_the_right_thing(window, typed, expect):
    pal = window.palette
    items = pal._collect()
    top = [it.label for it in pal.rank(typed, items)[:3]]
    assert any(expect.lower() in t.lower() for t in top), (typed, top)


def test_search_box_opens_palette_with_text(window, qtbot):
    window.command_bar._search("resid")
    assert window.palette.isVisible() and window.palette.edit.text() == "resid"
    labels = [window.palette.list.item(i).text() for i in range(window.palette.list.count())]
    assert labels and "Residuals" in labels[0]
    assert any(t.startswith("Ask the copilot") for t in labels)
    window.palette.hide()
