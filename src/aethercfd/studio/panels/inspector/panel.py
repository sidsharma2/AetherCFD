"""Properties: details of whatever is selected -- a probed cell (every field there), a case file (header,
boundary conditions of a field file), the dictionary keyword under the editor cursor (value and where it
comes from), a patch (its type and every field's BC on it), a mesh hotspot, or a run.

Sources call ``services.select({"kind": ..., ...})``; this panel only renders.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.tokens import T


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, (list, tuple)):
        if len(v) == 1:
            return _fmt(v[0])
        s = "(" + " ".join(_fmt(x) for x in v) + ")"
        if len(v) == 3 and all(isinstance(x, (int, float)) for x in v):
            s += f"   |{math.sqrt(sum(x * x for x in v)):.6g}|"
        return s
    return str(v)


def field_summary(path: Path) -> dict:
    """Header, dimensions, internalField and per-patch BCs of an OpenFOAM field file (parsed, lossless)."""
    from aethercfd.core.textio import decode_bytes
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    with open(path, "rb") as fh:
        data = fh.read(64 * 2**20)
    doc = parse(decode_bytes(data)[0])
    view = DictView(doc)
    out: dict = {"header": view.header()}
    for key in ("dimensions", "internalField"):
        lk = view.lookup(key)
        if lk is not None and lk.entry is not None and lk.entry.value is not None:
            txt = doc.text(lk.entry.value.start, lk.entry.value.end).strip()
            if len(txt) > 120:
                n = txt.split("(", 1)[0].split()[-1] if "(" in txt else "?"
                txt = f"{txt.split()[0]} {txt.split()[1] if len(txt.split()) > 1 else ''} … ({n} values)"
            out[key] = txt
    bcs = {}
    bf = view.dict_at("boundaryField")
    if bf is not None:
        for patch in view.keys("boundaryField"):
            row = {}
            for k in ("type", "value", "inletValue", "uniformValue", "massFlowRate", "p0", "T0", "gradient"):
                lk = view.lookup(f"boundaryField/{patch}/{k}")
                if lk is not None and lk.entry is not None and lk.entry.value is not None:
                    t = doc.text(lk.entry.value.start, lk.entry.value.end).strip()
                    row[k] = t if len(t) < 80 else t[:77] + "…"
            bcs[patch] = row
    out["boundaryField"] = bcs
    out["syntax_errors"] = [d.message for d in doc.diagnostics if d.severity == "error"][:3]
    return out


class InspectorPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.title = QLabel("Nothing selected")
        self.title.setStyleSheet(f"color: {T.text}; font-weight: 600;")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        lay.addWidget(self.tree, 1)
        self.hint = QLabel("Select a file in Case Files, probe a cell in the 3D viewport (right-click), move the "
                           "editor cursor onto a keyword, or pick a mesh hotspot.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.hint)
        self.sv.selection_changed.connect(self.show)
        if self.sv.selection:
            self.show(self.sv.selection)

    def _row(self, parent, key: str, value="", *, bold: bool = False) -> QTreeWidgetItem:
        it = QTreeWidgetItem([key, _fmt(value) if value != "" else ""])
        it.setToolTip(1, _fmt(value) if value != "" else "")
        if bold:
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
        (parent.addChild(it) if isinstance(parent, QTreeWidgetItem) else self.tree.addTopLevelItem(it))
        return it

    def show(self, sel: dict | None) -> None:
        self.tree.clear()
        if not sel:
            self.title.setText("Nothing selected")
            return
        self.hint.hide()
        kind = sel.get("kind")
        try:
            getattr(self, f"_show_{kind}", self._show_generic)(sel)
        except Exception as exc:
            self.title.setText(f"{kind}: could not read details ({exc})")
        self.tree.expandAll()

    # ------------------------------------------------------------------ kinds
    def _show_cell(self, sel: dict) -> None:
        p = sel.get("point") or [0, 0, 0]
        cid = sel.get("cell")
        self.title.setText(f"Cell {cid} at ({p[0]:.5g}, {p[1]:.5g}, {p[2]:.5g})" if cid is not None
                           else "No cell at the probed point")
        self._row(None, "region", sel.get("region", ""))
        self._row(None, "time", sel.get("time", ""))
        vals = self._row(None, "fields", bold=True)
        for k in sorted(sel.get("values", {})):
            self._row(vals, k, sel["values"][k])

    def _show_file(self, sel: dict) -> None:
        path = Path(sel["path"])
        st = path.stat()
        self.title.setText(path.name)
        root = self.sv.session.root if self.sv.session else None
        rel = path.relative_to(root).as_posix() if root and path.is_relative_to(root) else str(path)
        self._row(None, "path", rel)
        self._row(None, "size", f"{st.st_size:,} bytes")
        self._row(None, "modified", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))
        if root:
            from aethercfd.case.model import classify_file

            kind = classify_file(root, path)
            self._row(None, "kind", kind.value)
            if kind.value in ("field", "dict") and st.st_size < 200 * 2**20:
                s = field_summary(path)
                h = self._row(None, "header", bold=True)
                for k, v in s["header"].items():
                    self._row(h, k, v)
                for k in ("dimensions", "internalField"):
                    if k in s:
                        self._row(None, k, s[k])
                if s["boundaryField"]:
                    b = self._row(None, "boundaryField", bold=True)
                    for patch, row in s["boundaryField"].items():
                        pi = self._row(b, patch, row.get("type", ""))
                        for k, v in row.items():
                            if k != "type":
                                self._row(pi, k, v)
                for e in s["syntax_errors"]:
                    self._row(None, "syntax error", e)

    def _show_keyword(self, sel: dict) -> None:
        from aethercfd.core.textio import decode_bytes
        from aethercfd.foamdict.parser import parse
        from aethercfd.foamdict.semantic import DictView

        path = Path(sel["file"])
        key = "/".join(sel["path"]) if isinstance(sel.get("path"), list) else str(sel.get("key", ""))
        self.title.setText(f"{path.name} › {key.replace('/', ' › ')}")
        text = sel.get("text")
        if text is None:
            text = decode_bytes(path.read_bytes())[0]
        doc = parse(text)
        lk = DictView(doc).lookup(key)
        if lk is None:
            self._row(None, "not found", key)
            return
        self._row(None, "provenance", lk.provenance.value)
        if lk.sources:
            self._row(None, "comes from", ", ".join(lk.sources))
        if lk.matched_pattern:
            self._row(None, "matched by", "a regular-expression key")
        if lk.entry is not None:
            self._row(None, "line", doc.line_col(lk.entry.start)[0])
            if lk.entry.value is not None:
                self._row(None, "value", doc.text(lk.entry.value.start, lk.entry.value.end).strip())
            elif lk.entry.dict is not None:
                self._row(None, "sub-dictionary", ", ".join(DictView(doc).keys(key))[:300])
        self._row(None, "editable in forms", "yes" if lk.editable_here else "no (edit it at its source)")

    def _show_patch(self, sel: dict) -> None:
        s = self.sv.session
        name = sel["name"]
        self.title.setText(f"Patch {name}")
        if s is None:
            return
        case = s.case()
        for r in case.regions:
            for p in r.patches:
                if p.name == name:
                    self._row(None, "region", r.name or "(single)")
                    self._row(None, "type", p.type)
                    self._row(None, "faces", p.n_faces or "")
                    if p.in_groups:
                        self._row(None, "groups", ", ".join(p.in_groups))
        zero = case.root / "0" / (sel.get("region") or "")
        if zero.is_dir():
            b = self._row(None, "boundary conditions", bold=True)
            for f in sorted(x for x in zero.iterdir() if x.is_file()):
                try:
                    row = field_summary(f)["boundaryField"].get(name)
                except Exception:
                    row = None
                if row:
                    fi = self._row(b, f.name, row.get("type", ""))
                    for k, v in row.items():
                        if k != "type":
                            self._row(fi, k, v)

    def _show_generic(self, sel: dict) -> None:
        self.title.setText(str(sel.get("title") or sel.get("kind", "selection")))
        for k, v in sel.items():
            if k in ("kind", "title"):
                continue
            if isinstance(v, dict):
                it = self._row(None, k, bold=True)
                for kk, vv in v.items():
                    self._row(it, kk, vv)
            else:
                self._row(None, k, v)


PANELS = [PanelSpec("aether.inspector", "Properties", "msc.symbol-property", "Inspect", InspectorPanel,
                    default_area=Area.RIGHT, description="Details of the selected cell, file, keyword, patch "
                    "or mesh hotspot")]
