"""Sections, views and flow paths for the 3D viewport (a mixin of ViewportPanel).

Beginner-first: every feature is one menu click on the viewport toolbar (Sections, Views, Flow paths) and a
copilot tool. Sections and flow paths are ordinary Visualization-pipeline stages (slice / clip / streamlines /
pathlines with a few extra keys, see aethercfd.results.pipeline), so they show in the Visualization panel,
render, mirror and save like any stage. Points are picked with left clicks on the shown surfaces.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import time
from pathlib import Path

import vtk
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from aethercfd.results import sections as sx
from aethercfd.studio.panels.viewport import look as lk

log = logging.getLogger(__name__)
_IDS = itertools.count(1)

# camera offset direction (from the focal point) and view-up of the standard views
STANDARD_VIEWS = {
    "front": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)), "back": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "top": ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0)), "bottom": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), "left": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "iso": ((1.0, 0.8, 1.0), (0.0, 1.0, 0.0)),
}
VIEW_LABELS = {"front": "Front (XY plane, from +Z)", "back": "Back (from −Z)", "top": "Top (XZ plane, from +Y)",
               "bottom": "Bottom (from −Y)", "right": "Right (YZ plane, from +X)", "left": "Left (from −X)",
               "iso": "Isometric"}
VIEW_ALIASES = {"x": "right", "+x": "right", "-x": "left", "y": "top", "+y": "top", "-y": "bottom", "z": "front",
                "+z": "front", "-z": "back", "isometric": "iso", "xy": "front", "xz": "top", "yz": "right"}
# quad view: the main (perspective) view top-right, three orthographic views around it
QUAD = (("top", (0.0, 0.5, 0.5, 1.0)), ("front", (0.0, 0.0, 0.5, 0.5)), ("right", (0.5, 0.0, 1.0, 0.5)))


class ViewToolsMixin:
    """Sections, standard views, bookmarks, quad view, cut-away, flow paths, picking and line probes."""

    # ------------------------------------------------------------------ toolbar menus
    def _build_view_menus(self) -> None:
        """Fill the Views, Sections and Flow-paths tool buttons' menus (rebuilt when opened: bookmarks and
        inlets change with the case)."""
        self.views_menu = QMenu(self)
        self.views_menu.aboutToShow.connect(self._fill_views_menu)
        self.btn_views.setMenu(self.views_menu)
        self.sections_menu = QMenu(self)
        self.sections_menu.aboutToShow.connect(self._fill_sections_menu)
        self.btn_sections.setMenu(self.sections_menu)
        self.flow_menu = QMenu(self)
        self.flow_menu.aboutToShow.connect(self._fill_flow_menu)
        self.btn_flow.setMenu(self.flow_menu)

    def _fill_views_menu(self) -> None:
        m = self.views_menu
        m.clear()
        m.addAction("Fit everything", self._fit)
        for key in ("iso", "front", "back", "top", "bottom", "right", "left"):
            m.addAction(VIEW_LABELS[key], lambda k=key: self.set_view(k))
        m.addSeparator()
        marks = self.bookmarks()
        if marks:
            sub = m.addMenu("Saved views")
            for name in marks:
                sub.addAction(name, lambda n=name: self.ctx.services.notify("Viewport: " + self.restore_bookmark(n)))
            dele = m.addMenu("Delete a saved view")
            for name in marks:
                dele.addAction(name, lambda n=name: self.ctx.services.notify("Viewport: " + self.delete_bookmark(n)))
        m.addAction("Save this view…", self._ask_bookmark)
        m.addSeparator()
        q = m.addAction("Quad view (top, front, right + 3D)")
        q.setCheckable(True)
        q.setChecked(bool(self._quad))
        q.toggled.connect(lambda on: self.ctx.services.notify("Viewport: " + self.set_quad(on)))

    def _fill_sections_menu(self) -> None:
        m = self.sections_menu
        m.clear()
        tip = m.addAction("Sections cut the mesh to show the flow inside")
        tip.setEnabled(False)
        for ax in "xyz":
            m.addAction(f"Mid-plane {ax.upper()}  (normal to {ax})",
                        lambda a=ax: self._notify(self.add_sections(f"mid-{a}")))
        m.addAction("Three orthogonal mid-planes", lambda: self._notify(self.add_sections("ortho3")))
        m.addAction("Slice stack (parallel slices)…", self._ask_stack)
        m.addAction("Plane through 3 picked points", lambda: self.start_pick(
            3, "Click 3 points on the shown surfaces for the plane",
            lambda pts: self._notify(self.add_sections("plane", points=pts))))
        m.addAction("Plane by point + normal…", self._ask_plane)
        m.addSeparator()
        show = m.addMenu("On the sections show")
        for key, label in (("field", "Field colours"), ("edges", "Mesh edges (the cut cells)"),
                           ("contours", "Contour lines"), ("arrows", "Arrows (in-plane velocity)")):
            a = show.addAction(label)
            a.setCheckable(True)
            a.setChecked(bool(self.section_show.get(key)))
            a.toggled.connect(lambda on, k=key: self._notify(self.section_display(**{k: on})))
        has = bool(self._sections())
        drag = m.addAction("Drag the section (move / tilt it with the handles)")
        drag.setCheckable(True)
        drag.setChecked(self._plane_widget is not None)
        drag.setEnabled(has)
        drag.toggled.connect(lambda on: self._notify(self.drag_section(on)))
        a2d = m.addAction("Open the section in 2D (look straight at it)", lambda: self._notify(self.open_section_2d()))
        a2d.setEnabled(has)
        m.addAction("Line probe: plot the field along a line (pick 2 points)", lambda: self._notify(self.line_probe()))
        m.addSeparator()
        cut = m.addMenu("Cut-away (clip to see inside)")
        for ax in "xyz":
            cut.addAction(f"Cut normal to {ax.upper()}", lambda a=ax: self._notify(self.cutaway(a)))
        clr = m.addAction("Remove sections and cut-aways", lambda: self._notify(self.clear_sections()))
        clr.setEnabled(has or any(s.get("cutaway") for s in self.stages))

    def _fill_flow_menu(self) -> None:
        m = self.flow_menu
        m.clear()
        inlets = self.inlet_paths()
        a = m.addAction("Flow paths from the inlets" + (f" ({len(inlets)})" if inlets else " (none found)"),
                        lambda: self._notify(self.flow_paths("inlets")))
        a.setEnabled(bool(inlets))
        if len(inlets) > 1:
            sub = m.addMenu("From one inlet")
            for p in inlets:
                sub.addAction(p.split("/")[-1], lambda q=p: self._notify(self.flow_paths("inlets", inlets=[q])))
        m.addAction("From a picked point", lambda: self.start_pick(
            1, "Click a point: flow paths pass through a small ball around it",
            lambda pts: self._notify(self.flow_paths("point", point=pts[0]))))
        m.addAction("Along a picked line", lambda: self.start_pick(
            2, "Click 2 points: flow paths start along the line between them",
            lambda pts: self._notify(self.flow_paths("line", p1=pts[0], p2=pts[1]))))
        m.addAction("On a plane (mid-plane of the long axis)", lambda: self._notify(self.flow_paths("plane")))
        m.addSeparator()
        col = m.addMenu("Colour paths by")
        for key, label in (("U", "|U| (speed)"), ("residence_time", "Residence time (time since the seed)"),
                           ("field", f"The shown field ({self.field.currentText() or '-'})")):
            act = col.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.flow_colour == key)
            act.triggered.connect(lambda _c=False, k=key: self._notify(self.set_flow_colour(k)))
        pl = m.addAction("Pathlines through the saved times (transient runs)")
        pl.setCheckable(True)
        pl.setChecked(any(s.get("flow") and s.get("type") == "pathlines" for s in self.stages))
        pl.toggled.connect(lambda on: self._notify(self.set_pathlines(on)))
        clr = m.addAction("Remove flow paths", lambda: self._notify(self.clear_flow_paths()))
        clr.setEnabled(any(s.get("flow") for s in self.stages))

    def _notify(self, msg: str) -> None:
        self.ctx.services.notify("Viewport: " + str(msg))

    # ------------------------------------------------------------------ stages owned by these tools
    def _stage_id(self, prefix: str) -> str:
        used = {s.get("id") for s in self.stages}
        while True:
            sid = f"{prefix}{next(_IDS)}"
            if sid not in used:
                return sid

    def _set_stages_ui(self, stages: list[dict]) -> None:
        """Change the stages from the viewport (menus, copilot) and keep the Visualization panel in step."""
        self._drop_plane_widget()
        self.stages = [dict(s) for s in stages]
        self.request_load()
        host = self.ctx.services.host
        vis = next(iter(host.panels_of("aether.vis_controls")), None) if host is not None else None
        if vis is not None and getattr(vis, "vp", None) is self:
            vis._retarget(self)

    def _sections(self) -> list[dict]:
        return [s for s in self.stages if s.get("section")]

    def _ready(self) -> str | None:
        if self.case_root is None:
            return "no case is open"
        if not self.meta:
            return "the results are still loading"
        return None

    # ------------------------------------------------------------------ sections
    def add_sections(self, preset: str, *, axis: str | None = None, n: int = 5, origin=None, normal=None,
                     points=None, through: str | None = None, keep: bool = False, show: dict | None = None,
                     smoothing: str | None = None) -> str:
        """Section presets (mid-x/y/z, ortho3, stack, plane through 3 points / point + normal / a patch);
        they replace the current sections unless ``keep``."""
        if (err := self._ready()) is not None:
            return err
        if through:
            hit = self._match_patches([through])
            if not hit:
                return f"no patch like {through!r}; patches: {', '.join(self._patch_names()[:30])}"
            through = hit[0]
        if preset == "stack" and not axis and self.bounds is not None:
            axis = sx.longest_axis(self.bounds)
        try:
            specs = sx.section_specs(preset, axis=axis, n=n, origin=origin, normal=normal, points=points,
                                     through=through)
        except ValueError as exc:
            return str(exc)
        if show:
            self.section_show.update({k: bool(v) for k, v in show.items() if k in self.section_show})
        for s in specs:
            s["id"] = self._stage_id("sec")
            s.update(self._show_keys())
        if smoothing:
            self.set_smoothing(smoothing, reload=False)
        if self.slice_on.isChecked():  # one way of cutting at a time: the sidebar slice gives way
            self.slice_on.blockSignals(True)
            self.slice_on.setChecked(False)
            self.slice_on.blockSignals(False)
        keep_stages = [s for s in self.stages if keep or not s.get("section")]
        self._set_stages_ui([*keep_stages, *specs])
        what = {"ortho3": "three orthogonal mid-planes", "stack": f"a stack of {specs[0].get('stack')} slices "
                f"along {specs[0].get('axis')}"}.get(preset, "a section" if preset == "plane" else
                                                     f"the mid-plane normal to {preset[-1]}")
        if through:
            what += f" through the centre of {through.split('/')[-1]}"
        return f"{what} ({self._show_text()}; {self.smoothing})"

    def _show_keys(self) -> dict:
        s = self.section_show
        return {"edges": bool(s["edges"]), "contours": 10 if s["contours"] else 0, "arrows": bool(s["arrows"]),
                "opacity": 1.0 if s["field"] else 0.0}

    def _show_text(self) -> str:
        on = [k for k in ("field", "edges", "contours", "arrows") if self.section_show.get(k)]
        return "showing " + (", ".join(on) if on else "nothing")

    def section_display(self, *, field: bool | None = None, edges: bool | None = None,
                        contours: bool | int | None = None, arrows: bool | None = None) -> str:
        """What every section shows: field colours, the cut cells' edges, contour lines, in-plane arrows."""
        for key, val in (("field", field), ("edges", edges), ("contours", contours), ("arrows", arrows)):
            if val is not None:
                self.section_show[key] = bool(val)
        stages = []
        for s in self.stages:
            if s.get("section"):
                s = {**s, **self._show_keys()}
                if isinstance(contours, int) and not isinstance(contours, bool) and contours > 0:
                    s["contours"] = int(contours)
            stages.append(s)
        if self._sections():
            self._set_stages_ui(stages)
        return f"sections: {self._show_text()}"

    def clear_sections(self) -> str:
        n = len([s for s in self.stages if s.get("section") or s.get("cutaway")])
        self._set_stages_ui([s for s in self.stages if not (s.get("section") or s.get("cutaway"))])
        return f"removed {n} section(s) / cut-away(s)"

    def cutaway(self, axis: str = "x", pos: float = 0.5, invert: bool = False) -> str:
        """Clip the volume by a plane to see inside (Studio: inside the glass shell of the domain)."""
        if (err := self._ready()) is not None:
            return err
        ax = (axis or "x").lower()[0]
        if ax not in "xyz":
            return "axis must be x, y or z"
        keep = [s for s in self.stages if not s.get("cutaway")]
        stage = {"id": self._stage_id("cut"), "type": "clip", "axis": ax, "pos": float(pos), "invert": bool(invert),
                 "cutaway": True, "visible": True, "edges": self.smoothing == "cells" and self.cell_edges}
        self._set_stages_ui([*keep, stage])
        return f"cut-away normal to {ax} at {pos:g} of the domain" + (" (in a glass shell)" if self.look == "studio"
                                                                       else "")

    def _ask_stack(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Slice stack")
        f = QFormLayout(dlg)
        ax = QComboBox()
        ax.addItems(["x", "y", "z"])
        if self.bounds is not None:
            ax.setCurrentText(sx.longest_axis(self.bounds))
        n = QSpinBox(minimum=2, maximum=40, value=5)
        f.addRow("Along", ax)
        f.addRow("Number of slices", n)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._notify(self.add_sections("stack", axis=ax.currentText(), n=n.value()))

    def _ask_plane(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Section plane")
        f = QFormLayout(dlg)
        b = self.bounds or [0, 1, 0, 1, 0, 1]
        c = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
        pt = QLineEdit(" ".join(f"{v:.6g}" for v in c))
        nr = QLineEdit("0 0 1")
        f.addRow(QLabel("A point on the plane and its normal (x y z):"))
        f.addRow("Point", pt)
        f.addRow("Normal", nr)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            o = [float(x) for x in pt.text().replace(",", " ").split()]
            n = [float(x) for x in nr.text().replace(",", " ").split()]
            if len(o) != 3 or len(n) != 3:
                raise ValueError
        except ValueError:
            self._notify("the point and the normal need three numbers each")
            return
        self._notify(self.add_sections("plane", origin=o, normal=n))

    # ------------------------------------------------------------------ dragging a section
    def drag_section(self, on: bool = True, stage_id: str | None = None) -> str:
        """Handles on a section: drag the plane (or its normal arrow) and the section follows live; loads are
        throttled through the worker (one in flight at a time)."""
        self._drop_plane_widget()
        if not on:
            return "section handles off"
        secs = [s for s in self._sections() if not stage_id or s.get("id") == stage_id]
        if not secs or self.bounds is None:
            return "no section to drag: add one from the Sections menu first"
        s = secs[-1]
        if s.get("stack"):
            return "a slice stack moves as a whole: drag a single section"
        from aethercfd.results.pipeline import plane_from

        spec = {k: v for k, v in s.items() if k != "through"}
        plane = plane_from(spec, self.bounds)
        rep = vtk.vtkImplicitPlaneRepresentation()
        rep.SetPlaceFactor(1.0)
        rep.PlaceWidget(self.bounds)
        rep.SetOrigin(plane.GetOrigin())
        rep.SetNormal(plane.GetNormal())
        rep.OutlineTranslationOff()
        rep.ScaleEnabledOff()
        rep.SetDrawPlane(False)  # the section itself is the plane
        rep.GetNormalProperty().SetColor(1.0, 0.75, 0.2)
        rep.GetEdgesProperty().SetColor(1.0, 0.75, 0.2)
        w = vtk.vtkImplicitPlaneWidget2()
        iren = self.plotter.iren.interactor if hasattr(self.plotter.iren, "interactor") else self.plotter.iren
        w.SetInteractor(iren)
        w.SetRepresentation(rep)
        sid = s["id"]

        def moved(*_a):
            o, n = list(rep.GetOrigin()), list(rep.GetNormal())
            for st in self.stages:
                if st.get("id") == sid:
                    st.update({"origin": o, "normal": n})
                    for k in ("pos", "through"):
                        st.pop(k, None)
            self._drag_timer.start()

        w.AddObserver("InteractionEvent", moved)
        w.On()
        self._plane_widget = (w, rep, sid)
        self.plotter.render()
        return "drag the section's outline or its normal arrow; it updates as you move"

    def _drop_plane_widget(self) -> None:
        pw, self._plane_widget = getattr(self, "_plane_widget", None), None
        if pw is not None:
            with contextlib.suppress(Exception):
                pw[0].Off()
                pw[0].SetInteractor(None)

    def _drag_load(self) -> None:
        """Throttle: a new load only when the previous one has been drawn (or seems lost after 4 s)."""
        busy = getattr(self, "drawn_gen", -1) != self.gen and time.monotonic() - self._last_request < 4.0
        if busy:
            self._drag_timer.start()
            return
        self.request_load()

    # ------------------------------------------------------------------ section in 2D + line probes
    def _my_dock(self):
        host = self.ctx.services.host
        for name, d in host.docks().items():
            if host.panel(name) is self:
                return d
        return None

    def open_section_2d(self, stage_id: str | None = None) -> str:
        """A second viewport looking straight at the section (orthographic), the same field, time, colours and
        smoothing; draw line probes on it from its Sections menu."""
        secs = [s for s in self._sections() if not stage_id or s.get("id") == stage_id]
        if not secs:
            return "no section yet: add one from the Sections menu"
        host = self.ctx.services.host
        spec_p = self.ctx.services.panels.get("aether.viewport")
        me = self._my_dock()
        d = host.open_beside(spec_p, me, instance="sec2d") if me is not None else host.open_panel(spec_p,
                                                                                                  instance="sec2d")
        other = host.build_panel(d.objectName())
        if other is None or other is self:
            return "could not open a second viewport"
        from aethercfd.results.pipeline import plane_from

        s = dict(secs[-1])
        plane = plane_from({k: v for k, v in s.items() if k != "through"}, self.bounds or [0, 1, 0, 1, 0, 1])
        flat = {**s, "id": "sec2d", "section": False, "stack": 0}
        if not s.get("through"):
            flat.update({"origin": list(plane.GetOrigin()), "normal": list(plane.GetNormal())})
        state = {"n": 0}
        field, t = self.field.currentText(), self.time.currentText()

        def tick():
            state["n"] += 1
            if getattr(other, "_disposed", False) or state["n"] > 400:
                timer.stop()
                return
            if other.field.count() == 0 or not other.meta:
                return
            timer.stop()
            other.section_2d = {"normal": list(plane.GetNormal())}
            if other.look != self.look:
                other.set_look(self.look)
            other.set_smoothing(self.smoothing, explicit=False, reload=False)
            other.cell_edges = self.cell_edges
            other.view_opts = {"base": "hidden"}
            other.stages = [flat]
            if self.cmap.currentText() != other.cmap.currentText():
                other.set_cmap(self.cmap.currentText())
            other._first_data = True
            other.show_field(field, t)

        timer = QTimer(other)
        timer.setInterval(250)
        timer.timeout.connect(tick)
        timer.start()
        tick()
        return "opened the section in a 2D view beside this one (Sections > Line probe draws a profile on it)"

    def _frame_section_2d(self) -> None:
        """Look straight at the section along its normal, orthographic, filling the view."""
        info = getattr(self, "section_2d", None)
        b = self.stage_bounds("sec2d")
        if not info or b is None:
            return
        n = info["normal"]
        c = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
        size = max(b[1] - b[0], b[3] - b[2], b[5] - b[4], 1e-12)
        cam = self.plotter.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        cam.SetFocalPoint(*c)
        cam.SetPosition(*[c[i] + 5 * size * n[i] for i in range(3)])
        up = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (0.0, 1.0, 0.0)
        cam.SetViewUp(*up)
        cam.OrthogonalizeViewUp()
        self.plotter.renderer.ResetCamera(b)
        self.plotter.camera_set = True
        self._framed("section2d")
        self.plotter.render()

    def line_probe(self, p1=None, p2=None, field: str | None = None, n: int = 300, *, plot: bool = True) -> str:
        """The field along a straight line through the volume, as a line plot (picks 2 points when none are
        given). Smooth: interpolated values; Cells: each sample takes its cell's value (the steps show)."""
        if (err := self._ready()) is not None:
            return err
        if p1 is None or p2 is None:
            self.start_pick(2, "Click 2 points: the field is plotted along the line between them",
                            lambda pts: self._notify(self.line_probe(pts[0], pts[1], field, n, plot=plot)))
            return "pick 2 points on the shown surfaces (Esc cancels)"
        shown = field or self.field.currentText()
        vtk_name = self._vtk_names.get(shown, shown)
        if not vtk_name:
            return "no field is shown"
        self._line_rid += 1
        self._line_plot = plot
        self.last_line = None
        self._req_line.emit(self.vid, self._line_rid, {
            "case": str(self.case_root), "decomposed": self._decomposed(), "field": vtk_name,
            "component": self.comp.currentText(), "p1": [float(x) for x in p1], "p2": [float(x) for x in p2],
            "n": int(n), "smoothing": self.smoothing,
            "time": self.meta["times"][self.time.currentIndex()] if self.meta.get("times") else None})
        self._draw_probe_line(p1, p2)
        return f"probing {shown} along the line ({self.smoothing}: " + (
            "interpolated values)" if self.smoothing == "smooth" else "each cell's own value, the steps show)")

    def _draw_probe_line(self, p1, p2) -> None:
        src = vtk.vtkLineSource()
        src.SetPoint1(*p1)
        src.SetPoint2(*p2)
        src.Update()
        if self._line_actor is None:
            m = vtk.vtkPolyDataMapper()
            a = vtk.vtkActor()
            a.SetMapper(m)
            a.GetProperty().SetColor(1.0, 0.85, 0.25)
            a.GetProperty().SetLineWidth(3.0)
            a.GetProperty().SetLighting(False)
            a.SetPickable(False)
            self._line_actor = a
            self.plotter.renderer.AddActor(a)
        self._line_actor.GetMapper().SetInputData(src.GetOutput())
        self._line_actor.SetVisibility(True)
        self.plotter.render()

    def _on_line(self, vid: str, rid: int, res: dict) -> None:
        if vid != self.vid or rid != self._line_rid:
            return
        self.last_line = res
        if res.get("error"):
            self._notify(f"line probe failed: {res['error']}")
            return
        vals = [v for v in res["values"] if v is not None]
        name = self.field.currentText() if self._vtk_names.get(self.field.currentText()) == res["field"] else \
            res["field"]
        if vals:
            self._notify(f"line probe of {name}: {len(vals)} of {res['n']} samples inside the mesh, "
                         f"{min(vals):.5g} .. {max(vals):.5g} ({res['association']} values)")
        else:
            self._notify("line probe: the line does not pass through the mesh")
        if self._line_plot and vals:
            self._show_line_plot(res, name)

    def _show_line_plot(self, res: dict, name: str) -> None:
        import pyqtgraph as pg

        dlg = QDialog(self)
        dlg.setWindowTitle(f"{name} along the line")
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(dlg)
        pw = pg.PlotWidget()
        pw.showGrid(x=True, y=True, alpha=0.25)
        pw.setLabel("bottom", "distance along the line [m]")
        pw.setLabel("left", name)
        xs = [s for s, v in zip(res["s"], res["values"], strict=True) if v is not None]
        ys = [v for v in res["values"] if v is not None]
        pw.plot(xs, ys, pen=pg.mkPen((90, 170, 255), width=2))
        lay.addWidget(pw)
        how = ("smooth: values interpolated to the mesh points" if res["association"] == "point" else
               "cells: every sample shows the value of the cell it falls in, so the steps are the mesh")
        lay.addWidget(QLabel(f"{how} · from {_fmt(res['p1'])} to {_fmt(res['p2'])}"))
        row = QHBoxLayout()
        save = QPushButton("Save CSV…")
        row.addStretch(1)
        row.addWidget(save)
        lay.addLayout(row)

        def write():
            start = str(self._out_dir("probes") / f"{self._safe_name(name)}_line.csv")
            f, _ = QFileDialog.getSaveFileName(dlg, "Save line probe", start, "CSV (*.csv)")
            if f:
                from aethercfd.core.textio import save_user_file

                err = save_user_file(f, self.line_csv(res))
                self._notify(err or f"saved {f}")

        save.clicked.connect(write)
        dlg.resize(620, 400)
        dlg.show()
        self._line_dialog = dlg

    @staticmethod
    def line_csv(res: dict) -> str:
        rows = ["s,x,y,z,value"]
        for s, p, v in zip(res["s"], res["points"], res["values"], strict=True):
            rows.append(f"{s:.9g},{p[0]:.9g},{p[1]:.9g},{p[2]:.9g},{'' if v is None else f'{v:.9g}'}")
        return "\n".join(rows) + "\n"

    # ------------------------------------------------------------------ picking points
    def start_pick(self, n: int, prompt: str, done) -> None:
        """The next ``n`` left clicks on the shown surfaces are picked points (Esc cancels); then done(points)."""
        self._cancel_pick()
        self._pick = {"n": int(n), "pts": [], "done": done, "markers": []}
        self.plotter.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.info.setText(prompt + " (Esc cancels)")
        self._notify(prompt + " (Esc cancels)")

    def _cancel_pick(self) -> None:
        pk, self._pick = getattr(self, "_pick", None), None
        if pk is None:
            return
        for a in pk["markers"]:
            self.plotter.renderer.RemoveActor(a)
        self.plotter.unsetCursor()
        self.plotter.render()

    def pick_point(self, x: float, y: float) -> list[float] | None:
        """The 3D point under widget pixel (x, y), on any shown surface."""
        dpr = self.plotter.devicePixelRatioF()
        picker = vtk.vtkCellPicker()
        picker.SetTolerance(0.0005)
        if not picker.Pick(x * dpr, (self.plotter.height() - y) * dpr, 0, self.plotter.renderer):
            return None
        return list(picker.GetPickPosition())

    def add_pick(self, p: list[float]) -> None:
        pk = self._pick
        if pk is None:
            return
        pk["pts"].append(list(p))
        b = self.bounds or [0, 1, 0, 1, 0, 1]
        diag = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5]))
        s = vtk.vtkSphereSource()
        s.SetCenter(*p)
        s.SetRadius(0.006 * diag)
        s.Update()
        m = vtk.vtkPolyDataMapper()
        m.SetInputData(s.GetOutput())
        a = vtk.vtkActor()
        a.SetMapper(m)
        a.GetProperty().SetColor(1.0, 0.8, 0.2)
        a.SetPickable(False)
        self.plotter.renderer.AddActor(a)
        pk["markers"].append(a)
        self.plotter.render()
        if len(pk["pts"]) >= pk["n"]:
            pts, done = pk["pts"], pk["done"]
            self._cancel_pick()
            done(pts)

    def _pick_event(self, ev) -> bool:
        """eventFilter hook: True when the event was a pick (and must not rotate the camera)."""
        pk = getattr(self, "_pick", None)
        if pk is None:
            return False
        t = ev.type()
        if t == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
            self._cancel_pick()
            self._notify("picking cancelled")
            return True
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            p = self.pick_point(ev.position().x(), ev.position().y())
            if p is None:
                self._notify("nothing under the cursor: click on a shown surface")
            else:
                self.add_pick(p)
            return True
        if t == QEvent.Type.MouseButtonRelease and ev.button() == Qt.MouseButton.LeftButton:
            return True  # the press was a pick: its release must not end a camera drag
        return t == QEvent.Type.MouseMove and bool(ev.buttons() & Qt.MouseButton.LeftButton)

    # ------------------------------------------------------------------ standard views
    def set_view(self, name: str) -> str:
        key = VIEW_ALIASES.get(name.lower().strip(), name.lower().strip())
        if key not in STANDARD_VIEWS:
            return f"unknown view {name!r}: {', '.join(STANDARD_VIEWS)} or a saved view"
        self._place_camera(self.plotter.renderer, key, parallel=self._is2d)
        self.plotter.camera_set = True
        self._framed("view")  # this direction, framed on what is shown (kept so on a resize or a new stage)
        self.plotter.render()
        return f"view: {VIEW_LABELS[key]}"

    def _place_camera(self, ren, key: str, *, parallel: bool) -> None:
        b = self.bounds
        if b is None:
            return
        d, up = STANDARD_VIEWS[key]
        c = [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2]
        size = max(b[1] - b[0], b[3] - b[2], b[5] - b[4], 1e-12)
        nd = math.sqrt(sum(x * x for x in d))
        cam = ren.GetActiveCamera()
        cam.SetParallelProjection(parallel)
        cam.SetFocalPoint(*c)
        cam.SetPosition(*[c[i] + 4 * size * d[i] / nd for i in range(3)])
        cam.SetViewUp(*up)
        ren.ResetCamera(b)
        ren.ResetCameraClippingRange()

    # ------------------------------------------------------------------ bookmarks
    def _bookmark_file(self) -> Path:
        return self._out_dir("views") / "bookmarks.json"

    def _all_bookmarks(self) -> dict:
        try:
            return json.loads(self._bookmark_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def bookmarks(self) -> dict[str, dict]:
        """Saved camera views of the open case (name -> camera)."""
        if self.case_root is None:
            return {}
        return dict(self._all_bookmarks().get(str(self.case_root), {}))

    def _write_bookmarks(self, marks: dict) -> None:
        allm = self._all_bookmarks()
        allm[str(self.case_root)] = marks
        from aethercfd.core.textio import save_user_file

        err = save_user_file(self._bookmark_file(), json.dumps(allm, indent=1))
        if err:
            self._notify(f"view not saved: {err}")

    def _camera_dict(self) -> dict:
        cam = self.plotter.renderer.GetActiveCamera()
        return {"position": list(cam.GetPosition()), "focal_point": list(cam.GetFocalPoint()),
                "view_up": list(cam.GetViewUp()), "view_angle": cam.GetViewAngle(),
                "parallel": bool(cam.GetParallelProjection()), "parallel_scale": cam.GetParallelScale()}

    def save_bookmark(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return "a saved view needs a name"
        if self.case_root is None:
            return "no case is open"
        marks = self.bookmarks()
        marks[name] = self._camera_dict()
        self._write_bookmarks(marks)
        return f"saved the view {name!r} ({len(marks)} saved view(s))"

    def restore_bookmark(self, name: str) -> str:
        marks = self.bookmarks()
        hit = name if name in marks else next((k for k in marks if k.lower() == (name or "").lower()), None)
        if hit is None:
            return f"no saved view {name!r}; saved: {', '.join(marks) or 'none'}"
        c = marks[hit]
        cam = self.plotter.renderer.GetActiveCamera()
        cam.SetPosition(*c["position"])
        cam.SetFocalPoint(*c["focal_point"])
        cam.SetViewUp(*c["view_up"])
        cam.SetViewAngle(c.get("view_angle", 30.0))
        cam.SetParallelProjection(bool(c.get("parallel")))
        cam.SetParallelScale(c.get("parallel_scale", 1.0))
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.camera_set = True
        self._framing = None  # a saved camera stays exactly as saved
        self.plotter.render()
        return f"view {hit!r}"

    def delete_bookmark(self, name: str) -> str:
        marks = self.bookmarks()
        if marks.pop(name, None) is None:
            return f"no saved view {name!r}"
        self._write_bookmarks(marks)
        return f"deleted the saved view {name!r}"

    def _ask_bookmark(self) -> None:
        name, ok = QInputDialog.getText(self, "Save this view", "Name of the view:",
                                        text=f"view {len(self.bookmarks()) + 1}")
        if ok and name.strip():
            self._notify(self.save_bookmark(name))

    def render_bookmarks(self, folder: Path, width: int = 1920, height: int = 1080, **kw) -> list[str]:
        """One rendered image per saved view (into ``folder``); the camera is restored afterwards."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        keep = vtk.vtkCamera()
        keep.DeepCopy(self.plotter.renderer.GetActiveCamera())
        out = []
        try:
            for name in self.bookmarks():
                self.restore_bookmark(name)
                f = folder / f"{self._safe_name(self.field.currentText())}_{self._safe_name(name)}.png"
                self.render_image(str(f), width, height, **kw)
                out.append(str(f))
        finally:
            self.plotter.renderer.GetActiveCamera().DeepCopy(keep)
            self.plotter.render()
        return out

    # ------------------------------------------------------------------ quad view
    def set_quad(self, on: bool = True) -> str:
        """Four views of the same scene: top, front and right (orthographic) plus the 3D view. One render
        window, the same actors: nothing is read twice and every change shows in all four."""
        rw = self.plotter.ren_win
        if not on:
            for ren, _label, _key in self._quad or []:
                rw.RemoveRenderer(ren)
            self._quad = None
            self.plotter.renderer.SetViewport(0.0, 0.0, 1.0, 1.0)
            if self.look == "studio" and not self._is2d:
                self.btn_ao.setChecked(True)
            self.plotter.render()
            return "quad view off"
        if self._quad:
            return "quad view is already on"
        self.btn_ao.setChecked(False)  # screen-space AO is built for one full-window view
        self.plotter.renderer.SetViewport(0.5, 0.5, 1.0, 1.0)
        self._quad = []
        for key, vp in QUAD:
            ren = vtk.vtkRenderer()
            ren.SetViewport(*vp)
            lk.apply_background(ren, self.look)
            kit = vtk.vtkLightKit()
            kit.AddLightsToRenderer(ren)
            ren.SetUseDepthPeeling(1)
            label = vtk.vtkTextActor()
            label.SetInput(VIEW_LABELS[key].split(" (")[0])
            label.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
            label.SetPosition(0.03, 0.92)
            label.GetTextProperty().SetFontSize(13)
            label.GetTextProperty().SetColor(0.85, 0.88, 0.95)
            rw.AddRenderer(ren)
            self._quad.append((ren, label, key))
        self._quad_sync()
        self._quad_fit()
        return "quad view: top, front and right (orthographic) and the 3D view; each can be turned and zoomed"

    def _quad_sync(self) -> None:
        """Put the current actors into the quad view's other quadrants."""
        if not self._quad:
            return
        props = [a for a, _m in self.actors.values()] + [self.edge_actor]
        if self._line_actor is not None:
            props.append(self._line_actor)
        for ren, label, _key in self._quad:
            ren.RemoveAllViewProps()
            for a in props:
                ren.AddActor(a)
            ren.AddViewProp(label)
            lk.apply_background(ren, self.look)
        self.plotter.render()

    def _quad_fit(self) -> None:
        for ren, _label, key in self._quad or []:
            self._place_camera(ren, key, parallel=True)
        self.plotter.render()

    # ------------------------------------------------------------------ flow paths
    def _patch_types(self) -> dict[tuple[str, str], str]:
        s = self.ctx.services.session
        case = s.case() if s is not None else None
        out = {}
        for reg in case.regions if case is not None else []:
            for p in reg.patches:
                out[(reg.name, p.name)] = p.type
        return out

    def _patch_paths(self, *, flow: bool = False) -> list[str]:
        """Reader patch paths that matter: never groups; in a multi-region case only the regions' own patches
        (not the unsplit top-level mesh), and with ``flow`` only regions that have a velocity field."""
        from aethercfd.results.foam_reader import region_of

        multi = bool(self.meta.get("regions"))
        flowing = {}
        out = []
        for p in self.meta.get("patches", []):
            if p.endswith("internalMesh") or "/patch/" not in f"/{p.strip('/')}":
                continue  # groups (group/wall) and the volume
            reg = region_of(p)
            if multi and not reg:
                continue
            if flow and multi:
                if reg not in flowing:
                    root = self.case_root
                    flowing[reg] = root is not None and any(
                        True for _ in itertools.chain(root.glob(f"*/{reg}/U"), root.glob(f"processor0/*/{reg}/U")))
                if not flowing[reg]:
                    continue
            out.append(p)
        return out

    def inlet_paths(self) -> list[str]:
        """Reader patch paths of the inlets (patch-type and name heuristics: inlet*, *INLET*, ...)."""
        from aethercfd.results.foam_reader import region_of

        types = self._patch_types()
        rows = []
        for p in self._patch_paths(flow=True):
            name = p.split("/")[-1]
            reg = region_of(p)
            rows.append((p, name, types.get((reg, name), types.get(("", name), "patch"))))
        return sx.find_inlets(rows)

    def _match_patches(self, names: list[str], *, flow: bool = False) -> list[str]:
        """Reader patch paths for patch names given loosely ('dilution inlet' -> fluid/patch/DILUTION_AIR_INLET)."""
        from rapidfuzz import fuzz, process

        paths = self._patch_paths(flow=flow)
        out = []
        for n in names:
            if n in paths:
                out.append(n)
                continue
            norm = {p: p.split("/")[-1].replace("_", " ").lower() for p in paths}
            want = str(n).split("/")[-1].replace("_", " ").lower()
            exact = [p for p, v in norm.items() if v == want]
            if exact:
                out.extend(exact)
                continue
            hit = process.extractOne(want, norm, scorer=fuzz.WRatio, score_cutoff=70)
            if hit:
                out.append(hit[2])
        return list(dict.fromkeys(out))

    def flow_paths(self, source: str = "inlets", *, inlets: list[str] | None = None, point=None, p1=None, p2=None,
                   axis: str | None = None, pos: float | None = None, n: int | None = None,
                   color_by: str | None = None, tubes: bool | None = None, pathlines: bool | None = None) -> str:
        """Flow paths (streamlines of the interpolated velocity; pathlines through saved times when asked):
        from the inlets (all, or the named ones), a point, a line or a plane. Replaces earlier flow paths."""
        if (err := self._ready()) is not None:
            return err
        if "U" not in self._vtk_names.values():
            return "this case has no velocity field U: no flow paths"
        source = (source or "inlets").lower()
        old = next((s for s in self.stages if s.get("flow")), {})
        if color_by:
            self.flow_colour = {"speed": "U", "|u|": "U", "residence": "residence_time", "time": "residence_time"}.get(
                color_by.lower(), color_by)
        col = self.flow_colour
        stage = {"id": self._stage_id("flow"), "type": "streamlines", "flow": True, "field": "U", "visible": True,
                 "color_by": ("residence_time" if col == "residence_time" else
                              self._vtk_names.get(self.field.currentText()) if col == "field" else
                              self._vtk_names.get(col, col)),
                 "tubes": (self.look == "studio") if tubes is None else bool(tubes)}
        if pathlines if pathlines is not None else old.get("type") == "pathlines":
            stage["type"] = "pathlines"
        b = self.bounds or [0, 1, 0, 1, 0, 1]
        if source in ("inlets", "inlet", "patches"):
            paths = self._match_patches(inlets, flow=True) if inlets else self.inlet_paths()
            if not paths:
                return ("no inlet patch found (patch names with 'inlet'); seed the flow paths from a point, a line "
                        "or a plane instead" if not inlets else
                        f"no patch like {', '.join(inlets)}; inlets found: "
                        f"{', '.join(p.split('/')[-1] for p in self.inlet_paths()) or 'none'}")
            stage.update({"seed": "patches", "patches": paths, "n": int(n or 160)})
            what = "from " + ", ".join(p.split("/")[-1] for p in paths)
        elif source == "point":
            if point is None:
                return "a point is needed (x, y, z)"
            stage.update({"seed": "point", "center": [float(x) for x in point], "n": int(n or 80)})
            what = "through a point"
        elif source == "line":
            if p1 is None or p2 is None:
                return "two points p1, p2 are needed"
            stage.update({"seed": "line", "p1": [float(x) for x in p1], "p2": [float(x) for x in p2],
                          "n": int(n or 60)})
            what = "from a line"
        elif source == "plane":
            ax = (axis or sx.longest_axis(b)).lower()[0]
            stage.update({"seed": "plane", "axis": ax, "pos": 0.5 if pos is None else float(pos), "n": int(n or 120)})
            what = f"from a plane normal to {ax}"
        else:
            return "source must be inlets, point, line or plane"
        self._set_stages_ui([*[s for s in self.stages if not s.get("flow")], stage])
        col_txt = {"residence_time": "residence time", "U": "|U|"}.get(col, stage["color_by"])
        kind = "pathlines" if stage["type"] == "pathlines" else "flow paths"
        return f"{kind} {what}, coloured by {col_txt}" + (", as tubes" if stage["tubes"] else "")

    def set_flow_colour(self, key: str) -> str:
        self.flow_colour = key
        old = next((s for s in self.stages if s.get("flow")), None)
        if old is None:
            return f"flow paths will be coloured by {key}"
        col = ("residence_time" if key == "residence_time" else
               self._vtk_names.get(self.field.currentText()) if key == "field" else key)
        self._set_stages_ui([{**s, "color_by": col} if s.get("flow") else s for s in self.stages])
        return f"flow paths coloured by {'residence time' if key == 'residence_time' else col}"

    def set_pathlines(self, on: bool) -> str:
        if not any(s.get("flow") for s in self.stages):
            if not on:
                return "no flow paths"
            msg = self.flow_paths("inlets", pathlines=True)
            return msg if msg.startswith("pathlines") else self.flow_paths("plane", pathlines=True)
        self._set_stages_ui([{**s, "type": "pathlines" if on else "streamlines"} if s.get("flow") else s
                             for s in self.stages])
        return "pathlines through the saved times" if on else "streamlines of the shown time"

    def clear_flow_paths(self) -> str:
        self._set_stages_ui([s for s in self.stages if not s.get("flow")])
        return "flow paths removed"


def _fmt(p) -> str:
    return "(" + ", ".join(f"{v:.4g}" for v in p) + ")"
