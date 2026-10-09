"""Editor panel: Raw / Form / Split views over ONE core Document per file.

- Raw: CodeEditor. Typing becomes minimal edits on the core Document (coalesced for undo).
- Form: a structured tree of the dictionary; editing a literal value goes through the same Document
  (minimal, style-preserving text edits), so raw and form views stay in sync and share undo.
- Save goes through the journalled ``file.save`` command with a conflict check; OpenFOAM validation
  (foamDictionary -expand) runs after saving and shows diagnostics in the gutter.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aethercfd.commands.base import USER
from aethercfd.foamdict.cst import Atom, DictNode, Entry, ListNode
from aethercfd.foamdict.edits import EditError
from aethercfd.foamdict.semantic import DictView, Provenance
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T
from aethercfd.studio.widgets.code_editor import CodeEditor, EditorDiagnostic, FindBar

log = logging.getLogger(__name__)
LARGE_BYTES = 5 * 2**20
_VALIDATE_DIRS = ("system", "constant")


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _prefix_suffix_diff(old: str, new: str) -> tuple[int, int, str]:
    """(start, end_in_old, replacement) of the single changed span between old and new."""
    n = min(len(old), len(new))
    i = 0
    step = 4096
    while i + step <= n and old[i:i + step] == new[i:i + step]:
        i += step
    while i < n and old[i] == new[i]:
        i += 1
    j = 0
    m = min(len(old), len(new)) - i
    while j < m and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
        j += 1
    return i, len(old) - j, new[i:len(new) - j]


class _Banner(QFrame):
    def __init__(self, color: str):
        super().__init__()
        self.setStyleSheet(f"QFrame {{ background: {T.alt}; border-left: 3px solid {color}; }}"
                           "QLabel { padding: 4px; }")
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(6, 2, 6, 2)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.lay.addWidget(self.label, 1)
        self.hide()


class EditorPanel(Panel):
    STATE_VERSION = 1

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.path: Path | None = None
        self.doc = None
        self.readonly = False
        self._syncing = False
        self._unsub = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # header: path + mode switch + save
        head = QHBoxLayout()
        head.setContentsMargins(8, 3, 6, 3)
        self.crumb = QLabel()
        self.crumb.setStyleSheet(f"color: {T.text_dim};")
        head.addWidget(self.crumb, 1)
        self.mode_group = QButtonGroup(self)
        for i, (name, ic) in enumerate((("Raw", "msc.code"), ("Form", "msc.list-tree"), ("Split", "msc.split-horizontal"))):
            b = QToolButton(text=name, icon=icon(ic), checkable=True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.mode_group.addButton(b, i)
            head.addWidget(b)
        self.mode_group.idClicked.connect(self._set_mode)
        self.save_btn = QToolButton(text="Save", icon=icon("msc.save"))
        self.save_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.save_btn.clicked.connect(self.save)
        head.addWidget(self.save_btn)
        lay.addLayout(head)
        self.gen_banner = _Banner(T.warn)
        self.conflict_banner = _Banner(T.error)
        for txt, slot in (("Take disk version", lambda: self._resolve("theirs")),
                          ("Merge", lambda: self._resolve("merged")), ("Keep mine", lambda: self._resolve("mine"))):
            b = QPushButton(txt)
            b.clicked.connect(slot)
            self.conflict_banner.lay.addWidget(b)
        self.info_banner = _Banner(T.info)
        for b in (self.gen_banner, self.conflict_banner, self.info_banner):
            lay.addWidget(b)
        # views
        self.editor = CodeEditor()
        self.editor.undo_requested.connect(lambda: self.doc and self.doc.undo())
        self.editor.redo_requested.connect(lambda: self.doc and self.doc.redo())
        self.editor.save_requested.connect(self.save)
        self.editor.document().contentsChange.connect(self._on_editor_change)
        self._kw_timer = QTimer(self, singleShot=True, interval=400, timeout=self._select_keyword)
        self.editor.cursorPositionChanged.connect(self._kw_timer.start)
        self.find = FindBar(self.editor)
        QShortcut(QKeySequence("Ctrl+F"), self, self.find.open, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Escape"), self.find, self.find.hide,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.form = QTreeWidget()
        self.form.setHeaderLabels(["Keyword", "Value", ""])
        self.form.setColumnWidth(0, 240)
        self.form.setColumnWidth(1, 320)
        self.form.itemChanged.connect(self._on_form_edit)
        self.form.itemDoubleClicked.connect(self._on_form_double)
        raw_box = QWidget()
        rl = QVBoxLayout(raw_box)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.find)
        rl.addWidget(self.editor, 1)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(raw_box)
        self.split.addWidget(self.form)
        lay.addWidget(self.split, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {T.text_faint}; padding: 2px 8px;")
        lay.addWidget(self.status)
        self._parse_timer = QTimer(self, singleShot=True, interval=250, timeout=self._reparse)
        self.mode_group.button(0).setChecked(True)
        self._set_mode(0)
        if "path" in ctx.args:
            self.open(Path(ctx.args["path"]))

    # ------------------------------------------------------------------ open
    def open(self, path: Path) -> None:
        if not Path(path).is_file():
            self.ctx.services.notify(f"{Path(path).name}: no such file ({path})", "warn")
            return
        self.path = Path(path)
        self.crumb.setText(str(self.path))
        self.title_changed.emit(self.path.name)
        size = self.path.stat().st_size if self.path.exists() else 0
        if size > LARGE_BYTES:
            self.readonly = True
            with open(self.path, "rb") as f:
                head = f.read(200_000).decode("utf-8", errors="replace")
            self._set_editor_text(head)
            self.editor.setReadOnly(True)
            self.info_banner.label.setText(f"Large file ({size / 2**20:.0f} MB): showing the beginning read-only. "
                                           "Use 'Open in VS Code' from Case Files for full editing.")
            self.info_banner.show()
            return
        store = self.ctx.services.documents
        self.doc = store.open(self.path)
        self._unsub = self.doc.subscribe(self._on_doc_event)
        if self.doc.snapshot.encoding == "latin-1":
            self.readonly = True
            self.editor.setReadOnly(True)
            self._set_editor_text(self._binary_view(self.doc.text))
            self.info_banner.label.setText("Binary-format field file: shown read-only, binary payloads elided. "
                                           "Edit boundary conditions via the Form view after converting to ASCII "
                                           "(foamFormatConvert), or in a text editor.")
            self.info_banner.show()
        else:
            self._set_editor_text(_lf(self.doc.text))
        head = self.doc.text[:3000].lower()
        if "generated by" in head or "do not hand-edit" in head or "do not edit" in head:
            line = next((ln for ln in self.doc.text[:3000].splitlines() if "generated" in ln.lower()), "")
            self.gen_banner.label.setText(f"This file says: {line.strip(' /*')}. Changes may be overwritten when "
                                          "it is regenerated; consider editing the generator instead.")
            self.gen_banner.show()
        self._reparse()
        self._update_status()

    def _binary_view(self, text: str) -> str:
        from aethercfd.foamdict.tokens import K, tokenize

        out = []
        pos = 0
        for t in tokenize(text):
            if t.kind is K.BLOB:
                out.append(text[pos:t.start])
                out.append(f"<binary payload: {t.end - t.start:,} bytes>")
                pos = t.end
        out.append(text[pos:])
        return "".join(out)

    # ------------------------------------------------------------------ editor <-> document sync
    def _set_editor_text(self, text: str) -> None:
        self._syncing = True
        try:
            self.editor.setPlainText(text)
        finally:
            self._syncing = False

    def _to_doc_newlines(self, lf_text: str) -> str:
        return lf_text.replace("\n", "\r\n") if self.doc and self.doc.newline == "\r\n" else lf_text

    def _on_editor_change(self, pos: int, removed: int, added: int) -> None:
        if self._syncing or self.doc is None or self.readonly:
            return
        new = self._to_doc_newlines(self.editor.toPlainText())
        s, e, rep = _prefix_suffix_diff(self.doc.text, new)
        if (s, e, rep) == (len(new), len(new), ""):
            return
        from aethercfd.foamdict.edits import TextEdit

        self._syncing = True
        try:
            self.doc.apply([TextEdit(s, e, rep)], origin="editor", coalesce=True)
        finally:
            self._syncing = False

    def _on_doc_event(self, doc, kind: str, info: dict) -> None:
        if kind in ("edited", "reloaded") and not (self._syncing and info.get("origin") == "editor"):
            self._sync_editor_from_doc()
        if kind == "conflict":
            mp = info.get("merge")
            clean = "can be merged automatically" if mp is not None and mp.clean else \
                f"has {len(mp.conflicts)} conflicting region(s)" if mp is not None else ""
            self.conflict_banner.label.setText(f"{self.path.name} changed on disk while you were editing; "
                                               f"the change {clean}.")
            self.conflict_banner.show()
        if kind in ("edited", "reloaded", "saved", "dirty", "conflict"):
            self._parse_timer.start()
            self._update_status()

    def _sync_editor_from_doc(self) -> None:
        target = _lf(self.doc.text)
        current = self.editor.toPlainText()
        if target == current:
            return
        s, e, rep = _prefix_suffix_diff(current, target)
        self._syncing = True
        try:
            c = QTextCursor(self.editor.document())
            c.setPosition(s)
            c.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            c.beginEditBlock()
            c.insertText(rep)
            c.endEditBlock()
        finally:
            self._syncing = False

    # ------------------------------------------------------------------ parse / form
    def _reparse(self) -> None:
        if self.doc is None:
            return
        parsed = self.doc.parsed()
        diags = []
        for d in parsed.diagnostics:
            line, _ = parsed.line_col(d.start)
            diags.append(EditorDiagnostic(line, d.message, d.severity))
        self._parse_diags = diags
        self.editor.set_diagnostics(diags + getattr(self, "_foam_diags", []))
        self._rebuild_form(parsed)

    def _rebuild_form(self, parsed) -> None:
        if not self.form.isVisible() and self.mode_group.checkedId() == 0:
            return
        expanded = {self._item_path(it) for it in self._iter_items() if it.isExpanded()}
        self.form.blockSignals(True)
        self.form.clear()
        if not parsed.ok:
            err = QTreeWidgetItem(["(raw text has errors — fix them to use the form)", "", ""])
            err.setForeground(0, Qt.GlobalColor.red)
            self.form.addTopLevelItem(err)
            self.form.blockSignals(False)
            return
        view = DictView(parsed)
        self._fill_form(self.form.invisibleRootItem(), parsed.root, view, [])
        for it in self._iter_items():
            if self._item_path(it) in expanded:
                it.setExpanded(True)
        self.form.blockSignals(False)

    def _fill_form(self, parent: QTreeWidgetItem, d: DictNode, view: DictView, path: list[str]) -> None:
        for m in d.members:
            if not isinstance(m, Entry):
                continue
            key = m.key.name
            if key == "FoamFile" and not path:
                continue
            p = [*path, key]
            if m.dict is not None:
                it = QTreeWidgetItem([key, "", ""])
                it.setIcon(0, icon("msc.symbol-namespace"))
                it.setData(0, Qt.ItemDataRole.UserRole, p)
                parent.addChild(it)
                self._fill_form(it, m.dict, view, p)
                continue
            text = view.doc.text(m.value.start, m.value.end) if m.value else ""
            prov = view.provenance_of(m)
            big = m.value is not None and any(isinstance(i, ListNode) and i.blob for i in m.value.items)
            shown = "(large list — edit in raw view)" if big else text.replace("\n", " ")[:300]
            it = QTreeWidgetItem([key, shown, "" if prov is Provenance.LITERAL else prov.value])
            it.setData(0, Qt.ItemDataRole.UserRole, p)
            if m.key.is_pattern:
                it.setToolTip(0, "pattern keyword: applies to every name it matches")
                it.setForeground(0, Qt.GlobalColor.darkCyan)
            if prov is Provenance.LITERAL and not big and not self.readonly:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            else:
                it.setForeground(1, Qt.GlobalColor.gray)
                it.setToolTip(1, f"{prov.value}: double-click to jump to the source")
            parent.addChild(it)

    def _iter_items(self):
        stack = [self.form.topLevelItem(i) for i in range(self.form.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it is None:
                continue
            yield it
            stack.extend(it.child(i) for i in range(it.childCount()))

    @staticmethod
    def _item_path(it: QTreeWidgetItem) -> str:
        return "/".join(it.data(0, Qt.ItemDataRole.UserRole) or [])

    def _on_form_edit(self, it: QTreeWidgetItem, col: int) -> None:
        if col != 1 or self.doc is None:
            return
        path = it.data(0, Qt.ItemDataRole.UserRole)
        try:
            self.doc.form_set("/".join(path), it.text(1).strip())
        except EditError as exc:
            self.ctx.services.notify(str(exc), "warn")
            QTimer.singleShot(0, self._reparse)

    def _on_form_double(self, it: QTreeWidgetItem, col: int) -> None:
        """Non-editable values: jump to where they are defined (edit at source)."""
        if self.doc is None or it.flags() & Qt.ItemFlag.ItemIsEditable:
            return
        view = DictView(self.doc.parsed())
        lk = view.lookup(it.data(0, Qt.ItemDataRole.UserRole) or [])
        target = lk.entry if lk else None
        if lk is not None and lk.entry is not None and lk.entry.value is not None:
            macro = next((a for a in lk.entry.value.atoms if a.kind.name == "MACRO"), None)
            if isinstance(macro, Atom):
                target = view.resolve_macro(macro.text, from_path=(it.data(0, Qt.ItemDataRole.UserRole) or [])[:-1]) \
                    or target
        if target is not None:
            line, _ = self.doc.parsed().line_col(target.start)
            self._set_mode(0)
            self.mode_group.button(0).setChecked(True)
            self.editor.goto_line(line)

    def _set_mode(self, mode: int) -> None:
        raw, form = mode in (0, 2), mode in (1, 2)
        self.split.widget(0).setVisible(raw)
        self.form.setVisible(form)
        if form and self.doc is not None:
            self._rebuild_form(self.doc.parsed())

    # ------------------------------------------------------------------ save / conflicts
    def _select_keyword(self) -> None:
        """The keyword under the cursor -> Properties panel (value, provenance, line)."""
        if self.path is None or not self.editor.hasFocus():
            return
        from aethercfd.foamdict.parser import parse
        from aethercfd.foamdict.semantic import path_at

        text = self.editor.toPlainText()
        if len(text) > 2_000_000:
            return
        doc = parse(text)
        path = [p for p in path_at(doc, self.editor.textCursor().position()) if p != "FoamFile"]
        if path:
            self.ctx.services.select({"kind": "keyword", "file": str(self.path), "path": path, "text": text})

    def save(self) -> None:
        if self.doc is None or self.readonly or not self.doc.dirty:
            return
        sv = self.ctx.services
        if not sv.ensure_project():
            sv.notify("Open the case first", "warn")
            return
        res = sv.session.executor.execute("file.save", {"path": str(self.path), "text": self.doc.text,
                                                        "expected_sha": self.doc.snapshot.sha256}, USER)
        if not res.ok:
            sv.notify(f"Save failed: {res.message}", "error")
            self.doc.check_disk()
            return
        self.doc.mark_saved()
        sv.notify(f"Saved {self.path.name} (journal {res.journal_id})")
        self._validate_with_openfoam()

    def _validate_with_openfoam(self) -> None:
        sv = self.ctx.services
        s = sv.session
        if s is None or s.runner is None or not s.forks or self.path is None:
            return
        try:
            rel = self.path.relative_to(s.root).as_posix()
        except ValueError:
            return
        if not rel.startswith(_VALIDATE_DIRS):
            return
        case = s.case()
        fork = case.fork_guess.pick(s.forks)
        if fork is None:
            return
        from aethercfd.foamdict.validate import validate_files

        def work():
            return validate_files(s.runner, fork, s.root, [rel])

        def done(res):
            out, err = res
            if err is not None:
                return
            issues = out.get(rel, [])
            self._foam_diags = [EditorDiagnostic(i.line or 1, f"OpenFOAM ({fork.short_tag}): {i.message}")
                                for i in issues]
            self.editor.set_diagnostics(getattr(self, "_parse_diags", []) + self._foam_diags)
            if issues:
                sv.notify(f"OpenFOAM reports a problem in {self.path.name}: {issues[0].message[:120]}", "warn")

        self.status.setText(self.status.text() + "  ·  validating with OpenFOAM…")
        sv.run_bg(work, done)

    def _resolve(self, how: str) -> None:
        if self.doc is not None:
            self.doc.resolve_conflict(how)
        self.conflict_banner.hide()

    def _update_status(self) -> None:
        if self.doc is None:
            return
        snap = self.doc.snapshot
        nl = "CRLF" if snap.newline == "\r\n" else "LF"
        dirty = "● modified" if self.doc.dirty else "saved"
        self.status.setText(f"{dirty}  ·  {snap.encoding}  ·  {nl}  ·  {len(self.doc.text):,} chars")
        self.dirty_changed.emit(self.doc.dirty)
        self.title_changed.emit(("● " if self.doc.dirty else "") + (self.path.name if self.path else "Editor"))

    # ------------------------------------------------------------------ panel API
    def save_state(self) -> dict:
        return {"path": str(self.path) if self.path else None, "mode": self.mode_group.checkedId(),
                "line": self.editor.textCursor().blockNumber() + 1}

    def restore_state(self, state: dict) -> None:
        if state.get("path") and self.path is None and Path(state["path"]).exists():
            self.open(Path(state["path"]))
        mode = state.get("mode", 0)
        if self.mode_group.button(mode):
            self.mode_group.button(mode).setChecked(True)
            self._set_mode(mode)
        if state.get("line"):
            self.editor.goto_line(state["line"])

    def can_close(self) -> bool:
        if self.doc is None or not self.doc.dirty:
            return True
        from PySide6.QtWidgets import QMessageBox

        r = QMessageBox.question(self, "Unsaved changes", f"Save changes to {self.path.name}?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            self.save()
            return not self.doc.dirty
        return r == QMessageBox.StandardButton.Discard

    def dispose(self) -> None:
        if self._unsub:
            self._unsub()


PANELS = [PanelSpec("aether.editor", "Editor", "msc.edit", "Edit", EditorPanel, multi_instance=True,
                    default_area=Area.CENTER, close_policy="destroy",
                    description="Raw / form / split editing of OpenFOAM dictionaries (lossless)")]
