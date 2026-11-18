# AetherCFD Studio: robustness and capability roadmap

*Written 2026-10-26 against `main` at `4690fef`, with PR #9 (fix/close-no-render) and PR #8 (Slurm cluster runs) open,
and the QoI branch (`feat/qoi-postprocessing`) in progress. The yardsticks are [original_spec.md](original_spec.md) and
[AUDIT.md](AUDIT.md). The meshing memo [automeshing_survey.md](../research/automeshing_survey.md) is an input, and this
plan does not repeat it.*

The goal is an OpenFOAM-based, AI-assisted CFD program that a first-year student and an expert can both trust. The
commercial codes earn that trust with setup tasks that show their state, meshing driven by the physics with a report
of what failed, solver steering past bad starts and divergence, and heavy testing of the product itself. This roadmap
checks each of these against public sources and maps it onto modules AetherCFD already has.

**How to read an item.** Each item gives *Why*, *Evidence* (tags point to the reference list at the end), *Touches*
(files or modules), *Effort*, *Risk* and *Accept when* (the test that closes it).

* Effort: **S** up to 1 week, **M** 2 to 4 weeks, **L** 1 to 3 months, **XL** research (more than 3 months, open
  outcome). Estimates assume one person working with Claude Code.
* **[APPROVAL]** marks anything that needs an install into WSL, a CUDA toolkit, a Windows registry change or a
  self-hosted runner. Nothing marked this way starts until the owner says yes.
* **[LOCAL]** marks a fact I checked on this machine on 2026-10-26 (the development WSL distro, ESI v2306 at
  `/usr/lib/openfoam/openfoam2306`, Foundation v14 at `/opt/openfoam14`, or the installed exe).

**Rules for every source.** Only public material is used: theory and user guides, release notes, vendor blogs and
white papers, papers and talks. No proprietary binary was decompiled, disassembled or instrumented. Vendor numbers
are marked *(vendor claim)* because their test conditions are not published.

## 1. Top five recommendations

1. **Put CI and crash forensics in place before adding features (Q1 to Q4).** The repo has no CI. Long studio test
   runs hang or crash, and two recent VTK bugs were deadlocks (`2d8564e`, `1959cc7`). A GitHub Actions setup with
   headless Qt and VTK tests, per-test thread dumps on a hang, a fake `wsl.exe` for fault injection, and written
   threading rules costs about two weeks. It protects everything that follows.
2. **Steer the solver and recover from divergence (Q5, Q6, M3).** Fluent and STAR-CCM+ start from a potential-flow
   or coarse-grid field, ramp CFL and order, and cut the CFL when a run starts to diverge [V20][V21][V22][V23][V24].
   OpenFOAM has the parts: `potentialFoam`, SIMPLEC, `runTimeControl` (ESI), `stopAtFile` (v14), `mapFields` and
   time-step control [O1][O2][O5][LOCAL]. AetherCFD already has signal-based write-now, guards and Murphy, so it can
   offer "stop at the last good time, restart more gently" as one approvable plan.
3. **Make meshing a task chain with stale-state tracking and per-patch layer reports (Q7, M1, M2).** Fluent's watertight
   and fault-tolerant workflows mark every downstream task out of date when you edit an upstream one, and check for
   leaks before meshing [V1][V3][V4]. ESI snappyHexMesh can now plan layers from a first-cell height
   (`thicknessModel`) and add them in several passes (`nOuterIter`) [O11][O12]. Report layer coverage per patch, and
   fall back to cfMesh where coverage stays low [O15].
4. **Make verification deeper (M4).** Extend the three-level GCI to snappy and imported meshes through `refineMesh`.
   Add the Eça and Hoekstra least-squares method for four or more noisy grids [M19]. Combine numerical, input and
   experimental uncertainty in the ASME V&V 20 way [M18]. Use the QoI branch's forces, flow rates and heat flows as
   the study quantities.
5. **Use ML where the evidence says it pays (M6, M7, R2), and say plainly where it does not (M8).** Build surrogates
   on the user's own run database (POD-GPR, with uncertainty) [S27][S28]. Use them to write better initial fields: the
   solver still has to meet its own convergence test, so a poor guess costs time and cannot change the answer
   [S17]. Add ensemble Kalman inversion for data assimilation [S22][S23]. Keep PINNs as a field-reconstruction tool
   with hard boundary conditions and held-out error bars. AetherCFD's own PINN on a turbulent 3-D jet reached only
   RMSE/std 0.78 to 0.81, and the literature agrees that soft-constraint PINNs struggle on turbulent flows [S2][S6][S8].

GPU work stays where a 4 GB laptop GPU helps: post-processing, ML training and quick-look side solvers. Linear-solver
offload for OpenFOAM is a research track (R1). The published evidence shows little gain below about ten million cells
per GPU [G2][G3][G8][G9], and Fluent's own guide budgets about 1 GB of GPU memory per million cells [V28].

## 2. Where AetherCFD stands

These features exist, and this plan builds on them:

* **Command layer**: plan, approve, journal and revert for every change (`commands/`, `project/journal.py`). The
  copilot can only act through it.
* **Guided workflow**: 12 stages with gates for dimensions, BC coverage, U/p pairing, scheme coverage, checkMesh, y+,
  species sum, Mach and convergence (`case/guided.py`, `case/gates.py`, `studio/panels/workflow/`).
* **Grid-convergence study**: three levels for blockMesh cases, Celik et al. procedure (`case/gridstudy.py`).
* **WSL run manager**: detached runs, signal control on both forks, staging, guards for log growth, repetition,
  disk, memory and stalls, and liveness that tolerates missing replies (`runs/`, `backend/wsl/`, ADR 0003 and 0006).
* **AetherMesh**: checkMesh-exact quality maps, grading designer, O-grid hole blocking, y+ first-cell planner, STL and
  CAD intake, snappyHexMesh setup on both forks, strategy advisor (`mesh/`).
* **Numerics lab**: 17 rules with one-click fixes (`case/checks.py`).
* **cfd_agents**: Murphy, Connie, Reggie and Darwin out of process (`services/cfd_agents/`).
* **GPU and ML**: CuPy iso-surfaces and POD/DMD (`results/iso.py`, `results/modes.py`), a GP surrogate
  (`ml/surrogate.py`), PINNs in a separate torch env (`ml/pinn.py`, `ml/pinn_worker.py`), optimal-interpolation
  assimilation (`assim/measurements.py`).
* **Test harness**: about 416 unit and studio tests, WSL live tests, a lossless corpus test, `--smoke` and `--sweep`
  GUI passes (`studio/smoke.py`, `studio/sweep.py`), `faulthandler` to `crash/faulthandler.log`
  (`studio/app/bootstrap.py`), and an import-linter contract that keeps the core free of Qt.

