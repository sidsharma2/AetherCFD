"""A fake Slurm cluster for unit tests (ADR 0007): implements the cluster ``Transport`` on the local file system.

Remote paths map under ``root``. The scheduler replies are the real tools' text formats (``sbatch --parsable``,
``squeue -o '%i|%T|%r'``, ``sacct -P``, their error messages), so the real parsers are exercised. Tests drive the
"job" with :meth:`FakeCluster.start`, :meth:`step`, :meth:`log` and :meth:`finish`, which write what the generated
batch script would write (steps.jsonl, log.*, exit_code).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from aethercfd.backend.wsl.runner import ScriptResult
from aethercfd.runs.cluster.scheduler import STATUS_BEGIN, STATUS_END
from aethercfd.runs.cluster.transport import CHECK_BEGIN, CHECK_END, RUN_FILES

_TIME = re.compile(r"^[-+]?[0-9.]+([eE][-+]?[0-9]+)?$")
ACTIVE = {"PENDING", "RUNNING", "COMPLETING"}


def ok(stdout: str = "") -> ScriptResult:
    return ScriptResult(0, stdout, "")


class FakeCluster:
    def __init__(self, root: Path, *, work: str = "/scratch/sid/aethercfd", openfoam: str = "v2306"):
        self.root = root
        self.work = work
        self.openfoam = openfoam
        self.jobs: dict[str, dict] = {}
        self.next_id = 41000
        self.scripts: list[str] = []
        self.signals: list[tuple[str, str]] = []  # (job id, signal) sent to the batch shell
        self.cancelled: list[str] = []
        self.down = False  # ssh fails (exit 255) while True
        self.controller_down = False  # squeue/sacct cannot reach slurmctld
        self.sacct_enabled = True
        self.purged: set[str] = set()  # finished jobs squeue no longer lists
        self.pulls = 0
        self.downloads: list[bool] = []  # skip_newest of each download

    # ------------------------------------------------------------------ helpers
    def local(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def _unreachable(self) -> ScriptResult:
        return ScriptResult(255, "", "ssh: connect to host login.example.org port 22: Connection timed out\n")

    @property
    def last_job(self) -> str:
        return max(self.jobs, key=int)

    # ------------------------------------------------------------------ Transport
    def run(self, script: str, *, timeout_s: float = 120) -> ScriptResult:
        if self.down:
            return self._unreachable()
        self.scripts.append(script)
        if "AETHER_WORK=" in script:
            self.local(self.work).mkdir(parents=True, exist_ok=True)
            return ok(f"Welcome to the fake cluster\nAETHER_WORK={self.work}\n")
        if script.startswith("ls -1A "):
            d = self.local(script.split()[2].strip("'"))
            names = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
            return ok("".join(f"{n}\n" for n in names) + "AETHER_LS_END\n")
        if "sbatch --parsable" in script:
            run_dir = re.search(r"cd (\S+) &&", script).group(1).strip("'")
            job = str(self.next_id)
            self.next_id += 1
            self.jobs[job] = {"state": "PENDING", "reason": "Priority", "exit": None, "run_dir": run_dir,
                              "script": (self.local(run_dir) / "job.sbatch").read_text(encoding="utf-8")}
            return ok(f"{job}\n")
        if STATUS_BEGIN in script:
            return ok(self._status(re.search(r"squeue -h -j (\S+)", script).group(1)))
        if CHECK_BEGIN in script:
            return ok(f"{CHECK_BEGIN}\nHOST=login1\nHAVE=sbatch\nHAVE=squeue\nHAVE=sacct\nHAVE=scancel\nHAVE=rsync\n"
                      f"WORK={self.work}\nWMV={self.openfoam}\nWMD=/apps/openfoam/{self.openfoam}\n{CHECK_END}\n")
        for m in re.finditer(r"touch (\S+)/stop_requested", script):
            self.local(m.group(1).strip("'")).joinpath("stop_requested").touch()
        if m := re.search(r"scancel --batch --signal=(\w+) (\S+)", script):
            self.signals.append((m.group(2).strip("'"), m.group(1)))
            return ok()
        if m := re.search(r"scancel (?:--full --signal=KILL )?(\d+)", script):
            job = m.group(1)
            self.cancelled.append(job)
            if job in self.jobs and self.jobs[job]["state"] in ACTIVE:
                self.jobs[job]["state"] = "CANCELLED"
            return ok()
        return ok()

    def put_text(self, remote_path: str, text: str, *, timeout_s: float = 120) -> ScriptResult:
        if self.down:
            return self._unreachable()
        p = self.local(remote_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        return ok()

    def upload_case(self, local_case: Path, remote_case: str, *, start_time: str | None,
                    timeout_s: float = 3600) -> ScriptResult:
        if self.down:
            return self._unreachable()
        dst = self.local(remote_case)
        dst.mkdir(parents=True, exist_ok=True)
        for p in local_case.iterdir():
            if p.is_dir() and p.name in ("system", "constant", start_time):
                shutil.copytree(p, dst / p.name, dirs_exist_ok=True)
            elif p.is_file() and not p.name.startswith("log."):
                shutil.copy2(p, dst / p.name)
        (dst / ".aethercfd_synced").unlink(missing_ok=True)
        return ok()

    def pull_live(self, remote_run: str, remote_case: str, local_run: Path, *, timeout_s: float = 120) -> ScriptResult:
        if self.down:
            return self._unreachable()
        self.pulls += 1
        (local_run / "case").mkdir(parents=True, exist_ok=True)
        for name in RUN_FILES:
            src = self.local(remote_run) / name
            if src.exists():
                shutil.copy2(src, local_run / name)
        rc = self.local(remote_case)
        for src in rc.glob("log.*") if rc.is_dir() else []:
            shutil.copy2(src, local_run / "case" / src.name)
        return ok()

    def download_results(self, remote_case: str, local_case: Path, *, skip_newest: bool, processor_dirs: bool,
                         timeout_s: float = 3600) -> ScriptResult:
        if self.down:
            return self._unreachable()
        self.downloads.append(skip_newest)
        rc = self.local(remote_case)
        times = sorted((p for p in rc.iterdir() if p.is_dir() and _TIME.match(p.name)), key=lambda p: float(p.name))
        if skip_newest and times:
            times = times[:-1]
        for p in times:
            shutil.copytree(p, local_case / p.name, dirs_exist_ok=True)
        if (rc / "postProcessing").is_dir():
            shutil.copytree(rc / "postProcessing", local_case / "postProcessing", dirs_exist_ok=True)
        for p in rc.glob("log.*"):
            shutil.copy2(p, local_case / p.name)
        if not skip_newest:
            (rc / ".aethercfd_synced").touch()
        return ok()

    # ------------------------------------------------------------------ scheduler replies (real formats)
    def _status(self, job: str) -> str:
        j = self.jobs.get(job)
        out = [STATUS_BEGIN]
        if self.controller_down:
            out += ["SQ_RC=1", "SQ:slurm_load_jobs error: Unable to contact slurm controller (connect failure)",
                    "SA_RC=1", "SA:sacct: error: Problem talking to the database: Connection refused"]
        else:
            if j is not None and j["state"] in ACTIVE and job not in self.purged:
                out += ["SQ_RC=0", f"SQ:{job}|{j['state']}|{j['reason'] if j['state'] == 'PENDING' else 'None'}"]
            elif j is not None and job not in self.purged:
                out += ["SQ_RC=0"]  # finished but not purged yet: squeue -j prints nothing
            else:
                out += ["SQ_RC=1", "SQ:slurm_load_jobs error: Invalid job id specified"]
            if not self.sacct_enabled:
                out += ["SA_RC=1", "SA:Slurm accounting storage is disabled"]
            elif j is not None:
                code = j["exit"] if j["exit"] is not None else 0
                out += ["SA_RC=0", f"SA:{job}|{j['state']}|{code}:0"]
            else:
                out += ["SA_RC=0"]
        out.append(STATUS_END)
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------ the job (what the batch script writes)
    def _run_dir(self, job: str) -> Path:
        return self.local(self.jobs[job]["run_dir"])

    def _case_dir(self, job: str) -> Path:
        return self._run_dir(job).parent.parent / "case"

    def start(self, job: str) -> None:
        self.jobs[job]["state"] = "RUNNING"
        (self._run_dir(job) / "runner.started").write_text(json.dumps({"job": job, "host": "node001"}) + "\n")

    def step(self, job: str, idx: int, state: str, log: str | None = None, rc: int | None = None) -> None:
        ev = {"step": idx, "state": state, "t": int(time.time())}
        if log:
            ev["log"] = log
        if rc is not None:
            ev["rc"] = rc
        with open(self._run_dir(job) / "steps.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(ev) + "\n")

    def log(self, job: str, name: str, text: str) -> None:
        with open(self._case_dir(job) / name, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def write_time(self, job: str, t: str) -> None:
        d = self._case_dir(job) / t
        d.mkdir(parents=True, exist_ok=True)
        (d / "U").write_text("FoamFile {}\n")

    def finish(self, job: str, code: int, *, state: str | None = None, exit_file: bool = True) -> None:
        if exit_file:
            (self._run_dir(job) / "exit_code").write_text(f"{code}\n")
        self.jobs[job]["state"] = state or ("COMPLETED" if code == 0 else "FAILED")
        self.jobs[job]["exit"] = code


def solver_log(times: list[float]) -> str:
    """simpleFoam-style log lines for a few iterations (residuals the parser picks up)."""
    out = []
    for i, t in enumerate(times):
        r = 0.5 / (i + 1)
        out += [f"Time = {t:g}", "",
                f"smoothSolver:  Solving for Ux, Initial residual = {r:.6g}, Final residual = {r / 100:.6g}, "
                "No Iterations 3",
                f"GAMG:  Solving for p, Initial residual = {r * 2:.6g}, Final residual = {r / 50:.6g}, No Iterations 7",
                "time step continuity errors : sum local = 1e-05, global = 1e-07, cumulative = 1e-07",
                f"ExecutionTime = {i + 1} s  ClockTime = {i + 1} s", ""]
    return "\n".join(out) + "\n"
