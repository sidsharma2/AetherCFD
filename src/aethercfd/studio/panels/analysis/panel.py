"""Analysis: modal decomposition of saved fields (POD/DMD) and a surrogate over runs (Gaussian process).

Modes   - POD energy spectrum (how many structures carry the fluctuations) and DMD growth rates and
          frequencies (e.g. which mode makes a "steady" run diverge, and how fast); "Write modes" saves the
          leading mode shapes as fields (journalled) so the 3D viewport can show them.
Surrogate - paste a table of runs (inputs..., output), fit a GP, see mean +- 2 std and the next run to make.
PINN    - physics-informed neural network (PyTorch, out of process, on the GPU): reconstruct a field from
          sparse data with advection-diffusion or Navier-Stokes physics; write it next to the CFD field.
          A field reconstruction, not a CFD solution: every result shows its error on held-out points.
          Without a PyTorch environment the tab says how to make one (scripts/setup_ml_env.py).
"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
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
from aethercfd.studio.widgets.region_picker import RegionPicker


class AnalysisPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.last = None
        self.m_busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.region = RegionPicker()  # multi-region (CHT) case: which region every tab reads
        self.region.changed.connect(lambda _r: self._on_region())
        lay.addWidget(self.region)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._build_modes()
        self._build_surrogate()
        self._build_pinn()
        self.sv.case_changed.connect(self._case_changed_slot)
        self.region.reload(self.sv.session.root if self.sv.session else None)
        self._fields()
        self._pinn_fields()

    def _case_changed_slot(self, s=None) -> None:
        """services.case_changed -> here; a bound slot, so it is disconnected when the panel is deleted."""
        self._on_case()

    def _on_case(self) -> None:
        """Results computed on the previous case's mesh must never be written into this one."""
        self.last = None
        self.m_write.setEnabled(False)
        self._cancel_running()
        self.p_job = self.p_result = None
        self._pinn_written = None
        self.p_write.setEnabled(False)
        self.p_compare.setEnabled(False)
        self.p_stop.setEnabled(False)
        self.p_go.setEnabled(self.p_env_ok)
        self.region.reload(self.sv.session.root if self.sv.session else None)
        self._fields()
        self._pinn_fields()

    def _on_region(self) -> None:
        """Modes of one region cannot be written into another: analyse again; the field lists follow."""
        self.last = None
        self.m_write.setEnabled(False)
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
            self.m_field.addItems(CaseData(s.root, self.region.region()).field_names())
        except Exception as exc:
            self.m_out.setText(f"could not read the fields: {exc}")

    @staticmethod
    def _snapshots(root, name: str, comp: str, region: str = ""):
        from aethercfd.results.casedata import CaseData

        data = CaseData(root, region)
        ts, X = data.snapshots(name)
        if X.ndim == 3:
            X = np.linalg.norm(X, axis=2) if comp == "magnitude" else X[:, :, "xyz".index(comp)]
        return data, ts, X

    def analyse(self) -> bool:
        """Reads every saved time and decomposes in the background (large cases take a while); the result
        appears in the panel. False when nothing was started."""
        from aethercfd.results.modes import dmd, pod

        if self.sv.session is None or self.m_busy or not self.m_field.currentText():
            if not self.m_field.currentText():
                self.m_out.setText("No field to analyse (the case has no saved results?)")
            return False
        method, n, delays = self.m_method.currentText(), self.m_n.value(), self.m_delays.value()
        name, root, comp = self.m_field.currentText(), self.sv.session.root, self.m_comp.currentText()
        region = self.region.region()
        where = f" (region {html.escape(region)})" if region else ""

        def work():
            data, ts, X = self._snapshots(root, name, comp, region)
            need = 2 if method == "POD" else 3
            if len(ts) < need:
                raise ValueError(f"{method} needs at least {need} saved times with {name}; this case has {len(ts)}")
            if method == "POD":
                try:
                    vol = data.quality().cell_volume
                except Exception:
                    vol = None
                return ts, pod(X, weights=vol, n_modes=n)
            dt = float(np.median(np.diff(ts)))
            return ts, dmd(X, dt=dt, rank=n, delays=delays)

        def done(res):
            self.m_busy = False
            value, err = res
            if self.sv.session is None or self.sv.session.root != root:
                return  # the case changed meanwhile
            if err is not None:
                self.m_out.setText(f"<span style='color:{T.error}'>{html.escape(str(err))}</span>")
                return
            ts, r = value
            self.m_plot.clear()
            self.last = (method, ts, r, name, root, region)
            if method == "POD":
                self.m_plot.addItem(pg.BarGraphItem(x=np.arange(1, len(r.energy) + 1), height=100 * r.energy,
                                                    width=0.6, brush=T.accent))
                self.m_plot.setLabel("left", "energy [%]")
                self.m_plot.setLabel("bottom", "mode")
                self.m_out.setHtml(f"POD of <b>{html.escape(name)}</b>{where} over {len(ts)} saved times ({r.backend}): "
                                   f"{r.modes_for(0.9)} mode(s) hold 90 %, {r.modes_for(0.99)} hold 99 % of the "
                                   "fluctuation energy." + "".join(f"<br>{html.escape(x)}" for x in r.notes))
            else:
                dt = float(np.median(np.diff(ts)))
                self.m_plot.plot(r.frequency, r.growth_rate, pen=None, symbol="o", symbolSize=8, symbolBrush=T.accent)
                self.m_plot.setLabel("left", "growth rate [1/time]")
                self.m_plot.setLabel("bottom", "frequency [1/time]")
                o = np.argsort(-r.growth_rate)
                rows = "".join(f"<tr><td>{i + 1}</td><td>{r.growth_rate[k]:.4g}</td><td>{abs(r.frequency[k]):.4g}"
                               f"</td><td>{r.amplitudes[k]:.3g}</td></tr>" for i, k in enumerate(o[:8]))
                self.m_out.setHtml(f"DMD of <b>{html.escape(name)}</b>{where} over {len(ts)} snapshots (Δt {dt:g}, "
                                   f"{r.backend}). Growth > 0 grows.<table cellspacing=6><tr><th>#</th><th>growth</th>"
                                   f"<th>frequency</th><th>amplitude</th></tr>{rows}</table>"
                                   + "".join(f"<br>{html.escape(x)}" for x in r.notes))
            self.m_write.setEnabled(True)

        self.m_busy = True
        self.m_out.setText(f"reading every saved time of {name} and computing {method}…")
        self.sv.run_bg(work, done)
        return True

    def write_modes(self) -> None:
        from aethercfd.commands.base import USER
        from aethercfd.results.write_fields import field_files

        if self.last is None or not self.sv.ensure_project():
            return
        kind, ts, r, name, root, region = self.last
        s = self.sv.session
        if s is None or s.root != root:
            self.sv.notify("The modes belong to another case: analyse this one first", "warn")
            return
        modes = r.modes if kind == "POD" else np.real(r.modes)
        k = min(len(modes), 4)
        t = f"{ts[-1]:g}"
        if not ((Path(s.root) / t).is_dir() or (Path(s.root) / "processor0" / t).is_dir()):
            t = str(ts[-1])
        files = field_files(s.root, t, {f"{name}{kind}{i + 1}": np.asarray(modes[i]).reshape(-1)[:modes.shape[1]]
                                        for i in range(k)}, region=region)
        res = s.executor.execute("files.write", {"root": str(s.root), "files": files}, USER)
        where = f" in region {region}" if region else ""
        self.sv.notify(f"Wrote {k} {kind} mode fields at t = {t}{where}" if res.ok else f"Not written: {res.message}",
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
        bad = ~np.isfinite(A).all(axis=1)
        if bad.any():
            A = A[~bad]
            if len(A) < 2:
                self.s_out.setText("Need at least two rows of finite numbers (NaN/inf rows are skipped)")
                return None
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
                           + "".join(f"<br>{n}" for n in gp.notes)
                           + (f"<br><span style='color:{T.warn}'>{int(bad.sum())} row(s) with NaN/inf skipped</span>"
                              if bad.any() else ""))
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
        self.p_region = QLineEdit(placeholderText="(optional) box: xmin ymin zmin xmax ymax zmax [m]")
        self.p_epochs = QSpinBox(minimum=100, maximum=200000, value=4000, singleStep=500)
        self.p_lbfgs = QSpinBox(minimum=0, maximum=20000, value=500, singleStep=100)
        f.addRow("Physics", self.p_phys)
        f.addRow("Field", self.p_field)
        f.addRow("D or ν", self.p_coef)
        f.addRow("Data points (random CFD cells)", self.p_ndata)
        self.p_unit = QComboBox()
        self.p_unit.addItems(["m", "mm", "cm", "um", "in"])
        self.p_unit.setToolTip("length unit of the CSV coordinates")
        csv_row = QHBoxLayout()
        csv_row.addWidget(self.p_csv, 1)
        csv_row.addWidget(self.p_unit)
        f.addRow("Measurements", csv_row)
        f.addRow("Box", self.p_region)
        f.addRow("Adam steps", self.p_epochs)
        f.addRow("L-BFGS steps", self.p_lbfgs)
        self.p_seed = QSpinBox(minimum=0, maximum=2**31 - 1, value=0)
        self.p_seed.setToolTip("random seed for the data, held-out and collocation points and the network "
                               "(saved with the result)")
        f.addRow("Seed", self.p_seed)
        lay.addLayout(f)
        r = QHBoxLayout()
        self.p_go = QPushButton(icon("mdi6.brain"), "Train PINN")
        self.p_go.clicked.connect(self.train_pinn)
        self.p_stop = QPushButton(icon("msc.debug-stop"), "Stop")
        self.p_stop.setToolTip("stop the training (the worker process ends; nothing is written)")
        self.p_stop.setEnabled(False)
        self.p_stop.clicked.connect(self.stop_pinn)
        self.p_recheck = QPushButton(icon("msc.refresh"), "Check again")
        self.p_recheck.setToolTip("look for the PyTorch environment again (after running scripts/setup_ml_env.py)")
        self.p_recheck.clicked.connect(self.check_ml_env)
        self.p_write = QPushButton(icon("msc.save"), "Write fields")
        self.p_write.setEnabled(False)
        self.p_write.clicked.connect(self.write_pinn)
        self.p_compare = QPushButton(icon("mdi6.view-split-vertical"), "Compare in 3D")
        self.p_compare.setToolTip("CFD | PINN | difference in linked viewports with one colour range")
        self.p_compare.setEnabled(False)
        self.p_compare.clicked.connect(self.compare_pinn)
        r.addWidget(self.p_go)
        r.addWidget(self.p_stop)
        r.addWidget(self.p_recheck)
        r.addStretch(1)
        lay.addLayout(r)
        r2 = QHBoxLayout()  # a second row: five buttons in one row were cut off in a side dock
        r2.addWidget(self.p_write)
        r2.addWidget(self.p_compare)
        r2.addStretch(1)
        lay.addLayout(r2)
        self.p_out = QTextBrowser()
        self.p_out.setMaximumHeight(160)
        lay.addWidget(self.p_out)
        self.p_plot = pg.PlotWidget(background=T.panel)
        self.p_plot.setLogMode(y=True)
        self.p_plot.setLabel("left", "loss · held-out RMSE/std")
        self.p_plot.setLabel("bottom", "log point")
        self.p_plot.addLegend(offset=(-10, 10))
        lay.addWidget(self.p_plot, 1)
        self.tabs.addTab(w, "PINN")
        self.p_job = None
        self.p_result = None
        self.p_env_ok = False
        self.p_timer = QTimer(self)
        self.p_timer.setInterval(1500)
        self.p_timer.timeout.connect(self._pinn_poll)
        self.check_ml_env()

    def check_ml_env(self) -> None:
        """Find and test the PyTorch environment in the background; without one, say how to get it."""
        self.p_status.setText("checking the PyTorch environment…")
        self.p_go.setEnabled(False)
        ml_py = getattr(self.sv.settings, "ml_python", None)

        def status():
            from aethercfd.ml.pinn import ml_status

            return ml_status(ml_py)

        def shown(res):
            from aethercfd.ml.pinn import SETUP_HINT

            st, err = res
            self.p_env_ok = err is None and bool(st.get("available"))
            busy = self.p_job is not None and self.p_timer.isActive()
            if not self.p_env_ok:
                why = f"{type(err).__name__}: {err}" if err is not None else st.get("note")
                self.p_status.setText("PyTorch environment not found or not working, so PINN training is not "
                                      f"available: {why}\n{st.get('hint') or SETUP_HINT}")
                self.p_status.setStyleSheet(f"color: {T.warn};")
                self.p_go.setEnabled(False)
                return
            self.p_status.setStyleSheet(f"color: {T.text_dim};")
            self.p_status.setText(f"PyTorch {st['torch']} · {st['gpu'] or 'CPU only'} · {st['python']}")
            self.p_go.setEnabled(not busy)

        self.sv.run_bg(status, shown)

    def _pinn_fields(self) -> None:
        self.p_field.clear()
        self.p_field.addItems([self.m_field.itemText(i) for i in range(self.m_field.count())])

    def train_pinn(self) -> None:
        from aethercfd.ml.pinn import build_job, parse_region, start

        s = self.sv.session
        if s is None:
            return
        phys = self.p_phys.currentData()
        field = self.p_field.currentText()
        try:
            box = parse_region(self.p_region.text())
        except ValueError as exc:
            self.p_out.setText(str(exc))
            return
        coef = self.p_coef.value()
        params = {"epochs_adam": self.p_epochs.value(), "lbfgs_steps": self.p_lbfgs.value(),
                  "seed": self.p_seed.value()}
        params["diffusivity" if phys == "ad" else "nu"] = coef
        csv_path, csv_unit = self.p_csv.text().strip(), self.p_unit.currentText()
        region = self.region.region()
        self.p_go.setEnabled(False)
        self.p_write.setEnabled(False)
        self.p_out.setText("preparing the job (reading the case)…")
        ml_py = getattr(self.sv.settings, "ml_python", None)

        def work():
            pts = vals = None
            if csv_path:
                from aethercfd.assim.measurements import load_measurements

                m = load_measurements(csv_path, length_unit=csv_unit)
                if field not in m.values:
                    raise ValueError(f"{Path(csv_path).name} has no column {field!r}")
                pts, vals = m.points, m.values[field]
            job = build_job(s.root, physics=phys, field=field, box=box, region=region, n_data=self.p_ndata.value(),
                            data_points=pts, data_values=vals, **params)
            jd = start(job, ml_python=ml_py, job_dir=Path(s.root) / ".aethercfd" / "local" / "pinn" / "last"
                       if s.project else None)
            return job, jd

        def done(res):
            value, err = res
            if err is not None:
                self.p_go.setEnabled(self.p_env_ok)
                self.p_out.setText(f"<span style='color:{T.error}'>{html.escape(str(err))}</span>")
                return
            if self.sv.session is None or self.sv.session.root != s.root:
                from aethercfd.ml.pinn import cancel

                self.sv.run_bg(lambda: cancel(value[1]))  # the case changed while the job was being prepared
                self.p_go.setEnabled(self.p_env_ok)
                return
            self.p_job = value
            self.p_stop.setEnabled(True)
            self.p_out.setText(f"training on {len(value[0].x_data)} data points, {len(value[0].x_colloc)} physics "
                               f"points, {len(value[0].x_eval)} cells" + (f" of region {region}" if region else "") +
                               (f"; {len(value[0].x_val)} held-out points" if value[0].x_val is not None else "") + "…")
            self.p_timer.start()

        self.sv.run_bg(work, done)

    def stop_pinn(self) -> None:
        """Ask the worker to stop (killed after a grace period, off the GUI thread); the poll shows the end."""
        from aethercfd.ml.pinn import cancel

        if self.p_job is None:
            return
        self.p_stop.setEnabled(False)
        self.p_out.setText("stopping the PINN…")
        jd = self.p_job[1]
        self.sv.run_bg(lambda: cancel(jd))

    def _cancel_running(self, grace_s: float = 5.0, *, background: bool = True) -> None:
        """A run of the previous case, or of a closing panel, must not train on unseen."""
        from aethercfd.ml.pinn import cancel

        self.p_timer.stop()
        if self.p_job is None:
            return
        jd = self.p_job[1]
        if background:
            self.sv.run_bg(lambda: cancel(jd, grace_s))
        else:
            cancel(jd, grace_s)

    def dispose(self) -> None:
        self._cancel_running(1.0, background=False)

    def _pinn_poll(self) -> None:
        try:
            self._pinn_poll_once()
        except Exception as exc:  # never a toast every 1.5 s: stop and say why
            self.p_timer.stop()
            self.p_go.setEnabled(self.p_env_ok)
            self.p_stop.setEnabled(False)
            self.p_out.setText(f"<span style='color:{T.error}'>following the PINN failed: "
                               f"{html.escape(f'{type(exc).__name__}: {exc}')}</span>")

    def _pinn_poll_once(self) -> None:
        from aethercfd.ml.pinn import load, progress, summary

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
            val = [float(np.mean(vs)) if (vs := [v for v in r.get("val") or [] if v is not None]) else np.nan
                   for r in h]  # mean held-out RMSE/std over the outputs
            if np.isfinite(val).any():
                self.p_plot.plot(steps, np.maximum(val, 1e-16), pen=pg.mkPen(T.text, width=2, style=Qt.PenStyle.DashLine),
                                 name="held-out RMSE/std")
            last = h[-1]
            held = f" · held-out {val[-1]:.3g}" if np.isfinite(val[-1]) else ""
            where = "final" if last["stage"] == "final" else f"{last['stage']} step {last['step']}"
            self.p_status.setText(f"{where} · data {last['data']:.3g} · physics "
                                  f"{last['pde']:.3g}{held} · {last['t']:.0f} s")
        if pr["done"]:
            self.p_timer.stop()
            self.p_go.setEnabled(self.p_env_ok)
            self.p_stop.setEnabled(False)
            res = load(jd)
            self.p_result = res
            if not res.get("ok"):
                tb = res.get("traceback", "") if res.get("reason") == "crash" else ""
                self.p_out.setHtml(f"<span style='color:{T.error}'>Error: "
                                   f"{html.escape(str(res.get('error')))}</span>"
                                   + (f"<pre>{html.escape(tb[-800:])}</pre>" if tb else ""))
                return
            self.p_out.setHtml("<br>".join(html.escape(x) for x in summary(res)))
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
        axes, region = job.params["axes"], job.params.get("region", "")
        data = CaseData(s.root, region)
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
        from aethercfd.ml.pinn import record_file

        files = field_files(s.root, tname, out, like=job.names[0] if job.physics != "ns" else "U", region=region)
        files |= record_file(res, "U" if job.physics == "ns" else job.names[0], tname, region)  # seed, errors, ...
        r = s.executor.execute("files.write", {"root": str(s.root), "files": files}, USER)
        self.sv.notify(f"Wrote {', '.join(out)} at t = {tname} (a PINN reconstruction; its record is in "
                       ".aethercfd/pinn)" if r.ok else f"Not written: {r.message}", "info" if r.ok else "error")
        if r.ok:
            self._pinn_written = (list(out), tname, job)
            self.p_compare.setEnabled(True)

    def compare_pinn(self) -> str:
        from aethercfd.studio.app.automation import AppAutomation

        names, tname, job = self._pinn_written
        base = "U" if job.physics == "ns" else job.names[0]
        return AppAutomation(self.sv).compare_fields(base, names[0], names[1], tname)


PANELS =[PanelSpec("aether.analysis", "Analysis", "mdi6.chart-bell-curve", "Results", AnalysisPanel,
                    default_area=Area.RIGHT, description="POD/DMD modes of saved fields; GP surrogate over runs; "
                                                         "PINN field reconstruction from sparse data")]
