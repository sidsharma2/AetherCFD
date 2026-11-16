"""Opt-in (ADR 0007): run the generated Slurm batch script in a real bash with a fake ``srun`` and a fake solver.

Unit tests read the batch script as text; this runs it, to prove the parts only bash can: the step contract
(steps.jsonl, logs, exit_code), write-now / stop-and-write forwarded from the batch shell to the solver only (what
``scancel --batch --signal=USR1/USR2`` does), the pre-walltime USR2 recorded as a walltime stop, and SIGTERM -> 143.

Nothing is installed: it uses Git for Windows' bash (or bash on PATH elsewhere), never WSL.
Run with:  AETHERCFD_FAKE_SLURM=1 pytest tests/cluster
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from aethercfd.core.clusters import ClusterProfile
from aethercfd.runs.cluster.scheduler import JobRequest, SlurmScheduler
from aethercfd.runs.cluster.scripts import batch_script
from aethercfd.runs.cluster.target import cluster_fork
from aethercfd.runs.spec import RunSpec, RunStep, StepKind

pytestmark = pytest.mark.cluster_fake


def _bash() -> str | None:
    for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if c and Path(c).exists() and "system32" not in c.lower():  # System32\bash.exe is WSL: never used here
            return c
    return None


BASH = _bash()
if not os.environ.get("AETHERCFD_FAKE_SLURM"):
    pytest.skip("opt-in: set AETHERCFD_FAKE_SLURM=1", allow_module_level=True)
if BASH is None:
    pytest.skip("no bash (Git for Windows) found", allow_module_level=True)

FAKE_SOLVER = """#!/bin/bash
# fake OpenFOAM solver: USR1 = write now and go on, USR2 = write now and stop (like -opt-switch writeNowSignal=10 ...)
W=; S=
trap 'echo "sigWriteNow : will write at end of the time step"; W=1' USR1
trap 'echo "sigStopAtWriteNow : will write and stop"; S=1' USR2
echo "args: $*"
for i in $(seq 1 300); do
  echo "Time = $i"
  echo "smoothSolver:  Solving for Ux, Initial residual = 0.$((1000 - i)), Final residual = 1e-05, No Iterations 2"
  if [ -n "$W" ] || [ -n "$S" ]; then mkdir -p "$i"; echo written > "$i/U"; W=; fi
  if [ -n "$S" ]; then echo End; exit 0; fi
  sleep 0.05
