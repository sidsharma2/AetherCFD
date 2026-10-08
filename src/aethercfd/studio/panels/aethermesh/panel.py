"""AetherMesh: meshing made explainable.

Quality   - checkMesh-exact metrics from the mesh files, histograms, and WHERE the bad cells are
            (hotspot clusters; double-click shows them in the 3D viewport)
Grading   - design blockMesh grading that matches neighbouring cell sizes (no size jumps)
Wall / y+ - first cell height for a target y+, and the layer plan up to the core cells
Geometry  - STL intake (watertight?, features, curvature, extruded/axisymmetric?) and the ranked meshing
            strategies with their first settings
"""

from __future__ import annotations

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
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.tokens import T
from aethercfd.studio.widgets.spin import dspin

METRIC_STAGE = {"nonOrthogonality": "nonOrthogonality", "skewness": "skewness", "aspectRatio": "aspectRatio",
                "volumeRatio": "volumeRatio"}


class AetherMeshPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.hot = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.status)
        self._build_quality()
        self._build_grading()
        self._build_wall()
        self._build_geometry()

    # ------------------------------------------------------------------ quality
    def _build_quality(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.region = QLineEdit(placeholderText="region (multi-region cases)")
        self.region.setMaximumWidth(180)
        b = QPushButton("Analyse mesh quality")
        row.addWidget(self.region)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.q_summary = QTextBrowser()
        self.q_summary.setMaximumHeight(130)
        self.q_tree = QTreeWidget()
        self.q_tree.setHeaderLabels(["Hotspot", "Cells", "Worst", "Where [mm]"])
        self.q_tree.setColumnWidth(0, 170)
        self.q_hist = pg.PlotWidget(background=T.panel)
        self.q_hist.setTitle("Face non-orthogonality", color=T.text_dim, size="9pt")
        self.q_hist.setLabel("bottom", "degrees")
        self.q_hist.setLabel("left", "faces")
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.q_summary)
        split.addWidget(self.q_tree)
        split.addWidget(self.q_hist)
        lay.addWidget(split, 1)
        lay.addWidget(QLabel("Double-click a hotspot to see those cells in the 3D viewport."))
        b.clicked.connect(self.analyse_quality)
        self.q_tree.itemDoubleClicked.connect(self._show_hotspot)
        self.tabs.addTab(w, "Quality")

    def analyse_quality(self) -> None:
        s = self.sv.session
        if s is None:
            return
        region = self.region.text().strip()

        def work():
            from aethercfd.mesh.polymesh import read_polymesh
            from aethercfd.mesh.quality import hotspots, quality

            d = s.root / "constant" / region / "polyMesh" if region else s.root / "constant" / "polyMesh"
            m = read_polymesh(d)
            r = quality(m)
            return r.summary, hotspots(m, r), np.histogram(r.face_non_ortho, bins=np.arange(0, 91, 2.5)), d

        def done(res):
            value, err = res
            if err is not None:
                self.status.setText(f"Quality analysis failed: {err}")
                return
            summ, hs, (hist, edges), d = value
            self.hot = hs
            self.q_summary.setHtml(
                f"<b>{summ['cells']:,} cells</b>, {summ['faces']:,} faces &nbsp; · &nbsp; non-orthogonality max "
                f"<b>{summ['non_ortho_max']:.1f}°</b> (avg {summ['non_ortho_avg']:.1f}°, {summ['faces_non_ortho_gt_65']:,} "
                f"faces > 65°) · skewness max <b>{summ['skewness_max']:.2f}</b> · aspect ratio max "
                f"<b>{summ['aspect_ratio_max']:.1f}</b> · min volume {summ['min_volume']:.3g} m³ "
                f"({summ['negative_volumes']} negative) · smallest neighbour volume ratio "
                f"<b>{summ['min_volume_ratio']:.3f}</b> ({summ['faces_volume_ratio_lt_0p2']:,} faces below 0.2 = size "
                f"jumps over 5:1)<br><span style='color:{T.text_dim}'>Same definitions as checkMesh; read from {d}</span>")
            self.q_tree.clear()
            for metric, clusters in hs.items():
                top = QTreeWidgetItem([metric, str(sum(h.cells for h in clusters)), "", ""])
                for h in clusters:
                    if np.isnan(h.worst):
                        top.addChild(QTreeWidgetItem(["(smaller clusters)", str(h.cells), "", ""]))
                        continue
                    it = QTreeWidgetItem(["cluster", f"{h.cells:,}", f"{h.worst:.3g}",
                                          ", ".join(f"{x * 1e3:.2f}" for x in h.centroid)])
                    it.setData(0, Qt.ItemDataRole.UserRole, (metric, h))
                    top.addChild(it)
                self.q_tree.addTopLevelItem(top)
                top.setExpanded(bool(clusters))
            self.q_hist.clear()
            self.q_hist.addItem(pg.BarGraphItem(x=edges[:-1] + 1.25, height=np.maximum(hist, 0.5), width=2.2,
                                                brush=T.accent))
            self.q_hist.setLogMode(y=True)
            self.status.setText("Mesh quality analysed")

        self.status.setText("Reading the mesh and computing quality…")
        self.sv.run_bg(work, done)

    def _show_hotspot(self, it: QTreeWidgetItem) -> None:
        data = it.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        metric, h = data
        from aethercfd.studio.app.automation import AppAutomation

        auto = AppAutomation(self.sv)
        limits = {"nonOrthogonality": {"min": 60.0}, "skewness": {"min": 2.0}, "aspectRatio": {"min": 30.0},
                  "volumeRatio": {"max": 0.25}}.get(metric, {})
        vp = auto._viewport()
        if vp is None:
            return
        vp.set_stages([s for s in vp.stages if not str(s.get("id", "")).startswith("hot")]
                      + [{"id": "hot1", "type": "quality", "metric": METRIC_STAGE.get(metric, metric), "visible": True,
                          **limits}])
        vp.set_view_options({**vp.view_opts, "base": "outline"})
        b = [h.bbox_min[0], h.bbox_max[0], h.bbox_min[1], h.bbox_max[1], h.bbox_min[2], h.bbox_max[2]]
        vp.frame_bounds(b)
        auto.open_panel("3D Viewport")
        self.sv.select({"kind": "hotspot", "title": f"{metric} hotspot", "metric": metric, "cells": h.cells,
                        "worst": h.worst, "centroid_mm": [round(x * 1e3, 3) for x in h.centroid],
                        "bbox_min_mm": [round(x * 1e3, 3) for x in h.bbox_min],
                        "bbox_max_mm": [round(x * 1e3, 3) for x in h.bbox_max]})

    # ------------------------------------------------------------------ grading
    def _build_grading(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.g_L = dspin(47.5, 1e-9, 1e9, 6)
        self.g_first = dspin(0.125, 0, 1e9, 6, special="free")
        self.g_last = dspin(0.125, 0, 1e9, 6, special="free")
        self.g_max = dspin(0.3, 0, 1e9, 6, special="none")
        self.g_ratio = dspin(1.1, 1.01, 2.0, 3, step=0.01)
        self.g_n = QSpinBox(minimum=0, maximum=100000, specialValueText="auto")
        f.addRow("Edge length", self.g_L)
        f.addRow("First cell (neighbour size)", self.g_first)
        f.addRow("Last cell (neighbour size)", self.g_last)
        f.addRow("Largest cell", self.g_max)
        f.addRow("Max cell-to-cell ratio", self.g_ratio)
        f.addRow("Cells", self.g_n)
        lay.addLayout(f)
        b = QPushButton("Design grading")
        lay.addWidget(b)
        self.g_out = QTextBrowser()
        self.g_out.setMaximumHeight(120)
        self.g_plot = pg.PlotWidget(background=T.panel)
        self.g_plot.setLabel("bottom", "cell index")
        self.g_plot.setLabel("left", "cell size")
        lay.addWidget(self.g_out)
        lay.addWidget(self.g_plot, 1)
        lay.addWidget(QLabel("Units are whatever you use for the length (e.g. mm with convertToMeters 0.001)."))
        b.clicked.connect(self.design)
        self.tabs.addTab(w, "Grading")

    def design(self) -> None:
        from aethercfd.mesh.grading import design

        d = design(self.g_L.value(), first=self.g_first.value() or None, last=self.g_last.value() or None,
                   n=self.g_n.value() or None, max_size=self.g_max.value() or None, max_ratio=self.g_ratio.value())
        s = d.summary()
        self.g_out.setHtml(f"<b>{s['cells']} cells</b>, first {s['first']:.4g}, last {s['last']:.4g}, largest "
                           f"{s['max']:.4g}, max cell-to-cell ratio <b>{s['max_cell_to_cell_ratio']}</b><br>"
                           f"blockMesh grading: <code>{s['blockMesh_grading']}</code>"
                           + "".join(f"<br><span style='color:{T.warn}'>{n}</span>" for n in s["notes"]))
        self.g_plot.clear()
        self.g_plot.plot(d.cells(), pen=pg.mkPen(T.accent, width=2))

    # ------------------------------------------------------------------ wall
    def _build_wall(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        self.w_yplus = dspin(1.0, 0.01, 1000, 2)
        self.w_U = dspin(10.0, 1e-6, 1e5, 4, suffix=" m/s")
        self.w_L = dspin(1.0, 1e-9, 1e4, 6, suffix=" m")
        self.w_kind = QComboBox()
        self.w_kind.addItems(["plate", "pipe"])
        self.w_T = dspin(293.15, 50, 5000, 2, suffix=" K")
        self.w_p = dspin(101325, 100, 1e8, 0, suffix=" Pa")
        self.w_core = dspin(0.002, 0, 10, 6, suffix=" m", special="none")
        f.addRow("Target y+", self.w_yplus)
        f.addRow("Velocity", self.w_U)
        f.addRow("Length (plate) / diameter (pipe)", self.w_L)
        f.addRow("Flow", self.w_kind)
        f.addRow("Air temperature", self.w_T)
        f.addRow("Pressure", self.w_p)
        f.addRow("Core cell size", self.w_core)
        lay.addLayout(f)
        b = QPushButton("First cell height and layers")
        lay.addWidget(b)
        self.w_out = QTextBrowser()
        lay.addWidget(self.w_out, 1)
        b.clicked.connect(self.wall)
        self.tabs.addTab(w, "Wall / y+")

    def wall(self) -> None:
        from aethercfd.mesh.wall import first_cell_height, ideal_gas_rho, plan_layers, sutherland_mu

        rho = ideal_gas_rho(self.w_p.value(), self.w_T.value())
        nu = sutherland_mu(self.w_T.value()) / rho
        r = first_cell_height(self.w_yplus.value(), self.w_U.value(), self.w_L.value(), nu=nu, rho=rho,
                              kind=self.w_kind.currentText())
        txt = (f"Re = {r.reynolds:,.0f} ({r.regime}), C<sub>f</sub> = {r.cf:.4g}, u<sub>τ</sub> = {r.u_tau:.4g} m/s<br>"
               f"<b>First cell height = {r.first_cell * 1e6:.2f} µm</b> (centre at y = {r.y_p * 1e6:.2f} µm for "
               f"y+ = {self.w_yplus.value():g})")
        if self.w_core.value() > 0:
            lp = plan_layers(r.first_cell, core=self.w_core.value())
            txt += (f"<br>Layers: <b>{lp.n}</b> at ratio {lp.ratio}, total {lp.total * 1e3:.3f} mm, last "
                    f"{lp.last * 1e6:.1f} µm" + (f"<br><span style='color:{T.warn}'>{lp.note}</span>" if lp.note else ""))
        txt += (f"<br><span style='color:{T.text_dim}'>Correlation estimate (±30 %): check y+ from the solution "
                "(yPlus function object).</span>")
        self.w_out.setHtml(txt)

    # ------------------------------------------------------------------ geometry
    def _build_geometry(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.stl = QLineEdit(placeholderText="STL/OBJ file (constant/triSurface/...)")
        browse = QPushButton("Browse…")
        go = QPushButton("Analyse and recommend")
        row.addWidget(self.stl, 1)
        row.addWidget(browse)
        row.addWidget(go)
        lay.addLayout(row)
        self.geo_out = QTextBrowser()
        lay.addWidget(self.geo_out, 1)
        browse.clicked.connect(self._browse_stl)
        go.clicked.connect(self.analyse_geometry)
        self.tabs.addTab(w, "Geometry & strategy")

    def _browse_stl(self) -> None:
        start = str(self.sv.session.root / "constant" / "triSurface") if self.sv.session else ""
        f, _ = QFileDialog.getOpenFileName(self, "Geometry", start, "Surfaces (*.stl *.obj)")
        if f:
            self.stl.setText(f)

    def analyse_geometry(self) -> None:
        path = self.stl.text().strip()
        if not path and self.sv.session:
            tri = list((self.sv.session.root / "constant" / "triSurface").glob("*.stl"))
            path = str(tri[0]) if tri else ""
            self.stl.setText(path)
        if not path or not Path(path).exists():
            self.status.setText("Choose an STL/OBJ file")
            return

        def work():
            from aethercfd.mesh.geometry import analyse
            from aethercfd.mesh.strategy import plan

            g = analyse(path)
            return g, plan(g)

        def done(res):
            value, err = res
            if err is not None:
                self.status.setText(f"Geometry analysis failed: {err}")
                return
            g, plans = value
            b = g.bounds
            html = (f"<b>{Path(g.path).name}</b>: {g.triangles:,} triangles, box {b[1] - b[0]:.4g} × {b[3] - b[2]:.4g} × "
                    f"{b[5] - b[4]:.4g}, area {g.area:.4g}" + (f", volume {g.volume:.4g}" if g.volume else "")
                    + f"<br>{'watertight' if g.watertight else '<b>not watertight</b>'} · shape <b>{g.shape}</b>"
                    + (f" ({g.axis})" if g.axis else "") + f" · feature angle <b>{g.feature_angle:.0f}°</b> "
                    f"({g.feature_edges} feature edges, length {g.feature_length:.4g})"
                    + (f" · smallest radius ~{g.min_radius:.3g}" if g.min_radius else "")
                    + "".join(f"<br><span style='color:{T.warn}'>{n}</span>" for n in g.notes) + "<hr>")
            for i, p in enumerate(plans):
                html += (f"<p><b>{i + 1}. {p.mesher}</b><br>" + "<br>".join(f"+ {x}" for x in p.why)
                         + "".join(f"<br><span style='color:{T.text_dim}'>– {c}</span>" for c in p.caveats)
                         + (f"<br><code>{p.settings}</code>" if p.settings else "") + "</p>")
            self.geo_out.setHtml(html)
            self.status.setText("Geometry analysed")

        self.status.setText("Analysing the geometry…")
        self.sv.run_bg(work, done)


PANELS = [PanelSpec("aether.aethermesh", "AetherMesh", "mdi6.vector-triangle", "Mesh", AetherMeshPanel,
                    default_area=Area.RIGHT, description="Mesh quality hotspots, grading designer, y+ and layers, "
                    "geometry intake and meshing strategy")]
