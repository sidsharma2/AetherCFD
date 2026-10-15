"""Where a run executes (ADR 0007): ``"local"`` (the WSL distro, ADR 0006) or ``"cluster:<profile>"``.

:class:`ExecutionTarget` is what the run manager needs from a target. ``ClusterTarget`` implements it; the local
WSL path is still the run manager's own code (moving it behind this protocol is a mechanical follow-up, left out
so that local runs stay exactly as they are).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aethercfd.backend.wsl.forks import ForkProfile
    from aethercfd.backend.wsl.runner import ScriptResult
    from aethercfd.case.model import Case
    from aethercfd.core.clusters import ClusterProfile
    from aethercfd.runs.manager import RunRecord, _Monitor
    from aethercfd.runs.spec import RunStep

LOCAL = "local"
CLUSTER_PREFIX = "cluster:"


class TargetError(ValueError):
    pass


def parse_target(target: str | None) -> tuple[str, str | None]:
    """("local", None) or ("cluster", profile name)."""
    t = (target or LOCAL).strip()
    if t in (LOCAL, "wsl"):
        return LOCAL, None
    if t.startswith(CLUSTER_PREFIX) and len(t) > len(CLUSTER_PREFIX):
        return "cluster", t[len(CLUSTER_PREFIX):]
    raise TargetError(f"unknown run target {target!r}: use 'local' or 'cluster:<profile name>'")


def find_profile(profiles: Sequence[ClusterProfile], name: str) -> ClusterProfile:
    for p in profiles:
        if p.name == name:
            return p
    known = ", ".join(p.name for p in profiles) or "none yet"
    raise TargetError(f"no cluster profile named {name!r} (profiles: {known}); add one under Run > Clusters…")


class ExecutionTarget(Protocol):
    def prepare(self, case: Case, fork: ForkProfile, steps: Sequence[RunStep], *, target: str, nprocs: int,
                enable_signals: bool, resources: dict) -> tuple[RunRecord, None]: ...

    def launch(self, rec: RunRecord, *, start_time: str | None) -> RunRecord: ...

    def control(self, rec: RunRecord, action: str) -> tuple[bool, str]: ...

    def is_alive(self, rec: RunRecord) -> bool | None:
        """True / False (definite: DEAD) / None (the answer says nothing): the same contract as the WSL probe."""
        ...

    def pull(self, m: _Monitor, now: float, *, force: bool = False) -> bool:
        """Bring the run's files to where the monitor reads them; True when fresh files arrived."""
        ...

    def sync_back(self, rec: RunRecord, *, skip_newest: bool) -> ScriptResult: ...

    def exec_case_win(self, rec: RunRecord) -> Path: ...
