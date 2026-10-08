"""Geometry-specific blocking: a round hole (or any O-grid) inside a structured box, through several levels.

The situation: a hole of radius ``r`` drilled through a wall that is meshed with blocks; around the hole an
O-grid maps the circle onto a box whose sides are set by the surrounding blocking -- e.g. the azimuthal
sector width on a tube (it changes with the radius of each level) and the axial pitch between rows of holes.
The O-grid's diagonal grid lines run from the circle's 45-degree points to the box corners; when the box is
elongated, a box corner sits at atan(w/a) instead of 45 degrees and the corner cells shear. The REV B.1
probe (2026-10-08) measured it: a 3:1 box (corner line 27 degrees off) gave 69.6 degrees non-orthogonality
exactly where the dilution jets enter the bore, and a box narrowed axially to the wrong size moved the
problem into the annulus (76.5 degrees).

``design_hole_box`` is the recipe that fixed it (REV B.1 G2h: 69.6 -> 64.7 degrees, determinant 0.0024 ->
0.0040, no new size jumps):

1. H-O topology: per level, a box half-length ``a`` as close to the box half-width ``w`` (a square box) as
   the hole gap (``a >= r + min_gap``) and the space left for the end slab (``a <= half_length -
   min_end``) allow; the band ends become graded H-type end slabs.
2. End slabs: ``n_end`` cells that continue the box cell (a/n_a) geometrically to the band plane.
3. Where the "hole" is not a physical surface (e.g. the far wall of an annulus), an elliptical layout with
   the box's aspect, so its diagonals point at the box corners; the O-grid morphs from the true circle.
4. Checks: corner misalignment per level (circle vs. ellipse), slab cone angles between levels, end-slab
   growth and the cell at the band plane (to match the neighbouring segments).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise


@dataclass(frozen=True)
class Level:
    name: str
    half_width: float  # box half-width w across the hole axis at this level (e.g. r_tube * sin(half sector))
    radial_pos: float = 0.0  # position of the level along the hole axis (for the slab cone angle)
    physical: bool = True  # is the hole surface real at this level? (False: free to use an ellipse)


def corner_misalignment(a: float, w: float, aspect: float = 1.0) -> float:
    """Degrees between the O-grid diagonal (atan(aspect)) and the box corner direction (atan(w/a))."""
    return abs(math.degrees(math.atan2(w, a)) - math.degrees(math.atan(aspect)))


def end_slab(a: float, half_length: float, n_a: int, n_end: int) -> dict:
    """``n_end`` cells continuing the box cell s = a/n_a geometrically (s*r, s*r^2, ...) over half_length - a."""
    s, length = a / n_a, half_length - a
    lo, hi = 0.2, 5.0
    for _ in range(200):
        r = 0.5 * (lo + hi)
        lo, hi = (r, hi) if sum(s * r ** k for k in range(1, n_end + 1)) < length else (lo, r)
    return {"length": length, "box_cell": s, "growth": r, "expansion": r ** (n_end - 1),
            "first": s * r, "band_plane_cell": s * r ** n_end}


def design_hole_box(hole_r: float, half_length: float, levels: list[Level], *, n_a: int = 6, n_end: int = 4,
                    min_gap: float | None = None, min_end: float | None = None,
                    max_growth: float = 1.25) -> dict:
    """Box half-length per level, end slabs, ellipse aspects and quality indicators (see module doc)."""
    if hole_r <= 0 or half_length <= hole_r:
        raise ValueError("need 0 < hole_r < half_length")
    min_gap = 0.44 * hole_r if min_gap is None else min_gap
    min_end = 0.4 * half_length if min_end is None else min_end
    lo, hi = hole_r + min_gap, half_length - min_end
    notes: list[str] = []
    if lo > hi:
        notes.append(f"the band is too short for an H-O split (need a in [{lo:.4g}, {hi:.4g}]); keep the O-grid "
                     "box at the band length and accept the corner shear, or move the row pitch")
        hi = lo
    rows = []
    for lv in levels:
        a = min(max(lv.half_width, lo), hi)
        aspect = 1.0 if lv.physical else lv.half_width / a
        slab = end_slab(a, half_length, n_a, n_end)
        rows.append({
            "level": lv.name, "half_width": lv.half_width, "box_half_length": a, "physical": lv.physical,
            "layout": "circle" if lv.physical else f"ellipse {hole_r:.4g} x {hole_r * aspect:.4g}",
            "misalignment_full_band_deg": corner_misalignment(half_length, lv.half_width),
            "misalignment_deg": corner_misalignment(a, lv.half_width, aspect),
            "gap_axial": a - hole_r, "end_slab": slab})
        if slab["growth"] > max_growth:
            notes.append(f"{lv.name}: end-slab growth {slab['growth']:.2f} > {max_growth}; use more end cells")
        if slab["growth"] < 1 / max_growth:
            notes.append(f"{lv.name}: end-slab cells shrink ({slab['growth']:.2f}) toward the band plane; the box "
                         "is too long for this end slab -- fewer end cells or a shorter box")
    for (r0, l0), (r1, l1) in pairwise(zip(rows, levels, strict=True)):
        dz = abs(l1.radial_pos - l0.radial_pos)
        if dz > 0:
            ang = math.degrees(math.atan2(abs(r1["box_half_length"] - r0["box_half_length"]), dz))
            r1["slab_cone_deg_from_previous"] = ang
            if ang > 15:
                notes.append(f"{l0.name}->{l1.name}: slab faces lean {ang:.0f} deg (adds non-orthogonality); "
                             "make the box lengths of neighbouring levels closer")
    worst = max(rows, key=lambda r: r["misalignment_deg"])
    if worst["misalignment_deg"] > 20:
        notes.append(f"{worst['level']}: corner misalignment {worst['misalignment_deg']:.0f} deg remains (box "
                     "limited by the hole gap / band length); expect non-orthogonality above ~60 deg there")
    return {"hole_r": hole_r, "half_length": half_length, "n_a": n_a, "n_end": n_end,
            "box_half_length_range": [lo, hi], "levels": rows, "notes": notes,
            "rule": "corner misalignment <= ~15 deg keeps O-grid corner cells near the round-box quality; the "
                    "full-band box column shows what an O-grid spanning the whole band would give"}


def tube_levels(radii: dict[str, float], sector_deg: float, physical: dict[str, bool] | None = None) -> list[Level]:
    """Levels on concentric tubes: box half-width = r * sin(sector/2) (the chord of the hole's sector pair)."""
    physical = physical or {}
    return [Level(n, r * math.sin(math.radians(sector_deg / 2)), r, physical.get(n, True))
            for n, r in radii.items()]
