"""Run panel target picker and the Clusters dialog (ADR 0007). No cluster and no SSH: the connection check is faked."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox, QPlainTextEdit

from aethercfd.core.clusters import ClusterProfile
from aethercfd.core.settings import Settings
from aethercfd.runs.cluster.transport import CheckLine
from aethercfd.runs.manager import RunRecord, RunState
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.dialogs.clusters import ClusterProfilesDialog
from aethercfd.studio.panels.run.panels import PlanDialog, RunPanel
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"
HPC = ClusterProfile(name="hpc", host="login.example.org", user="sid", openfoam_version="v2306", walltime="03:00:00")


@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "cavity"
    shutil.copytree(FIX, case)
    s = AppServices()
    s.settings.clusters = [HPC]
    s.open_case(case, create_project=True)
    s.session.settings.clusters = [HPC]
    return s


def test_run_panel_offers_cluster_targets(sv, qtbot):
    p = RunPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    assert [p.target.itemData(i) for i in range(p.target.count())] == ["local", "cluster:hpc"]
    assert p._form.isRowVisible(p.placement) and not p._form.isRowVisible(p.walltime)
    p.target.setCurrentIndex(1)
    assert not p._form.isRowVisible(p.placement) and p._form.isRowVisible(p.walltime)
    assert p.walltime.placeholderText() == "03:00:00" and p.nprocs.maximum() == 4096
    p.target.setCurrentIndex(0)
    assert p.nprocs.maximum() == 64


def test_cluster_plan_dialog_says_where_commands_run(sv, qtbot):
    from aethercfd.commands.base import USER
    from aethercfd.runs.manager import RunManager

    s = sv.session
    s.runs = RunManager(s.project, None, settings=s.settings, bus=s.bus)  # type: ignore[arg-type]
    s.context.run_manager = s.runs
    plan = s.executor.plan("run.start", {"case": str(s.root), "target": "cluster:hpc", "nprocs": 4}, USER)
    d = PlanDialog(plan)
    qtbot.addWidget(d)
    text = "\n".join(w.toPlainText() for w in d.findChildren(QPlainTextEdit))
    assert not plan.blocked, plan.blocked
    assert "run in the cluster job" in text and "srun -n 4" in text and "walltime 03:00:00" in text


def test_pending_cluster_run_buttons_and_state(sv, qtbot):
    s = sv.session
    run_dir = s.project.runs_dir / "r1"
    run_dir.mkdir(parents=True)
    RunRecord(id="r1", case_root=str(s.root), exec_case_wsl="/scratch/case", run_dir=str(run_dir), run_dir_wsl="/x",
              linux_run_dir="/scratch/runs/r1", fork={}, placement="cluster", placement_reason="hpc", steps=[],
              nprocs=4, signals_enabled=True, created="2026-10-14T00:00:00+00:00", state=RunState.PENDING.value,
              target="cluster:hpc", job_id="41000", scheduler_state="PENDING",
              remote={"profile": HPC.model_dump()}).save()
    from aethercfd.runs.manager import RunManager

    s.runs = RunManager(s.project, None, settings=s.settings, bus=s.bus)  # type: ignore[arg-type]
    p = RunPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    p.run_id = "r1"
    p._update_buttons()
    assert p.btns["kill"].isEnabled() and not p.btns["write_now"].isEnabled()
    assert p.btns["pause"].isHidden() and p.btns["resume"].isHidden()
    assert "job 41000" in p.state.text() and "pending" in p.state.text()


def test_clusters_dialog_add_validate_test_and_save(sv, qtbot, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    d = ClusterProfilesDialog(sv)
    qtbot.addWidget(d)
    assert d.list.count() == 1 and d.fields["host"].text() == "login.example.org"
    d._add()
    assert d.list.count() == 2 and d.list.currentRow() == 1
    d.fields["name"].setText("narval")
    d.fields["host"].setText("bad host; rm -rf ~")
    assert not d._commit() and warnings and "host" in warnings[-1]
    d.fields["host"].setText("narval.alliancecan.ca")
    d.fields["work_dir"].setText("$SCRATCH/aethercfd")
    d.fields["env_setup"].setText("module load StdEnv/2023 openfoam/v2306")
    d.fields["openfoam_version"].setText("v2306")
    d.launcher.setCurrentText("mpirun")
    d.extra.setPlainText("--constraint=cascade\n\n")
    assert d._commit()

    import aethercfd.runs.cluster.transport as tr

    seen = []
    monkeypatch.setattr(d, "_runner", lambda: object())
    monkeypatch.setattr(tr, "check_connection", lambda prof, runner: (
        seen.append(prof.name) or ([CheckLine(True, "connected to narval1"), CheckLine(False, "sacct missing")], None)))
    d._test()
    qtbot.waitUntil(lambda: "sacct missing" in d.report.toPlainText(), timeout=10000)
    assert seen == ["narval"] and "ok       connected" in d.report.toPlainText()

    d._save()
    saved = Settings.load().clusters
    assert [c.name for c in saved] == ["hpc", "narval"]
    assert saved[1].mpi_launcher == "mpirun" and saved[1].sbatch_extra == ["--constraint=cascade"]
    assert [c.name for c in sv.session.settings.clusters] == ["hpc", "narval"]  # the open session sees it at once
