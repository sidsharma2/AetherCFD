"""Batch-scheduler adapters (ADR 0007): script header, submit, status, cancel and signal commands, and the
parsers for their replies. Pure text in, text out, so every reply format is unit-tested without a cluster.

Only Slurm is implemented. A PBS or LSF adapter is one more :class:`Scheduler` subclass in ``SCHEDULERS``.
"""

from __future__ import annotations

import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

q = shlex.quote

STATUS_BEGIN = "AETHER_STATUS"
STATUS_END = "AETHER_END"


class SchedulerError(RuntimeError):
    pass


class JobPhase(StrEnum):
    QUEUED = "queued"  # waiting for resources (or held)
    RUNNING = "running"
    DONE = "done"  # terminal, any outcome (see JobStatus.state for which)
    GONE = "gone"  # the scheduler no longer knows the job (purged from squeue, no accounting)


@dataclass(frozen=True)
class JobStatus:
    phase: JobPhase
    state: str = ""  # the scheduler's own word: PENDING, RUNNING, COMPLETED, TIMEOUT, ...
    reason: str = ""  # squeue's pending reason (Priority, Resources, ...)
    exit_code: int | None = None  # from accounting (ExitCode "2:0" -> 2), when known

    @property
    def alive(self) -> bool:
        return self.phase in (JobPhase.QUEUED, JobPhase.RUNNING)


@dataclass(frozen=True)
class JobRequest:
    name: str
    output: str  # remote path of the scheduler's stdout/stderr file
    workdir: str  # remote case dir
    ntasks: int = 1
    nodes: int = 0
    tasks_per_node: int = 0
    walltime: str = "01:00:00"
    partition: str = ""
    account: str = ""
    qos: str = ""
    mem: str = ""
    extra: tuple[str, ...] = ()
    stop_signal_lead_s: int = 0  # ask the scheduler for SIGUSR2 on the batch shell this long before the walltime
    env: dict[str, str] = field(default_factory=dict)


class Scheduler(ABC):
    name: str = ""
    job_id_var: str = ""  # environment variable holding the job id inside the job
    commands: tuple[str, ...] = ()  # what Test connection looks for on the login node

    @abstractmethod
    def header(self, job: JobRequest) -> list[str]:
        """Directive lines of the batch script (after the shebang)."""

    @abstractmethod
    def submit_cmd(self, script_path: str) -> str: ...

    @abstractmethod
    def parse_submit(self, out: str) -> str:
        """Job id from the submit command's output; raises SchedulerError."""

    @abstractmethod
    def status_script(self, job_id: str) -> str: ...

    @abstractmethod
    def parse_status(self, job_id: str, out: str) -> JobStatus | None:
        """None when the reply says nothing (truncated, scheduler not reachable): never "gone" on doubt."""

    @abstractmethod
    def cancel_cmd(self, job_id: str) -> str: ...

    @abstractmethod
    def kill_cmd(self, job_id: str) -> str: ...

    @abstractmethod
    def signal_batch_cmd(self, job_id: str, signal: str) -> str:
        """Send ``signal`` to the job's batch shell only (which forwards USR1/USR2 to the solver)."""


# ============================================================================ Slurm
_SLURM_QUEUED = {"PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD", "SUSPENDED",
                 "STOPPED", "RESIZING"}
_SLURM_RUNNING = {"RUNNING", "COMPLETING", "SIGNALING", "STAGE_OUT"}
_SLURM_DONE = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED",
               "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT"}
_JOB_ID = re.compile(r"^\d+(_\d+)?$")
_PARSABLE = re.compile(r"^(\d+)(;\S+)?$")
_SUBMITTED = re.compile(r"Submitted batch job (\d+)")


def job_phase(state: str) -> JobPhase | None:
    if state in _SLURM_QUEUED:
        return JobPhase.QUEUED
    if state in _SLURM_RUNNING:
        return JobPhase.RUNNING
    if state in _SLURM_DONE:
        return JobPhase.DONE
    return None


