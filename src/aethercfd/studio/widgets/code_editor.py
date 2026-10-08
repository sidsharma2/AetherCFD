"""Code editor for OpenFOAM dictionaries: QPlainTextEdit + line-number gutter + highlighter +
find bar + diagnostics (squiggles and gutter marks). Its own undo is disabled: undo lives in the
core Document so it covers form and raw edits alike (ADR 0002 / plan)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QKeySequence,
    QPainter,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

from aethercfd.studio.theme.style import mono_font
from aethercfd.studio.theme.tokens import T


def _fmt(color: str, *, italic: bool = False, bold: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if italic:
        f.setFontItalic(True)
    if bold:
        f.setFontWeight(600)
    return f


class FoamHighlighter(QSyntaxHighlighter):
    """Per-line regexes plus block state for /* ... */ comments and #{ ... #} code blocks."""

    IN_COMMENT, IN_CODE = 1, 2
    F: ClassVar[dict[str, QTextCharFormat]] = {
        "comment": _fmt("#6a9955", italic=True), "string": _fmt("#ce9178"), "number": _fmt("#b5cea8"),
        "directive": _fmt("#c586c0"), "macro": _fmt("#4ec9b0"), "key": _fmt("#9cdcfe"),
        "keyword": _fmt("#569cd6"), "dims": _fmt("#808080"), "code": _fmt("#d7ba7d"), "type": _fmt("#4ec9b0"),
    }
    RULES: ClassVar[list] = [
        (re.compile(r"^\s*([A-Za-z_][\w.:<>(),|*-]*|\"[^\"]*\")(?=\s)"), "key", 1),
        (re.compile(r"\b(uniform|nonuniform|true|false|yes|no|on|off|none|default)\b"), "keyword", 0),
        (re.compile(r"\b(List<\w+>|vector|scalar|tensor|symmTensor)\b"), "type", 0),
        (re.compile(r"(?<![\w.])[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?(?![\w.])"), "number", 0),
        (re.compile(r"\[[^\]]*\]"), "dims", 0),
        (re.compile(r"#[A-Za-z_]\w*"), "directive", 0),
        (re.compile(r"\$\{[^}]*\}|\$[^\s;{}()\[\]\"]+"), "macro", 0),
        (re.compile(r"\"(?:[^\"\\]|\\.)*\""), "string", 0),
    ]

    def highlightBlock(self, text: str) -> None:
        state = self.previousBlockState()
        start = 0
        if state == self.IN_CODE:
            end = text.find("#}")
            if end < 0:
                self.setFormat(0, len(text), self.F["code"])
                self.setCurrentBlockState(self.IN_CODE)
                return
            self.setFormat(0, end + 2, self.F["code"])
            start = end + 2
        elif state == self.IN_COMMENT:
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, len(text), self.F["comment"])
                self.setCurrentBlockState(self.IN_COMMENT)
                return
            self.setFormat(0, end + 2, self.F["comment"])
            start = end + 2
        self.setCurrentBlockState(0)
        seg = text[start:]
        for rx, name, group in self.RULES:
            for m in rx.finditer(seg):
                s, e = m.span(group)
                self.setFormat(start + s, e - s, self.F[name])
        # comments last so they win
        i = start
        while i < len(text):
            lc = text.find("//", i)
            bc = text.find("/*", i)
            cb = text.find("#{", i)
            cands = [x for x in (lc, bc, cb) if x >= 0]
            if not cands:
                break
            j = min(cands)
            if j == lc and not _in_string(text, j):
                self.setFormat(j, len(text) - j, self.F["comment"])
                break
            if j == bc and not _in_string(text, j):
                end = text.find("*/", j + 2)
                if end < 0:
                    self.setFormat(j, len(text) - j, self.F["comment"])
                    self.setCurrentBlockState(self.IN_COMMENT)
                    break
                self.setFormat(j, end + 2 - j, self.F["comment"])
                i = end + 2
                continue
            if j == cb:
                end = text.find("#}", j + 2)
                if end < 0:
                    self.setFormat(j, len(text) - j, self.F["code"])
                    self.setCurrentBlockState(self.IN_CODE)
                    break
                self.setFormat(j, end + 2 - j, self.F["code"])
                i = end + 2
                continue
            i = j + 2


def _in_string(text: str, pos: int) -> bool:
    return text.count('"', 0, pos) % 2 == 1


@dataclass(frozen=True)
class EditorDiagnostic:
    line: int  # 1-based
    message: str
    severity: str = "error"


class _Gutter(QWidget):
    def __init__(self, editor: CodeEditor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, e) -> None:
        self.editor.paint_gutter(e)


