"""A run is only "lost" when WSL says so, twice: an empty or garbled probe reply is "unknown", never "lost".

Seen live (a private 3-D case, 2026-10-11): an empty reply under heavy load marked a 1M-cell run lost at iteration ~326 while
runner.sh, mpirun and all 8 ranks were computing, and a finished record can never be re-activated by _set_state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from aethercfd.core.settings import Settings
from aethercfd.runs.manager import RunManager, RunRecord, RunState, _Monitor

BOOT = "abc"
ALIVE_OK = f"ALIVE 555 {BOOT}\n"


class FakeRunner:
    """Answers the liveness probe from a queue of stdout replies (an Exception in the queue is raised)."""

    def __init__(self, replies=(), *, boot: str = BOOT, ok: bool = True):
        self.replies = list(replies)
        self.boot = boot
        self.ok = ok
        self.probes = 0

    def run_script(self, body: str, *a, **k):
        if body.startswith("cat /proc/sys/kernel/random/boot_id"):
            return SimpleNamespace(stdout=self.boot + "\n", stderr="", ok=True)
        assert 'echo "ALIVE' in body, "only the liveness probe is expected"
        self.probes += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(stdout=r, stderr="", ok=self.ok)


class Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))


def _mgr(runner, tmp_path: Path | None = None) -> RunManager:
    m = RunManager.__new__(RunManager)
    m.runner, m.settings, m.bus = runner, Settings(), Bus()
    m.project = SimpleNamespace(runs_dir=tmp_path)
    m._monitors, m._home = {}, None
    m.started = []
    m._start_monitor = lambda rec, **kw: (m.started.append((rec.id, kw)), m._monitors.setdefault(rec.id, rec))
    return m


def _rec(runs_dir: Path, run_id: str = "r1", state: RunState = RunState.RUNNING, **kw) -> RunRecord:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = RunRecord(id=run_id, case_root=str(runs_dir / "case"), exec_case_wsl="/mnt/x/case", run_dir=str(run_dir),
                    run_dir_wsl=f"/mnt/x/runs/{run_id}", linux_run_dir=f"/root/aethercfd/runs/{run_id}", fork={},
                    placement="in-place", placement_reason="test", steps=[], nprocs=8, signals_enabled=True,
                    created="2026-10-11T00:00:00+00:00", state=state.value, pgid=4242, start_jiffies="555",
                    boot_id=BOOT, **kw)
    rec.save()
    return rec


def _probe(mgr: RunManager, m: _Monitor) -> bool:
    """One monitor tick with the liveness probe due."""
    m.last_alive_check = float("-inf")
    return mgr._tick(m, Path(m.rec.run_dir))


# ---------------------------------------------------------------------------------------------- is_alive replies
@pytest.mark.parametrize(("stdout", "expected"), [
    (ALIVE_OK, True),
    ("  ALIVE 555 abc  \r\n", True),
    ("DEAD\n", False),
    ("ALIVE 999 abc\n", False),  # same pgid, another process (pid reuse)
    ("ALIVE 555 xyz\n", False),  # same pgid and start time on another WSL boot
    ("", None),  # WSL busy / hiccup: unknown, never "lost"
    ("   \n", None),
    ("garbage\n", None),
    ("DEAD extra\n", None),
    ("ALIVE 555\n", None),  # truncated
    ("ALIVE  abc\n", None),  # /proc/<pgid>/stat unreadable: no start time to compare
    ("ALIVE 555 abc extra\n", None),
    ("ALIVE 555 abc\nDEAD\n", None),
    ("DEAD\nALIVE 555 abc\n", None),
])
def test_alive_probe_replies(stdout, expected):
    assert _mgr(FakeRunner([stdout])).is_alive(SimpleNamespace(pgid=4242, run_dir_wsl="/x", start_jiffies="555",
                                                               boot_id=BOOT)) is expected


def test_alive_probe_failures_are_unknown():
    rec = SimpleNamespace(pgid=4242, run_dir_wsl="/x", start_jiffies="555", boot_id=BOOT)
    assert _mgr(FakeRunner(["DEAD\n"], ok=False)).is_alive(rec) is None  # wsl.exe / timeout exit status
    assert _mgr(FakeRunner([TimeoutError("wsl.exe did not return")])).is_alive(rec) is None
    assert _mgr(FakeRunner()).is_alive(SimpleNamespace(pgid=None)) is False  # never launched


# ---------------------------------------------------------------------------------------------- monitor: LOST rule
def test_empty_replies_never_mark_a_run_lost(tmp_path):
    runner = FakeRunner(["", "", "garbage\n", "", TimeoutError()])
    mgr = _mgr(runner, tmp_path)
    m = _Monitor(_rec(tmp_path))
    for _ in range(5):
        assert _probe(mgr, m) is False
    assert m.rec.state == RunState.RUNNING.value and m.dead_replies == 0 and runner.probes == 5
    assert RunRecord.load(Path(m.rec.run_dir)).state == RunState.RUNNING.value


def test_two_consecutive_dead_replies_mark_lost(tmp_path):
    mgr = _mgr(FakeRunner(["DEAD\n", "DEAD\n"]), tmp_path)
    m = _Monitor(_rec(tmp_path))
    assert _probe(mgr, m) is False and m.rec.state == RunState.RUNNING.value  # one DEAD is not enough
    assert _probe(mgr, m) is True
    assert RunRecord.load(Path(m.rec.run_dir)).state == RunState.LOST.value


def test_one_dead_rechecks_soon(tmp_path):
    mgr = _mgr(FakeRunner(["DEAD\n"]), tmp_path)
    m = _Monitor(_rec(tmp_path))
    _probe(mgr, m)
    # next probe due DEAD_RECHECK_S after this one, not ALIVE_EVERY_S
    due_in = m.last_alive_check + mgr.ALIVE_EVERY_S - time.monotonic()
    assert 0 < due_in <= mgr.DEAD_RECHECK_S


def test_alive_reply_resets_the_dead_count(tmp_path):
    mgr = _mgr(FakeRunner(["DEAD\n", ALIVE_OK, "DEAD\n"]), tmp_path)
    m = _Monitor(_rec(tmp_path))
    assert not any(_probe(mgr, m) for _ in range(3))
    assert m.rec.state == RunState.RUNNING.value and m.dead_replies == 1


def test_unknown_reply_between_deads_neither_counts_nor_resets(tmp_path):
    mgr = _mgr(FakeRunner(["DEAD\n", "", "DEAD\n"]), tmp_path)
    m = _Monitor(_rec(tmp_path))
    assert [_probe(mgr, m) for _ in range(3)] == [False, False, True]
    assert m.rec.state == RunState.LOST.value


def test_alive_mismatch_is_definite(tmp_path):
    mgr = _mgr(FakeRunner(["ALIVE 999 abc\n", "ALIVE 999 abc\n"]), tmp_path)  # pgid reused by another process
    m = _Monitor(_rec(tmp_path))
    assert [_probe(mgr, m) for _ in range(2)] == [False, True]
    assert m.rec.state == RunState.LOST.value


def test_exit_code_after_one_dead_finishes_normally(tmp_path):
    mgr = _mgr(FakeRunner(["DEAD\n"]), tmp_path)
    m = _Monitor(_rec(tmp_path))
    assert _probe(mgr, m) is False
    (Path(m.rec.run_dir) / "exit_code").write_text("0\n")
    assert _probe(mgr, m) is True
    assert m.rec.state == RunState.FINISHED.value and m.rec.exit_code == 0


# ---------------------------------------------------------------------------------------------- reattach / revive
def test_reattach_keeps_monitoring_on_unknown_and_on_a_single_dead(tmp_path):
    a = _rec(tmp_path, "a")
    b = _rec(tmp_path, "b")
    mgr = _mgr(FakeRunner(["", "DEAD\n"]), tmp_path)  # runs() is sorted: a then b
    resumed = mgr.reattach()
    assert [r.id for r in resumed] == ["a"]
    assert mgr.started == [("a", {"dead_replies": 0}), ("b", {"dead_replies": 1})]  # b needs one more DEAD
    assert RunRecord.load(Path(a.run_dir)).state == RunRecord.load(Path(b.run_dir)).state == RunState.RUNNING.value


def test_reattach_revives_a_lost_run_whose_processes_are_alive(tmp_path):
    rec = _rec(tmp_path, state=RunState.LOST, stop_reason="process disappeared without an exit code")
    mgr = _mgr(FakeRunner([ALIVE_OK]), tmp_path)
    resumed = mgr.reattach()
    assert [r.id for r in resumed] == [rec.id] and mgr.started == [(rec.id, {})]
    saved = json.loads((Path(rec.run_dir) / "run.json").read_text(encoding="utf-8"))
    assert saved["state"] == RunState.RUNNING.value and saved["stop_reason"] is None
    assert ("run.state", {"run_id": rec.id, "case": rec.case_root, "state": "running", "revived": True}) \
        in mgr.bus.events


@pytest.mark.parametrize("reply", ["DEAD\n", "ALIVE 999 abc\n", "", "garbage\n"])
def test_lost_run_is_not_revived_without_proof(tmp_path, reply):
    rec = _rec(tmp_path, state=RunState.LOST)
    mgr = _mgr(FakeRunner([reply]), tmp_path)
    assert mgr.reattach() == [] and mgr.started == []
    assert RunRecord.load(Path(rec.run_dir)).state == RunState.LOST.value


def test_lost_runs_from_another_boot_or_finished_are_not_probed(tmp_path):
    _rec(tmp_path, "old", state=RunState.LOST)
    done = _rec(tmp_path, "done", state=RunState.LOST)
    (Path(done.run_dir) / "exit_code").write_text("0\n")
    runner = FakeRunner([], boot="another-boot")
    mgr = _mgr(runner, tmp_path)
    assert mgr.reattach() == [] and runner.probes == 0
    assert _mgr(FakeRunner([]), tmp_path).reattach(revive_lost=False) == []
