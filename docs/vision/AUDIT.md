# AetherCFD Studio: audit against the original vision

*Audited 2026-10-11 on branch `feat/guided-workflow` (main at `6cd0b0b` plus this branch). The yardstick is
[original_spec.md](original_spec.md).*

**Method.** Every line below was checked against the code (module, function, test), not against the README.
The guided workflow was also driven end to end on a fresh pitzDaily copy, with real `blockMesh` /
`checkMesh` / `simpleFoam` runs in WSL through the app's run manager. Status:

* **Done**: works end to end in the app.
* **Partial**: a real but limited subset exists.
* **Missing**: there is no code for it.

Items new in this branch are marked *(new)*.

## Top findings

1. **The core promise holds.** Every change is an approvable, journalled, revertable plan, and the case on
   disk stays the source of truth. The command layer, the lossless dictionary editing and the WSL run
   manager (guards, staging, provenance) are solid.
2. **The guided workflow is now real** *(new)*. Before this branch the Workflow panel was a read-only list
   of 12 stages: one check (BC coverage) and no guided mode. It is now a staged workspace:
   * per-stage status, a next action and dependencies;
   * 20+ correctness gates (dimensions, pressure reference, U/p pairing, solver/scheme coverage,
     checkMesh, y+, species sum, Mach vs solver, run outcome, convergence, telemetry health);
   * physical questions that write the dictionaries as one `dict.batch` plan;
   * Advanced and Raw modes;
   * "Mark done" refused while any error gate fails;
   * copilot tools for the same path.
3. **The biggest gaps for a beginner doing a real case correctly:**
   * the grid-convergence study is new and covers blockMesh cases only (snappy through its background mesh);
   * integral-quantity convergence (forces, flow rates, mass balance; heat balance in closed boxes) is new in
     feat/qoi-postprocessing, with a run report;
   * y+ is a gate, not a field you can see;
   * no "why is this happening?" from a run anomaly;
   * templates miss premixed flames, nozzles and rotating machinery;
   * provenance is written but can never be replayed or branched.
4. **The far-horizon items are honestly absent:** native GPU solver, LBM, DSMC/hybrid kinetic, CUDA/GL
   interop, operator learning, online learning, the Physics DAG as a graph view, the repository indexer.
   Nothing pretends to provide them, with three exceptions where the UI hints at features that do not
   exist (palette keywords `lambda2` and `path`, and `--sweep`, which is a GUI test rather than a
   parameter sweep).
