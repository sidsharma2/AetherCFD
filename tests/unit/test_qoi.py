"""Quantities of interest: fork-aware monitors, reading both forks' output, the integral-convergence and
conservation gates, the guided Run/Validation/Post wiring, the run report, its copilot tools and CLI.

The .dat texts below are the exact layouts ESI v2306 and Foundation 14 wrote for these function objects
(pitzDaily and buoyantCavity, run live on 2026-10-26)."""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

import pytest

from aethercfd.case import guided
from aethercfd.case import qoi as Q
from aethercfd.case.checks import facts
from aethercfd.case.model import load_case
from aethercfd.commands.base import USER
from aethercfd.commands.executor import AutoApprove, Context, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
J = {"rationale": "report", "confidence": 0.9}

FORCE_ESI_HEAD = ("# Force       \n# CofR        : (0.000000e+00 0.000000e+00 0.000000e+00)\n#\n"
                  "# Time        \ttotal_x total_y total_z\tpressure_x pressure_y pressure_z\t"
                  "viscous_x viscous_y viscous_z\n")
FORCES_OF_HEAD = ("# Forces      \n# CofR        : (0.000000e+00 0.000000e+00 0.000000e+00)\n"
                  "# Time        forces(pressure viscous)\tmoments(pressure viscous)\n")
SFV_ESI_HEAD = "# Region type : patch {p}\n# Faces       : 30\n# Area        : 2.540000e-05\n# Scale factor: 1\n" \
               "# Time        \t{col}\n"
SFV_OF_HEAD = "# Selection   : patch({p})\n# Faces       : 30\n# Area        : 2.540000e-05\n# Time        \t{col}\n"
HEAT_ESI_HEAD = "# Wall heat-flux\n# Time        \tpatch         \tmin           \tmax           \tintegral      \n"
HEAT_OF_HEAD = ("# Wall heat-flux\n# Time        \tpatch         \tmin [W/m^2]   \tmax [W/m^2]   \tQ [W]         \t"
                "q [W/m^2]     \n")


def _case(tmp_path, name: str) -> Path:
    c = tmp_path / name
    shutil.copytree(FIX / name, c)
    Project.create(c)
    return c


def _dat(root: Path, obj: str, t: str, name: str, text: str) -> Path:
    p = root / "postProcessing" / obj / t / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _esi_force_rows(n: int, fx=lambda i: 1.0, start: int = 1) -> str:
    return "".join(f"{i}              {fx(i):.6e} 2.0e-03 1.0e-21 {fx(i) - 1e-4:.6e} 2.0e-03 0 1.0e-04 0 0\n"
                   for i in range(start, start + n))


def _flows(root: Path, n: int, inflow: float, outflow: float, *, esi: bool = True, drift: float = 0.0) -> None:
    head = SFV_ESI_HEAD if esi else SFV_OF_HEAD
    for patch, v in (("inlet", -inflow), ("outlet", outflow)):
        rows = "".join(f"{i}\t{v * (1 + drift * (i - n) / n):.6e}\n" for i in range(1, n + 1))
        _dat(root, f"aetherFlow_{patch}", "0", "surfaceFieldValue.dat", head.format(p=patch, col="sum(phi)") + rows)


