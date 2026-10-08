"""Physics code templates and the code.create / code.build commands (no WSL)."""

from __future__ import annotations

import shutil
from pathlib import Path

from aethercfd.codegen.build import parse_diagnostics
from aethercfd.codegen.templates import TEMPLATES, case_entries, render, template
from aethercfd.commands.base import USER
from aethercfd.scripting.api import Session

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "cases" / "esi_cavity"


def test_pope_template_renders_consistently():
    files = render(template("kEpsilonPope"), "myPope")
    assert set(files) == {"myPope.H", "myPope.C", "makemyPopeCompressible.C", "makemyPopeIncompressible.C",
                          "Make/files", "Make/options"}
    assert 'TypeName("myPope")' in files["myPope.H"] and "makeRASModel(myPope);" in files["makemyPopeCompressible.C"]
    assert "dev(symm(gradU))" in files["myPope.C"] and "chiMax" in files["myPope.C"]
    assert "libaethermyPope" in files["Make/files"]
    assert ("system/controlDict", "libs", '("libaethermyPope.so")') in case_entries(template("kEpsilonPope"),
                                                                                    "myPope")
    assert all("{NAME}" not in v for t in TEMPLATES for v in render(t, "X").values())


def test_code_create_command_and_build_preconditions(tmp_path):
    case = tmp_path / "c"
    shutil.copytree(FIX, case)
    s = Session(case, create_project=True, with_backend=False)
    dest = case / "aether-code" / "pope"
    res = s.executor.execute("code.create", {"template": "kEpsilonPope", "name": "pope", "dest": str(dest)}, USER)
    assert res.ok, res.message
    assert (dest / "pope.C").exists() and (dest / "Make" / "options").exists()
    assert next(s.project.journal.entries(newest_first=True))["command"] == "code.create"
    bad = s.executor.execute("code.create", {"template": "kEpsilonPope", "name": "1bad", "dest": str(dest)}, USER)
    assert not bad.ok and "identifier" in bad.message
    plan = s.executor.plan("code.build", {"dir": str(dest)}, USER)
    assert plan.blocked and "backend" in plan.blocked[0].message  # no WSL in this unit test


def test_compiler_diagnostics_are_mapped():
    out = ("pope.C:42:13: error: 'foo' was not declared in this scope\n"
           "/mnt/d/x/aether-code/pope/pope.H:10:5: warning: unused variable 'y'\n")
    d = parse_diagnostics(out, "/mnt/d/x/aether-code/pope")
    assert [(x.severity, x.line) for x in d] == [("error", 42), ("warning", 10)]
    assert d[0].file.replace("\\", "/").lower().endswith("d:/x/aether-code/pope/pope.c")
