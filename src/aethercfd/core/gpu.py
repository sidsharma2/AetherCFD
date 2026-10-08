"""GPU compute backend: CuPy (CUDA) when available, numpy otherwise -- same array code for both.

Algorithms written against ``xp`` (numpy-compatible) run on the NVIDIA GPU through CuPy, which needs only
the display driver (CUDA runtime and NVRTC come as pip wheels; no toolkit install). Everything falls back
to numpy transparently, so features never depend on having a GPU.
"""

from __future__ import annotations

import logging
import os
import warnings
from functools import lru_cache

import numpy as np

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _cupy():
    if os.environ.get("AETHERCFD_NO_CUDA"):
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "CUDA path could not be detected": the pip wheels are used
            import cupy as cp

            if cp.cuda.runtime.getDeviceCount() < 1:
                return None
            cp.zeros(1).sum()  # touch the device once (fails fast on driver problems)
        return cp
    except Exception as exc:  # no CUDA, driver too old, missing NVRTC ...
        log.info("CUDA compute unavailable: %s", exc)
        return None


def array_module(prefer: str = "auto"):
    """(xp, name): 'cuda' if requested/available, else numpy ('cpu')."""
    if prefer == "cpu":
        return np, "cpu"
    cp = _cupy()
    if cp is None:
        if prefer == "cuda":
            log.warning("CUDA requested but unavailable; using the CPU")
        return np, "cpu"
    return cp, "cuda"


def info() -> dict | None:
    cp = _cupy()
    if cp is None:
        return None
    p = cp.cuda.runtime.getDeviceProperties(0)
    free, total = cp.cuda.Device(0).mem_info
    return {"name": p["name"].decode(), "compute_capability": cp.cuda.Device(0).compute_capability,
            "memory_gb": round(total / 2**30, 2), "free_gb": round(free / 2**30, 2),
            "runtime": cp.cuda.runtime.runtimeGetVersion(), "driver": cp.cuda.runtime.driverGetVersion()}


def to_numpy(a) -> np.ndarray:
    if isinstance(a, np.ndarray):
        return a
    get = getattr(a, "get", None)
    return get() if get is not None else np.asarray(a)


def segment_sum(xp, index, values, n: int):
    """sum of values (1D or 2D rows) per index -> length-n result (bincount: fast on CPU and GPU)."""
    if values.ndim == 1:
        return xp.bincount(index, weights=values, minlength=n)
    return xp.stack([xp.bincount(index, weights=values[:, k], minlength=n) for k in range(values.shape[1])], axis=1)
