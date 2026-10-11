"""3D Viewport: OpenFOAM results on the REAL finite-volume mesh (cell data, flat shading).

pyvistaqt's QtInteractor provides the render window/interactor; actors, mappers and lookup tables
are plain VTK so a new time step only swaps mapper inputs (no actor rebuild, ADR 0005). Reading and
filtering happen on the shared VtkWorker thread.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import vtk
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
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
from aethercfd.studio.panels.viewport import look as lk
from aethercfd.studio.panels.viewport.views import ViewToolsMixin
from aethercfd.studio.panels.viewport.worker import shared_worker
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

log = logging.getLogger(__name__)
PARAVIEW_SCRIPT = """# AetherCFD: open the case in ParaView (generated; safe to edit or delete)
from paraview.simple import *
r = OpenFOAMReader(registrationName={name!r}, FileName={control!r})
r.CaseType = 'Decomposed Case' if {decomposed} else 'Reconstructed Case'
r.UpdatePipelineInformation()
avail = list(r.GetProperty('MeshRegions').Available) if hasattr(r.GetProperty('MeshRegions'), 'Available') else []
inner = [m for m in avail if m.endswith('internalMesh')]
regions = [m for m in inner if m.strip('/').count('/') >= 1]
if inner:
    r.MeshRegions = regions or inner
r.UpdatePipelineInformation()
scene = GetAnimationScene()
scene.UpdateAnimationUsingDataTimeSteps()
if {time} is not None:
    scene.AnimationTime = {time}
view = GetActiveViewOrCreate('RenderView')
d = Show(r, view)
if {field!r}:
    try:
        ColorBy(d, ('CELLS', {field!r}))
        d.RescaleTransferFunctionToDataRange(True, False)
        d.SetScalarBarVisibility(view, True)
    except Exception as exc:
        print('could not colour by', {field!r}, exc)
