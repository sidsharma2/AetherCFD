"""Publication plot style: the user's plot_style.py (settings ``plot_style``) or a bundled journal fallback
(STIX serif, inward ticks, PDF + PNG). Import only in a subprocess: it switches matplotlib to Agg and changes
global rcParams."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def repair_font_cache() -> bool:
    """Rebuild matplotlib's font list in memory when its cache cannot find matplotlib's own bundled font.

    matplotlib keeps one ``fontlist-v<version>.json`` per user (``~/.matplotlib``) and trusts it whenever the
    version matches. A second matplotlib of the same version (another venv, the packaged app) can leave a list
    without DejaVu Sans; every text draw then raises "Failed to find font DejaVu Sans ... fallback to the default
    font was disabled" and no figure is saved. Returns True when the list was rebuilt. The user's cache file is
    left alone (the other install may rely on it)."""
    from matplotlib import font_manager as fm

    try:
        fm.fontManager.findfont("DejaVu Sans", fallback_to_default=False, rebuild_if_missing=False)
        return False
    except ValueError:
        pass
    fresh = fm.FontManager()  # scans matplotlib's bundled fonts and the system fonts
    fm.fontManager.__dict__.update(fresh.__dict__)  # in place: other modules hold bound methods of this object
    fm.FontManager._findfont_cached.cache_clear()
    fm._get_font.cache_clear()
    return True


def load_style(style: str | None):
    """Apply ``apply_style()`` of the user's module, or the fallback; returns the module's
    ``save_fig(fig, out_without_extension)`` if it has one, else None (save .pdf and .png yourself)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    repair_font_cache()

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
