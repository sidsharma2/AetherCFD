"""The divergence guard (roadmap Q6): stops only on unambiguous signs, warns early, remembers the last good time."""

from __future__ import annotations

import math

from aethercfd.runs.guards import DivergenceGuard, Level
from aethercfd.runs.logparse import Residual, StepRecord, parse_log_text


def step(i: int, res: dict[str, float], **kw) -> StepRecord:
    r = {f: Residual(x, x * 1e-3, 3, "GAMG") for f, x in res.items()}
    return StepRecord(i, float(i), str(i), residuals=r, **kw)


def healthy(n: int, start: int = 1):
    """Steady residuals falling from 1 to ~1e-4."""
    return [step(i, {"p": 10 ** (-4 * min(1.0, i / n)), "Ux": 0.5 * 10 ** (-3 * min(1.0, i / n))})
            for i in range(start, start + n)]


def feed_all(g: DivergenceGuard, steps):
    return [g.feed(s) for s in steps]


def test_healthy_run_never_warns_and_tracks_last_ok():
    g = DivergenceGuard()
    out = feed_all(g, healthy(300))
    assert all(v.level is Level.OK for v in out)
    assert g.last_ok_time == 300.0


def test_nan_stops_at_once_with_the_last_good_time():
    g = DivergenceGuard()
    feed_all(g, healthy(50))
    v = g.feed(step(51, {"p": math.nan, "Ux": 0.1}))
    assert v.level is Level.STOP and v.guard == "divergence"
    assert "NaN" in v.message and v.detail["last_ok_time"] == 50.0
    assert g.feed(step(52, {"p": math.nan})).level is Level.OK  # reported once


def test_blow_up_warns_first_then_stops_after_sustain_steps():
    g = DivergenceGuard()
    feed_all(g, healthy(100))  # p min ~1e-4
    levels = []
    for i, x in enumerate([2e-2, 5e-2, 0.2, 0.3, 0.5, 0.7, 0.9, 1.5, 3.0, 8.0, 20.0], start=101):
        levels.append(g.feed(step(i, {"p": x, "Ux": 1e-3})))
    kinds = [v.level for v in levels]
    assert Level.WARN in kinds
    assert kinds.index(Level.WARN) < kinds.index(Level.STOP)
    stop = next(v for v in levels if v.level is Level.STOP)
    assert stop.detail["kind"] == "residual:p" and stop.detail["time"] == 110.0
    assert stop.detail["last_ok_time"] == 102.0  # the step before the residual started to grow


def test_a_single_spike_does_not_stop():
    g = DivergenceGuard()
    feed_all(g, healthy(100))
    assert g.feed(step(101, {"p": 2.0, "Ux": 1e-3})).level is Level.OK
    assert all(v.level is Level.OK for v in feed_all(g, healthy(50, start=102)))
    assert not g.tripped


def test_residual_growth_is_not_judged_during_warmup():
    g = DivergenceGuard(warmup=20)
    out = feed_all(g, [step(i, {"p": 1e-6 if i < 3 else 1.0}) for i in range(1, 15)])
    assert all(v.level is Level.OK for v in out)


def test_courant_and_temperature_limits():
    g = DivergenceGuard()
    assert g.feed(step(1, {"p": 0.1}, co_max=30.0)).level is Level.WARN
    assert g.feed(step(2, {"p": 0.1}, co_max=30.0)).level is Level.OK  # same sign, not repeated
    assert g.feed(step(3, {"p": 0.1}, co_max=2000.0)).level is Level.STOP
    g = DivergenceGuard()
    v = g.feed(step(1, {"p": 0.1}, minmax={"T": (-5.0, 900.0)}))
    assert v.level is Level.STOP and "T" in v.message


def test_high_courant_alone_keeps_the_last_good_time_moving():
    g = DivergenceGuard()
    for i in range(1, 10):
        g.feed(step(i, {"p": 0.1}, co_max=25.0))
    assert g.last_ok_time == 9.0


def test_warn_mode_never_stops():
    g = DivergenceGuard(stop_enabled=False)
    feed_all(g, healthy(30))
    v = g.feed(step(31, {"p": math.inf}))
    assert v.level is Level.WARN and "NaN or inf" in v.message
    assert not g.tripped


def test_bounding_streak_warns_but_does_not_stop():
    g = DivergenceGuard(bounding_steps=5)
    out = [g.feed(step(i, {"p": 0.1}, bounded={"epsilon": 1e-3})) for i in range(1, 8)]
    assert [v.level for v in out].count(Level.WARN) == 1
    assert not g.tripped and g.last_ok_time == 7.0


def test_velocity_limit_needs_a_reference_speed():
    assert DivergenceGuard().feed(step(1, {"p": 0.1}, minmax={"mag(U)": (0.0, 1e6)})).level is Level.OK
    g = DivergenceGuard(u_ref=10.0)
    assert g.feed(step(1, {"p": 0.1}, minmax={"mag(U)": (0.0, 150.0)})).level is Level.WARN
    assert g.feed(step(2, {"p": 0.1}, minmax={"mag(U)": (0.0, 1500.0)})).level is Level.STOP


def test_parser_records_bounded_fields_per_step():
    log = "\n".join([
        "Time = 1", "smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 0.01, No Iterations 2",
        "bounding epsilon, min: -1.2e-05 max: 3.4 average: 0.1", "bounding k, min: -2e-06 max: 0.2 average: 0.01",
        "ExecutionTime = 0.1 s  ClockTime = 0 s", "Time = 2",
        "smoothSolver:  Solving for Ux, Initial residual = nan, Final residual = nan, No Iterations 1000",
        "ExecutionTime = 0.2 s  ClockTime = 0 s"])
    _, steps = parse_log_text(log)
    assert steps[0].bounded == {"epsilon": 3.4, "k": 0.2} and steps[1].bounded == {}
    assert math.isnan(steps[1].residuals["Ux"].initial)


def test_bounded_field_blowing_up_stops_but_healthy_growth_does_not():
    g = DivergenceGuard()
    sizes = [20.0, 600.0, 4.7e3, 5e4, 2e4]  # what the healthy backward-facing step reaches (2e4 growth)
    assert all(g.feed(step(i, {"p": 0.5}, bounded={"k": s * 20})).level is Level.OK for i, s in enumerate(sizes, 1))
    g = DivergenceGuard()
    levels = [g.feed(step(i, {"p": 0.5}, bounded={"k": s})).level
              for i, s in enumerate([20.0, 611.0, 5e4, 5e7, 2e9, 7e11], 1)]
    assert levels[3] is Level.WARN and levels[4] is Level.STOP


def test_linear_solver_divergence_stops():
    g = DivergenceGuard()
    s = StepRecord(1, 1.0, "1", residuals={"Ux": Residual(0.716, 166.4, 1000, "smoothSolver")})
    v = g.feed(s)
    assert v.level is Level.STOP and v.detail["kind"] == "linear-solver:Ux"
    g = DivergenceGuard()
    assert g.feed(StepRecord(1, 1.0, "1", residuals={"p": Residual(1.0, 0.9, 1000, "GAMG")})).level is Level.OK
