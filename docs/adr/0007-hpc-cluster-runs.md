# ADR 0007 — Runs on a remote HPC cluster (Slurm first)

Status: accepted (2026-10-14). Designed and tested without cluster access: every remote step is exercised
against a fake SSH/scheduler layer (`tests/unit/test_cluster_*.py`) and an opt-in local fake Slurm
(`tests/cluster/`). The checklist at the end lists what to confirm on the first real cluster.

## Context
Runs today are detached processes in the WSL distro (ADR 0006), placed in place or on an ext4 stage
(ADR 0003). The run manager tails log files, evaluates guards and decides "finished / failed / lost"
from an `exit_code` file plus a liveness probe (two definite DEAD replies before "lost").
Large cases need a cluster: a login node reached over SSH, a batch scheduler, a shared scratch file
system, often a jump host and sometimes MFA. AetherCFD must not store passwords, must keep working
when the cluster is unreachable for a while, and must not change how local runs behave.

## Decisions

### 1. Execution target on the run record
`RunRecord.target` is `"local"` (default, every existing record) or `"cluster:<profile>"`.
`run.start`, `Session.run(target=…)`, the copilot's `run_openfoam` and the Run panel take the same
string. `RunManager` keeps the local WSL path exactly as it is and sends cluster records to a
`ClusterTarget` (`runs/cluster/target.py`) that implements the `ExecutionTarget` protocol
(`runs/targets.py`): `prepare`, `launch`, `control`, `is_alive`, `pull`, `sync_back`,
`exec_case_win`. Moving the local code behind the same protocol is a mechanical follow-up. It was
not done here so that local runs stay byte-for-byte the same while live runs depend on them.

The monitor loop is shared. A cluster run differs only in where its files come from: the
`ClusterTarget` mirrors the remote run files (`steps.jsonl`, `exit_code`, `guard_stop.json`,
`job.out`) into the local run folder and the remote `log.*` into `<run dir>/case/`. From there,
the existing code (log tail, residual parser, repetition/stall guards, Residuals and Solver Log
panels) works unchanged. The log-growth guard samples only after a pull, because a mirrored log
grows in bursts.

### 2. Cluster profiles (settings, no secrets)
`Settings.clusters: list[ClusterProfile]` in `settings.toml` (`core/clusters.py`):
name, host, user, port, `identity_file` (a path inside the WSL distro; empty = ssh-agent or
`~/.ssh/config`), `jump_host` (ProxyJump), `work_dir` (remote root, e.g. `$SCRATCH/aethercfd`;
`~` and `$VARS` are expanded on the cluster), `env_setup` (one bash line, e.g.
`module load openfoam/v2306`), `openfoam_version` (checked against `WM_PROJECT_VERSION` after
`env_setup`), scheduler (`slurm`), partition, account, qos, default walltime/nodes/tasks per node/
memory, `mpi_launcher` (`srun` | `mpirun`) plus launcher arguments, extra `#SBATCH` lines,
ControlMaster settings, poll and download intervals. A profile has **no password field**, and
nothing AetherCFD writes holds a credential.
The run record keeps a snapshot of the profile, so later edits or a deleted profile do not
break monitoring of a run that is already submitted.

### 3. Transport: OpenSSH and rsync inside the WSL distro
All SSH, rsync and remote commands run from the OpenFOAM WSL distro through `WslRunner` (still the
only code that executes `wsl.exe`). Reasons: rsync and ControlMaster multiplexing are available
there; Windows OpenSSH has neither (no ControlMaster on Win32-OpenSSH). The case is also
already reachable as `/mnt/<drive>/…`. Requirement: `ssh` and `rsync` in the distro (Test
Connection checks; AetherCFD never installs them). Every call uses `BatchMode=yes`, so a missing
key or an expired MFA session fails at once (exit 255) instead of waiting for a prompt.
Remote scripts are base64-encoded into the local script and decoded into
`bash -l -c 'script=$(cat); exec </dev/null; eval "$script"'` on the login node. This avoids
quoting layers and gives `module` from the login environment. Replies are parsed by marker lines,
so login banners cannot corrupt them. The `Transport` protocol (`runs/cluster/transport.py`) is
the seam the fake implements in tests.

### 4. Credentials and MFA
- Keys: an `identity_file` in the distro (`~/.ssh/id_ed25519`, mode 600) or an ssh-agent in the
  distro. AetherCFD never reads key material and never stores or forwards passwords.
- MFA/2FA clusters: `ControlMaster=auto`, `ControlPath=~/.ssh/aethercfd-%C`,
  `ControlPersist=8h` (per profile). **Connect (interactive)…** opens a console that runs
  `ssh -M -fN` for the profile. The user types the password and one-time code in that console,
  and the master connection then stays in the background for `ControlPersist`. All AetherCFD calls
  reuse the socket. When it expires, calls fail fast and runs show "cluster unreachable". They
  are never marked lost (see 7).

### 5. Scheduler adapter (Slurm first)
`Scheduler` (`runs/cluster/scheduler.py`) turns a `JobRequest` into a batch-script header and
gives the submit/status/cancel/signal commands with their parsers. `SlurmScheduler` is
implemented. A PBS or LSF adapter is one more class in `SCHEDULERS` (qsub/qstat/qdel/
`qsig -s USR1`; bsub/bjobs/bkill/`bkill -s USR1`). Nothing outside the adapter names a scheduler
command.
- Script: `#SBATCH` lines for job name, `--output=<run>/job.out`, `--chdir=<case>`,
  `--ntasks=<nprocs>`, optional `--nodes`, `--ntasks-per-node`, `--time`, `--partition`,
  `--account`, `--qos`, `--mem`, extra lines, and `--signal=B:USR2@<lead>` (see 6).
