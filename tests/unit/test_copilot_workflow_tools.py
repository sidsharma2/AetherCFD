"""The copilot drives the guided workflow through the same plans and gates as the GUI."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from aethercfd.commands.executor import AutoApprove, DenyAll, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.copilot.tools import ToolContext, ToolSet
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_cfd_agents"
J = {"rationale": "guided step", "confidence": 0.9}


def _ts(tmp_path, name="esi_pitzDaily", broker=None) -> ToolSet:
    shutil.copytree(FIX / name, tmp_path / "c")
    s = Session(tmp_path / "c", with_backend=False, create_project=True)
    ex = Executor(default_registry(), s.context, broker or AutoApprove())
    return ToolSet(ToolContext(s, executor=ex))


def test_state_stage_and_answer(tmp_path):
    ts = _ts(tmp_path)
    assert {"workflow_state", "workflow_stage", "workflow_answer", "workflow_fix", "workflow_mark_done",
            "dict_batch", "cfd_agents_status", "cfd_agent_run"} <= set(ts.tools)
    ok, text = ts.call("workflow_state", {})
    d = json.loads(text)
    assert ok and len(d["stages"]) == 12 and d["current_stage"] == "domain"
    ok, text = ts.call("workflow_stage", {"stage": "bc"})
    st = json.loads(text)
    assert ok and any(q["id"] == "inlet_U:inlet" for q in st["questions"])
    ok, text = ts.call("workflow_answer", {"stage": "bc", "answers": {"inlet_U:inlet": 15}, **J})
    out = json.loads(text)
    assert ok, out
    assert out["journal_id"] and "|U| 10 → 15" in out["changes"][0]
    assert "uniform (15 0 0)" in (tmp_path / "c" / "0" / "U").read_text()
    ok, text = ts.call("workflow_stage", {"stage": "nope"})
    assert not ok and "unknown stage" in text


def test_mark_done_is_refused_while_gates_fail(tmp_path):
    ts = _ts(tmp_path)
    ok, text = ts.call("workflow_mark_done", {"stage": "mesh", **J})
    assert not ok and "No mesh yet" in text
    ok, text = ts.call("workflow_mark_done", {"stage": "materials", **J})
    assert ok, text
    d = json.loads(ts.call("workflow_state", {})[1])
    assert next(s for s in d["stages"] if s["id"] == "materials")["status"] == "done"


def test_fix_and_batch_need_approval(tmp_path):
    ts = _ts(tmp_path, "esi_cavity", broker=DenyAll())
    sol = tmp_path / "c" / "system" / "fvSolution"
    sol.write_text(sol.read_text().replace("    pRefCell        0;\n    pRefValue       0;\n", ""))
    ok, text = ts.call("workflow_fix", {"stage": "bc", "gate": "p-reference", **J})
    out = json.loads(text)
    assert not ok and "not approved" in out["message"] and "pRefCell" not in sol.read_text()
    ok, text = ts.call("dict_batch", {"edits": [{"file": "system/controlDict", "key": "endTime", "value": "1"}],
                                      "rationale": "x", "confidence": 0.5})
    assert not ok and "not approved" in text


def test_agent_tools_with_a_fake_cfd_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CFD_AGENTS_PYTHON", sys.executable)
    monkeypatch.setenv("AETHERCFD_CFD_AGENTS_ROOT", str(FAKE))
    ts = _ts(tmp_path, "of14_cavity")
    ok, text = ts.call("cfd_agents_status", {})
    d = json.loads(text)
    assert ok and d["agents"]["murphy"]["available"] and not d["agents"]["larry"]["integrated"]
    ok, text = ts.call("cfd_agent_run", {"agent": "murphy"})
    assert ok and json.loads(text)["agent"] == "murphy"
    ok, text = ts.call("cfd_agent_run", {"agent": "bill"})
    assert not ok and "not called from AetherCFD" in text
    ok, text = ts.call("cfd_agent_run", {"agent": "reggie"})  # no checkMesh log yet
    assert not ok and "checkMesh" in text
