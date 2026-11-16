"""Cluster runs without a cluster (ADR 0007): Slurm reply parsing, batch-script generation, profile validation.

The squeue / sacct / sbatch texts below are the formats those tools print (including their error messages).
"""

from __future__ import annotations

import pytest

from aethercfd.backend.wsl.forks import Family
from aethercfd.core.clusters import ClusterProfile
from aethercfd.core.settings import Settings
from aethercfd.runs.cluster.scheduler import (
    STATUS_BEGIN,
    STATUS_END,
    JobPhase,
    JobRequest,
    SchedulerError,
    SlurmScheduler,
    get_scheduler,
)
from aethercfd.runs.cluster.scripts import batch_script
from aethercfd.runs.cluster.target import cluster_fork
from aethercfd.runs.spec import RunSpec, RunStep, StepKind

S = SlurmScheduler()
PROFILE = ClusterProfile(name="hpc", host="login.example.org", user="sid", openfoam_version="v2306",
                         env_setup="module load openfoam/v2306", partition="compute", account="def-sid")


def reply(*lines: str, end: bool = True) -> str:
    return "\n".join(["Welcome to the cluster", STATUS_BEGIN, *lines, *([STATUS_END] if end else [])]) + "\n"


# ---------------------------------------------------------------------------------------------- sbatch
@pytest.mark.parametrize(("out", "job"), [
    ("12345\n", "12345"),
    ("12345;cedar\n", "12345"),
    ("Lmod has detected the following error\nSubmitted batch job 777\n", "777"),
    ("motd line\n\n4242\n", "4242"),
])
def test_submit_reply(out, job):
    assert S.parse_submit(out) == job


@pytest.mark.parametrize("out", ["", "sbatch: error: Batch job submission failed: Invalid account\n", "abc\n"])
def test_submit_failure_is_an_error(out):
    with pytest.raises(SchedulerError):
        S.parse_submit(out)


# ---------------------------------------------------------------------------------------------- status
@pytest.mark.parametrize(("out", "phase", "state"), [
    (reply("SQ_RC=0", "SQ:41000|PENDING|Priority", "SA_RC=0", "SA:41000|PENDING|0:0"), JobPhase.QUEUED, "PENDING"),
    (reply("SQ_RC=0", "SQ:41000|RUNNING|None", "SA_RC=0", "SA:41000|RUNNING|0:0"), JobPhase.RUNNING, "RUNNING"),
    (reply("SQ_RC=0", "SQ:41000|COMPLETING|None"), JobPhase.RUNNING, "COMPLETING"),
    (reply("SQ_RC=0", "SA_RC=0", "SA:41000|COMPLETED|0:0"), JobPhase.DONE, "COMPLETED"),
    (reply("SQ_RC=1", "SQ:slurm_load_jobs error: Invalid job id specified", "SA_RC=0", "SA:41000|TIMEOUT|0:15"),
     JobPhase.DONE, "TIMEOUT"),
    (reply("SQ_RC=0", "SA_RC=0", "SA:41000|CANCELLED by 1234|0:15"), JobPhase.DONE, "CANCELLED"),
    (reply("SQ_RC=0", "SA_RC=0", "SA:41000|OUT_OF_MEMORY|0:125"), JobPhase.DONE, "OUT_OF_MEMORY"),
    (reply("SQ_RC=0", "SA_RC=1", "SA:Slurm accounting storage is disabled"), JobPhase.GONE, ""),
    (reply("SQ_RC=1", "SQ:slurm_load_jobs error: Invalid job id specified", "SA_RC=0"), JobPhase.GONE, ""),
])
def test_status_replies(out, phase, state):
    st = S.parse_status("41000", out)
    assert st is not None and st.phase is phase and st.state == state
    assert st.alive == (phase in (JobPhase.QUEUED, JobPhase.RUNNING))


def test_status_exit_code_from_accounting():
    st = S.parse_status("41000", reply("SQ_RC=0", "SA_RC=0", "SA:41000|FAILED|2:0"))
    assert st.exit_code == 2 and st.phase is JobPhase.DONE


@pytest.mark.parametrize("out", [
    "",  # nothing came back
    reply("SQ_RC=0", "SQ:41000|RUNNING|None", end=False),  # truncated
    reply("SQ_RC=1", "SQ:slurm_load_jobs error: Unable to contact slurm controller (connect failure)",
          "SA_RC=1", "SA:sacct: error: Problem talking to the database: Connection refused"),  # controller down
    reply("SA_RC=0"),  # no squeue verdict at all
    reply("SQ_RC=0", "SQ:41001|RUNNING|None"),  # a line, but not about this job
])
def test_status_unknown_is_never_gone(out):
    assert S.parse_status("41000", out) is None


def test_status_script_refuses_odd_job_ids():
    with pytest.raises(SchedulerError):
        S.status_script("1; rm -rf ~")
    s = S.status_script("41000_3")
    assert "squeue -h -j 41000_3" in s and "sacct -n -P -X -j 41000_3" in s


def test_signals_go_to_the_batch_shell_only():
    assert S.signal_batch_cmd("41000", "USR1") == "scancel --batch --signal=USR1 41000"
    with pytest.raises(SchedulerError):
        S.signal_batch_cmd("41000", "KILL")
    assert "--full --signal=KILL" in S.kill_cmd("41000") and S.cancel_cmd("41000") == "scancel 41000"


