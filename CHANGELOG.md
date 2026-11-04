# Changelog

## Unreleased

### Added
- **Several versions of one fork side by side (ESI v2306 + v2606).** Tested on LMAE-OpenFOAM with v2606 (2606.0~rc2
  apt build) installed next to v2306 and Foundation 14: the fork probe finds all three, the new-case wizard lists
  both ESI versions, and the 7 ESI toy cases, the live template/run/codegen tests and the copilot note all pass on
  v2606. `scripts/toy_campaign.py --fork esi-v2606` picks the version to run; the live tests take
  `AETHERCFD_LIVE_ESI=v2606` / `AETHERCFD_LIVE_FOUNDATION=<ver>` (default: the oldest installed, as before).
- Copilot log note for OpenFOAM v2606's `Empty 'probeLocations' list` stop (a probes/patchProbes function with
  `probeLocations ()`, which v2306 accepted; seen on backwardFacingStep2D's sampleCp).

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
- **Runs: a healthy run is no longer marked "lost" by an unanswered liveness probe.** Under heavy host load the
  `alive` probe can return nothing or garbled output; that used to end a running job as `lost` (seen on
  2026-10-11: a REV B.1 probe run at iteration ~326 while runner.sh, mpirun and all 8 rhoSimpleFoam ranks were
  computing). `RunManager.is_alive` now answers *unknown* (`None`) unless the reply is exactly `DEAD` or a complete
  `ALIVE <start> <boot>` line (and the probe exited cleanly), and a run is only declared lost after two consecutive
  definite DEAD replies (an unknown reply neither counts nor resets; the second probe follows ~5 s after the first).
  `reattach()` applies the same rule, and now also re-opens a run already marked lost on the current WSL boot when
  its own process group is verifiably alive (pgid + start time + boot id); `RunManager.revive(rec)` does this for one
  record, and `reattach(revive_lost=False)` opts out.
