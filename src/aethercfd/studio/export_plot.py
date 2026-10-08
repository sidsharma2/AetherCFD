"""Publication export of a residual plot, run in a SUBPROCESS so the user's matplotlib style module
(D:\\OpenFOAM\\plot_style.py: STIX serif, PDF + PNG) never touches the GUI process.

    python -m aethercfd.studio.export_plot <spec.json>
spec: {"npz": path, "out": path-without-extension, "style": path-to-plot_style.py, "x_label": str,
       "title": str|None, "decimate": int}
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    style = Path(spec.get("style") or "")
    save_fig = None
    if style.is_file():
        s = importlib.util.spec_from_file_location("user_plot_style", style)
        mod = importlib.util.module_from_spec(s)
        s.loader.exec_module(mod)  # type: ignore[union-attr]
        if hasattr(mod, "apply_style"):
            mod.apply_style()
        save_fig = getattr(mod, "save_fig", None)
    else:  # bundled fallback: same journal conventions
        plt.rcParams.update({"font.family": "serif", "font.serif": ["STIXGeneral", "DejaVu Serif"],
                             "mathtext.fontset": "stix", "font.size": 9, "axes.linewidth": 0.7,
                             "xtick.direction": "in", "ytick.direction": "in", "xtick.top": True,
                             "ytick.right": True})
    data = np.load(spec["npz"])
    x = data["x"]
    fig, ax = plt.subplots(figsize=(4.6, 3.2))
    step = max(1, int(len(x) / max(1, spec.get("decimate", 4000))))
    for name in data.files:
        if name == "x":
            continue
        y = data[name]
        ax.semilogy(x[::step], y[::step], lw=0.9, label=name)
    ax.set_xlabel(spec.get("x_label", "Iteration"))
    ax.set_ylabel("Initial residual")
    if spec.get("title"):
        ax.set_title(spec["title"])
    ax.legend(ncol=2, frameon=False)
    fig.tight_layout()
    out = spec["out"]
    if save_fig is not None:
        save_fig(fig, out)
    else:
        fig.savefig(out + ".pdf")
        fig.savefig(out + ".png", dpi=300)
    print(json.dumps({"pdf": out + ".pdf", "png": out + ".png"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
