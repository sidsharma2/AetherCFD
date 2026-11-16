"""packaging/build.py without running PyInstaller: preflight, restartable steps, component and size checks."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

import aethercfd
from aethercfd.core.runtime import XTERM_DIR

ROOT = Path(__file__).resolve().parents[2]
GB = 2**30


@pytest.fixture
def build():
    spec = importlib.util.spec_from_file_location("aether_packaging_build_t", ROOT / "packaging" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ------------------------------------------------------------------------------------------- version
def test_version_has_one_source(build):
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in pyproject["project"]
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "src/aethercfd/__init__.py"
    assert build.read_version() == aethercfd.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?", aethercfd.__version__)


@pytest.mark.parametrize("ver, nums", [("0.1.0.dev0", (0, 1, 0, 0)), ("1.2.3rc1", (1, 2, 3, 0)),
                                       ("2.0", (2, 0, 0, 0)), ("10.20.30", (10, 20, 30, 0))])
def test_windows_file_version(build, ver, nums):
    assert build.win_version_tuple(ver) == nums


def test_exe_version_resource(build):
    text = str(build.exe_version_info("1.2.3rc1", "AetherCFD", "AetherCFD Studio"))
    assert "filevers=(1, 2, 3, 0)" in text
    assert "'ProductVersion', '1.2.3rc1'" in text
    assert "'OriginalFilename', 'AetherCFD.exe'" in text


# ------------------------------------------------------------------------------------------- preflight
def test_low_commit_memory_stops_the_build(build):
    msg = build.memory_problem(12, lambda: (5 * GB, 40 * GB))
    assert msg and "5.0 GB" in msg and "40.0 GB" in msg and "--min-commit-gb 0" in msg
    assert build.memory_problem(12, lambda: (20 * GB, 40 * GB)) is None
    assert build.memory_problem(0, lambda: (1, 1)) is None  # check turned off

    def broken():
        raise OSError("no kernel32")

    assert build.memory_problem(12, broken) is None  # cannot measure: do not block


def test_preflight_reports_missing_terminal_assets_and_spec_inputs(build, tmp_path):
    xterm = tmp_path / "src" / "aethercfd" / "studio" / "resources" / "xterm"
    shutil.copytree(XTERM_DIR, xterm, ignore=shutil.ignore_patterns("node_modules"))
    for rel in build.SPEC_INPUTS:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).touch()
    assert build.preflight_problems(cpu_only=True, root=tmp_path) == []
    (xterm / "dist" / "xterm.js").unlink()
    (tmp_path / "src" / "aethercfd" / "ml" / "pinn_worker.py").unlink()
    problems = build.preflight_problems(cpu_only=True, root=tmp_path)
    assert any("xterm.js" in p and "git checkout" in p for p in problems)
    assert any("pinn_worker.py" in p for p in problems)


def test_preflight_on_this_checkout(build):
    assert build.preflight_problems(cpu_only=True) == []


# ------------------------------------------------------------------------------------------- restartable
def test_fingerprint_follows_inputs(build, tmp_path):
    src = tmp_path / "src" / "aethercfd"
    src.mkdir(parents=True)
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock 1\n", encoding="utf-8")
    fp = build.fingerprint(False, tmp_path)
    assert fp == build.fingerprint(False, tmp_path)
    assert fp != build.fingerprint(True, tmp_path)
    (tmp_path / "uv.lock").write_text("lock 2\n", encoding="utf-8")
    fp2 = build.fingerprint(False, tmp_path)
    assert fp2 != fp
    (src / "a.py").write_text("x = 22\n", encoding="utf-8")
    assert build.fingerprint(False, tmp_path) != fp2
    (src / "__pycache__").mkdir()
    fp3 = build.fingerprint(False, tmp_path)
    (src / "__pycache__" / "a.pyc").write_bytes(b"x")
    assert build.fingerprint(False, tmp_path) == fp3


def _fake_dist(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "AetherCFD.exe").write_bytes(b"MZ")
    (d / "aethercfd-cli.exe").write_bytes(b"MZ")
    return d


def test_finished_build_needs_matching_stamp_and_exes(build, tmp_path):
    dist = _fake_dist(tmp_path / "dist")
    stamp = tmp_path / "stamp.json"
    assert not build.finished_build(dist, stamp, "fp")
    stamp.write_text(json.dumps({"fingerprint": "fp"}), encoding="utf-8")
    assert build.finished_build(dist, stamp, "fp")
    assert not build.finished_build(dist, stamp, "other")
    (dist / "aethercfd-cli.exe").unlink()
    assert not build.finished_build(dist, stamp, "fp")


@pytest.fixture
def staged(build, tmp_path, monkeypatch):
    """build.py's main() with PyInstaller and the smoke checks replaced by recorders."""
    calls = {"pyinstaller": 0, "check": 0}
    dist = tmp_path / "dist" / "AetherCFD"

    def fake_pyinstaller(cpu_only):
        calls["pyinstaller"] += 1
        _fake_dist(dist)

    def fake_check(d, cpu_only):
        calls["check"] += 1
        return calls.get("check_code", 1)

    monkeypatch.setattr(build, "dist_dir", lambda cpu_only=False: dist)
    monkeypatch.setattr(build, "work_dir", lambda cpu_only=False: tmp_path / "work")
    monkeypatch.setattr(build, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(build, "preflight_problems", lambda cpu_only: [])
    monkeypatch.setattr(build, "fingerprint", lambda cpu_only: calls.get("fp", "fp1"))
    monkeypatch.setattr(build, "run_pyinstaller", fake_pyinstaller)
    monkeypatch.setattr(build, "missing_components", lambda d, cpu_only: calls.get("missing", []))
    monkeypatch.setattr(build, "write_size_report", lambda d, suffix="": {})
    monkeypatch.setattr(build, "check", fake_check)
    (tmp_path / "work").mkdir()
    return calls


def test_failed_checks_do_not_redo_pyinstaller(build, staged):
    plenty = lambda: (30 * GB, 60 * GB)  # noqa: E731
    assert build.main(["--no-zip"], mem_fn=plenty) == 1  # built, then a smoke check failed
    assert staged == {"pyinstaller": 1, "check": 1}
    assert build.main(["--no-zip"], mem_fn=plenty) == 1  # rerun after the "fix": no second build
    assert staged["pyinstaller"] == 1 and staged["check"] == 2
    staged["fp"] = "fp2"  # a source file changed
    build.main(["--no-zip"], mem_fn=plenty)
    assert staged["pyinstaller"] == 2
    build.main(["--no-zip", "--rebuild"], mem_fn=plenty)
    assert staged["pyinstaller"] == 3


def test_low_memory_stops_before_pyinstaller(build, staged, capsys):
    assert build.main(["--no-zip"], mem_fn=lambda: (2 * GB, 16 * GB)) == 2
    assert staged["pyinstaller"] == 0
    assert "commit memory" in capsys.readouterr().err


def test_finished_build_skips_memory_check(build, staged):
    build.main(["--no-zip"], mem_fn=lambda: (30 * GB, 60 * GB))
    assert build.main(["--no-zip"], mem_fn=lambda: (1 * GB, 16 * GB)) == 1  # reached the checks
    assert staged["pyinstaller"] == 1


def test_missing_component_stops_before_smoke(build, staged, capsys):
    staged["missing"] = ["missing _internal/winpty/winpty.dll"]
    assert build.main(["--no-zip"], mem_fn=lambda: (30 * GB, 60 * GB)) == 2
    assert staged["check"] == 0
    assert "winpty.dll" in capsys.readouterr().err


def test_preflight_failure_stops_everything(build, staged, monkeypatch, capsys):
    monkeypatch.setattr(build, "preflight_problems", lambda cpu_only: ["missing xterm.js"])
    assert build.main([], mem_fn=lambda: (30 * GB, 60 * GB)) == 2
    assert staged == {"pyinstaller": 0, "check": 0}
    assert "preflight failed" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------- components
def _complete_dist(build, d: Path, gpu: bool) -> Path:
    _fake_dist(d)
    for rel in build.REQUIRED:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(b"x")
    shutil.copytree(XTERM_DIR, d / build.XTERM_IN_DIST, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("node_modules"))
    if gpu:
        for rel in build.REQUIRED_GPU:
            (d / rel).mkdir(parents=True, exist_ok=True)
            (d / rel / "lib.dll").write_bytes(b"x")
    return d


def test_component_check_gpu_build(build, tmp_path):
    d = _complete_dist(build, tmp_path / "gpu", gpu=True)
    assert build.missing_components(d, cpu_only=False) == []
    shutil.rmtree(d / "_internal" / "nvidia" / "cuda_nvrtc")
    (d / build.XTERM_IN_DIST / "dist" / "addon-fit.js").unlink()
    (d / "_internal" / "aethercfd" / "ml" / "pinn_worker.py").unlink()
    problems = build.missing_components(d, cpu_only=False)
    assert any("cuda_nvrtc" in p for p in problems)
    assert any("addon-fit.js" in p for p in problems)
    assert any("pinn_worker.py" in p for p in problems)


def test_component_check_cpu_build(build, tmp_path):
    d = _complete_dist(build, tmp_path / "cpu", gpu=False)
    assert build.missing_components(d, cpu_only=True) == []
    assert any("cuda_nvrtc" in p for p in build.missing_components(d, cpu_only=False))
    (d / "_internal" / "nvidia" / "cublas").mkdir(parents=True)
    assert any("CPU-only build contains _internal/nvidia" in p for p in build.missing_components(d, cpu_only=True))


# ------------------------------------------------------------------------------------------- size report
@pytest.mark.parametrize("rel, key", [
    ("AetherCFD.exe", "AetherCFD.exe"),
    ("_internal/nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas"),
    ("_internal/PySide6/Qt6WebEngineCore.dll", "PySide6 (QtWebEngine)"),
    ("_internal/PySide6/Qt6Core.dll", "PySide6"),
    ("_internal/vtkmodules/x/y.pyd", "vtkmodules"),
    ("_internal/python312.dll", "python312.dll (file)"),
])
def test_size_report_groups(build, rel, key):
    assert build.component_of(Path(rel)) == key


def test_size_report_ranks_and_saves(build, tmp_path):
    d = tmp_path / "AetherCFD"
    for rel, n in [("AetherCFD.exe", 10), ("_internal/nvidia/cublas/bin/a.dll", 500),
                   ("_internal/nvidia/cuda_nvrtc/bin/b.dll", 300), ("_internal/PySide6/Qt6Core.dll", 400),
                   ("_internal/PySide6/x.pyd", 50)]:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(b"x" * n)
    rep = build.write_size_report(d, tmp_path / "reports")
    assert rep["total_bytes"] == 1260 and rep["files"] == 5 and rep["nvidia_bytes"] == 800
    assert [c["component"] for c in rep["largest"]] == ["nvidia/cublas", "PySide6", "nvidia/cuda_nvrtc",
                                                        "AetherCFD.exe"]
    assert json.loads((tmp_path / "reports" / "size-report.json").read_text(encoding="utf-8")) == rep
    assert "nvidia/cublas" in (tmp_path / "reports" / "size-report.txt").read_text(encoding="utf-8")


def test_pyinstaller_reports_are_kept(build, tmp_path, monkeypatch):
    work = tmp_path / "work" / "aethercfd"
    work.mkdir(parents=True)
    (work / "warn-aethercfd.txt").write_text("missing module", encoding="utf-8")
    (work / "xref-aethercfd.html").write_text("<html>", encoding="utf-8")
    monkeypatch.setattr(build, "work_dir", lambda cpu_only=False: tmp_path / "work")
    kept = build.keep_pyinstaller_reports(False, tmp_path / "reports")
    assert sorted(p.name for p in kept) == ["warn-aethercfd.txt", "xref-aethercfd.html"]
    kept = build.keep_pyinstaller_reports(True, tmp_path / "reports")
    assert sorted(p.name for p in kept) == ["warn-aethercfd-cpu.txt", "xref-aethercfd-cpu.html"]


def test_zip_note_warns_about_github_limit(build):
    assert "too large" in build.zip_note(2 * GB + 1)
    assert "close to" in build.zip_note(int(1.9 * GB))
    assert "GitHub" not in build.zip_note(int(1.0 * GB))


def test_cpu_only_paths_do_not_touch_the_default_build(build):
    assert build.dist_dir(False) == ROOT / "dist" / "AetherCFD"
    assert build.dist_dir(True) == ROOT / "dist" / "cpu" / "AetherCFD"
    assert build.work_dir(False) != build.work_dir(True)


def test_spec_gates_every_cuda_part_on_the_cpu_flag():
    """The spec only runs inside PyInstaller; check its text so the default build keeps CUDA."""
    spec = (ROOT / "packaging" / "aethercfd.spec").read_text(encoding="utf-8")
    assert 'CPU_ONLY = os.environ.get("AETHERCFD_BUILD_CPU_ONLY") == "1"' in spec
    assert 'excludes += ["cupy", "cupyx", "cupy_backends", "cuda", "fastrlock", "nvidia"]' in spec
    assert "for sp in ([] if CPU_ONLY else site.getsitepackages()):" in spec
    assert "version=_build.exe_version_info(VERSION" in spec
