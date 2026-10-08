"""Analysis: modal decomposition of saved fields (POD/DMD) and a surrogate over runs (Gaussian process).

Modes   - POD energy spectrum (how many structures carry the fluctuations) and DMD growth rates and
          frequencies (e.g. which mode makes a "steady" run diverge, and how fast); "Write modes" saves the
          leading mode shapes as fields (journalled) so the 3D viewport can show them.
Surrogate - paste a table of runs (inputs..., output), fit a GP, see mean +- 2 std and the next run to make.
PINN    - physics-informed neural network (PyTorch, out of process, on the GPU): reconstruct a field from
          sparse data with advection-diffusion or Navier-Stokes physics; write it next to the CFD field.
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
        self._build_pinn()
        self.sv.case_changed.connect(lambda _s: (self._fields(), self._pinn_fields()))
        self._fields()
        self._pinn_fields()

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

    # ------------------------------------------------------------------ PINN
    def _build_pinn(self) -> None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QLineEdit

        from aethercfd.studio.widgets.spin import dspin

        w = QWidget()
        lay = QVBoxLayout(w)
        self.p_status = QLabel("checking the PyTorch environment…")
        self.p_status.setWordWrap(True)
        self.p_status.setStyleSheet(f"color: {T.text_dim};")
        lay.addWidget(self.p_status)
        f = QFormLayout()
        self.p_phys = QComboBox()
        for label, key in (("Advection–diffusion of a scalar (CFD velocity)", "ad"),
                           ("Incompressible Navier–Stokes (U, p)", "ns"), ("Data fit only (no physics)", "data")):
            self.p_phys.addItem(label, key)
        self.p_field = QComboBox()
        self.p_coef = dspin(2e-5, 1e-12, 1e3, 9, tip="advection–diffusion: diffusivity D [m²/s]; Navier–Stokes: "
                                                     "kinematic viscosity ν [m²/s]")
        self.p_ndata = QSpinBox(minimum=3, maximum=100000, value=40)
        self.p_csv = QLineEdit(placeholderText="(optional) measurements CSV instead of random CFD cells")
        self.p_region = QLineEdit(placeholderText="(optional) region: xmin ymin zmin xmax ymax zmax [m]")
        self.p_epochs = QSpinBox(minimum=100, maximum=200000, value=4000, singleStep=500)
        self.p_lbfgs = QSpinBox(minimum=0, maximum=20000, value=500, singleStep=100)
        f.addRow("Physics", self.p_phys)
        f.addRow("Field", self.p_field)
        f.addRow("D or ν", self.p_coef)
        f.addRow("Data points (random CFD cells)", self.p_ndata)
        f.addRow("Measurements", self.p_csv)
        f.addRow("Region", self.p_region)
        f.addRow("Adam steps", self.p_epochs)
        f.addRow("L-BFGS steps", self.p_lbfgs)
        lay.addLayout(f)
        r = QHBoxLayout()
        self.p_go = QPushButton(icon("mdi6.brain"), "Train PINN")
        self.p_go.clicked.connect(self.train_pinn)
        self.p_write = QPushButton(icon("msc.save"), "Write fields")
        self.p_write.setEnabled(False)
        self.p_write.clicked.connect(self.write_pinn)
        r.addWidget(self.p_go)
        r.addWidget(self.p_write)
        r.addStretch(1)
        lay.addLayout(r)
        self.p_out = QTextBrowser()
        self.p_out.setMaximumHeight(120)
        lay.addWidget(self.p_out)
        self.p_plot = pg.PlotWidget(background=T.panel)
        self.p_plot.setLogMode(y=True)
        self.p_plot.setLabel("left", "loss")
        self.p_plot.setLabel("bottom", "step")
        lay.addWidget(self.p_plot, 1)
        self.tabs.addTab(w, "PINN")
        self.p_job = None
        self.p_result = None
        self.p_timer = QTimer(self)
        self.p_timer.setInterval(1500)
        self.p_timer.timeout.connect(self._pinn_poll)

        def status():
            from aethercfd.ml.pinn import ml_status

            return ml_status(getattr(self.sv.settings, "ml_python", None))

        def shown(res):
            st, err = res
            if err is not None or not st.get("available"):
                self.p_status.setText(f"PyTorch environment not found ({err or st.get('note')}). Set ml_python in "
                                      "settings or create .ml-env (README).")
                self.p_go.setEnabled(False)
                return
            self.p_status.setText(f"PyTorch {st['torch']} · {st['gpu'] or 'CPU only'} · {st['python']}")

        self.sv.run_bg(status, shown)

    def _pinn_fields(self) -> None:
        self.p_field.clear()
        self.p_field.addItems([self.m_field.itemText(i) for i in range(self.m_field.count())])

    def train_pinn(self) -> None:
        from aethercfd.ml.pinn import build_job, start

        s = self.sv.session
        if s is None:
            return
        phys = self.p_phys.currentData()
        field = self.p_field.currentText()
        region = None
        if self.p_region.text().strip():
            try:
                v = [float(x) for x in self.p_region.text().replace(",", " ").split()]
                region = (v[:3], v[3:6])
            except (ValueError, IndexError):
                self.p_out.setText("region: six numbers, xmin ymin zmin xmax ymax zmax")
                return
        coef = self.p_coef.value()
        params = {"epochs_adam": self.p_epochs.value(), "lbfgs_steps": self.p_lbfgs.value()}
        params["diffusivity" if phys == "ad" else "nu"] = coef
        csv_path = self.p_csv.text().strip()
        self.p_go.setEnabled(False)
        self.p_write.setEnabled(False)
        self.p_out.setText("preparing the job (reading the case)…")
        ml_py = getattr(self.sv.settings, "ml_python", None)

        def work():
            pts = vals = None
            if csv_path:
                from aethercfd.assim.measurements import load_measurements

                m = load_measurements(csv_path)
                if field not in m.values:
                    raise ValueError(f"{Path(csv_path).name} has no column {field!r}")
                pts, vals = m.points, m.values[field]
            job = build_job(s.root, physics=phys, field=field, region=region, n_data=self.p_ndata.value(),
                            data_points=pts, data_values=vals, **params)
            jd = start(job, ml_python=ml_py, job_dir=Path(s.root) / ".aethercfd" / "local" / "pinn" / "last"
                       if s.project else None)
            return job, jd

        def done(res):
            value, err = res
            if err is not None:
                self.p_go.setEnabled(True)
                self.p_out.setText(f"<span style='color:{T.error}'>{err}</span>")
                return
            self.p_job = value
            self.p_out.setText(f"training on {len(value[0].x_data)} data points, {len(value[0].x_colloc)} physics "
                               f"points, {len(value[0].x_eval)} cells…")
            self.p_timer.start()

        self.sv.run_bg(work, done)

    def _pinn_poll(self) -> None:
        from aethercfd.ml.pinn import load, progress

        if self.p_job is None:
            self.p_timer.stop()
            return
        _job, jd = self.p_job
        pr = progress(jd)
        h = pr["history"]
        if h:
            steps = np.arange(len(h))
            self.p_plot.clear()
            self.p_plot.plot(steps, [max(r["data"], 1e-16) for r in h], pen=pg.mkPen(T.accent, width=2), name="data")
            if any(r["pde"] > 0 for r in h):
                self.p_plot.plot(steps, [max(r["pde"], 1e-16) for r in h], pen=pg.mkPen(T.warn, width=2), name="physics")
            last = h[-1]
            self.p_status.setText(f"{last['stage']} step {last['step']} · data {last['data']:.3g} · physics "
                                  f"{last['pde']:.3g} · {last['t']:.0f} s")
        if pr["done"]:
            self.p_timer.stop()
            self.p_go.setEnabled(True)
            res = load(jd)
            self.p_result = res
            if not res.get("ok"):
                self.p_out.setText(f"<span style='color:{T.error}'>{res.get('error')}</span><pre>"
                                   f"{res.get('traceback', '')[-800:]}</pre>")
                return
            errs = res.get("rmse_over_std_vs_cfd") or []
            self.p_out.setHtml(f"done on {res['device']} ({res.get('gpu') or 'CPU'}) in {res['seconds']:.0f} s. "
                               "Error vs the CFD solution (RMSE / std) on the region's cells: "
                               + ", ".join(f"{n} {e:.3g}" for n, e in zip(res["names"], errs, strict=False)
                                           if e is not None) + ".")
            self.p_write.setEnabled(res.get("eval_cells") is not None)

    def write_pinn(self) -> None:
        from aethercfd.commands.base import USER
        from aethercfd.results.casedata import CaseData
        from aethercfd.results.write_fields import field_files

        if self.p_result is None or self.p_job is None or not self.sv.ensure_project():
            return
        job, _jd = self.p_job
        res, s = self.p_result, self.sv.session
        cells, y, t = res["eval_cells"], res["y_eval"], job.params["time"]
        axes = job.params["axes"]
        data = CaseData(s.root)
        out = {}
        if job.physics == "ns":
            base = data.fields(t, ["U"])["U"].copy()
            pred = base.copy()
            for j, a in enumerate(axes):
                pred[cells, a] = y[:, j]
            out["UPinn"], out["UPinnErr"] = pred, pred - base
        else:
            name = job.names[0]
            v = data.fields(t, [name])[name]
            base = (v if v.ndim == 1 else np.linalg.norm(v, axis=1)).copy()
            pred = base.copy()
            pred[cells] = y[:, 0]
            out[f"{name}Pinn"], out[f"{name}PinnErr"] = pred, pred - base
        tname = next((d for d in (f"{t:g}", str(t)) if (Path(s.root) / d).is_dir()
                      or (Path(s.root) / "processor0" / d).is_dir()), f"{t:g}")
        files = field_files(s.root, tname, out, like=job.names[0] if job.physics != "ns" else "U")
        r = s.executor.execute("files.write", {"root": str(s.root), "files": files}, USER)
        self.sv.notify(f"Wrote {', '.join(out)} at t = {tname}" if r.ok else f"Not written: {r.message}",
                       "info" if r.ok else "error")


PANELS =[PanelSpec("aether.analysis", "Analysis", "mdi6.chart-bell-curve", "Results", AnalysisPanel,
                    default_area=Area.RIGHT, description="POD/DMD modes of saved fields; GP surrogate over runs")]
