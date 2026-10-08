"""Cell-size grading for blockMesh edges: geometric series, multi-grading, smooth size transitions.

Conventions follow blockMesh: an expansion ratio R is (last cell)/(first cell) along the edge; with n
cells the cell-to-cell ratio is r = R**(1/(n-1)). A multi-graded edge is a list of segments
(length fraction, cell fraction, expansion ratio) -- blockMesh's ``((0.2 0.3 4) (0.6 0.4 1) ...)``.

``design()`` answers the practical question "this block is L long and must start with cells of size a
and end with cells of size b (to match its neighbours); how many cells and what grading keep every
cell-to-cell ratio <= r_max?" -- which removes size jumps like 9:1 between blocks by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


def sizes(n: int, length: float, ratio: float) -> np.ndarray:
    """Cell sizes of an n-cell edge of the given length and blockMesh expansion ratio."""
    if n < 1 or length <= 0:
        raise ValueError("need n >= 1 and length > 0")
    if n == 1 or abs(ratio - 1.0) < 1e-12:
        return np.full(n, length / n)
    r = ratio ** (1.0 / (n - 1))
    d1 = length * (r - 1.0) / (r**n - 1.0)
    return d1 * r ** np.arange(n)


def n_for(length: float, first: float, r: float) -> float:
    """(Real) number of cells to cover ``length`` growing from ``first`` with cell ratio r."""
    if abs(r - 1.0) < 1e-12:
        return length / first
    return math.log(1.0 + length * (r - 1.0) / first) / math.log(r)


def length_for(first: float, r: float, n: int) -> float:
    return first * n if abs(r - 1.0) < 1e-12 else first * (r**n - 1.0) / (r - 1.0)


@dataclass
class Segment:
    length: float
    n: int
    ratio: float  # blockMesh expansion ratio of this segment

    def cells(self) -> np.ndarray:
        return sizes(self.n, self.length, self.ratio)


@dataclass
class GradingDesign:
    length: float
    segments: list[Segment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return sum(s.n for s in self.segments)

    def cells(self) -> np.ndarray:
        return np.concatenate([s.cells() for s in self.segments])

    @property
    def max_cell_ratio(self) -> float:
        c = self.cells()
        if len(c) < 2:
            return 1.0
        q = c[1:] / c[:-1]
        return float(np.maximum(q, 1.0 / q).max())

    def blockmesh(self) -> str:
        """The grading entry for this edge: a number, or blockMesh multi-grading."""
        if len(self.segments) == 1:
            return f"{self.segments[0].ratio:.6g}"
        parts = " ".join(f"({s.length / self.length:.6g} {s.n / self.n:.6g} {s.ratio:.6g})" for s in self.segments)
        return f"({parts})"

    def summary(self) -> dict:
        c = self.cells()
        return {"length": self.length, "cells": self.n, "first": float(c[0]), "last": float(c[-1]),
                "min": float(c.min()), "max": float(c.max()), "max_cell_to_cell_ratio": round(self.max_cell_ratio, 4),
                "blockMesh_grading": self.blockmesh(),
                "segments": [{"length": s.length, "cells": s.n, "expansion": round(s.ratio, 6)} for s in self.segments],
                "notes": self.notes}


def _grow(first: float, target: float, r: float) -> tuple[int, float]:
    """Cells and length to grow from ``first`` to about ``target`` with cell ratio r (target > first)."""
    if target <= first * r:
        return 0, 0.0
    k = max(1, round(math.log(target / first) / math.log(r)))
    return k, length_for(first * r, r, k)  # cells after the first-size cell


def design(length: float, *, first: float | None = None, last: float | None = None, n: int | None = None,
           max_size: float | None = None, max_ratio: float = 1.2) -> GradingDesign:
    """Grading for one edge.

    - n only: uniform.
    - first (and/or last) without n: smallest cell count whose cell-to-cell ratio stays <= max_ratio,
      growing from the end size(s) up to ``max_size`` (default: as large as the ratio allows) and
      continuing uniformly; three segments (grow, uniform, shrink) when both ends are given.
    - first + last + n: one geometric segment if it satisfies max_ratio, else the note says so.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if first is None and last is None:
        if n is None:
            raise ValueError("give n, or first and/or last cell sizes")
        return GradingDesign(length, [Segment(length, n, 1.0)])
    if n is not None and first is not None and last is not None:
        d = _fit_n(length, first, last, n)
        if d.max_cell_ratio > max_ratio * 1.0001:
            d.notes.append(f"with {n} cells the cell-to-cell ratio is {d.max_cell_ratio:.3f} > {max_ratio}; "
                           "omit n to let the design choose the count")
        return d
    a = first if first is not None else last
    b = last if last is not None else first
    assert a is not None and b is not None
    flip = first is None  # only 'last' given: design from that end and mirror
    big = max_size if max_size is not None else float("inf")
    r = max_ratio
    # grow from both ends towards a common core size h (<= big) that the length allows
    best = None
    h_hi = min(big, length)
    for h in np.geomspace(max(a, b), max(max(a, b), h_hi), 200):
        ka, la = _grow(a, h, r)
        kb, lb = _grow(b, h, r) if (first is not None and last is not None) else (0, 0.0)
        rest = length - (a + la) - ((b + lb) if (first is not None and last is not None) else 0.0)
        if rest < -1e-12 * length:
            break
        core_n = max(0, math.ceil(rest / h - 1e-9)) if rest > 1e-12 * length else 0
        total = (ka + 1) + ((kb + 1) if (first is not None and last is not None) else 0) + core_n
        if best is None or total < best[0]:
            best = (total, h, ka, kb, rest, core_n)
    if best is None:  # the ends alone exceed the length: uniform fallback
        nn = max(1, round(length / min(a, b)))
        d = GradingDesign(length, [Segment(length, nn, 1.0)])
        d.notes.append("end sizes do not fit the length; uniform cells used")
        return d
    _, h, ka, kb, rest, core_n = best
    segs: list[Segment] = []
    both = first is not None and last is not None
    la = length_for(a, r, ka + 1)
    lb = length_for(b, r, kb + 1) if both else 0.0
    core = length - la - lb
    if core < 0:
        core = 0.0
    # distribute any leftover so the grow segments end close to the core size
    if ka + 1 > 1:
        segs.append(Segment(la, ka + 1, a * r**ka / a))
    else:
        core += la
    if core_n > 0 or core > 0:
        segs.append(Segment(core, max(1, core_n if core_n else round(core / h)), 1.0))
    if both:
        if kb + 1 > 1:
            segs.append(Segment(lb, kb + 1, 1.0 / (b * r**kb / b)))
        elif segs:
            segs[-1] = Segment(segs[-1].length + lb, segs[-1].n + 1, segs[-1].ratio)
    if flip:
        segs = [Segment(s.length, s.n, 1.0 / s.ratio) for s in reversed(segs)]
    d = GradingDesign(length, [s for s in segs if s.n > 0 and s.length > 0])
    d = _rebalance(d)
    if max_size is not None and d.cells().max() > max_size * 1.05:
        d.notes.append(f"largest cell {d.cells().max():.4g} exceeds max_size {max_size:.4g}")
    return d


def _fit_n(length: float, first: float, last: float, n: int) -> GradingDesign:
    return GradingDesign(length, [Segment(length, n, last / first)])


def _rebalance(d: GradingDesign) -> GradingDesign:
    """Make segment lengths consistent with their cells (the length fractions blockMesh needs)."""
    total = sum(s.length for s in d.segments)
    if total <= 0:
        return d
    scale = d.length / total
    d.segments = [Segment(s.length * scale, s.n, s.ratio) for s in d.segments]
    return d


def jumps(cells_a: np.ndarray, cells_b: np.ndarray) -> float:
    """Size ratio across an interface between the last cell of one edge and the first of the next."""
    x, y = float(cells_a[-1]), float(cells_b[0])
    return max(x / y, y / x)
