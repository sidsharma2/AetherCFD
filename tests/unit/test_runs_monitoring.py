from __future__ import annotations

import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aethercfd.runs.datsources import find_residual_dat
from aethercfd.runs.guards import (
    GB,
    MB,
    Level,
    LogGrowthGuard,
    RepetitionGuard,
    StallGuard,
    disk_verdict,
    memory_verdict,
)
from aethercfd.runs.logparse import LogParser, parse_log_text
from aethercfd.runs.logtail import LogTail

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
BFS_LOG = Path(r"D:\OpenFOAM\backwardFacingStep2D\log.simpleFoam")
SANDIA_LOG = Path(r"D:\OpenFOAM\SandiaFlameD\log.reactingFoam")
OF14_LOG = FIX / "of14_cavity" / "log.foamRun"


def autofoam_history(log: str) -> dict[str, list[float]]:
    """Oracle: AutoFOAM's _parse_residuals (openfoam_agent/runner.py:45), initial-residual history."""
    history: dict[str, list[float]] = {}
    for line in log.splitlines():
        if "Solving for" in line and "Initial residual" in line:
            parts = line.split(",")
            field = parts[0].split("Solving for")[-1].strip().split()[0]
            for part in parts:
                if "Initial residual" in part:
                    history.setdefault(field, []).append(float(part.split("=")[-1].strip()))
    return history


@pytest.mark.skipif(not BFS_LOG.exists(), reason="corpus not available")
def test_golden_residuals_match_autofoam_on_bfs():
    text = BFS_LOG.read_text(encoding="utf-8", errors="replace")
    parser, steps = parse_log_text(text)
    oracle = autofoam_history(text)
    assert len(steps) == 2000 and parser.finished and not parser.fatal
    for fld in ("Ux", "Uy", "p", "k", "omega"):  # simpleFoam solves each field once per iteration
        assert [s.residuals[fld].initial for s in steps] == oracle[fld]


@pytest.mark.skipif(not SANDIA_LOG.exists(), reason="corpus not available")
def test_large_log_parses_fast_and_counts_repeated_solves():
    text = SANDIA_LOG.read_bytes().decode("utf-8", errors="replace")
    # CPU time of this process (not wall time): robust when the laptop is busy with other work
    t0 = time.process_time()
    parser, steps = parse_log_text(text)
    assert time.process_time() - t0 < 2.0
    assert len(steps) == 5000 and parser.finished
    s = steps[2]
    assert s.residuals["p"].n_solves == 3 and s.residuals["p"].initial == pytest.approx(0.01804323735)
    assert s.minmax["T"] == (291.0, 1880.0)


def test_foundation_time_suffix_and_courant_attach_to_next_step():
    parser, steps = parse_log_text(OF14_LOG.read_text(encoding="utf-8"))
    assert parser.header["Exec"] == "foamRun" and parser.header["Build"].startswith("14-")
    assert steps[0].time == pytest.approx(0.005) and steps[0].time_text == "0.005"
    # the Courant line printed after step 1's ExecutionTime belongs to step 2
    assert steps[1].co_max is not None and steps[1].co_max > 0
    assert all(s.exec_time is not None for s in steps[:-1])


@settings(max_examples=25, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=4000), min_size=1, max_size=30))
def test_chunking_does_not_change_results(cuts):
    text = OF14_LOG.read_text(encoding="utf-8")
    lines = text.split("\n")
    _, whole = parse_log_text(text)
    p = LogParser()
    got = []
    pos = 0
    for c in sorted(set(cuts)):
        got += p.feed(lines[pos:c])
        pos = c
    got += p.feed(lines[pos:])
    if (last := p.flush()) is not None:
        got.append(last)
    assert [(s.time_text, {k: v.initial for k, v in s.residuals.items()}, s.co_max) for s in got] == \
           [(s.time_text, {k: v.initial for k, v in s.residuals.items()}, s.co_max) for s in whole]


def test_fatal_detection_ignores_trapfpe_banner():
    log = ("trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).\nTime = 1\n"
           "#0  Foam::error::printStack(Foam::Ostream&) at ??:?\n"
           "#1  Foam::sigFpe::sigHandler(int) in /usr/lib/openfoam/openfoam2306/lib/libOpenFOAM.so\n"
           "Floating point exception (core dumped)\n")
    parser, _ = parse_log_text(log)
    assert [e.text[:3] for e in parser.fatal] == ["#1 ", "Flo"]


