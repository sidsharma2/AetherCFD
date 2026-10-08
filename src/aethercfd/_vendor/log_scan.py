# Vendored from cfd_agents@820a5f9 case/log_scan.py
# (D:/Research-Software/projects/repository-capability-lab/agent-architecture). Never import
# cfd_agents itself -- see docs/adr/0001-reuse-cfd-agents.md. Local edits are marked 'AetherCFD:'.
"""Anchored scanning of OpenFOAM solver logs.

Substring matching on a whole log is wrong for OpenFOAM: every run started
with floating-point trapping prints

    trapFpe: Floating point exception trapping enabled (FOAM_SIGFPE).

near the top, so "floating point" or "sigfpe" anywhere in the text says
nothing about failure. This mistake stopped the healthy SST-DDES pilot
``les_pilot_hybrid_00`` and made Murphy classify every healthy log as an
FP exception (see cfd_agents/learning/corpus/round_jet). Fatal evidence is
recognised only as whole lines OpenFOAM prints when it actually aborts.
"""

from __future__ import annotations

import re

# Startup/informational lines that mention failure vocabulary but are not failures.
BENIGN_LINE_PATTERNS = (
    re.compile(r"^\s*trapFpe:\s*Floating point exception trapping enabled", re.IGNORECASE),
    re.compile(r"^\s*sigFpe\s*:\s*Enabling floating point exception trapping", re.IGNORECASE),
    re.compile(r"^\s*setNaN\s*:", re.IGNORECASE),
)

# Lines OpenFOAM prints when a run aborts. Each must match a whole line start.
FATAL_LINE_PATTERNS = (
    re.compile(r"^--> FOAM FATAL (?:IO )?ERROR"),
    re.compile(r"^#\d+\s+Foam::sigFpe::sigHandler"),
    re.compile(r"^#\d+\s+Foam::sigSegv::sigHandler"),
    re.compile(r"^Floating point exception(?: \(core dumped\))?\s*$"),
    re.compile(r"^Segmentation fault(?: \(core dumped\))?\s*$"),
)


def is_benign(line: str) -> bool:
    return any(p.search(line) for p in BENIGN_LINE_PATTERNS)


def strip_benign_lines(text: str) -> str:
    """The log with known-benign startup lines removed (all other lines kept)."""
    return "\n".join(line for line in text.splitlines() if not is_benign(line))


def fatal_lines(text: str) -> list[tuple[int, str]]:
    """``(1-based line number, line)`` for every anchored fatal marker."""
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if any(p.match(line) for p in FATAL_LINE_PATTERNS)
    ]
