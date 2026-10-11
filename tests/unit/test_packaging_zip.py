"""The shareable release zip: app folder + installers + INSTALL.md, versioned name, CRLF for the .cmd."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _build_module():
    spec = importlib.util.spec_from_file_location("aether_packaging_build", ROOT / "packaging" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_release_zip_layout(tmp_path):
    build = _build_module()
    dist = tmp_path / "dist" / "AetherCFD"
    (dist / "_internal").mkdir(parents=True)
    (dist / "AetherCFD.exe").write_bytes(b"MZ")
    (dist / "_internal" / "lib.dll").write_bytes(b"x")
    out = build.make_zip(dist, tmp_path / "out", "1.2.3")
    assert out.name == "AetherCFD-1.2.3-win64.zip"
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"AetherCFD/AetherCFD.exe", "AetherCFD/_internal/lib.dll", "install.py", "Install AetherCFD.cmd",
                "INSTALL.md"} <= names
        cmd = z.read("Install AetherCFD.cmd")
        assert b"\r\n" in cmd and b"\n" not in cmd.replace(b"\r\n", b"")  # every line ends CRLF
        assert b"wizard" in z.read("INSTALL.md").lower()
    assert build.RELEASE_EXTRAS.keys() == {"install.py", "Install AetherCFD.cmd", "INSTALL.md"}
    assert all(p.is_file() for p in build.RELEASE_EXTRAS.values())


def test_release_zip_needs_a_build(tmp_path):
    with pytest.raises(FileNotFoundError, match="build.py"):
        _build_module().make_zip(tmp_path / "nothing", tmp_path, "1")


def test_install_script_finds_the_app_next_to_it(tmp_path):
    rel = tmp_path / "AetherCFD-1.2.3-win64"
    (rel / "AetherCFD").mkdir(parents=True)
    (rel / "AetherCFD" / "AetherCFD.exe").write_bytes(b"MZ")
    (rel / "install.py").write_bytes((ROOT / "packaging" / "install.py").read_bytes())
    spec = importlib.util.spec_from_file_location("aether_release_install", rel / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert mod.DIST == rel / "AetherCFD"
