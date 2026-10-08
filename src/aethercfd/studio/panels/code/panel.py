"""Physics code: write your own OpenFOAM models, sources, BCs and solvers.

Pick a template (e.g. k-epsilon with Pope's round-jet correction), name it, create it in the case's
aether-code/ folder, edit it in the editor, build it with wmake inside WSL (errors are clickable), then
"Use in case" adds the libs entry and model selection with one approval.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from aethercfd.studio.shell.panel_base import Area, Panel, PanelContext, PanelSpec
from aethercfd.studio.theme.style import icon, mono_font
from aethercfd.studio.theme.tokens import T


class CodePanel(Panel):
    def __init__(self, ctx: PanelContext):
        super().__init__(ctx)
        self.sv = ctx.services
        from aethercfd.codegen.templates import TEMPLATES

        self.templates = TEMPLATES
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        for t in TEMPLATES:
            it = QListWidgetItem(icon({"library": "mdi6.library", "runtime": "mdi6.code-braces",
                                       "solver": "mdi6.cog-play"}[t.kind]), f"{t.title}   [{t.family}]")
            it.setToolTip(t.summary)
            self.list.addItem(it)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"color: {T.text_dim};")
        form = QFormLayout()
        self.name = QLineEdit()
        self.base = QLineEdit("rhoSimpleFoam", toolTip="Solver to copy (solver template)")
        form.addRow("Name", self.name)
        form.addRow("Copy solver", self.base)
        row = QHBoxLayout()
        self.btn_create = QPushButton(icon("msc.new-file"), "Create in case")
        self.btn_build = QPushButton(icon("mdi6.hammer-wrench"), "Build (wmake in WSL)")
        self.btn_use = QPushButton(icon("mdi6.link-variant"), "Use in case…")
        for b in (self.btn_create, self.btn_build, self.btn_use):
            row.addWidget(b)
        tl.addWidget(QLabel("Templates"))
        tl.addWidget(self.list, 1)
        tl.addWidget(self.summary)
        tl.addLayout(form)
        tl.addLayout(row)
        split.addWidget(top)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.files = QListWidget(toolTip="Double-click to edit")
        self.problems = QListWidget(toolTip="Compiler errors: double-click to jump to the line")
        self.output = QPlainTextEdit(readOnly=True)
        self.output.setFont(mono_font(8.5))
        self.output.setMaximumBlockCount(4000)
        bl.addWidget(QLabel("Files"))
        bl.addWidget(self.files, 1)
        bl.addWidget(QLabel("Problems"))
        bl.addWidget(self.problems, 1)
        bl.addWidget(self.output, 2)
        split.addWidget(bottom)
        lay.addWidget(split, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.status)
        self.list.currentRowChanged.connect(self._select)
        self.btn_create.clicked.connect(self.create)
        self.btn_build.clicked.connect(self.build)
        self.btn_use.clicked.connect(self.use)
        self.files.itemDoubleClicked.connect(lambda it: self._open(Path(it.data(Qt.ItemDataRole.UserRole))))
        self.problems.itemDoubleClicked.connect(self._open_problem)
        self.list.setCurrentRow(0)

    @property
    def t(self):
        i = self.list.currentRow()
        return self.templates[i] if 0 <= i < len(self.templates) else None

    def _dir(self) -> Path | None:
        s = self.sv.session
        return None if s is None or not self.name.text().strip() else s.root / "aether-code" / self.name.text().strip()

    def _select(self, _row: int) -> None:
        t = self.t
        if t is None:
            return
        self.summary.setText(f"{t.summary}\n\nUse: {t.usage}")
        if not self.name.text() or self.name.text() in [x.id for x in self.templates]:
            self.name.setText(t.id)
        self.base.setVisible(t.kind == "solver")
        self.btn_build.setEnabled(t.kind != "runtime")
        self._refresh_files()

    def _refresh_files(self) -> None:
        self.files.clear()
        d = self._dir()
        if d is None or not d.exists():
            return
        for f in sorted(p for p in d.rglob("*") if p.is_file() and "linux64" not in str(p) and "lnInclude" not in
                        str(p)):
            it = QListWidgetItem(str(f.relative_to(d)))
            it.setData(Qt.ItemDataRole.UserRole, str(f))
            self.files.addItem(it)

    def _open(self, path: Path, line: int | None = None) -> None:
        spec = self.sv.panels.get("aether.editor")
        d = self.sv.host.open_panel(spec, args={"path": str(path)})
        p = self.sv.host.build_panel(d.objectName())
        if line and p is not None:
            p.editor.goto_line(line)

    def _open_problem(self, it: QListWidgetItem) -> None:
        f, line = it.data(Qt.ItemDataRole.UserRole)
        if Path(f).exists():
            self._open(Path(f), line)

    # ------------------------------------------------------------------ actions
    def create(self) -> None:
        t, d, s = self.t, self._dir(), self.sv.session
        if t is None or d is None or s is None or not self.sv.ensure_project():
            self.sv.notify("Open a case and give the code a name", "warn")
            return
        from aethercfd.commands.base import USER

        if t.kind == "solver":
            if s.runner is None or not s.forks:
                self.sv.notify("The WSL backend is still starting", "warn")
                return
            from aethercfd.codegen.build import copy_solver

            try:
                msg = copy_solver(s.runner, s.forks[0], self.base.text().strip(), self.name.text().strip(), d)
            except Exception as exc:
                self.sv.notify(f"Copy failed: {exc}", "error")
                return
            self.sv.notify(msg)
        res = s.executor.execute("code.create", {"template": t.id, "name": self.name.text().strip(), "dest": str(d)},
                                 USER)
        self.sv.notify(res.message if res.ok else f"Create failed: {res.message}", "info" if res.ok else "error")
        self._refresh_files()
        if res.ok and self.files.count():
            self._open(Path(self.files.item(0).data(Qt.ItemDataRole.UserRole)))

    def build(self) -> None:
        d, s = self._dir(), self.sv.session
        if d is None or s is None:
            return
        target = "wmake" if (self.t and self.t.kind == "solver") else "libso"
        from aethercfd.commands.base import USER

        try:
            plan = s.executor.plan("code.build", {"dir": str(d), "target": "" if target == "wmake" else target}, USER)
        except Exception as exc:
            self.sv.notify(str(exc), "error")
            return
        if plan.blocked:
            self.sv.notify("; ".join(c.message for c in plan.blocked), "warn")
            return
        self.status.setText("Building with wmake inside WSL… (about 1-2 minutes)")
        self.btn_build.setEnabled(False)

        def work():
            return s.executor.execute("code.build", {}, USER, plan=plan, approved_hash=plan.plan_hash)

        def done(res_err):
            res, err = res_err
            self.btn_build.setEnabled(True)
            if err is not None:
                self.status.setText(f"Build failed: {err}")
                return
            data = res.data or {}
            self.output.setPlainText(data.get("output", ""))
            self.output.moveCursor(self.output.textCursor().MoveOperation.End)
            self.problems.clear()
            for dg in data.get("diagnostics", []):
                it = QListWidgetItem(icon("msc.error" if dg["severity"] == "error" else "msc.warning",
                                          T.error if dg["severity"] == "error" else T.warn),
                                     f"{Path(dg['file']).name}:{dg['line']}  {dg['message']}")
                it.setData(Qt.ItemDataRole.UserRole, (dg["file"], dg["line"]))
                self.problems.addItem(it)
            self.status.setText(res.message)
            self.sv.notify(res.message, "info" if res.ok else "error")

        self.sv.run_bg(work, done)

    def use(self) -> None:
        t, s = self.t, self.sv.session
        if t is None or s is None or not self.sv.ensure_project():
            return
        from aethercfd.codegen.templates import case_entries
        from aethercfd.commands.base import USER, CommandPlan, Risk
        from aethercfd.studio.widgets.approval import ApprovalDialog

        entries = case_entries(t, self.name.text().strip())
        if not entries:
            self.sv.notify(t.usage)
            return
        plans = []
        for rel, key, value in entries:
            f = s.root / rel
            if not f.exists() and rel.endswith("turbulenceProperties") and (s.root / "constant/momentumTransport").exists():
                f = s.root / "constant/momentumTransport"
            params = {"file": str(f), "key": key, "value": value, "insert_if_missing": True}
            try:
                plans.append((params, s.executor.plan("dict.set", params, USER)))
            except Exception as exc:
                self.sv.notify(f"{rel} › {key}: {exc}", "warn")
        combined = CommandPlan("code.use", {}, USER, Risk.EDIT, f"use {self.name.text()} in this case")
        for _p, pl in plans:
            combined.preconditions += pl.preconditions
            combined.file_changes += pl.file_changes
        if ApprovalDialog(combined, self, who="Physics code").exec() != ApprovalDialog.DialogCode.Accepted:
            return
        bad = [r.message for p, pl in plans
               if not (r := s.executor.execute("dict.set", p, USER, plan=pl, approved_hash=pl.plan_hash)).ok]
        self.sv.notify("Case now uses the new model (see the Command Journal)" if not bad else bad[0],
                       "info" if not bad else "error")


PANELS = [PanelSpec("aether.code", "Physics Code", "mdi6.code-tags", "Physics", CodePanel, default_area=Area.RIGHT,
                    description="Write your own OpenFOAM models (e.g. Pope-corrected k-epsilon), sources, BCs and "
                    "solvers; build with wmake in WSL")]
