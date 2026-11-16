"""The only code in AetherCFD that executes ``wsl.exe`` (ADR 0006).

Rules enforced here:
- argv lists only (never a shell string through PowerShell/cmd), scripts fed on stdin to
  ``bash --noprofile --norc -s`` so no temp files and no quoting layers;
- a hard denylist: AetherCFD never shuts down, terminates, unregisters, imports or exports a distro
  (``wsl --shutdown`` kills every solve on the machine);
- only distros the user allowed in settings (never the Agora sandbox);
- a Linux-side ``timeout`` so a hung command cannot outlive its caller;
- output read as bytes, decoded robustly (wsl.exe itself may emit UTF-16LE), and capped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
OUTPUT_CAP = 8 * 2**20

_DENY = {"--shutdown", "-t", "--terminate", "--unregister", "--import", "--import-in-place", "--export",
         "--set-default", "-s", "--set-version", "--install", "--uninstall", "--update", "--mount",
         "--unmount", "--manage"}


class WslError(RuntimeError):
    pass


class DistroNotAllowed(WslError):
    pass


@dataclass(frozen=True)
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.returncode == 124  # GNU timeout's exit status


def decode_output(data: bytes) -> str:
    """wsl.exe's own messages may be UTF-16LE; Linux programs print UTF-8."""
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def check_wsl_options(options: Sequence[str]) -> None:
    """Raise if any wsl.exe *option* (not script argument) is on the denylist."""
    for opt in options:
        if opt in _DENY or opt.split("=", 1)[0] in _DENY:
            raise WslError(f"refusing to pass {opt!r} to wsl.exe (denylisted)")


class WslRunner:
    def __init__(self, distro: str, *, allowed: Sequence[str] | None = None, wsl_exe: str = "wsl.exe"):
        allowed = list(allowed) if allowed is not None else [distro]
        if not distro:
            raise DistroNotAllowed("no WSL distro is set up for OpenFOAM yet: open Help > Setup Wizard to pick one "
                                   "(or set wsl.default_distro in settings.toml)")
        if distro not in allowed:
            raise DistroNotAllowed(f"distro {distro!r} is not in the allowed list {allowed}")
        if distro.lower() == "agora":
            raise DistroNotAllowed("the Agora sandbox distro is never used by AetherCFD")
        self.distro = distro
        self.wsl_exe = wsl_exe

    # ------------------------------------------------------------------ argv
    def argv(self, inner: Sequence[str], *, cwd: str = "/") -> list[str]:
        options = ["-d", self.distro, "--cd", cwd]
        check_wsl_options(options)
        return [self.wsl_exe, *options, "-e", *inner]

    #: Read the *whole* script from stdin first, then detach stdin before running it. With a plain
    #: ``bash -s`` bash reads the script incrementally, so any child that touches stdin (ESI's
    #: etc/bashrc does) hangs or swallows the rest of the script.
    _BOOT = 'script=$(cat); exec </dev/null; eval "$script"'

    def script_argv(self, args: Sequence[str] = (), *, timeout_s: float | None = None) -> list[str]:
        inner: list[str] = []
        if timeout_s is not None:
            inner += ["timeout", "-k", "5", f"{max(1, int(timeout_s))}"]
        inner += ["/bin/bash", "--noprofile", "--norc", "-c", self._BOOT, "aethercfd", *args]
        return self.argv(inner)

    @staticmethod
    def _env() -> dict[str, str]:
        return dict(os.environ, WSL_UTF8="1")

    # ------------------------------------------------------------------ run
    def run_script(self, body: str, args: Sequence[str] = (), *, timeout_s: float = 120) -> ScriptResult:
        """Run a bash script (fed on stdin) with positional ``args``. Blocking."""
        if "\r" in body:
            raise WslError("script body contains a carriage return (CRLF) -- bash would choke on it")
        argv = self.script_argv(args, timeout_s=timeout_s)
        try:
            proc = subprocess.run(argv, input=body.encode("utf-8"), capture_output=True,
                                  timeout=timeout_s + 30, env=self._env(),
                                  creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except subprocess.TimeoutExpired as exc:
            raise WslError(f"wsl.exe did not return within {timeout_s + 30:.0f}s") from exc
        except FileNotFoundError as exc:
            raise WslError("wsl.exe not found -- is WSL installed?") from exc
        out, err = proc.stdout, proc.stderr
        truncated = len(out) > OUTPUT_CAP or len(err) > OUTPUT_CAP
        return ScriptResult(proc.returncode, decode_output(out[-OUTPUT_CAP:]), decode_output(err[-OUTPUT_CAP:]),
                            truncated)

    def open_console(self, inner: Sequence[str]) -> subprocess.Popen:
        """Run ``inner`` in the distro in a new console window the user types into (e.g. an SSH sign-in with a
        password and one-time code: AetherCFD never sees what is typed)."""
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0  # type: ignore[attr-defined]
        return subprocess.Popen(self.argv(inner), env=self._env(), creationflags=flags)

    def popen_detached(self, inner: Sequence[str], *, cwd: str = "/") -> subprocess.Popen:
        """Start a wsl.exe client detached from our console (used only by explicit opt-in features)."""
        return subprocess.Popen(self.argv(inner, cwd=cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=self._env(),
                                creationflags=(DETACHED_PROCESS | CREATE_NO_WINDOW) if sys.platform == "win32" else 0)
