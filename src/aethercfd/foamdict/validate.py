"""Authoritative validation: let OpenFOAM itself read a dictionary (``foamDictionary -expand``).

Our own parser gives instant syntax diagnostics while typing; this adds what only OpenFOAM knows
(``#include`` resolution, ``$macro`` expansion, ``#calc``/``#codeStream``, fork-specific directives).
It validates files *on disk* (after save or on demand) and never writes into the case: cases whose
path OpenFOAM cannot handle (spaces) are validated through a temporary mirror of system/ and
constant/ (mesh excluded) under /tmp.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aethercfd.backend.wsl.forks import ForkProfile, prelude
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.core.paths import openfoam_path_safety, win_to_wsl

_FILE_LINE = re.compile(r"file:\s*(\S.*?)\s+at line (\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationIssue:
    file: str  # path relative to the case (posix)
    line: int | None
    message: str


def _parse_fatal(block: str) -> tuple[int | None, str]:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    msg_lines = []
    line_no = None
    for ln in lines:
        if ln.startswith("--> FOAM FATAL"):
            continue
        m = _FILE_LINE.search(ln)
        if m:
            line_no = int(m.group(2))
            continue
        if ln.strip().startswith(("From ", "in file ", "FOAM exiting", "    From")):
            continue
        msg_lines.append(ln.strip())
    return line_no, " ".join(msg_lines)[:400] or "OpenFOAM could not read this dictionary"


def validate_files(runner: WslRunner, fork: ForkProfile, case_root: Path, rel_files: list[str],
                   *, timeout_s: float = 120) -> dict[str, list[ValidationIssue]]:
    """Run foamDictionary -expand on each file (paths relative to the case). Empty list = OK."""
    case_wsl = win_to_wsl(str(case_root))
    q = shlex.quote
    mirror = not openfoam_path_safety(case_wsl).safe
    body = prelude(fork)
    if mirror:
        body += ("M=$(mktemp -d /tmp/aethercfd-validate.XXXXXX)\n"
                 f"rsync -a --exclude=polyMesh --include='/system/***' --include='/constant/***' "
                 f"--exclude='/*' {q(case_wsl)}/ \"$M/\"\n"
                 'cd "$M"\n')
    else:
        body += f"cd {q(case_wsl)}\n"
    for rel in rel_files:
        rp = str(PurePosixPath(rel))
        body += (f'echo "@@FILE {rp}"\n'
                 f"foamDictionary -expand {q(rp)} > /dev/null 2> /tmp/aethercfd-validate-err.$$ ; rc=$?\n"
                 'echo "@@RC $rc"; cat /tmp/aethercfd-validate-err.$$; echo "@@END"\n')
    body += "rm -f /tmp/aethercfd-validate-err.$$\n"
    if mirror:
        body += 'cd /; rm -rf "$M"\n'
    res = runner.run_script(body, timeout_s=timeout_s)
    out: dict[str, list[ValidationIssue]] = {r: [] for r in rel_files}
    cur = None
    rc = 0
    buf: list[str] = []
    for ln in res.stdout.splitlines():
        if ln.startswith("@@FILE "):
            cur, buf, rc = ln[7:].strip(), [], 0
        elif ln.startswith("@@RC ") and cur is not None:
            rc = int(ln[5:].strip() or 0)
        elif ln == "@@END" and cur is not None:
            text = "\n".join(buf)
            if rc != 0 or "FOAM FATAL" in text:
                line_no, msg = _parse_fatal(text)
                out[cur].append(ValidationIssue(cur, line_no, msg))
            cur = None
        elif cur is not None:
            buf.append(ln)
    return out
