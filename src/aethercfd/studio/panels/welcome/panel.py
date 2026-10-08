"""Start panel: open a case, recent cases, cases found under your case roots, environment status."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from aethercfd.studio.shell.panel_base import Panel, PanelContext
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T


def _section(title: str) -> QLabel:
    lbl = QLabel(title.upper())
    lbl.setStyleSheet(f"color: {T.text_faint}; font-size: 8pt; letter-spacing: 1px; padding-top: 10px;")
    return lbl


class WelcomePanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        sv = ctx.services
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        head = QLabel("AetherCFD Studio")
        head.setStyleSheet(f"font-size: 18pt; color: {T.text};")
        sub = QLabel("OpenFOAM, without hiding it. Every action maps to real files and commands.")
        sub.setStyleSheet(f"color: {T.text_dim};")
        lay.addWidget(head)
        lay.addWidget(sub)

        row = QHBoxLayout()
        open_btn = QPushButton(icon("msc.folder-opened"), "Open Case…")
        open_btn.clicked.connect(self._open)
        open_btn.setDefault(True)
        scan_btn = QPushButton(icon("msc.search"), "Find Cases")
        scan_btn.clicked.connect(self._scan)
        row.addWidget(open_btn)
        row.addWidget(scan_btn)
        row.addStretch(1)
        lay.addLayout(row)

        cols = QHBoxLayout()
        left, right = QVBoxLayout(), QVBoxLayout()
        left.addWidget(_section("Recent"))
        self.recent = QListWidget()
        self.recent.itemActivated.connect(lambda it: self._open_path(it.data(Qt.ItemDataRole.UserRole)))
        left.addWidget(self.recent, 1)
        right.addWidget(_section("Cases under your case roots"))
        self.found = QListWidget()
        self.found.itemActivated.connect(lambda it: self._open_path(it.data(Qt.ItemDataRole.UserRole)))
        right.addWidget(self.found, 1)
        cols.addLayout(left, 1)
        cols.addLayout(right, 1)
        lay.addLayout(cols, 1)
        self.env = QLabel()
        self.env.setStyleSheet(f"color: {T.text_dim};")
        self.env.setWordWrap(True)
        lay.addWidget(_section("Environment"))
        lay.addWidget(self.env)
        self._refresh_recent()
        sv.backend_ready.connect(self._on_backend)
        self.env.setText("OpenFOAM backend: not started yet (it starts when you open a case).")

    def _refresh_recent(self) -> None:
        self.recent.clear()
        for p in self.ctx.services.settings.recent_cases:
            it = QListWidgetItem(icon("msc.folder"), Path(p).name)
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.recent.addItem(it)
        if not self.ctx.services.settings.recent_cases:
            self.recent.addItem("No recent cases yet")

    def _open(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Open OpenFOAM case", self.ctx.services.settings.case_roots[0]
                                             if self.ctx.services.settings.case_roots else "")
        if d:
            self._open_path(d)

    def _open_path(self, path: str | None) -> None:
        if not path:
            return
        sv = self.ctx.services
        sv.settings.add_recent(str(Path(path)))
        sv.settings.save()
        self._refresh_recent()
        sv.open_case(path)

    def _scan(self) -> None:
        from aethercfd.case.discovery import discover_cases

        roots = [Path(r) for r in self.ctx.services.settings.case_roots if Path(r).exists()]
        self.found.clear()
        self.found.addItem("Searching…")

        def work():
            out = []
            for r in roots:
                out += discover_cases(r, max_depth=5)
            return out

        def done(res):
            cases, err = res
            self.found.clear()
            if err is not None:
                self.found.addItem(f"Search failed: {err}")
                return
            for c in cases[:500]:
                it = QListWidgetItem(icon("mdi6.cube-outline"), str(c))
                it.setData(Qt.ItemDataRole.UserRole, str(c))
                self.found.addItem(it)
            if not cases:
                self.found.addItem("No cases found")

        self.ctx.services.run_bg(work, done)

    def _on_backend(self, session) -> None:
        forks = ", ".join(f.label for f in session.forks) or "none found"
        self.env.setText(f"WSL distro {session.runner.distro}: {forks}.")


