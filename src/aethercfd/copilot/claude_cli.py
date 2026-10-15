"""Find and query the Claude Code CLI that powers the copilot (no Qt here).

Search order: the ``copilot.claude_exe`` setting, ``claude`` on PATH, the native installer location
(~/.local/bin), npm's global bin, and the copy the Claude desktop app keeps under
%APPDATA%\\Claude\\claude-code\\<version>\\<build>\\claude.exe (newest version wins). Sign-in is checked
with ``claude auth status --json``, which costs no usage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from aethercfd.copilot.provider import NO_WINDOW, AuthStatus, CliInfo, version_key

ClaudeCli = CliInfo  # the name the rest of the app (and older tests) use
_version_key = version_key
__all__ = ["API_BILLING_ENV", "NO_WINDOW", "SUBSCRIPTION_METHOD", "AuthStatus", "ClaudeCli", "auth_status",
           "candidates", "cli_env", "find_claude", "login_argv", "logout_argv", "subscription_only"]


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


# Environment that makes Claude Code bill an API account or a cloud provider instead of the subscription it is
# signed in to. The copilot never passes it on (docs/copilot-accounts.md), unless copilot_subscription_only is off.
API_BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                   "CLAUDE_CODE_USE_FOUNDRY")
SUBSCRIPTION_METHOD = "claude.ai"  # `claude auth status --json` authMethod of a Claude subscription sign-in


def subscription_only(settings=None) -> bool:
    if settings is None:
        from aethercfd.core.settings import Settings

        try:
            settings = Settings.load()
        except Exception:
            return True  # unreadable settings: the safe choice
    return bool(getattr(settings, "copilot_subscription_only", True))


def cli_env(settings=None, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every Claude Code process the copilot starts gets (sign-in check, sign-in, sessions)."""
    env = dict(os.environ if base is None else base)
    if subscription_only(settings):
        for k in list(env):
            if k.upper() in API_BILLING_ENV:
                del env[k]
    return env


def auth_status(cli: ClaudeCli, timeout: float = 30) -> AuthStatus:
    try:
        r = subprocess.run([str(cli.exe), "auth", "status", "--json"], capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW, env=cli_env())
        d = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return AuthStatus(False, "unknown", str(exc))
    return AuthStatus(bool(d.get("loggedIn")), str(d.get("authMethod", "none")),
                      str(d.get("email") or d.get("orgName") or ""), str(d.get("subscriptionType") or ""))


def login_argv(cli: ClaudeCli) -> list[str]:
    """Interactive sign-in with a Claude subscription (opens the browser); run it in a visible console."""
    return [str(cli.exe), "auth", "login", "--claudeai"]


def logout_argv(cli: ClaudeCli) -> list[str]:
    """Sign out; shared by everything on this Windows account that uses Claude Code (desktop app, terminal)."""
    return [str(cli.exe), "auth", "logout"]
