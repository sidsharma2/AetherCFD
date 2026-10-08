# AetherCFD Studio

A desktop workbench that drives OpenFOAM **without hiding it**. Every GUI action maps to real files
and real commands; dictionaries stay byte-for-byte yours; the case on disk is the source of truth.

## Launch

- Double-click **`AetherCFD Studio.cmd`** (no console window), or drag a case folder onto it.
- From a terminal: `uv run aethercfd-studio [case]` (add `--reset-layout` to ignore saved layouts).
- Headless: `uv run aethercfd doctor --probe`, `aethercfd open <case>`, `aethercfd run <case> --wait`,
  `aethercfd runs <case>`, `aethercfd control <case> <run> stop_write`, `aethercfd set <case> system/controlDict endTime 500`.

## What is in Milestone 1

| Panel | What it does |
|---|---|
| Start | open / recent / find cases under your case roots, environment status |
| Case Files | the real case tree; time folders grouped; live refresh; open in VS Code / Explorer; copy WSL path |
| Editor (Raw / Form / Split) | lossless editing (comments, alignment, macros, CRLF kept), one undo history for form + text, OpenFOAM validation after save, conflict merge when files change on disk |
| Workflow | the 12 stages as a view of your files, with real issues (e.g. a patch with no boundary condition) |
| Run | plan-first start (exact commands, placement, consequences), live status/ETA, write-now, stop & write, pause, kill, guards |
| Residuals / Solver Log | live from any log (finished cases plot immediately); publication export with your `plot_style.py` |
| 3D Viewport | results on the real finite-volume mesh (cell data, no resampling), time/fields/slices/probe; 2D & wedge views |
| Mesh Quality | checkMesh with explanations and remedies |
| Terminal | WSL shell in the case folder with exactly one OpenFOAM version sourced (`[v2306]` / `[of14]`) |
| Command Journal | every change: who, what, why, diff — and revert |

Ctrl+K opens the command palette (`#` files, `@` dictionary keywords). Ctrl+Alt+1…5 switch perspectives
(Setup, Meshing, Run Monitor, Post-processing, Code).

## Panels, monitors, layouts

Every panel docks, tabs, splits, auto-hides or floats onto another monitor (View ▸ Add Panel; Window ▸
Move Focused Panel to Monitor N; Window ▸ Identify Monitors). Layouts are remembered **per perspective
and per monitor setup** (laptop alone vs. docked with two screens) and restored automatically when
monitors are plugged/unplugged; windows on a vanished monitor come back. Window ▸ Layout Diagnostics
shows what was detected.

## Where things live

- `<case>/.aethercfd/` — project sidecar: `project.toml`, and `local/` (journal, run records,
  provenance, trash). Added to `.git/info/exclude`; opening a case never writes into it.
- `%APPDATA%\AetherCFD\` — settings (`settings.toml`) and layouts. `%LOCALAPPDATA%\AetherCFD\Cache\` —
  OpenFOAM version/capability cache, `studio.log`, crash logs.
- Runs execute in the `LMAE-OpenFOAM` WSL distro: in place under `/mnt/d/...` when the path is safe,
  otherwise from a staged copy in `~/aethercfd/stage/` that syncs results back (never `system/`/`constant/`).

## Safety rails

Only allow-listed distros; `wsl --shutdown/--terminate/--unregister/...` are refused in code; two
OpenFOAM versions are never sourced in one shell; runaway-log, disk and memory guards (in the GUI and
inside WSL) stop a run before it can fill the disk; nothing is ever hard-deleted (trash in the sidecar).

## Development

```
uv sync
uv run pytest tests/unit tests/studio            # fast suites
uv run pytest tests/wsl -m "wsl or wsl_slow"      # live WSL tests (scratch copies only)
uv run pytest tests/corpus -m corpus              # lossless round-trip over every file in D:\OpenFOAM
uv run python -m aethercfd.studio <case> --smoke  # GUI smoke run with screenshots in build/smoke
```

Design decisions: `docs/adr/`. Plan and roadmap: see ADRs and the project plan.
