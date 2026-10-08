"""New Case from Template: answer a few physics questions, pick a ranked tutorial-based template, choose
where the case goes, adjust the first parameters. The case is the tutorial plus journalled edits."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from aethercfd.core.paths import openfoam_path_safety, win_to_wsl
from aethercfd.studio.theme.tokens import T
from aethercfd.templates.catalog import QUESTIONS, TEMPLATES, Template, rank


class NewCaseWizard(QWizard):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.sv = services
        self.setWindowTitle("New Case from Template")
        self.resize(820, 600)
        self.runner = None
        self.forks: list = []
        self.available: dict[str, dict[str, bool]] = {}
        self.case_dir: Path | None = None
        self.values: dict[str, tuple[str, str, str]] = {}
        self.p1, self.p2, self.p3 = _ChoosePage(self), _WherePage(self), _ParamsPage(self)
        for p in (self.p1, self.p2, self.p3):
            self.addPage(p)
        self._load_backend()

    def _load_backend(self) -> None:
        def work():
            from aethercfd.backend.wsl.runner import WslRunner
            from aethercfd.scripting.api import load_forks
            from aethercfd.templates.create import available

            w = self.sv.settings.wsl
            runner = WslRunner(w.default_distro, allowed=w.allowed_distros)
            forks = load_forks(runner)
            return runner, forks, {f.id: available(runner, f, TEMPLATES) for f in forks}

        def done(res):
            value, err = res
            if err is not None:
                self.p1.status.setText(f"OpenFOAM is not reachable: {err}")
                return
            self.runner, self.forks, self.available = value
            self.p1.fill_forks()

        self.p1.status.setText("Checking the installed OpenFOAM versions and their tutorials…")
        self.sv.run_bg(work, done)

    @property
    def fork(self):
        return self.p1.chosen_fork()

    @property
    def template(self) -> Template | None:
        it = self.p1.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def accept(self) -> None:
        if self.case_dir is None or self.template is None:
            return super().accept()
        sv = self.sv
        sv.open_case(self.case_dir, create_project=True)
        s = sv.session
        from aethercfd.commands.base import USER
        from aethercfd.templates.create import new_value_text

        changed, failed = 0, []
        for p in self.template.params:
            box = self.p3.edits.get(p.id)
            if box is None or p.id not in self.values:
                continue
            rel, key, cur = self.values[p.id]
            txt = box.text().strip()
            if not txt or txt == box.property("initial"):
                continue
            try:
                value = new_value_text(p.kind, cur, txt if p.kind == "vector" else float(txt))
            except ValueError:
                failed.append(f"{p.label}: {txt!r} is not a number")
                continue
            res = s.executor.execute("dict.set", {"file": str(self.case_dir / rel), "key": key, "value": value}, USER)
            if res.ok:
                changed += 1
            else:
                failed.append(f"{p.label}: {res.message}")
        msg = f"Created {self.case_dir.name} from '{self.template.title}'" + (f"; {changed} parameter(s) set"
                                                                              if changed else "")
        sv.notify(msg)
        if failed:
            QMessageBox.warning(self, "Some parameters were not set", "\n".join(failed))
        if self.template.allrun:
            QMessageBox.information(self, "Next step", f"{self.template.notes or ''}\n\nThis tutorial prepares its "
                                    "mesh/fields with its Allrun script: open the Terminal panel in the case and run "
                                    "./Allrun, or start the steps from the Run panel.")
        super().accept()


class _ChoosePage(QWizardPage):
    def __init__(self, wiz: NewCaseWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("What are you simulating?")
        self.setSubTitle("Templates are OpenFOAM's own tutorials, ranked by how well they fit your answers.")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.boxes: dict[str, QComboBox] = {}
        for qid, label, options in QUESTIONS:
            cb = QComboBox()
            for val, text in options:
                cb.addItem(text, val)
            cb.currentIndexChanged.connect(self.rerank)
            self.boxes[qid] = cb
            form.addRow(label, cb)
        self.fork = QComboBox()
        self.fork.currentIndexChanged.connect(self.rerank)
        form.addRow("OpenFOAM version", self.fork)
        lay.addLayout(form)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.currentItemChanged.connect(lambda *_: self.completeChanged.emit())
        lay.addWidget(self.list, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {T.text_faint};")
        lay.addWidget(self.status)
        self.rerank()

    def fill_forks(self) -> None:
        self.fork.blockSignals(True)
        self.fork.clear()
        for f in self.wiz.forks:
            self.fork.addItem(f.label, f.id)
        self.fork.blockSignals(False)
        self.status.setText(f"{len(self.wiz.forks)} OpenFOAM version(s) found")
        self.rerank()

    def chosen_fork(self):
        fid = self.fork.currentData()
        return next((f for f in self.wiz.forks if f.id == fid), None)

    def rerank(self) -> None:
        answers = {qid: cb.currentData() for qid, cb in self.boxes.items()}
        fork = self.chosen_fork()
        fam = str(fork.family.value) if fork else None
        avail = self.wiz.available.get(fork.id, {}) if fork else {}
        self.list.clear()
        for t, _score, why in rank(answers, fam):
            ok = avail.get(t.id, fork is None)
            it = QListWidgetItem(f"{t.title}   [{t.cost}]\n{t.summary}" + (f"\nfits: {', '.join(why)}" if why else ""))
            it.setData(Qt.ItemDataRole.UserRole, t)
            if not ok:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                it.setText(it.text() + "\n(not in this OpenFOAM installation)")
            self.list.addItem(it)
        for i in range(self.list.count()):
            if self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(i)
                break
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        it = self.list.currentItem()
        return bool(it and it.flags() & Qt.ItemFlag.ItemIsEnabled and self.chosen_fork() is not None)


class _WherePage(QWizardPage):
    def __init__(self, wiz: NewCaseWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("Where should the case go?")
        lay = QFormLayout(self)
        roots = list(getattr(wiz.sv.settings, "case_roots", []) or [])
        self.parent_dir = QLineEdit(str(roots[0]) if roots else r"D:\OpenFOAM")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.parent_dir, 1)
        row.addWidget(browse)
        lay.addRow("Folder", row)
        self.name = QLineEdit()
        lay.addRow("Case name", self.name)
        self.check = QLabel()
        self.check.setWordWrap(True)
        lay.addRow(self.check)
        for w in (self.parent_dir, self.name):
            w.textChanged.connect(self._validate)

    def initializePage(self) -> None:
        t = self.wiz.template
        if t and not self.name.text():
            self.name.setText(f"{t.id}_1")
        self._validate()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for the new case", self.parent_dir.text())
        if d:
            self.parent_dir.setText(d)

    def target(self) -> Path:
        return Path(self.parent_dir.text().strip()) / self.name.text().strip()

    def _validate(self) -> None:
        t = self.target()
        msg, ok = "", True
        if not self.name.text().strip():
            msg, ok = "Give the case a name.", False
        elif t.exists():
            msg, ok = f"{t} already exists.", False
        else:
            s = openfoam_path_safety(win_to_wsl(str(t)))
            if not s.safe:
                msg, ok = f"OpenFOAM cannot use this path ({', '.join(s.problems)}).", False
            else:
                msg = f"The tutorial is copied to {t}"
        self.check.setText(msg)
        self.check.setStyleSheet(f"color: {T.text_dim if ok else T.warn};")
        self._ok = ok
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return getattr(self, "_ok", False)

    def validatePage(self) -> bool:
        from aethercfd.templates.create import copy_tutorial, current_values, family_of

        wiz = self.wiz
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            copy_tutorial(wiz.runner, wiz.fork, wiz.template, self.target())
        except Exception as exc:
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Could not create the case", str(exc))
            return False
        QGuiApplication.restoreOverrideCursor()
        wiz.case_dir = self.target()
        wiz.values = current_values(wiz.case_dir, wiz.template, family_of(wiz.fork))
        return True


class _ParamsPage(QWizardPage):
    def __init__(self, wiz: NewCaseWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("First parameters")
        self.setSubTitle("Change what you need; everything else stays as the tutorial has it. Each change is a "
                         "journalled edit you can revert.")
        self.lay = QFormLayout(self)
        self.edits: dict[str, QLineEdit] = {}

    def initializePage(self) -> None:
        from aethercfd.templates.create import display_value

        while self.lay.rowCount():
            self.lay.removeRow(0)
        self.edits.clear()
        t = self.wiz.template
        if t is None:
            return
        for p in t.params:
            cur = self.wiz.values.get(p.id)
            if cur is None:
                continue
            v = display_value(p.kind, cur[2])
            txt = " ".join(f"{x:g}" for x in v) if isinstance(v, list) else ("" if v is None else f"{v:g}"
                                                                            if isinstance(v, float) else str(v))
            e = QLineEdit(txt)
            e.setProperty("initial", txt)
            e.setToolTip(f"{cur[0]} › {cur[1]} = {cur[2]}" + (f"\n{p.help}" if p.help else ""))
            self.edits[p.id] = e
            self.lay.addRow(f"{p.label} [{p.unit}]", e)
        if not self.edits:
            self.lay.addRow(QLabel("This template has no quick parameters; edit the case files directly."))
        if t.notes:
            n = QLabel(t.notes)
            n.setWordWrap(True)
            n.setStyleSheet(f"color: {T.text_dim};")
            self.lay.addRow(n)
        self.open_after = QCheckBox("Open the case when done")
        self.open_after.setChecked(True)
        self.open_after.setEnabled(False)
        self.lay.addRow(self.open_after)
