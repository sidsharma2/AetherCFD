"""Copilot AetherMesh tools on the fixture case."""

from __future__ import annotations

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
    ok, text = ts.call("grading_design", {"length": 47.5, "first": 0.125, "last": 0.125, "max_size": 0.3,
                                          "max_ratio": 1.1})
    d = json.loads(text)
    assert ok and d["max_cell_to_cell_ratio"] <= 1.1 and d["compare_single_ratio"]["expansion"] == 1.0
    ok, text = ts.call("wall_spacing", {"yplus": 1, "U": 190, "L": 0.002159, "kind": "pipe", "T": 1339,
                                        "p": 3.7e5, "core_cell": 2e-4})
    w = json.loads(text)
    assert ok and 5e3 < w["reynolds"] < 2e4 and 2e-6 < w["first_cell"] < 3e-5 and w["layers"]["n"] > 5
