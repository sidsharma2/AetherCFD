"""Region picker for the analysis panels: which region of a multi-region (CHT) case they read.

Hidden for a single-region case (``region()`` is then ""). Filled from CaseData.regions() and opened on
the fluid region; ``changed`` fires when the user picks another one.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget


class RegionPicker(QWidget):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Region"))
        self.combo = QComboBox()
        self.combo.setToolTip("Region of the multi-region (CHT) case to analyse: fields and cells are per region")
        lay.addWidget(self.combo, 1)
        self._filling = False
        self.combo.currentTextChanged.connect(lambda t: None if self._filling else self.changed.emit(t))
        self.setVisible(False)

    def region(self) -> str:
        return self.combo.currentText() if self.combo.count() else ""

    def reload(self, root: str | Path | None) -> list[str]:
        """Regions of the case at ``root`` (default: its fluid region). Never raises: an unreadable case has
        no regions here, and the panel says what is wrong when it reads the fields."""
        from aethercfd.results.casedata import CaseData

        regions, default = [], ""
        if root is not None:
            try:
                data = CaseData(root)
                regions = data.regions()
                default = data.default_region()
            except Exception:
                regions = []
        self._filling = True
        try:
            self.combo.clear()
            self.combo.addItems(regions)
            if default:
                self.combo.setCurrentText(default)
        finally:
            self._filling = False
        self.setVisible(bool(regions))
        return regions
