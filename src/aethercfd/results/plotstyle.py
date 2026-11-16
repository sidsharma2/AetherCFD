"""Publication plot style: the user's plot_style.py (settings ``plot_style``) or a bundled journal fallback
(STIX serif, inward ticks, PDF + PNG). Import only in a subprocess: it switches matplotlib to Agg and changes
global rcParams."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def load_style(style: str | None):
    """Apply ``apply_style()`` of the user's module, or the fallback; returns the module's
    ``save_fig(fig, out_without_extension)`` if it has one, else None (save .pdf and .png yourself)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = Path(style or "")
    if path.is_file():
        s = importlib.util.spec_from_file_location("user_plot_style", path)
        mod = importlib.util.module_from_spec(s)
        s.loader.exec_module(mod)  # type: ignore[union-attr]
        if hasattr(mod, "apply_style"):
            mod.apply_style()
        return getattr(mod, "save_fig", None)
    plt.rcParams.update({"font.family": "serif", "font.serif": ["STIXGeneral", "DejaVu Serif"],
                         "mathtext.fontset": "stix", "font.size": 9, "axes.linewidth": 0.7,
                         "xtick.direction": "in", "ytick.direction": "in", "xtick.top": True,
                         "ytick.right": True})
    return None
