"""Opening a case is visible and recorded; the copilot never swallows a message; decomposed results count."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aethercfd.case.model import load_case
from aethercfd.studio.app.automation import AppAutomation
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.copilot.panel import CopilotPanel
from aethercfd.studio.panels.welcome.panel import WelcomePanel
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def _decomposed_case(tmp_path) -> Path:
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    for i in range(2):
        for t in ("0", "200", "400"):
            (case / f"processor{i}" / t).mkdir(parents=True, exist_ok=True)
    return case


@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    return AppServices()


def test_processor_times_are_results(tmp_path):
    c = load_case(_decomposed_case(tmp_path))
    assert [t for _, t in c.processor_times] == ["0", "200", "400"]
    assert c.latest_result_time == "400"


def test_open_case_records_recent_and_shows_card(sv, qtbot, tmp_path):
    w = WelcomePanel(PanelContext(sv, "#main"))
    qtbot.addWidget(w)
    case = _decomposed_case(tmp_path)
    sv.open_case(case)  # e.g. from the command line, not via the Start page
    assert sv.settings.recent_cases[0] == str(case.resolve())
    assert not w.card.isHidden()
    assert "processor folders" in w.card_info.text() and "400" in w.card_info.text()
    assert w.recent.item(0).data(256) == str(case.resolve())  # Qt.UserRole


def test_copilot_keeps_the_message_when_claude_cannot_start(sv, qtbot, tmp_path, monkeypatch):
    sv.open_case(_decomposed_case(tmp_path))
    p = CopilotPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)

    def boom(**_kw):
        raise FileNotFoundError("no such folder")

    monkeypatch.setattr(p, "_ensure_session", boom)
    p.input.setPlainText("open the REV B.1 probe")
    p._send_input()
    assert p.input.toPlainText() == "open the REV B.1 probe"
    assert p.items and p.items[-1]["role"] == "note" and "could not start" in p.items[-1]["text"]


def test_automation_open_case(sv, qtbot, tmp_path):
    a = AppAutomation(sv)
    assert "not an OpenFOAM case" in a.open_case(str(tmp_path))
    case = _decomposed_case(tmp_path)
    msg = a.open_case(str(case), delay_ms=10)
    assert msg.startswith("opening")
    qtbot.waitUntil(lambda: sv.session is not None and Path(sv.session.root) == case.resolve(), timeout=5000)
    assert "already the open case" in a.open_case(str(case))


def test_start_page_built_after_a_case_is_open(sv, qtbot, tmp_path):
    case = _decomposed_case(tmp_path)
    sv.open_case(case)
    w = WelcomePanel(PanelContext(sv, "#main"))  # e.g. the layout restores the Start page after opening
    qtbot.addWidget(w)
    assert not w.card.isHidden() and "400" in w.card_info.text()


def test_copilot_chat_continues_when_it_opens_a_case(sv, qtbot, tmp_path, monkeypatch):
    """The copilot opened a case: the chat stays on screen and the request is handed to a new conversation
    for that case (it used to wipe the chat and leave the user with an empty box)."""
    import time

    a, b = _decomposed_case(tmp_path / "a"), _decomposed_case(tmp_path / "b")
    sv.open_case(a, create_project=True)
    p = CopilotPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    p.items = [{"role": "user", "text": "open case b"}, {"role": "assistant", "text": "Opening b."}]
    sent = []
    monkeypatch.setattr(p, "send", lambda text, **kw: sent.append((text, kw)) or True)
    sv.copilot_handoff = {"path": str(b), "t": time.monotonic(), "request": "open case b and show its regions"}
    sv.open_case(b, create_project=True)
    assert [i["text"] for i in p.items[:2]] == ["open case b", "Opening b."]  # kept
    qtbot.waitUntil(lambda: bool(sent), timeout=3000)
    text, kw = sent[0]
    assert "open case b and show its regions" in text and kw.get("fresh") and kw.get("show_as") == "note"
    # a case opened by the user (no hand-over) starts with a clean chat
    sv.open_case(a)
    assert p.items == []


def test_copilot_ignores_a_stopped_conversation_and_foreign_session_ids(sv, qtbot, tmp_path):
    import json

    a, b = _decomposed_case(tmp_path / "a"), _decomposed_case(tmp_path / "b")
    sv.open_case(a, create_project=True)
    p = CopilotPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    stale = p._gen
    p._stop_session()  # e.g. the case changed while the reply was still arriving
    p._on_event((stale, {"type": "result", "session_id": "old-conversation", "subtype": "success"}))
    f = p._state_file()
    assert not f.exists() or json.loads(f.read_text())["session_id"] != "old-conversation"
    # a copilot.json naming another case is not resumed here
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"session_id": "x", "case": str(b)}))
    assert p._saved_session_id() is None
    f.write_text(json.dumps({"session_id": "y", "case": str(a)}))
    assert p._saved_session_id() == "y"


def test_copilot_enter_sends_and_shift_enter_adds_a_line(sv, qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    sv.open_case(_decomposed_case(tmp_path))
    p = CopilotPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    sent = []
    monkeypatch.setattr(p, "send", lambda text, **kw: sent.append(text) or True)
    p.input.setPlainText("hello")
    qtbot.keyClick(p.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert not sent and "\n" in p.input.toPlainText()
    qtbot.keyClick(p.input, Qt.Key.Key_Return)
    assert sent and sent[0].startswith("hello")


def test_opening_something_that_is_not_a_case_says_what_to_do(sv, qtbot, tmp_path):
    """A moved Recent entry, a file, or the parent folder of cases: a plain message, no case opened."""
    notes = []
    sv.notification.connect(lambda msg, level: notes.append((msg, level)))
    parent = tmp_path / "my cases é"
    shutil.copytree(FIX, parent / "cavity")
    (parent / "readme.txt").write_text("x")
    (tmp_path / "empty").mkdir()
    for path, words in ((tmp_path / "gone", "does not exist"), (parent / "readme.txt", "is a file"),
                        (parent, "holds 1: open one of them (cavity)"), (tmp_path / "empty", "no system/controlDict")):
        notes.clear()
        sv.open_case(path)
        assert notes and notes[-1][1] == "error" and words in notes[-1][0], (path, notes)
        assert sv.session is None
        assert not (Path(path) / ".aethercfd").exists()
    assert sv.settings.recent_cases == []
    sv.open_case(parent / "cavity")
    assert sv.session is not None and sv.session.root == (parent / "cavity").resolve()


def test_a_broken_settings_file_is_reported_once_the_window_listens(tmp_path, monkeypatch, qtbot):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg2"))
    (tmp_path / "cfg2").mkdir()
    (tmp_path / "cfg2" / "settings.toml").write_text("[wsl\n", encoding="utf-8")
    s = AppServices()  # used to raise TOMLDecodeError: the windowed app closed with no message
    notes = []
    s.notification.connect(lambda msg, level: notes.append((msg, level)))
    s.report_startup_problems()
    assert notes and notes[0][1] == "error" and "settings.toml" in notes[0][0]
