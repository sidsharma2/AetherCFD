"""Units of OpenFOAM fields from their ``dimensions [kg m s K mol A cd]`` header, and readable SI scaling
for colour bars (11 677 150 Pa -> 11.68 MPa). No Qt here."""

from __future__ import annotations

import gzip
import math
import re
from pathlib import Path

_DIM = re.compile(rb"dimensions\s*\[\s*([-0-9.eE+\s]+?)\s*\]")
_NAMED = {
    (0, 1, -1, 0, 0, 0, 0): "m/s", (1, -1, -2, 0, 0, 0, 0): "Pa", (0, 2, -2, 0, 0, 0, 0): "m²/s²",
    (0, 0, 0, 1, 0, 0, 0): "K", (1, -3, 0, 0, 0, 0, 0): "kg/m³", (0, 2, -1, 0, 0, 0, 0): "m²/s",
    (1, -1, -1, 0, 0, 0, 0): "kg/(m·s)", (0, 0, -1, 0, 0, 0, 0): "1/s", (0, 2, -3, 0, 0, 0, 0): "m²/s³",
    (1, 0, -3, 0, 0, 0, 0): "W/m²", (1, -2, -2, 0, 0, 0, 0): "Pa/m", (0, 1, 0, 0, 0, 0, 0): "m",
    (0, 0, 0, 0, 0, 0, 0): "",
}
_BASE = ("kg", "m", "s", "K", "mol", "A", "cd")
_PREFIX = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ")]


def dimensions_of(case: str | Path, field: str, time: str | None = None) -> tuple[float, ...] | None:
    """The dimension set of ``field`` from any time folder that has it (processor0 / region folders too)."""
    root = Path(case)

    def candidates():
        # lazily, newest time first, stopping at the first header found: a long run has thousands of time
        # folders (and \wsl.localhost paths are slow), so never list them all; region folders only for the
        # newest few times
        for base in (root, root / "processor0"):
            try:
                dirs = [base / time] if time else sorted((d for d in base.iterdir() if d.is_dir()
                                                          and d.name[:1].isdigit()), key=_num, reverse=True)
            except OSError:
                continue
            for k, d in enumerate(dirs):
                yield d / field
                yield d / f"{field}.gz"
                if k < 3:
                    yield from d.glob(f"*/{field}")
                    yield from d.glob(f"*/{field}.gz")

    for f in candidates():
        if not f.is_file():
            continue
        try:
            with (gzip.open(f, "rb") if f.suffix == ".gz" else open(f, "rb")) as fh:
                head = fh.read(4000)
        except OSError:
            continue
        m = _DIM.search(head)
        if m:
            try:
                return tuple(float(x) for x in m.group(1).split())
            except ValueError:
                return None
    return None


def _num(d: Path) -> float:
    try:
        return float(d.name)
    except ValueError:
        return float("-inf")


def unit_text(dims: tuple[float, ...] | None) -> str | None:
    """'m/s', 'Pa', 'K', '' (dimensionless) or a generic 'kg m⁻¹ s⁻²'; None when unknown."""
    if dims is None:
        return None
    d = tuple(round(x) for x in (list(dims) + [0] * 7)[:7])
    if d in _NAMED:
        return _NAMED[d]
    sup = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")
    return " ".join(b if e == 1 else f"{b}{str(e).translate(sup)}" for b, e in zip(_BASE, d, strict=True) if e)


def si_scale(lo: float, hi: float, unit: str | None) -> tuple[float, str]:
    """(factor, label unit) so values / factor read naturally: Pa over 1e6 -> (1e6, 'MPa'). Only SI units
    with a plain symbol get a prefix; others (m²/s², dimensionless) get a power-of-ten factor."""
    m = max(abs(lo), abs(hi))
    if not math.isfinite(m) or m == 0:
        return 1.0, unit or ""
    if unit in ("Pa", "W/m²", "m", "kg/m³") and unit is not None:
        for f, p in _PREFIX:
            if m >= f * 0.999:
                if unit == "m" and p in ("k", "M", "G"):
                    break  # metres stay metres (domains are not km)
                if unit == "kg/m³" and p:
                    break
                return f, f"{p}{unit}"
        return 1.0, unit
    e = math.floor(math.log10(m))
    if -2 <= e <= 4:
        return 1.0, unit or ""
    p = 3 * (e // 3)
    sup = str(p).translate(str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹"))
    return 10.0 ** p, f"×10{sup} {unit or ''}".strip()


def bar_title(field: str, unit: str | None, scale_label: str) -> str:
    lab = scale_label if scale_label else (unit or "")
    return f"{field} [{lab}]" if lab else field
