"""Create, check and register the PyTorch environment that PINN training runs in (standard library only).

    python scripts/setup_ml_env.py            # create it in the default place, check it, save the setting
    python scripts/setup_ml_env.py --check    # only check the environment AetherCFD would use
    python scripts/setup_ml_env.py --cpu      # the CPU build even with an NVIDIA GPU
    python scripts/setup_ml_env.py --dry-run  # print the commands, run nothing

The environment is a plain virtual environment with ``torch`` and ``numpy``. It is created with ``uv`` when uv
is on PATH (fast, and it fetches a suitable Python), otherwise with ``python -m venv`` and pip. The CUDA build
of PyTorch is chosen when ``nvidia-smi`` reports a driver for CUDA 12.6 or newer; otherwise the CPU build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TORCH_INDEX = "https://download.pytorch.org/whl/{tag}"
CUDA_TAG = "cu126"  # the CUDA build that is proven here (torch 2.14.1+cu126 on an RTX 3050 Ti)
CUDA_MIN = (12, 6)  # the driver must support at least this CUDA version for the cu126 wheels
PYTHON = "3.12"

CHECK = ("import json, sys, numpy, torch\n"
         "cuda = torch.cuda.is_available()\n"
         "dev = 'cuda' if cuda else 'cpu'\n"
         "x = torch.linspace(0, 1, 64, device=dev).reshape(-1, 1).requires_grad_(True)\n"
         "y = torch.nn.Sequential(torch.nn.Linear(1, 8), torch.nn.Tanh(), torch.nn.Linear(8, 1)).to(dev)(x)\n"
         "g = torch.autograd.grad(y.sum(), x, create_graph=True)[0]\n"
         "ok = bool(torch.isfinite(g).all())\n"
         "print(json.dumps({'torch': torch.__version__, 'numpy': numpy.__version__, 'cuda': cuda, "
         "'cuda_version': torch.version.cuda, 'gpu': torch.cuda.get_device_name(0) if cuda else None, "
         "'autograd_ok': ok, 'python_version': sys.version.split()[0]}))\n")


def env_python(env_dir: str | Path) -> Path:
    """The python executable of a virtual environment folder."""
    env_dir = Path(env_dir)
    return env_dir / "Scripts" / "python.exe" if os.name == "nt" else env_dir / "bin" / "python"


def default_env_dir() -> Path:
    """The per-user data folder (the same place platformdirs.user_data_dir gives) + ml-env."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "AetherCFD" / "ml-env"


def check_env(py: str | Path, timeout_s: float = 180) -> dict:
    """Run a tiny forward/backward pass in ``py``: available True with versions and the GPU, or False with a
    plain ``note`` (torch missing, torch broken, no answer)."""
    try:
        r = subprocess.run([str(py), "-I", "-c", CHECK], capture_output=True, text=True, timeout=timeout_s,
                           creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"available": False, "python": str(py),
                "note": f"the PyTorch check in {py} gave no answer within {timeout_s:.0f} s"}
    except OSError as exc:
        return {"available": False, "python": str(py), "note": f"{py} does not run ({exc})"}
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        err = (r.stderr or r.stdout).strip()
        last = err.splitlines()[-1] if err else f"exit code {r.returncode}"
        what = "is not installed" if "No module named 'torch'" in err else (
            "is there but numpy is missing" if "No module named 'numpy'" in err else "does not load")
        return {"available": False, "python": str(py), "note": f"PyTorch {what} in {py} ({last})",
                "detail": err[-1500:]}
    if not info.get("autograd_ok"):
        return {"available": False, "python": str(py), **info, "note": "PyTorch loads but gives NaN in a test"}
    return {"available": True, "python": str(py), **info}


