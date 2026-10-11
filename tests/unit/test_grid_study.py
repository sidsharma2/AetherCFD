"""Grid-convergence study: refined blockMesh levels, Richardson/GCI maths, the Validation gate."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from aethercfd.case import gridstudy as gs
from aethercfd.case import guided
from aethercfd.case.model import load_case
from aethercfd.commands.base import USER
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"


def _case(tmp_path, name="esi_pitzDaily") -> Path:
    c = tmp_path / name
    shutil.copytree(FIX / name, c)
    Project.create(c)
    return c


def _ex(root: Path) -> Executor:
    return Executor(default_registry(), Context(Project.find(root)), AutoApprove())


# ============================================================================ GCI maths
@pytest.mark.parametrize("p_true,cells", [(2.0, (8000, 3555, 1580)), (1.3, (18000, 8000, 4500)),
                                          (2.0, (40000, 10000, 2500))])
def test_gci_recovers_order_and_extrapolated_value(p_true, cells):
    phi0, C, dim = 3.0, 40.0, 2
    h = [(1.0 / n) ** (1 / dim) for n in cells]
    phi = tuple(phi0 + C * hi**p_true for hi in h)
    r = gs.gci("q", phi, cells, dim)
    assert r.convergence == "monotonic"
    assert r.p == pytest.approx(p_true, rel=1e-6)  # unequal ratios: needs the fixed-point iteration
    assert r.phi_ext == pytest.approx(phi0, rel=1e-9)
    assert r.e_ext21 == pytest.approx(abs(phi[0] - phi0) / phi0, rel=1e-6)
    rp = r.r21**r.p
    assert r.gci_fine == pytest.approx(1.25 * abs((phi[0] - phi[1]) / phi[0]) / (rp - 1))
    assert r.asymptotic == pytest.approx(1.0, rel=0.05)  # exact power law: in the asymptotic range


def test_gci_oscillatory_divergent_and_identical():
    cells = (8000, 3555, 1580)
    osc = gs.gci("q", (1.00, 1.02, 0.99), cells, 2)
    assert osc.convergence == "oscillatory" and osc.gci_fine is not None and osc.phi_ext is None
    div = gs.gci("q", (1.0, 1.1, 1.12), cells, 2)  # bigger change between the FINER meshes
    assert div.convergence == "divergent"
    same = gs.gci("q", (2.0, 2.0, 2.0), cells, 2)
    assert same.gci_fine == 0.0 and same.convergence == "converged"


# ============================================================================ levels
def test_scale_block_mesh_keeps_grading_and_the_empty_direction():
    text = (FIX / "esi_pitzDaily/system/blockMeshDict").read_text()
    fine, changes = gs.scale_block_mesh(text, 1.5, two_d=True)
    assert changes[0] == ((18, 30, 1), (27, 45, 1)) and changes[1] == ((180, 27, 1), (270, 40, 1))
    assert "simpleGrading (0.5 $posY 1)" in fine and "edgeGrading (4 4 4 4 $negY" in fine
    assert fine.count("hex (") == text.count("hex (")
    _coarse, ch = gs.scale_block_mesh(text, 1 / 1.5, two_d=True)
    assert ch[0][1] == (12, 20, 1)
    _three_d, ch3 = gs.scale_block_mesh(text, 2.0, two_d=False)
    assert ch3[0][1] == (36, 60, 2)
    with pytest.raises(gs.GridStudyError, match="macros"):
        gs.scale_block_mesh("blocks ( hex (0 1 2 3 4 5 6 7) ($nx 10 1) simpleGrading (1 1 1) );", 2, two_d=True)


def test_create_levels_as_one_journalled_plan(tmp_path):
    root = _case(tmp_path)
    (root / "100").mkdir()
    (root / "100" / "U").write_text("results")
    (root / "log.simpleFoam").write_text("x")
    params, plans, _notes = gs.plan_levels(root)
    assert [p.name for p in plans] == ["coarse", "fine"]
    assert plans[1].cells_estimate > plans[0].cells_estimate
    rels = set(params["copies"])
    assert "fine/0/U" in rels and "coarse/system/fvSchemes" in rels and "fine/constant/transportProperties" in rels
    assert not any("/100/" in r or "log." in r for r in rels)  # never results or logs
    res = _ex(root).execute("files.write", params, USER)
    assert res.ok and res.journal_id
    bmd = (root / "grid_study/fine/system/blockMeshDict").read_text()
    assert re.search(r"\(\s*27 45 1\s*\)", bmd)
    assert [n for n, _ in gs.levels(root)] == ["fine", "medium", "coarse"]
    with pytest.raises(gs.GridStudyError, match="already exists"):
        gs.plan_levels(root)
    # the study folder is never copied into a level, and the run plans cover the two new levels
    assert all(p["preset"] == "mesh-check-solve" for p in gs.run_params(root)) and len(gs.run_params(root)) == 2
    assert _no_study_files(gs.input_files(root))


def _no_study_files(files) -> bool:
    return not any(f.startswith(gs.STUDY_DIR) for f in files)


def test_no_blockmesh_is_explained(tmp_path):
    root = _case(tmp_path)
    (root / "system" / "blockMeshDict").unlink()
    with pytest.raises(gs.GridStudyError, match="refineMesh"):
        gs.plan_levels(root)


def test_dat_values_take_the_last_row(tmp_path):
    d = tmp_path / "postProcessing" / "forces1" / "0"
    d.mkdir(parents=True)
    (d / "forceCoeffs.dat").write_text("# Time Cd Cl\n1 0.5 0.1\n2 0.42 0.12\n")
    (d / "solverInfo.dat").write_text("# Time p\n2 1e-3\n")
    assert gs.dat_values(tmp_path) == {"forces1/forceCoeffs:Cd": 0.42, "forces1/forceCoeffs:Cl": 0.12}


# ============================================================================ the gate
def _record(root: Path, phi, cells=(27500, 12225, 5430), converged=True) -> None:
    r = gs.gci("mean p on inlet", phi, cells, 2)
    rec = {"format": 1, "dim": 2, "results": [r.as_dict()],
           "levels": [{"name": n, "converged": converged} for n in ("fine", "medium", "coarse")],
           "medium_mesh": guided.fingerprint([root / "constant" / "polyMesh"])}
    res = _ex(root).execute("dict.batch", gs.record_params(root, rec), USER)
    assert res.ok, res.message


def test_gate_without_a_study_warns(tmp_path):
    root = _case(tmp_path)
    v = gs.study_gate(load_case(root), {})
    assert not v.gate.ok and v.gate.severity == "warn" and "Mesh independence" in v.gate.title


def test_gate_passes_fails_on_tolerance_and_divergence(tmp_path):
    root = _case(tmp_path)
    h = [(1 / n) ** 0.5 for n in (27500, 12225, 5430)]
    _record(root, tuple(10 + 3 * x**2 for x in h))
    v = gs.study_gate(load_case(root), {})
    assert v.gate.ok and "Mesh-independent" in v.gate.title and "observed order p = 2" in " ".join(v.gate.evidence)
    _record(root, tuple(10 + 3000 * x**2 for x in h))  # a large mesh dependence
    v = gs.study_gate(load_case(root), {"gci_tolerance": 1})
    assert v.gate.blocking and "> 1 %" in v.gate.title
    _record(root, (1.0, 1.1, 1.12))
    assert gs.study_gate(load_case(root), {}).gate.blocking
    _record(root, tuple(10 + 3 * x**2 for x in h), converged=False)
    v = gs.study_gate(load_case(root), {})
    assert not v.gate.ok and v.gate.severity == "warn" and "iteratively" in v.gate.title


def test_validation_stage_offers_the_study(tmp_path):
    root = _case(tmp_path)
    st = next(s for s in guided.assess(root) if s.id == "validation")
    assert any(a.id == "grid.create" for a in st.actions)
    assert any(q.id == "gci_tolerance" for q in st.questions)
    assert any(g.id == "grid-study" for g in st.gates)
    gp = guided.plan_answers(root, "validation", {"gci_tolerance": 2})
    assert gp.answers == {"gci_tolerance": 2.0} and not gp.edits
    h = [(1 / n) ** 0.5 for n in (27500, 12225, 5430)]
    _record(root, tuple(10 + 3 * x**2 for x in h))
    st = next(s for s in guided.assess(root) if s.id == "validation")
    assert any(q.id == "grid_qoi" and q.choices for q in st.questions)
    json.dumps(st.as_dict(root))


def test_fine_level_gets_more_iterations(tmp_path):
    root = _case(tmp_path)
    params, _plans, notes = gs.plan_levels(root)
    cd = params["files"]["fine/system/controlDict"]
    assert re.search(r"^endTime\s+4500;", cd, re.M)  # 2000 x 1.5^2 (2D, steady SIMPLE)
    assert "fine/system/controlDict" not in params["copies"] and "coarse/system/controlDict" in params["copies"]
    assert any("endTime 2000 → 4500" in n for n in notes)


def _record_full(root, phi, levels, nut="nutkWallFunction"):
    r = gs.gci("mean p on inlet", phi, (27500, 12225, 5430), 2)
    rec = {"format": 1, "dim": 2, "results": [r.as_dict()], "levels": levels, "nut_wall": nut,
           "medium_mesh": guided.fingerprint([root / "constant" / "polyMesh"])}
    assert _ex(root).execute("dict.batch", gs.record_params(root, rec), USER).ok


def test_unconverged_level_makes_the_study_inconclusive_and_yplus_drift_is_explained(tmp_path):
    root = _case(tmp_path)
    levels = [{"name": "fine", "converged": False, "yplus_mean": 9.3},
              {"name": "medium", "converged": True, "yplus_mean": 15.0},
              {"name": "coarse", "converged": True, "yplus_mean": 23.8}]
    _record_full(root, (-4.75, -5.38, -5.84), levels)
    v = gs.study_gate(load_case(root), {})
    assert v.gate.blocking and "inconclusive" in v.gate.title and "fine" in v.gate.title
    assert "log layer" in v.gate.how and "nutUSpaldingWallFunction" in v.gate.how
    assert any("23.8 / 15 / 9.3" in e for e in v.gate.evidence)
    for lv in levels:
        lv["converged"] = True
    _record_full(root, (-4.75, -5.38, -5.84), levels)
    v = gs.study_gate(load_case(root), {})
    assert v.gate.blocking and v.gate.title == "Grid study diverges" and "log layer" in v.gate.how
    _record_full(root, (-4.75, -5.38, -5.84), levels, nut="nutUSpaldingWallFunction")
    assert "log layer" not in gs.study_gate(load_case(root), {}).gate.how  # Spalding is valid at any y+


def test_dat_values_skip_non_quantities(tmp_path):
    for obj, stem in (("mm", "fieldMinMax"), ("yp", "yPlus"), ("f", "forces")):
        d = tmp_path / "postProcessing" / obj / "0"
        d.mkdir(parents=True)
        (d / f"{stem}.dat").write_text("# Time a\n1 2.5\n")
    assert gs.dat_values(tmp_path) == {"f/forces:a": 2.5}


def test_iterative_convergence_is_judged_on_the_quantity(tmp_path):
    root = _case(tmp_path)
    h = [(1 / n) ** 0.5 for n in (27500, 12225, 5430)]
    phi = tuple(10 + 3 * x**2 for x in h)
    e_min = min(abs(phi[0] - phi[1]), abs(phi[1] - phi[2]))
    q = "mean p on inlet"
    levels = [{"name": "fine", "converged": False, "changes": {q: 0.01 * e_min}},
              {"name": "medium", "converged": True, "changes": {q: 0.0}},
              {"name": "coarse", "converged": True}]
    _record_full(root, phi, levels)
    v = gs.study_gate(load_case(root), {})
    assert v.gate.ok and any("residuals stalled but the quantity is steady" in e for e in v.gate.evidence)
    levels[0]["changes"] = {q: 0.5 * e_min}  # still drifting: the iterative error rivals the mesh effect
    _record_full(root, phi, levels)
    v = gs.study_gate(load_case(root), {})
    assert not v.gate.ok and "fine" in " ".join(v.gate.evidence)