5. **Sharing is much better since PR #1** (merged here): a first-run setup wizard, a `mechanism_roots`
   setting, no hard-coded plot-style/ParaView paths, and a Codex provider next to Claude. What remains:
   * the status-bar disk check is still fixed to `D:\` (`studio/shell/main_window.py`);
   * the cfd_agents adapter falls back to the group's `D:\` locations. They are only used if they exist;
     otherwise the app explains and names the `cfd_agents_python` / `cfd_agents_root` settings. The setup
     wizard does not ask for them yet.
6. **Bugs found and fixed in this branch:**
   * The Checks panel's multi-edit fixes conflicted whenever two edits touched one file (e.g. pMinFactor +
     pMaxFactor). Each `dict.set` was planned against the original text, so the second one always failed
     the conflict check. It now uses the new single-plan `dict.batch`.
   * Regex keys such as `"(k|epsilon|omega)"` could not be addressed for editing at all
     (`foamdict.semantic.DictView.find_in`).
   * Integration finding: Murphy's verdict depends on the success/converged flags its caller passes.
     Called naively (both false) it labels a healthy, converged log "no_convergence", so the adapter derives
     both flags from the log with AetherCFD's parser.

## 1. Staged workspace

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Geometry → … → ML stages | **Done** *(new)* | `case/guided.py` STAGE_DEFS (12 stages, dependencies); `studio/panels/workflow/panel.py`; `tests/unit/test_guided_workflow.py`, `tests/studio/test_workflow_panel.py` | Dependencies are between stages, not between OpenFOAM objects |
| Completion state, warnings, dependencies, next action | **Done** *(new)* | `guided.assess`: done / ready / attention / error / blocked / changed / na; the next action names the first failing gate and its fix; a done mark is invalidated when the stage's files change (fingerprint) | — |
| Guided mode asks physical questions | **Done** *(new)* | ~22 questions, for example: inlet speed (scales k/ε/ω), intensity and length scale (defaults reproduce the tutorial's k=0.375, ε=14.855), fluid/ν, U/L/T giving Ma and Re, turbulence model (creates missing fields and solver entries), wall treatment, scheme profile, maxCo, endTime, residual target, monitoring function objects | Compressible/thermo and chemistry questions only point to the Combustion panel and New Case |
| Advanced mode | **Done** | Per-stage buttons open AetherMesh, Checks (numerics lab), Combustion, Mesh Quality, Run, Residuals, Measurements, Analysis | — |
| Raw mode | **Done** | Stage files open in the editor in raw text | Python scripts, C++ and CUDA are not listed per stage (C++ lives in the Physics Code panel) |
| Modes stay in sync | **Done** | All three modes edit the same files; answers are re-read from disk on every refresh; typed but unapplied answers survive a refresh | — |
| Git diffs in raw mode | **Partial** | Diff panel (two files, other case, before last journalled change) | No git diff or status view |

## 2. Interface and viewport

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Dark, dense scientific UI | **Done** | `studio/theme/tokens.py`, `style.py` | A light theme is in progress on its own branch |
| Large 2D/3D viewport, generic fields (p, T, ρ, U, any volField) | **Done** | `studio/panels/viewport/panel.py`, `results/foam_reader.py` | — |
| Q-criterion, iso-surfaces, slices, clips, thresholds, contours, streamlines, glyphs, schlieren | **Done** | `results/pipeline.py` stages, GPU iso in `results/iso.py`; `tests/unit/test_results_pipeline.py`, `test_iso_gpu.py` | — |
| Mach number, λ2, vorticity, gradients | **Partial / Missing** | `iso.vorticity()` exists but is not wired to any stage; schlieren uses grad(ρ) | No derived Mach or λ2 field; the palette keyword `lambda2` leads to Q-criterion |
| Species, reaction progress, heat release, mixture fraction | **Partial** | Shown only if the solver wrote them; colour-map rules in `viewport/look.py` | No derived Z, c or HRR on the mesh |
| Particles (Lagrangian), pathlines | **Missing** | — | Clouds are not read; the palette keyword `path` leads to steady streamlines |
| Probes | **Partial** | "Probe cell here" (`viewport/panel.py`) | No probe lines or monitors |
| Mesh, mesh-quality maps, bad-cell locations | **Done** | quality stage (`results/pipeline.py`), `mesh/quality.py` hotspots | — |
| y+, AMR levels, geometry overlay in the viewport | **Missing** | y+ is a gate *(new)*, not a field map | — |
| CUDA/OpenGL(/Vulkan/D3D12) interop | **Missing** | CuPy computes, results are copied to VTK | Far horizon |
| Live visualisation during a run | **Partial** | "Follow" refreshes when a time folder appears | Never updates for decomposed runs (`viewport/panel.py` `_on_files_changed` checks only top-level time folders) |
| ParaView / VTK export | **Partial** | "Open in ParaView", pvbatch ray-traced renders | No VTK/foamToVTK export |

## 3. Navigator and global command bar

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Left workflow navigator with state, warnings, dependencies, next actions | **Done** *(new)* | Workflow panel | — |
| Global command/search bar | **Partial** | `studio/palette/command_palette.py`: actions, panels, viz stages, templates, perspectives, `#` case files, `@` dictionary keywords, typo-tolerant, fallback "Ask the copilot" | No intent actions ("add k-omega SST" goes to the copilot, which can now do it through `workflow_answer`); no models, solvers, docs, source code, mechanisms or tutorial index; a template hit opens the wizard without preselecting that template |

