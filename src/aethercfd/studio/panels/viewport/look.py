"""Viewport looks: Studio (AetherCFD's own post-processing look) and Classic (ParaView-style).

Studio: radial vignette background, physically based shading with ambient occlusion, colours
smoothed from the cell values (always labelled, never silent), crisp outline edges, a slim vertical
colour bar with units and SI prefixes in Segoe UI, a one-line title, a clickable orientation gizmo,
and per-field colour maps. Classic: true cell colours, flat shading, ParaView-like defaults. Both
draw the same data on the same finite-volume mesh; only the presentation differs.
"""

from __future__ import annotations

import re
from pathlib import Path

import vtk

from aethercfd.studio.theme.tokens import T

LOOKS = ("studio", "classic")
_FONT = Path(r"C:\Windows\Fonts\segoeui.ttf")
_FONT_SEMI = Path(r"C:\Windows\Fonts\seguisb.ttf")
# radial vignette: lighter at the centre, deep at the edges (reads as depth, keeps colours vivid)
STUDIO_CENTRE = (0.155, 0.185, 0.255)
STUDIO_EDGE = (0.030, 0.036, 0.058)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def classic_bg() -> tuple[float, float, float]:
    """The Classic look follows the app theme (dark grey / light grey); Studio stays dark in both."""
    return _rgb(T.viewport_bg)


def classic_text() -> tuple[float, float, float]:
    return _rgb(T.viewport_text)

# per-field colour maps for the Studio look (perceptual; diverging only where zero is meaningful)
# none of them starts at black: on the dark Studio background a black low end makes the domain vanish
_AUTO = [(r"^(T|Tsolid|T_.*|h|he|e|TPinn)$", "plasma"), (r"^(U|UMean|U_.*|UPinn)$", "turbo"),
         (r"^(p|p_rgh|pMean|prgh)$", "viridis"), (r"^(k|epsilon|omega|nut|alphat|mut|nuTilda)$", "viridis"),
         (r"^(alpha\..*|alpha.water|alpha1)$", "cividis"), (r"(Err|Corr|Diff)$", "coolwarm"),
         (r"^(rho|psi)$", "viridis"), (r"^(Y.*|CH4|O2|CO2|H2O|CO|OH|N2|Z|Zvar)$", "viridis")]


def auto_cmap(field: str | None) -> str:
    base = (field or "").split("_mag")[0]
    for pat, cm in _AUTO:
        if re.search(pat, base):
            return cm
    return "viridis"


def _text(tp: vtk.vtkTextProperty, size: int, color=(0.86, 0.88, 0.93), semibold: bool = False) -> None:
    font = _FONT_SEMI if semibold and _FONT_SEMI.exists() else _FONT
    if font.exists():
        tp.SetFontFamily(vtk.VTK_FONT_FILE)
        tp.SetFontFile(str(font))
    tp.SetFontSize(size)
    tp.SetColor(*color)
    tp.ShadowOff()
    tp.ItalicOff()
    tp.BoldOff()


def style_bar(bar: vtk.vtkScalarBarActor, look: str) -> None:
    if look == "studio":
        bar.SetOrientationToVertical()
        bar.SetPosition(0.875, 0.18)
        bar.SetWidth(0.10)
        bar.SetHeight(0.64)
        bar.SetBarRatio(0.18)
        bar.SetNumberOfLabels(5)
        bar.SetLabelFormat("%-#6.3g")
        bar.SetTextPositionToSucceedScalarBar()  # labels to the right of the bar, never over it
        bar.SetVerticalTitleSeparation(6)
        bar.DrawFrameOff()
        bar.DrawBackgroundOff()
        bar.UnconstrainedFontSizeOn()
        _text(bar.GetLabelTextProperty(), 12, (0.80, 0.83, 0.90))
        _text(bar.GetTitleTextProperty(), 13, (0.93, 0.95, 1.0), semibold=True)
        bar.GetLabelTextProperty().SetJustificationToLeft()
    else:
        bar.SetOrientationToHorizontal()
        bar.SetPosition(0.30, 0.02)
        bar.SetWidth(0.40)
        bar.SetHeight(0.09)
        bar.SetBarRatio(0.375)
        bar.SetNumberOfLabels(5)
        bar.SetLabelFormat("%-#6.4g")
        bar.SetTextPositionToSucceedScalarBar()
        bar.UnconstrainedFontSizeOn()
        for tp, size in ((bar.GetLabelTextProperty(), 11), (bar.GetTitleTextProperty(), 12)):
            tp.SetFontFamilyToArial()
            tp.SetColor(*classic_text())
            tp.SetFontSize(size)
            tp.ItalicOff()
            tp.BoldOff()
            tp.ShadowOff()