**In flight elsewhere, so not planned here:** QoIs and conservation gates (`case/qoi.py` on `feat/qoi-postprocessing`),
cluster runs (PR #8), the close-without-render fix (PR #9).

**Still open from the AUDIT roadmap** (still valid; this plan refers to them where they connect): y+ and gate results
as viewport maps, run anomaly to "why?", reproduce or branch a run from `provenance.json`, more templates, the physics
graph view, and the data layer.

## 3. What the commercial codes do, and the OpenFOAM equivalent

| Area | What the vendor does | OpenFOAM part that can do the same | Item |
|---|---|---|---|
| Guided meshing | Fluent watertight workflow: import, local sizing, surface mesh, describe geometry, boundaries, layers, volume mesh [V1]. Edit and Update marks downstream tasks out of date [V4]. Fault-tolerant workflow wraps dirty CAD and detects leaks from a set threshold [V3][V5] | `surfaceCheck`, `surfaceFeatureExtract`/`surfaceFeatures`, blockMesh, the three snappy phases, checkMesh; leak path output on ESI | M1 |
| Physics-driven sizing | SimScale: one fineness slider; layers only on wall BCs; invalid setups (two BCs on a face, inlet-only steady case) flagged before meshing [V15]. COMSOL: each physics adds size and layer suggestions [V16] | `mesh/wall.py` y+ planner, `mesh/strategy.py`, gates | Q7, M1 |
| Hybrid meshes | Mosaic poly-hexcore: octree hex core, poly-prism layers, polyhedra between them. 20 to 50 % fewer cells than hexcore *(vendor claim)* [V6][V7][V8]. STAR-CCM+: wrapper, poly, trimmed, prism, thin meshers in one pipeline [V11] | snappyHexMesh (hex core plus layers), cfMesh `cartesianMesh` (ESI) [LOCAL], `polyDualMesh` [LOCAL] | M2 |
| Layer robustness | Layer compression and stop columns in collision zones; marching directions smoothed to a quality floor [V9] | snappy `nRelaxedIter`, `meshShrinker`, `nOuterIter` [O12][O13]; cfMesh `optimiseLayer` [O15] | Q7, M2 |
| Adaptation | Fluent PUMA polyhedral adaption on gradient criteria; "adapt only a reasonably converged solution" [V10]. STAR-CCM+ AMR: several criteria, refine to the smallest size requested, coarsen only if all agree [V13]. CONVERGE remeshes at run time [V14] | `topoSet` plus `refineMesh` (both forks) [LOCAL]; `dynamicRefineFvMesh` (ESI); `refiner` in `fvMeshTopoChangers` (v14) [O17][LOCAL] | M5 |
| Initialisation | Fluent hybrid init: Laplace equations for velocity and pressure [V21]. FMG: Euler on coarse levels [V20]. STAR-CCM+ grid sequencing [V26] | `potentialFoam -initialiseUBCs -writep` [O1]; coarse grid-study level plus `mapFields` | Q5, M3 |
| Steering | Fluent steering: first to target order at low CFL, then CFL adjusted from residual history, CFL cut on divergence [V22]. STAR-CCM+ automatic CFL picks CFL and relaxation [V24][V25] | SIMPLEC `consistent yes` [O2]; `localEuler` [O7]; relaxation and `maxCo` edits with `runTimeModifiable` [O6] | M3 |
| Divergence recovery | Fluent `divergence_prevention` changes numerics when it sees a divergence pattern [V23] | `runTimeControl` `minMax` and `equationInitialResidual` (ESI) [O5][LOCAL]; `stopAtFile` (v14) [LOCAL]; GUI guards | Q6 |
| Convergence | Fluent's coupled solver relaxes through pseudo time or a Courant number and solves with AMG (F cycles by default) [V18][V19]. STAR-CCM+ adds a continuity accelerator because the linear system can converge while mass imbalance remains [V25] | `residualControl` [O4], QoI gates (QoI branch) | M3, M4 |
| GPU solvers | Fluent native multi-GPU since 2023 R1; DrivAer on one A100 11× faster than 48 cores *(vendor claim)* [V27]; about 1 GB per million cells in single precision [V28]. STAR-CCM+ GPU on AmgX since 2022.1; coupled solver in 2306 with "equivalent flow solutions" [V29][V30][V31] | OGL/Ginkgo (ESI), PETSc4FOAM plus AmgX (ESI), ESI v2606 stdpar branch [G1][G4][O19] | R1 |
| ML | SimAI trains on 30 to 100 past runs, geometry in, 3-D fields and a confidence out [V32]. Simcenter PhysicsAI (May 2026) and HEEDS AI predictor learn from past and DOE runs [V34][V35]. NVIDIA cites AI initialisation for Fluent *(vendor claim)* [V33] | `ml/surrogate.py`, `results/modes.py`, run provenance | M6, M7 |
| Adjoint | Fluent discrete adjoint plus morphing and a gradient optimiser [V36]. STAR-CCM+ adjoint with sensitivity smoothing [V37] | ESI `adjointOptimisationFoam` (continuous adjoint, incompressible, SA) [O9][O10][LOCAL]; DAFoam (GPL-3.0) [S24] | R4 |

The vendors split work into small tasks, each with a state. They treat a failed layer or a diverging run as a normal
event with a known next step, and their ML products need tens of past runs before they help [V32]. AetherCFD has the
parts for each of these. It lacks the chain that joins them, and the tests that keep that chain working.

## 4. Quick wins (each S to M, about 6 to 8 weeks in total)

### Q1. CI on GitHub Actions with headless Qt and VTK

* **Why.** No CI exists, so every merge relies on someone remembering to run 400+ tests on one laptop. The AUDIT
  already found a "unit" test that needs WSL (`test_run_scripts`) and a flaky editor test. CI would have caught both.
* **Evidence.** The free plan includes 2,000 minutes a month for private repos. Overage is $0.006 a minute on Linux
  and $0.010 on Windows [R1]. Older docs counted Windows minutes double; budget for that until the docs say otherwise.
  Hosted runners for private repos have 2 CPUs, 8 GB RAM and no GPU [R3]. Pin `windows-2025` and `ubuntu-24.04`,
  because `-latest` labels move [R2]. Qt needs a virtual display or `QT_QPA_PLATFORM=offscreen` on Linux [R5].
  `pyvista/setup-headless-display-action` installs Mesa on Windows for offscreen VTK [R6]. Since VTK 9.4 one wheel
  picks EGL or OSMesa at run time on Linux [R7]. `astral-sh/setup-uv` caches uv on `uv.lock` [R8].
* **Plan.**
  * Set `timeout-minutes` on every job (the default is 360) and cancel superseded runs with `concurrency` [R4].
  * Job `fast` (Ubuntu, every push and PR, 10 min limit): ruff, `lint-imports` [R26], pyright, then
    `pytest tests/unit -m "not wsl and not gl and not slow"`.
  * Job `gui-win` (Windows, PRs to main, 25 min limit, cancel superseded runs): Mesa offscreen,
    `pytest tests/studio -m "not gl"`, `-n 2 --max-worker-restart 5`. On failure, upload `faulthandler.log`,
    `studio.log` and screenshots.
  * Mark the WSL-dependent test in `tests/unit/test_run_scripts.py` as `wsl`.
  * The PINN tests look up `.ml-env` next to the checkout (AUDIT), so make them skip with a clear reason in CI.
  * Estimated budget: about 4 min × 80 pushes on Linux plus 15 min × 25 PRs on Windows, roughly 700 to 1,100 minutes
    a month at the 2× Windows rate.
* **Touches.** New `.github/workflows/ci.yml`; `pyproject.toml` (`[tool.pytest.ini_options]`: `timeout_method =
  "thread"`, `faulthandler_timeout`, `qt_api = "pyside6"`, `qt_log_level_fail`); `tests/conftest.py`.
* **Effort.** S to M. **Risk.** Medium. Mesa rendering is not the NVIDIA driver, so keep `gl` tests local (the marker
  exists). QWebEngine under offscreen is not officially supported in Qt 6 [R5], so the terminal tests start as
  allowed-to-fail.
* **Accept when.** A PR shows both jobs green. A test that sleeps forever is killed inside its timeout and leaves a
  thread dump as an artifact.

### Q2. Hang and crash forensics

* **Why.** Today a native crash leaves `faulthandler.log` and nothing else, and a hang leaves nothing at all. The two
  VTK deadlocks were found by hand.
* **Evidence.** `faulthandler.dump_traceback_later` dumps every thread's stack from a watchdog thread. `register()`
  does not exist on Windows. pytest has `faulthandler_timeout` built in [R13]. pytest-timeout's thread method is the
  portable one: it dumps stacks and calls `os._exit` [R12]. xdist restarts a crashed worker and fails only that test
  [R14]. pytest-forked does not work on Windows [R15]. WER `LocalDumps` writes per-app minidumps, but it is an HKLM
  registry key and needs admin [R20]. ProcDump `-h` writes a dump when a window hangs for 5 s [R21]. WinDbg
  `!analyze -hang` names the threads that block others [R22]. `qInstallMessageHandler` routes Qt warnings, and
  `QT_FATAL_WARNINGS` turns them into a stop with a backtrace [R23].
* **Plan.**
  1. Route Qt messages into `studio.log` with a handler in `studio/__main__.py` beside `_install_excepthook`.
  2. Give `--sweep` the same `AETHER_SMOKE_WATCHDOG` dump that `--smoke` has, per step, so a hung step names itself
     in `sweep.json`.
  3. Add a GUI-thread heartbeat: a `QTimer` stamps a time, and a watchdog thread dumps all stacks to `crash/` when
     the GUI thread has been stuck for more than 10 s. A stuck viewport then leaves a stack trace.
  4. Add **Help ▸ Report a problem**: it zips `faulthandler.log`, the tail of `studio.log`, the last `sweep.json`, the
     versions and the hang dumps into one local file. Nothing is uploaded. Sentry or Crashpad stay off, because dumps
     can hold paths and environment variables [R28].
  5. **[APPROVAL]** Optional, documented in INSTALL.md: WER `LocalDumps` for `AetherCFD.exe` (registry) and ProcDump
     for hang dumps (a Sysinternals download).
* **Touches.** `studio/app/bootstrap.py`, `studio/__main__.py`, `studio/sweep.py`, `studio/smoke.py`, a new
  `studio/app/watchdog.py`, the Help menu in `studio/shell/main_window.py`.
* **Effort.** S. **Risk.** Low. The heartbeat must not fire during a long, legitimate modal dialog, so pause it around
  `exec()`.
* **Accept when.** A test that blocks the GUI thread for 15 s leaves a dump in `crash/` that names the blocking frame.
  The report zip opens and holds no file from the case folder.

### Q3. Threading discipline as a written rule with checks

* **Why.** Both recent deadlocks came from VTK objects shared across threads, or from rendering at the wrong time.
  `results/foam_reader.py` now deep-copies (`2d8564e`). But `studio/panels/viewport/worker.py` still says the GUI
  "receives ready-to-render vtkPolyData (shallow copies)", and `results/pipeline.py`, `results/sections.py` and the
  worker still use `ShallowCopy` in about 20 places. Some of these may be safe, but nothing states which.
* **Evidence.** Qt: widgets belong to the main thread; delete a QObject only in its own thread; a blocking queued
  connection to the same thread deadlocks [R16]. Deleting a running QThread crashes [R17]. VTK: rendering is not
  thread-safe, and a GL context cannot be shared across threads on Windows [R18]. pyvistaqt documents no method as
  safe from a worker thread, and its `closeEvent` stops the render timer first [R19].
* **Plan.** Write ADR 0008 with these rules:
  1. Only the GUI thread touches widgets, `QtInteractor`, render windows and `render()`.
  2. A worker runs `Update()` only on pipelines it owns.
  3. Data crosses threads as a `DeepCopy`, or as the output of a filter the worker will never run again. A frozen
     output may be shallow-copied, and each such site carries a comment that says why it is safe.
  4. On close: stop timers and callbacks, disconnect render triggers, and never render once close has started (the
     `1959cc7` pattern).
  5. Never call `exec()` dialogs from code under test; tests patch them out [R29].

  Add a debug assertion, `QThread.currentThread() is app.thread()`, in the viewport's render path, switched on in tests
  and `--sweep`. Add an import-linter contract so that `aethercfd.results` never imports `pyvistaqt` or PySide6. Audit
  the `ShallowCopy` sites against rule 3.
* **Touches.** `docs/adr/0008-threading.md`, `studio/panels/viewport/worker.py`, `studio/panels/viewport/panel.py`,
  `results/pipeline.py`, `results/sections.py`, `pyproject.toml` (importlinter).
* **Effort.** S. **Risk.** Low. Deep copies cost memory on million-cell meshes; measure on the 1.43M-cell multi-region case
  from ADR 0005.
* **Accept when.** A soak test opens, re-reads and closes a multi-region viewport 200 times while the worker loads.
  It finishes with no hang and no assertion.

### Q4. Fault injection at the WSL boundary

* **Why.** `backend/wsl/runner.py` is the only code that runs `wsl.exe`. That makes it the right place to inject
  faults. The `feeeb4a` liveness bug (a healthy run marked lost under load) shows that WSL can return nothing, garbled
  text or UTF-16 under stress.
* **Evidence.** Hypothesis `RuleBasedStateMachine` checks invariants over random sequences of operations [R24].
  `pytest-subprocess` fakes `Popen` output and return codes [R24]. A shim script on PATH covers what a mock cannot.
* **Plan.** Add a `FaultyRunner` test double with modes: hang, empty reply, garbled reply, UTF-16 reply, exit in the
  middle of a stream, slow reply. Then add a Hypothesis state machine over `RunManager` (launch, probe, reattach,
  revive, stop). Its invariant: a run with a live process group is never `lost`, and a run is `lost` only after two
  definite `DEAD` replies.
* **Touches.** `backend/wsl/runner.py` (an injectable seam, if one is missing), `runs/manager.py`,
  `tests/unit/test_run_alive.py`, a new `tests/unit/test_run_manager_states.py`.
* **Effort.** S to M. **Risk.** Low.
* **Accept when.** The state machine passes 500 examples in CI. Reverting `feeeb4a` makes it fail.

### Q5. Potential-flow initialisation and a SIMPLEC preset

* **Why.** A uniform or zero start is the most common reason a beginner's steady case blows up in its first 50
  iterations. Fluent makes hybrid (Laplace) initialisation the default for steady single-phase cases [V21].
* **Evidence.** `potentialFoam -initialiseUBCs -writep` solves for a velocity potential and writes starting U and p
  (irrotational, inviscid, incompressible) [O1]. Both forks ship it [LOCAL]. SIMPLEC (`consistent yes;`) usually
  removes the need for pressure relaxation and allows U relaxation near 0.9 [O2].
* **Plan.**
  * Add a guided question in the BC/IC stage: "Start from a potential-flow field?" (default yes for incompressible
    steady cases). It adds a `potentialFoam` step before the solver in the run spec and copies `0/` to `0.orig/`
    first, so the start can be repeated.
  * Add a "SIMPLEC" choice to the Numerics question, with a rule in `case/checks.py` that warns when
    `consistent yes` is combined with p relaxation below 1.
  * Compressible cases keep a uniform or `setFields` start, and the question says why.
* **Touches.** `case/guided.py`, `runs/spec.py`, `runs/scripts.py`, `case/checks.py`, `templates/create.py`.
* **Effort.** S. **Risk.** Low. `potentialFoam` can fail on some outlet BCs, so the step must fall back to the
  uniform start and log why.
* **Accept when.** On pitzDaily and two other incompressible templates (both forks), the potential start reaches the
  residual target in fewer iterations than the uniform start, and the final QoIs agree within 0.5 %.

### Q6. Divergence watchdog with rollback to the last good time

*Status (2026-11-16): built, see [docs/divergence.md](../divergence.md). The limits come from a corpus of 111
real logs. The ESI `runTimeControl` backstop was left out: its residual condition cannot see NaN.*

* **Why.** A beginner's diverged run ends with NaNs in the newest time folder, and often the last good folder is
  already gone. Fluent watches for divergence patterns and changes the numerics itself [V23]. AetherCFD should stop
  early, keep good data and propose the change for approval.
* **Evidence.** ESI `runTimeControl` can stop and write on `minMax` limits, on `equationInitialResidual`, or on
  `minTimeStep` [O5]. It is present in v2306 and absent in v14 [LOCAL]. v14 has a `stopAtFile` function object
  [LOCAL]. The run manager already stops runs with SIGUSR2 (write and exit) on both forks (ADR 0006).
* **Plan.**
  * Add a `DivergenceGuard` in `runs/guards.py` as a pure function of samples. It trips on any of: a NaN or `inf` in
    the log, the same field "bounding" more than N times in a row, an initial residual more than 100× its running
    minimum, a Courant number above a limit, or a `fieldMinMax` value outside a physical range (T < 0, k < 0,
    |U| > 10× the reference speed).
  * On a trip, the manager sends write-and-stop (signals) or creates the stop file (v14). It keeps the last two good
    writes, and the run spec sets `purgeWrite` to at least 3.
  * It then offers one plan: restart from the last good time with relaxation × 0.7, `maxCo` × 0.5 or first-order
    convection, with Murphy's diagnosis attached.
  * On ESI the guided Run stage also writes a `runTimeControl` object, so the solver stops itself even when the GUI
    is closed.
* **Touches.** `runs/guards.py`, `runs/manager.py`, `runs/logparse.py`, `case/checks.py` (the restart edits),
  `services/cfd_agents/` (Murphy input), `studio/panels/run/panels.py`.
* **Effort.** M. **Risk.** Medium. Limits set too tight stop healthy transients. Start with "warn", move to "stop"
  only on NaN or an order-of-magnitude blow-up, and test against a corpus of real logs (a 1M-cell 3-D jet case and
  Sandia D, read-only).
* **Accept when.** A pitzDaily run with relaxation set to 1.0 trips the guard before any NaN is written. The rollback
  plan, once applied, converges. No healthy run in the log corpus trips "stop".

### Q7. Layer planner that writes `thicknessModel` and reads coverage back

* **Why.** `mesh/wall.py` already computes the first-cell height and `mesh/snappy.py` writes layers. Neither tells the
  user how many layers were actually added. Fluent and Hexpress advertise layer insertion as a headline feature
  [V9]. snappy's silent layer collapse is its best-known weakness [M13][O14].
* **Evidence.** ESI v2012 added `thicknessModel` (`firstAndExpansion` and others) [O11]. ESI v2206 added
  `nOuterIter`, which adds layers in several passes; on the release sphere test, mean layer coverage rose from 2.38
  to 2.62 and from 2.7 to 2.91 [O12]. v2306 documents `nRelaxedIter`, `mergePatchFacesAngle` and the
  `displacementMotionSolver` shrinker [O13]. Foundation has none of these keywords, so v14 keeps the current
  relative-size path.
* **Plan.**
  * On ESI, write `thicknessModel firstAndExpansion` with the first-cell height from the y+ planner, and
    `nOuterIter 2..3`.
  * After meshing, parse the "Extruding … layers" table from `log.snappyHexMesh` and report coverage per patch: the
    share of faces that got layers and the mean thickness achieved against the target.
  * Add a gate in the Mesh stage that warns when a wall patch has less than 80 % coverage and links to the fallback
    (M2).
* **Touches.** `mesh/wall.py`, `mesh/snappy.py`, `case/gates.py`, `copilot/mesh_tools.py`.
* **Effort.** S. **Risk.** Low.
* **Accept when.** On the snappy test geometry used in `tests/wsl/test_snappy_live.py`, the gate reports a per-patch
  coverage that matches the snappy log table, on ESI. On v14 the gate says the keyword is not available.

### Q8. Shrink the shareable exe

* **Why.** The installed build (dated 2026-10-12) is **3.2 GB**, and **about 2.0 GB of that is NVIDIA CUDA
  libraries**: cuBLAS 737 MB, cuSPARSE 456 MB, cuSOLVER 450 MB, NVRTC 179 MB, nvJitLink 84 MB, cuRAND 78 MB [LOCAL].
  Students without an NVIDIA GPU download all of it for nothing.
* **Evidence.** PyInstaller writes `warn-<name>.txt` and an import graph (`xref-<name>.html`) that show what pulled
  each module in [R27]. `core/gpu.py` already falls back to numpy, so the CUDA libraries are optional by design.
* **Plan.** Ship a CPU build plus an optional "GPU pack". Two ways to do it:
  * Move CuPy work (POD, iso-surfaces) into the existing out-of-process ML environment, which already holds the CUDA
    libraries for torch.
  * Or install CuPy into a side environment on demand and load it from there.

  The first keeps one CUDA stack on the machine. Archive the PyInstaller warn and xref files with each build, and add
  a size budget check to `packaging/build.py`.
* **Touches.** `packaging/aethercfd.spec`, `packaging/build.py`, `pyproject.toml`, `core/gpu.py`,
  `results/modes.py`, `results/iso.py`.
* **Effort.** M. **Risk.** Medium. Moving GPU iso-surfaces out of process adds a copy. Measure with the 1.43M-cell
  case before choosing.
* **Accept when.** The CPU zip is under 1.3 GB. All GPU features fall back cleanly without the pack. With the pack,
  `test_iso_gpu.py` and `test_pod_and_dmd_agree_on_cpu_and_gpu` pass.

## 5. One to three months

### M1. Meshing as a task chain with stale states and a leak check

* **Why.** AetherMesh has the pieces (intake, features, snappy setup, quality, hole blocking) as separate tools. A
  beginner does not know the order, and nothing says that the layers are stale after the STL changed.
* **Evidence.** Fluent's watertight workflow is a fixed task list with optional tasks [V1], and it can be scripted
  [V2]. A finished task is up to date and locked. An edit plus Update marks every downstream task out of date [V4].
  The fault-tolerant workflow adds region detection and a leakage threshold before wrapping [V3]. SimScale checks BC
  and material assignment before it meshes and lists what is invalid [V15]. COMSOL's Application Builder turns a
  model into an app with fixed inputs and forms [V17], which is the same idea for templates. STAR-CCM+ moved from a wizard to
  templates with an in-file guide linked to tree nodes [V12].
* **Plan.** Model the mesh stage as tasks:
  1. Geometry check (watertight, normals, open edges).
  2. Regions and leaks: `locationInMesh`/`locationsOutsideMesh`, leak path on ESI.
  3. Features.
  4. Background mesh.
  5. Castellate.
  6. Snap.
  7. Layers (Q7).
  8. checkMesh.
  9. Fix (M2).

  Each task stores its inputs' fingerprints, as stage "done" marks already do in `case/guided.py`. An upstream edit
  marks downstream tasks stale. Each task shows its log, a progress bar and its gate inline. Before meshing, a
  pre-mesh check flags unassigned patches, a wall patch named "inlet", or an internal flow with only inlets.
* **Touches.** `case/guided.py` (task sub-states), `mesh/geometry.py`, `mesh/snappy.py`, `mesh/strategy.py`,
  `runs/spec.py` (run snappy by phase: `castellatedMesh`/`snap`/`addLayers` switches),
  `studio/panels/aethermesh/panel.py`, `copilot/mesh_tools.py`.
* **Effort.** L. **Risk.** Medium. Running snappy one phase at a time needs care with time folders and
  `-overwrite`. Test on both forks.
* **Accept when.** On the motorBike tutorial, a beginner can go from STL to a checked mesh by pressing "Next" only.
  Changing the STL turns tasks 1 to 9 stale. A deliberately leaky STL stops at task 2 with the leak shown in the
  viewport (ESI).

### M2. Check, diagnose and adjust, with a cfMesh fallback

* **Why.** This is item 5 of the meshing survey, and it is still open. The commercial meshers compress or stop prism
  columns and smooth until a quality floor holds [V9]. AetherCFD should run the same loop with the tools it has.
* **Evidence.** cfMesh `cartesianMesh` and `improveMeshQuality` exist in v2306 [LOCAL]. cfMesh's `boundaryLayers`
  takes `nLayers`, `thicknessRatio` and `maxFirstLayerThickness`, and `optimiseLayer` smooths the layer normals
  [O15]. `improveMeshQuality` runs its smoother on a mesh read from disk [O16], but users report it can make a mesh
  worse, so re-check afterwards. Smoothers that "never make a mesh worse" rest on optimisation-based node movement,
such as Knupp's target-matrix paradigm [M12]. In one thesis, snappyHexMesh "was incapable of generating quality viscous layers" on
  a turbine case where cfMesh and Hexpress could [M13].
* **Plan.** A bounded loop (at most 3 rounds, keep the best mesh so far, never overwrite `polyMesh` without approval):
  run checkMesh and Q7 coverage, map the failures to edits (more `nSmoothNormals`, lower `featureAngle`, relaxed
  quality controls, `nOuterIter`), re-mesh, and compare. If coverage stays below the gate on ESI, offer cfMesh
  `cartesianMesh` with `boundaryLayers` from the same y+ plan, then checkMesh. Show both results side by side.
* **Touches.** `mesh/strategy.py`, `mesh/quality.py`, `mesh/snappy.py`, a new `mesh/cfmesh.py`, `case/gates.py`.
* **Effort.** M to L. **Risk.** Medium. cfMesh exists only in ESI here, so v14 users get the snappy loop alone.
* **Accept when.** On two geometries where snappy's default layers cover less than 60 % of a wall, the loop or the
  cfMesh fallback reaches at least 85 % coverage with checkMesh passing.

### M3. Solution steering as a staged run recipe

* **Why.** Q5 and Q6 handle the start and the crash. Steering handles the iterations between them: it starts on
  stable first-order settings and ends on the target schemes.
* **Evidence.** Fluent steering runs FMG, then ramps from first order to the target order at low CFL, then adjusts
  CFL from the residual history and cuts it on divergence [V22]. Its FMG initialisation solves Euler on up to 5
  coarse levels [V20]. STAR-CCM+ automatic CFL picks CFL and relaxation and became the steady default in 2020.1
  *(vendor claim: up to 2.4× faster)* [V24][V25]. In OpenFOAM, `localEuler` is a first-order pseudo-transient
  scheme for steady results [O7]. Relaxation sits in `fvSolution` [O3]. `runTimeModifiable` re-reads dictionaries
  during a run [O6].
* **Plan.** A recipe is a list of stages, each with edits and a switch condition:
  1. Potential start (Q5), or map from the coarse grid-study level with `mapFields`. This is AetherCFD's version of
     FMG and grid sequencing, and it reuses `grid_study/coarse`.
  2. First-order convection, relaxation 0.5/0.3, for N iterations or until residuals drop 2 decades.
  3. Target schemes, relaxation ramped to the SIMPLEC or SIMPLE defaults.
  4. QoI convergence (QoI branch) ends the run.

  This stage switch works on both forks: write and stop, apply the next edits as one journalled plan,
  restart from `latestTime`. On v14 the solver is a `foamRun` module [O8], so the same edits apply. A live edit through `runTimeModifiable` is an option where it is tested; whether every
  solver re-reads `fvSchemes` live needs checking per fork. The Workflow panel shows the recipe and which stage is
  active.
* **Touches.** `runs/spec.py` (multi-stage specs), `runs/manager.py`, `commands/builtin/run_cmds.py`,
  `case/guided.py`, `case/gridstudy.py` (coarse level reuse), `studio/panels/run/panels.py`.
* **Effort.** L. **Risk.** Medium. Automatic switching can hide a problem, so every switch is written to the journal
  with the residuals that triggered it.
* **Accept when.** On three templates started from a deliberately bad field, the default settings diverge and the
  recipe converges. The recipe's final QoIs match a hand-tuned run within 0.5 %.

### M4. Deeper verification

* **Why.** The GCI gate covers blockMesh cases and three levels. Real student cases use snappy or imported meshes,
  and their quantities are noisy.
* **Evidence.** Celik et al. is the three-grid procedure with a safety factor of 1.25 [M17], after Roache [M16]. Eça
  and Hoekstra fit four error models by least squares on four or more grids, pick one by its standard deviation, and
  set the safety factor from the observed order and the scatter [M19]. ASME V&V 20 combines numerical, input and
  experimental uncertainty at a validation point [M18]. `refineMesh` and `topoSet` exist on both forks [LOCAL].
* **Plan.**
  * Build levels for snappy and imported meshes with `refineMesh` (whole mesh or a `cellSet`). This is AUDIT roadmap
    item 1. Note that `refineMesh` gives r = 2, which is a large step for 3-D cases.
  * Add the Eça and Hoekstra estimator when four or more levels exist, and show both estimates.
  * Add a V&V 20 panel in the Validation stage. It takes u_num from the study, u_input from a GP sensitivity or a
    small sweep (M6), and u_D from the Measurements CSV `std` column, and reports E ± u_val.
  * Feed QoIs from `case/qoi.py` (once merged) into the study quantities.
* **Touches.** `case/gridstudy.py`, `assim/measurements.py`, `case/qoi.py` (after merge), `case/gates.py`,
  `studio/panels/workflow/panel.py`.
* **Effort.** M. **Risk.** Low. Refining the snappy layers with `refineMesh` changes layer y+, so the gate must say so
  (the existing y+ explanation covers half of this).
* **Accept when.** The study runs on a snappy case on both forks. On a published four-grid data set from the Eça and
  Hoekstra paper, the estimator reproduces their uncertainty within rounding.

### M5. Indicator-driven refinement on hex meshes

* **Why.** Adaptation is how the commercial codes get accuracy per cell [V10][V13]. A full anisotropic remesher does
  not fit OpenFOAM's hex and polyhedral meshes (R3), but isotropic refinement where a sensor says it is needed does
  fit, with stock tools.
* **Evidence.**
  * Fluent adapts on gradients (pressure for shocks, velocity, k/ε, T or species) and warns against adapting an
    unconverged solution [V10].
  * STAR-CCM+ combines criteria, refines to the smallest size requested and coarsens only when all criteria agree
    [V13].
  * The multiscale metric of Loseille and Alauzet minimises the Lp interpolation error for a fixed node count [M6].
    For isotropic refinement, its Hessian-based size is the sensor to use.
  * Foundation v12+ has `refiner` (hexRef8; non-hex cells are protected) [O17]. ESI has `dynamicRefineFvMesh`.
  * TU Darmstadt's blastAMR does load-balanced polyhedral AMR on ESI v2112+, but its README calls it work in progress
    [M14][M15].
* **Plan.**
  * *Static adaptation (both forks):* compute a sensor on the GPU with CuPy from a converged solution: a normalised
    Hessian of a chosen field, or the jump between grid-study levels. Mark the top x % of cells, grow the set by one
    layer, then run `topoSet`, `refineMesh -overwrite`, `mapFields` and a restart. Repeat two or three times, and run
    the GCI on the adapted sequence.
  * *Dynamic AMR (transient):* a setup form for `dynamicRefineFvMesh` (ESI) or `refiner` (v14) with sane
    `refineInterval`, `maxCells` and `nBufferLayers`. On v12+ the load distributor is set up separately [O18]. A
    gate refuses it on meshes that are mostly non-hex.
* **Touches.** A new `mesh/adapt.py`, `core/gpu.py`, `case/gridstudy.py`, `runs/spec.py`,
  `studio/panels/aethermesh/panel.py`, `copilot/mesh_tools.py`.
* **Effort.** L. **Risk.** Medium. `refineMesh` on snappy layers splits them badly, so exclude layer cells from the
  set by default.
* **Accept when.** On the backward-facing step, two adaptation rounds reach the same reattachment length as uniform
  refinement with at most half its cells. The adapted mesh passes checkMesh on both forks.

### M6. A run database, surrogates over it, and sweeps

* **Why.** Every run already writes `provenance.json`, and nothing reads it back (AUDIT). SimAI and Simcenter
  PhysicsAI both sell "learn from your past runs" [V32][V35]. AetherCFD can do a small version of this with the data it
  already has, and report its error every time.
* **Evidence.** POD-GPR gives probabilistic predictions of whole fields, with data chosen actively [S28]. POD-NN does
  the same with an MLP [S27]. GP surrogates get hard above about 10 inputs [S29]. Deep ensembles give useful
  uncertainty and grow it out of distribution [S30]. SimAI needs 30 to 100 runs [V32].
* **Plan.**
  * Index provenance and QoIs of all runs under the case roots into one table (SQLite in `%LOCALAPPDATA%`).
  * Add a sweep engine: one run per parameter set, approved as one plan, on the run manager or a cluster (PR #8).
  * Add a POD-GPR field surrogate (reusing `results/modes.py` and `ml/surrogate.py`) with a held-out error and the GP
    uncertainty shown as a field.
  * Active learning proposes the next run, and with approval launches it. This closes the AUDIT gap "it never
    launches it".
  * Refuse more than 10 inputs, and say why.
* **Touches.** `runs/provenance.py`, a new `runs/database.py`, `ml/surrogate.py`, `results/modes.py`,
  `commands/builtin/run_cmds.py`, `studio/panels/analysis/panel.py`, copilot tools.
* **Effort.** L. **Risk.** Low to medium. Runs on different meshes need a common grid for POD; map them with
  `mapFields` or sample them on a fixed point cloud.
* **Accept when.** On a 12-run sweep of pitzDaily inlet speed, leave-one-out field error is reported, the GP
  uncertainty covers the held-out error at about the stated level, and the suggested next run lands where the
  uncertainty is largest.

### M7. ML warm start for the initial fields

* **Why.** This is the cheapest way for ML to help a CFD user: the solver still decides the answer.
* **Evidence.**
  * CFDNet predicts U, p and ν_t and lets the solver finish. It reported 1.9× to 7.4× speedups on steady laminar and
    RANS cases, with the solver's own convergence test still met, including on unseen geometries [S17].
  * A 2025 single-airfoil study reports 26.3× fewer iterations from a CNN initial field trained on one simulation
    [S17].
  * NVIDIA cites PhysicsNeMo-initialised Fluent runs *(vendor claim)* [V33].
  * Kochkov et al. show larger gains, but with learned corrections inside the solver and only on 2-D turbulence
    [S18].
* **Plan.** Add a guided option "Start from a similar past run". It offers, in order, the nearest past run in the
  database (M6) mapped with `mapFields`, the POD-GPR prediction, and `potentialFoam` (Q5). Report iterations and
  wall time against the potential start, every time. Never skip convergence checks.
* **Touches.** `case/guided.py`, `runs/database.py`, `ml/surrogate.py`, `results/write_fields.py`, `runs/spec.py`.
* **Effort.** M (after M6). **Risk.** Low. A bad guess can be worse than a potential start, so the report always shows
  the comparison.
* **Accept when.** Over the M6 sweep, the warm start needs fewer iterations than the potential start on at least 8
  of 10 held-out runs, and the final QoIs agree within the convergence tolerance.

### M8. PINNs with hard constraints and held-out errors

* **Why.** The PINN works on Kovasznay flow (0.06 to 0.4 %) and fails on a turbulent 3-D jet (RMSE/std 0.78 to 0.81).
  That gap is typical, so the UI should say so, and the cheap fixes from the literature should go in.
* **Evidence.**
  * Soft-constraint PINNs [S1] fail on moderately hard convection and reaction problems because the loss is
    ill-conditioned; curriculum and time-marching help [S2].
  * Adaptive loss weights fix unbalanced gradients [S3], and causal weighting helps time-dependent cases [S4].
  * NSFnets for turbulence needed boundary data from DNS [S8].
  * Of 76 ML-for-PDE papers that claimed to beat a numerical method, 60 used weak baselines [S6]. FEM beat PINNs on
    speed and accuracy in a direct test [S7].
  * Hard constraints: penalty and augmented-Lagrangian constraints in hPINN [S9]. An approximate distance function
    multiplies the network so BCs hold exactly [S10].
    Divergence-free outputs by construction [S12]. Physics encoded in the architecture, as in PeRCNN [S11].
  * The jaxpi reference code is licensed for non-profit research only, so reimplement from the papers [S5].
* **Plan.**
  * In `ml/pinn_worker.py`, add exact Dirichlet BCs through a distance function, a stream-function output for 2-D
    incompressible flow (divergence-free by construction), adaptive loss weights, a held-out probe set, and a 5-member
    ensemble for an uncertainty band (fits easily in 4 GB at the current network size).
  * Record dataset and model hashes in the result.
  * In the Analysis panel, label the PINN as "field reconstruction from sparse data" and show held-out error next to
    every result. Add a docs page with the 3-D jet numbers.
* **Touches.** `ml/pinn_worker.py`, `ml/pinn.py`, `studio/panels/analysis/panel.py`, `tests/unit/test_pinn.py`, a new
  `docs/research/pinn_limits.md`.
* **Effort.** M. **Risk.** Low (reporting) to medium (gains on 3-D jets are uncertain).
* **Accept when.** Kovasznay stays at or below the current error. A 2-D cavity reconstruction with hard BCs beats the
  soft-BC version on held-out probes. Every PINN result in the UI carries a held-out error.

### M9. Golden images and a weekly soak on the development laptop

* **Why.** Hosted runners have no GPU and no OpenFOAM (Q1). The bugs that matter most (GL, WSL, long runs) need the
  real machine.
* **Evidence.** pytest-pyvista compares renders against a cached image with a tolerance [R25]. The windows-2025
  runner image lists WSL 2, but nobody has shown that an OpenFOAM install fits on a 2-CPU runner [R9]. A self-hosted
  runner costs no minutes, but on a private repo anyone who can open a PR can run code on it, so it must never run
  PRs from forks [R10]. Installed as a service it runs in session 0, which cannot start WSL, so run it at logon
  [R11].
* **Plan.**
  * **[APPROVAL]** Install a self-hosted runner on the development laptop, labelled `wsl`, started at logon, this repo only,
    manual and weekly triggers only.
  * The weekly job runs `tests/wsl`, `--sweep` on three cases (pitzDaily, a CHT case, a multi-region case), golden
    images for Studio and Classic looks, and an exe `--self-test` with startup time over 3 runs (fail on a regression
    above 25 %).
  * It never runs when a solver is live: check the run records first and skip with a message.
* **Touches.** `.github/workflows/weekly.yml`, `studio/sweep.py`, `packaging/entry_gui.py` (`--self-test`), a new
  `tests/golden/`.
* **Effort.** M. **Risk.** Medium (machine availability; GPU shared with Ollama and training runs).
* **Accept when.** Four weekly runs in a row complete or skip cleanly. A seeded render regression fails the golden
  test.

## 6. Research tracks (XL, open outcome)

### R1. GPU linear solvers for OpenFOAM on this laptop **[APPROVAL: CUDA toolkit in WSL, OGL build]**

* **Why.** Students will ask whether OpenFOAM can solve on the GPU. On a 4 GB card the answer needs a measurement.
* **Evidence.**
  * OGL (GPL-3.0, Ginkgo BSD-3) offloads only the linear solve, as `GKOCG` with `executor cuda`; matrix assembly
    stays on the CPU [G1]. The Meccanica paper used ESI v2306 and found speedups that grow with problem size, from 1
    to 64M cells: about 4× on 4 A100s against 76 cores [G2]. OGL's CI now tests ESI v2512 only, and nothing shows it
    working on Foundation [G1].
  * A 2026 EXASIM report found that linear-solver-only offload gives "no or only limited advantages" outside
    pressure-dominated cases [G3].
  * SPUMA, a full GPU port, is clearly ahead only above about 10M cells per GPU [G8]. A stdpar study saw 0.28× on a
    1M-cell mesh [G9].
  * ESI v2606 (June 2026) is the first release with GPU offload through C++ stdpar, on a separate branch aimed at
    v2612 [O19]. CUDA on WSL2 has no full managed memory and limits pinned memory [G10].
  * The RTX 3050 Ti (GA107) runs FP64 at 1/64 of FP32 [G11].
* **Plan.** Build OGL against ESI v2306 in a separate user directory (not the system install). Benchmark the pressure
  solve only, on the lid-driven cavity at 0.5, 1, 2 and 4M cells. Report time per step with the share spent in the
  pressure solve (Amdahl), and compare against GAMG on 8 and 16 cores. Track v2612 stdpar on WSL2 and test it only
  when it ships. Skip PETSc4FOAM with AmgX (a heavy PETSc build, ESI only, gains reported on large meshes [G4][G5][G6][G7])
  and RapidCFD (CUDA 8 era [G21]).
* **Touches.** `backend/wsl/capabilities.py` (detect `libOGL`), `case/checks.py` (a rule for `GKO*` solvers),
  `docs/research/gpu_linear_solvers.md`.
* **Effort.** M for the benchmark, XL for anything shipped. **Risk.** High that the result is "not worth it". A written
  negative result still tells students what to skip.
* **Accept when.** A benchmark table exists, and the app either offers OGL as "experimental, ESI only, above N cells"
  or documents why it does not.

### R2. Ensemble data assimilation and model calibration

* **Why.** The OI step in `assim/measurements.py` corrects a field once. Ensemble methods correct the model (inflow,
  coefficients, a RANS correction field) with uncertainty, and they treat OpenFOAM as a black box.
* **Evidence.**
  * Iterative ensemble Kalman inversion for Reynolds stresses [S22].
  * DAFI is EnKF-based, needs no derivatives, gives UQ, has OpenFOAM I/O, and is Apache-2.0 [S23].
  * CONES couples OpenFOAM to a Python EnKF online. It reduced error in a channel LES, but the subgrid bias remained
    [S25].
  * Nudging toward data costs "negligible" extra [S26], but no public OpenFOAM `fvModels` nudging exists, so it would
    be AetherCFD's own code (Physics Code templates).
  * The main limit of learned closures: Reynolds stresses within 0.5 % can still give mean-velocity errors up to 35 %
    when injected explicitly [S21].
* **Plan.** Start with EnKF calibration of two or three scalars (an inlet profile parameter, a model constant) against
  the Measurements CSV, with ensemble runs through the run manager or the cluster. Then a steady nudging source as a
  Physics Code template. Field-inversion closures [S19] and tensor-basis networks [S31] stay last and case-specific.
* **Touches.** `assim/`, `runs/manager.py`, `codegen/templates.py`, `studio/panels/measurements/panel.py`.
* **Effort.** XL. **Risk.** Medium. Ensembles cost N runs, which is fine on the cluster and slow on the laptop.
* **Accept when.** On a synthetic twin (data made from a run with a known constant), EnKF recovers the constant within
  its reported spread.

### R3. Anisotropic metric-based adaptation

* **Why.** It is the mathematically best route to accuracy per cell [M5][M6], and the next step after M5.
* **Evidence.** Mmg (LGPL), its MPI version ParMmg, and NASA refine (Apache-2.0) adapt triangle and tet meshes only
  [M7][M8][M9]. Omega_h is
  BSD-2 and simplex-only [M10]. No published pipeline adapts OpenFOAM meshes with Mmg. In the UGAWG turbulent cases,
  the finite-volume pipelines converged more slowly than the finite-element one [M11].
* **Plan.** A gmsh tet mesh, then `refine` driven by an Lp metric from the OpenFOAM solution, then back to OpenFOAM
  (optionally through `polyDualMesh` to polyhedra, which exists on both forks [LOCAL]), then a solve. Test it on a 2-D
  case first.
* **Effort.** XL. **Risk.** High (tet quality for finite-volume, layer handling).
* **Accept when.** A 2-D airfoil case converges in drag with fewer cells than M5's isotropic adaptation.

### R4. Adjoint sensitivities and output-based error

* **Why.** The adjoint tells the user which cells and which shape changes matter for drag or pressure loss.
* **Evidence.** ESI `adjointOptimisationFoam` is a continuous adjoint for steady incompressible flow with
  Spalart-Allmaras, with shape and topology optimisation documented for v2312 [O9][O10]. It exists in v2306 here
  [LOCAL]. Output-based adaptation weights local residuals by the adjoint [M1][M2][M3][M4]. No published DWR
  adaptation on OpenFOAM's adjoint was found. DAFoam is GPL-3.0 and fine as a separate program [S24].
* **Plan.** First, surface sensitivity maps for one objective on an ESI incompressible case, shown on the walls. Then
  an adjoint-weighted refinement sensor for M5.
* **Effort.** XL. **Risk.** High (incompressible only; setup is expert-level).
* **Accept when.** The sensitivity map on a 2-D duct points to the same region that a finite-difference shape change
  confirms.

### R5. A GPU-native side solver for quick looks **[APPROVAL: install]**

* **Why.** The spec asks for explicit, justified backends beside OpenFOAM, LBM among them. On a 4 GB card, a
  single-precision LBM or high-order code is where the GPU actually beats the CPU.
* **Evidence.**
  * FluidX3D runs on OpenCL natively on Windows (no WSL), uses 55 bytes per cell, and lists 1181 to 2341 MLUPs/s on
    a 4 GB RTX 3050M Ti. Its licence allows public research, education and personal use, and bans commercial use [G14].
  * XLB is Apache-2.0 on JAX or Warp [G15]. NVIDIA Warp is Apache-2.0 [G19]. PyFR is BSD-3 and supports single
    precision on CUDA [G13].
  * NekRS is BSD-style but FP64-heavy [G12].
  * waLBerla (GPLv3), OpenLB (GPL-2.0) and Palabos (AGPLv3) can be called as separate programs [G16][G17][G18][G20].
* **Plan.** A "quick look" backend for external aero: same STL, a lattice sized to the VRAM, results read back as VTK
  into the viewport, and clearly labelled "LBM quick look, not a validated RANS result". XLB is the first candidate
  because of its licence. FluidX3D is the first candidate for speed, but only while AetherCFD stays free.
* **Effort.** XL. **Risk.** Medium (licence drift; result-comparison effort).
* **Accept when.** On a sphere at Re = 100 to 300, the quick-look drag matches a reference within 5 %, inside a
  time budget that makes it worth having.

### R6. Neural operators

* **Why.** They are the headline of CFD ML, so the roadmap should state a position on them.
* **Evidence.** FNO reached 0.86 % error with 1000 training samples on its easier case. On the harder case it was
  19 % with 1000 samples and 8.2 % with 10,000 [S13]. Geo-FNO trained on 1000 airfoils [S32], and GINO on 500 car
  shapes [S15]. DeepONet's approximation theorem says nothing about optimisation or generalisation error [S14].
  MeshGraphNets run 1 to 2 orders of magnitude faster than the simulator that made their training data [S16]. Code
  licences are open (neuraloperator MIT, PhysicsNeMo Apache-2.0).
* **Position.** A single user will rarely have hundreds of comparable runs, and 3-D training will rarely fit in
  4 GB. Revisit when the M6 database of a research group passes about 300 comparable runs, or to import a model
  someone else trained.

## 7. What this plan leaves out on purpose

* **A learned turbulence model applied to arbitrary cases.** It is ill-conditioned [S21], it generalises only within
  a flow class [S20], and the open toolchain (DAFoam) is GPL-3.0.
* **PINNs as a forward solver.** See M8 and [S6][S7].
* **PETSc4FOAM with AmgX, RapidCFD, NekRS on this laptop.** These were judged in R1 and R5.
* **Telemetry uploads.** Crash data stays local (Q2) unless a later, opt-in design is approved.

## 8. Order of work

| When | Items | Why this order |
|---|---|---|
| Weeks 1 to 2 | Q1, Q2, Q3, Q4 | Tests and forensics protect everything after them |
| Weeks 3 to 6 | Q5, Q6, Q7, Q8 | Visible robustness for beginners; a smaller download for the group |
| Months 2 to 3 | M1, M3, M4 (after the QoI merge) | The task chain and steering use Q5 to Q7; M4 needs QoIs |
| Months 3 to 4 | M2, M5, M6, M9 | Mesh loop and adaptation build on M1; the database feeds M7 |
| Months 4 to 5 | M7, M8 | Warm start needs the database; PINN rework is independent and can move earlier |
| Ongoing | R1 to R6 | Each starts with a written protocol and an approval where marked |

## References

All links were checked between 2026-10-07 and 2026-10-26. *(vendor claim)* in the text marks numbers whose test
conditions are not public.

**Vendors (public guides, blogs, papers, product pages)**
- [V1] Ansys Fluent 2025 R2 User's Guide, Watertight Geometry workflow — https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/flu_ug/tgd_user_workflow_guided_watertight.html
- [V2] PyFluent, meshing workflows — https://fluent.docs.pyansys.com/version/dev/user_guide/meshing/new_meshing_workflows.html
- [V3] PyFluent, fault-tolerant meshing example (exhaust system) — https://fluent.docs.pyansys.com/version/stable/examples/00-fluent/exhaust_system_settings_api.html
- [V4] Fluent 2024 R2 User's Guide, editing and updating tasks — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/tgd_user_workflow_guided_custom_task_edit.html
- [V5] Ansys blog, fault-tolerant meshing wraps non-watertight geometry — https://ansys.synopsys.com/blog/fluent-meshing-workflow-wraps-non-watertight-geometries
- [V6] Ansys Mosaic meshing product page — https://ansys.synopsys.com/products/fluids/ansys-fluent/mosaic-meshing
- [V7] Zore, Parkhi, Sasanapuri, Varghese, Ansys Mosaic poly-hexcore mesh for a high-lift aircraft, 21st Annual CFD Symposium, Bangalore (2019) — https://nal.res.in/themes/stark/cfdimgs/FullPaper/P41-ANSYS-Mosaic-Poly-Hexcore-Mesh-For-High-Lift-Aircraft-Configuration.pdf
- [V8] Zore, Caridi, Lockley, SAE 2020-01-5011 (DrivAer with Mosaic) — https://saemobilus.sae.org/papers/fast-accurate-prediction-vehicle-aerodynamics-using-ansys-mosaic-mesh-2020-01-5011
- [V9] Ansys 2025 R2 help, advanced prism meshing (auto reduction, stop columns, smoothing) — https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/icm_help/imesh_advprismmesh.html
- [V10] Fluent 2026 R1 User's Guide, mesh adaption overview — https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/flu_ug/flu_ug_AdaptOverview.html
- [V11] Simcenter STAR-CCM+ meshing solutions fact sheet — https://www.plm.automation.siemens.com/media/global/ja/Siemens%20SW%20Simcenter%20STAR%20CCM%2B%20meshing%20solutions%20Fact%20Sheet_tcm57-101050.pdf
- [V12] Siemens blog, simulation templates and Simulation Guide — https://blogs.sw.siemens.com/simcenter/your-ultimate-cfd-productivity-boost/
- [V13] Siemens blog, model-driven adaptive mesh refinement — https://blogs.sw.siemens.com/simcenter/whistle-while-you-mesh-simcenter-star-ccm-model-driven-adaptive-mesh-refinement-amr/
- [V14] CONVERGE, autonomous meshing — https://convergecfd.com/benefits/autonomous-meshing/
- [V15] SimScale docs, standard mesher — https://www.simscale.com/docs/simulation-setup/meshing/standard/
- [V16] COMSOL 6.1 reference manual, physics-controlled mesh — https://doc.comsol.com/6.1/doc/com.comsol.help.comsol/comsol_ref_mesh.23.11.html
- [V17] COMSOL 6.4 Application Builder — https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/application_builder_manual_intro.10.3.html
- [V18] Fluent 2024 R2 User's Guide, pressure-velocity coupling, pseudo time and Courant number — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/flu_ug_sec_uns_solve_pvel_1.html
- [V19] Fluent 2025 R1 Theory Guide, multigrid cycles — https://ansyshelp.ansys.com/public/views/secured/corp/v251/en/flu_th/flu_th_MultigridCycle.html
- [V20] Fluent 2024 R2 User's Guide, FMG initialization — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/flu_ug_sec_fmg_multigrid.html
- [V21] Fluent 2024 R2 Theory Guide, hybrid initialization — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_th/flu_th_sec_hybrid_initialization.html
- [V22] Fluent 2024 R2 User's Guide, solution steering — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/flu_ug_sec_solution_steering.html
- [V23] PyFluent API, `divergence_prevention` — https://fluent.docs.pyansys.com/version/dev/api/solver/_autosummary/settings/divergence_prevention_1.html
- [V24] Siemens blog, automatic CFL control (2020) — https://blogs.sw.siemens.com/simcenter/effortless-convergence-in-cfd-it-will-make-your-day/
- [V25] Volupe (reseller) blog, coupled solver settings in STAR-CCM+ — https://volupe.com/blog-volupe/coupled-solver-settings-in-simcenter-star-ccm/
- [V26] NASA HiLiftPW-5, RANS one-page summary R-006 (STAR-CCM+ grid sequencing) — https://hiliftpw.larc.nasa.gov/Workshop5/1_page_summaries_PDF/RANS_OnePageSummary_HLPW5_R-006.pdf
- [V27] Ansys blog, Fluent 2024 R1 (GPU solver) — https://ansys.synopsys.com/blog/fluent-2024-r1
- [V28] Fluent 2024 R2 User's Guide, GPU solver memory — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/flu_ug_sec_gpu_solver_gpu_memory.html
- [V29] Siemens blog, GPU acceleration for CFD (2022.1) — https://blogs.sw.siemens.com/simcenter/gpu-acceleration-for-cfd-simulation
- [V30] Siemens blog, coupled solver on GPU (2306) — https://blogs.sw.siemens.com/simcenter/cfd-coupled-solver-is-taking-off-on-gpu/
- [V31] Siemens blog, Simcenter STAR-CCM+ 2602 — https://blogs.sw.siemens.com/simcenter/simcenter-star-ccm-2602-released/
- [V32] Ansys SimAI product page — https://ansys.synopsys.com/products/simai
- [V33] NVIDIA blog, AI physics for aerospace and automotive design (Oct 2025) — https://blogs.nvidia.com/blog/ai-physics-aerospace-automotive-design-engineering
- [V34] HEEDS AI Simulation Predictor and Simcenter Reduced Order Modeling (trade press, Nov 2023) — https://www.industrialmachinerydigest.com/software/digital-twin/siemens-revolutionizes-engineering-simulation-with-heeds-ai-simulation-predictor-and-simcenter-reduced-order-modeling
- [V35] Siemens press release, Simcenter PhysicsAI (May 2026) — https://news.siemens.com/en-us/siemens-simcenter-physicsai/
- [V36] Fluent 2024 R2 User's Guide, adjoint solver theory overview — https://ansyshelp.ansys.com/public/Views/Secured/corp/v242/en/flu_ug/flu_adj_theory_overview.html
- [V37] Siemens blog, STAR-CCM+ adjoint — https://blogs.sw.siemens.com/simcenter/?p=30330

**OpenFOAM**
- [O1] OpenFOAM v2606 docs, potentialFoam — https://doc.openfoam.com/2606/tools/processing/solvers/rtm/basic/potentialFoam
- [O2] ESI commit introducing SIMPLEC (`consistent`) — https://develop.openfoam.com/Development/openfoam/-/commit/38177526ffd9719f34fcc0cec9339c301b1f461d
- [O3] OpenFOAM v13 User Guide, fvSolution — https://doc.cfd.direct/openfoam/user-guide-v13/fvsolution
- [O4] OpenFOAM v2606 docs, case termination (residualControl) — https://doc.openfoam.com/2606/tools/processing/numerics/solvers/case-termination
- [O5] OpenFOAM v2606 docs, runTimeControl — https://doc.openfoam.com/2606/tools/post-processing/function-objects/utilities/runTimeControl
- [O6] OpenFOAM v13 User Guide, controlDict — https://doc.cfd.direct/openfoam/user-guide-v13/controldict
- [O7] OpenFOAM v2606 docs, localEuler — https://doc.openfoam.com/2606/tools/processing/numerics/schemes/time/rtm/localEuler
- [O8] OpenFOAM v11 User Guide, solver modules and foamRun — https://doc.cfd.direct/openfoam/user-guide-v11/solvers-modules
- [O9] OpenFOAM v1906 release (adjointOptimisationFoam) — https://www.openfoam.com/news/main-news/openfoam-v1906
- [O10] adjointOptimisationFoam manual, v2312 — https://www.openfoam.com/documentation/files/adjointOptimisationFoamManual_v2312.pdf
- [O11] OpenFOAM v2012 release, snappyHexMesh `thicknessModel` — https://www.openfoam.com/node/188
- [O12] OpenFOAM v2206 release, snappyHexMesh `nOuterIter` — https://www.openfoam.com/node/455
- [O13] OpenFOAM v2306 docs, snappyHexMesh layers — https://doc.openfoam.com/2306/tools/pre-processing/mesh/generation/snappyhexmesh/layers/
- [O14] OpenFOAM v1806 release, layer `featureAngle` behaviour — https://www.openfoam.com/node/287
- [O15] cfMesh User Guide v1.1 — https://cfmesh.com/wp-content/uploads/2015/09/User_Guide-cfMesh_v1.1.pdf
- [O16] improveMeshQuality manual page — https://www.openfoam.com/documentation/guides/v2112/man/improveMeshQuality.html
- [O17] OpenFOAM v12 source, fvMeshTopoChangers refiner — https://cpp.openfoam.org/v12/fvMeshTopoChangersRefiner_8H_source.html
- [O18] CFD Direct, dynamic meshes — https://cfd.direct/openfoam/free-software/dynamic-meshes/
- [O19] OpenFOAM v2606 release, infrastructure (GPU offloading) — https://www.openfoam.com/news/main-news/openfoam-v2606/infrastructure

**Meshing and verification literature**
- [M1] Venditti, Darmofal, JCP 176 (2002) 40–69, doi:10.1006/jcph.2001.6967 — https://acdl.mit.edu/darmofalpubs/JCP_vol176_p40.pdf
- [M2] Venditti, Darmofal, JCP 187 (2003) 22–46, doi:10.1016/S0021-9991(03)00074-3 — https://acdl.mit.edu/darmofalpubs/JCP_vol187_p22.pdf
- [M3] Fidkowski, Darmofal, Review of output-based error estimation and mesh adaptation in CFD, AIAA J 49(4) (2011) 673–694, doi:10.2514/1.J050073 — https://acdl.mit.edu/darmofalpubs/Fidkowski_Review_AIAAJ.pdf
- [M4] Becker, Rannacher, Acta Numerica 10 (2001) 1–102, doi:10.1017/S0962492901000010
- [M5] Loseille, Dervieux, Alauzet, JCP 229(8) (2010), doi:10.1016/j.jcp.2009.12.021
- [M6] Loseille, Alauzet, Continuous mesh framework Part I, SIAM J Numer Anal 49(1) (2011) 38–60, doi:10.1137/090754078 — https://epubs.siam.org/doi/10.1137/090754078
- [M7] Mmg — https://github.com/MmgTools/mmg
- [M8] ParMmg — https://github.com/MmgTools/ParMmg
- [M9] NASA refine — https://github.com/nasa/refine
- [M10] Omega_h — https://github.com/sandialabs/omega_h
- [M11] UGAWG, turbulent verification cases — https://ntrs.nasa.gov/citations/20180006169
- [M12] Knupp, target-matrix paradigm, Eng. Comput. 28(4) 419–429, doi:10.1007/s00366-011-0230-1
- [M13] Piaget, MSc thesis, Polytechnique Montréal (2016) — https://publications.polymtl.ca/2147/1/2016_DanielPiaget.pdf
- [M14] Rettenmaier et al., SoftwareX 10 (2019) 100317, doi:10.1016/j.softx.2019.100317
- [M15] blastAMR (TU Darmstadt STFS) — https://github.com/STFS-TUDa/blastAMR
- [M16] Roache, JFE 116(3) (1994) 405, doi:10.1115/1.2910291
- [M17] Celik et al., JFE 130(7) (2008) 078001, doi:10.1115/1.2960953
- [M18] ASME V&V 20 — https://www.asme.org/codes-standards/find-codes-standards/standard-for-verification-and-validation-in-computational-fluid-dynamics-and-heat-transfer
- [M19] Eça, Hoekstra, JCP 262 (2014) 104–130, doi:10.1016/j.jcp.2014.01.006

**GPU**
- [G1] OGL — https://github.com/hpsim/OGL
- [G2] Olenik, Koch, Boutanios, Anzt, Meccanica (2024), doi:10.1007/s11012-024-01806-1 — https://publikationen.bibliothek.kit.edu/1000171847
- [G3] Hetmann (Upstream CFD), EXASIM report (2026) — https://oa.tib.eu/renate/items/823e9cda-34cd-47d7-b5c3-d75ae2408c41/full
- [G4] PETSc4FOAM (ESI external-solver module) — https://develop.openfoam.com/modules/external-solver
- [G5] NVIDIA AmgX — https://github.com/NVIDIA/AMGX
- [G6] Posey (NVIDIA), NAFEMS World Congress 2021 — https://www.nafems.org/publications/resource_center/nwc21-262-b/
- [G7] Sircar (ORNL), accelerating OpenFOAM on Frontier GPUs (2024) — https://www.olcf.ornl.gov/wp-content/uploads/Sircar-Accelerating-OpenFOAM-on-Frontier-GPUs-for-Fusion-Multiphysics-Simulations.pptx.pdf
- [G8] SPUMA, arXiv:2512.22215 — https://arxiv.org/html/2512.22215v1
- [G9] Malenza et al., arXiv:2507.18268 — https://arxiv.org/html/2507.18268v1
- [G10] NVIDIA, CUDA on WSL user guide — https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- [G11] NVIDIA Ampere GA102 architecture whitepaper — https://images.nvidia.com/aem-dam/en-zz/Solutions/geforce/ampere/pdf/NVIDIA-ampere-GA102-GPU-Architecture-Whitepaper-V1.pdf
- [G12] NekRS — https://github.com/Nek5000/nekRS
- [G13] PyFR installation and backends — https://pyfr.readthedocs.io/en/latest/installation.html
- [G14] FluidX3D (README and LICENSE.md) — https://github.com/ProjectPhysX/FluidX3D
- [G15] XLB — https://github.com/Autodesk/XLB
- [G16] waLBerla — https://www.walberla.net/
- [G17] OpenLB — https://www.openlb.net/overview/
- [G18] Palabos — https://gitlab.com/unigespc/palabos
- [G19] NVIDIA Warp — https://github.com/NVIDIA/warp
- [G20] GNU GPL FAQ, mere aggregation — https://www.gnu.org/licenses/gpl-faq.html#MereAggregation
- [G21] RapidCFD — https://github.com/Atizar/RapidCFD-dev

**Scientific ML**
- [S1] Raissi, Perdikaris, Karniadakis, JCP 378 (2019) 686–707, doi:10.1016/j.jcp.2018.10.045
- [S2] Krishnapriyan et al., NeurIPS 2021 — https://arxiv.org/abs/2109.01050
- [S3] Wang, Teng, Perdikaris, gradient pathologies — https://arxiv.org/abs/2001.04536
- [S4] Wang, Sankaran, Perdikaris, respecting causality — https://arxiv.org/abs/2203.07404
- [S5] Wang et al., An expert's guide to training PINNs — https://arxiv.org/abs/2308.08468 ; jaxpi licence — https://github.com/PredictiveIntelligenceLab/jaxpi
- [S6] McGreivy, Hakim, Nat. Mach. Intell. 6 (2024) 1256 — https://arxiv.org/abs/2407.07218
- [S7] Grossmann et al., IMA J. Appl. Math. (2024), doi:10.1093/imamat/hxae011
- [S8] Jin et al., NSFnets, JCP (2021) — https://arxiv.org/abs/2003.06496
- [S9] Lu et al., hPINN, SIAM JSC 43(6) (2021) — https://arxiv.org/abs/2102.04626
- [S10] Sukumar, Srivastava, exact imposition of BCs with distance functions — https://arxiv.org/abs/2104.08426
- [S11] Rao et al., PeRCNN, Nat. Mach. Intell. 5 (2023) 765, doi:10.1038/s42256-023-00685-7
- [S12] Richter-Powell, Lipman, Chen, Neural conservation laws, NeurIPS 2022 — https://arxiv.org/abs/2210.01741
- [S13] Li et al., Fourier neural operator — https://arxiv.org/abs/2010.08895
- [S14] Lu et al., DeepONet, Nat. Mach. Intell. (2021) — https://arxiv.org/abs/1910.03193
- [S15] Li et al., GINO, NeurIPS 2023 — https://arxiv.org/abs/2309.00583
- [S16] Pfaff et al., MeshGraphNets, ICLR 2021 — https://arxiv.org/abs/2010.03409
- [S17] Obiols-Sales et al., CFDNet, ICS '20 — https://arxiv.org/abs/2005.04485 ; Fuchi et al. (2025), CNN initialisation for an airfoil — https://arxiv.org/abs/2501.14699
- [S18] Kochkov et al., PNAS 118 (2021) e2101784118 — https://arxiv.org/abs/2102.01010
- [S19] Parish, Duraisamy, JCP 305 (2016) 758, doi:10.1016/j.jcp.2015.11.012
- [S20] Duraisamy, Iaccarino, Xiao, Annu. Rev. Fluid Mech. 51 (2019), doi:10.1146/annurev-fluid-010518-040547 ; Duraisamy, perspectives on ML-augmented RANS — https://arxiv.org/abs/2009.10675
- [S21] Wu et al., JFM (2019), conditioning of RANS with data-driven stresses — https://arxiv.org/abs/1803.05581
- [S22] Xiao et al., JCP 324 (2016) 115 — https://arxiv.org/abs/1508.06315
- [S23] Michelén Ströfer, Zhang, Xiao, DAFI, CiCP 29(5) (2021) — https://arxiv.org/abs/2012.02651 ; code — https://github.com/xiaoh/DAFI
- [S24] DAFoam licence — https://github.com/mdolab/dafoam/blob/main/LICENSE.md
- [S25] CONES, OpenFOAM with online EnKF — https://arxiv.org/abs/2310.18016
- [S26] Zauner et al., nudging URANS toward data — https://arxiv.org/abs/2110.10651
- [S27] Hesthaven, Ubbiali, POD-NN, JCP 363 (2018) 55, doi:10.1016/j.jcp.2018.02.037
- [S28] Guo, Hesthaven, POD-GPR, CMAME 341 (2018) 807, doi:10.1016/j.cma.2018.07.017
- [S29] Binois, Wycoff, high-dimensional Bayesian optimisation survey, ACM TELO (2022) — https://arxiv.org/abs/2111.05040
- [S30] Lakshminarayanan et al., deep ensembles, NeurIPS 2017 — https://arxiv.org/abs/1612.01474
- [S31] Ling, Kurzawski, Templeton, TBNN, JFM 807 (2016) 155, doi:10.1017/jfm.2016.615
- [S32] Li et al., Geo-FNO, JMLR 24(388) (2023) — https://jmlr.org/papers/v24/23-0064.html

**App robustness and CI**
- [R1] GitHub Actions billing — https://docs.github.com/en/billing/concepts/product-billing/github-actions
- [R2] GitHub runner images (labels) — https://github.com/actions/runner-images
- [R3] GitHub-hosted runners reference — https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- [R4] Workflow syntax (`timeout-minutes`, `concurrency`) — https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions
- [R5] pytest-qt troubleshooting — https://pytest-qt.readthedocs.io/en/latest/troubleshooting.html ; QWebEngine offscreen report — https://forum.qt.io/topic/149893/docker-qt6-xcb-issue-running-pytest
- [R6] pyvista/setup-headless-display-action — https://github.com/pyvista/setup-headless-display-action
- [R7] VTK discourse, run-time OpenGL render window selection — https://discourse.vtk.org/t/status-update-runtime-opengl-render-window-selection-in-vtk/14583
- [R8] uv, GitHub integration — https://docs.astral.sh/uv/guides/integration/github/
- [R9] windows-2025 runner image readme (WSL 2) — https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md
- [R10] GitHub Actions secure use (self-hosted runners) — https://docs.github.com/en/actions/reference/security/secure-use
- [R11] Canonical blog, testing Ubuntu on WSL (runner session) — https://ubuntu.com/blog/improved-testing-ubuntu-wsl
- [R12] pytest-timeout — https://pypi.org/project/pytest-timeout/
- [R13] Python faulthandler — https://docs.python.org/3/library/faulthandler.html ; pytest `faulthandler_timeout` — https://docs.pytest.org/en/stable/how-to/failures.html
- [R14] pytest-xdist, crashed workers — https://pytest-xdist.readthedocs.io/en/stable/crash.html
- [R15] pytest-forked (no Windows) — https://pypi.org/project/pytest-forked/
- [R16] Qt 6, threads and QObjects — https://doc.qt.io/qt-6/threads-qobject.html
- [R17] Qt 6, QThread — https://doc.qt.io/qt-6/qthread.html
- [R18] VTK discourse, rendering from another thread — https://discourse.vtk.org/t/how-to-render-vtk-objects-in-different-thread/12641
- [R19] pyvistaqt API reference — http://qt.pyvista.org/api_reference.html
- [R20] Microsoft, collecting user-mode dumps (WER LocalDumps) — https://learn.microsoft.com/en-us/windows/win32/wer/collecting-user-mode-dumps
- [R21] Sysinternals ProcDump — https://learn.microsoft.com/en-us/sysinternals/downloads/procdump
- [R22] WinDbg `!analyze` — https://learn.microsoft.com/en-us/windows-hardware/drivers/debuggercmds/-analyze
- [R23] Qt 6, debugging techniques — https://doc.qt.io/qt-6/debug.html
- [R24] Hypothesis, stateful testing — https://hypothesis.readthedocs.io/en/latest/stateful.html ; pytest-subprocess — https://pytest-subprocess.readthedocs.io/en/latest/usage.html
- [R25] pytest-pyvista — https://github.com/pyvista/pytest-pyvista
- [R26] import-linter — https://import-linter.readthedocs.io/en/stable/
- [R27] PyInstaller, when things go wrong — https://pyinstaller.org/en/stable/when-things-go-wrong.html
- [R28] Sentry, Crashpad and data privacy — https://docs.sentry.io/platforms/native/guides/crashpad/ ; https://docs.sentry.io/platforms/python/configuration/options/
- [R29] pytest-qt, modal dialogs — https://pytest-qt.readthedocs.io/en/latest/note_dialogs.html
