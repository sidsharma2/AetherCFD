"""A run is only "lost" when WSL says so: an empty or garbled probe reply is "unknown"."""

from __future__ import annotations

from types import SimpleNamespace

from aethercfd.runs.manager import RunManager


def _mgr(stdout: str):
    m = RunManager.__new__(RunManager)
    m.runner = SimpleNamespace(run_script=lambda *a, **k: SimpleNamespace(stdout=stdout, stderr="", ok=True))
    return m


REC = SimpleNamespace(pgid=123, run_dir_wsl="/x", start_jiffies="555", boot_id="abc")


def test_alive_probe_replies():
    assert _mgr("ALIVE 555 abc\n").is_alive(REC) is True
    assert _mgr("DEAD\n").is_alive(REC) is False
    assert _mgr("ALIVE 999 abc\n").is_alive(REC) is False  # same pgid, another process (pid reuse)
    assert _mgr("").is_alive(REC) is None  # WSL busy / hiccup: unknown, never "lost"
    assert _mgr("garbage\n").is_alive(REC) is None
    assert RunManager.is_alive(_mgr(""), SimpleNamespace(pgid=None)) is False
