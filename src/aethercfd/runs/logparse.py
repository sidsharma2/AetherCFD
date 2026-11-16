"""Streaming parser for OpenFOAM solver logs (ESI and Foundation; single- and multi-region).

Feed it lines in any chunking; it yields one :class:`StepRecord` per completed time step/iteration.
Lines that print *before* a ``Time =`` line (Courant number, deltaT) belong to that upcoming step.
Plotted residuals use the *first* solve of each field in a step (the standard convention); the number
of solves and the last final residual are kept too.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from aethercfd._vendor.log_scan import FATAL_LINE_PATTERNS, is_benign

_TIME = re.compile(r"^Time = ([-+0-9.eE]+)s?\s*$")
_SOLVE = re.compile(r"^(\S+):\s+Solving for (\S+), Initial residual = ([^,]+), Final residual = ([^,]+), "
                    r"No Iterations (\d+)")
_COURANT = re.compile(r"^(Interface )?Courant Number mean: (\S+) max: (\S+)")
_DELTAT = re.compile(r"^deltaT = (\S+)")
_CONT = re.compile(r"^time step continuity errors(?: \((\S+)\))? : sum local = ([^,]+), global = ([^,\s]+)"
                   r"(?:, cumulative = (\S+))?")
_EXEC = re.compile(r"^ExecutionTime = (\S+) s\s+ClockTime = (\S+) s")
_PIMPLE = re.compile(r"^PIMPLE: [Ii]teration (\d+)")
_MINMAX1 = re.compile(r"^min/max\(([^)]+)\) = ([^,]+), (\S+)")
_MINMAX2 = re.compile(r"^Min/max (\w+):\s*(\S+)\s+(\S+)")
_REGION = re.compile(r"^Solving for (?:fluid|solid) region (\S+)")
_BOUNDING = re.compile(r"^bounding ([^,\s]+), min: (\S+) max: (\S+)")
_HEADER = re.compile(r"^(Build|Exec|Date|Time|Host|PID|Case|nProcs|Arch)\s*:\s*(.*)$")


def _f(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return math.nan


@dataclass
class Residual:
    initial: float
    final: float
    iters: int
    solver: str
    n_solves: int = 1
    last_final: float = math.nan


@dataclass
class StepRecord:
    index: int
    time: float
    time_text: str
    delta_t: float | None = None
    co_mean: float | None = None
    co_max: float | None = None
    interface_co_max: float | None = None
    residuals: dict[str, Residual] = field(default_factory=dict)
    continuity: tuple[float, float, float] | None = None  # sum local, global, cumulative
    exec_time: float | None = None
    clock_time: float | None = None
    minmax: dict[str, tuple[float, float]] = field(default_factory=dict)
    outer_iters: int = 0
    bounded: dict[str, float] = field(default_factory=dict)  # field -> largest |min| or |max| in its "bounding" line


@dataclass
class LogEvent:
    kind: str  # fatal | warning | bounding | end
    line_no: int
    text: str


class LogParser:
    def __init__(self) -> None:
        self.header: dict[str, str] = {}
        self.steps_seen = 0
        self.line_no = 0
        self.events: list[LogEvent] = []
        self.finished = False
        self.warnings = 0
        self.bounding = 0
        self._cur: StepRecord | None = None
        self._pending: dict = {}
        self._region = ""
        self._in_header = True

    # ------------------------------------------------------------------ feeding
    def feed(self, lines: list[str]) -> list[StepRecord]:
        done: list[StepRecord] = []
        for line in lines:
            self.line_no += 1
            rec = self._line(line.rstrip("\r"))
            if rec is not None:
                done.append(rec)
        return done

    def flush(self) -> StepRecord | None:
        """Finish the current step (call at end of a finished log)."""
        return self._close()

    # ------------------------------------------------------------------ internals
    def _close(self) -> StepRecord | None:
        cur, self._cur = self._cur, None
        return cur

    def _line(self, s: str) -> StepRecord | None:
        if not s:
            return None
        c = s[0]
        if c == "T" and s.startswith("Time = "):
            m = _TIME.match(s)
            if m:
                out = self._close()
                self._in_header = False
                self._region = ""
                self._cur = StepRecord(self.steps_seen, _f(m.group(1)), m.group(1), **self._pending)
                self._pending = {}
                self.steps_seen += 1
                return out
        cur = self._cur
        if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" and ":  Solving for " in s:
            m = _SOLVE.match(s)
            if m and cur is not None:
                fld = f"{self._region}/{m.group(2)}" if self._region else m.group(2)
                r = cur.residuals.get(fld)
                if r is None:
                    cur.residuals[fld] = Residual(_f(m.group(3)), _f(m.group(4)), int(m.group(5)), m.group(1),
                                                  last_final=_f(m.group(4)))
                else:
                    r.n_solves += 1
                    r.last_final = _f(m.group(4))
                return None
        if c == "E":
            if s.startswith("ExecutionTime"):
                m = _EXEC.match(s)
                if m and cur is not None:
                    cur.exec_time, cur.clock_time = _f(m.group(1)), _f(m.group(2))
                    return self._close()
                return None
            if s.strip() == "End":
                self.finished = True
                self.events.append(LogEvent("end", self.line_no, s))
                return self._close()
        if c in "CI" and "Courant Number mean" in s:
            m = _COURANT.match(s)
            if m:
                target = self._pending if cur is None else None
                if m.group(1):  # interface Courant (VoF)
                    if target is not None:
                        target["interface_co_max"] = _f(m.group(3))
                    elif cur is not None:
                        cur.interface_co_max = _f(m.group(3))
                elif target is not None:
                    target["co_mean"], target["co_max"] = _f(m.group(2)), _f(m.group(3))
                elif cur is not None and cur.co_max is None:
                    cur.co_mean, cur.co_max = _f(m.group(2)), _f(m.group(3))
            return None
        if c == "d" and s.startswith("deltaT"):
            m = _DELTAT.match(s)
            if m:
                if cur is None:
                    self._pending["delta_t"] = _f(m.group(1))
                elif cur.delta_t is None:
                    cur.delta_t = _f(m.group(1))
            return None
        if c == "t" and s.startswith("time step continuity"):
            m = _CONT.match(s)
            if m and cur is not None:
                cur.continuity = (_f(m.group(2)), _f(m.group(3)), _f(m.group(4)) if m.group(4) else math.nan)
            return None
        if c == "S" and s.startswith("Solving for ") and " region " in s:
            m = _REGION.match(s)
            if m:
                self._region = m.group(1)
            return None
        if c == "P" and s.startswith("PIMPLE"):
            m = _PIMPLE.match(s)
            if m and cur is not None:
                cur.outer_iters = max(cur.outer_iters, int(m.group(1)))
            return None
        if (c in "mM" and s[1:8] == "in/max(") or s.startswith("Min/max "):
            m = _MINMAX1.match(s) or _MINMAX2.match(s)
            if m and cur is not None:
                cur.minmax[m.group(1)] = (_f(m.group(2)), _f(m.group(3)))
            return None
        if c == "-" and s.startswith("--> FOAM Warning"):
            self.warnings += 1
            self.events.append(LogEvent("warning", self.line_no, s))
            return None
        if c == "b" and s.startswith("bounding "):
            self.bounding += 1
            m = _BOUNDING.match(s)
            if m and cur is not None:
                name = f"{self._region}/{m.group(1)}" if self._region else m.group(1)
                size = max(abs(_f(m.group(2))), abs(_f(m.group(3))))
                cur.bounded[name] = max(cur.bounded.get(name, 0.0), size) if size == size else math.nan
            if self.bounding <= 50:
                self.events.append(LogEvent("bounding", self.line_no, s))
            return None
        if c in "-#FS" and not is_benign(s) and any(p.match(s) for p in FATAL_LINE_PATTERNS):
            self.events.append(LogEvent("fatal", self.line_no, s))
            return None
        if self._in_header:
            m = _HEADER.match(s)
            if m and m.group(1) not in self.header:
                self.header[m.group(1)] = m.group(2).strip()
        return None

    # ------------------------------------------------------------------ helpers
    @property
    def fatal(self) -> list[LogEvent]:
        return [e for e in self.events if e.kind == "fatal"]


def parse_log_text(text: str) -> tuple[LogParser, list[StepRecord]]:
    p = LogParser()
    steps = p.feed(text.split("\n"))
    last = p.flush()
    if last is not None:
        steps.append(last)
    return p, steps
