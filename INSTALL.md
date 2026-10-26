# Installing AetherCFD Studio on your PC

AetherCFD Studio is a Windows desktop workbench for OpenFOAM. OpenFOAM itself runs inside WSL (the
Windows Subsystem for Linux), so there are two parts to set up: **the app** (a few minutes) and **WSL +
OpenFOAM** (about 20 minutes the first time). An optional **AI copilot** needs a Claude or ChatGPT
account of your own.

On the first start, a **setup wizard** checks everything below and tells you exactly what is still
missing. It never installs anything by itself. You can open it again any time from **Help > Setup Wizard**.

## What you need

| | Required? | Notes |
|---|---|---|
| Windows 10 (21H2+) or 11, 64-bit | yes | about 3 GB free for the app, plus your cases |
| WSL 2 with Ubuntu | yes, to run cases | without it you can still open cases and look at results |
| OpenFOAM ESI v2306 or Foundation 14 (inside Ubuntu) | yes, to run cases | one is enough; both can be installed side by side |
| An NVIDIA GPU | no | used for the 3D view and CUDA post-processing when present; everything falls back to the CPU |
| Claude Code or the OpenAI Codex CLI | no | only for the AI copilot |
| ParaView, FreeCAD, a PyTorch Python | no | ray-traced renders / CAD import / PINN training |

## 1. Get AetherCFD

Either of these:

- **The release zip** (easiest). Ask the owner for `AetherCFD-<version>-win64.zip` (they make it with
  `uv run python packaging/build.py`, see the end of this file).