def make_hud() -> vtk.vtkTextActor:
    t = vtk.vtkTextActor()
    t.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
    t.SetPosition(0.018, 0.955)
    _text(t.GetTextProperty(), 13, (0.88, 0.90, 0.96), semibold=True)
    t.GetTextProperty().SetVerticalJustificationToTop()
    t.SetVisibility(False)
    return t


def make_edges_actor() -> tuple[vtk.vtkActor, vtk.vtkPolyDataMapper]:
    m = vtk.vtkPolyDataMapper()
    m.ScalarVisibilityOff()
    a = vtk.vtkActor()
    a.SetMapper(m)
    p = a.GetProperty()
    p.SetColor(0.90, 0.93, 1.0)
    p.SetOpacity(0.30)
    p.SetLineWidth(1.3)
    p.SetLighting(False)
    a.SetPickable(False)
    a.SetVisibility(False)
    return a, m


def apply_background(renderer: vtk.vtkRenderer, look: str) -> None:
    if look == "studio":
        renderer.GradientBackgroundOn()
        if hasattr(renderer, "SetGradientMode"):
            renderer.SetGradientMode(vtk.vtkViewport.GradientModes.VTK_GRADIENT_RADIAL_VIEWPORT_FARTHEST_CORNER)
            renderer.SetBackground(*STUDIO_CENTRE)
            renderer.SetBackground2(*STUDIO_EDGE)
        else:  # vertical gradient: VTK's Background is the bottom colour
            renderer.SetBackground(*STUDIO_EDGE)
            renderer.SetBackground2(*STUDIO_CENTRE)
    else:
        renderer.GradientBackgroundOff()
        renderer.SetBackground(*classic_bg())


def surface_material(prop: vtk.vtkProperty, look: str, *, smooth: bool, flat_view: bool = False) -> None:
    """Studio 3D: smooth Phong with a satin highlight, lit softly enough that colours stay true to the
    colour bar; Studio 2D (a flat view gains nothing from shading): unlit, exact colour-bar colours;
    Classic: flat ParaView-style shading."""
    prop.SetLighting(True)
    if look == "studio" and flat_view:
        prop.SetInterpolationToFlat()
        prop.SetLighting(False)  # colours exactly as on the colour bar
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        prop.SetSpecular(0.0)
    elif look == "studio":
        if smooth:
            prop.SetInterpolationToPhong()
        else:
            prop.SetInterpolationToFlat()
        prop.SetAmbient(0.30)
        prop.SetDiffuse(0.76)
        prop.SetSpecular(0.20)
        prop.SetSpecularPower(32)
        prop.SetSpecularColor(1.0, 1.0, 1.0)
    else:
        prop.SetInterpolationToFlat()
        prop.SetAmbient(0.0)
        prop.SetDiffuse(1.0)
        prop.SetSpecular(0.0)


def label_format(lo: float, hi: float) -> str:
    """printf format with enough significant digits that ticks over a narrow range still differ
    (299.95 .. 300.05 needs 5 digits, not '300.0' five times)."""
    import math

    span, mag = abs(hi - lo), max(abs(lo), abs(hi))
    if span <= 0 or mag <= 0 or not math.isfinite(span / mag):
        return "%.4g"
    digits = min(8, max(3, math.ceil(math.log10(mag / span)) + 2))
    digits = max(digits, math.floor(math.log10(mag)) + 1)  # 2000 stays "2000", never "2e+03"
    return f"%.{min(digits, 9)}g"


def nice_ticks(lo: float, hi: float, n: int = 6) -> list[float]:
    """Round tick values inside [lo, hi] (matplotlib's locator): 0, 10, 20, ... not 2.53e-05, 12.49."""
    from matplotlib.ticker import MaxNLocator

    if not (hi > lo):
        return [lo]
    span = hi - lo
    if abs(lo) < 1e-3 * span:  # 2.5e-05 .. 50 is "0 .. 50" to a reader
        lo = 0.0
    if abs(hi) < 1e-3 * span:
        hi = 0.0
    ticks = [float(t) for t in MaxNLocator(nbins=n, steps=[1, 2, 2.5, 5, 10]).tick_values(lo, hi)
             if lo - 1e-9 * (hi - lo) <= t <= hi + 1e-9 * (hi - lo)]
    span = hi - lo
    return [0.0 if abs(t) < 1e-9 * span else t for t in ticks]