class SlurmScheduler(Scheduler):
    name = "slurm"
    job_id_var = "SLURM_JOB_ID"
    commands = ("sbatch", "squeue", "sacct", "scancel")

    def header(self, job: JobRequest) -> list[str]:
        h = [f"#SBATCH --job-name={job.name}", f"#SBATCH --output={job.output}", f"#SBATCH --chdir={job.workdir}",
             f"#SBATCH --ntasks={job.ntasks}", "#SBATCH --cpus-per-task=1"]
        if job.nodes:
            h.append(f"#SBATCH --nodes={job.nodes}")
        if job.tasks_per_node:
            h.append(f"#SBATCH --ntasks-per-node={job.tasks_per_node}")
        if job.walltime:
            h.append(f"#SBATCH --time={job.walltime}")
        for opt, val in (("partition", job.partition), ("account", job.account), ("qos", job.qos), ("mem", job.mem)):
            if val:
                h.append(f"#SBATCH --{opt}={val}")
        if job.stop_signal_lead_s > 0:
            # B: = the batch shell only; it forwards to the solver (stop-and-write before the walltime kill)
            h.append(f"#SBATCH --signal=B:USR2@{int(job.stop_signal_lead_s)}")
        h += [f"#SBATCH {x.strip()}" for x in job.extra if x.strip()]
        return h

    def submit_cmd(self, script_path: str) -> str:
        return f"sbatch --parsable {q(script_path)}"

    def parse_submit(self, out: str) -> str:
        for ln in reversed(out.strip().splitlines()):  # login banners may precede the reply
            ln = ln.strip()
            if m := _PARSABLE.match(ln):
                return m.group(1)
            if m := _SUBMITTED.search(ln):
                return m.group(1)
        raise SchedulerError(f"sbatch did not return a job id: {out.strip()[-300:]!r}")

    def status_script(self, job_id: str) -> str:
        if not _JOB_ID.match(job_id):
            raise SchedulerError(f"bad job id {job_id!r}")
        return "\n".join([
            f"echo {STATUS_BEGIN}",
            f"out=$(squeue -h -j {job_id} -o '%i|%T|%r' 2>&1); rc=$?",
            'printf "SQ_RC=%s\\n" "$rc"; [ -n "$out" ] && printf "%s\\n" "$out" | sed "s/^/SQ:/"',
            f"out=$(sacct -n -P -X -j {job_id} -o JobID,State,ExitCode 2>&1); rc=$?",
            'printf "SA_RC=%s\\n" "$rc"; [ -n "$out" ] && printf "%s\\n" "$out" | sed "s/^/SA:/"',
            f"echo {STATUS_END}",
            "",
        ])

    def parse_status(self, job_id: str, out: str) -> JobStatus | None:
        lines = [ln.strip() for ln in out.splitlines()]
        try:
            lines = lines[lines.index(STATUS_BEGIN) + 1:]
            lines = lines[:lines.index(STATUS_END)]
        except ValueError:
            return None  # truncated or garbled
        sq_rc = sa_rc = None
        sq: list[str] = []
        sa: list[str] = []
        for ln in lines:
            if ln.startswith("SQ_RC="):
                sq_rc = ln[6:]
            elif ln.startswith("SA_RC="):
                sa_rc = ln[6:]
            elif ln.startswith("SQ:"):
                sq.append(ln[3:])
            elif ln.startswith("SA:"):
                sa.append(ln[3:])
        if sq_rc is None:
            return None
        for ln in sq:
            parts = ln.split("|")
            if len(parts) >= 2 and parts[0].strip() == job_id:
                state = parts[1].strip().split()[0] if parts[1].strip() else ""
                phase = job_phase(state)
                if phase is not None:
                    return JobStatus(phase, state, parts[2].strip() if len(parts) > 2 else "")
        if sa_rc == "0":
            for ln in sa:
                parts = ln.split("|")
                if len(parts) >= 3 and parts[0].strip() == job_id:
                    state = (parts[1].split() or [""])[0]  # "CANCELLED by 1234" -> CANCELLED
                    phase = job_phase(state)
                    if phase is None:
                        continue
                    code = parts[2].split(":")[0].strip()
                    return JobStatus(phase, state, exit_code=int(code) if code.isdigit() else None)
        # squeue answered cleanly that it does not know the job (finished and purged) and accounting has no
        # record of it: the scheduler is definite. Any other squeue failure (controller down) is unknown.
        if sq_rc == "0" and not sq:
            return JobStatus(JobPhase.GONE)
        if any("Invalid job id" in ln for ln in sq):
            return JobStatus(JobPhase.GONE)
        return None

    def cancel_cmd(self, job_id: str) -> str:
        return f"scancel {q(job_id)}"

    def kill_cmd(self, job_id: str) -> str:
        return f"scancel --full --signal=KILL {q(job_id)}; scancel {q(job_id)}"

    def signal_batch_cmd(self, job_id: str, signal: str) -> str:
        if signal not in ("USR1", "USR2", "TERM"):
            raise SchedulerError(f"signal {signal!r} is not sent to jobs")
        return f"scancel --batch --signal={signal} {q(job_id)}"


SCHEDULERS: dict[str, type[Scheduler]] = {"slurm": SlurmScheduler}


def get_scheduler(name: str) -> Scheduler:
    try:
        return SCHEDULERS[name.lower()]()
    except KeyError:
        raise SchedulerError(f"scheduler {name!r} is not supported yet (available: {', '.join(SCHEDULERS)})") from None
