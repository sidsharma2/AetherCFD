"""3D Viewport: OpenFOAM results on the REAL finite-volume mesh (cell data, flat shading).

pyvistaqt's QtInteractor provides the render window/interactor; actors, mappers and lookup tables
are plain VTK so a new time step only swaps mapper inputs (no actor rebuild, ADR 0005). Reading and
filtering happen on the shared VtkWorker thread.
"""

from __future__ import annotations

import logging
from pathlib import Path

import vtk
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSlider,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aethercfd.results.foam_reader import display_name
from aethercfd.studio.panels.viewport.worker import shared_worker
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)
CMAPS = ["viridis", "turbo", "inferno", "plasma", "coolwarm", "RdBu_r", "jet", "gray"]
_DIVERGING = {"coolwarm", "RdBu_r"}


def _lut(cmap: str, lo: float, hi: float, n: int = 256) -> vtk.vtkLookupTable:
    import matplotlib

    cm = matplotlib.colormaps[cmap]
    lut = vtk.vtkLookupTable()
    lut.SetNumberOfTableValues(n)
    for i in range(n):
        r, g, b, a = cm(i / (n - 1))
        lut.SetTableValue(i, r, g, b, a)
    lut.SetTableRange(lo, hi)
    lut.Build()
    return lut


def _tool(ic: str, tip: str, *, checkable: bool = False) -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(ic))
    b.setToolTip(tip)
    b.setCheckable(checkable)
    b.setAutoRaise(True)
    return b


