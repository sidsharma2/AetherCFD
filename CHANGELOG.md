# Changelog

## Unreleased

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
