"""Light / Dark / System themes, switched live (no restart).

``THEME.set_mode("light" | "dark" | "system")`` activates a palette (tokens.T reads it), re-applies the
application palette, style sheet and plot defaults, then re-tints everything already on screen:

- widget style sheets and rich-text labels built from ``T.*`` (each old colour maps to exactly one new
  one, tokens.color_map);
- read-only text documents (logs, chat, console output) and extra selections (current line, diff rows);
- tree/list/table item colours;
- pyqtgraph plots (background, axes, titles, legends, curves).

Last it emits ``changed``: panels with their own drawing (syntax highlighter, xterm.js, VTK viewport,
HTML they re-render) refresh in ``Panel.on_theme_changed``. "system" follows Windows' app mode through
Qt's ``styleHints().colorScheme()`` and ``colorSchemeChanged``.
"""

from __future__ import annotations

import contextlib
import logging
import re

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPen, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QTableWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItemIterator,
)

from aethercfd.studio.theme import style, tokens

log = logging.getLogger(__name__)
MODES = ("system", "light", "dark")
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def remap_text(s: str, cmap: dict[str, str]) -> str:
    """Replace every '#rrggbb' of the old palette in ``s`` by its new-palette counterpart."""
    return _HEX.sub(lambda m: cmap.get(m.group(0).lower(), m.group(0)), s) if s and "#" in s else s


def _remap_brush(b: QBrush, cmap: dict[str, str]) -> QBrush | None:
    if b.style() == Qt.BrushStyle.NoBrush:
        return None
    new = cmap.get(b.color().name())
    if new is None:
        return None
    nb = QBrush(b)
    nb.setColor(QColor(new))
    return nb


def _remap_pen(p: QPen, cmap: dict[str, str]) -> QPen | None:
    new = cmap.get(p.color().name())
    if new is None:
        return None
    np_ = QPen(p)
    np_.setColor(QColor(new))
    return np_


def retint_document(doc, cmap: dict[str, str]) -> None:
    """Re-colour the character and block formats of a (read-only) QTextDocument in place."""
    edits = []
    block = doc.begin()
    while block.isValid():
        bf = block.blockFormat()
        bg = _remap_brush(bf.background(), cmap)
        if bg is not None:
            edits.append(("block", block.position(), 0, bg))
        for it in block:
            frag = it.fragment()
            if not frag.isValid():
                continue
            cf = frag.charFormat()
            nf, changed = QTextCharFormat(), False
            fg = _remap_brush(cf.foreground(), cmap)
            if fg is not None:
                nf.setForeground(fg)
                changed = True
            bg = _remap_brush(cf.background(), cmap)
            if bg is not None:
                nf.setBackground(bg)
                changed = True
            if changed:
                edits.append(("char", frag.position(), frag.length(), nf))
        block = block.next()
    if not edits:
        return
    undo = doc.isUndoRedoEnabled()
    doc.setUndoRedoEnabled(False)
    try:
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        for kind, pos, length, what in edits:
            cur.setPosition(pos)
            if kind == "block":
                f = cur.blockFormat()
                f.setBackground(what)
                cur.setBlockFormat(f)
            else:
                cur.setPosition(pos + length, QTextCursor.MoveMode.KeepAnchor)
                cur.mergeCharFormat(what)
        cur.endEditBlock()
    finally:
        doc.setUndoRedoEnabled(undo)


def _retint_extra_selections(ed, cmap: dict[str, str]) -> None:
    sels = ed.extraSelections()
    changed = False
    for s in sels:
        f = s.format
        for get, put in ((f.background, f.setBackground), (f.foreground, f.setForeground)):
            nb = _remap_brush(get(), cmap)
            if nb is not None:
                put(nb)
                changed = True
        u = cmap.get(f.underlineColor().name())
        if u is not None:
            f.setUnderlineColor(QColor(u))
            changed = True
        s.format = f
    if changed:
        ed.setExtraSelections(sels)


def _retint_item(item, cmap: dict[str, str], columns: int | None = None) -> None:
    """Item foreground/background brushes; ``columns`` for tree items (per-column brushes)."""
    for c in range(columns) if columns is not None else (None,):
        args = () if c is None else (c,)
        for get, put in ((item.foreground, item.setForeground), (item.background, item.setBackground)):
            nb = _remap_brush(get(*args), cmap)
            if nb is not None:
                put(*args, nb)


def _retint_items(w, cmap: dict[str, str]) -> None:
    if isinstance(w, QTreeWidget):
        it = QTreeWidgetItemIterator(w)
        while it.value():
            _retint_item(it.value(), cmap, w.columnCount())
            it += 1
    elif isinstance(w, QListWidget):
        for i in range(w.count()):
            _retint_item(w.item(i), cmap)
    elif isinstance(w, QTableWidget):
        for r in range(w.rowCount()):
            for c in range(w.columnCount()):
                item = w.item(r, c)
                if item is not None:
                    _retint_item(item, cmap)


