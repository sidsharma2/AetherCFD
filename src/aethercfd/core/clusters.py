"""Cluster connection profiles (ADR 0007). Stored in settings.toml under ``[[clusters]]``.

A profile never holds a password or key material: authentication is an SSH key/agent inside the WSL
distro, or a ControlMaster connection the user opened interactively (MFA clusters).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_HOSTISH = re.compile(r"^[A-Za-z0-9_.@:%\[\]-]*$")  # host, user@host, user@host:port (no spaces, no shell)
_REMOTE_DIR = re.compile(r"^[A-Za-z0-9_./~${}-]+$")  # no spaces/quotes; ~ and $VARS expand on the cluster
_WALLTIME = re.compile(r"^(\d+-)?\d{1,3}(:\d{2}){0,2}$")  # Slurm: MM, MM:SS, HH:MM:SS, D-HH[:MM[:SS]]


def check_walltime(v: str) -> str:
    if not _WALLTIME.match(v.strip()):
        raise ValueError(f"walltime {v!r}: use HH:MM:SS or D-HH:MM:SS")
    return v.strip()


class ClusterProfile(BaseModel):
    name: str  # the target is "cluster:<name>"
    host: str  # login node, as ssh would take it (an alias from ~/.ssh/config in the distro works too)
    user: str = ""  # empty: ssh's default (~/.ssh/config or the WSL user)
    port: int = 22
    identity_file: str = ""  # private key path inside the WSL distro; empty: ssh-agent / ~/.ssh/config
    jump_host: str = ""  # ProxyJump, e.g. "me@gateway.example.org"
    work_dir: str = "~/aethercfd"  # remote root for cases and runs (put it on scratch, e.g. $SCRATCH/aethercfd)
    env_setup: str = ""  # one bash line that loads OpenFOAM, e.g. "module load openfoam/v2306"
    openfoam_version: str = ""  # expected WM_PROJECT_VERSION after env_setup ("v2306", "11"); also picks the fork
    scheduler: str = "slurm"
    partition: str = ""
    account: str = ""
    qos: str = ""
    walltime: str = "01:00:00"
    nodes: int = 0  # 0: let the scheduler place the tasks
    tasks_per_node: int = 0
    mem: str = ""  # e.g. "64G" (per node); empty: the partition default
    mpi_launcher: str = "srun"  # srun | mpirun
    launcher_args: str = ""  # e.g. "--mpi=pmix"
    sbatch_extra: list[str] = Field(default_factory=list)  # extra "#SBATCH ..." option lines (without the prefix)
    write_before_walltime_s: int = 300  # stop-and-write this long before the walltime (0: off)
    use_control_master: bool = True
    control_persist: str = "8h"
    connect_timeout_s: int = 20
    login_shell: bool = True  # bash -l on the cluster (needed for `module` on most systems)
    poll_s: float = 15.0  # mirror logs/run files this often
    sync_every_s: float = 600.0  # download finished time folders this often while running (0: only at the end)
    sync_processor_dirs: bool = False  # also download processor*/ (decomposed results)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("use letters, digits, '_', '.', '-' only")
        return v

    @field_validator("host", "user", "jump_host")
    @classmethod
    def _hostish(cls, v: str) -> str:
        v = v.strip()
        if not _HOSTISH.match(v):
            raise ValueError(f"{v!r}: no spaces or shell characters")
        return v

    @field_validator("work_dir")
    @classmethod
    def _work_dir(cls, v: str) -> str:
        v = v.strip().rstrip("/") or "~"
        if not _REMOTE_DIR.match(v):
            raise ValueError("remote work dir: no spaces or quotes (OpenFOAM cannot run there)")
        return v

    @field_validator("walltime")
    @classmethod
    def _walltime(cls, v: str) -> str:
        return check_walltime(v)

    @field_validator("mpi_launcher")
    @classmethod
    def _launcher(cls, v: str) -> str:
        if v not in ("srun", "mpirun"):
            raise ValueError("mpi_launcher is srun or mpirun")
        return v

    @field_validator("env_setup")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("env_setup is one line (join commands with ';' or '&&')")
        return v.strip()

    @property
    def target(self) -> str:
        return f"cluster:{self.name}"

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host
