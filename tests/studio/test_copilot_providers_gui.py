"""Copilot panel with two providers: switching, persisted choice, per-provider sign-in, neutral events, resume."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from aethercfd.copilot.provider import AuthStatus, CliInfo
from aethercfd.copilot.providers import PROVIDERS
from aethercfd.core.settings import Settings
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.copilot.panel import CopilotPanel
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


@pytest.fixture
def fake_providers(monkeypatch):
    """No real CLI lookups: Claude is installed and signed in, Codex installed but signed out."""
    state = {"codex_signed_in": False}
    cl, cx = PROVIDERS["claude"], PROVIDERS["codex"]
    monkeypatch.setattr(cl, "find", lambda configured=None: CliInfo(Path("claude.exe"), "2.1.0", "test"))
    monkeypatch.setattr(cl, "auth_status", lambda cli, timeout=30: AuthStatus(True, "claude.ai", "me@lab"))
    monkeypatch.setattr(cx, "find", lambda configured=None: CliInfo(Path("codex.exe"), "0.159.2", "test"))
    monkeypatch.setattr(cx, "auth_status", lambda cli, timeout=30: AuthStatus(
        state["codex_signed_in"], "chatgpt" if state["codex_signed_in"] else "none", "ChatGPT" if state[
            "codex_signed_in"] else "Not logged in"))
    monkeypatch.setattr(cx, "models", lambda cli: [("Default model", None), ("GPT-Test", "gpt-test")])
    return state


@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch, fake_providers):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    sv = AppServices()
    sv.open_case(case, create_project=True)
    p = CopilotPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    qtbot.waitUntil(lambda: p.cli is not None, timeout=10000)
    yield p
    p.dispose()
    sv.pool.waitForDone(10000)


def _pick(p: CopilotPanel, pid: str) -> None:
    p.provider_box.setCurrentIndex(p.provider_box.findData(pid))


def test_default_is_claude_and_switching_persists(panel, qtbot):
    p = panel
    assert p.provider.id == "claude" and p.provider_box.currentData() == "claude"
    assert [p.model.itemText(i) for i in range(p.model.count())] == ["Default model", "Opus", "Sonnet", "Haiku"]
    assert p.banner.isHidden() and "Claude Code 2.1.0" in p.status.text()
    _pick(p, "codex")
    qtbot.waitUntil(lambda: p.cli is not None and p.cli.version == "0.159.2", timeout=10000)
    assert p.provider.id == "codex"
    assert [p.model.itemData(i) for i in range(p.model.count())] == [None, "gpt-test"]  # Codex's own catalog
    assert not p.banner.isHidden() and p.btn_login.text() == "Sign in to Codex" and p.btn_login.isVisibleTo(p)
    p.model.setCurrentIndex(1)
    p.model.activated.emit(1)
    st = Settings.load()  # written to disk, so the next start opens with Codex and this model
    assert st.copilot_provider == "codex" and st.copilot_model == "gpt-test"
    # a typed model id the catalog does not list is kept as it is
    p.model.setEditText("gpt-custom")
    p.model.lineEdit().editingFinished.emit()
    assert Settings.load().copilot_model == "gpt-custom"
    _pick(p, "claude")
    qtbot.waitUntil(lambda: p.cli is not None and p.cli.version == "2.1.0", timeout=10000)
    assert Settings.load().copilot_provider == "claude" and p.banner.isHidden()


def test_signed_out_provider_keeps_the_message(panel, qtbot, fake_providers, monkeypatch):
    p = panel
    _pick(p, "codex")
    qtbot.waitUntil(lambda: p.cli is not None and p.cli.version == "0.159.2", timeout=10000)
    assert p.send("hello") is False  # not signed in: nothing starts, the text stays in the box
    assert "codex login" in p.items[-1]["text"]
    fake_providers["codex_signed_in"] = True
    started = []

    class _Sess:
        alive, session_id, cost_usd = True, None, 0.0

        def start(self):
            started.append("start")

        def send(self, text):
            started.append(text)

        def interrupt(self):
            pass

        def stop(self, timeout=5.0):
            self.alive = False

    monkeypatch.setattr(PROVIDERS["codex"], "session",
                        lambda *a, **kw: (started.append(kw.get("model")), _Sess())[1])
    assert p.send("hello") is True  # signed in meanwhile: the panel asks again before starting
    assert started == [None, "start", "hello"] and p.items[-1] == {"role": "user", "text": "hello"}


def test_neutral_events_render_and_resume_per_provider(panel, qtbot):
    p = panel
    _pick(p, "codex")
    qtbot.waitUntil(lambda: p.cli is not None and p.cli.version == "0.159.2", timeout=10000)
    p._conv_provider = p.provider
    p._conv_state, p._conv_case = p._state_file(), str(p.sv.session.root)
    p._set_busy(True)
    g = p._gen
    for ev in [{"type": "init", "model": "default", "session_id": "th-1", "tools_failed": []},
               {"type": "message", "text": "Looking at the case."},
               {"type": "tool_call", "id": "i1", "name": "case_overview", "input": {}},
               {"type": "tool_result", "id": "i1", "is_error": False},
               {"type": "tool_call", "id": "i2", "name": "dict_get", "input": {"file": "system/fvSolution"}},
               {"type": "tool_result", "id": "i2", "is_error": True},
               {"type": "error", "message": "Reconnecting... 1/5"},
               {"type": "message", "text": "It is a cavity."},
               {"type": "result", "session_id": "th-1", "num_turns": 3, "cost_usd": None,
                "usage": {"input_tokens": 105149, "output_tokens": 422}, "is_error": False, "message": ""}]:
        p._on_event((g, ev))
    roles = [(it["role"], it.get("who"), it.get("status")) for it in p.items]
    assert roles == [("assistant", "Codex", None), ("tool", None, "ok"), ("tool", None, "error"), ("note", None, None),
                     ("assistant", "Codex", None)]
    assert "file=system/fvSolution" in p.items[2]["text"]
    assert not p._busy and "105.1k in / 422 out tokens" in p.status.text() and "3 turns" in p.status.text()
    saved = json.loads(p._state_file().read_text(encoding="utf-8"))
    assert saved["session_id"] == "th-1" and saved["provider"] == "codex"
    assert p._saved_session_id() == "th-1"
    _pick(p, "claude")  # a Codex thread id is never handed to Claude
    assert p._saved_session_id() is None
    p._render()
    assert "Codex" in p.view.toPlainText()


def test_events_of_a_stopped_conversation_are_dropped(panel):
    p = panel
    n = len(p.items)
    p._on_event((p._gen - 1, {"type": "message", "text": "late"}))
    assert len(p.items) == n
