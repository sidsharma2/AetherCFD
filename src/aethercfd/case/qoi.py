"""Quantities of interest (QoIs): what a run is FOR, monitored while it runs and judged when it ends.

A case's QoIs follow from its patches and physics, with no user input needed:

* walls -> the total force on them (pressure + viscous), per component (``forces``);
* every open patch (inlet, outlet, opening) -> its flow rate, the sum of ``phi`` (volumetric in an
  incompressible solver, mass flow in a compressible one), and its area-mean pressure (and T);
* walls of a case with an energy equation -> the heat flow through each wall (``wallHeatFlux``);
* one inlet and one outlet -> the pressure drop between them (derived);
* any other function object the case already writes (forceCoeffs, user surfaceFieldValues ...) -> its scalar
  columns, named ``<object>/<file>:<column>`` like the grid study does.

:func:`plan` lists the QoIs and the function objects that produce them, written for the case's fork (the
syntax was taken from each fork's own etc/caseDicts and checked by running both: ESI v2306 selects a patch
with ``regionType patch; name <p>`` and writes ``force.dat`` with total/pressure/viscous triplets; the
Foundation (v14) uses ``patch <p>`` and writes ``forces.dat`` as ``((Fp) (Fv)) ((Mp) (Mv))``, and reads
``system/functions`` instead of ``controlDict/functions`` when that file exists). :func:`read_series` reads
``postProcessing/<object>/<startTime>/*.dat``, restarts concatenated. Two gates judge the run with them:
:func:`convergence_gate` (has each QoI stopped changing?) and :func:`conservation_gates` (does the mass that
enters leave; in a closed heated box, does the heat that enters leave?).

No Qt here; nothing here writes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.case import gates as G
from aethercfd.case.checks import CaseFacts, facts
from aethercfd.case.model import Case, load_case

PREFIX = "aether"
FORCES = f"{PREFIX}Forces"
HEAT = f"{PREFIX}WallHeatFlux"
DEFAULT_TOL_PCT = 1.0
MIN_SAMPLES = 20
# not quantities of interest: residuals, field extremes, y+ (see gridstudy._NOT_QOI), geometry output
_SKIP_OBJ = {"solverInfo", "residuals", "fieldMinMax", "yPlus", "continuityError", "Co", "CourantNo", "streamlines",
             "sets", "surfaces", "cuttingPlane", "probes", "sample"}
_SKIP_FILE = {"solverInfo", "residuals", "fieldMinMax", "yPlus", "continuityError", "moment", "moments"}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf)", re.I)
_MAX_BYTES = 64 * 2**20


@dataclass
class QoI:
    id: str  # force:Fx | flow:<patch> | mean:<field>:<patch> | heat:<patch> | dp | <object>/<file>:<column>
    title: str
    unit: str
    group: str  # force | flow | mean | heat | derived | other
    obj: str = ""  # the function object that writes it
    patches: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        d = {"id": self.id, "title": self.title, "unit": self.unit, "group": self.group}
        if self.obj:
            d["object"] = self.obj
        if self.patches:
            d["patches"] = list(self.patches)
        return d


@dataclass
class QoIPlan:
    qois: list[QoI] = field(default_factory=list)
    objects: dict[str, str] = field(default_factory=dict)  # function-object name -> its dictionary text
    open_patches: list[str] = field(default_factory=list)
    walls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, QoI]:
        return {q.id: q for q in self.qois}


def fork_is_esi(case: Case) -> bool:
    """ESI (openfoam.com) vs Foundation (openfoam.org) dictionary syntax; an 'application' entry means ESI or
    an older Foundation release, which reads the same function-object keywords."""
    return (case.fork_guess.family if case.fork_guess else "") == "esi" or bool(case.application)


def libs_text(esi: bool, lib: str) -> str:
    """The ``libs`` entry: ESI takes a word list, Foundation 14 only a string (``libs (fieldFunctionObjects)`` is a
    FATAL IO ERROR there: 'wrong token type - expected string')."""
    return f"({lib})" if esi else f'("lib{lib}.so")'


def _select(esi: bool, patch: str) -> str:
    return f"regionType patch; name {patch};" if esi else f"patch {patch};"


def _multiphase(fields: dict) -> bool:
    return any(n.startswith("alpha.") for n in fields)


def _moving(U, patch) -> bool:
    """A wall that drives the flow (a lid, a rotor): a moving-wall type or a fixed, non-zero velocity."""
    t = U.patch_entry(patch, "type") or ""
    if t in ("movingWallVelocity", "rotatingWallVelocity", "movingWallSlipVelocity"):
        return True
    vec = G._uniform_vector(U.patch_entry(patch, "value")) if t == "fixedValue" else None
    return bool(vec and any(vec))


def inlet_outlet(case: Case, fields: dict) -> tuple[list[str], list[str]]:
    """Inlets (a fixed, non-zero velocity or a flow rate) and outlets (a fixed pressure), by their 0/ fields."""
    U = fields.get("U")
    p = fields.get("p_rgh") or fields.get("p")
    ins, outs = [], []
    for pt in G._flow_patches(case):
        if pt.type == "wall":
            continue
        ut = (U.patch_entry(pt, "type") or "") if U else ""
        pt_ = (p.patch_entry(pt, "type") or "") if p else ""
        vec = G._uniform_vector(U.patch_entry(pt, "value")) if U and ut == "fixedValue" else None
        if ut in ("flowRateInletVelocity", "surfaceNormalFixedValue") or (vec and any(vec)):
            ins.append(pt.name)
        elif pt_ in ("fixedValue", "totalPressure", "prghPressure", "prghTotalPressure", "fixedMean"):
            outs.append(pt.name)
    return ins, outs


def plan(case: Case | str | Path, answers: dict | None = None, f: CaseFacts | None = None,
         fields: dict | None = None) -> QoIPlan:
    """The case's QoIs and the function objects (for its fork) that write them."""
    case = case if isinstance(case, Case) else load_case(case)
    a = answers or {}
    f = f or facts(case)
    fields = fields if fields is not None else G.initial_fields(case)
    out = QoIPlan()
    if len(case.regions) > 1:
        out.notes.append("multi-region case: integral monitors are not generated yet (one region per case only)")
        return out
    esi = fork_is_esi(case)
    common = "writeControl timeStep; writeInterval 1; log false;"
    walls = [p.name for p in G._wall_patches(case)]
    opens = [p.name for p in G._flow_patches(case) if p.type != "wall"]
    out.walls, out.open_patches = walls, opens
    variable_rho = f.compressible or _multiphase(fields)
    U = fields.get("U")
    moving = [p.name for p in G._wall_patches(case) if U is not None and _moving(U, p)]
    # in a closed box with still walls the wall force is just the fluid's weight: not a quantity of interest
    if walls and U is not None and (opens or moving):
        if variable_rho:
            rho, funit = "rho rho;", "N"
        else:
            dens = G.FLUIDS.get(a.get("fluid") or "", (None, None))[1]
            if dens:
                rho, funit = f"rho rhoInf; rhoInf {dens:g};", "N"
                out.notes.append(f"forces use ρ = {dens:g} kg/m³ ({a.get('fluid')}): the solver's p is p/ρ")
            else:
                rho, funit = "rho rhoInf; rhoInf 1;", "m⁴/s² (force/ρ)"
                out.notes.append("forces are per unit density (ρ∞ = 1): give the fluid in the Materials stage to get "
                                 "newtons")
        out.objects[FORCES] = (f"{{ type forces; libs {libs_text(esi, 'forces')}; patches ({' '.join(walls)}); {rho} "
                               f"CofR (0 0 0); {common} }}")
        for c in "xyz":
            out.qois.append(QoI(f"force:F{c}", f"Force on the walls, {c}", funit, "force", FORCES, tuple(walls)))
    if "U" in fields and opens:
        funit = "kg/s" if f.compressible else "m³/s"
        pname = "p" if "p" in fields else ("p_rgh" if "p_rgh" in fields else None)
        punit = "Pa" if f.compressible or _multiphase(fields) else "m²/s² (p/ρ)"
        means = [x for x in (pname, "T" if "T" in fields and f.compressible else None) if x]
        sfv = f"{{ type surfaceFieldValue; libs {libs_text(esi, 'fieldFunctionObjects')}; {common} writeFields false;"
        for pt in opens:
            out.objects[f"{PREFIX}Flow_{pt}"] = f"{sfv} {_select(esi, pt)} operation sum; fields (phi); }}"
            out.qois.append(QoI(f"flow:{pt}", f"Flow rate through {pt} (out +)", funit, "flow",
                                f"{PREFIX}Flow_{pt}", (pt,)))
            if means:
                out.objects[f"{PREFIX}Mean_{pt}"] = (f"{sfv} {_select(esi, pt)} operation areaAverage; "
                                                     f"fields ({' '.join(means)}); }}")
                for m in means:
                    out.qois.append(QoI(f"mean:{m}:{pt}", f"Mean {m} on {pt}", "K" if m == "T" else punit, "mean",
                                        f"{PREFIX}Mean_{pt}", (pt,)))
        ins, outs = inlet_outlet(case, fields)
        if pname and len(ins) == 1 and len(outs) == 1:
            out.qois.append(QoI("dp", f"Pressure drop {ins[0]} → {outs[0]} ({pname})", punit, "derived",
                                patches=(ins[0], outs[0])))
    if walls and "T" in fields and f.compressible:
        out.objects[HEAT] = (f"{{ type wallHeatFlux; libs {libs_text(esi, 'fieldFunctionObjects')}; {common} "
                             "executeControl timeStep; executeInterval 1; }")
        for w in walls:
            out.qois.append(QoI(f"heat:{w}", f"Heat flow into the fluid through {w}", "W", "heat", HEAT, (w,)))
    return out