view.ResetCamera()
Render()
"""
CMAPS = ["viridis", "turbo", "inferno", "magma", "plasma", "cividis", "coolwarm", "RdBu_r", "jet", "gray"]
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


def _text_tool(text: str, tip: str) -> QToolButton:
    b = QToolButton(text=text, toolTip=tip, checkable=True, autoRaise=True)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    return b


def _menu_tool(ic: str, text: str, tip: str) -> QToolButton:
    b = _tool(ic, tip)
    b.setText(text)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    return b


SMOOTHINGS = ("smooth", "cells")


class ViewportPanel(ViewToolsMixin, Panel):
    STATE_VERSION = 1
    _req_meta = Signal(str, int, dict)
    _req_load = Signal(str, int, dict)
    _req_probe = Signal(str, int, dict)
    _req_line = Signal(str, int, dict)
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
        self.look_box = QComboBox(toolTip="Look: Studio = AetherCFD's post-processing (smooth shading, outlines, "
                                          "depth); Classic = ParaView-style true cell colours")
        self.look_box.addItems(["Studio", "Classic"])
        # Smooth / Cells: how a cell-centred field is drawn, independent of the look
        self.btn_smooth = _text_tool("Smooth", "Smooth: colours, sections and contours interpolated from the cell "
                                               "values (no mesh stair-steps); labelled 'smoothed'")
        self.btn_cells = _text_tool("Cells", "Cells: every cell's own value, contours per cell (the stair-steps "
                                             "are the mesh resolution)")
        self._smooth_group = QButtonGroup(self)
        self._smooth_group.setExclusive(True)
        self._smooth_group.addButton(self.btn_smooth)
        self._smooth_group.addButton(self.btn_cells)
        self.btn_edges = _tool("mdi6.grid", "Mesh edges on the surfaces and sections (Cells mode)", checkable=True)
        self.cmap = QComboBox(toolTip="Colour map (Studio picks one per field until you choose)")
        self.cmap.addItems(CMAPS)
        self.lock = QCheckBox("Lock range", toolTip="Keep the colour range when the time changes")
        # multi-region (CHT) cases: which regions are shown (and sliced, and run through the stages)
        self.region_btn = QToolButton(toolTip="Regions of this multi-region (CHT) case to show: slices and "
                                              "visualization stages use only the shown regions")
        self.region_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.region_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.region_menu = QMenu(self.region_btn)
        self.region_btn.setMenu(self.region_menu)
        self.region_btn.setVisible(False)
        self.region_sel: list[str] | None = None  # shown regions; None = all (or a single-region case)
        self.region_kinds: dict[str, str] = {}  # region -> 'fluid' | 'solid'
        self.block_regions: dict[str, str] = {}  # drawn block -> its region (per-region depth offsets)
        for w in (QLabel("Field"), self.field, self.comp, self.region_btn, QLabel("  t"), self.prev_t, self.time,
                  self.next_t,
                  self.last_t, self.follow, QLabel("  "), self.look_box, self.btn_smooth, self.btn_cells,
                  self.btn_edges, self.rep, self.cmap, self.lock):
            bar.addWidget(w)
        bar.addStretch(1)
        self.btn_fit = _tool("msc.screen-full", "Fit view")
        self.btn_views = _menu_tool("mdi6.cube-scan", "Views", "Standard views (front, top, right, iso...), saved "
                                                               "views and the quad view")
        self.btn_sections = _menu_tool("mdi6.box-cutter", "Sections", "Cut the mesh: mid-planes, three orthogonal "
                                                                      "planes, a slice stack, a plane through 3 "
                                                                      "points; contours, arrows, line probes, "
                                                                      "cut-away")
        self.btn_flow = _menu_tool("mdi6.waves", "Flow paths", "Streamlines from the inlets, a point, a line or a "
                                                               "plane; pathlines; residence time")
        self.btn_ao = _tool("mdi6.circle-half-full", "Ambient occlusion (depth cues for 3D surfaces)", checkable=True)
        self.btn_shot = _tool("msc.device-camera", "Screenshot (PNG, 2x)")
        self.btn_render = _tool("mdi6.image-filter-hdr", "Render image… (up to 8K, 8x anti-aliasing)")
        self.btn_side = _tool("msc.layout-sidebar-right", "Display options", checkable=True)
        self.btn_rec = _tool("mdi6.movie-open-outline", "Animation: turntable or time sweep (GIF / WebP)")
        self.btn_show = _tool("mdi6.creation", "Showcase: the best first view of this result (Studio look, "
                                               "mid-plane slice in a glass shell for 3D, isometric camera)")
        self.btn_pv = _tool("mdi6.alpha-p-box-outline", "Open this case in ParaView (with this field and time)")
        for b in (self.btn_show, self.btn_sections, self.btn_flow, self.btn_views, self.btn_fit, self.btn_ao,
                  self.btn_shot, self.btn_render, self.btn_rec, self.btn_pv, self.btn_side):
            bar.addWidget(b)
        lay.addLayout(bar)
        # ---------------- view + sidebar
        split = QSplitter(Qt.Orientation.Horizontal)
        self.plotter = QtInteractor(self, auto_update=False)
        self.plotter.set_background(T.viewport_bg)
        self.plotter.enable_anti_aliasing("fxaa")
        self.plotter.enable_depth_peeling(number_of_peels=4)  # correct order for translucent base meshes
        self.plotter.enable_lightkit()  # key/fill/back lights: shape reads clearly on smooth surfaces
        # coincident faces (the surfaces of two regions at their interface) get per-mapper depth offsets, so
        # one region always wins there instead of the two flickering in dark streaks (z-fighting)
        vtk.vtkMapper.SetResolveCoincidentTopologyToPolygonOffset()
        self.plotter.add_axes(color=T.text_dim)
        split.addWidget(self.plotter)
        side = QWidget()
        sl = QFormLayout(side)
        self.patches = QListWidget()
        self.patches.itemChanged.connect(self._on_patch_toggled)
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
        self.flow_bar = vtk.vtkScalarBarActor()  # flow paths' own colour bar (e.g. residence time)
        self.flow_bar.VisibilityOff()
        self.plotter.renderer.AddViewProp(self.flow_bar)
        self.bar_lut = None  # the colour bar's own LUT, in display units (e.g. MPa)
        self.hud = lk.make_hud()
        self.plotter.renderer.AddViewProp(self.hud)
        self.edge_actor, self.edge_mapper = lk.make_edges_actor()
        self.plotter.renderer.AddActor(self.edge_actor)
        self._units: dict[str, str | None] = {}
        self._cmap_user = False
        self._orient = None
        self._sweep = None  # a time-sweep animation in progress
        self._ao_diag = None  # the domain size the ambient-occlusion pass was built for
        self.smoothed = False
        self.look = getattr(sv.settings, "viewport_look", "studio") if getattr(sv, "settings", None) else "studio"
        if self.look not in lk.LOOKS:
            self.look = "studio"
        self.look_box.setCurrentText(self.look.capitalize())
        # Smooth / Cells: the user's explicit choice (settings.viewport_smoothing) wins over the look's default
        chosen = getattr(sv.settings, "viewport_smoothing", "auto") if getattr(sv, "settings", None) else "auto"
        self._smooth_explicit = chosen in SMOOTHINGS
        self.smoothing = chosen if chosen in SMOOTHINGS else ("smooth" if self.look == "studio" else "cells")
        self.cell_edges = False
        self._sync_smooth_buttons()
        # sections, views, flow paths (ViewToolsMixin)
        self.section_show = {"field": True, "edges": False, "contours": False, "arrows": False}
        self.flow_colour = "U"
        self._plane_widget = None
        self._pick = None
        self._quad = None
        self._line_actor = None
        self._line_rid = 0
        self._line_plot = True
        self.last_line = None
        self.section_2d = None
        self._last_request = 0.0
        self._drag_timer = QTimer(self, singleShot=True, interval=90, timeout=self._drag_load)
        self._build_view_menus()
        # ---------------- wiring
        self._slice_timer = QTimer(self, singleShot=True, interval=150, timeout=self.request_load)
        w = shared_worker()
        self._req_meta.connect(w.meta, Qt.ConnectionType.QueuedConnection)
        self._req_load.connect(w.load, Qt.ConnectionType.QueuedConnection)
        self._req_probe.connect(w.probe, Qt.ConnectionType.QueuedConnection)
        self._req_line.connect(w.probe_line, Qt.ConnectionType.QueuedConnection)
        w.meta_ready.connect(self._on_meta)
        w.data_ready.connect(self._on_data)
        w.probe_ready.connect(self._on_probe)
        w.line_ready.connect(self._on_line)
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
        self.cmap.activated.connect(lambda *_: setattr(self, "_cmap_user", True))
        self.look_box.currentTextChanged.connect(lambda t: self.set_look(t.lower()))
        self.btn_smooth.clicked.connect(lambda: self.set_smoothing("smooth"))
        self.btn_cells.clicked.connect(lambda: self.set_smoothing("cells"))
        self.btn_edges.toggled.connect(lambda on: self.set_smoothing(self.smoothing, edges=on, explicit=False))
        self.btn_rec.clicked.connect(self.animation_dialog)
        self.btn_show.clicked.connect(lambda: self.ctx.services.notify("Viewport: " + self.showcase()))
        self.btn_pv.clicked.connect(self.open_in_paraview)
        self.btn_fit.clicked.connect(self._fit)
        self.btn_shot.clicked.connect(self.screenshot)
        self.btn_render.clicked.connect(self.render_dialog)
        self.btn_ao.toggled.connect(self._set_ao)
        self.btn_side.toggled.connect(self.side.setVisible)
        self.plotter.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.plotter.customContextMenuRequested.connect(self._context_menu)
        sv.case_changed.connect(self._on_case_changed)
        sv.files_changed.connect(self._on_files_changed)
        self._refresh_meta_timer = QTimer(self, singleShot=True, interval=1500, timeout=self.request_meta)
        QTimer.singleShot(300, self._report_gpu)
        self.plotter.installEventFilter(self)
        self._apply_scene()
        sv.set_active_viewport(self)
        if sv.session is not None:
            self.set_case(sv.session.root)

    # ------------------------------------------------------------------ case / meta
    def _on_case_changed(self, session) -> None:
        self.set_case(session.root if session else None)

    def set_case(self, root: Path | None) -> None:
        self._end_sweep("the case changed")
        self._drop_plane_widget()
        self._cancel_pick()
        self._steady = None
        if self._line_actor is not None:
            self._line_actor.SetVisibility(False)
        self.gen += 1  # anything still loading for the previous case is now stale
        self._is2d = False
        self.case_root = Path(root) if root else None
        self._queued_show = None
        self._showcase_pending = False
        self.bounds = None  # never the previous case's
        for w in (self.field, self.time):  # never offer the previous case's fields and times
            w.blockSignals(True)
            w.clear()
            w.blockSignals(False)
        self._clear_actors()
        self._first_data = True
        self.meta = {}
        self.region_sel, self.region_kinds, self.block_regions = None, {}, {}
        self.region_btn.setVisible(False)
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
        self._units.clear()
        self._apply_scene()  # 2D cases: no ambient occlusion
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
        queued, self._queued_show = getattr(self, "_queued_show", None), None
        if queued is not None:
            QTimer.singleShot(0, lambda q=queued: self.ctx.services.notify("Viewport: " + self.show_field(*q)))
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
        if self.look == "studio" and not self._cmap_user and self.field.currentText():
            self.cmap.blockSignals(True)  # the first field gets its Studio colour map too
            self.cmap.setCurrentText(lk.auto_cmap(self.field.currentText()))
            self.cmap.blockSignals(False)
        self.time.clear()
        self.time.addItems([f"{t:g}" for t in meta["times"]])
        if prev_time and not (self.follow.isChecked() and was_latest) and prev_time in [f"{t:g}" for t in meta["times"]]:
            self.time.setCurrentText(prev_time)
        else:
            self.time.setCurrentIndex(self._latest_time_with(self.field.currentText()))
        if self.patches.count() == 0 or self._patch_names() != meta["patches"]:
            self.patches.clear()
            from aethercfd.results.foam_reader import region_of

            big = (self.ctx.services.session.case().n_cells or 0) > 2_000_000 if self.ctx.services.session else False
            multi = bool(meta.get("regions"))
            for p in meta["patches"]:
                it = QListWidgetItem(p)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                internal = p.endswith("internalMesh")
                on = (internal and not big) or (not internal and big)
                if multi and not region_of(p):
                    on = False  # a multi-region case's top-level mesh (often the unsplit original) overlaps the regions
                it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                self.patches.addItem(it)
            self._check_regions(self.region_sel)  # a rebuilt list keeps the shown regions
        for w in (self.field, self.time, self.patches):
            w.blockSignals(False)
        self._setup_regions()
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
        from aethercfd.results.foam_reader import region_of

        self.ctx.services.select({"kind": "patch", "name": cur.text().split("/")[-1], "region": region_of(cur.text())})

    def _internal_meshes(self) -> list[str]:
        """Internal meshes of the shown regions (all of them for a single-region case)."""
        from aethercfd.results.foam_reader import region_of

        meshes = [p for p in self.meta.get("patches", []) if p.endswith("internalMesh")]
        if not self.meta.get("regions"):
            return meshes
        return [p for p in meshes if region_of(p) and (self.region_sel is None or region_of(p) in self.region_sel)]

    # ------------------------------------------------------------------ regions (multi-region / CHT cases)
    def regions(self) -> list[str]:
        return list(self.meta.get("regions") or [])

    def _setup_regions(self) -> None:
        """Fill the Regions menu from the case's regions (hidden for a single-region case)."""
        regions = self.regions()
        if regions and self.case_root is not None and set(self.region_kinds) != set(regions):
            from aethercfd.results.casedata import region_kinds

            self.region_kinds = region_kinds(self.case_root, regions)
        pending, self._pending_regions = getattr(self, "_pending_regions", None), None
        if pending and regions and self.region_sel is None:
            from aethercfd.results.casedata import resolve_regions

            with contextlib.suppress(ValueError):  # a saved / queued choice that no longer fits: all regions
                sel = resolve_regions(pending, regions, self.region_kinds)
                if sel and len(sel) < len(regions):
                    self._check_regions(sel)
                    self.region_sel = sel
                    self._first_data = True
        if self.region_sel is not None and not set(self.region_sel) <= set(regions):
            self.region_sel = None
        m = self.region_menu
        m.clear()
        if not regions:
            self.region_btn.setVisible(False)
            return

        def later(fn):  # the menu is rebuilt by what its actions do: never inside their own signal
            return lambda *_: QTimer.singleShot(0, fn)

        m.addAction("All regions", later(lambda: self.set_regions(None)))
        fluids = [r for r in regions if self.region_kinds.get(r) == "fluid"]
        solids = [r for r in regions if self.region_kinds.get(r) == "solid"]
        if fluids and solids:
            m.addAction("Fluid only" if len(fluids) == 1 else "Fluids only", later(lambda: self.set_regions("fluid")))
            m.addAction("Solids only" if len(solids) > 1 else "Solid only", later(lambda: self.set_regions("solids")))
        m.addSeparator()
        shown = set(self.region_sel or regions)
        for r in regions:
            a = m.addAction(f"{r}  ({self.region_kinds.get(r, '?')})")
            a.setCheckable(True)
            a.setChecked(r in shown)
            a.toggled.connect(lambda on, r=r: QTimer.singleShot(0, lambda: self._toggle_region(r, on)))
        self.region_btn.setText(f"Regions: {self._regions_label()}  ")
        self.region_btn.setVisible(True)

    def _regions_label(self, sel: list[str] | None = None) -> str:
        sel = self.region_sel if sel is None else sel
        regions = self.regions()
        if sel is None or set(sel) == set(regions):
            return "all"
        kinds = {self.region_kinds.get(r) for r in sel}
        same_kind = [r for r in regions if self.region_kinds.get(r) in kinds]
        if len(kinds) == 1 and set(sel) == set(same_kind) and len(sel) > 1:
            return "solids" if kinds == {"solid"} else "fluids"
        return ", ".join(sel) if len(sel) <= 2 else f"{len(sel)} of {len(regions)}"

    def _toggle_region(self, region: str, on: bool) -> None:
        current = self.region_sel or self.regions()
        shown = [r for r in self.regions() if (r in current and r != region) or (r == region and on)]
        if shown:
            self.set_regions(shown)
        else:  # never nothing: the last region stays
            self._setup_regions()

    def _check_regions(self, sel: list[str] | None) -> None:
        """Tick the 'Show' list for the shown regions: a hidden region's entries off; a shown region with
        nothing ticked gets its default (internal mesh, or its patches for a very large case)."""
        from aethercfd.results.foam_reader import region_of

        items = [self.patches.item(i) for i in range(self.patches.count())]
        big = (self.ctx.services.session.case().n_cells or 0) > 2_000_000 if self.ctx.services.session else False
        ticked = {region_of(it.text()) for it in items if it.checkState() == Qt.CheckState.Checked}
        was = self.patches.blockSignals(True)
        try:
            for it in items:
                r = region_of(it.text())
                if not r:
                    continue
                internal = it.text().endswith("internalMesh")
                if sel is not None and r not in sel:
                    it.setCheckState(Qt.CheckState.Unchecked)
                elif r not in ticked and internal != big:
                    it.setCheckState(Qt.CheckState.Checked)
        finally:
            self.patches.blockSignals(was)

    def _on_patch_toggled(self, *_) -> None:
        """The expert 'Show' list: the shown regions follow what is ticked there."""
        from aethercfd.results.foam_reader import region_of

        regions = self.regions()
        if regions:
            on = {region_of(self.patches.item(i).text()) for i in range(self.patches.count())
                  if self.patches.item(i).checkState() == Qt.CheckState.Checked} - {""}
            sel = [r for r in regions if r in on]
            new = None if not sel or len(sel) == len(regions) else sel
            if new != self.region_sel:
                self.region_sel = new
                self._setup_regions()
        self.request_load()

    def set_regions(self, regions=None) -> str:
        """Show only these regions of a multi-region (CHT) case: names, 'fluid', 'solids' or None / 'all'
        (see casedata.resolve_regions). Slices and visualization stages then use only these regions, and
        the camera frames them. Returns what is shown, or why not."""
        msg = self._apply_regions(regions)
        if not msg.startswith("cannot"):
            self.request_load(reset_range=True)
        return msg

    def _apply_regions(self, regions) -> str:
        """set_regions without loading (the caller loads)."""
        from aethercfd.results.casedata import resolve_regions

        if self.case_root is not None and not self.meta:
            self._pending_regions = regions
            return f"the case is still loading; regions {regions!r} will be shown as soon as its results are read"
        avail = self.regions()
        if not avail:
            return "this case has a single region (nothing to choose)" if regions in (None, "", "all", [], ["all"]) \
                else "cannot choose regions: this case has a single region"
        try:
            sel = resolve_regions(regions, avail, self.region_kinds)
        except ValueError as exc:
            return f"cannot show regions: {exc}"
        if sel is not None and len(sel) == len(avail):
            sel = None
        self._check_regions(sel)
        changed = sel != self.region_sel
        self.region_sel = sel
        self._setup_regions()
        if changed:
            self.bounds = None  # the slice, the showcase and the camera follow the shown regions
            self._first_data = True
        kinds = ", ".join(f"{r} ({self.region_kinds.get(r, '?')})" for r in (sel or avail))
        return f"regions shown: {self._regions_label()} -- {kinds}"

    def _patch_names(self) -> list[str]:
        return [self.patches.item(i).text() for i in range(self.patches.count())]

    def _on_field_changed(self, *_):
        if self.look == "studio" and not self._cmap_user and self.field.currentText():
            self.cmap.blockSignals(True)
            self.cmap.setCurrentText(lk.auto_cmap(self.field.currentText()))
            self.cmap.blockSignals(False)
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
               "view": self.view_opts, "look": self.look, "regions": self.region_sel,
               "smoothing": self.smoothing}
        if any(s.get("type") == "pathlines" and s.get("visible", True) for s in self.stages):
            req["steady"] = self._is_steady()
        self._last_request = __import__("time").monotonic()
        if self.slice_on.isChecked():
            # placed by the worker along the extent of the shown regions (right after a region change too)
            ax = self.slice_axis.currentIndex()
            normal = [0.0, 0.0, 0.0]
            normal[ax] = 1.0
            req["slice"] = {"axis": ax, "frac": self.slice_pos.value() / 1000, "normal": normal}
            req["shell"] = self.look == "studio" and not self._is2d
            if not any(p.endswith("internalMesh") for p in patches):
                req["patches"] = [*patches, *self._internal_meshes()]
        self._req_load.emit(self.vid, self.gen, req)

    def _on_data(self, vid: str, gen: int, data: dict) -> None:
        if vid != self.vid or gen != self.gen:
            return  # stale (a newer request superseded it)
        self.drawn_gen = gen  # the view now shows request `gen` (tests and the copilot wait for this)
        blocks: dict = data["blocks"]
        for name in list(self.actors):
            if name not in blocks:
                actor, _m = self.actors.pop(name)
                self.plotter.renderer.RemoveActor(actor)
        self.scalar = data["scalar"]
        self.smoothed = bool(data.get("smoothed"))  # before drawing: the colour bar says "smoothed"
        self.stage_info = data.get("stage_info", {})
        self.block_regions = data.get("block_regions", {})
        if data["bounds"] is not None:
            self.bounds = data["bounds"]
            if self.btn_ao.isChecked():
                b = self.bounds
                diag = ((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) ** 0.5
                if diag > 0 and (self._ao_diag is None or not (0.8 < diag / self._ao_diag < 1.25)):
                    self._set_ao(True, quiet=True)  # (re)build the pass for this domain's size
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
        if self._quad:
            self._quad_sync()
        if self._first_data:
            self._first_data = False
            if self.section_2d:
                self._frame_section_2d()
            else:
                self._fit()
        self.comp.setVisible(data.get("components", 0) > 1)
        if getattr(self, "_showcase_pending", False) and self.bounds is not None:
            self._showcase_pending = False
            QTimer.singleShot(0, lambda: self.ctx.services.notify("Viewport: " + self.showcase()))
        edges = (data.get("overlays") or {}).get("edges")
        self.edge_mapper.SetInputData(edges if edges is not None else vtk.vtkPolyData())
        self.edge_actor.SetVisibility(edges is not None and self.look == "studio"
                                      and self.view_opts.get("base") != "hidden")
        msg = (f"{data['n_cells']:,} cells read · {data['seconds']:.2f} s · t = {data['time']:g}"
               if data["time"] is not None else "")
        if self.field.currentText() and data["scalar"] is None:
            msg += f" · '{self.field.currentText()}' is not written at this time"
            self.ctx.services.notify(f"Viewport: '{self.field.currentText()}' is not written at t = {data['time']:g}")
        if self.smoothed:
            msg += " · smoothed: colours interpolated from the cell values (Cells shows each cell's own value)"
        elif data["scalar"] is not None:
            msg += " · cells: each cell's own value" + (" with the mesh edges" if self.cell_edges else "")
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
        cells_edges = self.smoothing == "cells" and self.cell_edges
        flow_keys = {f"stage:{s['id']}" for s in self.stages if s.get("flow")}
        flow_bar = None
        stage_bar_key = None
        has_shell = any(n.startswith("shell:") for n in self.actors) and any(
            (s.get("section") or s.get("cutaway")) and s.get("visible", True) for s in self.stages)
        for name, (actor, mapper) in self.actors.items():
            prop = actor.GetProperty()
            info = self.stage_info.get(name)
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(*self._depth_offset(name))
            if info is not None and info.get("overlay"):  # contour lines / arrows on a section: solid colours
                mapper.ScalarVisibilityOff()
                prop.SetRepresentationToSurface()
                prop.SetEdgeVisibility(False)
                prop.SetOpacity(1.0)
                if info["overlay"] == "lines":
                    prop.SetLighting(False)
                    prop.SetLineWidth(1.8)
                    prop.SetColor(*((0.97, 0.97, 1.0) if self.look == "studio" else (0.05, 0.05, 0.06)))
                elif info["overlay"] == "nodata":  # regions without the field (solids on a velocity section)
                    lk.surface_material(prop, self.look, smooth=False, flat_view=self._is2d)
                    prop.SetColor(0.55, 0.57, 0.60)
                    prop.SetEdgeVisibility(cells_edges)
                    prop.SetEdgeColor(0.12, 0.12, 0.14)
                else:
                    prop.SetLighting(True)
                    prop.SetInterpolationToPhong()
                    prop.SetColor(0.93, 0.94, 0.97)
                    prop.SetSpecular(0.3)
                continue
            if info is not None and info.get("type") in ("slice", "clip"):  # cut surfaces: the look's material
                key = name.split("~")[0]
                prop.SetRepresentationToSurface()
                prop.SetOpacity(info.get("opacity", 1.0))
                point = info.get("association") == "point"
                lk.surface_material(prop, self.look, smooth=point, flat_view=self._is2d)
                prop.SetEdgeVisibility(bool(info.get("edges")) or cells_edges)
                prop.SetEdgeColor(0.12, 0.12, 0.14)
                prop.SetColor(0.85, 0.85, 0.88)
                if info.get("scalar") and info.get("range"):
                    slut = stage_luts.get(key)
                    if slut is None:
                        smap = info.get("cmap") or cmap
                        slut = stage_luts[key] = _lut(smap if smap in CMAPS else cmap, *info["range"])
                    if point:
                        mapper.SetScalarModeToUsePointFieldData()
                        mapper.InterpolateScalarsBeforeMappingOn()
                    else:
                        mapper.SetScalarModeToUseCellFieldData()
                    mapper.SelectColorArray(info["scalar"])
                    mapper.SetLookupTable(slut)
                    mapper.SetUseLookupTableScalarRange(True)
                    mapper.ScalarVisibilityOn()
                    if key == self.bar_stage or (stage_bar is None and "~" not in name):
                        stage_bar = (slut, self._stage_bar_title(key, info))
                        stage_bar_key = key
                else:
                    mapper.ScalarVisibilityOff()
                continue
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
                    # the colour bar explains what the eye goes to: the selected stage, else the first stage;
                    # flow paths get their own (second) bar: they are usually coloured by something else
                    if key in flow_keys and key != self.bar_stage:
                        flow_bar = flow_bar or (slut, self._stage_bar_title(key, info))
                    elif key == self.bar_stage or (stage_bar is None and "~" not in name):
                        stage_bar = (slut, self._stage_bar_title(key, info))
                        stage_bar_key = key
                else:
                    mapper.ScalarVisibilityOff()
                continue
            if name.startswith("shell:"):  # Studio: the domain around a slice, as faint glass
                mapper.ScalarVisibilityOff()
                prop.SetRepresentationToSurface()
                prop.SetEdgeVisibility(False)
                prop.SetInterpolationToPhong()
                prop.SetLighting(True)
                prop.SetColor(0.72, 0.80, 0.95)
                prop.SetOpacity(0.10)
                prop.SetAmbient(0.2)
                prop.SetDiffuse(0.6)
                prop.SetSpecular(0.5)
                prop.SetSpecularPower(60)
                actor.SetVisibility(True)
                actor.SetPickable(False)
                continue
            # the base mesh: translucent by default once pipeline stages are shown (they sit inside it); hidden
            # when the glass shell of sections / a cut-away already shows the domain
            is_slice = name.startswith("slice")
            base = self.view_opts.get("base") or ("hidden" if has_shell and not is_slice else "translucent" if any(
                s.get("visible", True) for s in self.stages) and not is_slice else "surface")
            actor.SetVisibility(base != "hidden")
            prop.SetOpacity({"translucent": 0.15, "outline": 0.35}.get(base, 1.0))
            poly = mapper.GetInput()
            smooth = (self.smoothing == "smooth" and self.scalar is not None and poly is not None
                      and poly.GetPointData().GetArray(self.scalar) is not None)
            lk.surface_material(prop, self.look, smooth=smooth or (self.look == "studio" and self.scalar is None),
                                flat_view=self._is2d)
            if rep == 2 or base == "outline":
                prop.SetRepresentationToWireframe()
            else:
                prop.SetRepresentationToSurface()
            prop.SetEdgeVisibility((rep == 1 or cells_edges) and base == "surface")
            prop.SetEdgeColor(0.15, 0.15, 0.17)
            prop.SetColor(0.65, 0.67, 0.70)
            if self.scalar and lo_hi is not None:
                if smooth:
                    mapper.SetScalarModeToUsePointFieldData()
                    mapper.InterpolateScalarsBeforeMappingOn()  # crisp gradients, no Gouraud colour smear
                else:
                    mapper.SetScalarModeToUseCellFieldData()
                mapper.SelectColorArray(self.scalar)
                mapper.SetLookupTable(lut)
                mapper.SetUseLookupTableScalarRange(True)
                mapper.ScalarVisibilityOn()
            else:
                mapper.ScalarVisibilityOff()
        smoothed_bar = self.smoothed
        if stage_bar is not None and self.bar_stage != "field":
            bar = stage_bar
            sinfo = self.stage_info.get(stage_bar_key) or {}
            # sections, clips, contours and isosurfaces follow Smooth / Cells: say so when they are smoothed
            smoothed_bar = (sinfo.get("type") in ("slice", "clip", "contour", "isosurface")
                            and bool(sinfo.get("interpolated")) and self.smoothing == "smooth")
        if bar is None and flow_bar is not None:  # only flow paths are coloured: they take the main bar
            bar, flow_bar, smoothed_bar = flow_bar, None, False
        if bar is not None:
            title, blut = self._bar_title_and_lut(bar[0], bar[1], cmap)
            if smoothed_bar:  # honest: interpolated colours say so on the colour bar too
                title += "\nsmoothed"
            self.bar_lut = blut
            self.scalar_bar.SetLookupTable(blut)
            self.scalar_bar.SetTitle(title)
            self.scalar_bar.VisibilityOn()
        else:
            self.scalar_bar.VisibilityOff()
        if flow_bar is not None:
            ftitle, flut = self._bar_title_and_lut(flow_bar[0], flow_bar[1], cmap, self.flow_bar)
            self.flow_bar.SetLookupTable(flut)
            self.flow_bar.SetTitle(ftitle)
        self.flow_bar.SetVisibility(flow_bar is not None)
        self._update_hud()
        self.plotter.render()

    def _depth_offset(self, name: str) -> tuple[float, float]:
        """Polygon offset (factor, units) of a drawn block: each region of a multi-region case gets its own
        small constant depth step, so where two regions' surfaces coincide (their interface, seen through a
        translucent base or the glass shells) one region always wins -- the solids over the fluid -- instead
        of the two flickering. No slope factor: at grazing angles it pushes a region through a thin wall in
        front of it (the inner tube speckling through the outer one)."""
        region = self.block_regions.get(name.split("~")[0], "")
        regions = self.regions()
        if not region or region not in regions or name.startswith("stage:"):
            return 0.0, 0.0
        order = sorted(regions, key=lambda r: (self.region_kinds.get(r) == "solid", regions.index(r)))
        return 0.0, -4.0 * order.index(region)

    # ------------------------------------------------------------------ looks
    def _unit(self, field: str) -> str | None:
        if field not in self._units and self.case_root is not None:
            from aethercfd.results.units import dimensions_of, unit_text

            try:
                self._units[field] = unit_text(dimensions_of(self.case_root, field))
            except OSError:
                self._units[field] = None
        return self._units.get(field)

    def _bar_title_and_lut(self, lut, name: str, cmap: str, bar=None):
        """Colour-bar title with units and an SI prefix (p [MPa]); its LUT is in those display units."""
        from aethercfd.results.units import bar_title, si_scale

        bar = bar or self.scalar_bar
        if name.endswith("]") and " [" in name:  # a stage's own title with its unit (residence time [s])
            lo, hi = lut.GetTableRange()
            bar.SetUseCustomLabels(False)
            bar.SetLabelFormat(lk.label_format(lo, hi).replace("%", "%-#"))
            return name, lut
        shown = display_name(name)
        base = shown.split("_mag")[0].split("_x")[0].split("_y")[0].split("_z")[0].strip("|")
        unit = self._unit(base)
        lo, hi = lut.GetTableRange()
        f, lab = si_scale(lo, hi, unit)
        pretty = f"|{base}|" if shown.endswith("_mag") else shown
        scaled = vtk.vtkLookupTable()
        scaled.DeepCopy(lut)
        scaled.SetTableRange(lo / f, hi / f)
        fmt = lk.label_format(lo / f, hi / f)
        if self.look == "studio":  # round tick values in display units (0, 10, 20 ... not 2.53e-05, 12.49)
            ticks = vtk.vtkDoubleArray()
            for t in lk.nice_ticks(lo / f, hi / f):
                ticks.InsertNextValue(t)
            bar.SetUseCustomLabels(True)
            bar.SetCustomLabels(ticks)
            bar.SetLabelFormat(fmt)
        else:
            bar.SetUseCustomLabels(False)
            bar.SetLabelFormat(fmt.replace("%", "%-#"))
        return bar_title(pretty, unit, lab), scaled

    def _update_hud(self) -> None:
        if self.look != "studio" or self.case_root is None or not self.scalar:
            self.hud.SetVisibility(False)
            return
        t = self.time.currentText()
        self.hud.SetInput(f"{self.case_root.name}   ·   {self.field.currentText()}   ·   t = {t}")
        self.hud.SetVisibility(True)

    def _apply_scene(self) -> None:
        """Background, depth cues, orientation widget and colour-bar style of the current look."""
        lk.apply_background(self.plotter.renderer, self.look)
        lk.style_bar(self.scalar_bar, self.look)
        lk.style_second_bar(self.flow_bar, self.look)
        studio = self.look == "studio"
        try:
            if studio:
                self.plotter.hide_axes()
                if self._orient is None:
                    self._orient = self.plotter.add_camera_orientation_widget()
                    rep = self._orient.GetRepresentation()
                    rep.SetSize(76, 76)
                    rep.AnchorToUpperRight()
                self._orient.On()
            else:
                if self._orient is not None:
                    self._orient.Off()
                self.plotter.show_axes()
        except Exception:
            log.exception("orientation widget")
        self.btn_ao.blockSignals(True)
        self.btn_ao.setChecked(studio and not self._is2d)
        self.btn_ao.blockSignals(False)
        self._set_ao(studio and not self._is2d, quiet=True)
        inp = self.edge_mapper.GetInput()
        self.edge_actor.SetVisibility(studio and inp is not None and inp.GetNumberOfLines() > 0
                                      and self.view_opts.get("base") != "hidden")
        self._update_hud()

    def on_theme_changed(self) -> None:
        """The Classic look follows the app theme (background, colour-bar text, axes labels); the Studio
        look keeps its dark vignette and light text in both themes."""
        try:
            self.plotter.add_axes(color=T.text_dim)
            if self.look == "studio":
                self.plotter.hide_axes()
            lk.apply_background(self.plotter.renderer, self.look)
            lk.style_bar(self.scalar_bar, self.look)
            self.plotter.render()
        except Exception:
            log.exception("viewport theme")

    def set_look(self, name: str) -> str:
        """'studio' (AetherCFD post-processing) or 'classic' (ParaView-style true cell colours)."""
        name = (name or "").lower()
        if name not in lk.LOOKS:
            return f"unknown look {name!r}: studio or classic"
        self.look = name
        self.look_box.blockSignals(True)
        self.look_box.setCurrentText(name.capitalize())
        self.look_box.blockSignals(False)
        st = getattr(self.ctx.services, "settings", None)
        if st is not None and getattr(st, "viewport_look", None) != name:
            st.viewport_look = name
            try:
                st.save()
            except Exception:
                log.exception("saving the viewport look")
        if name == "studio" and not self._cmap_user and self.field.currentText():
            self.cmap.blockSignals(True)
            self.cmap.setCurrentText(lk.auto_cmap(self.field.currentText()))
            self.cmap.blockSignals(False)
        if not self._smooth_explicit:  # the look's default until the user picks Smooth or Cells
            self.smoothing = "smooth" if name == "studio" else "cells"
            self._sync_smooth_buttons()
        self._apply_scene()
        if self._quad:
            self._quad_sync()
        self.request_load()
        return f"look: {name} ({self.smoothing})"

    def _sync_smooth_buttons(self) -> None:
        for b, on in ((self.btn_smooth, self.smoothing == "smooth"), (self.btn_cells, self.smoothing == "cells")):
            b.blockSignals(True)
            b.setChecked(on)
            b.blockSignals(False)
        self.btn_edges.blockSignals(True)
        self.btn_edges.setChecked(self.cell_edges)
        self.btn_edges.setEnabled(self.smoothing == "cells")
        self.btn_edges.blockSignals(False)

    def set_smoothing(self, mode: str, *, edges: bool | None = None, explicit: bool = True,
                      reload: bool = True) -> str:
        """Smooth (colours, sections and contours interpolated from the cell values) or Cells (every cell's
        own value, per-cell contours: the mesh resolution stays visible; optionally with the mesh edges).
        'auto' goes back to the look's default. An explicit choice is remembered (settings)."""
        mode = (mode or "").lower().strip()
        if mode not in (*SMOOTHINGS, "auto"):
            return f"unknown smoothing {mode!r}: smooth, cells or auto"
        if mode == "auto":
            self._smooth_explicit = False
            mode = "smooth" if self.look == "studio" else "cells"
        elif explicit:
            self._smooth_explicit = True
        if edges is not None:
            self.cell_edges = bool(edges)
        changed = mode != self.smoothing
        self.smoothing = mode
        self._sync_smooth_buttons()
        st = getattr(self.ctx.services, "settings", None)
        want = mode if self._smooth_explicit else "auto"
        if explicit and st is not None and getattr(st, "viewport_smoothing", None) != want:
            st.viewport_smoothing = want
            try:
                st.save()
            except Exception:
                log.exception("saving the viewport smoothing")
        if reload and changed:
            self.request_load()
        elif reload:
            self._apply_display()
        return ("smooth: colours and contours interpolated from the cell values (labelled 'smoothed')"
                if mode == "smooth" else
                "cells: every cell's own value, contours per cell" + (", mesh edges shown" if self.cell_edges
                                                                      else ""))

    def _is_steady(self) -> bool:
        """Steady run (SIMPLE / steadyState): its saved times are iterations (no pathlines through them)."""
        if getattr(self, "_steady", None) is None:
            self._steady = False
            s = self.ctx.services.session
            try:
                from aethercfd.case.checks import facts

                self._steady = bool(s is not None and facts(s.case()).steady)
            except Exception:
                log.exception("steady/transient check")
        return self._steady

    def showcase(self, regions=None) -> str:
        """The best first view of the result, in one click (also the copilot's): the Studio look; a 3D
        case gets a mid-plane slice through its long axis inside a glass shell, an isometric camera and
        depth shading; a 2D case is framed flat. On a multi-region (CHT) case ``regions`` ('solids', a
        region name, ...) picks what to show first: the slice then cuts through those regions' own
        middle and the camera frames them."""
        if self.case_root is None or not self.meta:
            return "no results are loaded yet"
        where = ""
        if regions is not None:
            msg = self._apply_regions(regions)
            if msg.startswith("cannot"):
                return msg
        if self.region_sel is not None:
            where = f" in {self._regions_label()}"
        if self.look != "studio":
            self.set_look("studio")
        if self.bounds is None:  # nothing drawn yet: decide once the first data (and its size) arrive
            self._showcase_pending = True
            self.request_load(reset_range=True)
            return f"showcase{where}: will frame the result as soon as it is loaded"
        if not self._is2d and self.bounds is not None:
            b = self.bounds
            ext = [b[1] - b[0], b[3] - b[2], b[5] - b[4]]
            # the plane spanning the two largest extents shows the most of the flow (along a pipe, through
            # a box and the tube entering it): cut normal to the smallest extent, through the middle
            normal = ext.index(min(ext))
            self.slice_axis.setCurrentIndex(normal)
            self.slice_pos.blockSignals(True)
            self.slice_pos.setValue(500)
            self.slice_pos.blockSignals(False)
            self.slice_on.setChecked(True)
            self._first_data = True  # re-frame when the slice arrives
            self.request_load(reset_range=True)
            hint = ("; the case has regions: showcase(regions='solids') or a region name shows only those"
                    if self.regions() and self.region_sel is None else "")
            return (f"showcase{where}: mid-plane slice normal to {'xyz'[normal]} in a glass shell, Studio look "
                    f"({self.field.currentText()}){hint}")
        self.slice_on.setChecked(False)
        self._first_data = True
        self.request_load(reset_range=True)
        return f"showcase{where}: {self.field.currentText()} in the Studio look"

    # ------------------------------------------------------------------ animation + ParaView
    def _out_dir(self, sub: str) -> Path:
        s = self.ctx.services.session
        base = (Path(s.project.meta_dir) / "local" / sub) if s is not None and s.project is not None else \
            Path.home() / "Pictures" / "AetherCFD"
        base.mkdir(parents=True, exist_ok=True)
        return base

    MAX_SWEEP_FRAMES = 240

    @staticmethod
    def _safe_name(text: str) -> str:
        """A file-name-safe version of a field name (thermo:mu -> thermo_mu; ':' is an NTFS stream)."""
        import re

        return re.sub(r"[^\w.+-]+", "_", text or "view").strip("._") or "view"

    def record_animation(self, kind: str = "turntable", path: str | None = None, *, frames: int = 72,
                         fps: int = 24, on_done=None) -> str:
        """Turntable (one orbit of the camera) or time sweep (the saved times, at most MAX_SWEEP_FRAMES) of
        what is shown, written as an animated GIF or WebP (by the file extension). Frames go to temporary
        PNGs, so memory stays small. A time sweep loads each time first: it finishes later and calls
        ``on_done(path)``; it is aborted cleanly if a load fails, the case changes or the panel closes."""
        import tempfile

        if not self.actors:
            return "nothing is shown yet"
        if getattr(self, "_sweep", None) is not None:
            return "a time sweep is already being recorded in this viewport"
        stamp = __import__("time").strftime("%Y%m%d_%H%M%S")
        out = Path(path) if path else self._out_dir("animations") / (
            f"{self._safe_name(self.field.currentText())}_{kind}_{stamp}.gif")
        tmp = tempfile.TemporaryDirectory(prefix="aether-anim-")
        shots: list[Path] = []

        def grab():
            from PIL import Image

            self.plotter.render()
            img = self.plotter.screenshot(return_img=True)
            f = Path(tmp.name) / f"{len(shots):05d}.png"
            Image.fromarray(img[:, :, :3]).save(f)
            shots.append(f)

        def write() -> str:
            from PIL import Image

            try:
                if not shots:
                    raise ValueError("no frames were captured")
                first = Image.open(shots[0]).convert("RGB")
                gif = out.suffix.lower() == ".gif"
                if gif:  # ONE palette for every frame: no colour flicker between frames
                    first = first.quantize(colors=255, method=Image.Quantize.MEDIANCUT)

                def rest():
                    for f in shots[1:]:
                        im = Image.open(f).convert("RGB")
                        yield im.quantize(palette=first) if gif else im

                out.parent.mkdir(parents=True, exist_ok=True)
                first.save(out, save_all=True, append_images=rest(), duration=int(1000 / max(fps, 1)), loop=0)
            finally:
                tmp.cleanup()
            self.ctx.services.notify(f"Animation saved: {out}")
            return str(out)

        if kind == "turntable":
            cam = self.plotter.camera
            keep = (cam.GetPosition(), cam.GetFocalPoint(), cam.GetViewUp())
            n = max(8, min(360, int(frames)))
            try:
                for _ in range(n):
                    grab()
                    cam.Azimuth(360.0 / n)
            finally:
                cam.SetPosition(*keep[0])
                cam.SetFocalPoint(*keep[1])
                cam.SetViewUp(*keep[2])
                self.plotter.render()
            res = write()
            if on_done:
                on_done(res)
            return res
        if kind != "time":
            tmp.cleanup()
            return f"unknown animation {kind!r}: turntable or time"
        count = self.time.count()
        if count < 2:
            tmp.cleanup()
            return "only one saved time: nothing to sweep (use a turntable)"
        step_n = max(1, -(-count // self.MAX_SWEEP_FRAMES))  # subsample long histories
        indices = list(range(0, count, step_n))
        if indices[-1] != count - 1:
            indices.append(count - 1)
        sweep = {"indices": indices, "k": 0, "lock": self.lock.isChecked(), "start": self.time.currentIndex(),
                 "case": self.case_root, "on_done": on_done, "tmp": tmp}
        watchdog = QTimer(self, singleShot=True, interval=180_000)
        sweep["watchdog"] = watchdog

        def step(_info=None):
            if self._sweep is not sweep:
                return
            watchdog.start()
            grab()
            sweep["k"] += 1
            if sweep["k"] >= len(indices):
                self._end_sweep()
                res = write()
                if on_done:
                    on_done(res)
                return
            self.time.setCurrentIndex(indices[sweep["k"]])

        sweep["slot"] = step
        watchdog.timeout.connect(lambda: self._end_sweep("a time step took more than 3 minutes to load"))
        self._sweep = sweep
        self.lock.setChecked(True)  # one colour range for the whole sweep
        self.stages_updated.connect(step)
        watchdog.start()
        if self.time.currentIndex() == indices[0]:
            self.request_load()
        else:
            self.time.setCurrentIndex(indices[0])
        return (f"recording a time sweep over {len(indices)} of {count} saved times into {out}"
                + (f" (every {step_n}th)" if step_n > 1 else ""))

    def _end_sweep(self, aborted: str | None = None) -> None:
        """Finish or abort the time sweep: disconnect it, restore the colour lock and the time."""
        sweep, self._sweep = getattr(self, "_sweep", None), None
        if sweep is None:
            return
        with contextlib.suppress(RuntimeError, TypeError):
            self.stages_updated.disconnect(sweep["slot"])
        sweep["watchdog"].stop()
        self.lock.setChecked(sweep["lock"])
        if sweep["case"] == self.case_root and 0 <= sweep["start"] < self.time.count():
            self.time.setCurrentIndex(sweep["start"])
        if aborted:
            sweep["tmp"].cleanup()
            self.ctx.services.notify(f"Animation stopped: {aborted}", "warn")

    def animation_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QSpinBox

        dlg = QDialog(self)
        dlg.setWindowTitle("Animation")
        f = QFormLayout(dlg)
        kind = QComboBox()
        kind.addItem("Turntable (orbit the camera once)", "turntable")
        kind.addItem("Time sweep (every saved time)", "time")
        fmt = QComboBox()
        fmt.addItems(["GIF (plays everywhere)", "WebP (smaller, sharper)"])
        n = QSpinBox(minimum=12, maximum=360, value=72)
        fps = QSpinBox(minimum=2, maximum=60, value=24)
        f.addRow("What", kind)
        f.addRow("Format", fmt)
        f.addRow("Turntable frames", n)
        f.addRow("Frames per second", fps)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ext = ".gif" if fmt.currentIndex() == 0 else ".webp"
        stamp = __import__("time").strftime("%Y%m%d_%H%M%S")
        default = self._out_dir("animations") / f"{self.field.currentText() or 'view'}_{kind.currentData()}_{stamp}{ext}"
        path, _ = QFileDialog.getSaveFileName(self, "Save animation", str(default), f"Animation (*{ext})")
        if path:
            self.ctx.services.notify(self.record_animation(kind.currentData(), path, frames=n.value(),
                                                           fps=fps.value()))

    def open_in_paraview(self) -> str:
        """The open case in ParaView (the backup / expert view), on this field and time. A small pvpython
        script in the project's local folder sets it up; nothing is written into the case."""
        import subprocess

        from aethercfd.firstrun.detect import find_paraview

        st = getattr(self.ctx.services, "settings", None)
        exe = find_paraview(getattr(st, "paraview_exe", None))
        if self.case_root is None:
            return "no case is open"
        if exe is None:
            msg = ("ParaView was not found (install it from paraview.org/download, or set paraview_exe in "
                   "settings / Help > Setup Wizard)")
            self.ctx.services.notify(msg, "warn")
            return msg
        field = self.field.currentText()
        t = self.meta.get("times", [None])[self.time.currentIndex()] if self.meta.get("times") else None
        try:
            script = self._out_dir("paraview") / "open_case.py"
            script.write_text(PARAVIEW_SCRIPT.format(control=str(self.case_root / "system" / "controlDict"),
                                                     decomposed=self._decomposed(), field=field,
                                                     time=repr(t), name=self.case_root.name), encoding="utf-8")
            subprocess.Popen([str(exe), f"--script={script}"], cwd=str(self.case_root), creationflags=0x00000008)
        except OSError as exc:
            msg = f"could not start ParaView: {exc}"
            self.ctx.services.notify(msg, "error")
            return msg
        msg = f"opening {self.case_root.name} in ParaView ({field} at t = {t})"
        self.ctx.services.notify(msg)
        return msg

    def _stage_bar_title(self, key: str, info: dict) -> str:
        if info.get("title"):
            return info["title"]
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
            try:
                others = [p for p in sv.host.panels_of("aether.viewport") if p is not self and p.isVisible()]
                sv.set_active_viewport(others[0] if others else None)
            except RuntimeError:  # shutting down: the services' Qt side is already deleted
                pass

    def eventFilter(self, obj, ev) -> bool:  # Qt override: a click/focus makes this the active viewport
        from PySide6.QtCore import QEvent

        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
            self.ctx.services.set_active_viewport(self)
        return self._pick_event(ev)  # picking points (sections, flow seeds, line probes) eats the clicks

    def _on_fail(self, vid: str, gen: int, msg: str) -> None:
        if vid == self.vid:
            self._end_sweep("a time step could not be read")
            self.info.setText(f"Could not read results: {msg}")
            self.ctx.services.notify(f"Viewport: {msg}", "warn")

    def _clear_actors(self) -> None:
        for actor, _m in self.actors.values():
            self.plotter.renderer.RemoveActor(actor)
        self.actors.clear()
        self.scalar_bar.VisibilityOff()
        self.flow_bar.VisibilityOff()
        self.edge_actor.SetVisibility(False)
        self.edge_mapper.SetInputData(vtk.vtkPolyData())
        self.hud.SetVisibility(False)
        self.plotter.render()

    # ------------------------------------------------------------------ camera
    def _fit(self) -> None:
        if self._is2d and self.bounds is not None:
            self._fit_2d()
        else:
            if self.look == "studio":
                self.plotter.view_isometric()  # a 3D case reads as 3D from the start
            self.plotter.reset_camera()
            self.plotter.camera_set = True
        if self._quad:
            self._quad_fit()
        self.plotter.render()

    def _view(self, plane: str) -> None:
        self.set_view(plane)

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
        if self.look == "studio":  # the colour bar is at the right: keep the domain clear of it
            scale = (1 + padding) * max(h / 2 / 0.80, w / (2 * aspect * 0.84))
            shift = scale * aspect * 0.13  # world units: move the view so the domain sits left of the bar
            fp, ps = list(cam.GetFocalPoint()), list(cam.GetPosition())
            fp[horiz] += shift
            ps[horiz] += shift
            cam.SetFocalPoint(*fp)
            cam.SetPosition(*ps)
            cam.SetParallelScale(scale)
        else:  # leave the bottom ~14% for the colour bar
            cam.SetParallelScale((1 + padding) * max(h / 2 / 0.86, w / (2 * aspect)))
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.camera_set = True  # stop pyvista from resetting our camera on its first render

    def _set_ao(self, on: bool, *, quiet: bool = False) -> None:
        b = self.bounds or [0, 1, 0, 1, 0, 1]
        diag = ((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) ** 0.5 or 1.0
        try:
            # pyvista keeps an existing pass (and its radius): drop it first, then build it for this domain
            self.plotter.disable_ssao()
            self._ao_diag = None
            if on and self.bounds is not None:
                self.plotter.enable_ssao(radius=0.02 * diag, bias=0.002 * diag, kernel_size=128, blur=True)
                self._ao_diag = diag
        except Exception as exc:
            if not quiet:
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
        if background in ("gradient", "look") and self.look == "studio":
            lk.apply_background(ren, "studio")  # the same vignette as on screen
        elif background == "gradient":
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
        if self.edge_actor.GetVisibility() and self.edge_mapper.GetInput() is not None:
            em = vtk.vtkPolyDataMapper()
            em.SetInputData(self.edge_mapper.GetInput())
            em.ScalarVisibilityOff()
            ea = vtk.vtkActor()
            ea.SetMapper(em)
            ea.GetProperty().DeepCopy(self.edge_actor.GetProperty())
            ea.GetProperty().SetLineWidth(max(1.0, 1.3 * height / max(1, self.plotter.height())))
            ren.AddActor(ea)
        if self.hud.GetVisibility():
            hud = lk.make_hud()
            hud.SetInput(self.hud.GetInput())
            hud.GetTextProperty().SetFontSize(max(10, round(13 * height / max(1, self.plotter.height()))))
            hud.SetVisibility(True)
            ren.AddViewProp(hud)
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
        for src_bar in (self.scalar_bar, self.flow_bar):
            if not (colorbar and src_bar.GetVisibility()):
                continue
            sb = vtk.vtkScalarBarActor()
            sb.ShallowCopy(src_bar)
            scale = height / max(1, self.plotter.height())
            for tp_src, tp in ((src_bar.GetLabelTextProperty(), sb.GetLabelTextProperty()),
                               (src_bar.GetTitleTextProperty(), sb.GetTitleTextProperty())):
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
        marks = self.bookmarks()
        all_views = QCheckBox(f"Also render every saved view ({len(marks)}), one image each")
        all_views.setEnabled(bool(marks))
        form.addRow(all_views)
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
            kw = {"background": bg.currentText(), "transparent": transparent.isChecked(), "colorbar": bar.isChecked(),
                  "base": base.currentText()}
            self.render_image(f, w, h, frame_stages=frame.isChecked(), **kw)
            self.ctx.services.notify(f"Rendered {w}×{h}: {f}")
            if all_views.isChecked():
                done = self.render_bookmarks(Path(f).parent, w, h, **kw)
                self.ctx.services.notify(f"Rendered {len(done)} saved view(s) into {Path(f).parent}")
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
    def show_field(self, field: str, time: str | None = None, slice_axis: str | None = None,
                   regions=None, smoothing: str | None = None) -> str:
        """Show a field (at a time, optionally on a mid-plane slice); on a multi-region (CHT) case in
        ``regions`` only ('solids', 'fluid', region names, 'all'; None keeps the shown regions); ``smoothing``
        smooth / cells / auto (None keeps the current choice)."""
        if smoothing:
            msg = self.set_smoothing(smoothing, reload=False)
            if msg.startswith("unknown"):
                return msg
        if self.case_root is not None and not self.meta:
            # the case was just opened and its time steps / fields are still being read: do it when ready
            self._queued_show = (field, time, slice_axis, regions)
            return f"the case is still loading; {field} will be shown as soon as its results are read"
        names = [self.field.itemText(i) for i in range(self.field.count())]
        if not names:
            return "the viewport has no results loaded for this case yet"
        if field not in names:
            return f"field {field!r} is not available; available: {', '.join(names[:40])}"
        where = ""
        if regions is not None:
            msg = self._apply_regions(regions)
            if msg.startswith("cannot"):
                return msg
        if self.region_sel is not None:
            missing = [r for r in self.region_sel if not self._region_has(r, field)]
            if len(missing) == len(self.region_sel):
                self.request_load(reset_range=True)
                return (f"{field} is not written in {', '.join(missing)} (a solid region has e.g. T but no U); "
                        "nothing changed except the shown regions")
            where = f" in {self._regions_label()}" + (f" (not written in {', '.join(missing)})" if missing else "")
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
        return (f"showing {field}{where} at t = {self.time.currentText()}"
                + (f", mid-plane slice normal to {slice_axis}" if self.slice_on.isChecked() else ""))

    def _region_has(self, region: str, field: str) -> bool:
        """Is ``field`` saved in some time folder of ``region`` (cheap existence check)?"""
        if self.case_root is None:
            return True
        for base in (self.case_root, self.case_root / "processor0"):
            for t in base.glob("[0-9]*"):
                if (t / region / field).exists() or (t / region / f"{field}.gz").exists():
                    return True
        return False

    # ------------------------------------------------------------------ comparisons
    def set_range(self, lo: float, hi: float) -> None:
        """Fix the colour range (and lock it), e.g. the same range in side-by-side views."""
        self.range = (float(lo), float(hi))
        self.lock.setChecked(True)
        self._apply_display()
        self.plotter.render()

    def set_cmap(self, name: str) -> None:
        if name in CMAPS:
            self._cmap_user = True  # chosen on purpose: the Studio look stops picking per field
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
                for o in list(getattr(self, "_linked", [])):
                    if getattr(o, "_disposed", False):
                        self._linked.remove(o)
                        continue
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
                    except (RuntimeError, AttributeError):  # its window is gone: stop syncing to it
                        if o in self._linked:
                            self._linked.remove(o)
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
                "stages": self.stages, "view": self.view_opts, "regions": self.region_sel,
                "camera": {"position": list(cam.position), "focal_point": list(cam.focal_point),
                           "view_up": list(cam.up)}}

    def restore_state(self, state: dict) -> None:
        self.rep.setCurrentIndex(int(state.get("rep", 0)))
        if state.get("cmap") in CMAPS:
            self.cmap.setCurrentText(state["cmap"])
            self._cmap_user = True
        self.follow.setChecked(bool(state.get("follow", False)))
        if state.get("component"):
            self.comp.setCurrentText(state["component"])
        self.stages = [s for s in state.get("stages", []) if isinstance(s, dict) and s.get("type")]
        self.view_opts = dict(state.get("view") or {})
        if isinstance(state.get("regions"), list) and state["regions"]:
            self._pending_regions = [str(r) for r in state["regions"]]  # applied once the case's regions are known

    def dispose(self) -> None:
        self._end_sweep()
        self._drop_plane_widget()
        self._disposed = True
        for o in getattr(self, "_linked", []):  # unlink from compare-view peers before the plotter goes
            peers = getattr(o, "_linked", None)
            if peers is not None and self in peers:
                peers.remove(self)
        self._linked = []
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
