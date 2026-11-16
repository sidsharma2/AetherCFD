"""Run-related panels never wait on a subprocess on the GUI thread, and clean up after themselves."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QFileDialog, QMessageBox

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.run import panels as P
from aethercfd.studio.shell.panel_base import PanelContext


@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    return AppServices()


def _panel(sv, qtbot, tmp_path, monkeypatch):
    p = P.ResidualsPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    p.data = {"p": [1.0, 0.1, 0.01], "Ux": [1.0, 0.5, 0.2]}
    p.x_iter = [1.0, 2.0, 3.0]
    p.x_time = [1.0, 2.0, 3.0]
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "res.pdf"), ""))
    return p


def test_residual_export_runs_in_the_background_and_removes_its_temp_folder(sv, qtbot, tmp_path, monkeypatch):
    p = _panel(sv, qtbot, tmp_path, monkeypatch)
    release, seen = threading.Event(), []

    def slow_run(argv, **kw):
        seen.append((threading.current_thread() is threading.main_thread(), Path(argv[-1]).parent))
        release.wait(10)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(P.subprocess, "run", slow_run)
    notes = []
    sv.notification.connect(lambda msg, level: notes.append((msg, level)))
    t0 = time.monotonic()
    p.export()
    assert time.monotonic() - t0 < 2.0  # the GUI did not wait for matplotlib
    release.set()
    qtbot.waitUntil(lambda: any(m.startswith("Exported") for m, _ in notes), timeout=10000)
    assert seen and seen[0][0] is False
    assert not seen[0][1].exists()  # the temporary spec folder is gone


def test_residual_export_timeout_is_a_message_not_a_traceback(sv, qtbot, tmp_path, monkeypatch):
    p = _panel(sv, qtbot, tmp_path, monkeypatch)

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(P.subprocess, "run", hang)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    notes = []
    sv.notification.connect(lambda msg, level: notes.append((msg, level)))
    p.export()
    qtbot.waitUntil(lambda: any(level == "error" for _, level in notes), timeout=10000)
    assert "did not finish within 120 s" in notes[-1][0] and warned


def test_reattach_after_opening_a_case_runs_off_the_gui_thread(sv, qtbot, tmp_path, monkeypatch):
    """reattach() probes every active run (WSL, or SSH for a cluster run): the GUI thread must not wait."""
    import shutil

    from aethercfd.backend.wsl import runner as R
    from aethercfd.runs.manager import RunManager
    from aethercfd.scripting import api

    class FakeRunner:
        def __init__(self, distro, allowed=None):
            self.distro = distro

    monkeypatch.setattr(R, "WslRunner", FakeRunner)
    monkeypatch.setattr(api, "load_forks", lambda runner: [])
    on_main = []

    def slow_reattach(self, *, revive_lost=True):
        on_main.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.3)
        return ["r1"]

    monkeypatch.setattr(RunManager, "reattach", slow_reattach)
    notes = []
    sv.notification.connect(lambda msg, level: notes.append((msg, level)))
    case = tmp_path / "cavity"
    shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity", case)
    sv.open_case(case, create_project=True)
    qtbot.waitUntil(lambda: any("Re-attached to 1" in m for m, _ in notes), timeout=10000)
    assert on_main == [False]