done
echo End
"""
FAKE_UTIL = '#!/bin/bash\necho "$(basename "$0") $*"; echo End\n'
FAKE_SRUN = '#!/bin/bash\n# srun -n N app args...\nshift 2\nexec "$@"\n'


def msys(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    return f"/{s[0].lower()}{s[2:]}" if s[1:2] == ":" else s


@pytest.fixture
def job(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, text in (("icoFoam", FAKE_SOLVER), ("decomposePar", FAKE_UTIL), ("reconstructPar", FAKE_UTIL),
                       ("srun", FAKE_SRUN)):
        (bin_ / name).write_text(text, newline="\n")
    case, run = tmp_path / "case", tmp_path / "runs" / "r1"
    case.mkdir()
    run.mkdir(parents=True)
    prof = ClusterProfile(name="fake", host="localhost", openfoam_version="v2306",
                          env_setup=f"export PATH={msys(bin_)}:$PATH WM_PROJECT_DIR=/fake WM_PROJECT_VERSION=v2306")

    def make(steps, *, lead: int = 300) -> Path:
        spec = RunSpec(case_wsl=msys(case), fork=cluster_fork(prof), steps=tuple(steps), run_dir_wsl=msys(run),
                       linux_run_dir=msys(run), run_id="r1", nprocs=2)
        req = JobRequest(name="t", output=f"{msys(run)}/job.out", workdir=msys(case), ntasks=2,
                         stop_signal_lead_s=lead)
        script = run / "job.sbatch"
        script.write_text(batch_script(spec, prof, SlurmScheduler(), req), newline="\n")
        return script

    return make, case, run


STEPS = [RunStep("decomposePar", ("-force",)), RunStep("icoFoam", kind=StepKind.SOLVER, parallel=True),
         RunStep("reconstructPar", ("-latestTime",))]


def _start(script: Path, run: Path) -> tuple[subprocess.Popen, str]:
    """Start the batch script as Slurm would; returns the batch shell's bash pid (what scancel --batch signals)."""
    pidf = run / "batch.pid"
    p = subprocess.Popen([BASH, "-c", f'echo $$ > "{msys(pidf)}"; exec bash "{msys(script)}"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait(lambda: pidf.exists() and pidf.read_text().strip())
    return p, pidf.read_text().strip()


def _signal(pid: str, sig: str) -> None:
    subprocess.run([BASH, "-c", f"kill -{sig} {pid}"], check=True)


def _wait(cond, timeout: float = 30.0) -> None:
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("timed out")
        time.sleep(0.05)


def _text(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _events(run: Path) -> list[dict]:
    f = run / "steps.jsonl"
    return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []


def test_steps_logs_and_exit_code(job):
    make, case, run = job
    fast = [RunStep("decomposePar", ("-force",)), RunStep("reconstructPar", ("-latestTime",))]
    subprocess.run([BASH, str(make(fast))], check=True, timeout=60)
    assert (run / "exit_code").read_text().strip() == "0"
    assert [(e["step"], e["state"]) for e in _events(run)] == [(0, "running"), (0, "done"), (1, "running"),
                                                               (1, "done")]
    assert "decomposePar -force" in (case / "log.decomposePar").read_text()
    assert json.loads((run / "runner.started").read_text())["host"]


def test_write_now_reaches_the_solver_and_it_keeps_running(job):
    make, case, run = job
    p, pid = _start(make(STEPS), run)
    try:
        _wait(lambda: "solver" in _text(run / "solver.pid") and "Time = 3" in _text(case / "log.icoFoam"))
        _signal(pid, "USR1")  # = scancel --batch --signal=USR1
        _wait(lambda: "sigWriteNow" in (case / "log.icoFoam").read_text())
        time.sleep(0.5)
        assert p.poll() is None and not (run / "exit_code").exists()  # still running
        assert any(d.name.isdigit() for d in case.iterdir())  # a time was written
        assert "-opt-switch writeNowSignal=10" in (case / "log.icoFoam").read_text()  # srun passed the switches
        (run / "stop_requested").touch()  # what Stop & write does before the signal
        _signal(pid, "USR2")
        p.wait(60)
    finally:
        if p.poll() is None:
            p.kill()
    assert (run / "exit_code").read_text().strip() == "0"
    assert "sigStopAtWriteNow" in (case / "log.icoFoam").read_text()
    assert [e["state"] for e in _events(run) if e["step"] == 2] == ["skipped"]  # reconstructPar skipped
    assert not (run / "guard_stop.json").exists()  # the user asked: not a walltime stop


def test_pre_walltime_usr2_is_recorded_as_a_walltime_stop(job):
    make, case, run = job
    p, pid = _start(make(STEPS), run)
    try:
        _wait(lambda: "Time = 2" in _text(case / "log.icoFoam"))
        _signal(pid, "USR2")  # = Slurm's #SBATCH --signal=B:USR2@300
        p.wait(60)
    finally:
        if p.poll() is None:
            p.kill()
    assert (run / "exit_code").read_text().strip() == "0"
    assert json.loads((run / "guard_stop.json").read_text())["reason"].startswith("walltime")


def test_usr1_during_a_utility_is_not_forwarded(job):
    make, case, run = job
    slow_util = '#!/bin/bash\ntrap "echo GOT-USR1" USR1\nfor i in $(seq 1 40); do sleep 0.05; done\necho End\n'
    (Path(case).parent / "bin" / "decomposePar").write_text(slow_util, newline="\n")
    p, pid = _start(make(STEPS[:1]), run)
    try:
        _wait(lambda: (run / "solver.pid").exists())
        _signal(pid, "USR1")
        p.wait(60)
    finally:
        if p.poll() is None:
            p.kill()
    assert "GOT-USR1" not in (case / "log.decomposePar").read_text()
    assert (run / "exit_code").read_text().strip() == "0"


def test_sigterm_records_143(job):
    make, case, run = job
    p, pid = _start(make(STEPS), run)
    solver = ""
    try:
        _wait(lambda: "Time = 2" in _text(case / "log.icoFoam"))
        solver = _text(run / "solver.pid").split()[0]
        _signal(pid, "TERM")  # = scancel / the walltime kill
        p.wait(60)
    finally:
        if p.poll() is None:
            p.kill()
        if solver:  # Slurm signals every process of the job; here only the batch shell got it
            subprocess.run([BASH, "-c", f"kill -KILL {solver} 2>/dev/null"], check=False)
    assert (run / "exit_code").read_text().strip() == "143"