def retint_plot(w, cmap: dict[str, str]) -> None:
    """A pyqtgraph PlotWidget: background, axes, title, legend and the curves drawn in token colours."""
    import pyqtgraph as pg

    t = tokens.T
    nb = _remap_brush(w.backgroundBrush(), cmap)
    if nb is not None:
        w.setBackground(nb.color())
    pi = w.getPlotItem() if hasattr(w, "getPlotItem") else None
    if pi is None:
        return
    for name in ("left", "bottom", "right", "top"):
        ax = pi.axes.get(name, {}).get("item")
        if ax is None:
            continue
        ax.setPen(pg.mkPen(t.text_dim))
        ax.setTextPen(pg.mkPen(t.text_dim))
    tl = pi.titleLabel
    col = tl.opts.get("color")
    if isinstance(col, str) and col.lower() in cmap:
        tl.setText(tl.text, color=cmap[col.lower()])
    leg = pi.legend
    if leg is not None:
        with contextlib.suppress(Exception):
            leg.setLabelTextColor(t.text_dim)
    for item in pi.items:
        if isinstance(item, pg.PlotDataItem):
            pen = item.opts.get("pen")
            if pen is not None:
                np_ = _remap_pen(pg.mkPen(pen), cmap)
                if np_ is not None:
                    item.setPen(np_)
            sb = item.opts.get("symbolBrush")
            if sb is not None:
                nb = _remap_brush(pg.mkBrush(sb), cmap)
                if nb is not None:
                    item.setSymbolBrush(nb)
        elif isinstance(item, pg.BarGraphItem):
            b = item.opts.get("brush")
            if b is not None:
                nb = _remap_brush(pg.mkBrush(b), cmap)
                if nb is not None:
                    item.setOpts(brush=nb)


def retint_widgets(app: QApplication, cmap: dict[str, str]) -> None:
    """Re-tint everything already built with the old palette (see the module docstring)."""
    if not cmap:
        return
    try:
        import pyqtgraph as pg

        plot_types: tuple = (pg.GraphicsView,)
    except Exception:
        plot_types = ()
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                new = remap_text(ss, cmap)
                if new != ss:
                    w.setStyleSheet(new)
            if isinstance(w, QLabel) and w.textFormat() != Qt.TextFormat.PlainText:
                txt = w.text()
                new = remap_text(txt, cmap)
                if new != txt:
                    w.setText(new)
            elif isinstance(w, QTextEdit | QPlainTextEdit):
                if w.isReadOnly():
                    retint_document(w.document(), cmap)
                _retint_extra_selections(w, cmap)
            elif isinstance(w, QTreeWidget | QListWidget | QTableWidget):
                _retint_items(w, cmap)
            elif plot_types and isinstance(w, plot_types):
                retint_plot(w, cmap)
        except RuntimeError:  # deleted while we walked
            continue
        except Exception:
            log.exception("re-tinting %s", type(w).__name__)


class ThemeManager(QObject):
    """The one place the theme changes. ``changed(name)`` carries the resolved theme: "light" | "dark"."""

    changed = Signal(str)
    mode_changed = Signal(str)  # the choice: "system" | "light" | "dark"

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.mode = "system"
        self._hooked = False
        self._applied = False

    @property
    def name(self) -> str:
        """The active theme: "light" or "dark"."""
        return tokens.current().name

    def system_theme(self) -> str:
        app = QGuiApplication.instance()
        if app is None:
            return "dark"
        return "light" if app.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"

    def resolve(self, mode: str | None = None) -> str:
        mode = mode or self.mode
        return mode if mode in ("light", "dark") else self.system_theme()

    def set_mode(self, mode: str) -> str:
        """'system' | 'light' | 'dark'. Returns the resolved theme."""
        mode = (mode or "system").lower()
        if mode not in MODES:
            raise ValueError(f"unknown theme {mode!r}: {', '.join(MODES)}")
        previous, self.mode = self.mode, mode
        self._hook()
        self._request_scheme(mode)
        self.apply(self.resolve())
        if mode != previous:
            self.mode_changed.emit(mode)
        return self.name

    def apply(self, name: str) -> None:
        new = tokens.THEMES[name]
        old = tokens.activate(new)
        app = QApplication.instance()
        if app is None:
            return
        if old is new and self._applied:
            app.setPalette(style.palette())  # Qt may have reset it on a system theme change
            return
        style.apply_tokens(app)
        self._applied = True
        if old is not new:
            retint_widgets(app, tokens.color_map(old, new))
            log.info("theme: %s (%s)", new.name, self.mode)
            self.changed.emit(new.name)

    # ------------------------------------------------------------------ system colour scheme
    def _hook(self) -> None:
        app = QGuiApplication.instance()
        if self._hooked or app is None:
            return
        app.styleHints().colorSchemeChanged.connect(self._on_scheme)
        self._hooked = True

    def _request_scheme(self, mode: str) -> None:
        """Tell Qt (and so Windows' title bars) which scheme the app shows; "system" lets it follow."""
        app = QGuiApplication.instance()
        if app is None:
            return
        hints = app.styleHints()
        with contextlib.suppress(AttributeError):  # Qt < 6.8 cannot request a scheme
            if mode == "system":
                hints.unsetColorScheme()
            else:
                hints.setColorScheme(Qt.ColorScheme.Light if mode == "light" else Qt.ColorScheme.Dark)

    def _on_scheme(self, _scheme=None) -> None:
        self.apply(self.resolve())


THEME = ThemeManager()
