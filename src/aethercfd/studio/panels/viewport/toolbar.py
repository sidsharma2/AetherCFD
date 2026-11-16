"""The viewport's toolbar: groups of controls that wrap onto more rows instead of being squeezed.

A plain QHBoxLayout squeezes every control below its size hint when the viewport is narrow (side panels open),
so labels were cut mid-word ("Stud", "Surfa", "Lock ra"). Here each group of related controls keeps its own
size hint and never shrinks; the groups flow left to right and wrap onto a new row when the next one does not
fit. The last group (the actions) sits at the right edge of its row, as before. When dropping the text of
the menu buttons (Sections, Flow paths, Views) saves a row, they become icon-only; their tooltips name them,
and their menus and keyboard access are unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLayout, QSizePolicy, QToolButton, QWidget

GAP = 10  # between groups on a row
ROW_GAP = 2


class ToolGroup(QWidget):
    """Related controls on one row; never narrower than its contents."""

    def __init__(self, widgets, parent=None, spacing: int = 4):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(spacing)
        for w in widgets:
            w.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            lay.addWidget(w)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self) -> QSize:  # Qt override: a group is never squeezed
        return self.sizeHint()


class FlowLayout(QLayout):
    """Lays out widgets (here: ToolGroups) in rows, wrapping when the next one does not fit. The last item is
    aligned to the right edge of its row. ``before_layout(width)`` (if set) runs first, so the owner can switch
    to a compact form for that width."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self.before_layout = None
        self.setContentsMargins(6, 3, 6, 3)

    # QLayout interface
    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        m = self.contentsMargins()
        shown = [it for it in self._items if not it.isEmpty()]
        w = sum(it.sizeHint().width() for it in shown) + GAP * max(0, len(shown) - 1)
        h = max((it.sizeHint().height() for it in shown), default=0)
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self) -> QSize:
        m = self.contentsMargins()
        w = max((it.minimumSize().width() for it in self._items if not it.isEmpty()), default=0)
        return QSize(w + m.left() + m.right(), self.heightForWidth(w + m.left() + m.right()))

    # the flow
    def rows(self, width: int, widths: list[int] | None = None) -> list[list[int]]:
        """Item indices per row for an available ``width`` (contents), from the items' widths (or ``widths``)."""
        shown = [i for i, it in enumerate(self._items) if not it.isEmpty()]
        ws = widths if widths is not None else [it.sizeHint().width() for it in self._items]
        rows: list[list[int]] = []
        x = 0
        for i in shown:
            w = ws[i]
            if rows and rows[-1] and x + GAP + w <= width:
                rows[-1].append(i)
                x += GAP + w
            else:
                rows.append([i])
                x = w
        return rows

    def _do_layout(self, rect: QRect, *, apply: bool) -> int:
        m = self.contentsMargins()
        width = max(0, rect.width() - m.left() - m.right())
        if self.before_layout is not None:
            self.before_layout(width)
        y = rect.y() + m.top()
        rows = self.rows(width)
        for r, row in enumerate(rows):
            h = max(self._items[i].sizeHint().height() for i in row)
            x = rect.x() + m.left()
            last_row = r == len(rows) - 1
            for k, i in enumerate(row):
                it = self._items[i]
                sh = it.sizeHint()
                px = x
                if last_row and k == len(row) - 1 and i == len(self._items) - 1:
                    px = max(x, rect.x() + m.left() + width - sh.width())  # the actions: right edge
                if apply:
                    it.setGeometry(QRect(QPoint(px, y + (h - sh.height()) // 2), sh))
                x = px + sh.width() + GAP
            y += h + (ROW_GAP if r < len(rows) - 1 else 0)
        return y - rect.y() + m.bottom()


class ViewportToolbar(QWidget):
    """The toolbar strip: ``groups`` flow onto rows; ``compactable`` text+icon buttons go icon-only when that
    saves a row (a tooltip always names them)."""

    def __init__(self, groups: list[QWidget], compactable: list[QToolButton], parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self)
        for g in groups:
            self.flow.addWidget(g)
        self.compactable = list(compactable)
        self.compact = False
        self._hints: dict[int, tuple[int, int]] | None = None  # id(button) -> (full width, compact width)
        self.flow.before_layout = self._choose
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def _measure(self) -> dict[int, tuple[int, int]]:
        if self._hints is None:
            hints = {}
            for b in self.compactable:
                style = b.toolButtonStyle()
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                full = b.sizeHint().width()
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
                small = b.sizeHint().width()
                b.setToolButtonStyle(style)
                hints[id(b)] = (full, small)
            self._hints = hints
        return self._hints

    def _widths(self, compact: bool) -> list[int]:
        hints = self._measure()
        out = []
        for i in range(self.flow.count()):
            w = self.flow.itemAt(i).widget()
            width = w.sizeHint().width() if w is not None else 0
            for b in self.compactable:
                if w is not None and w.isAncestorOf(b) and b.isVisibleTo(w):
                    full, small = hints[id(b)]
                    now = b.sizeHint().width()
                    width += (small if compact else full) - now
            out.append(width)
        return out

    def _choose(self, width: int) -> None:
        if width <= 0 or not self.compactable:
            return
        full = len(self.flow.rows(width, self._widths(False)))
        small = len(self.flow.rows(width, self._widths(True)))
        self.set_compact(small < full)

    def set_compact(self, on: bool) -> None:
        if on == self.compact:
            return
        self.compact = on
        style = Qt.ToolButtonStyle.ToolButtonIconOnly if on else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        for b in self.compactable:
            b.setToolButtonStyle(style)

    def changeEvent(self, ev) -> None:  # Qt override: a new font or style changes every size hint
        from PySide6.QtCore import QEvent

        if ev.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._hints = None
        super().changeEvent(ev)

    def row_count(self) -> int:
        """Rows the toolbar has at its current width (tests, and to say where a control went)."""
        m = self.flow.contentsMargins()
        return len(self.flow.rows(max(0, self.width() - m.left() - m.right())))
