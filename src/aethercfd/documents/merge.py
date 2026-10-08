"""Line-based three-way merge (diff3-style) for "the file changed on disk while you were editing".

Changes from both sides are clustered when they overlap *or touch* in the base (like git), so two
edits on adjacent lines are reported as a conflict rather than merged blindly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass(frozen=True)
class MergeConflict:
    base: list[str]
    ours: list[str]
    theirs: list[str]
    line: int  # 1-based line in the merged output where the conflict block starts


@dataclass
class MergeResult:
    text: str  # merged text; conflict regions keep *ours* (the UI shows both sides)
    conflicts: list[MergeConflict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts


def _changes(base: list[str], other: list[str]) -> list[tuple[int, int, int, int]]:
    sm = SequenceMatcher(a=base, b=other, autojunk=False)
    return [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def merge3(base: str, ours: str, theirs: str) -> MergeResult:
    if ours == theirs:
        return MergeResult(ours)
    if base == ours:
        return MergeResult(theirs)
    if base == theirs:
        return MergeResult(ours)
    b = base.splitlines(keepends=True)
    sides = {"o": ours.splitlines(keepends=True), "t": theirs.splitlines(keepends=True)}
    regions = sorted([(i1, i2, "o", j1, j2) for i1, i2, j1, j2 in _changes(b, sides["o"])]
                     + [(i1, i2, "t", j1, j2) for i1, i2, j1, j2 in _changes(b, sides["t"])])
    out: list[str] = []
    conflicts: list[MergeConflict] = []
    pos = 0
    k = 0
    while k < len(regions):
        lo, hi = regions[k][0], regions[k][1]
        cluster = [regions[k]]
        k += 1
        while k < len(regions) and regions[k][0] <= hi:  # overlapping or touching
            cluster.append(regions[k])
            hi = max(hi, regions[k][1])
            k += 1
        out.extend(b[pos:lo])

        def side_text(who: str, cluster=cluster, lo=lo, hi=hi) -> list[str] | None:
            regs = [r for r in cluster if r[2] == who]
            if not regs:
                return None
            res: list[str] = []
            p = lo
            for i1, i2, _w, j1, j2 in regs:
                res += b[p:i1] + sides[who][j1:j2]
                p = i2
            return res + b[p:hi]

        o_txt, t_txt = side_text("o"), side_text("t")
        if t_txt is None or o_txt == t_txt:
            out.extend(o_txt or [])
        elif o_txt is None:
            out.extend(t_txt)
        else:
            conflicts.append(MergeConflict(b[lo:hi], o_txt, t_txt, len(out) + 1))
            out.extend(o_txt)
        pos = hi
    out.extend(b[pos:])
    return MergeResult("".join(out), conflicts)