# ============================================================================ plan: what to monitor
def test_plan_esi_pitzDaily_monitors_and_syntax(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    case = load_case(root)
    assert Q.fork_is_esi(case)
    qp = Q.plan(case, {"fluid": "air"})
    assert set(qp.objects) == {"aetherForces", "aetherFlow_inlet", "aetherFlow_outlet", "aetherMean_inlet",
                               "aetherMean_outlet"}
    assert "libs (forces)" in qp.objects["aetherForces"] and "rhoInf 1.204" in qp.objects["aetherForces"]
    assert "regionType patch; name inlet;" in qp.objects["aetherFlow_inlet"]
    assert "operation sum; fields (phi);" in qp.objects["aetherFlow_inlet"]
    assert "operation areaAverage; fields (p);" in qp.objects["aetherMean_outlet"]
    ids = {q.id for q in qp.qois}
    assert {"force:Fx", "force:Fy", "flow:inlet", "flow:outlet", "mean:p:inlet", "mean:p:outlet", "dp"} <= ids
    assert next(q for q in qp.qois if q.id == "flow:inlet").unit == "m³/s"
    # no fluid given: forces per unit density, and the note says how to get newtons
    qp1 = Q.plan(case, {})
    assert "rhoInf 1;" in qp1.objects["aetherForces"] and any("Materials" in n for n in qp1.notes)


def test_plan_foundation_lid_cavity_gets_forces_and_no_flows(tmp_path):
    root = _case(tmp_path, "of14_cavity")
    case = load_case(root)
    assert not Q.fork_is_esi(case)
    qp = Q.plan(case)
    assert list(qp.objects) == ["aetherForces"]  # closed box, but the lid moves
    assert 'libs ("libforces.so")' in qp.objects["aetherForces"]
    assert not qp.open_patches and not any(q.group == "flow" for q in qp.qois)


def test_plan_closed_still_box_has_no_force_qoi(tmp_path):
    root = _case(tmp_path, "of14_cavity")
    u = root / "0" / "U"
    u.write_text(u.read_text().replace("uniform (1 0 0)", "uniform (0 0 0)"))
    assert "aetherForces" not in Q.plan(load_case(root)).objects


# ============================================================================ reading both forks' output
def test_read_esi_force_restart_and_partial_row(tmp_path):
    root = tmp_path / "c"
    _dat(root, "aetherForces", "0", "force.dat", FORCE_ESI_HEAD + _esi_force_rows(4, lambda i: float(i)))
    # a restart at t=3 replaces 3 and 4; its last row is still being written (no newline): ignored
    _dat(root, "aetherForces", "3", "force.dat", FORCE_ESI_HEAD + _esi_force_rows(3, lambda i: 10.0 * i, start=3)
         + "6              -1.0")
    s = Q.read_series(root)
    assert [t for t, _ in s["force:Fx"]] == [1, 2, 3, 4, 5]
    assert [v for _, v in s["force:Fx"]] == [1.0, 2.0, 30.0, 40.0, 50.0]
    assert "force:Fy" in s and "force:Fz" in s


def test_read_foundation_forces_sum_pressure_and_viscous_and_drop_time_zero(tmp_path):
    root = tmp_path / "c"
    rows = ("0             \t((0 0 0) (0 -2.6e-23 0)) ((0 0 0) (0 0 4.1e-25))\n"
            "1             \t((-1.105449e-02 2.853919e-03 -8.592717e-17) (1.090331e-04 -6.681527e-06 1.386198e-17)) "
            "((5.8e-22 2.0e-17 -9.9e-05) (-1.9e-19 -2.1e-18 4.7e-07))\n")
    _dat(root, "aetherForces", "0", "forces.dat", FORCES_OF_HEAD + rows)
    s = Q.read_series(root)
    assert len(s["force:Fx"]) == 1  # the t=0 row (before the first solve) is dropped
    assert s["force:Fx"][0][1] == pytest.approx(-1.105449e-02 + 1.090331e-04)
    assert s["force:Fy"][0][1] == pytest.approx(2.853919e-03 - 6.681527e-06)


def test_read_surface_values_and_wall_heat_flux_both_forks(tmp_path):
    root = tmp_path / "c"
    _dat(root, "aetherFlow_outlet", "0", "surfaceFieldValue.dat",
         SFV_OF_HEAD.format(p="outlet", col="sum(phi)") + "0\t0\n1\t2.57e-04\n2\t2.55e-04\n")
    _dat(root, "aetherMean_inlet", "0", "surfaceFieldValue.dat",
         SFV_ESI_HEAD.format(p="inlet", col="areaAverage(p)\tareaAverage(T)") + "1\t708\t300\n2\t95\t301\n")
    _dat(root, "aetherWallHeatFlux", "0", "wallHeatFlux.dat",
         HEAT_ESI_HEAD + "1\thot\t352\t353\t400.07\n1\tcold\t-116\t-116\t-131.5\n")
    s = Q.read_series(root)
    assert s["flow:outlet"] == [(1.0, 2.57e-04), (2.0, 2.55e-04)]
    assert s["mean:p:inlet"][-1] == (2.0, 95.0) and s["mean:T:inlet"][-1] == (2.0, 301.0)
    assert s["heat:hot"] == [(1.0, 400.07)] and s["heat:cold"] == [(1.0, -131.5)]
    root2 = tmp_path / "c2"
    _dat(root2, "aetherWallHeatFlux", "0", "wallHeatFlux.dat",
         HEAT_OF_HEAD + "1.000000e+00\thot\t3.5e+02\t3.5e+02\t4.000722e+02\t3.529219e+02\n")
    assert Q.read_series(root2)["heat:hot"] == [(1.0, 400.0722)]  # Q [W], not q [W/m^2]


def test_other_function_objects_are_read_like_the_grid_study(tmp_path):
    root = tmp_path / "c"
    _dat(root, "forceCoeffs1", "0", "coefficient.dat", "# Time\tCd\tCl\n1\t0.5\t0.1\n2\t0.52\t0.11\n")
    _dat(root, "fieldMinMax1", "0", "fieldMinMax.dat", "# Time\tfield\tmin\n1\tp\t0\n")
    s = Q.read_series(root)
    assert s["forceCoeffs1/coefficient:Cd"][-1] == (2.0, 0.52)
    assert not any(k.startswith("fieldMinMax1") for k in s)


def test_derived_pressure_drop(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    for patch, v in (("inlet", 12.0), ("outlet", 0.5)):
        _dat(root, f"aetherMean_{patch}", "0", "surfaceFieldValue.dat",
             SFV_ESI_HEAD.format(p=patch, col="areaAverage(p)") + f"1\t{v}\n2\t{v}\n")
    s = Q.read_series(root, Q.plan(load_case(root)))
    assert s["dp"] == [(1.0, 11.5), (2.0, 11.5)]


# ============================================================================ judging
def test_steady_band_and_transient_drift():
    flat = [(float(i), 1.0 + (0.5 if i < 50 else 0.0)) for i in range(1, 101)]
    st = Q.judge({"x": flat}, steady=True, tol_pct=1.0)[0]
    assert st.ok and st.window == 10 and st.change_pct == 0.0
    wobble = [(float(i), 1.0 + 0.05 * math.sin(i)) for i in range(1, 101)]
    st = Q.judge({"x": wobble}, steady=True, tol_pct=1.0)[0]
    assert st.ok is False and st.change_pct > 5
    # transient: an oscillation with a steady mean passes; a drifting mean does not
    osc = [(i * 0.01, math.sin(i * 0.3) + 2.0) for i in range(400)]
    assert Q.judge({"x": osc}, steady=False, tol_pct=2.0)[0].ok
    drift = [(i * 0.01, 2.0 + i * 0.01) for i in range(400)]
    assert Q.judge({"x": drift}, steady=False, tol_pct=2.0)[0].ok is False
    assert Q.judge({"x": flat[:5]}, steady=True)[0].ok is None


def test_force_components_share_one_scale():
    # Fy ~ 0 next to a large Fx: its tiny wiggle must not count as "not converged"
    fx = [(float(i), 10.0) for i in range(1, 41)]
    fy = [(float(i), 1e-6 * (-1) ** i) for i in range(1, 41)]
    stats = {s.id: s for s in Q.judge({"force:Fx": fx, "force:Fy": fy}, steady=True)}
    assert stats["force:Fy"].ok and stats["force:Fy"].scale == pytest.approx(10.0, rel=1e-6)


# ============================================================================ gates
def _gates(root: Path):
    case = load_case(root)
    f = facts(case)
    qp = Q.plan(case)
    s = Q.read_series(root, qp)
    return case, f, qp, s


def test_convergence_gate_without_monitors_says_how(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    case, f, qp, s = _gates(root)
    g, _ = Q.convergence_gate(case, f, qp, s)
    assert not g.ok and g.severity == "warn" and "Add integral monitors" in g.how


def test_mass_balance_pass_warn_error(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    _flows(root, 30, 2.54e-4, 2.5401e-4)
    g = next(x for x in Q.conservation_gates(*_gates(root)) if x.id == "mass-balance")
    assert g.ok and "closes" in g.title and g.evidence[0].startswith("Σ")
    _flows(root, 30, 2.54e-4, 2.50e-4)
    g = next(x for x in Q.conservation_gates(*_gates(root)) if x.id == "mass-balance")
    assert not g.ok and g.severity == "warn" and "1.57 %" in g.title
    _flows(root, 30, 2.54e-4, 2.0e-4)
    g = next(x for x in Q.conservation_gates(*_gates(root)) if x.id == "mass-balance")
    assert g.blocking and g.severity == "error"


def test_mass_balance_not_audited_when_a_patch_has_no_monitor(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    _dat(root, "aetherFlow_inlet", "0", "surfaceFieldValue.dat",
         SFV_ESI_HEAD.format(p="inlet", col="sum(phi)") + "1\t-1\n")
    g = next(x for x in Q.conservation_gates(*_gates(root)) if x.id == "mass-balance")
    assert g.ok and g.severity == "info" and "outlet" in g.how


def test_energy_balance_closed_box():
    qp = Q.QoIPlan(qois=[Q.QoI("heat:hot", "", "W", "heat", Q.HEAT, ("hot",)),
                         Q.QoI("heat:cold", "", "W", "heat", Q.HEAT, ("cold",))], walls=["hot", "cold"])
    f = type("F", (), {"steady": True, "compressible": True})()
    ok = Q.conservation_gates(None, f, qp, {"heat:hot": [(1.0, 100.0)], "heat:cold": [(1.0, -99.5)]})
    assert next(g for g in ok if g.id == "energy-balance").ok
    bad = Q.conservation_gates(None, f, qp, {"heat:hot": [(1.0, 179.0)], "heat:cold": [(1.0, -59.7)]})
    g = next(g for g in bad if g.id == "energy-balance")
    assert not g.ok and "66.6" in g.title


# ============================================================================ guided workflow wiring
def _ex(root: Path) -> Executor:
    return Executor(default_registry(), Context(Project.find(root)), AutoApprove())


def test_run_stage_adds_monitors_with_residual_monitors_in_one_plan(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    run = next(s for s in guided.assess(root) if s.id == "run")
    q = next(q for q in run.questions if q.id == "qoi_monitors")
    assert q.value is False and "inlet" in q.help and q.writes == ["system/controlDict"]
    gp = guided.plan_answers(root, "run", {"monitoring": True, "qoi_monitors": True})
    assert not gp.errors, gp.errors
    res = _ex(root).execute("dict.batch", gp.batch_params(root), USER)
    assert res.ok, res.message
    text = (root / "system" / "controlDict").read_text()
    from aethercfd.foamdict.parser import parse

    assert parse(text).ok
    assert all(k in text for k in ("aetherFieldMinMax", "aetherForces", "aetherFlow_inlet", "aetherMean_outlet"))
    assert Q.has_monitors(load_case(root))
    run = next(s for s in guided.assess(root) if s.id == "run")
    assert next(q for q in run.questions if q.id == "qoi_monitors").value is True
    # answering again changes nothing
    assert guided.plan_answers(root, "run", {"qoi_monitors": True}).empty


def test_foundation_monitors_go_into_system_functions(tmp_path):
    root = _case(tmp_path, "of14_cavity")
    (root / "system" / "functions").write_text(
        "FoamFile { format ascii; class dictionary; object functions; }\n\n#includeFunc streamlines\n")
    gp = guided.plan_answers(root, "run", {"qoi_monitors": True})
    assert {e["file"].replace("\\", "/").rsplit("/", 2)[-1] for e in gp.edits} == {"functions"}
    assert _ex(root).execute("dict.batch", gp.batch_params(root), USER).ok
    assert 'libs ("libforces.so")' in (root / "system" / "functions").read_text()


def test_foundation_residual_monitors_use_string_libs(tmp_path):
    """Foundation 14 stops with 'wrong token type - expected string' on libs (fieldFunctionObjects) (live run of
    buoyantCavity, 2026-10-26): every monitor written for the Foundation names its library as a string."""
    root = _case(tmp_path, "of14_cavity")
    gp = guided.plan_answers(root, "run", {"monitoring": True, "qoi_monitors": True})
    text = " ".join(str(e["value"]) for e in gp.edits)
    assert 'libs ("libfieldFunctionObjects.so")' in text and 'libs ("libutilityFunctionObjects.so")' in text
    assert "libs (fieldFunctionObjects)" not in text and "libs (utilityFunctionObjects)" not in text
    # and no fieldMinMax ("Unknown function type" in Foundation 14): volFieldValue min/max over all cells
    assert "fieldMinMax" not in text and "cellZone all; " in text and "operation min;" in text


def test_foundation_min_telemetry_feeds_the_health_gate(tmp_path):
    from aethercfd.case import gates as G

    root = _case(tmp_path, "of14_cavity")
    head = "# Selection   : all\n# Cells       : 400\n# Time        \tmin(p)\tmin(k)\tmin(U)\n"
    _dat(root, "aetherMin", "0", "volFieldValue.dat", head + "1\t-3.0\t1e-4\t(0 0 0)\n2\t-3.1\t2e-4\t(0 0 0)\n")
    assert G.health_gate(load_case(root)).ok
    _dat(root, "aetherMin", "0", "volFieldValue.dat", head + "1\t-3.0\t1e-4\t(0 0 0)\n2\t-3.1\t-2e-4\t(0 0 0)\n")
    g = G.health_gate(load_case(root))
    assert g.blocking and "k min -2e-4 < 0 at t=2" in g.evidence[0]
    assert not any(k.startswith("aetherMin") for k in Q.read_series(root))


def test_parser_reads_multiline_includefunc():
    """Foundation 14's pitzDailySteady/system/functions; AetherCFD could not edit it before."""
    from aethercfd.foamdict.parser import parse

    text = ("FoamFile { format ascii; class dictionary; object functions; }\n\n#includeFunc streamlinesLine\n(\n"
            "    name=streamlines,\n    start=(-0.0205 0.001 0.00001),\n    end=(-0.0205 0.0251 0.00001),\n"
            "    nPoints=10,\n    fields=(p k U)\n)\n\n#includeFunc residuals(p, U)\nmine { type x; }\n")
    doc = parse(text)
    assert doc.ok, doc.diagnostics
    dirs = [m for m in doc.root.members if type(m).__name__ == "Directive"]
    assert len(dirs) == 2 and "nPoints=10" in dirs[0].args_text and dirs[1].args_text == "residuals(p, U)"


def test_validation_stage_has_qoi_gates_and_tolerance(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    _flows(root, 40, 2.54e-4, 2.54e-4)
    _dat(root, "aetherForces", "0", "force.dat", FORCE_ESI_HEAD + _esi_force_rows(40))
    val = next(s for s in guided.assess(root) if s.id == "validation")
    ids = {g.id for g in val.gates}
    assert {"qoi-convergence", "mass-balance"} <= ids
    assert next(g for g in val.gates if g.id == "qoi-convergence").ok
    assert any(q.id == "qoi_tolerance" for q in val.questions)
    gp = guided.plan_answers(root, "validation", {"qoi_tolerance": 0.5})
    assert gp.answers == {"qoi_tolerance": 0.5} and not gp.edits  # kept in the sidecar, not in the case


def test_post_stage_offers_the_report(tmp_path):
    root = _case(tmp_path, "esi_pitzDaily")
    post = next(s for s in guided.assess(root) if s.id == "post")
    assert post.actions[0].id == "report.build"


# ============================================================================ report, copilot, CLI
def _ran_case(tmp_path) -> Path:
    """pitzDaily with a result folder and monitor output: steady, converged flows and forces."""
    root = _case(tmp_path, "esi_pitzDaily")
    shutil.copytree(root / "0", root / "100")
    _flows(root, 100, 2.54e-4, 2.5401e-4)
    _dat(root, "aetherForces", "0", "force.dat", FORCE_ESI_HEAD + _esi_force_rows(100, lambda i: 1e-3 + 1e-3 / i))
    return root


def test_report_markdown_json_and_figures(tmp_path):
    from aethercfd.results.report import build, latest

    root = _ran_case(tmp_path)
    rep = build(root, renders=False)
    assert rep.dir.parent == Project.find(root).local / "reports"
    assert latest(root) == rep.markdown
    md = rep.markdown.read_text(encoding="utf-8")
    for h in ("## Verdict", "## Quantities of interest", "## Conservation", "## Workflow stages", "## Provenance"):
        assert h in md
    assert re.search(r"\| Flow rate through outlet \(out \+\) \| 0\.00025401 \| m³/s \| 0 % \| yes \|", md)
    assert "Mass balance closes" in md
    data = json.loads((rep.dir / "report.json").read_text(encoding="utf-8"))
    assert data["verdict"] in ("ready", "not ready") and data["qois"]
    assert {"qoi_forces.png", "qoi_flows.png"} <= {p.name for p in rep.figures}
    assert (rep.dir / "qoi_forces.pdf").is_file() and not (rep.dir / "series.npz").exists()
    assert not any(p.name == "postProcessing" for p in rep.dir.iterdir())
    post = next(s for s in guided.assess(root) if s.id == "post")
    assert any(g.id == "report" and g.ok for g in post.gates)


def test_verdict_is_the_validation_gates():
    from aethercfd.case.gates import Gate, passed
    from aethercfd.results.report import mark, verdict

    assert verdict([passed("run-done", "ok"), passed("mass-balance", "ok")]) == ("ready", [])
    v, why = verdict([passed("run-done", "ok"), Gate("mass-balance", False, "error", "Mass imbalance 9 %", "why")])
    assert v == "not ready" and "Mass imbalance 9 %: why" in why
    assert verdict([Gate("run-done", False, "error", "The run failed", "FOAM FATAL")])[0] == "failed"
    assert mark(passed("x", "t")) == "PASS" and mark(Gate("x", True, "info", "t", "not audited")) == "INFO"


def test_copilot_qoi_status_and_run_report(tmp_path):
    from aethercfd.copilot.tools import ToolContext, ToolSet
    from aethercfd.scripting.api import Session

    root = _ran_case(tmp_path)
    s = Session(root, with_backend=False)
    ts = ToolSet(ToolContext(s, executor=Executor(default_registry(), s.context, AutoApprove())))
    ok, text = ts.call("qoi_status", {})
    d = json.loads(text)
    assert ok and not d["monitored"] and "aetherForces" in d["monitor_objects"]
    assert any(v["id"] == "flow:outlet" and v["ok"] for v in d["values"])
    assert any(g["id"] == "mass-balance" and g["ok"] for g in d["gates"])
    ok, text = ts.call("run_report", {"renders": False})
    d = json.loads(text)
    assert ok and Path(d["markdown"]).is_file() and d["verdict"] in ("ready", "not ready")


def test_cli_report(tmp_path, capsys):
    from aethercfd.scripting.cli import main

    root = _ran_case(tmp_path)
    code = main(["report", str(root), "--no-render", "--out", str(tmp_path / "rep")])
    out = capsys.readouterr().out
    assert code in (0, 3) and "verdict" in out and (tmp_path / "rep" / "report.md").is_file()
    assert "Flow rate through outlet" in out