class ViewportPanel(Panel):
    STATE_VERSION = 1
    _req_meta = Signal(str, int, dict)
    _req_load = Signal(str, int, dict)
    _req_probe = Signal(str, int, dict)
    stages_updated = Signal(dict)  # stage block -> info (scalar, range, notes, cells) after each load

    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        from pyvistaqt import QtInteractor

        sv = ctx.services
        self.vid = f"viewport{ctx.instance_id}"
        self.gen = 0
        self.case_root: Path | None = None
        self.meta: dict = {}
        self.actors: dict[str, tuple[vtk.vtkActor, vtk.vtkPolyDataMapper]] = {}
        self.scalar: str | None = None
        self.range: tuple[float, float] | None = None
        self.bounds = None
        self._first_data = True
        self._is2d = False
        self._pending_state: dict | None = None
        self.stages: list[dict] = []  # Visualization pipeline (see aethercfd.results.pipeline)
        self.view_opts: dict = {}  # mirror / revolve
        self.stage_info: dict[str, dict] = {}
        self.bar_stage: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # ---------------- toolbar
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 3, 6, 3)
        bar.setSpacing(4)
        self.field = QComboBox(minimumWidth=110, toolTip="Field (cell data on the real mesh)")
        self.comp = QComboBox(toolTip="Vector component")
        self.comp.addItems(["magnitude", "x", "y", "z"])
        self.time = QComboBox(minimumWidth=90, toolTip="Time")
        self.prev_t, self.next_t, self.last_t = (_tool("msc.chevron-left", "Previous time"),
                                                 _tool("msc.chevron-right", "Next time"),
                                                 _tool("msc.debug-step-over", "Latest time"))
        self.follow = QCheckBox("Follow", toolTip="Jump to new time steps as a run writes them")
        self.rep = QComboBox(toolTip="Representation")
        self.rep.addItems(["Surface", "Surface + edges", "Wireframe"])
        self.cmap = QComboBox(toolTip="Colour map")
        self.cmap.addItems(CMAPS)
        self.lock = QCheckBox("Lock range", toolTip="Keep the colour range when the time changes")
        for w in (QLabel("Field"), self.field, self.comp, QLabel("  t"), self.prev_t, self.time, self.next_t,
                  self.last_t, self.follow, QLabel("  "), self.rep, self.cmap, self.lock):
            bar.addWidget(w)
        bar.addStretch(1)
        self.btn_fit = _tool("msc.screen-full", "Fit view")
        self.btn_x, self.btn_y, self.btn_z = (_tool("mdi6.axis-x-arrow", "Look along X"),
                                             _tool("mdi6.axis-y-arrow", "Look along Y"),
                                             _tool("mdi6.axis-z-arrow", "Look along Z"))
        self.btn_iso = _tool("mdi6.cube-scan", "Isometric")
        self.btn_ao = _tool("mdi6.circle-half-full", "Ambient occlusion (depth cues for 3D surfaces)", checkable=True)
        self.btn_shot = _tool("msc.device-camera", "Screenshot (PNG, 2x)")
        self.btn_render = _tool("mdi6.image-filter-hdr", "Render image… (up to 8K, 8x anti-aliasing)")
        self.btn_side = _tool("msc.layout-sidebar-right", "Display options", checkable=True)
        for b in (self.btn_fit, self.btn_x, self.btn_y, self.btn_z, self.btn_iso, self.btn_ao, self.btn_shot,
                  self.btn_render, self.btn_side):
            bar.addWidget(b)
        lay.addLayout(bar)
        # ---------------- view + sidebar
        split = QSplitter(Qt.Orientation.Horizontal)
        self.plotter = QtInteractor(self, auto_update=False)
        self.plotter.set_background(T.viewport_bg)
        self.plotter.enable_anti_aliasing("fxaa")
        self.plotter.enable_depth_peeling(number_of_peels=4)  # correct order for translucent base meshes
        self.plotter.enable_lightkit()  # key/fill/back lights: shape reads clearly on smooth surfaces
        self.plotter.add_axes(color=T.text_dim)
        split.addWidget(self.plotter)
        side = QWidget()
        sl = QFormLayout(side)
        self.patches = QListWidget()
        self.patches.itemChanged.connect(lambda *_: self.request_load())
        self.patches.currentItemChanged.connect(self._select_patch)
        sl.addRow(QLabel("Show"))
        sl.addRow(self.patches)
        self.slice_on = QCheckBox("Slice through the mesh")
        self.slice_axis = QComboBox()
        self.slice_axis.addItems(["X", "Y", "Z"])
        self.slice_pos = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=1000, value=500)
        self.slice_on.toggled.connect(lambda *_: self.request_load())
        self.slice_axis.currentIndexChanged.connect(lambda *_: self.request_load())
        self.slice_pos.valueChanged.connect(lambda *_: self._slice_timer.start())
        sl.addRow(self.slice_on)
        sl.addRow("Normal", self.slice_axis)
        sl.addRow("Position", self.slice_pos)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color: {T.text_faint};")
        sl.addRow(self.info)
        side.setMinimumWidth(200)
        split.addWidget(side)
        side.setVisible(False)
        self.side = side
        lay.addWidget(split, 1)
        self.scalar_bar = vtk.vtkScalarBarActor()
        self.scalar_bar.SetNumberOfLabels(5)
        self.scalar_bar.SetOrientationToHorizontal()  # bottom strip: never covers the domain
        self.scalar_bar.SetPosition(0.30, 0.02)
        self.scalar_bar.SetWidth(0.40)
        self.scalar_bar.SetHeight(0.09)
        self.scalar_bar.UnconstrainedFontSizeOn()
        self.scalar_bar.SetLabelFormat("%-#6.4g")
        for tp, size in ((self.scalar_bar.GetLabelTextProperty(), 11), (self.scalar_bar.GetTitleTextProperty(), 12)):
            tp.SetColor(0.84, 0.84, 0.84)
            tp.SetFontSize(size)
            tp.ItalicOff()
            tp.BoldOff()
            tp.ShadowOff()
        self.scalar_bar.VisibilityOff()
        self.plotter.renderer.AddViewProp(self.scalar_bar)  # (AddActor2D was removed in VTK 9.7)
        # ---------------- wiring
        self._slice_timer = QTimer(self, singleShot=True, interval=150, timeout=self.request_load)
        w = shared_worker()
        self._req_meta.connect(w.meta, Qt.ConnectionType.QueuedConnection)
        self._req_load.connect(w.load, Qt.ConnectionType.QueuedConnection)
        self._req_probe.connect(w.probe, Qt.ConnectionType.QueuedConnection)
        w.meta_ready.connect(self._on_meta)
        w.data_ready.connect(self._on_data)
        w.probe_ready.connect(self._on_probe)
        w.failed.connect(self._on_fail)
        self.field.currentIndexChanged.connect(self._on_field_changed)
        self.comp.currentIndexChanged.connect(lambda *_: self.request_load(reset_range=True))
        self.time.currentIndexChanged.connect(lambda *_: self.request_load())
        self.prev_t.clicked.connect(lambda: self.time.setCurrentIndex(max(0, self.time.currentIndex() - 1)))
        self.next_t.clicked.connect(lambda: self.time.setCurrentIndex(min(self.time.count() - 1,
                                                                          self.time.currentIndex() + 1)))
        self.last_t.clicked.connect(lambda: self.time.setCurrentIndex(self.time.count() - 1))
        self.rep.currentIndexChanged.connect(lambda *_: self._apply_display())
        self.cmap.currentIndexChanged.connect(lambda *_: self._apply_display())
        self.btn_fit.clicked.connect(self._fit)
        self.btn_x.clicked.connect(lambda: self._view("yz"))
        self.btn_y.clicked.connect(lambda: self._view("xz"))
        self.btn_z.clicked.connect(lambda: self._view("xy"))
        self.btn_iso.clicked.connect(lambda: (self.plotter.view_isometric(), self.plotter.render()))
        self.btn_shot.clicked.connect(self.screenshot)
        self.btn_render.clicked.connect(self.render_dialog)
        self.btn_ao.toggled.connect(self._set_ao)
        self.btn_side.toggled.connect(self.side.setVisible)
        self.plotter.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plotter.customContextMenuRequested.connect(self._context_menu)
        sv.case_changed.connect(lambda s: self.set_case(s.root if s else None))
        sv.files_changed.connect(self._on_files_changed)
        self._refresh_meta_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.request_meta)
        QTimer.singleShot(300, self._report_gpu)
        self.plotter.installEventFilter(self)
        sv.set_active_viewport(self)
        if sv.session is not None:
            self.set_case(sv.session.root)

    # ------------------------------------------------------------------ case / meta
    def set_case(self, root: Path | None) -> None:
        self.case_root = Path(root) if root else None
        self._clear_actors()
        self._first_data = True
        self.meta = {}
        if self.case_root is None:
            return
        case = self.ctx.services.session.case() if self.ctx.services.session else None
        if case is not None:
            reg = case.regions[0] if case.regions else None
            self._is2d = bool(reg and (reg.is_2d or reg.is_axisymmetric))
            n = case.n_cells or 0
            if n > 2_000_000:
                self.info.setText(f"{n:,} cells: boundary patches are shown by default; enable the internal mesh "
                                  "or a slice when needed.")
        self.request_meta()

    def request_meta(self) -> None:
        if self.case_root is None:
            return
        self.gen += 1
        self._req_meta.emit(self.vid, self.gen, {"case": str(self.case_root), "decomposed": self._decomposed()})

    def _decomposed(self) -> bool:
        s = self.ctx.services.session
        if s is None:
            return False
        c = s.case()
        return c.decomposed and not c.times[1:] if c.times else c.decomposed

    def _on_meta(self, vid: str, gen: int, meta: dict) -> None:
        if vid != self.vid or gen != self.gen:
            return
        prev_field, prev_time = self.field.currentText(), self.time.currentText()
        was_latest = self.time.currentIndex() == self.time.count() - 1
        self.meta = meta
        for w in (self.field, self.time, self.patches):
            w.blockSignals(True)
        self.field.clear()
        names = [display_name(a) for a in meta["cell_arrays"]]
        self._vtk_names = dict(zip(names, meta["cell_arrays"], strict=False))
        self.field.addItems(names)
        pref = prev_field or next((f for f in ("T", "U", "p", "p_rgh", "alpha.water") if f in names), names[0] if
                                  names else "")
        if pref in names:
            self.field.setCurrentText(pref)
        self.time.clear()
        self.time.addItems([f"{t:g}" for t in meta["times"]])
        if prev_time and not (self.follow.isChecked() and was_latest) and prev_time in [f"{t:g}" for t in meta["times"]]:
            self.time.setCurrentText(prev_time)
        else:
            self.time.setCurrentIndex(self._latest_time_with(self.field.currentText()))
        if self.patches.count() == 0 or self._patch_names() != meta["patches"]:
            self.patches.clear()
            big = (self.ctx.services.session.case().n_cells or 0) > 2_000_000 if self.ctx.services.session else False
            for p in meta["patches"]:
                it = QListWidgetItem(p)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                internal = p.endswith("internalMesh")
                it.setCheckState(Qt.CheckState.Checked if (internal and not big) or (not internal and big)
                                 else Qt.CheckState.Unchecked)
                self.patches.addItem(it)
        for w in (self.field, self.time, self.patches):
            w.blockSignals(False)
        self.request_load(reset_range=True)

    def _latest_time_with(self, field: str) -> int:
        """Index of the latest time folder that actually contains ``field`` (cheap existence check):
        e.g. SandiaFlameD/9999 holds only post-processing fields, not T."""
        times = self.meta.get("times", [])
        if not times or self.case_root is None:
            return len(times) - 1
        names = {}
        s = self.ctx.services.session
        if s is not None:
            for val, name in s.case().times:
                names[round(val, 12)] = name
        for i in range(len(times) - 1, -1, -1):
            d = names.get(round(times[i], 12), f"{times[i]:g}")
            base = self.case_root / d
            if (base / field).exists() or (base / f"{field}.gz").exists() \
                    or (self.case_root / "processor0" / d / field).exists():
                return i
        return len(times) - 1

    def _select_patch(self, cur, _prev=None) -> None:
        """Reader names look like 'patch/inlet' or 'fluid/patch/inlet' -> Properties panel."""
        if cur is None or cur.text().endswith("internalMesh"):
            return
        parts = cur.text().split("/")
        region = parts[0] if len(parts) >= 3 else ""
        self.ctx.services.select({"kind": "patch", "name": parts[-1], "region": region})

    def _patch_names(self) -> list[str]:
        return [self.patches.item(i).text() for i in range(self.patches.count())]

    def _on_field_changed(self, *_):
        if self.meta and not self.follow.isChecked():
            idx = self._latest_time_with(self.field.currentText())
            if idx != self.time.currentIndex():
                self.time.blockSignals(True)
                self.time.setCurrentIndex(idx)
                self.time.blockSignals(False)
        self.request_load(reset_range=True)

    def _on_files_changed(self, paths: list) -> None:
        if self.case_root is None:
            return
        for p in paths:
            try:
                rel = Path(p).relative_to(self.case_root)
            except ValueError:
                continue
            if rel.parts and rel.parts[0].replace(".", "", 1).replace("e-", "", 1).isdigit():
                self._refresh_meta_timer.start()  # a new/updated time folder (quiet for 1.5 s)
                return

    # ------------------------------------------------------------------ load
    def request_load(self, *, reset_range: bool = False) -> None:
        if self.case_root is None or not self.meta or self.time.currentIndex() < 0:
            return
        if reset_range and not self.lock.isChecked():
            self.range = None
        self.gen += 1
        patches = [self.patches.item(i).text() for i in range(self.patches.count())
                   if self.patches.item(i).checkState() == Qt.CheckState.Checked]
        req = {"case": str(self.case_root), "decomposed": self._decomposed(),
               "time": self.meta["times"][self.time.currentIndex()] if self.meta.get("times") else None,
               "field": self._vtk_names.get(self.field.currentText()), "component": self.comp.currentText(),
               "patches": patches, "show_internal": any(p.endswith("internalMesh") for p in patches),
               "stages": [{**s, "field": self._vtk_names.get(s["field"], s["field"])} if s.get("field") else s
                          for s in self.stages],
               "view": self.view_opts}
        if self.slice_on.isChecked() and self.bounds is not None:
            ax = self.slice_axis.currentIndex()
            b = self.bounds
            frac = self.slice_pos.value() / 1000
            origin = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
            origin[ax] = b[2 * ax] + frac * (b[2 * ax + 1] - b[2 * ax])
            normal = [0.0, 0.0, 0.0]
            normal[ax] = 1.0
            req["slice"] = {"origin": origin, "normal": normal}
            if not any(p.endswith("internalMesh") for p in patches):
                req["patches"] = [*patches, *[p for p in self.meta["patches"] if p.endswith("internalMesh")]]
        self._req_load.emit(self.vid, self.gen, req)

    def _on_data(self, vid: str, gen: int, data: dict) -> None:
        if vid != self.vid or gen != self.gen:
            return  # stale (a newer request superseded it)
        blocks: dict = data["blocks"]
        for name in list(self.actors):
            if name not in blocks:
                actor, _m = self.actors.pop(name)
                self.plotter.renderer.RemoveActor(actor)
        self.scalar = data["scalar"]
        self.stage_info = data.get("stage_info", {})
        if data["bounds"] is not None:
            self.bounds = data["bounds"]
        if self.range is None or not self.lock.isChecked():
            self.range = data["range"]
        for name, poly in blocks.items():
            pair = self.actors.get(name)
            if pair is None:
                mapper = vtk.vtkPolyDataMapper()
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                self.plotter.renderer.AddActor(actor)
                self.actors[name] = (actor, mapper)
            _actor, mapper = self.actors[name]
            mapper.SetInputData(poly)
        self._apply_display()
        if self._first_data:
            self._first_data = False
            self._fit()
        self.comp.setVisible(data.get("components", 0) > 1)
        msg = (f"{data['n_cells']:,} cells read · {data['seconds']:.2f} s · t = {data['time']:g}"
               if data["time"] is not None else "")
        if self.field.currentText() and data["scalar"] is None:
            msg += f" · '{self.field.currentText()}' is not written at this time"
            self.ctx.services.notify(f"Viewport: '{self.field.currentText()}' is not written at t = {data['time']:g}")
        interp = sorted({i["label"] for k, i in self.stage_info.items() if i.get("interpolated") and "~" not in k})
        if interp:
            msg += " · interpolated: " + ", ".join(interp)
        for k, i in self.stage_info.items():
            for note in i.get("notes", []) if "~" not in k else []:
                msg += f" · {i['label']}: {note}"
        self.info.setText(msg)
        self.plotter.render()
        self.stages_updated.emit(self.stage_info)

    def _apply_display(self) -> None:
        rep = self.rep.currentIndex()
        cmap = self.cmap.currentText()
        lo_hi = self.range
        if lo_hi is not None and cmap in _DIVERGING:
            m = max(abs(lo_hi[0]), abs(lo_hi[1]))
            lo_hi = (-m, m)
        lut = _lut(cmap, *(lo_hi or (0, 1)))
        stage_luts: dict[str, vtk.vtkLookupTable] = {}
        bar = (lut, self.scalar) if self.scalar and lo_hi is not None else None
        stage_bar = None
        for name, (actor, mapper) in self.actors.items():
            prop = actor.GetProperty()
            info = self.stage_info.get(name)
            if info is not None:  # a pipeline stage: its own array, range and colour map
                key = name.split("~")[0]
                prop.SetRepresentationToSurface()
                prop.SetEdgeVisibility(False)
                prop.SetOpacity(info.get("opacity", 1.0))
                if info.get("association") == "point":  # smooth, lit surfaces (isosurfaces, contours)
                    prop.SetInterpolationToPhong()
                    prop.SetAmbient(0.12)
                    prop.SetDiffuse(0.85)
                    prop.SetSpecular(0.35)
                    prop.SetSpecularPower(28)
                else:
                    prop.SetInterpolationToFlat()
                    prop.SetSpecular(0.0)
                prop.SetColor(0.85, 0.85, 0.88)
                if info.get("scalar") and info.get("range"):
                    slut = stage_luts.get(key)
                    if slut is None:
                        smap = info.get("cmap") or cmap
                        slut = stage_luts[key] = _lut(smap if smap in CMAPS or smap == "gray" else cmap,
                                                      *info["range"])
                    if info.get("association") == "point":
                        mapper.SetScalarModeToUsePointFieldData()
                    else:
                        mapper.SetScalarModeToUseCellFieldData()
                    mapper.SelectColorArray(info["scalar"])
                    mapper.SetLookupTable(slut)
                    mapper.SetUseLookupTableScalarRange(True)
                    mapper.ScalarVisibilityOn()
                    # the colour bar explains what the eye goes to: the selected stage, else the first stage
                    if key == self.bar_stage or (stage_bar is None and "~" not in name):
                        stage_bar = (slut, self._stage_bar_title(key, info))
                else:
                    mapper.ScalarVisibilityOff()
                continue
            # the base mesh: translucent by default once pipeline stages are shown (they sit inside it)
            base = self.view_opts.get("base") or ("translucent" if any(
                s.get("visible", True) for s in self.stages) else "surface")
            actor.SetVisibility(base != "hidden")
            prop.SetOpacity({"translucent": 0.15, "outline": 0.35}.get(base, 1.0))
            prop.SetInterpolationToFlat()
            if rep == 2 or base == "outline":
                prop.SetRepresentationToWireframe()
            else:
                prop.SetRepresentationToSurface()
            prop.SetEdgeVisibility(rep == 1 and base == "surface")
            prop.SetEdgeColor(0.15, 0.15, 0.17)
            prop.SetColor(0.65, 0.67, 0.70)
            if self.scalar and lo_hi is not None:
                mapper.SetScalarModeToUseCellFieldData()
                mapper.SelectColorArray(self.scalar)
                mapper.SetLookupTable(lut)
                mapper.SetUseLookupTableScalarRange(True)
                mapper.ScalarVisibilityOn()
            else:
                mapper.ScalarVisibilityOff()
        if stage_bar is not None and self.bar_stage != "field":
            bar = stage_bar
        if bar is not None:
            self.scalar_bar.SetLookupTable(bar[0])
            self.scalar_bar.SetTitle(display_name(bar[1]))
            self.scalar_bar.VisibilityOn()
        else:
            self.scalar_bar.VisibilityOff()
        self.plotter.render()

    def _stage_bar_title(self, key: str, info: dict) -> str:
        sid = key.split(":", 1)[1]
        s = next((x for x in self.stages if x.get("id") == sid), {})
        col = s.get("color_by") or ("U" if s.get("type") == "qcriterion" else s.get("field") or info.get("scalar"))
        return f"|{col}|" if col == "U" else str(col)

    def stage_bounds(self, stage_id: str | None = None) -> list[float] | None:
        """Bounds of one stage's geometry (or all visible stages)."""
        b = [float("inf"), float("-inf")] * 3
        hit = False
        for name, (actor, mapper) in self.actors.items():
            if not name.startswith("stage:") or not actor.GetVisibility():
                continue
            if stage_id and name.split("~")[0] != f"stage:{stage_id}":
                continue
            poly = mapper.GetInput()
            if poly is None or poly.GetNumberOfPoints() == 0:
                continue
            pb = poly.GetBounds()
            for k in range(3):
                b[2 * k] = min(b[2 * k], pb[2 * k])
                b[2 * k + 1] = max(b[2 * k + 1], pb[2 * k + 1])
            hit = True
        return b if hit else None

    def frame_bounds(self, b: list[float], padding: float = 0.6) -> None:
        """Zoom onto a box (e.g. a mesh hotspot), with generous context around it."""
        size = max(b[1] - b[0], b[3] - b[2], b[5] - b[4], 1e-12)
        pad = size * padding
        self.plotter.renderer.ResetCamera([b[0] - pad, b[1] + pad, b[2] - pad, b[3] + pad, b[4] - pad, b[5] + pad])
        self.plotter.camera_set = True
        self.plotter.render()

    def frame_stage(self, stage_id: str | None = None, padding: float = 0.08) -> bool:
        """Zoom the camera onto a stage (keeps the viewing direction)."""
        b = self.stage_bounds(stage_id)
        if b is None:
            return False
        pad = [(b[2 * k + 1] - b[2 * k]) * padding for k in range(3)]
        self.plotter.renderer.ResetCamera([b[0] - pad[0], b[1] + pad[0], b[2] - pad[1], b[3] + pad[1],
                                           b[4] - pad[2], b[5] + pad[2]])
        self.plotter.camera_set = True
        self.plotter.render()
        return True

    # ------------------------------------------------------------------ pipeline (Visualization panel)
    def set_stages(self, stages: list[dict]) -> None:
        self.stages = [dict(s) for s in stages]
        self.request_load()

    def set_view_options(self, opts: dict) -> None:
        self.view_opts = dict(opts)
        self.request_load()

    def set_bar_stage(self, stage_id: str | None) -> None:
        self.bar_stage = f"stage:{stage_id}" if stage_id else None
        self._apply_display()

    def field_names(self) -> list[str]:
        return [self.field.itemText(i) for i in range(self.field.count())]

    def showEvent(self, ev) -> None:  # the viewport you can see is the one the Visualization panel edits
        super().showEvent(ev)
        self.ctx.services.set_active_viewport(self)

    def hideEvent(self, ev) -> None:
        super().hideEvent(ev)
        sv = self.ctx.services
        if sv.active_viewport is self and sv.host is not None:
            others = [p for p in sv.host.panels_of("aether.viewport") if p is not self and p.isVisible()]
            sv.set_active_viewport(others[0] if others else None)

    def eventFilter(self, obj, ev) -> bool:  # Qt override: a click/focus makes this the active viewport
        from PySide6.QtCore import QEvent

        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.ctx.services.set_active_viewport(self)
        return False

    def _on_fail(self, vid: str, gen: int, msg: str) -> None:
        if vid == self.vid:
            self.info.setText(f"Could not read results: {msg}")
            self.ctx.services.notify(f"Viewport: {msg}", "warn")

    def _clear_actors(self) -> None:
        for actor, _m in self.actors.values():
            self.plotter.renderer.RemoveActor(actor)
        self.actors.clear()
        self.scalar_bar.VisibilityOff()
        self.plotter.render()

    # ------------------------------------------------------------------ camera
    def _fit(self) -> None:
        if self._is2d and self.bounds is not None:
            self._fit_2d()
        else:
            self.plotter.reset_camera()
            self.plotter.camera_set = True
        self.plotter.render()

    def _view(self, plane: str) -> None:
        {"xy": self.plotter.view_xy, "xz": self.plotter.view_xz, "yz": self.plotter.view_yz}[plane]()
        self.plotter.reset_camera()
        self.plotter.render()

    def _fit_2d(self, padding: float = 0.04) -> None:
        """2D / wedge cases: look along the THINNEST axis (empty or wedge direction -- e.g. y for
        SandiaFlameD, whose axis is z), longest in-plane axis horizontal, parallel projection scaled so
        the whole domain fills the widget."""
        b = self.bounds
        ext = [b[1] - b[0], b[3] - b[2], b[5] - b[4]]
        c = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
        thin = ext.index(min(ext))
        a, bb = [i for i in range(3) if i != thin]
        horiz, up = (a, bb) if ext[a] >= ext[bb] else (bb, a)
        w, h = max(ext[horiz], 1e-12), max(ext[up], 1e-12)
        aspect = max(self.plotter.width(), 1) / max(self.plotter.height(), 1)
        cam = self.plotter.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        dist = 10 * max(ext)
        pos = list(c)
        pos[thin] += dist
        vu = [0.0, 0.0, 0.0]
        vu[up] = 1.0
        cam.SetFocalPoint(*c)
        cam.SetPosition(*pos)
        cam.SetViewUp(*vu)
        # make the horizontal axis point to the right: flip the viewing side if it would be mirrored
        right = [0.0, 0.0, 0.0]
        right[horiz] = 1.0
        d = [c[i] - pos[i] for i in range(3)]
        cross = [vu[1] * d[2] - vu[2] * d[1], vu[2] * d[0] - vu[0] * d[2], vu[0] * d[1] - vu[1] * d[0]]
        if sum(cross[i] * right[i] for i in range(3)) > 0:
            pos[thin] = c[thin] - dist
            cam.SetPosition(*pos)
        # leave the bottom ~14% for the colour bar
        cam.SetParallelScale((1 + padding) * max(h / 2 / 0.86, w / (2 * aspect)))
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.camera_set = True  # stop pyvista from resetting our camera on its first render

    def _set_ao(self, on: bool) -> None:
        b = self.bounds or [0, 1, 0, 1, 0, 1]
        diag = ((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) ** 0.5 or 1.0
        try:
            if on:
                self.plotter.enable_ssao(radius=0.02 * diag, bias=0.002 * diag, kernel_size=128, blur=True)
            else:
                self.plotter.disable_ssao()
        except Exception as exc:
            self.ctx.services.notify(f"Ambient occlusion unavailable: {exc}", "warn")
        self.plotter.render()

    def render_image(self, path: str, width: int, height: int, *, background: str = "dark",
                     transparent: bool = False, msaa: int = 8, colorbar: bool = True, base: str = "as shown",
                     frame_stages: bool = False) -> str:
        """Offscreen render at full resolution with MSAA (a separate GL context: sharp edges and text at
        4K/8K, independent of the window size). Same actors, camera and colours as on screen."""
        rw = vtk.vtkRenderWindow()
        rw.SetOffScreenRendering(1)
        rw.SetMultiSamples(int(msaa))
        rw.SetAlphaBitPlanes(1)
        rw.SetSize(int(width), int(height))
        ren = vtk.vtkRenderer()
        rw.AddRenderer(ren)
        bg = {"dark": (0.105, 0.11, 0.122), "black": (0, 0, 0), "white": (1, 1, 1)}.get(background, (0.105, 0.11, 0.122))
        ren.SetBackground(*bg)
        if background == "gradient":
            ren.GradientBackgroundOn()
            ren.SetBackground(0.03, 0.035, 0.05)
            ren.SetBackground2(0.20, 0.22, 0.27)
        kit = vtk.vtkLightKit()
        kit.AddLightsToRenderer(ren)
        for name, (actor, mapper) in self.actors.items():
            is_stage = name in self.stage_info
            if not is_stage and base == "hidden":
                continue
            if not actor.GetVisibility() and not (not is_stage and base in ("outline", "translucent")):
                continue
            m = vtk.vtkPolyDataMapper()
            m.ShallowCopy(mapper)
            m.SetInputData(mapper.GetInput())
            a = vtk.vtkActor()
            a.SetMapper(m)
            a.GetProperty().DeepCopy(actor.GetProperty())
            if not is_stage and base == "outline":
                a.GetProperty().SetRepresentationToWireframe()
                a.GetProperty().SetOpacity(0.12)
                m.ScalarVisibilityOff()
                a.GetProperty().SetColor(0.75, 0.78, 0.85)
            elif not is_stage and base == "translucent":
                a.GetProperty().SetOpacity(0.12)
            ren.AddActor(a)
        if base in ("outline", "translucent", "as shown"):
            rw.SetAlphaBitPlanes(1)
            ren.SetUseDepthPeeling(1)
            ren.SetMaximumNumberOfPeels(6)
        cam = vtk.vtkCamera()
        cam.DeepCopy(self.plotter.renderer.GetActiveCamera())
        ren.SetActiveCamera(cam)
        sb_bounds = self.stage_bounds() if frame_stages else None
        if sb_bounds is not None:
            pad = [(sb_bounds[2 * k + 1] - sb_bounds[2 * k]) * 0.06 for k in range(3)]
            ren.ResetCamera([sb_bounds[0] - pad[0], sb_bounds[1] + pad[0], sb_bounds[2] - pad[1],
                             sb_bounds[3] + pad[1], sb_bounds[4] - pad[2], sb_bounds[5] + pad[2]])
        if colorbar and self.scalar_bar.GetVisibility():
            sb = vtk.vtkScalarBarActor()
            sb.ShallowCopy(self.scalar_bar)
            scale = height / max(1, self.plotter.height())
            for tp_src, tp in ((self.scalar_bar.GetLabelTextProperty(), sb.GetLabelTextProperty()),
                               (self.scalar_bar.GetTitleTextProperty(), sb.GetTitleTextProperty())):
                tp.ShallowCopy(tp_src)
                tp.SetFontSize(max(8, round(tp_src.GetFontSize() * scale)))
                if background == "white":
                    tp.SetColor(0.1, 0.1, 0.1)
            ren.AddViewProp(sb)
        ren.ResetCameraClippingRange()
        rw.Render()
        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(rw)
        if transparent:
            w2i.SetInputBufferTypeToRGBA()
        w2i.ReadFrontBufferOff()
        w2i.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(path)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()
        rw.Finalize()
        return path

    def beauty_scene(self, width: int, height: int, *, base: str = "outline", frame_stages: bool = True,
                     background: str = "gradient", spp: int = 32):
        """The visible scene for a ParaView path-traced render (aethercfd.results.beauty)."""
        from aethercfd.results.beauty import Block, Scene

        blocks = []
        bar_title = self.scalar_bar.GetTitle() if self.scalar_bar.GetVisibility() else None
        bar_done = False
        for name, (actor, mapper) in self.actors.items():
            is_stage = name in self.stage_info
            if not is_stage and base == "hidden":
                continue
            if not actor.GetVisibility() and not (not is_stage and base in ("outline", "translucent")):
                continue
            prop = actor.GetProperty()
            poly = mapper.GetInput()
            if poly is None or poly.GetNumberOfCells() == 0:
                continue
            arr, assoc, rng, cmap = None, "cell", None, self.cmap.currentText()
            if mapper.GetScalarVisibility() and not (not is_stage and base == "outline"):
                arr = mapper.GetArrayName()
                assoc = "point" if mapper.GetScalarMode() == vtk.VTK_SCALAR_MODE_USE_POINT_FIELD_DATA else "cell"
                lut = mapper.GetLookupTable()
                rng = tuple(lut.GetRange()) if lut is not None else None
                if is_stage:
                    cmap = self.stage_info[name].get("cmap") or cmap
            show_bar = bool(arr and bar_title and not bar_done and (is_stage or not self.stage_info))
            bar_done = bar_done or show_bar
            blocks.append(Block(poly, arr, assoc, rng, cmap,
                                opacity=(0.12 if not is_stage and base in ("outline", "translucent") else
                                         prop.GetOpacity()),
                                wireframe=(not is_stage and base == "outline") or
                                prop.GetRepresentation() == vtk.VTK_WIREFRAME,
                                smooth=prop.GetInterpolation() != vtk.VTK_FLAT, color=tuple(prop.GetColor()),
                                bar=show_bar, title=bar_title or "",
                                outline=not is_stage and base == "outline"))
        cam = vtk.vtkCamera()
        cam.DeepCopy(self.plotter.renderer.GetActiveCamera())
        if frame_stages and self.stage_bounds() is not None:
            saved = vtk.vtkCamera()
            saved.DeepCopy(self.plotter.renderer.GetActiveCamera())
            self.frame_stage(None, padding=0.06)
            cam.DeepCopy(self.plotter.renderer.GetActiveCamera())
            self.plotter.renderer.GetActiveCamera().DeepCopy(saved)
            self.plotter.render()
        bg = {"gradient": [(0.03, 0.035, 0.05), (0.20, 0.22, 0.27)], "dark": [(0.105, 0.11, 0.122)],
              "black": [(0, 0, 0)], "white": [(1, 1, 1)]}.get(background, [(0.105, 0.11, 0.122)])
        return Scene(blocks, {"position": list(cam.GetPosition()), "focal_point": list(cam.GetFocalPoint()),
                              "view_up": list(cam.GetViewUp()), "view_angle": cam.GetViewAngle(),
                              "parallel": bool(cam.GetParallelProjection()),
                              "parallel_scale": cam.GetParallelScale()}, (width, height), bg, True, spp)

    def render_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout

        dlg = QDialog(self)
        dlg.setWindowTitle("Render image")
        form = QFormLayout(dlg)
        res = QComboBox()
        for label, wh in (("1920 × 1080 (Full HD)", (1920, 1080)), ("2560 × 1440 (QHD)", (2560, 1440)),
                          ("3840 × 2160 (4K)", (3840, 2160)), ("7680 × 4320 (8K)", (7680, 4320)),
                          ("Square 3000 × 3000", (3000, 3000))):
            res.addItem(label, wh)
        res.setCurrentIndex(2)
        bg = QComboBox()
        bg.addItems(["dark", "gradient", "black", "white"])
        transparent = QCheckBox("Transparent background (PNG alpha)")
        bar = QCheckBox("Colour bar")
        bar.setChecked(True)
        base = QComboBox()
        base.addItems(["as shown", "outline", "translucent", "hidden"])
        if self.stages:
            base.setCurrentText("outline")
        frame = QCheckBox("Frame the visualization stages (zoom onto them)")
        frame.setChecked(bool(self.stages))
        frame.setEnabled(bool(self.stages))
        from aethercfd.results.beauty import find_pvbatch

        pvb = find_pvbatch(getattr(self.ctx.services.settings, "paraview_exe", None))
        ray = QCheckBox("Ray-traced: path tracing + shadows + denoiser (ParaView" +
                        (f", {pvb.parent.parent.name})" if pvb else " not found)"))
        ray.setEnabled(pvb is not None)
        spp = QComboBox()
        for label, n in (("draft (8 samples)", 8), ("good (32)", 32), ("final (128)", 128)):
            spp.addItem(label, n)
        spp.setCurrentIndex(1)
        form.addRow("Resolution", res)
        form.addRow("Background", bg)
        form.addRow("Mesh surfaces", base)
        form.addRow(frame)
        form.addRow(transparent)
        form.addRow(bar)
        form.addRow(ray)
        form.addRow("Ray-tracing quality", spp)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        start = str((self.case_root or Path.home()) / f"render_{self.field.currentText()}_{self.time.currentText()}.png")
        f, _ = QFileDialog.getSaveFileName(self, "Save rendered image", start, "PNG (*.png)")
        if not f:
            return
        w, h = res.currentData()
        if ray.isChecked() and pvb is not None:
            from aethercfd.results.beauty import render as beauty

            scene = self.beauty_scene(w, h, base=base.currentText(), frame_stages=frame.isChecked(),
                                      background=bg.currentText(), spp=int(spp.currentData()))
            self.ctx.services.notify(f"Ray tracing {w}×{h} in ParaView (runs in the background)…")
            self.ctx.services.run_bg(lambda: beauty(scene, f, pvbatch=pvb), lambda r: self.ctx.services.notify(
                (f"Ray-traced render ({r[0]['backend']}): {f}" if r[0] and r[0]["ok"] else
                 f"Ray-traced render failed: {r[1] or (r[0] or {}).get('log', '')[-300:]}"),
                "info" if r[0] and r[0]["ok"] else "error"))
            return
        try:
            self.render_image(f, w, h, background=bg.currentText(), transparent=transparent.isChecked(),
                              colorbar=bar.isChecked(), base=base.currentText(), frame_stages=frame.isChecked())
            self.ctx.services.notify(f"Rendered {w}×{h}: {f}")
        except Exception as exc:
            self.ctx.services.notify(f"Render failed: {exc}", "error")

    def screenshot(self) -> None:
        start = str((self.case_root or Path.home()) / f"view_{self.field.currentText()}_{self.time.currentText()}.png")
        f, _ = QFileDialog.getSaveFileName(self, "Save screenshot", start, "PNG (*.png)")
        if f:
            self.plotter.screenshot(f, scale=2)
            self.ctx.services.notify(f"Saved {f}")

    def _context_menu(self, pos) -> None:
        m = QMenu(self)
        m.addAction("Probe cell here", lambda: self._probe(pos))
        m.addAction("Fit view", self._fit)
        m.exec(self.plotter.mapToGlobal(pos))

    def _probe(self, pos) -> None:
        dpr = self.plotter.devicePixelRatioF()
        x, y = pos.x() * dpr, (self.plotter.height() - pos.y()) * dpr
        picker = vtk.vtkCellPicker()
        picker.SetTolerance(0.0005)
        if not picker.Pick(x, y, 0, self.plotter.renderer):
            self.ctx.services.notify("Probe: nothing under the cursor")
            return
        ds = picker.GetDataSet()
        cid = picker.GetCellId()
        val = None
        if ds is not None and self.scalar and cid >= 0:
            arr = ds.GetCellData().GetArray(self.scalar)
            if arr is not None:
                val = arr.GetTuple(cid)
        p = picker.GetPickPosition()
        txt = f"Probe at ({p[0]:.4g}, {p[1]:.4g}, {p[2]:.4g})"
        if val is not None:
            txt += f"  {display_name(self.scalar)} = {', '.join(f'{v:.5g}' for v in val)}"
        self.ctx.services.notify(txt)
        # all fields at that cell -> Properties panel (read on the worker)
        self._req_probe.emit(self.vid, self.gen, {
            "case": str(self.case_root), "decomposed": self._decomposed(), "point": list(p),
            "time": self.meta["times"][self.time.currentIndex()] if self.meta.get("times") else None})

    def _on_probe(self, vid: str, gen: int, data: dict) -> None:
        if vid != self.vid:
            return
        values = {display_name(k): v for k, v in data.get("values", {}).items()}
        self.ctx.services.select({"kind": "cell", "source": self.ctx.instance_id, **data, "values": values})

    def _report_gpu(self) -> None:
        try:
            caps = self.plotter.ren_win.ReportCapabilities()
        except Exception:
            return
        renderer = next((ln.split(":", 1)[1].strip() for ln in caps.splitlines() if "renderer string" in ln), "")
        if renderer and self.ctx.services.window is not None:
            self.ctx.services.gl_renderer = renderer
            self.ctx.services.window.set_gpu(renderer)

    # ------------------------------------------------------------------ scripted control (copilot)
    def show_field(self, field: str, time: str | None = None, slice_axis: str | None = None) -> str:
        names = [self.field.itemText(i) for i in range(self.field.count())]
        if not names:
            return "the viewport has no results loaded for this case yet"
        if field not in names:
            return f"field {field!r} is not available; available: {', '.join(names[:40])}"
        self.field.setCurrentText(field)
        if time is not None:
            times = [self.time.itemText(i) for i in range(self.time.count())]
            try:
                t = f"{float(time):g}"
            except ValueError:
                t = time
            if t not in times:
                return f"time {time} is not available; times: {', '.join(times[-15:])}"
            self.follow.setChecked(False)
            self.time.setCurrentText(t)
        if slice_axis and slice_axis.lower()[:1] in ("x", "y", "z"):
            self.slice_axis.setCurrentIndex("xyz".index(slice_axis.lower()[0]))
            self.slice_pos.setValue(500)
            self.slice_on.setChecked(True)
            self.btn_side.setChecked(True)
        else:
            self.slice_on.setChecked(False)
        self.request_load(reset_range=True)
        return (f"showing {field} at t = {self.time.currentText()}"
                + (f", mid-plane slice normal to {slice_axis}" if self.slice_on.isChecked() else ""))

    # ------------------------------------------------------------------ comparisons
    def set_range(self, lo: float, hi: float) -> None:
        """Fix the colour range (and lock it), e.g. the same range in side-by-side views."""
        self.range = (float(lo), float(hi))
        self.lock.setChecked(True)
        self._apply_display()
        self.plotter.render()

    def set_cmap(self, name: str) -> None:
        if name in CMAPS:
            self.cmap.setCurrentText(name)

    def link_camera(self, others: list) -> None:
        """Every camera move here is copied to the other viewports (and theirs to this one)."""
        self._linked = [o for o in others if o is not self]
        if getattr(self, "_cam_obs", None) is None:
            self._syncing = False

            def moved(*_a):
                if self._syncing:
                    return
                cam = self.plotter.camera
                for o in getattr(self, "_linked", []):
                    o._syncing = True
                    try:
                        oc = o.plotter.camera
                        oc.SetPosition(cam.GetPosition())
                        oc.SetFocalPoint(cam.GetFocalPoint())
                        oc.SetViewUp(cam.GetViewUp())
                        oc.SetViewAngle(cam.GetViewAngle())
                        oc.SetParallelScale(cam.GetParallelScale())
                        o.plotter.renderer.ResetCameraClippingRange()
                        o.plotter.render()
                    finally:
                        o._syncing = False
            iren = self.plotter.iren.interactor if hasattr(self.plotter.iren, "interactor") else self.plotter.iren
            # live while dragging (InteractionEvent), at the end, and for wheel zooms
            self._cam_obs = [iren.AddObserver(e, moved) for e in ("InteractionEvent", "EndInteractionEvent",
                                                                     "MouseWheelForwardEvent",
                                                                     "MouseWheelBackwardEvent")]
            self._cam_moved = moved

    def current_range(self) -> tuple[float, float] | None:
        return self.range

    # ------------------------------------------------------------------ panel API
    def save_state(self) -> dict:
        cam = self.plotter.camera
        return {"field": self.field.currentText(), "component": self.comp.currentText(),
                "rep": self.rep.currentIndex(), "cmap": self.cmap.currentText(), "follow": self.follow.isChecked(),
                "stages": self.stages, "view": self.view_opts,
                "camera": {"position": list(cam.position), "focal_point": list(cam.focal_point),
                           "view_up": list(cam.up)}}

    def restore_state(self, state: dict) -> None:
        self.rep.setCurrentIndex(int(state.get("rep", 0)))
        if state.get("cmap") in CMAPS:
            self.cmap.setCurrentText(state["cmap"])
        self.follow.setChecked(bool(state.get("follow", False)))
        if state.get("component"):
            self.comp.setCurrentText(state["component"])
        self.stages = [s for s in state.get("stages", []) if isinstance(s, dict) and s.get("type")]
        self.view_opts = dict(state.get("view") or {})

    def dispose(self) -> None:
        if getattr(self.ctx.services, "active_viewport", None) is self:
            self.ctx.services.set_active_viewport(None)
        try:
            self.plotter.close()
        except Exception:
            log.exception("closing the viewport failed")


PANELS = [PanelSpec("aether.viewport", "3D Viewport", "mdi6.cube-outline", "Visualization", ViewportPanel,
                    multi_instance=True, default_area=Area.CENTER, close_policy="destroy",
                    requires=frozenset({"vtk"}),
                    description="Results on the real finite-volume mesh: fields, time, slices, probe")]
