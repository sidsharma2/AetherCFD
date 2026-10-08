"""Visualization panel: the pipeline of the active 3D viewport.

Add stages (slice, clip, threshold, contour, streamlines, glyphs, schlieren, mesh quality), switch them
on/off, edit their parameters live, mirror or revolve wedge cases. Stages run on the VTK worker thread
on the real mesh; interpolated ones are labelled in the viewport. "Exact grad(rho)" runs OpenFOAM's own
gradient (postProcess) so the schlieren is computed on the finite-volume discretisation, not by VTK.
"""

from __future__ import annotations

import contextlib
import itertools

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

AXES = ["x", "y", "z"]
METRICS = ["nonOrthogonality", "skewness", "aspectRatio", "volumeRatio", "cellVolume"]
TITLES = {"slice": "Slice", "clip": "Clip", "threshold": "Threshold", "contour": "Contour",
          "streamlines": "Streamlines", "glyphs": "Vector glyphs", "schlieren": "Schlieren",
          "quality": "Mesh quality"}
ICONS = {"slice": "mdi6.box-cutter", "clip": "mdi6.content-cut", "threshold": "mdi6.filter-outline",
         "contour": "mdi6.chart-bell-curve", "streamlines": "mdi6.waves", "glyphs": "mdi6.arrow-top-right",
         "schlieren": "mdi6.blur", "quality": "mdi6.grid"}
# (key, label, kind, default); kinds: axis frac bool field vfield int float optfloat text choice:<a|b>
PARAMS: dict[str, list[tuple[str, str, str, object]]] = {
    "slice": [("axis", "Normal", "axis", "z"), ("pos", "Position", "frac", 0.5)],
    "clip": [("axis", "Normal", "axis", "x"), ("pos", "Position", "frac", 0.5), ("invert", "Keep other side", "bool",
                                                                                  False),
             ("crinkle", "Whole cells", "bool", False)],
    "threshold": [("field", "Field", "field", None), ("component", "Component", "choice:magnitude|x|y|z",
                                                      "magnitude"),
                  ("lo", "From", "optfloat", None), ("hi", "To", "optfloat", None)],
    "contour": [("field", "Field", "field", None), ("values", "Values (blank = 5 auto)", "text", ""),
                ("color_by", "Colour by", "field", None), ("opacity", "Opacity", "frac", 1.0)],
    "streamlines": [("field", "Vector field", "vfield", "U"), ("seed", "Seeds", "choice:line|plane|sphere", "line"),
                    ("axis", "Seed normal", "axis", "x"), ("pos", "Seed position", "frac", 0.05),
                    ("n", "Seed count", "int", 50), ("max_length", "Max length (x diagonal)", "float", 3.0)],
    "glyphs": [("field", "Vector field", "vfield", "U"), ("max_points", "Max arrows", "int", 2000),
               ("scale", "Scale", "float", 1.0)],
    "schlieren": [("source", "Density field", "field", "rho"), ("k", "Contrast k", "float", 12.0),
                  ("axis", "Slice normal", "axis", "z"), ("pos", "Slice position", "frac", 0.5)],
    "quality": [("metric", "Metric", "choice:" + "|".join(METRICS), "nonOrthogonality"),
                ("min", "Only cells >=", "optfloat", None), ("max", "Only cells <=", "optfloat", None)],
}


class VisPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.vp = None
        self.stages: list[dict] = []
        self._ids = itertools.count(1)
        self._building = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.target = QLabel("No 3D viewport")
        self.target.setStyleSheet(f"color: {T.text_dim};")
        lay.addWidget(self.target)
        bar = QHBoxLayout()
        self.btn_add = QToolButton(icon=icon("msc.add"), text="Add", toolButtonStyle=Qt.ToolButtonStyle.
                                   ToolButtonTextBesideIcon, popupMode=QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(self)
        for t, title in TITLES.items():
            m.addAction(icon(ICONS[t]), title, lambda t=t: self.add(t))
        self.btn_add.setMenu(m)
        self.btn_del = QToolButton(icon=icon("msc.trash"), toolTip="Remove stage", autoRaise=True)
        self.btn_dup = QToolButton(icon=icon("msc.copy"), toolTip="Duplicate stage", autoRaise=True)
        bar.addWidget(self.btn_add)
        bar.addStretch(1)
        bar.addWidget(self.btn_dup)
        bar.addWidget(self.btn_del)
        lay.addLayout(bar)
        self.list = QListWidget()
        self.list.setMaximumHeight(150)
        lay.addWidget(self.list)
        self.props_box = QGroupBox("Stage")
        self.props = QFormLayout(self.props_box)
        lay.addWidget(self.props_box)
        view = QGroupBox("View")
        vf = QFormLayout(view)
        self.base = QComboBox(toolTip="How the mesh surfaces are drawn while stages are shown")
        self.base.addItems(["Auto", "surface", "translucent", "outline", "hidden"])
        vf.addRow("Base mesh", self.base)
        self.mirror = QComboBox()
        self.mirror.addItems(["Off", "x", "y", "z"])
        self.mirror_origin = QDoubleSpinBox(decimals=6, minimum=-1e9, maximum=1e9)
        self.rev_on = QCheckBox("Revolve wedge")
        self.rev_axis = QComboBox()
        self.rev_axis.addItems(AXES)
        self.rev_axis.setCurrentText("z")
        self.rev_angle = QDoubleSpinBox(decimals=3, minimum=0.1, maximum=180, value=5.0, suffix="°")
        self.rev_copies = QSpinBox(minimum=2, maximum=720, value=72)
        vf.addRow("Mirror about", self.mirror)
        vf.addRow("Mirror plane at", self.mirror_origin)
        vf.addRow(self.rev_on)
        vf.addRow("Axis", self.rev_axis)
        vf.addRow("Wedge angle", self.rev_angle)
        vf.addRow("Copies", self.rev_copies)
        lay.addWidget(view)
        self.btn_grad = QPushButton(icon("mdi6.function-variant"), "Exact grad(rho) with OpenFOAM…")
        self.btn_grad.setToolTip("Runs the solver's -postProcess -func 'grad(rho)' on the latest time so the "
                                 "schlieren uses OpenFOAM's gradient on the real discretisation")
        lay.addWidget(self.btn_grad)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.status)
        lay.addStretch(1)
        self._apply_timer = QTimer(self, singleShot=True, interval=180, timeout=self._push)
        self._view_timer = QTimer(self, singleShot=True, interval=180, timeout=self._push_view)
        self.list.currentRowChanged.connect(self._show_props)
        self.list.itemChanged.connect(self._on_item_changed)
        self.btn_del.clicked.connect(self.remove)
        self.btn_dup.clicked.connect(self.duplicate)
        for w in (self.mirror, self.rev_axis, self.base):
            w.currentIndexChanged.connect(lambda *_: self._view_timer.start())
        for w in (self.mirror_origin, self.rev_angle, self.rev_copies):
            w.valueChanged.connect(lambda *_: self._view_timer.start())
        self.rev_on.toggled.connect(lambda *_: self._view_timer.start())
        self.btn_grad.clicked.connect(self._exact_grad)
        self.sv.active_viewport_changed.connect(self._retarget)
        self._retarget(self.sv.active_viewport)

    # ------------------------------------------------------------------ target viewport
    def _retarget(self, vp) -> None:
        if self.vp is not None:
            with contextlib.suppress(RuntimeError, TypeError):  # the old viewport may be gone
                self.vp.stages_updated.disconnect(self._on_stage_info)
        self.vp = vp
        if vp is None:
            self.target.setText("No 3D viewport: open one (View ▸ Add Panel ▸ 3D Viewport)")
            self.stages = []
        else:
            host = self.sv.host
            title = next((d.windowTitle() for n, d in host.docks().items() if host.panel(n) is vp),
                         "3D Viewport") if host is not None else "3D Viewport"
            self.target.setText(f"Acting on: {title}")
            self.stages = [dict(s) for s in vp.stages]
            vp.stages_updated.connect(self._on_stage_info)
            v = vp.view_opts
            self._building = True
            self.base.setCurrentText(v.get("base") or "Auto")
            self.mirror.setCurrentText(v.get("mirror") or "Off")
            self.mirror_origin.setValue(float(v.get("mirror_origin", 0.0)))
            rv = v.get("revolve") or {}
            self.rev_on.setChecked(bool(rv))
            self.rev_axis.setCurrentText(rv.get("axis", "z"))
            self.rev_angle.setValue(float(rv.get("angle", 5.0)))
            self.rev_copies.setValue(int(rv.get("copies", 72)))
            self._building = False
            self._suggest_wedge()
        self._rebuild_list()

    def _suggest_wedge(self) -> None:
        s = self.sv.session
        if s is None or self.rev_on.isChecked():
            return
        case = s.case()
        if case.regions and case.regions[0].is_axisymmetric:
            self.status.setText("Wedge case: 'Revolve wedge' shows the full 360°; check the axis and angle.")

    # ------------------------------------------------------------------ stages
    def add(self, kind: str) -> None:
        stage = {"id": f"s{next(self._ids)}", "type": kind, "visible": True}
        fields = self.vp.field_names() if self.vp else []
        for key, _label, wkind, default in PARAMS[kind]:
            if wkind in ("field", "vfield") and default is None:
                default = self.vp.field.currentText() if self.vp and self.vp.field.currentText() else (
                    fields[0] if fields else None)
            if default is not None:
                stage[key] = default
        while any(s["id"] == stage["id"] for s in self.stages):
            stage["id"] = f"s{next(self._ids)}"
        self.stages.append(stage)
        self._rebuild_list(select=len(self.stages) - 1)
        self._apply_timer.start()

    def remove(self) -> None:
        i = self.list.currentRow()
        if 0 <= i < len(self.stages):
            self.stages.pop(i)
            self._rebuild_list(select=min(i, len(self.stages) - 1))
            self._apply_timer.start()

    def duplicate(self) -> None:
        i = self.list.currentRow()
        if 0 <= i < len(self.stages):
            s = dict(self.stages[i])
            s["id"] = f"s{next(self._ids)}"
            self.stages.insert(i + 1, s)
            self._rebuild_list(select=i + 1)
            self._apply_timer.start()

    def _label(self, s: dict) -> str:
        extra = s.get("field") or s.get("source") or s.get("metric") or s.get("axis", "")
        return f"{TITLES[s['type']]}  ·  {extra}"

    def _rebuild_list(self, select: int | None = None) -> None:
        self._building = True
        self.list.clear()
        for s in self.stages:
            it = QListWidgetItem(icon(ICONS.get(s["type"], "msc.symbol-event")), self._label(s))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if s.get("visible", True) else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        self._building = False
        if select is not None and 0 <= select < len(self.stages):
            self.list.setCurrentRow(select)
        else:
            self._show_props(self.list.currentRow())

    def _on_item_changed(self, it: QListWidgetItem) -> None:
        if self._building:
            return
        i = self.list.row(it)
        self.stages[i]["visible"] = it.checkState() == Qt.CheckState.Checked
        self._apply_timer.start()

    # ------------------------------------------------------------------ properties form
    def _show_props(self, row: int) -> None:
        while self.props.rowCount():
            self.props.removeRow(0)
        if not (0 <= row < len(self.stages)):
            self.props_box.setTitle("Stage")
            return
        s = self.stages[row]
        self.props_box.setTitle(TITLES[s["type"]])
        fields = self.vp.field_names() if self.vp else []
        for key, label, kind, default in PARAMS[s["type"]]:
            w = self._editor(s, key, kind, default, fields)
            self.props.addRow(label, w)
        if self.vp is not None:
            self.vp.set_bar_stage(s["id"])

    def _editor(self, s: dict, key: str, kind: str, default, fields: list[str]) -> QWidget:
        val = s.get(key, default)

        def setv(v):
            s[key] = v
            self.list.currentItem().setText(self._label(s)) if self.list.currentItem() else None
            self._apply_timer.start()

        if kind == "axis":
            w = QComboBox()
            w.addItems(AXES)
            w.setCurrentText(str(val or "z"))
            w.currentTextChanged.connect(setv)
        elif kind == "frac":
            w = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=1000, value=int(1000 * float(val or 0)))
            w.valueChanged.connect(lambda v: setv(v / 1000))
        elif kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(val))
            w.toggled.connect(setv)
        elif kind in ("field", "vfield"):
            w = QComboBox()
            w.addItems(["", *fields] if key == "color_by" else fields)
            if val:
                if val not in fields:
                    w.addItem(str(val))
                w.setCurrentText(str(val))
            w.currentTextChanged.connect(lambda t: setv(t or None))
        elif kind == "int":
            w = QSpinBox(minimum=1, maximum=100000, value=int(val or 1))
            w.valueChanged.connect(setv)
        elif kind == "float":
            w = QDoubleSpinBox(decimals=4, minimum=0.0, maximum=1e6, value=float(val or 0.0), singleStep=0.1)
            w.valueChanged.connect(setv)
        elif kind == "optfloat":
            w = QLineEdit("" if val is None else f"{val:g}", placeholderText="auto")

            def parse(t):
                with contextlib.suppress(ValueError):  # keep the last valid value while typing
                    setv(float(t) if t.strip() else None)
            w.textEdited.connect(parse)
        elif kind == "text":
            w = QLineEdit(", ".join(f"{v:g}" for v in val) if isinstance(val, list) else str(val or ""))

            def parse_list(t):
                with contextlib.suppress(ValueError):
                    setv([float(x) for x in t.replace(";", ",").split(",") if x.strip()])
            w.textEdited.connect(parse_list)
        elif kind.startswith("choice:"):
            w = QComboBox()
            w.addItems(kind.split(":", 1)[1].split("|"))
            w.setCurrentText(str(val))
            w.currentTextChanged.connect(setv)
        else:
            w = QLabel(str(val))
        return w

    # ------------------------------------------------------------------ push to the viewport
    def _push(self) -> None:
        if self.vp is not None:
            self.vp.set_stages(self.stages)
            self.status.setText("Updating…")

    def _push_view(self) -> None:
        if self.vp is None or self._building:
            return
        opts: dict = {}
        if self.base.currentText() != "Auto":
            opts["base"] = self.base.currentText()
        if self.mirror.currentText() != "Off":
            opts["mirror"] = self.mirror.currentText()
            opts["mirror_origin"] = self.mirror_origin.value()
        if self.rev_on.isChecked():
            opts["revolve"] = {"axis": self.rev_axis.currentText(), "angle": self.rev_angle.value(),
                               "copies": self.rev_copies.value()}
        self.vp.set_view_options(opts)

    def _on_stage_info(self, info: dict) -> None:
        lines = []
        for s in self.stages:
            i = info.get(f"stage:{s['id']}")
            if i is None:
                continue
            txt = f"{TITLES[s['type']]}: {i['cells']:,} cells"
            if i.get("range"):
                txt += f", range {i['range'][0]:.4g} … {i['range'][1]:.4g}"
            if i.get("interpolated"):
                txt += " (interpolated)"
            for n in i.get("notes", []):
                txt += f" — {n}"
            lines.append(txt)
        self.status.setText("\n".join(lines))

    # ------------------------------------------------------------------ exact gradient
    def _exact_grad(self) -> None:
        sv = self.sv
        s = sv.session
        if s is None or not sv.ensure_project():
            return
        case = s.case()
        if case.solver_module:  # Foundation v14: foamPostProcess with the solver module
            steps = [{"app": "foamPostProcess", "args": ["-solver", case.solver_module, "-func", "grad(rho)",
                                                         "-latestTime"]}]
        elif case.application:
            steps = [{"app": case.application, "args": ["-postProcess", "-func", "grad(rho)", "-latestTime"]}]
        else:
            sv.notify("No solver in controlDict; cannot run the solver's -postProcess", "warn")
            return
        from aethercfd.commands.base import USER
        from aethercfd.studio.widgets.approval import ApprovalDialog

        params = {"case": str(s.root), "steps": steps}
        try:
            plan = s.executor.plan("run.start", params, USER)
        except Exception as exc:
            sv.notify(f"Cannot plan the gradient run: {exc}", "error")
            return
        dlg = ApprovalDialog(plan, self, who="AetherCFD")
        if dlg.exec() != ApprovalDialog.DialogCode.Accepted:
            return
        res = s.executor.execute("run.start", params, USER, plan=plan, approved_hash=plan.plan_hash)
        sv.notify(res.message, "info" if res.ok else "error")

    # ------------------------------------------------------------------ state
    def save_state(self) -> dict:
        return {}


PANELS = [PanelSpec("aether.vis_controls", "Visualization", "mdi6.palette-swatch-outline", "Visualization", VisPanel,
                    default_area=Area.RIGHT, description="Pipeline of the active 3D viewport: slices, clips, "
                    "contours, streamlines, glyphs, schlieren, mesh quality, mirror/revolve")]
