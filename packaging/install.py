"""Install the packaged build (dist/AetherCFD) as a per-user Windows application.

    uv run python packaging/install.py            # after packaging/build.py

- copies dist/AetherCFD to %LOCALAPPDATA%\\Programs\\AetherCFD (the previous install is kept as
  AetherCFD.previous until the next install)
- creates "AetherCFD Studio" shortcuts in the Start menu and on the Desktop
No admin rights, no registry writes. Refuses while the installed app is running.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "AetherCFD"
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
        print(f"no build at {DIST}; run packaging/build.py first", file=sys.stderr)
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
