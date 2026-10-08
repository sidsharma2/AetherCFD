"""OpenFOAM fork profiles and the isolation prelude (ADR 0006).

ESI (openfoam.com, versions like ``v2306``) and the Foundation (openfoam.org, versions like ``14``)
differ in solvers, dictionaries and run control, and must never be sourced into the same shell.
Every script AetherCFD runs starts with :func:`prelude`, which refuses (exit 97) if an OpenFOAM
environment is already present and verifies (exit 98) that the intended one was loaded.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass
from enum import StrEnum

from aethercfd.backend.wsl.runner import WslRunner

EXIT_ALREADY_SOURCED = 97
EXIT_FORK_MISMATCH = 98
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class Family(StrEnum):
    ESI = "esi"  # openfoam.com, vYYMM
    FOUNDATION = "foundation"  # openfoam.org, integer versions / dev


@dataclass(frozen=True)
class ForkProfile:
    id: str  # stable id, e.g. "esi-v2306", "foundation-14"
    family: Family
    version: str  # WM_PROJECT_VERSION as reported
    distro: str
    bashrc: str  # absolute path inside the distro
    project_dir: str  # expected WM_PROJECT_DIR after sourcing
    mpi_run_as_root: bool = False

    @property
    def label(self) -> str:
        return f"OpenFOAM {'ESI ' if self.family is Family.ESI else 'Foundation '}{self.version}"

    @property
    def short_tag(self) -> str:
        return self.version if self.family is Family.ESI else f"of{self.version}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["family"] = self.family.value
        return d

    @classmethod
    def from_json(cls, d: dict) -> ForkProfile:
        return cls(**{**d, "family": Family(d["family"])})


def family_of_version(version: str) -> Family:
    return Family.ESI if version.startswith("v") else Family.FOUNDATION


def prelude(fork: ForkProfile) -> str:
    """Bash text that isolates and loads exactly one OpenFOAM fork (LF only)."""
    return (
        f"export PATH={SAFE_PATH}\n"
        'if [ -n "${WM_PROJECT_DIR:-}" ]; then\n'
        '  echo "AETHERCFD: refusing to run: an OpenFOAM environment is already loaded ($WM_PROJECT_DIR)" >&2\n'
        f"  exit {EXIT_ALREADY_SOURCED}\n"
        "fi\n"
        "set +u\n"
        f". {shlex.quote(fork.bashrc)} >/dev/null 2>&1\n"
        f'if [ "${{WM_PROJECT_DIR:-}}" != {shlex.quote(fork.project_dir)} ]; then\n'
        f'  echo "AETHERCFD: expected {fork.label} at {fork.project_dir}, got ${{WM_PROJECT_DIR:-nothing}}" >&2\n'
        f"  exit {EXIT_FORK_MISMATCH}\n"
        "fi\n"
    )


# Discovers every OpenFOAM install in a distro by sourcing each bashrc in its own subshell.
# NOTE: not `env -i`: ESI v2306's etc/bashrc recurses until bash segfaults (~60 s) when USER/LOGNAME
# are stripped. A plain subshell of the clean wsl.exe environment (no rc files sourced) is isolated
# enough: nothing OpenFOAM-related is set before each subshell starts.
_PROBE = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
for rc in /usr/lib/openfoam/openfoam*/etc/bashrc /opt/openfoam*/etc/bashrc /opt/OpenFOAM-*/etc/bashrc \
          "$HOME"/OpenFOAM/OpenFOAM-*/etc/bashrc; do
  [ -f "$rc" ] || continue
  (
    unset WM_PROJECT_DIR WM_PROJECT_VERSION FOAM_INST_DIR
    set +u; . "$rc" >/dev/null 2>&1
    printf "%s\t%s\t%s\t%s\n" "$rc" "${WM_PROJECT_DIR:-}" "${WM_PROJECT_VERSION:-}" "$(id -u)"
  ) </dev/null
done
"""


def probe_forks(runner: WslRunner, *, timeout_s: float = 120) -> list[ForkProfile]:
    res = runner.run_script(_PROBE, timeout_s=timeout_s)
    forks: list[ForkProfile] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[1] or not parts[2]:
            continue
        bashrc, pdir, version, uid = parts
        fam = family_of_version(version)
        fid = f"{fam.value}-{version}"
        forks.append(ForkProfile(id=fid, family=fam, version=version, distro=runner.distro, bashrc=bashrc,
                                 project_dir=pdir, mpi_run_as_root=uid.strip() == "0"))
    return forks


def forks_to_json(forks: list[ForkProfile]) -> str:
    return json.dumps([f.to_json() for f in forks], indent=2)
