"""Side-by-side diff rows for two texts (no Qt): aligned lines with filler, change kinds and statistics.

Used by the Diff panel and the copilot. Lines are compared as text; a replaced block is paired line by line
and the shorter side padded, so both columns stay aligned for synchronized scrolling.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Row:
    left: str | None  # None = filler (no line on this side)
    right: str | None
    kind: str  # equal | delete | insert | replace
    left_no: int | None = None  # 1-based line numbers
    right_no: int | None = None


@dataclass
class SideBySide:
    rows: list[Row] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    changed_blocks: int = 0

    @property
    def identical(self) -> bool:
        return self.changed_blocks == 0

    def summary(self) -> str:
        if self.identical:
            return "identical"
        return f"{self.changed_blocks} change(s): +{self.added} / -{self.removed} lines"


def side_by_side(a: str, b: str, *, context: int | None = None) -> SideBySide:
    """Rows for a two-column view. ``context`` keeps only that many equal lines around changes (None = all)."""
    al, bl = a.splitlines(), b.splitlines()
    out = SideBySide()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.rows += [Row(al[i1 + k], bl[j1 + k], "equal", i1 + k + 1, j1 + k + 1) for k in range(i2 - i1)]
            continue
        out.changed_blocks += 1
        out.removed += i2 - i1
        out.added += j2 - j1
        n = max(i2 - i1, j2 - j1)
        for k in range(n):
            li = i1 + k if i1 + k < i2 else None
            rj = j1 + k if j1 + k < j2 else None
            kind = tag if tag != "replace" else ("replace" if li is not None and rj is not None else
                                                 ("delete" if rj is None else "insert"))
            out.rows.append(Row(al[li] if li is not None else None, bl[rj] if rj is not None else None, kind,
                                li + 1 if li is not None else None, rj + 1 if rj is not None else None))
    if context is not None:
        keep = [False] * len(out.rows)
        for i, r in enumerate(out.rows):
            if r.kind != "equal":
                for j in range(max(0, i - context), min(len(out.rows), i + context + 1)):
                    keep[j] = True
        out.rows = [r for r, k in zip(out.rows, keep, strict=True) if k]
    return out


def unified(a: str, b: str, a_name: str = "a", b_name: str = "b", context: int = 3) -> str:
    return "".join(difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True), a_name, b_name,
                                        n=context))
