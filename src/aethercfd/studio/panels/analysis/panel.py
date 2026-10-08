"""Analysis: modal decomposition of saved fields (POD/DMD) and a surrogate over runs (Gaussian process).

Modes   - POD energy spectrum (how many structures carry the fluctuations) and DMD growth rates and
          frequencies (e.g. which mode makes a "steady" run diverge, and how fast); "Write modes" saves the
          leading mode shapes as fields (journalled) so the 3D viewport can show them.
Surrogate - paste a table of runs (inputs..., output), fit a GP, see mean +- 2 std and the next run to make.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T


class AnalysisPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.last = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._build_modes()
        self._build_surrogate()
        self.sv.case_changed.connect(lambda _s: self._fields())
        self._fields()

    # ------------------------------------------------------------------ modes
    def _build_modes(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.m_field = QComboBox()
        self.m_comp = QComboBox()
        self.m_comp.addItems(["magnitude", "x", "y", "z"])
        self.m_method = QComboBox()
        self.m_method.addItems(["POD", "DMD"])
        self.m_n = QSpinBox(minimum=1, maximum=50, value=6)
        self.m_delays = QSpinBox(minimum=1, maximum=50, value=1)
        self.m_delays.setToolTip("DMD time delays: > 1 when a structure oscillates in place (standing waves)")
        f.addRow("Field", self.m_field)
        f.addRow("Vector component", self.m_comp)
        f.addRow("Method", self.m_method)
        f.addRow("Modes / rank", self.m_n)
        f.addRow("DMD delays", self.m_delays)
        lay.addLayout(f)
        r = QHBoxLayout()
        go = QPushButton(icon("mdi6.chart-bell-curve"), "Analyse saved times")
        go.clicked.connect(self.analyse)
        self.m_write = QPushButton(icon("msc.save"), "Write modes as fields")
        self.m_write.setEnabled(False)
        self.m_write.clicked.connect(self.write_modes)
        r.addWidget(go)
        r.addWidget(self.m_write)
        r.addStretch(1)
        lay.addLayout(r)
        self.m_out = QTextBrowser()
        self.m_out.setMaximumHeight(150)
        lay.addWidget(self.m_out)
        self.m_plot = pg.PlotWidget(background=T.panel)
        lay.addWidget(self.m_plot, 1)
        self.tabs.addTab(w, "Modes (POD / DMD)")

    def _fields(self) -> None:
        self.m_field.clear()
        s = self.sv.session
        if s is None:
            return
        from aethercfd.results.casedata import CaseData

        try:
            self.m_field.addItems(CaseData(s.root).field_names())
        except Exception as exc:
            self.m_out.setText(f"could not read the fields: {exc}")

    def _snapshots(self):
        from aethercfd.results.casedata import CaseData

        data = CaseData(self.sv.session.root)
        ts, X = data.snapshots(self.m_field.currentText())
        if X.ndim == 3:
            comp = self.m_comp.currentText()
            X = np.linalg.norm(X, axis=2) if comp == "magnitude" else X[:, :, "xyz".index(comp)]
        return data, ts, X

    def analyse(self):
        from aethercfd.results.modes import dmd, pod

        if self.sv.session is None:
            return None
        try:
            data, ts, X = self._snapshots()
        except Exception as exc:
            self.m_out.setText(f"<span style='color:{T.error}'>{exc}</span>")
            return None
        self.m_plot.clear()
        if self.m_method.currentText() == "POD":
            try:
                vol = data.quality().cell_volume
            except Exception:
                vol = None
            r = pod(X, weights=vol, n_modes=self.m_n.value())
            self.last = ("POD", ts, r)
            self.m_plot.addItem(pg.BarGraphItem(x=np.arange(1, len(r.energy) + 1), height=100 * r.energy, width=0.6,
                                                brush=T.accent))
            self.m_plot.setLabel("left", "energy [%]")
            self.m_plot.setLabel("bottom", "mode")
            self.m_out.setHtml(f"POD of <b>{self.m_field.currentText()}</b> over {len(ts)} saved times ({r.backend}): "
                               f"{r.modes_for(0.9)} mode(s) hold 90 %, {r.modes_for(0.99)} hold 99 % of the fluctuation "
                               "energy." + "".join(f"<br>{n}" for n in r.notes))
        else:
            dt = float(np.median(np.diff(ts))) if len(ts) > 1 else 1.0
            r = dmd(X, dt=dt, rank=self.m_n.value(), delays=self.m_delays.value())
            self.last = ("DMD", ts, r)
            self.m_plot.plot(r.frequency, r.growth_rate, pen=None, symbol="o", symbolSize=8, symbolBrush=T.accent)
            self.m_plot.setLabel("left", "growth rate [1/time]")
            self.m_plot.setLabel("bottom", "frequency [1/time]")
            o = np.argsort(-r.growth_rate)
            rows = "".join(f"<tr><td>{i + 1}</td><td>{r.growth_rate[k]:.4g}</td><td>{abs(r.frequency[k]):.4g}</td>"
                           f"<td>{r.amplitudes[k]:.3g}</td></tr>" for i, k in enumerate(o[:8]))
            self.m_out.setHtml(f"DMD over {len(ts)} snapshots (Δt {dt:g}, {r.backend}). Growth > 0 grows."
                               f"<table cellspacing=6><tr><th>#</th><th>growth</th><th>frequency</th><th>amplitude</th>"
                               f"</tr>{rows}</table>" + "".join(f"<br>{n}" for n in r.notes))
        self.m_write.setEnabled(True)
        return r

    def write_modes(self) -> None:
        from aethercfd.commands.base import USER
        from aethercfd.results.write_fields import field_files

        if self.last is None or not self.sv.ensure_project():
            return
        kind, ts, r = self.last
        s = self.sv.session
        name = self.m_field.currentText()
        modes = r.modes if kind == "POD" else np.real(r.modes)
        k = min(len(modes), 4)
        t = f"{ts[-1]:g}"
        if not ((Path(s.root) / t).is_dir() or (Path(s.root) / "processor0" / t).is_dir()):
            t = str(ts[-1])
        files = field_files(s.root, t, {f"{name}{kind}{i + 1}": np.asarray(modes[i]).reshape(-1)[:modes.shape[1]]
                                        for i in range(k)})
        res = s.executor.execute("files.write", {"root": str(s.root), "files": files}, USER)
        self.sv.notify(f"Wrote {k} {kind} mode fields at t = {t}" if res.ok else f"Not written: {res.message}",
                       "info" if res.ok else "error")

    # ------------------------------------------------------------------ surrogate
    def _build_surrogate(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Runs, one per line: inputs…, output (header optional). E.g. outlet pressure [kPa], "
                             "dilution ratio"))
        self.s_table = QPlainTextEdit("p_kPa, DR\n300, 6.74\n310, 6.80\n320, 6.82\n330, 6.80\n340, 6.74")
        self.s_table.setMaximumHeight(130)
        lay.addWidget(self.s_table)
        r = QHBoxLayout()
        go = QPushButton(icon("mdi6.chart-bell-curve-cumulative"), "Fit surrogate")
        go.clicked.connect(self.fit_surrogate)
        r.addWidget(go)
        r.addStretch(1)
        lay.addLayout(r)
        self.s_out = QTextBrowser()
        self.s_out.setMaximumHeight(110)
        lay.addWidget(self.s_out)
        self.s_plot = pg.PlotWidget(background=T.panel)
        lay.addWidget(self.s_plot, 1)
        self.tabs.addTab(w, "Surrogate (runs)")

    def fit_surrogate(self):
        from aethercfd.ml.surrogate import GPSurrogate

        rows, header = [], None
        for ln in self.s_table.toPlainText().splitlines():
            parts = [p.strip() for p in ln.replace(";", ",").replace("\t", ",").split(",") if p.strip()]
            if not parts:
                continue
            try:
                rows.append([float(p) for p in parts])
            except ValueError:
                header = header or parts
        if len(rows) < 2 or len({len(r) for r in rows}) != 1 or len(rows[0]) < 2:
            self.s_out.setText("Need at least two rows with the same number of columns (inputs…, output)")
            return None
        A = np.array(rows)
        X, y = A[:, :-1], A[:, -1]
        gp = GPSurrogate().fit(X, y)
        lo, hi = X.min(0), X.max(0)
        pad = 0.15 * (hi - lo)
        nxt = gp.suggest(list(zip(lo, hi, strict=True)))
        names = header[:-1] if header and len(header) == A.shape[1] else [f"x{i + 1}" for i in range(X.shape[1])]
        out = header[-1] if header and len(header) == A.shape[1] else "output"
        self.s_plot.clear()
        if X.shape[1] == 1:
            xs = np.linspace(lo[0] - pad[0], hi[0] + pad[0], 200)
            m, sd = gp.predict(xs[:, None])
            self.s_plot.plot(xs, m, pen=pg.mkPen(T.accent, width=2))
            self.s_plot.plot(xs, m + 2 * sd, pen=pg.mkPen(T.text_faint))
            self.s_plot.plot(xs, m - 2 * sd, pen=pg.mkPen(T.text_faint))
            self.s_plot.plot(X[:, 0], y, pen=None, symbol="o", symbolBrush=T.warn)
            self.s_plot.setLabel("bottom", names[0])
            self.s_plot.setLabel("left", out)
        ls = ", ".join(f"{n} {v:.3g}" for n, v in zip(names, gp.length_scales(), strict=True))
        self.s_out.setHtml(f"GP over {len(y)} runs. Correlation lengths: {ls} (short = the output changes fast with "
                           f"that input). Next run to reduce the uncertainty most: <b>"
                           + ", ".join(f"{n} = {v:.4g}" for n, v in zip(names, nxt, strict=True)) + "</b>."
                           "<br>Outside the sampled range the GP reverts to its prior: do not trust extrapolation."
                           + "".join(f"<br>{n}" for n in gp.notes))
        return gp


PANELS = [PanelSpec("aether.analysis", "Analysis", "mdi6.chart-bell-curve", "Results", AnalysisPanel,
                    default_area=Area.RIGHT, description="POD/DMD modes of saved fields; GP surrogate over runs")]
