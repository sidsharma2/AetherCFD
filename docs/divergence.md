# Divergence watchdog

A run that diverges usually ends with NaN in its newest time folder, and with `purgeWrite` the last good folder
can be gone already. AetherCFD watches every solver step while the run goes. It stops the run when the run is
clearly diverging, and then offers one plan to restart from the last good time with gentler numerics.

## What it watches

The watchdog reads the solver log as it grows. It stops a run only on signs that a healthy run never shows:

| Sign | Stops when |
|---|---|
| NaN or inf | any residual, Courant number, time step, continuity error, `min/max` value or bounding value is NaN or inf |
| Bounded field blowing up | the out-of-bounds size in a `bounding k, min: … max: …` line is 1e8 times its size the first time that field needed bounding |
| Linear solver divergence | a final residual is 1 or more and 10 times the initial residual |
| Residual blow-up | an initial residual is 1 or more and 1e4 times its lowest value in the last 500 steps, for 3 steps in a row |
| Courant number | the maximum Courant number is 1000 or more |
| Temperature | `min(T)` is 0 K or lower |

It only warns on earlier signs: a residual 100 times its recent lowest value and above 0.1 for
5 steps, a Courant number of 20 or more, a negative k, epsilon, omega or nuTilda, or one field that needs
bounding in 50 steps in a row. Each warning shows once in the Run panel's message list, and again only if the
sign clears and comes back.

The watchdog also keeps the last good time: the newest step with no sign of divergence. If residuals start to
grow before a warning, the last good time stays at the step before the growth started.

## What happens after a stop

1. The watchdog sends Stop & write. The solver writes its current time and exits. The run shows
   `guard_stopped`.
2. The Run panel shows **Restart from last good time…**. Select it to make the plan. Tick **first-order
   convection** first if you also want upwind convection.
3. The plan restarts from the newest written time at or before the last good time whose fields hold no NaN or
   inf. A run that diverged in its first 50 steps restarts from the time it started from, because what it
   wrote is finite but already polluted.
4. The plan makes the numerics gentler:
   - Steady cases: every under-relaxation factor times 0.7. With plain SIMPLE, p is never above 0.3 and the
     equations never above 0.7. SIMPLEC keeps its unrelaxed pressure.
   - Transient cases: `maxCo` times 0.5 with an adaptive time step, otherwise `deltaT` times 0.5.
   - With **first-order convection**: `Gauss upwind` (or `bounded Gauss upwind`) for every transported field.
     Values that use a `$macro` are not changed.
   - `purgeWrite` 1 or 2 becomes 3, so the next divergence also has a good time to go back to.
5. The plan opens in the approval dialog with the reason, the last good time and, when the cfd_agents
   service is available, Murphy's diagnosis of the log. Approve it, then press Start.

Nothing is deleted. Time folders after the restart time stay where they are, and the restart writes over the
ones it reaches. The plan is one journal entry, so you can revert it in the Command Journal.

If the solver crashes with a floating-point error before the watchdog can stop it, the run ends as `failed`,
and the Run panel still offers the restart from the last good time the watchdog saw.

## Settings

In `settings.toml`, under `[guards]`:

```toml
divergence = "stop"   # "stop" (default), "warn" (report signs, never stop) or "off"
```

## How the limits were chosen

`scripts/divergence_corpus.py` runs the watchdog over a folder of real solver logs, read-only, and reports
what it would have done:

```bash
python scripts/divergence_corpus.py path/to/your/cases
```

On the 111 solver logs on the development PC (simpleFoam, pimpleFoam, rhoSimpleFoam, reactingFoam,
chtMultiRegionFoam and chtMultiRegionSimpleFoam, on ESI v2306 and OpenFOAM 14), it stopped the two runs that
diverged and none of the others. Healthy runs reach a bounding growth of 2e4 at most (the backward-facing step,
k), so the stop limit of 1e8 leaves four decades. No healthy log has a final residual of 1 or more. 11 healthy
logs get a warning, mostly long bounding streaks of k or nuTilda.

Both diverged runs blew up fast: the first sign came at step 13 or 14 and the solver crashed at step 27 or 28.
On real runs of steady pitzDaily with plain SIMPLE and relaxation 0.9 (scratch copies, through the run
manager), the watchdog stopped the run before any NaN reached a written time:

| Fork | Stopped at | Sign | Written times | Restart from | Restart |
|---|---|---|---|---|---|
| OpenFOAM 14 (`foamRun`) | step 14 | k out of bounds by 2.7e9 | 0, 5, 10, 15, 16, none with NaN | 0 | converged in 1117 iterations |
| ESI v2306 (`simpleFoam`) | step 5 | epsilon out of bounds by 8.3e21 | 0, 5, 6, none with NaN | 0 | converged in 1107 iterations |

Both restarts used the plan as it stands: p relaxation 0.3, equations 0.63. A first try on OpenFOAM 14 kept p
at 0.63 and restarted from time 10; it was stable for 2000 iterations but stalled with residuals near 1e-3.
That result set the plain-SIMPLE caps and the rule for runs that diverge early.

## Limits

- The watchdog reads the log in the app. A case that blows up in less than about a second (a few steps on a
  small mesh) can crash before the stop arrives. The run still gets the restart offer.
- When the app is closed the watchdog is not running. When the app opens again it reattaches and reads the log
  from there.
- ESI's `runTimeControl` function object is not used as a backstop inside the solver. Its residual condition
  fires on `residual > value`, which is false for NaN, and diverging residuals in these logs sit near 1, the
  same as the first iteration of a healthy steady run.
- The NaN scan reads ASCII and gzip field files. Binary files, and files over 64 MB, are not scanned: the plan
  says so, and the time is still used.
