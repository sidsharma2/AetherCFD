"""Command palette (Ctrl+K / Ctrl+Shift+P): one search box for actions, panels, perspectives,
monitors, case files (#) and dictionary keywords (@). Fuzzy-ranked with rapidfuzz."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout
from rapidfuzz import fuzz, process

from aethercfd.case.model import is_time_dir
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T


@dataclass
class PaletteItem:
    label: str
    category: str
    run: Callable[[], None]
    icon: str = "msc.symbol-event"
    detail: str = ""


class CommandPalette(QDialog):
    MAX = 60

    def __init__(self, window):
        super().__init__(window, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.window = window
        self.setStyleSheet(f"QDialog {{ background: {T.raised}; border: 1px solid {T.border}; border-radius: 6px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.edit = QLineEdit(placeholderText="Search actions, panels, files (#), keywords (@)…")
        self.edit.textChanged.connect(self._filter)
        self.edit.returnPressed.connect(self._run_current)
        self.list = QListWidget()
        self.list.itemActivated.connect(lambda it: self._run(it))
        lay.addWidget(self.edit)
        lay.addWidget(self.list)
        self.items: list[PaletteItem] = []
        self.resize(640, 420)
        QShortcut(QKeySequence("Down"), self.edit, lambda: self._move(1))
        QShortcut(QKeySequence("Up"), self.edit, lambda: self._move(-1))

    # ------------------------------------------------------------------ providers
    def _collect(self) -> list[PaletteItem]:
        w = self.window
        sv = w.services
        out: list[PaletteItem] = []

        def walk(menu, path):
            for a in menu.actions():
                if a.menu():
                    walk(a.menu(), [*path, a.text().replace("&", "")])
                elif a.text() and not a.isSeparator() and a.isEnabled():
                    out.append(PaletteItem(" › ".join([*path, a.text().replace("&", "")]), "action",
                                           a.trigger, "msc.symbol-event", a.shortcut().toString()))

        for a in w.menuBar().actions():
            if a.menu():
                walk(a.menu(), [a.text().replace("&", "")])
        for spec in sv.panels.specs():
            out.append(PaletteItem(f"Open {spec.title}", "panel", lambda s=spec: sv.host.open_panel(s), spec.icon,
                                   spec.description))
        from aethercfd.studio.layout.recipes import PERSPECTIVES
        from aethercfd.studio.layout.screens import current_screens

        for p in PERSPECTIVES:
            out.append(PaletteItem(f"Perspective: {p.title}", "perspective",
                                   lambda pid=p.id: w.layouts.switch(pid), "msc.layout", p.shortcut))
        focused = sv.host.manager.focusedDockWidget()
        if focused is not None:
            name = focused.objectName()
            for s in current_screens():
                out.append(PaletteItem(f"Move {focused.windowTitle()} to {s.label}", "monitor",
                                       lambda i=s.index, n=name: w.layouts.move_to_monitor(n, i),
                                       "mdi6.monitor-arrow-down"))
        return out

    def _case_files(self) -> list[PaletteItem]:
        sv = self.window.services
        s = sv.session
        if s is None:
            return []
        out = []
        root = s.root
        times = sorted((float(p.name), p.name) for p in root.iterdir() if p.is_dir() and is_time_dir(p.name))
        keep_times = {times[0][1], times[-1][1]} if times else set()
        for dirpath, dirnames, files in os.walk(root):
            rel = Path(dirpath).relative_to(root)
            top = rel.parts[0] if rel.parts else ""
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "processor")) and d != "postProcessing"
                           and not (not rel.parts and is_time_dir(d) and d not in keep_times) and d != "polyMesh"]
            for f in files:
                p = Path(dirpath) / f
                out.append(PaletteItem(str(p.relative_to(root)), "file",
                                       lambda pp=p: self._open_file(pp), "msc.file", top))
            if len(out) > 3000:
                break
        return out

    def _keywords(self) -> list[PaletteItem]:
        sv = self.window.services
        s = sv.session
        if s is None:
            return []
        from aethercfd.foamdict.cst import Entry
        from aethercfd.foamdict.parser import parse

        out = []
        for f in sorted((s.root / "system").glob("*")) + sorted((s.root / "constant").glob("*")):
            if not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            try:
                doc = parse(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue

            def walk(d, path, f=f, doc=doc):
                for m in d.members:
                    if isinstance(m, Entry) and m.key.name != "FoamFile":
                        p = [*path, m.key.name]
                        val = "" if m.value is None else doc.text(m.value.start, m.value.end)[:40]
                        line = doc.line_col(m.start)[0]
                        out.append(PaletteItem(f"{f.name} › {' › '.join(p)}", "keyword",
                                               lambda ff=f, ln=line: self._open_file(ff, ln), "msc.symbol-key", val))
                        if m.dict is not None and len(p) < 4:
                            walk(m.dict, p)

            walk(doc.root, [])
        return out

    def _open_file(self, p: Path, line: int | None = None) -> None:
        sv = self.window.services
        spec = sv.panels.get("aether.editor")
        if spec is None:
            return
        d = sv.host.open_panel(spec, args={"path": str(p)})
        if line:
            panel = sv.host.panel(d.objectName())
            if panel is not None:
                panel.editor.goto_line(line)

    # ------------------------------------------------------------------ ui
    def open(self) -> None:
        self.items = self._collect()
        self.edit.clear()
        self._filter("")
        g = self.window.geometry()
        self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 80)
        self.show()
        self.edit.setFocus()

    def _filter(self, text: str) -> None:
        text = text.strip()
        pool = self.items
        if text.startswith("#"):
            pool, text = self._case_files(), text[1:].strip()
        elif text.startswith("@"):
            pool, text = self._keywords(), text[1:].strip()
        elif text.startswith(">"):
            pool, text = [i for i in self.items if i.category == "action"], text[1:].strip()
        self.list.clear()
        if not text:
            ranked = pool[: self.MAX]
        else:
            labels = [i.label for i in pool]
            hits = process.extract(text, labels, scorer=fuzz.WRatio, limit=self.MAX, score_cutoff=45)
            ranked = [pool[idx] for _label, _score, idx in hits]
        for it in ranked:
            li = QListWidgetItem(icon(it.icon), f"{it.label}" + (f"    {it.detail}" if it.detail else ""))
            li.setData(Qt.ItemDataRole.UserRole, it)
            self.list.addItem(li)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _move(self, d: int) -> None:
        self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + d)))

    def _run_current(self) -> None:
        it = self.list.currentItem()
        if it is not None:
            self._run(it)

    def _run(self, li: QListWidgetItem) -> None:
        item: PaletteItem = li.data(Qt.ItemDataRole.UserRole)
        self.hide()
        item.run()


def install(window) -> CommandPalette:
    pal = CommandPalette(window)
    act = QAction("Command Palette…", window)
    act.setShortcuts([QKeySequence("Ctrl+K"), QKeySequence("Ctrl+Shift+P")])
    act.triggered.connect(pal.open)
    window.addAction(act)
    window.menuBar().actions()[1].menu().insertAction(window.menuBar().actions()[1].menu().actions()[0], act)
    return pal
