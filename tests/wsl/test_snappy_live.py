"""Live: the generated snappyHexMesh setups mesh in both forks (external sphere; internal pipe with
inlet/outlet/wall regions and layers). Scratch only; a few minutes."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, prelude, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.core.paths import win_to_wsl
from aethercfd.mesh.geometry import analyse
from aethercfd.mesh.snappy import minimal_system, snappy_setup
from aethercfd.mesh.strategy import plan
from tests.unit.test_mesh_snappy import _pipe_with_regions, _sphere
from tests.wsl._forks import DISTRO, SCRATCH_ROOT, by_family, needs_distro, needs_scratch

pytestmark = [pytest.mark.wsl_slow, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL"),
              needs_distro, needs_scratch]
SCRATCH = SCRATCH_ROOT / "snappy"


def _mesh(setup, case: Path, fork) -> str:
    if case.exists():
        shutil.rmtree(case)  # this test's own scratch folder
    for rel, text in {**minimal_system("esi" if fork.family is Family.ESI else "foundation"), **setup.files}.items():
        (case / rel).parent.mkdir(parents=True, exist_ok=True)
        (case / rel).write_text(text, encoding="utf-8", newline="\n")
    dst = case / setup.copy_surface[1]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(setup.copy_surface[0], dst)
    body = prelude(fork) + 'cd "$1" || exit 2\n' + "".join(
        f"{app} {' '.join(args)} > log.{app} 2>&1 || {{ echo FAILED {app}; tail -30 log.{app}; exit 3; }}\n"
        for app, args in setup.steps) + "tail -5 log.checkMesh\n"
    r = WslRunner(DISTRO).run_script(body, [win_to_wsl(str(case))], timeout_s=1200)
    assert r.ok, r.stdout[-4000:] + r.stderr[-2000:]
    return (case / "log.checkMesh").read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def forks():
    return by_family(probe_forks(WslRunner(DISTRO)))


@pytest.mark.timeout(1500)
@pytest.mark.parametrize("family", [Family.ESI, Family.FOUNDATION])
def test_external_sphere_meshes(tmp_path, forks, family):
    stl = _sphere(tmp_path / "ball.stl")
    geo = analyse(stl)
    best = next(p for p in plan(geo, target_cells=150_000) if p.mesher.startswith("snappyHexMesh"))
    s = snappy_setup(stl, geo, fork_family="esi" if family is Family.ESI else "foundation", target_cells=150_000,
                     strategy_settings=best.settings, n_layers=3)
    log = _mesh(s, SCRATCH / f"sphere_{family.value}", forks[family])
    assert "Mesh OK" in log
    assert "ball" in (SCRATCH / f"sphere_{family.value}" / "constant" / "polyMesh" / "boundary").read_text()


@pytest.mark.timeout(1500)
def test_internal_pipe_regions_mesh(tmp_path, forks):
    stl = _pipe_with_regions(tmp_path / "pipe.stl")
    s = snappy_setup(stl, analyse(stl), flow="internal", background_cell=0.002, surface_levels=(1, 2),
                     first_layer=2e-4, n_layers=3)
    log = _mesh(s, SCRATCH / "pipe_esi", forks[Family.ESI])
    assert "Mesh OK" in log
    boundary = (SCRATCH / "pipe_esi" / "constant" / "polyMesh" / "boundary").read_text()
    assert "pipe_inlet" in boundary and "pipe_outlet" in boundary and "pipe_wall" in boundary
