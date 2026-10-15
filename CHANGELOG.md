# Changelog

## Unreleased

### Added
- **Runs on a remote HPC cluster (Slurm), next to local WSL runs** (ADR 0007, guide in `docs/cluster-runs.md`).
  Cluster profiles in settings (`[[clusters]]`: host, user, key, jump host, work dir, OpenFOAM `module load` line,
  partition/account/walltime defaults; no password field). Run panel: *Where* picks *This PC (WSL)* or a cluster,
  *Clusters…* adds, tests and signs in to profiles (*Connect (interactive)…* for MFA clusters). The case is uploaded
  with rsync, the job is submitted with `sbatch`, and logs and run files are mirrored back every 15 s. Residuals,
  the solver log and the guards then work as for local runs, and results download into the case folder.
  Write now / Stop & write use `scancel --batch --signal=USR1/USR2`, and the solver writes and stops 5 min before
  the walltime. A queued job shows as `pending`. An unreachable cluster never marks a run lost, and reattach after a
  restart uses the job id. Scripting: `Session.run(target="cluster:<name>", walltime=...)`,
  `Session.check_cluster(name)`; CLI: `aethercfd run --target cluster:<name>`, `aethercfd clusters --check <name>`;
  copilot: `run_openfoam(target=...)`, `cluster_profiles`.

### Fixed
- **Runs: a healthy run is no longer marked "lost" by an unanswered liveness probe.** Under heavy host load the
  `alive` probe can return nothing or garbled output; that used to end a running job as `lost` (seen on
  2026-10-11: a REV B.1 probe run at iteration ~326 while runner.sh, mpirun and all 8 rhoSimpleFoam ranks were
  computing). `RunManager.is_alive` now answers *unknown* (`None`) unless the reply is exactly `DEAD` or a complete
  `ALIVE <start> <boot>` line (and the probe exited cleanly), and a run is only declared lost after two consecutive
  definite DEAD replies (an unknown reply neither counts nor resets; the second probe follows ~5 s after the first).
  `reattach()` applies the same rule, and now also re-opens a run already marked lost on the current WSL boot when
  its own process group is verifiably alive (pgid + start time + boot id); `RunManager.revive(rec)` does this for one
  record, and `reattach(revive_lost=False)` opts out.
