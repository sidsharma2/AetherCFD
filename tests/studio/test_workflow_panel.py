"""The Workflow panel: stage list, gates, guided answers -> approved plan, mark done, agents."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.workflow.panel import WorkflowPanel
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_cfd_agents"


@pytest.fixture
def panel(tmp_path, monkeypatch, qtbot):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "pitz"
    shutil.copytree(FIX / "esi_pitzDaily", case)
    sv = AppServices()
    sv.open_case(case, create_project=True)
    p = WorkflowPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    p.refresh_now()
    approvals = []
    monkeypatch.setattr(p, "_approve", lambda plan, who="": approvals.append(plan) or True)
    p.approvals = approvals
    return p


def test_stage_list_and_page(panel):
    assert panel.tree.topLevelItemCount() == 12
    assert panel.current == "domain"  # the first unfinished stage (geometry is not needed)
    assert "stages done" in panel.progress.text()
    panel.select_stage("mesh")
    assert "No mesh yet" in panel.next.text()
    assert not panel.mark_btn.isEnabled()
    titles = [panel.gates.topLevelItem(i).text(0) for i in range(panel.gates.topLevelItemCount())]
    assert any("No mesh yet" in t for t in titles)
    assert any(b.property("action") == "run.mesh" for b in panel.findChildren(type(panel.apply_btn)))
    panel.select_stage("materials")
    assert panel.raw.count() >= 1 and "transportProperties" in panel.raw.item(0).text()
    assert panel.tabs.count() == 3 and [panel.tabs.tabText(i) for i in range(3)] == ["Guided", "Advanced", "Raw"]


def test_guided_answer_is_one_approved_plan(panel):
    panel.select_stage("bc")
    panel.set_answer("inlet_U:inlet", 12.0)
    assert panel.answers() == {"inlet_U:inlet": "12"}
    assert panel.apply_answers()
    plan = panel.approvals[-1]
    assert plan.command == "dict.batch" and any(fc.path.name == "U" for fc in plan.file_changes)
    root = Path(panel.sv.session.root)
    assert "uniform (12 0 0)" in (root / "0" / "U").read_text()
    # k/epsilon followed the new inlet speed at the same intensity and length scale
    assert "0.54" in (root / "0" / "k").read_text()  # 1.5 (0.05*12)^2


def test_mark_done_and_move_on(panel, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    msgs = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: msgs.append(a[2]))
    panel.select_stage("mesh")
    assert not panel.mark_done(True) and "cannot be marked done" in msgs[-1]
    panel.select_stage("materials")
    assert panel.mark_btn.isEnabled()
    assert panel.mark_done(True)
    assert panel.stage("materials").status == "done"
    assert panel.current != "materials"  # moved on to the next unfinished stage


def test_typed_answers_survive_a_refresh(panel):
    panel.select_stage("run")
    panel.set_answer("end_time", 321.0)
    panel.refresh_now()
    assert panel.answers().get("end_time") == "321"


def test_agent_runs_in_the_background(panel, qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("AETHERCFD_CFD_AGENTS_PYTHON", sys.executable)
    monkeypatch.setenv("AETHERCFD_CFD_AGENTS_ROOT", str(FAKE))
    root = Path(panel.sv.session.root)
    (root / "log.simpleFoam").write_text("Time = 1\nFloating point exception (core dumped)\n")
    panel.select_stage("run")
    panel.run_agent("murphy")
    qtbot.waitUntil(lambda: "fp_exception" in panel.agent_out.toPlainText(), timeout=60000)
    assert "nothing was changed" in panel.agent_out.toPlainText()


def test_grid_study_create_from_the_validation_stage(panel):
    panel.select_stage("validation")
    assert any(b.property("action") == "grid.create" for b in panel.findChildren(type(panel.apply_btn)))
    assert panel.grid_create()
    plan = panel.approvals[-1]
    assert plan.command == "files.write" and "fine" in plan.summary
    root = Path(panel.sv.session.root)
    assert (root / "grid_study" / "coarse" / "system" / "blockMeshDict").is_file()
    panel.select_stage("validation")
    assert any(b.property("action") == "grid.run" for b in panel.findChildren(type(panel.apply_btn)))
