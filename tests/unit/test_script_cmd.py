"""script.run: confinement, output capture, journal."""

from __future__ import annotations

import shutil
from pathlib import Path

from aethercfd.commands.base import USER
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def test_script_run_in_case(tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    s = Session(case, create_project=True, with_backend=False)
    sc = case / "tools" / "gen.py"
    sc.parent.mkdir()
    sc.write_text("import sys, pathlib\npathlib.Path('out.txt').write_text('ok ' + ' '.join(sys.argv[1:]))\n"
                  "print('generated')\n", encoding="utf-8")
    res = s.executor.execute("script.run", {"path": str(sc), "args": ["a", "b"], "case": str(case)}, USER)
    assert res.ok and "generated" in res.data["stdout"]
    assert (sc.parent / "out.txt").read_text() == "ok a b"
    outside = tmp_path / "evil.py"
    outside.write_text("print(1)\n")
    bad = s.executor.execute("script.run", {"path": str(outside), "case": str(case)}, USER)
    assert not bad.ok and "outside" in bad.message