- **The source code**, if you have been added to the private GitHub repository (see
  [Sharing the app](#for-the-owner-sharing-the-app) for how the owner adds you). Accept the invitation
  e-mail from GitHub, then:

  ```
  git clone https://github.com/sidsharma2/AetherCFD.git
  ```

## 2. Install the app

**From the release zip**

1. Right-click the zip > *Extract All...* (do not run it from inside the zip).
2. Double-click **`Install AetherCFD.cmd`**. It copies the app to `%LOCALAPPDATA%\Programs\AetherCFD` and
   adds **AetherCFD Studio** to the Start menu and the Desktop. No admin rights, no Python needed.
   (With Python installed, `python install.py` does the same.)
3. Or skip installing: run `AetherCFD\AetherCFD.exe` straight from the extracted folder.

Windows may show *"Windows protected your PC"* the first time, because the app is not code-signed:
click *More info > Run anyway*.

**From the source code**

1. Install [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`, or see its website).
2. In the cloned folder:

   ```
   uv sync
   uv run aethercfd-studio
   ```

   `AetherCFD Studio.cmd` in the folder starts it too. To build your own installable copy:
   `uv run python packaging/build.py`, then `uv run python packaging/install.py`.

## 3. WSL and OpenFOAM

The wizard's **WSL and OpenFOAM** page shows these commands with a *Copy* button, adapted to your PC.

1. **WSL.** In a PowerShell opened with *Run as administrator*:

   ```
   wsl --install --no-distribution
   ```

   Restart Windows.

2. **Ubuntu.** In a normal PowerShell:

   ```
   wsl --install -d Ubuntu-22.04
   ```

   Choose a Linux user name and password when it asks.

3. **OpenFOAM**, inside Ubuntu (open *Ubuntu 22.04* from the Start menu). Install one of the two:

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

   If a command fails, check the official pages (they change from time to time):
   develop.openfoam.com (wiki > precompiled > debian) and openfoam.org/download/14-ubuntu.

4. Back in AetherCFD's wizard, pick the distro and press **Look for OpenFOAM**. It starts Ubuntu, finds
   the installed versions and remembers the distro. AetherCFD only ever uses the distro you choose there,
   and never shuts down or changes WSL.

## 4. The first start: setup wizard

The wizard opens by itself the first time (and from **Help > Setup Wizard** later):

1. **WSL and OpenFOAM**: which Linux distro to use, and what OpenFOAM it has (see step 3).
2. **Case folders**: where your cases live (default `%USERPROFILE%\OpenFOAM`; it can create it). Avoid
   OneDrive-synced folders and paths with spaces for cases you run.
3. **Optional tools**: ParaView, FreeCAD and a PyTorch Python are found automatically when installed;
   *Browse...* if one lives somewhere unusual.
4. **The copilot's AI**: whether Claude Code / Codex are installed and signed in, with a *Sign in* button
   for each, and which one the copilot uses.

Nothing is saved until you press *Finish*. You can skip any step and come back later.

## 5. The AI copilot (optional)

The copilot reads your case, logs and mesh, explains problems and proposes changes and runs. **Every
change and every run is shown to you as an exact plan to approve**, and lands in the Command Journal. The
AI itself cannot run shell commands or write files.

It runs through an AI command-line tool that **you sign in to with your own account** (AetherCFD never
sees your password or API key). Pick one in the Copilot panel's top bar (provider and model; remembered):

- **Claude Code** (default): needs a Claude subscription or an Anthropic API account. Install it from
  claude.com/code (or `npm install -g @anthropic-ai/claude-code`); the Claude desktop app also brings it.
  Sign in with the panel's *Sign in to Claude* button, or `claude auth login`.
- **OpenAI Codex**: needs a ChatGPT plan or an OpenAI API account. Install the Codex CLI with
  `npm install -g @openai/codex` (needs [Node.js](https://nodejs.org)), or install the Codex desktop app.
  Sign in with *Sign in to Codex*, or `codex login`.

Usage is billed to your own account. If the panel says the CLI *cannot read its settings*, your Codex CLI
is older than a `~/.codex/config.toml` written by a newer Codex app: update it with
`npm install -g @openai/codex@latest`.

## 6. Optional extras

- **ParaView** (paraview.org/download): *Open in ParaView* and ray-traced renders.
- **FreeCAD** (freecad.org/downloads): CAD import (STEP/IGES/BREP) for meshing.
- **PINNs** need a separate Python with PyTorch (kept apart so the app stays small). With uv:

  ```
  uv venv %LOCALAPPDATA%\AetherCFD\ml-env --python 3.12
  uv pip install --python %LOCALAPPDATA%\AetherCFD\ml-env\Scripts\python.exe torch numpy --index-url https://download.pytorch.org/whl/cu126
  ```

  It is found there automatically (or set `ml_python` in the settings).
- **Your own kinetic mechanisms** (CHEMKIN/YAML) for the Combustion panel: add their folders to
  `mechanism_roots` in the settings file. GRI-Mech 3.0 and the other Cantera built-ins work without it.

## Where things live

- Settings: `%APPDATA%\AetherCFD\settings.toml` (plain text; the wizard writes it). Delete it to start
  over with the wizard.
- Logs and caches: `%LOCALAPPDATA%\AetherCFD\Cache\` (`studio.log` is the first place to look when
  something goes wrong).
- Crash and hang records: `%LOCALAPPDATA%\AetherCFD\Cache\crash\`. A native crash adds the stack of every
  thread to `faulthandler.log`. When the window stops responding for more than 10 s, AetherCFD writes a
  `hang-*.txt` file with the stack of every thread.
- Per case: a hidden `.aethercfd` folder inside the case (journal, run records, copilot conversation).

## Troubleshooting

- **"OpenFOAM backend unavailable: no WSL distro is set up"**: run **Help > Setup Wizard** and pick a
  distro with OpenFOAM.
- **The wizard finds no OpenFOAM** right after installing it: press *Look for OpenFOAM* again; a stopped
  distro takes a few seconds to start.
- **The copilot says it is not signed in** although you just signed in: press *Check again* in the panel.
- **The window stops responding**: wait at least 10 s before you close it. AetherCFD then writes a
  `hang-*.txt` file that shows where it was stuck. Send it with a report (next item). The environment
  variable `AETHER_HANG_SECONDS` changes the 10 s; `0` turns the check off.
- Anything else: use **Help > Report a Problem**. It saves one zip in
  `%LOCALAPPDATA%\AetherCFD\Cache\reports\` with the app log, the crash and hang records and the
  versions. Nothing from your case folders goes into it, and nothing is sent. Email the zip to the owner.

### Optional: full Windows crash and hang dumps (needs admin)

Only set this up if the owner asks for it. It changes a Windows setting, and a full dump can hold
file paths and environment values, so check a dump before you share it.

- **Crash dumps**: Windows Error Reporting saves one when `AetherCFD.exe` crashes. In an admin
  PowerShell, create the key
  `HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\AetherCFD.exe` and give it
  `DumpType` (DWORD, `2` for a full dump) and `DumpFolder` (expandable string, for example
  `%LOCALAPPDATA%\CrashDumps`). Delete the key to turn it off.
- **Hang dumps**: download ProcDump from Microsoft Sysinternals and run
  `procdump -h -ma AetherCFD.exe` while the app runs. It writes a full dump when the window stops
  responding for 5 s.

## For the owner: sharing the app

1. **Give access to the code** (only if they should get the source): on GitHub, open the repository >
   *Settings* > *Collaborators* (under *Access*) > *Add people*, enter their GitHub user name or e-mail,
   choose the role (*Read* is enough to clone; *Write* to push). They get an e-mail invitation to accept.
   A private repository on a personal account can have collaborators on the free plan.
2. **Or give them the release zip**: `uv run python packaging/build.py` builds and tests the app, then
   writes `build/AetherCFD-<version>-win64.zip` (the app, both installers and this file). To zip an
   existing build without rebuilding: `uv run python packaging/build.py --zip-only`. Share the zip through
   OneDrive/Teams/a USB stick (it is a few hundred MB, too large for e-mail), or attach it to a GitHub
   release of the private repository.
3. Point them at this file. Nothing in the app is tied to your PC: they choose their own distro, case
   folders and AI account in the wizard.