def has_monitors(case: Case) -> bool:
    texts = [G._read(case.root / "system/controlDict") or "", G._read(case.root / "system/functions") or ""]
    return any(f"{PREFIX}Flow_" in t or FORCES in t for t in texts)


# ============================================================================ reading the output
Series = list[tuple[float, float]]


def _time_dirs(obj: Path) -> list[Path]:
    out = []
    for t in obj.iterdir():
        if t.is_dir():
            try:
                out.append((float(t.name), t))
            except ValueError:
                continue
    return [t for _v, t in sorted(out)]


def _rows(dat: Path) -> tuple[list[str], list[str]]:
    """(header columns, complete data lines). Only newline-terminated lines count: OpenFOAM may be writing
    the last one (cfd_agents' LIVE_TELEMETRY_PARTIAL_ROW_RACE)."""
    try:
        size = dat.stat().st_size
        with dat.open("rb") as fh:
            if size > _MAX_BYTES:
                fh.seek(size - _MAX_BYTES // 2)
                fh.readline()
            text = fh.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return [], []
    lines = text.split("\n")
    lines.pop()  # "" after the final newline, or a partial row
    header: list[str] = []
    data = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            cols = [c.strip() for c in s.lstrip("#").split("\t") if c.strip()]
            if cols and cols[0] == "Time" and not header:
                header = cols
            continue
        data.append(s)
    return header, data


def _nums(text: str) -> list[float]:
    return [float(x) for x in _NUM.findall(text)]


def _merge(chunks: list[tuple[float, dict[str, Series]]]) -> dict[str, Series]:
    """Concatenate restarts in start-time order; a later restart replaces everything from its start on."""
    out: dict[str, Series] = {}
    for start, part in chunks:
        for k, s in part.items():
            prev = [x for x in out.get(k, []) if x[0] < start]
            out[k] = prev + s
    return out


def _read_forces(dat: Path) -> dict[str, Series]:
    """ESI force.dat: total(3) pressure(3) viscous(3); Foundation forces.dat: ((Fp) (Fv)) ((Mp) (Mv))."""
    _h, rows = _rows(dat)
    out: dict[str, Series] = {}
    for r in rows:
        n = _nums(r)
        if len(n) == 10:  # time + 9
            fx, fy, fz = n[1:4]
        elif len(n) == 13:  # time + 12
            fx, fy, fz = (n[1 + i] + n[4 + i] for i in range(3))
        else:
            continue
        for c, v in zip("xyz", (fx, fy, fz), strict=True):
            out.setdefault(f"F{c}", []).append((n[0], v))
    return out


def _read_columns(dat: Path) -> dict[str, Series]:
    """A scalar table: header 'Time <col> <col> ...' (tab-separated), one value per column."""
    header, rows = _rows(dat)
    out: dict[str, Series] = {}
    for r in rows:
        n = _nums(r.replace("(", " ").replace(")", " "))
        if len(n) < 2:
            continue
        cols = header[1:] if header and len(header) == len(n) else [f"col{i}" for i in range(1, len(n))]
        for c, v in zip(cols, n[1:], strict=False):
            out.setdefault(c, []).append((n[0], v))
    return out


def _read_heat(dat: Path) -> dict[str, Series]:
    """wallHeatFlux.dat: 'Time patch min max integral' (ESI) / 'Time patch min max Q q' (Foundation)."""
    header, rows = _rows(dat)
    qi = next((i for i, c in enumerate(header) if c == "integral" or c.split()[0] == "Q"), 4)
    out: dict[str, Series] = {}
    for r in rows:
        parts = r.split()
        if len(parts) <= qi:
            continue
        try:
            out.setdefault(parts[1], []).append((float(parts[0]), float(parts[qi])))
        except ValueError:
            continue
    return out


def _clean(series: dict[str, Series]) -> dict[str, Series]:
    """Drop the time-0 row the Foundation writes before the first solve, and non-finite values."""
    out = {}
    for k, s in series.items():
        s = [(t, v) for t, v in s if math.isfinite(v)]
        if len(s) > 1 and s[0][0] == 0.0:
            s = s[1:]
        if s:
            out[k] = s
    return out


_CACHE: dict[str, tuple[tuple, dict[str, Series]]] = {}


def _signature(pp: Path) -> tuple:
    """(path, size, mtime) of every .dat file: the workflow re-assesses on every file change, so an unchanged
    postProcessing tree is read once."""
    sig = []
    for dat in pp.glob("*/*/*.dat"):
        try:
            st = dat.stat()
        except OSError:
            continue
        sig.append((str(dat), st.st_size, st.st_mtime_ns))
    return tuple(sorted(sig))


def read_series(case_root: str | Path, qplan: QoIPlan | None = None) -> dict[str, Series]:
    """Every QoI's history: {qoi id: [(time, value)]} (the aether* monitors and other objects' scalars)."""
    pp = Path(case_root) / "postProcessing"
    if not pp.is_dir():
        return {}
    sig = _signature(pp)
    hit = _CACHE.get(str(pp))
    if hit is not None and hit[0] == sig:
        series = {k: list(v) for k, v in hit[1].items()}
    else:
        series = _read_all(pp)
        if len(_CACHE) > 16:
            _CACHE.clear()
        _CACHE[str(pp)] = (sig, {k: list(v) for k, v in series.items()})
    if qplan is not None:
        _derive(series, qplan)
    return series


def _derive(series: dict[str, Series], qp: QoIPlan) -> None:
    dq = next((q for q in qp.qois if q.id == "dp"), None)
    if dq is None:
        return
    pin, pout = dq.patches
    name = next((q.id.split(":")[1] for q in qp.qois if q.id.startswith("mean:") and q.id.endswith(f":{pin}")
                 and not q.id.startswith("mean:T:")), None)
    a = dict(series.get(f"mean:{name}:{pin}", []))
    b = dict(series.get(f"mean:{name}:{pout}", []))
    dp = [(t, a[t] - b[t]) for t in sorted(a) if t in b]
    if dp:
        series["dp"] = dp


def _read_all(pp: Path) -> dict[str, Series]:
    series: dict[str, Series] = {}
    for obj in sorted(p for p in pp.iterdir() if p.is_dir()):
        name = obj.name
        if name in _SKIP_OBJ or name in (f"{PREFIX}Min", f"{PREFIX}Max") or name.startswith(
                (f"{PREFIX}FieldMinMax", f"{PREFIX}Residuals", f"{PREFIX}YPlus")):
            continue
        tdirs = _time_dirs(obj)
        if not tdirs:
            continue
        chunks: list[tuple[float, dict[str, Series]]] = []
        for td in tdirs:
            part: dict[str, Series] = {}
            for dat in sorted(td.glob("*.dat")):
                stem = dat.stem
                if stem in _SKIP_FILE:
                    continue
                if stem in ("force", "forces"):
                    got = _read_forces(dat) or _read_columns(dat)
                    pre = "force:" if name == FORCES else f"{name}/{stem}:"
                    part.update({f"{pre}{k}": s for k, s in got.items()})
                elif stem == "wallHeatFlux":
                    pre = "heat:" if name == HEAT else f"{name}/{stem}:"
                    part.update({f"{pre}{k}": s for k, s in _read_heat(dat).items()})
                else:
                    cols = _read_columns(dat)
                    if name.startswith(f"{PREFIX}Flow_") and cols:
                        part[f"flow:{name[len(PREFIX) + 5:]}"] = next(iter(cols.values()))
                    elif name.startswith(f"{PREFIX}Mean_"):
                        patch = name[len(PREFIX) + 5:]
                        for c, s in cols.items():
                            m = re.fullmatch(r"\w+\((\w+)\)", c)
                            part[f"mean:{m.group(1) if m else c}:{patch}"] = s
                    else:
                        part.update({f"{name}/{stem}:{c}": s for c, s in cols.items()})
            chunks.append((float(td.name), part))
        series.update(_merge(chunks))
    return _clean(series)


# ============================================================================ judging
@dataclass
class QoIStat:
    id: str
    final: float
    samples: int
    window: int  # samples judged
    change_pct: float | None  # band (steady) or drift of the window mean (transient), % of the group scale
    scale: float
    ok: bool | None  # None: too few samples
    unit: str = ""
    title: str = ""

    def as_dict(self) -> dict:
        d = {"id": self.id, "title": self.title, "final": float(f"{self.final:.6g}"), "unit": self.unit,
             "samples": self.samples, "window": self.window, "ok": self.ok}
        if self.change_pct is not None:
            d["change_pct"] = float(f"{self.change_pct:.3g}")
        return d


def _group_of(qid: str, qp: QoIPlan | None) -> str:
    q = qp.by_id().get(qid) if qp else None
    if q is not None:
        return q.group
    return qid.split(":", 1)[0] if ":" in qid and "/" not in qid else "other"


def _scales(series: dict[str, Series], qp: QoIPlan | None) -> dict[str, float]:
    """What 'small' means for each QoI: a net force near zero or an outlet at the reference pressure would make a
    relative change meaningless, so changes are measured against the largest value in the QoI's group (|F|, the
    largest flow, the largest patch-mean, the largest wall heat flow)."""
    last = {k: s[-1][1] for k, s in series.items() if s}
    groups: dict[str, list[str]] = {}
    for k in last:
        g = _group_of(k, qp)
        if g == "mean":  # mean p and mean T do not share a scale
            g = f"mean:{k.split(':')[1]}"
        if g == "other":
            g = f"other:{k}"
        groups.setdefault(g, []).append(k)
    out = {}
    for g, keys in groups.items():
        if g == "force":
            sc = math.sqrt(sum(last[k] ** 2 for k in keys))
        else:
            sc = max(abs(last[k]) for k in keys)
            if g == "derived" or g.startswith("mean:p"):  # dp and the patch-mean pressures share one scale
                sc = max([sc] + [abs(last[k]) for k in last if k == "dp" or k.startswith(("mean:p:", "mean:p_rgh:"))])
        for k in keys:
            out[k] = sc
    return out


def judge(series: dict[str, Series], steady: bool, tol_pct: float = DEFAULT_TOL_PCT,
          qp: QoIPlan | None = None) -> list[QoIStat]:
    """Steady: the band (max - min) over the last 10 % of the iterations (at least 10 samples). Transient: the
    change of the mean between the last two quarters of the run (statistically stationary or not)."""
    scales = _scales(series, qp)
    info = qp.by_id() if qp else {}
    out = []
    for k, s in sorted(series.items()):
        vals = [v for _t, v in s]
        n = len(vals)
        q = info.get(k)
        st = QoIStat(k, vals[-1], n, 0, None, scales.get(k, 0.0), None, q.unit if q else "", q.title if q else k)
        sc = st.scale
        if n >= MIN_SAMPLES:
            if steady:
                w = max(10, n // 10)
                win = vals[-w:]
                change = max(win) - min(win)
            else:
                w = max(10, n // 4)
                if 2 * w > n:
                    w = n // 2
                change = abs(sum(vals[-w:]) / w - sum(vals[-2 * w:-w]) / w)
            st.window = w
            if sc > 0:
                st.change_pct = 100.0 * change / sc
                st.ok = st.change_pct <= tol_pct
            else:
                st.change_pct, st.ok = 0.0, True  # identically zero (e.g. Fz of a 2-D case)
        out.append(st)
    return out


def _negligible(st: QoIStat) -> bool:
    """A force component or patch value that is ~0 against its group (Fz in a 2-D case, a closed patch)."""
    return (st.scale > 0 and abs(st.final) < 1e-9 * st.scale) or st.scale == 0


def convergence_gate(case: Case, f: CaseFacts, qp: QoIPlan, series: dict[str, Series],
                     tol_pct: float = DEFAULT_TOL_PCT) -> tuple[G.Gate, list[QoIStat]]:
    if not series:
        how = ("Run stage: answer 'Add integral monitors' (forces, flow rates, patch means, wall heat flow), then run."
               if qp.objects else "Add function objects (forces, surfaceFieldValue) for the quantity you report.")
        if qp.objects and has_monitors(case):
            how = "The monitors are in place: run (or continue) the case to produce their output."
        return G.Gate("qoi-convergence", False, "warn", "No integral quantities to judge convergence on",
                      "Residuals say how well the equations are solved, not whether the quantity you report has "
                      "stopped changing. Judge convergence on an integral quantity (force, pressure drop, flow, "
                      "heat flow).", how), []
    stats = [st for st in judge(series, f.steady, tol_pct, qp) if not _negligible(st)]
    judged = [st for st in stats if st.ok is not None]
    bad = [st for st in judged if not st.ok]
    what = "band over the last 10 % of the iterations" if f.steady else "drift of the mean between the last two quarters"
    ev = [f"{st.title or st.id}: {st.final:.5g} {st.unit}".rstrip() + (
        f"; {what} {st.change_pct:.2g} %" if st.change_pct is not None else f"; only {st.samples} samples")
        for st in sorted(stats, key=lambda x: (x.ok is not False, x.id))]
    if not judged:
        return G.Gate("qoi-convergence", False, "warn", "Too few samples to judge the integral quantities",
                      f"Each quantity needs at least {MIN_SAMPLES} written values.", "Run longer.", ev), stats
    if bad:
        names = ", ".join(st.title or st.id for st in bad[:4])
        return G.Gate("qoi-convergence", False, "warn",
                      f"{len(bad)} integral quantit{'y' if len(bad) == 1 else 'ies'} still changing",
                      (f"{names} still move by more than {tol_pct:g} % of their scale over the end of the run. A "
                       "result read now carries that iterative error on top of the mesh error." if f.steady else
                       f"The mean of {names} drifts by more than {tol_pct:g} % between the last two quarters: the run "
                       "has not reached a statistically steady state, or the averaging window is too short."),
                      "Run more iterations (continue from the latest time). If a steady run oscillates for good, the "
                      "flow may be unsteady: use a transient solver and average." if f.steady else
                      "Run longer before averaging; average over many flow-through times.", ev), stats
    return G.passed("qoi-convergence", f"Integral quantities converged (within {tol_pct:g} %)", ev), stats


def conservation_gates(case: Case, f: CaseFacts, qp: QoIPlan, series: dict[str, Series]) -> list[G.Gate]:
    """Mass: Σ flow over every open patch against the inflow. Energy: in a closed domain, Σ wall heat flow."""
    gates = []
    flows = {q.patches[0]: series[q.id] for q in qp.qois if q.group == "flow" and q.id in series}
    missing = [p for p in qp.open_patches if p not in flows]
    why = ("What enters the domain must leave it (or be stored, in a compressible transient). An imbalance at the "
           "end of a run means the solution is not converged or the boundary conditions conflict.")
    if not qp.open_patches:
        gates.append(G.Gate("mass-balance", True, "info", "Mass balance: closed domain (no open patches)"))
    elif missing:
        gates.append(G.Gate("mass-balance", True, "info", "Mass balance not audited",
                            why, f"No flow monitor output for {', '.join(missing[:6])}: add the integral monitors "
                            "(Run stage) and run."))
    elif f.compressible and not f.steady:
        gates.append(G.Gate("mass-balance", True, "info", "Mass balance not audited (compressible transient)",
                            "The mass stored in the domain changes in time, so inflow and outflow need not match.",
                            evidence=[f"{p}: {s[-1][1]:.5g}" for p, s in flows.items()]))
    else:
        last = {p: s[-1][1] for p, s in flows.items()}
        inflow = sum(-v for v in last.values() if v < 0)
        outflow = sum(v for v in last.values() if v > 0)
        unit = "kg/s" if f.compressible else "m³/s"
        ev = [f"{p}: {v:+.5g} {unit} ({'in' if v < 0 else 'out'})" for p, v in sorted(last.items())]
        through = max(inflow, outflow)
        if through <= 0:
            gates.append(G.Gate("mass-balance", True, "info", "Mass balance: no net through-flow", evidence=ev))
        else:
            imb = 100.0 * abs(sum(last.values())) / through
            ev.insert(0, f"Σ = {sum(last.values()):+.4g} {unit}: {imb:.3g} % of the through-flow")
            if imb <= 0.5:
                gates.append(G.passed("mass-balance", f"Mass balance closes ({imb:.2g} %)", ev))
            else:
                sev = "error" if imb > 5.0 else "warn"
                gates.append(G.Gate("mass-balance", False, sev, f"Mass imbalance {imb:.3g} %", why,
                                    "Run until the flow rates settle; check that no two patches both fix the "
                                    "velocity and the pressure, and that the outlet can let flow leave.", ev))
    heat = {q.patches[0]: series[q.id] for q in qp.qois if q.group == "heat" and q.id in series}
    if heat:
        last = {p: s[-1][1] for p, s in heat.items()}
        ev = [f"{p}: {v:+.5g} W" for p, v in sorted(last.items())]
        q_in = sum(v for v in last.values() if v > 0)
        q_out = sum(-v for v in last.values() if v < 0)
        if qp.open_patches:
            gates.append(G.Gate("energy-balance", True, "info", "Energy balance not audited (open domain)",
                                "With inlets and outlets the energy carried by the flow also counts; only the wall "
                                "heat flows are monitored.", evidence=ev))
        elif not f.steady:
            gates.append(G.Gate("energy-balance", True, "info", "Energy balance not audited (transient)",
                                "The energy stored in the domain changes in time.", evidence=ev))
        elif max(q_in, q_out) > 0:
            imb = 100.0 * abs(q_in - q_out) / max(q_in, q_out)
            ev.insert(0, f"in {q_in:.5g} W, out {q_out:.5g} W: {imb:.3g} % imbalance")
            if imb <= 2.0:
                gates.append(G.passed("energy-balance", f"Energy balance closes ({imb:.2g} %)", ev))
            else:
                gates.append(G.Gate("energy-balance", False, "warn", f"Energy imbalance {imb:.3g} %",
                                    "In a closed steady domain the heat entering through the walls must leave through "
                                    "the walls. A large imbalance means the temperature field has not converged.",
                                    "Run longer (energy converges slowest in natural convection) and check the "
                                    "thermal boundary conditions.", ev))
    return gates


__all__ = ["QoI", "QoIPlan", "QoIStat", "conservation_gates", "convergence_gate", "fork_is_esi", "has_monitors",
           "judge", "plan", "read_series"]
