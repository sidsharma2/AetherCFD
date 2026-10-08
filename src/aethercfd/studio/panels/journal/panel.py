"""Command Journal: every change AetherCFD made (who, what, why, which files, the diff), and revert."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aethercfd.commands.base import USER, unified_diff
from aethercfd.core.textio import decode_bytes
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon, mono_font
from aethercfd.studio.theme.tokens import T

_ACTOR_COLOR = {"user": T.text, "script": T.info, "agent": T.warn, "external": T.text_dim}


class JournalPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["When", "Actor", "Command", "Summary"])
        self.list.setRootIsDecorated(False)
        self.list.setColumnWidth(0, 150)
        self.list.setColumnWidth(1, 70)
        self.list.setColumnWidth(2, 110)
        self.list.currentItemChanged.connect(self._show)
        split.addWidget(self.list)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel()
        self.title.setWordWrap(True)
        bar.addWidget(self.title, 1)
        self.revert_btn = QPushButton(icon("msc.discard"), "Revert")
        self.revert_btn.setToolTip("Undo this command's file changes (journalled itself; refuses if the files "
                                   "changed since)")
        self.revert_btn.clicked.connect(self._revert)
        bar.addWidget(self.revert_btn)
        rl.addLayout(bar)
        self.detail = QPlainTextEdit(readOnly=True)
        self.detail.setFont(mono_font(9))
        rl.addWidget(self.detail, 1)
        split.addWidget(right)
        split.setSizes([520, 480])
        lay.addWidget(split)
        sv = ctx.services
        self._reload_timer = QTimer(self, singleShot=True, interval=300, timeout=self.reload)
        sv.bridge.event.connect(lambda topic, p: self._reload_timer.start() if topic == "command.applied" else None)
        sv.case_changed.connect(lambda s: self.reload())
        self.reload()

    def _journal(self):
        s = self.ctx.services.session
        return s.project.journal if s is not None and s.project is not None else None

    def reload(self) -> None:
        self.list.clear()
        j = self._journal()
        if j is None:
            self.title.setText("No project journal yet (it starts with the first change you make).")
            return
        for e in j.entries(newest_first=True):
            it = QTreeWidgetItem([e.get("ts", "")[:19].replace("T", " "), e.get("actor", {}).get("kind", ""),
                                  e.get("command", ""), e.get("summary", "")])
            it.setData(0, Qt.ItemDataRole.UserRole, e)
            it.setForeground(1, QColor(_ACTOR_COLOR.get(e.get("actor", {}).get("kind", ""), T.text)))
            if not e.get("ok", True):
                it.setForeground(3, QColor(T.error))
            self.list.addTopLevelItem(it)
        self.title.setText(f"{self.list.topLevelItemCount()} journalled command(s)")

    def _show(self, cur: QTreeWidgetItem | None, _prev=None) -> None:
        if cur is None:
            self.detail.clear()
            return
        e = cur.data(0, Qt.ItemDataRole.UserRole)
        j = self._journal()
        lines = [f"{e.get('command')}  —  {e.get('summary')}", f"id {e.get('id')}   risk {e.get('risk')}   "
                 f"actor {e.get('actor')}   approved by {e.get('approval', {}).get('by')}",
                 f"result: {'ok' if e.get('ok') else 'FAILED'}  {e.get('message', '')}"]
        if e.get("rationale"):
            lines.append(f"rationale: {e['rationale']}  (confidence {e.get('confidence')})")
        if e.get("shell"):
            lines += ["", "commands:"] + [f"  $ {c}" for c in e["shell"]]
        params = {k: v for k, v in (e.get("params") or {}).items() if not k.startswith("_")}
        lines += ["", "parameters:", json.dumps(params, indent=2)]
        store = self.ctx.services.session.project.objects if j is not None else None
        for f in e.get("files", []):
            lines += ["", f"--- {f['kind']}: {f['path']}"]
            if store is None:
                continue
            before = store.get(f["before"]) if f.get("before") else b""
            after = store.get(f["after"]) if f.get("after") else b""
            if before is None or after is None:
                lines.append("(content not stored: file larger than the journal's object limit)")
                continue
            lines.append(unified_diff(Path(f["path"]), decode_bytes(before or b"")[0], decode_bytes(after or b"")[0])
                         or "(no textual change)")
        self.detail.setPlainText("\n".join(lines))
        self.revert_btn.setEnabled(bool(e.get("files")) and e.get("command") != "file.revert" and e.get("ok", True))

    def _revert(self) -> None:
        cur = self.list.currentItem()
        s = self.ctx.services.session
        if cur is None or s is None:
            return
        e = cur.data(0, Qt.ItemDataRole.UserRole)
        res = s.executor.execute("file.revert", {"journal_id": e["id"]}, USER)
        if res.ok:
            for f in e.get("files", []):
                d = self.ctx.services.documents.get(f["path"])
                if d is not None:
                    d.check_disk()
            self.ctx.services.notify(f"Reverted: {e.get('summary')}")
        else:
            QMessageBox.warning(self, "Cannot revert", res.message)
        self.reload()


PANELS = [PanelSpec("aether.journal", "Command Journal", "msc.history", "Case", JournalPanel,
                    default_area=Area.BOTTOM, description="Every change: who, what, why, files, diff — and revert")]
