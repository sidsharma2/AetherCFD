# Contributing to AetherCFD Studio

Thank you for your interest. AetherCFD Studio is in active development, and help is welcome: bug reports,
tests on cases we do not have, documentation, and code.

## Report a bug

1. In the app, select Help > Report a Problem. It makes a zip with the end of the app log, the crash and hang
   records and the versions of the app and its libraries. It holds nothing from a case folder, but logs can
   show file paths: look inside it before you share it.
2. Open an [issue](https://github.com/sidsharma2/AetherCFD/issues). Say what you did, what you expected and what
   happened, your Windows version, the OpenFOAM fork and version, and attach the zip.

A case that shows the problem is the most useful thing you can send. Make it as small as you can, and send only
cases you are free to share.

## Set up for development

You need Windows 11 (or Windows 10 21H2 or newer), Git and [uv](https://docs.astral.sh/uv/). WSL 2 with
OpenFOAM is needed only for the live tests and for running cases. [INSTALL.md](INSTALL.md), path B, has every
step.

```
git clone https://github.com/sidsharma2/AetherCFD.git
cd AetherCFD
uv sync --locked
uv run aethercfd doctor
uv run aethercfd-studio
```

## Before you open a pull request

```
uv run ruff check src tests packaging scripts
uv run lint-imports
uv run pytest tests/unit tests/studio -m "not wsl and not wsl_slow and not corpus and not gl"
```

These are the checks CI runs on every pull request. Also:

- Add a test for each fix and each feature. A test that fails without your change is the best evidence.
- Keep `aethercfd.core`, `aethercfd.case`, `aethercfd.runs` and `aethercfd.results` free of Qt. The import
  contracts in `pyproject.toml` check this.
- Read [docs/adr/0008-threading.md](docs/adr/0008-threading.md) before you touch rendering or worker threads.
  Only the GUI thread renders, and data crosses threads as copies.
- Every change the app makes to a case goes through a command: plan, approval, journal. Do not write case files
  directly from the GUI.
- Never delete user data. Removed files go to the project trash.
- Live WSL tests (`tests/wsl`) must work on scratch copies only, never on a user's case.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

By contributing, you agree that your contribution is licensed under the project's license (see
[LICENSE](LICENSE)).

## Open tasks

These are good places to start. Each one links to the part of the roadmap or the docs that describes it. Say in
an issue which one you take, so two people do not work on the same thing.

### Test on hardware and setups we do not have

- **Cluster runs on a real Slurm cluster.** The tests use a fake Slurm. [docs/cluster-runs.md](docs/cluster-runs.md)
  lists what to check on the first real job.
- **The `--cpu-only` build** of `packaging/build.py` has never been built end to end.
- **Other OpenFOAM versions and Linux distributions in WSL.** Tested: ESI v2306 and v2606 (release candidate)
  and Foundation 14 on Ubuntu 22.04.
- **PINN setup on other GPUs.** `scripts/setup_ml_env.py` has been run on one NVIDIA laptop GPU.

### Known bugs and gaps

- An access violation in VTK's OpenFOAM reader while it loads a large decomposed case, seen once in three full
  studio test runs and not reproduced. A reproduction would help most.
- The same family of native crash happens now and then in `tests/studio` on CI's software OpenGL.
- Starting a run from the copilot or a script waits for the launch to finish (the GUI path does not).
- The 3D view's OpenGL context recovery depends on internals of pyvistaqt 0.13.1.

### Features on the roadmap

From [docs/vision/roadmap.md](docs/vision/roadmap.md):

- **Q7. Layer planner:** write `thicknessModel` for snappyHexMesh and report layer coverage per patch.
- **Q8. A smaller download:** the packaged app is about 1.8 GB.
- **M1, M2. Meshing as a task chain:** stale-state tracking, a leak check and a cfMesh fallback.
- **M3. Solution steering:** staged run recipes that start on robust settings and end on the target schemes.
- **M4. Deeper verification:** grid studies on snappy meshes, least-squares GCI for four or more grids.
- **M5 to M8:** indicator-driven refinement, a run database with surrogates, ML warm starts, PINNs with hard
  constraints.

### Documentation

- A short tutorial from an empty folder to a converged, checked case.
- Screenshots and notes for the Foundation fork (most current screenshots use ESI).
- Translations of the user documentation.

## Code of conduct

Be kind and specific. Critique code and results, not people. Maintainers may remove comments or contributions
that are abusive.
