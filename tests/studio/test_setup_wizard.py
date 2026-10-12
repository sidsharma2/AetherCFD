"""First-run setup wizard on a fresh config: it explains what is missing (no WSL, no distro, no OpenFOAM, no AI
sign-in) instead of failing, never installs anything, and writes the choices only on Finish."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QPushButton, QWizard

from aethercfd.backend.wsl.distro import DistroInfo
from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.copilot.provider import CliInfo
from aethercfd.core.settings import Settings
from aethercfd.firstrun import detect
from aethercfd.studio.app.services import AppServices
from aethercfd.studio.dialogs.setup_wizard import SetupWizard

ESI = ForkProfile("esi-v2306", Family.ESI, "v2306", "Ubuntu-22.04", "/usr/lib/openfoam/openfoam2306/etc/bashrc",
                  "/usr/lib/openfoam/openfoam2306", False)


class FakeDetector:
    def __init__(self, distros=None, wsl_missing=False, forks=None, ai=None):
        self.distros, self.wsl_missing, self.forks, self.ai = distros or [], wsl_missing, forks or {}, ai
        self.probed: list[str] = []

    def check_wsl(self):
        def lister():
            if self.wsl_missing:
                raise FileNotFoundError("wsl.exe")
            return self.distros
        return detect.check_wsl(lister)

    def probe_openfoam(self, distro):
        self.probed.append(distro)
        return detect.probe_openfoam(distro, lambda d: self.forks.get(d, []))

    def find_tools(self, settings):
        return [detect.ToolReport("paraview", "ParaView", None, detect.TOOL_HINTS["paraview"]),
                detect.ToolReport("freecad", "FreeCAD", Path("C:/FreeCAD/bin/FreeCADCmd.exe"), "x"),
                detect.ToolReport("ml_python", "Python with PyTorch (PINNs)", None, detect.TOOL_HINTS["ml_python"])]

    def check_ai(self, settings):
        return self.ai if self.ai is not None else [
            detect.AiReport("claude", "Claude", False, hint="Claude Code was not found. Install it"),
            detect.AiReport("codex", "Codex", True, "0.159.2", False, "none", "Not signed in: press Sign in",
                            CliInfo(Path("codex.exe"), "0.159.2", "t"))]

    def cached_forks(self, distro):
        return []


@pytest.fixture
def make(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))  # an empty config: a fresh install
    monkeypatch.setattr("aethercfd.studio.dialogs.setup_wizard.default_case_root", lambda: tmp_path / "home_OpenFOAM")
    made, wizards = [], []

    def build(det: FakeDetector, launched: list | None = None) -> SetupWizard:
        sv = AppServices()
        w = SetupWizard(sv, detector=det, launcher=(launched.append if launched is not None else None))
        made.append(sv)
        wizards.append(w)
        return w

    yield build
    for sv in made:
        sv.pool.waitForDone(10000)
    for w in wizards:  # as the app does: a closed wizard left alive crashes a later app-wide restyle
        w.close()
        w.deleteLater()
    qtbot.wait(50)


def _go_to(qtbot, w: SetupWizard, page) -> None:
    if w.currentId() < 0:
        w.restart()  # pages initialise as in exec(), without showing a window
    while w.currentPage() is not page:
        w.next()
    qtbot.wait(50)


def test_no_wsl_is_explained_not_a_crash(make, qtbot):
    assert not Settings.path().exists()
    w = make(FakeDetector(wsl_missing=True))
    _go_to(qtbot, w, w.p_openfoam)
    qtbot.waitUntil(lambda: w.p_openfoam.report is not None, timeout=10000)
    p = w.p_openfoam
    assert "WSL is not available" in p.status.text()
    assert "wsl --install" in p.details.toPlainText() and "administrator" in p.details.toPlainText()
    assert not p.btn_probe.isEnabled() and not p.distro.isEnabled()
    # the rest of the wizard still works, and Finish saves without a distro
    _go_to(qtbot, w, w.p_ai)
    qtbot.waitUntil(lambda: bool(w.p_ai.reports), timeout=10000)
    w.accept()
    st = Settings.load()
    assert st.setup_done and st.wsl.default_distro == "" and st.wsl.allowed_distros == []


def test_no_distro_then_no_openfoam(make, qtbot):
    w = make(FakeDetector(distros=[DistroInfo("docker-desktop", "Running", 2, False)]))
    _go_to(qtbot, w, w.p_openfoam)
    qtbot.waitUntil(lambda: w.p_openfoam.report is not None, timeout=10000)
    assert "wsl --install -d Ubuntu-22.04" in w.p_openfoam.details.toPlainText()

    det = FakeDetector(distros=[DistroInfo("Ubuntu-22.04", "Stopped", 2, True)])
    w2 = make(det)
    _go_to(qtbot, w2, w2.p_openfoam)
    p = w2.p_openfoam
    qtbot.waitUntil(lambda: p.report is not None, timeout=10000)
    assert p.chosen() == "Ubuntu-22.04" and p.btn_probe.isEnabled()
    assert det.probed == []  # nothing is booted until asked
    p.btn_probe.click()
    qtbot.waitUntil(lambda: "Ubuntu-22.04" in p.openfoam, timeout=10000)
    text = p.details.toPlainText()
    assert "openfoam2306-default" in text and "openfoam14" in text


def test_openfoam_found_and_finish_writes_the_choices(make, qtbot, tmp_path):
    det = FakeDetector(distros=[DistroInfo("Debian", "Stopped", 2, False),
                                DistroInfo("Ubuntu-22.04", "Running", 2, True)], forks={"Ubuntu-22.04": [ESI]})
    launched: list = []
    w = make(det, launched)
    _go_to(qtbot, w, w.p_openfoam)
    p = w.p_openfoam
    qtbot.waitUntil(lambda: p.report is not None, timeout=10000)
    p.probe()
    qtbot.waitUntil(lambda: "Ubuntu-22.04" in p.openfoam, timeout=10000)
    assert "ESI v2306" in p.details.toPlainText() and "Ready" in p.details.toPlainText()
    # case folders: create the default one
    _go_to(qtbot, w, w.p_folders)
    w.p_folders.btn_create.click()
    assert (tmp_path / "home_OpenFOAM").is_dir() and str(tmp_path / "home_OpenFOAM") in w.p_folders.roots()
    # tools: found / not found, with hints
    _go_to(qtbot, w, w.p_tools)
    qtbot.waitUntil(lambda: bool(w.p_tools.paths), timeout=10000)
    assert w.p_tools.paths["paraview"] is None and w.p_tools.paths["freecad"].endswith("FreeCADCmd.exe")
    # AI: Claude missing, Codex installed but signed out -> its own sign-in, via the CLI's login
    _go_to(qtbot, w, w.p_ai)
    qtbot.waitUntil(lambda: bool(w.p_ai.reports), timeout=10000)
    buttons = [b for b in w.p_ai.box.findChildren(QPushButton) if b.text().startswith("Sign in")]
    assert [b.text() for b in buttons] == ["Sign in to Codex"]
    buttons[0].click()
    assert launched == [["codex.exe", "login"]]
    radio = next(b for b in w.p_ai.group.buttons() if b.property("provider") == "codex")
    radio.setChecked(True)
    assert w.button(QWizard.WizardButton.FinishButton) is not None
    w.accept()
    st = Settings.load()
    assert st.setup_done and st.wsl.default_distro == "Ubuntu-22.04" and st.wsl.allowed_distros == ["Ubuntu-22.04"]
    assert st.copilot_provider == "codex" and str(tmp_path / "home_OpenFOAM") in st.case_roots
    assert st.paraview_exe is None  # only paths picked by hand are written; found ones stay automatic


def test_cancel_writes_nothing(make, qtbot):
    w = make(FakeDetector(wsl_missing=True))
    _go_to(qtbot, w, w.p_openfoam)
    w.reject()
    assert not Settings.path().exists()
