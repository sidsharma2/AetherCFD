"""Figures of the run report, drawn in a SUBPROCESS (the user's matplotlib style never touches the GUI):

    python -m aethercfd.results.report_plots <spec.json>

spec: {"npz": path, "out_dir": dir, "style": plot_style.py or "", "figures": [
        {"name": "forces", "y_label": "...", "x_label": "Iteration", "logy": false, "window": t0 | null,
         "series": [{"key": "<npz array prefix>", "label": "..."}]}]}
Each series is stored as <key>_t and <key>_v in the npz. ``window`` shades the part of the run the convergence
gate judged. Writes <name>.pdf and <name>.png per figure and prints {"figures": [...]} as JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_MATHTEXT = {"⁴": "$^4$", "³": "$^3$", "²": "$^2$", "ρ": r"$\rho$", "Δ": r"$\Delta$"}


def tex(label: str) -> str:
    """Unicode superscripts and Greek as mathtext: STIX text fonts lack some of them (⁴ draws as a box)."""
    for k, v in _MATHTEXT.items():
        label = label.replace(k, v)
    return label


def focus_tail(ax, series) -> None:
    """A start-up transient 100x the converged value flattens the part of the run that matters: when the full
    range is over 5x the range after the first 20 % of the samples, zoom the y axis on that later part."""
    import numpy as np

    full, tail = [], []
    for _t, v in series:
        v = np.asarray(v, dtype=float)
        if len(v) < 10:
            return
        full.append(v)
        tail.append(v[len(v) // 5:])
    lo, hi = min(x.min() for x in full), max(x.max() for x in full)
    tlo, thi = min(x.min() for x in tail), max(x.max() for x in tail)
    span = thi - tlo or abs(thi) or 1.0
    if hi - lo > 5 * span:
        ax.set_ylim(tlo - 0.15 * span, thi + 0.15 * span)


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    from aethercfd.results.plotstyle import load_style

    save_fig = load_style(spec.get("style"))
    import matplotlib.pyplot as plt
    import numpy as np

    data = np.load(spec["npz"])
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for fig_spec in spec["figures"]:
        fig, ax = plt.subplots(figsize=(4.6, 3.2))
        drawn = 0
        for s in fig_spec["series"]:
            t = data[f"{s['key']}_t"]
            v = data[f"{s['key']}_v"]
            if fig_spec.get("logy"):
                keep = v > 0
                t, v = t[keep], v[keep]
                if not len(v):
                    continue
                ax.semilogy(t, v, lw=0.9, label=tex(s["label"]))
            else:
                ax.plot(t, v, lw=0.9, label=tex(s["label"]))
            drawn += 1
        if not drawn:
            plt.close(fig)
            continue
        if not fig_spec.get("logy"):
            focus_tail(ax, [(data[f"{s['key']}_t"], data[f"{s['key']}_v"]) for s in fig_spec["series"]])
        if fig_spec.get("window") is not None:
            t_end = max(float(data[f"{s['key']}_t"][-1]) for s in fig_spec["series"])
            ax.axvspan(fig_spec["window"], t_end, color="0.85", lw=0, zorder=0, label="judged window")
        if not fig_spec.get("logy"):
            ax.axhline(0.0, color="0.6", lw=0.5, zorder=0)
        ax.set_xlabel(fig_spec.get("x_label", "Iteration"))
        ax.set_ylabel(tex(fig_spec.get("y_label", "")))
        ax.legend(ncol=2 if drawn > 3 else 1, frameon=False, fontsize="small")
        fig.tight_layout()
        out = str(out_dir / fig_spec["name"])
        if save_fig is not None:
            save_fig(fig, out)
        else:
            fig.savefig(out + ".pdf")
            fig.savefig(out + ".png", dpi=300)
        plt.close(fig)
        made.append(fig_spec["name"])
    print(json.dumps({"figures": made}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
