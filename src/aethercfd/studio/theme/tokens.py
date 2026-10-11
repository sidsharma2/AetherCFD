"""Design tokens: a calm, dense scientific theme in a dark and a light palette. Colors are used for
meaning only (one accent for focus/selection; status colors only for status). Sizes are in points so
they scale per monitor DPI.

``T`` always reads the ACTIVE palette (``T.text`` is the current text colour), so code that builds a
colour when it runs follows the theme by itself. The theme manager swaps the palette at run time
(``activate``) and re-tints what was already drawn (theme/manager.py)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    name: str = "dark"
    dark: bool = True
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
    # viewport (Classic look; the Studio look keeps its own dark vignette in both themes)
    viewport_bg: str = "#1b1c1f"
    viewport_text: str = "#d6d6d6"
    # diff rows
    diff_delete: str = "#5a2a2e"
    diff_insert: str = "#24452f"
    diff_replace: str = "#4d4122"
    # OpenFOAM dictionary syntax (VS Code Dark+)
    syn_comment: str = "#6a9955"
    syn_string: str = "#ce9178"
    syn_number: str = "#b5cea8"
    syn_directive: str = "#c586c0"
    syn_macro: str = "#4ec9b0"
    syn_key: str = "#9cdcfe"
    syn_keyword: str = "#569cd6"
    syn_dims: str = "#808080"
    syn_code: str = "#d7ba7d"
    syn_type: str = "#4ec9b0"
    # plot series (residual curves etc.), in order
    series: tuple[str, ...] = ("#4a8fe7", "#e0605a", "#5fae6e", "#d8a53c", "#b07fd8", "#4ec9b0", "#e8875a",
                               "#9cdcfe", "#c586c0", "#d7ba7d", "#7f8c9a", "#f2cc60", "#56b6c2", "#98c379",
                               "#e06c75", "#61afef")
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


DARK = Tokens()

LIGHT = Tokens(
    name="light", dark=False,
    base="#fbfbfc", panel="#f2f3f5", alt="#e7e9ed", raised="#fefeff", border="#c6cad1", border_soft="#dadde2",
    text="#1f2328", text_dim="#555b65", text_faint="#868b94",
    accent="#2f74d0", accent_soft="#cfe0f7",
    ok="#2e7d43", warn="#a86a00", error="#c62f2f", info="#1f76b0",
    viewport_bg="#d6d9de", viewport_text="#2b2e33",
    diff_delete="#fbe1e2", diff_insert="#dff3e4", diff_replace="#f8edcf",
    syn_comment="#3f8a3a", syn_string="#a31515", syn_number="#098658", syn_directive="#af00db",
    syn_macro="#267f99", syn_key="#0f4f9e", syn_keyword="#0000c8", syn_dims="#7a7f88", syn_code="#795e26",
    syn_type="#267f99",
    series=("#2f74d0", "#c62f2f", "#2e7d43", "#b07800", "#7d4fc0", "#13897a", "#c9581f", "#3c8fc2", "#a23d9a",
            "#8a6d1e", "#5f6b7a", "#c49a00", "#16838f", "#5a8f2e", "#b8324a", "#2364aa"),
)

THEMES = {"dark": DARK, "light": LIGHT}

# The interface colours that stylesheets, rich text and item colours are built from. They are unique
# within each palette, so a colour drawn with one palette maps to exactly one colour of the other
# (that is how widgets built before a theme switch are re-tinted).
UI_COLORS = ("base", "panel", "alt", "raised", "border", "border_soft", "text", "text_dim", "text_faint",
             "accent", "accent_soft", "ok", "warn", "error", "info", "viewport_bg", "diff_delete", "diff_insert",
             "diff_replace")


def color_map(old: Tokens, new: Tokens) -> dict[str, str]:
    """{old '#rrggbb' (lower case): new '#rrggbb'} for every interface colour that differs."""
    return {getattr(old, k).lower(): getattr(new, k) for k in UI_COLORS if getattr(old, k) != getattr(new, k)}


class _Active:
    """``T``: attribute access reads the active palette."""

    __slots__ = ("_t",)

    def __init__(self, t: Tokens):
        object.__setattr__(self, "_t", t)

    def __getattr__(self, key: str):
        return getattr(object.__getattribute__(self, "_t"), key)

    def __setattr__(self, key, value):
        raise AttributeError("theme tokens are read-only; switch themes with the theme manager")

    def __repr__(self) -> str:
        return f"T<{self._t.name}>"

    @property
    def tokens(self) -> Tokens:
        return object.__getattribute__(self, "_t")


T = _Active(DARK)


def activate(t: Tokens) -> Tokens:
    """Make ``t`` the active palette; returns the previous one."""
    old = T.tokens
    object.__setattr__(T, "_t", t)
    return old


def current() -> Tokens:
    return T.tokens


__all__ = ["DARK", "LIGHT", "THEMES", "UI_COLORS", "T", "Tokens", "activate", "color_map", "current"]
