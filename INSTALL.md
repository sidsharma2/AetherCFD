# Installing AetherCFD Studio

AetherCFD Studio is a Windows desktop program for OpenFOAM. OpenFOAM runs inside WSL (the Windows
Subsystem for Linux). You set up two things: the app, and WSL with OpenFOAM.

There are two ways to get the app:

- **Path A, the installed app.** You get a release zip and install it. You do not need Python. Use
  this path if you only want to use AetherCFD.
- **Path B, the source code.** You clone the GitHub repository and run the app with uv. Use this path
  if you change the code or build the app yourself.

Do the steps in order. Each step ends with a check. If a check fails, see
[Troubleshooting](#troubleshooting).

## What you need

| Part | Required? | Notes |
|---|---|---|
| Windows 11, 64-bit | yes | Windows 10 21H2 or newer should also work. |
| Disk space | yes | 3.5 GB for the installed app. For the source code: 3 GB for `.venv`, and 8 GB more to build the app. |
| WSL 2 with Ubuntu 22.04 | to run cases | Without it you can still open cases and look at results. |
| OpenFOAM ESI v2306 or Foundation 14, inside Ubuntu | to run cases | One is enough. Both can be installed side by side. |
| Git and uv | Path B only | Step B1 installs them. |
| An NVIDIA GPU | no | The 3D view and post-processing use it when it is there. Without it they use the CPU. |
| ParaView, FreeCAD, a Python with PyTorch | no | For ray-traced renders, CAD import and PINN training. |
| Claude Code or the OpenAI Codex CLI | no | Only for the AI copilot. You need your own account. |

## Path A: install the release zip

1. Get `AetherCFD-<version>-win64.zip`. If you have access to the GitHub repository, open its
   *Releases* page and download the zip. Otherwise ask the owner for it.
2. Right-click the zip and select *Extract All*. Do not run anything from inside the zip.
3. In the extracted folder, double-click `Install AetherCFD.cmd`. It copies the app to
   `%LOCALAPPDATA%\Programs\AetherCFD`. It adds **AetherCFD Studio** to the Start menu and the Desktop.
   It needs no admin rights and no Python.
4. Windows can show *"Windows protected your PC"* the first time you start the app. The app is not
   code-signed. Click *More info*, then *Run anyway*.

If you have Python, `python install.py` in the extracted folder does the same as step 3. You can also
skip the install and run `AetherCFD\AetherCFD.exe` from the extracted folder.

**Check.** Open a Command Prompt and run:

```
"%LOCALAPPDATA%\Programs\AetherCFD\aethercfd-cli.exe" doctor
```

Each line must be `ok` or `warn`. Warnings are for optional parts. A `FAIL` under *WSL and OpenFOAM*
is normal until you finish [WSL and OpenFOAM](#wsl-and-openfoam).

Then continue with [WSL and OpenFOAM](#wsl-and-openfoam).

## Path B: run from the source code

You need access to the private repository `sidsharma2/AetherCFD`. The owner adds you on GitHub. You
then get an e-mail invitation. Accept it before you clone.

### B1. Install Git and uv

Open PowerShell (no admin needed) and run:

```
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

Close PowerShell and open a new one, so that it finds the new programs.

**Check.** `git --version` and `uv --version` both print a version.

### B2. Clone the repository

```
git clone https://github.com/sidsharma2/AetherCFD.git
cd AetherCFD
```

Git asks you to sign in to GitHub the first time. Use a folder path without spaces, for example
`C:\dev\AetherCFD`.

### B3. Check the PC

```
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

The script only looks. It does not install anything and does not change WSL. It prints each part as
`ok`, `warn` or `FAIL`, with a fix for each part that is missing. A `FAIL` for WSL is normal on a new
PC. You fix it in [WSL and OpenFOAM](#wsl-and-openfoam). A `warn` for `.venv` is normal before step B4.

### B4. Install Python and the packages

```
uv sync --locked
```

uv downloads Python 3.12 and the exact package versions in `uv.lock`. Everything goes into the `.venv`
folder in the clone, about 3 GB. Nothing is installed for other programs. The first run takes a few
minutes.

**Check.** `uv run aethercfd --version` prints `aethercfd` and the version.

### B5. Run the tests

```
uv run pytest tests/unit -m "not wsl and not wsl_slow and not corpus and not gl"
```

This is the same command that CI runs on GitHub. It takes about 5 minutes. All tests must pass. The
tests marked `wsl`, `corpus` and `gl` need OpenFOAM, the owner's case collection or a real graphics
window, so this command leaves them out.

### B6. Start the app

```
uv run aethercfd-studio
```

You can also double-click `AetherCFD Studio.cmd` in the clone. On the first start the setup wizard
opens. See [The first start](#the-first-start-setup-wizard).

**Check.** Run `uv run aethercfd doctor`. Each line must be `ok` or `warn`. A `FAIL` under *WSL and
OpenFOAM* is normal until you finish [WSL and OpenFOAM](#wsl-and-openfoam).

The script in B3 can do B4, B5 and the check for you:
`powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Sync -Test -Doctor`.

### B7. Optional: build and install your own exe

```
uv run python packaging/build.py
uv run python packaging/install.py
```

The build takes 15 to 30 minutes and needs about 12 GB of free memory. See
[Building and releasing](#for-the-owner-building-and-releasing) for what it checks. `install.py`
replaces the app in `%LOCALAPPDATA%\Programs\AetherCFD` and keeps the old copy as
`AetherCFD.previous`. Close AetherCFD before you run it.

## WSL and OpenFOAM

The setup wizard's **WSL and OpenFOAM** page shows these commands with a *Copy* button.

1. **WSL.** Open PowerShell with *Run as administrator* and run:

   ```
   wsl --install --no-distribution
   ```

   Restart Windows.

2. **Ubuntu.** Open a normal PowerShell and run:

   ```
   wsl --install -d Ubuntu-22.04
   ```

   Choose a Linux user name and password when it asks.

3. **OpenFOAM.** Open *Ubuntu 22.04* from the Start menu. Install one of the two versions.

   OpenFOAM **ESI v2306** (openfoam.com):

   ```
   curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
   sudo apt-get install -y openfoam2306-default
   ```

   OpenFOAM **Foundation 14** (openfoam.org):

   ```
   sudo sh -c "wget -O - https://dl.openfoam.org/gpg.key > /etc/apt/trusted.gpg.d/openfoam.asc"
   sudo add-apt-repository http://dl.openfoam.org/ubuntu
   sudo apt-get update && sudo apt-get install -y openfoam14
   ```

   If a command fails, read the official pages. They change from time to time: develop.openfoam.com
   (wiki, precompiled, debian) and openfoam.org/download/14-ubuntu.

4. In AetherCFD's setup wizard, select the distro and press **Look for OpenFOAM**. The wizard starts
   Ubuntu, finds the installed versions and remembers the distro. AetherCFD only uses the distro you
   select there. It never shuts down WSL and never changes WSL settings.

**Check.** Run doctor with `--probe`. For the installed app:
`"%LOCALAPPDATA%\Programs\AetherCFD\aethercfd-cli.exe" doctor --probe`. From source:
`uv run aethercfd doctor --probe`. It starts the distro and lists the OpenFOAM versions it finds. The
last line must say `0 problem(s)`.

## The first start: setup wizard

The wizard opens by itself the first time. You can open it again from **Help > Setup Wizard**.

1. **WSL and OpenFOAM**: the Linux distro to use, and the OpenFOAM versions in it.
2. **Case folders**: where your cases are. The default is `%USERPROFILE%\OpenFOAM`, and the wizard can
   create it. For cases you run, do not use OneDrive folders or paths with spaces.
3. **Optional tools**: the wizard finds ParaView, FreeCAD and a PyTorch Python when they are
   installed. Use *Browse* if one is in an unusual folder.
4. **The copilot's AI**: whether Claude Code and Codex are installed and signed in. There is a
   *Sign in* button for each, and you select the one the copilot uses.

The wizard saves nothing until you press *Finish*. You can skip a step and come back later.

## The AI copilot (optional)

The copilot reads your case, logs and mesh. It explains problems and proposes changes and runs. You see
every change and every run as an exact plan, and you approve it or not. Each one goes into the Command
Journal. The AI itself cannot run shell commands or write files.

The copilot uses an AI command-line tool. You sign in to it with your own account. AetherCFD never
sees your password or API key. Select the tool in the Copilot panel's top bar.

- **Claude Code** (default). You need a Claude subscription. Install it from claude.com/code, or with
  `npm install -g @anthropic-ai/claude-code`. The Claude desktop app also installs it. Sign in with
  the panel's *Sign in to Claude* button, or run `claude auth login`.
- **OpenAI Codex**. You need a ChatGPT plan or an OpenAI API account. Install the Codex CLI with
  `npm install -g @openai/codex` (this needs [Node.js](https://nodejs.org)), or install the Codex
  desktop app. Sign in with *Sign in to Codex*, or run `codex login`.

Your own account pays for the use. If the panel says the CLI *cannot read its settings*, your Codex
CLI is older than the `~/.codex/config.toml` that a newer Codex app wrote. Update it with
`npm install -g @openai/codex@latest`.

**Check.** `aethercfd doctor` shows each CLI as `ok` with `signed in`.

## Optional extras

- **ParaView** (paraview.org/download): *Open in ParaView* and ray-traced renders.
- **FreeCAD** (freecad.org/downloads): CAD import (STEP, IGES, BREP) for meshing.
- **PINNs** need a separate Python with PyTorch. It is kept apart so that the app stays small. In the
  source folder, run:

  ```
  uv run python scripts/setup_ml_env.py
  ```

  The app finds the environment by itself. You can also set `ml_python` in the settings file.
- **Your own kinetic mechanisms** (CHEMKIN or YAML) for the Combustion panel: add their folders to
  `mechanism_roots` in the settings file. GRI-Mech 3.0 and the other Cantera mechanisms work without
  this.

**Check.** `aethercfd doctor` lists ParaView, FreeCAD and the PyTorch environment under *Optional
tools*.

## What `aethercfd doctor` checks

`aethercfd doctor` is the one command that tells you what works. In the installed app it is
`aethercfd-cli.exe doctor` in `%LOCALAPPDATA%\Programs\AetherCFD`. From source it is
`uv run aethercfd doctor`.

It checks the app version and Python, the Python packages, Qt and VTK, the terminal files, the GPU and
CuPy, WSL and its distros, OpenFOAM in the distro you selected, ParaView, FreeCAD, the PyTorch
environment, Claude Code and Codex with their sign-in state, and write access to the settings and
cache folders.

Each line is `ok`, `warn` or `FAIL`. A `FAIL` is a required part. A `warn` is an optional part. Each
line that is not `ok` has a `fix:` line under it. The exit code is 1 if there is a `FAIL`.

| Option | What it does |
|---|---|
| `--probe` | Starts the selected distro if it is stopped and looks for OpenFOAM in it. Without it, doctor uses the result of the last probe. |
| `--no-backend` | Skips WSL and OpenFOAM. |
| `--quick` | Skips the slow checks: the PyTorch import and the copilot sign-in. |
| `--json` | Prints the results as JSON. |

## Where things are

- Settings: `%APPDATA%\AetherCFD\settings.toml`. It is plain text, and the wizard writes it. Delete it
  to start again with the wizard.
- Logs and caches: `%LOCALAPPDATA%\AetherCFD\Cache\`. When something goes wrong, look in `studio.log`
  first.
- Crash and hang records: `%LOCALAPPDATA%\AetherCFD\Cache\crash\`. A native crash adds the stack of
  every thread to `faulthandler.log`. When the window stops responding for more than 10 s, AetherCFD
  writes a `hang-*.txt` file with the stack of every thread.
- In each case: a hidden `.aethercfd` folder with the journal, the run records and the copilot
  conversation.

## Troubleshooting

- **The Terminal panel is empty or says "Terminal is not defined".** The terminal's JavaScript files
  are missing. From source, run `git checkout -- src/aethercfd/studio/resources/xterm`. For the
  installed app, install it again. `aethercfd doctor` shows this as `xterm.js files`.
- **`uv sync --locked` says the lockfile needs to be updated.** Your clone is not at a commit from
  the repository, or someone edited `pyproject.toml`. Run `git status` and `git pull`. Do not run
  `uv lock` unless you mean to change the package versions.
- **"OpenFOAM backend unavailable: no WSL distro is set up"**: run **Help > Setup Wizard** and select
  a distro that has OpenFOAM.
- **The wizard finds no OpenFOAM right after you installed it**: press *Look for OpenFOAM* again. A
  stopped distro takes a few seconds to start.
- **The copilot says it is not signed in, but you just signed in**: press *Check again* in the panel.
- **`packaging/build.py` stops with "commit memory"**: Windows does not have enough free memory for
  PyInstaller. Close large programs and run it again. It continues from where it stopped.
- **The window stops responding**: wait at least 10 s before you close it. AetherCFD then writes a
  `hang-*.txt` file that shows where it was stuck. Send it with a report (next item). The environment
  variable `AETHER_HANG_SECONDS` changes the 10 s. The value `0` turns the check off.
- **Anything else**: use **Help > Report a Problem**. It saves one zip in
  `%LOCALAPPDATA%\AetherCFD\Cache\reports\` with the app log, the crash and hang records and the
  versions. Nothing from your case folders goes into it, and nothing is sent. E-mail the zip to the
  owner, together with the output of `aethercfd doctor`.

### Optional: full Windows crash and hang dumps (needs admin)

Only set this up if the owner asks for it. It changes a Windows setting. A full dump can contain file
paths and environment values, so look at a dump before you share it.

- **Crash dumps**: Windows Error Reporting saves one when `AetherCFD.exe` crashes. In an admin
  PowerShell, create the key
  `HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\AetherCFD.exe`. Give it
  `DumpType` (DWORD, `2` for a full dump) and `DumpFolder` (expandable string, for example
  `%LOCALAPPDATA%\CrashDumps`). Delete the key to turn it off.
- **Hang dumps**: download ProcDump from Microsoft Sysinternals. Run `procdump -h -ma AetherCFD.exe`
  while the app runs. It writes a full dump when the window stops responding for 5 s.

## For the owner: building and releasing

### The version

The version is in one place: `__version__` in `src/aethercfd/__init__.py`. `pyproject.toml` reads it
from there. The About box, `aethercfd --version`, `aethercfd doctor`, the run records and the exe file
properties (*Properties > Details*) all show it. Between releases it ends in `.dev0`, for example
`0.2.0.dev0`.

### What `packaging/build.py` does

1. **Preflight.** It checks free commit memory (12 GB by default, `--min-commit-gb` changes it), the
   terminal's xterm.js files, the files the spec bundles, and CuPy. If one is missing, it stops with a
   message before PyInstaller starts.
2. **PyInstaller.** This step takes 15 to 30 minutes. When it finishes, `build.py` writes
   `build/pyinstaller/build-stamp.json`. If you run `build.py` again with the same sources, it skips
   PyInstaller and goes to the checks. `--rebuild` forces a new build. PyInstaller's
   `warn-aethercfd.txt` and `xref-aethercfd.html` are copied to `build/reports/`.
3. **Required components.** The two exes, the terminal files, the PINN worker, winpty, the Cantera
   data, QtWebEngine and the CUDA libraries must be in the build.
4. **Size report.** The largest components, printed and saved to `build/reports/size-report.txt` and
   `.json`. The NVIDIA CUDA libraries are about 2.0 GB of the 3.2 GB.
5. **Smoke checks.** `aethercfd-cli.exe doctor --no-backend`, a GUI smoke run on a test case,
   `--run-module` and `--run-script`.
6. **Release zip.** `build/AetherCFD-<version>-win64.zip` with the app, both installers and this
   file.

Other options: `--skip-build` checks and zips the existing build. `--zip-only` only zips it.
`--no-smoke` stops after the size report. `--cpu-only` is experimental: it leaves out CuPy and the
NVIDIA libraries and writes `dist/cpu/AetherCFD` and a `-cpu` zip. The GPU features then use the
CPU. The default build does not change.

### Release procedure

1. Set the release version in `src/aethercfd/__init__.py`, for example `0.1.0`. Add the changes to
   `CHANGELOG.md`. Commit both.
2. Make sure CI passed on that commit.
3. Tag the commit and push the tag:

   ```
   git tag -a v0.1.0 -m "AetherCFD Studio 0.1.0"
   git push origin v0.1.0
   ```

4. Build from a clean clone of the tag, so that no local file goes into the zip:

   ```
   git clone --branch v0.1.0 https://github.com/sidsharma2/AetherCFD.git AetherCFD-0.1.0
   cd AetherCFD-0.1.0
   uv sync --locked
   uv run python packaging/build.py
   ```

5. Look at the size that `build.py` prints for the zip. GitHub accepts release files up to 2 GiB each.
   The zip is about 1.8 GB now, which is close to the limit. `build.py` tells you when the zip is too
   large. Then share the zip through OneDrive, or try the `--cpu-only` build.
6. Make the GitHub release with the zip attached. With the GitHub CLI:

   ```
   gh release create v0.1.0 build\AetherCFD-0.1.0-win64.zip --title "AetherCFD Studio 0.1.0" --notes-file CHANGELOG.md
   ```

   You can also do it on the repository's *Releases* page. Only people with access to the private
   repository can download it.
7. Set the next development version, for example `0.2.0.dev0`, and commit it.

### Giving other people access

- **The source code**: on GitHub, open the repository, then *Settings*, *Collaborators* (under
  *Access*), *Add people*. Enter their GitHub user name or e-mail and select a role. *Read* is enough
  to clone. They get an e-mail invitation to accept.
- **The installed app**: send them to the release on GitHub, or give them the zip through OneDrive,
  Teams or a USB stick. It is too large for e-mail.

Point them to this file. Nothing in the app is tied to your PC. They select their own distro, case
folders and AI account in the wizard.

### What a new PC needs that the repository cannot provide

- WSL 2 with a Linux distro and OpenFOAM installed in it (see [WSL and OpenFOAM](#wsl-and-openfoam)).
- Access to the private repository, and a GitHub sign-in for Git.
- The NVIDIA display driver, if the PC has an NVIDIA GPU.
- Your own sign-ins for Claude Code or Codex, and ParaView and FreeCAD if you use them.
- Your cases and your settings. Copy `%APPDATA%\AetherCFD\settings.toml` from the old PC if you want
  the same settings, then run the setup wizard to check them.
