"""Callout: a glowing outline plus a speech bubble over any widget, so the copilot (or a tutorial) can point
at what it is talking about. Click-through, fades out on its own."""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from aethercfd.studio.theme.tokens import T


class Callout(QWidget):
    _live: ClassVar[list[Callout]] = []  # at most one bubble at a time

    def __init__(self, target: QWidget, message: str, seconds: float = 6.0):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.message = message
        tl = target.mapToGlobal(QPoint(0, 0))
        self.target = QRect(tl, target.size())
        self.font_ = QFont(self.font())
        self.font_.setPointSizeF(10.5)
        fm = QFontMetrics(self.font_)
        width = min(420, max(180, fm.horizontalAdvance(message) + 28))
        text_rect = fm.boundingRect(QRect(0, 0, width - 28, 2000), Qt.TextFlag.TextWordWrap, message)
        self.bubble = QRect(0, 0, width, text_rect.height() + 22)
        screen = (target.screen() or QApplication.primaryScreen()).availableGeometry()
        pad = 6
        area = self.target.adjusted(-pad, -pad, pad, pad)
        bx = max(screen.left(), min(area.left() + 12, screen.right() - width))
        by = area.bottom() + 8 if area.bottom() + self.bubble.height() + 12 < screen.bottom() else \
            area.top() - self.bubble.height() - 8
        self.bubble.moveTo(bx, max(screen.top(), by))
        full = area.united(self.bubble).adjusted(-4, -4, 4, 4)
        self.setGeometry(full)
        self.area = area.translated(-full.topLeft())
        self.bubble = self.bubble.translated(-full.topLeft())
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(250)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        QTimer.singleShot(int(seconds * 1000), self.fade_out)

    def show_(self) -> Callout:
        for c in list(Callout._live):
            c.close()
        Callout._live = [self]
        self.show()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        return self

    def fade_out(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.close)
        self._fade.start()

    def closeEvent(self, ev) -> None:  # Qt override
        if self in Callout._live:
            Callout._live.remove(self)
        super().closeEvent(ev)

    def paintEvent(self, _ev) -> None:  # Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(T.accent)
        glow = QColor(accent)
        for w, a in ((9, 40), (5, 90), (2, 255)):
            glow.setAlpha(a)
            p.setPen(QPen(glow, w))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self.area, 6, 6)
        path = QPainterPath()
        path.addRoundedRect(self.bubble, 8, 8)
        p.fillPath(path, QColor(T.raised))
        p.setPen(QPen(accent, 1.5))
        p.drawPath(path)
        p.setPen(QColor(T.text))
        p.setFont(self.font_)
        p.drawText(self.bubble.adjusted(14, 11, -14, -11), Qt.TextFlag.TextWordWrap, self.message)


def point_at(target: QWidget, message: str, seconds: float = 6.0) -> Callout:
    return Callout(target, message, seconds).show_()
