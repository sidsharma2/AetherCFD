# ADR 0008 — Threads, Qt and VTK: who may touch what

Status: accepted (2026-10-26). Roadmap item Q3 (`docs/vision/roadmap.md`).

## Context

Two deadlocks reached the app in October 2026, and both were found by hand:

- `2d8564e`. `FoamResults.read` returned a shallow copy of the reader's output. The GUI then drew the
  reader's own cached mesh objects and arrays while the viewport worker read again with the same reader. The
  render and the reader's `Update()` blocked each other. The CHT sweep hung at "showcase of the solids".
- `1959cc7`. Saving the layout on close read pyvista's `plotter.camera`. For a camera that was never set,
  that property resets the camera and renders, into a GL window that was being destroyed.

The facts behind the rules:

- Qt widgets belong to the GUI thread.
- A QObject is deleted only in its own thread, and a blocking queued connection to the same thread deadlocks.
- VTK rendering is not thread-safe, and on Windows a GL context cannot be shared across threads.
- pyvistaqt documents no method as safe from a worker thread, except that `QtInteractor.render()` only emits a
  Qt signal.

## Rules

1. Only the GUI thread touches widgets, `QtInteractor`, render windows and `render()`.
2. A worker runs `Update()` only on pipelines it owns. The viewport worker owns its readers. Nothing on
   the GUI thread calls a reader.
3. Data crosses threads as a deep copy, or as frozen output. Frozen output is the output of a filter that
   was made for one call and never runs again. A shallow copy is allowed only of frozen output or of data the
   copying thread owns, and each such site says why it is safe. Objects in the worker's geometry cache are
   never modified after they are cached, because the GUI may already hold them.
4. On close: stop timers and callbacks, disconnect render triggers, and never render once close has
   started. Read camera state from the plain VTK camera (`renderer.GetActiveCamera()`), never from pyvista's
   `plotter.camera`.
5. Code under test never calls `exec()` on a dialog. Tests patch dialogs out. A closed dialog is deleted
   (`deleteLater`): a closed `SetupWizard` left alive crashed the next app-wide restyle.

## Checks

- Render thread. `studio/panels/viewport/render_thread.py` puts a `StartEvent` observer on each
  viewport's render window. A render on any thread other than the one that built the viewport is logged with
  its stack and recorded in `VIOLATIONS`. `--sweep` fails its last step when the list is not empty. A Python
  exception inside a VTK observer would be swallowed, so the check records instead of raising.
- Imports. An import-linter contract keeps `aethercfd.results` free of PySide6, pyvistaqt, pyqtgraph and
  the studio. Results code runs on worker threads.
- Tests. `tests/unit/test_robustness.py::test_reads_never_share_dataset_objects_with_the_reader` fails on
  the old shallow copy. `tests/studio/test_threading_soak.py` switches the regions and the showcase of a
  five-region case 200 times while loads are in flight, and closes and reopens the viewport every tenth
  cycle. It must end with no hang, no slot exception and no off-thread render. This soak does not reproduce
  the `2d8564e` hang on the old code; the unit test and the CHT sweep are the regression checks for that.

## The ShallowCopy audit (2026-10-26)

Every `ShallowCopy` in `results/` and `studio/panels/viewport/` (23 sites) was checked against rule 3:

- 12 copy the frozen output of a filter made inside the same function: 6 in `results/pipeline.py` (lines
  111, 128, 190, 231, 814, 1050), 3 in `results/sections.py` (174, 233, 317) and 3 in `worker.py`
  (`_slice` twice, `_outline`). The filters are append, geometry, normals, transform, tube, cell-to-point,
  extract-selection and plane-cutter filters.
- 6 copy data the copying thread owns, to give a dataset its own array lists before an array is added:
  `pipeline.py` 706 and 755, `sections.py` 328, 330 and 429, and `worker.py` `_smooth`.
- 2 in `worker.py` (`_pathlines` and the internal meshes in `load`) copy the reader's output, which is a deep
  copy since `2d8564e`.
- 3 in `panel.py` copy a mapper, a scalar bar and its text property for an off-screen render, on the GUI
  thread.

None breaks a rule today.

## Consequences

The deep copy costs one extra copy of what a load reads. Measured on a private decomposed 3-D jet case
(1,028,736 cells, field T with cell-to-point values), it took 0.03 s and 172 MB. The time does not matter.
The memory does on a big case. It scales with the mesh, so scaling the measurement linearly gives about
1.7 GB for a 10M-cell case with one field, and more with several. If that becomes a limit, the fix is a reader per load (no shared cache) rather than
going back to shallow copies.

New code that hands VTK objects from a worker to the GUI follows rule 3, and new GUI-side code reads cameras
as rule 4 says.
