"""Design tokens: one calm, dense, dark scientific theme. Colors are used for meaning only (one
accent for focus/selection; status colors only for status). Sizes are in points so they scale per
monitor DPI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    # neutrals
    base: str = "#1e1f22"
    panel: str = "#232428"
    alt: str = "#2a2b30"
    raised: str = "#2f3036"
    border: str = "#3a3b41"
    border_soft: str = "#303137"
    text: str = "#d6d6d6"
    text_dim: str = "#9a9ca3"
    text_faint: str = "#6d6f76"
    # single accent
    accent: str = "#4a8fe7"
    accent_soft: str = "#2b4a72"
    # status
    ok: str = "#5fae6e"
    warn: str = "#d8a53c"
    error: str = "#e0605a"
    info: str = "#5aa7d6"
    # viewport
    viewport_bg: str = "#1b1c1f"
    # type
    ui_font: str = "Segoe UI Variable Text"
    ui_font_fallback: str = "Segoe UI"
    ui_pt: float = 9.0
    mono_font: str = "Cascadia Mono"
    mono_fallback: str = "Consolas"
    mono_pt: float = 10.0
    # density
    row_px: int = 22
    tab_px: int = 26
    radius: int = 4


T = Tokens()
