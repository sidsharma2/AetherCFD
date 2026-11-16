"""Run guards as pure functions of samples (so they are unit-testable without running a solver).

Why they exist: a single repeated warning per time step once grew a solver log to 778 GB, filled
D: to zero bytes and crashed WSL (see the disk-hygiene memory / ADR 0006). The run manager samples
these every few seconds; an in-distro ``guard.sh`` and ``ulimit -f`` are independent backstops.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    OK = 0
    REVIEW = 1  # worth a look, never acted on automatically
    WARN = 2
    STOP = 3  # the run manager stops the run (writing a final time if it can)


@dataclass(frozen=True)
class Verdict:
    level: Level
    guard: str
    message: str
    detail: dict = field(default_factory=dict)


OK = Verdict(Level.OK, "", "")
GB = 1024**3
MB = 1024**2


# ---------------------------------------------------------------------------- log growth
@dataclass
class LogGrowthGuard:
    warn_mb_s: float = 2.0
    stop_mb_s: float = 20.0
    warn_after_s: float = 30.0
    stop_after_s: float = 20.0
    hard_cap_bytes: int = 10 * GB
    free_fraction_cap: float = 0.05  # also never let one log exceed 5% of the free disk
    alpha: float = 0.3  # EWMA smoothing
    _last: tuple[float, int] | None = None
    rate: float = 0.0  # bytes/s (EWMA)
    _over_warn_since: float | None = None
    _over_stop_since: float | None = None

    def sample(self, t: float, size: int, free_bytes: int | None = None) -> Verdict:
        cap = self.hard_cap_bytes
        if free_bytes is not None:
            cap = min(cap, int((free_bytes + size) * self.free_fraction_cap))
        if size >= cap:
            return Verdict(Level.STOP, "log-size", f"log reached {size / GB:.2f} GB (limit {cap / GB:.2f} GB)",
                           {"size": size, "cap": cap})
        if self._last is not None:
            dt = t - self._last[0]
            if dt > 0:
                inst = max(0.0, (size - self._last[1]) / dt)
                self.rate = inst if self.rate == 0 else self.alpha * inst + (1 - self.alpha) * self.rate
        self._last = (t, size)
        mb_s = self.rate / MB
        if mb_s >= self.stop_mb_s:
            self._over_stop_since = self._over_stop_since or t
            if t - self._over_stop_since >= self.stop_after_s:
                return Verdict(Level.STOP, "log-rate", f"log growing {mb_s:.1f} MB/s for "
                               f"{t - self._over_stop_since:.0f} s", {"mb_s": mb_s})
        else:
            self._over_stop_since = None
        if mb_s >= self.warn_mb_s:
            self._over_warn_since = self._over_warn_since or t
            if t - self._over_warn_since >= self.warn_after_s:
                return Verdict(Level.WARN, "log-rate", f"log growing {mb_s:.1f} MB/s", {"mb_s": mb_s})
        else:
            self._over_warn_since = None
        return OK


# ---------------------------------------------------------------------------- repeated lines
_DIGITS = re.compile(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?")
# lines every healthy step prints; repetition of these is normal
_PER_STEP = re.compile(r"^(Time = |.*Solving for |Courant Number|deltaT = |ExecutionTime = |time step continuity"
                       r"|PIMPLE|DILUPBiCG|smoothSolver|GAMG|diagonal|min/max|Min/max|bounding |\s*$)")


def template(line: str) -> str:
    return _DIGITS.sub("#", line.strip())[:160]


@dataclass
class RepetitionGuard:
    """Flags a log dominated by one repeated, non-standard message (the 778 GB failure mode)."""

    window: int = 4000
    share: float = 0.6
    baseline_steps: int = 50
    growth_factor: float = 10.0
    _lines: deque = field(default_factory=lambda: deque(maxlen=4000))
    _counts: Counter = field(default_factory=Counter)
    _bytes_since_step: int = 0
    _step_bytes: list[int] = field(default_factory=list)
    baseline_bytes_per_step: float | None = None

    def feed(self, lines: list[str]) -> Verdict:
        for ln in lines:
            if ln.startswith("Time = "):
                self._step_bytes.append(self._bytes_since_step)
                self._bytes_since_step = 0
                if self.baseline_bytes_per_step is None and len(self._step_bytes) > self.baseline_steps:
                    vals = sorted(self._step_bytes[1:])
                    self.baseline_bytes_per_step = float(vals[len(vals) // 2]) or 1.0
            self._bytes_since_step += len(ln) + 1
            if _PER_STEP.match(ln):
                continue
            tpl = template(ln)
            if len(self._lines) == self._lines.maxlen:
                old = self._lines[0]
                self._counts[old] -= 1
                if self._counts[old] <= 0:
                    del self._counts[old]
            self._lines.append(tpl)
            self._counts[tpl] += 1
        if len(self._lines) < 200 or not self._counts:
            return OK
        tpl, n = self._counts.most_common(1)[0]
        frac = n / len(self._lines)
        if frac < self.share:
            return OK
        recent = [*self._step_bytes[-20:], self._bytes_since_step]
        per_step = max(recent) if recent else 0
        base = self.baseline_bytes_per_step
        detail = {"template": tpl, "share": round(frac, 3), "bytes_per_step": per_step, "baseline": base}
        if base is not None and per_step > self.growth_factor * base:
            return Verdict(Level.STOP, "repeated-message",
                           f"one message makes up {frac:.0%} of recent output and steps are "
                           f"{per_step / base:.0f}x their normal size: {tpl[:100]!r}", detail)
        return Verdict(Level.WARN, "repeated-message", f"repeated message ({frac:.0%} of recent output): "
                       f"{tpl[:100]!r}", detail)


# ---------------------------------------------------------------------------- disk
def disk_verdict(free: int, total: int, *, reserve_gb: float = 20.0, reserve_fraction: float = 0.10,
                 bytes_per_write: int | None = None, writes_remaining: int | None = None,
                 label: str = "disk") -> Verdict:
    reserve = max(int(reserve_gb * GB), int(total * reserve_fraction))
    if free <= reserve:
        return Verdict(Level.STOP, f"{label}-reserve", f"{label}: only {free / GB:.1f} GB free "
                       f"(reserve {reserve / GB:.0f} GB)", {"free": free, "reserve": reserve})
    if bytes_per_write and writes_remaining:
        need = bytes_per_write * writes_remaining
        if need > free - reserve:
            return Verdict(Level.WARN, f"{label}-projection",
                           f"{label}: the remaining {writes_remaining} writes need ~{need / GB:.1f} GB but only "
                           f"{(free - reserve) / GB:.1f} GB is available above the reserve",
                           {"need": need, "free": free, "reserve": reserve})
    return OK


# ---------------------------------------------------------------------------- memory
def memory_verdict(available: int, total: int, *, warn_fraction: float = 0.08, stop_fraction: float = 0.03,
                   label: str = "memory") -> Verdict:
    if total <= 0:
        return OK
    frac = available / total
    if frac <= stop_fraction:
        return Verdict(Level.STOP, label, f"{label}: only {available / GB:.1f} GB of {total / GB:.0f} GB free")
    if frac <= warn_fraction:
        return Verdict(Level.WARN, label, f"{label}: {available / GB:.1f} GB of {total / GB:.0f} GB free")
    return OK


# ---------------------------------------------------------------------------- stall
@dataclass
class StallGuard:
    """No simulated-time progress for a long wall-clock window -> review (never stops a run)."""

    window_s: float = 600.0
    _last_progress: tuple[float, float] | None = None  # (wall, sim_time)

    def sample(self, wall: float, sim_time: float | None) -> Verdict:
        if sim_time is None:
            return OK
        if self._last_progress is None or sim_time > self._last_progress[1]:
            self._last_progress = (wall, sim_time)
            return OK
        idle = wall - self._last_progress[0]
        if idle >= self.window_s:
            return Verdict(Level.REVIEW, "stall", f"no simulated-time progress for {idle / 60:.0f} min")
        return OK


# ---------------------------------------------------------------------------- divergence
def _bad(x: float | None) -> bool:
    return x is not None and not math.isfinite(x)


_NONNEG = {"k", "epsilon", "omega", "nuTilda", "nut", "alphat"}


@dataclass
class DivergenceGuard:
    """Watches a solver's parsed steps for divergence (roadmap Q6). Feed it each completed
    :class:`aethercfd.runs.logparse.StepRecord` (or anything with the same attributes).

    It stops a run only on unambiguous signs, because limits set too tight stop healthy transients:
    a NaN or inf anywhere in a step, a temperature at or below 0 K, a Courant number above ``co_stop``,
    a velocity above ``u_stop_factor`` times ``u_ref``, a bounded field whose out-of-bounds size has grown
    ``bound_stop_growth`` times since it first needed bounding, a linear solver whose final residual ends
    at or above ``final_stop`` and ten times its initial one, or an initial residual that is both at least
    ``res_stop_factor`` times its lowest value in the last ``res_window`` steps and at least
    ``res_stop_floor``, for ``sustain`` steps in a row. Earlier signs only warn: residual growth by ``res_warn_factor``, a high Courant number,
    a negative turbulence quantity, or one field bounding in ``bounding_steps`` steps in a row.

    ``last_ok_time`` is the time of the newest step with no sign of divergence. The restart plan rolls
    back to the newest written time at or before it.
    """

    warmup: int = 20  # residual growth is judged only after this many steps
    sustain: int = 3  # steps in a row for a residual stop
    warn_sustain: int = 5
    res_window: int = 500  # residual growth is measured against the lowest value in this many recent steps
    res_ignore_below: float = 1e-8
    res_warn_factor: float = 100.0
    res_warn_floor: float = 0.1
    res_stop_factor: float = 1e4
    res_stop_floor: float = 1.0
    co_warn: float = 20.0
    co_stop: float = 1000.0
    bounding_steps: int = 50
    bound_warn_growth: float = 1e6  # healthy logs reach 2e4 (backward-facing step, k)
    bound_stop_growth: float = 1e8
    final_stop: float = 1.0  # a final linear-solver residual >= 1 and >= 10x the initial one
    u_ref: float | None = None
    u_warn_factor: float = 10.0
    u_stop_factor: float = 100.0
    stop_enabled: bool = True  # False: every sign only warns (settings: guards.divergence = "warn")
    steps: int = 0
    last_ok_time: float | None = None
    last_ok_text: str | None = None
    first_time: float | None = None  # time of the first step this guard saw (where the run started)
    tripped: bool = False
    _win: dict = field(default_factory=dict)
    _over_warn: Counter = field(default_factory=Counter)
    _over_stop: Counter = field(default_factory=Counter)
    _bounding: Counter = field(default_factory=Counter)
    _bound_first: dict = field(default_factory=dict)
    _active: set = field(default_factory=set)

    def feed(self, step) -> Verdict:
        if self.tripped:
            return OK
        self.steps += 1
        if self.first_time is None:
            self.first_time = step.time
        stop: list[tuple[str, str, dict]] = []
        warn: list[tuple[str, str, dict]] = []
        soft: set[str] = set()  # warnings that do not move last_ok_time back (bounding, a high Courant number)
        at = f"at time {step.time_text}"

        bad = [f for f, r in step.residuals.items() if _bad(r.initial) or _bad(r.final)]
        bad += [n for n in ("co_max", "interface_co_max", "delta_t") if _bad(getattr(step, n, None))]
        if step.continuity and any(_bad(x) for x in step.continuity[:2]):
            bad.append("continuity")
        bad += [k for k, (lo, hi) in step.minmax.items() if _bad(lo) or _bad(hi)]
        bad += [f"bounding {k}" for k, size in (getattr(step, "bounded", None) or {}).items() if _bad(size)]
        if bad:
            stop.append(("nan", f"NaN or inf in {', '.join(bad[:4])} {at}", {"fields": bad}))
        blown = [(f, r) for f, r in step.residuals.items() if math.isfinite(r.initial) and math.isfinite(r.final)
                 and r.final >= self.final_stop and r.final >= 10 * r.initial]
        if blown:
            f, r = blown[0]
            stop.append((f"linear-solver:{f}", f"the {f} linear solver diverged (residual {r.initial:.3g} -> "
                         f"{r.final:.3g} in {r.iters} iterations) {at}", {"field": f, "initial": r.initial,
                                                                          "final": r.final}))

        for f, r in step.residuals.items():
            x = r.initial
            if _bad(x):
                continue
            win = self._win.setdefault(f, deque())
            m = win[0][1] if win else None
            if m is not None and self.steps > self.warmup:
                ratio = x / m
                self._over_stop[f] = self._over_stop[f] + 1 if (ratio >= self.res_stop_factor
                                                                 and x >= self.res_stop_floor) else 0
                self._over_warn[f] = self._over_warn[f] + 1 if (ratio >= self.res_warn_factor
                                                                 and x >= self.res_warn_floor) else 0
                detail = {"field": f, "residual": x, "min": m, "ratio": ratio}
                if self._over_stop[f] >= self.sustain:
                    stop.append((f"residual:{f}", f"{f} initial residual {x:.3g} is {ratio:.0e} times its "
                                 f"lowest recent value ({m:.2e}) {at}", detail))
                elif self._over_warn[f] >= self.warn_sustain:
                    warn.append((f"residual:{f}", f"{f} initial residual {x:.3g} is {ratio:.0f} times its "
                                 f"lowest recent value ({m:.2e}) {at}", detail))
            if x >= self.res_ignore_below:  # a residual this small means there was nothing to solve
                while win and win[-1][1] >= x:  # sliding-window minimum (monotonic deque)
                    win.pop()
                win.append((self.steps, x))
            while win and win[0][0] <= self.steps - self.res_window:
                win.popleft()

        co = getattr(step, "co_max", None)
        if co is not None and math.isfinite(co):
            if co >= self.co_stop:
                stop.append(("courant", f"Courant number {co:.3g} {at}", {"co_max": co}))
            elif co >= self.co_warn:
                warn.append(("courant", f"Courant number {co:.3g} {at}", {"co_max": co}))
                soft.add("courant")

        for k, (lo, hi) in step.minmax.items():
            base = k.rsplit("/", 1)[-1]
            if _bad(lo) or _bad(hi):
                continue
            if base == "T" and lo <= 0:
                stop.append((f"range:{k}", f"{k} minimum {lo:.3g} K {at}", {"field": k, "min": lo}))
            elif base in _NONNEG and lo < 0:
                warn.append((f"range:{k}", f"{k} minimum {lo:.3g} is negative {at}", {"field": k, "min": lo}))
            elif base in ("U", "mag(U)") and self.u_ref:
                if hi >= self.u_stop_factor * self.u_ref:
                    stop.append((f"range:{k}", f"{k} maximum {hi:.3g} is {hi / self.u_ref:.0f} times the "
                                 f"reference speed {at}", {"field": k, "max": hi}))
                elif hi >= self.u_warn_factor * self.u_ref:
                    warn.append((f"range:{k}", f"{k} maximum {hi:.3g} is {hi / self.u_ref:.0f} times the "
                                 f"reference speed {at}", {"field": k, "max": hi}))

        bounded = dict(getattr(step, "bounded", None) or {})
        for f in list(self._bounding):
            if f not in bounded:
                del self._bounding[f]
        for f, size in bounded.items():
            if _bad(size):
                continue
            first = self._bound_first.setdefault(f, max(size, 1e-30))
            growth = size / first
            if growth >= self.bound_stop_growth:
                stop.append((f"bounding-growth:{f}", f"{f} is out of bounds by {size:.3g}, {growth:.0e} times "
                             f"the first time it needed bounding {at}", {"field": f, "size": size, "first": first}))
            elif growth >= self.bound_warn_growth:
                warn.append((f"bounding-growth:{f}", f"{f} is out of bounds by {size:.3g}, {growth:.0e} times "
                             f"the first time it needed bounding {at}", {"field": f, "size": size, "first": first}))
            self._bounding[f] += 1
            if self._bounding[f] >= self.bounding_steps:
                warn.append((f"bounding:{f}", f"{f} needed bounding in {self._bounding[f]} steps in a row {at}",
                             {"field": f, "steps": self._bounding[f]}))
                soft.add(f"bounding:{f}")

        base_detail = {"time": step.time, "time_text": step.time_text, "step": self.steps}
        if stop and self.stop_enabled:
            self.tripped = True
            key, msg, detail = stop[0]
            return Verdict(Level.STOP, "divergence", msg,
                           {**detail, **base_detail, "kind": key, "last_ok_time": self.last_ok_time,
                            "last_ok_text": self.last_ok_text, "first_time": self.first_time,
                            "all": [s[0] for s in stop]})
        warn = stop + warn  # stopping is off: stop signs are reported as warnings
        growing = any(self._over_warn.values()) or any(self._over_stop.values())
        if step.residuals and not growing and all(k in soft for k, _, _ in warn):  # an empty step proves nothing
            self.last_ok_time, self.last_ok_text = step.time, step.time_text
        keys = {k for k, _, _ in warn}
        new = [w for w in warn if w[0] not in self._active]
        self._active = keys  # a sign that clears and comes back is reported again
        if not new:
            return OK
        key, msg, detail = new[0]
        more = f" (and {len(new) - 1} more)" if len(new) > 1 else ""
        return Verdict(Level.WARN, "divergence", f"possible divergence: {msg}{more}",
                       {**detail, **base_detail, "kind": key, "last_ok_time": self.last_ok_time,
                        "last_ok_text": self.last_ok_text})


def worst(*verdicts: Verdict) -> Verdict:
    return max(verdicts, key=lambda v: v.level, default=OK)
