"""Cluster runs (ADR 0007): submit through a scheduler, mirror the remote run files into the local run folder,
and answer the run manager's liveness question from the scheduler.

The run manager keeps one monitor loop for every run. For a cluster run this class mirrors ``steps.jsonl``,
``exit_code``, ``guard_stop.json`` and ``job.out`` into ``<run dir>/`` and the remote ``log.*`` into
``<run dir>/case/``. From there the log tail, residual parser, guards and the Residuals / Solver Log panels
work exactly as for a local run.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aethercfd.backend.wsl.forks import Family, ForkProfile, family_of_version
from aethercfd.backend.wsl.runner import ScriptResult
from aethercfd.core.clusters import ClusterProfile
from aethercfd.core.paths import win_to_wsl
from aethercfd.core.textio import write_generated_text
from aethercfd.project.journal import new_id
from aethercfd.runs.cluster.scheduler import (
    JobPhase,
    JobRequest,
    JobStatus,
    SchedulerError,
    get_scheduler,
    job_phase,
)
from aethercfd.runs.cluster.scripts import batch_script
from aethercfd.runs.cluster.transport import (
    UNREACHABLE,
    Transport,
    WslSshTransport,
    failure_text,
    parse_work_dir,
    resolve_work_dir_script,
    unreachable,
)
from aethercfd.runs.placement import slug
from aethercfd.runs.spec import RunSpec, RunStep, StepKind
from aethercfd.runs.targets import find_profile, parse_target

if TYPE_CHECKING:
    from aethercfd.case.model import Case
    from aethercfd.runs.manager import RunManager, RunRecord, _Monitor

log = logging.getLogger(__name__)
q = shlex.quote
_TIME = re.compile(r"^[-+]?[0-9.]+([eE][-+]?[0-9]+)?$")
LS_END = "AETHER_LS_END"
RESOURCE_KEYS = ("walltime", "nodes", "tasks_per_node", "partition", "account", "qos", "mem")


def cluster_fork(profile: ClusterProfile) -> ForkProfile:
    """The OpenFOAM the profile's env_setup loads, as a fork profile (decides the write-now switches)."""
    ver = profile.openfoam_version
    return ForkProfile(id=f"cluster-{profile.name}", family=family_of_version(ver) if ver else Family.ESI,
                       version=ver or "?", distro="", bashrc="", project_dir="", mpi_run_as_root=False)


def remote_dirs(work_dir: str, project_id: str, case: Path, run_id: str) -> tuple[str, str]:
    base = f"{work_dir}/{slug(project_id)}/{slug(case.name)}-{slug(str(case))[-24:]}"
    return f"{base}/case", f"{base}/runs/{run_id}"