def driver_cuda(run: Callable = subprocess.run, which: Callable = shutil.which) -> tuple[int, int] | None:
    """The highest CUDA version the NVIDIA driver supports (from nvidia-smi), or None without one."""
    smi = which("nvidia-smi")
    if not smi:
        return None
    try:
        r = run([smi], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if r.returncode == 0 and m else None


def choose_build(cpu: bool, cuda_tag: str | None, drv: tuple[int, int] | None) -> tuple[str | None, str]:
    """(wheel index URL or None for PyPI, a sentence saying why)."""
    if cuda_tag:
        return TORCH_INDEX.format(tag=cuda_tag), f"the {cuda_tag} build, as asked"
    if cpu:
        return (None if sys.platform == "darwin" else TORCH_INDEX.format(tag="cpu")), "the CPU build, as asked"
    if sys.platform == "darwin":
        return None, "the macOS build from PyPI (CPU, or the Apple GPU through MPS)"
    if drv is None:
        return TORCH_INDEX.format(tag="cpu"), "the CPU build: no NVIDIA GPU or driver found"
    if drv < CUDA_MIN:
        return (TORCH_INDEX.format(tag="cpu"),
                f"the CPU build: the NVIDIA driver supports CUDA {drv[0]}.{drv[1]}, older than "
                f"{CUDA_MIN[0]}.{CUDA_MIN[1]}; update the driver and run this again for the GPU build")
    return TORCH_INDEX.format(tag=CUDA_TAG), f"the CUDA build ({CUDA_TAG}; the driver supports CUDA {drv[0]}.{drv[1]})"


def install_commands(env_dir: Path, index: str | None, uv: str | None, base_python: str,
                     create: bool = True) -> list[list[str]]:
    py = str(env_python(env_dir))
    idx = ["--index-url", index] if index else []
    if uv:
        cmds = [[uv, "venv", str(env_dir), "--python", PYTHON]] if create else []
        return [*cmds, [uv, "pip", "install", "--python", py, "torch", "numpy", *idx]]
    cmds = [[base_python, "-m", "venv", str(env_dir)]] if create else []
    return [*cmds, [py, "-m", "pip", "install", "torch", "numpy", *idx]]


def save_setting(py: Path) -> str:
    """Write ml_python into AetherCFD's settings file; a sentence saying what happened."""
    try:
        from aethercfd.core.settings import Settings
    except ImportError:
        return (f"AetherCFD's settings could not be opened from this Python, so set ml_python = '{py}' in "
                "Settings yourself (or leave it: the default place is found without a setting)")
    s = Settings.load()
    s.ml_python = str(py)
    s.save()
    return f"saved ml_python = {py} in {Settings.path()}"


def main(argv: Sequence[str] | None = None, *, run: Callable = subprocess.run, which: Callable = shutil.which,
         check: Callable = check_env, save: Callable = save_setting, out: Callable = print) -> int:
    ap = argparse.ArgumentParser(prog="setup_ml_env.py", description="Set up the PyTorch environment for PINNs.")
    ap.add_argument("--dir", type=Path, default=None, help=f"environment folder (default {default_env_dir()})")
    ap.add_argument("--cpu", action="store_true", help="the CPU build of PyTorch even with an NVIDIA GPU")
    ap.add_argument("--cuda", default=None, metavar="TAG", help="a specific CUDA build, e.g. cu126 or cu128")
    ap.add_argument("--check", action="store_true", help="only check the environment, install nothing")
    ap.add_argument("--force", action="store_true", help="install torch again into an existing environment")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and run nothing")
    ap.add_argument("--no-settings", action="store_true", help="do not write ml_python into the settings")
    a = ap.parse_args(argv)
    env_dir = (a.dir or default_env_dir()).expanduser().resolve()
    py = env_python(env_dir)
    exists = py.is_file()

    if a.check:
        if not exists:
            out(f"No environment at {env_dir}. Run this script without --check to create it.")
            return 1
        return _report(check(py), out)

    if exists and not a.force:
        st = check(py)
        if st.get("available"):
            out(f"An environment already exists at {env_dir} and works; nothing installed.")
            code = _report(st, out)
            if not a.no_settings and not a.dry_run:
                out(save(py))
            return code
        out(f"An environment exists at {env_dir} but does not work: {st.get('note')}. "
            "Run again with --force to install PyTorch into it, or choose another folder with --dir.")
        return 1

    index, why = choose_build(a.cpu, a.cuda, None if a.cpu or a.cuda else driver_cuda(run, which))
    uv = which("uv")
    cmds = install_commands(env_dir, index, uv, sys.executable, create=not exists)
    out(f"Installing PyTorch into {env_dir}: {why}. This downloads about "
        f"{'2.5 GB' if index and '/cu' in index else '200 to 800 MB'}.")
    for c in cmds:
        out("  " + " ".join(f'"{x}"' if " " in x else x for x in c))
    if a.dry_run:
        return 0
    env_dir.parent.mkdir(parents=True, exist_ok=True)
    for c in cmds:
        r = run(c)
        if getattr(r, "returncode", 1) != 0:
            out(f"This step failed (exit code {getattr(r, 'returncode', '?')}): {' '.join(c)}. Nothing was "
                "registered. Check the network connection and the free disk space, then run this script again.")
            return 1
    st = check(py)
    code = _report(st, out)
    if code == 0 and index and "/cu" in index and not st.get("cuda"):
        out("The CUDA build is installed but PyTorch does not see the GPU. Training will run on the CPU. "
            "An NVIDIA driver update usually fixes this.")
    if code == 0 and not a.no_settings:
        out(save(py))
    return code


def _report(st: dict, out: Callable) -> int:
    if not st.get("available"):
        out(f"Check failed: {st.get('note')}")
        return 1
    out(f"Check passed: torch {st.get('torch')}, numpy {st.get('numpy')}, Python {st.get('python_version')}, "
        + (f"GPU {st.get('gpu')} (CUDA {st.get('cuda_version')})" if st.get("cuda") else "CPU only")
        + f". Python: {st.get('python')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
