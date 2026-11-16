# Changelog

## Unreleased

### Hardening (2026-11-15)

Three hardening passes on top of the features below, merged in `release/hardening`.

#### PINN support

- `scripts/setup_ml_env.py` creates the PyTorch environment for PINNs in one step. It uses uv or venv, takes
  the CUDA build when `nvidia-smi` reports a driver for CUDA 12.6 or newer, checks the result with a forward
  and backward pass, and saves `ml_python`.
- `aethercfd-cli ml-setup` runs the same setup from the CLI and from the packaged app, which has no
  `scripts/` folder. The no-environment hints in the PINN tab and the setup wizard name the command that fits
  how the app was started.
- A PINN result now reports its error against the CFD field on cells the network did not train on, and on
  held-out points. It is labelled as a reconstruction and saves a record with the seed, the torch, CUDA and
  numpy versions, the device, the hyper-parameters and an input hash.
- The PINN tab has Stop, Check again and a seed. It plots the held-out error while training and says how to
  set up PyTorch when there is no environment. Changing case or closing the panel stops the worker.
- The worker stops on a cancel request or when the app is gone, reports a NaN loss as "diverged", restarts
  on the CPU after a CUDA out-of-memory error, and writes a plain result when torch is missing.
- Jobs built from Python take `hard_bc` (`"auto"` or face names): Dirichlet data on bounding-box faces are
  held exactly.
- A result gets a note when the training values cover less than 10 % of the field's spread, are all the
  same, or the field hardly varies. Relative case paths work.
- The copilot's `pinn_train` refuses with the setup hint when there is no environment, returns the held-out
  errors, seed and record, and stops the run on timeout.
- `docs/pinn.md`: user guide with a worked example, the meaning of the reported errors and the known limits.

#### Install and build

- `INSTALL.md` is one path for a new PC, top to bottom, with a check after each step.
- `scripts/bootstrap.ps1` checks a new PC and only looks by default. `-Sync`, `-Test` and `-Doctor` run
  `uv sync --locked`, the CI unit tests and `aethercfd doctor`.
- `aethercfd doctor` prints one ok / warn / FAIL line per part, each with a fix. FAIL is for required parts
  and sets exit code 1. New flags `--quick` and `--json`. It shows the copilot sign-in state without the
  account e-mail, and gives a clearer fix when CUDA is switched off.
- CI runs `aethercfd doctor --no-backend --quick` on the Windows runner.
- `packaging/build.py` checks commit memory, the terminal files, the bundled files and the packages before
  PyInstaller starts. A rerun reuses a finished PyInstaller step. The build checks its required components,
  writes a size report and stamps the version into the exes. `--cpu-only` (experimental) leaves out CuPy
  and the NVIDIA libraries.
- `packaging/install.py` takes `--target` and `--no-shortcuts`.
- The version lives in one place, `src/aethercfd/__init__.py`.
- The terminal's xterm.js files are committed with their licence. Before, a `dist/` ignore rule kept them out
  of git, so clones and builds made from them had a broken terminal.

#### Robustness fixes

- A broken `settings.toml` no longer stops the app. A syntax error gives the defaults, a bad value is
  dropped, the file is copied aside first, and the studio says so.
- Opening a missing folder, a file or a folder of cases gives a plain message.
- A damaged `.aethercfd/project.toml` says how to recover, and one bad external project no longer hides the
  others.
- A truncated fork cache is probed again, and the cache is written atomically.
- An unreachable cluster or a stuck `wsl.exe` gives a plain message. Run controls work on the thread pool,
  so the window does not freeze.
- Start in the Run panel works on the thread pool. A staged run of a big case no longer freezes the window
  while the case is copied.
- Residual export and run reattach after opening a case no longer block the GUI thread.
- A read-only or locked case file fails the command cleanly. Changes already written by the same plan are put
  back.
