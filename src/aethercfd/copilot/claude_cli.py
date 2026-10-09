"""Find and query the Claude Code CLI that powers the copilot (no Qt here).

Search order: the ``copilot.claude_exe`` setting, ``claude`` on PATH, the native installer location
(~/.local/bin), npm's global bin, and the copy the Claude desktop app keeps under
%APPDATA%\\Claude\\claude-code\\<version>\\<build>\\claude.exe (newest version wins). Sign-in is checked
with ``claude auth status --json``, which costs no usage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class ClaudeCli:
    exe: Path
    version: str
    source: str  # setting | path | native | npm | desktop-app


@dataclass
class AuthStatus:
    logged_in: bool
    method: str = "none"
    detail: str = ""


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def candidates(configured: str | None = None) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if configured:
        out.append((Path(configured), "setting"))
    on_path = shutil.which("claude")
    if on_path:
        out.append((Path(on_path), "path"))
    home = Path.home()
    out.append((home / ".local" / "bin" / "claude.exe", "native"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    out.append((appdata / "npm" / "claude.cmd", "npm"))
    desktop = appdata / "Claude" / "claude-code"
    if desktop.is_dir():
        builds = []
        for ver in desktop.iterdir():
            if ver.is_dir():
                builds.extend((ver.name, exe) for exe in ver.glob("*/claude.exe"))
        for _ver, exe in sorted(builds, key=lambda b: _version_key(b[0]), reverse=True):
            out.append((exe, "desktop-app"))
    return out


def find_claude(configured: str | None = None) -> ClaudeCli | None:
    for exe, source in candidates(configured):
        if not exe.is_file():
            continue
        try:
            r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30,
                               creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and "Claude Code" in r.stdout:
            return ClaudeCli(exe, r.stdout.split()[0], source)
    return None


def auth_status(cli: ClaudeCli, timeout: float = 30) -> AuthStatus:
    try:
        r = subprocess.run([str(cli.exe), "auth", "status", "--json"], capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return AuthStatus(False, "unknown", str(exc))
    return AuthStatus(bool(d.get("loggedIn")), str(d.get("authMethod", "none")),
                      str(d.get("email") or d.get("orgName") or ""))


def login_argv(cli: ClaudeCli) -> list[str]:
    """Interactive sign-in (opens the browser); run it in a visible console."""
    return [str(cli.exe), "auth", "login"]
