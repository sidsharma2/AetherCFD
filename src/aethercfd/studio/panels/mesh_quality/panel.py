"""Mesh Quality: checkMesh results with explanations (why it matters, what usually fixes it).

Reads the case's newest log.checkMesh immediately; "Run checkMesh" goes through the run.start
command (plan shown first, placement handled, journalled) and the table refreshes when it ends.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from aethercfd.case.checkmesh import CheckMeshReport, parse_checkmesh
from aethercfd.commands.base import USER
from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

_SEV = {"ok": (T.ok, "OK"), "warn": (T.warn, "check"), "fail": (T.error, "FAIL")}


class MeshQualityPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        sv = ctx.services
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        self.headline = QLabel("No checkMesh result yet")
        self.headline.setStyleSheet("font-size: 10.5pt;")
        bar.addWidget(self.headline, 1)
        run = QPushButton(icon("msc.run-all"), "Run checkMesh…")
        run.clicked.connect(self.run_check)
        bar.addWidget(run)
        lay.addLayout(bar)
        self.source = QLabel()
        self.source.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.source)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Check", "Value", "Status"])
        self.table.setRootIsDecorated(False)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 220)
        self.table.currentItemChanged.connect(self._explain)
        split.addWidget(self.table)
        self.explain = QTextBrowser()
        self.explain.setOpenExternalLinks(False)
        split.addWidget(self.explain)
        split.setSizes([300, 160])
        lay.addWidget(split, 1)
        self.report: CheckMeshReport | None = None
        sv.case_changed.connect(lambda s: self.load_latest())
        sv.bridge.event.connect(self._on_event)
        self.load_latest()

    def _log_path(self) -> Path | None:
        s = self.ctx.services.session
        if s is None:
            return None
        cands = [p for p in s.root.glob("log.checkMesh*") if p.is_file()]
        if (s.root / "logs").is_dir():
            cands += [p for p in (s.root / "logs").glob("log.checkMesh*") if p.is_file()]
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None

    def load_latest(self) -> None:
        p = self._log_path()
        self.table.clear()
        self.explain.clear()
        if p is None:
            self.headline.setText("No checkMesh result yet")
            self.source.setText("Run checkMesh to see mesh quality.")
            return
        self.report = parse_checkmesh(p.read_text(encoding="utf-8", errors="replace"))
        self.source.setText(f"{p.name}  ·  {p.parent}")
        r = self.report
        if r.mesh_ok:
            self.headline.setText(f"<span style='color:{T.ok}'>● Mesh OK</span>")
        elif r.failed:
            self.headline.setText(f"<span style='color:{T.error}'>● {r.failed} failed check(s)</span>")
        else:
            self.headline.setText("checkMesh did not finish" if not r.finished else "Mesh checked")
        cells = r.stats.get("cells")
        if cells is not None:
            types = ", ".join(f"{k} {v:,}" for k, v in r.cell_types.items() if v)
            it = QTreeWidgetItem(["Cells", f"{cells:,}", ""])
            it.setToolTip(1, types)
            self.table.addTopLevelItem(it)
        if r.directions:
            self.table.addTopLevelItem(QTreeWidgetItem(["Dimensions", r.directions.replace("Mesh has ", ""), ""]))
        for c in r.checks:
            col, label = _SEV.get(c.severity, (T.text, c.severity))
            it = QTreeWidgetItem([c.name, c.value, label])
            it.setForeground(2, QColor(col))
            it.setData(0, Qt.ItemDataRole.UserRole, c)
            self.table.addTopLevelItem(it)
        for st in r.sets:
            it = QTreeWidgetItem([f"set: {st.name}", f"{st.count:,} {st.kind}", "written"])
            it.setToolTip(0, f"{st.reason}; stored in constant/polyMesh/sets/{st.name}")
            it.setForeground(2, QColor(T.text_dim))
            self.table.addTopLevelItem(it)
        first_problem = next((self.table.topLevelItem(i) for i in range(self.table.topLevelItemCount())
                              if (c := self.table.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)) is not None
                              and c.severity != "ok"), None)
        if first_problem is not None:
            self.table.setCurrentItem(first_problem)

    def _explain(self, cur: QTreeWidgetItem | None, _prev=None) -> None:
        c = cur.data(0, Qt.ItemDataRole.UserRole) if cur is not None else None
        if c is None:
            self.explain.clear()
            return
        html = f"<b>{c.name}</b> — {c.value}<br><span style='color:{T.text_faint}'>{c.message}</span>"
        if c.advice:
            html += f"<p>{c.advice}</p>"
        else:
            html += "<p>Within normal limits.</p>"
        self.explain.setHtml(html)

    def run_check(self) -> None:
        sv = self.ctx.services
        s = sv.session
        if s is None or s.runner is None or not sv.ensure_project():
            sv.notify("Open a case first (the OpenFOAM backend starts with it)", "warn")
            return
        from aethercfd.studio.panels.run.panels import PlanDialog

        params = {"case": str(s.root), "preset": "check"}
        plan = s.executor.plan("run.start", params, USER)
        dlg = PlanDialog(plan, self)
        if dlg.exec() != PlanDialog.DialogCode.Accepted:
            return
        res = s.executor.execute("run.start", params, USER, plan=plan, approved_hash=plan.plan_hash)
        sv.notify(res.message if res.ok else f"checkMesh did not start: {res.message}", "info" if res.ok else "warn")
        self._pending = res.data if res.ok else None

    def _on_event(self, topic: str, p: dict) -> None:
        if topic == "run.state" and p.get("state") in ("finished", "failed") and p.get("run_id") == getattr(
                self, "_pending", None):
            self.load_latest()


PANELS = [PanelSpec("aether.mesh_quality", "Mesh Quality", "mdi6.vector-triangle", "Visualization",
                    MeshQualityPanel, default_area=Area.RIGHT,
                    description="checkMesh results with explanations and remedies")]
