# Vendored from cfd_agents@820a5f9 case/dat_parser.py
# (the agent-architecture repository by Siddharth Sharma). Never import
# cfd_agents itself -- see docs/adr/0001-reuse-cfd-agents.md. Local edits are marked 'AetherCFD:'.
"""Generic parser for OpenFOAM function-object `.dat` output — real,
tab-delimited, `#`-prefixed-header formats, confirmed live this round
against a scratch `icoFoam` cavity-tutorial run (never round-jet):

- `fieldMinMax.dat`: one row per (time, field) pair. Columns are always
  `Time, field, min, location(min), max, location(max)` regardless of
  `mode` — magnitude vs. component only changes whether a *vector*
  field's min/max value stays a `(x y z)` tuple or collapses to a
  scalar `mag(...)` reading; a *scalar* field's own min/max came back
  byte-identical under both modes in a direct side-by-side test (same
  sign, same value) — see `case/instrumentation.py`'s docstring.
- `solverInfo.dat`: one row per time, one COLUMN GROUP per solved field
  (`<field>_solver`, `<field>_initial`, `<field>_final`, `<field>_iters`,
  `<field>_converged`; vector fields split further per component, e.g.
  `Ux_final`).

A restart re-prints an identical `#`-prefixed header block partway
through the same file (confirmed live in a real round-jet case's own
`massFlowOutlet.dat`) — this parser keeps only the FIRST header line
that looks like a real column header (contains `Time`) and treats every
other `#`-prefixed line as a skippable comment. This is safe as long as
the column set doesn't change across a restart, true for every case in
this project (fields/mode are case-definition choices, not per-run
ones).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DatTable:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    # Fragments of rows still being written (an unterminated final line) or
    # cut by a byte-offset tail read (``partial_head``). Expected while a
    # solver runs; never evidence of failure.
    incomplete_rows: list[str] = field(default_factory=list)
    # Newline-terminated rows whose column count disagrees with the header.
    # OpenFOAM finished writing these, so they are real corruption: excluded
    # from ``rows`` and surfaced instead of silently skipped.
    malformed_rows: list[str] = field(default_factory=list)

    def column_index(self, name: str) -> int:
        return self.columns.index(name)


def parse_dat_file(path: Path, *, partial_head: bool = False) -> DatTable:
    """Real OpenFOAM `.dat` function-object files are tab-delimited
    between columns (space-delimited only WITHIN a single `(x y z)`
    vector/location value) — splitting on tabs, not generic whitespace,
    is what keeps a vector column intact as one field."""
    return parse_dat_text(Path(path).read_text(encoding="utf-8", errors="replace"), partial_head=partial_head)


def parse_dat_text(text: str, *, partial_head: bool = False) -> DatTable:
    """Parse `.dat` text that may still be growing.

    Only newline-terminated rows count as complete: the last line of a file
    OpenFOAM is writing may stop mid-number ("2.5886332626e-03" read as
    "2.58863" is 1000x wrong), which is how the guard race in
    les_pilot_hybrid_00_retry_01/02 began. Set ``partial_head`` when
    ``text`` came from a byte-offset tail read, so its first line is a
    fragment."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    incomplete: list[str] = []
    tail = lines.pop()  # "" when the text ends with a newline
    if tail.strip():
        incomplete.append(tail)
    if partial_head and lines and not lines[0].lstrip().startswith("#"):
        head = lines.pop(0)
        if head.strip():
            incomplete.insert(0, head)
    columns: list[str] = []
    rows: list[list[str]] = []
    malformed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            candidate = [c.strip() for c in stripped.lstrip("#").split("\t") if c.strip()]
            if not columns and "Time" in candidate:
                columns = candidate
            continue
        parts = [p.strip() for p in stripped.split("\t") if p.strip() != ""]
        if not parts:
            continue
        if len(columns) > 1 and len(parts) != len(columns):
            malformed.append(line)
            continue
        rows.append(parts)
    return DatTable(columns=columns, rows=rows, incomplete_rows=incomplete, malformed_rows=malformed)


def _parse_scalar_or_vector_magnitude(value: str) -> float:
    """Handles both a plain scalar (`"-5.14"`) and a parenthesized
    vector (`"(1.0 2.0 0.0)"`, from a `component`-mode fieldMinMax
    entry) by returning the vector's Euclidean magnitude — matching
    what `mode magnitude` itself would have reported directly."""
    value = value.strip()
    if value.startswith("(") and value.endswith(")"):
        components = [float(x) for x in value[1:-1].split()]
        return math.sqrt(sum(x * x for x in components))
    return float(value)


def unique_field_names(table: DatTable) -> list[str]:
    """Distinct values of the `field` column, in first-seen order —
    only meaningful for a `fieldMinMax`-shaped table."""
    if "field" not in table.columns:
        return []
    idx = table.column_index("field")
    seen: dict[str, None] = {}
    for row in table.rows:
        if idx < len(row):
            seen.setdefault(row[idx], None)
    return list(seen.keys())


def field_min_max_series(table: DatTable, field_name: str) -> list[tuple[float, float, float]]:
    """`[(time, min, max), ...]` for one field name exactly as it
    appears in `fieldMinMax`'s own `field` column (e.g. `"p"`,
    `"mag(U)"`, `"Co"`, `"k"`)."""
    required = {"Time", "field", "min", "max"}
    if not required.issubset(table.columns):
        return []
    time_idx, field_idx = table.column_index("Time"), table.column_index("field")
    min_idx, max_idx = table.column_index("min"), table.column_index("max")
    series = []
    for row in table.rows:
        if len(row) <= max(time_idx, field_idx, min_idx, max_idx):
            continue
        if row[field_idx] != field_name:
            continue
        try:
            t = float(row[time_idx])
            lo = _parse_scalar_or_vector_magnitude(row[min_idx])
            hi = _parse_scalar_or_vector_magnitude(row[max_idx])
        except ValueError:
            continue
        series.append((t, lo, hi))
    return series


def solver_info_final_residual_series(table: DatTable, quantity_column: str) -> list[tuple[float, float]]:
    """`[(time, value), ...]` for one named `solverInfo` column, e.g.
    `"p_final"` or `"Ux_final"`."""
    if "Time" not in table.columns or quantity_column not in table.columns:
        return []
    time_idx, value_idx = table.column_index("Time"), table.column_index(quantity_column)
    series = []
    for row in table.rows:
        if len(row) <= max(time_idx, value_idx):
            continue
        try:
            series.append((float(row[time_idx]), float(row[value_idx])))
        except ValueError:
            continue
    return series