- An unexpected error while a command applies is a failed result and is logged.
- Exports to a read-only or locked file (flamelet CSV, combustion report, line-probe CSV, saved views) give a
  plain message and leave the old file as it was.
- Mesh quality on a case without a mesh says to run blockMesh or snappyHexMesh.
- `load_case` and `aethercfd-cli report` accept a relative path.
- CLI errors are one line on stderr with exit code 2, not a traceback. `AETHERCFD_DEBUG=1` shows the
  traceback.
- A closed panel no longer raises "Internal C++ object already deleted" on the next case open or file change.
- Closing the app stops the case-folder watcher.
- The viewport reports a damaged field file as damaged, not as "not written".
- Run report plots survive a stale matplotlib font cache.
- Checks: a new warning when SIMPLEC (`consistent yes`) also relaxes the pressure, with a one-click fix.

#### Tests

- 609 unit tests and 134 studio tests on this branch (counted with `pytest --collect-only`), plus 28 live WSL
  tests and one corpus test that run on the owner's PC.
- PINN plumbing tests drive a fake worker and need no torch: held-out error and record, no environment, torch
  missing, crash, out of memory, divergence, Stop, a stuck worker, a timeout and a killed worker. Real runs
  cover hard boundary conditions, NaN, CUDA out of memory, cancel and an app that disappears.
- `aethercfd doctor` has unit tests that fake each failure.
- The install copy step is tested on a temporary folder.
- A unit test fails when a vendored terminal file is missing, changed or ignored by git again.
- Tests for the SIMPLEC pressure-relaxation check.
- The QoI report test no longer reads the user's settings.
- The sweep drives the viewport at the latest time folder that holds files.

### Added

- **Several versions of one fork side by side (ESI v2306 + v2606).** Tested on LMAE-OpenFOAM with v2606 (2606.0~rc2
  apt build) installed next to v2306 and Foundation 14: the fork probe finds all three, the new-case wizard lists
  both ESI versions, and the 7 ESI toy cases, the live template/run/codegen tests and the copilot note all pass on
  v2606. `scripts/toy_campaign.py --fork esi-v2606` picks the version to run; the live tests take
  `AETHERCFD_LIVE_ESI=v2606` / `AETHERCFD_LIVE_FOUNDATION=<ver>` (default: the oldest installed, as before).
- Copilot log note for OpenFOAM v2606's `Empty 'probeLocations' list` stop (a probes/patchProbes function with
  `probeLocations ()`, which v2306 accepted; seen on backwardFacingStep2D's sampleCp).
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
- **Schlieren: log scale, percentile reference, Smooth/Cells.** The schlieren stage takes `scale: log`
  (|grad rho| / rho = |grad ln rho|) and `percentile` (scale by that percentile of the gradient instead of its
  maximum), both in the Visualization panel and the copilot's `add_stage`. Scaled by the maximum, |grad rho| in a
  rocket plume is set by the throat (density ~20 kg/m3 there, ~0.01 in the expansion fans), so every wave in the
  plume came out pale; log scale with the 99th percentile shows them at every density level. The stage now follows
  the viewer's Smooth / Cells choice like slices do: with Smooth, S is averaged to the mesh points and drawn without
  cell edges (labelled interpolated).
- **Quantities of interest for every case.** `aethercfd.case.qoi` derives a case's quantities of interest from its
  patches and physics: the force on the walls (when flow passes through or a wall moves), the flow rate and mean
  p (and T) on every open patch, the pressure drop between one inlet and one outlet, the heat flow through each
  wall when an energy equation is solved, and the scalar columns of any other function object. The guided Run
  stage asks "Add integral monitors?" and writes the matching function objects as one approvable plan, in each
  fork's own syntax (ESI `regionType patch; name <p>`, Foundation `patch <p>`; `system/functions` on the
  Foundation when it exists). The reader handles both forks' output layouts and concatenates restarts.
