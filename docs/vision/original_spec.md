# AetherCFD Studio: original vision (from the project owner)

This is the product vision the app is measured against. The implementation plan and roadmap are in
docs/adr and the M1 plan; this file is the owner's own statement of what the app should become.

## Staged workspace
The core UX is a staged simulation workspace. A project moves through Geometry → Domain → Materials → Physics → Mesh → Models → Boundary/Initial Conditions → Numerics → Run → Validation → Post-processing → ML/Data Assimilation. Every stage has a simple guided mode, an advanced mode, and a raw-code mode. Guided mode asks physically meaningful questions instead of exposing cryptic dictionary names; advanced mode shows all relevant OpenFOAM objects and numerical settings; raw-code mode exposes the exact dictionaries, Python scripts, C++ extensions, CUDA kernels, configuration files, and Git diffs. Changes made in any mode remain synchronized with the others.

## Interface and viewport
Use a dark, dense, professional scientific interface. The center of the application is a large real-time 2D/3D flow viewport capable of pressure, temperature, density, velocity, Mach number, species, vorticity, Q-criterion, lambda-2, turbulence quantities, particles, reaction progress variables, heat release, mixture fraction, gradients, schlieren-like density-gradient rendering, streamlines, pathlines, iso-surfaces, slices, probes, meshes, AMR levels, and geometry overlays. Visualization should support CUDA/OpenGL/Vulkan or CUDA/D3D12 interoperability so simulation fields can be rendered without unnecessary CPU copies. ParaView/VTK export remains available, but the application should have its own live visualization path.

A left-side workflow navigator shows simulation stages, completion state, warnings, dependencies, and required next actions. A global command/search bar lets the user type things such as "add k-omega SST," "use reactingFoam," "find hydrogen mechanisms," "add a pressure outlet," "refine mesh near shock," "use a repository I cloned," "compare SIMPLE vs PIMPLE," or "train a surrogate on these completed cases." Search results should resolve to models, solvers, OpenFOAM dictionaries, documentation, local source code, Python modules, chemistry mechanisms, example cases, templates, or AI-assisted actions.

## Simulation Copilot
A right-side Simulation Copilot acts as an agentic engineering assistant. It understands the current geometry, mesh, equations, boundary conditions, models, residual history, hardware, and case files. It can explain settings, propose physically appropriate defaults, create or modify case files, run validation checks, launch diagnostic scripts, inspect logs, compare results, search local repositories, generate meshes, configure sweeps, train ML models, and suggest remedies for divergence. It must never silently make consequential modeling decisions: every recommendation should show the physical rationale, expected consequence, confidence, and files/settings that will change.

## Physics Graph
Include a visible Physics Graph / Simulation DAG showing how geometry, material properties, governing equations, constitutive models, turbulence, chemistry, multiphase models, particles, radiation, discretization, linear solvers, boundary conditions, experimental data, ML components, and outputs depend on one another. This graph should make complex multiphysics setups understandable instead of reducing them to a giant form.

## Backends
Support multiple computational backends inside one project: standard OpenFOAM finite-volume solvers; custom OpenFOAM solvers and libraries; Python-based numerical tools; C++ extensions; custom CUDA kernels; a native GPU compressible-flow solver; Lattice Boltzmann methods; kinetic/Boltzmann solvers where appropriate; and hybrid continuum–kinetic workflows in which Navier–Stokes is used in continuum regions and Boltzmann/LBM/DSMC-like methods are activated where Knudsen number or rarefaction criteria demand them. Backend choice should be explicit, modular, and physically justified rather than presented as interchangeable magic.

## CFD Measurement Integrated (CFDMI)
Experimental data from PIV, pressure taps, thermocouples, hot-wire measurements, emissions analyzers, particle instruments, schlieren-derived fields, or other sensors can be mapped into the computational domain. The user can assimilate those measurements through source terms, nudging, state estimation, inverse modeling, variational methods, Kalman-style methods, or learned corrective terms. The UI must distinguish measured data, simulated state, correction field, uncertainty, and final assimilated solution. Every correction should be auditable and dimensionally/physically constrained.

## PINN / scientific ML
Users can train PINNs on governing equations, boundary conditions, sparse experimental data, completed CFD solutions, or combinations of these. Support surrogate models, operator learning, reduced-order models, neural closures, turbulence corrections, combustion surrogates, inverse parameter estimation, super-resolution, field reconstruction, and active learning. The application should allow the user to choose which conservation laws, residuals, constitutive relations, positivity constraints, realizability conditions, dimensional scalings, and boundary conditions enter the loss function. ML models must have training/validation diagnostics, uncertainty indicators, dataset provenance, and the ability to compare predictions directly against baseline CFD.

Allow online learning during simulations. While a CFD run is progressing, selected snapshots and residual information can be streamed into a training pipeline. The system can train a surrogate, closure correction, or reduced-order model in parallel on spare CPU/GPU resources, then evaluate it without automatically replacing the governing solver. Promotion of a learned model into an active simulation must require validation gates and explicit user approval.