class CodeEditor(QPlainTextEdit):
    undo_requested = Signal()
    redo_requested = Signal()
    save_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setUndoRedoEnabled(False)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.highlighter = FoamHighlighter(self.document())
        self.diagnostics: list[EditorDiagnostic] = []
        self._gutter = _Gutter(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width()
        self._highlight_current_line()
        for seq, sig in (("Ctrl+Z", self.undo_requested), ("Ctrl+Y", self.redo_requested),
                         ("Ctrl+Shift+Z", self.redo_requested), ("Ctrl+S", self.save_requested)):
            sc = QShortcut(QKeySequence(seq), self, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(sig.emit)

    # ------------------------------------------------------------------ gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, e) -> None:
        p = QPainter(self._gutter)
        p.fillRect(e.rect(), QColor(T.panel))
        diag_lines = {d.line: d for d in self.diagnostics}
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        h = self.fontMetrics().height()
        while block.isValid() and top <= e.rect().bottom():
            if block.isVisible() and bottom >= e.rect().top():
                d = diag_lines.get(num + 1)
                if d is not None:
                    p.setPen(QColor(T.error if d.severity == "error" else T.warn))
                    p.drawText(2, top, 10, h, Qt.AlignmentFlag.AlignLeft, "●")
                p.setPen(QColor(T.text if num == cur else T.text_faint))
                p.drawText(0, top, self._gutter.width() - 6, h, Qt.AlignmentFlag.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1

    # ------------------------------------------------------------------ highlights
    def _highlight_current_line(self) -> None:
        self._apply_extra()

    def set_diagnostics(self, diags: list[EditorDiagnostic]) -> None:
        self.diagnostics = diags
        self._apply_extra()
        self._gutter.update()

    def _apply_extra(self) -> None:
        sels = []
        cur = QTextEdit.ExtraSelection()
        cur.format.setBackground(QColor(T.alt))
        cur.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cur.cursor = self.textCursor()
        cur.cursor.clearSelection()
        sels.append(cur)
        doc = self.document()
        for d in self.diagnostics:
            block = doc.findBlockByNumber(d.line - 1)
            if not block.isValid():
                continue
            s = QTextEdit.ExtraSelection()
            s.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            s.format.setUnderlineColor(QColor(T.error if d.severity == "error" else T.warn))
            s.format.setToolTip(d.message)
            c = QTextCursor(block)
            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            s.cursor = c
            sels.append(s)
        self.setExtraSelections(sels)

    def goto_line(self, line: int) -> None:
        block = self.document().findBlockByNumber(max(0, line - 1))
        c = QTextCursor(block)
        self.setTextCursor(c)
        self.centerCursor()
        self.setFocus()

    def event(self, e) -> bool:
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QToolTip

        if e.type() == QEvent.Type.ToolTip:
            c = self.cursorForPosition(e.pos())
            line = c.blockNumber() + 1
            d = next((x for x in self.diagnostics if x.line == line), None)
            if d is not None:
                QToolTip.showText(e.globalPos(), d.message, self)
                return True
            QToolTip.hideText()
        return super().event(e)


class FindBar(QWidget):
    def __init__(self, editor: CodeEditor):
        super().__init__(editor)
        self.editor = editor
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        self.find = QLineEdit(placeholderText="Find")
        self.replace = QLineEdit(placeholderText="Replace")
        self.regex = QCheckBox(".*")
        self.case = QCheckBox("Aa")
        nxt = QPushButton("Next")
        rep = QPushButton("Replace")
        rep_all = QPushButton("All")
        for w in (self.find, self.replace, self.regex, self.case, nxt, rep, rep_all):
            lay.addWidget(w)
        self.find.returnPressed.connect(self.next)
        nxt.clicked.connect(self.next)
        rep.clicked.connect(self.replace_one)
        rep_all.clicked.connect(self.replace_all)
        self.hide()

    def open(self) -> None:
        sel = self.editor.textCursor().selectedText()
        if sel:
            self.find.setText(sel)
        self.show()
        self.find.setFocus()
        self.find.selectAll()

    def _flags(self) -> QTextDocument.FindFlag:
        f = QTextDocument.FindFlag(0)
        if self.case.isChecked():
            f |= QTextDocument.FindFlag.FindCaseSensitively
        return f

    def next(self) -> bool:
        from PySide6.QtCore import QRegularExpression

        pat = self.find.text()
        if not pat:
            return False
        target = QRegularExpression(pat) if self.regex.isChecked() else pat
        if not self.editor.find(target, self._flags()):
            c = self.editor.textCursor()
            c.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(c)
            return self.editor.find(target, self._flags())
        return True

    def replace_one(self) -> None:
        c = self.editor.textCursor()
        if c.hasSelection():
            c.insertText(self.replace.text())
        self.next()

    def replace_all(self) -> None:
        text = self.editor.toPlainText()
        pat = self.find.text()
        if not pat:
            return
        flags = 0 if self.case.isChecked() else re.IGNORECASE
        rx = re.compile(pat if self.regex.isChecked() else re.escape(pat), flags)
        new = rx.sub(self.replace.text().replace("\\", "\\\\"), text)
        if new != text:
            c = self.editor.textCursor()
            c.beginEditBlock()
            c.select(QTextCursor.SelectionType.Document)
            c.insertText(new)
            c.endEditBlock()
