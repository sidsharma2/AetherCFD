# ADR 0006 — WSL launch, fork isolation and signal-based run control

Status: accepted (2026-10-07) — evidence: `spikes/spike_c_wsl.py`, `spikes/out/spike_c_*.json`
Environment: WSL 2.7.10.0, kernel 6.18.33.2-2, an OpenFOAM distro (Ubuntu 22.04.5, root,
16 threads, 19.4 GiB), ESI v2306 + Foundation v14.

## Results
| check | result |
|---|---|
| Fork prelude (reset PATH → refuse if `WM_PROJECT_DIR` set → source bashrc → verify dir), ESI | pass (`v2306`) |
| Same, Foundation v14 | pass (`14`) |
| Script sourcing ESI then v14 | refused, exit 97 |
| argv with spaces through `wsl.exe … bash --noprofile --norc <script> <args>` from Python list args | pass |
| ESI: `<app> -opt-switch writeNowSignal=10 -opt-switch stopAtWriteNowSignal=12`, SIGUSR1 → writes, keeps running | pass |
| ESI: SIGUSR2 → writes and exits ("End") | pass |
| v14: no `-opt-switch` option exists. `WM_PROJECT_SITE=<run dir>/site` with `etc/configDict` containing `OptimisationSwitches { writeNowSignal 10; stopAtWriteNowSignal 12; }` → SIGUSR1/SIGUSR2 work | pass |
| v14: `OptimisationSwitches` in the *case* controlDict | **does not work** — SIGUSR1 then kills the solver (default action) |
| `decomposePar` + `mpirun --allow-run-as-root -np 4 --bind-to none icoFoam -parallel` | pass |
| Decomposed case read with `vtkPOpenFOAMReader(CaseType=0)` from Windows | pass (400/400 cells, 6 times) |
| v14 `checkMesh -writeFields` | **not available** (ESI has it; v14 only `-writeSets`) |
| `setsid … &` then the launching script exits immediately | **child dies** (race with wsl.exe teardown) |
| `setsid -f …` + wait for a startup handshake (or ≥0.5 s) before exiting | child survives |
| Detached job with **no** wsl.exe client attached for 90 s | keeps running; WSL keeps the distro up |
| Note | PowerShell → `wsl.exe -c '<script>'` mangles quotes; always pass a script path + argv list |

## Decisions
1. Every launch script ends with a **startup handshake**: start the runner with `setsid -f`, then wait
   (≤5 s) until the runner has written its pid/pgid/start-time file before the launching script exits.
2. **No DistroAnchor by default.** WSL 2.7 keeps the distro alive while processes exist. Keep the
   anchor as an opt-in fallback setting in case a future WSL build changes this.
3. Signal run control is **enabled at launch or not at all**, recorded per run as `signals_enabled`:
   - ESI: `-opt-switch writeNowSignal=10 -opt-switch stopAtWriteNowSignal=12` on the solver argv.
   - Foundation: run-private `WM_PROJECT_SITE` dir (under `.aethercfd/local/runs/<id>/site`, which
     lives in the case sidecar or the stage) containing only the `OptimisationSwitches` override.
   - Never send SIGUSR1/2 to a run whose `signals_enabled` is false (it would kill it). Fallback
     control for such runs is a temporary `stopAt writeNow` edit (needs `runTimeModifiable`).
4. Mesh-quality field colouring uses `checkMesh -writeFields` on ESI; on Foundation the panel
   falls back to the checkMesh report and `-writeSets` (and later a `cellQuality`-style function).
5. MPI as root uses `--allow-run-as-root` (or `OMPI_ALLOW_RUN_AS_ROOT=1` +
   `OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1`) and `--bind-to none`.
