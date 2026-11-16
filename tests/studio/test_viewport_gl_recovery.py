"""The 3D viewport keeps drawing after perspective switches (problem: a viewport whose GL context pyvistaqt lost
track of rendered blank for good; see studio/panels/viewport/interactor.py).

The scenario runs in its own process with the app's real bootstrap (tests/studio/viewport_gl_scenario.py):
it switches Setup -> Meshing -> Post -> Setup ... as a user does from the Window menu, walks the docks the way
the Visualization panel and the layout snapshot do, and after each switch checks every shown viewport: the
render window is current after MakeCurrent, a screenshot is not blank and the widget's own frame is not blank.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyvistaqt")

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "cases" / "esi_multiRegionHeater"


def test_viewports_keep_drawing_after_perspective_switches(tmp_path):
    case = tmp_path / "heater"
    shutil.copytree(FIX, case)
    env = {**os.environ, "AETHERCFD_CONFIG_DIR": str(tmp_path / "cfg"), "AETHERCFD_CACHE_DIR": str(tmp_path / "cache"),
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT), os.environ.get("PYTHONPATH", "")]),
           "PYTHONUNBUFFERED": "1"}
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name("viewport_gl_scenario.py")), str(case)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    rows = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    steps = [r for r in rows if r.get("viewport")]
    assert rows and rows[-1].get("failures") == 0, "\n".join(map(json.dumps, steps)) + "\n" + proc.stderr[-3000:]
    # every perspective with a viewport was drawn, the revisited ones too
    assert {r["step"] for r in steps} == {"start", "meshing", "post", "setup"} and len(steps) == 8
    assert all(r["ok"] and r["current"] for r in steps), steps
    assert proc.returncode == 0
