"""Publication export of a residual plot, run in a SUBPROCESS so the user's matplotlib style module
(D:\\OpenFOAM\\plot_style.py: STIX serif, PDF + PNG) never touches the GUI process.

    python -m aethercfd.studio.export_plot <spec.json>
spec: {"npz": path, "out": path-without-extension, "style": path-to-plot_style.py, "x_label": str,
       "title": str|None, "decimate": int}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(spec_path: str) -> int:
    from aethercfd.results.plotstyle import load_style

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    save_fig = load_style(spec.get("style"))
    import matplotlib.pyplot as plt
    import numpy as np

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
