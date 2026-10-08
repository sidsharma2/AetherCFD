"""Reproducibility record written next to every run (provenance.json in the run dir).

Captures what is needed to reproduce or branch the run later: software versions (AetherCFD, the
OpenFOAM fork build, the solver binary's sha256), the machine, the case's git state, hashes of every
input file and of the mesh, OpenFOAM's own expansion of the key dictionaries, and the environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from aethercfd import __version__
from aethercfd.backend.wsl.forks import ForkProfile, prelude
from aethercfd.backend.wsl.runner import CREATE_NO_WINDOW, WslRunner
from aethercfd.case.model import is_time_dir
from aethercfd.core.textio import write_generated_text

_MAX_HASH_BYTES = 2 * 1024**3


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_hashes(case_root: Path, start_time: str | None) -> tuple[dict[str, str], str | None]:
    """sha256 of system/, constant/ (mesh separately) and the start-time fields -> (inputs, mesh_hash)."""
    files: dict[str, str] = {}
    mesh = hashlib.sha256()
    mesh_files = 0
    dirs = ["system", "constant"] + ([start_time] if start_time else [])
    for d in dirs:
        base = case_root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.stat().st_size > _MAX_HASH_BYTES:
                continue
            rel = p.relative_to(case_root).as_posix()
            digest = _sha_file(p)
            if "/polyMesh/" in f"/{rel}":
                mesh.update(rel.encode())
                mesh.update(digest.encode())
                mesh_files += 1
            else:
                files[rel] = digest
    return files, (mesh.hexdigest() if mesh_files else None)


def git_state(case_root: Path) -> dict | None:
    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(case_root), *args], capture_output=True, text=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    head = git("rev-parse", "HEAD")
    if head is None:
        return None
    status = git("status", "--porcelain", "--", ".") or ""
    diff = git("diff", "--", ".") or ""
    return {"head": head.strip(), "dirty": bool(status.strip()), "status": status[:20000], "diff": diff[:200000]}


def windows_hardware() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine(), "processor": platform.processor(),
            "cpus": os.cpu_count()}
    try:
        import psutil

        info["ram_bytes"] = psutil.virtual_memory().total
    except Exception:
        pass
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        if r.returncode == 0:
            info["gpus"] = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def linux_side(runner: WslRunner, fork: ForkProfile, case_wsl: str, apps: list[str]) -> dict:
    """Fork build, solver binary hashes, expanded dictionaries, environment, VM resources."""
    q = shlex.quote
    body = prelude(fork) + f"cd {q(case_wsl)} 2>/dev/null || true\n" + "\n".join([
        'echo "@@BUILD $(foamVersion 2>/dev/null || echo "$WM_PROJECT_VERSION")"',
        'echo "@@UNAME $(uname -a)"',
        'echo "@@OS $(. /etc/os-release && echo "$PRETTY_NAME")"',
        'echo "@@NPROC $(nproc)"',
        "echo \"@@MEM $(awk '/MemTotal/{print $2*1024}' /proc/meminfo)\"",
        "env | grep -E '^(WM_|FOAM_|OMPI_|MPI_)' | sed 's/^/@@ENV /'",
        *[f'p=$(command -v {q(a)} 2>/dev/null) && echo "@@BIN {a} $p $(sha256sum "$p" | cut -d" " -f1)"'
          for a in apps if "/" not in a],
        "for d in controlDict fvSchemes fvSolution; do",
        '  [ -f "system/$d" ] && { echo "@@DICT $d"; foamDictionary -expand "system/$d" 2>&1 | head -c 200000; '
        'echo "@@END"; }',
        "done",
    ]) + "\n"
    res = runner.run_script(body, timeout_s=120)
    out: dict = {"env": {}, "binaries": {}, "expanded": {}}
    cur: str | None = None
    buf: list[str] = []
    for ln in res.stdout.splitlines():
        if cur is not None:
            if ln == "@@END":
                out["expanded"][cur] = "\n".join(buf)
                cur, buf = None, []
            else:
                buf.append(ln)
            continue
        if ln.startswith("@@DICT "):
            cur = ln[7:].strip()
        elif ln.startswith("@@ENV "):
            k, _, v = ln[6:].partition("=")
            out["env"][k] = v
        elif ln.startswith("@@BIN "):
            parts = ln[6:].split()
            if len(parts) == 3:
                out["binaries"][parts[0]] = {"path": parts[1], "sha256": parts[2]}
        elif ln.startswith("@@"):
            key, _, val = ln[2:].partition(" ")
            out[key.lower()] = val.strip()
    if not res.ok:
        out["error"] = res.stderr.strip()[-500:]
    return out


def wsl_version() -> str | None:
    try:
        r = subprocess.run(["wsl.exe", "--version"], capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
                           env=dict(os.environ, WSL_UTF8="1"))
        return r.stdout.decode("utf-8", errors="replace").replace("\x00", "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def capture(rec, runner: WslRunner, *, start_time: str | None = None) -> dict:
    """Build and write provenance.json for a prepared/launched run record."""
    case_root = Path(rec.case_root)
    fork = rec.fork_profile
    files, mesh_hash = input_hashes(case_root, start_time or _first_time(case_root))
    prov = {
        "format": 1,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "aethercfd": __version__,
        "run": {"id": rec.id, "steps": rec.steps, "nprocs": rec.nprocs, "placement": rec.placement,
                "placement_reason": rec.placement_reason, "exec_case": rec.exec_case_wsl,
                "signals_enabled": rec.signals_enabled},
        "fork": rec.fork,
        "case": {"root": rec.case_root, "git": git_state(case_root)},
        "inputs": {"files": files, "mesh_sha256": mesh_hash},
        "windows": windows_hardware(),
        "wsl_version": wsl_version(),
        "linux": linux_side(runner, fork, rec.exec_case_wsl, [s["app"] for s in rec.steps]),
    }
    write_generated_text(Path(rec.run_dir) / "provenance.json", json.dumps(prov, indent=2) + "\n")
    return prov


def _first_time(case_root: Path) -> str | None:
    times = sorted((float(p.name), p.name) for p in case_root.iterdir() if p.is_dir() and is_time_dir(p.name))
    return times[0][1] if times else None