## Combustion and repositories
Provide deep combustion and reacting-flow integration. The application should discover and index local cloned repositories for combustion chemistry, turbulent-chemistry interaction, soot and particle formation, aerosol models, radiation, spray/lagrangian particle tracking, thermophysical property libraries, Cantera mechanisms, pyJac-generated chemistry, OpenSMOKE-like tools, Mutation++-style thermochemistry, user-developed CRNs, and custom OpenFOAM libraries. A repository browser should inspect APIs, build instructions, supported species/models, dependencies, tests, and examples, and an agent should help create adapters rather than copying code blindly.

## Geometry
Geometry should integrate tightly with FreeCAD. Users can import STEP/IGES/STL/OBJ, launch or embed FreeCAD workflows, parameterize geometry, create named faces and regions, perform Boolean operations, define rotating or moving parts, and push geometry changes directly into the meshing stage. Python scripting should be first-class so geometry construction, case generation, sweeps, post-processing, and optimization can all be automated.

## Meshing
The meshing environment must support blockMesh, snappyHexMesh, cfMesh/Gmsh-style workflows where available, surface cleanup, boundary-layer controls, local refinement boxes, feature edges, curvature refinement, AMR, dynamic refinement, overset meshes, moving meshes, and mesh morphing. Display skewness, non-orthogonality, aspect ratio, cell volume, y+, boundary-layer resolution, refinement levels, cut cells, and bad-cell locations directly in the viewport. The agent should explain why a mesh is inadequate and suggest specific corrections, not merely flag it red.

## Numerics laboratory
Provide a numerics laboratory rather than a single "solver settings" screen. Users can inspect and configure temporal discretization, spatial reconstruction, gradients, limiters, flux schemes, pressure–velocity coupling, relaxation, linear solvers, preconditioners, tolerances, CFL limits, adaptive time stepping, shock capturing, positivity preservation, under-relaxation, species coupling, stiffness treatment, and chemistry integration. Show stability/accuracy tradeoffs and automatically detect incompatible or suspicious combinations.

## Run dashboard
The run dashboard should feel like mission control. Show physical time, wall time, iteration count, timestep, CFL distribution, continuity error, equation residuals, forces/moments, mass imbalance, energy imbalance, species conservation, min/max thermodynamic values, mesh-motion quality, chemistry stiffness, GPU/CPU utilization, VRAM/RAM, disk throughput, checkpoint status, and estimated completion. Users should be able to click any anomaly and ask the agent "why is this happening?" The corresponding cells, boundaries, equations, and log lines should highlight.

## Verification and validation
Before solving, run dimensional checks, boundary-condition consistency checks, mesh-quality checks, conservation checks, turbulence-model applicability checks, wall-resolution checks, species-sum checks, thermodynamic-range checks, and solver/model compatibility checks. After solving, support grid-convergence studies, Richardson extrapolation, residual and integral convergence, conservation audits, experimental comparison, uncertainty propagation, sensitivity analysis, and benchmark-case comparison.

## Python-native post-processing
Every plot or derived quantity created in the GUI should have an inspectable Python representation using NumPy, SciPy, pandas, matplotlib, PyVista/VTK, CuPy, PyTorch/JAX where appropriate, and custom CUDA kernels for heavy field operations. Users should be able to click "open as Python" on any derived result, edit the calculation, and push the result back into the visual workspace.

## Experiment and data layer
Import CSV, HDF5, NetCDF, TDMS, MATLAB, OpenFOAM fields, VTK, images, probe logs, PIV fields, and instrument data. Data should carry units, coordinate systems, calibration metadata, uncertainty, timestamps, and provenance. Provide automatic coordinate registration between experiment and CFD geometry.

## Templates and onboarding
Include case templates and intelligent onboarding for canonical problems: incompressible internal flow, external aerodynamics, compressible nozzle flow, shock tube, conjugate heat transfer, rotating machinery, multiphase flow, reacting combustor, premixed/non-premixed flame, spray, particle transport, hypersonic flow, and transient thermal-fluid problems. A novice selecting a template should be guided through assumptions and applicability before values are requested.

## Transparency and reproducibility
Never hide the generated OpenFOAM case. The project tree should always expose 0/, constant/, system/, generated meshes, logs, scripts, ML datasets, checkpoints, Git status, and external dependencies. Every GUI action should map to real files and commands. A built-in terminal and editor must allow the user to work exactly as they would in VS Code or a shell.

Add integrated version control and reproducibility. Every run captures solver version, OpenFOAM distribution/version, Git commit, local modified files, compiler, CUDA version, GPU, CPU, operating system, environment variables, libraries, mesh hash, geometry hash, dictionaries, random seeds, ML model hash, and dataset provenance. Users should be able to reproduce or branch any prior run.

## Added later by the owner
- Integrate the group's cfd_agents (D:\Research-Software\projects\repository-capability-lab\agent-architecture\cfd_agents: Betty, Bill, Bob, Connie, Larry, Murphy, Reggie, Darwin) and create a guided, step-by-step workflow that makes OpenFOAM really easy to use, and make sure it is done correctly.
- Must be robust and easy for beginners and for AI agents; shareable with other students in the research group; work with other AI providers (e.g. Codex) as well as Claude.
