from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aethercfd.commands.base import USER, Actor, ActorKind, Risk
from aethercfd.commands.executor import AutoApprove, Context, DenyAll, Executor
from aethercfd.commands.registry import default_registry
from aethercfd.project.project import Project

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"
AGENT = Actor(ActorKind.AGENT, "copilot")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERCFD_CONFIG_DIR", str(tmp_path / "cfg"))
    case = tmp_path / "cavity"
    shutil.copytree(FIX / "esi_cavity", case)
    proj = Project.create(case)
    ex = Executor(default_registry(), Context(proj), AutoApprove())
    return case, proj, ex


def test_dict_set_plan_apply_journal_revert(setup):
    case, proj, ex = setup
    cd = case / "system" / "controlDict"
    original = cd.read_bytes()
    plan = ex.plan("dict.set", {"file": str(cd), "key": "endTime", "value": "1"}, USER)
    assert plan.risk is Risk.EDIT and not plan.blocked
    [fc] = plan.file_changes
    minus = [ln for ln in fc.diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    plus = [ln for ln in fc.diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert len(minus) == 1 and len(plus) == 1 and "endTime" in plus[0]
    res = ex.execute("dict.set", {}, USER, plan=plan)
    assert res.ok and res.journal_id
    assert cd.read_bytes() != original
    entry = proj.journal.find(res.journal_id)
    assert entry["command"] == "dict.set" and entry["files"][0]["before"] and entry["actor"]["kind"] == "user"
    rev = ex.execute("file.revert", {"journal_id": res.journal_id}, USER)
    assert rev.ok, rev.message
    assert cd.read_bytes() == original  # byte-exact undo


def test_conflict_between_plan_and_apply(setup):
    case, _proj, ex = setup
    cd = case / "system" / "controlDict"
    plan = ex.plan("dict.set", {"file": str(cd), "key": "endTime", "value": "1"}, USER)
    cd.write_bytes(cd.read_bytes() + b"\n// edited in VS Code\n")
    res = ex.execute("dict.set", {}, USER, plan=plan)
    assert not res.ok and "changed since the plan" in res.message


def test_agent_must_justify_and_be_approved(setup):
    case, proj, _ex = setup
    cd = str(case / "system" / "controlDict")
    params = {"file": cd, "key": "deltaT", "value": "0.002"}
    strict = Executor(default_registry(), Context(proj), DenyAll())
    assert "rationale" in strict.execute("dict.set", params, AGENT).message
    res = strict.execute("dict.set", params, AGENT, rationale="Co>1 at the lid corner", confidence=0.8)
    assert not res.ok and "not approved" in res.message
    ok = Executor(default_registry(), Context(proj), AutoApprove()).execute(
        "dict.set", params, AGENT, rationale="Co>1 at the lid corner", confidence=0.8)
    assert ok.ok and proj.journal.find(ok.journal_id)["rationale"] == "Co>1 at the lid corner"


def test_approval_is_bound_to_plan_hash(setup):
    case, _proj, ex = setup
    cd = str(case / "system" / "controlDict")
    plan = ex.plan("dict.set", {"file": cd, "key": "endTime", "value": "1"}, AGENT, rationale="r", confidence=0.5)
    other = ex.plan("dict.set", {"file": cd, "key": "endTime", "value": "2"}, AGENT, rationale="r", confidence=0.5)
    res = ex.execute("dict.set", {}, AGENT, plan=other, approved_hash=plan.plan_hash)
    assert not res.ok and "no longer matches" in res.message


def test_save_detects_external_change_and_keeps_encoding(setup):
    case, _proj, ex = setup
    fv = case / "system" / "fvSchemes"
    fv.write_bytes(fv.read_bytes().replace(b"\n", b"\r\n"))  # a CRLF file
    stale = "0" * 64
    res = ex.execute("file.save", {"path": str(fv), "text": "x", "expected_sha": stale}, USER)
    assert not res.ok and "changed on disk" in res.message
    text = fv.read_bytes().decode()
    res = ex.execute("file.save", {"path": str(fv), "text": text.replace("Euler", "backward")}, USER)
    assert res.ok and b"\r\n" in fv.read_bytes() and b"backward" in fv.read_bytes()


def test_refusals_surface_as_blocked_preconditions(setup):
    case, _proj, ex = setup
    bad = case / "system" / "broken"
    bad.write_text("FoamFile { object broken; }\na { b 1;\n", encoding="utf-8")
    res = ex.execute("dict.set", {"file": str(bad), "key": "a/b", "value": "2"}, USER)
    assert not res.ok and "syntax error at line" in res.message
    res = ex.execute("dict.set", {"file": str(case / "system" / "controlDict"), "key": "nope", "value": "1"}, USER)
    assert not res.ok and "not set in this file" in res.message


def test_destructive_needs_approval_even_for_users(setup):
    case, proj, _ex = setup
    strict = Executor(default_registry(), Context(proj), DenyAll())
    res = strict.execute("file.trash", {"path": str(case / "system" / "PDRblockMeshDict")}, USER)
    assert not res.ok and (case / "system" / "PDRblockMeshDict").exists()
    ok = Executor(default_registry(), Context(proj), AutoApprove()).execute(
        "file.trash", {"path": str(case / "system" / "PDRblockMeshDict")}, USER)
    assert ok.ok and not (case / "system" / "PDRblockMeshDict").exists()
    assert any(p.name.endswith("PDRblockMeshDict") for p in proj.trash_dir.iterdir())


def test_schema_export_for_mcp():
    schemas = {s["name"]: s for s in default_registry().export_json_schema()}
    assert {"dict.set", "dict.insert", "dict.remove", "file.save", "file.revert", "file.trash"} <= set(schemas)
    assert "key" in schemas["dict.set"]["input_schema"]["properties"]
