"""`aethercfd ml-setup`: the PyTorch environment setup reachable from the CLI (and so from the packaged app,
which has no scripts/ folder), and the hints that name the right command for how the app was started."""

from __future__ import annotations

import contextlib
import importlib
import sys

from aethercfd.ml import envsetup
from aethercfd.scripting import cli


def test_cli_ml_setup_dry_run_prints_the_commands_and_installs_nothing(tmp_path, capsys):
    code = cli.main(["ml-setup", "--dir", str(tmp_path / "env"), "--cpu", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "torch" in out and str(tmp_path / "env") in out
    assert not (tmp_path / "env").exists()


def test_cli_help_lists_ml_setup(capsys):
    with contextlib.suppress(SystemExit):
        cli.main(["--help"])
    assert "ml-setup" in capsys.readouterr().out


def test_packaged_app_without_uv_or_python_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    said, ran = [], []
    code = envsetup.main(["--dir", str(tmp_path / "env"), "--cpu"], run=lambda c, **k: ran.append(c),
                         which=lambda name: None, out=said.append)
    assert code == 1 and ran == []  # the exe itself is never used as "python -m venv"
    assert "winget install --id astral-sh.uv" in said[-1]


def test_packaged_app_without_uv_uses_a_python_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    said = []
    code = envsetup.main(["--dir", str(tmp_path / "env"), "--cpu", "--dry-run"],
                         which=lambda name: r"C:\Python312\python.exe" if name == "python" else None, out=said.append)
    assert code == 0
    assert any(line.strip().startswith(r"C:\Python312\python.exe -m venv") for line in said)
    assert not any(sys.executable in line for line in said)


def test_setup_hint_names_the_command_for_source_and_for_the_packaged_app(monkeypatch):
    from aethercfd.ml import pinn

    assert "scripts/setup_ml_env.py" in pinn.SETUP_HINT and "docs/pinn.md" in pinn.SETUP_HINT
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        frozen = importlib.reload(pinn)
        assert "aethercfd-cli ml-setup" in frozen.SETUP_HINT and "scripts/setup_ml_env.py" not in frozen.SETUP_HINT
    finally:
        monkeypatch.undo()
        importlib.reload(pinn)


def test_first_run_wizard_hint_points_at_the_one_step_setup():
    from aethercfd.firstrun.detect import TOOL_HINTS

    assert "ml-setup" in TOOL_HINTS["ml_python"] and "setup_ml_env.py" in TOOL_HINTS["ml_python"]
