"""Guess which OpenFOAM fork a case was written for, with evidence the user can confirm."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.backend.wsl.forks import Family, ForkProfile

_BANNER_ESI = re.compile(r"www\.openfoam\.com|OpenCFD", re.IGNORECASE)
_BANNER_ORG = re.compile(r"openfoam\.org", re.IGNORECASE)
_VERSION = re.compile(r"Version:\s*(v?\d+(?:\.\d+)?|dev)\b")
_SOLVER_KW = re.compile(r"^\s*solver\s+\w+\s*;", re.MULTILINE)
_APP_KW = re.compile(r"^\s*application\s+(\w+)\s*;", re.MULTILINE)

# constant/ and system/ files that only one family uses (in recent versions)
_FOUNDATION_FILES = {"momentumTransport", "physicalProperties", "fvModels", "fvConstraints",
                     "thermophysicalTransport", "combustionModel"}
_ESI_FILES = {"turbulenceProperties", "transportProperties", "fvOptions", "thermophysicalProperties"}


@dataclass
class ForkGuess:
    family: Family | None
    version_hint: str | None
    confidence: float  # 0..1
    evidence: list[str] = field(default_factory=list)

    def pick(self, available: list[ForkProfile]) -> ForkProfile | None:
        """Best installed fork for this guess: same family, exact version if possible, else the closest
        version, preferring one not newer than the case's (a v2412 case stays on v2306 when v2306 and
        v2606 are both installed). Without a version hint, the oldest installed: installing a newer
        version must not silently move existing cases onto it (choose it per case instead)."""
        same = sorted((f for f in available if f.family is self.family), key=lambda f: version_key(f.version))
        if not same:
            return None
        if self.version_hint:
            hint = version_key(self.version_hint)
            for f in same:
                if version_key(f.version) == hint:
                    return f
            older = [f for f in same if version_key(f.version) < hint]
            return older[-1] if older else same[0]
        return same[0]


def version_key(version: str) -> tuple[int, str]:
    """Sort key for 'v2306', '2606', '14', '9', 'dev' (dev sorts last; '14' after '9')."""
    v = version.strip().lstrip("v")
    return (int(v), "") if v.isdigit() else (1 << 30, v)


def _head(p: Path, n: int = 2048) -> str:
    try:
        with open(p, "rb") as fh:
            return fh.read(n).decode("utf-8", errors="replace")
    except OSError:
        return ""


def detect_fork(case_root: Path) -> ForkGuess:
    esi = org = 0.0
    ev: list[str] = []
    version: str | None = None
    cd_text = ""
    cd = case_root / "system" / "controlDict"
    if cd.exists():
        cd_text = cd.read_text(encoding="utf-8", errors="replace")
    banners = [cd_text[:2048]] + [_head(p) for p in sorted((case_root / "system").glob("*"))[:6] if p.is_file()]
    for b in banners:
        if _BANNER_ESI.search(b):
            esi += 2
            ev.append("header banner says www.openfoam.com (ESI/OpenCFD)")
            break
        if _BANNER_ORG.search(b):
            org += 2
            ev.append("header banner says openfoam.org (Foundation)")
            break
    for b in banners:
        m = _VERSION.search(b)
        if m:
            version = m.group(1)
            if version.startswith("v"):
                esi += 1
            break
    if _SOLVER_KW.search(cd_text) and not _APP_KW.search(cd_text):
        org += 3
        ev.append("controlDict uses 'solver' (Foundation foamRun modules)")
    elif (m := _APP_KW.search(cd_text)) is not None:
        if m.group(1) == "foamRun":
            org += 3
            ev.append("controlDict application is foamRun (Foundation)")
        else:
            esi += 0.5
            ev.append(f"controlDict uses 'application {m.group(1)}'")
    names = set()
    for sub in ("constant", "system"):
        d = case_root / sub
        if d.is_dir():
            names |= {p.name for p in d.iterdir()}
    f_hits = sorted(names & _FOUNDATION_FILES)
    e_hits = sorted(names & _ESI_FILES)
    if f_hits:
        org += len(f_hits)
        ev.append(f"Foundation-style files: {', '.join(f_hits)}")
    if e_hits:
        esi += len(e_hits) * 0.75
        ev.append(f"ESI-style files: {', '.join(e_hits)}")
    total = esi + org
    if total == 0:
        return ForkGuess(None, version, 0.0, ["no fork evidence found"])
    fam = Family.ESI if esi >= org else Family.FOUNDATION
    conf = max(esi, org) / total
    return ForkGuess(fam, version, round(conf, 2), ev)