- **Validation gates on the quantities.** Integral convergence (steady: band over the last 10 % of the iterations;
  transient: drift of the mean between the last two quarters; tolerance asked in the Validation stage, 1 % by
  default) and a conservation audit (Σ flow over every open patch: pass ≤ 0.5 %, error > 5 %; heat balance of a
  closed steady box).
- **Run report.** The Post stage (and `aethercfd-cli report <case>`, and the copilot's `run_report`) writes
  report.md, report.json, journal-style PDF/PNG plots of every quantity with the judged window shaded, the
  residuals, and ParaView (pvbatch) pictures of p, |U| and T on the real mesh, into the project's
  `local/reports` folder. The verdict is the Validation gates. The copilot's `qoi_status` returns the same
  numbers.

### Fixed

- **Installing a newer OpenFOAM no longer moves existing cases onto it.** A case's fork is now the exact version in
  its file headers, else the closest installed version not newer than that (a v2412 case runs on v2306, not v2606),
  else, with no version in the headers, the *oldest* installed one; before, both fell through to the newest. A
  version chosen per case (run settings, new-case wizard) still wins. Foundation versions now sort numerically
  (14 after 9).
- **Tutorial copies no longer contain symlinks Windows cannot open.** v2606's multiRegionHeater links
  `system/decomposeParDict -> decomposeParDict.4`; copied with `cp -r` to a Windows drive that link failed with
  WinError 1920. Tutorials are now copied with `cp -rL` (plain files).
- Note: the per-distro fork list is cached (`%LOCALAPPDATA%\AetherCFD\forks\<distro>.json`); after installing a
  version, use "Look again" in the setup wizard to refresh it.
- **Beauty renders coloured with inferno, viridis or plasma failed on ParaView 6.** ParaView 6.1 names those presets
  without the " (matplotlib)" suffix and raises for the old names, so pvbatch stopped before saving the image. The
  render script now falls back to the suffix-free name.
- **Guided monitoring no longer stops Foundation runs.** The Run stage's monitoring answer wrote
  `libs (fieldFunctionObjects)` for every fork. Foundation 14 reads `libs` only as a string list and stopped with
  "FOAM FATAL IO ERROR: wrong token type - expected string" when the objects went into controlDict (cases without a
  `system/functions` file, e.g. buoyantCavity). Foundation cases now get `libs ("libfieldFunctionObjects.so")` and
  `libs ("libutilityFunctionObjects.so")`; ESI keeps the word form. Foundation 14 also has no `fieldMinMax`
  ("Unknown function type"): its monitoring is now `volFieldValue` min and max over `cellZone all`, and the health
  gate reads that output for NaN/Inf and negative k, ε, ω, νt.
- **Dictionary parser: multi-line `#includeFunc` argument lists.** Foundation 14 tutorials write
  `#includeFunc streamlinesLine` with the `( ... )` arguments on the following lines (pitzDailySteady's
  `system/functions`). The parser stopped at the end of the line and reported a syntax error, so no edit to that
  file could be planned. It now reads to the matching `)`.
- **Runs: a healthy run is no longer marked "lost" by an unanswered liveness probe.** Under heavy host load the
  `alive` probe can return nothing or garbled output; that used to end a running job as `lost` (seen on
  2026-10-11: a REV B.1 probe run at iteration ~326 while runner.sh, mpirun and all 8 rhoSimpleFoam ranks were
  computing). `RunManager.is_alive` now answers *unknown* (`None`) unless the reply is exactly `DEAD` or a complete
  `ALIVE <start> <boot>` line (and the probe exited cleanly), and a run is only declared lost after two consecutive
  definite DEAD replies (an unknown reply neither counts nor resets; the second probe follows ~5 s after the first).
  `reattach()` applies the same rule, and now also re-opens a run already marked lost on the current WSL boot when
  its own process group is verifiably alive (pgid + start time + boot id); `RunManager.revive(rec)` does this for one
  record, and `reattach(revive_lost=False)` opts out.
