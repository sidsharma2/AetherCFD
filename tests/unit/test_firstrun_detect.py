"""First-run checks on a new PC: WSL/distros, OpenFOAM, tools, AI providers -- all faked, nothing booted."""

from __future__ import annotations

from pathlib import Path

import pytest

from aethercfd.backend.wsl.distro import DistroInfo
from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.backend.wsl.runner import DistroNotAllowed, WslRunner
from aethercfd.copilot.provider import AuthStatus, CliInfo
from aethercfd.core.settings import Settings
from aethercfd.firstrun import detect

UBU = DistroInfo("Ubuntu-22.04", "Stopped", 2, True)
ESI = ForkProfile("esi-v2306", Family.ESI, "v2306", "Ubuntu-22.04", "/usr/lib/openfoam/openfoam2306/etc/bashrc",
                  "/usr/lib/openfoam/openfoam2306", False)


def _raise(exc):
    def f(*a, **kw):
        raise exc
    return f


def test_no_wsl_explains_how_to_install_it():
    r = detect.check_wsl(_raise(FileNotFoundError("wsl.exe")))
    assert not r.ok and r.problem == "no-wsl" and "wsl --install" in r.message and not r.distros
    r = detect.check_wsl(_raise(OSError("The Windows Subsystem for Linux is not enabled")))
    assert r.problem == "no-wsl" and "not enabled" in r.message


def test_only_docker_and_agora_count_as_no_distro():
    r = detect.check_wsl(lambda: [DistroInfo("docker-desktop", "Running", 2, False),
                                  DistroInfo("Agora", "Stopped", 2, False)])
    assert r.ok and r.problem == "no-distro" and "wsl --install -d Ubuntu-22.04" in r.message
    assert "docker-desktop" in r.message and not r.distros


def test_usable_distros_and_the_best_pick():
    rows = [DistroInfo("Debian", "Stopped", 2, False), UBU, DistroInfo("Legacy", "Stopped", 1, False)]
    r = detect.check_wsl(lambda: rows)
    assert r.names == ["Debian", "Ubuntu-22.04"] and [d.name for d in r.other] == ["Legacy"]  # WSL 1 is not used
    assert detect.best_distro(r) == "Ubuntu-22.04"  # the WSL default
    assert detect.best_distro(r, "Debian") == "Debian"  # what the settings already use, if installed
    assert detect.best_distro(r, "Ubuntu-OpenFOAM") == "Ubuntu-22.04"  # another PC's distro name is ignored


def test_openfoam_probe_explains_both_supported_versions():
    r = detect.probe_openfoam("Ubuntu-22.04", lambda d: [])
    assert not r.ok and "openfoam2306-default" in r.message and "openfoam14" in r.message
    assert "Ubuntu-22.04" in r.message
    r = detect.probe_openfoam("Ubuntu-22.04", lambda d: [ESI])
    assert r.ok and "ESI v2306" in r.message
    r = detect.probe_openfoam("Ubuntu-22.04", _raise(RuntimeError("timed out")))
    assert not r.ok and "timed out" in r.message


def test_use_distro_allows_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path))
    s = Settings()
    assert s.wsl.default_distro == "" and s.wsl.allowed_distros == []  # nothing machine-specific by default
    detect.use_distro(s, "Ubuntu-22.04")
    detect.use_distro(s, "Ubuntu-22.04")
    assert s.wsl.default_distro == "Ubuntu-22.04" and s.wsl.allowed_distros == ["Ubuntu-22.04"]


def test_unset_distro_gives_a_clear_message():
    with pytest.raises(DistroNotAllowed, match="Setup Wizard"):
        WslRunner("", allowed=[])


def test_needs_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path))
    s = Settings()
    assert detect.needs_setup(s, settings_existed=False)
    s.wsl.default_distro = "Ubuntu-OpenFOAM"
    assert not detect.needs_setup(s, settings_existed=True)  # an existing, configured install: not shown by itself
    s.wsl.default_distro = ""
    assert detect.needs_setup(s, settings_existed=True)
    s.setup_done = True
    assert not detect.needs_setup(s, settings_existed=False)


def test_find_paraview_newest(tmp_path, monkeypatch):
    for v in ("ParaView 5.11.2", "ParaView 6.1.0", "ParaView 6.0.1"):
        (tmp_path / v / "bin").mkdir(parents=True)
        (tmp_path / v / "bin" / "paraview.exe").write_bytes(b"")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("PARAVIEW_EXE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nothing"))
    assert detect.find_paraview() == tmp_path / "ParaView 6.1.0" / "bin" / "paraview.exe"
    mine = tmp_path / "ParaView 5.11.2" / "bin" / "paraview.exe"
    assert detect.find_paraview(str(mine)) == mine
    assert detect.find_paraview(str(tmp_path / "missing.exe")) == tmp_path / "ParaView 6.1.0" / "bin" / "paraview.exe"


def test_check_ai_reports_each_provider():
    class P:
        def __init__(self, pid, cli, auth):
            self.id, self.label, self._cli, self._auth = pid, pid.title(), cli, auth
            self.install_hint, self.login_hint, self.exe_setting = f"install {pid}", f"{pid} login", "x"

        def configured_exe(self, settings):
            return None

        def find(self, configured=None):
            return self._cli

        def auth_status(self, cli, timeout=30):
            return self._auth

    cli = CliInfo(Path("x.exe"), "1.0", "t")
    reps = detect.check_ai(None, {"a": P("a", None, None), "b": P("b", cli, AuthStatus(False, "none")),
                                  "c": P("c", cli, AuthStatus(True, "chatgpt", "ChatGPT")),
                                  "d": P("d", cli, AuthStatus(False, "config-error", "cannot read config.toml"))})
    a, b, c, d = reps
    assert not a.found and a.hint == "install a"
    assert b.found and not b.signed_in and "b login" in b.hint and b.cli is cli
    assert c.signed_in and c.hint == "" and c.detail == "ChatGPT"
    assert d.hint == "cannot read config.toml"


def test_settings_defaults_have_no_machine_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path))
    s = Settings()
    dumped = str(s.model_dump())
    for bad in ("D:\\", "/mnt/", "ParaView 6", "Research-Software"):
        assert bad not in dumped
    assert s.copilot_provider == "claude" and s.copilot_model is None and s.paraview_exe is None
    assert s.case_roots == [str(Path.home() / "OpenFOAM")]
    s.save()  # None values are left out of the TOML and load back
    assert Settings.load().paraview_exe is None
