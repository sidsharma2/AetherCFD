"""Case Files: the real case on disk, never hidden. Time folders are grouped, file kinds are marked,
folders load lazily, and the tree follows changes on disk (solver writes, VS Code edits)."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QTreeView, QVBoxLayout

from aethercfd.case.model import FileKind, classify_file, is_time_dir
from aethercfd.core.paths import win_to_wsl
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
LOADED_ROLE = Qt.ItemDataRole.UserRole + 2
_KIND_ICON = {FileKind.DICT: "msc.symbol-key", FileKind.FIELD: "mdi6.chart-scatter-plot",
              FileKind.MESH: "mdi6.vector-triangle", FileKind.LOG: "msc.output", FileKind.SCRIPT: "msc.terminal",
              FileKind.POST: "msc.graph", FileKind.DOC: "msc.file-text", FileKind.OTHER: "msc.file"}
_HIDDEN = {".aethercfd", ".git", "__pycache__"}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class CaseTreePanel(Panel):
    file_activated = Signal(str)

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        self.filter = QLineEdit(placeholderText="Filter files…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter)
        lay.addLayout(bar)
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(["Name", "Size"])
        self.view = QTreeView()
        self.view.setModel(self.model)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(False)
        self.view.setHeaderHidden(True)
        self.view.setColumnWidth(0, 260)
        self.view.expanded.connect(self._on_expand)
        self.view.doubleClicked.connect(self._on_activate)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.selectionModel().currentChanged.connect(self._on_current)
        lay.addWidget(self.view, 1)
        self.root: Path | None = None
        self._refresh_timer = QTimer(self, singleShot=True, interval=400, timeout=self.refresh)
        ctx.services.files_changed.connect(self._files_changed_slot)
        ctx.services.case_changed.connect(self._case_changed_slot)
        if ctx.services.session is not None:
            self.set_root(ctx.services.session.root)

    def _on_current(self, cur, _prev) -> None:
        """Selecting a file shows its details in the Properties panel."""
        p = cur.siblingAtColumn(0).data(PATH_ROLE) if cur.isValid() else None
        if p and Path(p).is_file():
            self.ctx.services.select({"kind": "file", "path": p})

    # ------------------------------------------------------------------ model
    def _item(self, path: Path, label: str | None = None, *, is_dir: bool) -> list[QStandardItem]:
        name = QStandardItem(label or path.name)
        name.setEditable(False)
        name.setData(str(path), PATH_ROLE)
        name.setToolTip(str(path))
        size = QStandardItem("")
        size.setEditable(False)
        size.setForeground(Qt.GlobalColor.gray)
        if is_dir:
            name.setIcon(icon("msc.folder"))
            name.setData(False, LOADED_ROLE)
            name.appendRow([QStandardItem("…"), QStandardItem("")])  # lazy placeholder
        else:
            kind = classify_file(self.root, path) if self.root else FileKind.OTHER
            name.setIcon(icon(_KIND_ICON[kind]))
            with contextlib.suppress(OSError):
                size.setText(_human(path.stat().st_size))
        return [name, size]

    def _files_changed_slot(self, _paths=None) -> None:
        """services.files_changed -> here; a bound slot, so it is disconnected when the panel is deleted."""
        self._refresh_timer.start()

    def _case_changed_slot(self, s=None) -> None:
        """services.case_changed -> here; a bound slot, so it is disconnected when the panel is deleted."""
        self.set_root(s.root if s else None)

    def set_root(self, root: Path | None) -> None:
        self.root = Path(root) if root else None
        self.refresh()

    def refresh(self) -> None:
        expanded = self._expanded_paths()
        self.model.removeRows(0, self.model.rowCount())
        if self.root is None or not self.root.exists():
            return
        entries = sorted(self.root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        times = sorted((p for p in entries if p.is_dir() and is_time_dir(p.name)), key=lambda p: float(p.name))
        procs = [p for p in entries if p.is_dir() and p.name.startswith("processor")]
        for p in entries:
            if p.name in _HIDDEN or p in times or p in procs:
                continue
            self.model.appendRow(self._item(p, is_dir=p.is_dir()))
        # a decomposed run's results live in processor*/ until reconstructPar (CaseModel.processor_times)
        ptimes = []
        if procs and procs[0].is_dir():
            with contextlib.suppress(OSError):
                ptimes = [q.name for q in procs[0].iterdir() if q.is_dir() and is_time_dir(q.name)]
        if times:
            names = {t.name for t in times}
            extra = [t for t in ptimes if t not in names]
            latest = max([t.name for t in times] + ptimes, key=float)
            n = len(times) + len(extra)
            grp = QStandardItem(f"Results  ({n} time folder{'s' if n != 1 else ''}"
                                + (f", {len(extra)} only in processor*/" if extra else "") + f", latest {latest})")
            grp.setIcon(icon("mdi6.timeline-clock-outline", T.accent))
            grp.setEditable(False)
            for t in reversed(times):
                grp.appendRow(self._item(t, is_dir=True))
            self.model.insertRow(0, [grp, QStandardItem("")])
        if procs:
            res = [t for t in sorted(ptimes, key=float) if float(t) > 0]
            grp = QStandardItem(f"Decomposed  ({len(procs)} processor folders"
                                + (f"; results {res[0]} … {res[-1]}, latest {res[-1]}" if len(res) > 1 else
                                   f"; result {res[0]}" if res else "") + ")")
            grp.setIcon(icon("mdi6.grid"))
            grp.setEditable(False)
            for p in sorted(procs, key=lambda x: x.name):
                grp.appendRow(self._item(p, is_dir=True))
            self.model.appendRow([grp, QStandardItem("")])
        self._restore_expanded(expanded)
        self._apply_filter(self.filter.text())

    def _fill(self, item: QStandardItem) -> None:
        if item.data(LOADED_ROLE) is not False:
            return
        path = Path(item.data(PATH_ROLE))
        item.removeRows(0, item.rowCount())
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            children = []
        for c in children[:5000]:
            if c.name in _HIDDEN:
                continue
            item.appendRow(self._item(c, is_dir=c.is_dir()))
        if len(children) > 5000:
            item.appendRow([QStandardItem(f"… {len(children) - 5000} more"), QStandardItem("")])
        item.setData(True, LOADED_ROLE)

    def reveal(self, path: Path) -> bool:
        """Expand down to ``path`` and select it (used by the copilot to show the user a file)."""
        target = str(Path(path))

        def find(parent_item) -> QStandardItem | None:
            n = parent_item.rowCount() if parent_item is not None else self.model.rowCount()
            for r in range(n):
                it = parent_item.child(r) if parent_item is not None else self.model.item(r)
                if it is None:
                    continue
                p = it.data(PATH_ROLE)
                if p == target:
                    return it
                if p is None or target.startswith(p.rstrip("\\/") + ("\\" if "\\" in p else "/")):
                    self._fill(it)
                    hit = find(it)
                    if hit is not None:
                        return hit
            return None

        it = find(None)
        if it is None:
            return False
        idx = it.index()
        parent = idx.parent()
        while parent.isValid():
            self.view.expand(parent)
            parent = parent.parent()
        self.view.setCurrentIndex(idx)
        self.view.scrollTo(idx)
        return True

    def _on_expand(self, index: QModelIndex) -> None:
        item = self.model.itemFromIndex(index.siblingAtColumn(0))
        if item is not None:
            self._fill(item)
        self.view.resizeColumnToContents(0)

    def _expanded_paths(self) -> set[str]:
        out: set[str] = set()

        def walk(parent: QModelIndex):
            for r in range(self.model.rowCount(parent)):
                idx = self.model.index(r, 0, parent)
                if self.view.isExpanded(idx):
                    out.add(idx.data(PATH_ROLE) or idx.data())
                    walk(idx)

        walk(QModelIndex())
        return out

    def _restore_expanded(self, keys: set[str]) -> None:
        def walk(parent: QModelIndex):
            for r in range(self.model.rowCount(parent)):
                idx = self.model.index(r, 0, parent)
                key = idx.data(PATH_ROLE) or idx.data()
                if key in keys or (key and str(key).startswith("Results") and any(k.startswith("Results")
                                                                                   for k in keys)):
                    self.view.expand(idx)
                    walk(idx)

        walk(QModelIndex())

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()

        def walk(parent: QModelIndex) -> bool:
            any_visible = False
            for r in range(self.model.rowCount(parent)):
                idx = self.model.index(r, 0, parent)
                name = str(idx.data() or "").lower()
                child_visible = walk(idx) if self.model.hasChildren(idx) else False
                vis = not text or text in name or child_visible
                self.view.setRowHidden(r, parent, not vis)
                any_visible |= vis
            return any_visible

        walk(QModelIndex())

    # ------------------------------------------------------------------ actions
    def _path_at(self, index: QModelIndex) -> Path | None:
        p = index.siblingAtColumn(0).data(PATH_ROLE)
        return Path(p) if p else None

    def _on_activate(self, index: QModelIndex) -> None:
        p = self._path_at(index)
        if p is not None and p.is_file():
            self.open_file(p)

    def open_file(self, p: Path) -> None:
        sv = self.ctx.services
        spec = sv.panels.get("aether.editor")
        if spec is None:
            os.startfile(str(p))
            return
        for name, _d in sv.host.docks().items():
            panel = sv.host.panel(name)
            if panel is not None and getattr(panel, "path", None) == p:
                sv.host.open_panel(spec, instance=sv.host.split_name(name)[1])
                return
        sv.host.open_panel(spec, args={"path": str(p)})

    def _menu(self, pos) -> None:
        idx = self.view.indexAt(pos)
        p = self._path_at(idx)
        if p is None:
            return
        m = QMenu(self)
        if p.is_file():
            m.addAction(icon("msc.edit"), "Open", lambda: self.open_file(p))
        m.addAction(icon("msc.vscode"), "Open in VS Code", lambda: subprocess.Popen(
            ["code", str(p)], shell=True, creationflags=0x08000000))
        m.addAction(icon("msc.folder-opened"), "Reveal in Explorer",
                    lambda: subprocess.Popen(["explorer", "/select,", str(p)]))
        m.addSeparator()
        m.addAction("Copy Windows Path", lambda: QGuiApplication.clipboard().setText(str(p)))
        m.addAction("Copy WSL Path", lambda: QGuiApplication.clipboard().setText(win_to_wsl(str(p))))
        m.exec(self.view.viewport().mapToGlobal(pos))



PANELS = [PanelSpec("aether.case_tree", "Case Files", "msc.files", "Case", CaseTreePanel,
                    default_area=Area.LEFT, tab_group="nav", shortcut="Ctrl+Shift+E",
                    description="The OpenFOAM case on disk: dictionaries, fields, mesh, results, logs")]
