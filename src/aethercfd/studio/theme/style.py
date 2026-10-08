"""Apply the theme: Fusion + dark QPalette + token-generated QSS (+ QtAds overrides), fonts, icons."""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from aethercfd.studio.theme.tokens import T, Tokens


def _family(preferred: str, fallback: str) -> str:
    fams = set(QFontDatabase.families())
    return preferred if preferred in fams else fallback


def ui_font() -> QFont:
    f = QFont(_family(T.ui_font, T.ui_font_fallback))
    f.setPointSizeF(T.ui_pt)
    return f


def mono_font(pt: float | None = None) -> QFont:
    f = QFont(_family(T.mono_font, T.mono_fallback))
    f.setPointSizeF(pt or T.mono_pt)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def palette(t: Tokens = T) -> QPalette:
    p = QPalette()
    c = QColor
    roles = {
        QPalette.ColorRole.Window: t.panel, QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.base, QPalette.ColorRole.AlternateBase: t.alt,
        QPalette.ColorRole.ToolTipBase: t.raised, QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.Text: t.text, QPalette.ColorRole.Button: t.alt,
        QPalette.ColorRole.ButtonText: t.text, QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: t.accent_soft, QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: t.accent, QPalette.ColorRole.PlaceholderText: t.text_faint,
        QPalette.ColorRole.Mid: t.border, QPalette.ColorRole.Dark: t.base, QPalette.ColorRole.Light: t.raised,
    }
    for role, col in roles.items():
        p.setColor(role, c(col))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(t.text_faint))
    return p


def qss(t: Tokens = T) -> str:
    return f"""
QWidget {{ color: {t.text}; }}
QMainWindow, QDialog {{ background: {t.panel}; }}
QToolTip {{ background: {t.raised}; color: {t.text}; border: 1px solid {t.border}; padding: 4px; }}
QMenuBar {{ background: {t.panel}; border-bottom: 1px solid {t.border_soft}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {t.alt}; }}
QMenu {{ background: {t.raised}; border: 1px solid {t.border}; padding: 4px; }}
QMenu::item {{ padding: 4px 24px 4px 20px; border-radius: 3px; }}
QMenu::item:selected {{ background: {t.accent_soft}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 8px; }}
QToolBar {{ background: {t.panel}; border: none; spacing: 2px; padding: 2px; }}
QToolButton {{ border: 1px solid transparent; border-radius: {t.radius}px; padding: 3px; }}
QToolButton:hover {{ background: {t.alt}; border-color: {t.border_soft}; }}
QToolButton:checked {{ background: {t.accent_soft}; }}
QPushButton {{ background: {t.alt}; border: 1px solid {t.border}; border-radius: {t.radius}px; padding: 4px 12px; }}
QPushButton:hover {{ border-color: {t.text_faint}; }}
QPushButton:pressed {{ background: {t.raised}; }}
QPushButton:default {{ border-color: {t.accent}; }}
QPushButton:disabled {{ color: {t.text_faint}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t.base}; border: 1px solid {t.border}; border-radius: {t.radius}px; padding: 2px 6px;
    selection-background-color: {t.accent_soft}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {t.accent}; }}
QComboBox QAbstractItemView {{ background: {t.raised}; border: 1px solid {t.border}; }}
QTreeView, QListView, QTableView {{ background: {t.base}; alternate-background-color: {t.alt}; border: none;
    selection-background-color: {t.accent_soft}; outline: 0; }}
QTreeView::item, QListView::item {{ min-height: {t.row_px - 4}px; }}
QHeaderView::section {{ background: {t.panel}; color: {t.text_dim}; border: none;
    border-right: 1px solid {t.border_soft}; border-bottom: 1px solid {t.border_soft}; padding: 3px 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {t.border}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {t.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: {t.border_soft}; }}
QStatusBar {{ background: {t.panel}; border-top: 1px solid {t.border_soft}; color: {t.text_dim}; }}
QStatusBar::item {{ border: none; }}
QTabBar::tab {{ background: {t.panel}; color: {t.text_dim}; padding: 4px 12px; border: none; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QGroupBox {{ border: 1px solid {t.border_soft}; border-radius: {t.radius}px; margin-top: 10px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {t.text_dim}; }}
/* ---- Qt Advanced Docking System ---- */
ads--CDockContainerWidget {{ background: {t.base}; }}
ads--CDockAreaWidget {{ background: {t.panel}; border: 1px solid {t.border_soft}; }}
ads--CDockAreaTitleBar {{ background: {t.panel}; border-bottom: 1px solid {t.border_soft}; }}
ads--CDockWidgetTab {{ background: {t.panel}; border: none; padding: 0 6px; }}
ads--CDockWidgetTab[activeTab="true"] {{ background: {t.alt}; border-bottom: 2px solid {t.accent}; }}
ads--CDockWidgetTab QLabel {{ color: {t.text_dim}; }}
ads--CDockWidgetTab[activeTab="true"] QLabel {{ color: {t.text}; }}
ads--CDockWidgetTab[focused="true"] {{ border-bottom: 2px solid {t.accent}; }}
ads--CDockWidget {{ background: {t.panel}; }}
ads--CTitleBarButton {{ padding: 2px; }}
ads--CFloatingDockContainer {{ background: {t.panel}; }}
ads--CDockSplitter::handle {{ background: {t.border_soft}; }}
ads--CAutoHideTab {{ background: {t.panel}; color: {t.text_dim}; padding: 4px; }}
ads--CAutoHideTab[activeTab="true"] {{ color: {t.text}; background: {t.alt}; }}
ads--CAutoHideSideBar {{ background: {t.panel}; border: none; }}
"""


def icon(name: str, color: str | None = None) -> QIcon:
    try:
        return qta.icon(name, color=color or T.text_dim, color_active=T.text)
    except Exception:
        return QIcon()


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setFont(ui_font())
    app.setStyleSheet(qss())
