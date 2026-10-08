"""Residual history from function-object output, which survives restarts better than the log.

ESI writes ``postProcessing/<name>/<startTime>/solverInfo.dat`` with ``<field>_initial`` columns;
the Foundation writes ``postProcessing/<name>/<startTime>/residuals.dat`` with one column per field.
Multi-region cases nest them under ``postProcessing/<region>/<name>/...``. Restarts create a new
<startTime> folder; series are concatenated in start-time order, later restarts winning on overlap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd._vendor.dat_parser import parse_dat_file


@dataclass
class DatResiduals:
    source: str  # "solverInfo" | "residuals"
    files: list[Path] = field(default_factory=list)
    series: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # field -> [(time, initial)]


def _start_time(p: Path) -> float:
    try:
        return float(p.parent.name)
    except ValueError:
        return math.inf


def find_residual_dat(case_root: Path) -> DatResiduals | None:
    pp = Path(case_root) / "postProcessing"
    if not pp.is_dir():
        return None
    esi = sorted(pp.rglob("solverInfo.dat"), key=_start_time)
    org = sorted(pp.rglob("residuals.dat"), key=_start_time)
    if esi:
        return _merge(esi, "solverInfo", pp)
    if org:
        return _merge(org, "residuals", pp)
    return None


def _merge(files: list[Path], kind: str, pp: Path) -> DatResiduals:
    out = DatResiduals(kind, files)
    for f in files:
        rel = f.relative_to(pp).parts
        region = rel[0] if len(rel) > 3 else ""  # postProcessing/<region>/<fo>/<t>/file.dat
        table = parse_dat_file(f)
        if "Time" not in table.columns:
            continue
        ti = table.column_index("Time")
        cols = []
        for i, c in enumerate(table.columns):
            if kind == "solverInfo":
                if c.endswith("_initial"):
                    cols.append((i, c[: -len("_initial")]))
            elif i != ti:
                cols.append((i, c))
        first_t: float | None = None
        for row in table.rows:
            try:
                t = float(row[ti])
            except (ValueError, IndexError):
                continue
            if first_t is None:
                first_t = t
                # a restart overrides everything from its start time on
                for name in out.series:
                    out.series[name] = [x for x in out.series[name] if x[0] < first_t]
            for i, name in cols:
                if i >= len(row):
                    continue
                try:
                    v = float(row[i])
                except ValueError:
                    continue  # "N/A" for fields not solved this step
                key = f"{region}/{name}" if region else name
                out.series.setdefault(key, []).append((t, v))
    return out
