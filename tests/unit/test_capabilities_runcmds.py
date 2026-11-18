from __future__ import annotations

import shutil
from pathlib import Path

from aethercfd.backend.wsl.capabilities import _parse_esi_lists, _parse_foamtoc
from aethercfd.backend.wsl.forks import Family, ForkProfile
from aethercfd.commands.base import USER, Risk
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.foamdict.validate import _parse_fatal
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
ESI = ForkProfile("esi-v2306", Family.ESI, "v2306", "Ubuntu-OpenFOAM", "/a/etc/bashrc", "/a", True)


def test_parse_esi_list_formats():
    text = ("// * * * //\nscalarBCs\n3\n(\nfixedValue\nzeroGradient\ncalculated\n)\n"
            "Turbulence models\n3(LES RAS laminar)\nfunctionObjects2(probes sets)\n")
    out = _parse_esi_lists(text)
    assert out["scalarBCs"] == ["fixedValue", "zeroGradient", "calculated"]
    assert out["Turbulence models"] == ["LES", "RAS", "laminar"]
    assert out["functionObjects"] == ["probes", "sets"]


def test_parse_foamtoc_all_format():
    text = ("ToC:\n\n2\n(\nfvPatchScalarField (fvPatchField \n2\n(\nfixedValue libfiniteVolume.so\n"
            "zeroGradient libfiniteVolume.so\n))\nsolver (solver \n1\n(\nincompressibleFluid libx.so\n))\n)\n")
    out = _parse_foamtoc(text)
    assert out["fvPatchScalarField"] == ["fixedValue", "zeroGradient"] and out["solver"] == ["incompressibleFluid"]


def test_parse_fatal_block():
    block = ("--> FOAM FATAL IO ERROR: (openfoam-2306)\nAttempt to use undefined variable $nope\n\n"
             "file: /mnt/d/x/system/fvSchemes at line 28.\n\n    From ...\n    in file db/x.C at line 99.\n"
             "FOAM exiting\n")
    line, msg = _parse_fatal(block)
    assert line == 28 and "undefined variable" in msg and "FOAM exiting" not in msg


def _executor(tmp_path, monkeypatch, run_manager=None):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "cavity"
    shutil.copytree(FIX / "esi_cavity", case)
    proj = Project.create(case)
    return case, Executor(default_registry(), Context(proj, run_manager=run_manager, forks=[ESI]), AutoApprove())


def test_run_start_without_backend_is_blocked(tmp_path, monkeypatch):
    case, ex = _executor(tmp_path, monkeypatch)
    res = ex.execute("run.start", {"case": str(case)}, USER)
    assert not res.ok and "run manager" in res.message


class FakeManager:
    def active_for_case(self, root):
        return []

    def get(self, run_id):
        return None


def test_run_start_plan_meshing_over_results_is_destructive(tmp_path, monkeypatch):
    case, ex = _executor(tmp_path, monkeypatch, FakeManager())
    shutil.copytree(case / "0", case / "0.5")  # pretend results exist
    plan = ex.plan("run.start", {"case": str(case), "preset": "mesh-check-solve"}, USER)
    assert not plan.blocked
    assert plan.risk is Risk.DESTRUCTIVE and any("re-meshing" in c for c in plan.consequences)
    assert [s.argv[0] for s in plan.shell_steps] == ["blockMesh", "checkMesh", "icoFoam"]
    assert plan.params["_resolved"]["fork"] == "esi-v2306"
    plain = ex.plan("run.start", {"case": str(case), "preset": "solve"}, USER)
    assert plain.risk is Risk.EXECUTE and any("placement: in-place" in c for c in plain.consequences)


def test_run_control_unknown_run(tmp_path, monkeypatch):
    _case, ex = _executor(tmp_path, monkeypatch, FakeManager())
    res = ex.execute("run.control", {"run_id": "nope", "action": "kill"}, USER)
    assert not res.ok and "unknown run" in res.message
