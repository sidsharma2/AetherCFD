"""Diff, Python console, Measurements and Analysis panels; automation hooks the copilot uses."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")

from aethercfd.studio.app.services import AppServices
from aethercfd.studio.panels.analysis.panel import AnalysisPanel
from aethercfd.studio.panels.diff.panel import DiffPanel
from aethercfd.studio.panels.measurements.panel import MeasurementsPanel
from aethercfd.studio.panels.python_console.panel import PythonConsolePanel
from aethercfd.studio.shell.panel_base import PanelContext

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


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
    return s


def test_diff_panel(sv, qtbot, tmp_path):
    p = DiffPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    root = Path(sv.session.root)
    assert p.compare_paths(root / "0.1" / "p", root / "0.2" / "p") == "1 change(s): +1 / -1 lines"
    other = tmp_path / "other"
    shutil.copytree(root, other, ignore=shutil.ignore_patterns(".aethercfd"))
    (other / "system" / "controlDict").write_text((root / "system" / "controlDict").read_text().replace(
        "endTime", "endTime  // changed\n endTime", 1))
    assert "change" in p.compare_with_other_case(root / "system" / "controlDict", other)
    # before its last journalled change
    from aethercfd.commands.base import USER

    f = root / "system" / "controlDict"
    sv.session.executor.execute("dict.set", {"file": str(f), "key": "endTime", "value": "9"}, USER)
    assert "1 change(s)" in p.before_last_change(f)


def test_python_console_panel(sv, qtbot):
    p = PythonConsolePanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    p.run_code("print('max p', data.fields(names=['p'])['p'].max())")
    qtbot.waitUntil(lambda: "max p 5.0" in p.out.toPlainText(), timeout=30000)
    assert not p.run_code("for i in range(3):", force=False)  # incomplete: Enter adds a line
    assert "placed" in p.insert("x = 1")
    # a cell that never ends: Stop interrupts it, and the console is usable again
    p.run_code("while True: pass")
    qtbot.waitUntil(lambda: p.stop.isEnabled(), timeout=5000)
    p.interrupt()
    qtbot.waitUntil(lambda: not p._busy, timeout=10000)
    assert "KeyboardInterrupt" in p.out.toPlainText()
    # Restart while a cell runs: abandoned, no stuck "busy" state
    p.run_code("import time\nwhile True: time.sleep(0.01)")
    qtbot.waitUntil(lambda: p._busy, timeout=5000)
    p.restart()
    assert not p._busy
    p.run_code("print('after', 6 * 7)")
    qtbot.waitUntil(lambda: "after 42" in p.out.toPlainText(), timeout=10000)
    assert p._thread.daemon  # never keeps the app process alive


def test_measurements_panel(sv, qtbot, tmp_path):
    p = MeasurementsPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    f = tmp_path / "m.csv"
    with f.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["x", "y", "z", "p", "p_std"])
        w.writerows([[0.02 + 0.06 * i / 9, 0.05, 0.005, 6.0, 0.1] for i in range(10)])
    p.csv.setText(str(f))
    p.load()
    assert p.field.currentText() == "p"
    rep = p.compare()
    assert rep["fields"]["p"]["bias"] == pytest.approx(-1.0)  # latest time 0.4: p = 5 vs measured 6
    p.sigma_b.setValue(2.0)
    p.length.setValue(0.02)
    a = p.assimilate()
    assert np.sqrt(np.mean(a.residual ** 2)) < 0.5 < np.sqrt(np.mean(a.innovation ** 2))
    p.write_fields()
    assert (Path(sv.session.root) / "0.4" / "pAssim").is_file()


def test_analysis_panel_modes_and_surrogate(sv, qtbot):
    p = AnalysisPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    p.m_field.setCurrentText("p")
    assert p.analyse()  # runs in the background
    qtbot.waitUntil(lambda: not p.m_busy, timeout=60000)
    kind, _ts, r, name, _root = p.last
    assert kind == "POD" and name == "p" and r.energy[0] > 0.99  # uniform fields over time: one mode
    assert p.m_write.isEnabled()
    gp = p.fit_surrogate()
    assert gp is not None and "Next run" in p.s_out.toPlainText()
    p.s_table.setPlainText("1, nan\n2, 3\n3, 4")  # NaN rows are skipped, not fed to the GP
    assert p.fit_surrogate() is not None and "skipped" in p.s_out.toPlainText()
    p._on_case()  # another case opened: results of this mesh can no longer be written
    assert p.last is None and not p.m_write.isEnabled()


def test_pinn_tab_trains_and_writes(sv, qtbot):
    from aethercfd.ml.pinn import find_ml_python

    if find_ml_python() is None:
        pytest.skip("no PyTorch environment")
    p = AnalysisPanel(PanelContext(sv, "#main"))
    qtbot.addWidget(p)
    qtbot.waitUntil(lambda: "PyTorch" in p.p_status.text(), timeout=120000)
    p.p_phys.setCurrentIndex(2)  # data fit only (the fixture has no flow)
    p.p_field.setCurrentText("p")
    p.p_ndata.setValue(30)
    p.p_epochs.setValue(300)
    p.p_lbfgs.setValue(0)
    p.train_pinn()
    qtbot.waitUntil(lambda: p.p_result is not None, timeout=600000)
    assert p.p_result["ok"] and p.p_result["rmse_over_std_vs_cfd"][0] is not None
    p.write_pinn()
    assert (Path(sv.session.root) / "0.4" / "pPinn").is_file()


def test_analysis_on_a_case_without_results(tmp_path, qtbot, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    s = AppServices()
    s.open_case(case, create_project=True)
    p = AnalysisPanel(PanelContext(s, "#main"))
    qtbot.addWidget(p)
    if p.m_field.count():
        assert p.analyse()
        qtbot.waitUntil(lambda: not p.m_busy, timeout=30000)
    assert "no saved time" in p.m_out.toPlainText() or "No field" in p.m_out.toPlainText()
