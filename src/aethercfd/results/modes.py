"""Modal analysis of saved fields (no Qt): POD and DMD, on the GPU when CuPy is available.

POD (method of snapshots, volume-weighted): which spatial structures carry the fluctuation energy, how many
modes reproduce the field, and each mode's time coefficients. DMD (exact DMD, rank-truncated): modes with
one growth rate and one frequency each -- e.g. the mode that makes a "steady" run diverge, and how fast.

Snapshots are (n_times, n_cells) or (n_times, n_cells, n_components), usually from CaseData.snapshots().
For DMD the snapshots must be equally spaced in time (or in iterations, for a steady solver's history).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class PodResult:
    energy: np.ndarray  # fraction of fluctuation energy per mode (sums to 1)
    singular_values: np.ndarray
    modes: np.ndarray  # (n_modes, n_cells[, n_comp]), unit norm in the weighted inner product
    coefficients: np.ndarray  # (n_times, n_modes)
    mean: np.ndarray
    backend: str = "cpu"
    notes: list[str] = field(default_factory=list)

    def modes_for(self, fraction: float = 0.99) -> int:
        """How many modes hold ``fraction`` of the energy (0 when there is no fluctuation at all)."""
        if not len(self.energy) or self.energy.sum() <= 0:
            return 0
        return int(min(np.searchsorted(np.cumsum(self.energy), fraction) + 1, len(self.energy)))


@dataclass
class DmdResult:
    eigenvalues: np.ndarray  # discrete-time (per snapshot step)
    growth_rate: np.ndarray  # per unit of dt (log|lambda|/dt); > 0 grows
    frequency: np.ndarray  # cycles per unit of dt
    amplitudes: np.ndarray  # |b|, mode weight in the first snapshot
    modes: np.ndarray  # (r, n_cells[, n_comp]) complex
    backend: str = "cpu"
    notes: list[str] = field(default_factory=list)

    def fastest_growing(self) -> int:
        return int(np.argmax(self.growth_rate))


GPU_POD_MIN_VALUES = 2_000_000  # values per snapshot from which POD runs faster on the GPU (incl. the copy)


def _linalg_backend(backend: str, nbytes: int = 0):
    """(xp, name): CuPy only if its linear algebra (cuBLAS/cuSOLVER) loads and the data (about 4 working copies of
    ``nbytes``) fits in the GPU's free memory; numpy otherwise (a 4 GB laptop GPU swaps to a crawl when full)."""
    from aethercfd.core.gpu import array_module

    xp, name = array_module(backend)
    if name == "cuda":
        try:
            a = xp.ones((2, 2))
            _ = (a @ a, xp.linalg.eigh(a))
        except Exception:  # cuBLAS/cuSOLVER are not part of the minimal CUDA wheels
            return np, "cpu (CUDA linear algebra unavailable)"
        try:
            free, _total = xp.cuda.Device().mem_info
            if nbytes and 4 * nbytes > 0.8 * free:
                return np, f"cpu ({nbytes / 2**30:.1f} GB of snapshots: too large for the GPU's free memory)"
        except Exception:
            pass
    return xp, name


def _flatten(snapshots: np.ndarray) -> tuple[np.ndarray, tuple]:
    s = np.asarray(snapshots, dtype=np.float64)
    if s.ndim < 2:
        raise ValueError("snapshots must be (n_times, n_cells[, n_components])")
    return s.reshape(s.shape[0], -1), s.shape[1:]


