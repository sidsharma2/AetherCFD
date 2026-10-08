"""Gaussian-process surrogate over runs (no Qt, numpy + scipy): inputs -> one output, with uncertainty.

Typical use: a parameter sweep (outlet pressure, flow rate, a model constant) and one result per run
(dilution ratio, a peak temperature, a pressure drop). The GP interpolates between runs, says how sure it is,
and suggests where the next run reduces the uncertainty the most.

    gp = GPSurrogate().fit(X, y)          # X (n, d), y (n,)
    mean, std = gp.predict(Xnew)
    gp.suggest(bounds)                     # next input to run (largest predictive std)

Kernel: squared exponential with one length scale per input (ARD) plus a noise term; hyperparameters by
maximising the log marginal likelihood (L-BFGS-B, several starts). Inputs scaled to [0, 1], output
standardised. This is an interpolator for a handful of expensive runs, not a replacement for physics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class GPSurrogate:
    noise_floor: float = 1e-6
    restarts: int = 5
    theta: np.ndarray | None = None  # log [signal var, length scales..., noise var]
    notes: list[str] = field(default_factory=list)

    def _k(self, A, B, th):
        ls = np.exp(th[1:-1])
        d2 = (((A[:, None, :] - B[None, :, :]) / ls) ** 2).sum(-1)
        return np.exp(th[0]) * np.exp(-0.5 * d2)

    def _nll(self, th):
        K = self._k(self._X, self._X, th) + (np.exp(th[-1]) + self.noise_floor) * np.eye(len(self._X))
        try:
            L = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            return 1e25
        a = np.linalg.solve(L.T, np.linalg.solve(L, self._y))
        return 0.5 * self._y @ a + np.log(np.diag(L)).sum() + 0.5 * len(self._y) * np.log(2 * np.pi)

    def fit(self, X, y) -> GPSurrogate:
        from scipy.optimize import minimize

        X = np.atleast_2d(np.asarray(X, dtype=np.float64))
        if X.shape[0] == 1 and np.asarray(y).size > 1:
            X = X.T
        y = np.asarray(y, dtype=np.float64).ravel()
        if len(y) < 2:
            raise ValueError("a surrogate needs at least two runs")
        self._lo, self._hi = X.min(0), X.max(0)
        span = np.where(self._hi > self._lo, self._hi - self._lo, 1.0)
        self._span = span
        self._X = (X - self._lo) / span
        self._ym, self._ys = y.mean(), (y.std() or 1.0)
        self._y = (y - self._ym) / self._ys
        d = X.shape[1]
        rng = np.random.default_rng(0)
        best = None
        for i in range(self.restarts):
            th0 = np.r_[0.0, np.log(rng.uniform(0.2, 1.0, d)) if i else np.log(np.full(d, 0.5)), np.log(1e-2)]
            bounds = [(-5, 5)] + [(np.log(0.02), np.log(20))] * d + [(np.log(1e-8), np.log(1.0))]
            r = minimize(self._nll, th0, method="L-BFGS-B", bounds=bounds)
            if best is None or r.fun < best.fun:
                best = r
        self.theta = best.x
        K = self._k(self._X, self._X, self.theta) + (np.exp(self.theta[-1]) + self.noise_floor) * np.eye(len(y))
        self._L = np.linalg.cholesky(K)
        self._alpha = np.linalg.solve(self._L.T, np.linalg.solve(self._L, self._y))
        if len(y) < 2 * d + 2:
            self.notes.append(f"{len(y)} runs for {d} input(s): treat the uncertainty as indicative")
        return self

    def predict(self, Xs) -> tuple[np.ndarray, np.ndarray]:
        Xs = np.atleast_2d(np.asarray(Xs, dtype=np.float64))
        if Xs.shape[1] != self._X.shape[1] and Xs.shape[0] == self._X.shape[1]:
            Xs = Xs.T
        Z = (Xs - self._lo) / self._span
        # outside the sampled box the GP reverts to its prior and can be confidently wrong: flag it
        self.last_extrapolated = ((Z < -0.05) | (Z > 1.05)).any(axis=1)
        Ks = self._k(Z, self._X, self.theta)
        mean = Ks @ self._alpha
        v = np.linalg.solve(self._L, Ks.T)
        var = np.exp(self.theta[0]) - (v ** 2).sum(0)
        return mean * self._ys + self._ym, np.sqrt(np.clip(var, 0, None)) * self._ys

    def suggest(self, bounds: list[tuple[float, float]], n: int = 2000) -> np.ndarray:
        """The input inside ``bounds`` with the largest predictive standard deviation."""
        rng = np.random.default_rng(1)
        lo, hi = np.array(bounds, dtype=np.float64).T
        cand = lo + rng.random((n, len(bounds))) * (hi - lo)
        _m, s = self.predict(cand)
        return cand[int(np.argmax(s))]

    def length_scales(self) -> np.ndarray:
        """Per input, in its own units: how far the output stays correlated (large = weakly dependent)."""
        return np.exp(self.theta[1:-1]) * self._span
