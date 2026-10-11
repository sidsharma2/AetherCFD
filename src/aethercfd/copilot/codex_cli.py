"""Find and query the OpenAI Codex CLI, the copilot's second provider (no Qt here).

Candidates: the ``codex_exe`` setting, ``codex`` on PATH, the native executable inside npm's global
@openai/codex package (preferred over the ``codex.cmd`` shim, so stopping an answer stops the real
process), ~/.local/bin, and the copy the Codex desktop app keeps under
%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe. The explicit setting wins; otherwise the newest
version, because an older CLI may not read a ~/.codex/config.toml written by a newer Codex app.

Sign-in is checked with ``codex login status`` (no usage). What the CLI supports (``--ignore-user-config``,
feature flags, the model catalog) is probed once and kept in ``CliInfo.extra``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from aethercfd.copilot.provider import NO_WINDOW, AuthStatus, CliInfo, version_key

CONFIG_ERROR = "Error loading configuration"


def _npm_native(shim_dir: Path) -> list[Path]:
    """The real codex.exe(s) inside an npm global install (the .cmd shim only starts node)."""
    pkg = shim_dir / "node_modules" / "@openai" / "codex"
    if not pkg.is_dir():
        return []
    found = sorted(pkg.glob("node_modules/@openai/codex-win32-*/vendor/*/codex/codex.exe"))
    found += sorted(pkg.glob("vendor/*/codex/codex.exe"))  # older package layout
    found += sorted(pkg.glob("bin/codex-*-windows-*.exe"))
    return found


def candidates(configured: str | None = None) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if configured:
        out.append((Path(configured), "setting"))
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    on_path = shutil.which("codex")
    if on_path:
        p = Path(on_path)
        native = _npm_native(p.parent) if p.suffix.lower() in (".cmd", ".ps1", "") else []
        out += [(n, "npm") for n in native] or [(p, "path")]
    out += [(n, "npm") for n in _npm_native(appdata / "npm")]
    out.append((home / ".local" / "bin" / "codex.exe", "native"))
    app = local / "OpenAI" / "Codex" / "bin"
    if app.is_dir():
        out += [(exe, "desktop-app") for exe in sorted(app.glob("*/codex.exe"))]
    out.append((appdata / "npm" / "codex.cmd", "npm-shim"))  # last resort: works, but Stop cannot kill node's child
    seen, uniq = set(), []
    for exe, src in out:
        key = str(exe).lower()
        if key not in seen:
            seen.add(key)
            uniq.append((exe, src))
    return uniq


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)


def probe(exe: Path, source: str, timeout: float = 30) -> CliInfo | None:
    try:
        r = _run([str(exe), "--version"], timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"codex(?:-cli)?\s+v?(\d+\.\d+[\w.\-]*)", r.stdout or "", re.IGNORECASE)
    if r.returncode != 0 or not m:
        return None
    return CliInfo(exe, m.group(1), source)


def capabilities(cli: CliInfo, timeout: float = 30) -> dict:
    """Flags and feature names this codex build knows (unknown --disable features are an error)."""
    caps: dict = {"exec_flags": [], "features": []}
    try:
        h = _run([str(cli.exe), "exec", "--help"], timeout).stdout or ""
        caps["exec_flags"] = sorted(set(re.findall(r"(--[a-z][a-z0-9-]+)", h)))
        f = _run([str(cli.exe), "features", "list"], timeout)
        if f.returncode == 0:
            caps["features"] = [ln.split()[0] for ln in (f.stdout or "").splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return caps


def find_codex(configured: str | None = None, *, with_capabilities: bool = True) -> CliInfo | None:
    found: list[CliInfo] = []
    for exe, source in candidates(configured):
        if not exe.is_file():
            continue
        info = probe(exe, source)
        if info is None:
            continue
        if source == "setting":
            found = [info]
            break
        found.append(info)
    if not found:
        return None
    best = max(found, key=lambda c: (version_key(c.version), c.source != "npm-shim"))
    if with_capabilities:
        best.extra.update(capabilities(best))
    return best


def auth_status(cli: CliInfo, timeout: float = 30) -> AuthStatus:
    try:
        r = _run([str(cli.exe), "login", "status"], timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AuthStatus(False, "unknown", str(exc))
    text = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if CONFIG_ERROR in text:
        line = next((ln for ln in text.splitlines() if CONFIG_ERROR in ln), text)
        return AuthStatus(False, "config-error",
                          f"Codex {cli.version} cannot read its settings ({line.strip()}). Update the Codex CLI "
                          "(npm install -g @openai/codex@latest) or fix that line in ~/.codex/config.toml.")
    m = re.search(r"Logged in using (.+)", text)
    if r.returncode == 0 and m:
        method = m.group(1).strip().rstrip(".")
        return AuthStatus(True, "chatgpt" if "chatgpt" in method.lower() else "api-key", method)
    return AuthStatus(False, "none", text.splitlines()[0] if text else "")


def login_argv(cli: CliInfo) -> list[str]:
    """Interactive sign-in with a ChatGPT account (opens the browser); run it in a visible console."""
    return [str(cli.exe), "login"]


def list_models(cli: CliInfo, timeout: float = 30) -> list[tuple[str, str]]:
    """(display name, slug) from ``codex debug models`` (listed models, by priority); [] if unsupported."""
    try:
        r = _run([str(cli.exe), "debug", "models"], timeout)
        data = json.loads(r.stdout or "null")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    models = data.get("models", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    rows = [m for m in models if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"]
    rows.sort(key=lambda m: m.get("priority", 999))
    return [(str(m.get("display_name") or m["slug"]), str(m["slug"])) for m in rows]