def test_logtail_partial_lines_and_truncation(tmp_path):
    p = tmp_path / "log.x"
    p.write_bytes(b"a\nb")
    t = LogTail(p)
    assert t.read_new() == ["a"]
    with open(p, "ab") as f:
        f.write(b"c\r\nd\n")
    assert t.read_new() == ["bc", "d"]
    p.write_bytes(b"new\n")  # replaced by a restart
    assert t.read_new() == ["new"] and t.resets == 1
    big = tmp_path / "big.log"
    big.write_bytes(b"x" * 5000 + b"\n")
    assert LogTail(big, start_at_end=True).read_new() == []


# ---------------------------------------------------------------- dat sources
def test_dat_sources_concatenate_restarts(tmp_path):
    pp = tmp_path / "postProcessing" / "solverInfo"
    (pp / "0").mkdir(parents=True)
    (pp / "0" / "solverInfo.dat").write_text(
        "# Solver information\n# Time\tp_solver\tp_initial\tp_final\n1\tGAMG\t1.0\t0.1\n2\tGAMG\t0.5\t0.05\n"
        "3\tGAMG\t0.4\t0.04\n", encoding="utf-8")
    (pp / "2").mkdir()
    (pp / "2" / "solverInfo.dat").write_text(
        "# Solver information\n# Time\tp_solver\tp_initial\tp_final\n3\tGAMG\t0.3\t0.03\n4\tGAMG\t0.2\t0.02\n",
        encoding="utf-8")
    d = find_residual_dat(tmp_path)
    assert d.source == "solverInfo" and d.series["p"] == [(1.0, 1.0), (2.0, 0.5), (3.0, 0.3), (4.0, 0.2)]


def test_dat_sources_foundation_residuals(tmp_path):
    pp = tmp_path / "postProcessing" / "residuals" / "0"
    pp.mkdir(parents=True)
    (pp / "residuals.dat").write_text("# Residuals\n# Time\tp\tUx\n0.005\t1\t1\n0.01\t0.14\tN/A\n", encoding="utf-8")
    d = find_residual_dat(tmp_path)
    assert d.source == "residuals" and d.series["p"] == [(0.005, 1.0), (0.01, 0.14)] and len(d.series["Ux"]) == 1


# ---------------------------------------------------------------- guards
def test_runaway_log_pattern_is_stopped():
    """Replay the 778 GB incident in miniature: a thermo-table warning printed every step."""
    g = RepetitionGuard()
    healthy = ["Time = {t}", "smoothSolver:  Solving for Ux, Initial residual = 1e-3, Final residual = 1e-6, "
               "No Iterations 2", "ExecutionTime = 1 s  ClockTime = 1 s", ""]
    for t in range(60):
        v = g.feed([ln.format(t=t) for ln in healthy])
    assert v.level is Level.OK and g.baseline_bytes_per_step is not None
    warn = "--> FOAM Warning : Temperature {T} out of range 200-3600 to use janaf table for specie N2"
    verdict = None
    for t in range(60, 70):
        verdict = g.feed([f"Time = {t}"] + [warn.format(T=3600 + i * 0.37) for i in range(600)])
    assert verdict.level is Level.STOP and "janaf" in verdict.detail["template"]


def test_log_growth_rate_and_size_caps():
    g = LogGrowthGuard(stop_after_s=20)
    v = None
    for i in range(40):  # 50 MB/s sustained
        v = g.sample(float(i), int(i * 50 * MB), free_bytes=1000 * GB)
    assert v.level is Level.STOP and v.guard == "log-rate"
    small_disk = LogGrowthGuard().sample(0.0, int(3 * GB), free_bytes=int(40 * GB))
    assert small_disk.level is Level.STOP and small_disk.guard == "log-size"  # 5% of free space
    calm = LogGrowthGuard()
    assert all(calm.sample(float(i), i * 10_000).level is Level.OK for i in range(100))


def test_disk_memory_and_stall():
    assert disk_verdict(15 * GB, 1900 * GB).level is Level.STOP  # below 10% / 20 GB reserve
    v = disk_verdict(300 * GB, 1900 * GB, bytes_per_write=2 * GB, writes_remaining=100)
    assert v.level is Level.WARN and "need" in v.detail
    assert disk_verdict(1300 * GB, 1900 * GB, bytes_per_write=GB, writes_remaining=10).level is Level.OK
    assert memory_verdict(int(0.5 * GB), 19 * GB).level is Level.STOP
    assert memory_verdict(10 * GB, 19 * GB).level is Level.OK
    s = StallGuard(window_s=60)
    assert s.sample(0, 1.0).level is Level.OK and s.sample(30, 1.0).level is Level.OK
    assert s.sample(90, 1.0).level is Level.REVIEW and s.sample(91, 1.1).level is Level.OK
