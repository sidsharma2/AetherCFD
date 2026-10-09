"""Diff: two files side by side, aligned, with synchronized scrolling.

Compare any two files, the same file in another case (e.g. a converged run and a failed one), or a file with
its content before a journalled change (what the copilot or a form edit actually did).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
)

from aethercfd.documents.diff import side_by_side
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

_COLORS = {"delete": "#5a2a2e", "insert": "#24452f", "replace": "#4d4122"}
_FILLER = "#2a2b30"


MAX_DIFF_LINES = 60_000  # difflib is quadratic in the worst case: beyond this the GUI would freeze


def _binary(p: Path) -> bool:
    """NUL bytes near the start and no OpenFOAM header (binary-format field files keep a text header)."""
    with open(p, "rb") as f:
        head = f.read(8192)
    return b"\0" in head and b"FoamFile" not in head


def _read(p: Path) -> str:
    from aethercfd.core.textio import decode_bytes

    return decode_bytes(p.read_bytes())[0]


class _Side(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.setFont(f)


class DiffPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        row = QHBoxLayout()
        self.left_path = QLineEdit(placeholderText="left file")
        self.right_path = QLineEdit(placeholderText="right file")
        bl = QPushButton(icon("msc.folder-opened"), "")
        br = QPushButton(icon("msc.folder-opened"), "")
        swap = QPushButton(icon("mdi6.swap-horizontal"), "")
        go = QPushButton(icon("msc.diff"), "Compare")
        for w in (self.left_path, bl, swap, self.right_path, br, go):
            row.addWidget(w, 1 if isinstance(w, QLineEdit) else 0)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        other = QPushButton("Same file in another case…")
        before = QPushButton("Left file before its last change")
        self.summary = QLabel()
        self.summary.setStyleSheet(f"color: {T.text_dim};")
        row2.addWidget(other)
        row2.addWidget(before)
        row2.addStretch(1)
        row2.addWidget(self.summary)
        lay.addLayout(row2)
        self.left, self.right = _Side(), _Side()
        heads = QHBoxLayout()
        self.left_head, self.right_head = QLabel(), QLabel()
        for h in (self.left_head, self.right_head):
            h.setStyleSheet(f"color: {T.text_faint};")
            heads.addWidget(h, 1)
        lay.addLayout(heads)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.left)
        split.addWidget(self.right)
        lay.addWidget(split, 1)
        self.left.verticalScrollBar().valueChanged.connect(self.right.verticalScrollBar().setValue)
        self.right.verticalScrollBar().valueChanged.connect(self.left.verticalScrollBar().setValue)
        self.left.horizontalScrollBar().valueChanged.connect(self.right.horizontalScrollBar().setValue)
        bl.clicked.connect(lambda: self._browse(self.left_path))
        br.clicked.connect(lambda: self._browse(self.right_path))
        swap.clicked.connect(self._swap)
        go.clicked.connect(self.compare_fields)
        other.clicked.connect(self._other_case)
        before.clicked.connect(self._before_last_change)

    # ------------------------------------------------------------------ entry points
    def compare_paths(self, a: str | Path, b: str | Path) -> str:
        a, b = Path(a), Path(b)
        for p in (a, b):
            if not p.is_file():
                self.summary.setText(f"{p} is not a file")
                return f"{p} is not a file"
            if _binary(p):
                self.summary.setText(f"{p.name} is a binary file: not compared")
                return f"{p.name} is a binary file: not compared"
        self.left_path.setText(str(a))
        self.right_path.setText(str(b))
        return self.compare_texts(_read(a), _read(b), self._label(a), self._label(b))

    def compare_texts(self, a: str, b: str, a_label: str = "left", b_label: str = "right") -> str:
        n = max(a.count("\n"), b.count("\n"))
        if n > MAX_DIFF_LINES:
            msg = (f"{n:,} lines: too large to compare here (field data?). Use the Python console, e.g. "
                   "np.abs(a - b).max() on data.fields(...)")
            self.summary.setText(msg)
            return msg
        d = side_by_side(a, b)
        self.left_head.setText(a_label)
        self.right_head.setText(b_label)
        self._fill(self.left, [(r.left, r.kind) for r in d.rows])
        self._fill(self.right, [(r.right, r.kind) for r in d.rows])
        self.summary.setText(d.summary())
        first = next((i for i, r in enumerate(d.rows) if r.kind != "equal"), 0)
        self.left.verticalScrollBar().setValue(max(0, first - 3))
        return d.summary()

    def compare_fields(self) -> None:
        self.compare_paths(self.left_path.text().strip(), self.right_path.text().strip())

    # ------------------------------------------------------------------ helpers
    def _label(self, p: Path) -> str:
        s = self.sv.session
        if s is not None:
            try:
                return str(p.resolve().relative_to(Path(s.root).resolve()))
            except ValueError:
                pass
        return str(p)

    def _fill(self, ed: QPlainTextEdit, rows: list[tuple[str | None, str]]) -> None:
        ed.setPlainText("\n".join(t if t is not None else "" for t, _ in rows))
        sels = []
        doc = ed.document()
        for i, (text, kind) in enumerate(rows):
            color = _FILLER if text is None else _COLORS.get(kind)
            if color is None:
                continue
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(color))
            fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.format = fmt
            cur = QTextCursor(doc.findBlockByNumber(i))
            sel.cursor = cur
            sels.append(sel)
        ed.setExtraSelections(sels)

    def _browse(self, edit: QLineEdit) -> None:
        start = edit.text() or (str(self.sv.session.root) if self.sv.session else "")
        f, _ = QFileDialog.getOpenFileName(self, "File to compare", start)
        if f:
            edit.setText(f)

    def _swap(self) -> None:
        a, b = self.left_path.text(), self.right_path.text()
        self.left_path.setText(b)
        self.right_path.setText(a)
        if a and b:
            self.compare_fields()

    def _other_case(self) -> None:
        s = self.sv.session
        a = Path(self.left_path.text().strip())
        if s is None or not a.is_file():
            self.summary.setText("Choose the left file (inside the open case) first")
            return
        d = QFileDialog.getExistingDirectory(self, "Other case", str(Path(s.root).parent))
        if d:
            self.compare_with_other_case(a, d)

    def compare_with_other_case(self, a: str | Path, other_case: str | Path) -> str:
        s = self.sv.session
        a = Path(a)
        try:
            rel = a.resolve().relative_to(Path(s.root).resolve())
        except (ValueError, AttributeError):
            return "the left file is not inside the open case"
        b = Path(other_case) / rel
        if not b.is_file():
            self.summary.setText(f"{rel} does not exist in {Path(other_case).name}")
            return f"{rel} does not exist in {other_case}"
        out = self.compare_paths(a, b)
        self.right_head.setText(f"{Path(other_case).name}: {rel}")
        return out

    def _before_last_change(self) -> None:
        self.before_last_change(self.left_path.text().strip())

    def before_last_change(self, path: str | Path) -> str:
        """Left = the file before the most recent journalled change to it, right = the file now."""
        s = self.sv.session
        p = Path(path)
        if s is None or s.project is None or not p.is_file():
            msg = "no journal for this file (open a case with an AetherCFD project and pick a file in it)"
            self.summary.setText(msg)
            return msg
        target = str(p.resolve()).lower()
        for e in reversed(list(s.project.journal.entries())):
            for f in e.get("files", []):
                if str(Path(f["path"]).resolve()).lower() == target:
                    before = s.project.objects.get(f["before"]) if f.get("before") else b""
                    from aethercfd.core.textio import decode_bytes

                    self.left_path.setText(str(p))
                    self.right_path.setText(str(p))
                    return self.compare_texts(decode_bytes(before or b"")[0], _read(p),
                                              f"before: {e.get('summary', e.get('command'))} ({e.get('actor', '')})",
                                              f"now: {self._label(p)}")
        self.summary.setText("this file has no journalled change")
        return "this file has no journalled change"


PANELS = [PanelSpec("aether.diff", "Diff", "msc.diff", "Case", DiffPanel, default_area=Area.CENTER,
                    description="Two files side by side: vs another case, or before a journalled change")]
