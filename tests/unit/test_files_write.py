"""files.write: several files (text + binary copies) as one approvable, journalled, revertable plan."""

from __future__ import annotations

import shutil
from pathlib import Path

from aethercfd.commands.base import USER
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def test_files_write_and_revert(tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    s = Session(case, create_project=True, with_backend=False)
    src = tmp_path / "part.stl"
    src.write_bytes(b"\x00binary stl\x00" * 10)
    before_cd = (case / "system" / "controlDict").read_bytes()
    res = s.executor.execute("files.write", {
        "root": str(case),
        "files": {"system/snappyHexMeshDict": "castellatedMesh true;\n", "system/controlDict": "endTime 1;\n"},
        "copies": {"constant/triSurface/part.stl": str(src)}, "keep_existing": ["system/controlDict"]}, USER)
    assert res.ok, res.message
    assert (case / "system" / "snappyHexMeshDict").read_text() == "castellatedMesh true;\n"
    assert (case / "constant" / "triSurface" / "part.stl").read_bytes() == src.read_bytes()
    assert (case / "system" / "controlDict").read_bytes() == before_cd  # kept: it already existed
    assert len(res.plan.file_changes) == 2
    rev = s.executor.execute("file.revert", {"journal_id": res.journal_id}, USER)
    assert rev.ok and not (case / "system" / "snappyHexMeshDict").exists()
    # paths must stay inside the root
    bad = s.executor.execute("files.write", {"root": str(case), "files": {"../evil.txt": "x"}}, USER)
    assert not bad.ok and "outside" in bad.message
