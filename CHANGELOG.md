# Changelog

## Unreleased

### Added
- **Schlieren: log scale, percentile reference, Smooth/Cells.** The schlieren stage takes `scale: log`
  (|grad rho| / rho = |grad ln rho|) and `percentile` (scale by that percentile of the gradient instead of its
  maximum), both in the Visualization panel and the copilot's `add_stage`. Scaled by the maximum, |grad rho| in a
  rocket plume is set by the throat (density ~20 kg/m3 there, ~0.01 in the expansion fans), so every wave in the
  plume came out pale; log scale with the 99th percentile shows them at every density level. The stage now follows
  the viewer's Smooth / Cells choice like slices do: with Smooth, S is averaged to the mesh points and drawn without
  cell edges (labelled interpolated).

### Fixed
- **Beauty renders coloured with inferno, viridis or plasma failed on ParaView 6.** ParaView 6.1 names those presets
  without the " (matplotlib)" suffix and raises for the old names, so pvbatch stopped before saving the image. The
  render script now falls back to the suffix-free name.
- **Runs: a healthy run is no longer marked "lost" by an unanswered liveness probe.** Under heavy host load the
  `alive` probe can return nothing or garbled output; that used to end a running job as `lost` (seen on
  2026-10-11: a REV B.1 probe run at iteration ~326 while runner.sh, mpirun and all 8 rhoSimpleFoam ranks were
  computing). `RunManager.is_alive` now answers *unknown* (`None`) unless the reply is exactly `DEAD` or a complete
  `ALIVE <start> <boot>` line (and the probe exited cleanly), and a run is only declared lost after two consecutive
  definite DEAD replies (an unknown reply neither counts nor resets; the second probe follows ~5 s after the first).
  `reattach()` applies the same rule, and now also re-opens a run already marked lost on the current WSL boot when
  its own process group is verifiably alive (pgid + start time + boot id); `RunManager.revive(rec)` does this for one
  record, and `reattach(revive_lost=False)` opts out.
