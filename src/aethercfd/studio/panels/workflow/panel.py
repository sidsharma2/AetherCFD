"""Workflow navigator: the 12 stages as a view of the real case files, with status and issues.

Guided / Advanced / Raw decides how dictionaries open from here: Form view, Split view, or the raw
text. Every row maps to actual files; nothing is a hidden parallel model.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from aethercfd.case.workflow import assess
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

_STATUS = {"ok": ("msc.pass-filled", "ok"), "warn": ("msc.warning", "warn"), "todo": ("msc.circle-large-outline",
           "text_faint"), "info": ("msc.info", "info"), "na": ("msc.circle-slash", "text_faint")}  # (icon, token)
_STAGE_PANEL = {"mesh": "aether.mesh_quality", "run": "aether.run_status", "post": "aether.viewport"}


class WorkflowPanel(Panel):
    STATE_VERSION = 1

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        bar.addWidget(QLabel("Open dictionaries as"))
        self.mode = QButtonGroup(self)
        for i, (name, tip) in enumerate((("Guided", "Form view: physical meaning, sensible editors"),
                                         ("Advanced", "Form and raw text side by side"),
                                         ("Raw", "Exactly the file text"))):
            b = QToolButton(text=name, checkable=True)
            b.setToolTip(tip)
            self.mode.addButton(b, i)
            bar.addWidget(b)
        self.mode.button(1).setChecked(True)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._activate)
        lay.addWidget(self.tree, 1)
        sv = ctx.services
        self._timer = QTimer(self, singleShot=True, interval=800, timeout=self.refresh)
        sv.case_changed.connect(lambda s: self.refresh())
        sv.files_changed.connect(lambda _p: self._timer.start())
        self.refresh()

    def refresh(self) -> None:
        s = self.ctx.services.session
        expanded = {self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1)
                    for i in range(self.tree.topLevelItemCount()) if self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        if s is None:
            self.tree.addTopLevelItem(QTreeWidgetItem(["Open a case to see its workflow"]))
            return
        try:
            stages = assess(s.case())
        except Exception as exc:
            self.tree.addTopLevelItem(QTreeWidgetItem([f"Could not assess the case: {exc}"]))
            return
        for i, st in enumerate(stages, start=1):
            ic, tok = _STATUS.get(st.status, _STATUS["info"])
            it = QTreeWidgetItem([f"{i:>2}  {st.title}"])
            it.setIcon(0, icon(ic, getattr(T, tok)))
            it.setToolTip(0, st.summary)
            it.setData(0, Qt.ItemDataRole.UserRole + 1, st.id)
            sub = QTreeWidgetItem([st.summary])
            sub.setForeground(0, QColor(T.text_dim))
            it.addChild(sub)
            for issue in st.issues:
                w = QTreeWidgetItem([issue])
                w.setIcon(0, icon("msc.warning", T.warn))
                w.setForeground(0, QColor(T.warn))
                w.setToolTip(0, issue)
                it.addChild(w)
            for f in st.files:
                fi = QTreeWidgetItem([str(Path(f).relative_to(s.root)) if Path(f).is_relative_to(s.root) else str(f)])
                fi.setIcon(0, icon("msc.folder" if Path(f).is_dir() else "msc.file"))
                fi.setData(0, Qt.ItemDataRole.UserRole, str(f))
                it.addChild(fi)
            self.tree.addTopLevelItem(it)
            if st.id in expanded or st.status == "warn":
                it.setExpanded(True)

    def _activate(self, item: QTreeWidgetItem, _col: int) -> None:
        sv = self.ctx.services
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            p = Path(path)
            if p.is_file():
                spec = sv.panels.get("aether.editor")
                if spec is not None:
                    d = sv.host.open_panel(spec, args={"path": str(p)})
                    panel = sv.host.panel(d.objectName())
                    mode = {0: 1, 1: 2, 2: 0}[self.mode.checkedId()]  # Guided->Form, Advanced->Split, Raw->Raw
                    if panel is not None and hasattr(panel, "mode_group"):
                        panel.mode_group.button(mode).setChecked(True)
                        panel._set_mode(mode)
            else:
                tree = sv.panels.get("aether.case_tree")
                if tree is not None:
                    sv.host.open_panel(tree)
            return
        stage = item.data(0, Qt.ItemDataRole.UserRole + 1)
        target = _STAGE_PANEL.get(stage)
        if target and sv.panels.get(target) is not None:
            sv.host.open_panel(sv.panels.get(target))

    def save_state(self) -> dict:
        return {"mode": self.mode.checkedId()}

    def restore_state(self, state: dict) -> None:
        b = self.mode.button(int(state.get("mode", 1)))
        if b is not None:
            b.setChecked(True)


PANELS = [PanelSpec("aether.workflow", "Workflow", "msc.list-ordered", "Case", WorkflowPanel, default_area=Area.LEFT,
                    tab_group="nav", description="The 12 stages as a view of the real case files, with issues")]
