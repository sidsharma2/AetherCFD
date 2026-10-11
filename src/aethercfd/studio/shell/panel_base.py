"""Panels: everything the user can add, dock, tab, float to another monitor, or close.

A panel type is described by a :class:`PanelSpec` and created lazily (the first time its dock is
shown). Panels never import each other; they talk through :class:`PanelContext` services.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from aethercfd.studio.theme.tokens import T

if TYPE_CHECKING:
    from aethercfd.studio.app.services import AppServices


class Area(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    BOTTOM = "bottom"
    TOP = "top"
    CENTER = "center"
    FLOATING = "floating"
    AUTOHIDE_LEFT = "autohide-left"
    AUTOHIDE_RIGHT = "autohide-right"
    AUTOHIDE_BOTTOM = "autohide-bottom"


@dataclass(frozen=True)
class PanelSpec:
    type_id: str  # "aether.viewport"; plugins use their own prefix
    title: str
    icon: str  # qtawesome name, e.g. "mdi6.cube-outline"
    category: str  # Case | Edit | Visualization | Run | Tools | <plugin>
    factory: Callable[[PanelContext], Panel]
    multi_instance: bool = False
    default_area: Area = Area.RIGHT
    tab_group: str | None = None
    close_policy: str = "hide"  # "hide" keeps state; "destroy" disposes (multi-instance panels)
    requires: frozenset[str] = frozenset()  # {"case"}, {"vtk"}, {"webengine"}
    shortcut: str | None = None
    fallback_area: Area = Area.RIGHT  # where it docks back when its monitor disappears
    description: str = ""


@dataclass
class PanelContext:
    services: AppServices
    instance_id: str  # "#main" for singletons, "#1", "#2" for multi-instance panels
    args: dict[str, Any] = field(default_factory=dict)  # e.g. {"path": ...} for an editor


class Panel(QWidget):
    """Base class. Override what you need; everything has a sensible default."""

    title_changed = Signal(str)
    dirty_changed = Signal(bool)
    STATE_VERSION = 1

    def __init__(self, ctx: PanelContext):
        super().__init__()
        self.ctx = ctx
        from aethercfd.studio.theme.manager import THEME

        THEME.changed.connect(self._theme_changed)  # bound slot: disconnected when the panel is destroyed

    # state (JSON-serialisable, versioned per panel type)
    def save_state(self) -> dict:
        return {}

    def restore_state(self, state: dict) -> None:
        pass

    # lifecycle
    def on_shown(self) -> None:
        pass

    def on_hidden(self) -> None:
        pass

    def on_case_changed(self) -> None:
        pass

    def can_close(self) -> bool:
        return True

    def on_theme_changed(self) -> None:
        """The theme switched (T now reads the new palette). Style sheets, rich-text labels, read-only
        text, item colours and plots were already re-tinted; redraw what the panel paints itself."""

    def _theme_changed(self, _name: str) -> None:
        try:
            self.on_theme_changed()
        except RuntimeError as exc:  # a child widget already deleted: nothing left to redraw
            if "deleted" not in str(exc):
                raise

    def dispose(self) -> None:
        """Release GL contexts, PTYs, worker requests before the widget is destroyed."""

    def contributed_actions(self) -> list:
        return []


class PlaceholderPanel(Panel):
    """Stands in for a panel type named in a saved layout but not available (plugin missing)."""

    def __init__(self, ctx: PanelContext, type_id: str):
        super().__init__(ctx)
        lay = QVBoxLayout(self)
        msg = QLabel(f"Panel '{type_id}' is not available (plugin not installed?).\n"
                     "Its place in the layout is kept; close it to discard.")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {T.text_dim}; padding: 16px;")
        lay.addWidget(msg)
        lay.addStretch(1)
