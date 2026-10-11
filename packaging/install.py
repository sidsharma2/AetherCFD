"""Install the packaged build as a per-user Windows application.

    uv run python packaging/install.py            # in the source tree, after packaging/build.py
    python install.py                             # in an unzipped release (AetherCFD-<version>-win64.zip)

- copies the app folder (dist/AetherCFD in the source tree, or the AetherCFD folder next to this script in a
  release) to %LOCALAPPDATA%\\Programs\\AetherCFD (the previous install is kept as AetherCFD.previous until
  the next install)
- creates "AetherCFD Studio" shortcuts in the Start menu and on the Desktop
No admin rights, no registry writes. Refuses while the installed app is running. Without Python, the
release's "Install AetherCFD.cmd" does the same.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _find_dist() -> Path:
    for d in (HERE / "AetherCFD", HERE.parent / "dist" / "AetherCFD"):  # release zip | source tree
        if (d / "AetherCFD.exe").is_file():
            return d
    return HERE.parent / "dist" / "AetherCFD"


DIST = _find_dist()
TARGET = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "AetherCFD"
NAME = "AetherCFD Studio"


def _running_from(target: Path) -> bool:
    ps = ("Get-Process AetherCFD,aethercfd-cli -ErrorAction SilentlyContinue | "
          "ForEach-Object { $_.Path }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    return any(line.strip().lower().startswith(str(target).lower()) for line in out.splitlines())


def _shortcut(lnk: Path, exe: Path) -> None:
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{exe}';$s.WorkingDirectory='{exe.parent}';$s.IconLocation='{exe},0';"
          f"$s.Description='{NAME} - OpenFOAM workbench';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def _folder(name: str) -> Path:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path(out)


def main() -> int:
    exe_src = DIST / "AetherCFD.exe"
    if not exe_src.is_file():
        print(f"no build at {DIST}: in the source tree run packaging/build.py first; in a release, keep install.py "
              "next to the AetherCFD folder", file=sys.stderr)
        return 1
    if _running_from(TARGET):
        print("AetherCFD is running from the install folder; close it and run again", file=sys.stderr)
        return 2
    new = TARGET.with_name("AetherCFD.new")
    prev = TARGET.with_name("AetherCFD.previous")
    if new.exists():
        shutil.rmtree(new)
    print(f"copying {DIST} -> {new}")
    shutil.copytree(DIST, new)
    if TARGET.exists():
        if prev.exists():
            shutil.rmtree(prev)  # our own previous build output, not user data
        TARGET.rename(prev)
    new.rename(TARGET)
    exe = TARGET / "AetherCFD.exe"
    start_menu = _folder("Programs")
    desktop = _folder("Desktop")
    for folder in (start_menu, desktop):
        lnk = folder / f"{NAME}.lnk"
        _shortcut(lnk, exe)
        print(f"shortcut {lnk}")
    print(f"installed {exe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
