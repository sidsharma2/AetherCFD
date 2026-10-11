"""The guided workflow: stage states, correctness gates, answers -> dict.batch plans, marking done."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from aethercfd.case import gates as G
from aethercfd.case import guided
from aethercfd.case.model import load_case
from aethercfd.commands.base import USER
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
CHECKMESH_OK = """Checking geometry...
    Overall domain bounding box (0 0 0) (0.1 0.1 0.01)
    Mesh has 2 geometric (non-empty/wedge) directions (1 1 0)
    Boundary openness (0 0 0) OK.
    Max cell openness = 0 OK.
    Max aspect ratio = 1 OK.
    Minimum face area = 2.5e-05. Maximum face area = 5e-05.  Face area magnitudes OK.
    Min volume = 2.5e-07. Max volume = 2.5e-07.  Total volume = 0.0001.  Cell volumes OK.
    Mesh non-orthogonality Max: 0 average: 0
    Non-orthogonality check OK.
    Face pyramids OK.
    Max skewness = 1e-08 OK.

Mesh OK.

End
"""


def _case(tmp_path, name: str) -> Path:
    c = tmp_path / name
    shutil.copytree(FIX / name, c)
    Project.create(c)
    return c


def _ex(root: Path) -> Executor:
    return Executor(default_registry(), Context(Project.find(root)), AutoApprove())


def _stage(root: Path, sid: str):
    return next(s for s in guided.assess(root) if s.id == sid)


def _apply(root: Path, params: dict):
    res = _ex(root).execute("dict.batch", params, USER)
    assert res.ok, res.message
    return res


# ============================================================================ dict.batch
def test_dict_batch_same_file_nested_create_replace_and_revert(tmp_path):
    root = _case(tmp_path, "esi_cavity")
    sol = root / "system" / "fvSolution"
    before = sol.read_text()
    side = root / ".aethercfd" / "local" / "workflow.json"
    params = {"edits": [{"file": str(sol), "key": "PISO/nCorrectors", "value": "3"},
                        {"file": str(sol), "key": "PISO/nNonOrthogonalCorrectors", "value": "1"},
                        {"file": str(sol), "key": "relaxationFactors/equations/U", "value": "0.7",
                         "insert_if_missing": True}],
             "creates": {str(root / "0" / "nut"): "FoamFile { object nut; }\n"},
             "replace": {str(side): "{}\n"}, "summary": "batch"}
    ex = _ex(root)
    plan = ex.plan("dict.batch", params, USER)
    assert not plan.blocked
    assert len(plan.file_changes) == 3  # one change per file, although fvSolution has three edits
    res = ex.execute("dict.batch", params, USER)
    assert res.ok and res.journal_id
    txt = sol.read_text()
    assert re.search(r"nCorrectors\s+3;", txt) and re.search(r"nNonOrthogonalCorrectors\s+1;", txt)
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    assert DictView(parse(txt)).get_text("relaxationFactors/equations/U").strip() == "0.7"
    assert (root / "0" / "nut").is_file() and side.read_text() == "{}\n"
    rev = ex.execute("file.revert", {"journal_id": res.journal_id}, USER)
    assert rev.ok, rev.message
    assert sol.read_text() == before and not (root / "0" / "nut").exists() and not side.exists()


def test_dict_batch_refuses_existing_create_and_missing_key(tmp_path):
    root = _case(tmp_path, "esi_cavity")
    plan = _ex(root).plan("dict.batch", {"creates": {str(root / "0" / "U"): "x"},
                                         "edits": [{"file": str(root / "system/fvSolution"), "key": "PISO/nope",
                                                    "value": "1"}]}, USER)
    msgs = " ".join(c.message for c in plan.blocked)
    assert "already exists" in msgs and "not set" in msgs


# ============================================================================ stages
def test_twelve_stages_in_order_with_dependencies(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    stages = guided.assess(root)
    assert [s.id for s in stages] == guided.ORDER
    assert [s.title for s in stages][:4] == ["Geometry", "Domain", "Materials", "Physics"]
    by = {s.id: s for s in stages}
    assert by["geometry"].status == "na"  # blockMesh case: no CAD surfaces
    assert by["mesh"].status == "error" and by["mesh"].blocking[0].id == "mesh-exists"
    assert "blockMesh" in by["mesh"].next_action or "mesher" in by["mesh"].next_action
    assert by["bc"].status == "blocked" and "Mesh" in by["bc"].next_action
    assert by["run"].status == "error" and by["run"].actions[0].id == "run.start"
    for s in stages:
        assert s.next_action
        d = s.as_dict(root)
        json.dumps(d)  # the copilot gets this as JSON


def test_reference_quantities_and_physics_questions(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    ref = G.reference(load_case(root))
    assert pytest.approx(10.0) == ref.U and ref.nu == pytest.approx(1e-5)
    assert ref.mach == pytest.approx(10 / (1.4 * 287.05 * 293.15) ** 0.5, rel=1e-6)
    ph = _stage(root, "physics")
    q = {x.id: x for x in ph.questions}
    assert q["U_ref"].value == pytest.approx(10.0) and "Ma ≈ 0.029" in q["U_ref"].derived
    assert q["time_dependence"].value == "steady"
    mat = {x.id: x for x in _stage(root, "materials").questions}
    assert mat["fluid"].value == "air" and mat["nu"].value == pytest.approx(1e-5)


def test_inlet_turbulence_defaults_reproduce_the_tutorial(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    q = {x.id: x for x in _stage(root, "bc").questions}
    assert q["inlet_U:inlet"].value == pytest.approx(10.0)
    assert q["outlet_p:outlet"].value == 0.0
    assert q["turb_intensity"].value == pytest.approx(5.0, rel=1e-3)
    assert q["turb_length"].value == pytest.approx(0.00254, rel=1e-2)
    k, eps, _om = guided.turbulence_values(10.0, 5.0, q["turb_length"].value)
    assert k == pytest.approx(0.375) and eps == pytest.approx(14.855, rel=2e-3)


def test_answers_become_one_journalled_plan(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    gp = guided.plan_answers(root, "bc", {"inlet_U:inlet": 20, "turb_intensity": 5.0})
    assert not gp.errors
    files = {Path(e["file"]).name for e in gp.edits}
    assert {"U", "k", "epsilon"} <= files  # omega's inlet follows $internalField (internal value edited)
    res = _apply(root, gp.batch_params(root))
    assert len(res.plan.file_changes) >= 3
    assert "uniform (20 0 0)" in (root / "0/U").read_text()
    k = G.FieldFile(root / "0/k", (root / "0/k").read_text())
    assert G._uniform_scalar(k.get("boundaryField/inlet/value")) == pytest.approx(1.5)  # 1.5 (0.05*20)^2
    assert G._uniform_scalar(k.get("internalField")) == pytest.approx(1.5)
    om = G.FieldFile(root / "0/omega", (root / "0/omega").read_text())
    assert om.get("boundaryField/inlet/value") == "$internalField"  # macro kept, follows internalField
    last = next(iter(Project.find(root).journal.entries(newest_first=True)))
    assert last["command"] == "dict.batch" and "Guided" in last["summary"]


def test_sidecar_answers_are_kept_out_of_the_case(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    gp = guided.plan_answers(root, "domain", {"L_ref": 0.0254, "flow_kind": "internal"})
    assert not gp.edits and gp.answers == {"L_ref": 0.0254, "flow_kind": "internal"}
    _apply(root, gp.batch_params(root))
    st = guided.load_state(root)
    assert st["answers"]["L_ref"] == 0.0254
    assert G.reference(load_case(root), st["answers"]).reynolds == pytest.approx(10 * 0.0254 / 1e-5)


def test_bad_answers_are_explained(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    gp = guided.plan_answers(root, "models", {"turbulence": "kEpsilonWrong", "nope": 1})
    assert len(gp.errors) == 2 and gp.empty
    gp = guided.plan_answers(root, "physics", {"compressible": "yes"})
    assert "template" in gp.errors[0]
    with pytest.raises(ValueError):
        guided.plan_answers(root, "nostage", {})


def test_turbulence_switch_creates_fields_and_solver_entries(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    (root / "0" / "omega").unlink()
    sol = root / "system" / "fvSolution"
    sol.write_text(sol.read_text().replace('"(U|k|epsilon|omega|f|v2)"', '"(U|k|epsilon)"'))
    sch = root / "system" / "fvSchemes"
    sch.write_text(sch.read_text().replace("    div(phi,omega)  $turbulence;\n", ""))
    gp = guided.plan_answers(root, "models", {"turbulence": "kOmegaSST"})
    assert not gp.errors
    assert any(p.endswith("omega") for p in gp.creates)
    _apply(root, gp.batch_params(root))
    om = (root / "0" / "omega").read_text()
    assert "omegaWallFunction" in om and "frontAndBack" in om and "type empty" in om
    assert "dimensions      [0 0 -1 0 0 0 0]" in om
    case = load_case(root)
    from aethercfd.case.checks import facts

    f = facts(case)
    assert f.turbulence == "kOmegaSST"
    assert G.coverage_gate(case, f, G.initial_fields(case)).ok
    models = _stage(root, "models")
    assert not any(g.id == "check:turb-fields" for g in models.gates)


def test_monitoring_function_objects_go_into_the_functions_dict(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    gp = guided.plan_answers(root, "run", {"monitoring": True, "end_time": 500, "residual_target": 1e-4})
    _apply(root, gp.batch_params(root))
    cd = (root / "system/controlDict").read_text()
    from aethercfd.foamdict.parser import parse
    from aethercfd.foamdict.semantic import DictView

    doc = parse(cd)
    assert doc.ok
    v = DictView(doc)
    assert v.get_text("functions/aetherFieldMinMax/type").strip() == "fieldMinMax"
    assert v.get_text("functions/aetherResiduals/type").strip() == "solverInfo"  # ESI
    assert "#includeFunc streamlines" in cd  # the tutorial's own entry is untouched
    assert v.get_text("endTime").strip() == "500"
    fs = DictView(parse((root / "system/fvSolution").read_text()))
    assert fs.get_text("SIMPLE/residualControl/p").strip() == "0.0001"
    assert guided._has_monitors(load_case(root))


# ============================================================================ gates
def test_pressure_reference_gate_and_one_click_fix(tmp_path):
    root = _case(tmp_path, "esi_cavity")
    sol = root / "system" / "fvSolution"
    sol.write_text(sol.read_text().replace("    pRefCell        0;\n    pRefValue       0;\n", ""))
    st = _stage(root, "bc")
    g = next(g for g in st.gates if g.id == "p-reference")
    assert g.blocking and g.edits and "singular" in g.why
    _apply(root, {"edits": [{"file": str(root / e.file), "key": e.key, "value": e.value,
                             "insert_if_missing": e.insert_if_missing} for e in g.edits]})
    assert next(g for g in _stage(root, "bc").gates if g.id == "p-reference").ok


def test_dimensions_gate(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    p = root / "0" / "p"
    p.write_text(p.read_text().replace("[0 2 -2 0 0 0 0]", "[1 -1 -2 0 0 0 0]"))
    st = _stage(root, "bc")
    g = next(g for g in st.gates if g.id == "dimensions")
    assert g.blocking and "kg^-1" not in g.evidence[0] and "expected [0 2 -2 0 0 0 0]" in g.evidence[0]
    assert g.edits[0].key == "dimensions"


def test_u_p_pairing_warns_on_overconstrained_inlet(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    p = root / "0" / "p"
    p.write_text(p.read_text().replace("    inlet\n    {\n        type            zeroGradient;",
                                       "    inlet\n    {\n        type            fixedValue;\n        value uniform 0;"))
    g = next(g for g in _stage(root, "bc").gates if g.id == "u-p-pairing")
    assert not g.ok and g.severity == "warn" and "inlet" in g.evidence[0]


def test_mach_gate_blocks_incompressible_at_high_speed(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    U = root / "0" / "U"
    U.write_text(U.read_text().replace("uniform (10 0 0)", "uniform (200 0 0)"))
    st = _stage(root, "physics")
    g = next(g for g in st.gates if g.id == "mach")
    assert g.blocking and "0.58" in g.title
    assert not st.can_mark_done


def test_steady_answer_must_match_the_algorithm(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    _apply(root, guided.plan_answers(root, "physics", {"time_dependence": "transient"}).batch_params(root))
    g = next(g for g in _stage(root, "physics").gates if g.id == "steady")
    assert g.blocking and "SIMPLE" in g.why


def test_species_sum_gate(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    (root / "constant" / "thermophysicalProperties").write_text(
        "FoamFile { object thermophysicalProperties; }\nthermoType { type hePsiThermo; mixture "
        "multiComponentMixture; }\ninertSpecie N2;\n")
    (root / "constant" / "reactions").write_text("species 3 ( CH4 O2 N2 );\nreactions {}\n")
    for name, v in (("CH4", 0.1), ("O2", 0.25), ("N2", 0.7)):
        (root / "0" / name).write_text(
            f"FoamFile {{ class volScalarField; object {name}; }}\ndimensions [0 0 0 0 0 0 0];\n"
            f"internalField uniform {v};\nboundaryField {{ inlet {{ type fixedValue; value uniform {v}; }} "
            "outlet { type zeroGradient; } upperWall { type zeroGradient; } lowerWall { type zeroGradient; } "
            "frontAndBack { type empty; } }\n")
    case = load_case(root)
    g = G.species_sum_gate(case, G.initial_fields(case))
    assert g.blocking and "1.05" in " ".join(g.evidence) and "N2" in g.why
    (root / "0" / "N2").write_text((root / "0" / "N2").read_text().replace("0.7", "0.65"))
    case = load_case(root)
    assert G.species_sum_gate(case, G.initial_fields(case)).ok


def test_mesh_gates_follow_checkmesh_and_mark_done(tmp_path):
    root = _case(tmp_path, "esi_cavity")
    st = _stage(root, "mesh")
    g = next(g for g in st.gates if g.id == "mesh-quality")
    assert g.blocking and "checkMesh has not been run" in g.title
    assert "cannot be marked done" in guided.mark_params(root, "mesh", True)[1]
    (root / "log.checkMesh").write_text(CHECKMESH_OK)
    st = _stage(root, "mesh")
    assert st.status == "ready" and st.can_mark_done
    params, refusal = guided.mark_params(root, "mesh", True)
    assert refusal is None
    _apply(root, params)
    assert _stage(root, "mesh").status == "done"
    assert "Next:" in _stage(root, "mesh").next_action
    # a failing checkMesh written later turns the stage into an error (and the mark no longer counts)
    (root / "log.checkMesh").write_text(CHECKMESH_OK.replace("Mesh OK.", "Failed 1 mesh checks."))
    st = _stage(root, "mesh")
    assert st.status == "error" and st.marked_done


def test_changed_files_invalidate_a_done_mark(tmp_path):
    root = _case(tmp_path, "esi_cavity")
    _apply(root, guided.mark_params(root, "numerics", True)[0])
    assert _stage(root, "numerics").status == "done"
    sch = root / "system" / "fvSchemes"
    sch.write_text(sch.read_text() + "\n// edited by hand\n")
    st = _stage(root, "numerics")
    assert st.status == "changed" and "changed since" in st.next_action


def test_later_stages_do_not_invalidate_earlier_marks(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    for sid in ("physics", "numerics"):
        _apply(root, guided.mark_params(root, sid, True)[0])
    # the Run stage edits controlDict (function objects) and fvSolution (residualControl)
    _apply(root, guided.plan_answers(root, "run", {"monitoring": True, "residual_target": 1e-4}).batch_params(root))
    by = {s.id: s for s in guided.assess(root)}
    assert by["physics"].status == "done" and by["numerics"].status == "done"
    sch = root / "system" / "fvSchemes"
    sch.write_text(sch.read_text().replace("bounded Gauss linearUpwind grad(U)", "bounded Gauss upwind"))
    assert _stage(root, "numerics").status == "changed"  # its own decisions still invalidate it


def test_yplus_estimate_from_the_mesh(tmp_path):
    root = _case(tmp_path, "of14_cavity")
    case = load_case(root)
    dist = G.wall_distance(case)
    assert dist and all(0 < a <= m <= b for a, m, b in dist.values())
    from aethercfd.case.checks import facts

    f = facts(case)
    ref = G.Reference(U=1.0, L=0.1, nu=1e-5, T=293.0)
    g = G.yplus_gate(case, f, G.initial_fields(case), ref)
    assert any("y+" in e for e in g.evidence) and "a-priori" in g.evidence[0]


def test_run_gate_reads_the_solver_log(tmp_path):
    root = _case(tmp_path, "of14_cavity")
    _g, s = G.run_gate(load_case(root))
    assert s is not None and s.steps > 0
    st = _stage(root, "validation")
    assert any(x.id == "convergence" for x in st.gates)


def test_overview_for_the_copilot(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    ov = guided.overview(root)
    assert ov["current_stage"] == "domain"  # geometry is not needed; domain has an open question (L)
    assert len(ov["stages"]) == 12 and ov["reference"]["U"] == 10.0
    json.dumps(ov)


def test_monitors_ask_only_for_fields_the_model_creates(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")  # kEpsilon, but 0/omega and 0/nuTilda exist
    gp = guided.plan_answers(root, "run", {"monitoring": True})
    body = next(e["value"] for e in gp.edits if e["key"] == "functions/aetherFieldMinMax")
    assert "fields (U p k epsilon nut)" in body and "omega" not in body and "nuTilda" not in body
