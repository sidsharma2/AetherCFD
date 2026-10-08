"""Where a run executes: in place on /mnt/<drive>, or a staged ext4 copy synced back (ADR 0003)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from aethercfd.case.model import Case
from aethercfd.project.project import Placement

q = shlex.quote
_SLUG = re.compile(r"[^A-Za-z0-9_.-]+")


def slug(text: str) -> str:
    return _SLUG.sub("_", text).strip("_")[:60] or "case"


def resolve_placement(case: Case, requested: Placement, *, heavy_io: bool = False) -> tuple[Placement, str]:
    """Return (placement, reason)."""
    if requested is Placement.IN_PLACE:
        if not case.path_safety.safe:
            return Placement.STAGED, f"in-place impossible: path {', '.join(case.path_safety.problems)}"
        return Placement.IN_PLACE, "requested"
    if requested is Placement.STAGED:
        return Placement.STAGED, "requested"
    if not case.path_safety.safe:
        return Placement.STAGED, f"path {', '.join(case.path_safety.problems)}"
    if case.on_onedrive:
        return Placement.STAGED, "case is under OneDrive (file locking/sync conflicts)"
    if heavy_io:
        return Placement.STAGED, "heavy I/O run"
    return Placement.IN_PLACE, "path is OpenFOAM-safe"


@dataclass(frozen=True)
class Stage:
    src_wsl: str  # the Windows case as seen from WSL (/mnt/d/...)
    stage_wsl: str  # ~/aethercfd/stage/<project>/<case>

    def stage_in_sh(self, start_time: str | None) -> str:
        """Copy inputs (system, constant, the start time dir, top-level files) into the stage.
        Refuses if the stage holds result time dirs that were never synced back."""
        includes = ["--include=/system/***", "--include=/constant/***"]
        if start_time:
            includes.append(f"--include=/{start_time}/***")
        return "\n".join([
            f"SRC={q(self.src_wsl)}",
            f"STAGE={q(self.stage_wsl)}",
            'mkdir -p "$STAGE"',
            "unsynced=''",
            'for d in "$STAGE"/*/; do',
            '  n=$(basename "$d"); case "$n" in (*[!0-9.eE+-]*) continue;; esac',
            '  [ -d "$SRC/$n" ] || unsynced="$unsynced $n"',
            "done",
            'if [ -n "$unsynced" ] && [ ! -f "$STAGE/.aethercfd_synced" ]; then',
            '  echo "AETHERCFD: stage has results never synced back:$unsynced" >&2; exit 4',
            "fi",
            "rsync -a " + " ".join(q(i) for i in includes) + " --exclude='/*/' \"$SRC/\" \"$STAGE/\"",
            'rm -f "$STAGE/.aethercfd_synced"',
            "",
        ])

    def sync_back_sh(self, *, skip_newest: bool) -> str:
        """Copy results (time dirs, postProcessing, logs) back. Never touches system/ or constant/.
        ``skip_newest`` while the run is live (its newest time dir may still be being written)."""
        skip = "true" if skip_newest else "false"
        mark_synced = "true" if skip_newest else 'touch "$STAGE/.aethercfd_synced"'
        return "\n".join([
            f"SRC={q(self.src_wsl)}",
            f"STAGE={q(self.stage_wsl)}",
            '[ -d "$STAGE" ] || exit 0',
            "times=$(ls -1 \"$STAGE\" | grep -E '^[-+]?[0-9.]+([eE][-+]?[0-9]+)?$' | sort -g)",
            "newest=$(echo \"$times\" | tail -1)",
            "for t in $times; do",
            f'  if {skip} && [ "$t" = "$newest" ]; then continue; fi',
            '  rsync -a "$STAGE/$t" "$SRC/"',
            "done",
            '[ -d "$STAGE/postProcessing" ] && rsync -a "$STAGE/postProcessing" "$SRC/"',
            'for f in "$STAGE"/log.*; do [ -f "$f" ] && rsync -a "$f" "$SRC/"; done',
            mark_synced,
            "exit 0",
            "",
        ])