- Submit: `sbatch --parsable job.sbatch` → job id (`12345` or `12345;cluster`;
  "Submitted batch job N" is accepted too).
- Status: `squeue -h -j <id> -o '%i|%T|%r'`, then `sacct -n -P -X -j <id> -o
  JobID,State,ExitCode`, each with its exit status, between marker lines. Without the end marker
  the reply is "unknown". A squeue error other than "Invalid job id" (e.g. controller not
  reachable) is "unknown", never "gone".
- Cancel: `scancel <id>`; kill: `scancel --full --signal=KILL <id>` then `scancel <id>`.
- Pause/resume are refused (`scontrol suspend` needs operator rights).

### 6. Write-now and stop-and-write map to the same solver switches
The batch script is the cluster version of `runner.sh`: the same step function, `steps.jsonl`,
`exit_code`, TERM trap and `ulimit -f` cap. The solver gets the same
`-opt-switch writeNowSignal=10 -opt-switch stopAtWriteNowSignal=12` (ESI) or the run-private
`WM_PROJECT_SITE` (Foundation), as ADR 0006 specifies. The batch shell traps USR1/USR2 and
forwards them to the running solver only (srun or mpirun, which forward to every rank). Utilities
never receive them.
- Write now: `scancel --batch --signal=USR1 <id>`.
- Stop and write: touch `<run>/stop_requested` remotely, then `scancel --batch --signal=USR2 <id>`.
- Before the walltime: `#SBATCH --signal=B:USR2@<write_before_walltime_s>` (default 300 s). The
  solver writes its latest time and stops cleanly instead of being killed. The trap records
  `guard_stop.json` ("walltime") when the user did not ask for the stop, so the run ends as
  `guard_stopped` with that reason.
`--batch` is used (not plain `--signal`) because `mpirun` is not a Slurm step, and only the batch
shell knows which process is the solver. Runs started with signals disabled never get USR1/USR2,
as on WSL.

### 7. Liveness, states and reattach (same rules as local)
- New state `pending` (active): submitted and waiting in the queue. `launching → pending →
  running` follows the scheduler.
- `is_alive` for a cluster run = scheduler status. Queued or running gives `True`. A terminal state
  (COMPLETED, FAILED, CANCELLED, TIMEOUT, NODE_FAIL, OUT_OF_MEMORY, PREEMPTED…), or "the
  scheduler does not know the job" from a clean squeue reply, gives `False` (a definite DEAD).
  SSH failure, MFA expiry, a garbled reply or a scheduler error gives `None` (unknown). The two-DEAD
  rule, the fast recheck and "unknown neither counts nor resets" are the same code as local.
- Before a DEAD counts, the remote run files are pulled again, so an `exit_code` that just
  appeared finishes the run normally.
- Two DEADs without an exit code: if the scheduler gave a terminal state, the run ends `failed` or
  `killed` with `stop_reason "scheduler: TIMEOUT"` etc. It ends `lost` only when neither squeue nor
  sacct knows the job.
- A profile whose SSH call failed is not called again for 60 s, so an unreachable cluster cannot
  stall reattach or the monitor.
- Reattach after an app restart uses the stored job id (it needs no boot id). `revive` also works
  for cluster runs: a run marked lost whose job the scheduler still reports as queued/running is
  monitored again.

### 8. Case transfer
Remote layout under the resolved `work_dir`: `<project>/<case-slug>/case` (persistent, like the
ext4 stage, so a re-run sends only changed files) and `<project>/<case-slug>/runs/<run id>`.
- Upload (rsync): `system/`, `constant/`, the start time directory and top-level files, excluding
  `log.*`. Before the upload it refuses, as `Stage.stage_in_sh` does, if the remote case holds time
  directories that were never downloaded.
- decomposePar runs on the cluster as an ordinary step inside the job (the default parallel steps
  are decomposePar → solver under srun/mpirun → reconstructPar -latestTime).
- Live mirror every `poll_s` (default 15 s): `rsync --append` of the run files and `log.*`
  (logs only grow, so each pull sends only new bytes).
- Results every `sync_every_s` (default 600 s, 0 = only at the end) and at the end: time
  directories (the newest one is skipped while the job runs), `postProcessing/`, `log.*`, and
  optionally `processor*/` (`sync_processor_dirs`) into the user's case folder. `system/` and
  `constant/` are never overwritten, the same rule as ADR 0003. `RunManager.download_results`
  fetches them again on request (e.g. after a lost run).

## Consequences
- Local WSL runs are unchanged. A cluster run needs `ssh` and `rsync` in the distro, plus `rsync`
  on the cluster.
- Tutorial `./Allrun` steps are refused for cluster targets for now (they need the whole case
  tree). Mesh locally or use the mesh + check + solve preset.
- Quota and scratch purge policies are not watched by AetherCFD. The scheduler enforces
  time and memory limits, and `ulimit -f` caps any single file.

## First-access checklist (to confirm on the real cluster)
1. `Test connection`: ssh, rsync, sbatch/squeue/sacct/scancel found; `env_setup` loads the expected
   `WM_PROJECT_VERSION`; `work_dir` resolves to scratch.
2. Is sacct enabled? (Without it, a job that leaves squeue with no `exit_code` ends as lost.)
3. Does `srun` start the cluster's OpenFOAM MPI build (`--mpi=pmix`/`pmi2` in launcher args), or
   is `mpirun` the documented way?
4. Write now on a short `pitzDaily` job: the solver prints the write-now message on every rank and
   keeps running. Stop & write: one final time, then exit code 0.
5. Short walltime (e.g. 10 min with `write_before_walltime_s = 120`): the run ends
   `guard_stopped` with a written time.
6. On an MFA cluster: `Connect (interactive)…`, then Test connection in BatchMode works for
   `ControlPersist`.
