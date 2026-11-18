# Running cases on an HPC cluster

AetherCFD can submit a case to a Slurm cluster instead of running it in WSL. The design is in
[ADR 0007](adr/0007-hpc-cluster-runs.md). This page tells you how to add a cluster when you have an account.

## What you need

- An account on the cluster, and its login host name (e.g. `narval.alliancecan.ca`).
- `ssh` and `rsync` in the OpenFOAM WSL distro. AetherCFD checks for them but never installs them. If they are
  missing, install them yourself in the distro (`apt install openssh-client rsync`).
- OpenFOAM on the cluster, and the line that loads it (from the cluster documentation, e.g.
  `module load StdEnv/2023 openfoam/v2306`).
- Your allocation account and the partition to use, if the cluster requires them.

## 1. Set up SSH access from WSL

AetherCFD never asks for or stores a password. Pick the method your cluster supports:

- **SSH key** (most clusters). In the distro, run `ssh-keygen -t ed25519`, then register
  `~/.ssh/id_ed25519.pub` with the cluster (its web portal, or `ssh-copy-id`). In the profile, set
  *Key file* to `~/.ssh/id_ed25519`, or leave it empty if `~/.ssh/config` or an ssh-agent already selects the key.
- **Password and MFA / one-time code**. Keep *Reuse one SSH connection* on. Click *Connect (interactive)…*: a
  console opens, and you type your password and code there. AetherCFD then reuses that connection for
  *Keep connection* (default 8 h). When it expires, runs show "cluster unreachable" and keep running on the
  cluster. Connect again to continue monitoring.
- **Jump host / gateway**. Set *Jump host* (`me@gateway.example.org`).

The first connection to a new host asks you to accept its host key. Do this once with *Connect (interactive)…*
(compare the fingerprint with the cluster's documentation). Non-interactive calls never accept unknown keys.

## 2. Add the profile

Run panel → *Clusters…* → *Add*:

| Field | Example | Notes |
|---|---|---|
| Name | `narval` | the run target becomes `cluster:narval` |
| Login host / User | `narval.alliancecan.ca` / `sid` | |
| Remote work dir | `$SCRATCH/aethercfd` | no spaces; `~` and `$VARS` expand on the cluster |
| Load OpenFOAM | `module load StdEnv/2023 openfoam/v2306` | one line |
| OpenFOAM version | `v2306` | must equal `WM_PROJECT_VERSION` after that line |
| Partition / Account | `cpu` / `def-supervisor` | defaults; each run can override them |
| Walltime | `03:00:00` | default time limit |
| MPI launcher | `srun` | use `mpirun` if the cluster documents it; extra flags (e.g. `--mpi=pmix`) in *Launcher arguments* |
| Write before walltime | `300 s` | the solver writes and stops this long before the time limit |

Click *Test connection*. It checks, without a prompt, that the login node answers, that `sbatch`, `squeue`, `sacct`,
`scancel` and `rsync` exist, that the work dir is writable, and that the OpenFOAM line loads the version you set.
Then *Save*. The profile is stored in `settings.toml` as `[[clusters]]` and holds no credentials.

## 3. Run

In the Run panel, set *Where* to the cluster, set the MPI processes and (optionally) the walltime, and click
*Start…*. The plan shows the job (tasks, walltime, partition, account) and the commands before anything happens.

- The state goes *staging* (upload) → *launching* (sbatch) → *pending* (in the queue) → *running*.
- Residuals and the solver log update every 15 s (the profile's poll interval).
- *Write now* and *Stop & write* work while the job runs. *Pause* is not available on clusters. *Kill* cancels the job,
  and also works while it waits in the queue.
- Results (time folders, `postProcessing/`, logs) download into the case folder at the end, and every 10 min while
  the job runs. `system/` and `constant/` are never overwritten.

From a script or the console:

```python
s = Session(r"D:\cases\pitzDaily")
run_id = s.run(preset="solve", nprocs=64, target="cluster:narval", walltime="06:00:00", account="def-supervisor")
s.runs.wait(run_id)
print(s.check_cluster("narval"))
```

```
aethercfd clusters --check narval
aethercfd run D:\cases\pitzDaily --target cluster:narval --np 64 --walltime 06:00:00 --wait
```

## First real job: what to check

Use a small case (e.g. `pitzDaily`, 4 tasks, 10 min walltime) and confirm the items in the
[ADR's first-access checklist](adr/0007-hpc-cluster-runs.md#first-access-checklist-to-confirm-on-the-real-cluster).
In particular: that *Write now* writes a time and the solver keeps running; that `srun` starts the MPI build (or
switch to `mpirun`); and that a walltime of a few minutes ends the run as *guard stopped* with a written time.

## Limits

- Slurm only. PBS and LSF are one adapter class each in `runs/cluster/scheduler.py`.
- Tutorial `./Allrun` scripts are not submitted to clusters. Mesh locally, or use *Mesh + check + solve*.
- AetherCFD does not watch your cluster quota or scratch purge policy.
