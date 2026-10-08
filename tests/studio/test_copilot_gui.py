"""Copilot GUI pieces: approvals block the tool thread until the user decides; markdown rendering."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from aethercfd.commands.base import USER, CommandPlan, FileChange, Risk
from aethercfd.studio.panels.copilot.panel import _tool_summary, md_to_html
from aethercfd.studio.widgets.approval import ApprovalDialog, GuiApprovalBroker


def _plan(risk=Risk.EDIT) -> CommandPlan:
    p = CommandPlan("dict.set", {}, USER, risk, "fvSolution: relaxationFactors/fields/p = 0.1",
                    rationale="RS3r converged with p relaxation 0.1", confidence=0.7)
    p.file_changes.append(FileChange(Path("fvSolution"), "a" * 64, b"x", "--- a\n+++ b\n-    p 0.2;\n+    p 0.1;\n"))
    return p


def _answer_dialog(accept: bool, note: str = "") -> None:
    def go():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, ApprovalDialog) and w.isVisible():
                w.note.setText(note)
                w.accept() if accept else w.reject()
                return
        QTimer.singleShot(50, go)

    QTimer.singleShot(50, go)


@pytest.mark.parametrize("accept", [True, False])
def test_broker_blocks_worker_until_user_decides(qtbot, accept):
    broker = GuiApprovalBroker(lambda: None)
    decisions = []
    broker.on_decision = lambda plan, a: decisions.append(a.approved)
    result = {}

    def worker():
        result["approval"] = broker.request(_plan())

    _answer_dialog(accept, "" if accept else "use 0.15 instead")
    th = threading.Thread(target=worker)
    th.start()
    qtbot.waitUntil(lambda: "approval" in result, timeout=10000)
    th.join(5)
    a = result["approval"]
    assert a.approved is accept and decisions == [accept]
    if not accept:
        assert a.note == "use 0.15 instead"


def test_destructive_plans_default_to_decline(qtbot):
    dlg = ApprovalDialog(_plan(Risk.DESTRUCTIVE))
    qtbot.addWidget(dlg)
    assert not dlg.ok.isDefault()


def test_markdown_rendering():
    h = md_to_html("## Findings\n- **AR 72.5** in `segment E`\n- 9:1 jump\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
                   "```\nblockMesh\n```\n<script>")
    assert "<li><b>AR 72.5</b>" in h and "<table" in h and "<pre" in h and "&lt;script&gt;" in h


def test_tool_summary():
    assert _tool_summary("run_openfoam", {"steps": [{"app": "checkMesh", "args": ["-allTopology"]}]}) == \
        "run_openfoam(checkMesh -allTopology)"
    assert "key=SIMPLE/nNonOrthogonalCorrectors" in _tool_summary(
        "dict_set", {"file": "system/fvSolution", "key": "SIMPLE/nNonOrthogonalCorrectors", "value": "2"})
