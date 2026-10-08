"""Numeric inputs that do not silently round: decimals and range are set BEFORE the value (PySide applies
constructor keywords in order, so QDoubleSpinBox(value=0.125, decimals=6) stores 0.13)."""

from __future__ import annotations

from PySide6.QtWidgets import QDoubleSpinBox


def dspin(value: float, lo: float, hi: float, decimals: int = 4, *, suffix: str = "", step: float | None = None,
          special: str | None = None, tip: str = "") -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setDecimals(decimals)
    w.setRange(lo, hi)
    if step is not None:
        w.setSingleStep(step)
    if suffix:
        w.setSuffix(suffix)
    if special:
        w.setSpecialValueText(special)
    if tip:
        w.setToolTip(tip)
    w.setValue(value)
    return w
