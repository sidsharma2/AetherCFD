"""Parse ``checkMesh`` output (ESI v2306 and Foundation formats) into metrics, checks and sets.

The regexes follow the real output (e.g. ESI prints "Mesh non-orthogonality Max: 83.3 average: 3.7",
not "Max non-orthogonality"; see the vendored cfd_agents note), and every finding carries a short
explanation of why it matters and what usually fixes it -- a mesh is never just "red".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"


@dataclass
class MeshCheck:
    name: str
    ok: bool
    value: str = ""
    message: str = ""
    advice: str = ""
    severity: str = "ok"  # ok | warn | fail


@dataclass
class MeshSet:
    kind: str  # cells | faces | points
    name: str
    count: int
    reason: str


@dataclass
class CheckMeshReport:
    stats: dict[str, int] = field(default_factory=dict)
    cell_types: dict[str, int] = field(default_factory=dict)
    checks: list[MeshCheck] = field(default_factory=list)
    sets: list[MeshSet] = field(default_factory=list)
    failed: int | None = None
    mesh_ok: bool = False
    bounding_box: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    directions: str = ""
    non_ortho_max: float | None = None
    non_ortho_avg: float | None = None
    skewness_max: float | None = None
    aspect_max: float | None = None
    min_volume: float | None = None
    finished: bool = False

    @property
    def problems(self) -> list[MeshCheck]:
        return [c for c in self.checks if c.severity != "ok"]


_ADVICE = {
    "aspect": ("High aspect ratio is often fine in boundary layers (wall-normal stretching) and in 2D meshes, "
               "but hurts convergence in the free stream. Check where the flagged cells are (cellSet "
               "highAspectRatioCells); reduce grading or add cells in the long direction if they are away from walls."),
    "nonortho": ("Non-orthogonality > 70° needs non-orthogonal correction: set nNonOrthogonalCorrectors 1-3 in "
                 "fvSolution and use limited laplacian/snGrad schemes (e.g. 'Gauss linear limited corrected 0.33'). "
                 "Above ~85° the mesh should be improved (snappy layers, smoothing)."),
    "skew": ("Skewness above ~4 degrades interpolation accuracy and stability; improve the mesh near the flagged "
             "faces or use more robust (bounded/upwind-biased) schemes there."),
    "volume": "Zero or negative cell volumes are fatal: the mesh is invalid (inverted cells). Fix the geometry/mesher.",
    "determinant": ("Low cell determinant indicates badly shaped cells (e.g. flat prisms); usually from "
                    "layer addition. Relax layer settings or improve the surface."),
    "pyramids": "Incorrectly oriented faces (face pyramids): the mesh is inverted somewhere; it cannot be solved.",
    "openness": "The boundary is not closed: faces are missing; the mesh cannot conserve mass.",
    "regions": "The mesh has disconnected regions; usually unintended (stray blocks or snappy leftovers).",
    "default": "OpenFOAM flagged this check; inspect the reported set in the viewport.",
}


def parse_checkmesh(text: str) -> CheckMeshReport:
    r = CheckMeshReport()
    lines = text.replace("\r", "").split("\n")
    in_stats = in_types = False
    for raw in lines:
        s = raw.strip()
        if s.startswith("Mesh stats"):
            in_stats, in_types = True, False
            continue
        if s.startswith("Overall number of cells of each type"):
            in_stats, in_types = False, True
            continue
        if in_stats or in_types:
            m = re.match(r"([A-Za-z ]+):\s+(\d+)$", s)
            if m:
                (r.stats if in_stats else r.cell_types)[m.group(1).strip()] = int(m.group(2))
                continue
            if not s:
                in_stats = in_types = False
        if (m := re.match(rf"Overall domain bounding box \({_NUM} {_NUM} {_NUM}\) \({_NUM} {_NUM} {_NUM}\)", s)):
            v = [float(x) for x in m.groups()]
            r.bounding_box = ((v[0], v[1], v[2]), (v[3], v[4], v[5]))
        elif s.startswith("Mesh has") and "solution" in s:
            r.directions = s
        elif (m := re.search(rf"non-orthogonality Max:\s*{_NUM}\s+average:\s*{_NUM}", s)):
            r.non_ortho_max, r.non_ortho_avg = float(m.group(1)), float(m.group(2))
            sev = "fail" if r.non_ortho_max >= 85 else "warn" if r.non_ortho_max >= 70 else "ok"
            r.checks.append(MeshCheck("Non-orthogonality", sev != "fail", f"max {r.non_ortho_max:g}°, "
                                      f"avg {r.non_ortho_avg:g}°", s, _ADVICE["nonortho"] if sev != "ok" else "",
                                      sev))
        elif (m := re.search(r"Number of severely non-orthogonal \(> (\d+) degrees\) faces:\s*(\d+)", s)):
            nonortho = next((c for c in r.checks if c.name == "Non-orthogonality"), None)
            if nonortho is not None:
                nonortho.value += f", {m.group(2)} faces > {m.group(1)}°"
                if nonortho.severity == "ok":
                    nonortho.severity = "warn"
                    nonortho.advice = _ADVICE["nonortho"]
        elif (m := re.search(rf"Max skewness = {_NUM}", s)):
            r.skewness_max = float(m.group(1))
            bad = s.startswith("***") or "OK" not in s
            r.checks.append(MeshCheck("Skewness", not bad, f"max {r.skewness_max:g}", s,
                                      _ADVICE["skew"] if bad else "", "fail" if bad else "ok"))
        elif (m := re.search(rf"[Aa]spect ratio(?: cells found, Max aspect ratio)?[:=]?\s*{_NUM}", s)) and \
                "aspect" in s.lower():
            r.aspect_max = float(m.group(1))
            bad = s.startswith("***")
            n = re.search(r"number of cells (\d+)", s)
            r.checks.append(MeshCheck("Aspect ratio", not bad, f"max {r.aspect_max:g}" +
                                      (f", {n.group(1)} cells" if n else ""), s,
                                      _ADVICE["aspect"] if bad else "", "warn" if bad else "ok"))
        elif (m := re.search(rf"Min volume = {_NUM}", s)):
            r.min_volume = float(m.group(1))
            bad = r.min_volume <= 0 or s.startswith("***")
            r.checks.append(MeshCheck("Cell volumes", not bad, f"min {r.min_volume:g}", s,
                                      _ADVICE["volume"] if bad else "", "fail" if bad else "ok"))
        elif s.startswith("Boundary openness"):
            bad = "OK" not in s
            r.checks.append(MeshCheck("Boundary openness", not bad, "", s, _ADVICE["openness"] if bad else "",
                                      "fail" if bad else "ok"))
        elif s.startswith("Number of regions"):
            bad = "OK" not in s
            r.checks.append(MeshCheck("Connected regions", not bad, s.split(":")[-1].strip(), s,
                                      _ADVICE["regions"] if bad else "", "warn" if bad else "ok"))
        elif "determinant" in s.lower() and ("***" in s or "OK" in s):
            bad = s.startswith("***")
            r.checks.append(MeshCheck("Cell determinant", not bad, "", s, _ADVICE["determinant"] if bad else "",
                                      "warn" if bad else "ok"))
        elif s.startswith(("Face pyramids", "***Error in face pyramids")):
            bad = "OK" not in s
            r.checks.append(MeshCheck("Face pyramids", not bad, "", s, _ADVICE["pyramids"] if bad else "",
                                      "fail" if bad else "ok"))
        elif (m := re.match(r"<<Writing (\d+) (.+?) to set (\S+)", s)):
            what = m.group(2)
            kind = "cells" if "cell" in what else "faces" if "face" in what else "points"
            r.sets.append(MeshSet(kind, m.group(3), int(m.group(1)), what))
        elif (m := re.match(r"Failed (\d+) mesh checks", s)):
            r.failed = int(m.group(1))
        elif s == "Mesh OK.":
            r.failed, r.mesh_ok = 0, True
        elif s == "End":
            r.finished = True
        elif s.startswith("***") and not any(c.message == s for c in r.checks):
            r.checks.append(MeshCheck(s.strip("* ").split(",")[0][:60], False, "", s, _ADVICE["default"], "warn"))
    return r