def pod(snapshots, *, weights: np.ndarray | None = None, n_modes: int | None = None, subtract_mean: bool = True,
        backend: str = "auto") -> PodResult:
    from aethercfd.core.gpu import to_numpy

    X, shape = _flatten(snapshots)
    if backend == "auto" and X.shape[1] < GPU_POD_MIN_VALUES:
        backend = "cpu"  # measured on an RTX 3050 Ti: below ~2M values the PCIe copy costs more than it saves
    xp, name = _linalg_backend(backend, X.nbytes)
    nt = X.shape[0]
    if nt < 2:
        raise ValueError("POD needs at least two snapshots")
    Xd = xp.asarray(X)
    mean = Xd.mean(axis=0) if subtract_mean else xp.zeros(Xd.shape[1])
    F = Xd - mean
    w = None
    if weights is not None:
        w = xp.asarray(np.repeat(np.asarray(weights, dtype=np.float64), int(np.prod(shape[1:])) if len(shape) > 1
                                 else 1))
    Fw = F * w if w is not None else F
    C = (Fw @ F.T) / nt  # (nt, nt) temporal correlation, weighted
    lam, V = xp.linalg.eigh(C)
    order = xp.argsort(lam)[::-1]
    lam, V = lam[order], V[:, order]
    lam = xp.clip(lam, 0, None)
    k = int(min(n_modes or nt, nt))
    lam_k, V_k = lam[:k], V[:, :k]
    sig = xp.sqrt(lam_k * nt)
    good = sig > sig[0] * 1e-12 if k else sig > 0
    modes = (F.T @ V_k) / xp.where(good, sig, 1.0)  # (n, k), unit weighted norm
    coeffs = F @ (modes * (w[:, None] if w is not None else 1.0))  # projection a_k(t)
    total = float(to_numpy(lam).sum())
    energy = to_numpy(lam_k) / total if total > 0 else np.zeros(k)
    res = PodResult(energy, to_numpy(sig), to_numpy(modes.T).reshape((k, *shape)), to_numpy(coeffs),
                    to_numpy(mean).reshape(shape), name)
    if total <= 0:
        res.notes.append("the snapshots are identical (no fluctuation about the mean): there are no modes to "
                         "find, e.g. a converged steady run")
    if not subtract_mean:
        res.notes.append("mean not subtracted: mode 1 is (mostly) the mean field")
    return res


def dmd(snapshots, *, dt: float = 1.0, rank: int | None = None, delays: int = 1,
        backend: str = "auto") -> DmdResult:
    """Exact DMD of equally spaced snapshots x_0..x_m: x_{k+1} ~ A x_k, A projected on the leading POD modes.
    ``delays`` > 1 stacks time-shifted copies (Hankel DMD): needed when a spatial structure oscillates in place
    (a standing wave has one shape but two eigenvalues), which plain DMD cannot represent."""
    from aethercfd.core.gpu import to_numpy

    X, shape = _flatten(snapshots)
    if delays > 1:
        m = X.shape[0] - delays + 1
        if m < 3:
            raise ValueError(f"{X.shape[0]} snapshots are too few for {delays} delays")
        X = np.hstack([X[k:k + m] for k in range(delays)])
    if X.shape[0] < 3:
        raise ValueError("DMD needs at least three equally spaced snapshots")
    if backend == "auto":
        backend = "cpu"  # measured: with the method of snapshots the CPU beats copying the data to the GPU
    xp, name = _linalg_backend(backend, X.nbytes)
    D = xp.asarray(X.T)  # (n * delays, m+1)
    X1, X2 = D[:, :-1], D[:, 1:]
    # SVD by the method of snapshots: eigen-decompose the small (m x m) Gram matrix instead of factorising the
    # tall (n x m) data -- one big GEMM, fast on CPU and GPU (a tall-skinny SVD is slow on cuSOLVER)
    G = X1.conj().T @ X1
    lam_g, V = xp.linalg.eigh(G)
    order = xp.argsort(lam_g)[::-1]
    lam_g, V = xp.clip(lam_g[order], 0, None), V[:, order]
    s = xp.sqrt(lam_g)
    r = int(min(rank or len(s), len(s)))
    keep = s[:r] > s[0] * 1e-7  # the Gram matrix squares the condition number: a looser cut than for an SVD
    r = int(to_numpy(keep).sum()) or 1
    s, V = s[:r], V[:, :r]
    Vh = V.conj().T
    U = (X1 @ V) / s
    Atilde = U.conj().T @ X2 @ Vh.conj().T / s
    lam, W = np.linalg.eig(to_numpy(Atilde))  # small r x r: on the CPU
    Phi = to_numpy(X2 @ Vh.conj().T / s) @ W  # exact DMD modes (n, r)
    b = np.linalg.lstsq(Phi, to_numpy(X1[:, 0]), rcond=None)[0]
    with np.errstate(divide="ignore"):
        omega = np.log(lam.astype(complex)) / dt
    n_space = int(np.prod(shape))
    res = DmdResult(lam, omega.real, omega.imag / (2 * np.pi), np.abs(b), Phi[:n_space].T.reshape((r, *shape)),
                    name)
    if r < X.shape[0] - 1:
        res.notes.append(f"rank truncated to {r} of {X.shape[0] - 1}")
    n_snap = len(snapshots)
    if n_snap < 10:
        res.notes.append(f"only {n_snap} snapshots: indicative only (write every ~10 steps for a solid DMD)")
    if np.any(np.abs(np.abs(res.frequency) * dt - 0.5) < 0.05):
        res.notes.append("a mode sits at the sampling (Nyquist) frequency: it changes sign between saves; the "
                         "real frequency may be higher (save more often)")
    return res
