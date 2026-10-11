# AetherCFD Studio

A desktop workbench that drives OpenFOAM **without hiding it**. Every GUI action maps to real files and real
commands; dictionaries stay byte-for-byte yours; the case on disk is the source of truth. Claude works next
to you as a copilot that can see, explain and change the case — every change and run is shown as an exact
plan you approve, and lands in the Command Journal.

**New to AetherCFD, or setting it up on another PC?** Start with [INSTALL.md](INSTALL.md) (release zip or
source, WSL + OpenFOAM, the first-run setup wizard, the copilot sign-in).

## Launch

- **Start menu / Desktop: "AetherCFD Studio"** (the packaged app,
  `%LOCALAPPDATA%\Programs\AetherCFD\AetherCFD.exe`). Update it with `uv run python packaging/build.py`
  then `uv run python packaging/install.py`; the build also writes the shareable
  `build/AetherCFD-<version>-win64.zip`.
- From the source tree: `uv run aethercfd-studio [case]` (add `--reset-layout` to ignore saved layouts),
  or double-click `AetherCFD Studio.cmd`.
- Headless: `aethercfd-cli doctor --probe`, `open`, `run`, `runs`, `control`, `set`, and
  `aethercfd-cli copilot <case> "<question>" --approve none|edits|all [--provider claude|codex]` (packaged; from source:
  `uv run aethercfd copilot ...`).

