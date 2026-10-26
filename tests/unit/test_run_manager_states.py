"""Fault injection at the WSL boundary (roadmap Q4): a Hypothesis state machine over RunManager's liveness logic.

The faults are injected under the real WslRunner (subprocess.run is patched), so every reply goes through the real
decoding: UTF-16 from wsl.exe, empty output, garbage, a reply cut mid-line, a non-zero exit mid-stream, a wsl.exe
error, a timeout and a missing wsl.exe. Invariants:

- a run whose process group is alive is never marked lost;
- a run is lost only after two consecutive definite DEAD replies (an unknown reply neither counts nor resets);
- two consecutive definite DEAD replies do mark it lost on that tick;
- a lost run is revived only on a definite ALIVE reply that matches its process (start time and boot id).

Reverting feeeb4a (lost on the first DEAD) or the cut-reply check makes the machine fail: see the last two tests.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from aethercfd.backend.wsl.runner import WslRunner, decode_output
from aethercfd.core.settings import Settings
from aethercfd.runs.manager import RunManager, RunRecord, RunState, _Monitor

BOOT = "4f9c1a2e-0b7d-4c55-9e2a-3d8f6b1c7e90"
START = "555"
DEFINITE = ("ok", "utf16")  # replies that carry the truth
FAULTS = ("ok", "utf16", "empty", "blank", "garbled", "truncated", "exit_mid", "wsl_error", "timeout", "no_wsl")
GARBAGE = st.binary(max_size=48).filter(
    lambda b: "ALIVE" not in decode_output(b) and "DEAD" not in decode_output(b))


def _cp(argv, rc: int, out: bytes, err: bytes = b""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class FaultyWsl:
    """Stands in for subprocess.run under WslRunner.run_script: answers the liveness probe from the machine's
    true process state, through the fault the machine chose for this call."""

    def __init__(self, machine: RunLiveness):
        self.m = machine

    def __call__(self, argv, input=None, **_kw):
        body = (input or b"").decode("utf-8")
        if body.startswith("cat /proc/sys/kernel/random/boot_id"):
            return _cp(argv, 0, (BOOT + "\n").encode())
        assert 'echo "ALIVE' in body, "only the liveness probe and the boot id are expected"
        m = self.m
        line = f"ALIVE {m.start} {BOOT}\n" if m.alive else "DEAD\n"
        cut = line.encode()[:max(1, min(m.cut, len(line) - 1))]  # a strict prefix: the newline is never in it
        f = m.fault
        if f == "ok":
            return _cp(argv, 0, line.encode())
        if f == "utf16":  # wsl.exe can hand output on as UTF-16LE; decode_output reads it
            return _cp(argv, 0, line.encode("utf-16-le"))
        if f == "empty":
            return _cp(argv, 0, b"")
        if f == "blank":
            return _cp(argv, 0, b"  \r\n")
        if f == "garbled":
            return _cp(argv, 0, m.garbage)
        if f == "truncated":
            return _cp(argv, 0, cut)
        if f == "exit_mid":
            return _cp(argv, 255, cut)
        if f == "wsl_error":
            return _cp(argv, 1, b"", "The system cannot find the path specified.\r\n".encode("utf-16-le"))
        if f == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if f == "no_wsl":
            raise FileNotFoundError("wsl.exe")
        raise AssertionError(f"unknown fault {f}")


class RunLiveness(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.tmp = Path(tempfile.mkdtemp(prefix="aether-sm-"))
        self._patch = mock.patch("aethercfd.backend.wsl.runner.subprocess.run", FaultyWsl(self))
        self._patch.start()
        self.alive, self.start = True, START
        self.fault, self.garbage, self.cut = "ok", b"", 0
        self.consec_dead = 0  # the model: definite DEAD replies in a row since the monitor started
        self.mon: _Monitor | None = None
        self.done = False
        self.exit_written = False
        self.wrongly_lost = False  # the run starts "lost" by an older version while its processes compute
        mgr = RunManager.__new__(RunManager)
        mgr.runner = WslRunner("Test", allowed=["Test"])
        mgr.settings, mgr.bus = Settings(), SimpleNamespace(publish=lambda *a, **k: None)
        mgr.project = SimpleNamespace(runs_dir=self.tmp)
        mgr._monitors, mgr._home = {}, None

        def start_monitor(rec, *, dead_replies: int = 0):  # the real one starts a thread; the machine ticks itself
            self.mon = _Monitor(rec, dead_replies=dead_replies)
            mgr._monitors[rec.id] = self.mon

        mgr._start_monitor = start_monitor
        self.mgr = mgr

    def teardown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_fault(self, fault, garbage, cut):
        self.fault, self.garbage, self.cut = fault, garbage, cut

    @initialize(start_lost=st.booleans())
    def make_run(self, start_lost):
        run_dir = self.tmp / "r1"
        run_dir.mkdir()
        self.rec = RunRecord(
            id="r1", case_root=str(self.tmp / "case"), exec_case_wsl="/mnt/x/case", run_dir=str(run_dir),
            run_dir_wsl="/mnt/x/runs/r1", linux_run_dir="/root/aethercfd/runs/r1", fork={}, placement="in-place",
            placement_reason="test", steps=[], nprocs=8, signals_enabled=True, created="2026-10-26T00:00:00+00:00",
            state=(RunState.LOST if start_lost else RunState.RUNNING).value, pgid=4242, start_jiffies=START,
            boot_id=BOOT)
        self.rec.save()
        if start_lost:
            self.wrongly_lost = True
        else:
            self.mon = _Monitor(self.rec)
            self.mgr._monitors[self.rec.id] = self.mon

    # ---------------------------------------------------------------- rules
    @precondition(lambda self: self.mon is not None and not self.done)
    @rule(fault=st.sampled_from(FAULTS), garbage=GARBAGE, cut=st.integers(0, 60))
    def probe(self, fault, garbage, cut):
        """One monitor tick with the liveness probe due, through one fault."""
        self._set_fault(fault, garbage, cut)
        if fault in DEFINITE:
            self.consec_dead = 0 if self.alive else self.consec_dead + 1
        self.mon.last_alive_check = float("-inf")
        self.done = self.mgr._tick(self.mon, Path(self.rec.run_dir))
        if self.exit_written:
            assert self.done and self.rec.state == RunState.FINISHED.value  # the exit code always wins
        elif self.consec_dead >= RunManager.DEAD_REPLIES_FOR_LOST:
            assert self.done and self.rec.state == RunState.LOST.value, "two definite DEADs must mark it lost"
        else:
            assert not self.done and self.rec.state == RunState.RUNNING.value

    @precondition(lambda self: self.alive and not self.done)
    @rule()
    def process_dies(self):
        """The solver's process group is gone, with no exit code (a crash, an OOM kill)."""
        self.alive = False

    @precondition(lambda self: self.mon is not None and not self.done and not self.exit_written)
    @rule()
    def runner_writes_exit_code(self):
        self.alive = False
        (Path(self.rec.run_dir) / "exit_code").write_text("0\n")
        self.exit_written = True

    @precondition(lambda self: not self.exit_written)
    @rule(fault=st.sampled_from(FAULTS), garbage=GARBAGE, cut=st.integers(0, 60))
    def app_restarts(self, fault, garbage, cut):
        """The GUI restarts: monitoring stops, and reattach() probes each run once. An active run is monitored
        again; a lost one is revived only on proof that its processes are alive."""
        self._set_fault(fault, garbage, cut)
        was_active = RunState(self.rec.state).active
        self.mgr._monitors.clear()
        self.mon = None
        self.mgr.reattach(revive_lost=True)
        # reattach loads the records from disk: follow the object the manager now works on
        self.rec = self.mon.rec if self.mon is not None else RunRecord.load(Path(self.rec.run_dir))
        definite = fault in DEFINITE
        if was_active:
            assert self.mon is not None, "an active run is always monitored again"
            # a fresh monitor: one definite DEAD counts as the first, anything else starts the count at zero
            self.consec_dead = 1 if (definite and not self.alive) else 0
            assert self.mon.dead_replies == self.consec_dead
            self.done = False
        else:
            revived = self.mon is not None
            assert revived == (definite and self.alive), "a lost run is revived only on a matching ALIVE"
            if revived:
                self.wrongly_lost, self.consec_dead, self.done = False, 0, False
                assert self.rec.state == RunState.RUNNING.value

    @precondition(lambda self: self.rec.state == RunState.LOST.value and self.mon is None)
    @rule(fault=st.sampled_from(FAULTS), garbage=GARBAGE, cut=st.integers(0, 60))
    def revive(self, fault, garbage, cut):
        self._set_fault(fault, garbage, cut)
        revived = self.mgr.revive(self.rec)
        assert revived == (fault in DEFINITE and self.alive), "revive needs a definite ALIVE that matches"
        if revived:
            self.wrongly_lost = False
            self.consec_dead, self.done = 0, False
            assert self.rec.state == RunState.RUNNING.value and self.mon is not None

    @rule()
    def time_passes(self):
        """A tick with no probe due. Always possible; on a finished or lost run it must change nothing."""
        before = self.rec.state
        if self.mon is not None and not self.done:
            self.mon.last_alive_check = float("inf")  # the next probe is not due yet
            self.done = self.mgr._tick(self.mon, Path(self.rec.run_dir))
            if not self.exit_written:
                assert not self.done and self.rec.state == before
        else:
            assert self.rec.state == before

    # ---------------------------------------------------------------- invariants
    @invariant()
    def never_lost_while_alive(self):
        if getattr(self, "rec", None) is not None and self.rec.state == RunState.LOST.value and not self.wrongly_lost:
            assert not self.alive, "a run with live processes was marked lost"
            assert self.consec_dead >= RunManager.DEAD_REPLIES_FOR_LOST

    @invariant()
    def record_on_disk_matches(self):
        if getattr(self, "rec", None) is not None:
            assert RunRecord.load(Path(self.rec.run_dir)).state == self.rec.state


