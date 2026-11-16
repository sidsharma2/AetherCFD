"""Measurements: experiment vs simulation, and assimilation (a first CFD-measurement-integration step).

Load a CSV of points and measured values, compare with the simulation at a time (statistics, a
simulated-vs-measured scatter), then assimilate: optimal interpolation gives the corrected field and where
the data constrained it (posterior std). "Write fields" saves <field>Assim / <field>AssimCorr /
<field>AssimStd next to the solution (journalled) so the 3D viewport shows them.
"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T
from aethercfd.studio.widgets.region_picker import RegionPicker
from aethercfd.studio.widgets.spin import dspin


class MeasurementsPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.meas = None
        self.last = None  # (time, field, Assimilation, region)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.region = RegionPicker()  # multi-region (CHT) case: the region the measurements are compared with
        self.region.changed.connect(lambda _r: self._on_region())
        lay.addWidget(self.region)
        row = QHBoxLayout()
        self.csv = QLineEdit(placeholderText="measurements CSV: x, y, z, <field>[, <field>_std]")
        b = QPushButton(icon("msc.folder-opened"), "")
        b.clicked.connect(self._browse)
        self.unit = QComboBox()
        self.unit.addItems(["m", "mm", "cm"])
        load = QPushButton("Load")
        load.clicked.connect(self.load)
        for w in (self.csv, b, QLabel("coords in"), self.unit, load):
            row.addWidget(w, 1 if w is self.csv else 0)
        lay.addLayout(row)
        f = QFormLayout()
        self.field = QComboBox()
        self.time = QComboBox()
        self.sigma_b = dspin(10.0, 1e-12, 1e12, 6)
        self.length = dspin(0.002, 1e-9, 1e3, 6, suffix=" m")
        self.sigma_o = dspin(0.0, 0, 1e12, 6, special="from CSV / 1% of value")
        f.addRow("Field", self.field)
        f.addRow("Time", self.time)
        f.addRow("Background std σb (how wrong the model may be)", self.sigma_b)
        f.addRow("Correlation length L", self.length)
        f.addRow("Measurement std σo", self.sigma_o)
        lay.addLayout(f)
        r2 = QHBoxLayout()
        cmp_ = QPushButton(icon("mdi6.compare"), "Compare")
        cmp_.clicked.connect(self.compare)
        asm = QPushButton(icon("mdi6.merge"), "Assimilate")
        asm.clicked.connect(self.assimilate)
        self.write = QPushButton(icon("msc.save"), "Write fields")
        self.write.setEnabled(False)
        self.write.clicked.connect(self.write_fields)
        for w in (cmp_, asm, self.write):
            r2.addWidget(w)
        r2.addStretch(1)
        lay.addLayout(r2)
        self.out = QTextBrowser()
        self.out.setMaximumHeight(170)
        lay.addWidget(self.out)
        self.plot = pg.PlotWidget(background=T.panel)
        self.plot.setLabel("bottom", "measured")
        self.plot.setLabel("left", "simulated")
        lay.addWidget(self.plot, 1)
        self.sv.case_changed.connect(self._case_changed_slot)
        self.region.reload(self.sv.session.root if self.sv.session else None)
        self._times()

    def _case_changed_slot(self, s=None) -> None:
        """services.case_changed -> here; a bound slot, so it is disconnected when the panel is deleted."""
        self._on_case()

    def _on_case(self) -> None:
        # an assimilated field belongs to the mesh it was computed on: never write it into another case
        self.last = None
        self.write.setEnabled(False)
        self.region.reload(self.sv.session.root if self.sv.session else None)
        self._times()

    def _on_region(self) -> None:
        self.last = None  # ... nor into another region
        self.write.setEnabled(False)

    def _data(self):
        from aethercfd.results.casedata import CaseData

        s = self.sv.session
        return CaseData(s.root, self.region.region()) if s is not None else None

    def _times(self) -> None:
        self.time.clear()
        d = self._data()
        if d is None:
            return
        try:
            for t in reversed(d.times()):
                self.time.addItem(f"{t:g}", t)
        except Exception as exc:
            self.out.setText(f"could not read result times: {exc}")

    def _browse(self) -> None:
        s = self.sv.session
        f, _ = QFileDialog.getOpenFileName(self, "Measurements", str(s.root) if s else "", "CSV (*.csv *.txt)")
        if f:
            self.csv.setText(f)
            self.load()

    def load(self) -> None:
        from aethercfd.assim.measurements import load_measurements

        try:
            self.meas = load_measurements(self.csv.text().strip(), length_unit=self.unit.currentText())
        except Exception as exc:
            self.out.setText(f"<span style='color:{T.error}'>{html.escape(str(exc))}</span>")
            return
        self.field.clear()
        self.field.addItems(list(self.meas.values))
        self.out.setText(f"{len(self.meas.points)} points; fields: {', '.join(self.meas.values)}"
                         + (f"; uncertainties for {', '.join(self.meas.sigma)}" if self.meas.sigma else "")
                         + "".join(f"<br><span style='color:{T.warn}'>{html.escape(n)}</span>" for n in self.meas.notes))

    def _sim(self):
        d = self._data()
        name, t = self.field.currentText(), self.time.currentData()
        f = d.fields(t, [name])
        if name not in f:
            raise ValueError(f"the simulation has no field {name!r} at t = {t:g}"
                             + (f" in region {d.region}" if d.region else ""))
        v = f[name]
        return t, name, d.centres(t), (v if v.ndim == 1 else np.linalg.norm(v, axis=1))

    def _where(self) -> str:
        r = self.region.region()
        return f" (region {html.escape(r)})" if r else ""

    def compare(self) -> dict | None:
        from aethercfd.assim.measurements import compare

        if self.meas is None or self.sv.session is None:
            self.out.setText("Load measurements (and open a case) first")
            return None
        try:
            t, name, c, v = self._sim()
        except Exception as exc:
            self.out.setText(f"<span style='color:{T.error}'>{html.escape(str(exc))}</span>")
            return None
        try:
            rep = compare(self.meas, {name: v}, c)
        except Exception as exc:
            self.out.setText(f"<span style='color:{T.error}'>{html.escape(f'{type(exc).__name__}: {exc}')}</span>")
            return None
        st = rep["fields"][name]
        notes = "".join(f"<br><span style='color:{T.warn}'>{html.escape(n)}</span>" for n in rep.get("notes", []))
        if not st.get("n"):
            self.out.setHtml(f"<b>{name}</b>{self._where()} at t = {t:g}: no point has both a measured value and a "
                             "simulated one"
                             + notes)
            self.plot.clear()
            return rep
        self.out.setHtml(f"<b>{name}</b>{self._where()} at t = {t:g}: {st['n']} points, bias <b>{st['bias']:.4g}</b>, RMSE "
                         f"<b>{st['rmse']:.4g}</b> ({100 * st['relative_rmse']:.1f} %), max |error| "
                         f"{st['max_abs_error']:.4g}, R² {st['r2'] if st['r2'] is None else round(st['r2'], 3)}"
                         f"<br><span style='color:{T.text_dim}'>simulated = value of the cell containing each point "
                         f"(farthest point–cell-centre distance {rep['max_point_to_cell_distance']:.3g} m)</span>"
                         + notes)
        self.plot.clear()
        meas = self.meas.values[name]
        sim = np.array(st["simulated"], dtype=float) if st.get("simulated") is not None else None
        both = np.concatenate([meas, sim]) if sim is not None else np.zeros(0)
        if sim is not None and np.isfinite(both).any():
            self.plot.plot(meas, sim, pen=None, symbol="o", symbolSize=6, symbolBrush=T.accent)
            lo, hi = float(np.nanmin(both)), float(np.nanmax(both))
            self.plot.plot([lo, hi], [lo, hi], pen=pg.mkPen(T.text_faint, style=Qt.PenStyle.DashLine))
        return rep

    def assimilate(self) -> object | None:
        from aethercfd.assim.measurements import assimilate

        if self.meas is None or self.sv.session is None:
            self.out.setText("Load measurements (and open a case) first")
            return None
        try:
            t, name, c, v = self._sim()
        except Exception as exc:
            self.out.setText(f"<span style='color:{T.error}'>{html.escape(str(exc))}</span>")
            return None
        obs = self.meas.values[name]
        so = self.meas.sigma.get(name)
        if so is None:
            so = self.sigma_o.value() or np.maximum(0.01 * np.abs(obs), 1e-12)
        try:
            a = assimilate(c, v, self.meas.points, obs, so, sigma_b=self.sigma_b.value(), length=self.length.value())
        except Exception as exc:
            self.out.setText(f"<span style='color:{T.error}'>{html.escape(f'{type(exc).__name__}: {exc}')}</span>")
            return None
        self.last = (t, name, a, self.region.region())
        self.write.setEnabled(True)
        self.out.setHtml(f"<b>Assimilated {name}</b>{self._where()} at t = {t:g}: innovation RMS "
                         f"{np.sqrt(np.mean(a.innovation ** 2)):.4g} → residual RMS "
                         f"{np.sqrt(np.mean(a.residual ** 2)):.4g}; {a.touched_cells:,} cells corrected (max |Δ| "
                         f"{np.abs(a.correction).max():.4g}); posterior std {a.posterior_std.min():.3g}…"
                         f"{a.posterior_std.max():.3g}"
                         + "".join(f"<br><span style='color:{T.warn}'>{html.escape(n)}</span>" for n in a.notes))
        return a

    def write_fields(self) -> None:
        from aethercfd.commands.base import USER
        from aethercfd.results.write_fields import field_files

        if self.last is None or not self.sv.ensure_project():
            return
        t, name, a, region = self.last
        s = self.sv.session
        tname = next((d for d in [f"{t:g}", str(t)] if (Path(s.root) / d).is_dir() or
                      (Path(s.root) / "processor0" / d).is_dir()), f"{t:g}")
        files = field_files(s.root, tname, {f"{name}Assim": a.analysis, f"{name}AssimCorr": a.correction,
                                            f"{name}AssimStd": a.posterior_std}, like=name, region=region)
        res = s.executor.execute("files.write", {"root": str(s.root), "files": files}, USER)
        self.sv.notify(f"Wrote {name}Assim, {name}AssimCorr, {name}AssimStd at t = {tname}"
                       + (f" in region {region}" if region else "") + " (Command Journal can "
                       "revert)" if res.ok else f"Not written: {res.message}", "info" if res.ok else "error")


PANELS = [PanelSpec("aether.measurements", "Measurements", "mdi6.chart-scatter-plot", "Results", MeasurementsPanel,
                    default_area=Area.RIGHT,
                    description="Experiment vs simulation, and data assimilation (corrected field + uncertainty)")]
