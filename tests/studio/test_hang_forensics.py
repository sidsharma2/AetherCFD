"""Hang and crash forensics: a stuck GUI thread leaves a stack dump; Help > Report a Problem zips the evidence."""

from __future__ import annotations

import json
import os
import time
import zipfile

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from aethercfd.studio.app.watchdog import HangWatchdog


def _block_the_gui_thread_for_the_test(seconds: float) -> None:
    time.sleep(seconds)  # a synchronous call on the GUI thread: no events, no heartbeat


def _pump(seconds: float) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        QApplication.processEvents()
        time.sleep(0.02)


def test_a_stuck_gui_thread_leaves_one_dump_naming_the_blocking_frame(qapp, tmp_path):
    wd = HangWatchdog(tmp_path, threshold=1.0, interval_ms=100)
    wd.context = "sweep step 7: viewport showcase"
    wd.start()
    try:
        _pump(0.5)
        _block_the_gui_thread_for_the_test(2.5)
        _pump(0.5)
    finally:
        wd.stop()
    dumps = sorted(tmp_path.glob("hang-*.txt"))
    assert len(dumps) == 1  # one dump per hang, not one per poll
    text = dumps[0].read_text(encoding="utf-8")
    assert "sweep step 7: viewport showcase" in text
    assert "_block_the_gui_thread_for_the_test" in text  # the GUI thread's stack shows what blocked it
    assert "GUI (main) thread id" in text


def test_an_event_loop_that_keeps_running_leaves_no_dump(qapp, tmp_path):
    wd = HangWatchdog(tmp_path, threshold=1.0, interval_ms=100)
    wd.start()
    try:
        _pump(2.5)
        with wd.paused():  # a legitimate blocking call is excused
            _block_the_gui_thread_for_the_test(1.5)
        _pump(0.5)
    finally:
        wd.stop()
    assert not list(tmp_path.glob("hang-*.txt"))


def test_from_env_threshold_and_off_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_HANG_SECONDS", "0")
    assert HangWatchdog.from_env(tmp_path) is None
    monkeypatch.setenv("AETHER_HANG_SECONDS", "4.5")
    assert HangWatchdog.from_env(tmp_path).threshold == 4.5
    monkeypatch.setenv("AETHER_HANG_SECONDS", "soon")
    assert HangWatchdog.from_env(tmp_path).threshold == 10.0


def test_old_dumps_are_pruned(tmp_path):
    wd = HangWatchdog(tmp_path, keep=3)
    for i in range(5):
        p = tmp_path / f"hang-2026010{i}-000000-1.txt"
        p.write_text("x")
        t = time.time() - 100 + i
        os.utime(p, (t, t))
    wd._prune()
    assert sorted(p.name for p in tmp_path.glob("hang-*.txt")) == [
        "hang-20260102-000000-1.txt", "hang-20260103-000000-1.txt", "hang-20260104-000000-1.txt"]


def test_report_zip_holds_logs_dumps_and_versions_but_no_case_files(tmp_path):
    from aethercfd.studio.app import report

    cache = tmp_path / "cache"
    (cache / "crash").mkdir(parents=True)
    (cache / "studio.log").write_bytes(b"x" * (report.LOG_TAIL_BYTES + 5000) + b"\nlast line\n")
    (cache / "crash" / "faulthandler.log").write_text("Windows fatal exception: access violation\n")
    for i in range(report.MAX_HANG_DUMPS + 2):
        (cache / "crash" / f"hang-2026010{i:02d}-1.txt").write_text(f"dump {i}")
    case = tmp_path / "case"  # a case folder on the same disk: nothing of it may reach the zip
    (case / "system").mkdir(parents=True)
    (case / "system" / "controlDict").write_text("SECRET_CASE_CONTENT")

    class S:
        theme, gpu, gpu_compute, copilot_provider, viewport_look = "dark", "high-performance", "auto", "claude", "studio"

    z = report.make_report(cache, tmp_path / "reports", settings=S(), note="viewport froze")
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        body = b"".join(zf.read(n) for n in names)
        log = zf.read("studio.log")
        versions = zf.read("versions.txt").decode()
    hangs = {n for n in names if n.startswith("hangs/")}
    assert names - hangs == {"versions.txt", "note.txt", "studio.log", "faulthandler.log"}
    assert len(hangs) == report.MAX_HANG_DUMPS  # the newest ones
    assert log.endswith(b"last line\n") and len(log) < report.LOG_TAIL_BYTES + 100  # the tail only
    assert b"SECRET_CASE_CONTENT" not in body and str(case).encode() not in body  # no case data, no paths
    assert "AetherCFD" in versions and "setting theme = 'dark'" in versions


def test_help_report_a_problem_writes_the_zip(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AETHERCFD_CACHE_DIR", str(tmp_path / "cache"))
    from aethercfd.studio.app.services import AppServices
    from aethercfd.studio.panels import register_builtin_panels
    from aethercfd.studio.shell.main_window import MainWindow

    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    acts = [a for m in w.menuBar().actions() if m.menu() for a in m.menu().actions()]
    assert any(a.text() == "Report a Problem…" for a in acts)
    path = w.report_problem(ask=False)
    assert path is not None and path.is_file() and path.parent == tmp_path / "cache" / "reports"
    with zipfile.ZipFile(path) as zf:
        assert "versions.txt" in zf.namelist()
    sv.pool.waitForDone(10000)


def test_sweep_steps_name_themselves_for_the_watchdog_and_on_disk(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    from aethercfd.studio.app.services import AppServices
    from aethercfd.studio.panels import register_builtin_panels
    from aethercfd.studio.shell.main_window import MainWindow
    from aethercfd.studio.sweep import Sweep

    sv = AppServices()
    register_builtin_panels(sv.panels)
    w = MainWindow(sv)
    qtbot.addWidget(w)
    sv.watchdog = HangWatchdog(tmp_path / "crash")
    sw = Sweep(QApplication.instance(), w, tmp_path)
    seen = {}

    def first():
        seen["context"] = sv.watchdog.context
        seen["partial"] = json.loads((tmp_path / "sweep.partial.json").read_text(encoding="utf-8"))
        return "done"

    sw.step("open the viewport", first)
    assert seen["context"] == "sweep step 1: open the viewport"
    assert seen["partial"] == {"running": "open the viewport", "steps": []}  # on disk BEFORE the step ran
    sw.step("second", lambda: "ok")
    assert sv.watchdog.context == "sweep step 2: second"
    sv.pool.waitForDone(10000)
