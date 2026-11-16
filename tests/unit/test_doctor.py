"""`aethercfd doctor`: every check reports ok / warn / fail with a fix, and each failure can be faked."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aethercfd.backend.wsl.distro import DistroInfo
from aethercfd.copilot.provider import AuthStatus, CliInfo
from aethercfd.core.runtime import XTERM_DIR
from aethercfd.core.settings import Settings
from aethercfd.scripting import cli
from aethercfd.scripting import doctor as d


@pytest.fixture(autouse=True)
def _own_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AETHERCFD_CACHE_DIR", str(tmp_path / "cache"))


def _status(checks, name):
    return next(c for c in checks if c.name == name)


# ------------------------------------------------------------------------------------------ app, packages
def test_missing_and_broken_packages_fail_with_uv_sync_fix():
    checks = d.check_packages([("numpy", "numpy"), ("no_such_module_xyz", "numpy"), ("x", "no-such-dist-xyz")])
    assert [c.status for c in checks] == [d.OK, d.FAIL, d.FAIL]
    assert "not installed" in checks[2].detail
    assert all("uv sync" in c.fix for c in checks[1:])


def test_qt_or_vtk_import_failure(monkeypatch):
    real = d.importlib.import_module

    def fake(name):
        if name == "PySide6.QtWebEngineWidgets":
            raise ImportError("DLL load failed")
        return real(name)

    monkeypatch.setattr(d.importlib, "import_module", fake)
    checks = d.check_qt_vtk()
    web = _status(checks, "Qt web view (terminal)")
    assert web.status == d.FAIL and "DLL load failed" in web.detail
    assert _status(checks, "Qt widgets").status == d.OK


def test_packaged_app_without_gui_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "is_frozen", lambda: True)
    checks = d.check_app(gui_exe=tmp_path / "AetherCFD.exe")
    assert _status(checks, "GUI executable").status == d.FAIL
    (tmp_path / "AetherCFD.exe").write_bytes(b"MZ")
    assert _status(d.check_app(gui_exe=tmp_path / "AetherCFD.exe"), "GUI executable").status == d.OK


# ------------------------------------------------------------------------------------------ terminal
def test_terminal_assets_ok_and_missing(tmp_path):
    assert d.check_terminal_assets()[0].status == d.OK
    copy = tmp_path / "xterm"
    shutil.copytree(XTERM_DIR, copy, ignore=shutil.ignore_patterns("node_modules"))
    (copy / "dist" / "xterm.js").unlink()
    (copy / "dist" / "addon-fit.js").unlink()
    c = d.check_terminal_assets(copy)[0]
    assert c.status == d.FAIL and "xterm.js" in c.detail and "+1 more" in c.detail
    assert "git checkout" in c.fix


# ------------------------------------------------------------------------------------------ GPU
def test_gpu_ok():
    info = {"name": "RTX X", "memory_gb": 8.0, "runtime": 12090}
    c = d.check_gpu(Settings(), probe=lambda: {"ok": True, "info": info})[0]
    assert c.status == d.OK and "RTX X" in c.detail and "12.9" in c.detail


@pytest.mark.parametrize("driver, fix", [(True, "driver"), (False, "optional")])
def test_gpu_missing_is_a_warning(monkeypatch, driver, fix):
    monkeypatch.setattr(d.shutil, "which", lambda exe: "nvidia-smi.exe" if driver else None)
    c = d.check_gpu(Settings(), probe=lambda: {"ok": False, "error": "CUDA sees no GPU"})[0]
    assert c.status == d.WARN and "CUDA sees no GPU" in c.detail and fix in c.fix


def test_gpu_switched_off_by_environment(monkeypatch):
    monkeypatch.setenv("AETHERCFD_NO_CUDA", "1")
    assert "AETHERCFD_NO_CUDA" in d.gpu_probe()["error"]


def test_frozen_gpu_probe_without_gui(tmp_path):
    r = d.frozen_gpu_probe(tmp_path / "AetherCFD.exe")
    assert r["ok"] is False and "failed" in r["error"]


def test_gpu_json_entry_point(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "gpu_probe", lambda: {"ok": False, "error": "x"})
    assert d._gpu_json_main(["--gpu-json", str(tmp_path / "g.json")]) == 0
    assert json.loads((tmp_path / "g.json").read_text(encoding="utf-8")) == {"ok": False, "error": "x"}


# ------------------------------------------------------------------------------------------ WSL
def _settings(*allowed):
    s = Settings()
    s.wsl.allowed_distros = list(allowed)
    return s


def _distros(*rows):
    return lambda: [DistroInfo(n, st, 2, i == 0) for i, (n, st) in enumerate(rows)]


def test_no_wsl():
    def missing():
        raise FileNotFoundError("wsl.exe")

    c = d.check_wsl(_settings(), list_fn=missing)[0]
    assert c.status == d.FAIL and "wsl --install" in c.fix


def test_wsl_without_distro():
    checks = d.check_wsl(_settings(), list_fn=_distros())
    assert _status(checks, "Linux distro").status == d.FAIL
    assert "Ubuntu-22.04" in _status(checks, "Linux distro").fix
    only_docker = d.check_wsl(_settings(), list_fn=_distros(("docker-desktop", "Running")))
    assert _status(only_docker, "Linux distro").status == d.FAIL


def test_no_distro_chosen_yet():
    c = _status(d.check_wsl(_settings(), list_fn=_distros(("Ubuntu", "Stopped"))), "distro for AetherCFD")
    assert c.status == d.FAIL and "Setup Wizard" in c.fix


def test_chosen_distro_not_installed():
    c = _status(d.check_wsl(_settings("Gone"), list_fn=_distros(("Ubuntu", "Stopped"))), "Gone")
    assert c.status == d.FAIL and "not installed" in c.detail


def test_stopped_distro_uses_the_last_probe(tmp_path):
    lf = _distros(("Ubuntu", "Stopped"))
    c = _status(d.check_wsl(_settings("Ubuntu"), list_fn=lf), "Ubuntu")
    assert c.status == d.WARN and "--probe" in c.fix
    cache = tmp_path / "cache" / "forks"
    cache.mkdir(parents=True)
    (cache / "Ubuntu.json").write_text(json.dumps([{"id": "esi-v2306", "label": "OpenFOAM v2306"}]),
                                       encoding="utf-8")
    c = _status(d.check_wsl(_settings("Ubuntu"), list_fn=lf), "Ubuntu")
    assert c.status == d.OK and "OpenFOAM v2306" in c.detail


def test_running_distro_without_openfoam():
    c = _status(d.check_wsl(_settings("Ubuntu"), list_fn=_distros(("Ubuntu", "Running")), probe_fn=lambda n: []),
                "Ubuntu")
    assert c.status == d.FAIL and "INSTALL.md" in c.fix


def test_probe_finds_openfoam():
    class Fork:
        label = "OpenFOAM-14"

    c = _status(d.check_wsl(_settings("Ubuntu"), probe=True, list_fn=_distros(("Ubuntu", "Stopped")),
                            probe_fn=lambda n: [Fork()]), "Ubuntu")
    assert c.status == d.OK and "OpenFOAM-14" in c.detail


# ------------------------------------------------------------------------------------------ optional tools
@pytest.fixture
def no_tools(monkeypatch):
    import aethercfd.firstrun.detect as detect
    import aethercfd.mesh.cad as cad
    import aethercfd.ml.pinn as pinn

    monkeypatch.setattr(detect, "find_paraview", lambda s=None: None)
    monkeypatch.setattr(cad, "find_freecad", lambda s=None: None)
    monkeypatch.setattr(pinn, "find_ml_python", lambda s=None: None)
    return pinn


def test_missing_optional_tools_are_warnings(no_tools):
    checks = d.check_tools(Settings())
    assert {c.status for c in checks} == {d.WARN}
    assert "paraview.org" in _status(checks, "ParaView").fix
    assert "freecad.org" in _status(checks, "FreeCAD").fix
    assert "scripts/setup_ml_env.py" in _status(checks, "PyTorch environment (PINNs)").fix


def test_ml_env_without_torch(no_tools, monkeypatch, tmp_path):
    monkeypatch.setattr(no_tools, "find_ml_python", lambda s=None: tmp_path / "python.exe")
    broken = d.check_tools(Settings(), ml_status_fn=lambda s: {"available": False, "note": "No module named 'torch'"})
    c = _status(broken, "PyTorch environment (PINNs)")
    assert c.status == d.WARN and "torch" in c.detail
    good = d.check_tools(Settings(), ml_status_fn=lambda s: {"available": True, "torch": "2.9", "cuda": False})
    assert _status(good, "PyTorch environment (PINNs)").status == d.OK
    quick = d.check_tools(Settings(), quick=True, ml_status_fn=lambda s: pytest.fail("torch import in --quick"))
    assert "--quick" in _status(quick, "PyTorch environment (PINNs)").detail


# ------------------------------------------------------------------------------------------ copilot
class FakeProvider:
    install_hint = "Install it with `npm install -g fake`.\nSecond line."
    login_hint = "fake login"

    def __init__(self, pid, cli=None, auth=None):
        self.id = self.label = pid
        self._cli, self._auth = cli, auth

    def configured_exe(self, settings):
        return None

    def find(self, configured=None):
        return self._cli

    def auth_status(self, cli, timeout=30):
        return self._auth


def test_copilot_cli_missing_and_signed_out():
    cli_info = CliInfo(Path("fake.exe"), "1.0", "path")
    providers = {"a": FakeProvider("a"), "b": FakeProvider("b", cli_info, AuthStatus(False))}
    checks = d.check_copilot(Settings(), providers=providers)
    assert _status(checks, "a").status == d.WARN and _status(checks, "a").fix == "Install it with `npm install -g fake`."
    assert _status(checks, "b").status == d.WARN and "not signed in" in _status(checks, "b").detail
    assert _status(checks, "copilot").status == d.WARN


def test_copilot_signed_in_hides_the_account():
    cli_info = CliInfo(Path("fake.exe"), "1.0", "path")
    providers = {"a": FakeProvider("a", cli_info, AuthStatus(True, "subscription", "someone@example.com"))}
    checks = d.check_copilot(Settings(), providers=providers)
    assert checks[0].status == d.OK and "@" not in checks[0].detail
    assert len(checks) == 1  # no "no copilot ready" line


# ------------------------------------------------------------------------------------------ folders
def test_folders_ok_then_first_start(tmp_path):
    s = Settings()
    s.case_roots = [str(tmp_path)]
    checks = d.check_folders(s, None)
    assert _status(checks, "settings folder").status == d.OK
    assert _status(checks, "cache folder").status == d.OK
    assert _status(checks, "settings.toml").status == d.WARN  # not written yet


def test_unwritable_folder_and_broken_settings(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    s = Settings()
    s.case_roots = [str(tmp_path / "no-such-cases")]
    checks = d.check_folders(s, "TOMLDecodeError: bad line 3", config=blocker / "config", cache=tmp_path / "c")
    c = _status(checks, "settings folder")
    assert c.status == d.FAIL and "AETHERCFD_CONFIG_DIR" in c.fix
    assert _status(checks, "settings.toml").status == d.FAIL
    assert _status(checks, "case folders").status == d.WARN


def test_unreadable_settings_file_is_reported(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.toml").write_text("this is = = not toml", encoding="utf-8")
    settings, err = d.load_settings()
    assert isinstance(settings, Settings) and err


# ------------------------------------------------------------------------------------------ the whole run
def test_a_crashing_check_becomes_a_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug in a check")

    for name in ("check_packages", "check_qt_vtk", "check_gpu", "check_tools", "check_copilot"):
        monkeypatch.setattr(d, name, lambda *a, **k: [])
    monkeypatch.setattr(d, "check_terminal_assets", boom)
    checks = d.run_checks(backend=False, quick=True)
    c = _status(checks, "doctor check")
    assert c.section == "Terminal" and c.status == d.FAIL and "bug in a check" in c.detail


def test_exit_code_and_report(monkeypatch, capsys):
    monkeypatch.setattr(d, "run_checks", lambda **k: [d.Check("A", "x", d.OK, "fine"),
                                                      d.Check("B", "y", d.WARN, "meh", "do this")])
    assert d.doctor() == 0
    out = capsys.readouterr().out
    assert "[warn] y" in out and "fix: do this" in out and "0 problem(s): all required parts work" in out
    monkeypatch.setattr(d, "run_checks", lambda **k: [d.Check("A", "x", d.FAIL, "broken", "fix it")])
    assert d.doctor(as_json=True) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["checks"][0] == {"section": "A", "name": "x", "status": "fail", "detail": "broken", "fix": "fix it"}


def test_cli_flags_reach_the_doctor(monkeypatch):
    seen = {}
    monkeypatch.setattr(d, "doctor", lambda **k: seen.update(k) or 0)
    assert cli.main(["doctor", "--no-backend", "--quick", "--json"]) == 0
    assert seen == {"backend": False, "probe": False, "quick": True, "as_json": True}