def test_unknown_scheduler_is_refused():
    assert isinstance(get_scheduler("Slurm"), SlurmScheduler)
    with pytest.raises(SchedulerError, match="not supported"):
        get_scheduler("pbs")


# ---------------------------------------------------------------------------------------------- batch script
def _spec(steps, nprocs=4, fork=None, signals=True) -> RunSpec:
    return RunSpec(case_wsl="/scratch/sid/aethercfd/p/cavity/case", fork=fork or cluster_fork(PROFILE),
                   steps=tuple(steps), run_dir_wsl="/scratch/sid/aethercfd/p/cavity/runs/r1",
                   linux_run_dir="/scratch/sid/aethercfd/p/cavity/runs/r1", run_id="r1", nprocs=nprocs,
                   enable_signals=signals)


def _job(**kw) -> JobRequest:
    base = dict(name="aether-cavity", output="/scratch/r1/job.out", workdir="/scratch/case", ntasks=4,
                walltime="02:00:00", partition="compute", account="def-sid", stop_signal_lead_s=300)
    base.update(kw)
    return JobRequest(**base)


STEPS = [RunStep("decomposePar", ("-force",)), RunStep("simpleFoam", kind=StepKind.SOLVER, parallel=True),
         RunStep("reconstructPar", ("-latestTime",))]


def test_slurm_header():
    h = S.header(_job(nodes=2, mem="64G", extra=("--constraint=cascade", " ")))
    assert "#SBATCH --ntasks=4" in h and "#SBATCH --nodes=2" in h and "#SBATCH --time=02:00:00" in h
    assert "#SBATCH --partition=compute" in h and "#SBATCH --account=def-sid" in h and "#SBATCH --mem=64G" in h
    assert "#SBATCH --signal=B:USR2@300" in h and "#SBATCH --constraint=cascade" in h
    assert not any("--qos" in x or "--ntasks-per-node" in x for x in h)
    assert not any("--signal" in x for x in S.header(_job(stop_signal_lead_s=0)))


def test_batch_script_runs_like_the_local_runner():
    s = batch_script(_spec(STEPS), PROFILE, S, _job())
    assert "\r" not in s and s.startswith("#!/bin/bash\n#SBATCH")
    assert "module load openfoam/v2306" in s and '!= v2306 ]' in s and "exit 98" in s
    assert "srun -n 4 simpleFoam -opt-switch writeNowSignal=10 -opt-switch stopAtWriteNowSignal=12 -parallel" in s
    assert s.count("-opt-switch writeNowSignal") == 1  # solver only
    assert "--allow-run-as-root" not in s and "nice -n" not in s
    assert "trap 'fwd USR1' USR1" in s and "trap on_usr2 USR2" in s and "exit 143' TERM" in s
    assert 'CUR_KIND=$kind' in s and '[ "$CUR_KIND" = solver ]' in s  # signals reach the solver only
    assert "walltime" in s and "guard_stop.json" in s
    assert 'echo 0 > "$RUN_DIR/exit_code"' in s and "steps.jsonl" in s


def test_batch_script_mpirun_and_foundation_site_dir():
    prof = PROFILE.model_copy(update={"mpi_launcher": "mpirun", "launcher_args": "--map-by core",
                                      "openfoam_version": "11"})
    fork = cluster_fork(prof)
    assert fork.family is Family.FOUNDATION
    s = batch_script(_spec(STEPS, fork=fork), prof, S, _job())
    assert "mpirun -np 4 --map-by core simpleFoam -parallel" in s and "-opt-switch" not in s
    assert 'export WM_PROJECT_SITE="$RUN_DIR/site"' in s and "writeNowSignal 10; stopAtWriteNowSignal 12;" in s


def test_signals_disabled_means_no_switches_and_no_walltime_signal():
    s = batch_script(_spec(STEPS, signals=False), PROFILE, S, _job(stop_signal_lead_s=0))
    assert "-opt-switch" not in s and "--signal=B:USR2" not in s


# ---------------------------------------------------------------------------------------------- profiles
@pytest.mark.parametrize(("field", "value"), [
    ("name", "my cluster"), ("host", "login;rm -rf ~"), ("host", "a b"), ("work_dir", "/scratch/my runs"),
    ("work_dir", "/x'y"), ("walltime", "two hours"), ("mpi_launcher", "aprun"), ("env_setup", "a\nb"),
])
def test_profile_rejects(field, value):
    with pytest.raises(ValueError):
        ClusterProfile(**{"name": "hpc", "host": "login", field: value})


@pytest.mark.parametrize("wt", ["30", "30:00", "02:00:00", "1-12", "1-12:00:00", "120:00:00"])
def test_walltime_formats(wt):
    assert ClusterProfile(name="x", host="h", walltime=wt).walltime == wt


def test_profile_has_no_secret_fields_and_round_trips_through_settings(tmp_path, monkeypatch):
    assert not any(k in ClusterProfile.model_fields for k in ("password", "passphrase", "token", "otp"))
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path))
    s = Settings()
    s.clusters.append(PROFILE.model_copy(update={"jump_host": "sid@gw.example.org", "work_dir": "$SCRATCH/aether",
                                                 "sbatch_extra": ["--constraint=cascade"]}))
    s.save()
    back = Settings.load()
    assert back.clusters == s.clusters and back.clusters[0].target == "cluster:hpc"
    assert back.clusters[0].destination == "sid@login.example.org"
    assert Settings.model_validate({}).clusters == []  # old settings files load unchanged
