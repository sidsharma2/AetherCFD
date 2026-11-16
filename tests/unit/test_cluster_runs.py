"""Cluster runs end to end through RunManager, against a fake Slurm cluster on the local disk (ADR 0007).

Submit -> pending -> running -> residuals stream from the mirrored remote log -> results download -> finished, plus
write-now / stop-and-write / cancel, an unreachable cluster (never "lost"), scheduler-ended jobs, walltime stops and
reattach. Monitor ticks are driven by hand (no threads, no sleeps).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fake_cluster import FakeCluster, solver_log

from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.case.model import load_case
from aethercfd.commands.base import USER, Risk
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.core.clusters import ClusterProfile
from aethercfd.core.settings import Settings
from aethercfd.project.project import Project
from aethercfd.runs.cluster.target import ClusterTarget, cluster_fork
from aethercfd.runs.manager import RunManager, RunRecord, RunState, _Monitor, default_steps

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
PROFILE = ClusterProfile(name="hpc", host="login.example.org", user="sid", openfoam_version="v2306",
                         env_setup="module load openfoam/v2306", partition="compute", account="def-sid",
                         poll_s=0, sync_every_s=0)
ESI = ForkProfile("esi-v2306", Family.ESI, "v2306", "LMAE-OpenFOAM", "/a/etc/bashrc", "/a", True)


class Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))

    def topics(self, topic: str) -> list[dict]:
        return [p for t, p in self.events if t == topic]


class NoLocalRunner:
    """A cluster run must never touch the local WSL distro (except through the transport)."""

    distro = "LMAE-OpenFOAM"

    def run_script(self, *a, **k):
        raise AssertionError("the local WSL runner was used for a cluster run")


class ManualManager(RunManager):
    """Monitors without threads: tests call tick()."""

    def _start_monitor(self, rec, *, dead_replies: int = 0) -> None:
        m = _Monitor(rec, dead_replies=dead_replies)
        if dead_replies:
            m.last_alive_check = time.monotonic() - self.ALIVE_EVERY_S + self.DEAD_RECHECK_S
        self._monitors[rec.id] = m

    def tick(self, run_id: str, *, probe: bool = False) -> bool:
        m = self._monitors[run_id]
        m.last_alive_check = float("-inf") if probe else time.monotonic()
        done = self._tick(m, Path(m.rec.run_dir))
        if done:
            self._monitors.pop(run_id)
        return done


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "cavity"
    shutil.copytree(FIX / "esi_cavity", case)
    (case / "log.icoFoam").write_text("an earlier local run's log: never uploaded\n")
    proj = Project.create(case)
    fake = FakeCluster(tmp_path / "remote")
    mgr = ManualManager(proj, NoLocalRunner(), settings=Settings(clusters=[PROFILE]), bus=Bus())  # type: ignore[arg-type]
    mgr._cluster = ClusterTarget(mgr, transport_factory=lambda p: fake)
    return mgr, fake, case


def _submit(mgr, case, *, nprocs=4, signals=True, **resources) -> RunRecord:
    c = load_case(case)
    steps = default_steps(c, mesh=False, check=False, solve=True, nprocs=nprocs)
    rec, spec = mgr.prepare(c, cluster_fork(PROFILE), steps, nprocs=nprocs, enable_signals=signals,
                            target="cluster:hpc", resources=resources)
    assert spec is None and rec.state == RunState.QUEUED.value and rec.is_cluster
    return mgr.launch(rec, None, start_time="0")


def _run(mgr, fake, rec) -> str:
    """Submit -> the scheduler starts the job -> the monitor sees it running."""
    job = rec.job_id
    fake.start(job)
    mgr.tick(rec.id, probe=True)
    assert rec.state == RunState.RUNNING.value
    return job


# ---------------------------------------------------------------------------------------------- the happy path
def test_submit_monitor_and_download(env):
    mgr, fake, case = env
    system_before = {p.name: p.read_bytes() for p in (case / "system").iterdir()}
    rec = _submit(mgr, case, walltime="02:00:00")
    assert rec.state == RunState.PENDING.value and rec.job_id == "41000" and rec.placement == "cluster"
    remote_case = fake.local(rec.remote["case_dir"])
    assert rec.remote["case_dir"].startswith("/scratch/sid/aethercfd/") and rec.exec_case_wsl == rec.remote["case_dir"]
    assert (remote_case / "system" / "controlDict").exists() and (remote_case / "0" / "U").exists()
    assert not (remote_case / "log.icoFoam").exists()
    script = fake.jobs[rec.job_id]["script"]
    assert (Path(rec.run_dir) / "job.sbatch").read_text() == script
    assert "#SBATCH --time=02:00:00" in script and "#SBATCH --partition=compute" in script
    assert "srun -n 4 icoFoam -opt-switch writeNowSignal=10" in script and "decomposePar -force" in script

    mgr.tick(rec.id, probe=True)  # still queued
    assert rec.state == RunState.PENDING.value and rec.scheduler_state == "PENDING"
    job = _run(mgr, fake, rec)

    fake.step(job, 0, "running", "log.decomposePar")
    fake.log(job, "log.decomposePar", "Decomposing mesh\nEnd\n")
    fake.step(job, 0, "done", rc=0)
    fake.step(job, 1, "running", "log.icoFoam")
    fake.log(job, "log.icoFoam", solver_log([0.005, 0.01, 0.015]))
    mgr.tick(rec.id)
    states = [(p["step"], p["state"]) for p in mgr.bus.topics("run.step_state")]
    assert (1, "running") in states
    assert mgr.exec_case_win(rec) == Path(rec.run_dir) / "case"
    assert (Path(rec.run_dir) / "case" / "log.icoFoam").exists()  # the local mirror the Residuals panel reads
    logs = [ln for p in mgr.bus.topics("run.log") if p["log"] == "log.icoFoam" for ln in p["lines"]]
    assert "Time = 0.01" in logs
    steps = [p["step"] for p in mgr.bus.topics("run.progress")]
    assert len(steps) >= 2 and {"Ux", "p"} <= set(steps[0].residuals)

    fake.log(job, "log.icoFoam", solver_log([0.02, 0.025]))
    fake.write_time(job, "0.025")
    fake.step(job, 1, "done", rc=0)
    fake.finish(job, 0)
    assert mgr.tick(rec.id)
    assert rec.state == RunState.FINISHED.value and rec.exit_code == 0
    assert fake.downloads == [False]  # one final download
    assert (case / "0.025" / "U").exists() and "Time = 0.025" in (case / "log.icoFoam").read_text()
    assert {p.name: p.read_bytes() for p in (case / "system").iterdir()} == system_before
    assert (remote_case / ".aethercfd_synced").exists()
    assert [p["state"] for p in mgr.bus.topics("run.state")][-2:] == ["syncing", "finished"]
    assert mgr.bus.topics("run.synced")[-1]["final"] is True


def test_periodic_download_skips_the_newest_time(env):
    mgr, fake, case = env
    mgr.settings.clusters[0] = PROFILE.model_copy(update={"sync_every_s": 600})
    rec = _submit(mgr, case)
    rec.remote["profile"]["sync_every_s"] = 600
    job = _run(mgr, fake, rec)
    fake.write_time(job, "0.1")
    fake.write_time(job, "0.2")
    m = mgr._monitors[rec.id]
    mgr.tick(rec.id)  # the first chance only starts the clock
    m.last_sync -= 601
    mgr.tick(rec.id)
    assert fake.downloads == [True] and (case / "0.1").exists() and not (case / "0.2").exists()


def test_a_newer_record_field_set_still_loads(env):
    mgr, _fake, case = env
    rec = _submit(mgr, case)
    data = json.loads((Path(rec.run_dir) / "run.json").read_text())
    data["a_field_from_the_future"] = 1
    (Path(rec.run_dir) / "run.json").write_text(json.dumps(data))
    assert RunRecord.load(Path(rec.run_dir)).job_id == rec.job_id
    old = {k: v for k, v in data.items() if k not in ("target", "job_id", "scheduler_state", "remote",
                                                       "a_field_from_the_future")}
    (Path(rec.run_dir) / "run.json").write_text(json.dumps(old))
    assert RunRecord.load(Path(rec.run_dir)).target == "local"  # records written before ADR 0007


# ---------------------------------------------------------------------------------------------- controls
def test_write_now_and_stop_write_signal_the_batch_shell(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    ok, msg = mgr.control(rec.id, "write_now")
    assert not ok and "queue" in msg  # nothing to write while pending
    job = _run(mgr, fake, rec)
    assert mgr.control(rec.id, "write_now")[0]
    assert fake.signals == [(job, "USR1")] and rec.state == RunState.RUNNING.value
    ok, msg = mgr.control(rec.id, "pause")
    assert not ok and "not available" in msg
    assert mgr.control(rec.id, "stop_write")[0]
    assert fake.signals[-1] == (job, "USR2") and rec.state == RunState.STOPPING.value
    assert (fake.local(rec.remote["run_dir"]) / "stop_requested").exists()


def test_signals_disabled_refuses_write_now(env):
    mgr, fake, case = env
    rec = _submit(mgr, case, signals=False)
    _run(mgr, fake, rec)
    ok, msg = mgr.control(rec.id, "write_now")
    assert not ok and "kill the solver" in msg and fake.signals == []
    assert "--signal=B:USR2" not in fake.jobs[rec.job_id]["script"]


def test_cancelling_a_queued_job_ends_killed(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    assert mgr.control(rec.id, "kill")[0]
    assert fake.cancelled == [rec.job_id] and rec.state == RunState.STOPPING.value
    mgr.tick(rec.id, probe=True)  # first DEAD: not enough
    assert rec.state == RunState.STOPPING.value
    assert mgr.tick(rec.id, probe=True)
    assert rec.state == RunState.KILLED.value


# ---------------------------------------------------------------------------------------------- liveness
def test_an_unreachable_cluster_is_unknown_never_lost(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    _run(mgr, fake, rec)
    fake.down = True
    for _ in range(4):
        assert not mgr.tick(rec.id, probe=True)
    m = mgr._monitors[rec.id]
    assert rec.state == RunState.RUNNING.value and m.dead_replies == 0
    told = [p for p in mgr.bus.topics("run.guard") if p["guard"] == "cluster"]
    assert len(told) == 1 and "unreachable" in told[0]["message"]  # said once, not every probe
    n = len(fake.scripts)
    assert mgr.is_alive(rec) is None and len(fake.scripts) == n  # backing off: no ssh call at all
    fake.down = False
    mgr.cluster._down_until.clear()
    assert mgr.is_alive(rec) is True


def test_a_scheduler_that_cannot_answer_is_unknown(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    _run(mgr, fake, rec)
    fake.controller_down = True
    for _ in range(3):
        assert not mgr.tick(rec.id, probe=True)
    assert rec.state == RunState.RUNNING.value and mgr._monitors[rec.id].dead_replies == 0


def test_walltime_without_exit_code_ends_failed_with_the_scheduler_reason(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    fake.write_time(job, "0.3")
    fake.finish(job, 0, state="TIMEOUT", exit_file=False)
    assert not mgr.tick(rec.id, probe=True)
    assert mgr.tick(rec.id, probe=True)
    assert rec.state == RunState.FAILED.value and rec.stop_reason == "scheduler: TIMEOUT"
    assert (case / "0.3").exists()  # whatever was written still comes back


def test_a_job_the_scheduler_forgot_is_lost(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    fake.sacct_enabled = False
    fake.finish(job, 0, exit_file=False)
    fake.purged.add(job)
    mgr.tick(rec.id, probe=True)
    assert mgr.tick(rec.id, probe=True)
    assert rec.state == RunState.LOST.value and job in rec.stop_reason


def test_an_exit_code_that_appears_with_the_first_dead_finishes_normally(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    rec.remote["profile"]["poll_s"] = 3600  # no regular pull this tick: only the one the DEAD reply forces
    mgr._monitors[rec.id].last_pull = time.monotonic()
    fake.finish(job, 3)
    assert not mgr.tick(rec.id, probe=True)  # DEAD #1 pulls the run files: exit_code is now local
    assert (Path(rec.run_dir) / "exit_code").exists()
    assert mgr.tick(rec.id)  # the next poll reads it (as for a local run)
    assert rec.state == RunState.FAILED.value and rec.exit_code == 3 and rec.stop_reason == "scheduler: FAILED"


def test_a_failed_exit_code_is_not_blamed_on_a_stale_scheduler_state(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    fake.finish(job, 3)
    assert mgr.tick(rec.id)  # the regular pull brings exit_code; the last scheduler word was RUNNING
    assert rec.state == RunState.FAILED.value and rec.exit_code == 3 and rec.stop_reason is None


def test_pre_walltime_stop_ends_guard_stopped(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    run_dir = fake.local(rec.remote["run_dir"])
    (run_dir / "guard_stop.json").write_text('{"reason":"walltime: stopped before the time limit","t":1}\n')
    fake.finish(job, 0)
    assert mgr.tick(rec.id)
    assert rec.state == RunState.GUARD_STOPPED.value and rec.guard_stop.startswith("walltime")


def test_requeued_job_goes_back_to_pending(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    fake.jobs[job]["state"] = "PENDING"
    mgr.tick(rec.id, probe=True)
    assert rec.state == RunState.PENDING.value


# ---------------------------------------------------------------------------------------------- reattach / revive
def _fresh_manager(mgr, fake) -> ManualManager:
    m2 = ManualManager(mgr.project, NoLocalRunner(), settings=mgr.settings, bus=Bus())  # type: ignore[arg-type]
    m2._cluster = ClusterTarget(m2, transport_factory=lambda p: fake)
    return m2


def test_reattach_after_restart_uses_the_job_id(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    m2 = _fresh_manager(mgr, fake)
    resumed = m2.reattach()
    assert [r.id for r in resumed] == [rec.id] and rec.id in m2._monitors
    fake.finish(job, 0)
    assert m2.tick(rec.id)
    assert m2.get(rec.id).state == RunState.FINISHED.value


def test_reattach_revives_a_lost_cluster_run_whose_job_still_runs(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    rec.state, rec.stop_reason = RunState.LOST.value, "process disappeared"
    rec.save()
    m2 = _fresh_manager(mgr, fake)
    assert [r.id for r in m2.reattach()] == [rec.id]
    assert m2.get(rec.id).state == RunState.RUNNING.value
    fake.finish(job, 0, state="COMPLETED")
    fake.purged.add(job)
    m3 = _fresh_manager(mgr, fake)
    lost = m3.get(rec.id)
    lost.state = RunState.LOST.value
    lost.save()
    m3._monitors.clear()
    (Path(rec.run_dir) / "exit_code").unlink(missing_ok=True)
    assert m3.reattach() == []  # the scheduler says it ended: not revived


# ---------------------------------------------------------------------------------------------- launch refusals
def test_results_never_downloaded_block_the_next_upload(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    job = _run(mgr, fake, rec)
    fake.write_time(job, "0.5")
    fake.finish(job, 0, exit_file=False, state="CANCELLED")  # gone before anything was downloaded
    fake.jobs[job]["state"] = "CANCELLED"
    rec2 = _submit(mgr, case)  # rec is still active: the command layer would refuse, the manager does not
    assert rec2.state == RunState.FAILED.value and "never downloaded" in rec2.stop_reason
    (fake.local(rec.remote["case_dir"]) / ".aethercfd_synced").touch()
    assert _submit(mgr, case).state == RunState.PENDING.value


def test_unreachable_at_submit_fails_the_run_with_the_reason(env):
    mgr, fake, case = env
    fake.down = True
    rec = _submit(mgr, case)
    assert rec.state == RunState.FAILED.value and "Connection timed out" in rec.stop_reason


def test_allrun_is_refused_on_clusters(env):
    mgr, _fake, case = env
    from aethercfd.runs.spec import RunStep, StepKind

    with pytest.raises(ValueError, match="Allrun"):
        mgr.prepare(load_case(case), cluster_fork(PROFILE), [RunStep("bash", ("./Allrun",), StepKind.SCRIPT)],
                    target="cluster:hpc")


# ---------------------------------------------------------------------------------------------- run.start command
def _executor(mgr):
    ctx = Context(mgr.project, settings=mgr.settings, run_manager=mgr, forks=[ESI])
    return Executor(default_registry(), ctx, AutoApprove())


def test_run_start_plan_for_a_cluster(env):
    mgr, _fake, case = env
    ex = _executor(mgr)
    plan = ex.plan("run.start", {"case": str(case), "target": "cluster:hpc", "nprocs": 4, "walltime": "0-04:00:00",
                                 "account": "def-other"}, USER)
    assert not plan.blocked and plan.params["_resolved"]["placement"] == "cluster"
    assert plan.params["_resolved"]["resources"]["account"] == "def-other"
    assert plan.params["_resolved"]["resources"]["partition"] == "compute"  # profile default
    text = "\n".join(plan.consequences)
    assert "walltime 0-04:00:00" in text and "account def-other" in text and "scancel --batch" in text
    assert any(s.argv[:3] == ("srun", "-n", "4") for s in plan.shell_steps)
    assert all(s.cwd.startswith("hpc:") for s in plan.shell_steps)


@pytest.mark.parametrize(("params", "why"), [
    ({"target": "cluster:nope"}, "no cluster profile named 'nope'"),
    ({"target": "elsewhere"}, "unknown run target"),
    ({"target": "cluster:hpc", "preset": "allrun"}, "Allrun"),
    ({"target": "cluster:hpc", "walltime": "soon"}, "walltime"),
])
def test_run_start_cluster_refusals(env, params, why):
    mgr, _fake, case = env
    if params.get("preset") == "allrun":
        (case / "Allrun").write_text("#!/bin/sh\n")
    plan = _executor(mgr).plan("run.start", {"case": str(case), **params}, USER)
    assert plan.blocked and why in " ".join(c.message for c in plan.blocked)


def test_run_start_family_mismatch_is_blocked(env):
    mgr, _fake, case = env
    mgr.settings.clusters.append(PROFILE.model_copy(update={"name": "org", "openfoam_version": "11"}))
    plan = _executor(mgr).plan("run.start", {"case": str(case), "target": "cluster:org"}, USER)
    assert any("loads OpenFOAM Foundation 11" in c.message for c in plan.blocked)


def test_run_start_submits_in_the_background_and_wait_follows_it(env):
    mgr, fake, case = env
    ex = _executor(mgr)
    res = ex.execute("run.start", {"case": str(case), "target": "cluster:hpc", "nprocs": 2}, USER)
    assert res.ok and "uploading to cluster hpc" in res.message
    rec = mgr.wait(res.data, timeout=30)
    assert rec.state == RunState.PENDING.value and rec.job_id in fake.jobs
    plan = ex.plan("run.control", {"run_id": rec.id, "action": "pause"}, USER)
    assert any("not available for cluster jobs" in c.message for c in plan.blocked)
    plan = ex.plan("run.control", {"run_id": rec.id, "action": "kill"}, USER)
    assert plan.risk is Risk.DESTRUCTIVE and any(rec.job_id in c for c in plan.consequences)


def test_local_runs_are_untouched_by_the_target_field(env):
    mgr, _fake, case = env
    plan = _executor(mgr).plan("run.start", {"case": str(case)}, USER)
    assert plan.params["target"] == "local" and plan.params["_resolved"]["placement"] in ("in-place", "staged")


# ---------------------------------------------------------------------------------------------- failure paths
class Exploding:
    """A transport whose wsl.exe is missing or stuck: every call raises (WslRunner raises WslError then)."""

    def __getattr__(self, name):
        def boom(*a, **k):
            from aethercfd.backend.wsl.runner import WslError

            raise WslError("wsl.exe did not return within 150s")

        return boom


def test_control_of_an_unreachable_cluster_says_so_and_leaves_the_run_running(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    _run(mgr, fake, rec)
    fake.down = True
    ok, msg = mgr.control(rec.id, "stop_write")
    assert not ok and "unreachable" in msg and "nothing was sent" in msg and "VPN" in msg
    assert rec.state == RunState.RUNNING.value and rec.stop_reason is None  # not stuck in "stopping"
    n = len(fake.scripts)
    ok, msg = mgr.control(rec.id, "kill")  # backing off: answered at once, no second wait on ssh
    assert not ok and "Try again in a minute" in msg and len(fake.scripts) == n
    assert rec.state == RunState.RUNNING.value


def test_a_transport_that_raises_never_escapes_control_launch_or_download(env):
    mgr, fake, case = env
    rec = _submit(mgr, case)
    _run(mgr, fake, rec)
    mgr._cluster.transport_factory = lambda p: Exploding()
    ok, msg = mgr.control(rec.id, "terminate")
    assert not ok and "did not return" in msg and rec.state == RunState.RUNNING.value
    mgr._cluster._down_until.clear()
    assert mgr._sync_back(rec, skip_newest=False) is False
    sync = [p for p in mgr.bus.topics("run.guard") if p["guard"] == "sync"]
    assert sync and "did not return" in sync[-1]["message"]
    rec2 = _submit(mgr, case)
    assert rec2.state == RunState.FAILED.value and "cannot reach cluster hpc" in rec2.stop_reason


def test_a_stuck_wsl_never_escapes_local_control(env):
    mgr, _fake, case = env

    class StuckRunner:
        distro = "LMAE-OpenFOAM"

        def run_script(self, *a, **k):
            from aethercfd.backend.wsl.runner import WslError

            raise WslError("wsl.exe did not return within 90s")

    mgr.runner = StuckRunner()
    rec = RunRecord(id="r1", case_root=str(case), exec_case_wsl="/x", run_dir=str(mgr.project.runs_dir / "r1"),
                    run_dir_wsl="/x/r", linux_run_dir="/x/r", fork=ESI.to_json(), placement="in-place",
                    placement_reason="", steps=[], nprocs=1, signals_enabled=True, created="2026-11-15T00:00:00+00:00",
                    state=RunState.RUNNING.value, pgid=1234)
    Path(rec.run_dir).mkdir(parents=True)
    rec.save()
    ok, msg = mgr.control("r1", "terminate")
    assert not ok and "could not reach WSL" in msg
    assert mgr.get("r1").state == RunState.RUNNING.value


def test_no_cluster_configured_is_inert(tmp_path, monkeypatch):
    """No [[clusters]] profile: reattach and local planning never create the cluster target or call ssh."""
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "cavity"
    shutil.copytree(FIX / "esi_cavity", case)
    mgr = ManualManager(Project.create(case), NoLocalRunner(), settings=Settings(), bus=Bus())  # type: ignore[arg-type]
    assert mgr.reattach() == []
    assert "_cluster" not in mgr.__dict__
    ex = _executor(mgr)
    plan = ex.plan("run.start", {"case": str(case), "target": "cluster:hpc"}, USER)
    assert any("no cluster profile named 'hpc'" in c.message and "Run > Clusters" in c.message
               for c in plan.blocked)
    assert "_cluster" not in mgr.__dict__
