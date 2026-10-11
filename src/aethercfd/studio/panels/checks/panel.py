"""Checks: the numerics lab and pre-run checks for the open case (aethercfd.case.checks).

Findings are ranked (errors first) with the reason and the evidence; "Apply fix" shows every edit as one
plan (diffs) and applies them as journalled dict.set commands. Re-runs when system/, constant/ or 0/
files change.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon
from aethercfd.studio.theme.tokens import T

_SEV = {"error": ("msc.error", "error"), "warn": ("msc.warning", "warn"), "info": ("msc.info", "info")}  # tokens


class ChecksPanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        self.findings = []
        self.facts = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        self.summary = QLabel("No case")
        self.summary.setStyleSheet(f"color: {T.text_dim};")
        self.btn_run = QPushButton(icon("msc.refresh"), "Re-check")
        top.addWidget(self.summary, 1)
        top.addWidget(self.btn_run)
        lay.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        self.list = QListWidget()
        split.addWidget(self.list)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        self.text = QTextBrowser()
        self.btn_fix = QPushButton(icon("mdi6.auto-fix"), "Apply fix…")
        self.btn_fix.setEnabled(False)
        dl.addWidget(self.text, 1)
        dl.addWidget(self.btn_fix)
        split.addWidget(detail)
        split.setSizes([220, 260])
        lay.addWidget(split, 1)
        self.btn_run.clicked.connect(self.run)
        self.list.currentRowChanged.connect(self._show)
        self.btn_fix.clicked.connect(self._fix)
        self._timer = QTimer(self, singleShot=True, interval=1500, timeout=self.run)
        self.sv.case_changed.connect(lambda _s: self._timer.start())
        self.sv.files_changed.connect(self._on_files)
        if self.sv.session is not None:
            self._timer.start()

    def _on_files(self, paths: list) -> None:
        s = self.sv.session
        if s is None:
            return
        for p in paths:
            try:
                top = Path(p).relative_to(s.root).parts[0]
            except (ValueError, IndexError):
                continue
            if top in ("system", "constant", "0", "0.orig") or top.startswith("log.checkMesh"):
                self._timer.start()
                return

    def run(self) -> None:
        s = self.sv.session
        if s is None:
            self.summary.setText("No case")
            return
        self.summary.setText("Checking…")

        def work():
            from aethercfd.case.checks import run_checks

            return run_checks(s.case())

        def done(res):
            value, err = res
            if err is not None:
                self.summary.setText(f"Checks failed: {err}")
                return
            self.facts, self.findings = value
            self._fill()

        self.sv.run_bg(work, done)

    def _fill(self) -> None:
        self.list.clear()
        counts = {"error": 0, "warn": 0, "info": 0}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            ic, tok = _SEV.get(f.severity, ("msc.info", "text"))
            it = QListWidgetItem(icon(ic, getattr(T, tok)), f.title)
            it.setToolTip(f.why)
            self.list.addItem(it)
        fa = self.facts
        desc = (f"{'steady' if fa.steady else 'transient'} · {'compressible' if fa.compressible else 'incompressible'}"
                f" · {fa.algorithm} · {fa.turbulence or 'no turbulence model'}"
                + (f" · mesh non-orth {fa.non_ortho_max:.1f}°" if fa.non_ortho_max is not None else ""))
        self.summary.setText(f"{counts['error']} errors, {counts['warn']} warnings, {counts['info']} notes  ·  {desc}")
        if self.findings:
            self.list.setCurrentRow(0)
        else:
            self.text.setHtml(f"<p style='color:{T.ok}'>No problems found by the numerics and pre-run checks.</p>")
            self.btn_fix.setEnabled(False)

    def _show(self, row: int) -> None:
        if not (0 <= row < len(self.findings)):
            return
        f = self.findings[row]
        ev = "".join(f"<li><code>{_esc(e)}</code></li>" for e in f.evidence)
        fix = (f"<p><b>Fix:</b> {_esc(f.fix_label)}<br>" + "<br>".join(
            f"<code>{_esc(e.file)} › {_esc(e.key)} = {_esc(e.value)}</code>" for e in f.edits) + "</p>") if f.edits else ""
        color = getattr(T, _SEV.get(f.severity, ("", "text"))[1])
        self.text.setHtml(f"<p style='color:{color};font-weight:600'>{_esc(f.title)}</p><p>{_esc(f.why)}</p>"
                          f"<ul>{ev}</ul>{fix}")
        self.btn_fix.setEnabled(bool(f.edits))

    def _fix(self) -> None:
        row = self.list.currentRow()
        s = self.sv.session
        if not (0 <= row < len(self.findings)) or s is None or not self.sv.ensure_project():
            return
        f = self.findings[row]
        from aethercfd.commands.base import USER
        from aethercfd.studio.widgets.approval import ApprovalDialog

        # one dict.batch plan: edits of the same file are applied in order on the evolving text (separate
        # dict.set plans of one file conflict: the second was planned against the original content)
        params = {"edits": [{"file": str(s.root / e.file), "key": e.key, "value": e.value,
                             "insert_if_missing": e.insert_if_missing} for e in f.edits],
                  "summary": f"{f.fix_label} ({f.title})"}
        try:
            plan = s.executor.plan("dict.batch", params, USER, rationale=f.why)
        except Exception as exc:
            self.sv.notify(f"Cannot plan the fix: {exc}", "error")
            return
        dlg = ApprovalDialog(plan, self, who="AetherCFD checks")
        if dlg.exec() != ApprovalDialog.DialogCode.Accepted:
            return
        res = s.executor.execute("dict.batch", params, USER, plan=plan, approved_hash=plan.plan_hash)
        self.sv.notify("Fix applied (see the Command Journal)" if res.ok else f"Fix failed: {res.message}",
                       "info" if res.ok else "error")
        self._timer.start()


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PANELS = [PanelSpec("aether.checks", "Checks", "mdi6.stethoscope", "Case", ChecksPanel, default_area=Area.RIGHT,
                    tab_group="assist",
                    description="Numerics lab and pre-run checks with one-click, journalled fixes")]
