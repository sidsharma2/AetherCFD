"""CAD intake: STEP product names; a live FreeCAD conversion when FreeCAD is installed (a few seconds)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aethercfd.mesh.cad import _step_products, cad_to_stl, find_freecad

STEP_HEAD = """ISO-10303-21;
DATA;
#1=PRODUCT('asm','asm','',(#9));
#2=PRODUCT('Duct','Duct','',(#9));
#3=PRODUCT('Probe','Probe','',(#9));
#4=NEXT_ASSEMBLY_USAGE_OCCURRENCE('1','','',#1,#2,$);
ENDSEC;
"""


def test_step_products_and_format_check(tmp_path):
    f = tmp_path / "a.step"
    f.write_text(STEP_HEAD)
    assert _step_products(f) == ["Duct", "Probe"]
    r = cad_to_stl(tmp_path / "x.dwg", tmp_path / "x.stl")
    assert not r.ok and "not a CAD format" in r.notes[0]


@pytest.mark.skipif(find_freecad() is None, reason="FreeCAD not installed")
def test_freecad_step_to_region_stl(tmp_path):
    script = tmp_path / "mk.py"
    script.write_text(
        "import os, FreeCAD, Part, Import\n"
        "doc = FreeCAD.newDocument('t')\n"
        "b = doc.addObject('Part::Box', 'Duct'); b.Length, b.Width, b.Height = 100, 20, 20\n"
        "c = doc.addObject('Part::Cylinder', 'Probe'); c.Radius, c.Height = 3, 30\n"
        "c.Placement = FreeCAD.Placement(FreeCAD.Vector(150, 10, 0), FreeCAD.Rotation())\n"
        "doc.recompute(); Import.export([b, c], os.environ['OUT'])\n")
    step = tmp_path / "parts.step"
    subprocess.run([str(find_freecad()), str(script)], env={**os.environ, "OUT": str(step)}, capture_output=True,
                   timeout=300, creationflags=0x08000000)
    assert step.is_file()
    r = cad_to_stl(step, tmp_path / "parts.stl")
    assert r.ok, r.log
    assert [(g["name"], g["closed"]) for g in r.regions] == [("Duct", True), ("Probe", True)]
    assert r.regions[0]["volume_m3"] == pytest.approx(4e-5, rel=1e-6)
    assert r.bbox_m[1] == pytest.approx(0.153)
    from aethercfd.mesh.snappy import stl_solids

    assert stl_solids(Path(r.stl)) == ["Duct", "Probe"]
