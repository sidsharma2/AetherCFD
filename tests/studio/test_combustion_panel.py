"""Combustion workbench panel: Sandia D mixture, regime, advisor (Cantera, no WSL)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("cantera")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.combustion.panel import CombustionPanel
from aethercfd.studio.shell.panel_base import PanelContext


@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    sv = AppServices()
    p = CombustionPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    yield p
    sv.pool.waitForDone(60000)
    sv.bridge.close()


def test_sandia_d_mixture_regime_and_advice(panel, qtbot):
    panel.preset.setCurrentText("Sandia flame D (CH4/air 1:3 vol.)")
    panel.compute_mixture()
    qtbot.waitUntil(lambda: "mixture" in panel.report, timeout=60000)
    assert panel.report["mixture"]["stoich"]["Z_st"] == pytest.approx(0.351, abs=0.005)
    assert "Z<sub>st</sub>" in panel.mix_out.toHtml() or "Z" in panel.mix_out.toPlainText()
    panel.reg_SL.setValue(0.38)
    panel.reg_dL.setValue(0.45)
    panel.compute_regime()
    assert panel.report["regime"]["regime"] in ("corrugated flamelets", "thin reaction zones")
    for k in ("CO", "NOx"):
        panel.adv_want[k].setChecked(True)
    panel.adv_jet.setChecked(True)
    panel.recommend()
    first = panel.report["advice"][0]
    assert first["model"].startswith("EDC") and any("C1 = 1.6" in t for t in first["turbulence"])
    assert "constant/combustionProperties" in first["files"]
