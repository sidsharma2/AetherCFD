# ADR 0005 — Viewport loading: reader + geometry filter on a worker, render on the GUI thread

Status: accepted (2026-10-07) — evidence: `spikes/spike_b_reader_worker.py`, `spikes/out/spike_b.json`

## Spike B results
`vtkPOpenFOAMReader` with `FileName=<case>/system/controlDict` (no `.foam` stub), run on a dedicated
QThread, results handed to the GUI as `NewInstance()+ShallowCopy()` over a queued signal:

| case | cells | cold load (worker) | warm step | max GUI stall *while loading* | case tree |
|---|---|---|---|---|---|
| backwardFacingStep2D | 20,540 | 0.21 s | 0.06–0.10 s | 10.9 ms | unchanged |
| SandiaFlameD (wedge, 38 times) | 5,170 | 0.17 s | 0.13–0.18 s | 11.5 ms | unchanged |
| a private 2-D test case | 90,112 | 0.40 s | 0.07 s | 10.8 ms | unchanged |
| a private multi-region CHT case (`fluid` region) | 1,430,400 | 4.42 s | — | 54.1 ms | unchanged |

(The probe ticks every 10 ms, so ~11 ms means "never blocked".) VTK releases the GIL during reader
work, so the GUI stays responsive while the worker loads.

The GUI-side cost dominates for big meshes: 0.59 s for the 1.43M-cell case, because a
`vtkDataSetMapper` extracts the unstructured grid's surface on the GUI thread, and the spike rebuilt
the actor on every step (0.04–0.22 s even for small cases; the first render also compiles shaders).

Other findings:
- Multi-region cases expose patches as `<region>/internalMesh`, `<region>/patch/…`; select by suffix.
- VTK replaces `:` `<` `>` in field names with private-use code points (U+F03A, U+F03C, U+F03E),
  e.g. `turbulencePropertiesdevReff`. The viewport must map them back for display.
- Bounding-box thinness is not a reliable 2D test (bFS2D has a finite z extent). Use the case's
  `empty` patches (from `constant/polyMesh/boundary`) to decide 2D mode.
- Reading `nCells` from the `note` line in `polyMesh/owner` works on all cases tried (cheap pre-scan).
- No decomposed case exists on disk yet; decomposed loading is covered in Spike C with a scratch
  `decomposePar`.

## Decision
- The worker owns one reader per (case, region) and also runs the geometry filter
  (`vtkGeometryFilter` / `vtkDataSetSurfaceFilter`, plus slice/clip/threshold filters), so the GUI
  receives `vtkPolyData` ready for a `vtkPolyDataMapper` (composite mapper for patches).
- The GUI reuses actors/mappers across time steps and only swaps mapper inputs and scalar ranges;
  it never `clear()`s and re-adds per step. Shaders are warmed once after the viewport is created.
- Requests carry a generation id; stale results are dropped (newest request wins).
- Size policy: <2M cells load the internal mesh; 2–10M boundary patches by default with the internal
  mesh on demand for slices/clips; >10M boundary only plus processor subsets and "Open in ParaView".
