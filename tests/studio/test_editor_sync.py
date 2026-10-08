"""Editor <-> core Document sync (pytest-qt, offscreen-safe: no GL)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QTextCursor

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.editor.panel import EditorPanel, _prefix_suffix_diff
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"


@pytest.fixture
def services(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    sv = AppServices()
    yield sv
    sv.pool.waitForDone(10000)  # background jobs must not outlive the services
    sv.bridge.close()


def make(qtbot, services, path: Path) -> EditorPanel:
    p = EditorPanel(PanelContext(services, "#1", {"path": str(path)}))
    qtbot.addWidget(p)
    p.mode_group.button(2).setChecked(True)
    p._set_mode(2)
    return p


def test_prefix_suffix_diff():
    assert _prefix_suffix_diff("abcdef", "abXYef") == (2, 4, "XY")
    assert _prefix_suffix_diff("abc", "abc") == (3, 3, "")
    assert _prefix_suffix_diff("", "x") == (0, 0, "x")
    assert _prefix_suffix_diff("aaa", "aa") in ((2, 3, ""), (0, 1, ""))


def test_typing_updates_document_and_undo_restores(qtbot, services, tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    ed = make(qtbot, services, case / "system" / "controlDict")
    original = ed.doc.text
    c = ed.editor.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    ed.editor.setTextCursor(c)
    qtbot.keyClicks(ed.editor, "// note")
    assert ed.doc.text.endswith("// note") and ed.doc.dirty
    ed.editor.undo_requested.emit()
    assert ed.doc.text == original and ed.editor.toPlainText() == original


def test_form_edit_flows_into_raw_text(qtbot, services, tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    ed = make(qtbot, services, case / "system" / "controlDict")
    item = next(it for it in ed._iter_items() if it.text(0) == "endTime")
    item.setText(1, "7")  # what an in-place edit in the form does
    assert "endTime         7;" in ed.editor.toPlainText()
    assert "endTime         7;" in ed.doc.text


def test_save_keeps_crlf_and_comments(qtbot, services, tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    cd = case / "system" / "controlDict"
    cd.write_bytes(cd.read_bytes().replace(b"\n", b"\r\n"))
    services.open_case(case)
    ed = make(qtbot, services, cd)
    assert "\r" not in ed.editor.toPlainText()  # the view is LF ...
    item = next(it for it in ed._iter_items() if it.text(0) == "deltaT")
    item.setText(1, "0.001")
    ed.save()
    data = cd.read_bytes()
    assert b"deltaT          0.001;\r\n" in data  # ... the file stays CRLF, only the token changed
    assert data.count(b"\r\n") == data.count(b"\n")
    assert not ed.doc.dirty
    entries = list(services.session.project.journal.entries())
    assert entries[-1]["command"] == "file.save"
