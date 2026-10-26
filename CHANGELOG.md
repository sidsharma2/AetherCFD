# Changelog

## Unreleased

### Added
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