## 4. Simulation Copilot

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Understands case, mesh, BCs, models, residuals, files | **Done** | ~60 tools: `copilot/tools.py`, `mesh_tools.py`, `analysis_tools.py`, `workflow_tools.py` *(new)* | Equations are known only through the solver name |
| Understands hardware | **Missing** | Hardware exists only in run provenance | — |
| Explain, propose defaults, modify files, run checks, run scripts, inspect logs, compare | **Done** | `dict_*`, `dict_batch` *(new)*, `workflow_*` *(new)*, `case_checks`, `run_python`, `log_tail`, `solver_progress`, `diff_files`, `app_compare_fields` | — |
| Generate meshes | **Done** | `snappy_setup`, `grading_design`, `hole_box_design`, `run_openfoam` | — |
| Search local repositories | **Missing** | — | — |
| Configure sweeps | **Missing** | — | No parameter-sweep engine (`--sweep` is a GUI robustness test) |
| Train ML models | **Partial** | `pinn_train`, `surrogate_runs` (GP) | — |
| Divergence remedies | **Partial** | `solver_progress`, DMD growth modes, Murphy diagnosis *(new)* | — |
| Never silent; rationale, consequence, confidence, files shown | **Done** | `CommandPlan` and the executor refuse agent plans without a rationale and confidence; the approval dialog shows diff, risk and consequences; `tests/unit/test_commands.py` | Consequences come from the command code, not from the model's own prediction |
| Works with other AI providers | **Done** (PR #1) | `copilot/providers.py`: Claude Code and OpenAI Codex (`codex_cli.py`, `codex_session.py`) behind one provider interface; setting `copilot_provider` | — |

## 5. Physics Graph / Simulation DAG

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Visible dependency graph of geometry, materials, equations, models, BCs, numerics, data, ML | **Partial** *(new)* | Stage-level dependencies in `guided.STAGE_DEFS`, shown as text ("Depends on: …") | No object-level graph and no graph view; the guided gates are the natural data source for one |

## 6. Backends

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Standard OpenFOAM (ESI and Foundation) | **Done** | `backend/wsl/*`; fork chosen explicitly per case (`project.case_config().fork`, `case/fork_detect.py`) | No foam-extend |
| Custom OpenFOAM solvers and libraries | **Done** | `codegen/` templates + wmake build, Physics Code panel; `tests/wsl/test_codegen_live.py` | Template-based |
| Python numerical tools | **Done** | Cantera, GP, POD/DMD | — |
| C++ extensions, custom CUDA kernels | **Missing** | CuPy is used as a NumPy drop-in; no RawKernel | — |
| Native GPU compressible solver, LBM, Boltzmann/DSMC, hybrid continuum-kinetic with Knudsen switching | **Missing** | — | Far horizon; no Knudsen-number check either |

## 7. CFD Measurement Integrated (CFDMI)

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Map measurements into the domain | **Partial** | `assim/measurements.py` (CSV x,y,z,field[,std]; nearest cells) | Generic CSV only: no PIV/thermocouple/hot-wire/schlieren handling |
| Assimilation methods | **Partial** | Static-covariance optimal interpolation (a 3D-Var analysis step) | No nudging, source terms, inverse modelling, 4D-Var, EnKF or learned corrections |
| Measured vs simulated vs correction vs uncertainty vs assimilated | **Done** | Writes `<f>Assim`, `<f>AssimCorr`, `<f>AssimStd`; scatter plot in the Measurements panel | — |
| Auditable, dimensionally/physically constrained corrections | **Partial** | Journalled as field writes | No positivity, bounds or conservation constraints |

## 8. PINN / scientific ML

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| PINNs on governing equations and sparse data | **Partial** | `ml/pinn_worker.py`: steady incompressible NS or advection-diffusion, plus data, out of process on the GPU; `tests/unit/test_pinn.py` | No BC loss terms; no energy/species/compressible equations |
| Surrogates, ROM | **Partial** | GP surrogate over runs; POD/DMD analysis | The ROM does not predict |
| Operator learning, neural closures, turbulence/combustion surrogates, inverse estimation, super-resolution | **Missing** | — | — |
| Field reconstruction | **Done** | `<f>Pinn` / `<f>PinnErr` fields, compared against the CFD field | — |
| Active learning | **Partial** | GP proposes the next run | It never launches it |
| User-chosen loss terms, constraints | **Partial** | data/pde weights in the worker | Not in the UI; no positivity or realizability terms |
| Diagnostics, uncertainty, dataset provenance | **Partial** | Loss curve, RMSE/σ vs CFD | No held-out set, no PINN uncertainty, no dataset/model hash |
| Online learning during a run; promotion gates | **Missing** | — | — |

## 9. Combustion and repositories

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Cantera calculations and model advice | **Done** | `combustion/calc.py` (Z_st, T_ad, flame speed, ignition delay, flamelets, regime), `advisor.py`; tests | — |
| Mechanisms | **Done** | GRI, h2o2, built-ins plus the folders in the `mechanism_roots` setting (PR #1) | — |
| Repository browser and indexer; adapter-writing agent | **Missing** | — | — |

## 10. Geometry

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Import STEP/IGES/BREP/FCStd (FreeCAD headless), STL/OBJ | **Done** | `mesh/cad.py`, `mesh/geometry.py`; `tests/unit/test_cad.py` | — |
| Embed or launch FreeCAD; parameterised geometry; Booleans; moving parts | **Missing** | — | — |
| Named faces/regions → patches | **Partial** | Named CAD parts become STL solids, then patches | — |
| Push geometry changes into meshing | **Partial** | CAD → STL → snappy setup | Manual steps |
| Python scripting first-class | **Done** | `scripting/api.py` Session, CLI, console | — |

## 11. Meshing

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| blockMesh grading, O-grid hole blocking | **Done** | `mesh/grading.py`, `mesh/blocking.py`; tests | — |
| snappyHexMesh (features, curvature levels, layers) | **Done** | `mesh/snappy.py`; `tests/wsl/test_snappy_live.py` | — |
| Refinement boxes, AMR/dynamic refinement, overset, moving meshes, morphing, cfMesh/Gmsh | **Missing** | (Darwin plans refinement; see 19) | — |
| Quality metrics in the viewport, bad-cell clusters | **Done** | quality stage, hotspots | — |
| y+ and BL resolution | **Partial** *(new)* | `case/gates.py` `yplus_gate`: a-priori estimate (first-cell distance × u_τ/ν) or the yPlus field after a run; checked against the nut wall treatment | Not shown as a viewport map |
| Explain why a mesh is inadequate and suggest corrections | **Partial** | checkMesh advice text; the mesh gates block "done" and say how to fix | Advice is generic, not specific edits |

## 12. Numerics laboratory

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Rules on schemes, p-U coupling, relaxation, non-orthogonal correction, I/O | **Done** | `case/checks.py` (17 finding ids) with one-click journalled fixes | — |
| Solver and scheme coverage for every solved field | **Done** *(new)* | `gates.coverage_gate` (with fix edits) | — |
| Linear solvers/preconditioners/tolerances, gradient limiters, flux schemes, shock capturing, positivity, stiffness | **Partial / Missing** | — | Only presence is checked, not suitability |
| Stability/accuracy trade-off display | **Missing** | The Numerics guided question offers robust vs balanced with an explanation | No quantitative display |
| Incompatible/suspicious combinations | **Partial** | Central schemes vs boundedness, PISO mode, laplacian limiter vs mesh | — |

## 13. Run dashboard

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Physical time, wall time, Δt, Co max, continuity, residuals, ETA | **Done** | `studio/panels/run/panels.py`, `runs/logparse.py` | ETA ignores startTime, so it is wrong after a restart |
| CFL distribution, forces/moments, mass/energy/species balance, min/max of fields, mesh motion, chemistry stiffness | **Missing / Partial** | Min/max are parsed (copilot only) | — |
| GPU/CPU utilisation, VRAM, disk throughput, checkpoint status | **Missing / Partial** | RAM and free disk in the status bar (the disk check is still fixed to `D:\`) | — |
| Click an anomaly and ask "why?"; highlight cells/log lines | **Partial** | Solver Log jumps to the first error; the copilot's "Why did it fail?" button; Murphy *(new)* in the Run stage | No anomaly → question link from the plots |

## 14. Verification and validation

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Pre-solve: dimensions, BC consistency, mesh quality, wall resolution, species sum, thermo range, solver/model compatibility | **Done** *(new, plus earlier checks)* | `gates.py`: dimensions, bc-coverage, p-reference, u-p-pairing, wall-functions, coverage, mesh-quality, yplus, species-sum, mach, steady; `checks.py` thermo-range, turb-fields | — |
| Pre-solve: conservation, turbulence-model applicability | **Partial** | Re regime vs model warning *(new)* | — |
| Post-solve: residual convergence and telemetry health | **Done** *(new)* | `convergence_gate`, `health_gate` (fieldMinMax NaN/negative k), Connie | — |
| Post-solve: grid convergence, Richardson extrapolation, GCI | **Partial** *(new, follow-up branch)* | `case/gridstudy.py`: coarse/fine levels in `grid_study/` (blockMeshDict counts × 1/r and × r, fine endTime × r^d); Celik et al. (2008) observed order, φ_ext, GCI, asymptotic check; Validation gate with a tolerance question, "inconclusive" when a level is not iteratively converged (judged on the quantity's change over the last writes against the differences between meshes), and an explanation when y+ leaves the log layer with wall functions; copilot `grid_study_*` tools; `tests/unit/test_grid_study.py` | blockMesh cases only; quantities = patch means of p/T and postProcessing .dat values (no reattachment length or profiles yet) |
| Post-solve: integral convergence, conservation audit | **Done** *(feat/qoi-postprocessing)* | `case/qoi.py`: `convergence_gate` (steady: band over the last 10 % of iterations; transient: drift of the mean between the last two quarters; against the largest value of the same kind), `conservation_gates` (Σ phi over every open patch; Σ wall heat flow in a closed steady box); `tests/unit/test_qoi.py`, `tests/wsl/test_qoi_live.py` | Energy balance for open domains; multi-region |
| Post-solve: uncertainty propagation, sensitivity, benchmark comparison | **Missing** | — | — |
| Experimental comparison | **Done** | Measurements (bias, RMSE, R²) | — |

## 15. Python-native post-processing

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Python console with the case as numpy | **Done** | `scripting/console.py`, Python Console panel | — |
| "Open as Python" on any result; push results back to the viewport | **Missing / Partial** | Only the copilot can put code into the console | No UI action; no `add_field` / show-in-viewport API |

## 16. Experiment and data layer

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| CSV with uncertainty columns, length units | **Done** | `assim/measurements.py` | — |
| HDF5, NetCDF, TDMS, MAT, VTK, images, probe logs, PIV | **Missing** | — | — |
| Units, coordinate systems, calibration, timestamps, provenance; automatic registration | **Missing** | Only an "outside the mesh" warning | — |

## 17. Templates and onboarding

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Canonical templates | **Partial** | 19 tutorials (`templates/catalog.py`). Covered: incompressible internal and external, shock tube, CHT, multiphase, non-premixed flames, spray, particles, transient thermal | Missing: premixed flame, compressible nozzle, rotating machinery (MRF), hypersonic |
| Guided through assumptions and applicability before values | **Partial** *(improved)* | The wizard ranks templates against physics answers; the guided workflow then checks Mach, Re, steady/transient, y+ against the case | No explicit assumptions page in the wizard |

## 18. Transparency and reproducibility

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Never hide the case; tree, editor, terminal | **Done** | Case Files, Editor (raw/form/split, lossless), Terminal (ConPTY + WSL, one fork sourced) | — |
| Every GUI action maps to files and commands | **Done** | Command layer + journal (now including guided answers and "mark done") | — |
| Git status, ML datasets, checkpoints, external dependencies in the tree | **Missing** | `.git` is hidden; nothing shows git status | — |
| Run provenance: fork/version, solver binary hash, git head + diff, OS/CPU/GPU, env (WM_/FOAM_/MPI), input and mesh hashes, expanded dictionaries | **Done** | `runs/provenance.py` (provenance.json per run) | — |
| Compiler, CUDA version, Python libraries, random seeds, ML model hash, dataset provenance, CAD hash | **Missing** | — | — |
| Reproduce or branch any prior run | **Missing** | Nothing reads provenance.json back | Replay/branch from a record is the obvious next step |

## 19. Added later by the owner

| Requirement | Status | Evidence | Gap |
|---|---|---|---|
| Integrate the group's cfd_agents | **Partial** *(new)* | `services/cfd_agents` runs **Murphy** (log diagnosis), **Connie** (run health from fieldMinMax/solverInfo), **Reggie** (transient numerics, mapped to approvable edits) and **Darwin** (RANS refinement plan, written outside the case) out of process (`python -I -B _runner.py`, JSON). Missing config or deps are detected and explained. Shown in the Mesh / Numerics / Run / Validation stages and as copilot tools; tested with a fake package (`tests/unit/test_cfd_agents_adapter.py`) and live on this machine | **Betty, Bob** write the case outside approval; **Bill** always runs blockMesh and deletes time folders; **Larry** needs local Ollama. These four are listed with the reason, not called |
| Guided, step-by-step workflow, done correctly | **Done** *(new)* | See 1 and 14 | See the roadmap |
| Robust and easy for beginners and AI agents | **Partial** | The guided path plus gates; copilot `workflow_*` tools drive the same path | Help texts are short; no tutorial walkthrough mode |
| Shareable with other students | **Partial** | Packaged exe, per-user settings | Hard-coded `D:\` defaults (mechanisms, plot style, pvbatch, disk check, case roots, cfd_agents); WSL distro name assumed |
| Works with other AI providers | **Done** (PR #1) | Claude Code and Codex providers | — |

## Test-suite status at audit time

* `pytest tests/unit tests/studio` on main: **294 passed, 2 failed, 1 error, 3 skipped**.
  * `test_run_scripts::test_existing_step_log_is_kept_not_overwritten` is a "unit" test that calls WSL.
    It fails whenever WSL prints a systemd warning.
  * `test_editor_sync::test_typing_updates_document_and_undo_restores` passes on its own (flaky).
  * `test_postpro_view` errors.
  * The PINN tests skip in a worktree because the `.ml-env` is looked up next to the checkout.
* On this branch: **334 passed, 1 failed, 3 skipped**. The failure is the same WSL-dependent
  `test_run_scripts` test; it runs `C:\Windows\system32\bash.exe`, i.e. the default distro.
* `python -m aethercfd.studio gui_run_bfs --sweep`: 88/88 steps pass, including two new Workflow steps.
* A fresh pitzDaily (ESI v2306) was walked through every stage with real runs: blockMesh + checkMesh, then
  simpleFoam with the guided monitoring function objects. The walk reached every applicable stage "done",
  with one honest warning: kEpsilon wall functions at a mean y+ of 9–15, in the buffer layer.
* This branch adds 37 tests (guided workflow, gates, `dict.batch`, copilot workflow tools, the cfd_agents
  adapter against a fake package in a real subprocess, the Workflow panel).

## Roadmap: what makes the most difference for a beginner doing a real case correctly

Ordered by value per effort. "Beginner-critical" items stop wrong answers; the rest make the app nicer or
reach the long-term vision.

1. **Grid-convergence study (beginner-critical): started.** A three-level study with Richardson/GCI and a
   Validation gate exists for blockMesh cases. Next: refineMesh-based levels for imported and snappy
   meshes; richer quantities (reattachment length, profiles at stations, forces); a plot of φ against h.
2. **Integral-quantity monitors (beginner-critical): done** (branch `feat/qoi-postprocessing`). The guided Run
   stage adds forces on the walls, flow rate and mean p (and T) per open patch and wall heat flow
   (`case/qoi.py`, fork-aware syntax checked live in ESI v2306 and Foundation 14). The Validation stage judges
   convergence on them and audits the mass balance (and the heat balance of a closed steady box). The Post
   stage writes a run report (`results/report.py`: Markdown + PDF/PNG plots + ParaView field pictures +
   report.json). Still open: forceCoeffs with reference values asked in the guided flow, the run dashboard
   showing the QoIs live, energy balance for open domains (enthalpy flux at inlets and outlets), multi-region
   cases.
3. **y+ and other gate results as viewport maps.** Make the gates visible on the mesh: y+ per wall face,
   cells over a non-orthogonality limit, species sum ≠ 1. Add a "show me" button on each gate.
4. **More guided physics.**
   * Compressible: p in Pa, thermo model, T, alphat, generated consistently.
   * Transient: time step from Co and the mesh, write control.
   * Multi-region and combustion questions that hand off to the Combustion panel's verified dictionaries.
   * Explicit assumption/applicability page in the New Case wizard.
5. **Run anomalies → "why?"** Right-click a residual spike or a log line to send that window to the
   copilot / Murphy with the evidence. Highlight the time step and log lines.
6. **Reproduce / branch a run from provenance.json.** Copy the inputs as recorded into a sibling case and
   re-run with the same fork. Also record compiler, Python packages, seeds and ML model/dataset hashes.
7. **Shareability.**
   * Check free disk on the case's drive and the WSL VHD, not `D:\`.
   * Add the cfd_agents interpreter/root to the first-run setup wizard (PR #1 covers distro, case roots,
     providers and mechanisms).
   * Rebuild the packaged app: the spec now ships the cfd_agents `_runner.py` (like `pinn_worker.py`), but the
     installed exe predates this branch.
8. **Templates.** Premixed flame (e.g. a laminar flame tube), compressible nozzle, MRF mixer, plus a
   verification case per template with a known answer (benchmark comparison).
9. **Physics graph view.** Draw the stage DAG with object-level nodes (fields, models, schemes)
   annotated with gate results. Today's gates already know most of the edges.
10. **Data layer and CFDMI depth.** HDF5/MAT/PIV import with units and coordinate registration;
    constrained corrections (positivity, bounds); EnKF / nudging as fvOptions source terms.
11. **ML depth.** BC and constraint loss terms, held-out validation and dataset/model hashes; then
    operator learning and online learning with explicit promotion gates.
12. **Far horizon, honestly.** These are research projects, not features. Each needs its own design and
    validation campaign before it belongs in a beginner's workflow:
    * a native GPU compressible solver;
    * LBM and kinetic/DSMC backends with Knudsen-number switching;
    * CUDA/GL interop for zero-copy rendering.