SETTINGS = settings(max_examples=500, stateful_step_count=25, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


def _machine_fails(machine, s) -> None:
    """The machine must find a violation of its invariants (Hypothesis groups several distinct ones)."""
    with pytest.raises((AssertionError, ExceptionGroup)) as info:
        run_state_machine_as_test(machine, settings=s)
    errors = info.value.exceptions if isinstance(info.value, ExceptionGroup) else [info.value]
    assert errors and all(isinstance(e, AssertionError) for e in errors), errors


def test_run_liveness_state_machine():
    run_state_machine_as_test(RunLiveness, settings=SETTINGS)


def test_the_machine_catches_lost_on_the_first_dead(monkeypatch):
    """Reverting feeeb4a (a single DEAD marks the run lost) must fail the machine."""
    monkeypatch.setattr(RunManager, "DEAD_REPLIES_FOR_LOST", 1)

    class OneDeadIsLost(RunLiveness):
        @precondition(lambda self: self.mon is not None and not self.done)
        @rule(fault=st.sampled_from(FAULTS), garbage=GARBAGE, cut=st.integers(0, 60))
        def probe(self, fault, garbage, cut):  # the model keeps the real rule: lost only after two
            self._set_fault(fault, garbage, cut)
            if fault in DEFINITE:
                self.consec_dead = 0 if self.alive else self.consec_dead + 1
            self.mon.last_alive_check = float("-inf")
            self.done = self.mgr._tick(self.mon, Path(self.rec.run_dir))

        @invariant()
        def lost_needs_two(self):
            if getattr(self, "rec", None) is not None and self.rec.state == RunState.LOST.value \
                    and not self.wrongly_lost:
                assert self.consec_dead >= 2

    _machine_fails(OneDeadIsLost, settings(SETTINGS, max_examples=200))


def test_the_machine_catches_a_cut_reply_read_as_definite(monkeypatch):
    """Without the complete-line check, a reply cut inside the boot id reads as another boot (DEAD) while the
    processes compute; two of those mark a healthy run lost."""
    from aethercfd.runs.scripts import control_sh

    def without_line_check(self, rec):  # is_alive as it was before Q4
        if rec.pgid is None:
            return False
        try:
            res = self.runner.run_script(control_sh("alive", rec.run_dir_wsl, rec.pgid), timeout_s=30)
        except Exception:
            return None
        reply = res.stdout.strip()
        out = reply.split()
        if not res.ok or "\n" in reply or not (reply == "DEAD" or (len(out) == 3 and out[0] == "ALIVE")):
            return None
        if reply == "DEAD":
            return False
        return out[1] == rec.start_jiffies and out[2] == rec.boot_id

    monkeypatch.setattr(RunManager, "is_alive", without_line_check)
    _machine_fails(RunLiveness, settings(SETTINGS, max_examples=300))
