"""Combustion workbench: everything to know before a reacting simulation, computed with Cantera.

Mixture (Z_st, adiabatic flame temperature, transport) -> Flame (laminar flame speed and thickness,
ignition delay) -> Regime & mesh (Peters diagram, cells per flame thickness for this case's mesh) ->
Model advisor (ranked models with reasons and the exact OpenFOAM entries; apply with approval) ->
Flamelets (counterflow strain sweep, tables). Results collect in a report the copilot can read.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import mono_font
from aethercfd.studio.theme.tokens import T
from aethercfd.studio.widgets.spin import dspin

PRESETS = {
    "Methane / air": ("CH4:1", "O2:0.21, N2:0.79", 300, 300),
    "Sandia flame D (CH4/air 1:3 vol.)": ("CH4:0.25, O2:0.1575, N2:0.5925", "O2:0.21, N2:0.79", 294, 291),
    "Hydrogen / air": ("H2:1", "O2:0.21, N2:0.79", 300, 300),
    "Syngas 50/50 / air": ("H2:0.5, CO:0.5", "O2:0.21, N2:0.79", 300, 300),
    "Propane / air": ("C3H8:1", "O2:0.21, N2:0.79", 300, 300),
    "Ammonia / air": ("NH3:1", "O2:0.21, N2:0.79", 300, 300),
}


def _plot(title: str, xl: str, yl: str, logy: bool = False, logx: bool = False) -> pg.PlotWidget:
    w = pg.PlotWidget(background=T.panel)
    w.setTitle(title, color=T.text_dim, size="9pt")
    w.setLabel("bottom", xl)
    w.setLabel("left", yl)
    w.showGrid(x=True, y=True, alpha=0.2)
    w.setLogMode(x=logx, y=logy)
    return w


class CombustionPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.report: dict = {}
        self.mechs = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QFormLayout()
        self.preset = QComboBox()
        self.preset.addItems(list(PRESETS))
        self.mech = QComboBox(toolTip="Kinetic mechanism (Cantera built-ins, your CRECK sets and YAML files)")
        self.mech.addItem("GRI-Mech 3.0 (natural gas, NOx; 53 species)", "builtin:gri30.yaml")
        self.fuel = QLineEdit()
        self.ox = QLineEdit()
        self.tf = QDoubleSpinBox(minimum=50, maximum=3000, suffix=" K", decimals=1)
        self.to = QDoubleSpinBox(minimum=50, maximum=3000, suffix=" K", decimals=1)
        self.p = dspin(1.0, 0.01, 500, 3, suffix=" bar")
        self.phi = dspin(1.0, 0.1, 5, 2, step=0.05)
        row = QHBoxLayout()
        row.addWidget(self.tf)
        row.addWidget(QLabel("oxidizer"))
        row.addWidget(self.to)
        top.addRow("Preset", self.preset)
        top.addRow("Mechanism", self.mech)
        top.addRow("Fuel stream (mole fr.)", self.fuel)
        top.addRow("Oxidizer stream", self.ox)
        top.addRow("Fuel T", row)
        top.addRow("Pressure", self.p)
        top.addRow("Equivalence ratio φ", self.phi)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.status)
        self._build_mixture()
        self._build_flame()
        self._build_regime()
        self._build_advisor()
        self._build_flamelets()
        self._build_report()
        self.preset.currentTextChanged.connect(self._apply_preset)
        self._apply_preset(self.preset.currentText())
        self._discover()

    # ------------------------------------------------------------------ inputs
    def _apply_preset(self, name: str) -> None:
        f, o, tf, to = PRESETS[name]
        self.fuel.setText(f)
        self.ox.setText(o)
        self.tf.setValue(tf)
        self.to.setValue(to)

    def streams(self):
        from aethercfd.combustion.calc import Streams

        return Streams(self._mech_path(), self.fuel.text(), self.ox.text(), self.tf.value(), self.to.value(),
                       self.p.value() * 1e5)

    def _mech_path(self) -> str:
        data = self.mech.currentData() or "builtin:gri30.yaml"
        if data.startswith("builtin:"):
            return data.split(":", 1)[1]
        if data.startswith("mech:"):
            from aethercfd.combustion.mechanisms import ensure_yaml

            return ensure_yaml(self.mechs[int(data.split(":", 1)[1])])
        return data

    def _discover(self) -> None:
        def work():
            from aethercfd.combustion.mechanisms import discover

            return discover(getattr(self.sv.settings, "mechanism_roots", None))

        def done(res):
            value, err = res
            if err is not None or not value:
                return
            self.mechs = value
            self.mech.blockSignals(True)
            self.mech.clear()
            for i, m in enumerate(value):
                label = f"{m.name}  [{m.source}]" + (f"  {m.note}" if m.source == "builtin" else "")
                self.mech.addItem(label, f"builtin:{m.path}" if m.source == "builtin" else f"mech:{i}")
            self.mech.blockSignals(False)
            self.status.setText(f"{len(value)} mechanisms found (built-in, CRECK, YAML)")

        self.sv.run_bg(work, done)

    def _bg(self, label: str, work, done) -> None:
        self.status.setText(f"{label}…")
        t0 = time.perf_counter()

        def finish(res):
            value, err = res
            if err is not None:
                self.status.setText(f"{label} failed: {err}")
                return
            self.status.setText(f"{label}: done in {time.perf_counter() - t0:.1f} s")
            done(value)

        self.sv.run_bg(work, finish)

    # ------------------------------------------------------------------ mixture
    def _build_mixture(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        b = QPushButton("Compute stoichiometry, flame temperature and properties")
        self.mix_out = QTextBrowser()
        self.tad_plot = _plot("Adiabatic flame temperature", "φ", "T_ad [K]")
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.mix_out)
        split.addWidget(self.tad_plot)
        lay.addWidget(b)
        lay.addWidget(split, 1)
        b.clicked.connect(self.compute_mixture)
        self.tabs.addTab(w, "Mixture")

    def compute_mixture(self) -> None:
        st = self.streams()
        phi = self.phi.value()

        def work():
            from aethercfd.combustion import calc

            return {"stoich": calc.stoichiometric(st),
                    "tad": calc.adiabatic_flame(st, list(np.round(np.arange(0.5, 2.01, 0.05), 3))),
                    "unburnt": calc.transport(st, phi=phi), "mech": calc.gas_summary(st.mechanism),
                    "z_phi": calc.z_of_phi(st, phi)}

        def done(r):
            self.report["mixture"] = {"streams": st.__dict__, "phi": phi, **r}
            rows = r["tad"]["rows"]
            self.tad_plot.clear()
            self.tad_plot.plot([x["phi"] for x in rows], [x["T_ad"] for x in rows], pen=pg.mkPen(T.accent, width=2))
            at = min(rows, key=lambda x: abs(x["phi"] - phi))
            u = r["unburnt"]
            m = r["mech"]
            self.mix_out.setHtml(
                f"<b>Mechanism</b>: {m['species']} species, {m['reactions']} reactions"
                f"{' · NOx chemistry' if m['has_NOx'] else ''}{' · soot precursors' if m['has_soot_precursors'] else ''}"
                f"<br><b>Z<sub>st</sub></b> = {r['stoich']['Z_st']:.4f} &nbsp; (Z at φ={phi:g}: {r['z_phi']:.4f})"
                f"<br><b>T<sub>ad</sub></b>(φ={at['phi']:g}) = {at['T_ad']:.0f} K &nbsp; peak {r['tad']['peak']['T_ad']:.0f} K "
                f"at φ={r['tad']['peak']['phi']:g}"
                f"<br>Equilibrium products: {', '.join(f'{k} {v:.3f}' for k, v in list(at['major'].items())[:5])}"
                f"<br><b>Unburnt mixture</b>: ρ={u['rho']:.4g} kg/m³, μ={u['mu']:.4g} Pa·s, ν={u['nu']:.4g} m²/s, "
                f"cp={u['cp']:.4g} J/kg/K, Pr={u['Pr']:.3f}")
            self._refresh_report()

        self._bg("Mixture", work, done)

    # ------------------------------------------------------------------ flame
    def _build_flame(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        b1 = QPushButton("Laminar flame speed and thickness")
        b2 = QPushButton("Ignition delay (T sweep)")
        row.addWidget(b1)
        row.addWidget(b2)
        lay.addLayout(row)
        self.flame_out = QTextBrowser()
        self.flame_plot = _plot("Flame structure", "x [mm]", "T [K]")
        self.ign_plot = _plot("Ignition delay", "1000 / T₀ [1/K]", "τ_ign [s]", logy=True)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.flame_out)
        split.addWidget(self.flame_plot)
        split.addWidget(self.ign_plot)
        lay.addWidget(split, 1)
        b1.clicked.connect(self.compute_flame)
        b2.clicked.connect(self.compute_ignition)
        self.tabs.addTab(w, "Flame")

    def compute_flame(self) -> None:
        st, phi = self.streams(), self.phi.value()

        def work():
            from aethercfd.combustion.calc import flame_speed

            return flame_speed(st, phi)

        def done(f):
            self.report["flame"] = {"phi": phi, "S_L": f.S_L, "delta_L": f.delta_L, "T_ad": f.T_ad,
                                    "grid_points": f.grid_points}
            self.flame_plot.clear()
            self.flame_plot.plot(np.array(f.x) * 1e3, f.T, pen=pg.mkPen(T.warn, width=2))
            self.flame_out.setHtml(f"<b>S<sub>L</sub></b> = {f.S_L * 100:.2f} cm/s &nbsp; "
                                   f"<b>δ<sub>L</sub></b> (thermal) = {f.delta_L * 1e3:.3f} mm &nbsp; "
                                   f"T<sub>b</sub> = {f.T_ad:.0f} K &nbsp; ({f.grid_points} grid points)<br>"
                                   f"Chemical time δ/S<sub>L</sub> = {f.delta_L / f.S_L * 1e3:.3f} ms")
            self.reg_SL.setValue(f.S_L)
            self.reg_dL.setValue(f.delta_L * 1e3)
            self._refresh_report()

        self._bg("Freely propagating flame (Cantera)", work, done)

    def compute_ignition(self) -> None:
        st, phi = self.streams(), self.phi.value()

        def work():
            from aethercfd.combustion.calc import ignition_delay

            return [ignition_delay(st, phi, T0) for T0 in (1000, 1100, 1200, 1300, 1400, 1500, 1700, 1900)]

        def done(rows):
            self.report["ignition"] = rows
            ok = [r for r in rows if r["ignited"]]
            self.ign_plot.clear()
            if ok:
                self.ign_plot.plot([1000 / r["T0"] for r in ok], [r["tau_ign"] for r in ok], symbol="o",
                                   pen=pg.mkPen(T.accent, width=2), symbolBrush=T.accent)
            self._refresh_report()

        self._bg("Ignition delay sweep", work, done)

    # ------------------------------------------------------------------ regime and mesh
    def _build_regime(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.reg_SL = dspin(0.38, 0.001, 100, 4, suffix=" m/s")
        self.reg_dL = dspin(0.45, 0.0001, 100, 4, suffix=" mm")
        self.reg_u = dspin(2.0, 0.0001, 1000, 3, suffix=" m/s")
        self.reg_l = dspin(10.0, 0.001, 10000, 3, suffix=" mm")
        self.reg_cell = dspin(1.0, 0.0001, 10000, 4, suffix=" mm")
        f.addRow("Laminar flame speed S_L", self.reg_SL)
        f.addRow("Flame thickness δ_L", self.reg_dL)
        f.addRow("Turbulence intensity u'", self.reg_u)
        f.addRow("Integral length l_t", self.reg_l)
        f.addRow("Mesh cell size in the flame", self.reg_cell)
        lay.addLayout(f)
        row = QHBoxLayout()
        b = QPushButton("Regime and resolution")
        bc = QPushButton("Cell size from this case's mesh")
        row.addWidget(b)
        row.addWidget(bc)
        lay.addLayout(row)
        self.reg_out = QTextBrowser()
        self.reg_plot = _plot("Premixed regime diagram (Peters)", "l_t / δ_L", "u' / S_L", logx=True, logy=True)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.reg_out)
        split.addWidget(self.reg_plot)
        lay.addWidget(split, 1)
        b.clicked.connect(self.compute_regime)
        bc.clicked.connect(self._cell_from_case)
        self.tabs.addTab(w, "Regime && mesh")

    def _cell_from_case(self) -> None:
        s = self.sv.session
        if s is None:
            return

        def work():
            from aethercfd.mesh.polymesh import read_polymesh
            from aethercfd.mesh.quality import geometry

            c = s.case()
            md = c.regions[0].mesh_dir if c.regions else s.root / "constant" / "polyMesh"
            g = geometry(read_polymesh(md))
            h = np.cbrt(np.abs(g.cell_volumes))
            return {"min": float(h.min()), "median": float(np.median(h)), "p10": float(np.percentile(h, 10))}

        def done(r):
            self.reg_cell.setValue(r["p10"] * 1e3)
            self.status.setText(f"case cell size (cube root of volume): min {r['min'] * 1e3:.4g} mm, 10th percentile "
                                f"{r['p10'] * 1e3:.4g} mm, median {r['median'] * 1e3:.4g} mm")

        self._bg("Reading the mesh", work, done)

    def compute_regime(self) -> None:
        from aethercfd.combustion.calc import regime, resolution_advice

        nu = (self.report.get("mixture", {}).get("unburnt", {}) or {}).get("nu", 1.6e-5)
        SL, dL = self.reg_SL.value(), self.reg_dL.value() * 1e-3
        r = regime(SL, dL, self.reg_u.value(), self.reg_l.value() * 1e-3, nu)
        res = resolution_advice(dL, self.reg_cell.value() * 1e-3)
        self.report["regime"] = {**r, "resolution": res}
        self.reg_out.setHtml(f"<b>Regime</b>: {r['regime']} &nbsp; Da = {r['Da']:.3g}, Ka = {r['Ka']:.3g}, "
                             f"Re<sub>t</sub> = {r['Re_t']:.3g}<br>Kolmogorov η = {r['eta'] * 1e6:.1f} µm<br>"
                             f"<b>Mesh</b>: {res['cells_per_flame_thickness']:.2f} cells per flame thickness — "
                             f"{res['verdict']}")
        p = self.reg_plot
        p.clear()
        x = np.logspace(-1, 4, 50)
        p.plot(x, np.ones_like(x), pen=pg.mkPen(T.text_faint, style=Qt.PenStyle.DashLine))  # u' = S_L
        # Ka = 1:  u'/S_L = (l_t/delta)^(1/3);  Ka = 100: u'/S_L = 100^(2/3) (l/d)^(1/3);  Re_t = 1: u'/S_L = (l/d)^-1
        p.plot(x, x ** (1 / 3), pen=pg.mkPen(T.info, width=1.5))
        p.plot(x, 100 ** (2 / 3) * x ** (1 / 3), pen=pg.mkPen(T.warn, width=1.5))
        p.plot(x, 1 / x, pen=pg.mkPen(T.text_dim, width=1))
        p.plot([r["l_t_over_delta_L"]], [r["u_prime_over_S_L"]], symbol="star", symbolSize=16,
               symbolBrush=T.error, pen=None)
        self._refresh_report()

    # ------------------------------------------------------------------ advisor
    def _build_advisor(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.adv_flame = QComboBox()
        self.adv_flame.addItems(["non-premixed", "premixed", "partially-premixed"])
        self.adv_want = {k: QCheckBox(k) for k in ("temperature", "CO", "NOx", "soot", "extinction", "ignition")}
        self.adv_want["temperature"].setChecked(True)
        wrow = QHBoxLayout()
        for c in self.adv_want.values():
            wrow.addWidget(c)
        self.adv_jet = QCheckBox("round jet (e.g. Sandia D)")
        self.adv_steady = QCheckBox("steady (RANS)")
        self.adv_steady.setChecked(True)
        f.addRow("Flame", self.adv_flame)
        f.addRow("I need", wrow)
        f.addRow(self.adv_jet)
        f.addRow(self.adv_steady)
        lay.addLayout(f)
        b = QPushButton("Recommend models")
        lay.addWidget(b)
        split = QSplitter(Qt.Orientation.Vertical)
        self.adv_list = QListWidget()
        self.adv_text = QPlainTextEdit(readOnly=True)
        self.adv_text.setFont(mono_font(9))
        split.addWidget(self.adv_list)
        split.addWidget(self.adv_text)
        lay.addWidget(split, 1)
        self.btn_apply = QPushButton("Apply the selected model's dictionaries to the case…")
        self.btn_apply.setEnabled(False)
        lay.addWidget(self.btn_apply)
        b.clicked.connect(self.recommend)
        self.adv_list.currentRowChanged.connect(self._show_advice)
        self.btn_apply.clicked.connect(self._apply_advice)
        self.advice = []
        self.tabs.addTab(w, "Model advisor")

    def recommend(self) -> None:
        from aethercfd.combustion.advisor import Situation, advise

        s = self.sv.session
        fork = "esi"
        if s is not None:
            fam = getattr(s.case().fork_guess, "family", "esi")
            fork = "foundation" if "foundation" in str(fam).lower() else "esi"
        reg = self.report.get("regime", {})
        mech = (self.report.get("mixture", {}) or {}).get("mech", {})
        sit = Situation(self.adv_flame.currentText(), reg.get("regime"), reg.get("Da"), reg.get("Ka"),
                        tuple(k for k, c in self.adv_want.items() if c.isChecked()), int(mech.get("species", 53)),
                        int((s.case().n_cells if s else 0) or 100_000), self.adv_steady.isChecked(),
                        self.adv_jet.isChecked(), fork)
        self.advice = advise(sit)
        self.report["advice"] = [{"model": a.model, "why": a.why, "caveats": a.caveats, "turbulence": a.turbulence,
                                  "chemistry": a.chemistry, "files": a.files} for a in self.advice]
        self.adv_list.clear()
        for a in self.advice:
            self.adv_list.addItem(f"{a.model}")
        self.adv_list.setCurrentRow(0)
        self._refresh_report()

    def _show_advice(self, row: int) -> None:
        if not (0 <= row < len(self.advice)):
            return
        a = self.advice[row]
        lines = ["WHY"] + [f"  + {x}" for x in a.why] + ["", "CAVEATS"] + [f"  - {x}" for x in a.caveats]
        if a.turbulence:
            lines += ["", "TURBULENCE"] + [f"  * {x}" for x in a.turbulence]
        if a.chemistry:
            lines += ["", "CHEMISTRY"] + [f"  * {x}" for x in a.chemistry]
        for path, txt in a.files.items():
            lines += ["", f"--- {path} ---", txt]
        self.adv_text.setPlainText("\n".join(lines))
        self.btn_apply.setEnabled(bool(a.files) and self.sv.session is not None)

    def _apply_advice(self) -> None:
        row = self.adv_list.currentRow()
        s = self.sv.session
        if not (0 <= row < len(self.advice)) or s is None or not self.sv.ensure_project():
            return
        from aethercfd.commands.base import USER, CommandPlan, Risk
        from aethercfd.foamdict.cst import Entry
        from aethercfd.foamdict.parser import parse
        from aethercfd.studio.widgets.approval import ApprovalDialog

        a = self.advice[row]
        plans = []
        for rel, txt in a.files.items():
            f = s.root / rel
            if not f.exists():
                header = ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n    class       dictionary;\n"
                          f"    location    \"constant\";\n    object      {f.name};\n}}\n\n")
                params = {"path": str(f), "text": header + txt}
                plans.append(("file.save", params, s.executor.plan("file.save", params, USER)))
                continue
            doc = parse(txt)
            for m in doc.root.members:
                if not isinstance(m, Entry):
                    continue
                value = doc.text(m.dict.start, m.dict.end) if m.dict is not None else (
                    doc.text(m.value.start, m.value.end) if m.value is not None else "")
                params = {"file": str(f), "key": m.key.name, "value": value, "insert_if_missing": True}
                try:
                    plans.append(("dict.set", params, s.executor.plan("dict.set", params, USER)))
                except Exception as exc:
                    self.sv.notify(f"{rel} › {m.key.name}: {exc}", "warn")
        combined = CommandPlan("combustion.apply", {}, USER, Risk.EDIT, f"use {a.model}",
                               rationale="; ".join(a.why[:2]))
        for _n, _p, pl in plans:
            combined.preconditions += pl.preconditions
            combined.file_changes += pl.file_changes
        if ApprovalDialog(combined, self, who="Combustion advisor").exec() != ApprovalDialog.DialogCode.Accepted:
            return
        bad = [res.message for n, p, pl in plans
               if not (res := s.executor.execute(n, p, USER, plan=pl, approved_hash=pl.plan_hash)).ok]
        self.sv.notify("Combustion settings applied (see the Command Journal)" if not bad else f"Partly failed: {bad[0]}",
                       "info" if not bad else "error")

    # ------------------------------------------------------------------ flamelets
    def _build_flamelets(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.strains = QLineEdit("10, 50, 100, 200, 400, 800")
        b = QPushButton("Counterflow flamelets")
        e = QPushButton("Export table (CSV)…")
        row.addWidget(QLabel("Strain rates [1/s]"))
        row.addWidget(self.strains, 1)
        row.addWidget(b)
        row.addWidget(e)
        lay.addLayout(row)
        self.fl_plot = _plot("Flamelets: T(Z)", "mixture fraction Z", "T [K]")
        self.fl_s = _plot("Upper branch of the S-curve", "strain rate [1/s]", "T_max [K]", logx=True)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.fl_plot)
        split.addWidget(self.fl_s)
        lay.addWidget(split, 1)
        b.clicked.connect(self.compute_flamelets)
        e.clicked.connect(self._export_flamelets)
        self.tabs.addTab(w, "Flamelets")

    def compute_flamelets(self) -> None:
        st = self.streams()
        try:
            strains = [float(x) for x in self.strains.text().replace(";", ",").split(",") if x.strip()]
        except ValueError:
            self.status.setText("strain rates: numbers separated by commas")
            return

        def work():
            from aethercfd.combustion.calc import counterflow_flamelet

            out = []
            for a in strains:
                try:
                    out.append(counterflow_flamelet(st, a))
                except Exception as exc:  # extinguished / not converged
                    out.append({"strain_nominal": a, "error": str(exc), "burning": False})
            return out

        def done(rows):
            self.report["flamelets"] = [{k: v for k, v in r.items() if k not in ("Z", "T")} for r in rows]
            self._flamelets = rows
            self.fl_plot.clear()
            colors = [T.accent, T.ok, T.warn, T.info, T.error, T.text_dim]
            for i, r in enumerate(rows):
                if "Z" in r:
                    self.fl_plot.plot(r["Z"], r["T"], pen=pg.mkPen(colors[i % len(colors)], width=1.6),
                                      name=f"a={r['strain_nominal']:g}")
            ok = [r for r in rows if "T_max" in r]
            self.fl_s.clear()
            if ok:
                self.fl_s.plot([r["strain_nominal"] for r in ok], [r["T_max"] for r in ok], symbol="o",
                               pen=pg.mkPen(T.accent, width=2), symbolBrush=T.accent)
            self._refresh_report()

        self._bg(f"{len(strains)} counterflow flamelets (Cantera)", work, done)

    def _export_flamelets(self) -> None:
        rows = getattr(self, "_flamelets", None)
        if not rows:
            return
        start = str((self.sv.session.root if self.sv.session else Path.home()) / "flamelets.csv")
        f, _ = QFileDialog.getSaveFileName(self, "Export flamelet table", start, "CSV (*.csv)")
        if not f:
            return
        from aethercfd.core.textio import save_user_file

        lines = ["strain_nominal,Z,T"]
        for r in rows:
            for z, t in zip(r.get("Z", []), r.get("T", []), strict=False):
                lines.append(f"{r['strain_nominal']},{z:.6g},{t:.6g}")
        err = save_user_file(f, "\n".join(lines) + "\n")
        self.sv.notify(err or f"Flamelet table written: {f}", "error" if err else "info")

    # ------------------------------------------------------------------ report
    def _build_report(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.rep = QPlainTextEdit(readOnly=True)
        self.rep.setFont(mono_font(9))
        b = QPushButton("Save report with the case (.aethercfd/local/combustion_report.json)")
        lay.addWidget(self.rep, 1)
        lay.addWidget(b)
        b.clicked.connect(self._save_report)
        self.tabs.addTab(w, "Report")

    def _refresh_report(self) -> None:
        self.rep.setPlainText(json.dumps(self.report, indent=1, default=lambda o: o.__dict__
                                         if hasattr(o, "__dict__") else str(o))[:200_000])

    def _save_report(self) -> None:
        s = self.sv.session
        if s is None:
            return
        from aethercfd.core.textio import save_user_file

        out = Path(s.root) / ".aethercfd" / "local" / "combustion_report.json"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.sv.notify(f"cannot create {out.parent}: {exc.strerror or exc}", "error")
            return
        err = save_user_file(out, json.dumps(self.report, indent=1, default=str))
        self.sv.notify(err or f"Saved {out}", "error" if err else "info")


PANELS = [PanelSpec("aether.combustion", "Combustion", "mdi6.fire", "Physics", CombustionPanel,
                    default_area=Area.CENTER, description="Cantera pre-calculations (flame temperature, flame "
                    "speed, ignition, regime, mesh resolution), model advisor, flamelets")]
