"""The slim command bar under the menu: which case, which perspective, search everything, run state, and
one-click access to the things people look for first (new case, checks, copilot)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QWidget,
)

from aethercfd.studio.layout.recipes import PERSPECTIVES
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

_RUN_COLOR = {"running": "accent", "paused": "warn", "finished": "ok", "failed": "error", "killed": "warn",
              "guard_stopped": "error", "lost": "warn", "staging": "info", "syncing": "info",
              "stopping": "warn"}  # tokens


class CommandBar(QToolBar):
    def __init__(self, window):
        super().__init__("Command bar", window)
        self.win = window
        sv = window.services
        self.setObjectName("aether_command_bar")
        self.setMovable(False)
        self.setFloatable(False)
        self.setStyleSheet(f"QToolBar#aether_command_bar {{ background: {T.panel}; border-bottom: 1px solid "
                           f"{T.border_soft}; spacing: 4px; padding: 2px 6px; }}"
                           f"QToolButton[persp='true'] {{ padding: 3px 10px; border-radius: 4px; color: {T.text_dim};}}"
                           f"QToolButton[persp='true']:checked {{ background: {T.accent_soft}; color: {T.text}; }}")
        self.case = QLabel("No case open")
        self.case.setStyleSheet(f"color: {T.text}; font-weight: 600; padding: 0 10px 0 4px;")
        self.case.setToolTip("The open OpenFOAM case")
        self.addWidget(self.case)
        self._sep()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.persp: dict[str, QToolButton] = {}
        for p in PERSPECTIVES:
            b = QToolButton(text=p.title, checkable=True, toolTip=f"{p.title} perspective ({p.shortcut})")
            b.setProperty("persp", True)
            b.clicked.connect(lambda _=False, pid=p.id: window.layouts.switch(pid))
            self.group.addButton(b)
            self.persp[p.id] = b
            self.addWidget(b)
        self._sep()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.search = QLineEdit(placeholderText="Search anything — typos are fine   (Ctrl+K)")
        self.search.setMinimumWidth(320)
        self.search.setMaximumWidth(520)
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(f"QLineEdit {{ background: {T.alt}; border: 1px solid {T.border}; "
                                  f"border-radius: 12px; padding: 3px 12px; }}")
        self.search.textEdited.connect(self._search)
        self.search.returnPressed.connect(lambda: self._search(self.search.text()))
        self.addWidget(spacer)
        self.addWidget(self.search)
        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(spacer2)
        self.run = QToolButton(text="  no run", toolTip="Latest run of this case (click: Run panel)")
        self.run.setIcon(icon("msc.debug-start", T.text_dim))
        self.run.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.run.clicked.connect(lambda: self._open("aether.run_status"))
        self.addWidget(self.run)
        self._sep()
        for ic, tip, fn in (("msc.new-folder", "New case from a template (Ctrl+N)", window.new_case_dialog),
                            ("msc.folder-opened", "Open a case (Ctrl+O)", window.open_case_dialog),
                            ("mdi6.stethoscope", "Checks: numerics and pre-run problems",
                             lambda: self._open("aether.checks")),
                            ("mdi6.robot-outline", "Copilot: ask Claude about this case",
                             lambda: self._open("aether.copilot"))):
            b = QToolButton(icon=icon(ic), toolTip=tip, autoRaise=True)
            b.clicked.connect(fn)
            self.addWidget(b)
        window.layouts.perspective_changed.connect(self._on_perspective)
        sv.case_changed.connect(self._on_case)
        sv.bridge.event.connect(self._on_event)
        if window.layouts.current:
            self._on_perspective(window.layouts.current)

    def _sep(self) -> None:
        s = QLabel("│")
        s.setStyleSheet(f"color: {T.border}; padding: 0 2px;")
        self.addWidget(s)

    def _open(self, type_id: str) -> None:
        spec = self.win.services.panels.get(type_id)
        if spec is not None:
            self.win.host.open_panel(spec)

    def _search(self, text: str) -> None:
        pal = self.win.palette
        pal.open(text)
        self.search.clear()

    def _on_perspective(self, pid: str) -> None:
        b = self.persp.get(pid)
        if b is not None:
            b.setChecked(True)

    def _on_case(self, s) -> None:
        if s is None:
            self.case.setText("No case open")
            return
        c = s.case()
        self.case.setText(f"{c.name}  ·  {c.solver_label}")
        self.case.setToolTip(str(s.root))
        self.run.setText("  no run")
        self.run.setIcon(icon("msc.debug-start", T.text_dim))

    def _on_event(self, topic: str, payload: dict) -> None:
        if topic != "run.state":
            return
        s = self.win.services.session
        if s is None or str(payload.get("case", "")).lower() != str(s.root).lower():
            return
        state = str(payload.get("state", ""))
        color = getattr(T, _RUN_COLOR.get(state, "text_dim"))
        self.run.setText(f"  {state.replace('_', ' ')}")
        self.run.setIcon(icon("msc.debug-start" if state != "running" else "msc.sync", color))
