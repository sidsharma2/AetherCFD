"""The PINN tab of the Analysis panel with a fake worker (no torch needed): an honest result, Stop, a crash,
and no environment. The real training run of the tab is test_new_panels.py::test_pinn_tab_trains_and_writes."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from aethercfd.ml import pinn
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.analysis.panel import AnalysisPanel
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"
FAKE = Path(__file__).resolve().parents[1] / "unit" / "fake_pinn_worker.py"
READY = {"available": True, "python": sys.executable, "torch": "fake", "gpu": None, "cuda": False}


@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    for i, v in enumerate((1.0, 3.0, 2.0, 5.0), start=1):
        d = case / f"0.{i}"
        shutil.copytree(case / "0", d)
        (d / "p").write_text(re.sub(r"internalField\s+uniform\s+[^;]+;", f"internalField uniform {v};",
                                    (d / "p").read_text()))
    s = AppServices()
    s.open_case(case, create_project=True)
    s.settings.ml_python = sys.executable
    return s


def _panel(sv, qtbot, monkeypatch, mode: str, status: dict = READY) -> AnalysisPanel:
    monkeypatch.setattr(pinn, "WORKER", FAKE)
    monkeypatch.setattr(pinn, "ml_status", lambda setting=None: status)
    orig = pinn.build_job

    def build(*a, **kw):
        job = orig(*a, **kw)
        job.params["_fake"] = mode
        return job

    monkeypatch.setattr(pinn, "build_job", build)
    p = AnalysisPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    qtbot.waitUntil(lambda: "checking" not in p.p_status.text(), timeout=30000)
    p.p_phys.setCurrentIndex(p.p_phys.findData("data"))
    p.p_field.setCurrentText("p")
    p.p_ndata.setValue(30)
    return p


def test_result_is_labelled_with_heldout_error_and_written_with_its_record(sv, qtbot, monkeypatch):
    p = _panel(sv, qtbot, monkeypatch, "ok")
    assert p.p_go.isEnabled() and "PyTorch fake" in p.p_status.text()
    p.p_seed.setValue(5)
    p.train_pinn()
    qtbot.waitUntil(lambda: p.p_result is not None, timeout=60000)
    text = p.p_out.toPlainText()
    assert p.p_result["ok"] and "not a CFD solution" in text and "did not train on" in text, text
    assert "held-out points" in text and "seed 5" in text and "done on" in text
    assert p.p_go.isEnabled() and not p.p_stop.isEnabled()
    p.write_pinn()
    root = Path(sv.session.root)
    assert (root / "0.4" / "pPinn").is_file()
    assert json.loads((root / ".aethercfd" / "pinn" / "pPinn_0.4.json").read_text())["seed"] == 5


def test_stop_ends_the_worker(sv, qtbot, monkeypatch):
    p = _panel(sv, qtbot, monkeypatch, "slow")
    p.train_pinn()
    qtbot.waitUntil(lambda: p.p_stop.isEnabled(), timeout=30000)
    qtbot.waitUntil(lambda: "step" in p.p_status.text(), timeout=30000)
    jd = p.p_job[1]
    p.stop_pinn()
    qtbot.waitUntil(lambda: p.p_result is not None, timeout=30000)
    assert not p.p_result["ok"] and "stopped by the user" in p.p_out.toPlainText()
    assert not pinn._worker_alive(jd) and p.p_go.isEnabled()


def test_a_crash_is_shown_and_the_tab_recovers(sv, qtbot, monkeypatch):
    p = _panel(sv, qtbot, monkeypatch, "crash")
    p.train_pinn()
    qtbot.waitUntil(lambda: p.p_result is not None, timeout=30000)
    assert "Error" in p.p_out.toPlainText() and "boom" in p.p_out.toPlainText()
    assert p.p_go.isEnabled() and not p.p_write.isEnabled()


def test_closing_the_case_or_panel_stops_a_run(sv, qtbot, monkeypatch):
    p = _panel(sv, qtbot, monkeypatch, "hang")
    p.train_pinn()
    qtbot.waitUntil(lambda: p.p_stop.isEnabled(), timeout=30000)
    jd = p.p_job[1]
    p.dispose()
    assert not pinn._worker_alive(jd)


def test_no_environment_says_how_to_get_one(sv, qtbot, monkeypatch):
    p = _panel(sv, qtbot, monkeypatch, "ok", {"available": False, "note": "no PyTorch environment found.",
                                              "hint": pinn.SETUP_HINT})
    txt = p.p_status.text()
    assert not p.p_go.isEnabled() and "setup_ml_env.py" in txt and "not available" in txt
    monkeypatch.setattr(pinn, "ml_status", lambda setting=None: READY)
    p.check_ml_env()  # after running the setup script: Check again
    qtbot.waitUntil(lambda: p.p_go.isEnabled(), timeout=30000)
