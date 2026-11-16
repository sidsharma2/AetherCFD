"""A closed (deleted) panel never reacts to app-wide signals: lambdas connected to them outlived the panel and
raised 'Internal C++ object already deleted' into the error hook on the next case open or file change."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.shell.panel_base import PanelContext


@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    return AppServices()


def _panels():
    from aethercfd.studio.panels.analysis.panel import AnalysisPanel
    from aethercfd.studio.panels.case_tree.panel import CaseTreePanel
    from aethercfd.studio.panels.checks.panel import ChecksPanel
    from aethercfd.studio.panels.journal.panel import JournalPanel
    from aethercfd.studio.panels.measurements.panel import MeasurementsPanel
    from aethercfd.studio.panels.mesh_quality.panel import MeshQualityPanel
    from aethercfd.studio.panels.workflow.panel import WorkflowPanel

    return [AnalysisPanel, CaseTreePanel, ChecksPanel, JournalPanel, MeasurementsPanel, MeshQualityPanel,
            WorkflowPanel]


@pytest.mark.parametrize("cls", _panels(), ids=lambda c: c.__name__)
def test_a_deleted_panel_ignores_app_wide_signals(sv, qtbot, monkeypatch, cls):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: errors.append(f"{t.__name__}: {v}"))
    p = cls(PanelContext(sv, "#main"))
    p.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    sv.case_changed.emit(None)
    sv.files_changed.emit([])
    sv.bridge.event.emit("command.applied", {})
    qtbot.wait(10)
    assert errors == []
