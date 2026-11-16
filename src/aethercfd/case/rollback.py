"""Restart a diverged run from its last good time, more gently (roadmap Q6).

When the divergence guard stops a run it records the last time with no sign of divergence. This module
finds the newest written time at or before it whose fields hold no NaN or inf, and builds one plan:

* ``startFrom startTime`` at that time (nothing is deleted: the later time folders stay where they are);
  a run that diverged in its first 50 steps restarts from the time it started from instead;
* steady cases: every under-relaxation factor times 0.7, and with plain SIMPLE never above p 0.3 and
  0.7 for the equations (SIMPLEC keeps its unrelaxed pressure);
* transient cases: ``maxCo`` times 0.5 with an adaptive time step, otherwise ``deltaT`` times 0.5;
* optionally, first-order upwind convection for every transported field;
* ``purgeWrite`` raised to 3 when it keeps fewer writes, so the next divergence still has a good time.

The edits are ``dict.batch`` edits: shown as one plan, approved, journalled and revertable like any other
change. No Qt here.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.case.checks import CONVECTION, _load, facts
from aethercfd.case.model import Case, load_case

RELAX = 0.7
COURANT = 0.5
EARLY_STEPS = 50  # a run that diverges this soon restarts from its own start, not from what it wrote
MAX_SCAN_BYTES = 64 * 2**20  # per field file; larger files are not scanned (their state stays unknown)

_NAN = re.compile(rb"(?<![A-Za-z_])[-+]?(nan|inf|infinity)(?![A-Za-z_0-9])", re.IGNORECASE)
_BINARY = re.compile(rb"\bformat\s+binary\s*;")
_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


@dataclass
class TimeDir:
    name: str
    value: float
    bad: bool | None = None  # True: a field holds NaN/inf; False: checked clean; None: not checked (binary, large)


@dataclass
class RestartPlan:
    start: TimeDir | None
    edits: list[dict] = field(default_factory=list)  # dict.batch edits (absolute file paths)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    later: list[str] = field(default_factory=list)  # time folders after the restart time, left in place

    def batch_params(self) -> dict:
        when = self.start.name if self.start else "?"
        return {"edits": self.edits, "summary": f"Restart from the last good time {when}: "
                + "; ".join(self.notes[:3])}


def _times_root(case_root: Path) -> Path:
    """Where the time folders are: the case, or processor0 for a decomposed run with no reconstructed times."""
    own = [p for p in case_root.iterdir() if p.is_dir() and _NUM.match(p.name) and p.name != "0"]
    proc = case_root / "processor0"
    if not own and proc.is_dir():
        return proc
    return case_root


def time_dirs(case_root: Path) -> list[TimeDir]:
    root = _times_root(case_root)
    out = []
    for p in root.iterdir():
        if p.is_dir() and _NUM.match(p.name):
            out.append(TimeDir(p.name, float(p.name)))
    return sorted(out, key=lambda t: t.value)


def field_is_bad(path: Path) -> bool | None:
    """True if a field file holds a NaN or inf, False if it is clean, None if it cannot be read as text."""
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return None
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    head = data[:4096]
    if _BINARY.search(head):
        return None
    body = data[data.find(b"}") + 1:] if b"FoamFile" in head else data  # skip the header (it can say "inf"o)
    return _NAN.search(body) is not None


def time_is_bad(case_root: Path, t: TimeDir) -> bool | None:
    d = _times_root(case_root) / t.name
    results = [field_is_bad(p) for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]
    if any(r is True for r in results):
        return True
    if results and all(r is False for r in results):
        return False
    return None


def restart_time(case_root: Path, last_ok_time: float | None) -> tuple[TimeDir | None, list[TimeDir]]:
    """The newest written time at or before ``last_ok_time`` with no NaN/inf in its fields, and the times after it.
    With no last good time (the run diverged at once), only the initial time qualifies."""
    times = time_dirs(case_root)
    limit = times[0].value if last_ok_time is None and times else (last_ok_time if last_ok_time is not None else -1)
    eps = 1e-9 * max(1.0, abs(limit))
    chosen = None
    for t in reversed(times):
        if t.value > limit + eps:
            continue
        t.bad = time_is_bad(case_root, t)
        if t.bad is not True:
            chosen = t
            break
    later = [t for t in times if chosen is not None and t.value > chosen.value]
    return chosen, later


def _before(case_root: Path, first_step_time: float) -> float | None:
    """The newest written time strictly before a run's first step: the time the run started from."""
    eps = 1e-9 * max(1.0, abs(first_step_time))
    earlier = [t.value for t in time_dirs(case_root) if t.value < first_step_time - eps]
    return max(earlier) if earlier else None


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    s = text.strip().rstrip(";").strip()
    return float(s) if _NUM.match(s) else None


def _fmt(x: float) -> str:
    return f"{float(f'{x:.3g}'):g}"


def _edit(case: Case, rel: str, key: str, value: str, *, insert: bool = False) -> dict:
    return {"file": str(case.root / rel), "key": key, "value": value, "insert_if_missing": insert,
            "allow_nonliteral": False}