**Copilot providers and sign-in:** the copilot runs through Claude Code (default; the Claude desktop app
ships it) or the OpenAI Codex CLI; pick the provider and model in the Copilot panel's top bar (settings
`copilot_provider`, `copilot_model`). Sign in once from the panel ("Sign in to Claude" / "Sign in to
Codex"), or run `claude auth login` / `codex login`. Either way the AI only gets the AetherCFD tools:
its own shell and file-writing tools are off, and every edit or run goes through the approval dialog.

**GPU:** the app renders on the discrete NVIDIA GPU automatically (setting `gpu = "high-performance"`;
`integrated` / `system` to change). CUDA compute (CuPy) is used when available (the very first GPU stage after an install compiles its kernels, about a minute; they are cached); everything falls back to
the CPU.

## What is in it

| Area | Panels / features |
|---|---|
| Find your way | command bar (case, perspectives, search, run state); Ctrl+K search that tolerates typos (`chek mesh`, `resdiuals`, `vortx`) and falls back to "Ask the copilot" |
| Set up | **New Case from Template** (Ctrl+N): physics questions → ranked OpenFOAM tutorials (both forks) → parameters; **Workflow**: the guided, staged workspace (Geometry → Domain → Materials → Physics → Mesh → Models → BC/IC → Numerics → Run → Validation → Post → ML), each stage with a status, next action, dependencies and correctness gates (dimensions, pressure reference, U/p pairing, solver/scheme coverage, checkMesh, y+, species sum, Mach vs solver, convergence), a Guided mode of physical questions that writes the dictionaries as one approvable plan, Advanced (the expert panels) and Raw (the exact files); "Mark done" is refused while a gate fails; **Checks**: numerics lab + pre-run checks with one-click journalled fixes; Case Files; Editor (raw/form/split, lossless); Properties |
| Mesh | **AetherMesh**: checkMesh-exact quality maps and hotspot clusters shown in 3D, blockMesh grading designer (no size jumps), **hole blocking** (O-grid in a box through several walls: H-O box per level, end slabs, ellipses, corner-misalignment check), y+ first-cell height and layers, STL intake (watertight, feature angle, extruded/axisymmetric) and ranked meshing strategies, **CAD import** through FreeCAD (STEP/IGES/BREP/FCStd, one patch per named part) and a one-click **snappyHexMesh setup** from them (background box sized from the cell budget, features, curvature-driven levels, layers, STL solids → patches; both forks, verified live); Mesh Quality (checkMesh log). Case-local mesh generators run as approvable, journalled scripts |
| Physics | **Combustion** (Cantera): Z_st, T_ad(φ), transport, laminar flame speed and thickness, ignition delay, Peters regime and mesh resolution, model advisor (EDC, PaSR, laminar, flamelets) with exact dictionaries per fork, counterflow flamelets; CRECK and GRI mechanisms; **Physics Code**: your own models/solvers from templates (e.g. k-ε with Pope's round-jet correction), built with wmake in WSL, wired into the case |
| Run | Run (plan-first; **Automatic** start does what the case needs: the tutorial's own `./Allrun`, or mesh + check + solve, or solve; write-now, stop & write, pause, kill; guards), Residuals, Solver Log, Terminal (WSL, one OpenFOAM version sourced) |
| Visualize | 3D Viewport on the real FV mesh in two looks: **Studio** (AetherCFD's own post-processing: smooth shading with depth cues, crisp outlines, per-field colour maps, a slim colour bar with units and SI prefixes, a title line, a clickable orientation gizmo; colours smoothed from the cell values and always labelled as such) and **Classic** (ParaView-style true cell colours). **Showcase** (✦) gives the best first view in one click (3D: a mid-plane slice through the most of the flow inside a glass shell of the domain). **Animation**: turntable or time sweep as GIF/WebP. **Open in ParaView** hands the case, field and time to ParaView as the expert backup. + **Visualization** pipeline: Q-criterion vortices and isosurfaces on the GPU (CUDA marching tetrahedra on any polyhedral mesh), slices, clips, thresholds, contours, streamlines, glyphs, schlieren (exact with OpenFOAM's grad(rho)), mesh-quality maps, mirror/revolve, presentation smoothing; **Render image** up to 8K with 8× MSAA, or **ray-traced** through ParaView (OptiX path tracer on RTX) |
| Analyse | **Python Console** (the open case as numpy: `data.fields()`, `data.residuals()`, plots inline, journalled `do(...)`); **Diff** (two files, vs another case, or before a journalled change); **Analysis**: POD/DMD modes of saved fields (growth rates of a diverging mode), a Gaussian-process surrogate over runs, and **PINNs** (PyTorch on the GPU, out of process: reconstruct a field from sparse data with advection-diffusion or Navier-Stokes physics; validated on Kovasznay flow to 0.06-0.4 %); **Measurements**: experiment vs simulation (bias, RMSE, R²) and optimal-interpolation data assimilation (corrected field + uncertainty, written as fields) |
| Assist | **Copilot** (Claude): reads the case, logs and mesh, runs checks and pre-calculations, proposes edits/runs for approval, and drives the app in front of you (opens panels and cases, switches looks, makes Showcase views and animations, adds visualization stages, points at things); it renders the view and looks at the image itself before describing a result |
| Agents | The research group's **cfd_agents**, run out of process in their own environment (`aethercfd.services.cfd_agents`): Murphy (failure diagnosis of a solver log), Connie (run health from fieldMinMax/solverInfo), Reggie (transient numerics design), Darwin (RANS-informed refinement plan, written outside the case). Their output is advice; applying it goes through an approvable plan. Betty/Bill/Bob/Larry are not called (they write the case directly, delete time folders, or need a local LLM). Settings `cfd_agents_python` / `cfd_agents_root` |
| Record | Command Journal (who, what, why, diff — revertable) |

Ctrl+Alt+1…5 switch perspectives (Setup, Meshing, Run Monitor, Post-processing, Code).

**PINN environment:** PINNs train in a separate PyTorch environment, so the app stays small. Create it once:
`uv venv .ml-env --python 3.12` then `uv pip install --python .ml-env/Scripts/python.exe torch numpy --index-url https://download.pytorch.org/whl/cu126` (or point the `ml_python` setting at any python with torch).

## Panels, monitors, layouts

Every panel docks, tabs, splits, auto-hides or floats onto another monitor (View ▸ Add Panel; Window ▸
Move Focused Panel to Monitor N; Window ▸ Identify Monitors). Layouts are remembered **per perspective
and per monitor setup** and restored when monitors are plugged/unplugged. Window ▸ Layout Diagnostics
shows what was detected.

## Where things live

- `<case>/.aethercfd/` — project sidecar: `project.toml`, `local/` (journal, run records, provenance,
  trash, renders, copilot session, combustion report). Added to `.git/info/exclude`.
- `<case>/aether-code/` — your own OpenFOAM sources (Physics Code); libraries build into
  `$FOAM_USER_LIBBIN` inside WSL.
- `%APPDATA%\AetherCFD\` — settings and layouts. `%LOCALAPPDATA%\AetherCFD\Cache\` — fork/capability
  cache, converted mechanisms, `studio.log`, crash logs.
- Runs execute in the WSL distro chosen in the setup wizard (`wsl.default_distro`): in place under
  `/mnt/<drive>/...` when the path is safe,
  otherwise from a staged ext4 copy that syncs results back.

## Safety rails

Only allow-listed distros; `wsl --shutdown/--terminate/--unregister/...` are refused in code; two OpenFOAM
versions are never sourced in one shell; runaway-log, disk and memory guards stop a run before it can fill
the disk; nothing is ever hard-deleted (trash in the sidecar); the copilot cannot run shell commands or
edit files directly — only through approvable, journalled commands.

## Development

```
uv sync
uv run pytest tests/unit tests/studio            # fast suites
uv run pytest tests/wsl -m "wsl or wsl_slow"      # live WSL tests (scratch copies only)
uv run pytest tests/corpus -m corpus              # lossless round-trip over every file in D:\OpenFOAM
uv run python -m aethercfd.studio <case> --smoke  # GUI smoke run with screenshots in build/smoke
uv run python packaging/build.py                  # build + test the packaged app (dist/AetherCFD)
uv run python packaging/install.py                # install it with Start-menu/Desktop shortcuts
```

Design decisions: `docs/adr/`. Research notes: `docs/research/` (meshing survey; the REV B.1 hole-blocking case
study that drove the AetherMesh hole tool).
