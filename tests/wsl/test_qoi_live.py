"""Live integral monitors and the run report (marker wsl_slow): real tutorials from both forks, the guided Run
answers 'monitoring' + 'qoi_monitors' applied as one journalled plan, a real run through run.start, then the
QoI series, the convergence and conservation gates and the report (with ParaView pictures) are checked.
Scratch only."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

import pytest

from aethercfd.backend.wsl.forks import Family, probe_forks
from aethercfd.backend.wsl.runner import WslRunner
from aethercfd.case import guided
from aethercfd.case import qoi as Q
from aethercfd.case.checks import facts
from aethercfd.case.model import load_case
from aethercfd.scripting.api import AutoApprove, Session
from aethercfd.templates.catalog import template
from aethercfd.templates.create import copy_tutorial
from tests.wsl._forks import DISTRO, SCRATCH_ROOT, needs_distro, needs_scratch

pytestmark = [pytest.mark.wsl_slow, pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="no WSL"),
              needs_distro, needs_scratch]
SCRATCH = SCRATCH_ROOT / "qoi"


@pytest.fixture(scope="module")
def forks():
    r = WslRunner(DISTRO)
    return r, {f.family: f for f in probe_forks(r)}


def _guided_run(forks, family: Family, tid: str, edits: dict, timeout: float) -> Path:
    runner, fk = forks
    fork = fk[family]
    dest = SCRATCH / f"{tid}_{family.value}_{uuid.uuid4().hex[:6]}"
    copy_tutorial(runner, fork, template(tid), dest)
    s = Session(dest, create_project=True, broker=AutoApprove())
    for k, v in edits.items():
        assert s.set("system/controlDict", k, v).ok
    gp = guided.plan_answers(dest, "run", {"monitoring": True, "qoi_monitors": True})
    assert not gp.errors, gp.errors
    res = s.do("dict.batch", **gp.batch_params(dest))
    assert res.ok, res.message
    rid = s.run(preset="auto", fork=fork.id)
    rec = s.runs.wait(rid, timeout=timeout)
    if rec is None or rec.state != "finished":
        s.runs.control(rid, "kill")
        pytest.fail(f"run {rid}: {getattr(rec, 'state', 'timeout')}")
    log = "\n".join(p.read_text(errors="replace") for p in dest.glob("log.*"))
    assert re.search(r"^Time = \S+", log, re.M), "the solver never stepped"
    for bad in ("FOAM FATAL", "Unknown function type", "the latter will be ignored", "Cannot find patch",
                "Unable to find field"):
        assert bad not in log, bad
    return dest


@pytest.mark.parametrize("family", [Family.ESI, Family.FOUNDATION])
def test_pitzdaily_monitors_gates_and_report(forks, family):
    root = _guided_run(forks, family, "pitzDaily", {"endTime": "300", "writeInterval": "100"}, 900)
    case = load_case(root)
    f = facts(case)
    qp = Q.plan(case)
    s = Q.read_series(root, qp)
    assert {"force:Fx", "force:Fy", "flow:inlet", "flow:outlet", "mean:p:inlet", "mean:p:outlet", "dp"} <= set(s)
    assert len(s["flow:outlet"]) >= 250
    gate, stats = Q.convergence_gate(case, f, qp, s)
    assert gate.id == "qoi-convergence" and any(st.id == "dp" and st.ok is not None for st in stats)
    mass = next(x for x in Q.conservation_gates(case, f, qp, s) if x.id == "mass-balance")
    assert mass.ok, mass.evidence  # incompressible steady: Σ phi closes to round-off after 300 iterations
    from aethercfd.results.report import build

    rep = build(root)
    names = {p.name for p in rep.figures}
    assert {"qoi_forces.png", "qoi_flows.png", "qoi_pressure.png", "residuals.png", "field_p.png",
            "field_U.png"} <= names, rep.notes
    assert rep.data["verdict"] in ("ready", "not ready")
    assert "## Fields at t = " in rep.markdown.read_text(encoding="utf-8")


@pytest.mark.parametrize("family", [Family.ESI, Family.FOUNDATION])
def test_buoyant_cavity_wall_heat_flow_and_energy_balance(forks, family):
    root = _guided_run(forks, family, "buoyantCavity", {"endTime": "60", "writeInterval": "30"}, 1500)
    case = load_case(root)
    qp = Q.plan(case)
    assert Q.HEAT in qp.objects and Q.FORCES not in qp.objects  # closed box with still walls: no force QoI
    s = Q.read_series(root, qp)
    assert len(s["heat:hot"]) >= 50 and s["heat:hot"][-1][1] > 0 > s["heat:cold"][-1][1]
    gates = Q.conservation_gates(case, facts(case), qp, s)
    assert next(g for g in gates if g.id == "mass-balance").title.startswith("Mass balance: closed domain")
    assert any(g.id == "energy-balance" for g in gates)  # 60 iterations: far from balanced, but judged