def plan_restart(case: Case | str | Path, divergence: dict | None, *, first_order: bool = False) -> RestartPlan:
    case = case if isinstance(case, Case) else load_case(case)
    div = divergence or {}
    last_ok = div.get("last_ok_time")
    step, first = div.get("step"), div.get("first_time")
    early = step is not None and step <= EARLY_STEPS and first is not None
    if early:
        # it never got healthy: what it wrote is finite but already polluted (on OpenFOAM 14 pitzDaily, a
        # restart from such a write ran 2000 iterations without converging; one from the start converged)
        last_ok = _before(case.root, first)
    start, later = restart_time(case.root, last_ok)
    plan = RestartPlan(start)
    if early and start is not None:
        plan.notes.append(f"the run diverged in its first {step} steps: restart from the fields it started with")
    if start is None:
        plan.errors.append("no written time to restart from" if not time_dirs(case.root) else
                           "every written time at or before the last good time holds NaN or inf")
        return plan
    plan.later = [t.name for t in later]
    plan.edits += [_edit(case, "system/controlDict", "startFrom", "startTime", insert=True),
                   _edit(case, "system/controlDict", "startTime", start.name, insert=True)]
    plan.notes.append(f"restart from time {start.name}")
    if start.bad is None:
        plan.notes.append(f"the fields at {start.name} could not be checked for NaN (binary or very large files)")
    f = facts(case)
    cd = _load(case, "system/controlDict")
    if f.steady:
        _relax(case, plan)
    elif cd is not None:
        adjustable = (cd.get("adjustTimeStep")[0] or "").strip() in ("yes", "true", "on")
        key = "maxCo" if adjustable else "deltaT"
        cur = _num(cd.get(key)[0])
        if cur is not None and cur > 0:
            plan.edits.append(_edit(case, "system/controlDict", key, _fmt(cur * COURANT)))
            plan.notes.append(f"{key} {_fmt(cur)} → {_fmt(cur * COURANT)}")
        else:
            plan.notes.append(f"{key} is not a plain number in controlDict: set a smaller one by hand")
    if first_order:
        _upwind(case, f.steady, plan)
    if cd is not None:
        purge = _num(cd.get("purgeWrite")[0])
        if purge is not None and 0 < purge < 3:
            plan.edits.append(_edit(case, "system/controlDict", "purgeWrite", "3"))
            plan.notes.append(f"purgeWrite {purge:g} → 3 (keep enough writes to roll back again)")
    if plan.later:
        plan.notes.append(f"{len(plan.later)} later time folder(s) left in place ({', '.join(plan.later[:4])}"
                          + (", …" if len(plan.later) > 4 else "") + "); the restart writes over the ones it reaches")
    return plan


def _relax(case: Case, plan: RestartPlan) -> None:
    sol = _load(case, "system/fvSolution")
    if sol is None:
        plan.errors.append("system/fvSolution is missing")
        return
    simplec = (sol.get("SIMPLE/consistent")[0] or "").strip() in ("yes", "true", "on")
    changed = []
    groups = [g for g in ("fields", "equations") if sol.view.dict_at(f"relaxationFactors/{g}") is not None]
    keys = [f"relaxationFactors/{g}/{k}" for g in groups for k in sol.keys(f"relaxationFactors/{g}")]
    keys += [f"relaxationFactors/{k}" for k in sol.keys("relaxationFactors") if k not in ("fields", "equations")]
    for key in keys:
        if simplec and key.startswith("relaxationFactors/fields/") and key.rsplit("/", 1)[-1] in ("p", "p_rgh"):
            continue  # SIMPLEC runs with the pressure unrelaxed
        val, prov = sol.get(key)
        x = _num(val)
        if x is None or prov != "literal" or not 0 < x <= 1:
            continue
        cap = 1.0 if simplec else (0.3 if key.rsplit("/", 1)[-1] in ("p", "p_rgh") else 0.7)
        new = max(0.05, min(x * RELAX, cap))  # plain SIMPLE: never above the textbook p 0.3, U 0.7
        plan.edits.append(_edit(case, "system/fvSolution", key, _fmt(new)))
        changed.append(f"{key.rsplit('/', 1)[-1]} {_fmt(x)} → {_fmt(new)}")
    if changed:
        plan.notes.append("relaxation " + ", ".join(changed[:4]) + (" …" if len(changed) > 4 else ""))
    elif sol.view.dict_at("relaxationFactors") is None:
        body = "{ " + ("" if simplec else "fields { p 0.3; } ") + "equations { U 0.5; \".*\" 0.5; } }"
        plan.edits.append(_edit(case, "system/fvSolution", "relaxationFactors", body, insert=True))
        plan.notes.append("relaxation added (equations 0.5" + ("" if simplec else ", p 0.3") + ")")
    else:
        plan.notes.append("no plain relaxation factors to lower: lower them by hand")


def _upwind(case: Case, steady: bool, plan: RestartPlan) -> None:
    sch = _load(case, "system/fvSchemes")
    if sch is None:
        return
    n = 0
    for k in sch.keys("divSchemes"):
        if not CONVECTION.match(k):
            continue
        val, prov = sch.get(f"divSchemes/{k}")
        if val is None or prov != "literal" or val.split()[-1] == "upwind":
            continue
        new = ("bounded " if steady and val.strip().startswith("bounded") else "") + "Gauss upwind"
        plan.edits.append(_edit(case, "system/fvSchemes", f"divSchemes/{k}", new))
        n += 1
    if n:
        plan.notes.append(f"first-order upwind convection for {n} field(s)")

