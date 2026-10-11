"""Approval of a command plan proposed by the copilot (or any non-user actor).

Shows exactly what would happen -- summary, risk, the agent's rationale and confidence, every file diff,
every command, consequences and blocking preconditions -- and lets the user approve or decline with a
note that goes back to Claude. ``GuiApprovalBroker`` is called from copilot worker threads and shows the
dialog on the GUI thread.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from aethercfd.commands.base import CommandPlan, Risk
from aethercfd.commands.executor import Approval
from aethercfd.studio.theme.style import mono_font
from aethercfd.studio.theme.tokens import T

_RISK_COLOR = {Risk.READ: "text_dim", Risk.EDIT: "info", Risk.EXECUTE: "warn", Risk.DESTRUCTIVE: "error"}  # tokens


class ApprovalDialog(QDialog):
    def __init__(self, plan: CommandPlan, parent: QWidget | None = None, *, who: str = "Claude"):
        super().__init__(parent)
        self.setWindowTitle(f"{who} asks to: {plan.summary}"[:120])
        self.resize(860, 600)
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{who}</b> wants to <b>{_esc(plan.summary)}</b>")
        head.setWordWrap(True)
        head.setStyleSheet(f"font-size: 11pt; color: {T.text};")
        lay.addWidget(head)
        color = getattr(T, _RISK_COLOR.get(plan.risk, "text"))
        conf = f" · confidence {plan.confidence:.0%}" if plan.confidence is not None else ""
        risk = QLabel(f"<span style='color:{color}; font-weight:600'>{plan.risk.value.upper()}</span>{conf}")
        lay.addWidget(risk)
        if plan.rationale:
            why = QLabel(f"<i>Why:</i> {_esc(plan.rationale)}")
            why.setWordWrap(True)
            why.setStyleSheet(f"color: {T.text}; padding: 6px; background: {T.alt}; border-radius: 4px;")
            lay.addWidget(why)
        if plan.risk is Risk.DESTRUCTIVE:
            warn = QLabel("This plan is DESTRUCTIVE: read the consequences below.")
            warn.setStyleSheet(f"color: {T.error}; font-weight: 600;")
            lay.addWidget(warn)
        body = QPlainTextEdit(readOnly=True)
        body.setFont(mono_font(9))
        body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        _fill(body, plan)
        lay.addWidget(body, 1)
        self.note = QLineEdit(placeholderText="Optional note to Claude (why you decline, or what to do instead)")
        lay.addWidget(self.note)
        bb = QDialogButtonBox()
        self.ok = bb.addButton("Approve" if not plan.blocked else "Blocked", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setEnabled(not plan.blocked)
        no = bb.addButton("Decline", QDialogButtonBox.ButtonRole.RejectRole)
        no.setDefault(plan.risk is Risk.DESTRUCTIVE)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fill(view: QPlainTextEdit, plan: CommandPlan) -> None:
    cur = view.textCursor()

    def put(text: str, color: str | None = None) -> None:
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        cur.insertText(text + "\n", fmt)

    for c in plan.preconditions:
        put(f"{'ok     ' if c.ok else 'BLOCKED'}  {c.message}", None if c.ok else T.error)
    for fc in plan.file_changes:
        put(f"\n{fc.kind.upper()}  {fc.path}", T.accent)
        lines = (fc.diff or "").splitlines()
        if len(lines) > 3000:
            lines = [*lines[:3000], f"@@ … {len(lines) - 3000:,} more diff lines not shown @@"]
        for ln in lines:
            put(ln, T.ok if ln.startswith("+") and not ln.startswith("+++") else
                T.error if ln.startswith("-") and not ln.startswith("---") else
                T.info if ln.startswith("@@") else None)
    if plan.shell_steps:
        put("\nCommands (inside WSL, with the case's OpenFOAM version):", T.accent)
        for s in plan.shell_steps:
            put(f"  $ {' '.join(s.argv)}" + (f"   > {s.log_name}" if s.log_name else ""))
    if plan.consequences:
        put("\nConsequences:", T.accent)
        for c in plan.consequences:
            put(f"  - {c}")
    view.moveCursor(QTextCursor.MoveOperation.Start)


class GuiApprovalBroker(QObject):
    """ApprovalBroker for the copilot: blocks the calling worker thread until the user decides."""

    _ask = Signal(object, object)  # plan, (event, box)

    def __init__(self, parent_widget_fn, parent: QObject | None = None):
        super().__init__(parent)
        self._parent_widget_fn = parent_widget_fn
        self._ask.connect(self._show, Qt.ConnectionType.QueuedConnection)
        self.on_decision = None  # optional callback(plan, approval) for the transcript

    def request(self, plan: CommandPlan) -> Approval:
        if threading.current_thread() is threading.main_thread():  # not used by the copilot; keep it working
            dlg = ApprovalDialog(plan, self._parent_widget_fn())
            return self._decide(plan, dlg, dlg.exec() == QDialog.DialogCode.Accepted)
        ev, box = threading.Event(), []
        self._ask.emit(plan, (ev, box))
        ev.wait()
        return box[0]

    @Slot(object, object)
    def _show(self, plan: CommandPlan, slot) -> None:
        """Modal but non-blocking (open(), not exec()): no nested event loop inside a queued slot."""
        ev, box = slot
        dlg = ApprovalDialog(plan, self._parent_widget_fn())
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def finished(code: int) -> None:
            box.append(self._decide(plan, dlg, code == QDialog.DialogCode.Accepted))
            ev.set()

        dlg.finished.connect(finished)
        dlg.open()

    def _decide(self, plan: CommandPlan, dlg: ApprovalDialog, approved: bool) -> Approval:
        a = Approval(approved, "user" if approved else "user-declined", dlg.note.text().strip())
        if self.on_decision:
            self.on_decision(plan, a)
        return a
