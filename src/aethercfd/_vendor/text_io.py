# Vendored from cfd_agents@820a5f9 case/_text_io.py
# (D:/Research-Software/projects/repository-capability-lab/agent-architecture). Never import
# cfd_agents itself -- see docs/adr/0001-reuse-cfd-agents.md. Local edits are marked 'AetherCFD:'.
"""Robust text reading for pre-existing case artifacts.

Found as a real bug (not theoretical) while running `cfd mesh check` for
the first time against the real converged round-jet case:
`vera_pre_solve_check.json` (written by an earlier point in this project,
before UTF-8 was consistently enforced everywhere) contains a raw
Windows-1252 em-dash byte (0x97) that isn't valid UTF-8 — `Path.read_text
(encoding="utf-8")` raised `UnicodeDecodeError`. Per the read-only
regression constraint, that pre-existing file is not rewritten to "fix"
its encoding — every NEW reader of an existing case artifact goes through
`read_text_robust()` instead, which tries UTF-8 first (the standard this
project's own new code writes) and falls back to cp1252 (Windows'
locale-default encoding, and the one this particular latent bug shows was
used somewhere upstream) rather than crashing.
"""

from __future__ import annotations

from pathlib import Path


def read_text_robust(path: Path) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")
