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
        self.btn_shot = _tool("msc.device-camera", "Screenshot (PNG, 2x)")
        self.btn_side = _tool("msc.layout-sidebar-right", "Display options", checkable=True)
        for b in (self.btn_fit, self.btn_x, self.btn_y, self.btn_z, self.btn_iso, self.btn_shot, self.btn_side):
            bar.addWidget(b)
        lay.addLayout(bar)
        # ---------------- view + sidebar
        split = QSplitter(Qt.Orientation.Horizontal)
        self.plotter = QtInteractor(self, auto_update=False)
        self.plotter.set_background(T.viewport_bg)
        self.plotter.enable_anti_aliasing("fxaa")
        self.plotter.add_axes(color=T.text_dim)
        split.addWidget(self.plotter)
        side = QWidget()
        sl = QFormLayout(side)
        self.patches = QListWidget()
        self.patches.itemChanged.connect(lambda *_: self.request_load())
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
        w.meta_ready.connect(self._on_meta)
        w.data_ready.connect(self._on_data)
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
        self.btn_side.toggled.connect(self.side.setVisible)
        self.plotter.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plotter.customContextMenuRequested.connect(self._context_menu)
        sv.case_changed.connect(lambda s: self.set_case(s.root if s else None))
        sv.files_changed.connect(self._on_files_changed)
        self._refresh_meta_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.request_meta)
        QTimer.singleShot(300, self._report_gpu)
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
            if (base / field).exists() or (base / f"{field}.gz").exists() or                     (self.case_root / "processor0" / d / field).exists():
                return i
        return len(times) - 1

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
               "patches": patches}
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
        self.info.setText(msg)
        self.plotter.render()

    def _apply_display(self) -> None:
        rep = self.rep.currentIndex()
        cmap = self.cmap.currentText()
        lo_hi = self.range
        if lo_hi is not None and cmap in _DIVERGING:
            m = max(abs(lo_hi[0]), abs(lo_hi[1]))
            lo_hi = (-m, m)
        lut = _lut(cmap, *(lo_hi or (0, 1)))
        for _name, (actor, mapper) in self.actors.items():
            prop = actor.GetProperty()
            prop.SetInterpolationToFlat()
            prop.SetRepresentationToWireframe() if rep == 2 else prop.SetRepresentationToSurface()
            prop.SetEdgeVisibility(rep == 1)
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
        if self.scalar and lo_hi is not None:
            self.scalar_bar.SetLookupTable(lut)
            self.scalar_bar.SetTitle(display_name(self.scalar))
            self.scalar_bar.VisibilityOn()
        else:
            self.scalar_bar.VisibilityOff()
        self.plotter.render()

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
        txt = f"Probe at ({p[0]:.4g}, {p[1]:.4g}, {p[2]:.4g})  cell {cid}"
        if val is not None:
            txt += f"  {display_name(self.scalar)} = {', '.join(f'{v:.5g}' for v in val)}"
        self.ctx.services.notify(txt)

    def _report_gpu(self) -> None:
        try:
            caps = self.plotter.ren_win.ReportCapabilities()
        except Exception:
            return
        renderer = next((ln.split(":", 1)[1].strip() for ln in caps.splitlines() if "renderer string" in ln), "")
        if renderer and self.ctx.services.window is not None:
            self.ctx.services.gl_renderer = renderer
            self.ctx.services.window.set_gpu(renderer)

    # ------------------------------------------------------------------ panel API
    def save_state(self) -> dict:
        cam = self.plotter.camera
        return {"field": self.field.currentText(), "component": self.comp.currentText(),
                "rep": self.rep.currentIndex(), "cmap": self.cmap.currentText(), "follow": self.follow.isChecked(),
                "camera": {"position": list(cam.position), "focal_point": list(cam.focal_point),
                           "view_up": list(cam.up)}}

    def restore_state(self, state: dict) -> None:
        self.rep.setCurrentIndex(int(state.get("rep", 0)))
        if state.get("cmap") in CMAPS:
            self.cmap.setCurrentText(state["cmap"])
        self.follow.setChecked(bool(state.get("follow", False)))
        if state.get("component"):
            self.comp.setCurrentText(state["component"])

    def dispose(self) -> None:
        try:
            self.plotter.close()
        except Exception:
            log.exception("closing the viewport failed")


PANELS = [PanelSpec("aether.viewport", "3D Viewport", "mdi6.cube-outline", "Visualization", ViewportPanel,
                    multi_instance=True, default_area=Area.CENTER, close_policy="destroy",
                    requires=frozenset({"vtk"}),
                    description="Results on the real finite-volume mesh: fields, time, slices, probe")]
