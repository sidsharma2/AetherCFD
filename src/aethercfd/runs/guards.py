"""Run guards as pure functions of samples (so they are unit-testable without running a solver).

Why they exist: a single repeated warning per time step once grew a solver log to 778 GB, filled
D: to zero bytes and crashed WSL (see the disk-hygiene memory / ADR 0006). The run manager samples
these every few seconds; an in-distro ``guard.sh`` and ``ulimit -f`` are independent backstops.
"""

from __future__ import annotations

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


def worst(*verdicts: Verdict) -> Verdict:
    return max(verdicts, key=lambda v: v.level, default=OK)
