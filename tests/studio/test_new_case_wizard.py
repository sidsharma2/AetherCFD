"""New Case wizard pages without WSL (forks/availability injected)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.dialogs.new_case import NewCaseWizard


@pytest.fixture
def wizard(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(NewCaseWizard, "_load_backend", lambda self: None)
    sv = AppServices()
    w = NewCaseWizard(sv)
    qtbot.addWidget(w)
    fam = SimpleNamespace(value="esi")
    w.forks = [SimpleNamespace(id="esi-v2306", label="OpenFOAM ESI v2306", family=fam)]
    w.available = {"esi-v2306": {"pitzDaily": True, "forwardStep": True, "motorBike": False}}
    w.p1.fill_forks()
    yield w
    sv.bridge.close()


def test_ranked_list_and_availability(wizard):
    p1 = wizard.p1
    assert wizard.template.id == "pitzDaily" and p1.isComplete()
    p1.boxes["regime"].setCurrentIndex(2)  # supersonic
    p1.boxes["time"].setCurrentIndex(1)  # transient
    assert wizard.template.id == "forwardStep"
    texts = [p1.list.item(i).text() for i in range(p1.list.count())]
    assert any("not in this OpenFOAM installation" in t for t in texts)


def test_where_page_validates_paths(wizard, tmp_path):
    p2 = wizard.p2
    p2.parent_dir.setText(str(tmp_path))
    p2.name.setText("pitz_1")
    assert p2.isComplete()
    p2.name.setText("with space")
    assert not p2.isComplete() and "space" in p2.check.text()
    (tmp_path / "taken").mkdir()
    p2.name.setText("taken")
    assert not p2.isComplete()
