"""The out-of-process cfd_agents adapter, against a fake cfd_agents package run in a real subprocess."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from aethercfd.services.cfd_agents import CATALOG, CfdAgents, explain_import_error, reggie_suggestions

FAKE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_cfd_agents"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases"


@pytest.fixture
def agents(tmp_path):
    return CfdAgents(sys.executable, FAKE, work_dir=tmp_path / "work", timeout=60)


def test_probe_reports_each_agent_and_loads_around_a_broken_package_init(agents):
    av = agents.probe()
    assert av.ok, av.problems
    for a in ("murphy", "connie", "reggie", "darwin"):
        assert av.available(a), av.agents
    # agents/__init__ imports 'nooa' (absent): connie/reggie are loaded without it, and say so
    assert "without cfd_agents.agents/__init__" in av.agents["connie"]["note"]
    d = av.as_dict()
    assert d["agents"]["bill"]["integrated"] is False and "deletes every time folder" in d["agents"]["bill"]["why"]
    assert set(d["agents"]) == set(CATALOG)


def test_missing_interpreter_and_package_are_explained(tmp_path):
    a = CfdAgents(tmp_path / "nope" / "python.exe", tmp_path, work_dir=tmp_path / "w")
    av = a.probe()
    assert not av.ok and len(av.problems) == 2
    assert "cfd_agents_python" in av.problems[0] and "cfd_agents_root" in av.problems[1]
    r = a.murphy(FIX / "of14_cavity" / "log.foamRun")
    assert not r.ok and "not available" in r.explanation and "interpreter" in r.explanation


def test_broken_environment_is_explained_not_raised(tmp_path):
    root = tmp_path / "broken"
    (root / "cfd_agents").mkdir(parents=True)
    (root / "cfd_agents" / "__init__.py").write_text("import nooa_missing_dependency\n")
    av = CfdAgents(sys.executable, root, work_dir=tmp_path / "w").probe()
    assert not av.ok and "does not import" in av.problems[0]
    assert "nooa" in explain_import_error("murphy", "ModuleNotFoundError: No module named 'nooa'")


def test_murphy_diagnoses_a_failed_log_and_reads_success_from_the_log(agents, tmp_path):
    bad = tmp_path / "log.simpleFoam"
    bad.write_text("Time = 1\n\n#0  Foam::error::printStack\nFloating point exception (core dumped)\n")
    r = agents.murphy(bad)
    assert r.ok and "fp_exception" in r.explanation and "reduce relaxation" in r.explanation
    assert "reduce dt" not in r.explanation  # False hints are dropped
    assert r.suggestions and not r.suggestions[0]["edits"]
    good = agents.murphy(FIX / "of14_cavity" / "log.foamRun")
    assert good.ok  # the finished log is passed as success; the fake says unknown/no_convergence
    assert good.argv[1:3] == ["-I", "-B"]


def test_connie_needs_telemetry_then_reports_health(agents, tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    r = agents.connie(case)
    assert not r.ok and "monitoring function objects" in r.explanation
    d = case / "postProcessing" / "aetherFieldMinMax" / "0"
    d.mkdir(parents=True)
    (d / "fieldMinMax.dat").write_text("# Time field min max\n1 k -0.1 2\n")
    r = agents.connie(case)
    assert r.ok and "failed" in r.explanation and r.data["policy_growth"] == 2.0


def test_reggie_suggestions_map_to_the_right_dictionaries(agents):
    r = agents.reggie(68.4, 1e-4, 0.8)
    assert r.ok
    edits = r.suggestions[0]["edits"]
    by_key = {e["key"]: e for e in edits}
    assert by_key["ddtSchemes/default"]["file"] == "system/fvSchemes"
    assert by_key["PIMPLE/nNonOrthogonalCorrectors"]["value"] == "1"
    assert by_key["maxCo"]["file"] == "system/controlDict" and by_key["maxCo"]["value"] == "0.8"
    assert by_key["adjustTimeStep"]["file"] == "system/controlDict"
    assert by_key["relaxationFactors/fields/p"]["value"] == "0.3"
    assert by_key["relaxationFactors/equations/U"]["value"] == "0.7"
    assert by_key["deltaT"]["value"] == "0.0001"
    assert reggie_suggestions({}) == []


def test_darwin_writes_only_outside_the_case(agents, tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX / "esi_cavity", case)
    before = sorted(p.relative_to(case) for p in case.rglob("*"))
    r = agents.darwin(case, "0", diameter_m=0.0072, velocity_jump_m_s=20.8)
    assert r.ok, r.explanation
    out = Path(r.data["_out_dir"])
    assert (out / "refinement_plan.json").is_file() and not out.is_relative_to(case)
    assert sorted(p.relative_to(case) for p in case.rglob("*")) == before
    assert "nothing was refined" in r.explanation
    empty = tmp_path / "nomesh"
    (empty / "system").mkdir(parents=True)
    r = agents.darwin(empty, "0", diameter_m=0.01, velocity_jump_m_s=10)
    assert not r.ok and "all-tet" in r.explanation