class ClusterTarget:
    UNREACHABLE_BACKOFF_S = 60.0  # after a failed SSH call, do not call that profile again for this long

    def __init__(self, mgr: RunManager, *, transport_factory: Callable[[ClusterProfile], Transport] | None = None):
        self.mgr = mgr
        self.transport_factory = transport_factory or (lambda p: WslSshTransport(p, mgr.runner))
        self._lock = threading.Lock()
        self._down_until: dict[str, float] = {}
        self._down_runs: set[str] = set()  # runs told "cluster unreachable" (told again only after a recovery)
        self._last_status: dict[str, JobStatus] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def profile_of(rec: RunRecord) -> ClusterProfile:
        return ClusterProfile.model_validate((rec.remote or {})["profile"])

    def transport(self, profile: ClusterProfile) -> Transport:
        return self.transport_factory(profile)

    def _is_down(self, profile: ClusterProfile) -> bool:
        with self._lock:
            return time.monotonic() < self._down_until.get(profile.name, 0.0)

    def _down(self, profile: ClusterProfile, rec: RunRecord | None, res: ScriptResult) -> None:
        with self._lock:
            self._down_until[profile.name] = time.monotonic() + self.UNREACHABLE_BACKOFF_S
            tell = rec is not None and rec.id not in self._down_runs
            if rec is not None:
                self._down_runs.add(rec.id)
        if tell and rec is not None:
            self.mgr._publish("run.guard", rec, level="REVIEW", guard="cluster",
                              message=f"cluster {profile.name} unreachable ({failure_text(res, limit=160)}); "
                                      "the job keeps running. Monitoring resumes when the connection is back "
                                      "(MFA clusters: Run > Clusters… > Connect)")

    def _up(self, profile: ClusterProfile, rec: RunRecord | None) -> None:
        with self._lock:
            self._down_until.pop(profile.name, None)
            if rec is not None:
                self._down_runs.discard(rec.id)

    def exec_case_win(self, rec: RunRecord) -> Path:
        return Path(rec.run_dir) / "case"

    # ------------------------------------------------------------------ prepare
    def prepare(self, case: Case, fork: ForkProfile, steps: Sequence[RunStep], *, target: str, nprocs: int,
                enable_signals: bool, resources: dict) -> tuple[RunRecord, None]:
        from aethercfd.runs.manager import RunRecord

        _, name = parse_target(target)
        profile = find_profile(self.mgr.settings.clusters, name or "")
        get_scheduler(profile.scheduler)  # unsupported scheduler: fail before anything is written
        if any(s.kind is StepKind.SCRIPT for s in steps):
            raise ValueError("tutorial scripts (./Allrun) do not run on clusters yet: they need the whole case tree")
        if any(s.parallel for s in steps) and nprocs < 2:
            raise ValueError("parallel steps need nprocs >= 2")
        res = {k: v for k, v in resources.items() if k in RESOURCE_KEYS and v not in (None, "")}
        run_id = new_id()
        run_dir = self.mgr.project.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        rec = RunRecord(id=run_id, case_root=str(case.root), exec_case_wsl="", run_dir=str(run_dir),
                        run_dir_wsl=win_to_wsl(str(run_dir)), linux_run_dir="", fork=fork.to_json(),
                        placement="cluster", placement_reason=f"{profile.name} ({profile.destination})",
                        steps=[{"app": s.app, "args": list(s.args), "kind": s.kind.value, "parallel": s.parallel,
                                "log": s.log} for s in steps],
                        nprocs=nprocs, signals_enabled=enable_signals,
                        created=datetime.now(UTC).isoformat(timespec="seconds"), target=profile.target,
                        remote={"profile": profile.model_dump(), "resources": res, "scheduler": profile.scheduler})
        rec.save()
        return rec, None

    # ------------------------------------------------------------------ launch
    def _fail(self, rec: RunRecord, why: str) -> RunRecord:
        from aethercfd.runs.manager import RunState

        self.mgr._set_state(rec, RunState.FAILED, stop_reason=why[:400])
        return rec

    def launch(self, rec: RunRecord, *, start_time: str | None) -> RunRecord:
        try:
            return self._launch(rec, start_time=start_time)
        except Exception as exc:  # wsl.exe missing or stuck, the distro stopped: a plain reason, never a traceback
            log.exception("cluster launch of run %s failed", rec.id)
            return self._fail(rec, f"cannot reach cluster {self.profile_of(rec).name}: {exc}")

    def _launch(self, rec: RunRecord, *, start_time: str | None) -> RunRecord:
        from aethercfd.runs.manager import RunState

        profile = self.profile_of(rec)
        tr = self.transport(profile)
        sched = get_scheduler(profile.scheduler)
        self.mgr._set_state(rec, RunState.STAGING)
        # 1) where on the cluster ($SCRATCH, ~ expand there)
        res = tr.run(resolve_work_dir_script(profile), timeout_s=120)
        work = parse_work_dir(res.stdout) if res.ok else None
        if work is None:
            return self._fail(rec, f"cluster {profile.name}: cannot reach it or create work dir {profile.work_dir}: "
                                   f"{failure_text(res)}")
        self._up(profile, rec)
        case_dir, run_dir = remote_dirs(work, self.mgr.project.id, Path(rec.case_root), rec.id)
        rec.exec_case_wsl, rec.linux_run_dir = case_dir, run_dir
        rec.remote = {**(rec.remote or {}), "work_dir": work, "case_dir": case_dir, "run_dir": run_dir}
        rec.save()
        # 2) never overwrite results on the cluster that were never brought back (same rule as the ext4 stage)
        res = tr.run(f"ls -1A {q(case_dir)} 2>/dev/null; echo {LS_END}\n", timeout_s=120)
        if LS_END not in res.stdout:
            return self._fail(rec, f"cannot list {case_dir} on {profile.name}: {res.stderr.strip()[-300:]}")
        names = [ln.strip() for ln in res.stdout.split(LS_END)[0].splitlines() if ln.strip()]
        local = {p.name for p in Path(rec.case_root).iterdir() if p.is_dir()}
        unsynced = sorted(n for n in names if _TIME.match(n) and n not in local)
        if unsynced and ".aethercfd_synced" not in names:
            return self._fail(rec, f"the cluster copy {case_dir} holds results never downloaded: "
                                   f"{' '.join(unsynced[:8])}; download them (the earlier run's Download results) "
                                   "or remove them on the cluster first")
        # 3) upload inputs
        res = tr.upload_case(Path(rec.case_root), case_dir, start_time=start_time)
        if not res.ok:
            return self._fail(rec, f"upload to {profile.name} failed: {failure_text(res)}")
        # 4) batch script (a copy stays in the local run folder)
        steps = tuple(RunStep(s["app"], tuple(s.get("args", ())), StepKind(s.get("kind", "utility")),
                              bool(s.get("parallel")), s.get("log")) for s in rec.steps)
        spec = RunSpec(case_wsl=case_dir, fork=rec.fork_profile, steps=steps, run_dir_wsl=run_dir,
                       linux_run_dir=run_dir, run_id=rec.id, nprocs=rec.nprocs, enable_signals=rec.signals_enabled,
                       file_cap_gb=self.mgr.settings.guards.log_hard_cap_gb)
        problems = spec.validate()
        if problems:
            return self._fail(rec, "; ".join(problems))
        r = (rec.remote or {}).get("resources", {})
        job = JobRequest(name=f"aether-{slug(Path(rec.case_root).name)[:40]}", output=f"{run_dir}/job.out",
                         workdir=case_dir, ntasks=rec.nprocs, nodes=int(r.get("nodes", profile.nodes) or 0),
                         tasks_per_node=int(r.get("tasks_per_node", profile.tasks_per_node) or 0),
                         walltime=str(r.get("walltime", profile.walltime)),
                         partition=str(r.get("partition", profile.partition)),
                         account=str(r.get("account", profile.account)), qos=str(r.get("qos", profile.qos)),
                         mem=str(r.get("mem", profile.mem)), extra=tuple(profile.sbatch_extra),
                         stop_signal_lead_s=profile.write_before_walltime_s if rec.signals_enabled else 0)
        text = batch_script(spec, profile, sched, job)
        write_generated_text(Path(rec.run_dir) / "job.sbatch", text)
        res = tr.put_text(f"{run_dir}/job.sbatch", text)
        if not res.ok:
            return self._fail(rec, f"cannot write the batch script on {profile.name}: {res.stderr.strip()[-300:]}")
        # 5) submit
        self.mgr._set_state(rec, RunState.LAUNCHING)
        res = tr.run(f"cd {q(run_dir)} && {sched.submit_cmd('job.sbatch')}\n", timeout_s=120)
        try:
            if not res.ok:
                raise SchedulerError((res.stderr or res.stdout).strip()[-300:])
            job_id = sched.parse_submit(res.stdout)
        except SchedulerError as exc:
            return self._fail(rec, f"submit to {profile.name} failed: {exc}")
        self.mgr._set_state(rec, RunState.PENDING, job_id=job_id, scheduler_state="PENDING")
        self.mgr._start_monitor(rec)
        return rec

    # ------------------------------------------------------------------ controls
    def control(self, rec: RunRecord, action: str) -> tuple[bool, str]:
        from aethercfd.runs.manager import RunState

        if rec.job_id is None:
            return False, "this run was never submitted"
        if action in ("pause", "resume"):
            return False, "pause/resume is not available for cluster jobs (it needs operator rights); use Stop & write"
        if action in ("write_now", "stop_write") and not rec.signals_enabled:
            return False, ("this run was started without write-now signals; sending them would kill the solver. "
                           "Use 'terminate' instead")
        if action in ("write_now", "stop_write") and rec.state == RunState.PENDING.value:
            return False, "the job is still waiting in the queue: nothing to write yet (Kill cancels it)"
        if action not in ("write_now", "stop_write", "terminate", "kill"):
            return False, f"unknown action {action!r}"
        profile = self.profile_of(rec)
        if self._is_down(profile):  # no SSH call (and no wait) while the backoff says the cluster is down
            return False, (f"cluster {profile.name} is unreachable just now; the job keeps running. Try again in a "
                           "minute (MFA clusters: Run > Clusters… > Connect)")
        sched = get_scheduler(profile.scheduler)
        run_dir = (rec.remote or {}).get("run_dir") or rec.linux_run_dir
        before = (rec.state, rec.stop_reason)
        if action in ("stop_write", "terminate", "kill"):
            self.mgr._set_state(rec, RunState.STOPPING, stop_reason=f"user: {action}")
        touch = f"touch {q(run_dir)}/stop_requested 2>/dev/null\n"
        script = {"write_now": sched.signal_batch_cmd(rec.job_id, "USR1") + "\n",
                  "stop_write": touch + sched.signal_batch_cmd(rec.job_id, "USR2") + "\n",
                  "terminate": touch + sched.cancel_cmd(rec.job_id) + "\n",
                  "kill": touch + sched.kill_cmd(rec.job_id) + "\n"}[action]
        try:
            res = self.transport(profile).run(script, timeout_s=60)
        except Exception as exc:
            log.exception("%s of run %s failed", action, rec.id)
            res = ScriptResult(UNREACHABLE, "", f"cannot run ssh in the WSL distro: {exc}")
        if unreachable(res):
            self._down(profile, rec, res)
            if rec.state == RunState.STOPPING.value and (rec.state, rec.stop_reason) != before:
                self.mgr._set_state(rec, RunState(before[0]), stop_reason=before[1])  # nothing reached the job
            return False, f"cluster {profile.name} unreachable: {failure_text(res)}; nothing was sent to the job"
        return res.ok, (res.stderr or res.stdout).strip()

    # ------------------------------------------------------------------ liveness
    def status(self, rec: RunRecord) -> JobStatus | None:
        if rec.job_id is None:
            return None
        profile = self.profile_of(rec)
        if self._is_down(profile):
            return None
        sched = get_scheduler(profile.scheduler)
        try:
            res = self.transport(profile).run(sched.status_script(rec.job_id), timeout_s=60)
        except Exception:
            log.info("status query for run %s failed", rec.id, exc_info=True)
            return None
        if unreachable(res):
            self._down(profile, rec, res)
            return None
        self._up(profile, rec)
        return sched.parse_status(rec.job_id, res.stdout)

    def is_alive(self, rec: RunRecord) -> bool | None:
        from aethercfd.runs.manager import RunState

        st = self.status(rec)
        if st is None:
            return None
        self._last_status[rec.id] = st
        word = st.state or st.phase.value.upper()
        if rec.scheduler_state != word:
            rec.scheduler_state = word
            rec.save()
        if st.phase is JobPhase.RUNNING and rec.state == RunState.PENDING.value:
            self.mgr._set_state(rec, RunState.RUNNING)
        elif st.phase is JobPhase.QUEUED and rec.state == RunState.RUNNING.value:
            self.mgr._set_state(rec, RunState.PENDING)  # requeued (preemption, node failure)
        if not st.alive:
            self._pull_files(rec)  # an exit_code that just appeared ends the run normally
        return st.alive

    @staticmethod
    def ended_reason(rec: RunRecord) -> str | None:
        """How the scheduler ended the job, if its last answer was a terminal state."""
        word = rec.scheduler_state or ""
        return f"scheduler: {word}" if job_phase(word) is JobPhase.DONE else None

    def end_without_exit_code(self, m: _Monitor) -> bool:
        """Two definite DEADs and no exit_code: say how the scheduler ended the job; "lost" only if it cannot."""
        from aethercfd.runs.manager import RunState

        rec = m.rec
        st = self._last_status.get(rec.id)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.mgr._set_state(rec, RunState.SYNCING)
        self.mgr._sync_back(rec, skip_newest=False)  # whatever the solver wrote before it ended
        if rec.stop_reason and rec.stop_reason.startswith("user"):
            self.mgr._set_state(rec, RunState.KILLED, finished_at=now, exit_code=st.exit_code if st else None)
        elif st is not None and st.phase is JobPhase.DONE:
            ok = st.state == "COMPLETED" and st.exit_code == 0
            self.mgr._set_state(rec, RunState.FINISHED if ok else RunState.FAILED, finished_at=now,
                                exit_code=st.exit_code, stop_reason=None if ok else f"scheduler: {st.state}")
        else:
            self.mgr._set_state(rec, RunState.LOST, stop_reason=f"the scheduler no longer knows job {rec.job_id} "
                                                                "and it left no exit code")
        return True

    # ------------------------------------------------------------------ files
    def _pull_files(self, rec: RunRecord) -> bool:
        profile = self.profile_of(rec)
        remote = rec.remote or {}
        if self._is_down(profile) or not remote.get("run_dir"):
            return False
        try:
            res = self.transport(profile).pull_live(remote["run_dir"], remote["case_dir"], Path(rec.run_dir))
        except Exception:
            log.info("pull for run %s failed", rec.id, exc_info=True)
            return False
        if unreachable(res):
            self._down(profile, rec, res)
            return False
        self._up(profile, rec)
        return True  # a partial transfer (a log renamed mid-copy) still brought what it could

    def pull(self, m: _Monitor, now: float, *, force: bool = False) -> bool:
        from aethercfd.runs.manager import RunState

        rec = m.rec
        if not force:
            if RunState(rec.state) not in (RunState.RUNNING, RunState.STOPPING, RunState.PAUSED):
                return False
            if now - m.last_pull < self.profile_of(rec).poll_s:
                return False
        m.last_pull = now
        return self._pull_files(rec)

    def sync_due(self, m: _Monitor, now: float) -> bool:
        every = self.profile_of(m.rec).sync_every_s
        if every <= 0:
            return False
        if m.last_sync == 0.0:  # not right at the start: nothing is written yet
            m.last_sync = now
            return False
        return now - m.last_sync > every

    def sync_back(self, rec: RunRecord, *, skip_newest: bool) -> ScriptResult:
        profile = self.profile_of(rec)
        case_dir = (rec.remote or {}).get("case_dir")
        if not case_dir:
            return ScriptResult(1, "", "the run never reached the cluster: nothing to download")
        try:
            res = self.transport(profile).download_results(case_dir, Path(rec.case_root), skip_newest=skip_newest,
                                                           processor_dirs=profile.sync_processor_dirs)
        except Exception as exc:
            log.exception("download for run %s failed", rec.id)
            res = ScriptResult(UNREACHABLE, "", f"cannot run rsync in the WSL distro: {exc}")
        if unreachable(res):
            self._down(profile, rec, res)
        return res
