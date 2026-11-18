from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aethercfd.backend.wsl.capabilities import load_capabilities
from aethercfd.backend.wsl.forks import Family
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.foamdict.validate import validate_files
from aethercfd.scripting.api import load_forks
from tests.wsl._forks import DISTRO, SCRATCH_ROOT, by_family, needs_distro, needs_scratch

pytestmark = [pytest.mark.wsl, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL"),
              needs_distro, needs_scratch]
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
SCRATCH = SCRATCH_ROOT


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import os

    os.environ["AETHERCFD_CACHE_DIR"] = str(tmp_path_factory.mktemp("cache"))
    r = WslRunner(DISTRO)
    return r, by_family(load_forks(r, refresh=True))


@pytest.mark.parametrize("family,app,bc,turb", [
    (Family.ESI, "simpleFoam", "fixedValue", "kOmegaSST"),
    (Family.FOUNDATION, "foamRun", "fixedValue", "kOmegaSST"),
])
def test_capability_index(ctx, family, app, bc, turb):
    r, forks = ctx
    caps = load_capabilities(r, forks[family], refresh=True)
    assert app in caps.applications
    assert bc in caps.get("scalarBCs") and bc in caps.get("vectorBCs")
    assert turb in caps.get("RAS"), sorted(caps.tables)
    if family is Family.FOUNDATION:
        assert "incompressibleFluid" in caps.get("solvers")
    else:
        assert "fieldMinMax" in caps.get("functionObjects")


@pytest.mark.parametrize("folder", ["validate_ok", "validate with space"])
def test_validation_reports_openfoam_errors_with_lines(ctx, folder):
    r, forks = ctx
    case = SCRATCH / folder
    if case.exists():
        shutil.rmtree(case)
    shutil.copytree(FIX / "esi_cavity", case)
    sch = case / "system" / "fvSchemes"
    good = validate_files(r, forks[Family.ESI], case, ["system/fvSchemes", "system/controlDict"])
    assert good == {"system/fvSchemes": [], "system/controlDict": []}
    text = sch.read_text(encoding="utf-8")
    sch.write_text(text.replace("default         Gauss linear;", "default         Gauss linear;\n    bad $nope;", 1),
                   encoding="utf-8")
    bad = validate_files(r, forks[Family.ESI], case, ["system/fvSchemes"])["system/fvSchemes"]
    assert bad and bad[0].line is not None and "nope" in bad[0].message
    assert not list(case.glob("**/*.aether-tmp"))  # nothing written into the case
