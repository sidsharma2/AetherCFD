# ADR 0001 — Do not import `cfd_agents`; vendor a few pure modules

Status: accepted (2026-10-07)

## Context
`cfd_agents` (the research group's agent-architecture repository) already drives
OpenFOAM in a WSL distro. Importing it is unsafe for an app:
- `case/__init__` → `manifest` → `contracts` → `_autofoam_path` calls `load_config()` at import time,
  needs a gitignored `config/local.yaml`, and inserts the AutoFOAM repo into `sys.path`.
- `pydantic`/`yaml` are used but not declared; `mesh_check` imports `agents.vera`.
- `wsl_backend.py` has real defects for a GUI: `cd {cwd!r}` quoting (:276), locale `text=True`
  decoding (:195, :266), undrained pipes that deadlock chatty processes (:264-267), a blocking
  15 s `time.sleep` in termination, login shells (`bash -lc`), one bashrc per backend instance,
  and `translate_path` confined to one case root.
- The repo is under active, uncommitted development (HEAD 820a5f9).

## Decision
Write a thin new backend modelled on it. Copy only pure, dependency-free pieces into
`aethercfd/_vendor/` with a provenance header (`from cfd_agents@820a5f9 <path>`) and port their tests:
`case/log_scan.py`, `case/dat_parser.py`, `case/_text_io.read_text_robust`,
`backend/path_translation.windows_drive_path_to_wsl` (extended for `\\wsl.localhost\…`), and the
checkMesh regexes. Re-implement (not copy) the setsid/pidfile/process-group launch, the
`output_guard` disk projection and the `run_hybrid_pilot.py` log-to-file pattern.
Never reuse Bill/`runner_adapter` (destructive: always blockMesh, deletes time dirs), `run()`,
`restart.classify_*` (strict UTF-8, fails on binary fields), or the case-root confinement.

## Consequences
The app never breaks when cfd_agents changes. Its agents (Connie, Murphy, Darwin) can be called later
as out-of-process services in their own Python 3.13 env through `aethercfd.services`.
