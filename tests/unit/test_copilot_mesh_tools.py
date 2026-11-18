"""Copilot AetherMesh tools on the fixture case."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from aethercfd.copilot.tools import ToolContext, ToolSet
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def _ts(tmp_path) -> ToolSet:
    shutil.copytree(FIX, tmp_path / "c")
    return ToolSet(ToolContext(Session(tmp_path / "c", with_backend=False)))


def test_mesh_hotspots_on_cavity(tmp_path):
    ok, text = _ts(tmp_path).call("mesh_hotspots", {})
    assert ok, text
    d = json.loads(text)
    assert d["summary"]["cells"] == 400 and d["summary"]["non_ortho_max"] < 1e-6
    assert all(v == [] for v in d["hotspots"].values())
    assert d["bounding_box"]["max"][0] == 100.0  # mm


def test_mesh_hotspots_missing_region(tmp_path):
    ok, text = _ts(tmp_path).call("mesh_hotspots", {"region": "fluid"})
    assert not ok and "no polyMesh" in text


def test_grading_and_wall_tools(tmp_path):
    ts = _ts(tmp_path)
    ok, text = ts.call("grading_design", {"length": 50.0, "first": 0.125, "last": 0.125, "max_size": 0.3,
                                          "max_ratio": 1.1})
    d = json.loads(text)
    assert ok and d["max_cell_to_cell_ratio"] <= 1.1 and d["compare_single_ratio"]["expansion"] == 1.0
    ok, text = ts.call("wall_spacing", {"yplus": 1, "U": 200, "L": 0.002, "kind": "pipe", "T": 1300,
                                        "p": 4e5, "core_cell": 2e-4})
    w = json.loads(text)
    assert ok and 5e3 < w["reynolds"] < 2e4 and 2e-6 < w["first_cell"] < 3e-5 and w["layers"]["n"] > 5


def test_checkmesh_region_mapped_lookup_note():
    from aethercfd.copilot.tools import log_note

    text = ("Checking geometry...\n    Max aspect ratio = 19.2 OK.\n ***Cells with small determinant (< 0.001) found\n"
            "    Face volume ratio check OK.\n\n--> FOAM FATAL ERROR: (openfoam-2306 patch=260127)\n\n"
            "    failed lookup of solid_1_collar (objectRegistry mesh_g2b)\n")
    note = log_note("log.checkMesh", text)
    assert note and "solid_1_collar" in note and "1 '***'" in note
    assert log_note("log.blockMesh", text) is None
    assert log_note("log.checkMesh", "Checking geometry...\n    Mesh OK.\n") is None


def test_v2606_empty_probe_locations_note():
    from aethercfd.copilot.tools import log_note

    # seen 2026-11-02: backwardFacingStep2D's sampleCp (patchProbes, probeLocations ()) under v2606
    text = ("--> FOAM FATAL IO ERROR: (openfoam-2606)\nEmpty 'probeLocations' list.\n\n"
            "file: system/controlDict/functions/sampleCp at line 1 to 7.\n")
    note = log_note("log.simpleFoam", text)
    assert note and "v2606" in note and "#includeFunc" in note


def test_snappy_setup_tool_writes_one_plan(tmp_path):
    from tests.unit.test_mesh_snappy import _sphere

    from aethercfd.commands.executor import Executor
    from aethercfd.commands.registry import default_registry
    from aethercfd.copilot.headless import PolicyApprover

    shutil.copytree(FIX, tmp_path / "c")
    s = Session(tmp_path / "c", create_project=True, with_backend=False)
    ex = Executor(default_registry(), s.context, PolicyApprover("edits", out=io.StringIO()))
    ts = ToolSet(ToolContext(s, executor=ex))
    _sphere(tmp_path / "c" / "ball.stl")
    ok, text = ts.call("snappy_setup", {"stl": "ball.stl", "target_cells": 100_000, "n_layers": 3,
                                        "rationale": "mesh the ball", "confidence": 0.8})
    assert ok, text
    out = json.loads(text)
    case = tmp_path / "c"
    assert (case / "system" / "snappyHexMeshDict").is_file() and (case / "constant" / "triSurface" / "ball.stl").is_file()
    assert "snappyHexMesh -overwrite" in out["next"] and out["journal_id"]
    assert (case / "system" / "controlDict").read_text() == (FIX / "system" / "controlDict").read_text()  # kept
