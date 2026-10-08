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

    def boom():
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
