"""Build user OpenFOAM code with wmake inside WSL, with the case's fork sourced; parse compiler errors back
to Windows paths so the editor can jump to them (no Qt here)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from aethercfd.backend.wsl.forks import ForkProfile, prelude
from aethercfd.core.paths import win_to_wsl, wsl_to_win

_DIAG = re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>error|warning|fatal error):"
                   r"\s*(?P<msg>.*)$", re.MULTILINE)


@dataclass
class Diagnostic:
    file: str  # Windows path when mappable
    line: int
    col: int
    severity: str
    message: str


@dataclass
class BuildResult:
    ok: bool
    returncode: int
    output: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    library: str | None = None


def parse_diagnostics(output: str, src_dir_wsl: str, distro: str = "LMAE-OpenFOAM") -> list[Diagnostic]:
    out = []
    for m in _DIAG.finditer(output):
        f = m.group("file")
        if not f.startswith("/"):
            f = f"{src_dir_wsl.rstrip('/')}/{f}"
        try:
            win = wsl_to_win(f, distro=distro)
        except ValueError:
            win = f
        out.append(Diagnostic(win, int(m.group("line")), int(m.group("col") or 0),
                              "error" if "error" in m.group("sev") else "warning", m.group("msg").strip()))
    return out


def build(runner, fork: ForkProfile, src_dir: Path, target: str = "libso", *, clean: bool = False,
          timeout_s: float = 1800) -> BuildResult:
    d = win_to_wsl(str(src_dir))
    body = prelude(fork) + (
        'cd "$1" || exit 2\n'
        + ('wclean >/dev/null 2>&1\n' if clean else '')
        + 'wmake $2 2>&1\n'
        'rc=$?\n'
        'echo "AETHER_USERLIB=$FOAM_USER_LIBBIN"\n'
        'echo "AETHER_USERBIN=$FOAM_USER_APPBIN"\n'
        'exit $rc\n')
    r = runner.run_script(body, [d, target], timeout_s=timeout_s)
    out = r.stdout + ("\n" + r.stderr if r.stderr else "")
    lib = None
    m = re.search(r"^LIB\s*=\s*\$\(FOAM_USER_LIBBIN\)/(\S+)", (src_dir / "Make" / "files").read_text(
        encoding="utf-8", errors="replace"), re.MULTILINE) if (src_dir / "Make" / "files").exists() else None
    if m:
        userlib = re.search(r"AETHER_USERLIB=(\S+)", out)
        lib = f"{userlib.group(1) if userlib else '$FOAM_USER_LIBBIN'}/{m.group(1)}.so"
    diags = parse_diagnostics(out, d, getattr(runner, "distro", "LMAE-OpenFOAM"))
    return BuildResult(r.ok and not any(x.severity == "error" for x in diags), r.returncode, out, diags, lib)


def copy_solver(runner, fork: ForkProfile, solver: str, name: str, dest: Path) -> str:
    """Copy an installed solver's source directory under a new executable name (EXE = $(FOAM_USER_APPBIN)/name)."""
    d = win_to_wsl(str(dest))
    body = prelude(fork) + (
        'src=$(find "$FOAM_SOLVERS" "$FOAM_APP/solvers" -maxdepth 4 -type d -name "$1" 2>/dev/null | head -1)\n'
        'if [ -z "$src" ]; then echo "AETHERCFD: no solver $1 in this OpenFOAM" >&2; exit 3; fi\n'
        'mkdir -p "$3" && cp -r "$src"/. "$3"/ || exit 4\n'
        'cd "$3" && sed -i -E "s|^EXE *=.*|EXE = \\$(FOAM_USER_APPBIN)/$2|" Make/files\n'
        'echo "copied $src"\n')
    r = runner.run_script(body, [solver, name, d], timeout_s=120)
    if not r.ok:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:])
    return r.stdout.strip()
