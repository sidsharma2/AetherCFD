"""First-run setup wizard (Help > Setup Wizard): WSL + OpenFOAM, case folders, optional tools, the copilot's AI.

It only looks and explains: missing software comes with the exact commands to run yourself (nothing is
installed or enabled by the wizard). The one thing that boots something is "Look for OpenFOAM", which
starts the chosen distro to find its OpenFOAM installs; the AI sign-in opens the provider's own login.
Settings are written only when you press Finish.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from aethercfd.core.settings import default_case_root
from aethercfd.firstrun import detect
from aethercfd.studio.theme.tokens import T

CREATE_NEW_CONSOLE = 0x00000010


def _mono_box(height: int = 150) -> QPlainTextEdit:
    box = QPlainTextEdit(readOnly=True)
    f = QFont("Consolas")
    f.setPointSizeF(8.5)
    box.setFont(f)
    box.setMinimumHeight(height)
    box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return box


def _dim(text: str) -> QLabel:
    lab = QLabel(text, wordWrap=True)
    lab.setStyleSheet(f"color: {T.text_dim};")
    return lab


# ======================================================================================================== pages
class IntroPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Set up AetherCFD on this PC")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "AetherCFD drives OpenFOAM inside WSL (Windows Subsystem for Linux). This wizard checks what this PC "
            "has, tells you exactly what is missing and how to get it, and remembers your choices:\n\n"
            "  1. WSL and an OpenFOAM installation (ESI v2306 or Foundation 14)\n"
            "  2. Where your cases live\n"
            "  3. Optional tools: ParaView, FreeCAD, a PyTorch environment for PINNs\n"
            "  4. The copilot's AI: Claude Code or OpenAI Codex, and its sign-in\n\n"
            "Nothing is installed by the wizard. You can open it again from Help > Setup Wizard.", wordWrap=True))
        lay.addStretch(1)


class OpenFoamPage(QWizardPage):
    _wsl_done = Signal(object)
    _probe_done = Signal(object)

    def __init__(self, wiz: SetupWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("WSL and OpenFOAM")
        self.setSubTitle("The Linux distro AetherCFD runs OpenFOAM in.")
        self.report: detect.WslReport | None = None
        self.openfoam: dict[str, detect.OpenFoamReport] = {}
        lay = QVBoxLayout(self)
        self.status = QLabel("Looking for WSL…", wordWrap=True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addWidget(QLabel("Distro:"))
        self.distro = QComboBox(minimumWidth=200)
        row.addWidget(self.distro, 1)
        self.btn_probe = QPushButton("Look for OpenFOAM")
        self.btn_probe.setToolTip("Starts this distro and looks for OpenFOAM installs (takes up to a minute)")
        self.btn_again = QPushButton("Look again")
        row.addWidget(self.btn_probe)
        row.addWidget(self.btn_again)
        lay.addLayout(row)
        self.details = _mono_box(190)
        lay.addWidget(self.details, 1)
        crow = QHBoxLayout()
        self.btn_copy = QPushButton("Copy instructions")
        crow.addWidget(self.btn_copy)
        crow.addStretch(1)
        lay.addLayout(crow)
        lay.addWidget(_dim("You can finish without OpenFOAM (to look at cases and results); running needs it."))
        self.btn_again.clicked.connect(self.refresh)
        self.btn_probe.clicked.connect(self.probe)
        self.btn_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.details.toPlainText()))
        self.distro.currentIndexChanged.connect(self._show_distro)
        self._wsl_done.connect(self._on_wsl)
        self._probe_done.connect(self._on_probe)
        self._started = False

    def initializePage(self) -> None:
        if not self._started:
            self._started = True
            self.refresh()

    def refresh(self) -> None:
        self.status.setText("Looking for WSL…")
        self.btn_again.setEnabled(False)
        self.wiz.run_bg(self.wiz.detector.check_wsl, self._wsl_done.emit)

    def _on_wsl(self, rep) -> None:
        self.btn_again.setEnabled(True)
        if isinstance(rep, Exception):
            rep = detect.WslReport(False, problem="error", message=f"Checking WSL failed: {rep}")
        self.report = rep
        self.distro.blockSignals(True)
        self.distro.clear()
        for d in rep.distros:
            self.distro.addItem(f"{d.name}  ({d.state.lower()}{', WSL default' if d.default else ''})", d.name)
        pick = detect.best_distro(rep, self.wiz.settings.wsl.default_distro)
        if pick:
            self.distro.setCurrentIndex(max(0, self.distro.findData(pick)))
        self.distro.blockSignals(False)
        ok = bool(rep.distros)
        self.distro.setEnabled(ok)
        self.btn_probe.setEnabled(ok)
        if not rep.ok:
            self.status.setText("<b>WSL is not available.</b> Install it as shown below (needs admin once and a "
                                "restart), then press Look again.")
        elif not ok:
            self.status.setText("<b>WSL works, but there is no Linux distro for OpenFOAM yet.</b>")
        else:
            self.status.setText(f"Found {len(rep.distros)} Linux distro(s). Pick the one with OpenFOAM and press "
                                "Look for OpenFOAM.")
        self._show_distro()
        self.completeChanged.emit()

    def _show_distro(self) -> None:
        rep = self.report
        if rep is None:
            return
        if not rep.distros:
            self.details.setPlainText(rep.message)
            return
        name = self.distro.currentData()
        of = self.openfoam.get(name)
        if of is None:
            cached = self.wiz.detector.cached_forks(name) if hasattr(self.wiz.detector, "cached_forks") else []
            self.details.setPlainText(
                (f"Last time AetherCFD found in {name}: " + ", ".join(f.label for f in cached) + "\n\n" if cached else "")
                + f"Press Look for OpenFOAM to check {name} now (it starts the distro if it is stopped).")
        elif of.ok:
            self.details.setPlainText(f"{name}: " + "\n".join(f"  {f.label}   ({f.bashrc})" for f in of.forks)
                                      + "\n\nReady: AetherCFD will run OpenFOAM here.")
        else:
            self.details.setPlainText(of.message)

    def probe(self) -> None:
        name = self.distro.currentData()
        if not name:
            return
        self.btn_probe.setEnabled(False)
        self.status.setText(f"Looking for OpenFOAM in {name} (starting it if needed)…")
        self.wiz.run_bg(lambda: self.wiz.detector.probe_openfoam(name), self._probe_done.emit)

    def _on_probe(self, rep) -> None:
        self.btn_probe.setEnabled(True)
        if isinstance(rep, Exception):
            rep = detect.OpenFoamReport(self.distro.currentData() or "?", [], f"Looking failed: {rep}")
        self.openfoam[rep.distro] = rep
        self.status.setText(f"{rep.distro}: " + (rep.message if rep.ok else "no OpenFOAM found yet (see below)."))
        self._show_distro()

    def chosen(self) -> str:
        return self.distro.currentData() or ""


class FoldersPage(QWizardPage):
    def __init__(self, wiz: SetupWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("Case folders")
        self.setSubTitle("Where your OpenFOAM cases live (the Start page lists cases found here).")
        lay = QVBoxLayout(self)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        rem = QPushButton("Remove")
        self.btn_create = QPushButton(f"Create {default_case_root()}")
        row.addWidget(add)
        row.addWidget(rem)
        row.addWidget(self.btn_create)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_dim("Tip: keep cases out of OneDrive-synced folders (file locking during runs) and avoid "
                           "spaces in paths: OpenFOAM tools cannot run in place on such paths (AetherCFD then runs "
                           "a staged copy)."))
        add.clicked.connect(self._add)
        rem.clicked.connect(lambda: [self.list.takeItem(self.list.row(i)) for i in self.list.selectedItems()])
        self.btn_create.clicked.connect(self._create)
        for r in wiz.settings.case_roots:
            self._append(r)
        self._update_create()

    def _append(self, path: str) -> None:
        if path and path.lower() not in (p.lower() for p in self.roots()):
            self.list.addItem(path + ("" if Path(path).is_dir() else "   (missing)"))

    def roots(self) -> list[str]:
        return [self.list.item(i).text().replace("   (missing)", "") for i in range(self.list.count())]

    def _add(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder with OpenFOAM cases", str(Path.home()))
        if d:
            self._append(str(Path(d)))

    def _create(self) -> None:
        root = default_case_root()
        root.mkdir(parents=True, exist_ok=True)
        items = self.list.findItems(str(root), Qt.MatchFlag.MatchStartsWith)
        for it in items:
            it.setText(str(root))
        if not items:
            self._append(str(root))
        self._update_create()

    def _update_create(self) -> None:
        self.btn_create.setVisible(not default_case_root().is_dir())


class ToolsPage(QWizardPage):
    _done = Signal(object)

    def __init__(self, wiz: SetupWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("Optional tools")
        self.setSubTitle("AetherCFD works without these; each one adds a feature.")
        self.paths: dict[str, str | None] = {}
        self.chosen: dict[str, str] = {}  # picked by hand: written to settings
        lay = QVBoxLayout(self)
        self.grid = QGridLayout()
        self.grid.setColumnStretch(1, 1)
        lay.addLayout(self.grid)
        lay.addStretch(1)
        self.rows: dict[str, tuple[QLabel, QLabel]] = {}
        self._done.connect(self._on_done)
        self._started = False

    def initializePage(self) -> None:
        if not self._started:
            self._started = True
            self.wiz.run_bg(lambda: self.wiz.detector.find_tools(self.wiz.settings), self._done.emit)

    def _on_done(self, reports) -> None:
        if isinstance(reports, Exception):
            reports = []
        for i, r in enumerate(reports):
            name = QLabel(f"<b>{r.label}</b>")
            where = QLabel(str(r.path) if r.path else "not found", wordWrap=True)
            where.setStyleSheet(f"color: {T.ok if r.path else T.warn};")
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, k=r.key, w=where: self._browse(k, w))
            self.grid.addWidget(name, 2 * i, 0)
            self.grid.addWidget(where, 2 * i, 1)
            self.grid.addWidget(browse, 2 * i, 2)
            self.grid.addWidget(_dim(r.hint), 2 * i + 1, 0, 1, 3)
            self.paths[r.key] = str(r.path) if r.path else None

    def _browse(self, key: str, label: QLabel) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Choose the program", "C:/Program Files", "Programs (*.exe)")
        if f:
            self.chosen[key] = self.paths[key] = str(Path(f))
            label.setText(str(Path(f)))
            label.setStyleSheet(f"color: {T.ok};")


class AiPage(QWizardPage):
    _done = Signal(object)

    def __init__(self, wiz: SetupWizard):
        super().__init__()
        self.wiz = wiz
        self.setTitle("The copilot's AI")
        self.setSubTitle("The copilot runs through an AI command-line tool you sign in to yourself.")
        self.reports: list = []
        lay = QVBoxLayout(self)
        self.box = QWidget()
        self.grid = QGridLayout(self.box)
        self.grid.setColumnStretch(1, 1)
        lay.addWidget(self.box)
        self.group = QButtonGroup(self)
        row = QHBoxLayout()
        self.btn_again = QPushButton("Check again")
        row.addWidget(self.btn_again)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_dim("Claude Code: a Claude subscription or Anthropic API account (claude.com/code). OpenAI "
                           "Codex: a ChatGPT plan or OpenAI API account (npm install -g @openai/codex). Either way "
                           "the copilot can only read your case and propose changes and runs, which you approve one "
                           "by one. The copilot is optional."))
        lay.addStretch(1)
        self.btn_again.clicked.connect(self.refresh)
        self._done.connect(self._on_done)
        self._started = False

    def initializePage(self) -> None:
        if not self._started:
            self._started = True
            self.refresh()

    def refresh(self) -> None:
        self.btn_again.setEnabled(False)
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.grid.addWidget(QLabel("Looking for Claude Code and Codex…"), 0, 0)
        self.wiz.run_bg(lambda: self.wiz.detector.check_ai(self.wiz.settings), self._done.emit)

    def _on_done(self, reports) -> None:
        self.btn_again.setEnabled(True)
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if isinstance(reports, Exception):
            self.grid.addWidget(QLabel(f"Checking failed: {reports}", wordWrap=True), 0, 0)
            return
        self.reports = reports
        self.group = QButtonGroup(self)
        current = self.wiz.settings.copilot_provider
        for i, r in enumerate(reports):
            radio = QRadioButton(r.label)
            radio.setProperty("provider", r.provider)
            radio.setChecked(r.provider == current)
            self.group.addButton(radio, i)
            if not r.found:
                state, color = "not installed", T.warn
            elif r.signed_in:
                state, color = f"{r.version} · signed in ({r.detail})", T.ok
            else:
                state, color = f"{r.version} · not signed in", T.warn
            lab = QLabel(state, wordWrap=True)
            lab.setStyleSheet(f"color: {color};")
            self.grid.addWidget(radio, 2 * i, 0)
            self.grid.addWidget(lab, 2 * i, 1)
            if r.found and not r.signed_in and r.cli is not None:
                b = QPushButton(f"Sign in to {r.label}")
                b.clicked.connect(lambda _=False, rep=r: self.sign_in(rep))
                self.grid.addWidget(b, 2 * i, 2)
            if r.hint:
                self.grid.addWidget(_dim(r.hint), 2 * i + 1, 0, 1, 3)

    def sign_in(self, rep) -> None:
        from aethercfd.copilot.providers import get_provider

        argv = get_provider(rep.provider).login_argv(rep.cli)
        self.wiz.launch(argv)
        self.wiz.notify(f"Complete the {rep.label} sign-in in the window that opened (your browser), then press "
                        "Check again")

    def provider(self) -> str:
        b = self.group.checkedButton()
        return b.property("provider") if b is not None else self.wiz.settings.copilot_provider


# ======================================================================================================== wizard
class _Detector:
    """The real checks (tests pass a stand-in with the same methods)."""

    check_wsl = staticmethod(detect.check_wsl)
    probe_openfoam = staticmethod(detect.probe_openfoam)
    find_tools = staticmethod(detect.find_tools)
    check_ai = staticmethod(detect.check_ai)

    @staticmethod
    def cached_forks(distro: str) -> list:
        import json

        from aethercfd.backend.wsl.forks import ForkProfile
        from aethercfd.core.settings import cache_dir

        f = cache_dir() / "forks" / f"{distro}.json"
        try:
            return [ForkProfile.from_json(d) for d in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []
        except (OSError, ValueError, TypeError, KeyError):
            return []


class SetupWizard(QWizard):
    def __init__(self, services, parent=None, *, detector=None, launcher=None):
        super().__init__(parent)
        self.sv = services
        self.settings = services.settings
        self.detector = detector or _Detector()
        self._launcher = launcher
        self.setWindowTitle("AetherCFD Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.resize(760, 560)
        self.p_intro = IntroPage()
        self.p_openfoam = OpenFoamPage(self)
        self.p_folders = FoldersPage(self)
        self.p_tools = ToolsPage(self)
        self.p_ai = AiPage(self)
        for p in (self.p_intro, self.p_openfoam, self.p_folders, self.p_tools, self.p_ai):
            self.addPage(p)

    # helpers the pages use (overridable in tests)
    def run_bg(self, fn, on_result) -> None:
        """fn() off the GUI thread; on_result(result or the exception) back on it."""
        self.sv.run_bg(fn, lambda res: on_result(res[1] if res[1] is not None else res[0]))

    def notify(self, msg: str) -> None:
        if hasattr(self.sv, "notify"):
            self.sv.notify(msg)

    def launch(self, argv: list[str]) -> None:
        if self._launcher is not None:
            self._launcher(argv)
            return
        subprocess.Popen(argv, creationflags=CREATE_NEW_CONSOLE)  # the CLI's own login: the user sees its prompts

    def apply(self) -> None:
        """Write what was chosen into the settings (Finish)."""
        s = self.settings
        distro = self.p_openfoam.chosen()
        changed_distro = bool(distro) and distro != s.wsl.default_distro
        if distro:
            detect.use_distro(s, distro)
        roots = self.p_folders.roots()
        if roots:
            s.case_roots = roots
        keys = {"paraview": "paraview_exe", "freecad": "freecad_cmd", "ml_python": "ml_python"}
        for k, path in self.p_tools.chosen.items():
            setattr(s, keys[k], path)
        s.copilot_provider = self.p_ai.provider()
        s.setup_done = True
        s.save()
        if changed_distro and getattr(self.sv, "session", None) is not None:
            self.notify(f"OpenFOAM will run in {distro} for cases you open from now on (reopen the current case)")

    def accept(self) -> None:
        self.apply()
        super().accept()
