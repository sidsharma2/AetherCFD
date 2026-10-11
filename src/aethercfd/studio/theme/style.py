"""Apply the theme: Fusion + QPalette + token-generated QSS (+ QtAds overrides), fonts, icons, pyqtgraph."""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QIconEngine, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

from aethercfd.studio.theme.tokens import UI_COLORS, T, Tokens


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


def palette(t: Tokens | None = None) -> QPalette:
    t = t or T.tokens
    p = QPalette()
    c = QColor
    on_accent = "#ffffff" if t.dark else t.text  # text on the selection colour (accent_soft)
    roles = {
        QPalette.ColorRole.Window: t.panel, QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.base, QPalette.ColorRole.AlternateBase: t.alt,
        QPalette.ColorRole.ToolTipBase: t.raised, QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.Text: t.text, QPalette.ColorRole.Button: t.alt,
        QPalette.ColorRole.ButtonText: t.text, QPalette.ColorRole.BrightText: "#ffffff" if t.dark else "#000000",
        QPalette.ColorRole.Highlight: t.accent_soft, QPalette.ColorRole.HighlightedText: on_accent,
        QPalette.ColorRole.Link: t.accent, QPalette.ColorRole.LinkVisited: t.accent,
        QPalette.ColorRole.PlaceholderText: t.text_faint,
        QPalette.ColorRole.Mid: t.border, QPalette.ColorRole.Midlight: t.border_soft,
        QPalette.ColorRole.Dark: t.base if t.dark else t.border, QPalette.ColorRole.Light: t.raised,
        QPalette.ColorRole.Shadow: "#000000" if t.dark else t.border,
    }
    if hasattr(QPalette.ColorRole, "Accent"):  # Qt >= 6.6
        roles[QPalette.ColorRole.Accent] = t.accent
    for role, col in roles.items():
        p.setColor(role, c(col))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(t.text_faint))
    return p


def qss(t: Tokens | None = None) -> str:
    t = t or T.tokens
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


# ---------------------------------------------------------------------------- icons
def _token_for(color: str | None) -> str | None:
    """The interface token a colour came from (``T.warn`` -> "warn"), so the icon follows the theme."""
    if not color:
        return None
    c = color.lower()
    return next((k for k in UI_COLORS if getattr(T, k).lower() == c), None)


_ICONS: dict[tuple[str, str, str], QIcon] = {}  # one qtawesome icon per glyph + colours


class _ThemedIconEngine(QIconEngine):
    """A qtawesome glyph whose colour is a theme token, resolved each time Qt draws it: icons made
    before a theme switch repaint in the new colours. Colours that are not tokens stay as given."""

    def __init__(self, name: str, color: str, color_active: str):
        super().__init__()
        self.name, self.color, self.color_active = name, color, color_active

    def _resolve(self, c: str) -> str:
        return getattr(T, c[1:]) if c.startswith("@") else c

    def _icon(self) -> QIcon:
        key = (self.name, self._resolve(self.color), self._resolve(self.color_active))
        ic = _ICONS.get(key)
        if ic is None:
            ic = _ICONS[key] = qta.icon(self.name, color=key[1], color_active=key[2])
        return ic

    def paint(self, painter, rect: QRect, mode, state) -> None:
        if rect.isEmpty():
            return
        self._icon().paint(painter, rect, mode=mode, state=state)

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        if size.isEmpty():
            return QPixmap()
        return self._icon().pixmap(size, mode, state)

    def clone(self) -> QIconEngine:
        return _ThemedIconEngine(self.name, self.color, self.color_active)


def icon(name: str, color: str | None = None) -> QIcon:
    try:
        qta.icon(name)  # unknown glyph names raise here, as before
    except Exception:
        return QIcon()
    tok = _token_for(color) if color else "text_dim"
    return QIcon(_ThemedIconEngine(name, f"@{tok}" if tok else str(color), "@text"))


def _guard_empty_icon_pixmaps() -> None:
    """Qt asks icon engines for 0x0 pixmaps (e.g. tabs of hidden docks); qtawesome then paints into a null
    pixmap and Qt logs ~10 QPainter warnings each time (thousands per session). Return the empty pixmap."""
    from qtawesome.iconic_font import CharIconEngine

    if getattr(CharIconEngine, "_aether_guarded", False):
        return
    original = CharIconEngine.pixmap

    def pixmap(self, size, mode, state):
        if size.isEmpty():
            return QPixmap()
        return original(self, size, mode, state)

    CharIconEngine.pixmap = pixmap
    CharIconEngine._aether_guarded = True


def plot_defaults() -> None:
    """pyqtgraph's defaults for plots created from now on (existing ones are re-tinted by the manager)."""
    import pyqtgraph as pg

    pg.setConfigOptions(antialias=True, background=T.base, foreground=T.text_dim)


def apply_tokens(app: QApplication) -> None:
    """Palette, style sheet and plot defaults of the active tokens."""
    app.setPalette(palette())
    app.setStyleSheet(qss())
    plot_defaults()


def apply(app: QApplication) -> None:
    _guard_empty_icon_pixmaps()
    app.setStyle("Fusion")
    app.setFont(ui_font())
    apply_tokens(app)
