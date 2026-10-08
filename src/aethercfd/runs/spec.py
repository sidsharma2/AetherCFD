"""What to run: a sequence of OpenFOAM applications against one case, with one fork, placement and
parallel layout. Pure data; turned into bash by ``scripts.py``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from aethercfd.backend.wsl.forks import Family, ForkProfile


class StepKind(StrEnum):
    SOLVER = "solver"  # long-running, gets signals, guards and progress parsing
    UTILITY = "utility"  # blockMesh, checkMesh, decomposePar, postProcess ...
    SCRIPT = "script"  # ./Allrun etc. (run as given)


@dataclass(frozen=True)
class RunStep:
    app: str  # executable name ("simpleFoam", "foamRun", "blockMesh", "./Allrun")
    args: tuple[str, ...] = ()
    kind: StepKind = StepKind.UTILITY
    parallel: bool = False  # run under mpirun with -parallel
    log_name: str | None = None  # default: log.<app>

    @property
    def log(self) -> str:
        if self.log_name:
            return self.log_name
        base = self.app.split("/")[-1].lstrip(".")
        return f"log.{base}"

    @property
    def label(self) -> str:
        return " ".join((self.app, *self.args))


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.+/-]+$")


@dataclass(frozen=True)
class RunSpec:
    case_wsl: str  # where OpenFOAM runs (in-place /mnt/... or the ext4 stage)
    fork: ForkProfile
    steps: tuple[RunStep, ...]
    run_dir_wsl: str  # .aethercfd/local/runs/<id> as seen from WSL (may contain spaces)
    linux_run_dir: str  # ext4 run-private dir without spaces (signal site dir, pid files)
    run_id: str
    nprocs: int = 1
    enable_signals: bool = True
    nice: int = 10
    file_cap_gb: float = 10.0  # ulimit -f backstop per process (bash units: 1024-byte blocks)
    env: dict[str, str] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems = []
        if not self.steps:
            problems.append("no steps to run")
        for s in self.steps:
            if not _SAFE_NAME.match(s.app):
                problems.append(f"unsafe application name {s.app!r}")
            if s.parallel and self.nprocs < 2:
                problems.append(f"{s.app} is marked parallel but nprocs={self.nprocs}")
        if " " in self.case_wsl or "'" in self.case_wsl or '"' in self.case_wsl:
            problems.append(f"OpenFOAM cannot run in {self.case_wsl!r} (spaces/quotes); use a staged copy")
        if " " in self.linux_run_dir:
            problems.append("linux_run_dir must not contain spaces")
        return problems

    @property
    def solver_steps(self) -> list[RunStep]:
        return [s for s in self.steps if s.kind is StepKind.SOLVER]

    def signal_args(self) -> tuple[str, ...]:
        """Extra solver arguments that enable write-now (SIGUSR1) / stop-at-write-now (SIGUSR2)."""
        if not self.enable_signals or self.fork.family is not Family.ESI:
            return ()
        return ("-opt-switch", "writeNowSignal=10", "-opt-switch", "stopAtWriteNowSignal=12")

    @property
    def uses_site_dir(self) -> bool:
        """Foundation has no -opt-switch: switches come from a run-private WM_PROJECT_SITE (ADR 0006)."""
        return self.enable_signals and self.fork.family is Family.FOUNDATION
